# Job search and the website

Search rules, the public pages, the private workspace, and how listings are made safe to display. Back to the [README](../README.md).

## Job search

`jobmarket.search` answers job searches from the serving views (see [Serving views](ANALYTICS.md#serving-views-job-search)). It never
calls a source, the model, or dbt, and it reads availability as the availability rules
stored it; it never changes or re-derives it.

- **State in the URL.** Every search is a set of URL parameters (`q`, `view`,
  `population`, `level`, `role`, `employer`, `place`, `arrangement`, `experience`,
  `skill`, `skill_mode`, `sponsorship`, `employment`, `unknown`, `old`, `sort`, `page`), so
  result pages can be shared and restored. Values are checked against the allowed choices
  (unknown values are dropped and reported) and reach SQL only as bound parameters.
- **Defaults.** Retained jobs (`population=all` adds every collected listing), active
  listings, every career level (`level=early` or `level=internship` narrows it; the default
  was early career until 2026-11-07), unknown information
  included, newest discovery first, 25 results per page.
- **Text** matches the title and description (stemmed full-text search) or the employer
  name.
- **AND/OR.** Choices within one filter are alternatives (any of them); different filters
  must all hold. Skills can be switched to "all of these" (`skill_mode=all`). A skill
  matches when it is asked for as required or preferred, including as one of a set
  ("Python or Java"); passing mentions do not count.
- **Unknown information.** For each structured filter a listing is a match, unknown (no
  extraction yet, or the listing does not state it clearly), or a contradiction (it states
  something else). Unknown listings are included unless `unknown=exclude`, each with the
  reasons ("Sponsorship: not stated", "Role: structured details pending"); contradictions
  never are, whatever the setting.
- **Filters on extracted fields.** Early career uses the extraction's early-career answer.
  Experience bands use the smallest required overall minimum (0-1 means under 2 years,
  2-4 under 5, 5+ the rest). Location uses the extraction's place: Dallas-Fort Worth,
  Houston, remote open to Texas, remote with an unclear limit, or elsewhere.
- **Duplicates.** A strong or reviewed duplicate group is one result, shown by its most
  recently observed listing, and matches when any of its listings does. Its availability
  rolls up as in the analytics. Possible duplicates stay separate results.
- **Views.** Active results are observed and possibly unavailable listings; assumed
  inactive and closed listings are in the archived view. Counts and pages count
  opportunities, so a group never takes two places.
- **Dates.** Discovery (first seen by this pipeline, with initial imports labeled), the
  source's own date (Greenhouse publication, Lever creation; sorted last when missing),
  content updates, and last sighting are kept apart. An update never moves a job up the
  newest-discovered order.
- **Freshness.** A listing whose board has had no complete collection for more than 36
  hours is flagged as possibly out of date.
- **Older jobs.** Jobs dated more than 30 days ago are hidden unless `old=include`, and the
  page says how many were hidden. A job's date is the source's own date (Greenhouse
  publication, Lever creation), or its first discovery when the source gives none; a
  duplicate group uses its newest member's date. Nothing is deleted: job pages, reports,
  and the dashboards still use every job.

## Newest jobs

`/newest` (`jobmarket.search.newest`) is the list for applying quickly, public like the
search. It shows active jobs first discovered in the past 3 days, newest discovery first.

- **No waiting for the AI.** The serving views are live views, so a listing is on the page
  as soon as its board's collection transaction commits. The list filters only on what the
  collection decided itself: the rule-based screening of the title, duties, and location
  (`population`, `role`, `area`). Extraction status, AI match, and unknown requirements
  never hide or reorder a row. Rows without extracted details say "Requirements processing"
  (queued, running, or waiting for a retry) or "Details temporarily unavailable" (gave up
  after its retries). When extraction finishes, the same row gains its details; nothing is
  added or moved.
- **Each row** has the source's title, employer, location, pay when the source gives it,
  discovery time, the source's own date, when the employer's board was last checked
  completely, and an Apply link to the source's application page (or the listing page).
  The job page has the full description.
- **Scope.** Kept jobs by default, with a link to every new listing (`population=all`).
- **Initial imports** (listings already on a board when it was first collected completely)
  are hidden unless `imports=include`, so adding a board does not fill the list with jobs
  that were posted long before. The page says how many are hidden.
- **Older jobs** follow the same 30-day rule as the search (`old=include`).
- A job page opened from this list links back to it (`list=newest`).

## Employers

`/employers` lists every enabled board with its last complete check (every page read), its
latest attempt, and its active and kept jobs, so a reader can see how fresh each employer's
listings are. Boards are read at the collection times (see the [runbook](../deploy/RUNBOOK.md),
section G), not continuously.

## Job-search website

A server-rendered Flask site over the search above (`src/jobmarket/web/`): Jinja templates,
one stylesheet, and a small script for tabs, evidence, and scroll restoration. Every page
also works without the script. Flask was chosen because the project had no web stack and
the pages need no client-side framework.

**Run it.**

1. `pip install -e ".[web]"` (Flask).
2. The analytics login must exist and its password be in `.env`
   (`ANALYTICS_DB_PASSWORD`; see [Analytics](ANALYTICS.md#analytics-dbt), Setup).
3. `jobmarket analytics build` once, to create the serving views.
4. `jobmarket web`, then open http://127.0.0.1:8000/. The command checks the login and the
   views before starting and says what is missing.

The site shows whatever was last collected and extracted: run `jobmarket collect` and
`jobmarket extract run` to refresh it. On the server, Airflow runs collection on a
schedule and the extraction worker extracts new listings as they are queued, so search shows
a result on the next page load (see [Scheduling](OPERATIONS.md#scheduling)).

**Routes.**

| Route | Page |
|---|---|
| `/` | search results; all search state is in the query string (see [Job search](#job-search)) |
| `/jobs/<posting_id>` | one listing: overview, Requirements and Job description tabs, other postings of the same job, possible duplicates; `?from=<query string>` is the search to return to |
| `/reports` | the reporting tables, read-only; `?population=kept` (default) or `all` |
| `/insights` | Market insights: the Tableau Public dashboards; `?d=<dashboard>` picks one (see [Dashboard data](ANALYTICS.md#dashboard-data-tableau-public)) |
| `/newest` | Newest jobs (see [Newest jobs](#newest-jobs)) |
| `/employers` | every enabled board and its last complete check (see [Employers](#employers)) |
| `/api/profile-stats.json` | the public numbers file the last successful publication wrote (see [Analytics](ANALYTICS.md#public-numbers-file)); 404 before the first one |
| `/healthz` | `ok` when the app and its database login work (for container health checks) |

Invalid or unknown job links give a 404 page; missing serving views or an unreachable
database give a 503 page that says what to run.

**Pages.** Results list compact rows (title, employer, location, arrangement, employment
type, a salary range when the source supplies one), with discovery (initial imports
labeled), the source's own date, description updates, and last sighting shown apart.
Possibly unavailable, stale, and held-board listings carry a warning. The detail page shows
the availability as stored, the last sighting and the board's last complete check, an
"Apply on company site" link only while the listing is active (closed listings say so;
assumed-inactive ones link with a warning), and the extracted requirements as labeled rows:
required and preferred skills with their own relationships ("Python or Java: any one of
these is enough"), experience, education, graduation, career level, sponsorship, work
authorization, citizenship, clearance, location, and employment type. "View source
evidence" opens the quotes the answer was checked against; they come from the same
description version the Job description tab shows. Without an extraction, the page says the
structured details are pending or unavailable rather than "not stated". Back to results
restores the search, the page, and the scroll position. Below 820 px the layout becomes one
column.

**Reports page.** Shows the reporting tables as of the last build or
publication: extraction coverage (with the queue, retries, and skips when processing is
incomplete), current inventory, daily activity, skills in demand (required alone, required
as one of several choices, and preferred kept in separate columns), experience,
sponsorship and other eligibility statements with all three denominators, pipeline health,
and board freshness. It says when the reports were built and what collection and
extraction they include, and warns when job search already has a newer collection. The
current day is marked as provisional. Before the first build the page says the reports do
not exist yet. It reads `analytics_reporting` (`reporting_schema` in `WebConfig`) and
computes nothing beyond the dbt models.

**Security.** Pages run as the analytics login in read-only sessions. Descriptions are
rebuilt from an allowlist (`safe_description_html`), templates escape everything else, and
only `https` links are kept (opened with `rel="noopener noreferrer"`). Responses carry a
strict Content-Security-Policy (scripts, styles, and fonts from the site only; frames only
from `https://public.tableau.com`, for the dashboards), `nosniff`, a `same-origin` referrer
policy (other sites receive no referrer), and frame denial (no other site may frame these
pages). Roboto is served locally (SIL Open Font License, `web/static/fonts/OFL.txt`).

**On a server.** `jobmarket web` uses Flask's development server and is for this computer
only. A server runs `gunicorn jobmarket.web.wsgi:app` behind a reverse proxy that
terminates HTTPS (`deploy/`). Set `WEB_TRUST_PROXY=1` there, and only there: the app then
takes the scheme, host, and client address from the proxy's `X-Forwarded-*` headers, which
the sign-in origin check and the `Secure` cookie need.

**Limitations.** Salary only from Lever's
structured range (never parsed from text); requirements are AI extractions checked for
evidence but not for accuracy (the evaluation labels are not reviewed yet); listings first
seen before content was stored for every listing, and not seen since, have no page.

## Private workspace

Job browsing needs no account. One owner account, created on the command line, signs in to a
private workspace. There is no registration.

**Where private data lives.** In the database, in its own schema (`private`), never in Git:
migrations create only the structure, and the repository contains no private records. The
analytics login (and so dbt, the serving views, and every public page) has no access to that
schema; the website reaches it through a separate login, `jobmarket_private`, that can use
the private schema and nothing else.

**Setup (once).**

1. `pip install -e ".[web]"` (Flask and argon2-cffi).
2. `jobmarket migrate` (creates the private schema).
3. As a PostgreSQL superuser, create the login:
   `CREATE ROLE jobmarket_private LOGIN PASSWORD '...';` (or in pgAdmin: Login/Group Roles,
   Create, name `jobmarket_private`, a password, "Can login"). Put the password in `.env` as
   `PRIVATE_DB_PASSWORD`.
4. As the database owner: `jobmarket private grant`, then `jobmarket private check-access`
   (the private login uses the private schema only; the analytics login cannot reach it).
5. `jobmarket owner create --username NAME` asks twice for a password at a hidden prompt.
   No default account or password exists; nothing is printed.

**Changing the password.** Signed in, the Account page changes it after checking the current
password (wrong tries count toward the sign-in pause). Every other session ends; this browser
gets a new session.

**Recovery.** Someone with owner access to the database runs `jobmarket owner reset-password`:
it sets a new password, ends every session, and clears a sign-in pause.
`jobmarket owner status` shows whether the account exists, open sessions, and a pause;
`jobmarket owner sign-out-all` ends every session.

**Sign-in rules.**

- Passwords: at least 8 characters, hashed with Argon2id (argon2-cffi defaults, RFC 9106
  low-memory parameters); never stored, logged, or printed.
- Every 3 wrong passwords in a row pause sign-in for 15 minutes (counted for the whole site,
  whatever username is typed).
- Sessions live on the server. The browser cookie (`__Host-jm_session`: Secure, HttpOnly,
  SameSite=Strict, Path=/) holds only a random token; the database keeps its SHA-256. A new
  session starts at every sign-in. A session ends after 2 hours without activity, after 7
  days at the latest, or when signing out (which deletes it on the server and sends
  `Clear-Site-Data`).
- Every form post needs the session's CSRF token and, when the browser sends one, this site's
  `Origin`. The sign-in form carries its own random token.
- Private pages are checked on the server for every request: without a valid session they
  redirect to sign in, and their forms are refused.
- Pages for a signed-in owner are sent with `Cache-Control: no-store` and `Vary: Cookie`.

**Profile.** An editable form (`/profile`): skills (each with an optional note or https
link), employment, internships, and projects as separate experience types, education with
completed or expected graduation month, and preferred role families, locations, work
arrangements, and employment types. Optional switches highlight sponsorship statements or
other eligibility restrictions on job pages. Nothing is prefilled, imported from a resume, or
inferred; every save needs an explicit confirmation, invalid values are reported next to the
field with everything typed kept, and each save increases the profile version.

**Saved jobs** (`/saved`). Saving is idempotent. A confident duplicate group counts as one job:
saving another posting of the group reuses the saved entry. Saved jobs stay listed with their
current status when the listing closes; removing one keeps its note (saving again restores
it). Entries point at source postings, so groups that merge or split later change nothing
stored; two entries that end up in one group are both kept and labeled.

**Applications** (`/applications`, and "Your application" on a job page). Statuses: Not
applied, Applied, Interviewing, Offer, Rejected, Withdrawn, each with a date (not in the
future) and an optional note. Any status may follow any other. "Correct this entry" adds a
correction that names the entry it replaces; the original stays in the history, marked
corrected. Opening "Apply on company site" changes nothing, and a closed listing never
changes a status.

**New since last visit.** A visit starts at each sign-in, or after more than an hour without
activity. Its cutoff is the previous visit's last activity and stays fixed for the whole visit,
however much you browse or page. Jobs first discovered after the cutoff are marked "New since
your last visit" (opportunities that arrived with a newly added board are marked "New board:
initial import" instead), and the "Only jobs new since your last visit" filter (`new=1`) shows
just those. Discovery time is used, never description updates. The first visit has no cutoff:
it is a baseline, and nothing is marked new.

**Comparisons** ("Your comparison" tab on a job page, owner only). The listing's stored AI
answer for its current description version is checked against the latest saved profile; no
model call is made, nothing is stored, and there is no score. Each check is Matched, Gap, or
Needs review, with an explanation and the listing's quotes, and the page names the profile
version and description version used.

- A skill missing from the profile is "not listed in your profile", never a gap. "Python or
  Java" is met by either; "at least two of" counts listed skills; required and preferred
  skills are separate sections.
- Gaps are kept for explicit, comparable mismatches: listed employment below a stated
  minimum of years, a listed education level below a stated level, graduation months outside
  a stated window, and stated preferences ("outside your preferences"). Everything missing,
  ambiguous, or not comparable is Needs review.
- Required years count employment only, with overlapping months counted once. Internships do
  not count but turn a shortfall into Needs review; projects never count. Experience with a
  specific skill or domain needs review.
- An expected degree needs review against a required degree. Certificates, bootcamps, and
  "other" credentials are not ranked. Alternative paths ("a degree or equivalent experience")
  are met when any path is met and otherwise need review.
- Sponsorship, work authorization, citizenship, and clearance appear only as statements from
  the listing (highlighted if the profile asks for it). Nothing about the owner's eligibility
  is stored or inferred, and a listing that says nothing is neither a yes nor a no.
- Without a current extraction (pending, failed, or skipped), or before the profile is saved,
  there is no comparison.

**Backups.** `jobmarket backup create` writes the private schema to its own file
(`private.dump`), next to the ingestion data; see [Backups and restore](OPERATIONS.md#backups-and-restore). Backups contain
personal data: keep them out of Git and off shared drives.

## Listing data for display

`jobmarket.listings.listing_detail()` returns what a detail page needs for one listing: the
original title and locations, the source's posting page and separate application page,
availability state, first discovery, source publication and creation dates, when the
current content was first seen, the board's last trusted check, the description, and the
extraction status with a display-ready summary (`summary_lines()`) when there is one.

Descriptions are untrusted employer HTML and are never rendered as collected.
`safe_description_html()` rebuilds them from an allowlist: headings, paragraphs, line
breaks, lists, bold, italics, block quotes, and absolute `https` links (opened in a new tab
with `rel="nofollow noopener noreferrer"`). Every other attribute and link type is dropped;
scripts, styles, images, frames, and forms are removed with their content; text is escaped.
The text is what the source API supplied, which can differ from the employer's page, so
each description carries a note on where it came from. A description that was too long to
store is reported as `omitted_too_long`, and an empty one as `empty`, never as complete.
`jobmarket listing POSTING_ID` prints the same data in a terminal.
