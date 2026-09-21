# job_scraper_bot

Checks LinkedIn hourly for new jobs and sends them to Telegram, grouped into one message per
search. Supports several people, each with their own searches and their own chat, through one bot.

## Setup

1. Telegram: create a bot with @BotFather → copy the token.
2. Repo → Settings → Secrets and variables → Actions → add `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`.
3. Actions tab → "Job scraper" → Run workflow, to test.

## Config

`config.json` holds one entry per person.

```json
{
  "recipients": [
    { "name": "Ervin", "chat_id_env": "TELEGRAM_CHAT_ID",   "queries": [ ... ] },
    { "name": "Nora",  "chat_id_env": "TELEGRAM_CHAT_ID_2", "queries": [ ... ] }
  ]
}
```

`chat_id_env` is the **name of a repo secret**, never a chat ID. Chat IDs live in Actions Secrets
so nothing personal lands in this public repo. The workflow forwards five slots
(`TELEGRAM_CHAT_ID`, `TELEGRAM_CHAT_ID_2` … `_5`), so adding a person needs a new secret but no
workflow edit. A recipient whose secret is unset is skipped with a log line rather than failing
the run.

Each query takes:

| key | required | meaning |
| --- | --- | --- |
| `keywords` | yes | search terms, e.g. `flutter developer` |
| `location` | no | geographic filter, e.g. `Budapest`, `Spain`, `Europe` |
| `exclude` | no | skip postings whose **title** contains any of these words |
| `min_score` | no | title relevance floor 0–1, default `0.66` (see below) |
| `min_desc_mentions` | no | times a distinctive term must appear in the body, default `2` |

### Relevance filtering

LinkedIn's keyword matching is loose — it expands semantically across the whole posting, not just
the title. Measured on a real run, `flutter developer` in Hungary returned **18 results, none of
which contained "flutter"**: Angular, Vue.js, React, and a "Fluent OMS Developer" that matched on
*Flu*. So titles are scored after scraping and the noise is dropped.

#### Stage 1: the title

`relevance(title, keywords)` is the weighted share of the query's terms present in the title:

- Terms are lowercased and split on punctuation, so `Flutter/React Developer` matches.
- Synonyms collapse to one form: developer = engineer = dev = programmer, plus the local-language
  titles LinkedIn returns (`fejlesztő`, `desarrollador`, `ontwikkelaar`, `développeur`, …).
- Generic words that appear in nearly every posting (`senior`, `mobile`, `software`, `app`,
  `remote`, `full stack`, …) carry a quarter weight. Distinctive terms like `flutter` carry full
  weight, which is what makes `Flutter Developer` rank far above `Mobile Developer` for a Flutter
  search.

Examples against `flutter developer`:

| title | score | at 0.66 |
| --- | --- | --- |
| `Flutter Software Engineer` | 1.00 | keep |
| `Desarrollador/a Flutter (freelance)` | 1.00 | keep |
| `Senior Delphi Developer` | 0.20 | drop |
| `Fluent OMS Developer` | 0.20 | drop |
| `Szoftverfejlesztő (Angular)` | 0.00 | drop |

Measured on 176 real postings, `0.66` kept 43 with **zero** false positives or negatives on the
Flutter queries — every kept title contained "flutter", every dropped one did not. Raise
`min_score` per query to tighten, lower it to loosen. Filtered jobs are deliberately *not* recorded
as seen, so lowering the threshold later lets them through on the next run.

#### Stage 2: the posting body

Titles alone lose real matches. A posting titled *Senior Mobile Engineer* whose body says "Design
and build sophisticated apps using Flutter … 5 years Mobile Development Experience Flutter" is a
Flutter job, but scores 0.20 on its title.

So postings the title rejects get a second look: the body is fetched from
`jobs-guest/jobs/api/jobPosting/<id>` (~0.75s each) and kept if **every distinctive query term
appears at least `min_desc_mentions` times**, default 2.

Coverage scoring is deliberately *not* used on the body. Almost any 3000-character description
contains "developer", "senior" and "software" somewhere, so coverage would pass nearly everything.
Only distinctive terms carry signal there — a query like `senior mobile developer` has none, so it
skips this stage entirely and the title decides.

The mention count is what separates a real match from a passing reference. Measured:

| body says | mentions | verdict |
| --- | --- | --- |
| "build sophisticated apps using Flutter … 5 years experience Flutter" | 4 | keep |
| "frameworks such as React Native or Flutter" | 1 | drop |
| "you are experienced with Vue.js … you're open to Flutter" | 1 | drop |

Cost is bounded by `DESC_BUDGET_PER_QUERY` (15) and `DESC_BUDGET` (120 per run), spent
best-first so a hopeless query cannot starve the rest. Mostly paid on the first catch-up run: a
body that was fetched and rejected is marked seen, so later runs only pay for genuinely new
postings. A body that could not be fetched (429) is **not** marked seen and is retried later.

Measured over 196 postings, stage 2 recovered 12 real matches that the title filter had dropped,
including `Mobile Developer`, `Desenvolvedor de aplicativos móveis` and two helpdesk-manager roles.
Recovered jobs are labelled "matched in description" in the Telegram message.

### How `location` is applied

`GEO_IDS` in `scraper.py` maps location names to LinkedIn's own location ids:

| location | geoId |
| --- | --- |
| Spain | 105646813 |
| Europe / EU / EMEA | 91000002 |
| Netherlands | 102890719 |
| Germany | 101282230 |
| Hungary | 100288700 |

`geoId` **overrides** the `location` string when both are sent — verified: `location=Spain`
with a Hungarian geoId returns Hungarian jobs. So the two cases are handled differently:

- **Mapped location** → send `geoId` and append `in <location>` to the keywords. The geoId pins
  the geography, so the extra keyword only affects relevance. Measured roughly neutral: Hungary
  28 → 35 results, Netherlands 133 → 131, Europe 150 → 144, and **no** foreign results in any case.
- **Unmapped location** → send the `location` param and leave the keywords alone. Without a geoId
  there is no hard geographic filter, and a location folded into the keywords becomes plain free
  text matched against the posting body: `senior mobile developer in Europe` with no geoId returned
  9 of 10 results from the United States. The run logs a note when a location has no geoId.

Add entries to `GEO_IDS` to pin more locations; grab the id from the `geoId=` parameter in a
LinkedIn job-search URL. Keep the list in `docs/index.html` (`GEO_LOCATIONS`) in step, since the
editor uses it to flag unmapped locations.

Seen-job history is tracked per person, keyed by the secret name, so the same posting reaches
everyone who searched for it. Removing a person drops their history; re-adding them later starts
fresh, which means one catch-up burst.

### Adding a person

1. They message the bot on Telegram **first**. A bot cannot open a conversation, so sends fail
   until they do.
2. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy their `chat.id`.
3. Settings → Secrets and variables → Actions → New repository secret named
   `TELEGRAM_CHAT_ID_2` (or the next free slot), value = that chat ID.
4. In the web editor: **+ Add person**, set the name, pick that secret from the dropdown, add
   their queries, **Commit to GitHub**.

### Web editor (GitHub Pages)

`docs/index.html` edits `config.json` from the browser. It builds the JSON from a form, so a
stray comma cannot silently break the file.

1. Repo → Settings → Pages → Source: *Deploy from a branch*, branch `main`.
2. Open `https://<owner>.github.io/<repo>/docs/` (drop the `/docs/` if Pages serves that folder).
3. Create a [fine-grained PAT](https://github.com/settings/personal-access-tokens/new) scoped to
   **only this repository** with **Contents: read and write**. Read-only loads but cannot commit.
4. Edit, then *Commit to GitHub*. The next scheduled run picks it up.

The token lives in your browser's localStorage and is sent only to `api.github.com`; it is never
written into the repo. *Forget token* clears it.

## Run locally

    pip install -r requirements.txt
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python scraper.py

## How it works

- `TIME_WINDOW` bounds how far back each run looks. GitHub runs scheduled workflows on a
  best-effort basis and skips many of them — gaps of 5+ hours have been observed against an
  hourly cron — so the window deliberately overlaps. It cannot fully cover a long gap.
- The guest search endpoint returns 10 results per request regardless of what you ask for;
  `PAGE_SIZE` reflects that and `MAX_PAGES` bounds the depth. When a query fills every page the
  log says so, meaning more results existed than were fetched.
- 429s are expected when paginating deep and are retried with backoff rather than silently
  truncating a query.
- Queries shared between people are scraped once per run and reused.
- `seen_jobs.json` is committed back by the workflow, with rebase-and-retry so a commit from the
  web editor mid-run doesn't lose it. IDs older than `SEEN_TTL_DAYS` are pruned.
- Jobs are marked seen only after the message carrying them is delivered, so a failed send is
  retried next run rather than silently dropped.
