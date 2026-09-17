"""Synthetic tests of event-level outcome accounting, no external market data."""
import datetime as dt
import unittest

from evaluate_tpo_breakouts import future_exit, group_cycles, interval, score

BASE = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc)


def bars(closes, missing_at=None):
    result=[]
    for i, close in enumerate(closes):
        hour=i+(1 if missing_at is not None and i >= missing_at else 0)
        result.append((BASE+dt.timedelta(hours=hour),close,close,close,close))
    return result


class EvaluationTests(unittest.TestCase):
    def test_sustained_breakout_requires_third_close_and_extension(self):
        prices=[1.201,1.203,1.206]+[1.20]*9
        outcome=future_exit(bars(prices),0,1.10,1.20,BASE+dt.timedelta(hours=20))
        self.assertTrue(outcome['sustained'])
        self.assertEqual(outcome['first_close'],0)
        self.assertEqual(outcome['third_close'],2)
        self.assertEqual(outcome['side'],1)

    def test_failed_wick_and_reentry_is_false(self):
        prices=[1.201,1.202,1.19]+[1.18]*9
        result=future_exit(bars(prices),0,1.10,1.20,BASE+dt.timedelta(hours=20))
        self.assertFalse(result['sustained'])

    def test_incomplete_or_gap_followup_is_censored(self):
        prices=[1.201,1.203,1.206]+[1.20]*9
        self.assertIsNone(future_exit(bars(prices,missing_at=5),0,1.10,1.20,BASE+dt.timedelta(hours=20)))
        self.assertIsNone(future_exit(bars(prices),0,1.10,1.20,BASE+dt.timedelta(hours=10)))

    def test_confirmation_outcome_does_not_use_signal_bar(self):
        prices=[1.208]+[1.19]*12
        result=future_exit(bars(prices),0,1.10,1.20,BASE+dt.timedelta(hours=20),offset=1)
        self.assertFalse(result['sustained'])

    def test_dedup_warning_and_no_invented_recall(self):
        def row(i,kind):
            return (BASE.isoformat() if i==0 else (BASE+dt.timedelta(hours=i)).isoformat(),kind,1.10,1.20,1.15,1.13,1.17)
        cycles=group_cycles([row(0,'start'),row(1,'warning'),row(2,'warning'),row(3,'confirmed_breakout')])
        self.assertEqual(len(cycles),1)
        self.assertEqual(len(cycles[0]['warnings']),2)
        prices=[1.19,1.201,1.202,1.206]+[1.21]*15
        report,cases=score(bars(prices),[row(0,'start'),row(1,'warning'),row(2,'warning'),row(3,'confirmed_breakout')],BASE+dt.timedelta(hours=30))
        self.assertEqual(report['cycles_with_warning'],1)
        self.assertEqual(report['duplicate_warning_events'],1)
        self.assertEqual(report['warning_sustained_exit_proxy']['success'],1)
        self.assertEqual(report['confirmation_capture_among_true_warning_events']['success'],1)
        self.assertEqual(report['median_confirmation_lag_after_first_outside_close_hours'],2)
        self.assertEqual(len(cases),1)


if __name__=='__main__':
    unittest.main()
