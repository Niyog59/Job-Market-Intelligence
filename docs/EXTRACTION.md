# Requirement extraction

How listings are sent to the language model, how the answers are checked, and how the queue and free-tier limits work. Back to the [README](../README.md).

## Requirement extraction

A language model (Gemini, through the official `google-genai` SDK) reads each listing and
returns its requirements as JSON. Python decides what is sent, checks every answer, tidies
names, stores everything with its versions, and controls retries and quota. Availability,
duplicates, and retention stay rule-based.

**What is extracted** (one request per listing version, all fields together): role
categories (several may apply; titles are kept as published), seniority, employment type;
skills as required, preferred, mentioned, or ambiguous, with AND/OR/"at least N"
relationships kept as such ("Python or Java" keeps both names and its wording); overall and
skill-specific experience with ranges, education, and education/experience alternatives;
explicit graduation windows; sponsorship (supported, not supported, not stated, ambiguous),
kept apart from work authorization, citizenship, and clearance; and locations (original
text plus city, state, country), work arrangement, explicit remote restrictions, and
on-site frequency. "Not stated" is never turned into "no", "remote" never means
worldwide, and no location or coordinate is invented.

**Early career.** Each answer also says whether the job is open to someone with no or very
little experience: `yes` for new-grad, recent-graduate, rotational, and early-career
programs, entry-level roles, "no experience needed", or at most one year of experience;
`internship`; `no`; or `unclear`, with the reasons and quotes. Python warns when `yes`
comes with a required experience of more than one year. `jobmarket jobs` lists only the
`yes` jobs by default (`--internships` for internships, `--all-levels` for everything);
every job is still collected, screened, and kept, and jobs not yet extracted are counted
rather than hidden silently.

**Skills** are named technologies only (languages, frameworks, tools, platforms, databases,
cloud services, products), not methods, concepts, soft skills, or certifications.

**Input and safety.** The model receives one document per version: title, location fields,
Lever workplace and commitment fields, and the description as display-safe text (headings
and lists kept). The posting is wrapped in `<posting>` tags and the instructions say its
contents are data, never instructions. Descriptions over 20,000 characters and empty ones
are skipped (`too_long`, `no_description`) and stay listed.

**Checks.** The API enforces the JSON schema; Python checks it again (types, allowed
values, required fields, bounds). A malformed answer is retried and never stored. Each
quote is then searched for in the exact document sent (case, spacing, and typographic
differences ignored), and its character offsets are stored. A quote that is not found, a
claim without a quote, a relationship naming an unlisted skill, or an inconsistent year
range is a warning: the answer is kept and marked `valid_with_warnings`. These checks show
an answer is well formed and quoted from the posting, not that it is correct.

**Clean-up.** Skill names are merged only through an exact alias table (`Postgres` becomes
`PostgreSQL`; `Java` and `JavaScript` stay apart; unknown names are kept as written).
States become two-letter codes, common country names one spelling, and Texas cities are
placed in the DFW or Houston area with the screening city lists.

**Queue.** `jobmarket collect` queues new content after every board (it never calls the
model). The extraction worker, or the hourly batch when no worker runs, then works through
the queue in two lanes that share the account's one daily allowance:

| Lane | Priority order | Share of the day's requests |
|---|---|---|
| New | 1 new or edited kept listing, 3 new or edited other listing | always first; at least `JOBMARKET_AI_NEW_RESERVE` (200) of the requests are kept for it |
| Backlog | 2 kept backlog, 4 other backlog (and reprocessing) | only requests the new lane does not need, at most the safety stop minus the reserve (250 with a 500 daily limit) |

A board added later brings everything it already lists (its initial import). Those listings
are not new postings: kept ones still get priority 1, so a new employer's relevant jobs are
read within the day, and the rest get priority 4. Without this, the first batch of added
boards (1,110 listings, 39 kept) would have used the new lane for about two and a half days.

"Kept" means the screening rules kept the listing: its role is in a target family (data,
analytics, BI, software, data science, ML) and it is in DFW or Houston or remote and open
to Texas. It does not mean the listing matches the owner's profile, level, or sponsorship
needs; those are compared on the website. Backlog is content recorded before extraction
tracking started.

Requests are sent one at a time, spaced to `JOBMARKET_AI_RPM` (and `JOBMARKET_AI_TPM` if
set), and stop at 90% of `JOBMARKET_AI_DAILY_LIMIT`. Every request is recorded before it
is sent, under one lock, so these limits hold across the worker, the hourly batch, manual
runs, and evaluations together. A job gets three tries. A try is used by a problem with
that listing: an invalid or blocked answer, or a rejected request (another 4xx, such as a
bad key, which also stops the run). The job's next try comes after 10, 30, then 90 seconds,
while other jobs go ahead. When two tries fail for the same reason, the job records the
error (`repeated_error`, listed by `extract status` and counted in the daily summary) and
gets a fourth try, never more; after its last try it waits for `extract reprocess`.
Provider failures (rate limits, server and network errors) never use a try, because every
other job would meet them too: they are retried in place after the same waits, or longer if
Google says so, and after four in a row the job goes back in line and extraction pauses for
15 minutes, for every process, so an outage does not spend the day's requests one listing
after another. A daily-quota error pauses extraction until midnight
Pacific time. A job is claimed with a lock that PostgreSQL releases if its process dies, so
a crash leaves nothing stuck: the job is picked up again and its unanswered request is
marked `interrupted` (it still counts toward the day). Collection is unaffected by any of
this.

### Extraction worker

`jobmarket extract worker` (the Compose service `extract-worker`, profile `worker`) runs the
queue continuously instead of once an hour. It claims the next ready job as soon as it is
free; when nothing is ready it queues anything still missing a job and checks again every
15 seconds, sleeping in between. It sleeps through an outage pause (15 minutes), and
through a quota pause or the safety stop until the Pacific reset, waits an hour after a rejected request, and keeps a heartbeat row in
`extraction_workers` with its state and the reason for a pause. Only one worker runs at a
time. While it runs, the hourly batch (`extract run --skip-if-worker`) skips itself; when it
stops, the hourly batch takes over again, which is also the rollback (RUNBOOK section W).
On SIGTERM it lets a request already sent finish and be recorded, then exits.

One worker is enough: at 15 requests a minute the pacing allows one request every 4
seconds, and a request took 3.6 seconds at the median (7.0 at the 95th percentile) over
472 requests recorded on the Mac copy of the database (2026-10-27 to 2026-11-03), so
concurrent requests would only reach the daily limit sooner. The
daily limit, not the worker, sets how many listings are extracted per day: about 450
requests, of which the backlog lane may use 250.

**Times to keep apart.** For a new listing:

1. The employer posts or edits it (the provider's own timestamps, when it gives them).
2. A collection sees it: 8 AM, noon, or 9 PM Chicago time, or a retry of a failed board.
3. Its extraction finishes: `extraction_results.created_at`. With the worker and quota
   left, a few seconds per listing ahead of it in the new lane (the target is within five
   minutes of queueing); without the worker, the next :30 batch of 20.
4. Search shows it at once (the serving views are live); the Reports page, the Market
   insights numbers, and the Google Sheet change at the next publication after a
   collection; Tableau Public re-reads the Sheet about once a day. Reports are not rebuilt
   after each result.

`extract status` shows the worker's state, open jobs by priority (ready and waiting for a
retry) with the oldest one's age, results in the last hour, results and reuses per lane in
the last 24 hours with the median and 95th percentile time from queueing and from
collection to a validated result, and today's requests by outcome.

**Caching and versions.** Results are keyed by the posting version and the configuration
(model, thinking level, prompt version, schema version). Unchanged content is never sent
again; identical text in another version reuses the stored answer without a request; a
result is never attached to newer content (the `current_extractions` view joins on the
current version only), and older results are kept. A new model, prompt, or schema applies
to new content only: `extract reprocess` queues older content explicitly. Python clean-up
changes are applied with `extract renormalize`, without model calls.

**Free tier only.** Nothing enables billing. The code cannot tell whether a Google project
is billed, so the free tier is confirmed in Google AI Studio; limits are configured from
the same page. `extract status` reports requests and tokens per Pacific day and what they
would cost at paid rates, for planning only. Free-tier requests may be used by Google to
improve its products; job descriptions are public listings.

Tables: `extraction_inputs` (the exact text sent), `extraction_jobs` (queue),
`extraction_runs`, `extraction_attempts` (every request, its outcome and token usage),
`extraction_results` (answer, normalized form, warnings, evidence offsets, versions),
`extraction_pauses`, `extraction_workers` (worker heartbeats). Views: `current_extractions`,
`extraction_usage_daily`, `screening_disagreements`.

### Evaluation

Extraction quality is measured against labelled cases, not assumed. A case pins one
posting version; its file (under `data/eval/cases/`, outside Git because it contains copied
posting text) holds the exact document the model receives and a TOML block of labels for a
chosen set of fields: role categories, seniority, employment type, required and preferred
skills, skill relationships, overall minimum years, degree-or-experience alternatives,
sponsorship, work authorization, citizenship, clearance, graduation, work arrangement,
locations, and remote restrictions.

Each case says who stands behind its labels: `proposed` (drafted without seeing a model
answer, not checked by a person) or `human_reviewed`. Reports keep the two apart; only
human-reviewed cases are reported as accuracy. Cases are split into `practice`, used while
adjusting the instructions, and `holdout`, kept unseen until a final run (`--final`).

```bash
jobmarket eval prepare --selection selection.json   # write case files (existing ones kept)
jobmarket eval run --split practice                 # extract through the normal queue
jobmarket eval report --split practice --cases      # per-field agreement and every mismatch
jobmarket eval run --split holdout --final          # once, after tuning
```

Reports count, per field, the cases that match, and for lists the matched, extra, and
missed items (skills are compared after alias clean-up), plus quotes not found and average
tokens per listing. Matching labels shows agreement with those labels on those fields; it
is not a general accuracy claim.
