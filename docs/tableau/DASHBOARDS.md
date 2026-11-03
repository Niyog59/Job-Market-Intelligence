# Dashboard designs

Seven dashboards in one Tableau Public workbook. The website's Market insights page
(`/insights`) shows the main one first and the rest under "See more dashboards"; the
questions and objectives below are the same ones the page shows
(`src/jobmarket/web/insights.py`).

Every chart answers a question that changes a decision for a job seeker in DFW or Houston
data, analytics, BI, software, data science, and ML roles. Chart types follow the data: bars
for ranked categories, lines for trends, 100% stacks for mix, histograms and box plots for
distributions, heatmaps for two categories at once. The main dashboard sticks to the types
most readers know; the specialized ones (dumbbell, slope, bump, waterfall, box plot,
bullet) are on the deeper dashboards, where they answer a question a plain bar cannot.

Rules for every dashboard:

- A caption in the bottom-right corner of every chart: the `data_pull_caption` field from
  the `about` tab ("Data collected through ... · reports built ...").
- A detail table at the bottom (the rows behind the charts), filtered by the same filters.
- Counts of jobs use `COUNTD([job_key])`. Level shares use jobs with a clear level (Junior,
  Mid, Senior+) as the denominator and say so in the subtitle.
- "Not extracted" stays its own category, shown in grey, never merged into Unclear or Not
  stated.
- Weekly charts mark the current, unfinished week (`week_complete` = FALSE) with a lighter
  mark and a tooltip "Week in progress".
- Colors: one blue for the main measure, grey for Unclear / Not stated / Not extracted, and a
  fixed color per level (Junior, Mid, Senior+) used the same way on every dashboard.

Field and tab names are in `DATA_DICTIONARY.md`; how to build each chart is in
`BUILD_GUIDE.md`.

## 1. What are employers asking for? (main, view `WhatEmployersAskFor`)

**Question.** Which skills, levels, degrees, and experience do DFW and Houston employers
require?
**Objective.** Decide which skills to build first and which roles are realistic to apply
for now.
**Filters (apply to every chart):** role group, metro.

| Chart | Type | Data | Answers |
|---|---|---|---|
| KPI cards | Text tiles | `about`, `jobs`, `weekly_jobs` | How big is the market right now: open jobs, employers, new jobs in one week (`KPI week`), % junior (of clear level), % mentioning generative AI. |
| Top required skills | Horizontal bar, top N (parameter `Top skills`) | `job_skills`, `requirement` = required | Which skills to learn first. A toggle switches the measure between jobs and employers, so one large employer cannot dominate the ranking. |
| Junior vs senior mix | 100% stacked bar by role group | `jobs`, `level_ai` | Which role groups are open to juniors. |
| Junior hiring each week | Combo: weekly bars (new jobs) with a line (junior share of new jobs with a clear level) | `weekly_jobs`, rule `ai` | Is the junior share of new hiring rising or falling. |
| Experience required | Histogram of `overall_min_years` (1-year bins), with a "Not stated" bar apart | `jobs` | How many years most jobs ask for. |
| Minimum degree | Horizontal bar | `jobs`, `minimum_degree` | Whether a Bachelor is the usual bar, and how often no degree is stated. |
| Detail table | Table | `jobs` | Employer, title, metro, level, years, degree, genAI, link. |

## 2. Junior vs senior hiring (view `JuniorVsSenior`)

**Question.** Is hiring for junior roles growing or shrinking compared with mid and senior
roles?
**Objective.** Know whether entry-level openings are expanding, to time applications and
set expectations.
**Filters:** role group, metro, level rule (parameter: "AI reading" = `ai`, "Years asked"
= `years`).

| Chart | Type | Data | Answers |
|---|---|---|---|
| New jobs by level | Line, one line per level | `weekly_jobs`, the chosen rule | Is each level's hiring growing week to week. |
| Level mix of new jobs | 100% stacked area | `weekly_jobs` | Is the junior share changing even when totals move. |
| Then vs now | Slope chart: share of open jobs per level, first complete week vs latest complete week | `weekly_jobs`, `open_jobs_end` | Did the mix of open jobs shift over the period. |
| Rule comparison | Side-by-side bars: the same current jobs by `level_ai` and by `level_years` | `jobs` | How much the two definitions of "junior" disagree, so the trend is not an artifact of one rule. |
| Detail table | Table | `jobs` | Junior jobs first: employer, title, level by both rules, years. |

## 3. AI in the job market (view `AIInTheJobMarket`)

**Question.** How is generative AI showing up in job requirements, including outside AI
roles?
**Objective.** See whether AI skills are becoming expected in ordinary data and software
roles.
**Filters:** metro, level.

| Chart | Type | Data | Answers |
|---|---|---|---|
| Share mentioning generative AI | Line by week: AI/ML roles vs all other roles | `weekly_jobs`, `genai_open_jobs_end` / `open_jobs_end` | Is AI wording spreading beyond AI roles. |
| AI/ML vs other roles | Dumbbell: % mentioning generative AI per role group, AI/ML dot vs the group's dot | `jobs` | How far behind AI/ML each other group is. |
| Which AI terms | Horizontal bar | `genai_terms` | What employers actually name: LLMs, RAG, agents, Copilot. |
| Employers: junior openings vs AI wording | Scatter, one point per employer with at least `Minimum employer jobs` current jobs: % junior (x) vs % mentioning generative AI (y), sized by jobs | `jobs` | Whether employers that talk about AI hire fewer juniors. Labeled "correlation only: this does not show that AI causes fewer junior roles". |
| Detail table | Table | `jobs`, `genai_mention` = TRUE | Employer, title, role group, terms, link. |

## 4. Skills in depth (view `SkillsInDepth`)

**Question.** How do skills differ between data and software roles, and which come
together?
**Objective.** Plan learning as the bundles of skills employers ask for together.
**Filters:** metro, level, requirement (required / preferred).

| Chart | Type | Data | Answers |
|---|---|---|---|
| Data vs software | Dumbbell: % of Data jobs vs % of Software jobs asking for each of the top N skills (`Top skills`) | `job_skills` | Which skills are shared and which are specific to one path. |
| Skills asked together | Heatmap: top N skills x top N skills (`Top skills`), color = jobs asking for both | `skill_pairs` | Which pairs to learn together (for example SQL with Python). |
| Rank over time | Bump chart: weekly rank of the top N required skills (`Top skills`) among new jobs | `weekly_skills`, `RANK` per week | Which skills are rising or falling in demand. |
| BI tools | Horizontal bar: Tableau, Power BI, Looker, Qlik, Excel | `job_skills` | Which BI tool is most worth learning here. |
| Detail table | Table | `job_skills` | Skill, requirement, employer, title. |

## 5. Market overview (view `MarketOverview`)

**Question.** Where are the jobs, who is hiring, and how is the number of open jobs
changing?
**Objective.** Focus applications on the places, work arrangements, and employers with the
most openings.
**Filters:** role group, level.

| Chart | Type | Data | Answers |
|---|---|---|---|
| Change in open jobs | Waterfall: discoveries up, closures and assumed-inactive down, per day over the period chosen with `Waterfall days` | `daily_activity` | Is the market growing or shrinking, and why (new openings vs closures). Initial imports shown as a separate grey step, not hiring. |
| Where and how | Highlight table: metro x work arrangement, color = jobs | `jobs` | Where on-site, hybrid, and remote openings are. |
| Top employers | Horizontal bar, top N by current jobs (`Top employers`) | `jobs` | Who is hiring most, and how concentrated the market is. |
| Detail table | Table | `jobs` | Employer, title, city, arrangement, discovery day, link. |

## 6. Requirements and eligibility (view `Requirements`)

**Question.** What experience, degrees, and work eligibility do employers require?
**Objective.** Spot early the jobs you are not eligible for, and see how common each
requirement is.
**Filters:** role group, metro.

| Chart | Type | Data | Answers |
|---|---|---|---|
| Years by role group | Box plot of `overall_min_years`, one box per role group | `jobs` | The typical and the spread of experience asked per path. |
| Degree by role group | 100% stacked bar of `minimum_degree` | `jobs` | Where a Master's is common and where no degree is stated. |
| Eligibility statements | Horizontal bars per concept (sponsorship, work authorization, citizenship, clearance) with a denominator parameter (of listed / of with details / of stating a policy) | `eligibility` | How often each requirement appears, under each honest denominator. |
| Detail table | Table | `jobs` | Employer, title, years, degree, sponsorship, citizenship, clearance. |

## 7. About the data (view `AboutTheData`)

**Question.** How fresh and complete is this data, and what are its limits?
**Objective.** Judge how much weight each number can carry.

| Chart | Type | Data | Answers |
|---|---|---|---|
| Extraction coverage | Bullet chart: % of current jobs with AI details vs a 100% target | `about` | How many jobs the level and requirement charts are based on. |
| Collection calendar | Calendar heatmap: day x week, color = complete scans | `pipeline_health` | Which days were collected and where there are gaps. |
| Definitions and caveats | Text | this document, `DATA_DICTIONARY.md` | What "job", "new", "junior", and "mentions generative AI" mean, and the limits listed there. |
| Detail table | Table | `pipeline_health` | Day, scans, failures, AI requests, coverage. |

## Display values

These are Tableau parameters, so they can change without rebuilding a chart. Values decided
2026-11-02.

| Parameter | Value | Used by | What it controls |
|---|---|---|---|
| `Top skills` | 15 | main, Skills in depth | How many skills the ranked bars, dumbbell, heatmap, and bump chart show. |
| `Top employers` | 15 | Market overview | How many employers the bar shows. |
| `Minimum employer jobs` | 3 | AI in the job market | Employers with fewer current jobs are left out of the scatter, because a share from one or two jobs swings between 0% and 100%. |
| `Waterfall days` | 28 | Market overview | How many recent days the waterfall covers (four weeks). |
| `KPI week` | latest complete week | main | The new-jobs card counts the latest complete week (`week_complete` = TRUE), labeled with its dates, so it does not look like a drop early in a week. |

## Later

- Nationwide: which US cities hire most and least, once collection covers boards outside
  Texas (a map with a ranked bar beside it).
