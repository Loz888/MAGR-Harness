-- H11: Engine-owned harness recovery metadata.  Money remains in the existing
-- budgets, authorization_budgets and usage_calls tables.
ALTER TABLE app.usage_calls DROP CONSTRAINT usage_calls_reserved_micros_check;
ALTER TABLE app.usage_calls ADD CONSTRAINT usage_calls_reserved_micros_check
    CHECK (reserved_micros >= 0);

CREATE TABLE app.harness_tasks (
    task_id text PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES app.runs,
    actor text NOT NULL,
    workspace_id text NOT NULL,
    repository text NOT NULL,
    registration jsonb NOT NULL,
    request jsonb NOT NULL,
    grant_scope jsonb NOT NULL,
    limits jsonb NOT NULL,
    identity_hash char(64) NOT NULL,
    profile_hash text NOT NULL REFERENCES app.authorization_budgets,
    deadline_at timestamptz NOT NULL,
    public_status text NOT NULL DEFAULT 'queued',
    tree_hash char(64),
    manifest_hash char(64),
    checkpoint_hash text REFERENCES app.artifacts,
    checkpoint_revision bigint NOT NULL DEFAULT 0 CHECK (checkpoint_revision >= 0),
    inbox jsonb NOT NULL DEFAULT '{"revision":0,"messages":[]}',
    checkpoint_inbox_revision integer NOT NULL DEFAULT 0 CHECK (checkpoint_inbox_revision >= 0),
    result_hash text REFERENCES app.artifacts,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.harness_operations (
    reservation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    operation_id text NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch > 0),
    kind text NOT NULL,
    budget_kind text CHECK (budget_kind IN ('model','tool')),
    request_hash char(64),
    read_only boolean NOT NULL DEFAULT false,
    state text NOT NULL CHECK
        (state IN ('prepared','dispatched','known','unknown','released')),
    intent jsonb NOT NULL DEFAULT '{}',
    receipt jsonb NOT NULL DEFAULT '{}',
    result_hash text REFERENCES app.artifacts,
    model_call_id uuid UNIQUE REFERENCES app.usage_calls,
    reserved_units integer NOT NULL DEFAULT 0 CHECK (reserved_units >= 0),
    actual_units integer NOT NULL DEFAULT 0 CHECK (actual_units >= 0),
    reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,operation_id)
);
CREATE INDEX harness_operations_recovery ON app.harness_operations(run_id,state);

CREATE FUNCTION app.protect_harness_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.task_id,NEW.run_id,NEW.actor,NEW.workspace_id,NEW.repository,
           NEW.registration,NEW.request,NEW.grant_scope,NEW.limits,NEW.identity_hash,
           NEW.profile_hash,NEW.deadline_at)
       IS DISTINCT FROM
       ROW(OLD.task_id,OLD.run_id,OLD.actor,OLD.workspace_id,OLD.repository,
           OLD.registration,OLD.request,OLD.grant_scope,OLD.limits,OLD.identity_hash,
           OLD.profile_hash,OLD.deadline_at) THEN
        RAISE EXCEPTION 'harness task authority is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_harness_identity BEFORE UPDATE ON app.harness_tasks
    FOR EACH ROW EXECUTE FUNCTION app.protect_harness_identity();
