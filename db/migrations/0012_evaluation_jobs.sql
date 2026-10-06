-- Bounded label-only evaluations of already server-owned immutable comparison inputs.
CREATE TABLE evaluation_jobs (
    job_id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    input_sha256 CHAR(64) NOT NULL,
    frozen_input JSONB NOT NULL CHECK (jsonb_typeof(frozen_input) = 'object'),
    replay_of UUID REFERENCES evaluation_jobs(job_id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','running','cancelling','completed','cancelled','failed')),
    completed_cases INTEGER NOT NULL DEFAULT 0 CHECK (completed_cases BETWEEN 0 AND 100),
    total_cases INTEGER NOT NULL CHECK (total_cases BETWEEN 1 AND 100),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    lease_owner UUID,
    lease_until TIMESTAMPTZ,
    error_code TEXT,
    result JSONB CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK ((status IN ('running','cancelling')) = (lease_owner IS NOT NULL)),
    CHECK ((status = 'completed') = (result IS NOT NULL)),
    CHECK (completed_cases <= total_cases)
);
CREATE INDEX evaluation_jobs_queue ON evaluation_jobs(created_at, job_id)
    WHERE status IN ('queued','running','cancelling');
CREATE INDEX evaluation_jobs_session ON evaluation_jobs(session_id, created_at DESC, job_id DESC);
