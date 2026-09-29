-- Keep old synchronous builds distinguishable from automatically resumed jobs.
ALTER TABLE catalog_builds DROP CONSTRAINT catalog_builds_status_check;
ALTER TABLE catalog_builds ADD CONSTRAINT catalog_builds_status_check
    CHECK (status IN ('queued', 'processing', 'ready', 'failed'));
ALTER TABLE catalog_builds ADD COLUMN auto_retry boolean NOT NULL DEFAULT false;
CREATE INDEX catalog_builds_queued ON catalog_builds(created_at, build_id)
    WHERE status = 'queued';
