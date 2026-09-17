"""Deterministic, synthetic causality / profile tests. Not market accuracy tests."""
import datetime as dt
import unittest

from tpo_range_h1 import (FOUR, HOUR, PROFILE_HOURS, contiguous, detect, profile)

UTC = dt.timezone.utc


class TpoResearchTests(unittest.TestCase):
    def setUp(self):
        self.base = dt.datetime(2024, 1, 1, tzinfo=UTC)

    def bar(self, index, high=1.101, low=1.099, close=1.100):
        return (self.base+index*HOUR, 1.100, high, low, close)

    def test_poc_and_value_area_deterministic(self):
        bars = [self.bar(i, 1.1002, 1.1000, 1.1001) for i in range(10)]
        p = profile(bars)
        self.assertEqual(p['poc'], 1.1001)
        self.assertLessEqual(p['low'], p['val'])
        self.assertLessEqual(p['val'], p['poc'])
        self.assertLess(p['poc'], p['vah'])
        self.assertLessEqual(p['vah'], p['high']+.0001)
        self.assertEqual(p, profile(bars))

    def test_incomplete_history_never_reconstructed(self):
        bars = [self.bar(i) for i in range(PROFILE_HOURS)]
        self.assertTrue(contiguous(bars, self.base+PROFILE_HOURS*HOUR))
        self.assertFalse(contiguous(bars, self.base+(PROFILE_HOURS+1)*HOUR))
        bars[40] = (bars[40][0]+HOUR, *bars[40][1:])
        self.assertFalse(contiguous(bars, self.base+PROFILE_HOURS*HOUR))

    def test_future_bar_cannot_change_past_profile(self):
        past = [self.bar(i) for i in range(PROFILE_HOURS)]
        before = profile(past)
        future = self.bar(PROFILE_HOURS, 1.1500, 1.0500, 1.105)
        self.assertEqual(before, profile((past+[future])[:-1]))

    def test_breakout_confirmation_h1_and_no_rearm_inside_same_range(self):
        h1 = [self.bar(i) for i in range(900)]
        # Signal arrives at H4[221] close, t = H1[888] OPEN.
        for j in (889, 890):
            h1[j] = self.bar(j, 1.103, 1.100, 1.102)
        h4 = [(self.base+i*FOUR, 1.100, 1.101, 1.099, 1.100)
              for i in range(225)]
        labels = ['transition']*225
        for i in range(219, 225):
            labels[i] = 'range'
        evidence = [5]*225
        atr = [.0004]*225
        metrics, rows = detect(h1, h4, labels, evidence, atr, 220, 225, 6.0)
        self.assertEqual(metrics['events'].get('cycles_started'), 1)
        self.assertEqual(metrics['events'].get('confirmed_close_breakout'), 1)
        self.assertTrue(any(r[1] == 'warning' for r in rows))
        self.assertTrue(any(r[1] == 'confirmed_breakout' for r in rows))
        self.assertEqual(rows[0][0], h1[888][0].isoformat())


if __name__ == '__main__':
    unittest.main()
