CREATE FUNCTION jev_native.embedding_distances(query jsonb, candidates jsonb)
RETURNS TABLE(ordinal bigint, distance double precision)
LANGUAGE c IMMUTABLE PARALLEL SAFE
AS 'MODULE_PATHNAME', 'embedding_distances_wrapper';

COMMENT ON FUNCTION jev_native.embedding_distances(jsonb,jsonb) IS
'Compare one query with up to 512 candidate matrices locally, preparing the query once. Ordinals are one-based and preserve candidate order.';
