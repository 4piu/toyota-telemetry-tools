"""Build a bounded, privacy-aware direct-DID validation plan from a catalog."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

from .project import atomic_json, load_json, project_root, resolve_path


def text_has(text: str, words: tuple[str, ...] | list[str]) -> bool:
    lowered = text.lower()
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", lowered)
        for word in words
    )


def validate_config(config: dict[str, Any]) -> None:
    """Validate a multi-ECU validation profile.

    ECUs behind one gateway share a catalog, selection rules, tier policy, and
    backend, so those are declared once and each entry in ``ecus`` adds only its
    endpoint and paths.
    """
    if config.get("schema_version") != 2:
        raise ValueError("only validation-profile schema 2 is supported")
    for field in ("profile_id", "transport", "catalog", "selection", "policies", "ecus"):
        if field not in config:
            raise ValueError(f"validation profile is missing {field!r}")

    transport = config["transport"]
    if not isinstance(transport.get("bitrate"), int):
        raise ValueError("transport.bitrate must be an integer")

    policies = config["policies"]
    if not isinstance(policies, dict) or not policies:
        raise ValueError("policies must be a non-empty object")
    for tier, policy in policies.items():
        for field in ("period_ms", "duration_seconds", "minimum_samples_per_did"):
            if not isinstance(policy.get(field), int) or policy[field] < 1:
                raise ValueError(f"policy {tier!r} has invalid {field!r}")

    ecus = config["ecus"]
    if not isinstance(ecus, list) or not ecus:
        raise ValueError("'ecus' must be a non-empty array")
    seen_keys: set[int] = set()
    seen_roots: dict[PurePosixPath, int] = {}
    for index, ecu in enumerate(ecus):
        where = f"ecus[{index}]"
        for field in ("key", "plan_id", "tester_can_id", "response_can_id", "paths"):
            if field not in ecu:
                raise ValueError(f"{where} is missing {field!r}")
        key = ecu["key"]
        if not isinstance(key, int) or key in seen_keys:
            raise ValueError(f"{where}.key must be a unique integer")
        seen_keys.add(key)
        for field in ("plan", "capture_root"):
            if not ecu["paths"].get(field):
                raise ValueError(f"{where}.paths is missing {field!r}")
        # Progress is reconstructed by walking a capture root recursively and
        # filtering on profile_id, which every ECU in a profile shares. Two ECUs
        # must therefore not share a root, and neither may sit inside another:
        # a parent would silently absorb its child's captures and report them as
        # its own progress.
        root = PurePosixPath(str(ecu["paths"]["capture_root"]).replace("\\", "/"))
        for other, other_key in seen_roots.items():
            if root == other:
                raise ValueError(
                    f"{where} shares capture_root {root} with ECU {other_key}; "
                    "give each its own"
                )
            if other in root.parents or root in other.parents:
                inner, outer = (root, other) if other in root.parents else (other, root)
                raise ValueError(
                    f"{where}: capture root {inner} sits inside {outer}. Progress is "
                    "discovered recursively, so the outer ECU would count the inner "
                    "one's captures as its own. Make them siblings."
                )
        seen_roots[root] = key


def ecu_views(config: dict[str, Any], keys: list[int] | None = None) -> list[dict[str, Any]]:
    """Expand a profile into one self-contained view per ECU.

    Each view carries the shared settings alongside that ECU's endpoint and
    paths, so everything downstream works with a single ECU at a time without
    knowing whether the file described one or several.
    """
    shared = {k: v for k, v in config.items() if k not in ("ecus", "transport")}
    transport = config["transport"]
    wanted = set(keys) if keys else None
    views: list[dict[str, Any]] = []
    for ecu in config["ecus"]:
        if wanted is not None and ecu["key"] not in wanted:
            continue
        paths = dict(ecu["paths"])
        paths.setdefault("output_root", str(Path(paths["capture_root"]) / "progressive"))
        views.append({
            **shared,
            "plan_id": ecu["plan_id"],
            "ecu_name": ecu.get("name", ""),
            "endpoint": {
                "ecu_key": ecu["key"],
                "tester_can_id": ecu["tester_can_id"],
                "response_can_id": ecu["response_can_id"],
                "bitrate": transport["bitrate"],
                "addressing": transport.get("addressing", "normal"),
            },
            "paths": paths,
        })
    if wanted is not None and len(views) != len(wanted):
        found = {v["endpoint"]["ecu_key"] for v in views}
        raise ValueError(f"unknown ECU key(s): {sorted(wanted - found)}")
    return views


def classify_tier(
    names: list[str], privacy_sensitive: bool, selection: dict[str, Any], policies: dict[str, Any]
) -> str:
    tier_words = selection.get("tier_words", {})
    archive = tuple(tier_words.get("archive", []))
    slow = tuple(tier_words.get("slow", []))
    fast = tuple(tier_words.get("fast", []))
    joined = "; ".join(names)
    selected = "normal"
    if privacy_sensitive or (names and archive and all(text_has(name, archive) for name in names)):
        selected = "archive"
    elif fast and text_has(joined, fast):
        selected = "fast"
    elif (slow and text_has(joined, slow)) or (archive and text_has(joined, archive)):
        selected = "slow"
    if selected not in policies:
        selected = str(selection.get("default_tier", next(iter(policies))))
    if selected not in policies:
        raise ValueError(f"selected tier {selected!r} has no policy")
    return selected


def load_candidates(
    catalog_path: Path, config: dict[str, Any], include_private: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    endpoint = config["endpoint"]
    catalog_config = config["catalog"]
    selection = config["selection"]
    policies = config["policies"]
    ecu_key = int(endpoint["ecu_key"])
    acquisition = str(catalog_config.get("acquisition", "direct DID"))
    profile_id = str(config["profile_id"])
    rows_by_did: dict[str, list[dict[str, str]]] = defaultdict(list)
    with catalog_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                row_ecu_key = int(row["ecu_key"])
                did_number = int(row["data_id"], 16)
            except (KeyError, TypeError, ValueError):
                continue
            if row_ecu_key != ecu_key or row.get("acquisition") != acquisition:
                continue
            if row.get("profile_id") and row["profile_id"] != profile_id:
                continue
            rows_by_did[f"0x{did_number:04X}"].append(row)
    if not rows_by_did:
        raise ValueError(
            f"no {acquisition!r} candidates found for ECU key {ecu_key} and profile {profile_id!r}"
        )

    identity_words = tuple(selection.get("identity_words", []))
    private_words = tuple(selection.get("private_words", []))
    did_min = int(str(selection.get("did_min", "0x0000")), 0)
    did_max = int(str(selection.get("did_max", "0xFFFF")), 0)
    max_response_bytes = int(selection.get("max_response_bytes", 4092))
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for did, rows in sorted(rows_by_did.items()):
        names = list(dict.fromkeys(row["name"] for row in rows if row.get("name")))
        response_bytes = max(
            (int(row["last_bit"]) // 8 + 1 for row in rows if row.get("last_bit")),
            default=0,
        )
        joined = "; ".join(names)
        privacy_sensitive = bool(private_words and text_has(joined, private_words))
        identity_sensitive = bool(identity_words and text_has(joined, identity_words))
        record: dict[str, Any] = {
            "data_id": did,
            "signals": names,
            "signal_count": len(names),
            "response_data_bytes_min": response_bytes,
            "tier": classify_tier(names, privacy_sensitive, selection, policies),
            "privacy_sensitive": privacy_sensitive,
            "identity_sensitive": identity_sensitive,
            "prior_live_evidence": any(
                row.get("live_state", "Not tested") != "Not tested" for row in rows
            ),
        }
        did_number = int(did, 16)
        if not did_min <= did_number <= did_max:
            record["excluded_reason"] = "outside_configured_did_range"
        elif identity_sensitive:
            record["excluded_reason"] = "identity_bearing_did_is_never_scheduled"
        elif response_bytes > max_response_bytes:
            record["excluded_reason"] = "response_size_limit"
        elif privacy_sensitive and not include_private:
            record["excluded_reason"] = "privacy_sensitive_requires_opt_in"
        else:
            candidates.append(record)
            continue
        excluded.append(record)
    return candidates, excluded


def build_plan(root: Path, view: dict[str, Any], include_private: bool) -> dict[str, Any]:
    """Build one ECU's plan from an expanded profile view."""
    config = view
    catalog_path = resolve_path(root, config["catalog"]["path"])
    candidates, excluded = load_candidates(catalog_path, config, include_private)
    policies = config["policies"]
    return {
        "schema_version": 1,
        "plan_id": config["plan_id"],
        "profile_id": config["profile_id"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "catalog": {
            "file": str(config["catalog"]["path"]),
            "sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        },
        "endpoint": config["endpoint"],
        "limits": {
            "maximum_dids_per_road_batch": int(
                config["selection"].get("maximum_dids_per_road_batch", 6)
            ),
            "max_response_bytes": int(config["selection"].get("max_response_bytes", 4092)),
        },
        "policies": policies,
        "selection": {
            "include_private": include_private,
            "candidate_count": len(candidates),
            "excluded_count": len(excluded),
            "tier_counts": {
                tier: sum(candidate["tier"] == tier for candidate in candidates)
                for tier in policies
            },
        },
        "candidates": candidates,
        "excluded": excluded,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument(
        "--ecu", type=int, action="append",
        help="build only these ECU keys; default is every ECU in the profile",
    )
    parser.add_argument(
        "--output", type=Path,
        help="override the plan path; only valid when building a single ECU",
    )
    parser.add_argument("--include-private", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        root = project_root(args.project_root, args.config)
        config = load_json(resolve_path(root, args.config))
        validate_config(config)
        views = ecu_views(config, args.ecu)
        if args.output and len(views) > 1:
            raise ValueError("--output applies to a single --ecu")
        built = []
        for view in views:
            output = resolve_path(root, args.output or view["paths"]["plan"])
            plan = build_plan(root, view, args.include_private)
            atomic_json(output, plan, replace=args.force)
            built.append({
                "ecu_key": view["endpoint"]["ecu_key"],
                "plan_id": plan["plan_id"],
                "output": str(output),
                **plan["selection"],
            })
        print(json.dumps(built if len(built) > 1 else built[0], indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

