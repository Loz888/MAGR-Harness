-- GA01. General task intake, revision-bound plans, and independently verifiable outcomes.
CREATE TABLE app.general_tasks (
    task_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    membership_id uuid,
    request_id uuid NOT NULL,
    request_hash char(64) NOT NULL,
    current_revision bigint NOT NULL CHECK (current_revision >= 1),
    UNIQUE NULLS NOT DISTINCT (project_id,membership_id,request_id),
    FOREIGN KEY (membership_id,project_id)
        REFERENCES app.harness_project_memberships(membership_id,workspace_id)
);

CREATE TABLE app.general_task_revisions (
    task_id uuid NOT NULL REFERENCES app.general_tasks,
    revision bigint NOT NULL CHECK (revision >= 1),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    spec jsonb NOT NULL CHECK (jsonb_typeof(spec) = 'object'),
    state text NOT NULL CHECK (state IN (
        'draft','ready','running','waiting','blocked','verifying','completed','failed','cancelled'
    )),
    reason text,
    plan jsonb,
    plan_version bigint CHECK (plan_version BETWEEN 1 AND version),
    outcome jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id,revision),
    CHECK ((plan IS NULL) = (plan_version IS NULL)),
    CHECK ((outcome IS NOT NULL) = (state = 'completed')),
    CHECK (state NOT IN ('running','verifying','completed') OR plan IS NOT NULL)
);

ALTER TABLE app.general_tasks ADD FOREIGN KEY (task_id,current_revision)
    REFERENCES app.general_task_revisions(task_id,revision)
    DEFERRABLE INITIALLY DEFERRED;
