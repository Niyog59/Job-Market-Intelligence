# Building and publishing the workbook

How to build the dashboards in `DASHBOARDS.md` in Tableau Desktop Public Edition (free),
from the Google Sheet the pipeline fills, and publish them so the website can embed them.

## 1. Before you start

- The dashboard Sheet exists and has been filled at least once (`deploy/RUNBOOK.md` section
  T, or `jobmarket tableau sync` from your computer).
- Tableau Desktop Public Edition is installed, and you are signed in to a Tableau Public
  account.
- You can open the Sheet with your own Google account (as its owner). Tableau Public reads it
  through your Google sign-in; the service account key is never used by Tableau.

## 2. Connect to the Sheet

1. Connect > To a Server > Google Sheets, sign in with your Google account, and pick
   `Job Market Intelligence - dashboard data`.
2. Make one data source per tab you need: `jobs`, `job_skills`, `genai_terms`,
   `skill_pairs`, `weekly_jobs`, `weekly_skills`, `daily_activity`, `eligibility`,
   `pipeline_health`. Name each data source after its tab.
3. Add the data-pull caption to each data source: drag the `about` tab next to the main tab,
   and in the relationship dialog choose "Create relationship calculation" on both sides
   with the value `1`. Every row then sees the single `about` row, so
   `[data_pull_caption]` is available in every worksheet.
4. Check the field types: dates (`week_start`, `report_day`, `discovery_day`) are Date,
   `first_discovered_at` and `built_at` are Date & Time, counts are numbers, and
   `genai_mention`, `week_complete`, `is_complete`, `extracted` are Boolean.

## 3. Calculated fields

Create these once per data source that needs them (Analysis > Create Calculated Field).

| Name | Data source | Formula |
|---|---|---|
| `Jobs` | jobs, job_skills, genai_terms | `COUNTD([job_key])` |
| `Employers` | jobs, job_skills | `COUNTD([employer_name])` |
| `Clear level jobs` | jobs | `COUNTD(IF [level_ai] IN ("Junior", "Mid", "Senior+") THEN [job_key] END)` |
| `% junior` | jobs | `COUNTD(IF [level_ai] = "Junior" THEN [job_key] END) / [Clear level jobs]` |
| `% genAI` | jobs | `COUNTD(IF [genai_mention] THEN [job_key] END) / [Jobs]` |
| `AI/ML or other` | jobs, weekly_jobs | `IF [role_group] = "AI/ML" THEN "AI/ML roles" ELSE "Other roles" END` |
| `Junior share of new` | weekly_jobs | `SUM(IF [level] = "Junior" THEN [new_jobs] END) / SUM(IF [level] IN ("Junior", "Mid", "Senior+") THEN [new_jobs] END)` |
| `genAI share open` | weekly_jobs | `SUM([genai_open_jobs_end]) / SUM([open_jobs_end])` |
| `Skill share` | job_skills | `[Jobs] / TOTAL([Jobs])`, computed along the skill (the share of jobs in the role group asking for the skill) |
| `Skill rank` | weekly_skills | `RANK(SUM([new_jobs_requiring]))`, computed along `skill`, restarting every `week_start` |
| `Measure: jobs or employers` | job_skills | `CASE [Count by] WHEN "Jobs" THEN [Jobs] ELSE [Employers] END` |
| `Week in progress` | weekly_jobs | `NOT [week_complete]` |
| `Latest week` | weekly_skills | `[week_start] = {MAX([week_start])}` (this tab has no completeness column; the latest week is the one in progress, except just after a week ends) |

Format every share as a percentage with no decimals.

**Parameters.**

- `Count by` (string: Jobs, Employers), for the main skills chart.
- `Level rule` (string: `ai` shown as "AI reading", `years` shown as "Years asked"). Add the
  filter `[level_rule] = [Level rule]` to every `weekly_jobs` worksheet: each job appears
  once under each rule, so a sheet without this filter counts jobs twice.
- `Denominator` (string: Of listed, Of with details, Of stating a policy), for eligibility,
  with the calculation `CASE [Denominator] WHEN "Of listed" THEN AVG([share_of_listed])
  WHEN "Of with details" THEN AVG([share_of_extracted]) ELSE AVG([share_of_stating]) END`.
- `Top skills`, `Top employers`, `Minimum employer jobs`, `Waterfall days` (integers): the
  display values in `DASHBOARDS.md` (15, 15, 3, 28). Use them in Top N filters (Filter > Top > By
  field > Top [parameter]) and in the scatter and waterfall filters.

## 4. Building the charts

One worksheet per chart, named after the chart. On every chart worksheet: Worksheet > Show
Caption, set the caption to `<ATTR(data_pull_caption)>`, right-aligned, small grey text.

**Main dashboard.**

- KPI cards: one worksheet per number, `Jobs`, `Employers`, `% junior`, `% genAI` from
  `jobs`, and new jobs from `weekly_jobs` (`SUM([new_jobs])`, filtered to the rule and
  to the latest week with `week_complete` = TRUE, and the week's dates as its label). Text mark, large font, label below.
- Top required skills: `job_skills`, filter `requirement` = required; `skill` on Rows,
  `Measure: jobs or employers` on Columns, sorted descending, Top N by `Top skills`. Show
  the `Count by` parameter control.
- Junior vs senior mix: `jobs`; `role_group` on Rows, `Jobs` on Columns, `level_ai` on
  Color; Quick Table Calculation > Percent of Total, computed along `level_ai`.
- Junior hiring each week: `weekly_jobs`, rule filter; `week_start` (exact week, continuous)
  on Columns, `SUM([new_jobs])` and `Junior share of new` on Rows; dual axis, bars for the
  first and a line for the second; do not synchronize the axes. `Week in progress` on
  Opacity or Color.
- Experience required: `jobs`; create bins on `overall_min_years` (size 1); bins on
  Columns, `Jobs` on Rows. Put "Not stated" in a separate one-bar sheet beside it, since a
  histogram cannot show missing values.
- Minimum degree: `jobs`; `minimum_degree` on Rows, `Jobs` on Columns, sorted in degree
  order (High school to Doctorate, then the others).

**Junior vs senior.**

- New jobs by level: `weekly_jobs`; `week_start` on Columns, `SUM([new_jobs])` on Rows,
  `level` on Color, line mark.
- Level mix: same, area mark, Percent of Total computed along `level`.
- Then vs now: `open_jobs_end` share per level, filtered to the first and latest complete
  weeks (`week_complete` = TRUE); `week_start` as a discrete dimension on Columns, line mark
  with `level` on Color and labels at both ends.
- Rule comparison: two bar sheets from `jobs`, `level_ai` and `level_years`, side by side.

**AI in the job market.**

- Share over time: `weekly_jobs`; `week_start` on Columns, `genAI share open` on Rows,
  `AI/ML or other` on Color, line mark.
- Dumbbell: `jobs`; `role_group` on Rows, `% genAI` on Columns twice (dual axis, circles,
  and a line mark with `role_group` on Path) to connect each group to AI/ML.
- Terms: `genai_terms`; `term` on Rows, `Jobs` on Columns, sorted descending.
- Scatter: `jobs`; `employer_name` on Detail, `% junior` on Columns, `% genAI` on Rows,
  `Jobs` on Size; filter `employer_name` by condition `Jobs >= [Minimum employer jobs]`.
  Title note: "Correlation only: this does not show that AI causes fewer junior roles."

**Skills in depth.**

- Dumbbell: `job_skills`, filter `role_group` in (Data, Software); `skill` on Rows,
  `Skill share` on Columns, `role_group` on Color, circle mark, plus a line mark on a dual
  axis with `role_group` on Path.
- Heatmap: `skill_pairs`; `skill_a` on Columns, `skill_b` on Rows, `SUM([jobs])` on Color,
  square mark; filter both to the top skills (a set from `job_skills` Top N).
- Bump chart: `weekly_skills`; `week_start` on Columns, `Skill rank` on Rows (axis
  reversed), `skill` on Color, line and circle marks on a dual axis.
- BI tools: `job_skills`, filter `skill` in (Tableau, Power BI, Looker, Qlik, Excel); bars.

**Market overview.**

- Waterfall: `daily_activity`; a calculation `[discoveries] + [reactivations] -
  [explicit_closures] - [assumed_inactive]` as the change per day; `report_day` on Columns,
  Running Sum of the change on Rows, Gantt bar mark sized by the negative of the change;
  filter to the last `Waterfall days` days. Show `initial_imports` as its own grey bar
  sheet: imports are coverage, not hiring.
- Highlight table: `jobs`; `metro` on Rows, `work_arrangement` on Columns, `Jobs` on Color
  and Label, square mark.
- Top employers: `jobs`; `employer_name` on Rows, `Jobs` on Columns, Top N by
  `Top employers`.

**Requirements and eligibility.**

- Box plot: `jobs`; `role_group` on Columns, `overall_min_years` on Rows (as a dimension
  so each job is a point, with `job_key` on Detail), circle mark, then Analytics > Box
  Plot.
- Degree by role group: `jobs`; `role_group` on Rows, `Jobs` on Columns, `minimum_degree`
  on Color, Percent of Total along `minimum_degree`.
- Eligibility: `eligibility`; `concept` and `status` on Rows, the `Denominator`
  calculation on Columns; show the parameter control. Leave empty shares empty (they do not
  apply to that denominator).

**About the data.**

- Bullet chart: from `about`, `SUM([jobs_with_ai_details]) / SUM([current_jobs])` as the
  bar with a reference line at 100%.
- Calendar: `pipeline_health`; `WEEKDAY(report_day)` on Columns, `WEEK(report_day)` on
  Rows, `SUM([complete_scans])` on Color, square mark.
- Definitions: a Text object with the definitions and limits from `DATA_DICTIONARY.md`.

**Detail tables.** One per dashboard: the fields listed in `DASHBOARDS.md` on Rows, with
`posting_url` as a URL action (Dashboard > Actions > Go to URL, on select).

## 5. Dashboards

1. One dashboard per section of `DASHBOARDS.md`, named exactly with its view name:
   `WhatEmployersAskFor`, `JuniorVsSenior`, `AIInTheJobMarket`, `SkillsInDepth`,
   `MarketOverview`, `Requirements`, `AboutTheData`. The website builds its addresses from
   these names; a different name shows "not found" in the page's frame.
2. Size: Fixed, at most 860 px high so it fits the page's frame (880 px) without a scroll
   bar on a laptop. Charts in a grid, KPI cards on top, detail table at the bottom.
3. Add the filters listed for that dashboard and apply each to "All using related data
   sources" (Filter > Apply to Worksheets), so one control filters every chart.
4. Each dashboard's title is its question; add its objective as a subtitle.

## 6. Publish and keep it in sync

1. File > Save to Tableau Public As..., workbook name `JobMarketIntelligence` (or your
   choice: the website only needs whatever name appears in the address).
2. When asked, turn on keeping the data in sync with Google Sheets. Tableau Public then
   re-reads the Sheet about once a day; you can also request an update on the workbook's
   page. Check on the workbook's settings that sync is on.
3. Turn off "Show workbook sheets as tabs": the website gives each dashboard its own page.
4. Open the main dashboard on Tableau Public. Its address is
   `https://public.tableau.com/views/NAME/WhatEmployersAskFor`. `NAME` is the value for
   `JOBMARKET_TABLEAU_WORKBOOK` (`deploy/RUNBOOK.md` T5).

## 7. Checks after publishing

- Each dashboard opens at `https://public.tableau.com/views/NAME/<view name>`.
- The caption under each chart matches the `about` tab's `data_pull_caption`.
- Totals agree with the website: the Open jobs card equals the job count in the job-search
  page's Market insights summary (both read the same publication).
- After the next scheduled publication and Tableau Public's next refresh, the caption shows
  the newer time.
- Everything in the workbook is public. The Sheet contains only published job-market data;
  never add other sources to this workbook.
