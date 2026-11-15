"""Boundary and counterexample tests for the revision analyses."""

import unittest
from analyze import DAY, replay, outcome
from test_analysis import decision
from revision_analysis import (
    landmark_panel,
    deletion_gaps,
    bridge_short_deletions,
    replay_notification_latest,
    simple_bound_membership,
)


class RevisionTests(unittest.TestCase):
    def test_landmark_requires_event_free_supported_time_after_boundary(self):
        rows = [
            decision([(30, "affected")], 40),
            decision([(31, "affected")], 40),
            decision([(29, "deleted")], 40),
            decision([(30, "deleted")], 40),
            decision([], 40),
        ]
        self.assertEqual(len(landmark_panel(rows, 30)), 2)

    def test_short_gap_reentry_preserves_long_gap_and_withdrawal(self):
        d = decision(
            [
                (1, "deleted"),
                (1.5, "unaffected"),
                (2, "deleted"),
                (4, "unaffected"),
                (5, "withdrawn"),
            ],
            10,
        )
        # outcome ends at the first gap; reconstruct the complete input series explicitly.
        series = [
            {"time": t * DAY, "state": state, "sha": f"s{i}", "commit": f"c{i}"}
            for i, (t, state) in enumerate(
                [
                    (0, "unaffected"),
                    (1, "deleted"),
                    (1.5, "unaffected"),
                    (2, "deleted"),
                    (4, "unaffected"),
                    (5, "withdrawn"),
                ]
            )
        ]
        resumed = bridge_short_deletions(series)
        self.assertEqual([s["time"] / DAY for s in resumed], [0, 1.5, 2, 4, 5])
        r = outcome({"decision_time": 0, "series": resumed}, 10 * DAY)
        self.assertEqual(r["end_time"], 2 * DAY)

    def test_restoration_counts_content_identity_separately(self):
        series = [
            {"time": 0, "state": "unaffected", "sha": "same", "commit": "a"},
            {"time": DAY, "state": "deleted", "sha": "0" * 40, "commit": "b"},
            {"time": DAY + 60, "state": "unaffected", "sha": "same", "commit": "c"},
        ]
        g = deletion_gaps(series)[0]
        self.assertTrue(g["same_blob"])
        self.assertTrue(g["same_classification"])
        self.assertTrue(g["bridge_eligible"])
        self.assertEqual(g["restoration_delay_days"], 60 / DAY)

    def test_unrestored_gap_is_not_bridged(self):
        series = [
            {"time": 0, "state": "unaffected", "sha": "x", "commit": "a"},
            {"time": DAY, "state": "deleted", "sha": "0" * 40, "commit": "b"},
        ]
        self.assertEqual(bridge_short_deletions(series), series)
        self.assertIsNone(deletion_gaps(series)[0]["restoration_time"])

    def test_latest_query_avoids_replaying_resolved_transient_state(self):
        d = decision([(2, "affected"), (2.25, "unaffected")])
        d["revision_actions"] = d["series"][1:]
        fifo = replay(d, 10, delay=DAY)
        latest = replay_notification_latest(d, 10, DAY)
        self.assertEqual(fifo["stale_affected_days"], 0.25)
        self.assertEqual(latest["stale_affected_days"], 0)
        self.assertEqual(latest["stale_unaffected_days"], 0.25)
        self.assertEqual(latest["outdated_notification_payloads"], 1)

    def test_latest_query_matches_fifo_for_isolated_transitions(self):
        d = decision([(2, "affected"), (5, "unaffected")])
        d["revision_actions"] = d["series"][1:]
        a = replay(d, 10, delay=DAY)
        b = replay_notification_latest(d, 10, DAY)
        for k in ("rechecks", "alerts", "stale_unaffected_days", "stale_affected_days"):
            self.assertEqual(a[k], b[k])

    def test_earlier_notification_can_reveal_later_state(self):
        d = decision([(2, "unaffected"), (2.5, "affected")])
        d["revision_actions"] = d["series"][1:]
        self.assertEqual(replay(d, 10, delay=DAY)["stale_unaffected_days"], 1)
        self.assertEqual(
            replay_notification_latest(d, 10, DAY)["stale_unaffected_days"], 0.5
        )

    def test_annotation_bounds_follow_version_equivalence(self):
        self.assertTrue(simple_bound_membership("PyPI", "3.5", "<= 3.5.0"))
        self.assertFalse(simple_bound_membership("npm", "3.5.3", "< 2.1.3"))
        self.assertIsNone(
            simple_bound_membership("npm", "3.5.3", "< 2.1.3 || >= 4.0.0")
        )


if __name__ == "__main__":
    unittest.main()
