"""Deterministic checks; no broker calls or external price data."""
import datetime as dt
import unittest
from compare_tpo_atr_h1 import alerts, contiguous, future_oracle, range_atr_signal, simple_atr, summarize

T = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
H = dt.timedelta(hours=1)


def bar(i, o=1.0, h=1.0001, l=.9999, c=1.0):
    return (T + i*H, o, h, l, c)


class CompareTests(unittest.TestCase):
    def test_range_atr_is_past_only_and_flat(self):
        history = [bar(i) for i in range(80)]
        self.assertAlmostEqual(simple_atr(history), .0002)
        self.assertAlmostEqual(range_atr_signal(history), 1.0)
        history.append(bar(80, 1, 2, .1, 1.5))
        self.assertGreater(range_atr_signal(history), 5.0)
        self.assertEqual(range_atr_signal(history[:-1]), 1.0)

    def test_gaps_are_censored(self):
        history = [bar(i) for i in range(20)]
        self.assertTrue(contiguous(history,T))
        history[7] = bar(8)
        self.assertFalse(contiguous(history,T))
        self.assertIsNone(simple_atr(history))

    def test_sustained_breakout_requires_three_and_extension(self):
        good = [bar(0, c=1.001), bar(1, c=1.002), bar(2, c=1.006)]
        label = future_oracle(good, .9, 1.0)
        self.assertTrue(label['breakout'])
        self.assertEqual((label['first'],label['third'],label['side']), (0,2,1))
        self.assertFalse(future_oracle(good[:2], .9, 1.0)['breakout'])
        self.assertFalse(future_oracle([bar(0,c=1.001),bar(1,c=.95),bar(2,c=1.006)],.9,1.0)['breakout'])

    def test_confirm_only_on_second_close(self):
        future=[bar(0,h=1.02,c=.99),bar(1,h=1.03,c=1.01),bar(2,h=1.04,c=1.02)]
        warning, confirm = alerts(future,.9,1.0)
        self.assertEqual(warning,0)
        self.assertEqual(confirm,(2,1,1))

    def test_confusion_on_identical_opportunities(self):
        def row(flag, breakout):
            return {'tpo':flag,'range_atr':not flag,'combined':False,
                    'oracle':{'breakout':breakout,'side':0,'first':None,'third':None},
                    'warning':None,'confirmation':None}
        episodes=[row(True,False),row(True,True),row(False,False),row(False,True)]
        scored=summarize(episodes,'tpo')
        self.assertEqual(scored['confusion_flat_TP_FP_TN_FN'],{'TP':1,'FP':1,'TN':1,'FN':1})
        self.assertEqual(scored['flat_precision_pct'],50.0)
        self.assertEqual(scored['overall_proxy_accuracy_pct'],50.0)
        self.assertEqual(scored['breakout_rejection_recall_pct'],50.0)


if __name__=='__main__':
    unittest.main()
