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
