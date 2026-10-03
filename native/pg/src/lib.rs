mod execution;

use execution::{SemanticScan, SourceSpec};
use jev_executor::Decision;
use jev_executor::evidence::{Observation, Policy};
use pgrx::JsonB;
use pgrx::prelude::*;

pgrx::pg_module_magic!(name, version);

#[pg_extern]
fn scan(
    source_sql: Option<&str>,
    questions: Option<JsonB>,
    options: default!(Option<JsonB>, "'{}'::jsonb"),
    fcinfo: pg_sys::FunctionCallInfo,
) -> TableIterator<
    'static,
    (
        name!(ordinal, i64),
        name!(source, JsonB),
        name!(decisions, JsonB),
        name!(observation, Option<JsonB>),
        name!(usage, JsonB),
        name!(receipt, Option<JsonB>),
    ),
> {
    let source_sql = source_sql.unwrap_or_else(|| error!("Source SELECT is required"));
    let questions = questions.unwrap_or_else(|| error!("Typed questions are required"));
    let options = options.unwrap_or_else(|| error!("Execution options are required"));
    let source = SourceSpec::new("source".into(), source_sql.into(), questions.0);
    TableIterator::new(SemanticScan::new(vec![source], options.0, fcinfo).map(
        |(_, ordinal, source, decisions, observation, usage, receipt)| {
            (ordinal, source, decisions, observation, usage, receipt)
        },
    ))
}

#[pg_extern]
fn scan_many(
    sources: Option<JsonB>,
    options: default!(Option<JsonB>, "'{}'::jsonb"),
    fcinfo: pg_sys::FunctionCallInfo,
) -> TableIterator<
    'static,
    (
        name!(source_id, String),
        name!(ordinal, i64),
        name!(source, JsonB),
        name!(decisions, JsonB),
        name!(observation, Option<JsonB>),
        name!(usage, JsonB),
        name!(receipt, Option<JsonB>),
    ),
> {
    let sources = sources.unwrap_or_else(|| error!("Independent source declarations are required"));
    let options = options.unwrap_or_else(|| error!("Execution options are required"));
    TableIterator::new(SemanticScan::new(
        execution::sources(sources.0),
        options.0,
        fcinfo,
    ))
}

#[pg_extern(immutable, parallel_safe)]
fn decide(
    source: Option<JsonB>,
    observation: Option<JsonB>,
    policy: default!(Option<JsonB>, "'{}'::jsonb"),
) -> JsonB {
    let source = source.unwrap_or_else(|| error!("Source context is required"));
    let observation = observation
        .unwrap_or_else(|| error!("Observation is absent; the operation was not evaluated"));
    let policy = policy.unwrap_or_else(|| error!("Decision policy is required"));
    let observation: Observation = serde_json::from_value(observation.0)
        .unwrap_or_else(|_| error!("Invalid observation envelope"));
    let policy: Policy =
        serde_json::from_value(policy.0).unwrap_or_else(|_| error!("Invalid decision policy"));
    let decisions = observation
        .decide(&source.0, policy)
        .unwrap_or_else(|message| error!("{}", message));
    JsonB(serde_json::to_value(decisions).expect("Typed decisions serialize"))
}

#[pg_extern(immutable, parallel_safe)]
fn require_bool(decisions: Option<JsonB>, question_id: Option<&str>) -> bool {
    let decisions = decisions
        .unwrap_or_else(|| error!("Semantic decisions are required; SQL NULL is unresolved"));
    let question_id = question_id.unwrap_or_else(|| error!("Question identity is required"));
    let decision: Decision = serde_json::from_value(
        decisions
            .0
            .get(question_id)
            .cloned()
            .unwrap_or_else(|| error!("Question is absent from the semantic result")),
    )
    .unwrap_or_else(|_| error!("Invalid semantic decision"));
    decision
        .require_bool()
        .unwrap_or_else(|message| error!("{}", message))
}

extension_sql!(
    r#"
REVOKE ALL ON FUNCTION jev_native.scan(text,jsonb,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_native.scan_many(jsonb,jsonb) FROM PUBLIC;
COMMENT ON FUNCTION jev_native.scan(text,jsonb,jsonb) IS
'Invoker-rights, bounded semantic evaluation over an explicit source SELECT. External model usage does not roll back with SQL.';
COMMENT ON FUNCTION jev_native.scan_many(jsonb,jsonb) IS
'One bounded native scheduler for independent source populations, with shared request admission and source-local result identity.';
COMMENT ON FUNCTION jev_native.require_bool(jsonb,text) IS
'Require a resolved Boolean value; UNKNOWN and NOT_EVALUATED cannot silently become false.';
COMMENT ON FUNCTION jev_native.decide(jsonb,jsonb,jsonb) IS
'Reapply a decision policy to a caller-supplied observation and matching source context without provider I/O. Stored evidence requires ordinary table access controls.';
"#,
    name = "native_permissions",
    requires = [scan, scan_many, require_bool, decide]
);

extension_sql_file!("../sql/registry.sql", name = "native_registry");
