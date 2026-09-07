from __future__ import annotations

import unittest

from toyota_telemetry.scheduler import DeterministicScheduler


def acquisition(
    data_id: int,
    *,
    priority: int = 50,
    target: int = 100,
    maximum: int = 500,
    cost: int = 10,
) -> dict:
    endpoint = "700-708-session"
    return {
        "acquisition_id": f"{endpoint}/0x{data_id:04X}",
        "endpoint_id": endpoint,
        "data_id": f"0x{data_id:04X}",
        "priority": priority,
        "target_interval_ms": target,
        "maximum_interval_ms": maximum,
        "cost": {"latency_p95_ms": cost},
        "objective_ids": [f"372/0x{data_id:04X}/0..7#observe"],
    }


class SchedulerTests(unittest.TestCase):
    def test_stable_tie_and_one_outstanding_request(self) -> None:
        scheduler = DeterministicScheduler([acquisition(0x1002), acquisition(0x1001)])
        decision = scheduler.select(0)
        self.assertEqual(decision.acquisition_id, "700-708-session/0x1001")
        with self.assertRaisesRegex(RuntimeError, "outstanding"):
            scheduler.select(1)
        scheduler.complete(decision.acquisition_id, 10, "positive")

    def test_priority_deficit_opportunity_and_cost_score_due_work(self) -> None:
        scheduler = DeterministicScheduler([
            acquisition(0x1001, priority=20, cost=5),
            acquisition(0x1002, priority=80, cost=20),
        ])
        scheduler.update_acquisition("700-708-session/0x1001", deficit=0.5)
        scheduler.update_acquisition("700-708-session/0x1002", opportunity=2.0)
        self.assertEqual(scheduler.select(0).acquisition_id, "700-708-session/0x1002")

    def test_maximum_deadline_preempts_higher_scored_target_due_work(self) -> None:
        scheduler = DeterministicScheduler([
            acquisition(0x1001, priority=1, target=100, maximum=200),
            acquisition(0x1002, priority=100, target=100, maximum=1000),
        ])
        first = scheduler.select(0)
        scheduler.complete(first.acquisition_id, 1, "positive")
        second = scheduler.select(1)
        scheduler.complete(second.acquisition_id, 2, "positive")
        decision = scheduler.select(202)
        self.assertEqual(decision.acquisition_id, "700-708-session/0x1001")
        self.assertEqual(decision.reason, "maximum_overdue")

    def test_aging_eventually_beats_static_priority(self) -> None:
        scheduler = DeterministicScheduler([
            acquisition(0x1001, priority=35, target=100, maximum=1000),
            acquisition(0x1002, priority=40, target=100, maximum=1000),
        ])
        scheduler.update_acquisition("700-708-session/0x1002", active=False)
        first = scheduler.select(0)
        scheduler.complete(first.acquisition_id, 0, "positive")
        scheduler.update_acquisition("700-708-session/0x1001", active=False)
        scheduler.update_acquisition("700-708-session/0x1002", active=True)
        second = scheduler.select(400)
        scheduler.complete(second.acquisition_id, 400, "positive")
        scheduler.update_acquisition("700-708-session/0x1001", active=True)
        self.assertEqual(scheduler.select(990).acquisition_id, "700-708-session/0x1001")

    def test_slack_fill_only_selects_work_that_fits(self) -> None:
        scheduler = DeterministicScheduler(
            [acquisition(0x1001, target=1000, maximum=2000, cost=20)], start_ms=100
        )
        self.assertIsNone(scheduler.select(50, slack_ms=19))
        decision = scheduler.select(50, slack_ms=20)
        self.assertEqual(decision.reason, "slack_fill")

    def test_completion_drops_missed_periods_without_catch_up(self) -> None:
        scheduler = DeterministicScheduler([acquisition(0x1001, target=100, maximum=500)])
        decision = scheduler.select(0)
        scheduler.complete(decision.acquisition_id, 1000, "positive")
        self.assertIsNone(scheduler.select(1001))
        self.assertEqual(scheduler.next_due_ms(), 1100)

    def test_request_floor_and_failure_backoff(self) -> None:
        scheduler = DeterministicScheduler(
            [acquisition(0x1001, target=10, maximum=100)],
            request_start_floor_ms=25,
        )
        decision = scheduler.select(0)
        scheduler.complete(decision.acquisition_id, 5, "timeout")
        self.assertIsNone(scheduler.select(20))
        self.assertEqual(scheduler.next_due_ms(), 30)
        retry = scheduler.select(30)
        self.assertEqual(retry.acquisition_id, decision.acquisition_id)

    def test_completed_or_inapplicable_work_can_be_deactivated(self) -> None:
        scheduler = DeterministicScheduler([acquisition(0x1001)])
        scheduler.update_acquisition("700-708-session/0x1001", deficit=0.0)
        self.assertIsNone(scheduler.select(1000, slack_ms=1000))
        self.assertIsNone(scheduler.next_due_ms())

    def test_phase_d_rejects_multiple_endpoints(self) -> None:
        other = acquisition(0x1002)
        other["endpoint_id"] = "7b0-7b8-session"
        other["acquisition_id"] = "7b0-7b8-session/0x1002"
        with self.assertRaisesRegex(ValueError, "exactly one endpoint"):
            DeterministicScheduler([acquisition(0x1001), other])


if __name__ == "__main__":
    unittest.main()
