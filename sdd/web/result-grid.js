function downloadWorkspaceFile(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name.replace(/[\\/:*?"<>|]/g, "_");
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

window.ResultGrid = (() => {
  let rows = [],
    columns = [],
    page = 0,
    sortColumn = null,
    direction = 1;
  const pageSize = 50;
  const display = (value) =>
    value === null
      ? "NULL"
      : typeof value === "object"
        ? JSON.stringify(value)
        : String(value ?? "");
  const numberParts = (value) => {
    const source = String(value);
    if (source.length > 100 || !/^-?\d+(\.\d+)?$/.test(source)) return null;
    const [whole, fraction = ""] = source.split(".");
    return [BigInt(whole + fraction), fraction.length];
  };
  function compare(left, right) {
    if (left === right) return 0;
    if (left === null || left === undefined) return 1;
    if (right === null || right === undefined) return -1;
    if (typeof left === "number" && typeof right === "number") return (left - right) * direction;
    const a = numberParts(left),
      b = numberParts(right);
    if (a && b) {
      const scale = Math.max(a[1], b[1]);
      const x = a[0] * 10n ** BigInt(scale - a[1]);
      const y = b[0] * 10n ** BigInt(scale - b[1]);
      return (x < y ? -1 : x > y ? 1 : 0) * direction;
    }
    return display(left).localeCompare(display(right), locale) * direction;
  }
  function visibleRows() {
    const filter = $("result-filter").value.toLocaleLowerCase();
    const filtered = rows.filter(
      (row) =>
        !filter || columns.some((key) => display(row[key]).toLocaleLowerCase().includes(filter)),
    );
    return sortColumn ? filtered.sort((a, b) => compare(a[sortColumn], b[sortColumn])) : filtered;
  }
  function inspect(key, value) {
    $("cell-title").textContent = key;
    $("cell-kind").textContent = t(
      value === null ? "nullCell" : typeof value === "string" ? "textCell" : "valueCell",
    );
    $("cell-value").value =
      value === null
        ? "NULL"
        : typeof value === "object"
          ? JSON.stringify(value, null, 2)
          : String(value ?? "");
    $("cell-copy").textContent = t("copyCell");
    $("cell-dialog").showModal();
  }
  function paint() {
    const filtered = visibleRows();
    page = Math.min(page, Math.max(0, Math.ceil(filtered.length / pageSize) - 1));
    $("table").replaceChildren();
    const table = text($("table"), "table", "");
    const head = text(text(table, "thead", ""), "tr", "");
    for (const key of columns) {
      const cell = text(head, "th", "");
      cell.scope = "col";
      cell.setAttribute(
        "aria-sort",
        key === sortColumn ? (direction === 1 ? "ascending" : "descending") : "none",
      );
      const button = text(
        cell,
        "button",
        key + (sortColumn === key ? (direction === 1 ? " ↑" : " ↓") : ""),
      );
      button.type = "button";
      button.className = "column-sort";
      button.addEventListener("click", () => {
        direction = key === sortColumn ? -direction : 1;
        sortColumn = key;
        page = 0;
        paint();
      });
    }
    const body = text(table, "tbody", "");
    for (const row of filtered.slice(page * pageSize, (page + 1) * pageSize)) {
      const tr = text(body, "tr", "");
      for (const key of columns) {
        const value = row[key],
          cell = text(tr, "td", display(value).slice(0, 240));
        cell.tabIndex = 0;
        cell.title = display(value).slice(0, 1000);
        if (value === null) cell.classList.add("null-value");
        cell.addEventListener("click", () => inspect(key, value));
        cell.addEventListener("keydown", (event) => {
          if (event.key === "Enter") inspect(key, value);
        });
      }
    }
    if (!filtered.length) text($("table"), "p", t("noRows")).className = "table-message";
    $("result-range").textContent = $("result-filter").value
      ? t("resultFiltered", { count: filtered.length, total: rows.length })
      : t("resultRange", {
          start: filtered.length ? page * pageSize + 1 : 0,
          end: Math.min((page + 1) * pageSize, filtered.length),
          count: filtered.length,
        });
    $("result-prev").disabled = page === 0;
    $("result-next").disabled = (page + 1) * pageSize >= filtered.length;
    $("export-csv").disabled = $("export-json").disabled = !filtered.length;
  }
  function clear() {
    rows = [];
    columns = [];
    $("result-tools").hidden = $("result-pages").hidden = true;
    $("result-filter").value = "";
  }
  function render(values, keys = [], isPlan = false) {
    rows = values;
    columns = keys.length ? keys : [...new Set(rows.flatMap(Object.keys))];
    page = 0;
    sortColumn = null;
    direction = 1;
    $("result-filter").value = "";
    if (!rows.length && !columns.length) {
      $("table").replaceChildren();
      text($("table"), "p", t(isPlan ? "planRows" : "noRows")).className = "table-message";
      return;
    }
    $("result-tools").hidden = $("result-pages").hidden = false;
    paint();
  }
  function csvCell(value) {
    if (value === null || value === undefined) return "";
    let source = display(value);
    if (typeof value === "string" && (/^[\s]*[=+@]/.test(source) || /^[\s]*-\D/.test(source)))
      source = "'" + source;
    return '"' + source.replaceAll('"', '""') + '"';
  }
  $("result-filter").addEventListener("input", () => {
    page = 0;
    paint();
  });
  $("result-prev").addEventListener("click", () => {
    page--;
    paint();
  });
  $("result-next").addEventListener("click", () => {
    page++;
    paint();
  });
  $("export-csv").addEventListener("click", () => {
    const lines = [
      columns.map(csvCell).join(","),
      ...visibleRows().map((row) => columns.map((key) => csvCell(row[key])).join(",")),
    ];
    downloadWorkspaceFile(
      "query-results.csv",
      "\ufeff" + lines.join("\r\n"),
      "text/csv;charset=utf-8",
    );
  });
  $("export-json").addEventListener("click", () =>
    downloadWorkspaceFile(
      "query-results.json",
      JSON.stringify(visibleRows(), null, 2),
      "application/json",
    ),
  );
  $("cell-close").addEventListener("click", () => $("cell-dialog").close());
  $("cell-copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("cell-value").value);
      $("cell-copy").textContent = t("copiedCell");
    } catch {
      $("cell-kind").textContent = t("copyFailed");
      $("cell-value").select();
    }
  });
  return { render, clear, inspect };
})();
