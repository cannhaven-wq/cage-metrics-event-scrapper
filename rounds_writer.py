"""Round records are replaced in one database transaction, or not at all.

On 2026-10-14 the scraper deleted fight 7349's rounds, then lost its connection
before the insert, and the fight kept its totals with no rounds behind them.
Writing first and pruning second (the previous fix) narrows that, but two
requests are still two transactions. This module calls
public.replace_fight_rounds / replace_fight_rounds_non_ufc (Cage-Metrics
fight_rounds_replace_rpc.sql): delete and insert inside one function call, so
Postgres either commits the whole new set or keeps the old one.

Three rules:
  * Retry only what can succeed on a second try (network, timeouts, overload).
    The function is a full replacement, so a retry after an unseen commit
    writes the same rows again — it cannot double them.
  * The function returns how many rows it stored; anything other than the
    number sent is a failure, not a warning.
  * No fallback. If the function is missing, the write fails and the run says
    so. Falling back to delete-then-insert is how fight 7349 lost its rounds.
"""
from __future__ import annotations

import time

RPC_FOR_TABLE = {
    "fight_rounds": "replace_fight_rounds",
    "fight_rounds_non_ufc": "replace_fight_rounds_non_ufc",
}

ATTEMPTS = 4
BACKOFF_SECONDS = (2, 4, 8)

# Postgres SQLSTATE classes worth a second try: connection exceptions (08),
# serialization failure / deadlock (40), insufficient resources (53),
# operator intervention incl. statement timeout and shutdown (57).
_TRANSIENT_SQLSTATE_PREFIXES = ("08", "40", "53", "57")
# PostgREST's own "could not reach / connection pool" errors.
_TRANSIENT_PGRST = {"PGRST000", "PGRST001", "PGRST002", "PGRST003"}


class RoundsWriteError(Exception):
    """The rounds for a fight were not stored as sent."""


def _code(exc):
    code = getattr(exc, "code", None)
    if code is None and exc.args and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    return str(code) if code is not None else None


def is_transient(exc) -> bool:
    """True when the same call could succeed later. An error carrying a
    database or PostgREST code is permanent unless the code says otherwise; an
    error with no code is the transport failing (reset, timeout, DNS)."""
    code = _code(exc)
    if code is None:
        return True
    if code in _TRANSIENT_PGRST:
        return True
    if len(code) == 3 and code.isdigit():
        return code.startswith("5")  # an HTTP status surfaced as the code
    return len(code) == 5 and code.startswith(_TRANSIENT_SQLSTATE_PREFIXES)


def _stored_count(data):
    """postgrest returns a scalar function result as the bare value; older
    clients wrap it. Anything unrecognised is None, which fails the check."""
    if isinstance(data, bool):
        return None
    if isinstance(data, int):
        return data
    if isinstance(data, list) and len(data) == 1:
        return _stored_count(next(iter(data[0].values()), None) if isinstance(data[0], dict) else data[0])
    if isinstance(data, dict) and len(data) == 1:
        return _stored_count(next(iter(data.values())))
    return None


def replace_rounds(client, rounds_table, fight_id, rounds, *, sleep=time.sleep, log=print):
    """Replace every round of one fight. Returns the number stored, which is
    always len(rounds). Raises RoundsWriteError otherwise; the fight's
    previous rounds are then exactly as they were."""
    fn = RPC_FOR_TABLE.get(rounds_table)
    if fn is None:
        raise RoundsWriteError(f"no replacement function for table {rounds_table!r}")
    if not fight_id or not rounds:
        raise RoundsWriteError(f"refusing to write an empty round set for fight {fight_id}")
    rows = [{k: v for k, v in r.items() if k not in ("id", "fight_id")} for r in rounds]
    params = {"p_fight_id": fight_id, "p_rounds": rows}

    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            res = client.rpc(fn, params).execute()
        except Exception as e:  # noqa: BLE001 — classified below
            last = e
            if not is_transient(e) or attempt == ATTEMPTS:
                break
            wait = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            log(f"  ~ {fn}({fight_id}) attempt {attempt} failed, retrying in {wait}s: {e}")
            sleep(wait)
            continue
        stored = _stored_count(getattr(res, "data", None))
        if stored != len(rows):
            raise RoundsWriteError(
                f"{fn}({fight_id}) stored {stored!r} rows, sent {len(rows)}")
        return stored
    raise RoundsWriteError(f"{fn}({fight_id}) failed after {attempt} attempt(s): {last}") from last
