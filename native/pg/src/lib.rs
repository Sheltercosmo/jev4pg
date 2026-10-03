use jev_executor::evidence::{Observation, Policy};
use jev_executor::source::{MAX_CONTEXT_BYTES, parse_source};
use jev_executor::{Decision, Executor, Limits, Provider, Questions, validate_questions};
use pgrx::JsonB;
use pgrx::prelude::*;
use serde_json::Value;
use std::collections::VecDeque;
use std::ffi::CString;

pgrx::pg_module_magic!(name, version);

type ResultRow = (i64, JsonB, JsonB, Option<JsonB>, JsonB);

struct SemanticScan {
    cursor: Option<String>,
    executor: Executor,
    questions: Questions,
    pending: VecDeque<ResultRow>,
    seen: usize,
    cleanup: Option<CursorCleanup>,
}

struct CursorCleanup {
    context: *mut pg_sys::ExprContext,
    name: *mut std::ffi::c_char,
}

#[pgrx::pg_guard]
unsafe extern "C-unwind" fn close_source_cursor(argument: pg_sys::Datum) {
    // Expression callbacks run on normal shutdown/rescan; PostgreSQL owns abort cleanup.
    unsafe {
        let portal = pg_sys::SPI_cursor_find(argument.cast_mut_ptr());
        if !portal.is_null() {
            pg_sys::SPI_cursor_close(portal);
        }
    }
}

fn register_cursor_cleanup(fcinfo: pg_sys::FunctionCallInfo, name: &str) -> CursorCleanup {
    let name = CString::new(name).expect("PostgreSQL cursor names contain no NUL");
    // pgrx has initialized this SRF's ReturnSetInfo and expression context before calling scan.
    unsafe {
        let info = (*fcinfo).resultinfo.cast::<pg_sys::ReturnSetInfo>();
        let context = (*info).econtext;
        let name = pg_sys::MemoryContextStrdup((*context).ecxt_per_query_memory, name.as_ptr());
        pg_sys::RegisterExprContextCallback(
            context,
            Some(close_source_cursor),
            pg_sys::Datum::from(name),
        );
        CursorCleanup { context, name }
    }
}

impl Iterator for SemanticScan {
    type Item = ResultRow;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(row) = self.pending.pop_front() {
            return Some(row);
        }
        let name = self.cursor.as_ref()?;
        pgrx::check_for_interrupts!();
        let batch_size = self.executor.limits.batch_rows as i64;
        let rows: Vec<Value> = Spi::connect(|client| {
            let mut cursor = client.find_cursor(name)?;
            let table = cursor.fetch(batch_size)?;
            let mut bytes = 0;
            let rows = table
                .into_iter()
                .map(|row| {
                    row.get::<String>(1).map(|value| {
                        let text = value.unwrap_or_else(|| {
                            error!("Semantic source exceeds the 1 MB context limit")
                        });
                        bytes += text.len();
                        if bytes > 8_000_000 {
                            error!("Semantic source batch exceeds 8 MB; reduce batch_rows");
                        }
                        parse_source(&text).unwrap_or_else(|message| error!("{}", message))
                    })
                })
                .collect::<Result<Vec<_>, _>>()?;
            cursor.detach_into_name();
            Ok::<_, pgrx::spi::Error>(rows)
        })
        .unwrap_or_else(|_| error!("Could not read the authorized source relation"));
        if rows.is_empty() {
            self.close();
            return None;
        }
        if self.seen + rows.len() > self.executor.limits.max_rows {
            error!(
                "Semantic source exceeds max_rows; constrain the source query or raise its explicit limit"
            );
        }
        let results = self
            .executor
            .evaluate(&rows, &self.questions, || {
                pgrx::check_for_interrupts!();
            })
            .unwrap_or_else(|message| error!("{}", message));
        for (source, evaluation) in rows.into_iter().zip(results) {
            self.seen += 1;
            self.pending.push_back((
                self.seen as i64,
                JsonB(source),
                JsonB(
                    serde_json::to_value(evaluation.decisions).expect("Typed decisions serialize"),
                ),
                evaluation.observation.map(|observation| {
                    JsonB(serde_json::to_value(observation).expect("Observations serialize"))
                }),
                JsonB(serde_json::to_value(&self.executor.usage).expect("Usage serializes")),
            ));
        }
        self.pending.pop_front()
    }
}

impl SemanticScan {
    fn close(&mut self) {
        if let Some(cleanup) = self.cleanup.take() {
            // Normal EOF unregisters before releasing the portal, preventing name reuse races.
            unsafe {
                pg_sys::UnregisterExprContextCallback(
                    cleanup.context,
                    Some(close_source_cursor),
                    pg_sys::Datum::from(cleanup.name),
                );
            }
        }
        if let Some(name) = self.cursor.take() {
            Spi::connect(|client| {
                if let Ok(cursor) = client.find_cursor(&name) {
                    drop(cursor);
                }
            });
        }
    }
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
    ),
> {
    let source_sql = source_sql.unwrap_or_else(|| error!("Source SELECT is required"));
    let questions = questions.unwrap_or_else(|| error!("Typed questions are required"));
    let options = options.unwrap_or_else(|| error!("Execution options are required"));
    if source_sql.is_empty() || source_sql.len() > 30_000 {
        error!("Supply one bounded source SELECT");
    }
    let questions: Questions =
        serde_json::from_value(questions.0).unwrap_or_else(|_| error!("Invalid typed questions"));
    validate_questions(&questions).unwrap_or_else(|message| error!("{}", message));
    let limits: Limits = serde_json::from_value(options.0)
        .unwrap_or_else(|_| error!("Invalid native execution options"));
    limits
        .validate()
        .unwrap_or_else(|message| error!("{}", message));
    let config_path = std::env::var("JEV_NATIVE_CONFIG_FILE").unwrap_or_else(|_| {
        error!("Administrator must configure JEV_NATIVE_CONFIG_FILE on the PostgreSQL server")
    });
    let config = std::fs::read_to_string(config_path)
        .unwrap_or_else(|_| error!("Cannot read native provider configuration"));
    let provider: Provider = serde_json::from_str(&config)
        .unwrap_or_else(|_| error!("Invalid native provider configuration"));
    let executor = Executor::new(provider, limits).unwrap_or_else(|message| error!("{}", message));
    let query = format!(
        "SELECT CASE WHEN octet_length(__jev_context.body) <= {MAX_CONTEXT_BYTES} \
         THEN __jev_context.body ELSE NULL END \
         FROM (SELECT row_to_json(__jev_source)::text AS body \
               FROM ({}) AS __jev_source OFFSET 0) AS __jev_context",
        source_sql.trim_end().trim_end_matches(';')
    );
    let cursor = Spi::connect(|client| {
        client
            .try_open_cursor(&query, &[])
            .map(|c| c.detach_into_name())
    })
    .unwrap_or_else(|_| {
        error!("Source must be one SELECT authorized for the current PostgreSQL role")
    });
    let cleanup = Some(register_cursor_cleanup(fcinfo, &cursor));
    TableIterator::new(SemanticScan {
        cursor: Some(cursor),
        executor,
        questions,
        pending: VecDeque::new(),
        seen: 0,
        cleanup,
    })
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
COMMENT ON FUNCTION jev_native.scan(text,jsonb,jsonb) IS
'Invoker-rights, bounded semantic evaluation over an explicit source SELECT. External model usage does not roll back with SQL.';
COMMENT ON FUNCTION jev_native.require_bool(jsonb,text) IS
'Require a resolved Boolean value; UNKNOWN and NOT_EVALUATED cannot silently become false.';
COMMENT ON FUNCTION jev_native.decide(jsonb,jsonb,jsonb) IS
'Reapply a decision policy to a caller-supplied observation and matching source context without provider I/O. Stored evidence requires ordinary table access controls.';
"#,
    name = "native_permissions",
    requires = [scan, require_bool, decide]
);
