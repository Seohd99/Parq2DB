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
render = renderBrowser = renderActivity = renderDdl = () => {};
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
