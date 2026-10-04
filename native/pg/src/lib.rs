mod execution;
mod plan;

use execution::{SemanticScan, SourceSpec};
use jev_executor::embedding::{AnswerMatrix, Basis};
use jev_executor::evidence::{EvaluatorIdentity, Observation, Policy};
use jev_executor::{Decision, Decisions};
use pgrx::JsonB;
use pgrx::prelude::*;

pgrx::pg_module_magic!(name, version);

#[pg_extern]
fn execute_plan(plan: Option<JsonB>, options: default!(Option<JsonB>, "'{}'::jsonb")) -> JsonB {
    let plan = plan.unwrap_or_else(|| error!("A typed native plan is required"));
    let options = options.unwrap_or_else(|| error!("Execution options are required"));
    plan::execute(plan.0, options.0)
}

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
        name!(policy, JsonB),
    ),
> {
    let source_sql = source_sql.unwrap_or_else(|| error!("Source SELECT is required"));
    let questions = questions.unwrap_or_else(|| error!("Typed questions are required"));
    let options = options.unwrap_or_else(|| error!("Execution options are required"));
    let source = SourceSpec::new("source".into(), source_sql.into(), questions.0);
    TableIterator::new(SemanticScan::new(vec![source], options.0, fcinfo).map(
        |(_, ordinal, source, decisions, observation, usage, receipt, policy)| {
            (
                ordinal,
                source,
                decisions,
                observation,
                usage,
                receipt,
                policy,
            )
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
        name!(policy, JsonB),
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

#[pg_extern]
fn embed(
    source_sql: Option<&str>,
    basis: Option<JsonB>,
    options: default!(Option<JsonB>, "'{}'::jsonb"),
    fcinfo: pg_sys::FunctionCallInfo,
) -> TableIterator<
    'static,
    (
        name!(ordinal, i64),
        name!(source, JsonB),
        name!(embedding, JsonB),
        name!(decisions, JsonB),
        name!(observation, Option<JsonB>),
        name!(usage, JsonB),
        name!(receipt, Option<JsonB>),
        name!(policy, JsonB),
    ),
> {
    let source_sql = source_sql.unwrap_or_else(|| error!("Source SELECT is required"));
    let basis = Basis::parse(
        basis
            .unwrap_or_else(|| error!("Embedding basis is required"))
            .0,
    )
    .unwrap_or_else(|message| error!("{}", message));
    let options = options.unwrap_or_else(|| error!("Execution options are required"));
    let source = SourceSpec::new(
        "embedding".into(),
        source_sql.into(),
        serde_json::to_value(&basis.questions).expect("Questions serialize"),
    )
    .with_context(basis.context_columns.clone());
    let scan = SemanticScan::new(vec![source], options.0, fcinfo);
    let evaluator = scan.executor.evaluator();
    TableIterator::new(scan.map(
        move |(_, ordinal, source, decisions, observation, usage, receipt, policy)| {
            let typed: Decisions =
                serde_json::from_value(decisions.0.clone()).expect("Native decisions are typed");
            let embedding = basis
                .project(&typed, Some(evaluator.clone()))
                .unwrap_or_else(|message| error!("{}", message));
            (
                ordinal,
                source,
                JsonB(serde_json::to_value(embedding).expect("Embedding serializes")),
                decisions,
                observation,
                usage,
                receipt,
                policy,
            )
        },
    ))
}

#[pg_extern(immutable, parallel_safe)]
fn answer_matrix(
    basis: Option<JsonB>,
    decisions: Option<JsonB>,
    evaluator: default!(Option<JsonB>, "NULL"),
) -> JsonB {
    let basis = Basis::parse(
        basis
            .unwrap_or_else(|| error!("Embedding basis is required"))
            .0,
    )
    .unwrap_or_else(|message| error!("{}", message));
    let decisions: Decisions = serde_json::from_value(
        decisions
            .unwrap_or_else(|| error!("Typed decisions are required"))
            .0,
    )
    .unwrap_or_else(|_| error!("Invalid typed decisions"));
    let evaluator: Option<EvaluatorIdentity> = evaluator.map(|identity| {
        serde_json::from_value(identity.0).unwrap_or_else(|_| error!("Invalid evaluator identity"))
    });
    let matrix = basis
        .project(&decisions, evaluator)
        .unwrap_or_else(|message| error!("{}", message));
    JsonB(serde_json::to_value(matrix).expect("Embedding serializes"))
}

#[pg_extern(immutable, parallel_safe)]
fn embedding_distance(left: Option<JsonB>, right: Option<JsonB>) -> f64 {
    let parse = |value: Option<JsonB>| -> AnswerMatrix {
        let value = value.unwrap_or_else(|| error!("Embedding is required"));
        if serde_json::to_vec(&value.0).expect("JSON serializes").len() > 2_000_000 {
            error!("Embedding exceeds 2 MB");
        }
        serde_json::from_value(value.0).unwrap_or_else(|_| error!("Invalid probability matrix"))
    };
    jev_executor::embedding::distance(&parse(left), &parse(right))
        .unwrap_or_else(|message| error!("{}", message))
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
REVOKE ALL ON FUNCTION jev_native.execute_plan(jsonb,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION jev_native.embed(text,jsonb,jsonb) FROM PUBLIC;
COMMENT ON FUNCTION jev_native.scan(text,jsonb,jsonb) IS
'Invoker-rights, bounded semantic evaluation over an explicit source SELECT. External model usage does not roll back with SQL.';
COMMENT ON FUNCTION jev_native.scan_many(jsonb,jsonb) IS
'One bounded native scheduler for independent source populations, with shared request admission and source-local result identity.';
COMMENT ON FUNCTION jev_native.execute_plan(jsonb,jsonb) IS
'Execute a typed relational and semantic stage DAG in a repeatable source snapshot with shared request limits and explicit held-stage receipts.';
COMMENT ON FUNCTION jev_native.embed(text,jsonb,jsonb) IS
'Evaluate independent questions over an explicit projected context and return answer probability matrices. Shares native scan admission, reuse and caller privileges.';
COMMENT ON FUNCTION jev_native.answer_matrix(jsonb,jsonb,jsonb) IS
'Project supplied typed decisions onto a fixed question basis without model I/O. Missing answers retain NULL cells and explicit operational states.';
COMMENT ON FUNCTION jev_native.embedding_distance(jsonb,jsonb) IS
'Root mean squared Hellinger distance across questions, requiring complete probability matrices with the same basis and evaluator revision.';
COMMENT ON FUNCTION jev_native.require_bool(jsonb,text) IS
'Require a resolved Boolean value; UNKNOWN and NOT_EVALUATED cannot silently become false.';
COMMENT ON FUNCTION jev_native.decide(jsonb,jsonb,jsonb) IS
'Reapply a decision policy to a caller-supplied observation and matching source context without provider I/O. Stored evidence requires ordinary table access controls.';
"#,
    name = "native_permissions",
    requires = [
        scan,
        scan_many,
        require_bool,
        decide,
        execute_plan,
        embed,
        answer_matrix,
        embedding_distance
    ]
);

extension_sql_file!("../sql/registry.sql", name = "native_registry");
