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

**Queue.** `jobmarket collect` queues new content (it never calls the model);
`jobmarket extract run` processes the queue in priority order: new or edited retained
listings, retained backlog, new or edited other listings, other backlog. Requests are sent
one at a time, spaced to `JOBMARKET_AI_RPM` (and `JOBMARKET_AI_TPM` if set). A failed
request is retried three times after 10, 30, and 90 seconds, or longer if Google says so;
a job that still fails is retried in the next run, five runs at most, then waits for
`extract reprocess`. A daily-quota error pauses extraction until midnight Pacific time.
Runs stop at 90% of `JOBMARKET_AI_DAILY_LIMIT`. An interrupted run is recovered by the next
one. Collection is unaffected by any of this.

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
`extraction_pauses`. Views: `current_extractions`, `extraction_usage_daily`,
`screening_disagreements`.

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
