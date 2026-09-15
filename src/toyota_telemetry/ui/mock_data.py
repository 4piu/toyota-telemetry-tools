"""Coherent example-only inventory for the offline interaction prototype.

These are fictional UI fixtures, not vehicle authority or probe results.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExampleDid:
    key: str
    label: str
    endpoint: str
    source: str
    state: str
    baseline: str
    response_bytes: str
    latency: str
    signals: tuple[str, ...]


EXAMPLE_DIDS = (
    ExampleDid("F190", "Vehicle identifier", "Powertrain", "TIS database", "Responded", "Example identifier", "17", "22 ms", ("Vehicle identifier",)),
    ExampleDid("F40D", "Coolant temperature", "Powertrain", "TIS database", "Responded", "82 C", "2", "17 ms", ("Coolant temperature", "Coolant validity")),
    ExampleDid("F423", "Brake switches", "Powertrain", "TIS database", "Pending", "-", "-", "-", ("Brake switch",)),
    ExampleDid("F421", "Wheel speeds", "Hybrid control", "Normalized file", "Waiting for endpoint", "-", "-", "-", ("Wheel speed FL", "Wheel speed FR", "Wheel speed RL", "Wheel speed RR")),
    ExampleDid("F430", "Battery current", "Hybrid control", "TIS database", "Not attempted", "-", "-", "-", ("Battery current",)),
)

EXAMPLE_ENDPOINTS = (
    ("Powertrain", "0x7E0 / 0x7E8 / default", "Confirmed"),
    ("Hybrid control", "0x7E2 / 0x7EA / extended", "Probe required"),
)

REQUIRED_DIDS = frozenset(("F40D", "F423", "F421"))
READINESS_ISSUES = (
    ("F423", "DID 0xF423: brake-switch response timing is missing", "Verify DID"),
    ("F421", "DID 0xF421: hybrid endpoint and response timing are missing", "Verify DID"),
    ("Coolant validity", "Coolant-validity condition source needs review", "Review signal"),
)

EXAMPLE_SIGNAL_COUNT = sum(len(did.signals) for did in EXAMPLE_DIDS)

# Display-only target examples. Zero means that requirement is not selected.
EXAMPLE_TARGETS = {
    "Vehicle identifier": ("text", "raw bytes", "Always", 1, 0, 0, "No run loaded", "No target"),
    "Coolant temperature": ("C", "raw - 40", "ICE running", 12, 0, 0, "6 / 12 minutes", "Incomplete"),
    "Coolant validity": ("state", "bit 0", "Always", 0, 0, 0, "2 of 3 states", "Needs review"),
    "Brake switch": ("state", "bit 7", "Always", 0, 0, 0, "4 of 7 states", "Incomplete"),
    "Wheel speed FL": ("km/h", "raw * 0.01", "Vehicle moving", 500, 120, 1, "386 / 500 samples", "Incomplete"),
    "Wheel speed FR": ("km/h", "raw * 0.01", "Vehicle moving", 500, 120, 1, "375 / 500 samples", "Incomplete"),
    "Wheel speed RL": ("km/h", "raw * 0.01", "Vehicle moving", 500, 120, 1, "379 / 500 samples", "Incomplete"),
    "Wheel speed RR": ("km/h", "raw * 0.01", "Vehicle moving", 500, 120, 1, "371 / 500 samples", "Incomplete"),
    "Battery current": ("A", "raw * 0.1", "Always", 0, 0, 0, "No run loaded", "No target"),
}
