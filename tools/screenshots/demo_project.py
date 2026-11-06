"""Build a synthetic demo workspace for documentation screenshots.

Everything here is invented: ECUs, CAN IDs, DIDs, names, formulas, and the
drives. Verification and collection go through the toolkit's own services
with simulated probes and a simulated worker on a simulated clock, so the
recordings have the real format and nothing touches a vehicle or adapter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import random
import threading
import uuid
from typing import Callable

from toyota_telemetry.capture_writer import NativeRunWriter
from toyota_telemetry.collector import CollectionRunner, load_verified_plan
from toyota_telemetry.operator.service import OperatorService
from toyota_telemetry.operator.sources import NormalizedFileSource
from toyota_telemetry.repository import ProjectRepository


RECIPE = "extended"
PARK, REVERSE, NEUTRAL, DRIVE = 0, 1, 2, 3

# ECU key, name, request ID, response ID; DIDs with byte length and fields
# (first bit, last bit, name, formula, unit, states, encode(drive state)).
ECUS = [
    (1, "Engine", "0x7E0", "0x7E8", {
        0x0110: (4, [
            (0, 15, "Engine speed", "raw / 4", "rpm", None, lambda s: s["rpm"] * 4),
            (16, 23, "Coolant temperature", "raw - 40", "°C", None, lambda s: s["coolant"] + 40),
            (24, 31, "Intake air temperature", "raw - 40", "°C", None, lambda s: s["intake"] + 40),
        ]),
        0x0111: (2, [(0, 15, "Battery voltage", "raw / 1000", "V", None, lambda s: s["volts"] * 1000)]),
        0x0112: (1, [(0, 7, "Throttle position", "raw * 100 / 255", "%", None,
                      lambda s: s["throttle"] * 255 / 100)]),
        0x0113: (2, [(0, 15, "EGR valve position", "raw / 2", "%", None, lambda s: 0)]),
    }),
    (2, "Hybrid control", "0x7D2", "0x7DA", {
        0x0120: (4, [
            (0, 7, "Battery state of charge", "raw / 2", "%", None, lambda s: s["soc"] * 2),
            (8, 23, "Battery current", "signed(raw, 16) / 10", "A", None,
             lambda s: round(s["current"] * 10) & 0xFFFF),
            (24, 31, "Shift position", "raw", "", {"0": "P", "1": "R", "2": "N", "3": "D"},
             lambda s: s["shift"]),
        ]),
        0x0121: (2, [(0, 15, "Motor speed", "signed(raw, 16)", "rpm", None,
                      lambda s: round(s["motor"]) & 0xFFFF)]),
        0x0122: (1, [(0, 7, "EV mode", "raw", "", {"0": "OFF", "1": "ON"}, lambda s: s["ev"])]),
    }),
    (3, "Brake", "0x7B0", "0x7B8", {
        0x0130: (2, [(0, 15, "Vehicle speed", "raw / 100", "km/h", None, lambda s: s["speed"] * 100)]),
        0x0131: (1, [
            (0, 0, "Brake pedal", "raw", "", {"0": "OFF", "1": "ON"}, lambda s: s["brake"]),
            (1, 1, "Parking brake", "raw", "", {"0": "Released", "1": "Applied"}, lambda s: s["parking"]),
        ]),
        0x0132: (2, [(0, 15, "Brake pad wear", "raw / 10", "mm", None, None)]),
    }),
]
UNSUPPORTED = {0x0132}
EXTRA_CANDIDATES = [
    {"ecu_key": 1, "ecu_name": "Engine", "tester_can_id": "0x7E0", "response_can_id": "0x7E8",
     "data_id": "0x0114", "first_bit": 0, "last_bit": 7, "name": "Fuel level", "formula": "raw / 2",
     "unit": "L"},
    {"ecu_key": 4, "ecu_name": "Body", "tester_can_id": "0x750", "response_can_id": "0x758",
     "data_id": "0x0140", "first_bit": 0, "last_bit": 7, "name": "Door status", "formula": "raw",
     "unit": "", "states": {"0": "Closed", "1": "Open"}},
]


def candidate_rows() -> list[dict]:
    rows = []
    for key, ecu, tester, response, dids in ECUS:
        for did, (_size, fields) in dids.items():
            for first, last, name, formula, unit, states, _encode in fields:
                row = {
                    "ecu_key": key, "ecu_name": ecu, "tester_can_id": tester, "response_can_id": response,
                    "data_id": f"0x{did:04X}", "first_bit": first, "last_bit": last,
                    "name": name, "formula": formula, "unit": unit,
                }
                if states:
                    row["states"] = states
                rows.append(row)
    return rows + EXTRA_CANDIDATES


def did_layout(did: int) -> tuple[int, list]:
    for _key, _ecu, _tester, _response, dids in ECUS:
        if did in dids:
            return dids[did]
    raise KeyError(did)


def encode(did: int, state: dict) -> str:
    size, fields = did_layout(did)
    value = 0
    for first, last, *_rest, encoder in fields:
        width = last - first + 1
        raw = 0 if encoder is None else max(0, min((1 << width) - 1, int(round(encoder(state)))))
        value |= raw << (size * 8 - 1 - last)
    data = value.to_bytes(size, "big")
    return " ".join(f"{byte:02X}" for byte in (0x62, did >> 8, did & 0xFF, *data))


def drive(length_s: float, cruise_kmh: float, seed: int) -> Callable[[float], dict]:
    """A plausible drive: park, town, an open road, back to town, park."""

    rng = random.Random(seed)
    phases = [(20, 0), (60, 50), (200, 50), (260, cruise_kmh), (length_s - 300, cruise_kmh),
              (length_s - 240, 50), (length_s - 120, 50), (length_s - 60, 0), (length_s, 0)]
    wobble = [(rng.uniform(0.01, 0.05), rng.uniform(0, 6)) for _ in range(3)]
    state = {"soc": 58.0 + rng.uniform(-4, 4), "last": 0.0, "speed": 0.0}

    def speed_at(t: float) -> float:
        previous = (0.0, 0.0)
        for end, target in phases:
            if t <= end:
                start, base = previous
                share = (t - start) / max(1e-6, end - start)
                value = base + (target - base) * (0.5 - 0.5 * math.cos(math.pi * share))
                if base == target and target > 0:
                    ease = math.sin(math.pi * share) ** 2  # no jump where a steady stretch starts or ends
                    value += ease * sum(2.5 * math.sin(f * t + p) for f, p in wobble)
                return max(0.0, value)
            previous = (end, target)
        return 0.0

    def at(t: float) -> dict:
        speed = speed_at(t)
        accel = (speed_at(t + 1) - speed_at(t - 1)) / 2 / 3.6
        engine_on = speed > 45 or accel > 0.9 or t < 45
        rpm = (1000 + 25 * speed + 300 * max(0.0, accel) + rng.uniform(-30, 30)) if engine_on else 0.0
        current = 40 * accel + 0.25 * speed - (30 if engine_on and speed > 60 else 0) + rng.uniform(-1.5, 1.5)
        dt = max(0.0, t - state["last"])
        state["soc"] = max(40.0, min(75.0, state["soc"] - current * dt / 3600 * 0.6))
        state["last"] = t
        parked = t < 12 or t > length_s - 12
        return {
            "rpm": rpm,
            "coolant": 20 + 68 * (1 - math.exp(-t / 260)),
            "intake": 24 + 3 * math.sin(t / 300) + rng.uniform(-0.4, 0.4),
            "volts": 14.2 + 0.08 * math.sin(t / 7) + rng.uniform(-0.02, 0.02),
            "throttle": max(0.0, min(100.0, 30 * accel + 0.2 * speed)) if engine_on else 0.0,
            "soc": state["soc"],
            "current": current,
            "shift": PARK if parked else DRIVE,
            "motor": speed * 92,
            "ev": int(not engine_on and speed > 1),
            "speed": speed,
            "brake": int(accel < -0.25 or (speed < 0.5 and not parked)),
            "parking": int(t < 8 or t > length_s - 8),
        }

    return at


class SimulatedWorker:
    """Answers each read from the drive at the simulated time."""

    def __init__(self, endpoint_id: str, clock: list[int], profile: Callable[[float], dict]) -> None:
        self.endpoint_id = endpoint_id
        self.clock = clock
        self.profile = profile

    def start(self) -> dict:
        return {"event": "ready", "schema_version": 1, "endpoint_id": self.endpoint_id,
                "backend_kind": "synthetic"}

    def exchange(self, sequence: int, transaction_id: str, event_sink) -> dict:
        did = int(transaction_id.rsplit("/0x", 1)[1], 16)
        self.clock[0] += 12
        return {"event": "result", "schema_version": 1, "sequence": sequence, "outcome": "positive",
                "response_hex": encode(did, self.profile(self.clock[0] / 1000)), "nrc": None,
                "pending_count": 0, "pending_duration_ms": 0, "latency_ms": 12, "detail": None}

    def request_stop(self) -> None:
        pass

    def close(self) -> None:
        pass

    def terminate(self) -> None:
        pass


class SimulatedProbes:
    """Every endpoint answers; reads answer with a resting value, except unsupported DIDs."""

    def __call__(self, config_path: Path):
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        read = None
        if "preflight_read" in config:
            did = int(config["preflight_read"]["request_hex"].replace(" ", "")[2:], 16)
            if did in UNSUPPORTED:
                read = {"attempted": True, "outcome": "negative", "latency_ms": 11, "nrc": "0x31",
                        "response_hex": None}
            else:
                read = {"attempted": True, "outcome": "positive", "latency_ms": 11, "nrc": None,
                        "response_hex": encode(did, drive(900, 100, 1)(0))}
        result = {
            "event": "probe_result", "schema_version": 1, "candidate_id": config["candidate_id"],
            "backend_kind": next(iter(config["device"])), "responded": True,
            "session_steps": [
                {"label": "tester present", "outcome": "positive", "latency_ms": 9, "nrc": None,
                 "response_hex": "7E 00"},
                {"label": "extended session", "outcome": "positive", "latency_ms": 10, "nrc": None,
                 "response_hex": "50 03"},
            ],
            "read": read,
        }

        class Worker:
            def probe(self) -> dict:
                return result

        return Worker()


def build(root: Path) -> Path:
    service = OperatorService.create(root, profile_id="example-car")
    repository = service.repository()
    transport = json.loads(repository.paths.transport.read_text(encoding="utf-8"))
    transport["safety"].update({"request_start_floor_ms": 100, "maximum_request_rate_hz": 10,
                                "maximum_nominal_session_utilization": 0.5})
    transport["session_recipes"] = [{"id": RECIPE, "steps": [
        {"request_hex": "3E 00", "expected_prefix_hex": "7E 00", "label": "tester present",
         "tolerate_negative": True},
        {"request_hex": "10 03", "expected_prefix_hex": "50 03", "label": "extended session",
         "tolerate_negative": False},
    ]}]
    repository.write_authored_document("transport", transport)
    service.save_connector(service.connector_document("pcan", {"channel": "PCAN_USBBUS1"}))

    rows = root / "state" / "demo-candidates.json"
    rows.write_text(json.dumps(candidate_rows(), indent=2, ensure_ascii=False), encoding="utf-8")
    service.apply_source(service.preview_source(NormalizedFileSource(rows).read()))
    rows.unlink()

    probes = SimulatedProbes()
    for key, _ecu, tester, response, dids in ECUS:
        pair = f"{tester[2:].lower()}-{response[2:].lower()}"
        tree = service.tree()
        tokens = [f"{pair}/0x{did:04X}" for did in dids]
        scope = service.verification_scope(tree, service.select(tree, tokens), session_recipe_id=RECIPE)
        service.verify(scope, probe_worker_factory=probes)
        service.confirm_endpoint(f"{pair}-{RECIPE}", [key], ["Answered with the engine running"])

    tree = service.tree()
    intervals = {0x0110: 200, 0x0111: 1000, 0x0112: 500, 0x0113: 2000, 0x0120: 500, 0x0121: 200,
                 0x0122: 1000, 0x0130: 200, 0x0131: 200}
    policy = None
    for _key, _ecu, tester, response, dids in ECUS:
        endpoint = f"{tester[2:].lower()}-{response[2:].lower()}-{RECIPE}"
        for did in dids:
            if did in intervals:
                policy = service.edit_dids(tree, [f"{endpoint}/0x{did:04X}"],
                                           target_interval_ms=intervals[did],
                                           maximum_interval_ms=intervals[did] * 3, policy=policy).policy
    targets = {
        "1/0x0110/0..15": {"minimum_changes": 200},
        "1/0x0110/16..23": {"minimum_span_seconds": 7200},
        "1/0x0111/0..15": {"minimum_samples": 600},
        "1/0x0112/0..7": {"minimum_changes": 100},
        "1/0x0113/0..15": {"minimum_changes": 10},
        "2/0x0120/0..7": {"minimum_runs": 3},
        "2/0x0120/8..23": {"minimum_changes": 300},
        "2/0x0120/24..31": {"required_states": [0, 1, 2, 3]},
        "2/0x0122/0..7": {"minimum_transitions": 6},
        "3/0x0130/0..15": {"minimum_runs": 3, "minimum_samples": 15000},
        "3/0x0131/0..0": {"minimum_transitions": 20},
        "3/0x0131/1..1": {"required_states": [0, 1]},
    }
    differences = {"1/0x0110/0..15": 50, "1/0x0112/0..7": 2, "1/0x0113/0..15": 1, "2/0x0120/8..23": 1}
    for ref, requirements in targets.items():
        extra = {"minimum_difference": differences[ref]} if ref in differences else {}
        policy = service.edit_targets(tree, [ref], requirements=requirements, policy=policy, **extra).policy
    service.save(policy=policy)

    started = datetime(2026, 10, 3, 8, 40, tzinfo=timezone.utc)
    trips = [(1, 900, 100), (2, 720, 80), (3, 840, 110)]
    for day, (seed, length_s, cruise) in enumerate(trips):
        for index, (_key, _ecu, tester, response, _dids) in enumerate(ECUS):
            endpoint = f"{tester[2:].lower()}-{response[2:].lower()}-{RECIPE}"
            readiness = service.readiness(endpoint)
            if not readiness.ready:
                continue
            created = started + timedelta(days=4 * day, minutes=20 * index)
            simulate_run(root, endpoint, created, drive(length_s, cruise, seed * 10 + index), length_s)
    service.rebuild_cache()
    return root


def simulate_run(root: Path, endpoint: str, created: datetime, profile, length_s: float) -> None:
    repository = ProjectRepository(root)
    plan_path = repository.paths.plans / "admitted-plan.json"
    plan, plan_bytes = load_verified_plan(plan_path, repository)
    clock = [0]
    utc = lambda: (created + timedelta(milliseconds=clock[0])).isoformat().replace("+00:00", "Z")
    writer = NativeRunWriter(
        repository.paths.captures, plan, plan_bytes,
        run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{endpoint}{created}")),
        created_utc=utc(), backend_kind="synthetic", endpoint_id=endpoint,
    )

    def wait(delay: int) -> None:
        clock[0] += delay

    runner = CollectionRunner(
        plan, writer, SimulatedWorker(endpoint, clock, profile),
        event_sink=lambda _event: None, stop_event=threading.Event(),
        monotonic_ms=lambda: clock[0], wait_ms=wait, utc_now=utc,
        heartbeat_ms=10**9, max_duration_ms=int(length_s * 1000),
    )
    runner.run()


if __name__ == "__main__":
    import sys

    print(build(Path(sys.argv[1])))
