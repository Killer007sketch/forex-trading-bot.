"""Synthetic deterministic tests for non-destructive research variants."""
import datetime as dt
import unittest

from atr_stability_experiment_h1 import gate, variants
from compare_tpo_atr_h1 import PROFILE_HOURS, range_atr_signal

START = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
HOUR = dt.timedelta(hours=1)


def bar(i, h=1.0001, l=.9999, c=1.0):
    return (START + i*HOUR, c, h, l, c)


class ATRStabilityTests(unittest.TestCase):
    def test_stable_interior(self):
        history = [bar(i) for i in range(PROFILE_HOURS)]
        self.assertEqual(variants(history, .9999, 1.0001), (True, True))

    def test_close_at_boundary_rejects_only_interior(self):
        history = [bar(i, c=1.0001) for i in range(PROFILE_HOURS)]
        self.assertEqual(variants(history, .9999, 1.0001), (True, False))

    def test_single_latest_spike_cannot_be_flat(self):
        history = [bar(i) for i in range(PROFILE_HOURS)]
        history[-1] = bar(PROFILE_HOURS-1, h=1.02, l=.98, c=1.0)
        self.assertGreater(range_atr_signal(history), 5)
        self.assertEqual(variants(history, .98, 1.02), (False, False))

    def test_never_use_future(self):
        past = [bar(i) for i in range(PROFILE_HOURS)]
        before = variants(past, .9999, 1.0001)
        next_hour = bar(PROFILE_HOURS, h=1.5, l=.5, c=1.0)
        self.assertEqual(variants(past, .9999, 1.0001), before)
        self.assertNotEqual(variants(past+[next_hour], .9999, 1.0001), before)

    def test_gate_requires_improvement_and_coverage(self):
        base = {'flat_predictions':100,'balanced_proxy_accuracy_pct':55.0,
                'breakout_risk_among_predicted_flat_pct':15.0,'flat_recall_pct':50.0}
        valid = {'flat_predictions':90,'balanced_proxy_accuracy_pct':56.1,
                 'breakout_risk_among_predicted_flat_pct':13.9,'flat_recall_pct':46.0}
        self.assertTrue(gate(base,valid))
        self.assertFalse(gate(base,dict(valid,flat_recall_pct=44.0)))
        self.assertFalse(gate(base,dict(valid,breakout_risk_among_predicted_flat_pct=14.5)))
        self.assertFalse(gate(base,dict(valid,balanced_proxy_accuracy_pct=55.5)))


if __name__ == '__main__':
    unittest.main()
