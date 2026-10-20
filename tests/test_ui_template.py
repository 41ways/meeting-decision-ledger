"""화면(web_chat.html) 문법 검사 — Vue 템플릿 식이나 스크립트에 따옴표 하나만 빠져도 화면 전체가 하얗게 뜬다.
node가 있으면 템플릿 식과 스크립트를 실제 JS 파서로 검사한다 (없으면 건너뜀)."""
# pylint: disable=missing-function-docstring,missing-class-docstring,redefined-outer-name,unused-argument  # pytest fixture·가짜 객체 관용
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parent.parent / "web_chat.html"
CHECK = r"""
const fs = require('fs'); const h = fs.readFileSync(process.argv[1], 'utf8');
const script = h.split('<script>\n')[1].split('</script>')[0];
const tpl = h.split('<div id="app" v-cloak>')[1].split('<script')[0];
const exprs = [...tpl.matchAll(/(?:[:@][\w.-]+|v-[\w-]+(?::[\w-]+)?)="([^"]*)"/g)].map(m => m[1])
  .concat([...tpl.matchAll(/\{\{([\s\S]*?)\}\}/g)].map(m => m[1]));
const bad = exprs.filter(e => { try { new Function('with(this){return (' + e + ')}'); return false; }
  catch { try { new Function('with(this){' + e + '}'); return false; } catch { return true; } } });
let scriptErr = null; try { new Function(script); } catch (e) { scriptErr = e.message; }
console.log(JSON.stringify({ bad, scriptErr, count: exprs.length }));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_template_and_script_parse():
    out = subprocess.run(["node", "-e", CHECK, str(HTML)], capture_output=True, text=True, check=True).stdout
    r = json.loads(out)
    assert r["scriptErr"] is None, r["scriptErr"]
    assert r["bad"] == [], r["bad"]
    assert r["count"] > 50
