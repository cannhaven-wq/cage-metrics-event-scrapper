"""
CageMetrics Events & Fights Scraper
Pulls every UFC event, every fight on every event, and per-round stats.
Pushes to Supabase events, fights, fight_rounds tables.

Run order:
1. Scrape all event list pages (completed + upcoming)
2. For each event, scrape the fight list
3. For each fight, scrape the fight detail page (per-round stats)

Rate-limited 1.5s between requests. ~3-4 hours for first full run.
Subsequent runs only update upcoming events + add new completed.
"""

import os
import re
import time
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
RATE_LIMIT_SECONDS = 1.5
HEADERS = {"User-Agent": "CageMetrics/1.0 (Personal UFC stats project)"}

# Scrape modes
INCREMENTAL = os.environ.get("INCREMENTAL", "false").lower() == "true"  # if true, only upcoming + recent

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SECRET_KEY environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

# ---------- Helpers ----------
def get_soup(url):
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ! Error fetching {url}: {e}")
        return None

def extract_id_from_url(url):
    """Extract the trailing UUID-like id from a ufcstats URL."""
    if not url:
        return None
    m = re.search(r"/details/([a-f0-9]+)", url)
    return m.group(1) if m else None

def parse_int(s):
    if s is None:
        return None
    s = s.strip()
    if not s or s == "--":
        return None
    try:
        return int(s)
    except ValueError:
        return None

def parse_x_of_y(s):
    """Parse '12 of 25' -> (12, 25)."""
    if not s:
        return (None, None)
    m = re.match(r"\s*(\d+)\s*of\s*(\d+)", s.strip())
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (None, None)

def parse_ctrl_time(s):
    """Parse '2:34' -> 154 seconds."""
    if not s or s.strip() == "--":
        return None
    m = re.match(r"(\d+):(\d+)", s.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None

def parse_event_date(s):
    """Parse 'May 03, 2025' -> '2025-05-03'."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%B %d, %Y").date().isoformat()
    except Exception:
        return None

# ---------- Fighter lookup cache ----------
_fighter_cache = None

def get_fighter_id(ufc_url):
    """Look up fighter id in our DB by their ufc_url."""
    global _fighter_cache
    if _fighter_cache is None:
        _fighter_cache = {}
        # Pull all fighters in pages
        from_idx = 0
        while True:
            res = supabase.table("fighters").select("id, ufc_url").range(from_idx, from_idx + 999).execute()
            if not res.data:
                break
            for row in res.data:
                if row.get("ufc_url"):
                    _fighter_cache[row["ufc_url"]] = row["id"]
            if len(res.data) < 1000:
                break
            from_idx += 1000
        print(f"  Loaded {len(_fighter_cache)} fighters into cache.")
    return _fighter_cache.get(ufc_url)

# ---------- Event list ----------
def get_event_urls(upcoming=False):
    """Fetch all event detail URLs from the events list pages."""
    if upcoming:
        url = "http://www.ufcstats.com/statistics/events/upcoming"
    else:
        url = "http://www.ufcstats.com/statistics/events/completed?page=all"
    print(f"  Fetching event list: {url}")
    soup = get_soup(url)
    if not soup:
        return []
    urls = []
    for row in soup.select("tr.b-statistics__table-row"):
        a = row.find("a", class_="b-link")
        if a and a.get("href") and "event-details" in a["href"]:
            urls.append(a["href"])
    return urls

# ---------- Event detail ----------
def parse_event(url, is_upcoming=False):
    soup = get_soup(url)
    if not soup:
        return None, []

    name_el = soup.select_one("span.b-content__title-highlight")
    name = name_el.get_text(strip=True) if name_el else None

    # Date + location from info list
    info = {}
    for li in soup.select("li.b-list__box-list-item"):
        text = li.get_text(separator="|", strip=True)
        # format like "Date:|May 03, 2025"
        parts = text.split("|", 1)
        if len(parts) == 2:
            key = parts[0].rstrip(":").lower().strip()
            value = parts[1].strip()
            info[key] = value

    event = {
        "ufc_event_id": extract_id_from_url(url),
        "name": name,
        "event_date": parse_event_date(info.get("date")),
        "location": info.get("location"),
        "is_upcoming": is_upcoming,
        "ufc_url": url,
    }

    # Fights on this event
    fight_rows = soup.select("tr.b-fight-details__table-row[data-link]")
    fight_urls = []
    for row in fight_rows:
        link = row.get("data-link")
        if link:
            fight_urls.append(link)

    return event, fight_urls

# ---------- Fight detail ----------
def parse_fight(url):
    """Parse a single fight detail page."""
    soup = get_soup(url)
    if not soup:
        return None, []

    fight = {
        "ufc_fight_id": extract_id_from_url(url),
        "ufc_url": url,
    }

    # Persons section: 2 fighters, win/loss markers
    persons = soup.select("div.b-fight-details__person")
    if len(persons) < 2:
        return None, []

    fighter_data = []
    for p in persons:
        a = p.select_one("a.b-fight-details__person-link")
        name = a.get_text(strip=True) if a else None
        fighter_url = a.get("href") if a else None
        status_el = p.select_one("i.b-fight-details__person-status")
        status = status_el.get_text(strip=True) if status_el else None
        fighter_data.append({"name": name, "url": fighter_url, "status": status})

    fight["fighter_a_name"] = fighter_data[0]["name"]
    fight["fighter_b_name"] = fighter_data[1]["name"]
    fight["fighter_a_id"] = get_fighter_id(fighter_data[0]["url"]) if fighter_data[0]["url"] else None
    fight["fighter_b_id"] = get_fighter_id(fighter_data[1]["url"]) if fighter_data[1]["url"] else None

    # Determine winner from status W/L
    if fighter_data[0]["status"] == "W":
        fight["winner_id"] = fight["fighter_a_id"]
    elif fighter_data[1]["status"] == "W":
        fight["winner_id"] = fight["fighter_b_id"]
    else:
        fight["winner_id"] = None  # Draw, NC, or upcoming

    # Fight info: weight class, title fight, main event, method
    fight_head = soup.select_one("div.b-fight-details__fight")
    if fight_head:
        # Weight class / title fight
        title_el = fight_head.select_one("i.b-fight-details__fight-title")
        if title_el:
            title_text = title_el.get_text(separator=" ", strip=True)
            fight["is_title_fight"] = "Title" in title_text or "Championship" in title_text
            # Strip trailing "Title Bout" / "Title Bout" markers
            wc = re.sub(r"(UFC )?(Interim )?(Women's )?(.*?)(Title Bout|Bout)?$", r"\4", title_text).strip()
            fight["weight_class"] = wc or title_text

        # Method, round, time, format
        for item in fight_head.select("i.b-fight-details__text-item, i.b-fight-details__text-item_first"):
            label_el = item.select_one("i.b-fight-details__label")
            if not label_el:
                continue
            label = label_el.get_text(strip=True).rstrip(":").lower()
            label_el.extract()
            value = item.get_text(strip=True)
            if label == "method":
                fight["method"] = value
            elif label == "round":
                fight["end_round"] = parse_int(value)
            elif label == "time":
                fight["end_time"] = value
            elif label == "time format":
                # "3 Rnd (5-5-5)" -> 3
                m = re.match(r"(\d+)", value)
                if m:
                    fight["scheduled_rounds"] = int(m.group(1))

        # Method detail (the small text under method)
        detail_el = fight_head.select_one("p.b-fight-details__text:nth-of-type(2)")
        if detail_el:
            fight["method_detail"] = detail_el.get_text(strip=True)

    # Per-round stats tables
    rounds_data = parse_round_stats(soup, fight["fighter_a_id"], fight["fighter_b_id"])

    # Sum totals from rounds for the fight aggregate
    if rounds_data:
        agg = {}
        for r in rounds_data:
            for k, v in r.items():
                if k.startswith(("a_", "b_")) and isinstance(v, (int, float)) and v is not None:
                    agg[k] = agg.get(k, 0) + v
        for k, v in agg.items():
            fight[k] = v

    return fight, rounds_data

def parse_round_stats(soup, fighter_a_id, fighter_b_id):
    """Parse the per-round stats tables on a fight page."""
    rounds = {}  # round_number -> dict

    # Table 1: Totals (per round) — kd, sig str, total str, td, sub att, rev, ctrl
    totals_section = soup.select_one("section.b-fight-details__section_collapse:nth-of-type(1)")
    if not totals_section:
        # Try alternative selectors
        sections = soup.select("section.b-fight-details__section")
    # Use a broader approach: find tables with class b-fight-details__table
    tables = soup.select("table.b-fight-details__table")

    # The page structure on ufcstats has: Totals (overall), Totals per round, Sig Strikes (overall), Sig Strikes per round
    # Per-round tables are inside <section class="b-fight-details__section_collapse">
    per_round_sections = soup.select("section.b-fight-details__section_collapse")

    for section in per_round_sections:
        # Determine which type of stats this is (totals or sig strikes)
        # Check sibling header
        section_type = None
        prev = section.find_previous("h3", class_="b-fight-details__collapse-link_tot") or \
               section.find_previous("a", class_="b-fight-details__collapse-link_tot")
        # Heuristic: look at column headers
        thead = section.select_one("thead.b-fight-details__table-head_rnd")
        if thead:
            headers_text = " ".join(th.get_text(strip=True).lower() for th in thead.select("th"))
            if "kd" in headers_text and "td" in headers_text:
                section_type = "totals"
            elif "head" in headers_text and "body" in headers_text and "leg" in headers_text:
                section_type = "sig_strikes"

        if not section_type:
            continue

        # Each round is its own thead.b-fight-details__table-row_type_head + tbody section
        # Actually the structure is: one table, multiple <tbody> alternating header rows and data rows
        # Let's iterate all rows
        round_num = 0
        for tr in section.select("tr.b-fight-details__table-row"):
            classes = tr.get("class", [])
            if "b-fight-details__table-row_type_head" in classes or tr.select_one("th.b-fight-details__table-col_style_grey-bg"):
                # This is a "Round N" header row
                txt = tr.get_text(strip=True)
                m = re.search(r"Round\s*(\d+)", txt)
                if m:
                    round_num = int(m.group(1))
                continue
            if round_num == 0:
                continue
            # Data row: 2 fighters' stats across columns
            cols = tr.select("td.b-fight-details__table-col")
            if not cols:
                continue
            # Each col has 2 <p> tags (one per fighter)
            def col_pair(col):
                ps = col.select("p")
                if len(ps) >= 2:
                    return ps[0].get_text(strip=True), ps[1].get_text(strip=True)
                return (None, None)

            r = rounds.get(round_num, {
                "round_number": round_num,
                "fighter_a_id": fighter_a_id,
                "fighter_b_id": fighter_b_id,
            })

            if section_type == "totals":
                # Cols: Fighter, KD, Sig.str., Sig.str.%, Total str., Td, Td %, Sub.att., Rev., Ctrl
                if len(cols) >= 10:
                    a, b = col_pair(cols[1]); r["a_kd"], r["b_kd"] = parse_int(a), parse_int(b)
                    a, b = col_pair(cols[2])
                    al, at = parse_x_of_y(a); bl, bt = parse_x_of_y(b)
                    r["a_sig_str_landed"], r["a_sig_str_attempted"] = al, at
                    r["b_sig_str_landed"], r["b_sig_str_attempted"] = bl, bt
                    a, b = col_pair(cols[4])
                    al, at = parse_x_of_y(a); bl, bt = parse_x_of_y(b)
                    r["a_total_str_landed"], r["a_total_str_attempted"] = al, at
                    r["b_total_str_landed"], r["b_total_str_attempted"] = bl, bt
                    a, b = col_pair(cols[5])
                    al, at = parse_x_of_y(a); bl, bt = parse_x_of_y(b)
                    r["a_td_landed"], r["a_td_attempted"] = al, at
                    r["b_td_landed"], r["b_td_attempted"] = bl, bt
                    a, b = col_pair(cols[7]); r["a_sub_attempts"], r["b_sub_attempts"] = parse_int(a), parse_int(b)
                    a, b = col_pair(cols[8]); r["a_rev"], r["b_rev"] = parse_int(a), parse_int(b)
                    a, b = col_pair(cols[9]); r["a_ctrl_seconds"], r["b_ctrl_seconds"] = parse_ctrl_time(a), parse_ctrl_time(b)

            elif section_type == "sig_strikes":
                # Cols: Fighter, Sig.str, Sig.str.%, Head, Body, Leg, Distance, Clinch, Ground
                if len(cols) >= 9:
                    a, b = col_pair(cols[3]); r["a_head_landed"], _ = parse_x_of_y(a); r["b_head_landed"], _ = parse_x_of_y(b)
                    a, b = col_pair(cols[4]); r["a_body_landed"], _ = parse_x_of_y(a); r["b_body_landed"], _ = parse_x_of_y(b)
                    a, b = col_pair(cols[5]); r["a_leg_landed"], _ = parse_x_of_y(a); r["b_leg_landed"], _ = parse_x_of_y(b)
                    a, b = col_pair(cols[6]); r["a_distance_landed"], _ = parse_x_of_y(a); r["b_distance_landed"], _ = parse_x_of_y(b)
                    a, b = col_pair(cols[7]); r["a_clinch_landed"], _ = parse_x_of_y(a); r["b_clinch_landed"], _ = parse_x_of_y(b)
                    a, b = col_pair(cols[8]); r["a_ground_landed"], _ = parse_x_of_y(a); r["b_ground_landed"], _ = parse_x_of_y(b)

            rounds[round_num] = r

    return list(rounds.values())

# ---------- Upserts ----------
def upsert_event(event):
    try:
        res = supabase.table("events").upsert(event, on_conflict="ufc_event_id").execute()
        if res.data:
            return res.data[0]["id"]
        # Look up if upsert didn't return id
        existing = supabase.table("events").select("id").eq("ufc_event_id", event["ufc_event_id"]).execute()
        if existing.data:
            return existing.data[0]["id"]
        return None
    except Exception as e:
        print(f"  ! Event upsert error: {e}")
        return None

def upsert_fight(fight):
    try:
        res = supabase.table("fights").upsert(fight, on_conflict="ufc_fight_id").execute()
        if res.data:
            return res.data[0]["id"]
        existing = supabase.table("fights").select("id").eq("ufc_fight_id", fight["ufc_fight_id"]).execute()
        if existing.data:
            return existing.data[0]["id"]
        return None
    except Exception as e:
        print(f"  ! Fight upsert error for {fight.get('ufc_fight_id')}: {e}")
        return None

def upsert_rounds(fight_id, rounds):
    if not rounds or not fight_id:
        return
    # Delete existing rounds for this fight (clean slate), then insert
    try:
        supabase.table("fight_rounds").delete().eq("fight_id", fight_id).execute()
        rows = [{**r, "fight_id": fight_id} for r in rounds]
        if rows:
            supabase.table("fight_rounds").insert(rows).execute()
    except Exception as e:
        print(f"  ! Rounds upsert error for fight {fight_id}: {e}")

# ---------- Main ----------
def main():
    print("=== CageMetrics Events & Fights Scraper ===")
    print(f"Mode: {'INCREMENTAL' if INCREMENTAL else 'FULL'}")

    # Get event URLs
    print("\nStep 1: Collecting event URLs...")
    upcoming_urls = get_event_urls(upcoming=True)
    print(f"  Upcoming events: {len(upcoming_urls)}")

    if INCREMENTAL:
        # Only do upcoming + most recent 3 completed (in case results just landed)
        completed_urls = get_event_urls(upcoming=False)[:3]
    else:
        completed_urls = get_event_urls(upcoming=False)
    print(f"  Completed events to process: {len(completed_urls)}")

    all_events = [(u, True) for u in upcoming_urls] + [(u, False) for u in completed_urls]

    # For each event
    fight_count = 0
    for ev_idx, (event_url, is_upcoming) in enumerate(all_events, 1):
        print(f"\n[Event {ev_idx}/{len(all_events)}] {event_url}")
        event, fight_urls = parse_event(event_url, is_upcoming=is_upcoming)
        if not event:
            continue
        event_db_id = upsert_event(event)
        if not event_db_id:
            print(f"  ! Could not save event, skipping fights")
            continue
        print(f"  Event: {event.get('name')} ({event.get('event_date')}) - {len(fight_urls)} fights")

        for f_idx, fight_url in enumerate(fight_urls, 1):
            fight, rounds = parse_fight(fight_url)
            if not fight:
                continue
            fight["event_id"] = event_db_id
            # First fight on the card by convention is the main event (top of card on ufcstats)
            fight["is_main_event"] = (f_idx == 1)
            fight_id = upsert_fight(fight)
            if fight_id and rounds:
                upsert_rounds(fight_id, rounds)
            fight_count += 1
            if fight_count % 25 == 0:
                print(f"  ... {fight_count} fights processed total")

    print(f"\n=== Done. {fight_count} fights saved. ===")

if __name__ == "__main__":
    main()
