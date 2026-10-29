-- Full-text search for the job-search website (title and description of each content
-- version). Computing the search vector for every listing on each request takes seconds, so
-- the vector is indexed. The website must call posting_search_vector() with exactly these
-- arguments for PostgreSQL to use the index.
--
-- Only an index and a function are added; no rows or columns change. The 'english'
-- configuration stems words ("pipelines" finds "pipeline") and ignores stop words.

CREATE FUNCTION posting_search_vector(title text, description text) RETURNS tsvector
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    RETURN to_tsvector('english'::regconfig, coalesce(title, '') || ' ' || coalesce(description, ''));

COMMENT ON FUNCTION posting_search_vector(text, text) IS
    'Search vector of a posting version (title and visible description text). Indexed by '
    'posting_versions_search_idx; queries must use the same expression.';

CREATE INDEX posting_versions_search_idx ON posting_versions
    USING gin (posting_search_vector(title, description_text));
