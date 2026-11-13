"""Journal Review —— 独立页面(不进 Trade Journal 的 tab 结构,直接开 /journal-review 用)。

「5 分钟开盘前复习」:昨天平了什么/开了什么、今天在仓要盯什么、本周有没有违反自己写的
铁律 —— 一句话洞察放最上面,零点击看到关键信息,深挖走 quick_links。

自包含单文件 HTML/CSS/JS,深色主题跟 PWA 其它独立页面对齐(照抄 stock_picker_page.py /
pretrade_checkup_page.py 的配色:#0d1117 背景 / #161b22 卡片 / #30363d 边框 /
#c9d1d9 文字 / #58a6ff 链接 / PASS #3fb950 / FAIL #f85149 / WARN #d29922,洞察卡片
额外用紫色 #a371f7 做左边框强调)。

本文件只管排版 + 一个 fetch(GET /api/journal_review),不写判断逻辑 —— KPI/规则违反/
what-if/回溯体检全在 journal_review/ 引擎里算好,这里原样显示引擎吐出来的字段。

用 r\"\"\"...\"\"\" 原始字符串装 HTML,避免 Python 字符串转义吃掉 JS 里的 `\'`
(见 gotcha_js_in_python_string_escaping)。复制命令用 data-cmd 属性 + 事件委托,
不用内联 onclick 塞动态字符串。
"""
from __future__ import annotations

JOURNAL_REVIEW_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Journal Review</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px 60px;
    background: #0d1117; color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 14px; line-height: 1.55;
  }
  .wrap { max-width: 860px; margin: 0 auto; }
  .topbar { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 20px; margin: 0; color: #c9d1d9; }
  .topbar-right { display: flex; align-items: center; gap: 10px; }
  .asof-note { color: #8b949e; font-size: 12px; white-space: nowrap; }
  .subtitle { color: #8b949e; font-size: 12.5px; margin: 6px 0 20px; }

  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .section-title {
    font-size: 12px; font-weight: 700; color: #8b949e; margin: 0 0 10px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }

  .btn-icon {
    border-radius: 6px; font-size: 13.5px; cursor: pointer; font-family: inherit;
    background: transparent; border: 1px solid #30363d; color: #c9d1d9; padding: 7px 11px;
  }
  .btn-icon:hover { background: #21262d; }
  .btn-icon:disabled { opacity: 0.5; cursor: not-allowed; }

  .spinner-wrap { display: none; align-items: center; gap: 10px; color: #8b949e; padding: 16px 0; }
  .spinner {
    width: 18px; height: 18px; border-radius: 50%;
    border: 2px solid #30363d; border-top-color: #58a6ff;
    animation: jr-spin 0.8s linear infinite;
  }
  @keyframes jr-spin { to { transform: rotate(360deg); } }

  .err-box {
    display: none; background: rgba(248, 81, 73, 0.1); border: 1px solid #f85149;
    color: #f85149; padding: 12px 14px; border-radius: 8px; margin-bottom: 16px;
    font-size: 13.5px; white-space: pre-wrap; word-break: break-word;
  }

  /* ── KPI band ── */
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 14px; }
  .kpi-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px; }
  .kpi-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 6px; }
  .kpi-value { font-size: 24px; font-weight: 700; line-height: 1.2; color: #c9d1d9; }
  .kpi-sub { font-size: 11.5px; color: #8b949e; margin-top: 4px; }
  .kpi-value.pos, .kpi-value.ok { color: #3fb950; }
  .kpi-value.neg, .kpi-value.fail { color: #f85149; }
  .kpi-value.warn { color: #d29922; }

  /* ── insight ── */
  .insight-card { border-left: 4px solid #a371f7; font-size: 14px; display: flex; gap: 10px; align-items: flex-start; }
  .insight-card .icon { font-size: 17px; }

  /* ── collapsible sections ── */
  .section-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; margin-bottom: 14px; overflow: hidden; }
  .section-card summary {
    padding: 12px 16px; cursor: pointer; display: flex; align-items: center; gap: 8px;
    font-weight: 600; font-size: 14px; list-style: none;
  }
  .section-card summary::-webkit-details-marker { display: none; }
  .section-card summary::before { content: '▸'; color: #6e7681; transition: transform 0.15s ease; }
  .section-card[open] summary::before { transform: rotate(90deg); }
  .count-badge { font-size: 11.5px; color: #8b949e; font-weight: 400; }
  .section-body { padding: 2px 16px 14px; }

  .row-item { padding: 10px 0; border-top: 1px solid #21262d; font-size: 13.5px; }
  .row-item:first-child { border-top: none; }
  .row-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .row-ticker { font-family: ui-monospace, 'SF Mono', Menlo, monospace; font-weight: 700; }
  .row-pnl { font-weight: 700; margin-left: auto; white-space: nowrap; }
  .dir-badge { font-size: 10.5px; font-weight: 700; padding: 1px 7px; border-radius: 10px; white-space: nowrap; }
  .dir-badge.bull { color: #3fb950; background: rgba(63, 185, 80, 0.12); }
  .dir-badge.bear { color: #f85149; background: rgba(248, 81, 73, 0.12); }
  .chip { display: inline-block; font-size: 11.5px; padding: 2px 8px; border-radius: 12px; white-space: nowrap; }
  .chip.danger { color: #f85149; background: rgba(248, 81, 73, 0.12); }
  .chip.warn { color: #d29922; background: rgba(210, 153, 34, 0.12); }
  .chip.ok { color: #3fb950; background: rgba(63, 185, 80, 0.12); }
  .chip.info { color: #58a6ff; background: rgba(88, 166, 255, 0.12); }
  .chip.muted { color: #8b949e; background: rgba(139, 148, 158, 0.12); }
  .row-detail { color: #8b949e; font-size: 12.5px; margin-top: 4px; }
  .row-detail b { color: #c9d1d9; }
  .empty-row { text-align: center; color: #8b949e; padding: 14px 4px; font-size: 13px; }

  .quick-links-card { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .qlink-btn {
    background: #21262d; border: 1px solid #30363d; color: #58a6ff; text-decoration: none;
    padding: 7px 14px; border-radius: 6px; font-size: 13px; cursor: pointer; font-family: inherit;
    display: inline-block;
  }
  .qlink-btn:hover { background: #1c2229; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>📓 Journal Review · 每天开盘前 5 分钟</h1>
    <div class="topbar-right">
      <span id="asOfNote" class="asof-note"></span>
      <button id="refreshBtn" class="btn-icon" title="重新拉一次">🔄</button>
    </div>
  </div>
  <div class="subtitle">数据: journal_review/ · 只读本地日记/缓存,不发起新的实时行情请求</div>

  <div id="spinner" class="spinner-wrap"><div class="spinner"></div><span>加载中…</span></div>
  <div id="errBox" class="err-box"></div>

  <div id="resultBox" style="display:none">
    <div class="kpi-grid" id="kpiGrid"></div>
    <div class="card insight-card" id="insightBox"></div>

    <details class="section-card" open>
      <summary>📉 昨日平的 <span id="closedCount" class="count-badge"></span></summary>
      <div id="closedBox" class="section-body"></div>
    </details>

    <details class="section-card" open>
      <summary>📊 昨日开的 <span id="openedCount" class="count-badge"></span></summary>
      <div id="openedBox" class="section-body"></div>
    </details>

    <details class="section-card" open>
      <summary>👀 今日重点关注(在仓) <span id="watchlistCount" class="count-badge"></span></summary>
      <div id="watchlistBox" class="section-body"></div>
    </details>

    <details class="section-card" open>
      <summary>🚨 本周规则违反 <span id="violationsCount" class="count-badge"></span></summary>
      <div id="violationsBox" class="section-body"></div>
    </details>

    <div class="card quick-links-card">
      <div class="section-title" style="width:100%">快速链接</div>
      <div id="quickLinksBox" style="display:contents"></div>
    </div>
  </div>
</div>

<script>
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fmtMoney(v) {
    if (v === null || v === undefined) { return '—'; }
    var n = Number(v);
    if (isNaN(n)) { return '—'; }
    var sign = n < 0 ? '-' : '+';
    return sign + '$' + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function pnlClass(v) {
    if (v === null || v === undefined) { return ''; }
    var n = Number(v);
    if (isNaN(n)) { return ''; }
    return n >= 0 ? 'pos' : 'neg';
  }

  function fmtPctVal(v, digits) {
    if (v === null || v === undefined) { return '—'; }
    var n = Number(v);
    if (isNaN(n)) { return '—'; }
    return (n > 0 ? '+' : '') + n.toFixed(digits === undefined ? 1 : digits) + '%';
  }

  function setLoading(on) {
    $('spinner').style.display = on ? 'flex' : 'none';
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

  // ── KPI band ──
  function renderKpi(kpi, nViolations) {
    kpi = kpi || {};
    var y = kpi.yesterday_net_pnl, w = kpi.week_net_pnl;
    var cash = kpi.cash_pct, floor = kpi.cash_pct_min;
    var posCount = kpi.positions_count, posMax = kpi.positions_max;

    var ruleMap = {
      ok: ['ok', '✅ 正常'],
      warn: ['warn', zt('⚠️ 有 ' + nViolations + ' 处违反', '⚠️ ' + nViolations + (nViolations === 1 ? ' violation' : ' violations'))],
      violated: ['fail', zt('🛑 违反较多(' + nViolations + ' 处)', '🛑 Many violations (' + nViolations + ')')]
    };
    var rs = ruleMap[kpi.rule_status] || ['', escapeHtml(kpi.rule_status || '—')];
    var cashCls = (cash === null || cash === undefined) ? '' : (cash >= floor ? 'ok' : 'fail');
    var posLeft = (posMax !== null && posMax !== undefined && posCount !== null && posCount !== undefined)
      ? Math.max(0, posMax - posCount) : null;

    var html = '';
    html += '<div class="kpi-card"><div class="kpi-label">昨日 P&amp;L</div>' +
            '<div class="kpi-value ' + pnlClass(y) + '">' + fmtMoney(y) + '</div>' +
            '<div class="kpi-sub">7 天: ' + fmtMoney(w) + '</div></div>';
    html += '<div class="kpi-card"><div class="kpi-label">' + zt('仓位', 'Positions') + '</div>' +
            '<div class="kpi-value">' + (posCount == null ? '—' : posCount) + '/' + (posMax == null ? '—' : posMax) + '</div>' +
            '<div class="kpi-sub">' + (posLeft === null ? '' : zt('剩 ' + posLeft + ' 个', posLeft + ' left')) + '</div></div>';
    html += '<div class="kpi-card"><div class="kpi-label">现金 %(估算)</div>' +
            '<div class="kpi-value ' + cashCls + '">' + (cash === null || cash === undefined ? '—' : cash.toFixed(1) + '%') + '</div>' +
            '<div class="kpi-sub">底线 ' + (floor == null ? '—' : floor + '%') + '</div></div>';
    html += '<div class="kpi-card"><div class="kpi-label">规则状态</div>' +
            '<div class="kpi-value ' + rs[0] + '" style="font-size:17px">' + rs[1] + '</div>' +
            '<div class="kpi-sub">近 7 天 · 点下方明细</div></div>';
    $('kpiGrid').innerHTML = html;
  }

  function renderInsight(text, textEn) {
    $('insightBox').innerHTML = '<span class="icon">🧭</span><span>' + escapeHtml(zt(text, textEn || text) || '(暂无洞察)') + '</span>';
  }

  // ── 昨日平的 ──
  function bucketChipClass(bucket) {
    if (!bucket) { return 'muted'; }
    if (bucket.indexOf('深亏') === 0) { return 'danger'; }
    if (bucket.indexOf('中亏') === 0 || bucket.indexOf('小亏') === 0) { return 'warn'; }
    if (bucket.indexOf('小赚') === 0 || bucket.indexOf('大赚') === 0) { return 'ok'; }
    return 'muted';
  }

  function whatifLine(w) {
    if (!w) { return 'what-if: 到期未到,暂无法算'; }
    var verdict = w.cut_was_wrong ? ('白砍 ' + fmtMoney(Math.abs(w.regret))) : ('砍对了,省下 ' + fmtMoney(Math.abs(w.regret)));
    return 'what-if 持有到期: ' + fmtMoney(w.hypothetical_pnl) + '(' + verdict + ')';
  }

  function renderClosed(rows) {
    rows = rows || [];
    $('closedCount').textContent = '(' + rows.length + ')';
    if (!rows.length) {
      $('closedBox').innerHTML = '<div class="empty-row">昨天没有平仓记录</div>';
      return;
    }
    var html = rows.map(function (r) {
      var dirCls = (r.direction === 'BULL') ? 'bull' : 'bear';
      return '<div class="row-item">' +
        '<div class="row-head">' +
          '<span class="dir-badge ' + dirCls + '">' + escapeHtml(r.direction || '') + '</span>' +
          '<span class="row-ticker">' + escapeHtml(r.ticker || '') + '</span>' +
          '<span>' + escapeHtml(r.spread_label || '') + '</span>' +
          (r.loss_bucket ? '<span class="chip ' + bucketChipClass(r.loss_bucket) + '">' + escapeHtml(r.loss_bucket) + '</span>' : '') +
          '<span class="row-pnl ' + pnlClass(r.net_pnl) + '">' + fmtMoney(r.net_pnl) + '</span>' +
        '</div>' +
        '<div class="row-detail">平仓时距到期 ' + (r.dte_at_close == null ? '—' : r.dte_at_close + ' 天') +
        ' · ' + escapeHtml(whatifLine(r.whatif)) + '</div>' +
      '</div>';
    }).join('');
    $('closedBox').innerHTML = html;
  }

  // ── 昨日开的 ──
  function renderOpened(rows) {
    rows = rows || [];
    $('openedCount').textContent = '(' + rows.length + ')';
    if (!rows.length) {
      $('openedBox').innerHTML = '<div class="empty-row">昨天没有新开仓</div>';
      return;
    }
    var html = rows.map(function (r) {
      var dirCls = (r.direction === 'BULL') ? 'bull' : 'bear';
      var passVal = r.retrospective_checkup_pass;
      var passChip;
      if (passVal === true) {
        passChip = '<span class="chip ok">✅ 回溯体检通过</span>';
      } else if (passVal === false) {
        var layers = (r.failed_layers_at_open || []).join(',');
        passChip = '<span class="chip danger">❌ 回溯 Layer ' + escapeHtml(layers) + ' fail</span>';
      } else {
        passChip = '<span class="chip muted">回溯体检未知</span>';
      }
      var overrideLine = r.was_override
        ? '<div class="row-detail">你 override 了,理由:"' + escapeHtml(r.override_reason || '') + '"</div>'
        : '';
      return '<div class="row-item">' +
        '<div class="row-head">' +
          '<span class="dir-badge ' + dirCls + '">' + escapeHtml(r.direction || '') + '</span>' +
          '<span class="row-ticker">' + escapeHtml(r.ticker || '') + '</span>' +
          '<span>' + escapeHtml(r.spread_label || '') + '</span>' +
          passChip +
        '</div>' + overrideLine +
      '</div>';
    }).join('');
    $('openedBox').innerHTML = html;
  }

  // ── 今日重点关注 ──
  function attentionChipClass(a) {
    if (a.indexOf('深亏') === 0) { return 'danger'; }
    if (a.indexOf('TP接近') === 0) { return 'ok'; }
    if (a.indexOf('近到期') === 0) { return 'warn'; }
    if (a.indexOf('财报') === 0) { return 'info'; }
    return 'muted';
  }

  function renderWatchlist(rows) {
    rows = rows || [];
    $('watchlistCount').textContent = '(' + rows.length + ')';
    if (!rows.length) {
      $('watchlistBox').innerHTML = '<div class="empty-row">当前没有持仓</div>';
      return;
    }
    var html = rows.map(function (r) {
      var dirCls = (r.direction === 'BULL') ? 'bull' : 'bear';
      var attention = r.attention || [];
      var attentionEn = r.attention_en || attention;
      var chips = attention.map(function (a, i) {
        return '<span class="chip ' + attentionChipClass(a) + '">' + escapeHtml(zt(a, attentionEn[i] || a)) + '</span>';
      }).join(' ');
      var reminder = (attention.indexOf('深亏') !== -1)
        ? '<div class="row-detail">⚠️ 记住:不止损!让它死等到期结算</div>' : '';
      return '<div class="row-item">' +
        '<div class="row-head">' +
          '<span class="dir-badge ' + dirCls + '">' + escapeHtml(r.direction || '') + '</span>' +
          '<span class="row-ticker">' + escapeHtml(r.ticker || '') + '</span>' +
          '<span>' + escapeHtml(r.spread_label || '') + '</span>' +
          '<span class="row-pnl ' + pnlClass(r.current_pnl_pct) + '">' + fmtPctVal(r.current_pnl_pct, 1) + '</span>' +
        '</div>' +
        '<div class="row-detail">' + (r.days_to_expiry == null ? '—' : r.days_to_expiry + ' DTE') +
        (chips ? ' &nbsp; ' + chips : '') + '</div>' + reminder +
      '</div>';
    }).join('');
    $('watchlistBox').innerHTML = html;
  }

  // ── 本周规则违反 ──
  function violationDetailLine(v) {
    var parts = [];
    if (v.date_range) { parts.push(zt('持续 ' + v.n_days + ' 天(' + escapeHtml(v.date_range) + ')', 'for ' + v.n_days + ' days (' + escapeHtml(v.date_range) + ')')); }
    if (v.actual !== undefined && v.actual !== null) { parts.push(zt('实际: ', 'actual: ') + v.actual); }
    if (v.actual_cash_pct !== undefined && v.actual_cash_pct !== null) { parts.push(zt('现金实际: ', 'actual cash: ') + v.actual_cash_pct + '%'); }
    if (v.net_pnl_sum !== undefined && v.net_pnl_sum !== null) { parts.push(zt('合计: ', 'total: ') + fmtMoney(v.net_pnl_sum)); }
    if (v.pnl_pct !== undefined && v.pnl_pct !== null) { parts.push(zt('亏损比例: ', 'loss: ') + v.pnl_pct + '%'); }
    if (v.dte_at_close !== undefined && v.dte_at_close !== null) { parts.push(zt('距到期 ' + v.dte_at_close + ' 天', v.dte_at_close + ' days to expiration')); }
    if (v.pct_of_nlv !== undefined && v.pct_of_nlv !== null) { parts.push(zt('占 NLV: ' + v.pct_of_nlv + '%', v.pct_of_nlv + '% of NLV')); }
    return parts.join(' · ');
  }

  function renderViolations(rows) {
    rows = rows || [];
    $('violationsCount').textContent = '(' + rows.length + ')';
    if (!rows.length) {
      $('violationsBox').innerHTML = '<div class="empty-row">本周未违反任何铁律,继续保持 ✅</div>';
      return;
    }
    var html = rows.map(function (v) {
      var trade = v.trade ? escapeHtml(v.trade) : '';
      var tradesLine = '';
      if (v.trades && v.trades.length) {
        var shown = v.trades.slice(0, 6).map(escapeHtml).join(zt('、', ', '));
        tradesLine = '<div class="row-detail">' + shown +
          (v.trades.length > 6 ? zt(' 等共 ' + v.trades.length + ' 笔', ' and more, ' + v.trades.length + ' in all') : '') + '</div>';
      }
      return '<div class="row-item">' +
        '<div class="row-head">' +
          '<span class="chip danger">' + escapeHtml(zt(v.rule || '', v.rule_en || v.rule || '')) + '</span>' +
          '<span>' + escapeHtml(v.date || '') + '</span>' +
          (trade ? '<span style="margin-left:auto">' + trade + '</span>' : '') +
        '</div>' +
        '<div class="row-detail">' + violationDetailLine(v) + '</div>' + tradesLine +
      '</div>';
    }).join('');
    $('violationsBox').innerHTML = html;
  }

  // ── 快速链接 ──
  function renderQuickLinks(links) {
    var html = (links || []).map(function (l) {
      if (l.url) {
        return '<a class="qlink-btn" href="' + escapeHtml(l.url) + '">' + escapeHtml(l.label) + '</a>';
      }
      if (l.cmd) {
        return '<button class="qlink-btn copy-cmd-btn" data-cmd="' + escapeHtml(l.cmd) + '">' + escapeHtml(l.label) + ' 📋</button>';
      }
      return '<span class="qlink-btn">' + escapeHtml(l.label) + '</span>';
    }).join('');
    $('quickLinksBox').innerHTML = html;
  }

  function copyCmdFromBtn(btn) {
    var text = btn.getAttribute('data-cmd') || '';
    if (!text || !navigator.clipboard) { return; }
    navigator.clipboard.writeText(text).then(function () {
      var orig = btn.textContent;
      btn.textContent = '✅ 已复制到剪贴板';
      setTimeout(function () { btn.textContent = orig; }, 1200);
    });
  }

  function renderResult(data) {
    clearError();
    $('resultBox').style.display = 'block';
    var now = new Date();
    var hh = String(now.getHours()).padStart(2, '0');
    var mm = String(now.getMinutes()).padStart(2, '0');
    $('asOfNote').textContent = 'as of ' + (data.as_of || '—') + ' ' + hh + ':' + mm;

    var nViol = (data.rule_violations_7d || []).length;
    renderKpi(data.kpi, nViol);
    renderInsight(data.insight, data.insight_en);
    renderClosed(data.yesterday_closed);
    renderOpened(data.yesterday_opened);
    renderWatchlist(data.watchlist_today);
    renderViolations(data.rule_violations_7d);
    renderQuickLinks(data.quick_links);
  }

  function loadReview() {
    clearError();
    setLoading(true);
    fetch('/api/journal_review').then(function (res) {
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
  window.loadReview = loadReview;

  document.addEventListener('DOMContentLoaded', function () {
    $('refreshBtn').addEventListener('click', loadReview);
    document.querySelector('.wrap').addEventListener('click', function (e) {
      var btn = e.target.closest('.copy-cmd-btn');
      if (btn) { copyCmdFromBtn(btn); }
    });
    loadReview();
  });
})();
</script>
</body>
</html>
"""
