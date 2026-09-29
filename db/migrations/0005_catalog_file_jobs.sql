CREATE TABLE catalog_file_jobs (
    batch_id uuid PRIMARY KEY,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    media_type text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'validating', 'imported', 'failed')),
    payload bytea CHECK (payload IS NULL OR octet_length(payload) <= 20000000),
    item_count integer NOT NULL DEFAULT 0 CHECK (item_count BETWEEN 0 AND 1000),
    error_code text,
    row_errors jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(row_errors) = 'array'),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status IN ('queued', 'validating')) = (payload IS NOT NULL)),
    CHECK (status <> 'imported' OR (item_count > 0 AND error_code IS NULL)),
    CHECK (status <> 'failed' OR error_code IS NOT NULL)
);
CREATE INDEX catalog_file_jobs_queued ON catalog_file_jobs(created_at, batch_id)
    WHERE status = 'queued';
