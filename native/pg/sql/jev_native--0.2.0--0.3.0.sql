CREATE FUNCTION jev_native.scan_mapped(
    source_sql text,
    questions jsonb,
    context_columns jsonb DEFAULT '{}'::jsonb,
    options jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(window_ordinal bigint, rows jsonb, evidence jsonb, usage jsonb, policy jsonb)
LANGUAGE c VOLATILE
AS 'MODULE_PATHNAME', 'scan_mapped_wrapper';

REVOKE ALL ON FUNCTION jev_native.scan_mapped(text,jsonb,jsonb,jsonb) FROM PUBLIC;
COMMENT ON FUNCTION jev_native.scan_mapped(text,jsonb,jsonb,jsonb) IS
'Invoker-rights semantic dictionaries with independent request packing and cross-window reuse. Returns bounded row windows with shared observations, per-question bindings and operational receipts.';
