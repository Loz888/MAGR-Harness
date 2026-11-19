-- GA02/GA03. Persist graph CAS, node claims, and routing receipts on the Engine connection.
CREATE TABLE app.general_task_graphs (
    task_id uuid NOT NULL,
    task_revision bigint NOT NULL,
    graph_revision bigint NOT NULL CHECK (graph_revision >= 1),
    version bigint NOT NULL CHECK (version >= 1),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    PRIMARY KEY (task_id,task_revision),
    FOREIGN KEY (task_id,task_revision) REFERENCES app.general_task_revisions(task_id,revision)
);

CREATE TABLE app.general_graph_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id uuid NOT NULL,
    task_revision bigint NOT NULL,
    graph_revision bigint NOT NULL CHECK (graph_revision >= 1),
    version bigint NOT NULL CHECK (version >= 1),
    kind text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id,task_revision) REFERENCES app.general_task_graphs,
    UNIQUE (task_id,task_revision,version)
);
