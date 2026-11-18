-- H16 accounts, managed project authority and bounded server-side dispatch.
ALTER TABLE app.web_accounts DROP COLUMN singleton;
ALTER TABLE app.web_accounts ADD COLUMN is_owner boolean NOT NULL DEFAULT true;
CREATE UNIQUE INDEX web_accounts_instance_owner
    ON app.web_accounts (is_owner) WHERE is_owner;

CREATE TABLE app.harness_managed_projects (
    workspace_id text PRIMARY KEY,
    registration_hash char(64) NOT NULL,
    max_parallel integer NOT NULL CHECK (max_parallel BETWEEN 1 AND 4),
    max_queued integer NOT NULL CHECK (max_queued BETWEEN 1 AND 64),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.harness_project_memberships (
    membership_id uuid PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES app.harness_managed_projects,
    user_id uuid NOT NULL REFERENCES app.web_accounts(user_id),
    role text NOT NULL CHECK (role IN ('reader','contributor')),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE (membership_id,workspace_id)
);
CREATE UNIQUE INDEX harness_one_active_membership
    ON app.harness_project_memberships(workspace_id,user_id) WHERE revoked_at IS NULL;
CREATE INDEX harness_active_user_projects
    ON app.harness_project_memberships(user_id,workspace_id) WHERE revoked_at IS NULL;

ALTER TABLE app.harness_sessions ADD COLUMN membership_id uuid;
ALTER TABLE app.harness_sessions ADD CONSTRAINT harness_session_member_project
    FOREIGN KEY (membership_id,project_id)
    REFERENCES app.harness_project_memberships (membership_id,workspace_id);
ALTER TABLE app.harness_sessions DROP CONSTRAINT harness_sessions_actor_initial_client_id_key;
CREATE UNIQUE INDEX harness_legacy_client_requests
    ON app.harness_sessions(actor,initial_client_id) WHERE membership_id IS NULL;
CREATE UNIQUE INDEX harness_member_client_requests
    ON app.harness_sessions(membership_id,initial_client_id) WHERE membership_id IS NOT NULL;

CREATE TABLE app.harness_project_credentials (
    workspace_id text NOT NULL REFERENCES app.harness_managed_projects,
    revision bigint GENERATED ALWAYS AS IDENTITY,
    encrypted_payload text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id,revision)
);
CREATE TRIGGER harness_project_credentials_append_only
    BEFORE UPDATE OR DELETE ON app.harness_project_credentials
    FOR EACH ROW EXECUTE FUNCTION app.web_settings_immutable();

ALTER TABLE app.harness_tasks ADD COLUMN managed_workspace_id text
    REFERENCES app.harness_managed_projects(workspace_id);
ALTER TABLE app.harness_tasks ADD COLUMN resume_requested_at timestamptz;
ALTER TABLE app.harness_tasks ADD CONSTRAINT harness_managed_workspace_matches
    CHECK (managed_workspace_id IS NULL OR managed_workspace_id = workspace_id);
CREATE INDEX harness_managed_dispatch
    ON app.harness_tasks(managed_workspace_id,created_at,task_id)
    WHERE managed_workspace_id IS NOT NULL;
