-- Older imports retain their original audit record; missing snapshots are not guessed.
ALTER TABLE catalog_imports ADD COLUMN items_snapshot jsonb
    CHECK (items_snapshot IS NULL OR jsonb_typeof(items_snapshot) = 'array');

CREATE TABLE catalog_builds (
    build_id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES catalog_imports(batch_id) ON DELETE RESTRICT,
    bundle_id uuid NOT NULL UNIQUE,
    base_bundle_id uuid REFERENCES bundle_versions(bundle_id) ON DELETE RESTRICT,
    status text NOT NULL CHECK (status IN ('processing', 'ready', 'failed')),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'array'),
    total_count integer NOT NULL CHECK (total_count BETWEEN 1 AND 5000),
    processed_count integer NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
    failed_count integer NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    attempts integer NOT NULL DEFAULT 1 CHECK (attempts > 0),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (processed_count <= total_count AND failed_count <= total_count),
    CHECK (status <> 'ready' OR (processed_count = total_count AND error_code IS NULL))
);
CREATE INDEX catalog_builds_batch ON catalog_builds(batch_id, created_at);
