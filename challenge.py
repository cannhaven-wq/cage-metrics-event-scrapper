"""
challenge.py
============
UFCStats deployed a JavaScript proof-of-work interstitial ("Checking your
browser…") in front of every page. A plain `requests.get` now returns a
~3 KB challenge page instead of real content, which silently broke every
scraper (events parsed to name=None, event lists came back empty).

The challenge is lightweight and does NOT require a browser:

  1. The page embeds  var nonce="<hex>"  and a difficulty
     ( target = new Array(D+1).join('0')  →  D leading hex zeros ).
  2. The client brute-forces an integer n such that
     sha256(nonce + ":" + n) starts with D zeros (D=2 today → ~256 tries).
  3. It POSTs  nonce=<nonce>&n=<n>  to  /__c , the server sets a clearance
     cookie (`_fmc`), and the real page is served on the next request.

We replicate that in pure Python with hashlib. Use a single Session per run:
once the cookie is set it carries every subsequent request, and if the cookie
expires mid-run the next response is a fresh challenge that we solve again.

We keep the honest identifying User-Agent — we pass the gate by doing the
proof-of-work the site asks for, not by pretending to be a browser.

Usage:
    from challenge import make_session, get_soup
    session = make_session()
    soup = get_soup(session, "http://www.ufcstats.com/...")  # auto-solves
"""

import re
import hashlib
import requests
from bs4 import BeautifulSoup

BASE = "http://www.ufcstats.com"
USER_AGENT = "CannonFightLab/1.0 (Personal UFC stats project)"

_NONCE_RE = re.compile(r'nonce\s*=\s*"([0-9a-fA-F]+)"')
_DIFF_RE = re.compile(r'target\s*=\s*new Array\(\s*(\d+)\s*\+\s*1\s*\)\.join\(')


def make_session():
    """A Session pre-seeded with our identifying User-Agent. Reuse it for a
    whole run so the clearance cookie is shared across requests."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def is_challenge(text):
    """True if the response body is the PoW interstitial rather than content."""
    return ("Checking your browser" in text) or (
        "/__c" in text and "var nonce=" in text and "sha256" in text
    )


def _solve(nonce, difficulty):
    """Find the smallest n where sha256('<nonce>:<n>') has `difficulty`
    leading hex zeros. Difficulty is 2 today, so this returns in ~256 tries."""
    prefix = "0" * difficulty
    n = 0
    while True:
        h = hashlib.sha256(f"{nonce}:{n}".encode()).hexdigest()
        if h[:difficulty] == prefix:
            return n
        n += 1


def solve_challenge(session, html, base=BASE):
    """Parse the challenge out of `html`, solve the PoW, and POST it to /__c so
    the server hands us the clearance cookie. Returns True on success."""
    nonce_m = _NONCE_RE.search(html)
    diff_m = _DIFF_RE.search(html)
    if not nonce_m or not diff_m:
        return False
    nonce = nonce_m.group(1)
    difficulty = int(diff_m.group(1))
    n = _solve(nonce, difficulty)
    r = session.post(
        base + "/__c",
        data={"nonce": nonce, "n": n},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    return 200 <= r.status_code < 300


def get(session, url, timeout=30, max_solves=2):
    """GET `url` through `session`, transparently solving the PoW challenge if
    it appears (including a fresh challenge after a mid-run cookie expiry).
    Returns the final Response."""
    r = session.get(url, timeout=timeout)
    solves = 0
    while is_challenge(r.text) and solves < max_solves:
        if not solve_challenge(session, r.text, base=_origin(url)):
            break
        solves += 1
        r = session.get(url, timeout=timeout)
    return r


def get_soup(session, url, timeout=30):
    """Convenience: get(...) + BeautifulSoup, or None on hard failure."""
    r = get(session, url, timeout=timeout)
    r.raise_for_status()
    if is_challenge(r.text):
        return None
    return BeautifulSoup(r.text, "html.parser")


def _origin(url):
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else BASE
