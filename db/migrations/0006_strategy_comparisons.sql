-- Complete comparisons are independent of ordinary recommendation/feedback records.
CREATE TABLE strategy_comparisons (
    comparison_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    input_sha256 char(64) NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now()
);
