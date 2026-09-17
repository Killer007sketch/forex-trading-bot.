"""Deterministic causality and proxy accounting tests, not evidence of trading success."""
import datetime as dt
import math
import unittest

from regime_v3_h4 import (WARMUP, classify_v3, completed_d1_context,
                          event_outcome, prequential_confidence, wilson_lower)


def synthetic(n=490):
    start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    bars = []
    for i in range(n):
        op = 1.1 + .00008*i + .0005*math.sin(i/8)
        close = 1.1 + .00008*(i+1) + .0005*math.sin((i+1)/8)
        bars.append((start + dt.timedelta(hours=4*i), op,
                     max(op, close)+.0003, min(op, close)-.0003, close))
    return bars


class RegimeV3Tests(unittest.TestCase):
    def test_prefix_invariance_no_future_candles(self):
        bars = synthetic()
        full = classify_v3(bars)
        prefix = classify_v3(bars[:427])
        for full_field, prefix_field in zip(full, prefix):
            self.assertEqual(full_field[:427], prefix_field)

    def test_d1_requires_whole_completed_utc_day(self):
        bars = synthetic(450)
        full = completed_d1_context(bars)
        prefix = completed_d1_context(bars[:443])
        self.assertEqual(full[:443], prefix)
        # Incomplete final day cannot be promoted into a completed D1 candle.
        self.assertEqual(len(full), 450)

    def test_wilson_requires_minimum_nonoverlap_count(self):
        self.assertIsNone(wilson_lower(29, 29))
        self.assertGreater(wilson_lower(29, 30), 0)
        self.assertLess(wilson_lower(29, 30), 1)

    def test_outcome_uses_frozen_channel_and_six_candles(self):
        bars = synthetic(260)
        labels = ['transition'] * len(bars)
        directions = [0] * len(bars)
        labels[230], directions[230] = 'trend', 1
        self.assertEqual(event_outcome(bars, labels, directions, 230),
                         bars[236][4] > bars[230][4])
        labels[230], directions[230] = 'range', 0
        high = max(b[2] for b in bars[211:231])
        low = min(b[3] for b in bars[211:231])
        expected = all(low <= bars[j][4] <= high for j in range(231, 237))
        self.assertEqual(event_outcome(bars, labels, directions, 230), expected)

    def test_online_confidence_cannot_read_unmatured_future(self):
        bars = synthetic(300)
        labels = ['warmup'] * WARMUP + ['trend'] * (len(bars) - WARMUP)
        directions = [0] * WARMUP + [1] * (len(bars) - WARMUP)
        votes = [0] * WARMUP + [5] * (len(bars) - WARMUP)
        estimates, n = prequential_confidence(bars, labels, directions, votes)
        modified = list(bars)
        t, o, hi, lo, _ = modified[290]
        modified[290] = (t, o, max(hi, o+.1), min(lo, o-.1), o+.1)
        estimates2, n2 = prequential_confidence(modified, labels, directions, votes)
        self.assertEqual(estimates[:290], estimates2[:290])
        self.assertEqual(n[:290], n2[:290])


if __name__ == '__main__':
    unittest.main()
