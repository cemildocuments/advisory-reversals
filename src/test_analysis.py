import unittest
from analyze import outcome, replay, DAY
from versioning import version


def decision(changes, end=10):
    s = [{"time": 0, "order": 0, "state": "unaffected", "commit": "c0", "sha": "h0"}]
    s += [
        {
            "time": t * DAY,
            "order": i + 1,
            "state": v,
            "commit": "c" + str(i + 1),
            "sha": "h" + str(i + 1),
        }
        for i, (t, v) in enumerate(changes)
    ]
    return outcome({"decision_time": 0, "series": s}, end * DAY)


class ReplayTests(unittest.TestCase):
    def test_no_recheck(self):
        d = decision([(2, "affected"), (5, "unaffected"), (7, "affected")])
        r = replay(d, 10)
        self.assertEqual(r["stale_unaffected_days"], 6)
        self.assertEqual(r["alerts"], 0)

    def test_instant(self):
        r = replay(decision([(2, "affected"), (5, "unaffected")]), 10, delay=0)
        self.assertEqual(r["stale_unaffected_days"], 0)
        self.assertEqual(r["stale_affected_days"], 0)
        self.assertEqual(r["rechecks"], 2)

    def test_delayed_fifo_transient(self):
        r = replay(decision([(2, "affected"), (2.25, "unaffected")]), 10, delay=DAY)
        self.assertAlmostEqual(r["stale_unaffected_days"], 0.25)
        self.assertAlmostEqual(r["stale_affected_days"], 0.25)

    def test_daily(self):
        r = replay(decision([(2.25, "affected"), (5.25, "unaffected")]), 10, period=1)
        self.assertAlmostEqual(r["stale_unaffected_days"], 0.75)
        self.assertAlmostEqual(r["stale_affected_days"], 0.75)

    def test_semantic_edit_without_status_change_counts(self):
        d = decision([(2, "unaffected"), (5, "affected")])
        r = replay(d, 10, delay=0)
        self.assertEqual(r["rechecks"], 2)
        self.assertEqual(r["alerts"], 1)

    def test_censor(self):
        d = decision([(2, "affected"), (3, "absent"), (4, "unaffected")])
        self.assertEqual(d["end_time"], 3 * DAY)
        self.assertEqual(d["censor"], "absent")
        self.assertEqual(replay(d, 10)["stale_unaffected_days"], 1)

    def test_after_horizon(self):
        d = decision([(8, "affected")])
        self.assertEqual(replay(d, 5)["stale_unaffected_days"], 0)

    def test_equivalent_registry_versions(self):
        dates = {version("PyPI", "1.0.0"): 123}
        self.assertEqual(dates[version("PyPI", "1.0")], 123)


if __name__ == "__main__":
    unittest.main()
