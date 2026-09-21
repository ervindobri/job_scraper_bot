import html
import json
import os
import re
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
JOB_DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
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
MIN_SCORE = 0.66  # default relevance floor; per-query "min_score" overrides it
DESC_LOOKUP = True  # second-stage check of the posting body for title-rejected jobs
MIN_DESC_MENTIONS = 2  # times a distinctive term must appear; 1 is usually "nice to have"
DESC_BUDGET_PER_QUERY = 15  # so one hopeless query cannot starve the rest
DESC_BUDGET = 120  # overall ceiling per run, ~0.75s each. Mostly hit on the first
                   # catch-up run: a body that was fetched and rejected is marked
                   # seen, so later runs only pay for genuinely new postings.

# LinkedIn matches keywords loosely against the whole posting, so a search for
# "flutter developer" returns Angular, Vue and even "Fluent OMS Developer". Titles
# are scored against the query afterwards and the noise is dropped.
SYNONYMS = {
    "developer": "dev", "engineer": "dev", "dev": "dev", "programmer": "dev",
    "development": "dev", "engineering": "dev",
    # the local-language titles LinkedIn returns for European searches
    "fejlesztő": "dev", "szoftverfejlesztő": "dev", "desarrollador": "dev",
    "desarrolladora": "dev", "ontwikkelaar": "dev", "entwickler": "dev",
    "développeur": "dev", "sviluppatore": "dev", "utvecklare": "dev",
    "sr": "senior", "snr": "senior", "jr": "junior",
}
# Words shared by almost every software posting, so matching one proves little.
# Distinctive terms (flutter, kotlin, swift, ...) keep full weight, which is what
# makes "Flutter Developer" rank above "Mobile Developer" for a Flutter search.
GENERIC_TOKENS = {
    "dev", "software", "senior", "junior", "mid", "medior", "lead", "staff",
    "principal", "mobile", "remote", "hybrid", "onsite", "app", "application",
    "applications", "fullstack", "full", "stack", "frontend", "backend", "front",
    "back", "end", "web", "cloud", "system", "systems", "tech", "technology",
    "it", "specialist", "consultant", "expert", "professional",
}
GENERIC_WEIGHT = 0.25

# LinkedIn's own location ids. geoId OVERRIDES the location string when both are
# sent, so an unmapped location must send no geoId at all or it would silently
# search the wrong place. Keys are compared lowercased.
GEO_IDS = {
    "spain": 105646813,
    "europe": 91000002,
    "eu": 91000002,
    "emea": 91000002,
    "europe, eu": 91000002,
    "netherlands": 102890719,
    "germany": 101282230,
    "hungary": 100288700,
}

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


def geo_id_for(location):
    """LinkedIn geoId for a location name, or None to fall back to the text filter."""
    return GEO_IDS.get((location or "").strip().lower())


def search_params(query):
    """Keywords + location half of a request, shared by the API call and the public URL.

    geoId overrides the location string, so when we have one it does the geographic
    filtering and "in <location>" is appended to the keywords instead: measured to
    return MORE results in the same country (Hungary 28 -> 35) with no leakage,
    because geoId still pins the geography.

    Without a geoId there is no hard filter, so the location must stay in its own
    param. Folding it into the keywords then makes it plain free text matched against
    the posting body, which returns jobs on other continents that merely mention the
    place.
    """
    keywords = query["keywords"].strip()
    location = query.get("location", "").strip()
    geo_id = geo_id_for(location)
    if not location:
        return {"keywords": keywords}
    if geo_id is None:
        return {"keywords": keywords, "location": location}
    return {"keywords": f"{keywords} in {location}", "geoId": geo_id}


def fetch_jobs(query, cache=None):
    """Scrape one query. `cache` memoises across recipients sharing a query."""
    key = (query["keywords"], query.get("location", ""))
    if cache is not None and key in cache:
        return cache[key]
    jobs = []
    ids = set()
    location = query.get("location", "")
    label = f"{query['keywords']!r} @ {location!r}"
    if location and geo_id_for(location) is None:
        print(f"  note: no geoId mapped for {location!r}; using the text filter, "
              f"which is looser. Add it to GEO_IDS to pin it down.")
    for page in range(MAX_PAGES):
        params = {
            "f_TPR": TIME_WINDOW,
            "start": page * PAGE_SIZE,
            "origin": "JOB_SEARCH_PAGE_LOCATION_HISTORY",
            **search_params(query),
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


def tokenize(text):
    return [t for t in re.split(r"[^0-9a-zà-öø-ÿ]+", (text or "").lower()) if t]


def canonical(token):
    return SYNONYMS.get(token, token)


def relevance(title, keywords):
    """Weighted share of the query's terms present in the title, 0..1.

    Generic role words count for little, so a Flutter search keeps "Flutter
    Software Engineer" (1.0) and drops "Senior Mobile Developer" (0.2).
    """
    want = [canonical(t) for t in tokenize(keywords)]
    if not want:
        return 1.0
    have = {canonical(t) for t in tokenize(title)}
    total = matched = 0.0
    for token in dict.fromkeys(want):  # unique, order preserved
        weight = GENERIC_WEIGHT if token in GENERIC_TOKENS else 1.0
        total += weight
        if token in have:
            matched += weight
    return matched / total if total else 1.0


def distinctive_terms(keywords):
    """Query terms specific enough to judge a posting body by.

    Scoring a 3000-character description by term coverage is useless -- almost any
    description contains "developer", "senior" and "software" somewhere. Only the
    distinctive terms carry signal there.
    """
    seen, out = set(), []
    for token in (canonical(t) for t in tokenize(keywords)):
        if token not in GENERIC_TOKENS and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def fetch_description(job_id, cache):
    """Posting body as lowercase text, cached per run.

    Returns None when the body could not be retrieved, which the caller must treat
    differently from an empty body: a rate-limited posting has not been judged, so
    it must stay unseen and be retried on a later run.
    """
    if job_id in cache:
        return cache[job_id]
    text = None
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            r = requests.get(JOB_DETAIL_URL.format(job_id), headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"    description fetch failed for {job_id}: {e}")
            break
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            el = (soup.select_one(".show-more-less-html__markup")
                  or soup.select_one(".description__text"))
            text = el.get_text(" ", strip=True).lower() if el else ""
            break
        if r.status_code in (429, 500, 502, 503, 504) and attempt < PAGE_RETRIES:
            wait = int(r.headers.get("Retry-After") or 0) or attempt * 2
            print(f"    description HTTP {r.status_code} for {job_id}, retrying in {wait}s")
            time.sleep(wait)
            continue
        print(f"    description HTTP {r.status_code} for {job_id}, giving up")
        break
    if text is not None:
        cache[job_id] = text   # only cache a real answer
    return text


def description_matches(description, keywords, min_mentions):
    """True when every distinctive query term appears often enough to be the real topic.

    One mention is usually "nice to have": a Vue.js posting saying "you are open to
    Flutter" is not a Flutter job, while one saying it four times is.
    """
    terms = distinctive_terms(keywords)
    if not terms or not description:
        return False  # nothing distinctive to look for; the title decides
    return all(
        len(re.findall(r"\b" + re.escape(t) + r"\w*", description)) >= min_mentions
        for t in terms
    )


def matches_excludes(job, query):
    title = job["title"].lower()
    return any(word.lower() in title for word in query.get("exclude", []))


def format_job(job):
    """One job as a compact, clickable block."""
    title = html.escape(job["title"]) or "(untitled)"
    line = f'• <a href="{html.escape(job["url"], quote=True)}">{title}</a>'
    bits = [job["company"], job["location"]]
    if job.get("via") == "description":
        bits.append("matched in description")
    meta = " · ".join(p for p in bits if p)
    if meta:
        line += f"\n  {html.escape(meta)}"
    return line


def search_page_url(query):
    """The LinkedIn search page for this query, so the message links to all results."""
    params = {"f_TPR": TIME_WINDOW, **search_params(query)}
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


def new_jobs_for(query, jobs, seen, now, desc_ctx=None):
    """Unseen, non-excluded, relevant postings for one query.

    Stage 1 scores the title. Stage 2 fetches the body for the ones the title
    rejected, best-first and within a budget, because a posting titled "Senior
    Mobile Engineer" whose body names Flutter four times is a real match.
    """
    try:
        min_score = float(query.get("min_score", MIN_SCORE))
    except (TypeError, ValueError):
        min_score = MIN_SCORE
    try:
        min_mentions = int(query.get("min_desc_mentions", MIN_DESC_MENTIONS))
    except (TypeError, ValueError):
        min_mentions = MIN_DESC_MENTIONS

    fresh, batch_ids, maybe = [], set(), []
    for job in jobs:
        if job["id"] in seen or job["id"] in batch_ids:
            continue
        batch_ids.add(job["id"])
        if matches_excludes(job, query):
            seen[job["id"]] = now  # remember exclusions so we stop re-checking them
            continue
        score = relevance(job["title"], query["keywords"])
        if score >= min_score:
            job["score"] = round(score, 2)
            fresh.append(job)
        else:
            maybe.append((score, job))

    checked = recovered = unavailable = 0
    if maybe and DESC_LOOKUP and desc_ctx is not None and distinctive_terms(query["keywords"]):
        # Best-first, so a tight budget is spent on the most promising titles.
        for score, job in sorted(maybe, key=lambda pair: -pair[0]):
            if desc_ctx["budget"] <= 0 or checked >= DESC_BUDGET_PER_QUERY:
                break
            desc_ctx["budget"] -= 1
            checked += 1
            description = fetch_description(job["id"], desc_ctx["cache"])
            if description is None:
                # Never actually judged, so leave it unseen for a later run.
                unavailable += 1
            elif description_matches(description, query["keywords"], min_mentions):
                job["score"] = round(score, 2)
                job["via"] = "description"
                fresh.append(job)
                recovered += 1
            else:
                # Fetched and rejected on the full text, so do not pay for it again.
                seen[job["id"]] = now
            time.sleep(0.3)

    if maybe:
        parts = [f"dropped {len(maybe) - recovered} below relevance {min_score:g}"]
        if checked:
            parts.append(f"checked {checked} description(s), recovered {recovered}")
        if unavailable:
            parts.append(f"{unavailable} body unavailable, will retry next run")
        unchecked = len(maybe) - checked
        if unchecked and desc_ctx is not None and desc_ctx["budget"] <= 0:
            parts.append(f"{unchecked} unchecked, description budget spent")
        print("    " + "; ".join(parts))
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
    desc_ctx = {"cache": {}, "budget": DESC_BUDGET}
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
                fresh = new_jobs_for(query, jobs, bucket, now, desc_ctx)
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
