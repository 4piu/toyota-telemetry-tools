"""Pure edits of the tracked review and policy documents.

Every function returns new documents plus an impact summary and never writes.
The CLI saves the result immediately; the desktop application keeps it as a
draft until Save. Both save through ``ProjectRepository.write_authored_document``
so validation is identical.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable, Mapping

from ..decoding import evaluate_formula
from ..identity import SignalRef
from ..repository import ProjectRepository
from ..schemas import validate_condition, validate_target
from .inventory import InventoryTree


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()

DEFAULT_SETTINGS = {"priority": 50, "target_interval_ms": 1000, "maximum_interval_ms": 5000}
DEFAULT_CONDITION_AGE_MS = 2000
REQUIREMENT_LABELS = {
    "minimum_samples": "Minimum samples",
    "minimum_runs": "Minimum runs",
    "minimum_span_seconds": "Minimum span",
    "required_states": "Required states",
    "minimum_transitions": "Minimum transitions",
    "minimum_changes": "Minimum changes",
    "minimum_applicable_seconds": "Minimum applicable time",
}
_OPERATORS = {"==": "eq", "!=": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte", "in": "in"}
_SYMBOLS = {value: key for key, value in _OPERATORS.items()}
_CLAUSE = re.compile(r"^\s*(\S+)\s*(==|!=|<=|>=|<|>|\bin\b)\s*(.+?)\s*$")
_DURATION = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([smh]?)\s*$")


@dataclass
class EditResult:
    review: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    signals: list[str] = field(default_factory=list)
    dids: list[str] = field(default_factory=list)
    siblings: list[str] = field(default_factory=list)
    created_settings: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)

    def impact(self) -> dict[str, Any]:
        return {
            "signals": list(self.signals),
            "dids": list(self.dids),
            "unselected_siblings": list(self.siblings),
            "created_did_settings": list(self.created_settings),
            "changes": list(self.changes),
        }


def parse_duration(text: str) -> float:
    """Parse ``90``, ``45s``, ``10m``, or ``2h`` into seconds."""

    match = _DURATION.fullmatch(str(text))
    if match is None:
        raise ValueError(f"invalid duration {text!r}; use seconds or a suffix such as 30s, 10m, 2h")
    value = float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    return value


def format_duration(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{seconds:g}s"


def parse_condition(text: str | None, maximum_age_ms: int = DEFAULT_CONDITION_AGE_MS) -> dict | None:
    """Parse ``REF OP VALUE [and|or REF OP VALUE ...]`` into a condition tree."""

    if text is None or not text.strip() or text.strip().lower() == "none":
        return None
    lowered = f" {text.lower()} "
    if " and " in lowered and " or " in lowered:
        raise ValueError("a condition may combine clauses with 'and' or with 'or', not both")
    connective = "any" if " or " in lowered else "all"
    parts = re.split(r"\s+(?:and|or)\s+", text.strip(), flags=re.IGNORECASE)
    clauses = [_parse_clause(part, maximum_age_ms) for part in parts]
    node = clauses[0] if len(clauses) == 1 else {connective: clauses}
    validate_condition(node, "condition")
    return node


def format_condition(node: Mapping[str, Any] | None) -> str:
    if node is None:
        return ""
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        children = node[key]
        if all("signal_ref" in child for child in children):
            joiner = " and " if key == "all" else " or "
            return joiner.join(_format_clause(child) for child in children)
        return json.dumps(node, sort_keys=True)
    return _format_clause(node)


def condition_age_ms(node: Mapping[str, Any] | None) -> int | None:
    if node is None:
        return None
    if "all" in node or "any" in node:
        ages = [condition_age_ms(child) for child in node.get("all", node.get("any"))]
        return max((age for age in ages if age is not None), default=None)
    return int(node["maximum_age_ms"])


def _parse_clause(text: str, maximum_age_ms: int) -> dict[str, Any]:
    match = _CLAUSE.match(text)
    if match is None:
        raise ValueError(f"invalid condition clause {text!r}; expected SIGNAL OP VALUE, e.g. 372/0x1002/0..15 > 10")
    reference, symbol, raw = match.groups()
    signal_ref = str(SignalRef.parse(reference))
    operator = _OPERATORS[symbol]
    if operator == "in":
        value: Any = [_scalar(item) for item in raw.split(",") if item.strip()]
        if not value:
            raise ValueError(f"'in' needs a comma-separated list in {text!r}")
    else:
        value = _scalar(raw)
    return {
        "signal_ref": signal_ref,
        "operator": operator,
        "value": value,
        "maximum_age_ms": int(maximum_age_ms),
    }


def _format_clause(node: Mapping[str, Any]) -> str:
    value = node["value"]
    rendered = ",".join(_render(item) for item in value) if isinstance(value, list) else _render(value)
    return f"{node['signal_ref']} {_SYMBOLS[node['operator']]} {rendered}"


def _scalar(text: str) -> Any:
    text = text.strip()
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"condition value must be a number or true/false, got {text!r}") from exc
    return int(number) if number.is_integer() and "." not in text else number


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _confirmed_signal(tree: InventoryTree, signal_ref: str, action: str):
    found = tree.find_signal(signal_ref)
    if found is None:
        raise ValueError(f"signal is not in the candidate list: {signal_ref}")
    endpoint, did, signal = found
    if not endpoint.confirmed:
        raise ValueError(
            f"{action} needs a confirmed endpoint; {endpoint.key} is {endpoint.state}. "
            "Verify it, then confirm it with `telemetry endpoint confirm`."
        )
    return endpoint, did, signal


def edit_signals(
    repository: ProjectRepository,
    tree: InventoryTree,
    signal_refs: Iterable[str],
    *,
    name: Any = UNSET,
    formula: Any = UNSET,
    unit: Any = UNSET,
    evidence_state: Any = UNSET,
    notes: Any = UNSET,
    variant: Any = UNSET,
    review: Mapping[str, Any] | None = None,
) -> EditResult:
    document = copy.deepcopy(dict(review if review is not None else repository.review))
    result = EditResult()
    if formula is not UNSET and formula:
        _check_formula(formula)
    for signal_ref in signal_refs:
        _confirmed_signal(tree, signal_ref, "Reviewing a signal")
        rows = [row for row in document["signals"] if row["signal_ref"] == signal_ref]
        if len(rows) > 1:
            raise ValueError(f"{signal_ref} has several review rows; edit review/signals.json directly")
        if rows:
            row = rows[0]
        else:
            row = {
                "signal_ref": signal_ref,
                "catalog_variant_id": None,
                "display_name_override": None,
                "formula_override": None,
                "unit_override": None,
                "evidence_state": "Candidate",
                "notes": "",
                "evidence_refs": [],
            }
            document["signals"].append(row)
            result.changes.append(f"{signal_ref}: new review entry")
        for field_name, value in (
            ("display_name_override", name),
            ("formula_override", formula),
            ("unit_override", unit),
            ("catalog_variant_id", variant),
        ):
            if value is UNSET:
                continue
            new_value = value or None
            if row[field_name] != new_value:
                result.changes.append(f"{signal_ref}: {field_name} {row[field_name]!r} -> {new_value!r}")
                row[field_name] = new_value
        if evidence_state is not UNSET:
            if not evidence_state:
                raise ValueError("evidence state must not be empty")
            if row["evidence_state"] != evidence_state:
                result.changes.append(f"{signal_ref}: evidence_state -> {evidence_state!r}")
                row["evidence_state"] = evidence_state
        if notes is not UNSET and row["notes"] != notes:
            result.changes.append(f"{signal_ref}: notes updated")
            row["notes"] = notes
        result.signals.append(signal_ref)
    document["signals"].sort(key=lambda row: (SignalRef.parse(row["signal_ref"]), row["catalog_variant_id"] or ""))
    result.review = document
    return result


def edit_did_settings(
    repository: ProjectRepository,
    tree: InventoryTree,
    did_keys: Iterable[str],
    *,
    selected_signals: Iterable[str] = (),
    target_interval_ms: Any = UNSET,
    maximum_interval_ms: Any = UNSET,
    priority: Any = UNSET,
    included: Any = UNSET,
    policy: Mapping[str, Any] | None = None,
) -> EditResult:
    document = copy.deepcopy(dict(policy if policy is not None else repository.policy))
    selected = set(selected_signals)
    rules = {row["acquisition_id"]: row for row in document["acquisitions"]}
    result = EditResult()
    for key in did_keys:
        found = tree.find_did(key)
        if found is None:
            raise ValueError(f"DID is not in the candidate list: {key}")
        endpoint, did = found
        if not endpoint.confirmed:
            raise ValueError(f"DID settings need a confirmed endpoint; {endpoint.key} is {endpoint.state}")
        acquisition_id = f"{endpoint.endpoint_id}/{did.data_id}"
        rule = rules.get(acquisition_id)
        if rule is None:
            rule = {"acquisition_id": acquisition_id, "included": True, **DEFAULT_SETTINGS}
            rules[acquisition_id] = rule
            result.created_settings.append(acquisition_id)
        for field_name, value in (
            ("target_interval_ms", target_interval_ms),
            ("maximum_interval_ms", maximum_interval_ms),
            ("priority", priority),
            ("included", included),
        ):
            if value is not UNSET and rule[field_name] != value:
                result.changes.append(f"{acquisition_id}: {field_name} {rule[field_name]} -> {value}")
                rule[field_name] = value
        if rule["target_interval_ms"] > rule["maximum_interval_ms"]:
            raise ValueError(f"{acquisition_id}: interval must not exceed the maximum interval")
        result.dids.append(acquisition_id)
        result.siblings.extend(
            signal.signal_ref for signal in did.signals if signal.signal_ref not in selected
        )
    document["acquisitions"] = sorted(rules.values(), key=lambda row: row["acquisition_id"])
    result.policy = document
    return result


def edit_targets(
    repository: ProjectRepository,
    tree: InventoryTree,
    signal_refs: Iterable[str],
    *,
    requirements: Mapping[str, Any] | None = None,
    condition: Any = UNSET,
    minimum_difference: Any = UNSET,
    included: Any = UNSET,
    policy: Mapping[str, Any] | None = None,
) -> EditResult:
    """Set explicitly given requirement fields; a value of 0 removes one."""

    document = copy.deepcopy(dict(policy if policy is not None else repository.policy))
    targets = {row["signal_ref"]: row for row in document["targets"]}
    rules = {row["acquisition_id"]: row for row in document["acquisitions"]}
    requirements = dict(requirements or {})
    for name in requirements:
        if name not in REQUIREMENT_LABELS:
            raise ValueError(f"unknown requirement {name!r}")
    result = EditResult()
    for signal_ref in signal_refs:
        endpoint, did, _signal = _confirmed_signal(tree, signal_ref, "A collection target")
        target = targets.get(signal_ref)
        if target is None:
            target = {"signal_ref": signal_ref, "included": True, "condition": None, "requirements": {}}
            targets[signal_ref] = target
            result.changes.append(f"{signal_ref}: new target")
        for name, value in requirements.items():
            if value in (None, 0, [], ""):
                if name in target["requirements"]:
                    del target["requirements"][name]
                    result.changes.append(f"{signal_ref}: removed {REQUIREMENT_LABELS[name].lower()}")
            elif target["requirements"].get(name) != value:
                target["requirements"][name] = value
                result.changes.append(f"{signal_ref}: {REQUIREMENT_LABELS[name].lower()} = {value}")
        if minimum_difference is not UNSET:
            if minimum_difference in (None, 0):
                target.pop("minimum_difference", None)
            else:
                target["minimum_difference"] = float(minimum_difference)
                result.changes.append(
                    f"{signal_ref}: minimum difference = {target['minimum_difference']:g}"
                )
        if "minimum_changes" not in target["requirements"]:
            target.pop("minimum_difference", None)
        elif "minimum_difference" not in target:
            raise ValueError(f"{signal_ref}: minimum changes needs a minimum difference (--min-difference)")
        if condition is not UNSET:
            target["condition"] = condition
            result.changes.append(f"{signal_ref}: condition = {format_condition(condition) or 'always'}")
        if included is not UNSET:
            target["included"] = bool(included)
        if not target["requirements"]:
            raise ValueError(f"{signal_ref}: a target needs at least one completion requirement")
        validate_target(target, signal_ref)
        acquisition_id = f"{endpoint.endpoint_id}/{did.data_id}"
        if acquisition_id not in rules:
            rules[acquisition_id] = {"acquisition_id": acquisition_id, "included": True, **DEFAULT_SETTINGS}
            result.created_settings.append(acquisition_id)
        if acquisition_id not in result.dids:
            result.dids.append(acquisition_id)
        result.signals.append(signal_ref)
    document["targets"] = sorted(targets.values(), key=lambda row: SignalRef.parse(row["signal_ref"]))
    document["acquisitions"] = sorted(rules.values(), key=lambda row: row["acquisition_id"])
    result.policy = document
    return result


def clear_targets(
    repository: ProjectRepository,
    signal_refs: Iterable[str],
    *,
    policy: Mapping[str, Any] | None = None,
) -> EditResult:
    document = copy.deepcopy(dict(policy if policy is not None else repository.policy))
    wanted = set(signal_refs)
    result = EditResult()
    kept = []
    for row in document["targets"]:
        if row["signal_ref"] in wanted:
            result.signals.append(row["signal_ref"])
            result.changes.append(f"{row['signal_ref']}: target removed (recordings are kept)")
        else:
            kept.append(row)
    document["targets"] = kept
    result.policy = document
    return result


def save_documents(
    repository: ProjectRepository,
    *,
    review: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> None:
    """Validate and atomically replace each changed authority."""

    if review is not None:
        repository.write_authored_document("signal_review", review)
    if policy is not None:
        repository.write_authored_document("collection_policy", policy)


def _check_formula(formula: str) -> None:
    try:
        evaluate_formula(formula, 1)
    except ZeroDivisionError:
        pass
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"invalid formula {formula!r}: {exc}") from exc
