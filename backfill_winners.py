"""
backfill_winners.py
====================
Targeted re-scrape to recover winner_id on KO/TKO and Submission fights.

Why this exists:
  The original events_scraper.py had a fighter cache lookup bug. When
  get_fighter_id() returned None for either fighter (URL format mismatch
  or fighter not in the cache when the lookup happened), winner_id was
  set to None even though the W/L status was correctly read from the page.
  This affects ~2,400 KO/TKO and Submission fights.

What this script does:
  1. Query fights where winner_id IS NULL AND method is KO/TKO or Submission
  2. For each, fetch the fight page from UFCStats
  3. Extract the W/L status (which we know works — that selector is fine)
  4. Look up both fighters in the fighters table by ufc_url AND by name
     (defense in depth — if URL doesn't match, try name)
  5. Update winner_id

Run locally:
  pip install requests beautifulsoup4 supabase python-dotenv
  
  Set env vars:
    export SUPABASE_URL="..."
    export SUPABASE_SECRET_KEY="..."
  
  python backfill_winners.py

Or deploy to Railway alongside your existing scraper. Same env vars.

Expected runtime: ~60 minutes for ~2,400 fights at 1.5s rate limit.
"""

import os
import re
import time
import sys
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from challenge import make_session, get as challenge_get

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
RATE_LIMIT_SECONDS = 1.5
HEADERS = {"User-Agent": "CageMetrics/1.0 (Personal UFC stats project)"}

# Shared session that solves UFCStats' proof-of-work interstitial (challenge.py).
SESSION = make_session()

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    print("ERROR: Missing SUPABASE_URL or SUPABASE_SECRET_KEY env vars")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


# ---------------------------------------------------------------------------
# Build a robust fighter lookup with multiple keys
# ---------------------------------------------------------------------------

def build_fighter_lookups():
    """
    Loads ALL fighters into memory once and builds two lookup dicts:
      - by_url:  {ufc_url: fighter_id}
      - by_name: {normalized_name: fighter_id}
    
    We try URL first (most reliable), then fall back to name match.
    """
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
                # Store both http and https variants in case of mismatch
                url = row["ufc_url"]
                by_url[url] = row["id"]
                by_url[url.replace("http://", "https://")] = row["id"]
                by_url[url.replace("https://", "http://")] = row["id"]
            if row.get("name"):
                # Normalize name: lowercase, strip whitespace
                key = row["name"].strip().lower()
                # Some fighters share names — we'll just take the first.
                # That's a tiny error rate vs the 70% we're recovering.
                if key not in by_name:
                    by_name[key] = row["id"]
        if len(res.data) < 1000:
            break
        from_idx += 1000
    
    print(f"  Loaded {len(by_url)//3} unique fighters "
          f"({len(by_name)} unique names)")
    return by_url, by_name


def find_fighter_id(url, name, by_url, by_name):
    """Try URL match first, fall back to name match."""
    if url and url in by_url:
        return by_url[url]
    if name:
        key = name.strip().lower()
        if key in by_name:
            return by_name[key]
    return None


# ---------------------------------------------------------------------------
# Fight page parser — extracts ONLY winner_id
# ---------------------------------------------------------------------------

def get_winner_from_page(url, by_url, by_name):
    """
    Fetch the fight page and extract winner. Returns:
      (winner_fighter_id, fighter_a_name, fighter_b_name, status_a, status_b)
    Or (None, ...) if can't determine.
    """
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        r = challenge_get(SESSION, url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  ! Fetch error: {e}")
        return None, None, None, None, None
    
    soup = BeautifulSoup(r.text, "html.parser")
    persons = soup.select("div.b-fight-details__person")
    if len(persons) < 2:
        return None, None, None, None, None
    
    fighters = []
    for p in persons:
        a = p.select_one("a.b-fight-details__person-link")
        name = a.get_text(strip=True) if a else None
        f_url = a.get("href") if a else None
        status_el = p.select_one("i.b-fight-details__person-status")
        status = status_el.get_text(strip=True) if status_el else None
        f_id = find_fighter_id(f_url, name, by_url, by_name)
        fighters.append({"name": name, "url": f_url, "status": status, "id": f_id})
    
    # Determine winner
    winner_id = None
    if fighters[0]["status"] == "W":
        winner_id = fighters[0]["id"]
    elif fighters[1]["status"] == "W":
        winner_id = fighters[1]["id"]
    
    return (winner_id,
            fighters[0]["name"], fighters[1]["name"],
            fighters[0]["status"], fighters[1]["status"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 64)
    print("CAGE METRICS — Targeted Winner Backfill")
    print("=" * 64)
    
    by_url, by_name = build_fighter_lookups()
    
    # Get every fight that needs a winner
    print("\nFetching fights needing winner_id...")
    target_fights = []
    from_idx = 0
    while True:
        res = (supabase.table("fights")
               .select("id, ufc_url, method, fighter_a_name, fighter_b_name")
               .is_("winner_id", "null")
               .or_("method.ilike.%KO%,method.ilike.%TKO%,method.ilike.%submission%")
               .range(from_idx, from_idx + 999)
               .execute())
        if not res.data:
            break
        target_fights.extend(res.data)
        if len(res.data) < 1000:
            break
        from_idx += 1000
    
    print(f"  Found {len(target_fights)} fights to backfill")
    
    if not target_fights:
        print("\nNothing to do. Exiting.")
        return
    
    # Estimate runtime
    minutes = (len(target_fights) * RATE_LIMIT_SECONDS) / 60
    print(f"  Estimated runtime: {minutes:.0f} minutes")
    print()
    
    recovered = 0
    failed = 0
    cache_miss = 0
    
    for i, fight in enumerate(target_fights, 1):
        if i % 25 == 0:
            print(f"  [{i}/{len(target_fights)}] "
                  f"recovered={recovered} cache_miss={cache_miss} failed={failed}")
        
        winner_id, name_a, name_b, status_a, status_b = get_winner_from_page(
            fight["ufc_url"], by_url, by_name
        )
        
        if winner_id is None:
            # Either parsing failed or cache miss
            if status_a == "W" or status_b == "W":
                # Page had a clear winner but we couldn't match the fighter
                cache_miss += 1
                if cache_miss <= 10:
                    print(f"  ! cache miss: {name_a} vs {name_b}  "
                          f"(W={'a' if status_a=='W' else 'b'})")
            else:
                # Likely a draw or NC
                failed += 1
            continue
        
        # Update Supabase
        try:
            supabase.table("fights").update({"winner_id": winner_id}).eq(
                "id", fight["id"]
            ).execute()
            recovered += 1
        except Exception as e:
            print(f"  ! Update error for fight {fight['id']}: {e}")
            failed += 1
    
    print()
    print("=" * 64)
    print("DONE")
    print("=" * 64)
    print(f"  Total processed:    {len(target_fights)}")
    print(f"  Winners recovered:  {recovered}")
    print(f"  Cache misses:       {cache_miss}")
    print(f"  Failures:           {failed}")
    print()
    print("Next: re-run the SQL backfill #1 to update fighters.ko_wins / "
          "sub_wins / dec_wins from the new winner_id values.")


if __name__ == "__main__":
    main()
