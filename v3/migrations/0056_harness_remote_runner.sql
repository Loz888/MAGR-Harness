-- H15 delivery metadata. The existing Harness Run owns epoch, status and receipts.
CREATE TABLE app.harness_runner_devices (
    device_id uuid PRIMARY KEY,
    actor text NOT NULL,
    name text NOT NULL,
    credential_hash char(64) NOT NULL UNIQUE,
    online boolean NOT NULL DEFAULT true,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.harness_runner_pairings (
    code_hash char(64) PRIMARY KEY,
    actor text NOT NULL,
    expires_at timestamptz NOT NULL,
    device_id uuid REFERENCES app.harness_runner_devices,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.harness_runner_workspaces (
    workspace_id uuid PRIMARY KEY,
    device_id uuid NOT NULL REFERENCES app.harness_runner_devices,
    name text NOT NULL,
    project_kind text NOT NULL CHECK (project_kind IN ('python','node','go')),
    plan_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, device_id)
);

CREATE TABLE app.harness_runner_jobs (
    task_id text PRIMARY KEY REFERENCES app.harness_tasks,
    device_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    client_id uuid NOT NULL UNIQUE,
    dispatchable boolean NOT NULL DEFAULT true,
    FOREIGN KEY (workspace_id,device_id)
        REFERENCES app.harness_runner_workspaces(workspace_id,device_id),
    UNIQUE (task_id,device_id)
);
CREATE INDEX harness_runner_jobs_queue ON app.harness_runner_jobs(device_id,task_id)
    WHERE dispatchable;

CREATE TABLE app.harness_runner_claims (
    task_id text NOT NULL,
    device_id uuid NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch > 0),
    ticket_hash char(64) NOT NULL,
    phase text NOT NULL CHECK (phase IN ('claimed','running','finished','released','fenced')),
    expires_at timestamptz NOT NULL,
    started_at timestamptz,
    receipt_hash char(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id,execution_epoch),
    UNIQUE (device_id,ticket_hash),
    FOREIGN KEY (task_id,device_id) REFERENCES app.harness_runner_jobs(task_id,device_id),
    CHECK ((phase = 'finished') = (receipt_hash IS NOT NULL))
);
CREATE UNIQUE INDEX harness_runner_one_active_claim ON app.harness_runner_claims(device_id)
    WHERE phase IN ('claimed','running');

CREATE TABLE app.harness_runner_events (
    event_id bigserial PRIMARY KEY,
    device_id uuid REFERENCES app.harness_runner_devices,
    task_id text REFERENCES app.harness_runner_jobs,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
