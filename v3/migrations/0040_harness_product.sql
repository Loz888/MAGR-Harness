-- Product projections use the existing Engine connection and monetary ledger.
-- 0038 and 0039 are reserved by the Super-Agent ownership agreement.
CREATE TABLE app.harness_project_authorizations (
    project_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    actor text NOT NULL,
    registration_hash text NOT NULL,
    allow_write boolean NOT NULL,
    allow_paid boolean NOT NULL,
    allow_github_read boolean NOT NULL DEFAULT false,
    max_cost_micros bigint NOT NULL CHECK (max_cost_micros BETWEEN 1 AND 12000000),
    max_model_calls integer NOT NULL CHECK (max_model_calls BETWEEN 1 AND 512),
    profile_hash text NOT NULL UNIQUE REFERENCES app.authorization_budgets,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, revision)
);
CREATE TABLE app.harness_sessions (
    session_id text PRIMARY KEY,
    project_id text NOT NULL,
    actor text NOT NULL,
    title text NOT NULL,
    initial_client_id text NOT NULL,
    thread_id uuid REFERENCES app.threads,
    latest_task_id text REFERENCES app.harness_tasks,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (actor,initial_client_id)
);
CREATE TABLE app.harness_session_turns (
    turn_id bigserial PRIMARY KEY,
    session_id text NOT NULL REFERENCES app.harness_sessions,
    client_id text NOT NULL,
    text text NOT NULL,
    task_id text REFERENCES app.harness_tasks,
    state text NOT NULL DEFAULT 'preparing'
        CHECK (state IN ('preparing','accepted','failed')),
    error_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, client_id)
);
CREATE TABLE app.harness_apply_receipts (
    task_id text PRIMARY KEY REFERENCES app.harness_tasks,
    session_id text NOT NULL REFERENCES app.harness_sessions,
    tree_hash text NOT NULL,
    state text NOT NULL CHECK (state IN ('prepared','applied','conflict','unknown')),
    receipt jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX harness_sessions_project ON app.harness_sessions(project_id,updated_at DESC);
CREATE TABLE app.harness_project_environments (
    project_id text PRIMARY KEY,
    registration_hash text NOT NULL,
    image_lock text NOT NULL,
    image_id text NOT NULL,
    receipt jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.harness_previews (
    session_id text PRIMARY KEY REFERENCES app.harness_sessions,
    task_id text NOT NULL REFERENCES app.harness_tasks,
    identity jsonb NOT NULL,
    lease jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('prepared','running','closed','unknown')),
    updated_at timestamptz NOT NULL DEFAULT now()
);
