use crate::execution::{SemanticScan, SourceSpec};
use jev_executor::plan::{Plan, Stage, same_scalar};
use jev_executor::source::parse_source;
use pgrx::JsonB;
use pgrx::prelude::*;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_PLAN: AtomicU64 = AtomicU64::new(0);

#[derive(Default)]
struct Progress {
    started: bool,
    terminal: bool,
    sealed: bool,
    rows: usize,
    state: &'static str,
    reason: String,
    decisions: BTreeMap<String, [usize; 3]>,
}

impl Progress {
    fn hold(&mut self, state: &'static str, reason: &str) {
        self.started = true;
        self.terminal = true;
        self.state = state;
        self.reason = reason.into();
    }
}

fn quote(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

fn write(sql: &str) {
    Spi::connect_mut(|client| client.update(sql, None, &[]).map(|_| ()))
        .unwrap_or_else(|error| error!("Native stage SQL failed: {}", error));
}

fn number(sql: &str) -> i64 {
    Spi::get_one::<i64>(sql)
        .unwrap_or_else(|error| error!("Native stage inspection failed: {}", error))
        .unwrap_or_else(|| error!("Native stage inspection returned SQL NULL"))
}

fn relation(stage: &Stage, table: &str, ordered: bool) -> String {
    let columns = Spi::get_one::<String>(&format!(
        "SELECT string_agg('s.' || quote_ident(attname),',' ORDER BY attnum) FROM pg_attribute \
         WHERE attrelid='pg_temp.{table}'::regclass AND attnum>0 AND NOT attisdropped AND attname<>'__jev_ordinal'"
    )).expect("Catalog column order is available").expect("Stage has columns");
    let sql = if stage.decision_ids().next().is_none() {
        format!("SELECT {columns} FROM pg_temp.{table} s")
    } else {
        format!(
            "SELECT {columns},d.__jev_decisions,d.__jev_observation,d.__jev_receipt,d.__jev_policy \
                 FROM pg_temp.{table} s JOIN pg_temp.{table}_decisions d USING (__jev_ordinal)"
        )
    };
    if ordered {
        format!("{sql} ORDER BY s.__jev_ordinal")
    } else {
        sql
    }
}

fn bind(
    stage: &Stage,
    plan: &Plan,
    tables: &[String],
    indices: &BTreeMap<String, usize>,
) -> String {
    let sql = stage.sql.trim().trim_end_matches(';');
    if stage.inputs.is_empty() {
        return sql.into();
    }
    let bindings = stage
        .inputs
        .iter()
        .map(|input| {
            let index = indices[&input.stage];
            format!(
                "{} AS ({})",
                quote(&input.alias),
                relation(&plan.stages[index], &tables[index], false)
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    format!("WITH {bindings} SELECT * FROM ({sql}) __jev_bound")
}

fn verify_schema(stage: &Stage, table: &str) {
    let schema = Spi::get_one::<JsonB>(&format!(
        "SELECT jsonb_object_agg(a.attname, jsonb_build_object('oid',a.atttypid::int,'category',t.typcategory::text)) \
         FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid \
         WHERE a.attrelid='pg_temp.{table}'::regclass AND a.attnum>0 AND NOT a.attisdropped AND a.attname<>'__jev_ordinal'"
    )).expect("Catalog schema query succeeds").expect("Stage has columns").0;
    let actual = schema.as_object().expect("Catalog schema is an object");
    if actual.len() != stage.columns.len()
        || stage.columns.keys().any(|name| !actual.contains_key(name))
    {
        error!(
            "Stage {} output names do not match its declared schema",
            stage.id
        );
    }
    for (name, column) in &stage.columns {
        let oid = actual[name]["oid"].as_i64().unwrap();
        let category = actual[name]["category"].as_str().unwrap();
        let matches = match column.kind.as_str() {
            "integer" => [20, 21, 23].contains(&oid),
            "number" => category == "N",
            "text" => category == "S",
            "boolean" => oid == 16,
            "date" => oid == 1082,
            "datetime" => [1114, 1184].contains(&oid),
            "json" => [114, 3802].contains(&oid),
            "other" => true,
            _ => false,
        };
        if !matches {
            error!(
                "Stage {} column {} has an incompatible PostgreSQL type",
                stage.id, name
            );
        }
        if !column.nullable
            && number(&format!(
                "SELECT count(*) FROM pg_temp.{table} WHERE {} IS NULL",
                quote(name)
            )) > 0
        {
            error!(
                "Stage {} column {} violates its non-null contract",
                stage.id, name
            );
        }
    }
    for key in &stage.keys {
        if key.is_empty() {
            if number(&format!("SELECT count(*) FROM pg_temp.{table}")) > 1 {
                error!("Stage {} violates its scalar grain", stage.id);
            }
            continue;
        }
        let columns = key
            .iter()
            .map(|name| quote(name))
            .collect::<Vec<_>>()
            .join(",");
        if number(&format!(
            "SELECT count(*) FROM (SELECT {columns} FROM pg_temp.{table} GROUP BY {columns} HAVING count(*)>1) duplicates"
        )) > 0
        {
            error!("Stage {} violates its declared unique grain", stage.id);
        }
    }
}

fn prerequisite(
    stage: &Stage,
    progress: &[Progress],
    tables: &[String],
    indices: &BTreeMap<String, usize>,
) -> Option<(&'static str, String)> {
    for input in &stage.inputs {
        let parent = &progress[indices[&input.stage]];
        if !parent.sealed {
            return Some((
                "BLOCKED_BY_DEPENDENCY",
                format!("Input {} did not produce a sealed relation", input.alias),
            ));
        }
        let required: Vec<_> = match &input.require_values {
            Some(ids) => ids.iter().collect(),
            None => parent.decisions.keys().collect(),
        };
        for question in required {
            if parent.decisions.get(question).map_or(0, |counts| counts[0]) != parent.rows {
                return Some((
                    "BLOCKED_BY_DEPENDENCY",
                    format!(
                        "Input {} has unresolved decisions for {}",
                        input.alias, question
                    ),
                ));
            }
        }
    }
    let guard = stage.guard.as_ref()?;
    let input = stage
        .inputs
        .iter()
        .find(|input| input.alias == guard.input)
        .expect("Guard validated");
    let index = indices[&input.stage];
    if progress[index].rows == 0 {
        return Some((
            "BLOCKED_BY_DEPENDENCY",
            "A scalar guard has no input row".into(),
        ));
    }
    if progress[index].rows != 1 {
        return Some((
            "FAILED",
            "A scalar guard requires exactly one input row".into(),
        ));
    }
    let decisions = Spi::get_one::<JsonB>(&format!(
        "SELECT __jev_decisions FROM pg_temp.{}_decisions",
        tables[index]
    ))
    .expect("Guard relation exists")
    .expect("Guard has one row")
    .0;
    let decision = &decisions[&guard.question];
    if decision["output_state"] != "VALUE" || decision["operation_state"] != "SUCCEEDED" {
        return Some((
            "BLOCKED_BY_DEPENDENCY",
            "The guard decision is unresolved".into(),
        ));
    }
    let value = &decision["value"];
    if value.is_boolean() != guard.equals.is_boolean()
        || value.is_string() != guard.equals.is_string()
        || value.is_number() != guard.equals.is_number()
    {
        return Some((
            "BLOCKED_BY_POLICY",
            "Guard value and comparison have different types".into(),
        ));
    }
    (!same_scalar(value, &guard.equals).unwrap_or_else(|message| error!("{}", message))).then(
        || {
            (
                "SKIPPED",
                "The resolved guard does not select this branch".into(),
            )
        },
    )
}

pub fn execute(value: Value, options: Value) -> JsonB {
    let plan = Plan::parse(value).unwrap_or_else(|message| error!("{}", message));
    let isolation = Spi::get_one::<String>("SHOW transaction_isolation")
        .expect("Transaction isolation is available")
        .unwrap();
    if !["repeatable read", "serializable"].contains(&isolation.as_str()) {
        error!("Native plans require REPEATABLE READ or SERIALIZABLE before execution");
    }
    let serial = NEXT_PLAN.fetch_add(1, Ordering::Relaxed);
    let tables: Vec<_> = (0..plan.stages.len())
        .map(|index| format!("__jev_plan_{serial}_{index}"))
        .collect();
    let indices: BTreeMap<_, _> = plan
        .stages
        .iter()
        .enumerate()
        .map(|(index, stage)| (stage.id.clone(), index))
        .collect();
    let mut progress: Vec<_> = plan.stages.iter().map(|_| Progress::default()).collect();
    let mut scan = SemanticScan::new(Vec::new(), options, std::ptr::null_mut());
    let mut materialized = 0;
    let mut created = Vec::new();
    let mut completion_order = Vec::new();
    let mut evidence_receipts = BTreeMap::new();
    let mut evaluators = BTreeMap::new();

    while progress.iter().any(|state| !state.terminal) {
        pgrx::check_for_interrupts!();
        for (index, stage) in plan.stages.iter().enumerate() {
            if progress[index].started
                || !stage
                    .inputs
                    .iter()
                    .all(|input| progress[indices[&input.stage]].terminal)
            {
                continue;
            }
            if let Some((state, reason)) = prerequisite(stage, &progress, &tables, &indices) {
                progress[index].hold(state, &reason);
                completion_order.push(stage.id.clone());
                continue;
            }
            let table = &tables[index];
            let sql = bind(stage, &plan, &tables, &indices);
            let remaining = scan.executor.limits.max_rows - materialized;
            write(&format!(
                "CREATE TEMP TABLE {table} ON COMMIT DROP AS \
                SELECT row_number() OVER () AS __jev_ordinal, s.* FROM ({sql}) s LIMIT {}",
                remaining + 1
            ));
            created.push(table.clone());
            let count = number(&format!("SELECT count(*) FROM pg_temp.{table}")) as usize;
            if count > remaining {
                error!("Native plan intermediate populations exceed the shared max_rows limit");
            }
            materialized += count;
            verify_schema(stage, table);
            progress[index].started = true;
            progress[index].rows = count;
            if stage.decision_ids().next().is_none() {
                progress[index].terminal = true;
                progress[index].sealed = true;
                progress[index].state = "SUCCEEDED";
                completion_order.push(stage.id.clone());
            } else {
                write(&format!(
                    "CREATE TEMP TABLE {table}_decisions (__jev_ordinal bigint PRIMARY KEY, \
                    __jev_decisions jsonb NOT NULL, __jev_observation jsonb, __jev_receipt jsonb, __jev_policy jsonb NOT NULL) ON COMMIT DROP"
                ));
                created.push(format!("{table}_decisions"));
                let columns = stage
                    .columns
                    .keys()
                    .map(|name| quote(name))
                    .collect::<Vec<_>>()
                    .join(",");
                let source = format!(
                    "SELECT row_to_json(c) AS __jev_context FROM \
                    (SELECT {columns} FROM pg_temp.{table} ORDER BY __jev_ordinal) c"
                );
                scan.add_source(SourceSpec::for_plan(
                    stage.id.clone(),
                    source,
                    stage.questions.clone(),
                    stage.row_guard.clone(),
                    stage.selections.clone(),
                ));
                for question in stage.decision_ids() {
                    progress[index].decisions.insert(question.clone(), [0; 3]);
                }
            }
        }
        let mut batches: BTreeMap<usize, Vec<Value>> = BTreeMap::new();
        for (id, ordinal, _, decisions, observation, _, receipt, policy) in scan.next_batch() {
            if let Some(observation) = &observation {
                let evaluator = &observation.0["evaluator"];
                evaluators.insert(evaluator.to_string(), evaluator.clone());
            }
            if let Some(receipt) = &receipt {
                if let Some(identity) = receipt.0["attempt_id"].as_str() {
                    evidence_receipts.insert(identity.to_owned(), receipt.0.clone());
                }
            }
            let index = indices[&id];
            for (question, result) in decisions.0.as_object().expect("Typed decisions") {
                let slot = match result["output_state"].as_str() {
                    Some("VALUE") => 0,
                    Some("UNKNOWN") => 1,
                    _ => 2,
                };
                progress[index]
                    .decisions
                    .get_mut(question)
                    .expect("Declared question")[slot] += 1;
            }
            batches.entry(index).or_default().push(json!({
                "ordinal": ordinal, "decisions": decisions.0, "observation": observation.map(|v| v.0),
                "receipt": receipt.map(|v| v.0), "policy": policy.0,
            }));
        }
        for (index, rows) in batches {
            let sql = format!(
                "INSERT INTO pg_temp.{}_decisions \
                SELECT ordinal,decisions,observation,receipt,policy FROM jsonb_to_recordset($1) \
                AS r(ordinal bigint, decisions jsonb, observation jsonb, receipt jsonb, policy jsonb)",
                tables[index]
            );
            Spi::connect_mut(|client| {
                client
                    .update(&sql, None, &[JsonB(json!(rows)).into()])
                    .map(|_| ())
            })
            .unwrap_or_else(|error| error!("Cannot store native stage decisions: {}", error));
        }
        for (index, stage) in plan.stages.iter().enumerate() {
            if progress[index].started && !progress[index].terminal && scan.finished(&stage.id) {
                progress[index].terminal = true;
                progress[index].sealed = true;
                progress[index].state = "SUCCEEDED";
                completion_order.push(stage.id.clone());
            }
        }
    }
    let target = indices[&plan.target];
    let mut rows = Vec::new();
    if progress[target].sealed {
        let sql = format!(
            "SELECT row_to_json(r)::text FROM ({}) r",
            relation(&plan.stages[target], &tables[target], true)
        );
        rows = Spi::connect(|client| {
            let mut cursor = client.try_open_cursor(&sql, &[])?;
            let mut bytes = 0;
            let mut rows = Vec::new();
            loop {
                let table = cursor.fetch(32)?;
                let mut fetched = 0;
                for row in table {
                    let text = row.get::<String>(1)?.expect("A relation row is not null");
                    bytes += text.len();
                    if bytes > 8_000_000 {
                        error!("Native plan result exceeds 8 MB; project or constrain the target");
                    }
                    rows.push(parse_source(&text).unwrap_or_else(|message| error!("{}", message)));
                    fetched += 1;
                }
                if fetched < 32 {
                    break;
                }
            }
            Ok::<_, pgrx::spi::Error>(rows)
        })
        .unwrap_or_else(|error| error!("Cannot read native plan result: {}", error));
    }
    let stages: Vec<_> = plan
        .stages
        .iter()
        .zip(&progress)
        .map(|(stage, state)| {
            json!({
                "id": stage.id, "operator": stage.operator, "population_closed": state.sealed,
                "output_state": if state.sealed { "VALUE" } else { "NOT_EVALUATED" },
                "operation_state": state.state, "reason": state.reason, "rows": state.rows,
                "decisions": state.decisions.iter().map(|(id, counts)| (id.clone(), json!({
                    "VALUE": counts[0], "UNKNOWN": counts[1], "NOT_EVALUATED": counts[2]
                }))).collect::<BTreeMap<_, _>>(),
            })
        })
        .collect();
    let result = JsonB(json!({
        "version": 1, "target": plan.target, "rows": rows, "stages": stages,
        "completion_order": completion_order, "materialized_rows": materialized,
        "usage": scan.executor.usage, "policy": scan.executor.limits.policy(),
        "evidence_receipts": evidence_receipts.into_values().collect::<Vec<_>>(),
        "evaluators": evaluators.into_values().collect::<Vec<_>>(),
    }));
    drop(scan);
    for table in created.iter().rev() {
        write(&format!("DROP TABLE pg_temp.{table}"));
    }
    result
}
