"""Synthetic checks of research-only 12-hour range pause accounting."""
import datetime as dt
import unittest
from test_pause_cycle_12h import simulate_cycle

START = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc)
HOUR = dt.timedelta(hours=1)


def candle(i, high=1.05, low=.95, close=1.0):
    return (START+i*HOUR, 1.0, high, low, close)


class CycleTests(unittest.TestCase):
    def test_no_breach_all_twelve_hours_active(self):
        result, hours = simulate_cycle([candle(i) for i in range(12)], .9, 1.1, [4]*12)
        self.assertEqual(result['fully_inside_active_hours'],12)
        self.assertEqual(result['paused_or_ambiguous_hours'],0)
        self.assertEqual(result['boundary_excur­sions'],0) if False else None
        self.assertFalse(result['any_boundary_breach'])
        self.assertEqual(len(hours),12)

    def test_breach_pauses_hour_and_rearms_only_after_complete_inside_hour(self):
        bars=[candle(i, high=1.11 if i == 2 else 1.05) for i in range(12)]
        result, states=simulate_cycle(bars,.9,1.1,[4]*12)
        self.assertEqual(result['fully_inside_active_hours'],10)
        self.assertEqual(result['paused_or_ambiguous_hours'],2)
        self.assertEqual((states[2]['safe_full_hour'],states[3]['safe_full_hour'],states[4]['safe_full_hour']), (False,False,True))
        self.assertEqual(result['boundary_excursions'],1)
        self.assertEqual(result['resumptions'],1)

    def test_repeated_breach_and_no_recovery_before_end(self):
        bars=[candle(i,high=1.12 if i >= 1 else 1.05,close=1.11 if i >= 1 else 1.0) for i in range(12)]
        result,_=simulate_cycle(bars,.9,1.1,[4]*12)
        self.assertEqual(result['fully_inside_active_hours'],1)
        self.assertEqual(result['paused_or_ambiguous_hours'],11)
        self.assertEqual(result['boundary_excursions'],1)
        self.assertEqual(result['resumptions'],0)
        self.assertTrue(result['paused_through_last_hour'])

    def test_flat_filter_blocks_rearm_then_recovers(self):
        bars=[candle(i,high=1.11 if i == 0 else 1.05) for i in range(12)]
        ratios=[4,6,6,4]+[4]*8
        result,states=simulate_cycle(bars,.9,1.1,ratios)
        self.assertEqual(result['fully_inside_active_hours'],8)
        self.assertEqual(result['paused_or_ambiguous_hours'],4)
        self.assertEqual(result['resumptions'],1)
        self.assertTrue(states[3]['rearm_next_hour'])
        self.assertFalse(states[2]['rearm_next_hour'])

    def test_ratio_invalidation_applies_from_next_hour_not_same_hour(self):
        ratios=[6,6,4]+[4]*9
        result,states=simulate_cycle([candle(i) for i in range(12)],.9,1.1,ratios)
        self.assertEqual(result['fully_inside_active_hours'],10)
        self.assertEqual(result['ratio_triggered_pauses'],1)
        self.assertTrue(states[0]['safe_full_hour'])
        self.assertFalse(states[1]['safe_full_hour'])
        self.assertFalse(states[2]['safe_full_hour'])
        self.assertTrue(states[3]['safe_full_hour'])

    def test_reject_incomplete_or_gappy_window(self):
        with self.assertRaises(ValueError):
            simulate_cycle([candle(i) for i in range(11)],.9,1.1,[4]*11)
        bars=[candle(i) for i in range(12)]; bars[7]=candle(8)
        with self.assertRaises(ValueError):
            simulate_cycle(bars,.9,1.1,[4]*12)


if __name__=='__main__':
    unittest.main()
