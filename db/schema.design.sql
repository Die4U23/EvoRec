-- DESIGN DRAFT: for an empty PostgreSQL database; not yet executed or a production migration.
-- UUIDs are supplied by the application. Cross-resource publication needs the protocol in docs/02.
-- 2026-09-15 architecture adds pending migration requirements: job leases/fencing,
-- publication operation records, bundle membership, request hidden snapshots/fallback reasons,
-- original feedback outcomes, and session ownership/favorites. This draft does not implement them.
BEGIN;

CREATE TABLE items (
    item_id varchar(128) PRIMARY KEY CHECK (item_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    title varchar(300) NOT NULL CHECK (length(btrim(title)) > 0),
    category varchar(100) NOT NULL CHECK (length(btrim(category)) > 0),
    description text NOT NULL DEFAULT '',
    image_url text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bundle_versions (
    bundle_id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('building', 'ready', 'active', 'retired', 'failed')),
    artifact_path text NOT NULL,
    manifest_sha256 char(64) CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status NOT IN ('ready', 'active', 'retired') OR manifest_sha256 IS NOT NULL)
);
CREATE UNIQUE INDEX one_active_bundle ON bundle_versions ((status)) WHERE status = 'active';

CREATE TABLE catalog_control (
    singleton smallint PRIMARY KEY CHECK (singleton = 1),
    active_bundle_id uuid REFERENCES bundle_versions(bundle_id),
    exclusion_version bigint NOT NULL DEFAULT 0 CHECK (exclusion_version >= 0)
);

CREATE TABLE sessions (
    session_id uuid PRIMARY KEY,
    seed_user_id text,
    epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    history_version bigint NOT NULL DEFAULT 0 CHECK (history_version >= 0),
    history jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(history) = 'array'),
    hidden_items jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(hidden_items) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE recommendation_requests (
    request_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(session_id),
    session_epoch bigint NOT NULL CHECK (session_epoch >= 0),
    history_version bigint NOT NULL CHECK (history_version >= 0),
    history_snapshot jsonb NOT NULL CHECK (jsonb_typeof(history_snapshot) = 'array'),
    bundle_id uuid NOT NULL REFERENCES bundle_versions(bundle_id),
    exclusion_version bigint NOT NULL CHECK (exclusion_version >= 0),
    requested_strategy text NOT NULL,
    actual_strategy text,
    status text NOT NULL CHECK (status IN ('accepted', 'completed', 'failed', 'expired')),
    timings jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE request_items (
    request_id uuid NOT NULL REFERENCES recommendation_requests(request_id),
    item_id varchar(128) NOT NULL REFERENCES items(item_id),
    position integer NOT NULL CHECK (position >= 1),
    score double precision,
    PRIMARY KEY (request_id, item_id),
    UNIQUE (request_id, position)
);

CREATE TABLE feedback_events (
    event_id uuid PRIMARY KEY,
    request_id uuid NOT NULL,
    item_id varchar(128) NOT NULL,
    event_kind text NOT NULL CHECK (event_kind IN ('detail_view', 'exposure', 'favorite_set', 'hide_set')),
    desired_state boolean,
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    evidence jsonb,
    FOREIGN KEY (request_id, item_id) REFERENCES request_items(request_id, item_id),
    CHECK ((event_kind IN ('favorite_set', 'hide_set') AND desired_state IS NOT NULL)
        OR (event_kind IN ('detail_view', 'exposure') AND desired_state IS NULL))
);

CREATE TABLE catalog_imports (
    batch_id uuid PRIMARY KEY,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    item_count integer NOT NULL CHECK (item_count BETWEEN 1 AND 1000),
    status text NOT NULL CHECK (status IN ('pending_validation', 'building', 'ready', 'published', 'failed')),
    bundle_id uuid REFERENCES bundle_versions(bundle_id),
    error_report jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE background_jobs (
    job_id uuid PRIMARY KEY,
    job_kind text NOT NULL CHECK (job_kind IN ('catalog_build', 'comparison', 'evaluation')),
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    parameters jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    heartbeat_at timestamptz,
    result_path text,
    error_report jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX queued_jobs ON background_jobs(created_at) WHERE status = 'queued';

CREATE TABLE experiment_runs (
    run_id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('planned', 'running', 'completed', 'failed')),
    configuration jsonb NOT NULL,
    data_manifest jsonb NOT NULL,
    code_manifest jsonb NOT NULL,
    bundle_id uuid REFERENCES bundle_versions(bundle_id),
    result_path text,
    metrics jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status <> 'completed' OR (result_path IS NOT NULL AND metrics IS NOT NULL))
);

-- Application transactions still verify ownership, epochs, version compatibility and idempotency.
-- Never infer online readiness from these tables existing alone.
COMMIT;
