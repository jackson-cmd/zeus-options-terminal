"""The page's option math (analyzeStrategy, written in JavaScript inside
optionstrat_analyzer.py) run under node: max profit and max loss on textbook
strategies. Skipped when node is not installed."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

UNLIMITED = 1e9


def _extract(src: str, roots: list) -> str:
    """Source of `roots` and every top-level function they call."""
    defs = {m.group(1): m.start() for m in re.finditer(r"^function ([A-Za-z_$][\w$]*)\s*\(", src, re.M)}

    def body(name):
        i = defs[name]
        k, depth, quote = src.index("{", i), 0, None
        while True:
            c = src[k]
            if quote:
                if c == "\\":
                    k += 2
                    continue
                if c == quote:
                    quote = None
            elif c in "'\"`":
                quote = c
            elif src.startswith("//", k):
                k = src.index("\n", k)
                continue
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return src[i:k + 1]
            k += 1

    seen, parts, todo = set(), [], list(roots)
    while todo:
        name = todo.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        text = body(name)
        parts.append(text)
        todo += [m.group(1) for m in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*\(", text) if m.group(1) in defs]
    return "\n\n".join(reversed(parts))


CASES = """
const exp = (() => { const d = new Date(); d.setDate(d.getDate() + 30); return d.toISOString().slice(0, 10); })();
const leg = (type, strike, qty, entry) => ({type, strike, qty, entry, current: entry, mid: entry, mult: 100, exp, iv: 0.3});
const strat = legs => ({current_price: 100, risk_free_rate: 0.04, iv: 0.3, expiration: exp, legs});
const cases = {
  bull_call: strat([leg('C', 100, 1, 6), leg('C', 110, -1, 2)]),
  bear_put: strat([leg('P', 100, 1, 6), leg('P', 90, -1, 2)]),
  iron_condor: strat([leg('P', 85, 1, 0.5), leg('P', 90, -1, 1.2), leg('C', 110, -1, 1.1), leg('C', 115, 1, 0.3)]),
  short_put: strat([leg('P', 100, -1, 3)]),
  long_put: strat([leg('P', 100, 1, 3)]),
  long_call: strat([leg('C', 100, 1, 3)]),
  short_call: strat([leg('C', 100, -1, 3)]),
  call_backspread: strat([leg('C', 100, -1, 6), leg('C', 110, 2, 2)]),
};
const out = {};
for (const k in cases) { const a = analyzeStrategy(cases[k]); out[k] = [a.maxProfit, a.maxLoss]; }
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    src = (ROOT / "options_analyzer" / "optionstrat_analyzer.py").read_text(encoding="utf-8")
    script = tmp_path_factory.mktemp("js") / "analyzer_cases.js"
    script.write_text(_extract(src, ["analyzeStrategy"]) + "\n" + CASES, encoding="utf-8")
    run = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize("name, max_profit, max_loss", [
    ("bull_call", 600, -400),           # defined risk: unchanged
    ("bear_put", 600, -400),
    ("iron_condor", 150, -350),
    ("short_put", 300, -9700),          # the stock can go to zero
    ("long_put", 9700, -300),
    ("long_call", UNLIMITED, -300),     # shown as ∞
    ("short_call", 300, -UNLIMITED),
    ("call_backspread", UNLIMITED, -800),   # worst case exactly at the 110 strike
])
def test_max_profit_and_loss(results, name, max_profit, max_loss):
    got_profit, got_loss = results[name]
    assert got_profit == pytest.approx(max_profit, abs=0.5)
    assert got_loss == pytest.approx(max_loss, abs=0.5)
