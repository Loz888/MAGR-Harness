-- Engine-owned universal action leases, intents, outbox and audit trail.
CREATE TABLE app.general_action_leases (
    lease_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    task_revision bigint NOT NULL,
    capability text NOT NULL,
    epoch bigint NOT NULL CHECK (epoch > 0),
    grant_snapshot jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY (task_id,task_revision) REFERENCES app.general_task_revisions(task_id,revision),
    UNIQUE (task_id,task_revision,capability,epoch)
);
CREATE UNIQUE INDEX general_action_current_lease
    ON app.general_action_leases (task_id,task_revision,capability) WHERE revoked_at IS NULL;

CREATE TABLE app.general_action_intents (
    action_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    task_revision bigint NOT NULL,
    plan_version bigint NOT NULL CHECK (plan_version > 0),
    project_id text NOT NULL,
    provider_id text NOT NULL,
    execution_mode text NOT NULL CHECK (execution_mode IN ('fixture','live')),
    target_sha256 char(64) NOT NULL,
    idempotency_key text NOT NULL,
    intent_sha256 char(64) NOT NULL,
    intent jsonb NOT NULL,
    lease_id uuid NOT NULL REFERENCES app.general_action_leases,
    required_approval text NOT NULL CHECK (required_approval IN ('none','operator','elevated')),
    approval jsonb,
    receipt jsonb,
    receipt_sha256 char(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id,task_revision) REFERENCES app.general_task_revisions(task_id,revision),
    UNIQUE (project_id,provider_id,execution_mode,idempotency_key),
    CHECK ((receipt IS NULL) = (receipt_sha256 IS NULL))
);

CREATE TABLE app.general_action_outbox (
    action_id uuid PRIMARY KEY REFERENCES app.general_action_intents,
    status text NOT NULL CHECK (status IN (
        'review','ready','dispatching','unknown','succeeded','failed','cancelled'
    )),
    attempt_id uuid UNIQUE,
    attempt_expires_at timestamptz,
    reservation jsonb,
    CHECK ((attempt_id IS NULL) = (reservation IS NULL)),
    CHECK ((attempt_id IS NULL) = (attempt_expires_at IS NULL))
);
CREATE INDEX general_action_outbox_pending
    ON app.general_action_outbox (status,action_id)
    WHERE status IN ('ready','dispatching','unknown');

CREATE TABLE app.general_action_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action_id uuid NOT NULL REFERENCES app.general_action_intents,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX general_action_events_action ON app.general_action_events (action_id,event_id);
