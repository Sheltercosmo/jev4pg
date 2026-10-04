window.TableView = (() => {
  let dataset = null,
    query = null,
    cursors = [null],
    page = 0,
    response = null,
    generation = 0;
  const dialog = $("table-browser");
  function controls(loading) {
    dialog.querySelectorAll("input,select,button").forEach((element) => {
      element.disabled = loading;
    });
    $("browse-close").disabled = false;
    $("browse-prev").disabled = loading || page === 0;
    $("browse-next").disabled = loading || !response?.has_more;
    $("browse-export").disabled = loading || !response?.result.length;
  }
  async function load(target) {
    const current = ++generation;
    controls(true);
    $("browse-status").textContent = t("loadingPage");
    try {
      const data = await request(`/datasets/${encodeURIComponent(dataset.id)}/scan`, {
        ...query,
        after: cursors[target],
      });
      if (current !== generation) return;
      response = data;
      page = target;
      cursors[page + 1] = data.next_after;
      cursors.length = page + 2;
      $("browse-grid").replaceChildren();
      const table = text($("browse-grid"), "table", "");
      const header = text(text(table, "thead", ""), "tr", "");
      data.columns.forEach((name) => {
        text(header, "th", name).scope = "col";
      });
      const body = text(table, "tbody", "");
      for (const row of data.result) {
        const tr = text(body, "tr", "");
        for (const name of data.columns) {
          const value = row[name];
          const label =
            value === null
              ? "NULL"
              : typeof value === "object"
                ? JSON.stringify(value)
                : String(value);
          const cell = text(tr, "td", label.slice(0, 240));
          cell.tabIndex = 0;
          if (value === null) cell.className = "null-value";
          cell.addEventListener("click", () => ResultGrid.inspect(name, value));
          cell.addEventListener("keydown", (event) => {
            if (event.key === "Enter") ResultGrid.inspect(name, value);
          });
        }
      }
      $("browse-status").textContent =
        t("livePage", { page: page + 1, rows: data.returned_rows, time: data.execution_ms }) +
        (data.has_more ? "" : " · " + t("lastPage"));
    } catch (error) {
      if (current === generation) $("browse-status").textContent = errorMessage(error);
    } finally {
      if (current === generation) controls(false);
    }
  }
  function apply() {
    const names = [...$("browse-columns").querySelectorAll("input:checked")].map(
      (item) => item.value,
    );
    if (!names.length) {
      $("browse-status").textContent = t("chooseOneColumn");
      return;
    }
    let filters = [];
    if ($("browse-column").value) {
      const definition = dataset.columns.find((column) => column.name === $("browse-column").value);
      const op = $("browse-op").value;
      let value = $("browse-value").value;
      if (["is_null", "not_null"].includes(op)) value = null;
      else if (definition.type === "boolean") {
        if (!["true", "false"].includes(value.toLowerCase())) {
          $("browse-status").textContent = t("booleanFilterHint");
          return;
        }
        value = value.toLowerCase() === "true";
      }
      filters = [{ column: definition.name, op, value }];
    }
    query = { columns: names, filters, limit: 100 };
    cursors = [null];
    page = 0;
    response = null;
    $("browse-grid").replaceChildren();
    load(0);
  }
  function open(item) {
    if (busy) return;
    dataset = item;
    $("browse-title").textContent = item.name;
    $("browse-column").replaceChildren(new Option(t("noFilter"), ""));
    $("browse-columns").replaceChildren();
    for (const column of item.columns) {
      $("browse-column").add(new Option(column.name, column.name));
      const label = text($("browse-columns"), "label", "");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.value = column.name;
      check.checked = true;
      label.append(check, document.createTextNode(column.name));
    }
    $("browse-value").value = "";
    $("browse-op").value = "eq";
    dialog.showModal();
    apply();
  }
  $("browse-filter-form").addEventListener("submit", (event) => {
    event.preventDefault();
    apply();
  });
  $("browse-reset").addEventListener("click", () => {
    $("browse-column").value = "";
    $("browse-value").value = "";
    apply();
  });
  $("browse-prev").addEventListener("click", () => load(page - 1));
  $("browse-next").addEventListener("click", () => load(page + 1));
  $("browse-export").addEventListener("click", () =>
    downloadWorkspaceFile(
      dataset.name + "-page.json",
      JSON.stringify(response.result, null, 2),
      "application/json",
    ),
  );
  $("browse-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    generation++;
    response = null;
    $("browse-grid").replaceChildren();
    controls(false);
  });
  $("token").addEventListener("input", () => dialog.close());
  return { open };
})();
