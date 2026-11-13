"""TA Screener 前端页(iframe 自包含;不依赖 PWA 主页面任何 JS)。
纯字符串模板,无服务端注入 —— 页面自己 fetch /api/ta/results。"""

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>技术分析 Screener</title>
<style>
:root { --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --text:#e6edf3; --text2:#8b949e;
        --border:#30363d; --red:#f85149; --green:#3fb950; --blue:#58a6ff; --gold:#e3b341; --orange:#d8943a; }
* { box-sizing:border-box; }
html { overscroll-behavior-x:none; }   /* 横滑表格不触发浏览器后退 */
body { margin:0; background:var(--bg); color:var(--text); overscroll-behavior-x:none;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:13px; }
[style*="overflow-x"], [style*="overflow:auto"] { overscroll-behavior-x:contain; }
.wrap { padding:12px 16px 40px; max-width:1500px; margin:0 auto; }
h1 { font-size:16px; margin:0; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.muted { color:var(--text2); font-size:11px; font-weight:400; }
.bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:10px 0; }
button { background:var(--bg3); color:var(--text); border:1px solid var(--border);
         border-radius:6px; padding:5px 12px; font-size:12px; cursor:pointer; }
button:hover { border-color:var(--blue); }
button.primary { background:#238636; border-color:#238636; color:#fff; font-weight:600; }
button.chip { padding:3px 10px; border-radius:12px; font-size:11px; color:var(--text2); }
button.chip.on { background:#1f6feb; border-color:#1f6feb; color:#fff; }
input[type=text] { background:var(--bg2); color:var(--text); border:1px solid var(--border);
                   border-radius:6px; padding:5px 10px; font-size:12px; width:140px; }
select { background:var(--bg2); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:4px 8px; font-size:12px; }
#progressWrap { display:none; flex:1; min-width:220px; align-items:center; gap:8px; }
#progressBar { flex:1; height:8px; background:var(--bg3); border-radius:4px; overflow:hidden; }
#progressFill { height:100%; width:0%; background:linear-gradient(90deg,#1f6feb,#3fb950); transition:width .4s; }
table { width:100%; border-collapse:collapse; margin-top:6px; }
th { text-align:left; font-size:11px; color:var(--text2); padding:6px 8px; border-bottom:1px solid var(--border);
     cursor:pointer; user-select:none; white-space:nowrap; position:sticky; top:0; background:var(--bg); z-index:2; }
th:hover { color:var(--blue); }
th .arr { font-size:9px; }
td { padding:6px 8px; border-bottom:1px solid #1c2128; white-space:nowrap; vertical-align:middle; }
tr.row { cursor:pointer; }
tr.row:hover { background:#161b2280; }
.num { font-variant-numeric:tabular-nums; text-align:right; }
.badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:700; }
.b-sup { background:rgba(63,185,80,.15); color:var(--green); border:1px solid rgba(63,185,80,.4); }
.b-res { background:rgba(248,81,73,.15); color:var(--red); border:1px solid rgba(248,81,73,.4); }
.b-in  { background:rgba(227,179,65,.15); color:var(--gold); border:1px solid rgba(227,179,65,.4); }
.b-near{ background:rgba(88,166,255,.12); color:var(--blue); border:1px solid rgba(88,166,255,.35); }
.b-none{ color:var(--text2); }
.conf3 { color:var(--gold); font-weight:700; }
.conf2 { color:var(--blue); }
.pat { display:inline-block; background:var(--bg3); border:1px solid var(--border); border-radius:8px;
       padding:1px 7px; font-size:10px; color:var(--text2); margin:1px 2px 1px 0; }
.pat.bc { border-color:#6e40c9; color:#bc8cff; }
.pat.ssp-bull { border-color:rgba(63,185,80,.6); color:var(--green); font-weight:700; }
.pat.ssp-bear { border-color:rgba(248,81,73,.6); color:var(--red); font-weight:700; }
.edge-bull { color:var(--green); font-weight:700; }
.edge-bear { color:var(--red); font-weight:700; }
.detail { background:transparent; border:none; padding:0; margin:2px 0 8px; }
.detail-flex { display:flex; gap:8px; flex-wrap:wrap; align-items:flex-start; font-size:11px; }
.detail-flex > div { background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:8px 10px; }
.detail h4 { margin:0 0 6px; padding-bottom:4px; font-size:11px; color:var(--text2); font-weight:700; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:6px; }
.detail .ztab td, .detail .ztab th { padding:2px 7px; }
.modcard { background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:8px 10px; margin-bottom:8px; font-size:11px; }
.modcard h4 { margin:0 0 6px; padding-bottom:4px; font-size:11px; color:var(--text2); font-weight:700; border-bottom:1px solid var(--border); }
.modcard .ztab td, .modcard .ztab th { padding:2px 7px; }
.ztab td, .ztab th { padding:3px 8px; font-size:11px; border-bottom:1px solid #1c2128; position:static; cursor:default; }
canvas.spark { background:#0a0d12; border:1px solid var(--border); border-radius:6px; }
#empty { text-align:center; padding:60px 20px; color:var(--text2); }
.scorebar { display:inline-block; height:6px; border-radius:3px; background:#1f6feb; vertical-align:middle; }
.legend { font-size:10px; color:var(--text2); margin-top:4px; }
.sw { display:inline-block; width:10px; height:10px; border-radius:2px; vertical-align:-1px; margin-right:3px; }
/* ── ? 帮助图标 + 大白话弹窗 ── */
.hicon { display:inline-flex; align-items:center; justify-content:center; width:15px; height:15px; border-radius:50%;
         background:var(--bg3); border:1px solid var(--border); color:var(--text2); font-size:10px; font-weight:700;
         cursor:pointer; vertical-align:middle; line-height:1; user-select:none; flex:none; }
.hicon:hover { border-color:var(--blue); color:var(--blue); background:rgba(88,166,255,.14); }
#helpBody .ex { background:rgba(63,185,80,.08); border-radius:5px; padding:7px 10px; margin:8px 0; font-size:11.5px; color:var(--text2); }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="taH1">📐 技术分析 Screener
    <span class="muted" id="metaLine">加载中…</span>
  </h1>
  <div id="taSubtitle" class="muted" style="margin-top:2px">MTF 共振 S/R(日/周/月 pivot 聚类,x3=黄金)· 触及支撑/阻力自动置顶 · Finviz pattern · Barchart screener</div>
  <div id="mktBanner" style="display:none;margin-top:6px;font-size:12px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:5px 10px"></div>
  <div id="taScreenerView">

  <div class="bar">
    <button class="primary" id="runBtn" onclick="runScan()">▶ 跑扫描(专属池)</button>
    <div id="progressWrap"><div id="progressBar"><div id="progressFill"></div></div><span id="progressMsg" class="muted"></span></div>
  </div>
  <!-- EOD 期权链爬取状态 + 「现在盘后爬取」(每天收盘后自动爬,累积 EOD 期权链历史库)-->
  <div id="eodStatus" style="display:none;align-items:center;gap:8px;background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:7px 12px;margin:2px 0 8px;font-size:12px"></div>
  <div id="eodCrawlProg" style="display:none;align-items:center;gap:8px;margin:0 0 8px">
    <div style="flex:1;height:8px;max-width:340px;background:var(--bg3);border-radius:4px;overflow:hidden"><div id="eodCrawlFill" style="height:100%;width:0%;background:linear-gradient(90deg,#1f6feb,#3fb950);transition:width .4s"></div></div>
    <span id="eodCrawlMsg" class="muted" style="font-size:12px">爬取中…</span>
  </div>

  <div class="bar" id="filterBar" style="display:none">
    <input type="text" id="q" placeholder="搜 ticker…" oninput="render()">
    <!-- 两个多选下拉(Barchart 榜单 / TA 条件)+ clear/设为默认/预设, 由 /static/ta_filters.js 填 -->
    <span id="taFilterDD" style="display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap"></span>
    <span style="display:inline-flex;gap:4px;margin:0 6px">
      <button class="chip" id="vbTable" onclick="setTaView('table')" style="border-color:#1f6feb">📋 表格</button>
      <button class="chip on" id="vbChart" onclick="setTaView('chart')" style="border-color:#d29922;color:#e3b341">📊 图</button>
      <button class="chip" id="vbHeat" onclick="setTaView('heat')" style="border-color:#8957e5;color:#bc8cff">🔥 Heatmap</button>
    </span>
    <select id="secSel" onchange="onSecChange()" title="按 GICS 行业筛选(借 heatmap 分类)">
      <option value="">行业:全部</option>
    </select>
    <select id="earnSel" onchange="render()" title="财报筛选(14 天内)">
      <option value="">财报:全部</option>
      <option value="ex">🚫 排除财报(14天内)</option>
      <option value="only">📣 只看财报(14天内)</option>
    </select>
    <span class="muted" id="countLine"></span>
  </div>
  <!-- 快捷预设条(命名保存的筛选组合 + 设为默认), 由 ta_filters.js 填 -->
  <div id="taPresetBar" style="display:none;gap:6px;align-items:center;flex-wrap:wrap;margin:0 0 8px;font-size:12px"></div>

  <div id="empty" style="display:none"></div>
  <!-- 🔥 Heatmap 视图:扫描结果按 sector/industry 分组,方格按今日涨跌着色(像 heatmap tab,票更少)-->
  <div id="heatWrap" style="display:none;margin:0 0 10px">
    <div style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2);margin-bottom:8px;flex-wrap:wrap">
      分组:
      <button class="chip on" id="hgSec" onclick="setHeatGroup('sector')">Sector</button>
      <button class="chip" id="hgInd" onclick="setHeatGroup('industry')">Industry</button>
      <span class="muted">· 方格颜色 = 今日涨跌(绿涨红跌)· 大小 = 评分 · 点格子进详情</span>
    </div>
    <div id="heatBody"></div>
  </div>
  <!-- 📊 图视图:finviz 6 个月日线图(像 finviz 截图),2/3 每行,点图进详情 -->
  <div id="chartWrap" style="display:none;margin:0 0 10px">
    <div style="display:flex;align-items:center;gap:5px;font-size:12px;color:var(--text2);margin-bottom:8px;flex-wrap:wrap">
      区间:
      <button class="chip" id="cr63" onclick="setChartRange(63)">3月</button>
      <button class="chip on" id="cr126" onclick="setChartRange(126)">6月</button>
      <button class="chip" id="cr252" onclick="setChartRange(252)">12月</button>
      <button class="chip" id="cr504" onclick="setChartRange(504)">2年</button>
      <span style="margin-left:6px">每行:</span>
      <button class="chip" id="cc2" onclick="setChartCols(2)">2</button>
      <button class="chip" id="cc3" onclick="setChartCols(3)">3</button>
      <button class="chip" id="cc4" onclick="setChartCols(4)">4</button>
      <button class="chip on" id="cc5" onclick="setChartCols(5)">5</button>
      <span class="muted" style="margin-left:6px">· <b style="color:#e3a008">━SMA50</b> <b style="color:#2f81f7">━SMA200</b> · <b style="color:#e3b341">┄S/R</b>(⭐=日周月共振)· 自绘无延迟 · 点图进详情</span>
    </div>
    <div id="chartBody"></div>
  </div>
  <table id="tbl" style="display:none">
    <thead><tr id="hdr"></tr></thead>
    <tbody id="tbody"></tbody>
  </table>
  <div style="text-align:center;margin-top:10px">
    <button id="moreBtn" style="display:none" onclick="LIMIT=99999;render()">显示全部</button>
  </div>
  <div class="legend" id="legend" style="display:none">
    说明:<span class="sw" style="background:rgba(227,179,65,.45)"></span>黄金 zone(日+周+月共振)
    <span class="sw" style="background:rgba(88,166,255,.35);margin-left:8px"></span>x2 zone ·
    S/R 状态看「昨收 vs zone」:昨收在 zone 下方今日打进 = 测试阻力;上方打进 = 测试支撑 ·
    <b>评分 v4(0–100,化简,bull 偏向)</b>:
    <b>①S/R 40</b>(触及同向zone 16 + 共振x3 +10/x2 +5 + 每多1个pivot +3(≤9) + 历史达标 +5)
    + <b>③穿越EMA100/200 20</b>(今日上穿=多 12 分,历史达标 +8)
    + <b>④趋势 25</b>(长12/中8/短5 同向——顺势是你的 edge)
    + <b>⑤MACD趋势态 15</b>(多头排列=看多);
    <b>bull 偏向:牛市/中性只推 bull,bear 仅熊市 regime 出现</b>(实验:未来20日均+3%/55%涨,做空逆潮水)·
    <b>🔇 只展示不计分</b>:期权OI/Vol·PCR·GEX·统计edge(实验证对方向 spread≈±1%=噪音)/MaxPain/异动流/MACD金叉/RSI/SSP X20·X50 ·
    S/R 触及胜率为 walk-forward 回测(多头口径 4周+5%,同 SSP)·
    ⏰ 交易日自动扫:ET 9:40 / 10:30 / 12:00 / 15:00
</div>
  </div><!-- /taScreenerView -->


  <!-- ===== 单票详情页(?ticker=XXX 或点 🔍 进入;heatmap 跳转目标)===== -->
  <div id="taDetailView" style="display:none">
    <div class="bar" style="position:sticky;top:0;z-index:5;background:var(--bg);padding:6px 0">
      <span id="tdTitle" style="font-weight:800;font-size:16px"></span>
      <button class="chip" onclick="taOpenSec()" title="看这只票的 SEC 10-Q/10-K 财报(双语)" style="margin-left:8px;color:#58a6ff;border:1px solid #1f6feb">📄 财报</button>
      <span style="flex:1"></span>
      <button class="chip" id="tdReload" onclick="if(window._tdSym)showTaDetail(window._tdSym,true)" title="重新拉取">↻</button>
    </div>
    <div id="tdBody" style="margin-top:6px"></div>
  </div>
</div>
</div>

<script>
let DATA = null, ROWS = [], FILTERS = new Set(['all']), SORT = {key:'score', dir:-1}, LIMIT = 400, OPEN = null, _lastGen = null;
const F = k => FILTERS.has(k);   // 多选:多个 chip 同时生效(AND);默认只看「高把握」

// The ? beside each block of a ticker's detail opens a card from /static/zeus_help.js (Chinese and
// English, loaded by zeus.js), whose ids are 'ta.' + the key given here.
function hIcon(k) {
  return '<span class="hicon" onclick="event.stopPropagation();showHelp(\'' + k + '\')" title="点我看这块怎么读">?</span>';
}
function showHelp(k) {
  if (window.zeusHelp) window.zeusHelp.open('ta.' + k);
}

function fmtPct(v, signed) {
  if (v == null || isNaN(v)) return '—';
  return (signed && v > 0 ? '+' : '') + v.toFixed(1) + '%';
}

// 粗粒度 GICS 行业(借 heatmap metadata 的 sector)→ 中文短标签;'Unknown' 多为 ETF
const SEC_CN = {
  'Technology': '科技', 'Communication Services': '通信服务', 'Industrials': '工业',
  'Consumer Cyclical': '可选消费', 'Consumer Defensive': '必需消费',
  'Financial Services': '金融', 'Healthcare': '医疗', 'Energy': '能源',
  'Basic Materials': '原材料', 'Utilities': '公用事业', 'Real Estate': '房地产',
  'Unknown': 'ETF/其他', '未分类': '未分类'
};
const secKey = r => (r && r.sector) ? r.sector : '未分类';
function secLabel(s) { return SEC_CN[s] || s; }
function secTag(r) {
  if (!r.sector) return '';
  return '<div class="muted" style="font-size:10px;line-height:1.1;margin-top:1px" title="' +
    r.sector + (r.category ? ' · ' + r.category : '') + '">' + secLabel(r.sector) + '</div>';
}
function populateSectors() {
  const counts = {};
  ROWS.forEach(r => { const s = secKey(r); counts[s] = (counts[s] || 0) + 1; });
  const order = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  const sel = document.getElementById('secSel');
  const cur = sel.value;
  sel.innerHTML = '<option value="">行业:全部</option>' +
    order.map(s => '<option value="' + s + '">' + secLabel(s) + ' (' + counts[s] + ')</option>').join('');
  sel.value = cur;   // 刷新后保留选择
}
function onSecChange() { LIMIT = 400; OPEN = null; render(); }

async function load() {
  try {
    const r = await fetch('/api/ta/results');
    DATA = await r.json();
  } catch (e) { DATA = {empty:true, msg:'加载失败: ' + e}; }
  if (DATA.empty) {
    document.getElementById('empty').style.display = '';
    document.getElementById('empty').innerHTML = '📭 ' + (DATA.msg || '还没数据') +
      '<br><br><button class="primary" onclick="runScan()">▶ 跑第一次扫描(约 2-4 分钟)</button>';
    document.getElementById('metaLine').textContent = '';
    return;
  }
  ROWS = DATA.rows || [];
  _lastGen = DATA.generated_at || null;
  populateSectors();
  document.getElementById('empty').style.display = 'none';
  document.getElementById('filterBar').style.display = '';
  document.getElementById('tbl').style.display = '';
  document.getElementById('legend').style.display = '';
  document.getElementById('metaLine').textContent =
    DATA.generated_at + ' · ' + DATA.n_ok + ' 票' +
    (DATA.pool_active ? ' · 专属池 ' + DATA.n_pool + '/' + DATA.n_universe + '(剔不会动/没催化/太便宜)' : '/' + DATA.n_universe);
  // 大盘 regime → Bull 到期自适应,横幅展示
  var mk = DATA.market;
  var mkEl = document.getElementById('mktBanner');
  if (mk && mkEl) {
    var col = mk.regime === 'rising' ? 'var(--green)' : (mk.regime === 'fell' ? 'var(--orange,#d8943a)' : 'var(--text2)');
    mkEl.innerHTML = '<span style="color:' + col + ';font-weight:600">' + (mk.label || '') + '</span>';
    if (!window._tdSym) mkEl.style.display = '';    // 详情模式(打开某票)不显示大盘横幅
  }
  render();
}

function setFilter(btn) {
  const f = btn.dataset.f;
  if (f === 'all') { FILTERS.clear(); }            // 「全部」= 清空所有筛选
  else if (FILTERS.has(f)) { FILTERS.delete(f); }  // 再点取消
  else { FILTERS.add(f); }                          // 多选叠加(AND)
  syncChips();
  LIMIT = 400; OPEN = null;
  render();
}
function syncChips() {
  document.querySelectorAll('#filterBar .chip').forEach(b => {
    const f = b.dataset.f;
    b.classList.toggle('on', f === 'all' ? FILTERS.size === 0 : FILTERS.has(f));
  });
}

function passFilter(r) {
  const t = r.touch, e = (r.edge || {}).cur;
  const su = r.setup || {};
  // 高把握 = 有方向 + 评分≥50(实测胜率主体)— 默认开,去噪
  if (F('hiconf') && !((r.score_detail || {}).dir && (r.score || 0) >= 40)) return false;
  if (F('dip') && !su.dip_buy) return false;
  if (F('bigdown') && !su.big_down_today) return false;
  if (F('touch') && !t) return false;
  if (F('sup') && (!t || t.kind !== 'sup')) return false;
  if (F('res') && (!t || (t.kind !== 'res' && t.kind !== 'inside'))) return false;
  const q = document.getElementById('q').value.trim().toUpperCase();
  if (q && !r.ticker.includes(q)) return false;
  const ltr = ((r.setup || {}).trend || {}).long;   // Bull/Bear 按长期趋势(信趋势)
  if (F('bull') && ltr !== 'bull') return false;
  if (F('bear') && ltr !== 'bear') return false;
  const sec = document.getElementById('secSel').value;
  if (sec && secKey(r) !== sec) return false;
  const em = document.getElementById('earnSel').value;
  const soon = r.earnings && r.earnings.days != null && r.earnings.days >= 0 && r.earnings.days <= 14;
  if (em === 'ex' && soon) return false;
  if (em === 'only' && !soon) return false;
  return true;
}

const COLS = [
  {k:'score',   t:'评分',    cls:'num'},
  {k:'ticker',  t:'Ticker',  cls:''},
  {k:'close',   t:'收盘',    cls:'num'},
  {k:'chg_pct', t:'今日%',   cls:'num'},
  {k:'_trend',  t:'趋势 短·中·长', cls:''},
  {k:'_earn',   t:'财报',    cls:''},
  {k:'_touch',  t:'S/R 状态', cls:''},
  {k:'_dn',     t:'距下撑',  cls:'num'},
  {k:'_up',     t:'距上阻',  cls:'num'},
  {k:'max_conf',t:'Conf',    cls:'num'},
  {k:'_ssp',    t:'信号胜率(SSP/MACD/RSI/Vol)', cls:''},
  {k:'_opt',    t:'期权结构(仅展示)', cls:''},
  {k:'_rec',    t:'期权推荐(debit)', cls:''},
];

function sortVal(r, k) {
  if (k === '_touch') return r.touch ? (r.touch.conf * 10 + (r.touch.kind === 'sup' ? 2 : 1)) : -1;
  if (k === '_up') return r.nearest && r.nearest.up ? r.nearest.up.dist : 1e9;
  if (k === '_dn') return r.nearest && r.nearest.dn ? -r.nearest.dn.dist : 1e9;
  if (k === '_edge') { const e = (r.edge || {}).cur; return e ? Math.abs(e.t || 0) : -1; }
  if (k === '_ssp') {
    const ss = r.ssp; if (!ss) return -1;
    const all = (ss.bull || []).concat(ss.bear || []);
    const alerts = all.filter(s => s.alert).length;
    const todays = all.filter(s => s.today).length;
    return alerts * 1000 + todays * 10 + Math.max(0, ...all.map(s => s.rate || 0)) / 100;
  }
  if (k === '_earn') { const e = r.earnings; return (e && e.days != null && e.days >= 0) ? -e.days : -9999; }
  if (k === '_rec') { const rec = r.spread_rec; return rec && rec.primary ? (rec.primary.rr || 0) : -1; }
  if (k === '_trend') { const t = (r.setup || {}).trend || {}; const v = s => s === 'bull' ? 2 : (s === 'bear' ? 0 : 1); return v(t.long) * 100 + v(t.medium) * 10 + v(t.short); }
  if (k === '_opt') { return r.opt_bias_applied != null ? r.opt_bias_applied : -999; }
  return r[k] == null ? -1e18 : r[k];
}

function headSort(k) {
  if (SORT.key === k) SORT.dir = -SORT.dir; else { SORT.key = k; SORT.dir = -1; }
  render();
}

function _trArrow(s) {
  if (s === 'bull') return '<span style="color:var(--green)" title="bull">▲</span>';
  if (s === 'bear') return '<span style="color:var(--red)" title="bear">▼</span>';
  if (s === 'neutral') return '<span style="color:var(--text2)" title="neutral">—</span>';
  return '<span style="color:#444" title="数据不足">·</span>';
}
function trendCell(r) {
  const t = (r.setup || {}).trend || {};
  return '<span style="font-size:13px;letter-spacing:1px" title="短(SMA20)·中(SMA50)·长(SMA200):价格在均线上方且均线上行=▲bull,下方且下行=▼bear,其余=—中性">' +
    _trArrow(t.short) + _trArrow(t.medium) + _trArrow(t.long) + '</span>';
}
function setupTag(r) {
  const s = r.setup || {};
  if (s.dip_buy) return '<span title="长期多头(站上200日线)+ 回调/今天大跌 + 跌到支撑 = 你的抄底设置" style="color:#7ee787;font-weight:700">🎯</span> ';
  if (s.long_bull === false) return '<span title="跌破200日线(长期偏空)" style="color:var(--red)">🔻</span> ';
  return '';
}

function setupDetail(r) {
  const s = r.setup;
  if (!s) return '';
  const yn = (b, t, f) => b ? ('<span style="color:var(--green)">✓' + t + '</span>') : ('<span class="muted">✗' + (f || t) + '</span>');
  return '<div style="min-width:230px"><h4>抄底设置(你的打法) ' + hIcon('dip') + '</h4><div style="font-size:12px;line-height:2">' +
    (s.dip_buy ? '<div style="color:#7ee787;font-weight:700">🎯 符合抄底:长多 + 回调 + 跌到支撑</div>' : '<div class="muted">非典型抄底设置</div>') +
    yn(s.long_bull, '长期多头(站上200日线' + (s.above_sma200_pct != null ? ' +' + s.above_sma200_pct + '%' : '') + ')', '长期偏空(破200线)') + '<br>' +
    yn(s.at_support, '跌到支撑位(测试S/R支撑)' + (s.support_qualified ? ' ·历史达标' : ''), '未触支撑') + '<br>' +
    yn(s.big_down_today || s.pulled_back, '回调:今日 ' + (r.chg_pct > 0 ? '+' : '') + r.chg_pct + '% / 近10日 ' + (s.ret_10d > 0 ? '+' : '') + s.ret_10d + '%', '未明显回调') +
    '</div></div>';
}

function touchBadge(r) {
  const t = r.touch;
  if (t) {
    const c = 'x' + t.conf;
    const s = r.sr_stats || {};
    const supQ = (s.sup || {}).qualified, resQ = (s.res || {}).qualified;
    if (t.kind === 'sup') return '<span class="badge b-sup">🟩 测试支撑 ' + c + (supQ ? ' ·' + s.sup.rate + '%✓' : '') + '</span>';
    if (t.kind === 'res') return '<span class="badge b-res">🟥 测试阻力 ' + c + (resQ ? ' ·' + s.res.rate + '%✓' : '') + '</span>';
    return '<span class="badge b-in">⛳ zone内 ' + c + (resQ ? ' ·' + s.res.rate + '%✓' : '') + '</span>';
  }
  const nz = r.nearest || {};
  const ds = [];
  if (nz.up) ds.push({d: Math.abs(nz.up.dist), s: '距阻力 ' + fmtPct(nz.up.dist, true)});
  if (nz.dn) ds.push({d: Math.abs(nz.dn.dist), s: '距支撑 ' + fmtPct(nz.dn.dist, true)});
  if (ds.length) {
    ds.sort((a, b) => a.d - b.d);
    if (ds[0].d <= 3) return '<span class="badge b-near">📍 ' + ds[0].s + '</span>';
  }
  return '<span class="b-none">—</span>';
}

function edgeCell(r) {
  const e = (r.edge || {}).cur;
  if (!e) return '<span class="b-none">—</span>';
  const cls = e.sig ? (e.dir === '多' ? 'edge-bull' : 'edge-bear') : '';
  const star = e.sig ? '*' : '';
  return '<span class="' + cls + '">' + e.win_rate.toFixed(0) + '% / t=' + e.t.toFixed(1) + star +
         (e.sig ? ' ' + (e.dir === '多' ? '▲多' : '▼空') : '') + '</span>' +
         ' <span class="muted">n=' + e.n + '</span>';
}

function earnCell(r) {
  const e = r.earnings;
  if (!e || e.days == null || e.days < 0) return '<span class="b-none">—</span>';
  const md = (e.date || '').slice(5).replace('-', '/');
  const col = e.days <= 7 ? 'var(--red)' : (e.days <= 14 ? 'var(--orange,#d8943a)' : 'var(--text2)');
  return '<span style="color:' + col + ';font-weight:' + (e.days <= 14 ? '700' : '400') + '" title="财报 ' + (e.date || '') + ' ' + (e.time || '') + '">📅' + md + '(' + e.days + 'd)</span>';
}

function optCell(r) {
  const m = r.opt_metrics, ap = r.opt_bias_applied;
  if (!m) return '<span class="b-none">—</span>';
  const col = ap == null ? 'var(--text2)' : (ap > 0 ? 'var(--green)' : (ap < 0 ? 'var(--red)' : 'var(--text2)'));
  const head = ap != null ? ((ap > 0 ? '+' : '') + ap.toFixed(1)) : '±0';
  const tip = 'PCR(OI) ' + (m.pcr_oi != null ? m.pcr_oi : '—') +
    ' · OI重心 ' + (m.oi_center_pct > 0 ? '+' : '') + m.oi_center_pct + '%(上方OI ' + m.oi_above_share + '%)' +
    ' · MaxPain ' + (m.max_pain != null ? m.max_pain + '(' + (m.max_pain_pct > 0 ? '+' : '') + m.max_pain_pct + '%)' : '—') +
    ' · GEX' + ((m.gex_total || 0) >= 0 ? '+' : '−') + (m.gex_flip ? '(翻转 ' + m.gex_flip + ')' : '') +
    ' · 修正按方向对齐应用,合计封顶±10';
  return '<span title="' + tip + '" style="color:' + col + ';font-weight:700">' + head + '</span>' +
    ' <span class="muted" style="font-size:10px">P' + (m.pcr_oi != null ? m.pcr_oi : '—') +
    ' M' + (m.max_pain_pct != null ? ((m.max_pain_pct > 0 ? '+' : '') + m.max_pain_pct + '%') : '—') + '</span>';
}


function optDetail(r) {
  const m = r.opt_metrics, ob = r.opt_bias;
  if (!m || !ob) return '';
  const rows = (ob.parts || []).map(p =>
    '<tr><td>' + p.label + '</td><td>' + p.reading + '</td>' +
    '<td class="num" style="color:' + (p.pts > 0 ? 'var(--green)' : (p.pts < 0 ? 'var(--red)' : 'var(--text2)')) + '">' + (p.pts > 0 ? '+' : '') + p.pts + '</td></tr>').join('');
  const sdc2 = (r.score_detail || {}).components || {};
  const fs = m.flow_bull_share;
  return '<div style="min-width:300px"><h4>期权结构(近2个月度链,自算) ' + hIcon('opt') + '</h4>' +
    (fs != null ? '<div style="font-size:12px;margin-bottom:6px;background:var(--bg3);border-radius:5px;padding:6px 9px;line-height:1.8">' +
      '② OI/Vol 方向结构:<b style="color:' + (fs >= 55 ? 'var(--green)' : fs <= 45 ? 'var(--red)' : 'var(--text)') + '">看涨侧占比 ' + fs + '%</b>' +
      ' <span class="muted">(上方call量 vs 下方put量·距离衰减)</span>' +
      '<br><span class="muted">入分:</span> OI/Vol <b>+' + (sdc2.opt || 0) + '</b>/20 · PCR <b>+' + (sdc2.pcr || 0) + '</b>/5 · GEX <b>+' + (sdc2.gex || 0) + '</b>/5 <span class="muted">(只加不减)</span></div>' : '') +
    '<table class="ztab"><tr><th>指标</th><th>读数</th><th>分</th></tr>' + rows + '</table>' +
    '<div class="muted" style="margin-top:4px;font-size:11px;line-height:1.7">' +
    '上表 bias 合计 <b>' + (ob.total > 0 ? '+' : '') + ob.total + '</b>(看涨为正)— <b>仅展示,不计分</b>;计分走上面三块' +
    '<br>口径:OI/Vol结构(call堆现价上方=看涨/put堆下方=看跌,45%→0线性到80%→满20) · PCR&lt;1 bull加/&gt;1 bear加(±0.3带打满)' +
    '<br>GEX区位(≤60天链;现价高于gamma翻转位=bull加/低于=bear加,3%打满) · MaxPain/OI重心仅参考' +
    '<br>总OI ' + (m.total_oi || 0).toLocaleString() + '</div></div>';
}

function recCell(r) {
  const rec = r.spread_rec;
  if (!rec || !rec.primary) {
    const dir = (r.score_detail || {}).dir;
    if (dir === 'bear') return '<span class="ssp-bear" title="看空方向 · 按你的设置不推荐开 bear 单, 仅提示方向">📉 看空·不开单</span>';
    return '<span class="b-none">—</span>';
  }
  const p = rec.primary;
  const isBull = rec.dir === 'bull';
  const md = (p.expiration || '').slice(5).replace('-', '/');
  const warn = (p.earnings_warn ? ' ⚠️' : '') + (rec.rsi_warn ? ' ⛔RSI' + (rec.rsi_now != null ? rec.rsi_now.toFixed(0) : '') : '');
  const cls = isBull ? 'ssp-bull' : 'ssp-bear';
  return '<span class="pat ' + cls + '" title="' + p.ticket + ' · BE ' + p.breakeven + '(' + (p.be_vs_spot_pct > 0 ? '+' : '') + p.be_vs_spot_pct + '% vs 今价) · R/R ' + (p.rr || '—') + ':1 · 共' + rec.n + '个候选(点行展开)' + (p.earnings_warn ? ' · ⚠️财报在到期前' : '') + '">' +
    (isBull ? '📈BCS' : '📉BPS') + ' ' + md + ' ' + p.long_strike + '/' + p.short_strike + ' @' + p.debit.toFixed(2) + warn + ' <span style="opacity:.7">×' + rec.n + '</span></span>';
}

function sspCell(r) {
  const ss = r.ssp;
  if (!ss) return '<span class="b-none">—</span>';
  let h = '';
  (ss.bull || []).forEach(s => {
    if (s.alert) h += '<span class="pat ssp-bull" title="今日触发且历史达标(4周内High≥+5%)">▲' + s.key + ' ' + s.rate.toFixed(0) + '%·n' + s.n + '</span>';
    else if (s.today) h += '<span class="pat" title="今日触发但未达标(N=' + s.n + ',胜率' + s.rate + '%)">' + s.key + ' ' + s.rate.toFixed(0) + '%</span>';
  });
  (ss.bear || []).forEach(s => {
    if (s.alert) h += '<span class="pat ssp-bear" title="今日触发且历史达标(1周内Low≤−1%)">▼' + s.key + ' ' + s.rate.toFixed(0) + '%·n' + s.n + '</span>';
    else if (s.today) h += '<span class="pat" title="今日触发但未达标(N=' + s.n + ',胜率' + s.rate + '%)">' + s.key + ' ' + s.rate.toFixed(0) + '%</span>';
  });
  // TA 信号(MACD/RSI/Volume,同 SSP 口径实测):今日触发的 alert 上 chip
  ((r.tasig || {}).signals || []).forEach(s => {
    if (s.alert) h += '<span class="pat ' + (s.dir === 'bull' ? 'ssp-bull' : 'ssp-bear') + '" title="今日触发且实测达标 n=' + s.n + '">' + (s.dir === 'bull' ? '▲' : '▼') + s.name + ' ' + s.rate.toFixed(0) + '%</span>';
  });
  // RSI 极端提示(v3 不罚分,仅提醒别追高杀跌)
  const ts = r.tasig || {};
  if (ts.rsi != null && (ts.rsi >= 75 || ts.rsi <= 25)) {
    h += '<span class="pat" style="border-color:var(--red);color:var(--red)" title="RSI 极端提示:超买慎开bull/超卖慎开bear(不计分,仅提醒)">⛔RSI ' + ts.rsi.toFixed(0) + '</span>';
  }
  return h || '<span class="b-none">—</span>';
}

let _bcTickers = null;   // Barchart 榜单交集 tickers(null=不按 Barchart 过滤);由 ta_filters.js 设置
function render() {
  let base = ROWS;
  if (_bcTickers) {   // 选了 Barchart 榜单:以交集为底, 池外票建最小 row(图/热力可画,表格简显)
    const _m = {}; ROWS.forEach(r => _m[String(r.ticker).toUpperCase()] = r);
    base = _bcTickers.map(t => _m[t] || { ticker: t, score: 0, close: null, chg_pct: null, nearest: {}, max_conf: 0, score_detail: {}, setup: {}, sector: '未分类', _nonpool: true });
  }
  const rows = base.filter(passFilter);
  rows.sort((a, b) => (sortVal(b, SORT.key) - sortVal(a, SORT.key)) * (SORT.dir === -1 ? 1 : -1));
  if (TAVIEW === 'heat') { renderHeat(rows); return; }
  if (TAVIEW === 'chart') { renderCharts(rows); return; }
  document.getElementById('hdr').innerHTML = COLS.map(c =>
    '<th onclick="headSort(\'' + c.k + '\')">' + c.t +
    (SORT.key === c.k ? ' <span class="arr">' + (SORT.dir === -1 ? '▼' : '▲') + '</span>' : '') + '</th>').join('');
  const maxScore = Math.max(1, ...rows.map(r => r.score || 0));
  const body = [];
  rows.slice(0, LIMIT).forEach((r, i) => {
    if (r._nonpool) {   // Barchart 池外票:表格简显(无 TA 数据), 点开看图/Finviz
      body.push('<tr class="row" onclick="showTaDetail(\'' + r.ticker + '\')" style="cursor:pointer">' +
        '<td class="num muted">—</td><td><b>' + r.ticker + '</b> <span class="muted" style="font-size:11px">池外·点开看图</span></td>' +
        '<td class="num">' + (r.close == null ? '—' : r.close) + '</td>' +
        '<td colspan="10" class="muted">不在 TA 池(无评分)· 点开看日线图 / Finviz / 新闻</td></tr>');
      return;
    }
    const chgCls = r.chg_pct > 0 ? 'style="color:var(--green)"' : (r.chg_pct < 0 ? 'style="color:var(--red)"' : '');
    const sdc = (r.score_detail || {}).components || {};
    const sdTip = (r.score_detail && r.score_detail.dir)
      ? ('S/R ' + (sdc.sr || 0) + ' · 穿越EMA ' + (sdc.emax || 0) + ' · 趋势 ' + (sdc.trend || 0) +
         ' · MACD ' + (sdc.macd || 0) + (sdc.extra ? ' · 额外+' + sdc.extra : ''))
      : '今日无同向证据(评分 0)';
    body.push('<tr class="row" onclick="toggleDetail(\'' + r.ticker + '\')">' +
      '<td class="num" title="' + sdTip.replace(/"/g, '&quot;') + '"><span class="scorebar" style="width:' + Math.max(3, r.score / maxScore * 46) + 'px"></span> ' + r.score.toFixed(0) + '</td>' +
      '<td>' + setupTag(r) + '<b>' + r.ticker + '</b>' + secTag(r) +
        ' <span onclick="event.stopPropagation();showTaDetail(\'' + r.ticker + '\')" title="打开单票详情页(TA + Digest 新闻 + Finviz)" style="cursor:pointer;opacity:.55;font-size:11px">🔍</span></td>' +
      '<td class="num">' + r.close + '</td>' +
      '<td class="num" ' + chgCls + '>' + fmtPct(r.chg_pct, true) + '</td>' +
      '<td>' + trendCell(r) + '</td>' +
      '<td>' + earnCell(r) + '</td>' +
      '<td>' + touchBadge(r) + '</td>' +
      '<td class="num">' + (r.nearest && r.nearest.dn ? fmtPct(r.nearest.dn.dist, true) + ' <span class="conf' + r.nearest.dn.conf + '">x' + r.nearest.dn.conf + '</span>' : '—') + '</td>' +
      '<td class="num">' + (r.nearest && r.nearest.up ? fmtPct(r.nearest.up.dist, true) + ' <span class="conf' + r.nearest.up.conf + '">x' + r.nearest.up.conf + '</span>' : '—') + '</td>' +
      '<td class="num conf' + r.max_conf + '">' + (r.max_conf >= 3 ? '⭐x3' : 'x' + r.max_conf) + '</td>' +
      '<td style="white-space:normal;max-width:210px">' + sspCell(r) + '</td>' +
      '<td>' + optCell(r) + '</td>' +
      '<td>' + recCell(r) + '</td></tr>');
    if (OPEN === r.ticker) body.push('<tr><td colspan="13">' + detailHtml(r) + '</td></tr>');
  });
  document.getElementById('tbody').innerHTML = body.join('');
  document.getElementById('countLine').textContent = rows.length + ' 票' + (rows.length > LIMIT ? '(显示前 ' + LIMIT + ')' : '');
  document.getElementById('moreBtn').style.display = rows.length > LIMIT ? '' : 'none';
  if (OPEN) { const r = rows.find(x => x.ticker === OPEN); if (r) drawSpark(r); }
}

async function toggleDetail(t) {
  if (OPEN === t) { OPEN = null; render(); return; }
  OPEN = t;
  const r = ROWS.find(x => x.ticker === t);
  if (r && r.spark === undefined) {            // 详情重数据按需拉(列表已剥离 spark/zones)
    r.spark = null;                            // 占位防重复请求
    try {
      const dd = await fetch('/api/ta/detail/' + encodeURIComponent(t)).then(x => x.json());
      r.spark = dd.spark || null;
      r.zones = dd.zones || [];
    } catch (e) { r.zones = r.zones || []; }
  }
  // 没批量算到推荐 → 现拉一个(只要有方向/趋势就给 debit spread)
  if (r && !r.spread_rec && r._recTried === undefined) {
    r._recTried = true;
    try {
      const sr = await fetch('/api/ta/spread/' + encodeURIComponent(t)).then(x => x.json());
      if (sr && sr.spread_rec) r.spread_rec = sr.spread_rec;
      else r._recErr = (sr && sr.error) || '无推荐';
    } catch (e) { r._recErr = '拉取失败'; }
  }
  render();
}

function detailHtml(r) {
  const zrows = (r.zones || []).map(z =>
    '<tr><td class="conf' + z.conf + '">' + (z.conf >= 3 ? '⭐x3' : 'x' + z.conf) + '</td>' +
    '<td class="num">' + z.avg + '</td><td class="num">' + z.lo + ' – ' + z.hi + '</td>' +
    '<td>' + z.tfs + '</td><td class="num">' + z.n_pivots + '</td>' +
    '<td class="num">' + fmtPct((z.avg / r.close - 1) * 100, true) + '</td></tr>').join('');
  return '<div class="detail"><div class="detail-flex">' +
    '<div><h4 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' + r.ticker + ' · S/R zones(黄金=金色带) ' +
    '<span style="font-weight:400;font-size:11px;display:inline-flex;gap:3px">' +
    '<button class="sparkWinBtn chip" data-win="63" onclick="setSparkWin(63)">短·3月</button>' +
    '<button class="sparkWinBtn chip on" data-win="252" onclick="setSparkWin(252)">中·1年</button>' +
    '<button class="sparkWinBtn chip" data-win="0" onclick="setSparkWin(0)">长·2年</button>' +
    '</span></h4>' +
    '<canvas class="spark" id="spark" width="760" height="230"></canvas></div>' +
    '<div style="min-width:300px"><h4>S/R zones(' + (r.zones || []).length + ') ' + hIcon('srzones') + '</h4>' +
    '<table class="ztab"><tr><th>Conf</th><th>中枢</th><th>区间</th><th>周期</th><th>pivots</th><th>距离</th></tr>' + zrows + '</table></div>' +
    '<div style="min-width:220px"><h4>关键位 ' + hIcon('keylevels') + '</h4>' +
    '<div class="muted">52周位置:<b>' + r.pos52 + '%</b></div>' + srStatsLine(r) + '</div>' +
    setupDetail(r) +
    optDetail(r) +
    recDetail(r) +
    '</div></div>';
}

function srStatsLine(r) {
  const s = r.sr_stats;
  if (!s) return '';
  const f = (x, lab) => x && x.n > 0 ? (lab + ' <b class="' + (x.qualified ? 'edge-bull' : '') + '">' + x.rate + '%</b><span class="muted">(n=' + x.n + (x.qualified ? '·edge+' + x.lift : (x.lift != null ? '·lift' + (x.lift > 0 ? '+' : '') + x.lift : '')) + ')</span>') : (lab + ' <span class="muted">样本不足</span>');
  return '<div style="margin-top:6px;font-size:11px;color:var(--text2)">本票 S/R 触及历史(walk-forward):<br>' +
    f(s.sup, '支撑→4周+5%') + ' · ' + f(s.res, '阻力→1周−1%') + '</div>';
}

function recDetail(r) {
  const rec = r.spread_rec;
  if (!rec || !(rec.candidates || []).length) {
    if (r._recErr) return '<div style="min-width:230px"><h4>期权推荐</h4><div class="muted" style="font-size:12px">' + r._recErr + '<br>(方向:' + ((r.setup || {}).long_bull ? '长期多头→可考虑 bull call' : '—') + ')</div></div>';
    return '';
  }
  const isBull = rec.dir === 'bull';
  const rows = rec.candidates.map((c, i) => {
    const beOk = c.be_vs_spot_pct <= 0.2;
    return '<tr style="' + (i === 0 ? 'background:rgba(63,185,80,.10)' : '') + '">' +
      '<td>' + (i === 0 ? '✓推荐' : '') + '</td>' +
      '<td>' + c.variant + '</td>' +
      '<td>' + (c.expiration || '').slice(5) + '<span class="muted">(' + c.dte + 'd' + (String(c.exp_type).toLowerCase() === 'regular' ? '·月' : '') + ')</span></td>' +
      '<td class="num">' + c.long_strike + '/' + c.short_strike + '</td>' +
      '<td class="num" style="color:var(--red)">$' + ((c.max_loss != null ? c.max_loss : c.debit * 100)).toFixed(0) + '</td>' +
      '<td class="num" style="color:var(--green)">$' + ((c.max_profit != null ? c.max_profit : c.max_payoff)).toFixed(0) + '</td>' +
      '<td class="num"><b>' + (c.rr || '—') + ':1</b></td>' +
      '<td class="num">' + (c.pop != null ? '<b style="color:' + (c.pop >= 50 ? 'var(--green)' : c.pop >= 35 ? 'var(--gold)' : 'var(--red)') + '">' + c.pop + '%</b>' : '—') + '</td>' +
      '<td class="num" style="color:' + (beOk ? 'var(--green)' : 'var(--red)') + '">' + (c.be_vs_spot_pct > 0 ? '+' : '') + c.be_vs_spot_pct + '%</td>' +
      '<td>' + (c.earnings_warn ? '<span style="color:var(--red)">⚠️财报</span>' : '<span class="muted">—</span>') +
      ' <button onclick="openInBuilder(\'' + r.ticker + '\',\'' + c.expiration + '\',\'' + rec.dir + '\',' + c.long_strike + ',' + c.short_strike + ')" style="font-size:9px;background:#1f6feb;color:#fff;border:none;border-radius:3px;padding:2px 6px;cursor:pointer;margin-right:3px" title="载入模拟盘">⚡模拟盘</button>' +
      ' <code style="font-size:9px;background:var(--bg3);padding:1px 4px;border-radius:3px;cursor:pointer" title="' + c.ticket + '(点击复制)" onclick="navigator.clipboard&&navigator.clipboard.writeText(this.title.split(\'(\')[0].trim())">📋</code></td></tr>';
  }).join('');
  return '<div style="min-width:430px"><h4>期权推荐 · ' + (isBull ? '📈 Bull Call(ATM/+10%·+5% · 35-45 DTE)' : '📉 看空(不推荐开单)') + ' · ' + rec.n + ' 个候选 ' + hIcon('rec') + '</h4>' +
    (rec.dte_reason ? '<div class="muted" style="font-size:11px;margin-bottom:4px">⏳ 到期逻辑:' + rec.dte_reason + '</div>' : '') +
    '<table class="ztab"><tr><th></th><th>腿型</th><th>到期</th><th>strikes</th><th>借记</th><th>最大亏</th><th>最大盈</th><th>R/R</th><th title="到期价越过盈亏平衡的概率(lognormal·long腿隐波)">POP</th><th>BE vs今价</th><th></th></tr>' + rows + '</table>' +
    '<div class="muted" style="margin-top:4px">' + (isBull ? '✓推荐 = 盈亏平衡≤今价里 R/R 最高(赌不破今价);BE% 绿=平衡点在今价下方' : '✓推荐 = R/R 最高') + ' · 最大亏=借记×100 · POP=盈利概率(隐波估) · 📋复制订单票</div></div>';
}


function drawSpark(r) {
  window._sparkRow = r;
  if (window._sparkWin === undefined) window._sparkWin = 252;   // 默认中(~1年)
  const cv = document.getElementById('spark');
  if (!cv || !r.spark) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height, padL = 46, padR = 8, padT = 8, padB = 18;
  ctx.clearRect(0, 0, W, H);
  const win = window._sparkWin;                                  // 0=全部(长)
  let cl = r.spark.close, ds = r.spark.dates;
  if (win > 0 && cl.length > win) { cl = cl.slice(-win); ds = ds.slice(-win); }
  if (!cl || cl.length < 2) return;
  let lo = Math.min(...cl), hi = Math.max(...cl);
  (r.zones || []).forEach(z => { if (z.lo < hi * 1.15 && z.hi > lo * 0.85) { lo = Math.min(lo, z.lo); hi = Math.max(hi, z.hi); } });
  const pad = (hi - lo) * 0.04 || 1; lo -= pad; hi += pad;
  const X = i => padL + i / (cl.length - 1) * (W - padL - padR);
  const Y = v => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  // zone 色带
  (r.zones || []).forEach(z => {
    if (z.hi < lo || z.lo > hi) return;
    ctx.fillStyle = z.conf >= 3 ? 'rgba(227,179,65,0.20)' : 'rgba(88,166,255,0.10)';
    ctx.fillRect(padL, Y(z.hi), W - padL - padR, Math.max(2, Y(z.lo) - Y(z.hi)));
    ctx.strokeStyle = z.conf >= 3 ? 'rgba(227,179,65,0.5)' : 'rgba(88,166,255,0.25)';
    ctx.strokeRect(padL, Y(z.hi), W - padL - padR, Math.max(2, Y(z.lo) - Y(z.hi)));
  });
  // y 轴
  ctx.font = '9px monospace'; ctx.fillStyle = '#8b949e'; ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4;
    ctx.fillText(v.toFixed(v > 50 ? 0 : 2), padL - 4, Y(v) + 3);
  }
  // close 线
  ctx.strokeStyle = '#e6edf3'; ctx.lineWidth = 1.6; ctx.beginPath();
  cl.forEach((v, i) => { i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); });
  ctx.stroke();
  // 最后价标记
  ctx.fillStyle = '#3fb950'; ctx.beginPath(); ctx.arc(X(cl.length - 1), Y(cl[cl.length - 1]), 3, 0, 6.3); ctx.fill();
  // x 轴日期
  ctx.textAlign = 'center';
  [0, Math.floor(ds.length / 2), ds.length - 1].forEach(i => ctx.fillText(ds[i].slice(5), X(i), H - 5));
}

// S/R 图 短/中/长 窗口切换(63根≈3月 / 252≈1年 / 0=全部≈2年)
function setSparkWin(win) {
  window._sparkWin = win;
  document.querySelectorAll('.sparkWinBtn').forEach(b => b.classList.toggle('on', (+b.dataset.win) === win));
  if (window._sparkRow) drawSpark(window._sparkRow);
}

// ── 视图切换:Table / 图(finviz) / Heatmap ──
let TAVIEW = 'chart', HEAT_GROUP = 'sector', CHART_COLS = 5, CHART_RANGE = 126;
function setTaView(v) {
  TAVIEW = v;
  document.getElementById('vbTable').classList.toggle('on', v === 'table');
  document.getElementById('vbChart').classList.toggle('on', v === 'chart');
  document.getElementById('vbHeat').classList.toggle('on', v === 'heat');
  document.getElementById('tbl').style.display = v === 'table' ? '' : 'none';
  document.getElementById('chartWrap').style.display = v === 'chart' ? '' : 'none';
  document.getElementById('heatWrap').style.display = v === 'heat' ? '' : 'none';
  const lg = document.getElementById('legend'); if (lg) lg.style.display = v === 'table' ? '' : 'none';
  render();
}
function setHeatGroup(g) {
  HEAT_GROUP = g;
  document.getElementById('hgSec').classList.toggle('on', g === 'sector');
  document.getElementById('hgInd').classList.toggle('on', g === 'industry');
  render();
}
function setChartCols(n) {
  CHART_COLS = n;
  [2, 3, 4, 5].forEach(c => { const b = document.getElementById('cc' + c); if (b) b.classList.toggle('on', n === c); });
  render();
}
function setChartRange(days) {
  CHART_RANGE = days;
  [63, 126, 252, 504].forEach(x => { const b = document.getElementById('cr' + x); if (b) b.classList.toggle('on', x === days); });
  render();
}
// 📊 图视图:finviz 6 个月日线图(candle + SMA50/200 + 量 + 趋势线),2/3 每行,点进详情
function renderCharts(rows) {
  document.getElementById('tbl').style.display = 'none';
  document.getElementById('heatWrap').style.display = 'none';
  document.getElementById('chartWrap').style.display = '';
  const lg = document.getElementById('legend'); if (lg) lg.style.display = 'none';
  document.getElementById('countLine').textContent = rows.length + ' 票 · 图';
  document.getElementById('moreBtn').style.display = 'none';
  const body = document.getElementById('chartBody');
  if (!rows.length) { body.innerHTML = '<div class="muted" style="padding:24px;text-align:center">当前筛选无票</div>'; return; }
  let h = '<div style="display:grid;grid-template-columns:repeat(' + CHART_COLS + ',minmax(0,1fr));gap:10px">';
  rows.forEach(r => {
    const cp = r.chg_pct == null ? 0 : r.chg_pct;
    const cpc = cp > 0 ? 'var(--green)' : (cp < 0 ? 'var(--red)' : 'var(--text2)');
    const dir = (r.score_detail || {}).dir;
    h += '<div onclick="showTaDetail(\'' + r.ticker + '\')" style="cursor:pointer;background:var(--bg2);border:1px solid ' + (dir === 'bull' ? '#238636' : 'var(--border)') + ';border-radius:8px;overflow:hidden">' +
      '<div style="display:flex;align-items:center;gap:7px;padding:5px 9px;font-size:12px">' +
        '<b style="color:#e6edf3;font-size:13px">' + r.ticker + '</b>' +
        '<span class="muted">' + (r.close != null ? r.close : '') + '</span>' +
        '<span style="color:' + cpc + '">' + (cp >= 0 ? '+' : '') + cp.toFixed(2) + '%</span>' +
        '<span style="font-size:10px;color:var(--text2)">' + secLabel(r.sector || '') + '</span>' +
        '<span style="margin-left:auto;font-weight:700;color:' + ((r.score || 0) >= 50 ? 'var(--green)' : 'var(--text2)') + '">分 ' + (r.score || 0).toFixed(0) + '</span></div>' +
      '<canvas class="miniChart" data-ticker="' + r.ticker + '" style="width:100%;display:block;height:172px;background:#0b0e14"></canvas></div>';
  });
  h += '</div>';
  body.innerHTML = h;
  _ensureChartObs();
  body.querySelectorAll('.miniChart').forEach(cv => _chartObs.observe(cv));
}
// 懒加载:画进视口的图才拉 OHLC 自绘(自己的数据,无 finviz 延迟,所有票都有)
let _chartObs = null;
const _ohlcCache = {};
function _ensureChartObs() {
  if (_chartObs) return;
  _chartObs = new IntersectionObserver((ents) => {
    ents.forEach(e => { if (e.isIntersecting) { _chartObs.unobserve(e.target); loadChart(e.target); } });
  }, {rootMargin: '300px'});
}
async function loadChart(cv) {
  const t = cv.dataset.ticker, key = t + '_' + CHART_RANGE;
  try {
    let d = _ohlcCache[key];
    if (!d) {
      d = await (await fetch('/api/ta/ohlc/' + encodeURIComponent(t) + '?days=' + CHART_RANGE)).json();
      if (!d || d.error || !d.c || !d.c.length) d = await (await fetch('/api/barchart/ohlc/' + encodeURIComponent(t) + '?days=' + CHART_RANGE)).json();  // 池外票 yfinance fallback
      if (d && d.c && d.c.length) _ohlcCache[key] = d;
    }
    const cx = cv.getContext('2d');
    const dpr = window.devicePixelRatio || 1, W = cv.clientWidth || 360, H = 172;
    cv.width = W * dpr; cv.height = H * dpr; cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx.clearRect(0, 0, W, H);
    if (!d || !d.c || !d.c.length) { cx.fillStyle = '#6e7681'; cx.font = '12px sans-serif'; cx.fillText('无数据', 10, 22); return; }
    drawCandles(cx, d, W, H);
  } catch (e) {}
}
function drawCandles(cx, d, W, H) {
  const n = d.c.length, padR = 46, padL = 4, padT = 6;
  const volH = 30, gap = 6, priceB = H - volH - gap, priceH = priceB - padT;
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) { if (d.l[i] != null && d.l[i] < lo) lo = d.l[i]; if (d.h[i] != null && d.h[i] > hi) hi = d.h[i]; }
  [d.sma50, d.sma200].forEach(s => s && s.forEach(v => { if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; } }));
  if (!(hi > lo)) return;
  const pad = (hi - lo) * 0.04; lo -= pad; hi += pad;
  const plotW = W - padL - padR;
  const X = i => padL + (n <= 1 ? plotW / 2 : i / (n - 1) * plotW);
  const Y = v => padT + (1 - (v - lo) / (hi - lo)) * priceH;
  const bw = Math.max(1, Math.min(7, plotW / n * 0.66));
  // 价格网格 + 右轴
  cx.font = '9px -apple-system,monospace'; cx.textAlign = 'left';
  cx.strokeStyle = 'rgba(120,130,150,0.12)';
  for (let k = 0; k <= 3; k++) { const v = lo + (hi - lo) * k / 3, y = Y(v); cx.beginPath(); cx.moveTo(padL, y); cx.lineTo(W - padR, y); cx.stroke(); cx.fillStyle = '#6e7681'; cx.fillText(v.toFixed(v >= 100 ? 0 : 1), W - padR + 3, y + 3); }
  // S/R zones(横线+band;支撑绿/阻力红 by 末价;conf→粗细;⭐=日周月共振 conf≥3)
  if (d.zones && d.zones.length) {
    const lastC = d.c[n - 1];
    d.zones.forEach(z => {
      if (z.avg > hi || z.avg < lo) return;
      const rgb = z.avg < lastC ? '63,185,80' : '248,81,73';
      cx.fillStyle = 'rgba(' + rgb + ',0.07)'; cx.fillRect(padL, Y(z.hi), W - padR - padL, Math.max(1, Y(z.lo) - Y(z.hi)));
      cx.strokeStyle = 'rgba(' + rgb + ',' + Math.min(0.55, 0.2 + z.conf * 0.12) + ')';
      cx.lineWidth = z.conf >= 3 ? 1.5 : 1; cx.setLineDash(z.conf >= 3 ? [] : [3, 3]);
      cx.beginPath(); cx.moveTo(padL, Y(z.avg)); cx.lineTo(W - padR, Y(z.avg)); cx.stroke();
      cx.setLineDash([]); cx.lineWidth = 1;
      if (z.conf >= 3) { cx.fillStyle = 'rgba(' + rgb + ',0.95)'; cx.textAlign = 'left'; cx.fillText('⭐', padL + 1, Y(z.avg) - 2); }
    });
  }
  // 量
  let vmax = 1; for (let i = 0; i < n; i++) if (d.v[i] > vmax) vmax = d.v[i];
  for (let i = 0; i < n; i++) { const up = d.c[i] >= d.o[i]; const vh = (d.v[i] / vmax) * volH; cx.fillStyle = up ? 'rgba(63,185,80,0.45)' : 'rgba(248,81,73,0.45)'; cx.fillRect(X(i) - bw / 2, H - vh, bw, vh); }
  // candle
  for (let i = 0; i < n; i++) {
    if (d.o[i] == null) continue;
    const up = d.c[i] >= d.o[i], col = up ? '#3fb950' : '#f85149';
    cx.strokeStyle = col; cx.fillStyle = col;
    cx.beginPath(); cx.moveTo(X(i), Y(d.h[i])); cx.lineTo(X(i), Y(d.l[i])); cx.stroke();
    const yo = Y(d.o[i]), yc = Y(d.c[i]); cx.fillRect(X(i) - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
  }
  // SMA50(橙)/ SMA200(蓝)
  const line = (s, color) => {
    if (!s) return; cx.strokeStyle = color; cx.lineWidth = 1.2; cx.beginPath(); let started = false;
    for (let i = 0; i < n; i++) { if (s[i] == null) { started = false; continue; } const x = X(i), y = Y(s[i]); if (!started) { cx.moveTo(x, y); started = true; } else cx.lineTo(x, y); }
    cx.stroke(); cx.lineWidth = 1;
  };
  line(d.sma50, '#e3a008'); line(d.sma200, '#2f81f7');
  // 末价标
  const last = d.c[n - 1]; if (last != null) { const y = Y(last); cx.fillStyle = '#e6b800'; cx.fillRect(W - padR, y - 7, padR, 14); cx.fillStyle = '#000'; cx.textAlign = 'left'; cx.fillText(last.toFixed(last >= 100 ? 0 : 1), W - padR + 3, y + 3); }
}
// 今日涨跌 → 颜色(绿涨红跌,±4% 饱和)
function chgColor(p) {
  if (p == null || isNaN(p)) return '#30363d';
  const a = Math.min(1, Math.abs(p) / 4);
  return (p >= 0 ? 'rgba(46,160,67,' : 'rgba(248,81,73,') + (0.22 + 0.6 * a).toFixed(2) + ')';
}
function heatKey(r) {
  return (HEAT_GROUP === 'industry') ? (r.industry || r.sector || '未分类') : (r.sector || '未分类');
}
function renderHeat(rows) {
  document.getElementById('tbl').style.display = 'none';
  document.getElementById('heatWrap').style.display = '';
  const lg = document.getElementById('legend'); if (lg) lg.style.display = 'none';
  document.getElementById('countLine').textContent = rows.length + ' 票 · Heatmap';
  document.getElementById('moreBtn').style.display = 'none';
  const body = document.getElementById('heatBody');
  if (!rows.length) { body.innerHTML = '<div class="muted" style="padding:24px;text-align:center">当前筛选无票</div>'; return; }
  const groups = {};
  rows.forEach(r => { const k = heatKey(r); (groups[k] = groups[k] || []).push(r); });
  const avgOf = g => g.reduce((s, r) => s + (r.chg_pct || 0), 0) / g.length;
  const order = Object.keys(groups).sort((a, b) => avgOf(groups[b]) - avgOf(groups[a]));
  const maxSc = Math.max(1, ...rows.map(r => r.score || 0));
  let h = '';
  order.forEach(k => {
    const g = groups[k].slice().sort((a, b) => (b.score || 0) - (a.score || 0));
    const avg = avgOf(g);
    h += '<div style="margin-bottom:12px">' +
      '<div style="font-size:12px;font-weight:600;color:#c9d1d9;margin-bottom:5px">' + secLabel(k) +
      ' <span class="muted" style="font-weight:400">· ' + g.length + ' 票 · 均 ' + (avg >= 0 ? '+' : '') + avg.toFixed(1) + '%</span></div>' +
      '<div style="display:flex;flex-wrap:wrap;gap:4px">';
    g.forEach(r => {
      const w = 54 + Math.round((r.score || 0) / maxSc * 44);
      const dir = (r.score_detail || {}).dir;
      const cp = r.chg_pct == null ? 0 : r.chg_pct;
      h += '<div onclick="showTaDetail(\'' + r.ticker + '\')" title="' + r.ticker + ' · 分 ' + (r.score || 0).toFixed(0) + ' · 今日 ' + (cp >= 0 ? '+' : '') + cp.toFixed(1) + '%" ' +
        'style="cursor:pointer;width:' + w + 'px;height:46px;border-radius:5px;background:' + chgColor(cp) +
        ';border:1px solid ' + (dir === 'bull' ? '#3fb950' : 'rgba(255,255,255,.12)') + ';display:flex;flex-direction:column;align-items:center;justify-content:center;overflow:hidden">' +
        '<b style="font-size:12px;color:#fff">' + r.ticker + '</b>' +
        '<span style="font-size:10px;color:#e6edf3">' + (cp >= 0 ? '+' : '') + cp.toFixed(1) + '%</span></div>';
    });
    h += '</div></div>';
  });
  body.innerHTML = h;
}

// ── 扫描控制 ──
let pollTimer = null;
async function runScan() {
  try { await fetch('/api/ta/run', {method: 'POST'}); } catch (e) {}
  document.getElementById('progressWrap').style.display = 'flex';
  document.getElementById('runBtn').disabled = true;
  poll();
}
async function poll() {
  if (pollTimer) clearTimeout(pollTimer);
  let st = null;
  try { st = await (await fetch('/api/ta/status')).json(); } catch (e) {}
  if (st) {
    document.getElementById('progressFill').style.width = (st.pct || 0) + '%';
    document.getElementById('progressMsg').textContent = st.msg || '';
    if (st.running) { pollTimer = setTimeout(poll, 1500); return; }
    document.getElementById('runBtn').disabled = false;
    if (!st.error && st.pct >= 100) { document.getElementById('progressWrap').style.display = 'none'; load(); return; }
  }
  document.getElementById('runBtn').disabled = false;
}
// ═══════════════ EOD 期权链爬取状态条 + 「现在盘后爬取」═══════════════
let _eodS = null, _eodCrawlPoll = null;

// EOD 期权链爬取状态条(只读 /api/ta/eod_status;进页面 / 点 ↻ 刷新)
async function loadEodStatus() {
  const el = document.getElementById('eodStatus');
  if (!el) return;
  let s = null;
  try { s = await fetch('/api/ta/eod_status').then(r => r.json()); } catch (e) {}
  _eodS = s;
  if (!s || !s.ok) { el.style.display = 'none'; return; }
  const dot = s.state === 'done' ? 'var(--green)' : (s.state === 'pending' ? 'var(--orange,#d8943a)' : 'var(--text2)');
  const icon = s.state === 'done' ? '🟢' : (s.state === 'pending' ? '🟠' : '⚪');
  const tip = (s.summary || []).join(' · ');
  let btn = '';
  if (s.state === 'pending') btn = '<button class="primary" id="eodCrawlBtn" onclick="runEodCrawl()" style="margin-left:8px;padding:4px 10px;font-size:12px;white-space:nowrap">⚡ 现在盘后爬取</button>';
  else if (s.state === 'done') btn = '<button class="chip" id="eodCrawlBtn" onclick="runEodCrawl()" title="重爬今天的 EOD 期权链" style="margin-left:8px;padding:3px 8px;font-size:11px;white-space:nowrap">↻ 重爬</button>';
  el.innerHTML = '<span style="font-size:13px">' + icon + '</span>' +
    '<b style="color:' + dot + '">EOD 爬取</b>' +
    '<span>' + zt(s.label || '', s.label_en || s.label || '') + '</span>' +
    (tip ? '<span class="muted" title="' + tip.replace(/"/g, '&quot;') + '" style="cursor:help">· ' + tip + '</span>' : '') +
    (s.file_mtime ? '<span class="muted" style="margin-left:auto">数据文件 ' + s.file_mtime + '</span>' : '') +
    '<span onclick="loadEodStatus()" title="刷新状态" style="cursor:pointer;opacity:.6;margin-left:' + (s.file_mtime ? '6px' : 'auto') + '">↻</span>' +
    btn;
  el.style.display = 'flex';
  // 已有爬取在跑(本次点的 / 定时任务起的)→ 接上进度条
  try {
    const cs = await fetch('/api/ta/eod_crawl/status').then(r => r.json());
    if (cs && cs.running) { document.getElementById('eodCrawlProg').style.display = 'flex'; eodCrawlPoll(); }
  } catch (e) {}
}

// ⚡ 现在盘后爬取:起子进程爬今天 EOD 链 + 接 paper;收盘前先弹窗确认
async function runEodCrawl() {
  const s = _eodS || {};
  if (s.before_close) {
    if (!confirm('市场还没收盘(收盘 15:00 德州时间)。现在爬到的是盘中快照,会被当成今天的 EOD 永久写入 —— 之后的自动任务发现今天已有数据就不再爬了。确定现在就爬?')) return;
  }
  const btn = document.getElementById('eodCrawlBtn'); if (btn) btn.disabled = true;
  document.getElementById('eodCrawlProg').style.display = 'flex';
  document.getElementById('eodCrawlFill').style.width = '0%';
  document.getElementById('eodCrawlMsg').textContent = '启动爬取…';
  try { await fetch('/api/ta/eod_crawl/run', { method: 'POST' }); } catch (e) {}
  eodCrawlPoll();
}
async function eodCrawlPoll() {
  if (_eodCrawlPoll) clearTimeout(_eodCrawlPoll);
  let st = null;
  try { st = await fetch('/api/ta/eod_crawl/status').then(r => r.json()); } catch (e) {}
  const fill = document.getElementById('eodCrawlFill'), msg = document.getElementById('eodCrawlMsg');
  if (st && st.running) {
    if (fill) fill.style.width = (st.pct || 0) + '%';
    if (msg) msg.textContent = st.msg || '爬取中…';
    _eodCrawlPoll = setTimeout(eodCrawlPoll, 2000);
    return;
  }
  if (st && st.phase === 'error') {
    if (fill) { fill.style.width = '100%'; fill.style.background = 'var(--red)'; }
    if (msg) msg.textContent = '❌ ' + (st.error || st.msg || '失败');
    const b = document.getElementById('eodCrawlBtn'); if (b) b.disabled = false;
    setTimeout(() => { document.getElementById('eodCrawlProg').style.display = 'none'; if (fill) fill.style.background = 'linear-gradient(90deg,#1f6feb,#3fb950)'; }, 5000);
    return;
  }
  if (fill) fill.style.width = '100%';
  if (msg) msg.textContent = '✅ 完成';
  setTimeout(() => { document.getElementById('eodCrawlProg').style.display = 'none'; }, 2500);
  loadEodStatus();
}

// ═══════════════ 单票详情页(TA + Digest 新闻 + Finviz)═══════════════
// 一键把一个推荐价差载入主页面的 🧪 模拟盘(本页是 /ta/ iframe,模拟盘 PT 在 parent),按实时 mid 模拟下单。
function openInBuilder(ticker, exp, dir, longK, shortK, entryNet) {
  const type = (dir === 'bear') ? 'P' : 'C';
  const legs = [{ type: type, strike: +longK, qty: 1 }, { type: type, strike: +shortK, qty: -1 }];
  const spec = { ticker: ticker, expiration: exp, legs: legs };
  if (entryNet != null && !isNaN(+entryNet)) spec.entryNet = +entryNet;
  try {
    const P = window.parent;
    if (P && P.PT && typeof P.PT.loadSpec === 'function') {
      P.PT.loadSpec(spec);
    } else {
      alert('模拟盘不可用:请在主页面里打开 TA 详情。Paper Trade is only available when the TA page is opened from the main window.');
    }
  } catch (e) { alert('打开模拟盘失败: ' + e); }
}
// 详情页快捷键:B = 把主推荐价差一键载入模拟盘
if (!window._taKeyBound) {
  window._taKeyBound = true;
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'b' && e.key !== 'B') return;
    if (/input|textarea|select/i.test((e.target && e.target.tagName) || '')) return;
    const p = window._tdPrimary;
    if (window._tdSym && p) { e.preventDefault(); openInBuilder(p.ticker, p.exp, p.dir, p.longK, p.shortK); }
  });
}

// 「📄 财报」按钮 → 跳到主 PWA 的 Events → SEC 财报,自动拉这只票(本页常嵌在父 iframe)。
function taOpenSec(sym) {
  sym = sym || window._tdSym;
  if (!sym) return;
  try {
    if (window.parent && window.parent !== window && window.parent.openSecFiling) {
      window.parent.openSecFiling(sym); return;
    }
  } catch (e) {}
  window.top.location.href = '/?sec=' + encodeURIComponent(sym);   // 独立打开 /ta/ 时的兜底
}
window.taOpenSec = taOpenSec;

async function showTaDetail(sym, force, fromPop) {
  sym = (sym || '').toUpperCase();
  if (!sym) return;
  window._tdSym = sym;
  OPEN = null;                                  // 清掉列表展开行,避免 #spark id 重复
  document.getElementById('taScreenerView').style.display = 'none';
  document.getElementById('taDetailView').style.display = '';
  // 详情模式:藏掉 screener 列表页的 chrome(标题/副标题/大盘横幅), 让单票信息占满
  ['taH1', 'taSubtitle', 'mktBanner'].forEach(function (id) { var el = document.getElementById(id); if (el) el.style.display = 'none'; });
  document.getElementById('tdTitle').textContent = ' · ' + sym;
  document.getElementById('tdBody').innerHTML = '<div class="muted" style="padding:40px;text-align:center">加载 ' + sym + ' 的 TA 分析 / Digest 新闻 / Finviz 技术面…</div>';
  // 开详情 push 一条历史 → 鼠标左滑/浏览器返回可回 screener(fromPop 时是 popstate 触发,不再 push)
  try {
    const url = '/ta/?ticker=' + sym;
    if (fromPop) { /* 已由 popstate 改了 URL */ }
    else if (location.search.indexOf('ticker=') >= 0) history.replaceState({ taTk: sym }, '', url);
    else history.pushState({ taTk: sym }, '', url);
  } catch (e) {}
  window.scrollTo(0, 0);
  const [data, fv] = await Promise.all([
    fetch('/api/ta/ticker/' + encodeURIComponent(sym)).then(r => r.json()).catch(e => ({ error: String(e) })),
    fetch('/api/finviz/' + encodeURIComponent(sym)).then(r => r.json()).catch(e => ({}))
  ]);
  if (window._tdSym !== sym) return;            // 期间又切了票
  renderTaDetail(sym, data, fv || {});
}

function hideTaDetail() {
  window._tdSym = null;
  document.getElementById('taDetailView').style.display = 'none';
  document.getElementById('taScreenerView').style.display = '';
  // 恢复 screener chrome
  ['taH1', 'taSubtitle'].forEach(function (id) { var el = document.getElementById(id); if (el) el.style.display = ''; });
  var mb = document.getElementById('mktBanner'); if (mb && mb.innerHTML.trim()) mb.style.display = '';
  try { history.replaceState(null, '', '/ta/'); } catch (e) {}
}

function renderTaDetail(sym, data, fv) {
  const body = document.getElementById('tdBody');
  const r = data && data.row;
  window._tdPrimary = null;
  if (r && r.spread_rec && r.spread_rec.primary) {
    const pr = r.spread_rec.primary;
    window._tdPrimary = { ticker: sym, exp: pr.expiration, dir: r.spread_rec.dir, longK: pr.long_strike, shortK: pr.short_strike };
  }
  let h = tdHeader(sym, data, r);
  if (window._tdPrimary) {
    const p = window._tdPrimary;
    h += '<div style="margin-top:6px"><button onclick="openInBuilder(\'' + p.ticker + '\',\'' + p.exp + '\',\'' + p.dir + '\',' + p.longK + ',' + p.shortK + ')" style="background:#1f6feb;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-weight:700">⚡ 载入模拟盘</button> <span class="muted" style="font-size:11px">主推荐 ' + (p.dir === 'bull' ? '📈BCS' : '📉BPS') + ' ' + p.longK + '/' + p.shortK + ' · exp ' + p.exp + ' · 快捷键 <b>B</b></span></div>';
  }
  h += '<div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start;margin-top:10px">';
  h += '<div style="flex:2;min-width:520px">' + (r ? detailHtml(r)
    : '<div class="detail"><div class="muted" style="padding:6px 4px 10px">该票不在当前 TA 扫描结果(无 S/R 评分);下面是日线图(候选自抓),右侧 Finviz / 新闻。</div><canvas id="tdFbChart" style="width:100%;height:300px;display:block;background:#0b0e14;border-radius:8px"></canvas></div>') + '</div>';
  h += '<div style="flex:1;min-width:330px">' + tdFinvizPanel(fv) + tdNewsPanel((data && data.news) || [], (fv && fv.news) || []) + '</div>';
  h += '</div>';
  body.innerHTML = h;
  if (r) { try { drawSpark(r); } catch (e) {} }
  else { loadDetailFallbackChart(sym); }   // 池外票:用 OHLC fallback(yfinance)画日线图
}

// 池外(不在 TA 池)票的详情图:/api/ta/ohlc 无数据 → /api/barchart/ohlc(yfinance) → 复用 drawCandles
async function loadDetailFallbackChart(sym) {
  const cv = document.getElementById('tdFbChart');
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth || 760, H = 300;
  const setup = () => { cv.width = W * dpr; cv.height = H * dpr; const cx = cv.getContext('2d'); cx.setTransform(dpr, 0, 0, dpr, 0, 0); cx.clearRect(0, 0, W, H); return cx; };
  let cx = setup(); cx.fillStyle = '#6e7681'; cx.font = '13px sans-serif'; cx.fillText('加载 ' + sym + ' 日线图…', 12, 26);
  try {
    let d = await fetch('/api/ta/ohlc/' + encodeURIComponent(sym) + '?days=252').then(r => r.json());
    if (!d || d.error || !d.c || !d.c.length) d = await fetch('/api/barchart/ohlc/' + encodeURIComponent(sym) + '?days=252').then(r => r.json());
    const pe = document.getElementById('tdFbPrice');
    cx = setup();
    if (!d || !d.c || !d.c.length) { cx.fillStyle = '#6e7681'; cx.font = '13px sans-serif'; cx.fillText('无日线数据', 12, 26); if (pe) pe.innerHTML = '<span class="muted">无价格</span>'; return; }
    drawCandles(cx, d, W, H);
    // 头部当前价(池外票):用最新收盘 + 较前日变化
    if (pe) {
      const n = d.c.length, last = d.c[n - 1], prev = n > 1 ? d.c[n - 2] : last;
      const ch = prev ? (last / prev - 1) * 100 : 0, col = ch > 0 ? 'var(--green)' : ch < 0 ? 'var(--red)' : 'var(--text2)';
      pe.innerHTML = '$' + last + ' <span style="color:' + col + '">' + (ch >= 0 ? '+' : '') + ch.toFixed(2) + '%</span> <span class="muted" style="font-size:11px">最新收盘</span>';
    }
  } catch (e) { cx = setup(); cx.fillStyle = '#f85149'; cx.font = '13px sans-serif'; cx.fillText('图加载失败', 12, 26); }
}

function tdHeader(sym, data, r) {
  const dir = r && (r.score_detail || {}).dir;
  const dirBadge = dir === 'bull' ? '<span style="color:var(--green);font-weight:700">▲ 看多</span>'
    : dir === 'bear' ? '<span style="color:var(--red);font-weight:700">▼ 看空</span>' : '<span class="muted">无方向</span>';
  const chg = r ? r.chg_pct : null;
  const chgStr = chg == null ? '' : '<span style="color:' + (chg > 0 ? 'var(--green)' : chg < 0 ? 'var(--red)' : 'var(--text2)') + '">' + (chg > 0 ? '+' : '') + chg + '%</span>';
  return '<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:8px 14px">' +
    '<div style="font-size:20px;font-weight:800">' + sym + '</div>' +
    (r ? '<div style="font-size:15px;font-weight:600">$' + r.close + ' ' + chgStr + '</div>'
       : '<div style="font-size:15px" id="tdFbPrice"><span class="muted">价格加载中…</span></div>') +
    (r && r.sector ? '<div class="muted" style="font-size:12px">' + r.sector + (r.category ? ' · ' + r.category : '') + '</div>' : '') +
    (r ? '<div>' + earnCell(r) + '</div>' : '') +
    (r ? '<div>' + touchBadge(r) + '</div>' : '') +
    (data && data.generated_at ? '<div class="muted" style="font-size:10px;margin-left:auto">TA ' + data.generated_at + '</div>' : '') +
    '</div>';
}

function _tdPct(v) {
  if (v == null) return '—';
  const s = String(v), num = parseFloat(s.replace(/[^0-9.\-]/g, ''));
  const col = isNaN(num) ? 'var(--text)' : (num > 0 ? 'var(--green)' : num < 0 ? 'var(--red)' : 'var(--text2)');
  return '<span style="color:' + col + '">' + s + '</span>';
}

function tdFinvizPanel(fv) {
  const f = (fv && fv.fundamentals) || {};
  if (!Object.keys(f).length) return '<div class="modcard"><h4>📊 Finviz</h4><div class="muted" style="font-size:12px">Finviz 暂无数据(可能限流/标的不支持)。点右上 ↻ 重试。</div></div>';
  const cell = (label, key, pct) => f[key] != null ? '<tr><td class="muted">' + label + '</td><td class="num">' + (pct ? _tdPct(f[key]) : f[key]) + '</td></tr>' : '';
  const tech = '<table class="ztab" style="width:100%">' + cell('RSI(14)', 'RSI (14)') + cell('ATR(14)', 'ATR (14)') +
    cell('SMA20', 'SMA20', 1) + cell('SMA50', 'SMA50', 1) + cell('SMA200', 'SMA200', 1) + cell('波动率', 'Volatility') + '</table>';
  const perf = '<table class="ztab" style="width:100%">' + cell('周', 'Perf Week', 1) + cell('月', 'Perf Month', 1) +
    cell('季', 'Perf Quarter', 1) + cell('半年', 'Perf Half Y', 1) + cell('年', 'Perf Year', 1) + cell('YTD', 'Perf YTD', 1) + '</table>';
  const fund = '<table class="ztab" style="width:100%">' + cell('P/E', 'P/E') + cell('Fwd P/E', 'Forward P/E') + cell('PEG', 'PEG') +
    cell('市值', 'Market Cap') + cell('P/S', 'P/S') + cell('P/B', 'P/B') + cell('Beta', 'Beta') + cell('股息', 'Dividend %') + '</table>';
  const an = (fv.analysts || []).slice(0, 5).map(a => '<tr><td class="muted">' + (a.date || '') + '</td><td>' + (a.category || '') +
    '</td><td>' + (a.analyst || '') + '</td><td>' + (a.rating || '') + '</td><td class="num">' + (a.target_to || a.target || '') + '</td></tr>').join('');
  return '<div class="modcard"><h4>📊 Finviz 技术 / 表现 / 基本面 ' + hIcon('finviz') + '</h4>' +
    '<div style="display:flex;gap:10px;flex-wrap:wrap">' +
    '<div style="flex:1;min-width:120px"><div class="muted" style="font-size:11px;margin-bottom:2px">技术(SMA=距均线%)</div>' + tech + '</div>' +
    '<div style="flex:1;min-width:110px"><div class="muted" style="font-size:11px;margin-bottom:2px">表现</div>' + perf + '</div>' +
    '<div style="flex:1;min-width:120px"><div class="muted" style="font-size:11px;margin-bottom:2px">基本面</div>' + fund + '</div>' +
    '</div>' +
    (an ? '<div style="margin-top:8px"><div class="muted" style="font-size:11px">分析师评级(近5)</div><table class="ztab" style="width:100%"><tr><th>日期</th><th>动作</th><th>机构</th><th>评级</th><th class="num">目标</th></tr>' + an + '</table></div>' : '') +
    (fv.description ? '<details style="margin-top:8px"><summary class="muted" style="cursor:pointer;font-size:11px">公司简介</summary><div style="font-size:11px;line-height:1.6;margin-top:4px">' + fv.description.slice(0, 700) + '</div></details>' : '') +
    '</div>';
}

function _tdSent(s) { return s > 0 ? '<span style="color:var(--green)" title="偏多">▲</span>' : s < 0 ? '<span style="color:var(--red)" title="偏空">▼</span>' : '<span class="muted">•</span>'; }

// 详情页新闻里的关联标的 → 点击切到该票详情(本页 iframe 内, 用 showTaDetail)。
function _tdNewsTk(sym) {
  const raw = String(sym == null ? '' : sym).trim();
  const clean = raw.toUpperCase().replace(/^\$/, '').replace(/[^A-Z0-9.\-]/g, '');
  const txt = raw.replace(/[<>&]/g, '');
  if (!clean) return txt;
  return '<span onclick="showTaDetail(\'' + clean + '\')" style="cursor:pointer;color:#58a6ff" title="切到 ' + clean + ' 详情">' + txt + '</span>';
}

function tdNewsPanel(news, fvNews) {
  let h = '<div class="modcard"><h4>📰 Digest 新闻(' + news.length + ' · 近6天)</h4><div style="max-height:520px;overflow-y:auto">';
  if (!news.length) h += '<div class="muted" style="font-size:12px">近 6 天 Digest 无该票新闻</div>';
  else h += news.map(n => '<div style="border-top:1px solid var(--border);padding:6px 0;font-size:12px">' +
    '<div>' + _tdSent(n.sentiment) + ' <span class="muted">' + n.date + ' ' + (n.hhmm || '') + '</span> ' +
    (n.link ? '<a href="' + n.link + '" target="_blank" rel="noopener" style="color:var(--text);font-weight:600">' + (n.title || '') + '</a>' : '<b>' + (n.title || '') + '</b>') + '</div>' +
    (n.title_en && n.title_en !== n.title ? '<div class="muted" style="font-size:10px">' + n.title_en + '</div>' : '') +
    ((n.tickers && n.tickers.length > 1) ? '<div class="muted" style="font-size:10px">相关:' + n.tickers.slice(0, 6).map(_tdNewsTk).join(' · ') + '</div>' : '') +
    '</div>').join('');
  if (fvNews && fvNews.length) {
    h += '<h4 style="margin-top:10px">📰 Finviz 新闻</h4>' + fvNews.slice(0, 12).map(n =>
      '<div style="border-top:1px solid var(--border);padding:5px 0;font-size:12px"><span class="muted">' + (n.date || '') + '</span> ' +
      '<a href="' + (n.url || '#') + '" target="_blank" rel="noopener" style="color:var(--text)">' + (n.title || '') + '</a> ' +
      '<span class="muted" style="font-size:10px">' + (n.source || '') + '</span></div>').join('');
  }
  return h + '</div></div>';
}

load();
loadEodStatus();
// 鼠标左滑/浏览器返回 → popstate → 收起详情回 screener(或前进则重开对应票)
window.addEventListener('popstate', function (e) {
  const tk = e.state && e.state.taTk;
  if (tk) { showTaDetail(tk, false, true); }
  else if (document.getElementById('taDetailView') && document.getElementById('taDetailView').style.display !== 'none') { hideTaDetail(); }
});
// 进入时若带 ?ticker= → 直接打开单票详情页(heatmap 跳转 / 刷新 / 分享 URL)
(function () { try { const qt = new URLSearchParams(location.search).get('ticker'); if (qt) { history.replaceState({}, '', '/ta/'); showTaDetail(qt); } } catch (e) {} })();
// 进来时如果有扫描在跑,接上进度条
fetch('/api/ta/status').then(r => r.json()).then(st => {
  if (st && st.running) { document.getElementById('progressWrap').style.display = 'flex'; document.getElementById('runBtn').disabled = true; poll(); }
}).catch(() => {});

// 后台自动扫(交易日 9:40/10:30/12:00/15:00)产出新数据 → 自动重载,无需手动刷新。
// 用 /api/ta/status 的 generated_at(轻量)判断,变了才重拉列表。保留筛选/排序/展开。
async function maybeRefresh() {
  try {
    const st = await fetch('/api/ta/status').then(r => r.json());
    if (st && !st.running && st.generated_at && _lastGen && st.generated_at !== _lastGen) {
      await load();
      const m = document.getElementById('metaLine');
      if (m) m.textContent = '🔄 已自动更新 · ' + m.textContent;
    }
  } catch (e) {}
}
setInterval(maybeRefresh, 45000);
document.addEventListener('visibilitychange', function () { if (!document.hidden) maybeRefresh(); });
// 给 ta_filters.js 的 API:操作 TA 条件 Set / 设 Barchart 交集 / 触发同一套渲染
window.taFilterApi = {
  render: function () { try { render(); } catch (e) {} },
  filters: FILTERS,                       // TA 条件 Set(多选 AND, 与原 pill 同逻辑)
  rows: function () { return ROWS; },
  setBarchart: function (tk) { _bcTickers = (tk && tk.length) ? tk : null; },
  syncChips: function () { try { syncChips(); } catch (e) {} },
};
</script>
<script src="/static/ta_filters.js" defer></script>
</body>
</html>
"""
