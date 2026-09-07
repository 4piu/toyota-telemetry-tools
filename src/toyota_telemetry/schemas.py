"""Exact validators for the new authored project documents.

The schemas deliberately reject unknown members.  They are a clean-break
contract, not permissive readers for earlier project files.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
import re
from typing import Any

from .identity import AcquisitionRef, SignalRef, canonical_did, stable_slug


CLASSES = frozenset(
    {
        "descriptive",
        "snapshot",
        "switch_or_enum",
        "fast_dynamic",
        "slow_trend",
        "counter_or_history",
        "exploration",
    }
)
TARGETS = frozenset(
    {
        "minimum_total_observations",
        "minimum_changed_observations",
        "minimum_distinct_raw_values",
        "maximum_distinct_raw_values",
        "minimum_transitions",
        "required_raw_states",
        "minimum_applicable_seconds",
        "minimum_time_span_seconds",
        "minimum_distinct_runs",
    }
)
OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in"})
SESSION_SERVICES = frozenset({0x10, 0x3E})
BACKEND_SETTINGS = {
    "pcan": ({"channel"}, set()),
    "j2534": ({"dll"}, set()),
    "gsusb": ({"vendor_id", "product_id"}, {"serial"}),
    "slcan": ({"port"}, set()),
}
_HEX_BYTES = re.compile(r"[0-9A-Fa-f]{2}(?: [0-9A-Fa-f]{2})*")
_HEX_CAN = re.compile(r"0x[0-9A-Fa-f]{3}")
_HEX_USB_ID = re.compile(r"[0-9A-Fa-f]{4}")


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    return value


def _array(value: Any, where: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be an array")
    if nonempty and not value:
        raise ValueError(f"{where} must not be empty")
    return value


def _keys(
    value: Mapping[str, Any],
    where: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ValueError(f"{where} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")


def _string(value: Any, where: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ValueError(f"{where} must be a non-empty string")
    return value


def _integer(
    value: Any, where: str, minimum: int | None = None, maximum: int | None = None
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{where} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{where} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{where} must be at most {maximum}")
    return value


def _number(
    value: Any, where: str, minimum: float | None = None, maximum: float | None = None
) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{where} must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValueError(f"{where} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{where} must be at most {maximum}")
    return number


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be boolean")
    return value


def _unique(values: list[Any], where: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{where} contains duplicates")


def _hex_bytes(value: Any, where: str) -> str:
    text = _string(value, where)
    if _HEX_BYTES.fullmatch(text) is None:
        raise ValueError(f"{where} must be space-separated hexadecimal bytes")
    if text != text.upper():
        raise ValueError(f"{where} must use uppercase hexadecimal")
    return text


def _did(value: Any, where: str) -> str:
    canonical = canonical_did(value)
    if value != canonical:
        raise ValueError(f"{where} must use canonical {canonical}")
    return canonical


def _signal_ref(value: Any, where: str) -> SignalRef:
    parsed = SignalRef.parse(value)
    if value != str(parsed):
        raise ValueError(f"{where} must use canonical {parsed}")
    return parsed


def _acquisition_ref(value: Any, where: str) -> AcquisitionRef:
    parsed = AcquisitionRef.parse(value)
    if value != str(parsed):
        raise ValueError(f"{where} must use canonical {parsed}")
    return parsed


def _provenance(value: Any, where: str) -> None:
    rows = _array(value, where, nonempty=True)
    for index, item in enumerate(rows):
        _tracked_relative_path(item, f"{where}[{index}]")


def _tracked_relative_path(value: Any, where: str) -> str:
    text = _string(value, where)
    path = PurePosixPath(text)
    if (
        "\\" in text
        or ":" in text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] == "scratch"
    ):
        raise ValueError(f"{where} must be a tracked project-relative path")
    return text


def _document_header(
    document: Mapping[str, Any], where: str, document_type: str, fields: set[str]
) -> None:
    _keys(document, where, {"document_type", "schema_version", "profile_id", *fields})
    if document["document_type"] != document_type:
        raise ValueError(f"{where}.document_type must be {document_type!r}")
    if document["schema_version"] != 1:
        raise ValueError(f"{where}.schema_version must be 1")
    _string(document["profile_id"], f"{where}.profile_id")


def validate_project_marker(marker: Mapping[str, Any]) -> None:
    marker = _object(marker, "telemetry-project.toml")
    _keys(
        marker,
        "telemetry-project.toml",
        {"schema_version", "project_kind", "project_id", "toolkit", "paths"},
    )
    if marker["schema_version"] != 2:
        raise ValueError("telemetry-project.toml.schema_version must be 2")
    if marker["project_kind"] != "vehicle":
        raise ValueError("telemetry-project.toml.project_kind must be 'vehicle'")
    stable_slug(_string(marker["project_id"], "telemetry-project.toml.project_id"), "project_id")
    toolkit = _object(marker["toolkit"], "telemetry-project.toml.toolkit")
    _keys(toolkit, "telemetry-project.toml.toolkit", {"repository", "revision"})
    _string(toolkit["repository"], "telemetry-project.toml.toolkit.repository")
    _string(toolkit["revision"], "telemetry-project.toml.toolkit.revision")
    paths = _object(marker["paths"], "telemetry-project.toml.paths")
    required_paths = {
        "transport", "collection_policy", "signal_review", "catalog_manifest",
        "result", "scratch", "local_settings", "database", "captures", "plans",
        "toolkit",
    }
    _keys(paths, "telemetry-project.toml.paths", required_paths)
    for key in required_paths:
        _string(paths[key], f"telemetry-project.toml.paths.{key}")


def validate_transport(document: Mapping[str, Any]) -> None:
    document = _object(document, "transport")
    _document_header(
        document,
        "transport",
        "telemetry.transport",
        {"network", "safety", "session_recipes", "endpoints", "dynamic_dids"},
    )
    network = _object(document["network"], "transport.network")
    _keys(network, "transport.network", {"type", "bitrate", "addressing"})
    if network["type"] != "classical-can" or network["addressing"] != "normal":
        raise ValueError("transport supports only classical-can normal addressing")
    _integer(network["bitrate"], "transport.network.bitrate", 10_000, 1_000_000)

    safety = _object(document["safety"], "transport.safety")
    safety_fields = {
        "did_min", "did_max", "request_start_floor_ms", "maximum_request_rate_hz",
        "maximum_estimated_bus_utilization", "maximum_nominal_session_utilization",
        "initial_response_timeout_ms", "response_pending_budget_ms", "transport_retries",
    }
    _keys(safety, "transport.safety", safety_fields)
    did_min = int(_did(safety["did_min"], "transport.safety.did_min"), 16)
    did_max = int(_did(safety["did_max"], "transport.safety.did_max"), 16)
    if did_min > did_max:
        raise ValueError("transport.safety.did_min must not exceed did_max")
    _integer(safety["request_start_floor_ms"], "transport.safety.request_start_floor_ms", 1)
    _number(safety["maximum_request_rate_hz"], "transport.safety.maximum_request_rate_hz", 0.01)
    _number(
        safety["maximum_estimated_bus_utilization"],
        "transport.safety.maximum_estimated_bus_utilization", 0.0001, 1.0,
    )
    _number(
        safety["maximum_nominal_session_utilization"],
        "transport.safety.maximum_nominal_session_utilization", 0.0001, 1.0,
    )
    _integer(safety["initial_response_timeout_ms"], "transport.safety.initial_response_timeout_ms", 1)
    _integer(safety["response_pending_budget_ms"], "transport.safety.response_pending_budget_ms", 1)
    _integer(safety["transport_retries"], "transport.safety.transport_retries", 0, 5)

    recipe_ids: list[str] = []
    for index, raw in enumerate(_array(document["session_recipes"], "transport.session_recipes", nonempty=True)):
        where = f"transport.session_recipes[{index}]"
        recipe = _object(raw, where)
        _keys(recipe, where, {"id", "steps"})
        recipe_id = stable_slug(_string(recipe["id"], f"{where}.id"), "session recipe id")
        recipe_ids.append(recipe_id)
        for step_index, raw_step in enumerate(_array(recipe["steps"], f"{where}.steps", nonempty=True)):
            step_where = f"{where}.steps[{step_index}]"
            step = _object(raw_step, step_where)
            _keys(step, step_where, {"request_hex", "expected_prefix_hex", "label", "tolerate_negative"})
            request = _hex_bytes(step["request_hex"], f"{step_where}.request_hex")
            _hex_bytes(step["expected_prefix_hex"], f"{step_where}.expected_prefix_hex")
            if int(request[:2], 16) not in SESSION_SERVICES:
                allowed = ", ".join(f"0x{service:02X}" for service in sorted(SESSION_SERVICES))
                raise ValueError(
                    f"{step_where}.request_hex service is not allowlisted; allowed: {allowed}"
                )
            _string(step["label"], f"{step_where}.label")
            _boolean(step["tolerate_negative"], f"{step_where}.tolerate_negative")
    _unique(recipe_ids, "transport session recipe ids")

    endpoint_ids: list[str] = []
    ecu_keys: list[int] = []
    for index, raw in enumerate(_array(document["endpoints"], "transport.endpoints", nonempty=True)):
        where = f"transport.endpoints[{index}]"
        endpoint = _object(raw, where)
        _keys(
            endpoint, where,
            {"id", "tester_can_id", "response_can_id", "session_recipe_id", "ecu_keys", "state", "provenance"},
        )
        endpoint_id = stable_slug(_string(endpoint["id"], f"{where}.id"), "endpoint id")
        endpoint_ids.append(endpoint_id)
        tester = _string(endpoint["tester_can_id"], f"{where}.tester_can_id")
        response = _string(endpoint["response_can_id"], f"{where}.response_can_id")
        if _HEX_CAN.fullmatch(tester) is None or _HEX_CAN.fullmatch(response) is None:
            raise ValueError(f"{where} CAN IDs must be 0x-prefixed three-digit hex")
        if tester != f"0x{int(tester, 16):03X}" or response != f"0x{int(response, 16):03X}":
            raise ValueError(f"{where} CAN IDs must use canonical uppercase hex")
        if int(tester, 16) == int(response, 16):
            raise ValueError(f"{where} tester and response CAN IDs must differ")
        if endpoint["session_recipe_id"] not in recipe_ids:
            raise ValueError(f"{where}.session_recipe_id is unresolved")
        members = _array(endpoint["ecu_keys"], f"{where}.ecu_keys", nonempty=True)
        for member_index, member in enumerate(members):
            ecu_keys.append(_integer(member, f"{where}.ecu_keys[{member_index}]", 0, 65535))
        _unique(members, f"{where}.ecu_keys")
        if endpoint["state"] != "confirmed":
            raise ValueError(f"{where}.state must be 'confirmed'")
        _provenance(endpoint["provenance"], f"{where}.provenance")
    _unique(endpoint_ids, "transport endpoint ids")
    _unique(ecu_keys, "transport ECU memberships")

    dynamic_keys: list[tuple[str, str]] = []
    for index, raw in enumerate(_array(document["dynamic_dids"], "transport.dynamic_dids")):
        where = f"transport.dynamic_dids[{index}]"
        dynamic = _object(raw, where)
        _keys(
            dynamic, where,
            {"endpoint_id", "target_did", "definition_hex", "expected_prefix_hex", "state", "provenance"},
        )
        if dynamic["endpoint_id"] not in endpoint_ids:
            raise ValueError(f"{where}.endpoint_id is unresolved")
        did = _did(dynamic["target_did"], f"{where}.target_did")
        definition = _hex_bytes(dynamic["definition_hex"], f"{where}.definition_hex")
        expected = _hex_bytes(dynamic["expected_prefix_hex"], f"{where}.expected_prefix_hex")
        if not definition.startswith("2C ") or not expected.startswith("6C "):
            raise ValueError(f"{where} must be an exact 0x2C definition/0x6C response")
        if dynamic["state"] != "reviewed_replay":
            raise ValueError(f"{where}.state must be 'reviewed_replay'")
        _provenance(dynamic["provenance"], f"{where}.provenance")
        dynamic_keys.append((dynamic["endpoint_id"], did))
    _unique(dynamic_keys, "transport dynamic DID identities")


def _validate_targets(value: Any, where: str) -> None:
    targets = _object(value, where)
    _keys(targets, where, set(), set(TARGETS))
    if not targets:
        raise ValueError(f"{where} must not be empty")
    for key, item in targets.items():
        item_where = f"{where}.{key}"
        if key == "required_raw_states":
            states = _array(item, item_where, nonempty=True)
            for index, state in enumerate(states):
                _integer(state, f"{item_where}[{index}]", 0)
            _unique(states, item_where)
        elif key.endswith("_seconds"):
            _number(item, item_where, 0.001)
        else:
            _integer(item, item_where, 1)


def _validate_rule_fields(value: Mapping[str, Any], where: str, *, objective: bool) -> None:
    common_required = {"priority", "target_interval_ms", "maximum_interval_ms", "targets"}
    common_optional = {"deadband"}
    required = set(common_required)
    optional = set(common_optional)
    if objective:
        required |= {"signal_ref", "objective_id", "class", "included", "applicability"}
    _keys(value, where, required, optional)
    _integer(value["priority"], f"{where}.priority", 0, 100)
    target = _integer(value["target_interval_ms"], f"{where}.target_interval_ms", 1)
    maximum = _integer(value["maximum_interval_ms"], f"{where}.maximum_interval_ms", 1)
    if target > maximum:
        raise ValueError(f"{where}.target_interval_ms must not exceed maximum_interval_ms")
    _validate_targets(value["targets"], f"{where}.targets")
    if "deadband" in value:
        _number(value["deadband"], f"{where}.deadband", 0.0)
    if objective:
        _signal_ref(value["signal_ref"], f"{where}.signal_ref")
        stable_slug(_string(value["objective_id"], f"{where}.objective_id"), "objective id")
        if value["class"] not in CLASSES:
            raise ValueError(f"{where}.class is not supported")
        _boolean(value["included"], f"{where}.included")
        if value["applicability"] is not None:
            _validate_applicability(value["applicability"], f"{where}.applicability")


def _validate_applicability(value: Any, where: str) -> None:
    node = _object(value, where)
    if set(node) in ({"all"}, {"any"}):
        key = next(iter(node))
        children = _array(node[key], f"{where}.{key}", nonempty=True)
        for index, child in enumerate(children):
            _validate_applicability(child, f"{where}.{key}[{index}]")
        return
    _keys(node, where, {"signal_ref", "operator", "value", "maximum_age_ms"})
    _signal_ref(node["signal_ref"], f"{where}.signal_ref")
    if node["operator"] not in OPERATORS:
        raise ValueError(f"{where}.operator is not supported")
    if node["operator"] == "in":
        _array(node["value"], f"{where}.value", nonempty=True)
    elif not isinstance(node["value"], (str, int, float, bool)):
        raise ValueError(f"{where}.value must be scalar")
    _integer(node["maximum_age_ms"], f"{where}.maximum_age_ms", 1)


def validate_policy(document: Mapping[str, Any]) -> None:
    document = _object(document, "collection policy")
    _document_header(
        document,
        "collection policy",
        "telemetry.collection-policy",
        {"storage", "class_defaults", "acquisitions", "objectives"},
    )
    storage = _object(document["storage"], "collection policy.storage")
    _keys(
        storage,
        "collection policy.storage",
        {"segment_seconds", "segment_max_mib", "capture_tree_max_mib", "disk_reserve_mib"},
    )
    _integer(storage["segment_seconds"], "collection policy.storage.segment_seconds", 1)
    segment = _integer(storage["segment_max_mib"], "collection policy.storage.segment_max_mib", 1)
    tree = _integer(storage["capture_tree_max_mib"], "collection policy.storage.capture_tree_max_mib", 1)
    if segment > tree:
        raise ValueError("collection policy segment limit exceeds capture-tree limit")
    _integer(storage["disk_reserve_mib"], "collection policy.storage.disk_reserve_mib", 1)

    defaults = _object(document["class_defaults"], "collection policy.class_defaults")
    if not defaults:
        raise ValueError("collection policy.class_defaults must not be empty")
    if unknown := defaults.keys() - CLASSES:
        raise ValueError(f"collection policy has unknown classes: {', '.join(sorted(unknown))}")
    for name, raw in defaults.items():
        _validate_rule_fields(_object(raw, f"collection policy.class_defaults.{name}"), f"collection policy.class_defaults.{name}", objective=False)

    acquisition_ids: list[str] = []
    for index, raw in enumerate(_array(document["acquisitions"], "collection policy.acquisitions")):
        where = f"collection policy.acquisitions[{index}]"
        row = _object(raw, where)
        _keys(row, where, {"acquisition_id", "included", "priority", "target_interval_ms", "maximum_interval_ms"})
        acquisition_ids.append(str(_acquisition_ref(row["acquisition_id"], f"{where}.acquisition_id")))
        _boolean(row["included"], f"{where}.included")
        _integer(row["priority"], f"{where}.priority", 0, 100)
        target = _integer(row["target_interval_ms"], f"{where}.target_interval_ms", 1)
        maximum = _integer(row["maximum_interval_ms"], f"{where}.maximum_interval_ms", 1)
        if target > maximum:
            raise ValueError(f"{where}.target_interval_ms must not exceed maximum_interval_ms")
    _unique(acquisition_ids, "collection policy acquisition ids")

    objective_ids: list[tuple[str, str]] = []
    for index, raw in enumerate(_array(document["objectives"], "collection policy.objectives")):
        where = f"collection policy.objectives[{index}]"
        row = _object(raw, where)
        _validate_rule_fields(row, where, objective=True)
        objective_ids.append((str(_signal_ref(row["signal_ref"], f"{where}.signal_ref")), row["objective_id"]))
    _unique(objective_ids, "collection policy objective identities")


def validate_review(document: Mapping[str, Any]) -> None:
    document = _object(document, "signal review")
    _document_header(document, "signal review", "telemetry.signal-review", {"signals"})
    identities: list[tuple[str, str | None]] = []
    for index, raw in enumerate(_array(document["signals"], "signal review.signals")):
        where = f"signal review.signals[{index}]"
        row = _object(raw, where)
        _keys(
            row,
            where,
            {
                "signal_ref", "catalog_variant_id", "display_name_override",
                "formula_override", "unit_override", "evidence_state", "notes",
                "evidence_refs",
            },
        )
        signal_ref = str(_signal_ref(row["signal_ref"], f"{where}.signal_ref"))
        variant = row["catalog_variant_id"]
        if variant is not None:
            _string(variant, f"{where}.catalog_variant_id")
        for name in ("display_name_override", "formula_override", "unit_override"):
            if row[name] is not None:
                _string(row[name], f"{where}.{name}")
        _string(row["evidence_state"], f"{where}.evidence_state")
        _string(row["notes"], f"{where}.notes", nonempty=False)
        refs = _array(row["evidence_refs"], f"{where}.evidence_refs")
        for ref_index, ref in enumerate(refs):
            _tracked_relative_path(ref, f"{where}.evidence_refs[{ref_index}]")
        identities.append((signal_ref, variant))
    _unique(identities, "signal review identities")


def validate_local_settings(document: Mapping[str, Any]) -> None:
    document = _object(document, "local settings")
    _keys(document, "local settings", {"document_type", "schema_version", "project_id", "backend"})
    if document["document_type"] != "telemetry.local-settings" or document["schema_version"] != 1:
        raise ValueError("local settings must be telemetry.local-settings schema 1")
    stable_slug(_string(document["project_id"], "local settings.project_id"), "project_id")
    backend = _object(document["backend"], "local settings.backend")
    _keys(backend, "local settings.backend", {"kind", "executable", "settings"})
    kind = stable_slug(_string(backend["kind"], "local settings.backend.kind"), "backend kind")
    if kind not in BACKEND_SETTINGS:
        raise ValueError(f"local settings.backend.kind is not supported: {kind}")
    _string(backend["executable"], "local settings.backend.executable")
    settings = _object(backend["settings"], "local settings.backend.settings")
    required, optional = BACKEND_SETTINGS[kind]
    _keys(settings, "local settings.backend.settings", required, optional)
    for name, value in settings.items():
        _string(value, f"local settings.backend.settings.{name}")
    if kind == "pcan" and re.fullmatch(r"PCAN_USBBUS[1-8]", settings["channel"]) is None:
        raise ValueError(
            "local settings.backend.settings.channel must be PCAN_USBBUS1 through PCAN_USBBUS8"
        )
    if kind == "gsusb":
        for name in ("vendor_id", "product_id"):
            if _HEX_USB_ID.fullmatch(settings[name]) is None:
                raise ValueError(
                    f"local settings.backend.settings.{name} must be four hexadecimal digits"
                )
