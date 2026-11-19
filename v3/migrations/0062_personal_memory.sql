-- GA10 keeps account-owned memory distinct from legacy repository memory.
CREATE TABLE app.personal_memory_items (
    memory_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    user_id uuid NOT NULL REFERENCES app.web_accounts ON DELETE CASCADE,
    project_id text REFERENCES app.harness_managed_projects,
    kind text NOT NULL CHECK (kind IN ('preference','project_fact','experience')),
    subject text NOT NULL,
    current_version bigint NOT NULL CHECK (current_version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (project_id IS NOT NULL OR kind='preference'),
    UNIQUE NULLS NOT DISTINCT (tenant_id,user_id,project_id,kind,subject)
);

CREATE TABLE app.personal_memory_versions (
    memory_id uuid NOT NULL REFERENCES app.personal_memory_items ON DELETE CASCADE,
    version bigint NOT NULL CHECK (version > 0),
    content text NOT NULL,
    source jsonb NOT NULL CHECK (jsonb_typeof(source)='object'),
    recall_policy text NOT NULL CHECK (recall_policy IN ('scope_only','explicit_projects')),
    status text NOT NULL CHECK (status IN ('active','revoked')),
    valid_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id,version)
);

ALTER TABLE app.personal_memory_items
    ADD FOREIGN KEY (memory_id,current_version)
    REFERENCES app.personal_memory_versions(memory_id,version)
    DEFERRABLE INITIALLY DEFERRED;
