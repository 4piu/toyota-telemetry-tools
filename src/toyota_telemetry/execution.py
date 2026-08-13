"""Resume qualification and road capture through a configured backend command."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from .capture import (
    read_jsonl,
    summarize_candidate_capture,
    validate_capture_context,
    validate_candidate_capture,
)
from .planning import validate_config
from .progress import (
    discover_progress,
    qualification_queue,
    road_queue,
    summarize_progress,
)
from .project import atomic_json, load_json, project_root, resolve_path


DID_PATTERN = re.compile(r"0x[0-9A-F]{4}\Z")


def load_plan(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    plan = load_json(path)
    if plan.get("schema_version") != 1:
        raise ValueError("only validation plan schema 1 is supported")
    if plan.get("profile_id") != config["profile_id"]:
        raise ValueError("plan profile_id does not match validation profile")
    if plan.get("endpoint") != config["endpoint"]:
        raise ValueError("plan endpoint does not match validation profile")
    candidates = plan.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("plan has no candidates")
    maximum_dids = int(plan.get("limits", {}).get("maximum_dids_per_road_batch", 0))
    if not 1 <= maximum_dids <= 64:
        raise ValueError("plan maximum DIDs per road batch must be 1..64")
    seen: set[str] = set()
    for candidate in candidates:
        did = candidate.get("data_id")
        tier = candidate.get("tier")
        if not isinstance(did, str) or not DID_PATTERN.fullmatch(did):
            raise ValueError(f"invalid candidate DID: {did!r}")
        if did in seen:
            raise ValueError(f"duplicate candidate DID: {did}")
        if tier not in plan.get("policies", {}):
            raise ValueError(f"candidate {did} has undefined tier {tier!r}")
        seen.add(did)
    return plan


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def assert_budget(output_root: Path, max_directory_mib: int, max_log_mib: int) -> None:
    used = (
        sum(path.stat().st_size for path in output_root.rglob("*") if path.is_file())
        if output_root.exists()
        else 0
    )
    projected = used + max_log_mib * 1024 * 1024
    if projected > max_directory_mib * 1024 * 1024:
        raise RuntimeError("capture directory budget would be exceeded")
    output_root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output_root).free < (max_log_mib + 256) * 1024 * 1024:
        raise RuntimeError("less than the log limit plus 256 MiB free-space reserve")


def quarantine(partial: Path) -> None:
    if partial.exists():
        partial.replace(Path(str(partial).replace(".partial.jsonl", ".incomplete.jsonl")))
    summary = partial.with_name(partial.name.replace(".jsonl", ".summary.json"))
    if summary.exists():
        summary.replace(
            Path(str(summary).replace(".partial.summary.json", ".incomplete.summary.json"))
        )


def backend_command(root: Path, backend: dict[str, Any], mode: str) -> list[str]:
    if backend.get("schema_version") != 1:
        raise ValueError("only validation-backend schema 1 is supported")
    executable_value = backend.get("executable")
    if not isinstance(executable_value, str) or not executable_value:
        raise ValueError("backend executable must be a non-empty string")
    if backend.get("search_path", False):
        executable = executable_value
    else:
        path = resolve_path(root, executable_value)
        if not path.is_file():
            raise FileNotFoundError(f"backend executable not found: {path}")
        executable = str(path)
    fixed = backend.get("arguments", [])
    modes = backend.get("mode_arguments", {})
    selected = modes.get(mode)
    if not isinstance(fixed, list) or not all(isinstance(item, str) for item in fixed):
        raise ValueError("backend arguments must be a string array")
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise ValueError(f"backend has no valid {mode!r} mode_arguments")
    return [executable, *fixed, *selected]


def capture_arguments(
    root: Path,
    backend: dict[str, Any],
    mode: str,
    dids: list[str],
    batch_id: str,
    output: Path,
    period_ms: int,
    duration_seconds: int | None,
    transport_retries: int,
    max_log_mib: int,
) -> list[str]:
    arguments = backend_command(root, backend, mode)
    arguments.extend(
        (
            "--batch-id",
            batch_id,
            "--period-ms",
            str(period_ms),
            "--max-log-mib",
            str(max_log_mib),
            "--output",
            str(output),
        )
    )
    if duration_seconds is not None:
        arguments.extend(("--duration-seconds", str(duration_seconds)))
    if mode == "road":
        arguments.extend(("--transport-retries", str(transport_retries), "--quiet"))
    for did in dids:
        arguments.extend(("--did", did))
    return arguments


def run_and_validate(
    command: list[str],
    root: Path,
    partial: Path,
    complete: Path,
    minimum_samples: int,
    maximum_dids: int,
    catalog: Path | None,
    ecu_key: int,
    decode_overrides: Path | None,
    profile_id: str,
    endpoint: dict[str, Any],
) -> None:
    summary_partial = partial.with_name(partial.name.replace(".jsonl", ".summary.json"))
    summary_complete = complete.with_name(complete.name.replace(".jsonl", ".summary.json"))
    try:
        capture = subprocess.run(command, cwd=root, check=False)
        if capture.returncode:
            raise RuntimeError(f"validation backend exited with code {capture.returncode}")
        start, samples = validate_candidate_capture(
            read_jsonl(partial), minimum_samples, maximum_dids
        )
        validate_capture_context(start, profile_id, endpoint)
        report = summarize_candidate_capture(
            start, samples, catalog, ecu_key, decode_overrides
        )
        atomic_json(summary_partial, report, replace=False)
        summary_partial.replace(summary_complete)
        partial.replace(complete)
    except BaseException:
        if summary_complete.exists() and not complete.exists():
            summary_complete.replace(
                Path(str(partial).replace(".partial.jsonl", ".incomplete.summary.json"))
            )
        quarantine(partial)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("status", "qualify", "road"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--capture-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--tiers")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--max-qualification-attempts", type=int, default=2)
    parser.add_argument("--transport-retries", type=int, default=2)
    parser.add_argument("--retry-terminal", action="store_true")
    parser.add_argument("--startup-delay-seconds", type=int, default=10)
    parser.add_argument("--max-log-mib", type=int, default=16)
    parser.add_argument("--max-directory-mib", type=int, default=2048)
    args = parser.parse_args()
    try:
        if args.max_items < 1 or args.max_qualification_attempts < 1:
            raise ValueError("--max-items and --max-qualification-attempts must be positive")
        if not 0 <= args.startup_delay_seconds <= 300:
            raise ValueError("--startup-delay-seconds must be 0..300")
        if not 0 <= args.transport_retries <= 5:
            raise ValueError("--transport-retries must be 0..5")
        if args.max_log_mib < 1 or args.max_directory_mib < args.max_log_mib:
            raise ValueError("invalid log/directory size bounds")

        root = project_root(args.project_root, args.config)
        config_path = resolve_path(root, args.config)
        config = load_json(config_path)
        validate_config(config)
        paths = config["paths"]
        plan_path = resolve_path(root, args.plan or paths["plan"])
        capture_root = resolve_path(root, args.capture_root or paths["capture_root"])
        output_root = resolve_path(
            root, args.output_root or paths.get("output_root", Path(paths["capture_root"]) / "progressive")
        )
        try:
            output_root.relative_to(capture_root)
        except ValueError as exc:
            raise ValueError("output root must be inside capture root") from exc
        plan = load_plan(plan_path, config)
        available_tiers = set(plan["policies"])
        tier_text = args.tiers
        if tier_text is None:
            tier_text = ",".join(
                config.get("road_default_tiers", list(plan["policies"]))
                if args.phase == "road"
                else plan["policies"].keys()
            )
        tiers = {tier.strip() for tier in tier_text.split(",") if tier.strip()}
        unknown = tiers - available_tiers
        if not tiers or unknown:
            raise ValueError(f"invalid tiers; unknown values: {sorted(unknown)}")
        terminal_nrcs = set(config.get("terminal_nrcs", ["0x31", "0x33"]))
        progress = discover_progress(capture_root, plan["profile_id"])
        status = summarize_progress(plan, progress, tiers, terminal_nrcs)
        print(json.dumps(status, indent=2))
        atomic_json(
            output_root / "progress.snapshot.json",
            summarize_progress(plan, progress, available_tiers, terminal_nrcs),
        )
        if args.phase == "status":
            return 0
        if args.phase == "qualify":
            queue: list[Any] = qualification_queue(
                plan,
                progress,
                tiers,
                args.max_qualification_attempts,
                args.retry_terminal,
                terminal_nrcs,
            )[: args.max_items]
            print(
                f"Qualification queue: {len(queue)} DID(s); "
                f"mode={'LIVE' if args.live else 'DRY RUN'}"
            )
        else:
            queue = road_queue(plan, progress, tiers)[: args.max_items]
            print(
                f"Road queue: {len(queue)} batch(es); "
                f"mode={'LIVE' if args.live else 'DRY RUN'}"
            )
        if not args.live:
            for item in queue:
                if args.phase == "qualify":
                    print(
                        f"  {item['data_id']} tier={item['tier']} "
                        f"signals={item['signal_count']}"
                    )
                else:
                    tier, group = item
                    policy = plan["policies"][tier]
                    print(
                        f"  {tier}: {','.join(candidate['data_id'] for candidate in group)} "
                        f"period={policy['period_ms']}ms "
                        f"duration={policy['duration_seconds']}s retries={args.transport_retries}"
                    )
            return 0
        if not queue:
            return 0
        backend = config.get("backend")
        if not isinstance(backend, dict):
            raise ValueError("live mode requires a backend object in the validation profile")
        print(f"Live sequence will start in {args.startup_delay_seconds} seconds.")
        if args.startup_delay_seconds:
            time.sleep(args.startup_delay_seconds)
        print("Startup delay finished; opening the configured backend.")
        maximum_dids = int(plan["limits"]["maximum_dids_per_road_batch"])
        catalog = resolve_path(root, config["catalog"]["path"])
        overrides_value = config.get("analysis", {}).get("decode_overrides")
        decode_overrides = resolve_path(root, overrides_value) if overrides_value else None
        for item in queue:
            assert_budget(output_root, args.max_directory_mib, args.max_log_mib)
            stamp = timestamp()
            if args.phase == "qualify":
                did = item["data_id"]
                stem = f"did-{did[2:].lower()}-{stamp}.qualification"
                partial = output_root / "qualification" / f"{stem}.partial.jsonl"
                complete = output_root / "qualification" / f"{stem}.complete.jsonl"
                period_ms = int(config.get("qualification_period_ms", 100))
                command = capture_arguments(
                    root,
                    backend,
                    "qualification",
                    [did],
                    f"progressive-{did[2:].lower()}",
                    partial,
                    period_ms,
                    None,
                    0,
                    args.max_log_mib,
                )
                minimum_samples = 1
                print(f"QUALIFY {did}")
            else:
                tier, group = item
                policy = plan["policies"][tier]
                dids = [candidate["data_id"] for candidate in group]
                digest = hashlib.sha256(",".join(dids).encode()).hexdigest()[:10]
                stem = f"{tier}-{digest}-{stamp}"
                partial = output_root / "road" / f"{stem}.partial.jsonl"
                complete = output_root / "road" / f"{stem}.complete.jsonl"
                command = capture_arguments(
                    root,
                    backend,
                    "road",
                    dids,
                    f"progressive-{tier}-{digest}",
                    partial,
                    int(policy["period_ms"]),
                    int(policy["duration_seconds"]),
                    args.transport_retries,
                    args.max_log_mib,
                )
                minimum_samples = int(policy["minimum_samples_per_did"])
                print(f"ROAD {tier} {','.join(dids)}")
            partial.parent.mkdir(parents=True, exist_ok=True)
            run_and_validate(
                command,
                root,
                partial,
                complete,
                minimum_samples,
                maximum_dids,
                catalog,
                int(config["endpoint"]["ecu_key"]),
                decode_overrides,
                str(config["profile_id"]),
                config["endpoint"],
            )
            progress = discover_progress(capture_root, plan["profile_id"])
            atomic_json(
                output_root / "progress.snapshot.json",
                summarize_progress(plan, progress, available_tiers, terminal_nrcs),
            )
        print("Sequence complete; progress snapshot updated.")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(
            "Interrupted; the current partial run does not count as progress.",
            file=sys.stderr,
        )
        raise SystemExit(130)
