(() => {
  let content = "",
    columns = null,
    approved = null,
    revision = 0,
    importing = false;
  function invalidate(resetColumns = false) {
    revision++;
    approved = null;
    $("csv-create").disabled = true;
    $("csv-status").textContent = t("csvPreviewRequired");
    if (resetColumns) {
      columns = null;
      $("csv-columns").replaceChildren();
      $("csv-sample").replaceChildren();
    }
  }
  function settings() {
    return {
      name: $("csv-name").value.trim(),
      content,
      columns,
      delimiter: $("csv-delimiter").value === "tab" ? "\t" : $("csv-delimiter").value,
      null_empty: $("csv-null").checked,
    };
  }
  function paint(preview) {
    columns = preview.columns;
    $("csv-columns").replaceChildren();
    for (const column of columns) {
      const label = text($("csv-columns"), "label", "");
      text(label, "span", column.name);
      const select = text(label, "select", "");
      select.ariaLabel = column.name;
      for (const kind of ["text", "integer", "number", "boolean", "date", "datetime", "json"]) {
        select.add(new Option(t(kind + "Type"), kind, false, column.type === kind));
      }
      select.addEventListener("change", () => {
        column.type = select.value;
        invalidate();
      });
    }
    $("csv-sample").replaceChildren();
    const table = text($("csv-sample"), "table", "");
    const head = text(text(table, "thead", ""), "tr", "");
    columns.forEach((column) => {
      text(head, "th", column.name).scope = "col";
    });
    const body = text(table, "tbody", "");
    for (const row of preview.sample) {
      const tr = text(body, "tr", "");
      columns.forEach((column) => {
        const value = row[column.name];
        const cell = text(
          tr,
          "td",
          value === null
            ? "NULL"
            : typeof value === "object"
              ? JSON.stringify(value)
              : String(value),
        );
        if (value === null) cell.className = "null-value";
      });
    }
    $("csv-status").textContent = t("csvSummary", {
      rows: preview.row_count,
      columns: columns.length,
    });
    if (preview.errors.length) {
      text($("csv-status"), "span", t("csvErrors", { count: preview.errors.length })).className =
        "import-error";
      for (const error of preview.errors)
        text(
          $("csv-status"),
          "span",
          t("csvRowError", {
            ...error,
            reason: locale === "en" ? error.reason : t("csvTypeError"),
          }),
        ).className = "import-error";
    }
  }
  $("open-csv").addEventListener("click", () => {
    $("csv-panel").open = true;
    $("csv-panel").scrollIntoView({ block: "start", behavior: "smooth" });
  });
  $("csv-file").addEventListener("change", async () => {
    invalidate(true);
    content = "";
    const generation = revision,
      file = $("csv-file").files[0];
    if (!file) return;
    try {
      if (file.size > 5_000_000) throw new Error(t("csvTooLarge"));
      const source = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (generation !== revision) return;
      content = source;
      $("csv-name").value = file.name.replace(/\.(csv|tsv)$/i, "").slice(0, 120);
    } catch {
      $("csv-status").textContent = t("csvTooLarge");
    }
  });
  $("csv-name").addEventListener("input", () => invalidate());
  $("csv-null").addEventListener("change", () => invalidate());
  $("csv-delimiter").addEventListener("change", () => invalidate(true));
  $("csv-preview").addEventListener("click", async () => {
    if (importing) return;
    invalidate();
    const generation = revision;
    $("csv-preview").disabled = true;
    $("csv-status").textContent = t("csvWorking");
    try {
      const body = settings();
      if (!body.content || !body.name) throw new Error(t("csvChoose"));
      const preview = await request("/datasets/csv/preview", body);
      if (generation !== revision) return;
      paint(preview);
      if (preview.valid) {
        approved = { ...settings(), fingerprint: preview.fingerprint };
        $("csv-create").disabled = false;
      }
    } catch (error) {
      if (generation === revision) $("csv-status").textContent = errorMessage(error);
    } finally {
      $("csv-preview").disabled = false;
    }
  });
  $("csv-create").addEventListener("click", async () => {
    if (!approved || importing) return;
    importing = true;
    const body = approved;
    approved = null;
    const controls = [...$("csv-panel").querySelectorAll("input,select,button")];
    controls.forEach((element) => {
      element.disabled = true;
    });
    $("csv-status").textContent = t("csvImporting");
    try {
      const dataset = await request("/datasets/csv", body);
      $("csv-status").textContent = t("csvImported", { name: dataset.name });
      await connect();
    } catch (error) {
      $("csv-status").textContent = errorMessage(error);
    } finally {
      importing = false;
      controls.forEach((element) => {
        element.disabled = false;
      });
      $("csv-create").disabled = true;
    }
  });
  $("token").addEventListener("input", () => {
    content = "";
    $("csv-file").value = "";
    invalidate(true);
  });
})();
