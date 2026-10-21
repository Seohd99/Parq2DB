"use strict";

// The API returns error codes and parameters; translate their messages here.
const I18N = { lang: "en", dict: {} };
const LANG_KEY = "parq2db.lang";
const THEME_KEY = "parq2db.theme";
const WORK_TAB_KEY = "parq2db.workTab";
// The release version the user closed the update notice for; the notice returns for a later one.
const UPDATE_DISMISS_KEY = "parq2db.updateDismissed";
const RELEASE_URL_PREFIX = "https://github.com/Seohd99/Parq2DB/releases/tag/";
const WORK_TABS = ["explore", "insert", "create"];
const TABLE_SUGGESTION_LIMIT = 50;
let LANGUAGES = [];
const state = {
  status: null,
  file: null,
  lengthScan: null,
  inspection: null,
  preview: null,
  ddl: null,
  tableNameEdited: false,
  selectedColumns: null,
  job: null,
  // Which tab started the current job: "create" or "insert".
  jobOrigin: null,
  workTab: "explore",
  insert: {
    dialect: null,
    report: null,
    // Source columns to insert; null inserts every matched column.
    selected: null,
    error: null,
    tables: {},
    tablesLoading: {},
    // Table-name suggestions filtered in the browser from the list fetched once per database.
    suggest: { open: false, active: -1, items: [], hidden: 0 },
  },
  connectionTests: {},
  browser: null,
  offset: 0,
  limit: 50,
  error: null,
  activityCount: 0,
  browserBusy: false,
  selectionBusy: false,
  testingDb: {},
  update: null,
};

function t(key, params = {}) {
  const tmpl = I18N.dict[key];
  if (tmpl === undefined) return key;
  const cleanTemplate = tmpl.replace(/\([^()]*\{\w+\}[^()]*\)/g, (group) => {
    const fields = [...group.matchAll(/\{(\w+)\}/g)].map((match) => match[1]);
    return fields.some((field) => params[field] === undefined || params[field] === null) ? "" : group;
  });
  return cleanTemplate.replace(/\{(\w+)\}/g, (_, k) => {
    const value = params[k];
    if (value === undefined || value === null) return "";
    return Array.isArray(value) ? value.join(", ") : String(value);
  }).replace(/[ \t]{2,}/g, " ").replace(/\s+([,.;:!?])/g, "$1").trim();
}

function tCode(item) {
  const code = item && typeof item.code === "string" ? item.code : "E_HTTP_ERROR";
  const params = item?.params && typeof item.params === "object" && !Array.isArray(item.params)
    ? item.params
    : {};
  const prefix = code.startsWith("W_") ? "warning." : "error.";
  return t(prefix + code, params);
}

function preferredLang(languages) {
  for (const candidate of languages) {
    if (typeof candidate !== "string") continue;
    const base = candidate.trim().toLowerCase().replaceAll("_", "-").split("-", 1)[0];
    const match = LANGUAGES.find((language) => language.code.toLowerCase() === base);
    if (match) return match.code;
  }
  return defaultLang();
}

function defaultLang() {
  return LANGUAGES.find((language) => language.code === "en")?.code || LANGUAGES[0]?.code || "en";
}

function browserLang() {
  const languages = Array.isArray(navigator.languages) && navigator.languages.length
    ? navigator.languages
    : [navigator.language];
  return preferredLang(languages);
}

async function setLang(lang, save = true) {
  if (!LANGUAGES.some((language) => language.code === lang)) lang = browserLang();
  const res = await fetch(`/i18n/${lang}.json`);
  if (!res.ok) throw new Error("Could not load the selected language.");
  I18N.dict = await res.json();
  I18N.lang = lang;
  document.documentElement.lang = lang;
  if (save) {
    try { localStorage.setItem(LANG_KEY, lang); } catch (_) { /* Storage may be unavailable. */ }
  }
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
  });
  renderTheme();
  if (state.inspection) populateDdlControls();
  document.getElementById("insert-dialect").dataset.signature = "";
  render();
  renderInsertPlan();
}

function savedLang() {
  try {
    const lang = localStorage.getItem(LANG_KEY);
    if (lang !== null && LANGUAGES.some((language) => language.code === lang)) return lang;
  } catch (_) { /* continue with browser preferences */ }
  return browserLang();
}

function populateLanguageOptions(languages) {
  LANGUAGES = Array.isArray(languages)
    ? languages.filter((language) => language && typeof language.code === "string" && typeof language.native_name === "string")
    : [];
  document.getElementById("lang").replaceChildren(...LANGUAGES.map((language) =>
    el("option", { value: language.code }, language.native_name),
  ));
}

function savedTheme() {
  try {
    const theme = localStorage.getItem(THEME_KEY);
    if (theme === "light" || theme === "dark") return theme;
  } catch (_) { /* Use the system preference when storage is unavailable. */ }
  return null;
}

function currentTheme() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === "light" || explicit === "dark") return explicit;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function renderTheme() {
  const theme = currentTheme();
  const button = document.getElementById("theme-toggle");
  const label = document.getElementById("theme-label");
  if (!button || !label) return;
  label.textContent = t(theme === "dark" ? "ui.theme_dark" : "ui.theme_light");
  button.setAttribute("aria-pressed", String(theme === "dark"));
  button.setAttribute("aria-label", t("ui.theme_toggle"));
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* The current page still changes. */ }
  renderTheme();
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (_) {
    throw { code: "E_HTTP_ERROR", params: {} };
  }
  let body;
  try {
    body = await response.json();
  } catch (_) {
    if (!response.ok) throw { code: "E_HTTP_ERROR", params: {} };
    body = {};
  }
  if (!response.ok) {
    const error = body && typeof body === "object" ? body.error : null;
    throw error && typeof error.code === "string" ? error : { code: "E_INTERNAL", params: {} };
  }
  return body;
}

async function withActivity(operation) {
  state.activityCount += 1;
  renderActivity();
  try {
    return await operation();
  } finally {
    state.activityCount = Math.max(0, state.activityCount - 1);
    renderActivity();
  }
}

async function withBusy(element, operation, refresh = null) {
  if (!element || element.disabled || element.dataset.busy === "true") return;
  const wasDisabled = element.disabled;
  element.disabled = true;
  element.dataset.busy = "true";
  element.setAttribute("aria-busy", "true");
  try {
    return await withActivity(operation);
  } finally {
    if (element.isConnected) {
      delete element.dataset.busy;
      element.removeAttribute("aria-busy");
      element.disabled = wasDisabled;
      if (refresh) refresh();
    }
  }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "disabled" || key === "hidden" || key === "open") node[key] = Boolean(value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function formatNumber(value) {
  return value === null || value === undefined ? t("inspect.unavailable") : new Intl.NumberFormat(I18N.lang).format(value);
}

function formatBytes(value) {
  if (value === null || value === undefined) return t("inspect.unavailable");
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let index = 0;
  while (n >= 1024 && index < units.length - 1) { n /= 1024; index += 1; }
  return `${new Intl.NumberFormat(I18N.lang, { maximumFractionDigits: 2 }).format(n)} ${units[index]}`;
}

function renderUploadFileStatus() {
  const input = document.getElementById("upload-file");
  const status = document.getElementById("upload-file-status");
  if (!input || !status) return;
  const file = input.files?.[0];
  status.textContent = file
    ? t("files.selected_upload_file", { name: file.name, size: formatBytes(file.size) })
    : t("files.no_file_selected");
  status.classList.toggle("has-file", Boolean(file));
  const button = document.getElementById("upload-submit");
  button.disabled = !file || button.dataset.busy === "true";
}

function formatValue(value) {
  if (value === null || value === undefined) return t("inspect.unavailable");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function topLevelFields() {
  return state.inspection?.schema?.fields || [];
}

function isColumnSelected(name) {
  return !state.selectedColumns || state.selectedColumns.has(name);
}

function selectedColumnNames() {
  // null sends "all columns", so the default plan matches the whole file.
  const fields = topLevelFields();
  const chosen = fields.filter((field) => isColumnSelected(field.name)).map((field) => field.name);
  return chosen.length === fields.length ? null : chosen;
}

function ddlOptions() {
  return {
    dialect: document.getElementById("ddl-dialect").value,
    mode: document.getElementById("ddl-mode").value,
    table_name: document.getElementById("ddl-table-name").value,
    column_policy: document.getElementById("ddl-column-policy").value,
    string_strategy: document.getElementById("ddl-string-strategy").value,
    length_scan: document.getElementById("ddl-length-scan").value,
    explode_column: document.getElementById("ddl-explode-column").value || null,
    preserve_empty: document.getElementById("ddl-preserve-empty").checked,
    parent_key: document.getElementById("ddl-parent-key").value || null,
    json_check: document.getElementById("ddl-json-check").checked,
    columns: selectedColumnNames(),
    preview_offset: 0,
    preview_limit: 25,
  };
}

function updateDdlControls() {
  const mode = document.getElementById("ddl-mode").value;
  const dialect = document.getElementById("ddl-dialect").value;
  document.getElementById("ddl-parent-key").disabled = mode !== "child";
  document.getElementById("ddl-explode-column").disabled = mode !== "explode";
  document.getElementById("ddl-preserve-empty").disabled = mode !== "explode";
  document.getElementById("ddl-json-check").disabled = dialect !== "oracle";
  document.getElementById("ddl-table-name").maxLength = dialect === "oracle" ? 128 : dialect === "postgres" ? 63 : 64;
  if (state.file && !state.tableNameEdited) {
    document.getElementById("ddl-table-name").value = state.file.default_table_names[dialect];
  }
}

function updateDdlOptions(event) {
  if (event.target.id === "ddl-table-name") state.tableNameEdited = true;
  state.ddl = null;
  updateDdlControls();
  renderDdl();
}

function populateDdlControls() {
  const fields = state.inspection?.schema?.fields || [];
  const keySelect = document.getElementById("ddl-parent-key");
  const explodeSelect = document.getElementById("ddl-explode-column");
  const oldKey = keySelect.value;
  const oldExplode = explodeSelect.value;
  keySelect.replaceChildren(el("option", { value: "" }, t("ddl.generated_key")));
  explodeSelect.replaceChildren(el("option", { value: "" }, t("ddl.choose_column")));
  for (const field of fields) {
    // A parent key or explode column must be one of the selected columns.
    if (!isColumnSelected(field.name)) continue;
    if (field.kind === "scalar") keySelect.append(el("option", { value: field.name }, field.name));
    if (field.kind === "list") explodeSelect.append(el("option", { value: field.name }, field.name));
  }
  if ([...keySelect.options].some((option) => option.value === oldKey)) keySelect.value = oldKey;
  if ([...explodeSelect.options].some((option) => option.value === oldExplode)) explodeSelect.value = oldExplode;
  renderColumnPicker();
  updateDdlControls();
}

function renderColumnPicker() {
  const fields = topLevelFields();
  const selected = fields.filter((field) => isColumnSelected(field.name)).length;
  document.getElementById("ddl-columns-summary").textContent = t("ddl.columns_summary", {
    selected,
    total: fields.length,
  });
  document.getElementById("ddl-column-list").replaceChildren(...fields.map((field) => {
    const box = el("input", { type: "checkbox", value: field.name });
    box.checked = isColumnSelected(field.name);
    box.addEventListener("change", () => setColumnSelected(field.name, box.checked));
    return el("label", {}, box,
      el("span", { class: "column-name" }, field.name),
      el("span", { class: "column-kind" }, field.kind));
  }));
}

function setColumnSelected(name, checked) {
  if (!state.selectedColumns) state.selectedColumns = new Set(topLevelFields().map((field) => field.name));
  if (checked) state.selectedColumns.add(name);
  else state.selectedColumns.delete(name);
  columnSelectionChanged();
}

function setAllColumnsSelected(checked) {
  state.selectedColumns = new Set(checked ? topLevelFields().map((field) => field.name) : []);
  columnSelectionChanged();
}

function columnSelectionChanged() {
  state.ddl = null;
  populateDdlControls();
  renderDdl();
}

function renderDdl() {
  const result = state.ddl;
  const warningList = document.getElementById("ddl-warnings");
  const measurementBadge = document.getElementById("ddl-measurement");
  warningList.replaceChildren();
  const mappingRoot = document.getElementById("ddl-mappings");
  const mappingContent = document.getElementById("ddl-mapping-content");
  const previewRoot = document.getElementById("flatten-previews");
  const previewContent = document.getElementById("flatten-preview-content");
  const output = document.getElementById("ddl-output");
  mappingContent.replaceChildren();
  previewContent.replaceChildren();
  output.textContent = result?.ddl || "";
  document.getElementById("ddl-result-tools").hidden = !result;
  output.hidden = !result;
  measurementBadge.hidden = !result;
  if (result) {
    const scanStatus = result.length_scan_status?.status || state.lengthScan?.status;
    const key = result.length_measurement === "exact"
      ? "ddl.measurement.exact"
      : scanStatus === "failed"
        ? "ddl.measurement.failed"
        : ddlOptions().length_scan === "sample"
          ? "ddl.measurement.sampled"
          : "ddl.measurement.estimated";
    measurementBadge.textContent = t(key);
    measurementBadge.dataset.status = result.length_measurement;
  } else {
    measurementBadge.textContent = "";
    delete measurementBadge.dataset.status;
  }
  document.getElementById("ddl-copy").disabled = !result;
  document.getElementById("ddl-download").disabled = !result;
  mappingRoot.hidden = !result || result.column_mappings.length === 0;
  previewRoot.hidden = !result || result.previews.length === 0;
  if (!result) {
    renderLoad();
    return;
  }

  warningList.replaceChildren(...result.warnings.map((item) => el("li", {}, tCode(item))));
  const mappingTable = el("table", {},
    el("thead", {}, el("tr", {},
      el("th", { text: t("ddl.table") }),
      el("th", { text: t("ddl.source_column") }),
      el("th", { text: t("ddl.target_column") }),
    )),
  );
  const mappingBody = el("tbody", {});
  for (const mapping of result.column_mappings) {
    mappingBody.append(el("tr", {},
      el("td", {}, el("code", {}, mapping.table)),
      el("td", {}, el("code", {}, mapping.original_path || mapping.original_name)),
      el("td", {}, el("code", {}, mapping.name), mapping.generated ? ` · ${t("ddl.generated_key")}` : ""),
    ));
  }
  mappingTable.append(mappingBody);
  mappingContent.append(mappingTable);

  const previews = previewContent;
  for (const preview of result.previews) {
    const section = el("section", { class: "flatten-preview" });
    section.append(el("div", { class: "section-heading" },
      el("h3", {}, el("code", {}, preview.name)),
      el("span", { class: "muted" }, t("ddl.row_count", {
        actual: formatNumber(preview.actual_rows),
        expected: formatNumber(preview.expected_rows),
      })),
    ));
    if (preview.rows.length === 0) {
      section.append(el("p", { class: "muted" }, t("ddl.no_rows")));
    } else {
      const table = el("table", { class: "preview-table" }, el("thead", {}, el("tr", {}, ...preview.columns.map((name) => el("th", { title: name }, name)))));
      const body = el("tbody", {});
      for (const row of preview.rows) {
        body.append(el("tr", {}, ...preview.columns.map((name) => {
          const value = row[name];
          const text = value === null || value === undefined
            ? t("inspect.null")
            : typeof value === "object" ? JSON.stringify(value) : String(value);
          return el("td", { title: text }, text);
        })));
      }
      table.append(body);
      section.append(el("div", { class: "table-wrap" }, table));
    }
    previews.append(section);
  }
  renderLoad();
}

function showError(error) {
  state.error = error && typeof error.code === "string" ? error : { code: "E_HTTP_ERROR", params: {} };
  renderFeedback();
}

function renderFeedback() {
  const feedback = document.getElementById("feedback");
  feedback.textContent = state.error ? tCode(state.error) : "";
  feedback.classList.toggle("error", Boolean(state.error));
}

function renderActivity() {
  const activity = document.getElementById("activity");
  if (!activity) return;
  activity.hidden = state.activityCount === 0;
  activity.textContent = state.activityCount ? t("ui.loading") : "";
}

function renderStatus() {
  if (!state.status) return;
  document.getElementById("version").textContent = `v${state.status.version}`;
  const list = document.getElementById("db-list");
  const offlineReasons = document.getElementById("db-offline-reasons");
  const offlineDetails = document.getElementById("db-offline-details");
  const offline = [];
  list.replaceChildren();
  for (const [kind, db] of Object.entries(state.status.db)) {
    const badge = el("span", { class: db.online ? "badge ok" : "badge off" }, t(db.online ? "status.online" : "status.offline"));
    const testButton = el("button", {
      type: "button",
      class: `db-test-button${state.testingDb[kind] ? " is-loading" : ""}`,
      "data-test-db": kind,
      disabled: !db.online || Boolean(state.testingDb[kind]),
      "aria-busy": Boolean(state.testingDb[kind]),
    }, t("status.test_connection"));
    const parts = [el("strong", {}, t(`db.${kind}`)), badge, testButton];
    if (state.connectionTests[kind]) {
      parts.push(el("span", { class: "muted" }, t("status.test_result", {
        version: state.connectionTests[kind].server_version,
      })));
    }
    list.append(el("li", {}, ...parts));
    if (!db.online) offline.push([kind, db]);
  }
  offlineReasons.replaceChildren(...offline.map(([kind, db]) => el("li", {}, t("status.offline_reason", {
    section: `db.${kind}`,
    reason: db.reason ? tCode(db.reason) : t("status.offline"),
  }))));
  offlineDetails.hidden = offline.length === 0;
  document.getElementById("config-warnings").replaceChildren(
    ...state.status.warnings.map((warning) => el("li", {}, tCode(warning))),
  );
}

function jobActive() {
  return Boolean(state.job && ["queued", "running"].includes(state.job.status));
}

function renderLoad() {
  const kind = document.getElementById("ddl-dialect").value;
  const profile = state.status?.db?.[kind];
  const offline = !profile || !profile.online;
  const scan = state.lengthScan;
  const scanPending = Boolean(state.file && scan?.status !== "ready");
  document.getElementById("load-start").disabled = !state.file || offline || jobActive() || scanPending;
  document.getElementById("load-offline-reason").textContent = offline
    ? t("load.offline", { reason: profile?.reason ? tCode(profile.reason) : t("status.offline") })
    : "";
  const offlineHelp = document.getElementById("load-offline-help");
  offlineHelp.hidden = !offline;
  offlineHelp.textContent = offline ? t("load.offline_help") : "";
  const measurementWait = document.getElementById("load-measurement-wait");
  measurementWait.hidden = !scanPending;
  if (scanPending) {
    measurementWait.textContent = scan?.status === "failed"
      ? t("load.scan_failed", { reason: scan.error ? tCode(scan.error) : t("error.E_STRING_SCAN_FAILED") })
      : t("load.scan_wait", { progress: scan?.progress_percent ?? 0 });
  } else {
    measurementWait.textContent = "";
  }
  renderJobs();
}

// Local receipt time of each job snapshot, so the elapsed time can tick between polls.
const jobReceivedAt = new WeakMap();
let jobElapsedShown = { jobId: null, seconds: 0 };
let jobElapsedTimer = null;

function isActiveJob(job) {
  return ["queued", "running"].includes(job?.status);
}

function jobElapsedText(job) {
  if (!jobReceivedAt.has(job)) jobReceivedAt.set(job, performance.now());
  const active = isActiveJob(job);
  const sinceSnapshot = active ? (performance.now() - jobReceivedAt.get(job)) / 1000 : 0;
  let seconds = Math.floor((job.elapsed_seconds || 0) + sinceSnapshot);
  // A newer snapshot can trail the local estimate by the request latency; never count down.
  if (active && jobElapsedShown.jobId === job.job_id) seconds = Math.max(seconds, jobElapsedShown.seconds);
  jobElapsedShown = { jobId: job.job_id, seconds };
  return t("load.elapsed", { seconds: formatNumber(seconds) });
}

function tickJobElapsed() {
  if (!isActiveJob(state.job)) {
    window.clearInterval(jobElapsedTimer);
    jobElapsedTimer = null;
    return;
  }
  const text = jobElapsedText(state.job);
  for (const node of document.querySelectorAll("[data-job-elapsed]")) node.textContent = text;
}

function renderJobs() {
  for (const origin of ["create", "insert"]) {
    const root = document.getElementById(`${origin}-job`);
    const job = state.jobOrigin === origin ? state.job : null;
    root.replaceChildren(...(job ? [jobView(job)] : []));
  }
  if (isActiveJob(state.job) && jobElapsedTimer === null) {
    jobElapsedTimer = window.setInterval(tickJobElapsed, 250);
  }
}

function jobView(job) {
  const active = ["queued", "running"].includes(job.status);
  const stage = job.stage === "measuring"
    ? t("load.stage.measuring", { progress: job.measurement_progress_percent ?? 0 })
    : job.stage === "loading"
      ? t(job.existing_policy === "insert_only" ? "load.stage.inserting" : "load.stage.loading")
      : t("load.stage.queued");
  const progress = (job.progress || []).map((item) => {
    const expected = item.rows_expected;
    return el("div", { class: "load-progress" },
      el("strong", {}, item.table),
      el("progress", {
        max: expected && expected > 0 ? expected : 1,
        value: expected && expected > 0 ? Math.min(item.rows_done, expected) : 0,
      }),
      el("span", { class: "muted" }, t("load.rows_progress", {
        done: formatNumber(item.rows_done),
        expected: formatNumber(expected),
      })),
    );
  });
  const results = (job.result?.tables || []).map((item) => el("div", {
    class: `load-result ${item.matched ? "matched" : "mismatch"}`,
  },
  el("strong", {}, item.table),
  el("span", {}, t("load.count_result", {
    expected: formatNumber(item.expected_rows),
    actual: formatNumber(item.actual_rows),
  })),
  ));
  return el("div", { class: "load-job" },
    el("div", { class: "section-heading load-job-heading" },
      el("strong", {}, t(`load.status.${job.status}`)),
      el("span", { class: "muted", "data-job-elapsed": "" }, jobElapsedText(job)),
      active ? el("button", { type: "button", class: "danger-button job-cancel", "data-job-cancel": job.job_id }, t("load.cancel")) : null,
    ),
    active ? el("p", { class: "muted" }, stage) : null,
    el("div", { class: "load-progress-list" }, ...progress),
    el("ul", { class: "warnings" }, ...(job.error ? [el("li", {}, tCode(job.error))] : [])),
    el("div", { class: "load-results" }, ...results),
  );
}

function insertDialect() {
  const dbs = state.status?.db || {};
  if (state.insert.dialect && dbs[state.insert.dialect]) return state.insert.dialect;
  return Object.keys(dbs).find((kind) => dbs[kind].online) || Object.keys(dbs)[0] || "oracle";
}

function insertProfile() {
  return state.status?.db?.[insertDialect()];
}

function matchedInsertColumns() {
  return (state.insert.report?.columns || []).filter((column) => column.source !== null);
}

function isInsertSelected(source) {
  return !state.insert.selected || state.insert.selected.has(source);
}

function insertColumnNames() {
  // null inserts every matched column, matching the server default.
  const matched = matchedInsertColumns();
  const chosen = matched.filter((column) => isInsertSelected(column.source)).map((column) => column.source);
  return chosen.length === matched.length ? null : chosen;
}

function insertChosenCount() {
  return matchedInsertColumns().filter((column) => isInsertSelected(column.source)).length;
}

function renderInsertControls() {
  const select = document.getElementById("insert-dialect");
  const dbs = Object.entries(state.status?.db || {});
  const kind = insertDialect();
  // Rebuild options only when they change, so an open dropdown is not reset by polling.
  const signature = `${I18N.lang}|${dbs.map(([name, db]) => `${name}:${db.online}`).join(",")}`;
  if (select.dataset.signature !== signature) {
    select.replaceChildren(...dbs.map(([name, db]) => el("option", { value: name },
      db.online ? t(`db.${name}`) : t("insert.dialect_offline", { name: t(`db.${name}`) }))));
    select.dataset.signature = signature;
  }
  if (dbs.length) select.value = kind;
  const profile = insertProfile();
  const offline = !profile || !profile.online;
  document.getElementById("insert-offline-reason").textContent = offline
    ? t("load.offline", { reason: profile?.reason ? tCode(profile.reason) : t("status.offline") })
    : "";
  const offlineHelp = document.getElementById("insert-offline-help");
  offlineHelp.hidden = !offline;
  offlineHelp.textContent = offline ? t("load.offline_help") : "";

  const tableName = document.getElementById("insert-table").value.trim();
  const check = document.getElementById("insert-check");
  if (check.dataset.busy !== "true") check.disabled = !state.file || offline || !tableName;

  const feedback = document.getElementById("insert-feedback");
  feedback.textContent = state.insert.error ? tCode(state.insert.error) : "";
  feedback.classList.toggle("error", Boolean(state.insert.error));

  const report = state.insert.report;
  const start = document.getElementById("insert-start");
  const count = insertChosenCount();
  start.textContent = report
    ? t("insert.start", { table: report.table, rows: formatNumber(report.source_rows), columns: count })
    : t("insert.start_short");
  if (start.dataset.busy !== "true") start.disabled = !report || !state.file || offline || count === 0 || jobActive();
}

function insertValueLabel(column) {
  if (column.source !== null && isInsertSelected(column.source)) {
    return [column.source_kind === "scalar" ? t("insert.value_file") : t("insert.value_json"), ""];
  }
  if (column.generated) return [t("insert.value_generated"), ""];
  if (column.has_default) return [t("insert.value_default"), ""];
  if (column.nullable) return [t("insert.value_null"), ""];
  return [t("insert.value_missing"), "insert-value-missing"];
}

function renderInsertPlan() {
  const report = state.insert.report;
  document.getElementById("insert-plan").hidden = !report;
  renderInsertControls();
  if (!report) return;
  const matched = matchedInsertColumns();
  document.getElementById("insert-plan-title").textContent = report.table;
  document.getElementById("insert-plan-summary").textContent = t("insert.summary", {
    matched: matched.length,
    columns: report.columns.length,
    rows: formatNumber(report.source_rows),
  });
  const body = document.getElementById("insert-columns-body");
  body.replaceChildren(...report.columns.map((column) => {
    const chosen = column.source !== null && isInsertSelected(column.source);
    const box = el("input", {
      type: "checkbox",
      "data-insert-source": column.source ?? "",
      disabled: column.source === null,
      "aria-label": column.name,
    });
    box.checked = chosen;
    const [value, valueClass] = insertValueLabel(column);
    return el("tr", { class: chosen ? "" : "insert-row-skipped" },
      el("td", { class: "insert-check-col" }, box),
      el("td", {}, el("code", {}, column.name)),
      el("td", {}, el("code", { class: "muted" }, column.data_type)),
      el("td", {}, column.source !== null ? el("code", {}, column.source) : el("span", { class: "muted" }, "—")),
      el("td", { class: valueClass }, value),
    );
  }));
  const all = document.getElementById("insert-columns-all");
  const count = insertChosenCount();
  all.disabled = matched.length === 0;
  all.checked = matched.length > 0 && count === matched.length;
  all.indeterminate = count > 0 && count < matched.length;

  const warnings = [];
  if (matched.length === 0) warnings.push({ code: "E_INSERT_NO_MATCHING_COLUMNS", params: {} });
  if (report.file_only.length) {
    warnings.push({ code: "W_INSERT_FILE_COLUMNS_SKIPPED", params: {
      table: report.table, columns: report.file_only.map((item) => item.source),
    } });
  }
  const missing = report.columns
    .filter((column) => column.required && !(column.source !== null && isInsertSelected(column.source)))
    .map((column) => column.name);
  if (missing.length) {
    warnings.push({ code: "W_INSERT_REQUIRED_COLUMNS", params: { table: report.table, columns: missing } });
  }
  document.getElementById("insert-warnings").replaceChildren(...warnings.map((item) => el("li", {}, tCode(item))));
}

function setInsertSelected(source, checked) {
  const matched = matchedInsertColumns().map((column) => column.source);
  if (!state.insert.selected) state.insert.selected = new Set(matched);
  if (checked) state.insert.selected.add(source);
  else state.insert.selected.delete(source);
  renderInsertPlan();
}

function resetInsertPlan() {
  state.insert.report = null;
  state.insert.selected = null;
  state.insert.error = null;
}

async function ensureInsertTables(kind = insertDialect()) {
  const profile = state.status?.db?.[kind];
  if (!profile?.online || state.insert.tables[kind] || state.insert.tablesLoading[kind]) return;
  state.insert.tablesLoading[kind] = true;
  try {
    const result = await api(`/api/db/${encodeURIComponent(kind)}/tables`);
    state.insert.tables[kind] = result.tables || [];
  } catch (_) {
    // Suggestions are optional; the table can still be typed and checked.
    state.insert.tables[kind] = [];
  } finally {
    state.insert.tablesLoading[kind] = false;
  }
  suggestInsertTable(kind);
  renderInsertControls();
  if (document.activeElement?.id === "insert-table" && kind === insertDialect()) openTableSuggestions();
}

function filterTableNames(names, query) {
  // Prefix matches first, then names that contain the query; both ignore case.
  const needle = query.trim().toLowerCase();
  if (!needle) return names;
  const prefix = [];
  const contains = [];
  for (const name of names) {
    const index = name.toLowerCase().indexOf(needle);
    if (index === 0) prefix.push(name);
    else if (index > 0) contains.push(name);
  }
  return [...prefix, ...contains];
}

function highlightMatch(name, query) {
  const needle = query.trim().toLowerCase();
  const index = needle ? name.toLowerCase().indexOf(needle) : -1;
  if (index < 0) return [name];
  return [name.slice(0, index), el("mark", {}, name.slice(index, index + needle.length)), name.slice(index + needle.length)];
}

function openTableSuggestions() {
  const input = document.getElementById("insert-table");
  const matches = filterTableNames(state.insert.tables[insertDialect()] || [], input.value);
  const suggest = state.insert.suggest;
  suggest.items = matches.slice(0, TABLE_SUGGESTION_LIMIT);
  suggest.hidden = matches.length - suggest.items.length;
  suggest.active = -1;
  // An exact match alone needs no list.
  suggest.open = suggest.items.length > 0
    && !(suggest.items.length === 1 && suggest.items[0].toLowerCase() === input.value.trim().toLowerCase());
  renderTableSuggestions();
}

function closeTableSuggestions() {
  state.insert.suggest.open = false;
  state.insert.suggest.active = -1;
  renderTableSuggestions();
}

function renderTableSuggestions() {
  const input = document.getElementById("insert-table");
  const list = document.getElementById("insert-table-list");
  const suggest = state.insert.suggest;
  list.hidden = !suggest.open;
  input.setAttribute("aria-expanded", String(suggest.open));
  if (!suggest.open) {
    input.removeAttribute("aria-activedescendant");
    list.replaceChildren();
    return;
  }
  const query = input.value;
  const options = suggest.items.map((name, index) => el("li", {
    id: `insert-table-option-${index}`,
    role: "option",
    class: index === suggest.active ? "active" : "",
    "aria-selected": String(index === suggest.active),
    "data-table": name,
  }, ...highlightMatch(name, query)));
  if (suggest.hidden > 0) {
    options.push(el("li", { class: "combo-more muted", role: "presentation" }, t("insert.more_tables", { count: formatNumber(suggest.hidden) })));
  }
  list.replaceChildren(...options);
  if (suggest.active >= 0) {
    input.setAttribute("aria-activedescendant", `insert-table-option-${suggest.active}`);
    document.getElementById(`insert-table-option-${suggest.active}`)?.scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
}

function chooseTableSuggestion(name) {
  const input = document.getElementById("insert-table");
  input.value = name;
  closeTableSuggestions();
  resetInsertPlan();
  renderInsertPlan();
  // Picking a table from the list checks it right away.
  document.getElementById("insert-check").click();
}

function suggestInsertTable(kind = insertDialect()) {
  // Prefill the file's default table name only when such a table already exists.
  const input = document.getElementById("insert-table");
  if (input.value.trim() || !state.file) return;
  const wanted = state.file.default_table_names?.[kind];
  if (!wanted) return;
  const match = (state.insert.tables[kind] || []).find((name) => name.toLowerCase() === wanted.toLowerCase());
  if (match) input.value = match;
}

function savedWorkTab() {
  try {
    const value = localStorage.getItem(WORK_TAB_KEY);
    return WORK_TABS.includes(value) ? value : "explore";
  } catch (_) {
    return "explore";
  }
}

function setWorkTab(tab, save = true) {
  if (!WORK_TABS.includes(tab)) return;
  state.workTab = tab;
  for (const name of WORK_TABS) {
    const selected = name === tab;
    const button = document.getElementById(`work-tab-${name}`);
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    document.getElementById(`work-${name}`).hidden = !selected;
  }
  if (save) {
    try { localStorage.setItem(WORK_TAB_KEY, tab); } catch (_) { /* Storage may be unavailable. */ }
  }
  if (tab === "insert") {
    suggestInsertTable();
    void ensureInsertTables();
    renderInsertControls();
  }
}

function renderBrowser() {
  const browser = state.browser;
  const roots = document.getElementById("browser-roots");
  const entries = document.getElementById("browser-entries");
  roots.replaceChildren();
  entries.replaceChildren();
  if (!browser) return;

  if (browser.roots.length === 0) {
    roots.append(el("p", { class: "muted", "data-i18n": "files.no_roots" }, t("files.no_roots")));
  } else {
    roots.append(el("div", { class: "entry-heading" }, t("files.allowed_roots")));
    for (const root of browser.roots) {
      roots.append(el("button", { class: "entry root-entry", type: "button", "data-path": root, disabled: state.browserBusy || state.selectionBusy }, root));
    }
  }

  document.getElementById("browser-current").textContent = browser.current || "";
  document.getElementById("browser-up").disabled = !browser.parent || state.browserBusy || state.selectionBusy;
  if (!browser.current) return;
  if (browser.entries.length === 0) {
    entries.append(el("p", { class: "muted", "data-i18n": "files.empty" }, t("files.empty")));
    return;
  }
  for (const entry of browser.entries) {
    const isDirectory = entry.kind === "directory";
    const button = el("button", {
      class: `entry ${isDirectory ? "directory" : "file"}`,
      type: "button",
      "data-path": entry.path,
      "data-kind": entry.kind,
      disabled: state.browserBusy || state.selectionBusy,
    });
    button.append(el("span", {}, `${isDirectory ? "▸ " : "▧ "}${entry.name}`));
    if (!isDirectory) button.append(el("span", { class: "entry-size muted" }, formatBytes(entry.size)));
    entries.append(button);
  }
}

function schemaNode(field) {
  const label = el("span", { class: "schema-name" }, field.name);
  const type = el("span", { class: "schema-type" }, field.type);
  const nullable = el("span", { class: "muted schema-nullable" }, t(field.nullable ? "inspect.nullable" : "inspect.required"));
  const summary = el("summary", {}, label, type, nullable);
  const children = field.children || [];
  if (children.length === 0) return el("div", { class: "schema-leaf" }, label, type, nullable);
  const details = el("details", { class: `schema-node kind-${field.kind}` }, summary);
  for (const child of children) details.append(schemaNode(child));
  return details;
}

function renderInspection() {
  const section = document.getElementById("inspection");
  section.hidden = !state.file || !state.inspection;
  if (!state.file || !state.inspection) return;
  const data = state.inspection;
  const meta = data.metadata;
  const cards = [
    ["inspect.rows", formatNumber(meta.num_rows)],
    ["inspect.row_groups", formatNumber(meta.num_row_groups)],
    ["inspect.file_size", formatBytes(meta.file_size)],
    ["inspect.compression", meta.compression.join(", ") || t("inspect.unavailable")],
    ["inspect.created_by", meta.created_by || t("inspect.unavailable")],
    ["inspect.format_version", meta.format_version || t("inspect.unavailable")],
  ];
  document.getElementById("metadata-cards").replaceChildren(...cards.map(([key, value]) =>
    el("div", { class: "metadata-card" }, el("span", { class: "muted" }, t(key)), el("strong", {}, value)),
  ));

  const nested = data.classification;
  const badge = document.getElementById("type-badge");
  badge.className = `badge ${nested.kind === "nested" ? "nested" : "flat"}`;
  badge.textContent = t(`inspect.kind.${nested.kind}`);
  const nestedInfo = document.getElementById("nested-info");
  nestedInfo.replaceChildren(el("strong", {}, t("inspect.nested_columns")));
  if (nested.nested_columns.length === 0) {
    nestedInfo.append(el("span", { class: "muted" }, t("inspect.no_nested_columns")));
  } else {
    const list = el("ul", {});
    for (const column of nested.nested_columns) {
      list.append(el("li", {}, el("code", {}, column.name), " ", t(`inspect.kind.${column.kind}`), " · ", t("inspect.depth", { depth: column.max_depth })));
    }
    nestedInfo.append(list);
  }

  const statisticsBody = document.getElementById("statistics-body");
  statisticsBody.replaceChildren(...data.statistics.map((stat) => el("tr", {},
    el("td", {}, el("code", {}, stat.column)),
    el("td", {}, formatNumber(stat.null_count)),
    el("td", {}, formatValue(stat.min)),
    el("td", {}, formatValue(stat.max)),
    el("td", {}, stat.compression.join(", ") || t("inspect.unavailable")),
  )));
  document.getElementById("schema-tree").replaceChildren(
    ...(data.schema.fields || []).map(schemaNode),
  );
  renderPreview();
}

function renderPreview() {
  const preview = state.preview;
  if (!preview) return;
  const table = document.getElementById("preview-table");
  const thead = el("thead", {}, el("tr", {}, ...preview.columns.map((column) => el("th", { title: column }, column))));
  const tbody = el("tbody", {});
  for (const row of preview.rows) {
    const cells = preview.columns.map((column) => {
      const value = row[column];
      if (value !== null && typeof value === "object") {
        const details = el("details", { class: "json-value" }, el("summary", {}, t("inspect.view_json")));
        const fullValue = JSON.stringify(value, null, 2);
        details.append(el("pre", {}, fullValue));
        return el("td", { title: fullValue }, details);
      }
      const text = value === null || value === undefined ? t("inspect.null") : String(value);
      return el("td", { title: text }, text);
    });
    tbody.append(el("tr", {}, ...cells));
  }
  table.classList.add("preview-table");
  table.replaceChildren(thead, tbody);
  const total = preview.total_rows;
  const first = total === 0 ? 0 : preview.offset + 1;
  const last = Math.min(preview.offset + preview.rows.length, total);
  document.getElementById("preview-count").textContent = t("inspect.preview_count", { first, last, total });
  document.getElementById("page-label").textContent = t("inspect.page", { page: Math.floor(preview.offset / preview.page_size) + 1 });
  document.getElementById("page-prev").disabled = preview.offset <= 0;
  document.getElementById("page-next").disabled = !preview.has_next;
}

function dismissedUpdate() {
  try { return localStorage.getItem(UPDATE_DISMISS_KEY); } catch (_) { return null; }
}

// Decide whether to show the update notice; kept pure so it can be tested without a DOM.
function updateNotice(update, dismissed) {
  if (!update || update.status !== "available") return null;
  const latest = update.latest_version;
  const url = update.release_url;
  if (typeof latest !== "string" || !latest || latest === dismissed) return null;
  if (typeof url !== "string" || !url.startsWith(RELEASE_URL_PREFIX)) return null;
  return { latest, current: String(update.current_version || ""), url };
}

function renderUpdate() {
  const host = document.getElementById("update-notice");
  const notice = updateNotice(state.update, dismissedUpdate());
  host.hidden = !notice;
  host.replaceChildren();
  if (!notice) return;
  host.append(
    el("div", { class: "update-notice-body" },
      el("strong", {}, t("update.title")),
      el("p", {}, t("update.available", { latest: notice.latest, current: notice.current })),
      el("p", { class: "muted" }, t("update.how_to")),
      el("a", { href: notice.url, target: "_blank", rel: "noopener noreferrer" }, t("update.release_notes")),
    ),
    el("button", {
      type: "button",
      class: "update-dismiss",
      "aria-label": t("update.dismiss"),
      title: t("update.dismiss"),
      "data-update-dismiss": notice.latest,
    }, "×"),
  );
}

async function checkForUpdate() {
  // A failed check is not an error for the user; the notice simply stays hidden.
  try { state.update = await api("/api/updates"); } catch (_) { state.update = null; }
  renderUpdate();
}

function render() {
  renderUploadFileStatus();
  renderStatus();
  renderBrowser();
  renderFeedback();
  const selected = document.getElementById("selected-file");
  selected.hidden = !state.file;
  if (state.file) {
    document.getElementById("selected-name").textContent = state.file.display_name;
    document.getElementById("selected-meta").textContent = t("files.selected_meta", {
      source: t(state.file.source === "upload" ? "files.source_upload" : "files.source_local"),
      size: formatBytes(state.file.size),
    });
    document.getElementById("delete-upload").hidden = state.file.source !== "upload";
  }
  renderInspection();
  renderDdl();
  renderLoad();
  renderInsertControls();
  renderActivity();
  renderUpdate();
}

async function loadBrowser(path = null) {
  if (state.browserBusy) return;
  state.browserBusy = true;
  renderBrowser();
  await withActivity(async () => {
    try {
      const url = path ? `/api/files/browse?path=${encodeURIComponent(path)}` : "/api/files/browse";
      state.browser = await api(url);
      state.error = null;
    } catch (error) { showError(error); }
    finally {
      state.browserBusy = false;
      renderBrowser();
    }
  });
}

async function loadSelection(file) {
  if (state.selectionBusy) return;
  state.selectionBusy = true;
  renderBrowser();
  await withActivity(async () => {
    const previousFileId = state.file?.file_id;
    state.file = file;
    state.lengthScan = file.length_scan || { status: "pending", progress_percent: 0, error: null };
    state.inspection = null;
    state.preview = null;
    state.ddl = null;
    state.offset = 0;
    state.error = null;
    state.tableNameEdited = false;
    state.selectedColumns = null;
    resetInsertPlan();
    updateDdlControls();
    render();
    if (previousFileId && previousFileId !== file.file_id) {
      api(`/api/files/${encodeURIComponent(previousFileId)}/release`, { method: "POST" }).catch(() => {});
    }
    void pollLengthScan(file.file_id);
    try {
      const inspection = await api(`/api/files/${encodeURIComponent(file.file_id)}/inspect`);
      if (state.file?.file_id !== file.file_id) return;
      state.inspection = inspection;
      populateDdlControls();
      renderInsertPlan();
      if (state.workTab === "insert") suggestInsertTable();
      await loadPreview();
    } catch (error) {
      if (state.file?.file_id === file.file_id) showError(error);
    } finally {
      state.selectionBusy = false;
      render();
    }
  });
}

async function requestDdl(fileId, options = ddlOptions()) {
  let result = await api(`/api/files/${encodeURIComponent(fileId)}/ddl`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options),
  });
  if (state.file?.file_id !== fileId) return result;
  if (options.length_scan === "full" && result.length_measurement === "estimated" && state.lengthScan?.status === "ready") {
    result = await api(`/api/files/${encodeURIComponent(fileId)}/ddl`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    });
  }
  return result;
}

async function updateDdlForFile(fileId) {
  const ddl = await requestDdl(fileId);
  if (state.file?.file_id !== fileId) return false;
  state.ddl = ddl;
  return true;
}

function pollDelay(failures, baseDelay = 500, maxDelay = 10000) {
  return Math.min(baseDelay * (2 ** Math.min(failures, 5)), maxDelay);
}

async function pollLengthScan(fileId) {
  let failures = 0;
  let lastPollError = null;
  while (state.file?.file_id === fileId) {
    let delay = 500;
    try {
      const scan = await api(`/api/files/${encodeURIComponent(fileId)}/length-scan`);
      if (state.file?.file_id !== fileId) return;
      if (lastPollError && state.error === lastPollError) {
        state.error = null;
        renderFeedback();
      }
      lastPollError = null;
      failures = 0;
      state.lengthScan = scan;
      render();
      if (scan.status === "ready") {
        if (state.ddl?.length_measurement === "estimated" && ddlOptions().length_scan === "full") {
          if (!await updateDdlForFile(fileId)) return;
          state.error = null;
          renderDdl();
          renderFeedback();
        }
        return;
      }
      if (scan.status === "failed") return;
    } catch (error) {
      if (state.file?.file_id !== fileId) return;
      showError(error);
      lastPollError = state.error;
      failures += 1;
      delay = pollDelay(failures);
    }
    await new Promise((resolve) => window.setTimeout(resolve, delay));
  }
}

async function loadPreview(rollbackOffset = state.offset) {
  const fileId = state.file?.file_id;
  if (!fileId) return;
  state.limit = Number(document.getElementById("page-size").value || 50);
  const requestOffset = state.offset;
  const requestLimit = state.limit;
  try {
    const preview = await api(`/api/files/${encodeURIComponent(fileId)}/preview?offset=${requestOffset}&limit=${requestLimit}`);
    if (state.file?.file_id !== fileId || state.offset !== requestOffset || state.limit !== requestLimit) return;
    state.preview = preview;
  } catch (error) {
    if (state.file?.file_id === fileId && state.offset === requestOffset && state.limit === requestLimit) {
      state.offset = rollbackOffset;
    }
    throw error;
  }
}

async function pollLoadJob(jobId = state.job?.job_id, failures = 0, lastPollError = null) {
  if (!jobId || state.job?.job_id !== jobId || !["queued", "running"].includes(state.job.status)) return;
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (state.job?.job_id !== jobId) return;
    if (lastPollError && state.error === lastPollError) {
      state.error = null;
      renderFeedback();
    }
    state.job = job;
    renderLoad();
    renderInsertControls();
    if (["queued", "running"].includes(state.job.status)) {
      window.setTimeout(() => pollLoadJob(jobId), 700);
    }
  } catch (error) {
    if (state.job?.job_id !== jobId || !["queued", "running"].includes(state.job.status)) return;
    showError(error);
    const pollError = state.error;
    const nextFailures = failures + 1;
    window.setTimeout(() => pollLoadJob(jobId, nextFailures, pollError), pollDelay(nextFailures, 700));
  }
}

function setTab(tab) {
  const local = tab === "local";
  document.getElementById("upload-panel").hidden = local;
  document.getElementById("local-panel").hidden = !local;
  document.getElementById("tab-upload").classList.toggle("active", !local);
  document.getElementById("tab-local").classList.toggle("active", local);
  document.getElementById("tab-upload").setAttribute("aria-pressed", String(!local));
  document.getElementById("tab-local").setAttribute("aria-pressed", String(local));
  if (local) loadBrowser();
}

async function init() {
  const theme = savedTheme();
  if (theme) document.documentElement.dataset.theme = theme;
  document.getElementById("lang").addEventListener("change", (event) => setLang(event.target.value));
  document.getElementById("theme-toggle").addEventListener("click", () => {
    setTheme(currentTheme() === "dark" ? "light" : "dark");
  });
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    if (!document.documentElement.dataset.theme) renderTheme();
  });
  document.getElementById("tab-upload").addEventListener("click", () => setTab("upload"));
  document.getElementById("tab-local").addEventListener("click", () => setTab("local"));
  document.getElementById("db-list").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-test-db]");
    if (!button || button.disabled) return;
    const kind = button.dataset.testDb;
    state.testingDb[kind] = true;
    renderStatus();
    await withActivity(async () => {
      try {
        state.connectionTests[kind] = await api(`/api/db/${kind}/test`, { method: "POST" });
        state.error = null;
      } catch (error) { showError(error); }
      finally {
        state.testingDb[kind] = false;
        renderStatus();
      }
    });
  });
  document.getElementById("browser-up").addEventListener("click", () => {
    const button = document.getElementById("browser-up");
    if (state.browser && state.browser.parent) {
      withBusy(button, () => loadBrowser(state.browser.parent), renderBrowser);
    }
  });
  document.getElementById("browser-roots").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-path]");
    if (button) withBusy(button, () => loadBrowser(button.dataset.path), renderBrowser);
  });
  document.getElementById("browser-entries").addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-path]");
    if (!button || button.disabled) return;
    if (button.dataset.kind === "directory") {
      withBusy(button, () => loadBrowser(button.dataset.path), renderBrowser);
      return;
    }
    await withBusy(button, async () => {
      try {
        const result = await api("/api/files/local", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: button.dataset.path }),
        });
        await loadSelection(result.file);
      } catch (error) { showError(error); }
    }, renderBrowser);
  });
  document.getElementById("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button[type=submit]");
    const input = document.getElementById("upload-file");
    if (!input.files.length) return;
    const form = new FormData();
    form.append("file", input.files[0]);
    await withBusy(button, async () => {
      try {
        const result = await api("/api/files/upload", { method: "POST", body: form });
        input.value = "";
        renderUploadFileStatus();
        await loadSelection(result.file);
      } catch (error) { showError(error); }
    }, renderUploadFileStatus);
  });
  const uploadInput = document.getElementById("upload-file");
  const dropzone = document.getElementById("upload-dropzone");
  uploadInput.addEventListener("change", () => {
    state.error = null;
    renderUploadFileStatus();
    renderFeedback();
  });
  dropzone.addEventListener("dragenter", (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragover");
  });
  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    dropzone.classList.add("is-dragover");
  });
  dropzone.addEventListener("dragleave", (event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) dropzone.classList.remove("is-dragover");
  });
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragover");
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    uploadInput.files = transfer.files;
    uploadInput.dispatchEvent(new Event("change", { bubbles: true }));
  });
  document.getElementById("delete-upload").addEventListener("click", async (event) => {
    if (!state.file || state.file.source !== "upload") return;
    const fileId = state.file.file_id;
    await withBusy(event.currentTarget, async () => {
      try {
        await api(`/api/files/${encodeURIComponent(fileId)}`, { method: "DELETE" });
        if (state.file?.file_id !== fileId) return;
        state.file = null;
        state.lengthScan = null;
        state.inspection = null;
        state.preview = null;
        state.ddl = null;
        render();
      } catch (error) { showError(error); }
    }, render);
  });
  document.getElementById("page-prev").addEventListener("click", async (event) => {
    await withBusy(event.currentTarget, async () => {
      const previousOffset = state.offset;
      state.offset = Math.max(0, previousOffset - state.limit);
      try { await loadPreview(previousOffset); renderPreview(); } catch (error) { showError(error); }
    }, renderPreview);
  });
  document.getElementById("page-next").addEventListener("click", async (event) => {
    await withBusy(event.currentTarget, async () => {
      const previousOffset = state.offset;
      state.offset += state.limit;
      try { await loadPreview(previousOffset); renderPreview(); } catch (error) { showError(error); }
    }, renderPreview);
  });
  document.getElementById("page-size").addEventListener("change", async (event) => {
    await withBusy(event.currentTarget, async () => {
      const previousOffset = state.offset;
      state.offset = 0;
      state.limit = Number(event.target.value);
      try { await loadPreview(previousOffset); renderPreview(); } catch (error) { showError(error); }
    }, renderPreview);
  });
  const ddlOptionsElements = [
    "ddl-dialect", "ddl-mode", "ddl-table-name", "ddl-column-policy", "ddl-string-strategy", "ddl-length-scan",
    "ddl-parent-key", "ddl-explode-column", "ddl-preserve-empty", "ddl-json-check",
  ].map((id) => document.getElementById(id));
  for (const control of ddlOptionsElements) {
    control.addEventListener("change", updateDdlOptions);
    control.addEventListener("input", updateDdlOptions);
  }
  document.getElementById("ddl-generate").addEventListener("click", async (event) => {
    if (!state.file) return;
    const fileId = state.file.file_id;
    await withBusy(event.currentTarget, async () => {
      try {
        if (!await updateDdlForFile(fileId)) return;
        state.error = null;
        renderDdl();
        renderFeedback();
      } catch (error) {
        if (state.file?.file_id === fileId) showError(error);
      }
    });
  });
  document.getElementById("ddl-copy").addEventListener("click", async (event) => {
    if (!state.ddl) return;
    await withBusy(event.currentTarget, async () => {
      try {
        await navigator.clipboard.writeText(state.ddl.ddl);
      } catch (_) {
        const area = el("textarea", { class: "copy-buffer" });
        area.value = state.ddl.ddl;
        document.body.append(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
    });
  });
  document.getElementById("ddl-download").addEventListener("click", async (event) => {
    if (!state.file || !state.ddl) return;
    await withBusy(event.currentTarget, async () => {
      try {
        const response = await fetch(`/api/files/${encodeURIComponent(state.file.file_id)}/ddl/download`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(ddlOptions()),
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw body.error || { code: "E_INTERNAL", params: {} };
        }
        const url = URL.createObjectURL(await response.blob());
        const link = el("a", { href: url, download: `${state.ddl.table_name}.sql` });
        document.body.append(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      } catch (error) { showError(error); }
    });
  });
  document.getElementById("load-start").addEventListener("click", async (event) => {
    if (!state.file) return;
    const fileId = state.file.file_id;
    const policy = document.getElementById("load-policy").value;
    await withBusy(event.currentTarget, async () => {
      try {
        if (!await updateDdlForFile(fileId)) return;
        renderDdl();
        let confirmName = null;
        if (policy === "truncate" || policy === "drop_create") {
          const parent = state.ddl.tables[0].name;
          const children = state.ddl.tables.map((table) => table.name).join(", ");
          confirmName = window.prompt(t("load.confirm_prompt", { parent, tables: children }), "");
          if (confirmName === null) return;
          if (confirmName !== parent) {
            showError({ code: "E_CONFIRM_REQUIRED", params: { table: parent } });
            return;
          }
        }
        if (state.file?.file_id !== fileId) return;
        const job = await api(`/api/files/${encodeURIComponent(fileId)}/load`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...ddlOptions(),
            existing_policy: policy,
            confirm_table_name: confirmName,
          }),
        });
        if (state.file?.file_id !== fileId) return;
        state.job = job;
        state.jobOrigin = "create";
        state.error = null;
        renderLoad();
        renderInsertControls();
        pollLoadJob();
      } catch (error) {
        if (state.file?.file_id === fileId) showError(error);
      }
    }, renderLoad);
  });
  for (const id of ["create-job", "insert-job"]) {
    document.getElementById(id).addEventListener("click", async (event) => {
      const button = event.target.closest("[data-job-cancel]");
      if (!button || !state.job?.job_id) return;
      const jobId = state.job.job_id;
      await withBusy(button, async () => {
        try {
          const job = await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
          if (state.job?.job_id !== jobId) return;
          state.job = job;
          renderLoad();
          pollLoadJob();
        } catch (error) {
          if (state.job?.job_id === jobId) showError(error);
        }
      });
    });
  }
  const workTabs = document.querySelector(".work-tabs");
  workTabs.addEventListener("click", (event) => {
    const button = event.target.closest("[data-work-tab]");
    if (button) setWorkTab(button.dataset.workTab);
  });
  workTabs.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const next = WORK_TABS[(WORK_TABS.indexOf(state.workTab) + step + WORK_TABS.length) % WORK_TABS.length];
    setWorkTab(next);
    document.getElementById(`work-tab-${next}`).focus();
  });
  document.getElementById("insert-dialect").addEventListener("change", (event) => {
    state.insert.dialect = event.target.value;
    closeTableSuggestions();
    resetInsertPlan();
    renderInsertPlan();
    suggestInsertTable();
    void ensureInsertTables();
  });
  const insertTable = document.getElementById("insert-table");
  insertTable.addEventListener("input", () => {
    if (state.insert.report || state.insert.error) {
      resetInsertPlan();
      renderInsertPlan();
    } else {
      renderInsertControls();
    }
    openTableSuggestions();
  });
  insertTable.addEventListener("focus", () => {
    if (!state.insert.report) openTableSuggestions();
  });
  insertTable.addEventListener("blur", closeTableSuggestions);
  insertTable.addEventListener("keydown", (event) => {
    const suggest = state.insert.suggest;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!suggest.open) {
        openTableSuggestions();
        if (!suggest.open) return;
      }
      const count = suggest.items.length;
      const step = event.key === "ArrowDown" ? 1 : -1;
      suggest.active = suggest.active < 0
        ? (step > 0 ? 0 : count - 1)
        : (suggest.active + step + count) % count;
      renderTableSuggestions();
    } else if (event.key === "Escape" && suggest.open) {
      event.preventDefault();
      closeTableSuggestions();
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (suggest.open && suggest.active >= 0) {
        chooseTableSuggestion(suggest.items[suggest.active]);
      } else {
        closeTableSuggestions();
        document.getElementById("insert-check").click();
      }
    }
  });
  const tableList = document.getElementById("insert-table-list");
  // Keep focus in the input so blur does not close the list before the click lands.
  tableList.addEventListener("mousedown", (event) => event.preventDefault());
  tableList.addEventListener("click", (event) => {
    const option = event.target.closest("[data-table]");
    if (option) chooseTableSuggestion(option.dataset.table);
  });
  document.getElementById("insert-check").addEventListener("click", async (event) => {
    const tableName = insertTable.value.trim();
    if (!state.file || !tableName) return;
    const fileId = state.file.file_id;
    await withBusy(event.currentTarget, async () => {
      resetInsertPlan();
      renderInsertPlan();
      try {
        const report = await api(`/api/files/${encodeURIComponent(fileId)}/insert/check`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dialect: insertDialect(), table_name: tableName }),
        });
        if (state.file?.file_id !== fileId) return;
        state.insert.report = report;
        insertTable.value = report.table;
      } catch (error) {
        if (state.file?.file_id !== fileId) return;
        state.insert.error = error;
      }
      renderInsertPlan();
    }, renderInsertControls);
  });
  document.getElementById("insert-columns-body").addEventListener("change", (event) => {
    const source = event.target.dataset?.insertSource;
    if (!source) return;
    setInsertSelected(source, event.target.checked);
    document.querySelector(`[data-insert-source="${CSS.escape(source)}"]`)?.focus();
  });
  document.getElementById("insert-columns-all").addEventListener("change", (event) => {
    state.insert.selected = event.target.checked ? null : new Set();
    renderInsertPlan();
  });
  document.getElementById("insert-start").addEventListener("click", async (event) => {
    const report = state.insert.report;
    if (!state.file || !report) return;
    const fileId = state.file.file_id;
    await withBusy(event.currentTarget, async () => {
      try {
        state.insert.error = null;
        const job = await api(`/api/files/${encodeURIComponent(fileId)}/insert`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dialect: report.dialect, table_name: report.table, columns: insertColumnNames() }),
        });
        if (state.file?.file_id !== fileId) return;
        state.job = job;
        state.jobOrigin = "insert";
        renderLoad();
        pollLoadJob();
      } catch (error) {
        if (state.file?.file_id !== fileId) return;
        state.insert.error = error;
      }
      renderInsertControls();
    }, renderInsertControls);
  });
  document.getElementById("ddl-columns-all").addEventListener("click", () => setAllColumnsSelected(true));
  document.getElementById("ddl-columns-none").addEventListener("click", () => setAllColumnsSelected(false));
  document.getElementById("ddl-mode").addEventListener("change", updateDdlControls);
  document.getElementById("ddl-dialect").addEventListener("change", updateDdlControls);
  document.getElementById("update-notice").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-update-dismiss]");
    if (!button) return;
    try { localStorage.setItem(UPDATE_DISMISS_KEY, button.dataset.updateDismiss); } catch (_) { /* Hide it for this page only. */ }
    state.update = null;
    renderUpdate();
  });
  setWorkTab(savedWorkTab(), false);
  try { state.status = await api("/api/status"); } catch (error) { showError(error); }
  populateLanguageOptions(state.status?.languages || []);
  const lang = savedLang();
  document.getElementById("lang").value = lang;
  await setLang(lang, false);
  if (state.workTab === "insert") void ensureInsertTables();
  void checkForUpdate();
}

init();
