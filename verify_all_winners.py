"""
verify_all_winners.py
=====================
Re-verifies the winner_id of EVERY completed UFC fight in the database against
the W/L badge on UFCStats. Source of truth is the page itself — specifically the
`i.b-fight-details__person-status` element which contains "W", "L", or "NC".

Why this exists:
  Two prior backfill paths (a SQL scorecard parser, then `backfill_winners.py`)
  populated winner_id for fights using different conventions, leading to
  inconsistencies. In particular, scorecards in `method_detail` are stored in
  (loser_score - winner_score) order regardless of which corner the fighter was
  in, making them unreliable for determining direction. The W/L badge is the
  only authoritative signal.

What this script does:
  1. Load every fighter into a robust lookup (URL + name fallback, http/https
     variants) — same hardened lookup as backfill_winners.py
  2. Pull every fight on a completed event
  3. For each fight: fetch the page, read W/L status, look up the winner's id
  4. Compare against the existing winner_id. If different, UPDATE.
  5. Track and report every change made + every cache miss + every error

Idempotent: running this script repeatedly costs the page fetches but produces
no DB churn after the first clean run.

Run locally:
  pip install requests beautifulsoup4 supabase
  export SUPABASE_URL="..."
  export SUPABASE_SECRET_KEY="..."
  python verify_all_winners.py

Or deploy to Railway. Same env vars as the other scripts in this repo.

Expected runtime: ~3.3 hours for ~8,661 completed fights at 1.5s rate limit.

Output progress every 25 fights:
  [N/8661] confirmed=X changed=Y nc=Z cache_miss=A failed=B
"""

import os
import re
import time
import sys
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
RATE_LIMIT_SECONDS = 1.5
HEADERS = {"User-Agent": "CageMetrics/1.0 (Personal UFC stats project)"}

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    print("ERROR: Missing SUPABASE_URL or SUPABASE_SECRET_KEY env vars")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


# ---------------------------------------------------------------------------
# Fighter lookup — URL primary, name fallback, http/https variants
# ---------------------------------------------------------------------------

def build_fighter_lookups():
    """Loads every fighter and builds two lookup dicts."""
    by_url = {}
    by_name = {}
    from_idx = 0
    
    print("Loading fighters from database...")
    while True:
        res = (supabase.table("fighters")
               .select("id, name, ufc_url")
               .range(from_idx, from_idx + 999)
               .execute())
        if not res.data:
            break
        for row in res.data:
            if row.get("ufc_url"):
                url = row["ufc_url"]
                by_url[url] = row["id"]
                by_url[url.replace("http://", "https://")] = row["id"]
                by_url[url.replace("https://", "http://")] = row["id"]
            if row.get("name"):
                key = row["name"].strip().lower()
                if key not in by_name:
                    by_name[key] = row["id"]
        if len(res.data) < 1000:
            break
        from_idx += 1000
    
    print(f"  Loaded ~{len(by_url)//3} unique fighters by URL "
          f"({len(by_name)} unique names)")
    return by_url, by_name


def find_fighter_id(url, name, by_url, by_name):
    """URL match first, name match fallback."""
    if url and url in by_url:
        return by_url[url]
    if name:
        key = name.strip().lower()
        if key in by_name:
            return by_name[key]
    return None


# ---------------------------------------------------------------------------
# Fight page parser — pulls the W/L badge for both fighters
# ---------------------------------------------------------------------------

def get_winner_from_page(url, by_url, by_name):
    """
    Fetch fight page, read W/L badges, resolve to fighter id.
    Returns:
      (winner_id, status_a, status_b, name_a, name_b, both_resolved)
    
    where:
      winner_id is None for NC/draw OR if we can't resolve the winner to a row
      status_a, status_b are 'W' / 'L' / 'NC' / 'D' / None
      both_resolved is True only if both fighters were found in our lookup
    """
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        return None, None, None, None, None, False, f"fetch_error: {e}"
    
    soup = BeautifulSoup(r.text, "html.parser")
    persons = soup.select("div.b-fight-details__person")
    if len(persons) < 2:
        return None, None, None, None, None, False, "no_persons"
    
    parsed = []
    for p in persons:
        a = p.select_one("a.b-fight-details__person-link")
        name = a.get_text(strip=True) if a else None
        f_url = a.get("href") if a else None
        status_el = p.select_one("i.b-fight-details__person-status")
        status = status_el.get_text(strip=True) if status_el else None
        f_id = find_fighter_id(f_url, name, by_url, by_name)
        parsed.append({"name": name, "url": f_url, "status": status, "id": f_id})
    
    a, b = parsed[0], parsed[1]
    both_resolved = (a["id"] is not None and b["id"] is not None)
    
    # Determine winner from W/L badge
    winner_id = None
    if a["status"] == "W":
        winner_id = a["id"]
    elif b["status"] == "W":
        winner_id = b["id"]
    # else: NC, D, or upcoming — winner_id stays None
    
    return (winner_id, a["status"], b["status"], a["name"], b["name"], both_resolved, None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 64)
    print("CAGE METRICS — Universal Winner Verification")
    print("=" * 64)
    print()
    
    by_url, by_name = build_fighter_lookups()
    
    # Fetch every fight on a completed event
    print("\nFetching all fights on completed events...")
    
    # Get the completed event ids first (Supabase doesn't easily join across these)
    completed_event_ids = []
    from_idx = 0
    while True:
        res = (supabase.table("events")
               .select("id")
               .eq("is_upcoming", False)
               .range(from_idx, from_idx + 999)
               .execute())
        if not res.data:
            break
        completed_event_ids.extend([r["id"] for r in res.data])
        if len(res.data) < 1000:
            break
        from_idx += 1000
    print(f"  Completed events: {len(completed_event_ids)}")
    
    # Now pull all fights on those events
    all_fights = []
    from_idx = 0
    while True:
        res = (supabase.table("fights")
               .select("id, ufc_url, winner_id, fighter_a_id, fighter_b_id, "
                       "fighter_a_name, fighter_b_name, method, event_id")
               .in_("event_id", completed_event_ids)
               .range(from_idx, from_idx + 999)
               .execute())
        if not res.data:
            break
        all_fights.extend(res.data)
        if len(res.data) < 1000:
            break
        from_idx += 1000
    
    print(f"  Total fights to verify: {len(all_fights)}")
    minutes = (len(all_fights) * RATE_LIMIT_SECONDS) / 60
    print(f"  Estimated runtime: {minutes:.0f} minutes ({minutes/60:.1f} hours)")
    print()
    
    confirmed = 0      # Winner already correct
    changed = 0        # Winner was wrong, fixed it
    nc_correct = 0     # No contest, winner_id was already null and should stay null
    nc_set_to_null = 0 # winner_id was set, should be null (NC or unresolvable)
    cache_miss = 0     # Page parsed correctly, but fighter not in our lookup
    failed = 0         # Page fetch / parse failed entirely
    
    changes_log = []   # Track first 50 changes for visibility
    
    for i, fight in enumerate(all_fights, 1):
        if i % 25 == 0:
            print(f"  [{i}/{len(all_fights)}] "
                  f"confirmed={confirmed} changed={changed} "
                  f"nc_set_null={nc_set_to_null} cache_miss={cache_miss} failed={failed}")
        
        result = get_winner_from_page(fight["ufc_url"], by_url, by_name)
        winner_id, status_a, status_b, name_a, name_b, both_resolved, err = result
        
        if err:
            failed += 1
            if failed <= 5:
                print(f"  ! {err}: {fight['ufc_url']}")
            continue
        
        current_winner = fight.get("winner_id")
        
        # Case 1: page indicates a clear W/L
        if (status_a == "W" or status_b == "W"):
            if winner_id is None:
                # Page has a winner but we couldn't resolve it to a row in fighters
                cache_miss += 1
                if cache_miss <= 10:
                    side = "a" if status_a == "W" else "b"
                    print(f"  ! cache miss: {name_a} vs {name_b} (W={side})")
                continue
            
            if current_winner == winner_id:
                confirmed += 1
            else:
                # Update needed
                try:
                    supabase.table("fights").update(
                        {"winner_id": winner_id}
                    ).eq("id", fight["id"]).execute()
                    changed += 1
                    if len(changes_log) < 50:
                        old_winner = "null" if current_winner is None else (
                            name_a if current_winner == fight.get("fighter_a_id") else name_b
                        )
                        new_winner = name_a if winner_id == fight.get("fighter_a_id") else name_b
                        changes_log.append(
                            f"{name_a} vs {name_b} ({fight.get('method')}): "
                            f"{old_winner} -> {new_winner}"
                        )
                except Exception as e:
                    print(f"  ! update error for fight {fight['id']}: {e}")
                    failed += 1
        
        # Case 2: page indicates NC / Draw / no clear W
        else:
            if current_winner is None:
                nc_correct += 1
            else:
                # winner_id was set but page says no winner — clear it
                try:
                    supabase.table("fights").update(
                        {"winner_id": None}
                    ).eq("id", fight["id"]).execute()
                    nc_set_to_null += 1
                except Exception as e:
                    print(f"  ! update-to-null error for fight {fight['id']}: {e}")
                    failed += 1
    
    print()
    print("=" * 64)
    print("DONE")
    print("=" * 64)
    print(f"  Total processed:        {len(all_fights)}")
    print(f"  Already correct:        {confirmed}")
    print(f"  Changed (winner_id):    {changed}")
    print(f"  NC was already null:    {nc_correct}")
    print(f"  Set to null (was NC):   {nc_set_to_null}")
    print(f"  Cache misses (skipped): {cache_miss}")
    print(f"  Failures:               {failed}")
    print()
    
    if changes_log:
        print("First 50 changes (sample):")
        for c in changes_log:
            print(f"  {c}")
        print()
    
    print("Next: re-run 03b_recompute_ufc_record.sql to refresh fighters.ufc_*")
    print("counts from the now-corrected fights.winner_id values.")


if __name__ == "__main__":
    main()
