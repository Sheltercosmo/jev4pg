CREATE TABLE jev.client_roles (
    login name PRIMARY KEY,
    tenant text NOT NULL CHECK (length(tenant) BETWEEN 1 AND 100),
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    access text NOT NULL CHECK (access IN ('reader', 'reviewer'))
);

CREATE TABLE jev.jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    login name NOT NULL,
    tenant text NOT NULL,
    request jsonb NOT NULL,
    idempotency_key text,
    state text NOT NULL DEFAULT 'QUEUED'
        CHECK (state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    worker uuid,
    lease_until timestamptz,
    response jsonb,
    UNIQUE (login, idempotency_key)
);
CREATE INDEX jobs_queued ON jev.jobs(created_at) WHERE state = 'QUEUED';
CREATE INDEX jobs_active_tenant ON jev.jobs(tenant) WHERE state IN ('QUEUED', 'RUNNING');

SELECT pg_catalog.pg_extension_config_dump('jev.client_roles', '');
SELECT pg_catalog.pg_extension_config_dump('jev.jobs', '');

CREATE FUNCTION jev.submit(
    operator text, arguments jsonb DEFAULT '{}', limits jsonb DEFAULT '{}',
    policy jsonb DEFAULT '{}', idempotency_key text DEFAULT NULL, approval_id text DEFAULT NULL
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    principal jev.client_roles;
    body jsonb;
    existing jev.jobs;
    job_id uuid;
BEGIN
    SELECT * INTO principal FROM jev.client_roles WHERE login = session_user;
    IF NOT FOUND THEN RAISE EXCEPTION 'JEV access is not configured for this login' USING ERRCODE='42501'; END IF;
    IF operator IS NULL OR length(operator) NOT BETWEEN 1 AND 100
       OR jsonb_typeof(arguments) IS DISTINCT FROM 'object'
       OR jsonb_typeof(limits) IS DISTINCT FROM 'object'
       OR jsonb_typeof(policy) IS DISTINCT FROM 'object'
       OR length(idempotency_key) > 200 OR length(approval_id) > 200 THEN
        RAISE EXCEPTION 'Invalid JEV request' USING ERRCODE='22023';
    END IF;
    body := jsonb_build_object('operator', operator, 'arguments', arguments,
        'limits', limits, 'policy', policy, 'approval_id', approval_id);
    IF octet_length(body::text) > 524288 THEN
        RAISE EXCEPTION 'JEV request exceeds 512 KiB' USING ERRCODE='22023';
    END IF;
    PERFORM pg_advisory_xact_lock(1747654245, hashtext(principal.tenant));
    SELECT * INTO existing FROM jev.jobs j
        WHERE j.login = session_user AND j.idempotency_key = submit.idempotency_key;
    IF FOUND THEN
        IF existing.request <> body OR existing.tenant <> principal.tenant THEN
            RAISE EXCEPTION 'Idempotency key already belongs to a different request' USING ERRCODE='22023';
        END IF;
        RETURN existing.id;
    END IF;
    IF (SELECT count(*) FROM jev.jobs WHERE tenant = principal.tenant AND state IN ('QUEUED', 'RUNNING')) >= 100 THEN
        RAISE EXCEPTION 'Tenant queue is full; retry after current jobs finish' USING ERRCODE='54000';
    END IF;
    INSERT INTO jev.jobs(login, tenant, request, idempotency_key)
        VALUES(session_user, principal.tenant, body, submit.idempotency_key) RETURNING id INTO job_id;
    RETURN job_id;
END $$;

CREATE FUNCTION jev.result(job_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE job jev.jobs;
BEGIN
    SELECT j.* INTO job FROM jev.jobs j JOIN jev.client_roles p
        ON p.login = session_user AND p.tenant = j.tenant
        WHERE j.id = job_id AND j.login = session_user;
    IF NOT FOUND THEN RAISE EXCEPTION 'JEV job not found or access denied' USING ERRCODE='42501'; END IF;
    RETURN coalesce(job.response, jsonb_build_object(
        'output_state', 'NOT_EVALUATED', 'operation_state', job.state, 'value', NULL))
        || jsonb_build_object('job_id', job.id, 'job_state', job.state,
            'created_at', job.created_at, 'started_at', job.started_at, 'finished_at', job.finished_at);
END $$;

CREATE FUNCTION jev.cancel(job_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE changed integer;
BEGIN
    PERFORM jev.result(job_id);
    UPDATE jev.jobs SET state='CANCELLED', finished_at=clock_timestamp(),
        response=jsonb_build_object('output_state', 'NOT_EVALUATED', 'operation_state', 'CANCELLED', 'value', NULL)
        WHERE id=job_id AND state='QUEUED';
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END $$;

CREATE FUNCTION jev._claim(worker_id uuid, lease_seconds integer DEFAULT 60) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE job jev.jobs; principal jev.client_roles;
BEGIN
    IF worker_id IS NULL OR lease_seconds IS NULL OR lease_seconds NOT BETWEEN 10 AND 3600 THEN
        RAISE EXCEPTION 'Invalid worker lease';
    END IF;
    UPDATE jev.jobs SET state='FAILED', finished_at=clock_timestamp(),
        response=jsonb_build_object('output_state', 'NOT_EVALUATED', 'operation_state', 'FAILED',
            'value', NULL, 'reason', 'Worker lease expired; inspect operator records before resubmitting')
        WHERE state='RUNNING' AND lease_until < clock_timestamp();
    SELECT * INTO job FROM jev.jobs WHERE state='QUEUED'
        ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO principal FROM jev.client_roles WHERE login=job.login AND tenant=job.tenant;
    IF NOT FOUND THEN
        UPDATE jev.jobs SET state='FAILED', finished_at=clock_timestamp(),
            response=jsonb_build_object('output_state', 'NOT_EVALUATED', 'operation_state', 'FAILED',
                'value', NULL, 'reason', 'SQL client access was revoked') WHERE id=job.id;
        RETURN NULL;
    END IF;
    UPDATE jev.jobs SET state='RUNNING', worker=worker_id, started_at=clock_timestamp(),
        lease_until=clock_timestamp() + make_interval(secs=>lease_seconds) WHERE id=job.id;
    RETURN jsonb_build_object('id', job.id, 'tenant', job.tenant,
        'actor', principal.actor, 'role', principal.access, 'request', job.request);
END $$;

CREATE FUNCTION jev._heartbeat(job_id uuid, worker_id uuid, lease_seconds integer DEFAULT 60) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE changed integer;
BEGIN
    IF lease_seconds IS NULL OR lease_seconds NOT BETWEEN 10 AND 3600 THEN RAISE EXCEPTION 'Invalid worker lease'; END IF;
    UPDATE jev.jobs SET lease_until=clock_timestamp() + make_interval(secs=>lease_seconds)
        WHERE id=job_id AND worker=worker_id AND state='RUNNING' AND lease_until > clock_timestamp();
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END $$;

CREATE FUNCTION jev._finish(job_id uuid, worker_id uuid, result jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE changed integer;
BEGIN
    IF jsonb_typeof(result) IS DISTINCT FROM 'object'
        OR coalesce(result->>'output_state', '') NOT IN ('VALUE', 'UNKNOWN', 'NOT_EVALUATED')
        OR result->>'operation_state' IS NULL THEN RAISE EXCEPTION 'Invalid operator result'; END IF;
    UPDATE jev.jobs SET state=CASE WHEN result->>'operation_state'='FAILED' THEN 'FAILED' ELSE 'COMPLETED' END,
        response=result, finished_at=clock_timestamp(), lease_until=NULL
        WHERE id=job_id AND worker=worker_id AND state='RUNNING' AND lease_until > clock_timestamp();
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed = 1;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA jev FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA jev FROM PUBLIC;
REVOKE ALL ON SCHEMA jev FROM PUBLIC;
