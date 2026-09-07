from __future__ import annotations

import unittest

from toyota_telemetry.live_progress import LiveObjectiveLedger


def objective(**changes) -> dict:
    value = {
        "signal_ref": "372/0x1001/0..7",
        "objective_id": "observe",
        "class": "exploration",
        "targets": {"minimum_total_observations": 2},
        "deadband": None,
        "applicability": None,
        "prior_progress": None,
    }
    value.update(changes)
    return value


def acquisition(**changes) -> dict:
    value = {
        "objective_ids": ["372/0x1001/0..7#observe"],
        "dependency_for": [],
    }
    value.update(changes)
    return value


class LiveObjectiveLedgerTests(unittest.TestCase):
    def test_total_target_completes_and_deactivates_acquisition(self) -> None:
        ledger = LiveObjectiveLedger([objective()])
        states = {"372/0x1001/0..7#observe": "true"}
        ledger.observe("372/0x1001/0..7", 1, 1.0, states, 0)
        self.assertTrue(ledger.acquisition_progress(acquisition()).active)
        ledger.observe("372/0x1001/0..7", 1, 1.0, states, 100)
        self.assertTrue(ledger.all_complete())
        self.assertFalse(ledger.acquisition_progress(acquisition()).active)

    def test_prior_progress_is_reused_without_inventing_a_new_reviewed_run(self) -> None:
        row = objective(prior_progress={"counters": {
            "total_observations": 1, "changed_observations": 0, "transitions": 0,
            "raw_states": [1], "applicable_seconds": 0, "time_span_seconds": 0,
            "distinct_reviewed_runs": 0,
        }})
        ledger = LiveObjectiveLedger([row])
        ledger.observe("372/0x1001/0..7", 2, 2.0,
                       {"372/0x1001/0..7#observe": "true"}, 0)
        self.assertTrue(ledger.all_complete())

    def test_unknown_condition_moves_target_to_background_but_keeps_dependency_active(self) -> None:
        condition = {"signal_ref": "372/0x1002/0..7", "operator": "eq",
                     "value": 1, "maximum_age_ms": 1000}
        target = objective(applicability=condition)
        ledger = LiveObjectiveLedger([target])
        self.assertTrue(ledger.acquisition_progress(acquisition()).background)
        dependency = acquisition(objective_ids=[], dependency_for=["372/0x1001/0..7#observe"])
        progress = ledger.acquisition_progress(dependency)
        self.assertTrue(progress.active)
        self.assertFalse(progress.background)


if __name__ == "__main__":
    unittest.main()
