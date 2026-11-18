-- Harness-owned. 0044 remains reserved for the separately agreed SA12 scope.
-- This additive action/outbox journal uses the original Engine's locked
-- connection. It does not change Workbench's Draft-PR-only contract.
CREATE TABLE app.harness_github_authorizations (
    project_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    actor text NOT NULL,
    registration_hash char(64) NOT NULL,
    repository text NOT NULL,
    base_branch text NOT NULL,
    allow_publish boolean NOT NULL,
    allow_merge boolean NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    PRIMARY KEY(project_id, revision)
);
CREATE TABLE app.harness_github_actions (
    publication_id uuid PRIMARY KEY,
    parent_id uuid REFERENCES app.harness_github_actions,
    session_id text NOT NULL REFERENCES app.harness_sessions,
    task_id text NOT NULL REFERENCES app.harness_tasks,
    project_id text NOT NULL,
    actor text NOT NULL,
    action_type text NOT NULL CHECK(action_type IN ('draft_pr','merge_pr')),
    execution_mode text NOT NULL CHECK(execution_mode IN ('fixture','live')),
    payload_hash char(64) NOT NULL,
    bundle jsonb NOT NULL,
    approval jsonb,
    grant_revision bigint,
    status text NOT NULL CHECK(status IN (
        'review','ready','dispatching','succeeded','failed','outcome_unknown','cancelled'
    )),
    reason_code text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    external_id text,
    pull_request jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(task_id,action_type,payload_hash)
);
CREATE TABLE app.harness_github_outbox (
    publication_id uuid PRIMARY KEY REFERENCES app.harness_github_actions,
    status text NOT NULL CHECK(status IN (
        'ready','dispatching','succeeded','failed','outcome_unknown','cancelled'
    )),
    lease_token uuid,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.harness_github_steps (
    publication_id uuid NOT NULL REFERENCES app.harness_github_actions,
    ordinal integer NOT NULL CHECK(ordinal BETWEEN 0 AND 2),
    kind text NOT NULL CHECK(kind IN ('branch','commit','pull_request','merge')),
    status text NOT NULL CHECK(status IN ('dispatching','succeeded','rejected','outcome_unknown')),
    attempt integer NOT NULL DEFAULT 1 CHECK(attempt > 0),
    request_hash char(64) NOT NULL,
    receipt jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(publication_id,ordinal)
);
CREATE TABLE app.harness_github_step_attempts (
    publication_id uuid NOT NULL,
    ordinal integer NOT NULL,
    attempt integer NOT NULL CHECK(attempt > 0),
    request_hash char(64) NOT NULL,
    status text NOT NULL CHECK(status IN ('dispatching','succeeded','rejected','outcome_unknown')),
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(publication_id,ordinal,attempt),
    FOREIGN KEY(publication_id,ordinal) REFERENCES app.harness_github_steps
);
CREATE INDEX harness_github_actions_session
    ON app.harness_github_actions(session_id,created_at DESC);
