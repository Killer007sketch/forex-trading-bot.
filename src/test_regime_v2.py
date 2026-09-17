"""Deterministic regression tests for research-only market-regime diagnostic."""
import datetime as dt
import math
import unittest

from regime_v2_h4 import classify_v2, decide, diagnostic, efficiency


class RegimeV2Tests(unittest.TestCase):
    def test_efficiency_includes_every_forward_move(self):
        self.assertAlmostEqual(efficiency([1, 2, 3, 4, 5, 6, 7], 0), 1.0)
        self.assertAlmostEqual(efficiency([1, 2, 1, 2, 1, 2, 1], 0), 0.0)

    def test_two_confirmations_and_trend_hysteresis(self):
        self.assertEqual(decide('transition', 0, ('trend', 1), ('transition', 0), ('transition', 0)), ('transition', 0))
        self.assertEqual(decide('transition', 0, ('trend', 1), ('trend', 1), ('transition', 0)), ('trend', 1))
        self.assertEqual(decide('trend', 1, ('transition', 0), ('transition', 0), ('trend', 1)), ('trend', 1))
        self.assertEqual(decide('trend', 1, ('trend', -1), ('trend', -1), ('transition', 0)), ('transition', 0))

    def test_future_data_does_not_change_past_labels(self):
        base = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        bars = []
        for i in range(275):
            close = 1.1 + .0001 * i + .002 * math.sin(i / 5)
            bars.append((base + dt.timedelta(hours=4 * i), close, close + .001, close - .001, close))
        labels, directions = classify_v2(bars)
        mutated = list(bars)
        for i in range(251, len(bars)):
            t, _, _, _, _ = mutated[i]
            price = 1.5 + .02 * i
            mutated[i] = (t, price, price + .01, price - .01, price)
        changed_labels, changed_directions = classify_v2(mutated)
        self.assertEqual(labels[:251], changed_labels[:251])
        self.assertEqual(directions[:251], changed_directions[:251])

    def test_diagnostic_never_uses_future_from_next_segment(self):
        base = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        bars = [(base + dt.timedelta(hours=4 * i), 1.0, 1.01, 0.99, 1.0) for i in range(240)]
        labels = ['transition'] * len(bars)
        result = diagnostic(bars, labels, [0] * len(bars), 220, 225)
        self.assertEqual(result['forward_samples']['transition'], 0)


if __name__ == '__main__':
    unittest.main()
