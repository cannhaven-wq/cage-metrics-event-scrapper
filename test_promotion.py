"""T-086: a non-UFC card never reaches the UFC tables.

    python -m unittest test_promotion -v

No network and no database: `supabase.create_client` is replaced with a
recorder before the scrapers are imported, so every table touched is visible.
NAMES is the same list as Cage-Metrics tests/test_promotion_rule.py.
"""
from __future__ import annotations

import importlib
import sys
import types
import unittest

import promotion

NAMES = [
    ("UFC 300: Pereira vs. Hill", True),
    ("UFC 100", True),
    ("UFC 37.5: As Real As It Gets", True),
    ("UFC Fight Night: Buckley vs. Malott", True),
    ("UFC Fight Night 56: Shogun vs Saint Preux", True),
    ("UFC Fight Night 1", True),
    ("UFC Fight Night - Fight for the Troops", True),
    ("UFC on FOX: Johnson vs. Reis", True),
    ("UFC on FX: Belfort vs. Rockhold", True),
    ("UFC on FUEL TV: Nogueira vs. Werdum", True),
    ("UFC Live: Cruz vs Johnson", True),
    ("UFC Macao: Franklin vs Le", True),
    ("UFC: Silva vs Irvin", True),
    ("UFC - Ultimate Japan", True),
    ("UFC - Ultimate Ultimate '96", True),
    ("UFC 306: Riyadh Season Noche UFC", True),
    ("Noche UFC: Silva vs. Delgado", True),
    ("UFC Freedom 250", True),
    ("The Ultimate Fighter: Heavy Hitters Finale", True),
    ("The Ultimate Fighter: Team Joanna vs. Team Cláudia Finale", True),
    ("Ortiz vs Shamrock 3: The Final Chapter", True),
    ("UFC - Road to UFC 4.6", True),
    ("DWCS 10.3", False),
    ("DWCS 11.1", False),
    ("Dana White's Contender Series 2026: Week 5", False),
    ("Dana White’s Contender Series 2024: Week 1", False),
    ("Road to UFC Season 4: Episode 6", False),
    ("road to ufc 5.1", False),
    ("Bellator 300", False),
    ("PFL World Championship", False),
    ("", False),
]


class MissingTable(Exception):
    pass


class Recorder:
    """Stands in for the Supabase client. Records (table, operation)."""

    def __init__(self):
        self.calls = []
        self.missing = set()
        self._next = 1000

    def table(self, name):
        rec = self

        class Q:
            def __init__(self):
                self.op = None

            def _go(self, op, payload=None):
                self.op = op
                rec.calls.append((name, op, payload))
                return self

            def upsert(self, payload, on_conflict=None): return self._go("upsert", payload)
            def insert(self, payload): return self._go("insert", payload)
            def delete(self): return self._go("delete")
            def select(self, *_a, **_k): return self._go("select")
            def update(self, payload): return self._go("update", payload)
            def eq(self, *_a): return self
            def gte(self, *_a): return self
            def lt(self, *_a): return self
            def order(self, *_a, **_k): return self
            def range(self, *_a): return self

            def execute(self):
                if name in rec.missing:
                    raise MissingTable("{'code': 'PGRST205', 'message': \"Could not find the table "
                                       f"'public.{name}' in the schema cache\"}}")
                rec._next += 1
                data = [{"id": rec._next}] if self.op in ("upsert", "insert") else []
                return types.SimpleNamespace(data=data)

        return Q()


def load(module_name, recorder):
    fake = types.ModuleType("supabase")
    fake.create_client = lambda *_a, **_k: recorder
    fake.Client = object
    sys.modules["supabase"] = fake
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


class TestRule(unittest.TestCase):

    def test_names(self):
        for name, want in NAMES:
            with self.subTest(name=name):
                self.assertEqual(promotion.is_ufc_event_name(name), want)

    def test_labels(self):
        self.assertEqual(promotion.non_ufc_promotion("DWCS 10.3"), "DWCS")
        self.assertEqual(promotion.non_ufc_promotion("Dana White's Contender Series 2026: Week 5"), "DWCS")
        self.assertEqual(promotion.non_ufc_promotion("Road to UFC Season 4: Episode 6"), "RTU")
        self.assertEqual(promotion.non_ufc_promotion("Bellator 300"), "OTHER")


class TestRouting(unittest.TestCase):

    def run_card(self, module_name, name, missing=()):
        rec = Recorder()
        rec.missing = set(missing)
        mod = load(module_name, rec)
        event = {"ufc_event_id": "x1", "name": name, "event_date": "2026-08-25",
                 "location": "Las Vegas", "is_upcoming": False, "ufc_url": "http://u/x1"}
        event_id, tables = mod.upsert_event(event)
        if event_id:
            fid = mod.upsert_fight({"ufc_fight_id": "f1", "event_id": event_id}, tables)
            mod.upsert_rounds(fid, [{"round_number": 1}], tables)
        return rec, event_id

    def test_both_scrapers(self):
        for module_name in ("events_scraper", "weekly_refresh"):
            with self.subTest(scraper=module_name, case="UFC card"):
                rec, eid = self.run_card(module_name, "UFC Fight Night: Buckley vs. Malott")
                self.assertTrue(eid)
                touched = {t for t, _op, _p in rec.calls}
                self.assertEqual(touched, {"events", "fights", "fight_rounds"})

            with self.subTest(scraper=module_name, case="DWCS, tables present"):
                rec, eid = self.run_card(module_name, "DWCS 10.3")
                self.assertTrue(eid)
                touched = {t for t, _op, _p in rec.calls}
                self.assertEqual(touched, {"events_non_ufc", "fights_non_ufc", "fight_rounds_non_ufc"})
                payload = next(p for t, op, p in rec.calls if t == "events_non_ufc" and op == "upsert")
                self.assertEqual(payload["promotion"], "DWCS")

            with self.subTest(scraper=module_name, case="DWCS, T-086 not applied yet"):
                rec, eid = self.run_card(module_name, "DWCS 10.3",
                                         missing=("events_non_ufc", "fights_non_ufc", "fight_rounds_non_ufc"))
                self.assertIsNone(eid)
                touched = {t for t, _op, _p in rec.calls}
                self.assertNotIn("events", touched)
                self.assertNotIn("fights", touched)
                self.assertNotIn("fight_rounds", touched)

            with self.subTest(scraper=module_name, case="no name"):
                rec, eid = self.run_card(module_name, None)
                self.assertIsNone(eid)
                self.assertEqual(rec.calls, [])


if __name__ == "__main__":
    unittest.main()
