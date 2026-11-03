# Dashboard data dictionary

The Tableau Public workbook reads the Google Sheet that `jobmarket tableau sync` writes after
every report publication. Each tab is a copy of one published reporting table
(`analytics_reporting`), replaced in full on every sync. Header row first; one row per record.

Dates are Sheets dates (Tableau reads them as dates). Times are in America/Chicago. Weeks run
Monday to Sunday and are named by their Monday (`week_start`). Booleans are TRUE/FALSE.
Multi-valued fields (such as `genai_terms`) are comma-separated text; where a chart needs
one row per value, a separate tab provides it.

## Shared definitions

| Term | Meaning |
|---|---|
| Job | One distinct opening. Postings of the same job on several boards, or posted twice, are merged (`job_key`). Counts of jobs use `COUNTD([job_key])`. |
| Kept | Passed the screening rules: a target role in DFW, Houston, or remote open to Texas. Every job tab holds kept jobs only (`daily_activity` and `eligibility` are filtered to their `retained` population by the sync); `pipeline_health` covers every board. |
| Current | Listed in the latest built day (observed, or possibly unavailable after a missed sighting). Closed and assumed-inactive jobs are excluded. |
| New job | First discovered in that week, excluding the initial import of a newly added board (those were already open, so they are not new hiring). |
| `role_group` | One group per job, by priority Data > AI/ML > Software > Other. Data: data analysis, BI, data and analytics engineering. AI/ML: ML and data science. Software: software engineering, backend, full stack. |
| `level_ai` | Junior (early career, entry, internship), Mid, Senior+ (senior, staff, lead, principal, manager, director, executive), or Unclear: from the AI's reading of title and description. |
| `level_years` | By the smallest required overall minimum years of experience: 0-2 years, 3-4 years, 5+ years, or Not stated. |
| Clear level | `level_ai` is Junior, Mid, or Senior+. Level shares use jobs with a clear level as the denominator. |
| `minimum_degree` | The lowest degree listed as required (the entry bar): High school, Associate, Bachelor, Master, Doctorate; otherwise Other credential, Degree preferred only, or Not stated. |
| `metro` | DFW, Houston, DFW and Houston, Remote (Texas), Remote (unclear), Elsewhere. |
| Generative-AI mention | The title or description matches one of the terms in `analytics/macros/genai_terms.sql` (version 1), after the employer's own name is removed. A bare "AI" never counts. |
| Not extracted | The AI has not processed the job's current text yet. Kept as its own value, never folded into Not stated or Unclear. |

## Tabs

### about (one row)

| Column | Meaning |
|---|---|
| `built_at` | When the reports were built. |
| `latest_complete_collection` | The newest complete board scan the build included. |
| `latest_extraction` | The newest AI extraction the build included. |
| `data_pull_caption` | Ready-made caption: "Data collected through ... · reports built ...". Shown under every chart. |
| `current_jobs` | Current kept jobs. |
| `employers` | Employers with at least one current kept job. |
| `jobs_with_ai_details` | Current kept jobs that have AI-extracted details. |

### jobs (rpt_jobs_current): one row per current kept job

| Column | Meaning |
|---|---|
| `job_key` | Job identifier (merged duplicates share it). |
| `posting_id` | The posting the job's details come from (the one seen most recently). |
| `employer_name`, `title` | As listed. |
| `metro`, `city` | Where the job is (see definitions). `city` is the first listed location. |
| `work_arrangement` | onsite, hybrid, remote, multiple, not_stated, ambiguous, or not_extracted. |
| `employment_type` | full_time, part_time, contract, contract_to_hire, temporary, internship, apprenticeship, not_stated, ambiguous, or not_extracted. |
| `role_group`, `role_categories` | The group, and the AI's role categories (comma-separated). |
| `level_ai`, `level_years`, `overall_min_years` | Level by both rules, and the required minimum years (number, empty when not stated). |
| `minimum_degree` | See definitions. |
| `early_career`, `seniority` | The AI's raw answers behind `level_ai`. |
| `sponsorship`, `work_authorization`, `citizenship`, `clearance` | What the listing states (codes as in the Reports page, for example `not_supported` = no sponsorship). |
| `genai_mention`, `genai_terms` | Whether it mentions generative AI, and which terms. |
| `extracted` | TRUE when AI details exist. |
| `first_discovered_at`, `discovery_day`, `discovery_week` | When the job was first found (earliest posting of the group). |
| `cohort` | `discovered` (found after its board's first scan) or `initial_import` (already open when the board was added). |
| `postings_in_group` | How many postings were merged into this job. |
| `posting_url` | The employer's listing. |
| `as_of` | The time this row's state refers to. |

### job_skills (rpt_job_skills): one row per current job per skill per requirement level

`job_key`, `employer_name`, `role_group`, `metro`, `level_ai`, `level_years` (repeated from
jobs, for filtering), `skill`, `requirement` (`required` or `preferred`), `mention_kind`
(`individual`, or `alternative` when the skill is one of a set such as "Python or Java").
Count jobs with `COUNTD([job_key])`, employers with `COUNTD([employer_name])`.

### genai_terms (rpt_job_genai_terms): one row per current job per generative-AI term

`job_key`, `employer_name`, `role_group`, `metro`, `level_ai`, `term`.

### skill_pairs (rpt_skill_pairs): one row per skill pair per role group

`skill_a` (sorts before `skill_b`), `skill_b`, `role_group`, `jobs` (current jobs that require
or prefer both), `employers`. Jobs add up across role groups; employers do not.

### weekly_jobs (rpt_weekly_jobs): one row per week x level rule x level x role group x metro

| Column | Meaning |
|---|---|
| `week_start`, `week_complete` | The week, and FALSE for the current, unfinished week. |
| `level_rule`, `level` | `ai` (level is a `level_ai` value) or `years` (a `level_years` value). Every job appears once under each rule: always filter to one rule. |
| `role_group`, `metro` | As in jobs. |
| `new_jobs`, `new_job_employers` | New jobs that week (initial imports excluded), classified by what is known now; their employers. |
| `genai_new_jobs` | New jobs mentioning generative AI. |
| `open_jobs_end`, `genai_open_jobs_end` | Jobs listed at the end of the week (or the latest day, for the current week), classified by what was known then; and those mentioning generative AI. |

Employer counts do not add up across rows.

### weekly_skills (rpt_weekly_skills): one row per week x skill x role group

`week_start`, `skill`, `role_group`, `new_jobs_requiring` (new kept jobs that week requiring
the skill, individually or as one of a set).

### daily_activity (rpt_daily_activity, kept postings)

One row per day: `report_day`, `population` (always `retained`), `is_complete` (FALSE for
today), `initial_imports`, `discoveries`, `updates`, `became_possibly_unavailable`,
`assumed_inactive`, `explicit_closures`, `reactivations`, `boards_baselined`,
`boards_with_baseline`. Counts are postings, not merged jobs.

### eligibility (rpt_eligibility, kept postings)

One row per `concept` (sponsorship, work_authorization, citizenship, clearance) per `status`:
`postings`, the three denominators `listed_postings`, `extracted_postings`,
`stating_postings`, and the shares `share_of_listed`, `share_of_extracted`,
`share_of_stating` (percent, empty where that denominator does not apply).

### pipeline_health (rpt_pipeline_health_daily)

One row per day: `report_day`, `is_complete`, `scans`, `complete_scans`, `partial_scans`,
`failed_scans`, `boards_scanned`, `boards_on_hold`, `extraction_requests`,
`extraction_successes`, `extraction_failures`, `prompt_tokens`, `output_tokens`,
`listed_retained_postings`, `listed_retained_extracted`, `retained_extraction_coverage_pct`.

## Limits to keep in mind

- Coverage: only the employers' boards the project tracks, not the whole DFW and Houston
  market. A new board adds jobs that are not new hiring (shown as initial imports).
- Concentration: one employer can hold a large share of current jobs. Charts that rank
  skills or requirements can count employers instead of jobs to show breadth.
- AI details: levels, requirements, and skills come from AI extraction checked against
  quoted evidence, not reviewed by hand. Jobs not yet processed are shown as Not extracted.
- Trends: weekly charts need several weeks of collection; early weeks have few new jobs.
- Generative-AI mentions are keyword matches: they show what listings say, not how the job
  uses AI. Changing the term list changes every week's numbers from the next build on.
