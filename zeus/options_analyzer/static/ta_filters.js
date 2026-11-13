// ta_filters.js — TA 筛选栏:两个多选下拉(Barchart Screener / TA 条件)+ 一键clear + 设为默认 + 命名预设。
//   驱动 page.py 的 window.taFilterApi(同一套 render/表格/图/热力)。不选 Barchart = 纯 TA 筛选。
(function () {
  var BC = [];                  // saved screeners [{key,name,tickers,count,asof}]
  var bcSel = new Set();        // 选中的 screener key
  var bcMode = "union";         // 'union' (合集/并集) | 'inter' (交集)
  var ddOpen = null;            // 'bc' | 'ta' | null
  var scraping = false;
  var LS = "taFiltersV1";
  var store = { presets: [], def: null };
  try { var s = JSON.parse(localStorage.getItem(LS)); if (s) store = s; } catch (e) {}

  var TA_COND = [
    { k: "all", label: "全部(不筛)" },
    { k: "hiconf", label: "🏅 高把握" },
    { k: "dip", label: "🎯 抄底@支撑(长多+回调)" },
    { k: "bigdown", label: "📉 今天大跌" },
    { k: "touch", label: "触及 S/R" },
    { k: "sup", label: "🟩 测试支撑" },
    { k: "res", label: "🟥 测试阻力" },
    { k: "bull", label: "📈 看多 Bull" },
    { k: "bear", label: "📉 看空 Bear" },
  ];

  function api() { return window.taFilterApi; }
  function F() { return api().filters; }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function bcTickers() {
    var sel = BC.filter(function (L) { return bcSel.has(L.key) && L.tickers && L.tickers.length; });
    if (!sel.length) return null;
    if (bcMode === "union") {
      var all = new Set();
      sel.forEach(function (L) { L.tickers.forEach(function (t) { all.add(String(t).toUpperCase()); }); });
      return Array.from(all);
    }
    var inter = sel[0].tickers.slice();
    for (var i = 1; i < sel.length; i++) { var s2 = new Set(sel[i].tickers); inter = inter.filter(function (t) { return s2.has(t); }); }
    return Array.from(new Set(inter.map(function (t) { return String(t).toUpperCase(); })));
  }
  function apply() {
    api().setBarchart(bcTickers());
    api().syncChips();
    api().render();
    renderControls();
  }

  function toggleTa(k) {
    var f = F();
    if (k === "all") f.clear();
    else { f.delete("all"); if (f.has(k)) f.delete(k); else f.add(k); }
    apply();
  }
  function toggleBc(k) { if (bcSel.has(k)) bcSel.delete(k); else bcSel.add(k); apply(); }
  function clearTa() { F().clear(); apply(); }
  function clearBc() { bcSel.clear(); apply(); }
  function setBcMode(m) { bcMode = m; apply(); }

  function curSel() { return { filters: Array.from(F()), bc: Array.from(bcSel), bcMode: bcMode }; }
  function applySel(sel) {
    var f = F(); f.clear(); (sel.filters || []).forEach(function (k) { f.add(k); });
    bcSel = new Set(sel.bc || []);
    bcMode = sel.bcMode || "union";
    apply();
  }
  function persist() { try { localStorage.setItem(LS, JSON.stringify(store)); } catch (e) {} }
  function setDefault() { store.def = curSel(); persist(); renderPresets(); flash("已设为默认(重启 PWA 自动加载)"); }
  function clearDefault() { store.def = null; persist(); renderPresets(); }
  function savePreset() {
    var n = (prompt("给这组筛选起个名(如 setting1):") || "").trim();
    if (!n) return;
    store.presets = store.presets.filter(function (p) { return p.name !== n; });
    store.presets.push(Object.assign({ name: n }, curSel()));
    persist(); renderPresets();
  }
  function delPreset(n) { store.presets = store.presets.filter(function (p) { return p.name !== n; }); persist(); renderPresets(); }
  function applyPreset(n) { var p = store.presets.find(function (x) { return x.name === n; }); if (p) applySel(p); }
  function starPreset(n) { var p = store.presets.find(function (x) { return x.name === n; }); if (p) { store.def = { filters: p.filters, bc: p.bc, bcMode: p.bcMode || "union" }; persist(); renderPresets(); flash("「" + n + "」设为默认"); } }

  function flash(msg) {
    var el = document.getElementById("tafFlash"); if (!el) return;
    el.textContent = msg; el.style.opacity = "1"; setTimeout(function () { el.style.opacity = "0"; }, 2500);
  }

  // ── 爬取 ──
  function doScrape() {
    if (scraping) return;
    scraping = true;
    paintPanels();
    fetch("/api/barchart/screeners/scrape", { method: "POST" }).then(function () {
      pollScrape();
    }).catch(function () { scraping = false; paintPanels(); });
  }
  function pollScrape() {
    fetch("/api/barchart/screeners/scrape/status").then(function (r) { return r.json(); }).then(function (d) {
      if (d.running) { setTimeout(pollScrape, 3000); return; }
      scraping = false;
      loadBC().then(function () { paintPanels(); flash("Screener 已刷新"); });
    }).catch(function () { scraping = false; paintPanels(); });
  }

  // ── 渲染 ──
  function injectCss() {
    if (document.getElementById("taf-css")) return;
    var st = document.createElement("style"); st.id = "taf-css";
    st.textContent =
      ".taf-dd{position:relative;display:inline-block}" +
      ".taf-ddbtn{background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:14px;padding:3px 12px;font-size:11px;cursor:pointer}" +
      ".taf-ddbtn.has{border-color:#1f6feb;color:#9cc3ff}" +
      ".taf-ddpanel{position:absolute;top:calc(100% + 4px);left:0;z-index:60;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:6px;min-width:240px;max-height:320px;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.5)}" +
      ".taf-ddpanel.taf-bc-panel{min-width:300px;max-height:440px}" +
      ".taf-grp{font-size:10px;font-weight:700;color:#8b949e;padding:5px 6px 2px}" +
      ".taf-opt{display:flex;align-items:center;gap:7px;padding:4px 6px;border-radius:5px;font-size:12px;color:#e6edf3;cursor:pointer}" +
      ".taf-opt:hover{background:#1f2937}" +
      ".taf-cnt{color:#6e7681;font-size:10px;margin-left:auto;white-space:nowrap}" +
      ".taf-mode-toggle{display:flex;gap:2px;padding:4px 6px 6px;border-bottom:1px solid #21262d;margin-bottom:4px}" +
      ".taf-mode-btn{flex:1;text-align:center;background:#0d1117;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:3px 8px;font-size:11px;cursor:pointer}" +
      ".taf-mode-btn:hover{color:#e6edf3;border-color:#8b949e}" +
      ".taf-mode-btn.active{background:#1f6feb;color:#fff;border-color:#1f6feb}" +
      ".taf-bc-footer{font-size:10px;color:#6e7681;padding:6px;border-top:1px solid #21262d;margin-top:4px;text-align:center}" +
      ".taf-scrape-btn{background:#0d1117;color:#8b949e;border:1px solid #30363d;border-radius:6px;padding:3px 8px;font-size:11px;cursor:pointer;width:100%}" +
      ".taf-scrape-btn:hover{color:#e6edf3;border-color:#58a6ff}" +
      ".taf-scrape-btn.running{color:#e3b341;cursor:wait}" +
      ".taf-mini{background:#0d1117;color:#8b949e;border:1px solid #30363d;border-radius:12px;padding:3px 9px;font-size:11px;cursor:pointer}" +
      ".taf-mini:hover{color:#e6edf3;border-color:#8b949e}" +
      ".taf-pchip{background:#11202e;color:#9cc3ff;border:1px solid #1f6feb;border-radius:12px;padding:2px 4px 2px 9px;font-size:11px;display:inline-flex;align-items:center;gap:4px}" +
      ".taf-pchip b{cursor:pointer;font-weight:600}.taf-pchip .x{cursor:pointer;color:#8b949e}.taf-pchip .x:hover{color:#f85149}" +
      ".taf-pchip .star{cursor:pointer}";
    document.head.appendChild(st);
  }

  function ddPanel(kind) {
    var h = "";
    if (kind === "bc") {
      // 合集/交集切换
      h += '<div class="taf-mode-toggle">' +
        '<button class="taf-mode-btn' + (bcMode === "union" ? " active" : "") + '" data-mode="union">合集(并集)</button>' +
        '<button class="taf-mode-btn' + (bcMode === "inter" ? " active" : "") + '" data-mode="inter">交集</button>' +
        '</div>';
      h += '<div class="taf-grp">🎯 我的 Saved Screener</div>';
      if (BC.length) {
        BC.forEach(function (L) {
          h += '<div class="taf-opt" data-k="' + esc(L.key) + '">' +
            '<input type="checkbox" ' + (bcSel.has(L.key) ? "checked" : "") + ' readonly>' +
            '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(L.name) + '</span>' +
            '<span class="taf-cnt">' + (L.count || 0) + (L.asof ? ' · ' + esc(L.asof) : '') + '</span>' +
            "</div>";
        });
      } else {
        h += '<div class="taf-opt" style="color:#6e7681">未爬取，点下方刷新</div>';
      }
      // 底栏
      var result = bcTickers();
      var total = result ? result.length : 0;
      if (bcSel.size) {
        h += '<div class="taf-bc-footer">' + (bcMode === "union" ? "并集" : "交集") + ' = ' + total + ' 只票</div>';
      }
      h += '<div style="padding:4px 6px;border-top:1px solid #21262d;margin-top:4px">' +
        '<button class="taf-scrape-btn' + (scraping ? " running" : "") + '" id="tafScrapeBtn">' +
        (scraping ? '⏳ 爬取中…' : '↻ 刷新爬取') + '</button></div>';
    } else {
      h += '<div class="taf-grp">TA 条件(多选 AND)</div>';
      var f = F();
      TA_COND.forEach(function (c) {
        var on = c.k === "all" ? f.size === 0 : f.has(c.k);
        h += '<div class="taf-opt" data-k="' + c.k + '"><input type="checkbox" ' + (on ? "checked" : "") + " readonly>" + esc(c.label) + "</div>";
      });
    }
    return h;
  }

  function renderControls() {
    var host = document.getElementById("taFilterDD");
    if (!host) return;
    var f = F();
    var taN = f.size, bcN = bcSel.size;
    var bcLabel = "📊 Screener";
    if (bcN) {
      var result = bcTickers();
      var total = result ? result.length : 0;
      var modeLabel = bcMode === "union" ? "合集" : "交集";
      bcLabel += " (" + bcN + " " + modeLabel + " " + total + "票)";
    }
    host.innerHTML =
      '<span class="taf-dd"><button class="taf-ddbtn ' + (bcN ? "has" : "") + '" id="tafBcBtn">' + bcLabel + " ▾</button>" +
      '<div class="taf-ddpanel taf-bc-panel" id="tafBcPanel" hidden></div></span>' +
      '<span class="taf-dd"><button class="taf-ddbtn ' + (taN ? "has" : "") + '" id="tafTaBtn">🎯 TA 条件 ' + (taN ? "(" + taN + ")" : ": 全部") + " ▾</button>" +
      '<div class="taf-ddpanel" id="tafTaPanel" hidden></div></span>' +
      '<button class="taf-mini" id="tafClr">✕ 清空</button>' +
      '<button class="taf-mini" id="tafDef" title="把当前筛选存为默认, 重启 PWA 自动加载">⭐ 设为默认</button>' +
      '<button class="taf-mini" id="tafSave" title="把当前筛选命名保存为快捷预设">💾 存预设</button>' +
      '<span id="tafFlash" style="font-size:11px;color:#3fb950;opacity:0;transition:opacity .3s"></span>';
    var bcBtn = document.getElementById("tafBcBtn"), taBtn = document.getElementById("tafTaBtn");
    bcBtn.onclick = function (e) { e.stopPropagation(); ddOpen = ddOpen === "bc" ? null : "bc"; paintPanels(); };
    taBtn.onclick = function (e) { e.stopPropagation(); ddOpen = ddOpen === "ta" ? null : "ta"; paintPanels(); };
    document.getElementById("tafClr").onclick = function () { F().clear(); bcSel.clear(); apply(); };
    document.getElementById("tafDef").onclick = setDefault;
    document.getElementById("tafSave").onclick = savePreset;
    paintPanels();
  }
  function paintPanels() {
    var bp = document.getElementById("tafBcPanel"), tp = document.getElementById("tafTaPanel");
    if (!bp || !tp) return;
    bp.hidden = ddOpen !== "bc"; tp.hidden = ddOpen !== "ta";
    if (ddOpen === "bc") {
      bp.innerHTML = ddPanel("bc");
      bp.querySelectorAll("[data-mode]").forEach(function (btn) {
        btn.onclick = function (e) { e.stopPropagation(); setBcMode(btn.dataset.mode); };
      });
      bp.querySelectorAll("[data-k]").forEach(function (o) {
        o.onclick = function (e) { e.stopPropagation(); toggleBc(o.dataset.k); };
      });
      var sb = document.getElementById("tafScrapeBtn");
      if (sb) sb.onclick = function (e) { e.stopPropagation(); doScrape(); };
    }
    if (ddOpen === "ta") { tp.innerHTML = ddPanel("ta"); tp.querySelectorAll("[data-k]").forEach(function (o) { o.onclick = function (e) { e.stopPropagation(); toggleTa(o.dataset.k); }; }); }
  }
  document.addEventListener("click", function (e) {
    if (!ddOpen) return;
    if (!e.target.closest || !e.target.closest("#taFilterDD")) { ddOpen = null; paintPanels(); }
  });

  function renderPresets() {
    var bar = document.getElementById("taPresetBar");
    if (!bar) return;
    if (!store.presets.length && !store.def) { bar.style.display = "none"; bar.innerHTML = ""; return; }
    bar.style.display = "flex";
    var h = '<span style="color:#6e7681">快捷预设:</span>';
    var defKey = store.def ? JSON.stringify({ filters: (store.def.filters || []).slice().sort(), bc: (store.def.bc || []).slice().sort(), bcMode: store.def.bcMode || "union" }) : null;
    store.presets.forEach(function (p) {
      var isDef = defKey && JSON.stringify({ filters: (p.filters || []).slice().sort(), bc: (p.bc || []).slice().sort(), bcMode: p.bcMode || "union" }) === defKey;
      h += '<span class="taf-pchip"><span class="star" data-star="' + esc(p.name) + '" title="设为默认">' + (isDef ? "★" : "☆") + '</span>' +
        '<b data-apply="' + esc(p.name) + '">' + esc(p.name) + '</b><span class="x" data-del="' + esc(p.name) + '">✕</span></span>';
    });
    if (store.def && !store.presets.some(function (p) { return JSON.stringify({ filters: (p.filters || []).slice().sort(), bc: (p.bc || []).slice().sort(), bcMode: p.bcMode || "union" }) === defKey; })) {
      h += '<span style="color:#6e7681;font-size:11px">· 默认已设(未命名) <span class="taf-mini" id="tafClrDef" style="padding:1px 7px">取消默认</span></span>';
    }
    bar.innerHTML = h;
    bar.querySelectorAll("[data-apply]").forEach(function (b) { b.onclick = function () { applyPreset(b.dataset.apply); }; });
    bar.querySelectorAll("[data-del]").forEach(function (b) { b.onclick = function () { delPreset(b.dataset.del); }; });
    bar.querySelectorAll("[data-star]").forEach(function (b) { b.onclick = function () { starPreset(b.dataset.star); }; });
    var cd = document.getElementById("tafClrDef"); if (cd) cd.onclick = clearDefault;
  }

  async function loadBC() {
    try {
      var j = await (await fetch("/api/barchart/screeners")).json();
      BC = j.screeners || [];
    } catch (e) { BC = []; }
  }

  async function init() {
    if (!document.getElementById("taFilterDD") || !window.taFilterApi) { setTimeout(init, 300); return; }
    injectCss();
    try { F().delete("all"); } catch (e) {}
    await loadBC();
    if (store.def) {
      bcMode = store.def.bcMode || "union";
      applySel({ filters: store.def.filters || [], bc: [] });
    }
    else renderControls();
    renderPresets();
  }
  if (document.readyState !== "loading") init(); else document.addEventListener("DOMContentLoaded", init);
})();
