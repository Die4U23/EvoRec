-- Independent short comparison jobs; only complete results enter strategy_comparisons.
CREATE TABLE strategy_comparison_jobs (
    job_id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    input_sha256 CHAR(64) NOT NULL,
    frozen_input JSONB NOT NULL CHECK (jsonb_typeof(frozen_input) = 'object'),
    requested_strategies JSONB NOT NULL CHECK (jsonb_array_length(requested_strategies) BETWEEN 2 AND 3),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'cancelling', 'completed', 'cancelled', 'failed')),
    completed_strategies INTEGER NOT NULL DEFAULT 0 CHECK (completed_strategies BETWEEN 0 AND 3),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    lease_owner UUID,
    lease_until TIMESTAMPTZ,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK ((status IN ('running', 'cancelling')) = (lease_owner IS NOT NULL))
);
CREATE INDEX strategy_comparison_jobs_queue ON strategy_comparison_jobs(created_at, job_id)
    WHERE status IN ('queued', 'running', 'cancelling');
CREATE INDEX strategy_comparison_jobs_session ON strategy_comparison_jobs(session_id, created_at DESC);
