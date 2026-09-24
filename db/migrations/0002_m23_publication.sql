-- Persist idempotent catalog imports and recoverable single-coordinator publication.

CREATE TABLE catalog_imports (
    batch_id uuid PRIMARY KEY,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    item_count integer NOT NULL CHECK (item_count BETWEEN 1 AND 1000),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE publication_operations (
    operation_id uuid PRIMARY KEY,
    target_bundle_id uuid NOT NULL REFERENCES bundle_versions(bundle_id) ON DELETE RESTRICT,
    expected_active_bundle_id uuid,
    previous_bundle_id uuid,
    status text NOT NULL CHECK (status IN ('preparing', 'switched', 'completed', 'aborted')),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (status <> 'completed' OR completed_at IS NOT NULL)
);

CREATE INDEX publication_operations_unfinished
    ON publication_operations (created_at) WHERE status IN ('preparing', 'switched');
