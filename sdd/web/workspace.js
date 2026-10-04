window.Workspace = (() => {
  const key = "sdd-query-tabs";
  let documents = [],
    active = null,
    restoring = false;
  const results = new Map(),
    expanded = new Set();
  const quote = (name) => '"' + name.replaceAll('"', '""') + '"';

  function capture() {
    const current = documents.find((item) => item.id === active);
    if (!current) return;
    Object.assign(current, {
      name: $("query-name").value,
      question: $("question").value,
      mode: $("mode").value,
      planner: $("planner-mode").value,
      budget: $("budget").value,
      datasets: catalog.length ? selected() : current.datasets || [],
      parent: historyParentId,
    });
  }
  function save() {
    if (restoring) return;
    capture();
    try {
      sessionStorage.setItem(key, JSON.stringify({ documents, active }));
      $("draft-status").textContent = t("sessionSaved");
    } catch {
      $("draft-status").textContent = t("draftUnsaved");
    }
  }
  function tabs() {
    $("query-tabs").replaceChildren();
    for (const item of documents) {
      const group = text($("query-tabs"), "span", "");
      group.className = "query-tab";
      const button = text(
        group,
        "button",
        item.name || t("untitledQuery", { number: documents.indexOf(item) + 1 }),
      );
      button.type = "button";
      button.setAttribute("aria-pressed", String(item.id === active));
      button.addEventListener("click", () => activate(item.id));
      const close = text(group, "button", "×");
      close.type = "button";
      close.title = close.ariaLabel = t("closeQuery");
      close.className = "close-query";
      close.addEventListener("click", () => {
        if (busy) return;
        capture();
        if (item.question.trim() && !confirm(t("closeDraft"))) return;
        documents = documents.filter((entry) => entry.id !== item.id);
        results.delete(item.id);
        if (!documents.length) add();
        else if (item.id === active) activate(documents.at(-1).id);
        else {
          tabs();
          save();
        }
      });
    }
  }
  function scope(item) {
    for (const option of $("datasets").options)
      option.selected = item.datasets.includes(option.value);
    missingHistoryDatasets = item.datasets.some(
      (id) => !catalog.some((dataset) => dataset.id === id),
    );
    $("history-warning").hidden = !missingHistoryDatasets;
    schema();
  }
  function activate(identity) {
    if (busy) return;
    capture();
    restoring = true;
    active = identity;
    const item = documents.find((entry) => entry.id === active);
    resetHistoryContext();
    clearOutput();
    invalidateDraftActions();
    $("question").value = item.question;
    $("query-name").value = item.name;
    $("mode").value = item.mode;
    $("planner-mode").value = item.planner;
    $("budget").value = item.budget;
    historyParentId = item.parent || null;
    if (catalog.length) scope(item);
    updateMode();
    if (results.has(active)) show(results.get(active));
    else $("status").textContent = t("ready");
    restoring = false;
    tabs();
    save();
    $("question").focus();
  }
  function add(question = "", name = "", datasets = [], mode = "sql") {
    if (busy) return false;
    if (documents.length >= 12) {
      $("draft-status").textContent = t("tabLimit");
      return false;
    }
    capture();
    const item = {
      id: crypto.randomUUID(),
      question,
      name: name || t("untitledQuery", { number: documents.length + 1 }),
      mode,
      planner: "jev",
      budget: 100,
      datasets,
      parent: null,
    };
    documents.push(item);
    activate(item.id);
    return true;
  }
  function selectSQL(dataset) {
    const keys = dataset.primary_key.filter((name) =>
      dataset.columns.some((column) => column.name === name),
    );
    return (
      `SELECT\n  ${dataset.columns.map((column) => quote(column.name)).join(",\n  ")}\nFROM ${quote(dataset.name)}` +
      (keys.length ? `\nORDER BY ${keys.map(quote).join(", ")}` : "") +
      "\nLIMIT 100;"
    );
  }
  function catalogBrowser() {
    const filter = $("catalog-search").value.toLocaleLowerCase();
    const choices = selected();
    $("catalog-browser").replaceChildren();
    for (const dataset of catalog.filter((item) =>
      [item.name, ...item.columns.map((column) => column.name)].some((value) =>
        value.toLocaleLowerCase().includes(filter),
      ),
    )) {
      const details = text($("catalog-browser"), "details", "");
      details.className = "catalog-table";
      details.open = expanded.has(dataset.id) || Boolean(filter);
      details.addEventListener("toggle", () => {
        if (details.open) expanded.add(dataset.id);
        else expanded.delete(dataset.id);
      });
      const summary = text(details, "summary", "");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = choices.includes(dataset.id);
      check.ariaLabel = dataset.name;
      check.addEventListener("click", (event) => event.stopPropagation());
      check.addEventListener("change", () => {
        const option = [...$("datasets").options].find((item) => item.value === dataset.id);
        option.selected = check.checked;
        $("datasets").dispatchEvent(new Event("change"));
      });
      summary.append(check);
      text(summary, "span", dataset.name).className = "table-name";
      text(summary, "small", String(dataset.columns.length));
      if (dataset.description) text(details, "p", dataset.description).className = "hint";
      if (!dataset.writable) text(details, "p", t("readOnlyTable")).className = "hint";
      const actions = text(details, "div", "");
      actions.className = "catalog-actions";
      for (const [label, execute] of [
        ["browseRows", true],
        ["writeQuery", false],
      ]) {
        const button = text(actions, "button", t(label));
        button.type = "button";
        button.className = "text-button";
        button.addEventListener("click", () => {
          if (execute) TableView.open(dataset);
          else add(selectSQL(dataset), dataset.name, [dataset.id]);
        });
      }
      const list = text(details, "ul", "");
      list.className = "catalog-columns";
      for (const column of dataset.columns) {
        const row = text(list, "li", "");
        const button = text(row, "button", column.name);
        button.type = "button";
        button.title = [t("insertColumn"), column.description].filter(Boolean).join(" · ");
        button.addEventListener("click", () => {
          if (busy) return;
          if ($("mode").value !== "sql") {
            add(
              `SELECT ${quote(column.name)}\nFROM ${quote(dataset.name)}\nLIMIT 100;`,
              dataset.name,
              [dataset.id],
            );
            return;
          }
          const editor = $("question");
          editor.setRangeText(
            quote(column.name),
            editor.selectionStart,
            editor.selectionEnd,
            "end",
          );
          editor.dispatchEvent(new Event("input"));
          editor.focus();
        });
        text(
          row,
          "small",
          (dataset.primary_key.includes(column.name) ? "◆ " : "") + t(column.type + "Type"),
        );
      }
      for (const link of dataset.links) {
        const target = catalog.find((item) => item.id === link.target_id);
        if (target)
          text(
            details,
            "p",
            `${link.source_column} → ${target.name}.${link.target_column}`,
          ).className = "hint";
      }
    }
    if (!$("catalog-browser").children.length)
      text(
        $("catalog-browser"),
        "p",
        t(catalog.length ? "noCatalogMatches" : "catalogDisconnected"),
      ).className = "hint";
  }

  try {
    const stored = JSON.parse(sessionStorage.getItem(key) || "null");
    if (
      stored?.documents?.length &&
      stored.documents.length <= 12 &&
      stored.documents.every(
        (item) => typeof item.question === "string" && Array.isArray(item.datasets),
      )
    ) {
      documents = stored.documents;
      active = stored.active;
      const first = documents.some((item) => item.id === active) ? active : documents[0].id;
      active = null;
      activate(first);
    }
  } catch {
    documents = [];
    active = null;
  }
  if (!documents.length)
    add(
      $("question").value,
      "",
      draft.datasets || [],
      $("question").value ? $("mode").value : "sql",
    );
  $("new-query-tab").addEventListener("click", () => add());
  $("query-name").addEventListener("input", () => {
    save();
    tabs();
  });
  for (const id of ["question", "mode", "planner-mode", "budget", "datasets"])
    $(id).addEventListener(id === "question" ? "input" : "change", save);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Tab" && $("mode").value === "sql") {
      event.preventDefault();
      const editor = $("question");
      editor.setRangeText("  ", editor.selectionStart, editor.selectionEnd, "end");
      editor.dispatchEvent(new Event("input"));
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      $("save-sql-file").click();
    }
  });
  $("save-sql-file").addEventListener("click", () => {
    save();
    downloadWorkspaceFile(
      ($("query-name").value || "query") + ($("mode").value === "sql" ? ".sql" : ".txt"),
      $("question").value,
      "text/plain;charset=utf-8",
    );
  });
  $("open-sql-file").addEventListener("click", () => $("sql-file").click());
  $("sql-file").addEventListener("change", async () => {
    const file = $("sql-file").files[0];
    if (!file) return;
    try {
      if (file.size > 120000) throw new Error(t("queryFileLimit"));
      const source = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (source.length > 30000) throw new Error(t("queryFileLimit"));
      add(source, file.name.replace(/\.(sql|txt)$/i, ""));
    } catch {
      $("draft-status").textContent = t("queryFileLimit");
    }
    $("sql-file").value = "";
  });
  $("catalog-search").addEventListener("input", catalogBrowser);
  document.addEventListener("sdd:catalog", catalogBrowser);
  document.addEventListener("sdd:connected", () => {
    const item = documents.find((entry) => entry.id === active);
    if (item) scope(item);
  });
  document.addEventListener("sdd:result", (event) => {
    if (!restoring && active) {
      results.set(active, event.detail);
      save();
    }
  });
  window.addEventListener("pagehide", save);
  $("token").addEventListener("input", () => {
    results.clear();
    catalog = [];
    $("datasets").replaceChildren();
    expanded.clear();
    clearOutput();
    $("cell-dialog").close();
    $("cell-value").value = "";
    documents = [];
    active = null;
    const wasBusy = busy;
    busy = false;
    add();
    busy = wasBusy;
    catalogBrowser();
    sessionStorage.removeItem("sdd-workspace");
    sessionStorage.removeItem("sdd-token");
    draft = {};
  });
  catalogBrowser();
  return { add, save };
})();
