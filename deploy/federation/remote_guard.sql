-- Experimental acquisition protocol; see docs/REMOTE_POSTGRESQL_PROTOCOL.md.
BEGIN;

CREATE SCHEMA jev_remote;
REVOKE ALL ON SCHEMA jev_remote FROM PUBLIC;

CREATE FUNCTION jev_remote.fence_ddl() RETURNS event_trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog AS $$
BEGIN
    IF pg_is_in_recovery() THEN
        RAISE EXCEPTION 'Remote guards require a writable primary; standby replay bypasses the schema gate';
    END IF;
    PERFORM pg_advisory_xact_lock(1246058067, 1);
END
$$;

CREATE FUNCTION jev_remote.acquire(source regclass)
RETURNS TABLE(contract jsonb, guard jsonb)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    source_name text;
    source_kind "char";
    dependency record;
    dependencies jsonb := '[]';
    columns jsonb;
    constraints jsonb;
    origin jsonb;
    key_hi bigint;
    key_lo bigint;
    token uuid;
    expression_review boolean := false;
BEGIN
    IF pg_is_in_recovery() THEN
        RAISE EXCEPTION 'Remote guards require a writable primary; standby replay bypasses the schema gate';
    END IF;
    IF NOT pg_try_advisory_xact_lock_shared(1246058067, 1) THEN
        RAISE EXCEPTION 'Remote schema maintenance is pending; retry in a new transaction'
            USING ERRCODE='55P03';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_event_trigger WHERE evtname='jev_remote_schema_gate'
          AND evtfoid='jev_remote.fence_ddl()'::regprocedure
          AND evtevent='ddl_command_start' AND evtenabled='A' AND evttags IS NULL
    ) THEN
        RAISE EXCEPTION 'The remote schema gate must be installed and enabled ALWAYS';
    END IF;
    SELECT format('%I.%I', n.nspname, c.relname), c.relkind
      INTO STRICT source_name, source_kind
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
     WHERE c.oid=source;
    IF source_kind NOT IN ('r','p','v','m') THEN
        RAISE EXCEPTION 'Remote sources must be local tables or invoker views';
    END IF;

    -- The guard snapshot is disposable. A second transaction locks, then proves
    -- this guard is still alive before trusting its own snapshot or source data.
    FOR dependency IN
        WITH RECURSIVE relations(oid) AS (
            SELECT source::oid
            UNION
            SELECT child.oid FROM relations parent
            CROSS JOIN LATERAL (
                SELECT d.refobjid AS oid
                  FROM pg_class p JOIN pg_rewrite r ON r.ev_class=p.oid
                  JOIN pg_depend d ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid
                 WHERE p.oid=parent.oid AND p.relkind='v'
                   AND d.refclassid='pg_class'::regclass AND d.refobjid<>p.oid
                UNION
                SELECT i.inhrelid FROM pg_inherits i WHERE i.inhparent=parent.oid
                UNION
                SELECT d.refobjid FROM pg_policy p
                  JOIN pg_depend d ON d.classid='pg_policy'::regclass AND d.objid=p.oid
                 WHERE p.polrelid=parent.oid AND d.refclassid='pg_class'::regclass
                   AND d.refobjid<>parent.oid
            ) child
        )
        SELECT c.oid, c.relkind, n.nspname, c.relname, c.reloptions,
               c.relrowsecurity, c.relforcerowsecurity,
               CASE WHEN c.relkind='v' THEN pg_get_viewdef(c.oid, false) END AS view_sql
          FROM relations JOIN pg_class c USING(oid)
          JOIN pg_namespace n ON n.oid=c.relnamespace ORDER BY c.oid
    LOOP
        IF dependency.nspname LIKE 'pg\_%' ESCAPE '\'
           OR dependency.nspname IN ('information_schema','sdd_catalog','sdd_data','jev','jev_native','jev_remote')
           OR dependency.relkind NOT IN ('r','p','v','m') THEN
            RAISE EXCEPTION 'Remote source dependencies must be user tables or invoker views';
        END IF;
        IF dependency.relkind='v' AND NOT EXISTS (
            SELECT FROM unnest(dependency.reloptions) option
             WHERE option IN ('security_invoker=true','security_invoker=on','security_invoker=1')
        ) THEN
            RAISE EXCEPTION 'Every remote view requires security_invoker=true';
        END IF;
        IF EXISTS (
            SELECT FROM pg_depend d
            LEFT JOIN pg_proc f ON d.refclassid='pg_proc'::regclass AND d.refobjid=f.oid
            LEFT JOIN pg_operator o ON d.refclassid='pg_operator'::regclass AND d.refobjid=o.oid
            WHERE (
                (d.classid='pg_rewrite'::regclass AND d.objid IN
                    (SELECT oid FROM pg_rewrite WHERE ev_class=dependency.oid))
                OR (d.classid='pg_policy'::regclass AND d.objid IN
                    (SELECT oid FROM pg_policy WHERE polrelid=dependency.oid))
            ) AND coalesce(f.pronamespace,o.oprnamespace) <> 'pg_catalog'::regnamespace
        ) THEN
            RAISE EXCEPTION 'Remote views and policies require declared dependencies for user-defined functions or operators';
        END IF;
        dependencies := dependencies || jsonb_build_array(jsonb_build_object(
            'oid', dependency.oid::bigint, 'schema', dependency.nspname,
            'table', dependency.relname, 'kind', dependency.relkind,
            'options', dependency.reloptions,
            'view_sql', dependency.view_sql,
            'policies', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                'name', polname, 'command', polcmd, 'permissive', polpermissive, 'roles', polroles,
                'using', pg_get_expr(polqual,polrelid), 'check', pg_get_expr(polwithcheck,polrelid)
            ) ORDER BY polname),'[]') FROM pg_policy WHERE polrelid=dependency.oid),
            'row_security', dependency.relrowsecurity,
            'force_row_security', dependency.relforcerowsecurity
        ));
        expression_review := expression_review OR dependency.relkind='v'
            OR EXISTS(SELECT FROM pg_policy WHERE polrelid=dependency.oid);
    END LOOP;

    -- Inspect every dependency before the optimizer can evaluate view expressions.
    FOR dependency IN SELECT * FROM jsonb_to_recordset(dependencies)
        AS r(oid bigint, schema text, "table" text, kind text)
    LOOP
        IF dependency.kind='m' THEN
            EXECUTE format('SELECT 1 FROM %I.%I LIMIT 0', dependency.schema, dependency."table");
        ELSE
            EXECUTE format('LOCK TABLE %I.%I IN ACCESS SHARE MODE', dependency.schema, dependency."table");
        END IF;
        IF NOT EXISTS (
            SELECT FROM pg_locks WHERE pid=pg_backend_pid() AND relation=dependency.oid
              AND locktype='relation' AND mode='AccessShareLock' AND granted
        ) THEN
            RAISE EXCEPTION 'Remote source changed while acquiring locks; retry in a new transaction';
        END IF;
    END LOOP;

    SELECT jsonb_agg(jsonb_build_object(
        'name', a.attname, 'position', a.attnum, 'type_oid', a.atttypid::bigint,
        'database_type', format_type(a.atttypid,a.atttypmod), 'modifier', a.atttypmod,
        'collation', a.attcollation::bigint, 'nullable', NOT a.attnotnull,
        'description', col_description(a.attrelid,a.attnum)
    ) ORDER BY a.attnum) INTO columns
      FROM pg_attribute a WHERE a.attrelid=source AND a.attnum>0 AND NOT a.attisdropped;
    SELECT coalesce(jsonb_agg(jsonb_build_object(
        'name', conname, 'kind', contype, 'columns', conkey,
        'target_oid', confrelid::bigint, 'target_columns', confkey,
        'validated', convalidated, 'deferrable', condeferrable
    ) ORDER BY conname), '[]') INTO constraints
      FROM pg_constraint WHERE conrelid=source AND contype IN ('p','f');
    SELECT jsonb_build_object(
        'cluster', system_identifier::text,
        'database', (SELECT oid::bigint FROM pg_database WHERE datname=current_database()),
        'role', (SELECT oid::bigint FROM pg_roles WHERE rolname=current_user)
    ) INTO origin FROM pg_control_system();

    LOOP
        token := gen_random_uuid();
        key_hi := ('x' || substr(replace(token::text,'-',''),1,8))::bit(32)::bigint;
        key_lo := ('x' || substr(replace(token::text,'-',''),9,8))::bit(32)::bigint;
        EXIT WHEN pg_try_advisory_xact_lock(key_hi::bit(32)::integer, key_lo::bit(32)::integer);
    END LOOP;
    contract := jsonb_build_object(
        'protocol', 1, 'origin', origin, 'oid', source::oid::bigint,
        'name', source_name, 'kind', source_kind, 'columns', columns,
        'constraints', constraints, 'relations', dependencies,
        'dependency_state', CASE WHEN expression_review THEN 'UNKNOWN' ELSE 'VALUE' END,
        'description', obj_description(source, 'pg_class')
    );
    guard := jsonb_build_object(
        'pid', pg_backend_pid(), 'key_hi', key_hi, 'key_lo', key_lo,
        'database', origin->'database', 'role', origin->'role'
    );
    RETURN NEXT;
END
$$;

CREATE VIEW jev_remote.active_guards WITH(security_invoker=true, security_barrier=true) AS
SELECT l.pid, l.classid::bigint AS key_hi, l.objid::bigint AS key_lo,
       l.database::bigint AS database, a.usesysid::bigint AS role
  FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
 WHERE l.locktype='advisory' AND l.objsubid=2 AND l.mode='ExclusiveLock' AND l.granted
   AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
   AND a.usesysid=(SELECT oid FROM pg_roles WHERE rolname=current_user);

REVOKE ALL ON FUNCTION jev_remote.acquire(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_remote.fence_ddl() FROM PUBLIC;
REVOKE ALL ON jev_remote.active_guards FROM PUBLIC;

CREATE EVENT TRIGGER jev_remote_schema_gate ON ddl_command_start
EXECUTE FUNCTION jev_remote.fence_ddl();
ALTER EVENT TRIGGER jev_remote_schema_gate ENABLE ALWAYS;

COMMIT;
