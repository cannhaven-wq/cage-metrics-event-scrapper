"""Is this card a UFC event? (Cannon Fight Lab T-086, 2026-10-13)

UFCStats lists some cards that are not UFC events, most visibly Dana White's
Contender Series ("DWCS 10.3"). Stored in `events` / `fights`, a Contender
Series bout counts as a UFC fight on every page and in every statistic: it
gave ten fighters an extra "UFC" fight, including one on the next card.

So the scrapers classify every card by NAME before writing it:

  * a UFC card goes to events / fights / fight_rounds, exactly as before;
  * any other card goes to events_non_ufc / fights_non_ufc /
    fight_rounds_non_ufc, with the same columns. Its history is kept, but it
    is never counted as UFC;
  * if the *_non_ufc tables do not exist yet (T-086 not applied), the card is
    SKIPPED and logged. It is never written to the UFC tables.

The rule must match the one in the Cage-Metrics repository (build/promotion.js,
the events_ufc_name_only CHECK in t086_dwcs_non_ufc.sql, and
reconcile_ufc_records.py). test_promotion.py runs the same names list.

UFC = a name containing the word "UFC" (UFC 300, UFC Fight Night, Noche UFC,
UFC Freedom 250, "UFC - Road to UFC 4.6"), "The Ultimate Fighter ..." or the
2006 card "Ortiz vs Shamrock 3", but never a name starting "Road to UFC".
"""
from __future__ import annotations

import re

_UFC_NAME = re.compile(
    r"(^|[^A-Za-z0-9_])UFC(?![A-Za-z0-9_])"
    r"|^The Ultimate Fighter(?![A-Za-z0-9_])"
    r"|^Ortiz vs\.? Shamrock 3(?![A-Za-z0-9_])"
)
_ROAD_TO_UFC = re.compile(r"^\s*road to ufc", re.IGNORECASE)
_DWCS = re.compile(r"^\s*(dwcs\b|dana white.?s contender series)", re.IGNORECASE)

UFC_TABLES = {"events": "events", "fights": "fights", "rounds": "fight_rounds"}
NON_UFC_TABLES = {"events": "events_non_ufc", "fights": "fights_non_ufc",
                  "rounds": "fight_rounds_non_ufc"}


def is_ufc_event_name(name: str | None) -> bool:
    s = name or ""
    return bool(_UFC_NAME.search(s)) and not _ROAD_TO_UFC.search(s)


def non_ufc_promotion(name: str | None) -> str:
    """The events_non_ufc.promotion label for a card that is not UFC."""
    s = name or ""
    if _DWCS.search(s):
        return "DWCS"
    if _ROAD_TO_UFC.search(s):
        return "RTU"
    return "OTHER"


def tables_for(event: dict) -> dict:
    """Where this card's event, bouts and rounds are written."""
    return UFC_TABLES if is_ufc_event_name(event.get("name")) else NON_UFC_TABLES


def is_missing_table_error(exc: Exception) -> bool:
    """PostgREST's answer when a table is not in its schema cache."""
    msg = str(exc)
    return "PGRST205" in msg or "42P01" in msg or (
        "_non_ufc" in msg and ("Could not find" in msg or "does not exist" in msg))
