-- H12 (0037; SA reserves 0030-0036): immutable experiment metadata references
-- the existing money authority.  The committed H11 0030 remains unchanged.
-- Only H12's shared authorization may exceed the historical 100-call ceiling.
ALTER TABLE app.authorization_budgets ADD COLUMN budget_scope text NOT NULL DEFAULT 'legacy'
    CHECK (budget_scope IN ('legacy','harness-h12'));
ALTER TABLE app.authorization_budgets DROP CONSTRAINT authorization_budgets_call_limit_check;
ALTER TABLE app.authorization_budgets ADD CONSTRAINT authorization_budgets_call_limit_check
    CHECK (call_limit BETWEEN 1 AND 1536 AND (call_limit<=100 OR budget_scope='harness-h12'));
ALTER TABLE app.authorization_budgets ADD CONSTRAINT harness_h12_authorization_cap
    CHECK (budget_scope<>'harness-h12' OR (limit_micros=12000000 AND call_limit=1536));

CREATE FUNCTION app.protect_h12_budget() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (OLD.budget_scope='harness-h12' OR NEW.budget_scope='harness-h12') AND
       ROW(NEW.profile_hash,NEW.budget_scope,NEW.limit_micros,NEW.call_limit) IS DISTINCT FROM
       ROW(OLD.profile_hash,OLD.budget_scope,OLD.limit_micros,OLD.call_limit) THEN
        RAISE EXCEPTION 'H12 shared authorization is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_h12_budget BEFORE UPDATE ON app.authorization_budgets
    FOR EACH ROW EXECUTE FUNCTION app.protect_h12_budget();

CREATE TABLE app.harness_experiments (
    experiment_id text PRIMARY KEY,
    project_id text NOT NULL,
    actor text NOT NULL,
    config jsonb NOT NULL,
    config_hash char(64) NOT NULL,
    approval_document jsonb NOT NULL,
    authorization_hash char(64) NOT NULL,
    profile_hash text NOT NULL REFERENCES app.authorization_budgets,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION app.protect_harness_experiment() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'harness experiment is immutable';
END $$;
CREATE TRIGGER immutable_harness_experiment BEFORE UPDATE ON app.harness_experiments
    FOR EACH ROW EXECUTE FUNCTION app.protect_harness_experiment();

ALTER TABLE app.harness_tasks ADD COLUMN experiment_id text REFERENCES app.harness_experiments;
ALTER TABLE app.harness_tasks ADD COLUMN evaluation_profile jsonb;
ALTER TABLE app.harness_tasks ADD COLUMN sealed_tree_hash char(64);
ALTER TABLE app.harness_tasks ADD COLUMN sealed_at timestamptz;
ALTER TABLE app.harness_tasks ADD CONSTRAINT harness_evaluation_binding
    CHECK ((experiment_id IS NULL)=(evaluation_profile IS NULL));
ALTER TABLE app.harness_tasks ADD CONSTRAINT harness_evaluation_seal
    CHECK ((sealed_tree_hash IS NULL)=(sealed_at IS NULL));
CREATE FUNCTION app.protect_harness_evaluation_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.experiment_id,NEW.evaluation_profile) IS DISTINCT FROM
       ROW(OLD.experiment_id,OLD.evaluation_profile) THEN
        RAISE EXCEPTION 'harness evaluation binding is immutable';
    END IF;
    IF OLD.sealed_tree_hash IS NOT NULL AND
       ROW(NEW.sealed_tree_hash,NEW.sealed_at,NEW.tree_hash) IS DISTINCT FROM
       ROW(OLD.sealed_tree_hash,OLD.sealed_at,OLD.tree_hash) THEN
        RAISE EXCEPTION 'sealed evaluation tree is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_harness_evaluation_binding BEFORE UPDATE ON app.harness_tasks
    FOR EACH ROW EXECUTE FUNCTION app.protect_harness_evaluation_binding();
