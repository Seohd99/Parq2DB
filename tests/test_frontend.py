"""Exercise browser state transitions with Node when it is available."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.core.flatten import default_table_name


def test_default_table_name_follows_dialect_and_preserves_user_edits():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript state test")
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    # Run the actual handlers without starting the browser's DOM/network setup.
    source, startup = source.rsplit("\ninit();", 1)
    assert not startup.strip()
    files = [
        {
            "file_id": str(index),
            "display_name": filename,
            "default_table_names": {
                dialect: default_table_name(filename, dialect)
                for dialect in ("oracle", "mysql", "postgres")
            },
        }
        for index, filename in enumerate(("123.parquet", "한글.parquet", "a" * 130 + ".parquet"))
    ]
    harness = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {id, value: '', disabled: false});
    return elements.get(id);
  },
};
"""
    checks = r"""
render = renderBrowser = renderActivity = renderDdl = renderInsertPlan = renderInsertControls = () => {};
populateDdlControls = updateDdlControls;
pollLengthScan = loadPreview = async () => {};
api = async () => ({});
async function checkNames(files) {
  const dialect = document.getElementById('ddl-dialect');
  const name = document.getElementById('ddl-table-name');
  dialect.value = 'oracle';
  for (const file of files) {
    await loadSelection(file);
    assert.equal(name.value, file.default_table_names[dialect.value]);
    for (const target of ['mysql', 'postgres', 'oracle']) {
      dialect.value = target;
      updateDdlOptions({target: dialect});
      assert.equal(name.value, file.default_table_names[target]);
      assert.equal(ddlOptions().table_name, name.value);
    }
    // Treat even an edit equal to the current default as an intentional name.
    for (const custom of ['MY_TABLE', name.value, '']) {
      name.value = custom;
      updateDdlOptions({target: name});
      dialect.value = dialect.value === 'oracle' ? 'postgres' : 'oracle';
      updateDdlOptions({target: dialect});
      populateDdlControls();
      assert.equal(name.value, custom);
      assert.equal(ddlOptions().table_name, custom);
    }
  }
}
checkNames(FILES).catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + source + "\nconst FILES = " + json.dumps(files) + ";\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_column_selection_sends_null_for_all_and_file_order_for_a_subset():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript state test")
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    source, startup = source.rsplit("\ninit();", 1)
    assert not startup.strip()
    harness = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {id, value: '', checked: false, disabled: false});
    return elements.get(id);
  },
};
"""
    checks = r"""
renderDdl = () => {};
populateDdlControls = () => {};
state.inspection = {schema: {fields: [
  {name: 'id', kind: 'scalar'}, {name: 'name', kind: 'scalar'}, {name: 'tags', kind: 'list'},
]}};
state.selectedColumns = null;
assert.equal(ddlOptions().columns, null);
setColumnSelected('id', false);
assert.deepEqual(ddlOptions().columns, ['name', 'tags']);
setColumnSelected('id', true);
assert.equal(ddlOptions().columns, null);
setAllColumnsSelected(false);
assert.deepEqual(ddlOptions().columns, []);
setColumnSelected('tags', true);
setColumnSelected('id', true);
assert.deepEqual(ddlOptions().columns, ['id', 'tags']);
state.ddl = {stale: true};
setColumnSelected('name', true);
assert.equal(state.ddl, null);
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + source + "\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_insert_column_selection_sends_null_for_all_matched_columns():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript state test")
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    source, startup = source.rsplit("\ninit();", 1)
    assert not startup.strip()
    harness = r"""
const assert = require('node:assert/strict');
const document = { getElementById: () => ({}) };
"""
    checks = r"""
renderInsertPlan = () => {};
const column = (name, source, extra = {}) => ({
  name, source, data_type: 'NUMBER', nullable: true, has_default: false, generated: false,
  required: false, source_kind: source === null ? null : 'scalar', ...extra,
});
state.insert.report = {table: 'T', source_rows: 3, file_only: [], columns: [
  column('ID', 'id'), column('NAME', 'name'), column('NOTE', null), column('TOTAL', null, {generated: true}),
]};
assert.equal(insertColumnNames(), null);
assert.equal(insertChosenCount(), 2);
setInsertSelected('id', false);
assert.deepEqual(insertColumnNames(), ['name']);
assert.equal(insertChosenCount(), 1);
setInsertSelected('id', true);
assert.equal(insertColumnNames(), null);
setInsertSelected('name', false);
setInsertSelected('id', false);
assert.deepEqual(insertColumnNames(), []);
assert.equal(insertChosenCount(), 0);
resetInsertPlan();
assert.equal(state.insert.report, null);
assert.equal(state.insert.selected, null);
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + source + "\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_table_suggestions_filter_locally_with_prefix_matches_first():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript state test")
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    source, startup = source.rsplit("\ninit();", 1)
    assert not startup.strip()
    checks = r"""
const assert = require('node:assert/strict');
const names = ['SALES_CLUSTER_MAP', 'CLUSTER_INFO', 'ORDERS', 'cluster_log'];
assert.deepEqual(filterTableNames(names, ''), names);
assert.deepEqual(filterTableNames(names, 'clus'), ['CLUSTER_INFO', 'cluster_log', 'SALES_CLUSTER_MAP']);
assert.deepEqual(filterTableNames(names, '  Orders '), ['ORDERS']);
assert.deepEqual(filterTableNames(names, 'zzz'), []);
"""
    result = subprocess.run(
        [node, "-"],
        input="const document = { getElementById: () => ({}) };\n" + source + "\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_api_normalizes_network_and_non_json_errors():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript state test")
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    source, startup = source.rsplit("\ninit();", 1)
    assert not startup.strip()
    harness = r"""
const assert = require('node:assert/strict');
const feedback = {textContent: '', classList: {toggle() {}}};
const document = {getElementById: () => feedback};
"""
    checks = r"""
const expected = {code: 'E_HTTP_ERROR', params: {}};
async function assertHttpError() {
  await assert.rejects(api('/api/test'), error => {
    assert.deepEqual(error, expected);
    return true;
  });
}
(async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assertHttpError();
  globalThis.fetch = async () => ({ok: false, json: async () => { throw new SyntaxError('not JSON'); }});
  await assertHttpError();
  globalThis.fetch = async () => ({ok: true, json: async () => { throw new SyntaxError('empty response'); }});
  assert.deepEqual(await api('/api/empty'), {});
  assert.equal(tCode(undefined), 'error.E_HTTP_ERROR');
  assert.equal(tCode({code: 17}), 'error.E_HTTP_ERROR');
  showError(new TypeError('Failed to fetch'));
  assert.deepEqual(state.error, expected);
  state.insert.error = new TypeError('Failed to fetch');
  assert.doesNotThrow(() => tCode(state.insert.error));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + source + "\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_async_file_results_do_not_overwrite_current_file_and_preview_rolls_back():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript state test")
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    source, startup = source.rsplit("\ninit();", 1)
    assert not startup.strip()
    harness = r"""
const assert = require('node:assert/strict');
const document = {getElementById: () => ({value: '50'})};
globalThis.window = {setTimeout: resolve => { resolve(); }};
render = renderFeedback = renderDdl = () => {};
"""
    checks = r"""
(async () => {
  let finishScan;
  state.file = {file_id: 'scan-old'};
  state.lengthScan = {status: 'pending', marker: 'keep'};
  api = () => new Promise(resolve => { finishScan = resolve; });
  const scanRequest = pollLengthScan('scan-old');
  state.file = {file_id: 'scan-current'};
  finishScan({status: 'ready', marker: 'stale'});
  await scanRequest;
  assert.deepEqual(state.lengthScan, {status: 'pending', marker: 'keep'});

  let finishPreview;
  state.file = {file_id: 'preview-old'};
  state.offset = 50;
  state.limit = 50;
  state.preview = {marker: 'keep'};
  api = () => new Promise(resolve => { finishPreview = resolve; });
  const previewRequest = loadPreview(0);
  state.file = {file_id: 'preview-current'};
  state.offset = 0;
  state.preview = {marker: 'current'};
  finishPreview({marker: 'stale'});
  await previewRequest;
  assert.deepEqual(state.preview, {marker: 'current'});

  let finishDdl;
  state.file = {file_id: 'ddl-old'};
  state.ddl = {marker: 'keep'};
  requestDdl = () => new Promise(resolve => { finishDdl = resolve; });
  const ddlRequest = updateDdlForFile('ddl-old');
  state.file = {file_id: 'ddl-current'};
  finishDdl({marker: 'stale'});
  assert.equal(await ddlRequest, false);
  assert.deepEqual(state.ddl, {marker: 'keep'});

  state.file = {file_id: 'preview-failure'};
  state.offset = 50;
  api = async () => { throw {code: 'E_HTTP_ERROR', params: {}}; };
  await assert.rejects(loadPreview(0));
  assert.equal(state.offset, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + source + "\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_poll_retries_with_bounded_backoff_and_stops_for_replaced_job():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the JavaScript state test")
    source = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    source, startup = source.rsplit("\ninit();", 1)
    assert not startup.strip()
    harness = r"""
const assert = require('node:assert/strict');
globalThis.window = {setTimeout: () => 0};
renderFeedback = renderLoad = renderInsertControls = () => {};
"""
    checks = r"""
(async () => {
  const scheduled = [];
  globalThis.window.setTimeout = (callback, delay) => { scheduled.push({callback, delay}); };
  state.job = {job_id: 'job-old', status: 'running'};
  state.error = null;
  let calls = 0;
  api = async () => { calls += 1; throw {code: 'E_HTTP_ERROR', params: {}}; };
  await pollLoadJob();
  assert.equal(calls, 1);
  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].delay, 1400);
  assert.equal(pollDelay(100, 700), 10000);
  state.job = {job_id: 'job-current', status: 'running'};
  await scheduled[0].callback();
  assert.equal(calls, 1);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-"],
        input=harness + source + "\n" + checks,
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
