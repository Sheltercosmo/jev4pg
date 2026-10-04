const element = (id) => document.getElementById(id);
let page = 0,
  cursors = [null],
  next = null,
  filters = {},
  loading = false;
async function load(target) {
  if (loading) return;
  loading = true;
  document.querySelectorAll("button,input,select").forEach((control) => {
    control.disabled = true;
  });
  element("status").textContent = "Loading…";
  try {
    const params = new URLSearchParams(filters);
    if (cursors[target] !== null) params.set("after", cursors[target]);
    const response = await fetch("/api/activity?" + params);
    const data = await response.json();
    if (!response.ok)
      throw new Error(typeof data.detail === "string" ? data.detail : "Check the filter values.");
    page = target;
    next = data.next_after?.[0] ?? null;
    cursors[page + 1] = next;
    cursors.length = page + 2;
    element("rows").replaceChildren();
    for (const row of data.result) {
      const tr = document.createElement("tr");
      for (const name of ["id", "category", "amount", "note", "created_at"]) {
        const td = document.createElement("td");
        td.textContent = row[name] ?? "NULL";
        tr.append(td);
      }
      element("rows").append(tr);
    }
    element("status").textContent =
      `Page ${page + 1} · ${data.returned_rows} rows · database ${data.execution_ms} ms` +
      (data.has_more ? "" : " · End of results");
  } catch (error) {
    element("status").textContent = error.message;
  } finally {
    loading = false;
    document.querySelectorAll("button,input,select").forEach((control) => {
      control.disabled = false;
    });
    element("back").disabled = page === 0;
    element("next").disabled = next === null;
  }
}
function restart() {
  filters = {};
  for (const id of ["category", "minimum"])
    if (element(id).value.trim()) filters[id] = element(id).value.trim();
  cursors = [null];
  page = 0;
  next = null;
  element("rows").replaceChildren();
  load(0);
}
element("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  restart();
});
element("refresh").addEventListener("click", restart);
element("back").addEventListener("click", () => load(page - 1));
element("next").addEventListener("click", () => load(page + 1));
load(0);
