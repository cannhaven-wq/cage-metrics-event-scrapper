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

    def test_upsert_before_prune(self):
        ok = self.wr.upsert_rounds(7, [{"round_number": 1}, {"round_number": 2}])
        self.assertTrue(ok)
        ops = [op for _t, op, _p in self.rec.calls if _t == "fight_rounds"]
        self.assertEqual(ops[0], "upsert", "rounds must be written before anything is deleted")
        self.assertNotIn("insert", ops)

    def test_failed_write_deletes_nothing(self):
        class Boom:
            def table(self, name):
                q = types.SimpleNamespace()
                def fail(*_a, **_k):
                    raise RuntimeError("write refused")
                q.upsert = fail
                q.delete = lambda: (_ for _ in ()).throw(AssertionError("deleted after a failed write"))
                return q
        self.wr.supabase = Boom()
        self.assertFalse(self.wr.upsert_rounds(7, [{"round_number": 1}]))

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

    def test_duplicate_booking_is_never_repaired(self):
        data = {
            "events": [{"id": 1}],
            "fights": [
                {"id": 43, "event_id": 1, "ufc_url": "old", "method": "Decision", "winner_id": 5,
                 "fighter_a_id": 5, "fighter_b_id": 6},
                {"id": 27872, "event_id": 1, "ufc_url": "new", "method": "Decision", "winner_id": 5,
                 "fighter_a_id": 6, "fighter_b_id": 5},
            ],
            "fight_rounds": [{"fight_id": 27872}],
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
        self.assertEqual(self.wr.fights_missing_rounds(), [])
        self.assertEqual(self.wr.STATS["rounds_duplicate_booking"], 1)


if __name__ == "__main__":
    unittest.main()
