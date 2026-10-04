#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == postgres || "${1:-}" == -* ]] && [[ -z "${JEV_NATIVE_CONFIG_FILE:-}" ]]; then
    endpoint="${SDD_JEV_ENDPOINT:-https://api.typesafe.ai/v1/systemone}"
    key_file="${SDD_JEV_API_KEY_FILE:-/dev/null}"
    registry_file="${SDD_NATIVE_REGISTRY_PASSWORD_FILE:-/dev/null}"
    if [[ "$registry_file" != /dev/null && ! -s "$registry_file" ]]; then
        echo 'The native registry password file must not be empty.' >&2
        exit 1
    fi
    umask 077
    mkdir -p /run/jev
    export JEV_NATIVE_CONFIG_FILE=/run/jev/provider.json
    jq -n \
        --arg endpoint "$endpoint" \
        --arg model "${SDD_JEV_MODEL:-jev-1.13.0}" \
        --arg revision "${SDD_JEV_REVISION:-${SDD_JEV_MODEL:-jev-1.13.0}}" \
        --arg database "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}" \
        --rawfile key "$key_file" \
        --rawfile fallback "${TYPESAFE_API_KEY_FILE:-/dev/null}" \
        --rawfile password "$registry_file" '
        def trim: gsub("^\\s+|\\s+$"; "");
        {endpoint: $endpoint, model: $model, revision: $revision,
         api_key: (if ($key | trim | length) > 0 then ($key | trim)
                   elif $endpoint == "https://api.typesafe.ai/v1/systemone" then ($fallback | trim)
                   else "" end)}
        + if ($password | trim | length) > 0 then
            {registry: {
                dsn: ("postgresql://jev_registry:" + ($password | trim | @uri)
                      + "@127.0.0.1:5432/" + ($database | @uri)),
                max_active: 16, max_daily: 10000, max_age_seconds: 86400,
                connect_timeout_ms: 2000
            }}
          else {} end
    ' > "$JEV_NATIVE_CONFIG_FILE"
    if [[ "$(id -u)" == 0 ]]; then
        chown -R postgres:postgres /run/jev
    fi
fi
exec docker-entrypoint.sh "$@"
