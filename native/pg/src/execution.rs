use jev_executor::conditional::{RowGuard, Selection};
use jev_executor::registry::RegistryConfig;
use jev_executor::source::{MAX_CONTEXT_BYTES, parse_source};
use jev_executor::{
    Decision, Evaluation, EvaluationInput, Executor, Limits, Provider, Questions,
    validate_questions,
};
use pgrx::JsonB;
use pgrx::prelude::*;
use serde_json::Value;
use std::collections::{BTreeMap, HashSet, VecDeque};
use std::ffi::CString;

pub type ResultRow = (
    String,
    i64,
    JsonB,
    JsonB,
    Option<JsonB>,
    JsonB,
    Option<JsonB>,
    JsonB,
);

pub struct SourceSpec {
    id: String,
    sql: String,
    questions: Questions,
    nested_context: bool,
    row_guard: Option<RowGuard>,
    selections: BTreeMap<String, Selection>,
    context_columns: Option<Vec<String>>,
}

impl SourceSpec {
    pub fn new(id: String, sql: String, questions: Value) -> Self {
        if id.is_empty() || id.len() > 200 || sql.is_empty() || sql.len() > 30_000 {
            error!("Supply a source identity and one bounded source SELECT");
        }
        let questions: Questions =
            serde_json::from_value(questions).unwrap_or_else(|_| error!("Invalid typed questions"));
        validate_questions(&questions).unwrap_or_else(|message| error!("{}", message));
        Self {
            id,
            sql,
            questions,
            nested_context: false,
            row_guard: None,
            selections: BTreeMap::new(),
            context_columns: None,
        }
    }

    pub fn for_plan(
        id: String,
        sql: String,
        questions: Questions,
        row_guard: Option<RowGuard>,
        selections: BTreeMap<String, Selection>,
        context_columns: Option<Vec<String>>,
    ) -> Self {
        if id.is_empty() || id.len() > 200 || sql.is_empty() || sql.len() > 30_000 {
            error!("Supply a source identity and one bounded source SELECT");
        }
        Self {
            id,
            sql,
            questions,
            nested_context: true,
            row_guard,
            selections,
            context_columns,
        }
    }

    fn prepare(&self, source: &Value) -> (Option<Value>, Option<Evaluation>) {
        if !self.selections.is_empty() {
            return (
                None,
                Some(Evaluation {
                    decisions: self
                        .selections
                        .iter()
                        .map(|(id, selection)| (id.clone(), selection.evaluate(source)))
                        .collect(),
                    observation: None,
                    receipt: None,
                }),
            );
        }
        let matched = self
            .row_guard
            .as_ref()
            .map_or(Ok(true), |guard| guard.evaluate(source));
        let blocked = match matched {
            Ok(true) => {
                if let Some(columns) = &self.context_columns {
                    let context = columns
                        .iter()
                        .map(|name| (name.clone(), source[name].clone()))
                        .collect();
                    return (Some(Value::Object(context)), None);
                }
                let Some(guard) = &self.row_guard else {
                    return (None, None);
                };
                let mut context = source.clone();
                context
                    .as_object_mut()
                    .expect("Source is a row")
                    .remove(&guard.column);
                return (Some(context), None);
            }
            Ok(false) => Decision::blocked(
                "SKIPPED",
                "The resolved row condition does not select this branch",
            ),
            Err(blocked) => blocked,
        };
        (
            None,
            Some(Evaluation {
                decisions: self
                    .questions
                    .keys()
                    .map(|id| (id.clone(), blocked.clone()))
                    .collect(),
                observation: None,
                receipt: None,
            }),
        )
    }
}

pub fn sources(value: Value) -> Vec<SourceSpec> {
    if serde_json::to_vec(&value).expect("JSON serializes").len() > 2_000_000 {
        error!("Native source declarations exceed 2 MB");
    }
    let entries = value
        .as_array()
        .filter(|items| (1..=32).contains(&items.len()))
        .unwrap_or_else(|| error!("Supply 1 to 32 independent source declarations"));
    let mut identities = HashSet::new();
    entries
        .iter()
        .map(|entry| {
            let object = entry
                .as_object()
                .unwrap_or_else(|| error!("Invalid source declaration"));
            if object.len() != 3
                || !object
                    .keys()
                    .all(|key| ["id", "sql", "questions"].contains(&key.as_str()))
            {
                error!("Each source requires only id, sql and questions");
            }
            let id = entry["id"]
                .as_str()
                .unwrap_or_else(|| error!("Source identity must be text"));
            if !identities.insert(id.to_owned()) {
                error!("Source identities must be unique");
            }
            let sql = entry["sql"]
                .as_str()
                .unwrap_or_else(|| error!("Source SELECT must be text"));
            SourceSpec::new(id.to_owned(), sql.to_owned(), entry["questions"].clone())
        })
        .collect()
}

struct SourceCursor {
    spec: SourceSpec,
    cursor: Option<String>,
    seen: usize,
    cleanup: Option<CursorCleanup>,
}

struct CursorCleanup {
    context: *mut pg_sys::ExprContext,
    name: *mut std::ffi::c_char,
}

#[pgrx::pg_guard]
unsafe extern "C-unwind" fn close_source_cursor(argument: pg_sys::Datum) {
    // Normal shutdown/rescan uses this callback; PostgreSQL owns abort cleanup.
    unsafe {
        let portal = pg_sys::SPI_cursor_find(argument.cast_mut_ptr());
        if !portal.is_null() {
            pg_sys::SPI_cursor_close(portal);
        }
    }
}

fn register_cursor_cleanup(fcinfo: pg_sys::FunctionCallInfo, name: &str) -> CursorCleanup {
    let name = CString::new(name).expect("PostgreSQL cursor names contain no NUL");
    // Callback names live in the query context, independently of movable Rust values.
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

impl SourceCursor {
    fn open(spec: SourceSpec, fcinfo: pg_sys::FunctionCallInfo) -> Self {
        let query = format!(
            "SELECT CASE WHEN octet_length(__jev_context.body) <= {MAX_CONTEXT_BYTES} \
             THEN __jev_context.body ELSE NULL END \
             FROM (SELECT row_to_json(__jev_source)::text AS body \
                   FROM ({}) AS __jev_source OFFSET 0) AS __jev_context",
            spec.sql.trim_end().trim_end_matches(';')
        );
        let cursor = Spi::connect(|client| {
            client
                .try_open_cursor(&query, &[])
                .map(|c| c.detach_into_name())
        })
        .unwrap_or_else(|_| {
            error!("Source must be one SELECT authorized for the current PostgreSQL role")
        });
        let cleanup = (!fcinfo.is_null()).then(|| register_cursor_cleanup(fcinfo, &cursor));
        Self {
            spec,
            cursor: Some(cursor),
            seen: 0,
            cleanup,
        }
    }

    fn fetch(&mut self, count: usize, bytes: &mut usize) -> VecDeque<(i64, Value)> {
        let name = self
            .cursor
            .as_ref()
            .expect("Only active cursors are fetched");
        let rows: Vec<Value> = Spi::connect(|client| {
            let mut cursor = client.find_cursor(name)?;
            let table = cursor.fetch(count as i64)?;
            let rows = table
                .into_iter()
                .map(|row| {
                    row.get::<String>(1).map(|value| {
                        let text = value.unwrap_or_else(|| {
                            error!("Semantic source exceeds the 1 MB context limit")
                        });
                        *bytes += text.len();
                        if *bytes > 8_000_000 {
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
        if rows.len() < count {
            self.close();
        }
        rows.into_iter()
            .map(|mut source| {
                self.seen += 1;
                if self.spec.nested_context {
                    source = source
                        .as_object_mut()
                        .and_then(|row| row.remove("__jev_context"))
                        .unwrap_or_else(|| error!("Native plan context is absent"));
                }
                (self.seen as i64, source)
            })
            .collect()
    }

    fn close(&mut self) {
        if let Some(cleanup) = self.cleanup.take() {
            // Unregister before closing so later portal-name reuse cannot close another cursor.
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

impl Drop for SourceCursor {
    fn drop(&mut self) {
        self.close();
    }
}

struct SourceRow {
    index: usize,
    ordinal: i64,
    source: Value,
}

pub struct SemanticScan {
    sources: Vec<SourceCursor>,
    pub(crate) executor: Executor,
    pending: VecDeque<ResultRow>,
    next_source: usize,
    seen: usize,
}

impl SemanticScan {
    pub fn add_source(&mut self, source: SourceSpec) {
        self.sources
            .push(SourceCursor::open(source, std::ptr::null_mut()));
    }

    pub fn finished(&self, id: &str) -> bool {
        self.sources
            .iter()
            .any(|source| source.spec.id == id && source.cursor.is_none())
    }

    pub fn next_batch(&mut self) -> Vec<ResultRow> {
        let mut rows = Vec::new();
        if let Some(row) = self.next() {
            rows.push(row);
            rows.extend(self.pending.drain(..));
        }
        rows
    }
    pub fn new(
        sources: Vec<SourceSpec>,
        mut options: Value,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> Self {
        let settings = options
            .as_object_mut()
            .unwrap_or_else(|| error!("Execution options must be an object"));
        let scope = settings
            .remove("evidence_scope")
            .unwrap_or_else(|| Value::String(String::new()));
        let scope = scope
            .as_str()
            .filter(|scope| scope.len() <= 200)
            .unwrap_or_else(|| error!("Evidence scope must be at most 200 bytes of text"));
        let max_age = settings.remove("evidence_max_age_seconds");
        let limits: Limits = serde_json::from_value(options)
            .unwrap_or_else(|_| error!("Invalid native execution options"));
        limits
            .validate()
            .unwrap_or_else(|message| error!("{}", message));
        let config_path = std::env::var("JEV_NATIVE_CONFIG_FILE").unwrap_or_else(|_| {
            error!("Administrator must configure JEV_NATIVE_CONFIG_FILE on the PostgreSQL server")
        });
        let config = std::fs::read_to_string(config_path)
            .unwrap_or_else(|_| error!("Cannot read native provider configuration"));
        let mut config: Value = serde_json::from_str(&config)
            .unwrap_or_else(|_| error!("Invalid native provider configuration"));
        let registry = config
            .as_object_mut()
            .unwrap_or_else(|| error!("Invalid native provider configuration"))
            .remove("registry");
        let provider: Provider = serde_json::from_value(config)
            .unwrap_or_else(|_| error!("Invalid native provider configuration"));
        let mut executor =
            Executor::new(provider, limits).unwrap_or_else(|message| error!("{}", message));
        if let Some(registry) = registry {
            let mut registry: RegistryConfig = serde_json::from_value(registry)
                .unwrap_or_else(|_| error!("Invalid native registry configuration"));
            if let Some(max_age) = max_age {
                let age = max_age
                    .as_u64()
                    .filter(|age| {
                        (0..=31_536_000).contains(&registry.max_age_seconds)
                            && *age <= registry.max_age_seconds as u64
                    })
                    .unwrap_or_else(|| {
                        error!("Evidence age must not exceed the administrator's configured limit")
                    });
                registry.max_age_seconds = age as i32;
            }
            let database = unsafe { pg_sys::MyDatabaseId };
            let identity = unsafe {
                serde_json::json!({
                    "cluster": pg_sys::GetSystemIdentifier().to_string(),
                    "database": database.to_string(),
                    "role": pg_sys::GetUserId().to_string(),
                    "scope": scope,
                })
            }
            .to_string();
            executor
                .enable_registry(registry, identity)
                .unwrap_or_else(|message| error!("{}", message));
        } else if max_age.is_some() {
            error!("Evidence age requires an administrator-configured registry");
        }
        let sources = sources
            .into_iter()
            .map(|source| SourceCursor::open(source, fcinfo))
            .collect();
        Self {
            sources,
            executor,
            pending: VecDeque::new(),
            next_source: 0,
            seen: 0,
        }
    }

    fn read_batch(&mut self) -> Vec<SourceRow> {
        let mut active = self
            .sources
            .iter()
            .filter(|source| source.cursor.is_some())
            .count();
        let mut chunks = Vec::new();
        let mut count = 0;
        let mut bytes = 0;
        let start = self.next_source;
        for offset in 0..self.sources.len() {
            if count == self.executor.limits.batch_rows || active == 0 {
                break;
            }
            let index = (start + offset) % self.sources.len();
            if self.sources[index].cursor.is_none() {
                continue;
            }
            let quota = (self.executor.limits.batch_rows - count).div_ceil(active);
            active -= 1;
            let rows = self.sources[index].fetch(quota, &mut bytes);
            count += rows.len();
            chunks.push((index, rows));
        }
        if self.seen + count > self.executor.limits.max_rows {
            error!(
                "Semantic source exceeds max_rows; constrain the source query or raise its explicit limit"
            );
        }
        self.seen += count;
        let mut rows = Vec::with_capacity(count);
        while rows.len() < count {
            for (index, chunk) in &mut chunks {
                if let Some((ordinal, source)) = chunk.pop_front() {
                    rows.push(SourceRow {
                        index: *index,
                        ordinal,
                        source,
                    });
                }
            }
        }
        if let Some(last) = rows.last() {
            self.next_source = (last.index + 1) % self.sources.len();
        }
        rows
    }
}

impl Iterator for SemanticScan {
    type Item = ResultRow;

    fn next(&mut self) -> Option<Self::Item> {
        if let Some(row) = self.pending.pop_front() {
            return Some(row);
        }
        pgrx::check_for_interrupts!();
        let rows = self.read_batch();
        if rows.is_empty() {
            return None;
        }
        let prepared: Vec<_> = rows
            .iter()
            .map(|row| self.sources[row.index].spec.prepare(&row.source))
            .collect();
        let local_bytes: usize = prepared
            .iter()
            .filter_map(|(_, result)| result.as_ref())
            .map(|result| {
                serde_json::to_vec(result)
                    .expect("Typed decisions serialize")
                    .len()
            })
            .sum();
        if local_bytes > 8_000_000 {
            error!("Conditional result batch exceeds 8 MB; reduce batch_rows");
        }
        let inputs: Vec<_> = rows
            .iter()
            .zip(&prepared)
            .filter(|(_, (_, result))| result.is_none())
            .map(|(row, (context, _))| EvaluationInput {
                source: context.as_ref().unwrap_or(&row.source),
                questions: &self.sources[row.index].spec.questions,
            })
            .collect();
        let mut results = if inputs.is_empty() {
            Vec::new()
        } else {
            self.executor
                .evaluate_many(&inputs, || {
                    pgrx::check_for_interrupts!();
                })
                .unwrap_or_else(|message| error!("{}", message))
        }
        .into_iter();
        for (row, (_, ready)) in rows.into_iter().zip(prepared) {
            let evaluation =
                ready.unwrap_or_else(|| results.next().expect("Every admitted row has a result"));
            self.pending.push_back((
                self.sources[row.index].spec.id.clone(),
                row.ordinal,
                JsonB(row.source),
                JsonB(
                    serde_json::to_value(evaluation.decisions).expect("Typed decisions serialize"),
                ),
                evaluation.observation.map(|observation| {
                    JsonB(serde_json::to_value(observation).expect("Observations serialize"))
                }),
                JsonB(serde_json::to_value(&self.executor.usage).expect("Usage serializes")),
                evaluation.receipt.map(|receipt| {
                    JsonB(serde_json::to_value(receipt).expect("Receipt serializes"))
                }),
                JsonB(
                    serde_json::to_value(self.executor.limits.policy()).expect("Policy serializes"),
                ),
            ));
        }
        self.pending.pop_front()
    }
}
