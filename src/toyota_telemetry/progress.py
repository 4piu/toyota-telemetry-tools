"""Reconstruct immutable validation progress from completed capture files."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .capture import read_jsonl


@dataclass
class Progress:
    qualified: set[str] = field(default_factory=set)
    road_complete: set[str] = field(default_factory=set)
    negative_codes: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    qualification_attempts: Counter[str] = field(default_factory=Counter)
    corrupt_files: list[str] = field(default_factory=list)

    def terminal_negative(self, did: str, terminal_nrcs: set[str]) -> bool:
        return did not in self.qualified and any(
            code in terminal_nrcs for code in self.negative_codes.get(did, [])
        )


def discover_progress(capture_root: Path, profile_id: str) -> Progress:
    progress = Progress()
    if not capture_root.exists():
        return progress
    for path in capture_root.rglob("*.complete.jsonl"):
        try:
            records = read_jsonl(path)
            if not records or records[0].get("profile_id") != profile_id:
                continue
            start, end = records[0], records[-1]
            if end.get("record") != "session_end" or end.get("reason") != "completed":
                continue
            # Explicit experiments are independent evidence and must never
            # satisfy the progressive catalog queue merely because they use
            # the same candidate-batch capture format.
            batch_id = start.get("batch_id")
            if isinstance(batch_id, str) and batch_id.startswith("experiment-"):
                continue
            samples = [
                record for record in records if record.get("record") == "candidate_sample"
            ]
            if start.get("mode") == "candidate_qualification":
                for sample in samples:
                    did = sample.get("did")
                    if not isinstance(did, str):
                        continue
                    progress.qualification_attempts[did] += 1
                    if sample.get("supported") is True:
                        progress.qualified.add(did)
                    elif sample.get("supported") is False:
                        code = sample.get("negative_response_code")
                        if isinstance(code, str):
                            progress.negative_codes[did].append(code)
            elif start.get("mode") == "candidate_batch":
                progress.road_complete.update(
                    sample["did"]
                    for sample in samples
                    if sample.get("supported") is True and isinstance(sample.get("did"), str)
                )
        except (OSError, ValueError, TypeError):
            progress.corrupt_files.append(str(path))
    return progress


def batches(values: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def qualification_queue(
    plan: dict[str, Any],
    progress: Progress,
    tiers: set[str],
    max_attempts: int,
    retry_terminal: bool,
    terminal_nrcs: set[str],
) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for candidate in plan["candidates"]:
        did = candidate["data_id"]
        if candidate["tier"] not in tiers or did in progress.qualified:
            continue
        if progress.terminal_negative(did, terminal_nrcs) and not retry_terminal:
            continue
        if progress.qualification_attempts[did] >= max_attempts:
            continue
        queue.append(candidate)
    return queue


def road_queue(
    plan: dict[str, Any], progress: Progress, tiers: set[str]
) -> list[tuple[str, list[dict[str, Any]]]]:
    by_tier: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in plan["candidates"]:
        did = candidate["data_id"]
        if (
            candidate["tier"] in tiers
            and did in progress.qualified
            and did not in progress.road_complete
        ):
            by_tier[candidate["tier"]].append(candidate)
    size = int(plan["limits"]["maximum_dids_per_road_batch"])
    result: list[tuple[str, list[dict[str, Any]]]] = []
    for tier in plan["policies"]:
        if tier in tiers:
            result.extend((tier, group) for group in batches(by_tier[tier], size))
    return result


def summarize_progress(
    plan: dict[str, Any], progress: Progress, tiers: set[str], terminal_nrcs: set[str]
) -> dict[str, Any]:
    candidates = [item for item in plan["candidates"] if item["tier"] in tiers]
    terminal = {
        item["data_id"]
        for item in candidates
        if progress.terminal_negative(item["data_id"], terminal_nrcs)
    }
    retryable = {
        item["data_id"]
        for item in candidates
        if item["data_id"] not in progress.qualified
        and item["data_id"] in progress.negative_codes
        and item["data_id"] not in terminal
    }
    untried = {
        item["data_id"]
        for item in candidates
        if progress.qualification_attempts[item["data_id"]] == 0
        and item["data_id"] not in progress.qualified
    }
    return {
        "schema_version": 1,
        "plan_id": plan["plan_id"],
        "profile_id": plan["profile_id"],
        "candidate_dids": len(candidates),
        "qualification": {
            "qualified": len(progress.qualified & {item["data_id"] for item in candidates}),
            "terminal_negative": len(terminal),
            "retryable_negative": len(retryable),
            "untried": len(untried),
        },
        "road": {
            "complete": len(progress.road_complete & {item["data_id"] for item in candidates}),
            "pending_qualified": len(
                (progress.qualified - progress.road_complete)
                & {item["data_id"] for item in candidates}
            ),
        },
        "by_tier": {
            tier: {
                "candidates": sum(item["tier"] == tier for item in candidates),
                "qualified": sum(
                    item["tier"] == tier and item["data_id"] in progress.qualified
                    for item in candidates
                ),
                "road_complete": sum(
                    item["tier"] == tier and item["data_id"] in progress.road_complete
                    for item in candidates
                ),
            }
            for tier in plan["policies"]
            if tier in tiers
        },
        "corrupt_complete_files": progress.corrupt_files,
    }
