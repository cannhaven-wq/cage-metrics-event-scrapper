"""Cancelled bouts are retired, never deleted, and never on a bad read.

    python -m unittest test_card_sync -v

No network and no database.
"""
from __future__ import annotations

import types
import unittest

from card_sync import dropped_bookings, retire_dropped_bouts, ufc_id_from_url


def row(i, ufc, winner=None, method=None, active=True):
    return {"id": i, "ufc_fight_id": ufc, "winner_id": winner, "method": method,
            "is_active": active, "fighter_a_name": f"A{i}", "fighter_b_name": f"B{i}"}


# The real case: UFC Fight Night: Cortes Acosta vs. Walker, 2026-11-07.
CARD = [row(n, f"aa{n:02x}") for n in range(1, 13)] + [row(49281, "8f7e5ebb20694f22")]
LISTED = [f"aa{n:02x}" for n in range(1, 13)]


class TestSelection(unittest.TestCase):

    def test_cancelled_bout_is_retired(self):
        stale, refused = dropped_bookings(CARD, LISTED)
        self.assertIsNone(refused)
        self.assertEqual([r["id"] for r in stale], [49281])

    def test_nothing_dropped(self):
        stale, refused = dropped_bookings(CARD, LISTED + ["8f7e5ebb20694f22"])
        self.assertEqual((stale, refused), ([], None))

    def test_empty_page_is_a_failed_read(self):
        for listed in ([], None, [None, None]):
            stale, refused = dropped_bookings(CARD, listed)
            self.assertEqual(stale, [])
            self.assertIn("failed read", refused)

    def test_more_than_half_missing_retires_nothing(self):
        stale, refused = dropped_bookings(CARD, LISTED[:5])
        self.assertEqual(stale, [])
        self.assertIn("broken read", refused)

    def test_a_fight_with_a_result_is_never_retired(self):
        rows = [row(1, "u1"), row(2, "gone", winner=77), row(3, "gone2", method="Decision - Split"),
                row(4, "gone3", method="Overturned")]
        stale, refused = dropped_bookings(rows * 1, ["u1", "x", "y", "z"])
        self.assertIsNone(refused)
        self.assertEqual(stale, [])

    def test_already_retired_rows_are_left_alone(self):
        rows = CARD[:-1] + [row(49281, "8f7e5ebb20694f22", active=False)]
        stale, _ = dropped_bookings(rows, LISTED)
        self.assertEqual(stale, [])

    def test_url_ids(self):
        self.assertEqual(ufc_id_from_url("http://www.ufcstats.com/fight-details/8f7e5ebb20694f22"),
                         "8f7e5ebb20694f22")
        self.assertIsNone(ufc_id_from_url("http://www.ufcstats.com/somewhere-else"))
        self.assertIsNone(ufc_id_from_url(None))


class FakeDB:
    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.ops = rows, fail, []

    def table(self, name):
        db = self

        class Q:
            def select(self, *_a): self.op = ("select", None); return self
            def update(self, payload): self.op = ("update", payload); return self
            def delete(self): self.op = ("delete", None); return self
            def eq(self, col, val): self.filt = (col, val); return self

            def execute(self):
                if db.fail:
                    raise RuntimeError("network down")
                db.ops.append((name, self.op[0], self.op[1], self.filt))
                return types.SimpleNamespace(data=db.rows if self.op[0] == "select" else [])
        return Q()


class TestWrite(unittest.TestCase):

    URLS = [f"http://www.ufcstats.com/fight-details/{u}" for u in LISTED]

    def test_retire_is_an_update_never_a_delete(self):
        db = FakeDB(CARD)
        n = retire_dropped_bouts(db, "fights", 4629, self.URLS, log=lambda *_: None)
        self.assertEqual(n, 1)
        writes = [o for o in db.ops if o[1] != "select"]
        self.assertEqual(writes, [("fights", "update", {"is_active": False}, ("id", 49281))])
        self.assertFalse(any(o[1] == "delete" for o in db.ops))

    def test_failure_never_raises(self):
        n = retire_dropped_bouts(FakeDB(CARD, fail=True), "fights", 4629, self.URLS, log=lambda *_: None)
        self.assertEqual(n, 0)

    def test_empty_page_writes_nothing(self):
        db = FakeDB(CARD)
        self.assertEqual(retire_dropped_bouts(db, "fights", 4629, [], log=lambda *_: None), 0)
        self.assertEqual([o for o in db.ops if o[1] != "select"], [])


if __name__ == "__main__":
    unittest.main()
