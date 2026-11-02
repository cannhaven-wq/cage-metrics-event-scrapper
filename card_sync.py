"""
card_sync.py — retire bouts that have dropped off a card.

The scrapers upsert every bout listed on a UFCStats event page, keyed on
ufc_fight_id. That adds new bouts and refreshes existing ones, but a bout that
is CANCELLED simply stops being listed — nothing ever touched its row again, so
it stayed on the site as a live fight. (Sean Brady vs. Gabriel Bonfim, UFC
Fight Night: Cortes Acosta vs. Walker, 2026-11-07, was still on Card Lab after
the bout was pulled.) An opponent change does the same thing: UFCStats issues a
new fight id and the old pairing lingers.

After an event page is read, this marks `is_active = false` on every row for
that event that is no longer listed. `is_active = false` is the existing
dead-booking flag (D-024 / T-093) that Card Lab, Market Lab, Fight Lab and the
fighter records already honour. NOTHING IS DELETED: every foreign key to
`fights` cascades on delete, and odds, snapshots and locks hang off these rows.

Guards, because a wrong retire hides a real fight:
  * an event page that listed no bouts is treated as a failed read, never as
    "everything was cancelled";
  * a row with a result (winner or method) is never touched — a fight that
    happened is not a dropped booking;
  * if more than half the card would go, the page is assumed to be broken and
    nothing is retired;
  * a bout that comes back onto the page is re-activated by the scraper's
    normal upsert (it writes is_active = true for every listed bout).

Pure selection logic lives in `dropped_bookings`, which has no network and no
database, so it is unit-tested directly.
"""

from __future__ import annotations

import re

_ID_RE = re.compile(r"-details/([a-f0-9]+)")  # same rule as the scrapers' extract_id_from_url


def ufc_id_from_url(url):
    if not url:
        return None
    m = _ID_RE.search(url)
    return m.group(1) if m else None


def dropped_bookings(rows, listed_ufc_ids):
    """Return (to_retire, refused_reason).

    rows            -- the event's fight rows: dicts with id, ufc_fight_id,
                       winner_id, method, is_active
    listed_ufc_ids  -- ufc_fight_ids the event page lists right now
    """
    listed = {i for i in (listed_ufc_ids or []) if i}
    if not listed:
        return [], "event page listed no bouts (treated as a failed read)"

    active = [r for r in rows if r.get("is_active") is not False]
    stale = [
        r for r in active
        if r.get("ufc_fight_id") not in listed
        and r.get("winner_id") is None
        and not r.get("method")
    ]
    if stale and len(stale) * 2 > len(active):
        return [], (f"{len(stale)} of {len(active)} active bouts missing from the page — "
                    "looks like a broken read, retiring nothing")
    return stale, None


def retire_dropped_bouts(supabase, fights_table, event_id, fight_urls, log=print):
    """Mark bouts no longer on the event page as is_active = false.
    Returns the number retired. Never raises: a failure here must not stop
    the rest of the scrape."""
    if not event_id:
        return 0
    try:
        res = (supabase.table(fights_table)
               .select("id, ufc_fight_id, winner_id, method, is_active, fighter_a_name, fighter_b_name")
               .eq("event_id", event_id)
               .execute())
        rows = res.data or []
        listed = [ufc_id_from_url(u) for u in (fight_urls or [])]
        stale, refused = dropped_bookings(rows, listed)
        if refused:
            log(f"    ! Not retiring dropped bouts: {refused}")
            return 0
        for r in stale:
            supabase.table(fights_table).update({"is_active": False}).eq("id", r["id"]).execute()
            log(f"    - Retired dropped bout {r['id']}: "
                f"{r.get('fighter_a_name')} vs {r.get('fighter_b_name')} (no longer on the card)")
        return len(stale)
    except Exception as e:
        log(f"    ! Retire-dropped-bouts error for event {event_id}: {e}")
        return 0
