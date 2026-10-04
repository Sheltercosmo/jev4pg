window.BackgroundJobs = (() => {
  const activeStates = new Set(["SUBMITTING", "UNCONFIRMED", "QUEUED", "RUNNING", "CANCELLING"]);
  let connected = false,
    timer = null,
    generation = 0,
    posting = null,
    changing = false,
    failures = 0;

  function stopPolling() {
    clearTimeout(timer);
    timer = null;
    generation++;
  }

  function schedule() {
    clearTimeout(timer);
    const job = Workspace.current()?.job;
    if (connected && !document.hidden && job?.id && activeStates.has(job.state))
      timer = setTimeout(refresh, 1500 * 2 ** Math.min(failures, 4));
  }

  function message(job) {
    if (job.notice) return t(job.notice);
    if (job.error === "LEASE_EXPIRED" || job.error === "LEASE_LOST") return t("jobLeaseLost");
    if (job.error === "RESULT_TOO_LARGE") return t("jobResultTooLarge");
    if (job.operation === "PARTIAL") return t("partialHint");
    if (job.operation === "TRUNCATED") return t("jobTruncated");
    if (job.operation === "AWAITING_REVIEW") return t("jobPreviewHint");
    return t("jobHint" + job.state);
  }

  function render() {
    const current = Workspace.current();
    let job = current?.job;
    if (job?.state === "SUBMITTING" && posting !== current.id) {
      job = { ...job, state: "UNCONFIRMED" };
      Workspace.setJob(current.id, job);
    }
    $("run-background").hidden = $("mode").value !== "sql";
    $("run-background").disabled =
      busy || !connected || Boolean(job && activeStates.has(job.state));
    $("background-job").hidden = !job;
    if (!job) return;
    $("job-state").textContent = t("jobStatus" + job.state);
    $("job-state").dataset.state = job.state;
    $("job-query").textContent = (job.request?.sql || "").replace(/\s+/g, " ").slice(0, 180);
    $("job-message").textContent = message(job);
    $("job-cancel").hidden = !job.id || !["QUEUED", "RUNNING", "CANCELLING"].includes(job.state);
    $("job-cancel").disabled = changing || !connected || job.state === "CANCELLING";
    $("job-open").hidden = job.state !== "SUCCEEDED";
    $("job-open").disabled = busy || !connected;
    $("job-retry").hidden = job.state !== "UNCONFIRMED";
    $("job-retry").disabled = busy || !connected;
    $("job-refresh").hidden = !job.id;
    $("job-refresh").disabled = changing || !connected;
  }

  function retain(identity, previous, response) {
    const job = {
      ...previous,
      id: response.id,
      state: response.job_state,
      operation: response.operation_state,
      error: response.error,
      notice: null,
    };
    if (!Workspace.setJob(identity, job, previous)) return null;
    if (previous.state !== job.state) refreshHistory();
    return job;
  }

  async function refresh() {
    const current = Workspace.current();
    if (!connected || !current?.job?.id) return;
    const job = current.job;
    stopPolling();
    const version = generation,
      revision = connectionRevision;
    try {
      const response = await request(`/data/query-jobs/${encodeURIComponent(job.id)}`);
      if (version !== generation || revision !== connectionRevision) return;
      retain(current.id, job, response);
      failures = 0;
    } catch (error) {
      if (version !== generation || revision !== connectionRevision) return;
      failures++;
      if ([401, 403].includes(error.status)) connected = false;
      Workspace.setJob(current.id, { ...job, notice: "jobStatusUnavailable" }, job);
    } finally {
      if (version === generation && revision === connectionRevision) {
        render();
        schedule();
      }
    }
  }

  async function submit(identity, job) {
    if (busy || !connected) return;
    const revision = connectionRevision;
    stopPolling();
    busy = true;
    posting = identity;
    const persisted = Workspace.setJob(identity, { ...job, state: "SUBMITTING", notice: null });
    $("run").disabled = $("preview").disabled = true;
    render();
    try {
      if (!persisted) {
        const uncertain = job.state === "UNCONFIRMED";
        Workspace.setJob(identity, {
          ...job,
          state: uncertain ? "UNCONFIRMED" : "REJECTED",
          notice: uncertain ? "jobRetrySaveFailed" : "jobSaveFailed",
        });
        return;
      }
      const response = await request("/data/query-jobs", {
        ...job.request,
        idempotency_key: job.key,
      });
      if (revision !== connectionRevision) return;
      retain(identity, job, response);
      if (Workspace.current()?.id === identity) {
        historyParentId = response.history_id;
        Workspace.save();
      }
    } catch (error) {
      if (revision !== connectionRevision) return;
      const rejected =
        job.state !== "UNCONFIRMED" && [400, 401, 403, 404, 409, 422].includes(error.status);
      Workspace.setJob(identity, {
        ...job,
        state: rejected ? "REJECTED" : "UNCONFIRMED",
        notice: [401, 403].includes(error.status) ? "jobNotAuthorized" : null,
      });
    } finally {
      posting = null;
      busy = false;
      $("run").disabled = false;
      updateMode();
      if (revision === connectionRevision) schedule();
    }
  }

  async function start() {
    if (busy || $("mode").value !== "sql" || !connected) return;
    const current = Workspace.current();
    if (!current || activeStates.has(current.job?.state)) return;
    try {
      ensureHistoryScope();
      const editor = $("question");
      const selectedText = editor.value.slice(editor.selectionStart, editor.selectionEnd);
      const sql = selectedText.trim() ? selectedText : editor.value;
      const budget = Number($("budget").value);
      if (!sql.trim()) throw new Error(t("queryRequired"));
      if (!Number.isInteger(budget) || budget < 0 || budget > 1000)
        throw new Error(t("invalidInput"));
      const job = {
        key: crypto.randomUUID(),
        id: null,
        state: "SUBMITTING",
        request: { sql, max_evaluations: budget, parent_history_id: historyParentId },
      };
      Workspace.clearResult(current.id);
      clearOutput();
      $("status").textContent = t("backgroundQuery");
      await submit(current.id, job);
    } catch (error) {
      showError(error);
    }
  }

  async function cancel() {
    const current = Workspace.current();
    if (changing || !connected || !current?.job?.id) return;
    const job = current.job;
    const revision = connectionRevision;
    stopPolling();
    changing = true;
    render();
    try {
      const response = await request(`/data/query-jobs/${encodeURIComponent(job.id)}/cancel`, {});
      if (revision !== connectionRevision) return;
      retain(current.id, job, response);
    } catch {
      if (revision === connectionRevision)
        Workspace.setJob(current.id, { ...job, notice: "jobCancelUnconfirmed" }, job);
    } finally {
      changing = false;
      render();
      if (revision === connectionRevision) schedule();
    }
  }

  async function open(entry) {
    stopPolling();
    const revision = connectionRevision;
    const response = await request(`/data/query-jobs/${encodeURIComponent(entry.query_job_id)}`);
    if (revision !== connectionRevision) return;
    const current = Workspace.current();
    busy = false;
    if (
      current?.job?.id !== response.id ||
      current.question !== entry.input.text ||
      current.mode !== "sql"
    ) {
      if (!Workspace.add(entry.input.text, t("backgroundQuery"), entry.dataset_ids || [], "sql"))
        return;
    }
    restoreHistoryInput(entry, "sql");
    showHistoryContext(entry);
    const job = {
      id: response.id,
      key: null,
      request: {
        sql: entry.input.text,
        max_evaluations: entry.input.max_evaluations ?? 100,
      },
    };
    Workspace.setJob(Workspace.current().id, job);
    retain(Workspace.current().id, job, response);
    if (response.result) show({ ...response.result, history_id: entry.id });
    else {
      clearOutput();
      $("sql").textContent = entry.input.text;
      $("status").textContent = t("jobStatus" + response.job_state);
    }
    Workspace.save();
    render();
    schedule();
  }

  $("run-background").addEventListener("click", start);
  $("job-refresh").addEventListener("click", refresh);
  $("job-cancel").addEventListener("click", cancel);
  $("job-retry").addEventListener("click", () => {
    const current = Workspace.current();
    if (current?.job?.state === "UNCONFIRMED") submit(current.id, current.job);
  });
  $("job-open").addEventListener("click", () => {
    const id = Workspace.current()?.job?.id;
    if (id) openHistory(id);
  });
  document.addEventListener("sdd:document", () => {
    stopPolling();
    render();
    refresh();
  });
  document.addEventListener("sdd:connected", () => {
    connected = true;
    failures = 0;
    render();
    refresh();
  });
  document.addEventListener("visibilitychange", () => {
    stopPolling();
    if (!document.hidden) refresh();
  });
  $("token").addEventListener("input", () => {
    connected = false;
    stopPolling();
    render();
  });
  window.addEventListener("pagehide", stopPolling);
  render();
  return { render, open };
})();
