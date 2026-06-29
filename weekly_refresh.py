"""
weekly_refresh.py
=================
CFL unified weekly refresh. Runs Mon/Wed/Fri/Sun on Railway cron.

Replaces the previous separate fighter scraper + events scraper. Does
EVERYTHING needed to keep the database current, but bounded so each run
takes ~15-30 minutes instead of hours.

PHASES (in order):
  1. Refresh upcoming events list — catches new cards, lineup changes
  2. Refresh recently completed events — captures results from last weekend
  3. Lazy-fetch any fighters missing from the database (debutants,
     short-notice replacements). This fixes the Marwan/Schmid pattern.
  4. Re-scrape fighters who fought in the last 14 days — their stats
     just changed (new record, new last_fight_date, new career averages).
  5. Resolve any NULL fighter_a_id / fighter_b_id rows in fights table
     using the now-fresh fighter cache.

What this DOES NOT do:
  - Re-scrape the full 4,488-fighter roster (use scraper.py for that)
  - Re-scrape events older than 30 days (those are stable; the existing
    data is correct)
  - Backfill historical winner_id (that's verify_all_winners.py)

Run locally:
  export SUPABASE_URL='...'
  export SUPABASE_SECRET_KEY='...'
  python weekly_refresh.py

Run on Railway:
  Cron schedule: 0 6 * * 0,1,3,5  (Sun/Mon/Wed/Fri at 06:00 UTC)
  Start command: python weekly_refresh.py

Tunable env vars:
  RECENT_EVENT_DAYS    — how far back to refresh events (default 14)
  RECENT_FIGHTER_DAYS  — how far back to refresh fighter stats (default 14)
"""

import os
import re
import time
import sys
import requests
from datetime import datetime, date, timedelta
from bs4 import BeautifulSoup
from supabase import create_client, Client
from challenge import make_session, get as challenge_get

# Status lines use unicode (→, ✓). On Windows the default console encoding
# (cp1252) can't encode those and crashes mid-run; force UTF-8 stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# =============================================================================
# Config
# =============================================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
RATE_LIMIT_SECONDS = 1.5
HEADERS = {"User-Agent": "CannonFightLab/1.0 (Personal UFC stats project)"}

# Shared HTTP session that transparently solves UFCStats' proof-of-work
# interstitial (see challenge.py). One session per run so the clearance
# cookie is reused across every request.
SESSION = make_session()

RECENT_EVENT_DAYS = int(os.environ.get("RECENT_EVENT_DAYS", "14"))
RECENT_FIGHTER_DAYS = int(os.environ.get("RECENT_FIGHTER_DAYS", "14"))

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SECRET_KEY environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

# Stats for the run summary
STATS = {
    "events_seen": 0,
    "events_upserted": 0,
    "fights_upserted": 0,
    "fighters_lazy_fetched": 0,
    "fighters_refreshed": 0,
    "null_fighters_resolved": 0,
    "errors": 0,
}


# =============================================================================
# Shared HTTP helper
# =============================================================================

def get_soup(url):
    """Fetch a URL with rate limiting. Returns BeautifulSoup or None.
    Routes through the challenge-solving session so the UFCStats PoW
    interstitial is handled transparently."""
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        r = challenge_get(SESSION, url, timeout=30)
        r.raise_for_status()
        from challenge import is_challenge
        if is_challenge(r.text):
            print(f"  ! Still challenged after solve attempts: {url}")
            STATS["errors"] += 1
            return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ! Error fetching {url}: {e}")
        STATS["errors"] += 1
        return None


# =============================================================================
# Parsing helpers — shared between event and fighter scrapers
# =============================================================================

def extract_id_from_url(url):
    if not url:
        return None
    m = re.search(r"-details/([a-f0-9]+)", url)
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
    if not s:
        return (None, None)
    m = re.match(r"\s*(\d+)\s*of\s*(\d+)", s.strip())
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (None, None)

def parse_ctrl_time(s):
    if not s or s.strip() == "--":
        return None
    m = re.match(r"(\d+):(\d+)", s.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None

def parse_event_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%B %d, %Y").date().isoformat()
    except Exception:
        return None

def parse_height(s):
    if not s or s == "--":
        return None
    m = re.match(r"(\d+)' (\d+)\"", s.strip())
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    return None

def parse_reach(s):
    if not s or s == "--":
        return None
    m = re.match(r"([\d.]+)", s.strip())
    return float(m.group(1)) if m else None

def parse_pct(s):
    if not s or s == "--":
        return None
    m = re.match(r"(\d+)", s.strip())
    return int(m.group(1)) if m else None

def parse_num(s):
    if not s or s == "--":
        return None
    try:
        return float(s.strip())
    except ValueError:
        return None

def parse_dob(s):
    if not s or s == "--":
        return None
    try:
        return datetime.strptime(s.strip(), "%b %d, %Y").date()
    except ValueError:
        return None

def calc_age(dob):
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# =============================================================================
# Fighter cache — same logic as in events_scraper.py and backfill_winners.py.
# Loads once at the start of the run, then we add to it as we lazy-fetch.
# =============================================================================

_fighter_by_url = None
_fighter_by_name = None

def _url_variants(url):
    if not url:
        return []
    variants = {url}
    variants.add(url.replace("http://", "https://"))
    variants.add(url.replace("https://", "http://"))
    for v in list(variants):
        if "://www." in v:
            variants.add(v.replace("://www.", "://"))
        else:
            variants.add(v.replace("://", "://www.", 1))
    return list(variants)

def _load_fighter_cache():
    global _fighter_by_url, _fighter_by_name
    _fighter_by_url = {}
    _fighter_by_name = {}
    from_idx = 0
    while True:
        res = supabase.table("fighters").select("id, name, ufc_url").range(from_idx, from_idx + 999).execute()
        if not res.data:
            break
        for row in res.data:
            if row.get("ufc_url"):
                for v in _url_variants(row["ufc_url"]):
                    _fighter_by_url[v] = row["id"]
            if row.get("name"):
                key = row["name"].strip().lower()
                if key not in _fighter_by_name:
                    _fighter_by_name[key] = row["id"]
        if len(res.data) < 1000:
            break
        from_idx += 1000
    print(f"  Loaded fighter cache: {len(_fighter_by_name)} unique names, {len(_fighter_by_url)} URL variants.")

def _add_to_cache(fighter_id, name, ufc_url):
    """When we lazy-fetch a new fighter, immediately register them in the cache
    so subsequent fights on the same card can resolve them."""
    if not _fighter_by_url and not _fighter_by_name:
        return
    if ufc_url:
        for v in _url_variants(ufc_url):
            _fighter_by_url[v] = fighter_id
    if name:
        key = name.strip().lower()
        if key not in _fighter_by_name:
            _fighter_by_name[key] = fighter_id

def get_fighter_id(ufc_url, name=None):
    if _fighter_by_url is None:
        _load_fighter_cache()
    if ufc_url:
        if ufc_url in _fighter_by_url:
            return _fighter_by_url[ufc_url]
        for v in _url_variants(ufc_url):
            if v in _fighter_by_url:
                return _fighter_by_url[v]
    if name:
        key = name.strip().lower()
        if key in _fighter_by_name:
            return _fighter_by_name[key]
    return None


# =============================================================================
# Fighter scraper (parse_fighter, upsert_fighter, lazy_fetch_fighter)
# Duplicated from cage-metrics-scrapper/scraper.py.
# TODO: merge repos so this duplication goes away.
# =============================================================================

DIVISION_MAP = {
    "Strawweight": "Strawweight", "Flyweight": "Flyweight",
    "Bantamweight": "Bantamweight", "Featherweight": "Featherweight",
    "Lightweight": "Lightweight", "Welterweight": "Welterweight",
    "Middleweight": "Middleweight", "Light Heavyweight": "Light Heavyweight",
    "Heavyweight": "Heavyweight",
    "Women's Strawweight": "Women's Strawweight",
    "Women's Flyweight": "Women's Flyweight",
    "Women's Bantamweight": "Women's Bantamweight",
    "Women's Featherweight": "Women's Featherweight",
}

def extract_division(soup):
    rows = soup.select("tr.b-fight-details__table-row")
    for row in rows:
        if row.get("class") and "b-fight-details__table-row_type_head" in row.get("class", []):
            continue
        cols = row.select("td.b-fight-details__table-col")
        if not cols:
            continue
        for col in cols:
            text = col.get_text(" ", strip=True)
            for wc, clean in DIVISION_MAP.items():
                if wc.lower() in text.lower():
                    return clean
        break
    return None

def extract_last_fight_date(soup):
    """Pull the most recent fight date from the fight history table."""
    rows = soup.select("tr.b-fight-details__table-row")
    for row in rows:
        if row.get("class") and "b-fight-details__table-row_type_head" in row.get("class", []):
            continue
        cols = row.select("td.b-fight-details__table-col")
        if not cols:
            continue
        # Date is in the last column; format like "May. 03, 2025"
        date_text = cols[-1].get_text(" ", strip=True)
        try:
            # Try common formats
            for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y"):
                try:
                    return datetime.strptime(date_text, fmt).date().isoformat()
                except ValueError:
                    continue
        except Exception:
            pass
        break
    return None

def parse_fighter(url):
    """Scrape one fighter's profile page. Same logic as scraper.py:parse_fighter
    plus we also extract last_fight_date here."""
    soup = get_soup(url)
    if not soup:
        return None

    name_el = soup.select_one("span.b-content__title-highlight")
    name = name_el.get_text(strip=True) if name_el else None
    nick_el = soup.select_one("p.b-content__Nickname")
    nickname = nick_el.get_text(strip=True) if nick_el else None
    if nickname:
        nickname = nickname.strip('"').strip()

    record_el = soup.select_one("span.b-content__title-record")
    wins = losses = draws = 0
    if record_el:
        rtext = record_el.get_text(strip=True).replace("Record:", "").strip()
        m = re.match(r"(\d+)-(\d+)-(\d+)", rtext)
        if m:
            wins, losses, draws = int(m.group(1)), int(m.group(2)), int(m.group(3))

    info = {}
    for li in soup.select("li.b-list__box-list-item"):
        title = li.select_one("i.b-list__box-item-title")
        if not title:
            continue
        key = title.get_text(strip=True).rstrip(":").lower()
        title.extract()
        value = li.get_text(strip=True)
        info[key] = value

    dob = parse_dob(info.get("dob"))
    age = calc_age(dob)
    division = extract_division(soup)
    last_fight_date = extract_last_fight_date(soup)

    return {
        "name": name,
        "nickname": nickname or None,
        "division": division,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "height_in": parse_height(info.get("height")),
        "reach_in": parse_reach(info.get("reach")),
        "stance": info.get("stance") if info.get("stance") and info.get("stance") != "--" else None,
        "dob": dob.isoformat() if dob else None,
        "age": age,
        "slpm": parse_num(info.get("slpm")),
        "str_acc": parse_pct(info.get("str. acc.")),
        "sapm": parse_num(info.get("sapm")),
        "str_def": parse_pct(info.get("str. def")),
        "td_avg": parse_num(info.get("td avg.")),
        "td_acc": parse_pct(info.get("td acc.")),
        "td_def": parse_pct(info.get("td def.")),
        "sub_avg": parse_num(info.get("sub. avg.")),
        "last_fight_date": last_fight_date,
        "ufc_url": url,
    }

def upsert_fighter(fighter):
    """Upsert into fighters table. Returns the row id, or None on failure."""
    try:
        res = supabase.table("fighters").upsert(fighter, on_conflict="ufc_url").execute()
        if res.data:
            return res.data[0]["id"]
        existing = supabase.table("fighters").select("id").eq("ufc_url", fighter.get("ufc_url")).execute()
        if existing.data:
            return existing.data[0]["id"]
        return None
    except Exception as e:
        print(f"  ! Fighter upsert error for {fighter.get('name')}: {e}")
        STATS["errors"] += 1
        return None

def lazy_fetch_fighter(url, name=None):
    """If a fighter URL appears in event/fight parsing but they aren't in our
    cache, fetch them now, insert into the database, and add to the cache.
    Returns the new fighter id, or None if scrape failed.
    
    This is the fix for the Marwan/Schmid bug — short-notice replacements get
    discovered immediately instead of being silently dropped as NULL fighter_id.
    """
    if not url:
        return None
    print(f"    → lazy-fetch {name or '(unknown)'}: {url}")
    fighter = parse_fighter(url)
    if not fighter or not fighter.get("name"):
        print(f"    ! lazy-fetch failed for {url}")
        return None
    fid = upsert_fighter(fighter)
    if fid:
        _add_to_cache(fid, fighter["name"], url)
        STATS["fighters_lazy_fetched"] += 1
        print(f"    ✓ added fighter id={fid} name={fighter['name']}")
    return fid


# =============================================================================
# Event & fight scraper (from events_scraper.py)
# Modified: get_fighter_id now lazy-fetches when missing.
# =============================================================================

def get_event_urls(upcoming=False):
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

def parse_event(url, is_upcoming=False):
    soup = get_soup(url)
    if not soup:
        return None, []
    name_el = soup.select_one("span.b-content__title-highlight")
    name = name_el.get_text(strip=True) if name_el else None

    info = {}
    for li in soup.select("li.b-list__box-list-item"):
        text = li.get_text(separator="|", strip=True)
        parts = text.split("|", 1)
        if len(parts) == 2:
            key = parts[0].rstrip(":").lower().strip()
            info[key] = parts[1].strip()

    event = {
        "ufc_event_id": extract_id_from_url(url),
        "name": name,
        "event_date": parse_event_date(info.get("date")),
        "location": info.get("location"),
        "is_upcoming": is_upcoming,
        "ufc_url": url,
    }
    fight_rows = soup.select("tr.b-fight-details__table-row[data-link]")
    fight_urls = [r.get("data-link") for r in fight_rows if r.get("data-link")]
    return event, fight_urls

def get_or_lazy_fetch_fighter_id(url, name):
    """Wrapper: try cache first, then lazy-fetch if missing."""
    fid = get_fighter_id(url, name)
    if fid is not None:
        return fid
    # Cache miss → fetch the fighter now
    return lazy_fetch_fighter(url, name)

def parse_fight(url):
    soup = get_soup(url)
    if not soup:
        return None, []
    fight = {"ufc_fight_id": extract_id_from_url(url), "ufc_url": url}

    persons = soup.select("div.b-fight-details__person")
    if len(persons) < 2:
        return None, []

    fighter_data = []
    for p in persons:
        a = p.select_one("a.b-fight-details__person-link")
        name = a.get_text(strip=True) if a else None
        f_url = a.get("href") if a else None
        status_el = p.select_one("i.b-fight-details__person-status")
        status = status_el.get_text(strip=True) if status_el else None
        fighter_data.append({"name": name, "url": f_url, "status": status})

    # Lazy-fetch fighters if not in cache. This is the bug fix.
    fight["fighter_a_name"] = fighter_data[0]["name"]
    fight["fighter_b_name"] = fighter_data[1]["name"]
    fight["fighter_a_id"] = get_or_lazy_fetch_fighter_id(fighter_data[0]["url"], fighter_data[0]["name"])
    fight["fighter_b_id"] = get_or_lazy_fetch_fighter_id(fighter_data[1]["url"], fighter_data[1]["name"])

    if fighter_data[0]["status"] == "W":
        fight["winner_id"] = fight["fighter_a_id"]
    elif fighter_data[1]["status"] == "W":
        fight["winner_id"] = fight["fighter_b_id"]
    else:
        fight["winner_id"] = None

    fight_head = soup.select_one("div.b-fight-details__fight")
    if fight_head:
        title_el = fight_head.select_one("i.b-fight-details__fight-title")
        if title_el:
            title_text = title_el.get_text(separator=" ", strip=True)
            fight["is_title_fight"] = "Title" in title_text or "Championship" in title_text
            wc = re.sub(r"(UFC )?(Interim )?(Women's )?(.*?)(Title Bout|Bout)?$", r"\4", title_text).strip()
            fight["weight_class"] = wc or title_text

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
                m = re.match(r"(\d+)", value)
                if m:
                    fight["scheduled_rounds"] = int(m.group(1))

        detail_el = fight_head.select_one("p.b-fight-details__text:nth-of-type(2)")
        if detail_el:
            fight["method_detail"] = detail_el.get_text(strip=True)

    rounds_data = parse_round_stats(soup, fight["fighter_a_id"], fight["fighter_b_id"])
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
    """Same as events_scraper.py — kept verbatim."""
    rounds = {}
    per_round_tables = soup.select("table.js-fight-table")
    for table in per_round_tables:
        thead = table.select_one("thead.b-fight-details__table-head_rnd")
        if not thead:
            continue
        headers_text = " ".join(th.get_text(strip=True).lower() for th in thead.select("th"))
        if "kd" in headers_text and "ctrl" in headers_text:
            section_type = "totals"
        elif "head" in headers_text and "body" in headers_text and "leg" in headers_text:
            section_type = "sig_strikes"
        else:
            continue
        round_num = 0
        body = table.select_one("tbody") or table
        for el in body.find_all(["thead", "tr"], recursive=True):
            classes = el.get("class") or []
            if "b-fight-details__table-row_type_head" in classes:
                m = re.search(r"Round\s*(\d+)", el.get_text(strip=True))
                if m:
                    round_num = int(m.group(1))
                continue
            if el.name != "tr" or "b-fight-details__table-row" not in classes or round_num == 0:
                continue
            cols = el.select("td.b-fight-details__table-col")
            if not cols:
                continue
            def col_pair(col):
                ps = col.select("p.b-fight-details__table-text")
                if len(ps) >= 2:
                    return ps[0].get_text(strip=True), ps[1].get_text(strip=True)
                return (None, None)
            r = rounds.get(round_num, {
                "round_number": round_num, "fighter_a_id": fighter_a_id, "fighter_b_id": fighter_b_id,
            })
            if section_type == "totals" and len(cols) >= 10:
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
            elif section_type == "sig_strikes" and len(cols) >= 9:
                a, b = col_pair(cols[3])
                r["a_head_landed"], _ = parse_x_of_y(a); r["b_head_landed"], _ = parse_x_of_y(b)
                a, b = col_pair(cols[4])
                r["a_body_landed"], _ = parse_x_of_y(a); r["b_body_landed"], _ = parse_x_of_y(b)
                a, b = col_pair(cols[5])
                r["a_leg_landed"], _ = parse_x_of_y(a); r["b_leg_landed"], _ = parse_x_of_y(b)
                a, b = col_pair(cols[6])
                r["a_distance_landed"], _ = parse_x_of_y(a); r["b_distance_landed"], _ = parse_x_of_y(b)
                a, b = col_pair(cols[7])
                r["a_clinch_landed"], _ = parse_x_of_y(a); r["b_clinch_landed"], _ = parse_x_of_y(b)
                a, b = col_pair(cols[8])
                r["a_ground_landed"], _ = parse_x_of_y(a); r["b_ground_landed"], _ = parse_x_of_y(b)
            rounds[round_num] = r
    return list(rounds.values())

def upsert_event(event):
    try:
        res = supabase.table("events").upsert(event, on_conflict="ufc_event_id").execute()
        if res.data:
            return res.data[0]["id"]
        existing = supabase.table("events").select("id").eq("ufc_event_id", event["ufc_event_id"]).execute()
        if existing.data:
            return existing.data[0]["id"]
        return None
    except Exception as e:
        print(f"  ! Event upsert error: {e}")
        STATS["errors"] += 1
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
        STATS["errors"] += 1
        return None

def upsert_rounds(fight_id, rounds):
    if not rounds or not fight_id:
        return
    try:
        supabase.table("fight_rounds").delete().eq("fight_id", fight_id).execute()
        rows = [{**r, "fight_id": fight_id} for r in rounds]
        if rows:
            supabase.table("fight_rounds").insert(rows).execute()
    except Exception as e:
        print(f"  ! Rounds upsert error for fight {fight_id}: {e}")
        STATS["errors"] += 1


# =============================================================================
# PHASE FUNCTIONS
# =============================================================================

def phase_1_upcoming_events():
    """Refresh the upcoming events list. Pulls every upcoming card and its
    fight list. Late-replacement fighters get caught here via lazy_fetch."""
    print("\n[PHASE 1] Refreshing upcoming events...")
    upcoming_urls = get_event_urls(upcoming=True)
    print(f"  Found {len(upcoming_urls)} upcoming events")
    for ev_idx, event_url in enumerate(upcoming_urls, 1):
        print(f"\n  [{ev_idx}/{len(upcoming_urls)}] {event_url}")
        event, fight_urls = parse_event(event_url, is_upcoming=True)
        if not event:
            continue
        STATS["events_seen"] += 1
        event_db_id = upsert_event(event)
        if not event_db_id:
            continue
        STATS["events_upserted"] += 1
        print(f"    {event.get('name')} ({event.get('event_date')}) — {len(fight_urls)} fights")
        for f_idx, fight_url in enumerate(fight_urls, 1):
            fight, rounds = parse_fight(fight_url)
            if not fight:
                continue
            fight["event_id"] = event_db_id
            fight["is_main_event"] = (f_idx == 1)
            fight_id = upsert_fight(fight)
            STATS["fights_upserted"] += 1
            if fight_id and rounds:
                upsert_rounds(fight_id, rounds)


def phase_2_recent_completed_events():
    """Re-scrape recently completed events to capture results from the
    last weekend. Bounded by RECENT_EVENT_DAYS (default 14)."""
    print(f"\n[PHASE 2] Refreshing events from last {RECENT_EVENT_DAYS} days...")
    cutoff = (date.today() - timedelta(days=RECENT_EVENT_DAYS)).isoformat()
    res = (supabase.table("events")
           .select("id, ufc_url, event_date")
           .eq("is_upcoming", False)
           .gte("event_date", cutoff)
           .order("event_date", desc=True)
           .execute())
    targets = res.data or []
    # Also flip is_upcoming=true rows whose date passed (data hygiene)
    stale_res = (supabase.table("events")
                 .select("id, ufc_url, event_date")
                 .eq("is_upcoming", True)
                 .lt("event_date", date.today().isoformat())
                 .execute())
    targets.extend(stale_res.data or [])
    print(f"  {len(targets)} events to refresh")
    for ev_idx, ev in enumerate(targets, 1):
        print(f"\n  [{ev_idx}/{len(targets)}] {ev['ufc_url']} ({ev.get('event_date')})")
        event, fight_urls = parse_event(ev["ufc_url"], is_upcoming=False)
        if not event:
            continue
        STATS["events_seen"] += 1
        event_db_id = upsert_event(event)
        if not event_db_id:
            continue
        STATS["events_upserted"] += 1
        for f_idx, fight_url in enumerate(fight_urls, 1):
            fight, rounds = parse_fight(fight_url)
            if not fight:
                continue
            fight["event_id"] = event_db_id
            fight["is_main_event"] = (f_idx == 1)
            fight_id = upsert_fight(fight)
            STATS["fights_upserted"] += 1
            if fight_id and rounds:
                upsert_rounds(fight_id, rounds)


def phase_3_resolve_null_fighters():
    """Final sweep: any fights with NULL fighter_a_id or fighter_b_id, try to
    resolve from the now-fresh cache (which includes any lazy-fetched fighters
    from phases 1 & 2)."""
    print("\n[PHASE 3] Resolving any remaining NULL fighter_id rows...")
    res = (supabase.table("fights")
           .select("id, ufc_url, fighter_a_id, fighter_b_id, fighter_a_name, fighter_b_name")
           .or_("fighter_a_id.is.null,fighter_b_id.is.null")
           .execute())
    null_rows = res.data or []
    print(f"  {len(null_rows)} fights have at least one NULL fighter_id")
    if not null_rows:
        return
    for row in null_rows:
        # Re-fetch the fight page to get the fighter URLs
        soup = get_soup(row["ufc_url"])
        if not soup:
            continue
        persons = soup.select("div.b-fight-details__person")
        if len(persons) < 2:
            continue
        update = {}
        for idx, p in enumerate(persons[:2]):
            a = p.select_one("a.b-fight-details__person-link")
            f_url = a.get("href") if a else None
            f_name = a.get_text(strip=True) if a else None
            current_key = "fighter_a_id" if idx == 0 else "fighter_b_id"
            if row.get(current_key) is None:
                fid = get_or_lazy_fetch_fighter_id(f_url, f_name)
                if fid:
                    update[current_key] = fid
        if update:
            try:
                supabase.table("fights").update(update).eq("id", row["id"]).execute()
                STATS["null_fighters_resolved"] += len(update)
                print(f"    ✓ resolved {len(update)} fighter id(s) for fight {row['id']}")
            except Exception as e:
                print(f"    ! update error: {e}")
                STATS["errors"] += 1


def phase_4_refresh_recent_fighters():
    """Re-scrape every fighter who fought in the last RECENT_FIGHTER_DAYS days.
    Their stats just changed (new record, new last_fight_date, updated career
    averages)."""
    print(f"\n[PHASE 4] Refreshing fighters who fought in last {RECENT_FIGHTER_DAYS} days...")
    cutoff = (date.today() - timedelta(days=RECENT_FIGHTER_DAYS)).isoformat()
    # Find fight ids from recent completed events
    ev_res = (supabase.table("events")
              .select("id")
              .eq("is_upcoming", False)
              .gte("event_date", cutoff)
              .execute())
    event_ids = [r["id"] for r in (ev_res.data or [])]
    if not event_ids:
        print("  No completed events in window — nothing to refresh.")
        return
    fight_res = (supabase.table("fights")
                 .select("fighter_a_id, fighter_b_id")
                 .in_("event_id", event_ids)
                 .execute())
    fighter_ids = set()
    for row in (fight_res.data or []):
        if row.get("fighter_a_id"): fighter_ids.add(row["fighter_a_id"])
        if row.get("fighter_b_id"): fighter_ids.add(row["fighter_b_id"])
    if not fighter_ids:
        print("  No fighters found in window.")
        return
    # Fetch their UFC URLs
    fighter_ids = list(fighter_ids)
    urls = []
    # Batch by 100s to avoid query bloat
    for i in range(0, len(fighter_ids), 100):
        batch = fighter_ids[i:i+100]
        res = supabase.table("fighters").select("ufc_url").in_("id", batch).execute()
        urls.extend([r["ufc_url"] for r in (res.data or []) if r.get("ufc_url")])
    print(f"  Refreshing {len(urls)} fighters who recently fought...")
    for i, url in enumerate(urls, 1):
        if i % 10 == 0:
            print(f"    [{i}/{len(urls)}] refreshed so far")
        fighter = parse_fighter(url)
        if fighter and fighter.get("name"):
            if upsert_fighter(fighter):
                STATS["fighters_refreshed"] += 1


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 64)
    print("CFL Weekly Refresh")
    print(f"Started at {datetime.now().isoformat()}")
    print("=" * 64)
    started = time.time()

    _load_fighter_cache()
    phase_1_upcoming_events()
    phase_2_recent_completed_events()
    phase_3_resolve_null_fighters()
    phase_4_refresh_recent_fighters()

    elapsed_min = (time.time() - started) / 60
    print()
    print("=" * 64)
    print("DONE")
    print("=" * 64)
    print(f"  Runtime:                  {elapsed_min:.1f} minutes")
    print(f"  Events seen:              {STATS['events_seen']}")
    print(f"  Events upserted:          {STATS['events_upserted']}")
    print(f"  Fights upserted:          {STATS['fights_upserted']}")
    print(f"  Fighters lazy-fetched:    {STATS['fighters_lazy_fetched']}")
    print(f"  Fighters refreshed:       {STATS['fighters_refreshed']}")
    print(f"  Null fighter ids resolved:{STATS['null_fighters_resolved']}")
    print(f"  Errors:                   {STATS['errors']}")
    print("=" * 64)


if __name__ == "__main__":
    main()
