import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node needed for dashboard behavior test")
def test_l4_dashboard_force_unload_works_while_load_is_pending():
    script = r"""
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const elements = new Map();
const element = id => {if (!elements.has(id)) elements.set(id, {value:'', checked:false, disabled:false, textContent:'', append(){}, replaceChildren(){}, addEventListener(){}, close(){}, focus(){}, setAttribute(){}, removeAttribute(){}}); return elements.get(id);};
const ctx = vm.createContext({document:{getElementById:element}, location:{origin:'http://testserver'}, confirm:()=>true});
vm.runInContext(fs.readFileSync('gateway/static/app.js','utf8'), ctx);
(async()=>{
  vm.runInContext('authenticated=true; key="test"; askConfirmation=async()=>true; state={phase:"loading",selected_engine:"one"}; poll=async()=>{}; models=()=>{};', ctx);
  let resolveLoad; const called = [];
  ctx.apiOverride = async path => {called.push(path); if(path==='/api/model') return new Promise(r=>resolveLoad=r); return {};};
  vm.runInContext('api=apiOverride', ctx);
  const load = vm.runInContext('action("/api/model",{model:"fixture"})', ctx);
  assert.equal(element('select-engine').disabled,true);
  assert.equal(element('unload').disabled,true);
  element('force').checked=true;
  vm.runInContext('buttons()',ctx);
  assert.equal(element('unload').disabled,false,'Force-unload is disabled during loading');
  const stop = vm.runInContext('action("/api/unload",{})',ctx);
  await Promise.resolve();
  assert.ok(called.includes('/api/unload'),'Working gate blocked force cancellation');
  await stop; resolveLoad({}); await load;
  assert.equal(element('force').checked,false);
})().catch(error=>{console.error(error); process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
