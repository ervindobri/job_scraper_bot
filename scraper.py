import html
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
QUERIES_FILE = ROOT / "queries.json"
SEEN_FILE = ROOT / "seen_jobs.json"

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
SEARCH_PAGE_URL = "https://www.linkedin.com/jobs/search/"  # human-facing equivalent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
TIME_WINDOW = "r7200"  # 2h lookback: overlaps the hourly cron so a delayed or skipped
                       # run self-heals. seen_jobs.json filters the duplicates.
PAGE_SIZE = 10  # what the guest endpoint actually returns per request, regardless of ask
MAX_PAGES = 10  # x PAGE_SIZE = up to 100 postings per query
PAGE_DELAY = 2  # seconds between page requests; LinkedIn 429s if pushed harder
PAGE_RETRIES = 3
SEEN_TTL_DAYS = 14
MAX_MESSAGE_CHARS = 4096  # Telegram hard limit
CHUNK_BUDGET = 3500  # per message, leaving room for the header

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def load_json(path, default):
    """Load path, or return default if it does not exist yet. Malformed JSON is an error."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"{path.name} is not valid JSON: {e}")


def fetch_page(params, label):
    """GET one page of results, retrying transient failures. None means give up."""
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            r = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"  request failed ({label}): {e}")
            return None
        if r.status_code == 200:
            return r
        # 429 is routine when paginating deep; back off rather than truncating.
        if r.status_code in (429, 500, 502, 503, 504) and attempt < PAGE_RETRIES:
            wait = int(r.headers.get("Retry-After") or 0) or PAGE_DELAY * 2 ** attempt
            print(f"  HTTP {r.status_code} ({label}), retrying in {wait}s")
            time.sleep(wait)
            continue
        print(f"  HTTP {r.status_code} ({label}), skipping rest of this query")
        return None
    return None


def parse_cards(html_text):
    cards = BeautifulSoup(html_text, "html.parser").select("div.base-card")
    out = []
    for card in cards:
        urn = card.get("data-entity-urn", "")
        job_id = urn.rsplit(":", 1)[-1]
        if not job_id:
            continue
        title = card.select_one(".base-search-card__title")
        company = card.select_one(".base-search-card__subtitle")
        location = card.select_one(".job-search-card__location")
        out.append({
            "id": job_id,
            "title": title.get_text(strip=True) if title else "",
            "company": company.get_text(strip=True) if company else "",
            "location": location.get_text(strip=True) if location else "",
            "url": f"https://www.linkedin.com/jobs/view/{job_id}",
        })
    return len(cards), out


def fetch_jobs(query):
    jobs = []
    ids = set()
    label = f"{query['keywords']!r} @ {query.get('location', '')!r}"
    for page in range(MAX_PAGES):
        params = {
            "keywords": query["keywords"],
            "location": query.get("location", ""),
            "f_TPR": TIME_WINDOW,
            "start": page * PAGE_SIZE,
            "origin": "SEMANTIC_SEARCH_LANDING_PAGE",
        }
        r = fetch_page(params, f"{label} start={params['start']}")
        if r is None:
            break
        card_count, parsed = parse_cards(r.text)
        for job in parsed:
            if job["id"] not in ids:   # LinkedIn repeats rows near the tail
                ids.add(job["id"])
                jobs.append(job)
        if card_count < PAGE_SIZE:
            break  # last page
        if page == MAX_PAGES - 1:
            print(f"  note: {label} filled all {MAX_PAGES} pages; more results exist "
                  f"than MAX_PAGES allows")
        time.sleep(PAGE_DELAY)
    return jobs


def matches_excludes(job, query):
    title = job["title"].lower()
    return any(word.lower() in title for word in query.get("exclude", []))


def format_job(job):
    """One job as a compact, clickable block."""
    title = html.escape(job["title"]) or "(untitled)"
    line = f'• <a href="{html.escape(job["url"], quote=True)}">{title}</a>'
    meta = " · ".join(p for p in (job["company"], job["location"]) if p)
    if meta:
        line += f"\n  {html.escape(meta)}"
    return line


def search_page_url(query):
    """The LinkedIn search page for this query, so the message links to all results."""
    params = {"keywords": query["keywords"], "f_TPR": TIME_WINDOW}
    location = query.get("location", "")
    if location:
        params["location"] = location
    return SEARCH_PAGE_URL + "?" + urlencode(params)


def format_header(query, total, part, parts):
    label = query["keywords"]
    location = query.get("location", "")
    if location:
        label += f" · {location}"
    header = f"🔎 <b>{html.escape(label)}</b> — {total} new"
    if parts > 1:
        header += f" ({part}/{parts})"
    url = html.escape(search_page_url(query), quote=True)
    return header + f'\n<a href="{url}">all results on LinkedIn ↗</a>'



def build_messages(query, jobs):
    """Group a query's jobs into as few messages as Telegram's size limit allows.

    Returns (text, jobs_in_message) pairs so the caller can mark jobs seen only
    once the message carrying them was actually delivered.
    """
    pages, current, size = [], [], 0
    for job in jobs:
        block = format_job(job)
        if len(block) > CHUNK_BUDGET:  # pathological title; keep it sendable
            block = block[:CHUNK_BUDGET]
        if current and size + len(block) + 2 > CHUNK_BUDGET:
            pages.append(current)
            current, size = [], 0
        current.append((block, job))
        size += len(block) + 2
    if current:
        pages.append(current)

    messages = []
    for i, page in enumerate(pages, 1):
        header = format_header(query, len(jobs), i, len(pages))
        text = header + "\n\n" + "\n\n".join(block for block, _ in page)
        messages.append((text, [job for _, job in page]))
    return messages


def send_message(text, attempts=3):
    """Post one message, honouring Telegram's rate-limit backoff."""
    for attempt in range(1, attempts + 1):
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if r.ok:
            time.sleep(0.5)
            return
        if r.status_code == 429 and attempt < attempts:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            print(f"rate limited, retrying in {wait}s")
            time.sleep(wait + 1)
            continue
        print(f"telegram {r.status_code}: {r.text[:200]}")
        r.raise_for_status()


def new_jobs_for(query, jobs, seen, now):
    """Filter a query's postings down to unseen, non-excluded ones."""
    fresh, batch_ids = [], set()
    for job in jobs:
        if job["id"] in seen or job["id"] in batch_ids:
            continue
        batch_ids.add(job["id"])
        if matches_excludes(job, query):
            seen[job["id"]] = now  # remember exclusions so we stop re-checking them
            continue
        fresh.append(job)
    return fresh


def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    queries = load_json(QUERIES_FILE, [])
    if not queries:
        sys.exit(f"no queries found in {QUERIES_FILE.name}")

    seen = load_json(SEEN_FILE, {})
    now = time.time()
    seen = {k: v for k, v in seen.items() if now - v < SEEN_TTL_DAYS * 86400}

    sent_jobs = 0
    sent_messages = 0
    found = 0
    try:
        for query in queries:
            jobs = fetch_jobs(query)
            found += len(jobs)
            fresh = new_jobs_for(query, jobs, seen, now)
            print(f"{query['keywords']!r} @ {query.get('location', '')!r}: "
                  f"{len(jobs)} found, {len(fresh)} new")
            if not fresh:
                continue
            for text, batch in build_messages(query, fresh):
                send_message(text)
                sent_messages += 1
                # Mark seen only now, so a failed send is retried next run.
                for job in batch:
                    seen[job["id"]] = now
                sent_jobs += len(batch)
    finally:
        # Persist even on failure, so a crash mid-run does not resend everything.
        SEEN_FILE.write_text(json.dumps(seen, indent=0))
        print(f"Found {found} postings, sent {sent_jobs} new jobs "
              f"in {sent_messages} messages")


if __name__ == "__main__":
    main()
