-- GA05 task-bound verification plans, observations and attestations.
CREATE TABLE app.general_verification_plans (
    verification_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    task_revision bigint NOT NULL,
    task_version bigint NOT NULL,
    plan_version bigint NOT NULL,
    plan_sha256 char(64) NOT NULL,
    plan jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id,task_revision) REFERENCES app.general_task_revisions(task_id,revision),
    UNIQUE(task_id,task_revision,task_version)
);

CREATE TABLE app.general_verification_evidence (
    verification_id uuid NOT NULL REFERENCES app.general_verification_plans,
    evidence_version bigint GENERATED ALWAYS AS IDENTITY,
    step_id text NOT NULL,
    observation jsonb NOT NULL,
    PRIMARY KEY(verification_id,evidence_version)
);
CREATE INDEX general_verification_latest_evidence
    ON app.general_verification_evidence(verification_id,step_id,evidence_version DESC);

CREATE TABLE app.general_verification_reports (
    report_id uuid PRIMARY KEY,
    verification_id uuid NOT NULL REFERENCES app.general_verification_plans,
    outcome_sha256 char(64),
    result_sha256 char(64) NOT NULL,
    evidence_versions jsonb NOT NULL,
    report jsonb NOT NULL,
    confirmed_by text,
    confirmed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((confirmed_by IS NULL)=(confirmed_at IS NULL))
);
CREATE INDEX general_verification_outcome
    ON app.general_verification_reports(verification_id,outcome_sha256) WHERE outcome_sha256 IS NOT NULL;
