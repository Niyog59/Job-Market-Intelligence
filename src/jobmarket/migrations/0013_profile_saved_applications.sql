-- The owner's profile, saved jobs, application tracking, and visits (private schema).
--
-- The profile is typed in by the owner and confirmed on every save; nothing is inferred or
-- imported. Saved jobs and applications point at source postings (stable identities), never
-- at duplicate groups, so a group that later merges or splits changes nothing here.

CREATE TABLE __PRIVATE__.profile (
    account_id             smallint PRIMARY KEY
                               REFERENCES __PRIVATE__.accounts (account_id) ON DELETE CASCADE,
    version                integer NOT NULL CHECK (version >= 1),
    confirmed_at           timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    preferred_roles        text[] NOT NULL DEFAULT '{}',
    preferred_places       text[] NOT NULL DEFAULT '{}',
    preferred_arrangements text[] NOT NULL DEFAULT '{}',
    preferred_employment   text[] NOT NULL DEFAULT '{}',
    highlight_sponsorship  boolean NOT NULL DEFAULT false,
    highlight_restrictions boolean NOT NULL DEFAULT false
);

COMMENT ON COLUMN __PRIVATE__.profile.version IS
    'Increases on every save; comparisons say which version they used.';

CREATE TABLE __PRIVATE__.profile_skills (
    skill_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id smallint NOT NULL REFERENCES __PRIVATE__.profile (account_id) ON DELETE CASCADE,
    position   integer NOT NULL,
    name       text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    canonical  text NOT NULL,
    note       text CHECK (length(note) <= 500),
    link       text CHECK (link ~ '^https://' AND length(link) <= 500),
    UNIQUE (account_id, canonical)
);

CREATE TABLE __PRIVATE__.profile_experiences (
    experience_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    smallint NOT NULL
                      REFERENCES __PRIVATE__.profile (account_id) ON DELETE CASCADE,
    position      integer NOT NULL,
    -- Kept apart: only employment counts toward required years (decision 2026-10-29).
    kind          text NOT NULL CHECK (kind IN ('employment', 'internship', 'project')),
    title         text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    organization  text CHECK (length(organization) <= 120),
    start_month   date NOT NULL CHECK (extract(day FROM start_month) = 1),
    end_month     date CHECK (extract(day FROM end_month) = 1),
    current       boolean NOT NULL DEFAULT false,
    note          text CHECK (length(note) <= 1000),
    link          text CHECK (link ~ '^https://' AND length(link) <= 500),
    CHECK (end_month IS NULL OR end_month >= start_month),
    CHECK (NOT (current AND end_month IS NOT NULL))
);

CREATE TABLE __PRIVATE__.profile_education (
    education_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id       smallint NOT NULL
                         REFERENCES __PRIVATE__.profile (account_id) ON DELETE CASCADE,
    position         integer NOT NULL,
    level            text NOT NULL CHECK (level IN ('high_school', 'associate', 'bachelor',
                         'master', 'doctorate', 'certificate', 'bootcamp', 'other')),
    field            text CHECK (length(field) <= 120),
    institution      text CHECK (length(institution) <= 120),
    status           text NOT NULL CHECK (status IN ('completed', 'expected')),
    graduation_month date CHECK (extract(day FROM graduation_month) = 1)
);

-- One row per saved posting; unsaving keeps the row (removed_at) so notes are not lost.
CREATE TABLE __PRIVATE__.saved_jobs (
    saved_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id smallint NOT NULL REFERENCES __PRIVATE__.accounts (account_id) ON DELETE CASCADE,
    posting_id bigint NOT NULL REFERENCES __INGESTION__.source_postings (posting_id),
    saved_at   timestamptz NOT NULL,
    removed_at timestamptz,
    note       text CHECK (length(note) <= 5000),
    UNIQUE (account_id, posting_id)
);

-- Application tracking is entered by the owner only: opening an Apply link changes nothing,
-- and a closed source posting never changes it.
CREATE TABLE __PRIVATE__.applications (
    application_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id     smallint NOT NULL
                       REFERENCES __PRIVATE__.accounts (account_id) ON DELETE CASCADE,
    posting_id     bigint NOT NULL REFERENCES __INGESTION__.source_postings (posting_id),
    created_at     timestamptz NOT NULL,
    UNIQUE (account_id, posting_id)
);

-- Status history. Any status may follow any other (real searches are not linear). A
-- correction is a new event that names the one it corrects; nothing is overwritten.
CREATE TABLE __PRIVATE__.application_events (
    event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    application_id    bigint NOT NULL
                          REFERENCES __PRIVATE__.applications (application_id) ON DELETE CASCADE,
    status            text NOT NULL CHECK (status IN ('not_applied', 'applied', 'interviewing',
                          'offer', 'rejected', 'withdrawn')),
    event_date        date NOT NULL,
    note              text CHECK (length(note) <= 5000),
    recorded_at       timestamptz NOT NULL,
    corrects_event_id bigint UNIQUE REFERENCES __PRIVATE__.application_events (event_id)
);

-- Visits, for "new since last visit". cutoff is the previous visit's last activity; it
-- stays fixed for the whole visit. The first visit has no cutoff (a baseline).
CREATE TABLE __PRIVATE__.visits (
    visit_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id       smallint NOT NULL
                         REFERENCES __PRIVATE__.accounts (account_id) ON DELETE CASCADE,
    session_id       bigint,
    started_at       timestamptz NOT NULL,
    last_activity_at timestamptz NOT NULL,
    cutoff           timestamptz,
    CHECK (cutoff IS NULL OR cutoff <= started_at),
    CHECK (last_activity_at >= started_at)
);

CREATE INDEX visits_latest_idx ON __PRIVATE__.visits (account_id, started_at DESC);
