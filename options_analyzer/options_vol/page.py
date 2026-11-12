"""options_vol 前端页(iframe 自包含;不依赖 PWA 主页面任何 JS)。

`render_page() -> str` 返回完整 HTML 字符串(由 api.py 的 GET /optvol/ 包成 HTMLResponse)。
页面自己 fetch /api/optvol/{summary,status,history/{T}};三张趋势图为手写 inline SVG,
**绝不依赖任何 CDN/外部 JS**(preview 沙箱无外网,d3/chart.js 加载不出)。

防自动撑高:iframe 内单页、单个 flex:1 滚动容器,绝不在外层叠同级块
(journal iframe 撑高/滚动环坑,见 gotcha_journal_iframe_autosize_scroll_loop)。

注意:JS 内嵌在 Python 字符串里 —— 用普通三引号(非 r-string)以便保留 SVG 里
的 \n;事件一律用 data-* 属性 + addEventListener 委托,杜绝 onclick="...\'...\'"
被 Python 转义吃掉的坑(见 gotcha_js_in_python_string_escaping)。

UI(2026-06):一张合并表(tier∈{gold,silver} 且 is_adr==false;排除 aside/nodata/ADR),
每行带 🥇/🥈 徽章;点行 → 紧贴该行下方插入一行手风琴展开(三张图 + 1M/3M 切换),
再点收起,点别行先收起旧的(见作者需求「options 一个 table 就行,点 tickers 图就在
tickers 下面展开」)。
"""


def render_page() -> str:
    """返回 /optvol/ 的完整页面字符串。"""
    return _PAGE_HTML


_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options 量稳定性</title>
<style>
:root { --bg:#0d1117; --bg2:#161b22; --bg3:#21262d; --text:#e6edf3; --text2:#8b949e;
        --border:#30363d; --red:#f85149; --green:#3fb950; --blue:#58a6ff;
        --gold:#e3b341; --silver:#a8b3c2; --orange:#d8943a; }
* { box-sizing:border-box; }
html { overscroll-behavior-x:none; }
body { margin:0; height:100vh; display:flex; flex-direction:column; overflow:hidden;
       background:var(--bg); color:var(--text); overscroll-behavior-x:none;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:13px; }
/* 单一滚动容器(flex:1),绝不在它外面叠同级块 → 不触发 iframe 自动撑高环 */
#app { flex:1; min-height:0; overflow-y:auto; overflow-x:hidden; padding:12px 16px 48px; }
.inner { max-width:1500px; margin:0 auto; }
h1 { font-size:16px; margin:0 0 2px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.muted { color:var(--text2); font-size:11px; font-weight:400; }
.subhead { color:var(--text2); font-size:11px; margin:0 0 8px; }
button { background:var(--bg3); color:var(--text); border:1px solid var(--border);
         border-radius:6px; padding:5px 12px; font-size:12px; cursor:pointer; }
button:hover { border-color:var(--blue); }
button.primary { background:#238636; border-color:#238636; color:#fff; font-weight:600; }
button.chip { padding:3px 10px; border-radius:12px; font-size:11px; color:var(--text2); }
button.chip.on { background:#1f6feb; border-color:#1f6feb; color:#fff; }
/* 状态条 */
#statusBar { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
             background:var(--bg2); border:1px solid var(--border); border-radius:8px;
             padding:8px 12px; margin:8px 0 4px; font-size:12px; }
#statusBar .pill { background:var(--bg3); border:1px solid var(--border); border-radius:10px;
                   padding:2px 9px; font-size:11px; color:var(--text2); }
#statusBar .pill b { color:var(--text); }
#scrapeNote { color:var(--text2); font-size:10.5px; }
/* 行内展开:三张图 ─────────────────────────────────────────── */
.rangeToggle { display:inline-flex; gap:4px; }
.expandInner { padding:4px 2px 8px; }
.expandHead { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:0 0 8px; }
.expandHead .sym { font-size:15px; font-weight:700; }
.charts { display:flex; gap:14px; flex-wrap:wrap; }
.chartbox { flex:1; min-width:280px; background:#0a0d12; border:1px solid var(--border);
            border-radius:8px; padding:8px 10px; }
.chartbox .ctitle { font-size:11px; color:var(--text2); margin-bottom:2px;
                    display:flex; justify-content:space-between; align-items:baseline; }
.chartbox .clast { font-size:12px; font-weight:700; color:var(--text); }
.chartbox svg { width:100%; height:130px; display:block; }
/* 表 ─────────────────────────────────────────────────────── */
.sect { margin-top:14px; }
table { width:100%; border-collapse:collapse; margin-top:4px; }
th { text-align:right; font-size:11px; color:var(--text2); padding:6px 8px;
     border-bottom:1px solid var(--border); cursor:pointer; user-select:none;
     white-space:nowrap; position:sticky; top:0; background:var(--bg); z-index:2; }
th:first-child, td:first-child { text-align:left; }
th:hover { color:var(--blue); }
th .arr { font-size:9px; margin-left:2px; }
td { padding:6px 8px; border-bottom:1px solid #1c2128; white-space:nowrap;
     vertical-align:middle; font-variant-numeric:tabular-nums; text-align:right; }
tr.row { cursor:pointer; }
tr.row:hover { background:#161b2280; }
tr.row.sel { background:#1f6feb22; }
tr.row.sel td { border-bottom-color:transparent; }
/* 展开行:整宽单元格,无 hover 高亮、不可点收起(点行头那一行才收) */
tr.expand > td { padding:0 8px; background:#0d1117; border-bottom:1px solid var(--border); }
.sym { font-weight:700; color:var(--blue); }
.badge { display:inline-block; padding:1px 7px; border-radius:9px; font-size:10px; font-weight:700; }
.b-gold { background:rgba(227,179,65,.16); color:var(--gold); border:1px solid rgba(227,179,65,.45); }
.b-silver { background:rgba(168,179,194,.14); color:var(--silver); border:1px solid rgba(168,179,194,.4); }
.symcell { display:inline-flex; align-items:center; gap:7px; }
.symcell .chev { color:var(--text2); font-size:9px; transition:transform .15s; display:inline-block; }
tr.row.sel .symcell .chev { transform:rotate(90deg); color:var(--blue); }
.pct-ok { color:var(--green); }
.pct-mid { color:var(--gold); }
.pct-lo { color:var(--text2); }
/* 空态 */
#empty { display:none; text-align:center; padding:64px 20px; color:var(--text2); }
#empty .big { font-size:15px; color:var(--text); margin-bottom:6px; }
#loading { text-align:center; padding:40px; color:var(--text2); }
.err { color:var(--red); }
</style>
</head>
<body>
<div id="app">
 <div class="inner">
  <h1>📊 Options 量稳定性 <span class="muted" id="genLine">加载中…</span></h1>
  <div class="subhead">合并 universe 每日期权成交量 · ≥90% 交易日达标(最近 ~1 个月)分档 ·
    🥇每天≥5000 / 🥈每天≥2000(已排除 ADR)· 点 Symbol 在该行下方展开 vol/OI/IV 的 1M/3M 趋势</div>

  <div id="statusBar">
    <button class="primary" id="rescrapeBtn">⚡ 现在重爬</button>
    <span class="pill" id="pillTime">最近爬取 —</span>
    <span class="pill" id="pillGold">🥇 —</span>
    <span class="pill" id="pillSilver">🥈 —</span>
    <span id="scrapeNote">重爬约需几分钟(后台跑,完成后刷新页面查看)</span>
  </div>

  <div id="loading">加载中…</div>

  <div id="empty">
    <div class="big">尚未爬取</div>
    <div>点击 <b>⚡ 现在重爬</b> 拉取 universe 的 options 历史量(约几分钟)。</div>
  </div>

  <div class="sect" id="tableSect">
    <table id="mainTable"><thead></thead><tbody></tbody></table>
  </div>
 </div>
</div>

<script>
(function(){
"use strict";

// ── 数值人性化:881927 → "882K" / 1.2M;小数用于 IV ──
function humanInt(v){
  if (v === null || v === undefined || isNaN(v)) return "—";
  v = Number(v);
  var a = Math.abs(v);
  if (a >= 1e6) return (v/1e6).toFixed(a >= 1e7 ? 1 : 2).replace(/\\.0+$/,'') + "M";
  if (a >= 1e3) return Math.round(v/1e3) + "K";
  return String(Math.round(v));
}
function fmtIv(v){
  if (v === null || v === undefined || isNaN(v)) return "—";
  return (Number(v)*100).toFixed(1) + "%";
}
function fmtPct(v){
  if (v === null || v === undefined || isNaN(v)) return "—";
  return Math.round(Number(v)*100) + "%";
}
function fmtNum2(v){
  if (v === null || v === undefined || isNaN(v)) return "—";
  return Number(v).toFixed(2);
}
function esc(s){ return String(s == null ? "" : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── 手写 SVG 折线(无任何外部库)──────────────────────────────────
// drawLine(svgEl, points, {color, yfmt}):points = 数值数组(可含 null,跳过)。
// 画折线 + 末值点 + y 轴 min/max 标签(左上/左下)。viewBox 固定 400x130。
var SVGNS = "http://www.w3.org/2000/svg";
function el(tag, attrs){
  var e = document.createElementNS(SVGNS, tag);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
function drawLine(svg, points, opts){
  opts = opts || {};
  var color = opts.color || "#58a6ff";
  var yfmt = opts.yfmt || function(v){ return humanInt(v); };
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 400, H = 130, padL = 4, padR = 40, padT = 10, padB = 16;
  var vals = points.filter(function(v){ return v !== null && v !== undefined && !isNaN(v); });
  if (!vals.length){
    svg.appendChild(el("text", {x:W/2, y:H/2, fill:"#8b949e", "font-size":"11",
      "text-anchor":"middle"})).textContent = "无数据";
    return;
  }
  var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
  if (mn === mx){ mn = mn - 1; mx = mx + 1; }   // 平线也给点高度
  var n = points.length;
  var plotW = W - padL - padR, plotH = H - padT - padB;
  function X(i){ return padL + (n <= 1 ? 0 : (i/(n-1))*plotW); }
  function Y(v){ return padT + (1 - (v - mn)/(mx - mn))*plotH; }

  // 横向基线(底)
  svg.appendChild(el("line", {x1:padL, y1:padT+plotH, x2:padL+plotW, y2:padT+plotH,
    stroke:"#21262d", "stroke-width":"1"}));

  // 折线(遇 null 断开 → 多段 polyline)
  var seg = [], lastPt = null, lastVal = null;
  function flush(){
    if (seg.length >= 2){
      svg.appendChild(el("polyline", {points:seg.join(" "), fill:"none",
        stroke:color, "stroke-width":"1.6", "stroke-linejoin":"round"}));
    }
    seg = [];
  }
  for (var i=0;i<n;i++){
    var v = points[i];
    if (v === null || v === undefined || isNaN(v)){ flush(); continue; }
    var px = X(i).toFixed(1), py = Y(v).toFixed(1);
    seg.push(px + "," + py);
    lastPt = [X(i), Y(v)]; lastVal = v;
  }
  flush();

  // 末值点 + 横虚线指引
  if (lastPt){
    svg.appendChild(el("line", {x1:padL, y1:lastPt[1].toFixed(1), x2:lastPt[0].toFixed(1),
      y2:lastPt[1].toFixed(1), stroke:color, "stroke-width":"0.6",
      "stroke-dasharray":"2 3", opacity:"0.5"}));
    svg.appendChild(el("circle", {cx:lastPt[0].toFixed(1), cy:lastPt[1].toFixed(1),
      r:"2.6", fill:color}));
    var lt = el("text", {x:(lastPt[0]+4).toFixed(1), y:(lastPt[1]+3).toFixed(1),
      fill:color, "font-size":"10", "text-anchor":"start"});
    lt.textContent = yfmt(lastVal);
    svg.appendChild(lt);
  }
  // y 轴 max(左上)/ min(左下)标签
  var tmax = el("text", {x:padL, y:padT+3, fill:"#8b949e", "font-size":"9", "text-anchor":"start"});
  tmax.textContent = yfmt(mx);
  svg.appendChild(tmax);
  var tmin = el("text", {x:padL, y:padT+plotH, fill:"#8b949e", "font-size":"9", "text-anchor":"start"});
  tmin.textContent = yfmt(mn);
  svg.appendChild(tmin);
}

// ── 状态 ────────────────────────────────────────────────────────
// rows = 合并表实际显示的行(gold/silver 且 非 ADR);byTicker 便于查 meta。
// expand = 当前展开的那一行 {ticker, range, series}(null=全收起)。
var STATE = { rows: [], byTicker: {},
              sort: { key:"median_vol", dir:-1 },
              expand: { ticker:null, range:"1M", series:null } };

// 表列定义:[key, 列名, 渲染函数]。第一列 Symbol 特殊处理(带徽章+折叠箭头)。
var COLS = [
  ["ticker",     "Symbol",  function(r){
      var b = r.tier === "gold" ? '<span class="badge b-gold">🥇</span>'
            : r.tier === "silver" ? '<span class="badge b-silver">🥈</span>' : '';
      return '<span class="symcell"><span class="chev">▶</span>'
           + '<span class="sym">'+esc(r.ticker)+'</span>'+b+'</span>'; }],
  ["latest_vol", "最新量",   function(r){ return humanInt(r.latest_vol); }],
  ["median_vol", "中位(1M)", function(r){ return humanInt(r.median_vol); }],
  ["pct_days_hi","%日≥5k",   function(r){ return '<span class="'+pctCls(r.pct_days_hi)+'">'+fmtPct(r.pct_days_hi)+'</span>'; }],
  ["pct_days_lo","%日≥2k",   function(r){ return '<span class="'+pctCls(r.pct_days_lo)+'">'+fmtPct(r.pct_days_lo)+'</span>'; }],
  ["latest_oi",  "OI",      function(r){ return humanInt(r.latest_oi); }],
  ["latest_iv",  "IV",      function(r){ return fmtIv(r.latest_iv); }],
  ["iv_rank",    "IVrank",  function(r){ return (r.iv_rank==null||isNaN(r.iv_rank))?"—":Math.round(r.iv_rank); }],
  ["latest_pc_vol","P/C",   function(r){ return fmtNum2(r.latest_pc_vol); }]
];
function pctCls(v){ if (v==null) return "pct-lo"; if (v>=0.9) return "pct-ok"; if (v>=0.5) return "pct-mid"; return "pct-lo"; }

// ── 渲染合并表 ─────────────────────────────────────────────────
// 排序后逐行输出;若某行正展开,紧随其后插入一行 <tr class="expand">(整宽单元格)。
function sortedRows(){
  var st = STATE.sort;
  var rows = STATE.rows.slice();
  rows.sort(function(a,b){
    var x = a[st.key], y = b[st.key];
    if (st.key === "ticker"){ x = String(x); y = String(y);
      return (x<y?-1:x>y?1:0) * st.dir; }
    x = (x==null||isNaN(x)) ? -Infinity : Number(x);
    y = (y==null||isNaN(y)) ? -Infinity : Number(y);
    return (x<y?-1:x>y?1:0) * st.dir;
  });
  return rows;
}
function renderTable(){
  var table = document.getElementById("mainTable");
  var thead = table.querySelector("thead");
  var tbody = table.querySelector("tbody");
  var st = STATE.sort;
  // 表头(点击排序;用 data-key,非 inline onclick)
  var htr = "<tr>";
  COLS.forEach(function(c){
    var arr = (st.key === c[0]) ? (st.dir < 0 ? " ▼" : " ▲") : "";
    htr += '<th data-key="'+c[0]+'">'+esc(c[1])+'<span class="arr">'+arr+'</span></th>';
  });
  htr += "</tr>";
  thead.innerHTML = htr;

  var rows = sortedRows();
  var html = "";
  var openTk = STATE.expand.ticker;
  rows.forEach(function(r){
    var sel = (openTk === r.ticker) ? " sel" : "";
    html += '<tr class="row'+sel+'" data-ticker="'+esc(r.ticker)+'">';
    COLS.forEach(function(c){ html += "<td>"+c[2](r)+"</td>"; });
    html += "</tr>";
    if (openTk === r.ticker){
      html += expandRowHtml(r);   // 紧贴该行下方插入展开行
    }
  });
  tbody.innerHTML = html;
  // DOM 插好后再渲染展开图(此时 svg 已在文档里)
  if (openTk) renderExpandCharts();
}

// 展开行的整宽单元格:头部(symbol+1M/3M 切换)+ 三张 SVG。
function expandRowHtml(r){
  var meta = "中位(1M) " + humanInt(r.median_vol) + " · 最新 " + humanInt(r.latest_vol)
           + " · IV " + fmtIv(r.latest_iv) + " · " + (r.n_days_total||"?") + " 天历史";
  var tierTxt = r.tier === "gold" ? "🥇 Gold" : r.tier === "silver" ? "🥈 Silver" : "";
  return '<tr class="expand" data-expand="'+esc(r.ticker)+'"><td colspan="'+COLS.length+'">'
       + '<div class="expandInner">'
       + '<div class="expandHead">'
       +   '<span class="sym">'+esc(r.ticker)+'</span>'
       +   '<span class="muted">'+esc(tierTxt)+' · '+esc(meta)+'</span>'
       +   '<span class="rangeToggle" data-roletoggle="1">'
       +     '<button class="chip'+(STATE.expand.range==="1M"?" on":"")+'" data-range="1M">1M</button>'
       +     '<button class="chip'+(STATE.expand.range==="3M"?" on":"")+'" data-range="3M">3M</button>'
       +   '</span>'
       + '</div>'
       + '<div class="charts">'
       +   '<div class="chartbox"><div class="ctitle"><span>Options Vol</span>'
       +     '<span class="clast" id="lastVol">…</span></div>'
       +     '<svg id="svgVol" viewBox="0 0 400 130" preserveAspectRatio="none"></svg></div>'
       +   '<div class="chartbox"><div class="ctitle"><span>Total OI</span>'
       +     '<span class="clast" id="lastOi">…</span></div>'
       +     '<svg id="svgOi" viewBox="0 0 400 130" preserveAspectRatio="none"></svg></div>'
       +   '<div class="chartbox"><div class="ctitle"><span>IV</span>'
       +     '<span class="clast" id="lastIv">…</span></div>'
       +     '<svg id="svgIv" viewBox="0 0 400 130" preserveAspectRatio="none"></svg></div>'
       + '</div></div></td></tr>';
}

// ── 展开图渲染(读 STATE.expand.series;series 未到 → 占位"…")──
function rangeSlice(series, range){
  if (!series) return [];
  if (range === "1M") return series.slice(-21);   // 尾 21 点 ≈ 1 个月
  return series;                                   // 3M = 全 65
}
function renderExpandCharts(){
  var svgVol = document.getElementById("svgVol");
  var svgOi  = document.getElementById("svgOi");
  var svgIv  = document.getElementById("svgIv");
  if (!svgVol || !svgOi || !svgIv) return;   // 展开行不在 DOM(已收起)
  var d = STATE.expand;
  if (d.series == null){    // 还在 fetch:占位空图
    drawLine(svgVol, [], {}); drawLine(svgOi, [], {}); drawLine(svgIv, [], {});
    return;
  }
  var pts = rangeSlice(d.series, d.range);
  var vol = pts.map(function(p){ return p.vol; });
  var oi  = pts.map(function(p){ return p.oi; });
  var iv  = pts.map(function(p){ return (p.iv==null?null:p.iv); });
  drawLine(svgVol, vol, {color:"#58a6ff", yfmt:humanInt});
  drawLine(svgOi,  oi,  {color:"#3fb950", yfmt:humanInt});
  drawLine(svgIv,  iv,  {color:"#e3b341", yfmt:function(v){return fmtIv(v);}});
  function lastOf(arr, fmt){ for (var i=arr.length-1;i>=0;i--){
    if (arr[i]!=null && !isNaN(arr[i])) return fmt(arr[i]); } return "—"; }
  document.getElementById("lastVol").textContent = lastOf(vol, humanInt);
  document.getElementById("lastOi").textContent  = lastOf(oi, humanInt);
  document.getElementById("lastIv").textContent  = lastOf(iv, fmtIv);
}

// 展开某行(累加渲染整表,把展开行插到对应行下);拉历史填图。
function expandTicker(ticker){
  STATE.expand.ticker = ticker;
  STATE.expand.series = null;   // 标记"加载中"
  renderTable();                // 重渲染:插入展开行(图先占位)
  fetch("/api/optvol/history/" + encodeURIComponent(ticker))
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (STATE.expand.ticker !== ticker) return;  // 已切走/收起
      STATE.expand.series = (j && j.series) || [];
      renderExpandCharts();
    })
    .catch(function(){
      if (STATE.expand.ticker !== ticker) return;
      STATE.expand.series = [];
      renderExpandCharts();
    });
}
function collapse(){
  STATE.expand.ticker = null;
  STATE.expand.series = null;
  renderTable();
}

// ── 状态条 ──
function applyStatus(s){
  var t = s && s.generated_at;
  document.getElementById("pillTime").innerHTML = "最近爬取 <b>" + (t ? esc(t.replace("T"," ")) : "—") + "</b>";
  document.getElementById("genLine").textContent =
    t ? ("· 数据 " + t.replace("T"," ")) : "· 尚未爬取";
}

// ── 主加载 ──
function load(){
  // 状态条(独立拉,失败不致命)
  fetch("/api/optvol/status").then(function(r){ return r.json(); })
    .then(applyStatus).catch(function(){});

  fetch("/api/optvol/summary").then(function(r){ return r.json(); })
    .then(function(j){
      document.getElementById("loading").style.display = "none";
      var tickers = (j && j.tickers) || [];
      // 合并表只收 gold/silver 且 非 ADR;aside/nodata/ADR 全部剔除
      var rows = tickers.filter(function(r){
        return (r.tier === "gold" || r.tier === "silver") && r.is_adr !== true;
      });
      STATE.rows = rows;
      STATE.byTicker = {};
      var nGold = 0, nSilver = 0;
      rows.forEach(function(r){
        STATE.byTicker[r.ticker] = r;
        if (r.tier === "gold") nGold++; else if (r.tier === "silver") nSilver++;
      });
      if (j && j.generated_at) applyStatus(j);
      // 计数 pill(反映合并后的实际行)
      document.getElementById("pillGold").textContent   = "🥇 " + nGold;
      document.getElementById("pillSilver").textContent = "🥈 " + nSilver;

      if (!rows.length){
        document.getElementById("empty").style.display = "block";
        document.getElementById("tableSect").style.display = "none";
        return;
      }
      document.getElementById("empty").style.display = "none";
      document.getElementById("tableSect").style.display = "block";
      renderTable();
    })
    .catch(function(e){
      document.getElementById("loading").innerHTML =
        '<span class="err">加载失败:' + esc(e && e.message) + '</span>';
    });
}

// ── 事件委托(全部 addEventListener,无 inline onclick)──────────
document.getElementById("app").addEventListener("click", function(ev){
  var t = ev.target;
  // 1M / 3M 切换(在展开行内;先拦,避免被当成点行)
  var rb = t.closest && t.closest("button[data-range]");
  if (rb){
    var rg = rb.getAttribute("data-range");
    if (rg !== STATE.expand.range){
      STATE.expand.range = rg;
      // 仅更新该展开行内按钮高亮 + 重画图(不重渲染整表,避免闪)
      var tg = rb.closest("[data-roletoggle]");
      if (tg) Array.prototype.forEach.call(tg.querySelectorAll("button"), function(x){
        x.classList.toggle("on", x === rb);
      });
      renderExpandCharts();
    }
    return;
  }
  // 展开行内部(非按钮)点击 → 不收起、不冒泡成切行
  if (t.closest && t.closest("tr.expand")) return;
  // 表头排序
  var th = t.closest && t.closest("th[data-key]");
  if (th){
    var key = th.getAttribute("data-key");
    var st = STATE.sort;
    if (st.key === key) st.dir = -st.dir; else { st.key = key;
      st.dir = (key === "ticker") ? 1 : -1; }  // 文本默认升,数字默认降
    renderTable();
    return;
  }
  // 行 → 手风琴切换:点已开的收起,点别的先收旧再开新
  var row = t.closest && t.closest("tr.row[data-ticker]");
  if (row){
    var tk = row.getAttribute("data-ticker");
    if (!tk) return;
    if (STATE.expand.ticker === tk) collapse();
    else expandTicker(tk);
    return;
  }
});

// ⚡ 现在重爬
document.getElementById("rescrapeBtn").addEventListener("click", function(){
  var btn = this; btn.disabled = true; var old = btn.textContent; btn.textContent = "已触发…";
  fetch("/api/optvol/scrape", {method:"POST"}).then(function(r){ return r.json(); })
    .then(function(j){
      btn.textContent = (j && j.started) ? "🟢 重爬中(约几分钟)" : "需手动跑";
      document.getElementById("scrapeNote").textContent = (j && j.started)
        ? "已在后台开始爬取,完成后刷新页面查看新数据。"
        : "未配置 run_optvol.sh,请手动跑 python -m options_vol.scrape_history && build_summary。";
    })
    .catch(function(){ btn.textContent = "触发失败"; })
    .finally(function(){ setTimeout(function(){ btn.disabled=false; btn.textContent=old; }, 8000); });
});

load();
})();
</script>
</body>
</html>
"""
