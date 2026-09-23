-- M11 core persistence schema. Applied transactionally by scripts/migrate_database.py.

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

CREATE UNIQUE INDEX one_active_bundle
    ON bundle_versions ((status)) WHERE status = 'active';

CREATE TABLE bundle_items (
    bundle_id uuid NOT NULL REFERENCES bundle_versions(bundle_id) ON DELETE RESTRICT,
    item_id varchar(128) NOT NULL REFERENCES items(item_id) ON DELETE RESTRICT,
    internal_item_id bigint NOT NULL CHECK (internal_item_id >= 0),
    PRIMARY KEY (bundle_id, item_id),
    UNIQUE (bundle_id, internal_item_id)
);

CREATE TABLE catalog_control (
    singleton smallint PRIMARY KEY CHECK (singleton = 1),
    active_bundle_id uuid REFERENCES bundle_versions(bundle_id) ON DELETE RESTRICT,
    exclusion_version bigint NOT NULL DEFAULT 0 CHECK (exclusion_version >= 0),
    admission_open boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO catalog_control (singleton) VALUES (1);

CREATE TABLE sessions (
    session_id uuid PRIMARY KEY,
    owner_token_sha256 char(64) NOT NULL CHECK (owner_token_sha256 ~ '^[0-9a-f]{64}$'),
    seed_user_id text,
    epoch bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    history_version bigint NOT NULL DEFAULT 0 CHECK (history_version >= 0),
    history jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(history) = 'array'),
    hidden_items jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(hidden_items) = 'array'),
    favorite_items jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(favorite_items) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE recommendation_requests (
    request_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    session_epoch bigint NOT NULL CHECK (session_epoch >= 0),
    history_version bigint NOT NULL CHECK (history_version >= 0),
    history_snapshot jsonb NOT NULL CHECK (jsonb_typeof(history_snapshot) = 'array'),
    hidden_snapshot jsonb NOT NULL CHECK (jsonb_typeof(hidden_snapshot) = 'array'),
    bundle_id uuid NOT NULL REFERENCES bundle_versions(bundle_id) ON DELETE RESTRICT,
    exclusion_version bigint NOT NULL CHECK (exclusion_version >= 0),
    requested_strategy text NOT NULL,
    actual_strategy text,
    requested_k integer NOT NULL CHECK (requested_k BETWEEN 1 AND 50),
    status text NOT NULL CHECK (status IN ('accepted', 'completed', 'failed', 'expired')),
    fallback_reason text CHECK (fallback_reason IS NULL OR length(btrim(fallback_reason)) > 0),
    failure_code text,
    timings jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (status <> 'completed' OR (actual_strategy IS NOT NULL AND completed_at IS NOT NULL))
);

CREATE INDEX recommendation_requests_session_created
    ON recommendation_requests (session_id, created_at DESC);

CREATE TABLE request_items (
    request_id uuid NOT NULL REFERENCES recommendation_requests(request_id) ON DELETE CASCADE,
    item_id varchar(128) NOT NULL REFERENCES items(item_id) ON DELETE RESTRICT,
    position integer NOT NULL CHECK (position >= 1),
    score double precision NOT NULL CHECK (
        score NOT IN ('NaN'::double precision, 'Infinity'::double precision, '-Infinity'::double precision)
    ),
    source text NOT NULL CHECK (length(btrim(source)) > 0),
    PRIMARY KEY (request_id, item_id),
    UNIQUE (request_id, position)
);

CREATE TABLE feedback_events (
    event_id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES sessions(session_id) ON DELETE RESTRICT,
    request_id uuid NOT NULL,
    item_id varchar(128) NOT NULL,
    session_epoch bigint NOT NULL CHECK (session_epoch >= 0),
    event_kind text NOT NULL CHECK (
        event_kind IN ('detail_view', 'exposure', 'favorite_set', 'hide_set')
    ),
    desired_state boolean,
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    evidence jsonb,
    outcome_history_version bigint NOT NULL CHECK (outcome_history_version >= 0),
    FOREIGN KEY (request_id, item_id)
        REFERENCES request_items(request_id, item_id) ON DELETE RESTRICT,
    CHECK ((event_kind IN ('favorite_set', 'hide_set') AND desired_state IS NOT NULL)
        OR (event_kind IN ('detail_view', 'exposure') AND desired_state IS NULL))
);

CREATE INDEX feedback_events_session_received
    ON feedback_events (session_id, received_at DESC);

CREATE TABLE session_item_states (
    session_id uuid NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    item_id varchar(128) NOT NULL REFERENCES items(item_id) ON DELETE RESTRICT,
    is_favorite boolean NOT NULL DEFAULT false,
    is_hidden boolean NOT NULL DEFAULT false,
    updated_by_event_id uuid NOT NULL REFERENCES feedback_events(event_id) ON DELETE RESTRICT,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, item_id)
);
