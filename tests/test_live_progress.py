from __future__ import annotations

import unittest

from toyota_telemetry.live_progress import LiveTargetLedger


SIGNAL = "372/0x1001/0..7"


def target(**changes) -> dict:
    value = {
        "signal_ref": SIGNAL,
        "included": True,
        "requirements": {"minimum_samples": 2},
        "minimum_difference": None,
        "condition": None,
        "prior_progress": None,
    }
    value.update(changes)
    return value


def acquisition(**changes) -> dict:
    value = {"objective_ids": [SIGNAL], "dependency_for": []}
    value.update(changes)
    return value


class LiveTargetLedgerTests(unittest.TestCase):
    def test_sample_target_completes_and_deactivates_acquisition(self) -> None:
        ledger = LiveTargetLedger([target()])
        ledger.observe(SIGNAL, 1, 1.0, "true", 0)
        self.assertTrue(ledger.acquisition_progress(acquisition()).active)
        ledger.observe(SIGNAL, 1, 1.0, "true", 100)
        self.assertTrue(ledger.all_complete())
        self.assertFalse(ledger.acquisition_progress(acquisition()).active)
        self.assertEqual(ledger.summary(), {"targets": 1, "complete": 1})

    def test_prior_progress_is_reused_and_current_run_counts_once(self) -> None:
        row = target(
            requirements={"minimum_samples": 2, "minimum_runs": 2},
            prior_progress={"counters": {
                "samples": 1, "changes": 0, "transitions": 0, "states": [1],
                "applicable_seconds": 0, "span_seconds": 0, "runs": 1,
            }},
        )
        ledger = LiveTargetLedger([row])
        self.assertFalse(ledger.all_complete())
        ledger.observe(SIGNAL, 2, 2.0, "true", 0)
        self.assertTrue(ledger.all_complete())

    def test_state_requirements_raise_opportunity_only_for_new_states(self) -> None:
        ledger = LiveTargetLedger([target(requirements={"required_states": [1, 2, 3]})])
        ledger.observe(SIGNAL, 1, 1.0, "true", 0)
        self.assertEqual(ledger.states[SIGNAL].opportunity, 1.0)
        ledger.observe(SIGNAL, 1, 1.0, "true", 100)
        self.assertEqual(ledger.states[SIGNAL].opportunity, 0.25)
        ledger.observe(SIGNAL, 2, 2.0, "true", 200)
        self.assertEqual(ledger.states[SIGNAL].opportunity, 1.0)

    def test_changes_need_the_minimum_difference(self) -> None:
        ledger = LiveTargetLedger([
            target(requirements={"minimum_changes": 1}, minimum_difference=1.0)
        ])
        ledger.observe(SIGNAL, 10, 10.0, "true", 0)
        ledger.observe(SIGNAL, 11, 10.5, "true", 100)
        self.assertFalse(ledger.all_complete())
        ledger.observe(SIGNAL, 12, 11.5, "true", 200)
        self.assertTrue(ledger.all_complete())

    def test_unknown_condition_moves_target_to_background_but_keeps_dependency_active(self) -> None:
        condition = {"signal_ref": "372/0x1002/0..7", "operator": "eq",
                     "value": 1, "maximum_age_ms": 1000}
        ledger = LiveTargetLedger([target(condition=condition)])
        self.assertTrue(ledger.acquisition_progress(acquisition()).background)
        dependency = acquisition(objective_ids=[], dependency_for=[SIGNAL])
        progress = ledger.acquisition_progress(dependency)
        self.assertTrue(progress.active)
        self.assertFalse(progress.background)


if __name__ == "__main__":
    unittest.main()
