"""Round records: a failed write never erases rounds, and a completed fight
with no rounds is re-scraped and classified rather than skipped in silence.

    python -m unittest test_rounds -v

No network and no database, same arrangement as test_promotion.py.
"""
from __future__ import annotations

import os
import sys
import types
import unittest
from datetime import date, timedelta

import test_promotion


class Rounds(unittest.TestCase):
    def setUp(self):
        self.rec = test_promotion.Recorder()
        self.wr = test_promotion.load("weekly_refresh", self.rec)
        self.wr.time.sleep = lambda *_a: None

    def test_rounds_go_through_the_one_transaction_function(self):
        ok = self.wr.upsert_rounds(7, [{"round_number": 1}, {"round_number": 2}])
        self.assertTrue(ok)
        self.assertEqual([(t, op) for t, op, _p in self.rec.calls], [("replace_fight_rounds", "rpc")],
                         "rounds must never be written as separate delete / insert requests")

    def test_failed_write_is_counted_and_fails_the_run(self):
        self.rec.missing = {"replace_fight_rounds"}
        self.assertFalse(self.wr.upsert_rounds(7, [{"round_number": 1}]))
        self.assertEqual(self.wr.STATS["rounds_write_failed"], 1)
        self.assertNotIn("delete", [op for _t, op, _p in self.rec.calls], "no fallback to delete-then-insert")
        for name in ("phase_1_upcoming_events", "phase_2_recent_completed_events", "phase_3_resolve_null_fighters",
                     "phase_4_refresh_recent_fighters", "phase_5_repair_missing_rounds", "_load_fighter_cache"):
            setattr(self.wr, name, lambda: None)
        self.wr.STATS["events_seen"] = 5
        with self.assertRaises(SystemExit) as cm:
            self.wr.main()
        self.assertIn("round writes fail", str(cm.exception))

    def test_events_scraper_fails_the_run_too(self):
        rec = test_promotion.Recorder()
        rec.missing = {"replace_fight_rounds"}
        es = test_promotion.load("events_scraper", rec)
        self.assertFalse(es.upsert_rounds(7, [{"round_number": 1}]))
        self.assertEqual(es.ROUND_WRITE_FAILURES, [7])
        es.get_event_urls = lambda upcoming: []
        with self.assertRaises(SystemExit):
            es.main()

    def test_repair_classifies(self):
        f = {"id": 7349, "event_id": 1, "ufc_url": "u"}
        self.wr.parse_fight = lambda _u: (None, [])
        self.assertEqual(self.wr.repair_fight(f), "fetch_failed")
        self.wr.parse_fight = lambda _u: ({"ufc_fight_id": "x", "method": "KO/TKO"}, [])
        self.assertEqual(self.wr.repair_fight(f), "source_empty")

    def test_repair_never_sends_null(self):
        sent = {}
        self.wr.parse_fight = lambda _u: ({"ufc_fight_id": "x", "end_time": None, "method": "KO/TKO"},
                                           [{"round_number": 1}])
        self.wr.upsert_fight = lambda row, tables=None: sent.update(row) or 9
        self.wr.upsert_rounds = lambda *_a, **_k: True
        self.assertEqual(self.wr.repair_fight({"id": 9, "event_id": 3, "ufc_url": "u"}), "repaired")
        self.assertNotIn("end_time", sent)
        self.assertEqual(sent["event_id"], 3)

    def test_missing_rounds_selection(self):
        past = (date.today() - timedelta(days=3)).isoformat()
        data = {
            "events": [{"id": 1}],
            "fights": [
                {"id": 10, "event_id": 1, "ufc_url": "a", "method": "KO/TKO", "winner_id": 5},
                {"id": 11, "event_id": 1, "ufc_url": "b", "method": "KO/TKO", "winner_id": 5},
                {"id": 12, "event_id": 1, "ufc_url": "c", "method": None, "winner_id": None},
                {"id": 13, "event_id": 2, "ufc_url": "d", "method": "KO/TKO", "winner_id": 5},
            ],
            "fight_rounds": [{"fight_id": 10}],
        }
        class DB:
            def table(self, name):
                class Q:
                    def __getattr__(self, _n):
                        return lambda *_a, **_k: self
                    def execute(self):
                        return types.SimpleNamespace(data=data[name])
                return Q()
        self.wr.supabase = DB()
        got = [f["id"] for f in self.wr.fights_missing_rounds()]
        self.assertEqual(got, [11], "only a completed fight on a past card with no rounds")


if __name__ == "__main__":
    unittest.main()
