const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { randomUUID } = require("node:crypto");
const { test } = require("node:test");
const vm = require("node:vm");

// Exercise the shipped controllers with local events and fault-injected transport.
// Browser rendering, focus and layout still require a real browser check.
class Element {
  constructor() {
    this.listeners = new Map();
    this.children = [];
    this.options = [];
    this.dataset = {};
    this.value = "";
    this.textContent = "";
    this.selectionStart = this.selectionEnd = 0;
  }
  addEventListener(name, handler) {
    const handlers = this.listeners.get(name) || [];
    handlers.push(handler);
    this.listeners.set(name, handlers);
  }
  dispatchEvent(event) {
    return Promise.all((this.listeners.get(event.type) || []).map((f) => f(event)));
  }
  append(child) {
    this.children.push(child);
  }
  replaceChildren() {
    this.children = [];
  }
  setAttribute(name, value) {
    this[name] = value;
  }
  removeAttribute(name) {
    delete this[name];
  }
  focus() {}
  close() {}
  scrollIntoView() {}
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function fixture(saved = new Map()) {
  const elements = new Map(),
    document = new Element(),
    timers = new Map();
  const calls = [],
    shown = [];
  let sequence = 0;
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  element("mode").value = "sql";
  element("budget").value = "100";
  element("token").value = "synthetic-controller-test";
  document.createElement = () => new Element();
  document.querySelectorAll = () => [];
  document.hidden = false;
  const context = vm.createContext({
    $,
    document,
    crypto: { randomUUID },
    Event,
    Intl,
    Date,
    busy: false,
    connectionRevision: 0,
    catalog: [],
    draft: {},
    pending: null,
    activeReview: null,
    locale: "en",
    confirm: () => true,
    selected: () => [],
    schema: () => {},
    errorMessage: (error) => error.message,
    t: (key) => key,
    setTimeout: (fn) => {
      const id = ++sequence;
      timers.set(id, fn);
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    sessionStorage: {
      getItem: (key) => saved.get(key) || null,
      setItem: (key, value) => {
        if (context.storageFailed) throw new Error("Storage full");
        saved.set(key, value);
      },
      removeItem: (key) => saved.delete(key),
    },
    request: async (path, body) => {
      calls.push({ path, body: body && structuredClone(body) });
      return context.respond(path, body);
    },
    respond: () => {
      throw new Error("Unexpected request");
    },
    text: (parent, tag, value) => {
      const child = new Element();
      child.textContent = value;
      parent.append(child);
      return child;
    },
    clearOutput: () => {
      element("status").textContent = "";
    },
    showError: (error) => {
      element("status").textContent = error.message;
    },
    show: (result) => {
      shown.push(structuredClone(result));
      document.dispatchEvent({ type: "sdd:result", detail: result });
    },
  });
  function $(id) {
    return element(id);
  }
  context.window = context;
  context.addEventListener = () => {};
  context.updateMode = () => context.BackgroundJobs?.render();
  for (const file of ["history.js", "workspace.js", "query-jobs.js"])
    vm.runInContext(readFileSync(join(__dirname, "../../sdd/web", file), "utf8"), context, {
      filename: file,
    });
  context.respond = async (path) => {
    if (path === "/query-history?limit=20") return { items: [], next_cursor: null };
    throw new Error("Unexpected request: " + path);
  };
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  const click = async (id) => {
    assert.ok(!element(id).disabled, id + " is disabled");
    await element(id).dispatchEvent({ type: "click" });
    await flush();
  };
  const input = async (sql) => {
    element("question").value = sql;
    element("question").selectionStart = element("question").selectionEnd = 0;
    await element("question").dispatchEvent({ type: "input" });
  };
  return {
    context,
    element,
    calls,
    saved,
    shown,
    timers,
    flush,
    click,
    input,
    connect: async () => {
      await document.dispatchEvent({ type: "sdd:connected" });
      await flush();
    },
    current: () => context.Workspace.current(),
    job: (state, extra = {}) => ({
      id: "job-1",
      history_id: "job-1",
      job_state: state,
      operation_state: state === "SUCCEEDED" ? "SUCCEEDED" : "NOT_EVALUATED",
      ...extra,
    }),
  };
}

function responses(f, handler) {
  f.context.respond = async (path, body) => {
    if (path === "/query-history?limit=20") return { items: [], next_cursor: null };
    if (path.startsWith("/query-history/"))
      return {
        id: "job-1",
        query_job_id: "job-1",
        dataset_ids: [],
        input: { text: "SELECT 1", mode: "sql" },
        output: { logical_sql: "SELECT 1" },
        created_at: "2026-10-04T12:00:00Z",
      };
    return handler(path, body);
  };
}

test("lost receipt retries the exact selected request and preserves newer draft edits", async () => {
  const f = fixture();
  let attempts = 0;
  responses(f, async (path) => {
    assert.equal(path, "/data/query-jobs");
    if (++attempts === 1) throw new Error("Response lost");
    return f.job("QUEUED");
  });
  await f.connect();
  await f.input("SELECT 1;\nSELECT 2;");
  f.element("question").selectionEnd = 9;
  await f.click("run-background");
  assert.equal(f.current().job.state, "UNCONFIRMED");
  await f.input("SELECT 99;");
  await f.click("job-retry");
  const submissions = f.calls.filter((call) => call.path === "/data/query-jobs");
  assert.equal(submissions.length, 2);
  assert.deepEqual(submissions[0].body, submissions[1].body);
  assert.equal(submissions[1].body.sql, "SELECT 1;");
  assert.equal(f.current().question, "SELECT 99;");
  assert.equal(f.current().job.state, "QUEUED");
  assert.equal(f.shown.length, 0);
});

test("storage failure prevents a fresh dispatch but cannot erase an uncertain submission", async () => {
  const f = fixture();
  responses(f, async () => {
    throw new Error("Lost response");
  });
  await f.connect();
  await f.input("SELECT 1");
  f.context.storageFailed = true;
  await f.click("run-background");
  assert.equal(f.current().job.state, "REJECTED");
  assert.equal(f.calls.filter((call) => call.body).length, 0);
  f.context.storageFailed = false;
  await f.click("run-background");
  const key = f.current().job.key;
  f.context.storageFailed = true;
  await f.click("job-retry");
  assert.equal(f.current().job.state, "UNCONFIRMED");
  assert.equal(f.current().job.key, key);
  assert.equal(f.element("run-background").disabled, true);
  assert.equal(f.calls.filter((call) => call.body).length, 1);
});

for (const status of [400, 401, 403, 404, 409, 422]) {
  test(`HTTP ${status} on recovery cannot prove an earlier submission was rejected`, async () => {
    const f = fixture();
    let attempts = 0;
    responses(f, () => {
      if (++attempts === 1) throw new Error("Lost receipt");
      throw Object.assign(new Error("Recovery denied"), { status });
    });
    await f.connect();
    await f.input("SELECT 1");
    await f.click("run-background");
    const key = f.current().job.key;
    await f.click("job-retry");
    assert.equal(f.current().job.state, "UNCONFIRMED");
    assert.equal(f.current().job.key, key);
    assert.equal(f.element("run-background").disabled, true);
  });
}

test("refresh restores a pending receipt without automatically resubmitting", async () => {
  const f = fixture(),
    receipt = deferred();
  responses(f, () => receipt.promise);
  await f.connect();
  await f.input("SELECT 1");
  const sending = f.click("run-background");
  await f.flush();
  const key = f.current().job.key;
  const restored = fixture(new Map(f.saved));
  assert.equal(restored.current().job.state, "UNCONFIRMED");
  assert.equal(restored.current().job.key, key);
  responses(restored, () => restored.job("SUCCEEDED", { result: { result: [{ value: 1 }] } }));
  await restored.connect();
  assert.equal(restored.calls.filter((call) => call.body).length, 0);
  await restored.click("job-retry");
  assert.equal(restored.calls.find((call) => call.body).body.idempotency_key, key);
  assert.equal(restored.shown.length, 0);
  receipt.resolve(f.job("SUCCEEDED"));
  await sending;
});

test("late polling cannot reverse an accepted cancellation", async () => {
  const f = fixture(),
    oldPoll = deferred();
  responses(f, (path) =>
    path.endsWith("/cancel")
      ? f.job("CANCELLING")
      : path === "/data/query-jobs"
        ? f.job("RUNNING")
        : oldPoll.promise,
  );
  await f.connect();
  await f.input("SELECT 1");
  await f.click("run-background");
  const refreshing = f.click("job-refresh");
  await f.click("job-cancel");
  oldPoll.resolve(f.job("RUNNING"));
  await refreshing;
  assert.equal(f.current().job.state, "CANCELLING");
  assert.equal(f.element("job-cancel").disabled, true);
  responses(f, () => f.job("CANCELLED"));
  await f.click("job-refresh");
  assert.equal(f.current().job.state, "CANCELLED");
  assert.equal(f.shown.length, 0);
  assert.equal(f.timers.size, 0);
});

test("changing identity discards an in-flight submission response", async () => {
  const f = fixture(),
    receipt = deferred();
  responses(f, () => receipt.promise);
  await f.connect();
  await f.input("SELECT 1");
  const sending = f.click("run-background");
  await f.flush();
  f.context.connectionRevision++;
  f.element("token").value = "different-synthetic-identity";
  await f.element("token").dispatchEvent({ type: "input" });
  receipt.resolve(f.job("SUCCEEDED", { result: { result: [{ secret: "old actor" }] } }));
  await sending;
  assert.equal(f.current().job, undefined);
  assert.equal(f.current().question, "");
  assert.equal(f.shown.length, 0);
  assert.equal(f.element("run-background").disabled, true);
});

test("opening saved output preserves an edited draft and never submits SQL", async () => {
  const f = fixture();
  responses(f, () => f.job("SUCCEEDED", { result: { result: [{ value: 1 }] } }));
  await f.connect();
  await f.input("SELECT 1");
  await f.click("run-background");
  const editedId = f.current().id;
  await f.input("SELECT 42");
  await f.click("job-open");
  assert.notEqual(f.current().id, editedId);
  assert.equal(f.current().question, "SELECT 1");
  assert.deepEqual(f.shown.at(-1).result, [{ value: 1 }]);
  const drafts = JSON.parse(f.saved.get("sdd-query-tabs")).documents;
  assert.equal(drafts.find((item) => item.id === editedId).question, "SELECT 42");
  assert.equal(f.calls.filter((call) => call.body).length, 1);
});

test("a history entry recovers a mutation preview without committing it", async () => {
  const f = fixture();
  responses(f, () =>
    f.job("SUCCEEDED", {
      operation_state: "AWAITING_REVIEW",
      result: { mutation_preview: true, preview_token: "review-token", affected_rows: 1 },
    }),
  );
  await f.connect();
  await f.context.openHistory("job-1");
  assert.equal(f.shown.at(-1).preview_token, "review-token");
  assert.equal(f.current().job.operation, "AWAITING_REVIEW");
  assert.equal(f.calls.filter((call) => call.body).length, 0);
});

test("a full tab strip preserves the current draft when a saved result needs another tab", async () => {
  const f = fixture();
  responses(f, () => f.job("SUCCEEDED", { result: { result: [{ value: 1 }] } }));
  await f.connect();
  for (let number = 1; number < 12; number++)
    assert.ok(f.context.Workspace.add(`SELECT ${number}`, `Draft ${number}`));
  const identity = f.current().id;
  await f.context.openHistory("job-1");
  assert.equal(f.current().id, identity);
  assert.equal(f.current().question, "SELECT 11");
  assert.equal(f.element("draft-status").textContent, "tabLimit");
  assert.equal(f.shown.length, 0);
  assert.equal(f.calls.filter((call) => call.body).length, 0);
});

test("closing a draft during cancellation cannot resurrect it from the late response", async () => {
  const f = fixture(),
    cancellation = deferred();
  responses(f, (path) => (path.endsWith("/cancel") ? cancellation.promise : f.job("RUNNING")));
  await f.connect();
  await f.input('SELECT "编号" FROM "工单"');
  await f.click("run-background");
  const identity = f.current().id;
  const cancelling = f.click("job-cancel");
  await f.element("query-tabs").children[0].children[1].dispatchEvent({ type: "click" });
  cancellation.resolve(f.job("CANCELLED"));
  await cancelling;
  assert.notEqual(f.current().id, identity);
  assert.equal(f.current().job, undefined);
  const drafts = JSON.parse(f.saved.get("sdd-query-tabs")).documents;
  assert.equal(
    drafts.some((item) => item.id === identity),
    false,
  );
  assert.equal(f.shown.length, 0);
});

test("a poll that finishes after switching tabs cannot change the new draft", async () => {
  const f = fixture(),
    oldPoll = deferred();
  responses(f, (path) => (path === "/data/query-jobs" ? f.job("RUNNING") : oldPoll.promise));
  await f.connect();
  await f.input("SELECT 1");
  await f.click("run-background");
  const original = f.current().id;
  const polling = f.click("job-refresh");
  assert.ok(f.context.Workspace.add("SELECT 2", "Another draft"));
  oldPoll.resolve(f.job("SUCCEEDED"));
  await polling;
  assert.equal(f.current().question, "SELECT 2");
  assert.equal(f.current().job, undefined);
  assert.equal(f.shown.length, 0);
  responses(f, () => f.job("SUCCEEDED"));
  await f.element("query-tabs").children[0].children[0].dispatchEvent({ type: "click" });
  await f.flush();
  assert.equal(f.current().id, original);
  assert.equal(f.current().job.state, "SUCCEEDED");
  assert.equal(f.current().question, "SELECT 1");
});

test("expired status access keeps server work unresolved until reconnection", async () => {
  const f = fixture();
  responses(f, (path) => {
    if (path === "/data/query-jobs") return f.job("RUNNING");
    throw Object.assign(new Error("Session expired"), { status: 401 });
  });
  await f.connect();
  await f.input("SELECT 1");
  await f.click("run-background");
  await f.click("job-refresh");
  assert.equal(f.current().job.state, "RUNNING");
  assert.equal(f.element("job-cancel").disabled, true);
  assert.equal(f.element("job-refresh").disabled, true);
  assert.equal(f.timers.size, 0);
  responses(f, () => f.job("SUCCEEDED"));
  await f.connect();
  assert.equal(f.current().job.state, "SUCCEEDED");
  assert.equal(f.element("job-open").disabled, false);
  assert.equal(f.calls.filter((call) => call.body).length, 1);
});
