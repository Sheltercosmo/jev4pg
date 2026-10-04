# Query workspace guide

[简体中文](zh/USER_GUIDE.md) · [All documentation](README.md)

Start the service, open [/ask/en](http://127.0.0.1:8000/ask/en), and connect with your database API token. The server's JEV and LLM credentials are separate; do not paste them into the workspace token field. Select the datasets relevant to your question.

## Browse and reuse work

Version 0.7.0 adds a searchable catalog, query tabs and a result grid. Expand a table to inspect columns, browse live rows or open a SQL draft. Live browsing filters in the database and advances by primary key. Select only the columns you need. See [large-table and application usage](APPLICATIONS.md).

Name query tabs, reopen `.sql` files and save drafts with Ctrl/Cmd+S. Drafts remain in the current browser tab's session; completed executions remain in Recent queries. In SQL mode, Ctrl/Cmd+Enter runs the selected text when a selection exists, otherwise the full editor. Opening a draft never runs it automatically.

Result-grid search, sorting and exports operate on returned rows, not the full source table. Open a cell to inspect or copy its complete value. JSON retains NULL values and exact decimal strings. CSV leaves NULL cells blank and prefixes formula-like text for spreadsheet safety; use JSON when those distinctions matter.

For small files, choose Import CSV, review column types, then create the table. Changing the file or settings requires a fresh preview. Every row is checked before creation. Use PostgreSQL bulk loading and attachments for larger files.

## Run SQL in the background

In the development workspace, choose SQL mode and Run in background. A selection submits only the selected SQL; otherwise the whole editor is submitted. After the server accepts it, you can edit the draft, open another query tab or close the page. Your edits do not change the submitted request.

The background card shows the submitted SQL and its status. Cancel query requests cancellation; already sent model requests may still finish and incur usage. Once a result is ready, choose Open result. If you have edited the draft, its saved result opens in another query tab. Results never replace newer edits automatically. Mutation results are previews and still require Commit changes.

Refreshing the page restores the draft and job reference within that browser tab's session. After closing the tab or using another browser, connect with the same identity and open the job in Recent queries. Opening or refreshing a job retrieves its existing outcome without executing it again.

If the submission response is lost, choose Retry submission to confirm the original request. It reuses the original SQL and request key, even if you have since edited the draft. A failed or cancelled job needs a new execution; Run in background creates a new request after you review it. Draft recovery requires browser session storage.

An administrator must start a separate query worker for your tenant; accepted work stays queued until a worker claims it. See [worker setup and query limits](QUERY_JOBS.md). Natural-language planning remains interactive: review its SQL, choose Edit SQL, then submit that SQL in the background.

If a required stage did not run, the background card says Result not evaluated and shows the saved reason. Open it to inspect the SQL and evidence, then edit and submit a new revision. Its empty result area is not a calculated zero or proof that nothing matched. Recent queries retains this distinction when you reopen older work.

## Query modes

| Mode | Use it for | How it works |
| --- | --- | --- |
| Natural language · JEV | Requests supported by the typed planner | Parallel semantic decisions assemble a checked SQL proposal. No LLM generation call. |
| Hybrid · LLM + JEV | Broader language and more complex SQL | JEV narrows context; the LLM proposes SQL; JEV reviews independent questions in parallel. |
| SQL | A query you want to write or edit directly | The same data-access and execution checks apply. |

Hybrid needs [LLM configuration](HYBRID_QUERY.md#configuration). Its default uses one generation call; a supported repair may add one more.

Describe the outcome you want, including business definitions, units and time periods when they matter. For example: “For each customer, show the latest payment and the change from their previous payment.” Selecting fewer relevant datasets helps focus the request without changing their stored data.

Choose Preview plan to inspect the interpretation before execution. Alt+Enter is the shortcut. Ctrl+Enter runs a read or previews a database change. These shortcuts apply while editing the query.

## Inspect and correct a plan

Hybrid results show How this plan was built: related data, SQL proposal and parallel review. Expand usage details for LLM calls, JEV planning requests, retained fields and review waves. Sampled values help planning; they do not restrict execution to the sample.

Under Interpretations, compare assumptions and SQL. Use this interpretation rebuilds the preview using that choice; it does not execute it. Local refinements remain identifiable and the earlier alternatives stay available. The detailed data constraints describe the proposed SQL, not proof that it expresses your intention.

If the plan is held, inspect its review items. Correct a decision, choose another interpretation, edit SQL, or explicitly confirm the available proposal. Invalid SQL must be fixed before execution. A review passing its checks is not a guarantee of a correct answer.

| Result state | Meaning |
| --- | --- |
| `VALUE` | A result exists, including a valid false or zero. |
| `UNKNOWN` | The requested result could not be resolved. |
| `NOT_EVALUATED` | This work was not evaluated. It is not false. |

Execution status is separate. `FAILED`, `BLOCKED_BY_BUDGET` and `TRUNCATED` explain operational limits. A proposal, review result and execution result may therefore have different states. Partial results should not be interpreted as a complete population.

Development SQL results stop at 1,000 rows or 4 MiB of row data. The result area distinguishes the row limit from the size limit. Returned cells remain complete; a first row too large to return produces an error. Choose fewer fields, calculate a summary in SQL, or use PostgreSQL export tooling for the full data. These presentation limits do not reduce the population used by aggregates and windows inside your query.

Database changes have a separate preview and Commit changes action. Reviewing a plan or selecting an alternative never commits a write.

## Reuse a recent query

Open Recent queries and select an entry. Restore its question or SQL, or choose Review saved decisions to correct its interpretation. Restoring never executes the query. Run the edited request to create a linked revision while preserving the original.

Unchanged hybrid decisions can reuse the saved proposal and review work. A changed request or context can require new calls; reuse is not a promise of zero cost for every edit.

## Create records from text

Under Manage data, open Extract entries from text:

1. Choose a destination and describe what one row represents.
2. Define each column's name, type and meaning. Include units and number conventions.
3. Paste the document or load a UTF-8 text file, then extract.
4. Review the proposed records and their source excerpts before importing.

The extractor maps exact text spans to typed values. Missing or ambiguous required values hold the import instead of inventing entries. Automatic import is optional and inserts only fully resolved records. Repeating extraction can create duplicates; it is not an update or deduplication operation. See the [text import guide](TEXT_IMPORT.md) for API calls, numeric formats and limits.

## Existing data and native execution

An administrator can [attach existing PostgreSQL tables or views](EXISTING_DATA.md). Attached datasets appear in the selector and are read-only through the workspace. Source permissions and row security determine the accessible population.

The development [Rust extension](../native/README.md) is enabled by the server administrator. With native execution enabled, supported semantic SQL can operate on derived relations and conditional branches. Planning, saved queries and user review keep the same workflow. Native embeddings are currently a SQL interface; use the [embedding guide](NATIVE_EMBEDDINGS.md) for question bases and similarity queries.

## Use operators directly

The API exposes a catalog at `GET /jev/operators`, examples at `GET /jev/operators/examples`, and execution at `POST /jev/call`. Start with the [operator guide](JEV_OPERATORS.md), then use the [function reference](JEV_FUNCTION_REFERENCE.md) and [runnable examples](../examples/operators/README.md).

The workspace's Guide contains a shorter version of these workflows. For measured capabilities and limitations, see the [performance and cost guide](PERFORMANCE_AND_COST.md).
