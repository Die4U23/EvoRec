-- Additive staging only: R06 packages cannot be activated until online support exists.
ALTER TABLE bundle_versions ADD COLUMN runtime_kind text NOT NULL DEFAULT 'cpu-demo-v1'
    CHECK (runtime_kind IN ('cpu-demo-v1', 'r06-frozen-bundle-v1'));

ALTER TABLE items ADD COLUMN r06_first_seen_ms bigint
    CHECK (r06_first_seen_ms BETWEEN 0 AND 253402300799999);
ALTER TABLE items ADD COLUMN r06_model_text text
    CHECK (length(r06_model_text) <= 32768);
ALTER TABLE items ADD CONSTRAINT r06_source_pair
    CHECK ((r06_first_seen_ms IS NULL) = (r06_model_text IS NULL));
