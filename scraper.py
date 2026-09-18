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
CONFIG_FILE = ROOT / "config.json"
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


def fetch_jobs(query, cache=None):
    """Scrape one query. `cache` memoises across recipients sharing a query."""
    key = (query["keywords"], query.get("location", ""))
    if cache is not None and key in cache:
        return cache[key]
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
    if cache is not None:
        cache[key] = jobs
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


def send_message(text, chat_id, attempts=3):
    """Post one message to one chat, honouring Telegram's rate-limit backoff."""
    for attempt in range(1, attempts + 1):
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
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


def load_config():
    """Read config.json into a list of recipients.

    Also accepts the old flat list of queries, which becomes a single recipient
    reading TELEGRAM_CHAT_ID.
    """
    raw = load_json(CONFIG_FILE, None)
    if raw is None:
        sys.exit(f"{CONFIG_FILE.name} not found")
    if isinstance(raw, list):  # legacy queries.json shape
        raw = {"recipients": [{"name": "default",
                               "chat_id_env": "TELEGRAM_CHAT_ID",
                               "queries": raw}]}
    if not isinstance(raw, dict) or not isinstance(raw.get("recipients"), list):
        sys.exit(f"{CONFIG_FILE.name} must be an object with a 'recipients' array")

    recipients = []
    for i, r in enumerate(raw["recipients"], 1):
        if not isinstance(r, dict):
            sys.exit(f"recipient {i} in {CONFIG_FILE.name} is not an object")
        env = (r.get("chat_id_env") or "").strip()
        queries = r.get("queries") or []
        name = (r.get("name") or env or f"recipient {i}").strip()
        if not env:
            sys.exit(f"recipient {name!r} is missing chat_id_env")
        if not isinstance(queries, list):
            sys.exit(f"recipient {name!r}: queries must be an array")
        clean = [q for q in queries if isinstance(q, dict) and (q.get("keywords") or "").strip()]
        recipients.append({"name": name, "chat_id_env": env, "queries": clean})
    if not recipients:
        sys.exit(f"no recipients configured in {CONFIG_FILE.name}")
    return recipients


def load_seen(recipients, now):
    """Seen ids bucketed per recipient, so one person's alerts never mask another's.

    Buckets are keyed by chat_id_env (the secret's *name*), never the chat id
    itself, so this file stays safe to commit to a public repo.
    """
    raw = load_json(SEEN_FILE, {})
    cutoff = SEEN_TTL_DAYS * 86400
    # Old format was a flat {job_id: timestamp}; adopt it for the first recipient.
    if raw and all(isinstance(v, (int, float)) for v in raw.values()):
        raw = {recipients[0]["chat_id_env"]: raw}
    seen = {}
    for r in recipients:
        bucket = raw.get(r["chat_id_env"], {})
        if not isinstance(bucket, dict):
            bucket = {}
        seen[r["chat_id_env"]] = {
            k: v for k, v in bucket.items()
            if isinstance(v, (int, float)) and now - v < cutoff
        }
    return seen


def main():
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN must be set")

    recipients = load_config()
    now = time.time()
    seen = load_seen(recipients, now)
    cache = {}
    totals = {"found": 0, "jobs": 0, "messages": 0}
    delivered = 0

    try:
        for r in recipients:
            chat_id = (os.environ.get(r["chat_id_env"]) or "").strip()
            if not chat_id:
                print(f"[{r['name']}] skipped: {r['chat_id_env']} is not set as a secret")
                continue
            if not r["queries"]:
                print(f"[{r['name']}] skipped: no queries configured")
                continue
            delivered += 1
            bucket = seen[r["chat_id_env"]]
            print(f"[{r['name']}] {len(r['queries'])} queries")
            for query in r["queries"]:
                jobs = fetch_jobs(query, cache)
                totals["found"] += len(jobs)
                fresh = new_jobs_for(query, jobs, bucket, now)
                print(f"  {query['keywords']!r} @ {query.get('location', '')!r}: "
                      f"{len(jobs)} found, {len(fresh)} new")
                if not fresh:
                    continue
                for text, batch in build_messages(query, fresh):
                    send_message(text, chat_id)
                    totals["messages"] += 1
                    # Mark seen only now, so a failed send is retried next run.
                    for job in batch:
                        bucket[job["id"]] = now
                    totals["jobs"] += len(batch)
    finally:
        # Persist even on failure, so a crash mid-run does not resend everything.
        SEEN_FILE.write_text(json.dumps(seen, indent=0, sort_keys=True))
        print(f"Found {totals['found']} postings, sent {totals['jobs']} new jobs "
              f"in {totals['messages']} messages to {delivered} recipient(s)")

    if not delivered:
        sys.exit("no recipient could be resolved; check the chat id secrets")


if __name__ == "__main__":
    main()
