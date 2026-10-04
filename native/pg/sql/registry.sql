CREATE TABLE jev_native.evidence (
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    id uuid PRIMARY KEY,
    scope text NOT NULL,
    identity text NOT NULL,
    observation jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX evidence_lookup ON jev_native.evidence (scope, identity, sequence DESC);

CREATE FUNCTION jev_native._immutable_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Native observations are immutable; publish a new revision'; END $$;
CREATE TRIGGER immutable_evidence BEFORE UPDATE ON jev_native.evidence
    FOR EACH ROW EXECUTE FUNCTION jev_native._immutable_evidence();

CREATE TABLE jev_native.request_pools (
    identity text PRIMARY KEY,
    active integer NOT NULL DEFAULT 0 CHECK (active >= 0)
);
CREATE TABLE jev_native.request_allowances (
    scope text NOT NULL,
    pool text NOT NULL,
    day date NOT NULL,
    requests integer NOT NULL DEFAULT 0 CHECK (requests >= 0),
    PRIMARY KEY (scope, pool, day)
);
CREATE TABLE jev_native.request_attempts (
    sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    id uuid PRIMARY KEY,
    scope text NOT NULL,
    identity text NOT NULL,
    pool text NOT NULL REFERENCES jev_native.request_pools(identity),
    state text NOT NULL CHECK (state IN ('DISPATCHING','SUCCEEDED','FAILED','UNCERTAIN','RETRY_ALLOWED','CLOSED')),
    deadline timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    error text
);
CREATE UNIQUE INDEX request_current ON jev_native.request_attempts(scope, identity)
    WHERE state IN ('DISPATCHING','UNCERTAIN');
CREATE INDEX request_latest ON jev_native.request_attempts(scope, identity, sequence DESC);

CREATE FUNCTION jev_native._registry_lookup(scope_key text, identities text[], max_age_seconds integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, jev_native AS $$
DECLARE
    saved record;
    result jsonb := '[]';
    bytes integer := 0;
BEGIN
    IF scope_key IS NULL OR length(scope_key) NOT BETWEEN 1 AND 4096
       OR identities IS NULL OR cardinality(identities) NOT BETWEEN 1 AND 32
       OR max_age_seconds IS NULL OR max_age_seconds NOT BETWEEN 0 AND 31536000 THEN
        RAISE EXCEPTION 'Invalid native evidence lookup';
    END IF;
    FOR saved IN
        SELECT DISTINCT ON (identity) id, identity, observation FROM jev_native.evidence
          WHERE scope=scope_key AND identity=ANY(identities)
            AND created_at >= clock_timestamp() - make_interval(secs => max_age_seconds)
          ORDER BY identity, sequence DESC
    LOOP
        bytes := bytes + octet_length(saved.observation::text);
        IF bytes > 8000000 THEN RAISE EXCEPTION 'Evidence batch exceeds 8 MB; reduce batch_rows'; END IF;
        result := result || jsonb_build_array(jsonb_build_object(
          'identity',saved.identity,'attempt',saved.id,'observation',saved.observation));
    END LOOP;
    RETURN result;
END $$;

CREATE FUNCTION jev_native._registry_claim(
    scope_key text, evidence_key text, pool_key text, max_active integer,
    max_daily integer, deadline_ms integer, max_age_seconds integer, refresh boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, jev_native AS $$
DECLARE
    saved jev_native.evidence%ROWTYPE;
    prior jev_native.request_attempts%ROWTYPE;
    active_count integer;
    charged integer;
    attempt uuid;
    today date := (clock_timestamp() AT TIME ZONE 'UTC')::date;
BEGIN
    IF length(scope_key) NOT BETWEEN 1 AND 4096 OR evidence_key !~ '^[a-f0-9]{64}$'
       OR pool_key !~ '^[a-f0-9]{64}$' OR max_active NOT BETWEEN 1 AND 128
       OR max_daily NOT BETWEEN 0 AND 1000000 OR deadline_ms NOT BETWEEN 1000 AND 180000
       OR max_age_seconds NOT BETWEEN 0 AND 31536000
       OR scope_key IS NULL OR evidence_key IS NULL OR pool_key IS NULL
       OR max_active IS NULL OR max_daily IS NULL OR deadline_ms IS NULL
       OR max_age_seconds IS NULL OR refresh IS NULL THEN
        RAISE EXCEPTION 'Invalid native coordination request';
    END IF;
    IF NOT refresh THEN
        SELECT * INTO saved FROM jev_native.evidence
          WHERE scope=scope_key AND identity=evidence_key
            AND created_at >= clock_timestamp() - make_interval(secs => max_age_seconds)
          ORDER BY sequence DESC LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object('state','READY','attempt',saved.id,'observation',saved.observation);
        END IF;
    END IF;

    INSERT INTO jev_native.request_pools(identity) VALUES(pool_key) ON CONFLICT DO NOTHING;
    SELECT active INTO active_count FROM jev_native.request_pools WHERE identity=pool_key FOR UPDATE;
    -- The pool lock covers admission only. No transaction remains open during provider I/O.
    SELECT * INTO prior FROM jev_native.request_attempts
      WHERE scope=scope_key AND identity=evidence_key ORDER BY sequence DESC LIMIT 1;
    IF FOUND THEN
        IF prior.state='DISPATCHING' AND prior.deadline < clock_timestamp() THEN
            UPDATE jev_native.request_attempts SET state='UNCERTAIN', error='Dispatch deadline expired'
              WHERE id=prior.id;
            prior.state := 'UNCERTAIN';
        END IF;
        IF prior.state IN ('DISPATCHING','UNCERTAIN','FAILED','CLOSED') THEN
            RETURN jsonb_build_object('state',prior.state,'attempt',prior.id);
        END IF;
        IF prior.state='SUCCEEDED' AND NOT refresh THEN
            SELECT * INTO saved FROM jev_native.evidence WHERE id=prior.id
              AND created_at >= clock_timestamp() - make_interval(secs => max_age_seconds);
            IF FOUND THEN
                RETURN jsonb_build_object('state','READY','attempt',saved.id,'observation',saved.observation);
            END IF;
        END IF;
    END IF;
    IF active_count >= max_active THEN
        RETURN jsonb_build_object('state','SATURATED');
    END IF;
    INSERT INTO jev_native.request_allowances(scope,pool,day) VALUES(scope_key,pool_key,today)
      ON CONFLICT DO NOTHING;
    SELECT requests INTO charged FROM jev_native.request_allowances
      WHERE scope=scope_key AND pool=pool_key AND day=today FOR UPDATE;
    IF charged >= max_daily THEN
        RETURN jsonb_build_object('state','BLOCKED_BY_BUDGET');
    END IF;
    attempt := gen_random_uuid();
    INSERT INTO jev_native.request_attempts(id,scope,identity,pool,state,deadline)
      VALUES(attempt,scope_key,evidence_key,pool_key,'DISPATCHING',
             clock_timestamp() + make_interval(secs => deadline_ms / 1000.0));
    UPDATE jev_native.request_pools SET active=active+1 WHERE identity=pool_key;
    UPDATE jev_native.request_allowances SET requests=requests+1
      WHERE scope=scope_key AND pool=pool_key AND day=today;
    RETURN jsonb_build_object('state','CLAIMED','attempt',attempt);
END $$;

CREATE FUNCTION jev_native._registry_finish(attempt_id uuid, observation jsonb, failure text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, jev_native AS $$
DECLARE
    prior jev_native.request_attempts%ROWTYPE;
BEGIN
    IF (observation IS NULL) = (failure IS NULL) OR failure NOT IN ('FAILED','UNCERTAIN') THEN
        RAISE EXCEPTION 'Supply either a valid observation or an explicit failure state';
    END IF;
    SELECT * INTO prior FROM jev_native.request_attempts WHERE id=attempt_id;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM jev_native.request_pools WHERE identity=prior.pool FOR UPDATE;
    SELECT * INTO prior FROM jev_native.request_attempts WHERE id=attempt_id FOR UPDATE;
    IF prior.state='SUCCEEDED' AND observation IS NOT NULL THEN
        RETURN EXISTS (SELECT 1 FROM jev_native.evidence e WHERE e.id=attempt_id AND e.observation=_registry_finish.observation);
    END IF;
    IF prior.state='FAILED' AND failure='FAILED' THEN RETURN true; END IF;
    IF prior.state NOT IN ('DISPATCHING','UNCERTAIN') THEN RETURN false; END IF;
    IF observation IS NOT NULL THEN
        INSERT INTO jev_native.evidence(id,scope,identity,observation)
          VALUES(prior.id,prior.scope,prior.identity,observation);
    END IF;
    UPDATE jev_native.request_attempts
      SET state=CASE WHEN observation IS NOT NULL THEN 'SUCCEEDED' ELSE failure END,
          finished_at=CASE WHEN failure='UNCERTAIN' THEN NULL ELSE clock_timestamp() END,
          error=CASE WHEN failure IS NOT NULL THEN 'No validated provider observation' END
      WHERE id=attempt_id;
    IF failure IS DISTINCT FROM 'UNCERTAIN' THEN
        UPDATE jev_native.request_pools SET active=active-1 WHERE identity=prior.pool;
    END IF;
    RETURN true;
END $$;

CREATE FUNCTION jev_native.reconcile_attempt(attempt_id uuid, resolution text, reason text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, jev_native AS $$
DECLARE
    prior jev_native.request_attempts%ROWTYPE;
BEGIN
    IF resolution NOT IN ('RETRY_ALLOWED','CLOSED') OR resolution IS NULL
       OR reason IS NULL OR length(trim(reason)) NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'Choose RETRY_ALLOWED or CLOSED and record a reconciliation reason';
    END IF;
    SELECT * INTO prior FROM jev_native.request_attempts WHERE id=attempt_id;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM jev_native.request_pools WHERE identity=prior.pool FOR UPDATE;
    SELECT * INTO prior FROM jev_native.request_attempts WHERE id=attempt_id FOR UPDATE;
    IF prior.state NOT IN ('DISPATCHING','UNCERTAIN','FAILED') THEN RETURN false; END IF;
    IF prior.state IN ('DISPATCHING','UNCERTAIN') THEN
        UPDATE jev_native.request_pools SET active=active-1 WHERE identity=prior.pool;
    END IF;
    UPDATE jev_native.request_attempts SET state=resolution, error=reason, finished_at=clock_timestamp()
      WHERE id=attempt_id;
    RETURN true;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA jev_native FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA jev_native FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_native._immutable_evidence() FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_native._registry_lookup(text,text[],integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_native._registry_claim(text,text,text,integer,integer,integer,integer,boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_native._registry_finish(uuid,jsonb,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_native.reconcile_attempt(uuid,text,text) FROM PUBLIC;
