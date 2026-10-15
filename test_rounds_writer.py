"""rounds_writer: one transaction, bounded retries, and a stored count that
must match what was sent.

    python -m unittest test_rounds_writer -v
"""
from __future__ import annotations

import types
import unittest

import rounds_writer as rw


class ApiError(Exception):
    """Shaped like postgrest's APIError: carries .code."""
    def __init__(self, code, message="x"):
        super().__init__({"code": code, "message": message})
        self.code = code


class Client:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def rpc(self, fn, params):
        client = self

        class R:
            def execute(self):
                client.calls.append((fn, params))
                o = client.outcomes.pop(0)
                if isinstance(o, Exception):
                    raise o
                return types.SimpleNamespace(data=o)
        return R()


ROUNDS = [{"round_number": 1, "a_kd": 0, "id": 55, "fight_id": 999}, {"round_number": 2, "a_kd": 1}]


def run(client, table="fight_rounds", rounds=ROUNDS):
    slept = []
    n = rw.replace_rounds(client, table, 7349, rounds, sleep=slept.append, log=lambda *_a: None)
    return n, slept


class TestReplaceRounds(unittest.TestCase):

    def test_one_call_with_the_fight_id_and_clean_rows(self):
        c = Client(2)
        n, slept = run(c)
        self.assertEqual(n, 2)
        self.assertEqual(slept, [])
        fn, params = c.calls[0]
        self.assertEqual(fn, "replace_fight_rounds")
        self.assertEqual(params["p_fight_id"], 7349)
        for r in params["p_rounds"]:
            self.assertNotIn("id", r)
            self.assertNotIn("fight_id", r, "the function sets fight_id; a row must not name another fight")

    def test_non_ufc_table_uses_its_own_function(self):
        c = Client(2)
        run(c, table="fight_rounds_non_ufc")
        self.assertEqual(c.calls[0][0], "replace_fight_rounds_non_ufc")

    def test_unknown_table_is_refused_before_any_call(self):
        c = Client()
        with self.assertRaises(rw.RoundsWriteError):
            run(c, table="fight_rounds_backup")
        self.assertEqual(c.calls, [])

    def test_empty_set_is_refused_before_any_call(self):
        c = Client()
        with self.assertRaises(rw.RoundsWriteError):
            run(c, rounds=[])
        self.assertEqual(c.calls, [])

    def test_transient_errors_retry_with_backoff_then_succeed(self):
        c = Client(ConnectionResetError("reset"), ApiError("57014"), ApiError("502"), 2)
        n, slept = run(c)
        self.assertEqual((n, slept, len(c.calls)), (2, [2, 4, 8], 4))

    def test_retries_are_bounded(self):
        c = Client(*[TimeoutError("t")] * 4)
        with self.assertRaises(rw.RoundsWriteError) as cm:
            run(c)
        self.assertEqual(len(c.calls), rw.ATTEMPTS)
        self.assertIn("4 attempt", str(cm.exception))

    def test_permanent_errors_are_not_retried(self):
        for code in ("PGRST202", "P0001", "23503", "22P02", "42501", "400"):
            with self.subTest(code=code):
                c = Client(ApiError(code))
                with self.assertRaises(rw.RoundsWriteError):
                    run(c)
                self.assertEqual(len(c.calls), 1)

    def test_stored_count_must_match(self):
        for data in (1, 3, None, [], True, "2", [{"a": 1}, {"b": 2}]):
            with self.subTest(data=data):
                with self.assertRaises(rw.RoundsWriteError):
                    run(Client(data))

    def test_count_shapes_older_clients_return(self):
        for data in (2, [2], [{"replace_fight_rounds": 2}], {"replace_fight_rounds": 2}):
            with self.subTest(data=data):
                self.assertEqual(run(Client(data))[0], 2)

    def test_there_is_no_table_fallback(self):
        src = open(rw.__file__).read()
        self.assertNotIn(".table(", src)
        self.assertNotIn(".delete(", src)


if __name__ == "__main__":
    unittest.main()
