-- Personal tasks use an authenticated account even before a project is linked.
ALTER TABLE app.general_tasks ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE app.general_tasks ADD COLUMN owner_user_id uuid REFERENCES app.web_accounts(user_id);
ALTER TABLE app.general_tasks ADD CONSTRAINT general_task_scope_check CHECK (
    project_id IS NOT NULL OR (owner_user_id IS NOT NULL AND membership_id IS NULL)
);

ALTER TABLE app.general_tasks DROP CONSTRAINT general_tasks_project_id_membership_id_request_id_key;
CREATE UNIQUE INDEX general_project_request_unique ON app.general_tasks
    (project_id,membership_id,request_id) NULLS NOT DISTINCT WHERE owner_user_id IS NULL;
CREATE UNIQUE INDEX general_personal_request_unique ON app.general_tasks
    (owner_user_id,request_id) WHERE owner_user_id IS NOT NULL;
