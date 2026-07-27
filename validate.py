#!/usr/bin/env python3
"""
NPS Artist-in-Residence directory: discovery, validation, and deadline extraction.

Usage:
    python validate.py discover   # scan ALL ~470 NPS units for AIR pages
    python validate.py check      # verify pages + extract application deadlines
    python validate.py partners   # enrich from partner orgs (NPAF, AIRIE)
    python validate.py all        # all three (default)

Environment:
    NPS_API_KEY        free key (nps.gov/subjects/developer) - required for discover
    ANTHROPIC_API_KEY  enables LLM extraction of deadlines/open-call status.
                       Without it, a regex fallback captures less.
    EXTRACT_MODEL      optional, default "claude-haiku-4-5-20251001"

Pipeline:
    discover  - enumerate every NPS unit via the Data API, scan each unit's
                siteindex.htm for AIR links (catches nonstandard URLs).
    check     - fetch each program page; classify verified/inactive/broken;
                extract structured application info (accepting?, opens,
                deadline, apply URL). If the page defers to CaFE/Submittable/
                a partner, follow that link one hop and extract there.
    partners  - fetch NPAF and AIRIE sites; fill deadlines for programs the
                park's own page doesn't cover.
"""

import json
import os
import re
import sys
import time
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
NPS_API = "https://developer.nps.gov/api/v1/parks"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
UA = {"User-Agent": "nps-air-directory/2.0 (open-source AIR program index)"}
TIMEOUT = 25
SLEEP = 0.4

AIR_RE = re.compile(r"artist\S{0,2}[\s\-]*in[\s\-]*residen", re.IGNORECASE)
INACTIVE_PHRASES = [
    "not currently accepting", "not accepting applications", "currently on hold",
    "on pause", "paused", "suspended", "on hiatus", "discontinued",
    "no longer offer", "not offering", "has ended",
]
OPEN_PHRASES = ["now accepting", "applications are open", "currently accepting",
                "apply now", "applications are being accepted", "call for artists is open"]
HOP_HOSTS = ("callforentry.org", "artist.callforentry.org", "submittable.com",
             "nationalparksartsfoundation.org", "airie.org")
MONTHS = ("january|february|march|april|may|june|july|august|september|"
          "october|november|december")

PARTNER_SOURCES = [
    ("National Parks Arts Foundation",
     "https://www.nationalparksartsfoundation.org/allresidencies", "npaf"),
    ("AIRIE (Artists in Residence in Everglades)", "https://airie.org/", "airie"),
]


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None


# ------------------------------------------------------------------ helpers

def load():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data):
    data["programs"].sort(key=lambda p: p["name"].lower())
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get(url, **kw):
    time.sleep(SLEEP)
    return requests.get(url, headers=UA, timeout=TIMEOUT, **kw)


def fetch(url):
    """Return (html, error)."""
    try:
        r = get(url, allow_redirects=True)
    except requests.RequestException as e:
        return None, f"request failed: {type(e).__name__}"
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}"
    return r.text, None


def strip_html(html):
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(html)).strip()


def focus(text, span=12000):
    """Window the text around application-related language to keep tokens down."""
    m = re.search(r"applic|deadline|submission|due date", text, re.I) or AIR_RE.search(text)
    if not m:
        return text[:span]
    start = max(0, m.start() - span // 3)
    return text[start:start + span]


EMPTY_APP = {"accepting": None, "opens": None, "deadline": None, "period_text": None,
             "apply_url": None, "apply_via": None, "next_expected": None,
             "evidence": None, "extracted": None, "method": None}


# ------------------------------------------------------------------ extract

def llm_extract(text, park_name, page_url, api_key, model):
    prompt = (
        f"Today is {date.today().isoformat()}. Below is text from an "
        f"artist-in-residence page related to {park_name} ({page_url}).\n"
        "Extract application information. Reply with ONLY a JSON object, no prose:\n"
        '{"accepting": true|false|null, "opens": "YYYY-MM-DD"|null, '
        '"deadline": "YYYY-MM-DD"|null, "period_text": "short phrase describing the '
        'application window"|null, "apply_url": "direct application URL if given"|null, '
        '"apply_via": "park"|"cafe"|"submittable"|"partner"|"email"|null, '
        '"next_expected": "when applications are expected to reopen, if stated"|null, '
        '"evidence": "shortest quote, max 20 words, supporting accepting/deadline"|null}\n'
        "Rules: use null when the page does not say. If a date lacks a year, choose "
        "the next future occurrence. accepting=true ONLY if applications are open "
        "now according to the page.\n\nPAGE TEXT:\n" + focus(text)
    )
    r = requests.post(
        ANTHROPIC_API,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 400,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    out = "".join(b.get("text", "") for b in r.json()["content"]
                  if b.get("type") == "text").strip()
    out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out)
    parsed = json.loads(out)
    app = dict(EMPTY_APP)
    for k in app:
        if k in parsed:
            app[k] = parsed[k]
    app["method"] = "llm"
    return app


def regex_extract(text):
    """Keyless fallback: coarse open/closed signal + the sentence with dates."""
    app = dict(EMPTY_APP)
    low = text.lower()
    if any(p in low for p in OPEN_PHRASES):
        app["accepting"] = True
    elif any(p in low for p in INACTIVE_PHRASES) or "closed" in low:
        app["accepting"] = False
    m = re.search(
        rf"([^.]*applic[^.]*?(?:{MONTHS})\s+\d{{1,2}}(?:,?\s+\d{{4}})?[^.]*\.)",
        low, re.IGNORECASE)
    if m:
        app["period_text"] = m.group(1).strip()[:240]
    app["method"] = "regex"
    return app


def extract_application(text, park_name, page_url):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            app = llm_extract(text, park_name, page_url,
                              api_key, os.environ.get("EXTRACT_MODEL", DEFAULT_MODEL))
        except Exception as e:
            print(f"    ! extraction failed ({type(e).__name__}), regex fallback")
            app = regex_extract(text)
    else:
        app = regex_extract(text)

    # Park page defers elsewhere and gave no dates? Follow one hop and retry.
    hop = app.get("apply_url")
    if hop and not app.get("deadline"):
        host = urlparse(hop).netloc.lower()
        if any(host.endswith(h) for h in HOP_HOSTS):
            html, err = fetch(hop)
            if html and api_key:
                try:
                    hop_app = llm_extract(strip_html(html), park_name, hop,
                                          api_key,
                                          os.environ.get("EXTRACT_MODEL", DEFAULT_MODEL))
                    for k in ("accepting", "opens", "deadline", "period_text",
                              "next_expected", "evidence"):
                        if hop_app.get(k) is not None:
                            app[k] = hop_app[k]
                    app["method"] = "llm+hop"
                except Exception:
                    pass
        if "callforentry" in (urlparse(hop).netloc or ""):
            app["apply_via"] = app.get("apply_via") or "cafe"
    app["extracted"] = date.today().isoformat()
    return app


# ------------------------------------------------------------------ check

def classify(text):
    if not AIR_RE.search(text):
        return "broken", "page no longer mentions artist-in-residence (possible redirect)"
    low = text.lower()
    for phrase in INACTIVE_PHRASES:
        if phrase in low:
            return "inactive", f'page says: "{phrase}"'
    return "verified", ""


def check(data):
    today = date.today().isoformat()
    counts = {"verified": 0, "inactive": 0, "broken": 0, "skipped": 0}
    open_calls = 0
    for p in data["programs"]:
        if not p.get("url"):
            counts["skipped"] += 1
            continue
        html, err = fetch(p["url"])
        if err:
            p["status"], p["notes"] = "broken", err
            counts["broken"] += 1
            print(f"  [broken  ] {p['name']} - {err}")
            continue
        text = strip_html(html)
        status, note = classify(text)
        p["status"], p["notes"] = status, note
        if status in ("verified", "inactive"):
            p["last_verified"] = today
            p["application"] = extract_application(text, p["name"], p["url"])
            if p["application"].get("accepting") is True:
                open_calls += 1
        counts[status] += 1
        due = (p.get("application") or {}).get("deadline")
        print(f"  [{status:8}] {p['name']}" + (f" - due {due}" if due else ""))
    print(f"Check done: {counts['verified']} verified, {counts['inactive']} inactive, "
          f"{counts['broken']} broken, {counts['skipped']} without URL. "
          f"{open_calls} open calls.")


# ------------------------------------------------------------------ partners

def partners(data):
    for org_name, url, tag in PARTNER_SOURCES:
        print(f"Partner scan: {org_name}")
        html, err = fetch(url)
        if err:
            print(f"  ! {err}")
            continue
        text = strip_html(html)
        low = text.lower()
        for p in data["programs"]:
            short = re.sub(r"\s+national.*$", "", p["name"], flags=re.I).lower()
            mentioned = (p.get("partner") and tag in ("npaf", "airie")
                         and org_name.split(" (")[0].lower() in (p["partner"] or "").lower())
            if not mentioned and short and short in low:
                mentioned = True
                p["partner"] = p.get("partner") or org_name
            if not mentioned:
                continue
            app = p.get("application") or {}
            if app.get("deadline") or app.get("accepting") is True:
                continue  # park's own page already answered
            extracted = extract_application(text, p["name"], url)
            if any(extracted.get(k) is not None
                   for k in ("accepting", "deadline", "opens", "period_text")):
                extracted["apply_via"] = "partner"
                p["application"] = extracted
                print(f"  enriched: {p['name']}")


# ------------------------------------------------------------------ discover

def fetch_all_units(api_key):
    units, start = [], 0
    while True:
        r = get(NPS_API, params={"limit": 100, "start": start, "api_key": api_key})
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("data", [])
        units.extend(batch)
        start += len(batch)
        if start >= int(payload.get("total", 0)) or not batch:
            break
    return units


def scan_unit_for_air(code):
    html, err = fetch(f"https://www.nps.gov/{code}/siteindex.htm")
    if err:
        return []
    parser = LinkParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    hits = []
    for href, text in parser.links:
        if not href:
            continue
        if AIR_RE.search(href) or AIR_RE.search(text):
            if href.startswith("/"):
                href = "https://www.nps.gov" + href
            if href.startswith("https://www.nps.gov"):
                hits.append((href, text))
    return hits


def discover(data):
    api_key = os.environ.get("NPS_API_KEY")
    if not api_key:
        sys.exit("discover requires NPS_API_KEY (free: nps.gov/subjects/developer)")
    print("Enumerating all NPS units via Data API...")
    units = fetch_all_units(api_key)
    print(f"  {len(units)} units. Scanning site indexes for AIR pages...")
    by_code = {p["unit_code"]: p for p in data["programs"]}
    added = updated = 0
    for i, u in enumerate(units, 1):
        code = u.get("parkCode", "").lower()
        if not code:
            continue
        if i % 50 == 0:
            print(f"  ...{i}/{len(units)}")
        hits = scan_unit_for_air(code)
        if not hits:
            continue
        url = hits[0][0]
        if code in by_code:
            p = by_code[code]
            if not p.get("url"):
                p["url"], p["source"] = url, "crawler"
                updated += 1
            elif p.get("status") == "broken" and p["url"] != url:
                p["notes"] = (p.get("notes") or "") + f" crawler candidate: {url}"
        else:
            entry = {"unit_code": code, "name": u.get("fullName", code),
                     "designation": u.get("designation") or "NPS Unit",
                     "states": (u.get("states") or "").split(","),
                     "url": url, "partner": None, "status": "unverified",
                     "last_verified": None, "source": "crawler",
                     "notes": f"found via site index: {hits[0][1]}",
                     "application": None}
            data["programs"].append(entry)
            by_code[code] = entry
            added += 1
    data["meta"]["last_full_crawl"] = date.today().isoformat()
    print(f"Discovery done: {added} new programs, {updated} URLs filled in.")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode not in ("discover", "check", "partners", "all"):
        sys.exit(__doc__)
    data = load()
    if mode in ("discover", "all"):
        discover(data)
        save(data)
    if mode in ("check", "all"):
        check(data)
        save(data)
    if mode in ("partners", "all"):
        partners(data)
        save(data)


if __name__ == "__main__":
    main()
