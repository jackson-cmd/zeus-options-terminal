"""开单前体检 —— 独立页面(不进主 dashboard tab,直接开 /pretrade-checkup 用)。

自包含单文件 HTML/CSS/JS,深色主题跟 PWA 其它页面对齐
(#0d1117 背景 / #161b22 卡片 / #30363d 边框 / #c9d1d9 文字 / #58a6ff 链接)。
本文件只管排版 + 两个 fetch(GET 体检 / POST override),不写判断逻辑 ——
数据和四层判断全在 pre_trade_checkup/ 引擎里,这里不重复一遍规则。

用 r\"\"\"...\"\"\" 原始字符串装 HTML,避免 Python 字符串转义吃掉 JS 里的 `\'`
(见 gotcha_js_in_python_string_escaping)。没有任何 Python 端字符串模板/格式化 ——
ticker/direction/dte 全部在浏览器端读表单值 + URL query 参数,服务端只原样吐整份常量。
"""
from __future__ import annotations

PRETRADE_CHECKUP_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>开单前体检</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 60px;
    background: #0d1117; color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 14px; line-height: 1.55;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  .topbar { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 20px; margin: 0; color: #c9d1d9; }
  .cli-ref {
    font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 11.5px;
    color: #58a6ff; text-decoration: none; cursor: pointer; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; max-width: 100%;
  }
  .cli-ref:hover { text-decoration: underline; }
  .subtitle { color: #8b949e; font-size: 12.5px; margin: 6px 0 20px; }

  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }

  .form-row { display: flex; gap: 14px; align-items: flex-end; flex-wrap: wrap; }
  .form-row label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #8b949e; }
  .form-row input, .form-row select {
    background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
    border-radius: 6px; padding: 7px 10px; font-size: 14px; font-family: inherit;
  }
  .form-row input:focus, .form-row select:focus { outline: none; border-color: #58a6ff; }
  #tkIn { width: 110px; text-transform: uppercase; font-family: ui-monospace, 'SF Mono', Menlo, monospace; }
  #dteIn { width: 80px; }

  .btn-primary, .btn-icon, .btn-danger {
    border-radius: 6px; font-size: 13.5px; cursor: pointer; font-family: inherit;
  }
  .btn-primary {
    background: #1f6feb; border: 1px solid #388bfd; color: #fff;
    padding: 8px 16px; font-weight: 600;
  }
  .btn-primary:hover { background: #388bfd; }
  .btn-primary:disabled, .btn-icon:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-icon {
    background: transparent; border: 1px solid #30363d; color: #c9d1d9;
    padding: 8px 12px;
  }
  .btn-icon:hover { background: #21262d; }
  .btn-danger {
    background: transparent; border: 1px solid #f85149; color: #f85149;
    padding: 8px 14px; font-weight: 600;
  }
  .btn-danger:hover { background: rgba(248, 81, 73, 0.12); }

  .spinner-wrap { display: none; align-items: center; gap: 10px; color: #8b949e; padding: 16px 0; }
  .spinner {
    width: 18px; height: 18px; border-radius: 50%;
    border: 2px solid #30363d; border-top-color: #58a6ff;
    animation: ptc-spin 0.8s linear infinite;
  }
  @keyframes ptc-spin { to { transform: rotate(360deg); } }

  .err-box {
    display: none; background: rgba(248, 81, 73, 0.1); border: 1px solid #f85149;
    color: #f85149; padding: 12px 14px; border-radius: 8px; margin-bottom: 16px;
    font-size: 13.5px; white-space: pre-wrap; word-break: break-word;
  }

  .verdict-banner {
    text-align: center; font-size: 20px; font-weight: 700;
    padding: 18px; border-radius: 10px; margin-bottom: 8px;
  }
  .verdict-banner.pass { background: rgba(63, 185, 80, 0.15); border: 1px solid #3fb950; color: #3fb950; }
  .verdict-banner.fail { background: rgba(248, 81, 73, 0.15); border: 1px solid #f85149; color: #f85149; }
  .asof-note { text-align: center; color: #8b949e; font-size: 12px; margin-bottom: 20px; }

  .layer-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    margin-bottom: 10px; overflow: hidden;
  }
  .layer-card summary {
    padding: 12px 16px; cursor: pointer; display: flex;
    justify-content: space-between; align-items: center; font-weight: 600;
    list-style: none;
  }
  .layer-card summary::-webkit-details-marker { display: none; }
  .layer-card summary::before {
    content: '▸'; display: inline-block; margin-right: 8px; color: #6e7681;
    transition: transform 0.15s ease;
  }
  .layer-card[open] summary::before { transform: rotate(90deg); }
  .layer-badge {
    font-size: 12px; font-weight: 700; padding: 2px 10px; border-radius: 20px;
    white-space: nowrap;
  }
  .layer-badge.pass { color: #3fb950; background: rgba(63, 185, 80, 0.12); }
  .layer-badge.fail { color: #f85149; background: rgba(248, 81, 73, 0.12); }
  .layer-body { padding: 2px 16px 14px; }
  .layer-body .line { padding: 3px 0; font-size: 13.5px; color: #c9d1d9; }
  .layer-body .line.warn { color: #d29922; }
  .layer-body .line.hot-title { color: #d29922; font-weight: 600; margin-top: 4px; }
  .layer-body .line.hot-item {
    font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-size: 12.5px; color: #e6a94c;
  }

  .suggestion-card { border-left: 4px solid #58a6ff; font-size: 13.5px; }
  .suggestion-card b { color: #58a6ff; }

  .override-card { border: 1px solid #30363d; border-left: 4px solid #d29922; display: none; }
  .override-title { color: #d29922; font-weight: 600; margin-bottom: 10px; }
  .override-card textarea {
    width: 100%; background: #0d1117; border: 1px solid #30363d; color: #c9d1d9;
    border-radius: 6px; padding: 10px; font-family: inherit; font-size: 13.5px; resize: vertical;
  }
  .override-card textarea:focus { outline: none; border-color: #d29922; }
  .override-row { display: flex; align-items: center; justify-content: space-between; margin-top: 8px; gap: 10px; }
  .reason-counter { font-size: 12px; color: #8b949e; }
  .reason-counter.ok { color: #3fb950; }
  .override-msg { margin-top: 10px; font-size: 13px; min-height: 18px; }
  .override-msg.ok { color: #3fb950; }
  .override-msg.err { color: #f85149; }
  .override-footnote {
    margin-top: 12px; font-size: 11px; color: #6e7681;
    font-family: ui-monospace, 'SF Mono', Menlo, monospace;
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>📛 开单前体检</h1>
    <a class="cli-ref" id="cliRef" onclick="copyCli()" title="点击复制到剪贴板">📖 CLI: python -m pre_trade_checkup.cli --ticker &lt;T&gt; --direction &lt;D&gt; --dte &lt;n&gt;</a>
  </div>
  <div class="subtitle">四层体检(pre_trade_checkup/ 引擎) · 任一层不过 = 结论「别开单」</div>

  <div class="card">
    <div class="form-row">
      <label>Ticker
        <input id="tkIn" type="text" value="SPY" autocomplete="off" spellcheck="false">
      </label>
      <label>Direction
        <select id="dirIn">
          <option value="bull">bull</option>
          <option value="bear">bear</option>
        </select>
      </label>
      <label>DTE
        <input id="dteIn" type="number" value="40" min="1" max="365">
      </label>
      <button id="runBtn" class="btn-primary" onclick="runCheckup()">🩺 跑体检</button>
      <button id="refreshBtn" class="btn-icon" onclick="runCheckup()" title="重新跑一次">🔄</button>
    </div>
  </div>

  <div id="spinner" class="spinner-wrap"><div class="spinner"></div><span>跑体检中…</span></div>
  <div id="errBox" class="err-box"></div>

  <div id="resultBox" style="display:none">
    <div id="verdictBanner" class="verdict-banner"></div>
    <div id="asOfNote" class="asof-note"></div>

    <div id="layersBox"></div>

    <div id="suggestionBox" class="card suggestion-card"></div>

    <div id="overrideBox" class="card override-card">
      <div class="override-title">⚠️ 如果你一定要开(慎用):</div>
      <textarea id="overrideReason" rows="3" placeholder="输入 ≥20 字理由… 认真想一下你在赌什么"></textarea>
      <div class="override-row">
        <span id="reasonCounter" class="reason-counter">0/20 字</span>
        <button class="btn-danger" onclick="submitOverride()">🔓 覆盖 verdict 并记录</button>
      </div>
      <div id="overrideMsg" class="override-msg"></div>
      <div class="override-footnote">强制开单记录会写入 pre_trade_checkup/data/override_log.jsonl</div>
    </div>
  </div>
</div>

<script>
(function () {
  'use strict';

  var lastResult = null;

  function $(id) { return document.getElementById(id); }

  function qs(name) {
    return new URLSearchParams(location.search).get(name);
  }

  function escapeHtml(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function updateCliRef() {
    var t = ($('tkIn').value || '<T>').toUpperCase();
    var d = $('dirIn').value || '<D>';
    var n = $('dteIn').value || '<n>';
    $('cliRef').textContent = '📖 CLI: python -m pre_trade_checkup.cli --ticker ' + t +
      ' --direction ' + d + ' --dte ' + n;
  }

  function copyCli() {
    var el = $('cliRef');
    var text = el.textContent.replace(/^.*CLI:\s*/, '');
    if (!navigator.clipboard) { return; }
    navigator.clipboard.writeText(text).then(function () {
      var orig = el.textContent;
      el.textContent = '✅ 已复制到剪贴板';
      setTimeout(function () { el.textContent = orig; }, 1200);
    });
  }
  window.copyCli = copyCli;

  function setLoading(on) {
    $('spinner').style.display = on ? 'flex' : 'none';
    $('runBtn').disabled = on;
    $('refreshBtn').disabled = on;
  }

  function showError(msg) {
    var box = $('errBox');
    box.textContent = '❌ ' + msg;
    box.style.display = 'block';
    $('resultBox').style.display = 'none';
  }

  function clearError() {
    $('errBox').style.display = 'none';
    $('errBox').textContent = '';
  }

  var LAYER_ORDER = ['1', '2', '3', '4'];
  var LAYER_FALLBACK_NAME = { '1': '长期趋势', '2': 'EMA 触及', '3': 'IV / 财报', '4': '宏观 / benzinga' };

  function layerLines(layer) {
    if (layer.lines && layer.lines.length) { return layer.lines.slice(); }
    var out = (layer.reasons_pass || []).slice();
    return out.concat(layer.reasons_fail || []);
  }

  function renderLayers(layers) {
    var html = '';
    LAYER_ORDER.forEach(function (n) {
      var layer = layers[n];
      if (!layer) { return; }
      var name = layer.name || LAYER_FALLBACK_NAME[n] || ('Layer ' + n);
      var ok = !!layer.pass;
      html += '<details class="layer-card" open>';
      html += '<summary><span>Layer ' + n + ' ' + escapeHtml(name) + '</span>' +
              '<span class="layer-badge ' + (ok ? 'pass' : 'fail') + '">' +
              (ok ? '✅ PASS' : '❌ FAIL') + '</span></summary>';
      html += '<div class="layer-body">';
      layerLines(layer).forEach(function (line) {
        html += '<div class="line">· ' + escapeHtml(line) + '</div>';
      });
      if (n === '4') {
        (layer.warnings || []).forEach(function (w) {
          html += '<div class="line warn">· ⚠️ ' + escapeHtml(w) + '</div>';
        });
        var hot = layer.hot_topics || [];
        if (hot.length) {
          html += '<div class="line hot-title">· 🔥 热点话题:</div>';
          hot.forEach(function (h) {
            var thr = h.meets_p95_highlift ? ('P95=' + h.p95) : ('P90=' + h.p90);
            var tail = h.meets_p95_highlift ? '  ← 高危,单独否决' : '';
            var lift = (typeof h.lift === 'number') ? h.lift.toFixed(2) : h.lift;
            html += '<div class="line hot-item">&nbsp;&nbsp;· ' + escapeHtml(h.label || h.topic) +
                    ': ' + h.count + ' 次 (' + thr + ', lift ' + lift + ')' +
                    escapeHtml(tail) + '</div>';
          });
        }
      }
      html += '</div></details>';
    });
    $('layersBox').innerHTML = html;
  }

  function renderResult(data) {
    lastResult = data;
    clearError();
    $('resultBox').style.display = 'block';

    var ok = !!data.pass;
    var banner = $('verdictBanner');
    banner.className = 'verdict-banner ' + (ok ? 'pass' : 'fail');
    banner.textContent = ok ? '✅ 可以开单' : '🛑 别开单';

    var asOf = data.as_of ? ('数据截至: ' + data.as_of) : '';
    if (data.today && data.as_of && data.today !== data.as_of) {
      asOf += (asOf ? '  ·  ' : '') + '请求日期: ' + data.today;
    }
    $('asOfNote').textContent = asOf;

    renderLayers(data.layers || {});

    var sugBox = $('suggestionBox');
    if (data.suggestion) {
      sugBox.style.display = 'block';
      sugBox.innerHTML = '<b>建议:</b> ' + escapeHtml(data.suggestion);
    } else {
      sugBox.style.display = 'none';
    }

    var ovBox = $('overrideBox');
    $('overrideMsg').textContent = '';
    $('overrideMsg').className = 'override-msg';
    ovBox.style.display = ok ? 'none' : 'block';
  }

  function runCheckup() {
    var ticker = ($('tkIn').value || '').trim().toUpperCase();
    var direction = $('dirIn').value;
    var dteVal = parseInt($('dteIn').value, 10);
    var dte = isNaN(dteVal) ? 40 : dteVal;
    if (!ticker) { showError('请输入 ticker'); return; }
    updateCliRef();
    clearError();
    $('resultBox').style.display = 'none';
    setLoading(true);
    var url = '/api/pre_trade_checkup?ticker=' + encodeURIComponent(ticker) +
              '&direction=' + encodeURIComponent(direction) +
              '&dte=' + encodeURIComponent(dte);
    fetch(url).then(function (res) {
      return res.json().then(function (data) { return { status: res.status, data: data }; });
    }).then(function (r) {
      if (r.data && r.data.error) {
        showError(r.data.error);
        return;
      }
      renderResult(r.data);
    }).catch(function (e) {
      showError(String(e && e.message || e));
    }).finally(function () {
      setLoading(false);
    });
  }
  window.runCheckup = runCheckup;

  function updateReasonCounter() {
    var len = $('overrideReason').value.trim().length;
    var el = $('reasonCounter');
    el.textContent = len + '/20 字';
    el.className = 'reason-counter' + (len >= 20 ? ' ok' : '');
  }

  function submitOverride() {
    if (!lastResult) { return; }
    var reason = $('overrideReason').value.trim();
    var msgEl = $('overrideMsg');
    if (reason.length < 20) {
      msgEl.textContent = '❌ 理由太短';
      msgEl.className = 'override-msg err';
      return;
    }
    var body = {
      ticker: lastResult.ticker,
      direction: lastResult.direction,
      dte: lastResult.dte,
      reason: reason
    };
    fetch('/api/pre_trade_checkup/override', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (res) {
      return res.json().then(function (data) { return { status: res.status, data: data }; });
    }).then(function (r) {
      if (r.data && r.data.error) {
        msgEl.textContent = (r.status === 400) ? '❌ 理由太短' : ('❌ ' + r.data.error);
        msgEl.className = 'override-msg err';
        return;
      }
      var entry = (r.data && r.data.entry) || {};
      var rtxt = entry.reason || reason;
      var truncated = rtxt.length > 60 ? (rtxt.slice(0, 60) + '…') : rtxt;
      msgEl.textContent = '✅ 已记录，理由：' + truncated;
      msgEl.className = 'override-msg ok';
    }).catch(function (e) {
      msgEl.textContent = '❌ ' + String(e && e.message || e);
      msgEl.className = 'override-msg err';
    });
  }
  window.submitOverride = submitOverride;

  function prefillFromQuery() {
    var qTicker = qs('ticker');
    var qDirection = qs('direction');
    var qDte = qs('dte');
    if (qTicker) { $('tkIn').value = qTicker.toUpperCase(); }
    if (qDirection === 'bull' || qDirection === 'bear') { $('dirIn').value = qDirection; }
    if (qDte) { $('dteIn').value = qDte; }
    return !!qTicker;
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('tkIn').addEventListener('input', updateCliRef);
    $('dirIn').addEventListener('change', updateCliRef);
    $('dteIn').addEventListener('input', updateCliRef);
    $('overrideReason').addEventListener('input', updateReasonCounter);
    ['tkIn', 'dteIn'].forEach(function (id) {
      $(id).addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { runCheckup(); }
      });
    });

    var shouldAutoRun = prefillFromQuery();
    updateCliRef();
    updateReasonCounter();
    if (shouldAutoRun) { runCheckup(); }
  });
})();
</script>
</body>
</html>
"""
