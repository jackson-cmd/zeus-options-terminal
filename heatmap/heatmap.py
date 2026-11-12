"""Embedded HTML and JS for the Heatmap tab.

Mirrors history_news.py — two Python r-strings exported for
optionstrat_server.py to splice into the single-page HTML response
and the JS injection block, respectively.

The heatmap is a d3.js treemap nested Sector → Industry → Ticker:
  • rect size  = market cap
  • rect color = today's % change (green / red gradient)
  • hover shows ticker + last + % change
  • the panel polls /api/heatmap/data every 10 s
"""

HEATMAP_HTML = r"""
<div id="tab-heatmap" class="top-tab-panel" style="overflow:hidden;padding:0;position:relative">
  <style>
    .hm-news-link { color:#58a6ff; text-decoration:underline; text-underline-offset:2px; cursor:pointer; }
    .hm-news-link:hover { color:#79c0ff; }
    /* TA 筛选 chips(移植自 TA screener) */
    .hm-flt { background:#161b22; color:#8b949e; border:1px solid #30363d; border-radius:4px; padding:3px 9px; font-size:11px; cursor:pointer; font-weight:600; line-height:1.4; }
    .hm-flt:hover { border-color:#388bfd; color:#e6edf3; }
    .hm-flt.on { background:#1f6feb; color:#fff; border-color:#1f6feb; }
    /* 工具栏收起/展开(给热图腾地方) */
    #hmToolbar.hm-tb-collapsed .hm-coll { display:none !important; }
    .hm-collapse-btn { background:#161b22; color:#8b949e; border:1px solid #30363d; border-radius:4px; padding:3px 9px; font-size:11px; cursor:pointer; font-weight:600; white-space:nowrap; }
    .hm-collapse-btn:hover { color:#e6edf3; border-color:#388bfd; }
    /* 💹 期权量稳定性 panel (移植自 options_vol/page.py; 选择器全部限定在 #hmOptVolPanel 内) */
    #hmOptVolPanel { font-size:13px; color:#e6edf3; }
    #hmOptVolPanel .ovInner { max-width:1500px; margin:0 auto; }
    #hmOptVolPanel h1 { font-size:16px; margin:0 0 2px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
    #hmOptVolPanel .ovMuted { color:#8b949e; font-size:11px; font-weight:400; }
    #hmOptVolPanel .ovSubhead { color:#8b949e; font-size:11px; margin:0 0 8px; }
    #hmOptVolPanel button.ovChip { background:#21262d; color:#8b949e; border:1px solid #30363d; border-radius:12px; padding:3px 10px; font-size:11px; cursor:pointer; }
    #hmOptVolPanel button.ovChip:hover { border-color:#58a6ff; }
    #hmOptVolPanel button.ovChip.on { background:#1f6feb; border-color:#1f6feb; color:#fff; }
    #hmOptVolPanel .ovStatusBar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; background:#161b22; border:1px solid #30363d; border-radius:8px; padding:8px 12px; margin:8px 0 4px; font-size:12px; }
    #hmOptVolPanel .ovStatusBar .pill { background:#21262d; border:1px solid #30363d; border-radius:10px; padding:2px 9px; font-size:11px; color:#8b949e; }
    #hmOptVolPanel .ovStatusBar .pill b { color:#e6edf3; }
    #hmOptVolPanel .ovRangeToggle { display:inline-flex; gap:4px; }
    #hmOptVolPanel .ovExpandInner { padding:4px 2px 8px; }
    #hmOptVolPanel .ovExpandHead { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:0 0 8px; }
    #hmOptVolPanel .ovCharts { display:flex; gap:14px; flex-wrap:wrap; }
    #hmOptVolPanel .ovChartbox { flex:1; min-width:240px; background:#0a0d12; border:1px solid #30363d; border-radius:8px; padding:8px 10px; }
    #hmOptVolPanel .ovChartbox .ovCtitle { font-size:11px; color:#8b949e; margin-bottom:2px; display:flex; justify-content:space-between; align-items:baseline; }
    #hmOptVolPanel .ovChartbox .ovClast { font-size:12px; font-weight:700; color:#e6edf3; }
    #hmOptVolPanel .ovChartbox svg { width:100%; height:130px; display:block; }
    #hmOptVolPanel table.ovTable { width:100%; border-collapse:collapse; margin-top:4px; }
    #hmOptVolPanel table.ovTable th { text-align:right; font-size:11px; color:#8b949e; padding:6px 8px; border-bottom:1px solid #30363d; cursor:pointer; user-select:none; white-space:nowrap; position:sticky; top:0; background:#010409; z-index:2; }
    #hmOptVolPanel table.ovTable th:first-child, #hmOptVolPanel table.ovTable td:first-child { text-align:left; }
    #hmOptVolPanel table.ovTable th:hover { color:#58a6ff; }
    #hmOptVolPanel table.ovTable th .arr { font-size:9px; margin-left:2px; }
    #hmOptVolPanel table.ovTable td { padding:6px 8px; border-bottom:1px solid #1c2128; white-space:nowrap; vertical-align:middle; font-variant-numeric:tabular-nums; text-align:right; }
    #hmOptVolPanel tr.ovRow { cursor:pointer; }
    #hmOptVolPanel tr.ovRow:hover { background:#161b2280; }
    #hmOptVolPanel tr.ovRow.sel { background:#1f6feb22; }
    #hmOptVolPanel tr.ovRow.sel td { border-bottom-color:transparent; }
    #hmOptVolPanel tr.ovExpand > td { padding:0 8px; background:#0d1117; border-bottom:1px solid #30363d; }
    #hmOptVolPanel .ovSym { font-weight:700; color:#58a6ff; }
    #hmOptVolPanel .ovBadge { display:inline-block; padding:1px 7px; border-radius:9px; font-size:10px; font-weight:700; }
    #hmOptVolPanel .ovBGold { background:rgba(227,179,65,.16); color:#e3b341; border:1px solid rgba(227,179,65,.45); }
    #hmOptVolPanel .ovBSilver { background:rgba(168,179,194,.14); color:#a8b3c2; border:1px solid rgba(168,179,194,.4); }
    #hmOptVolPanel .ovSymcell { display:inline-flex; align-items:center; gap:7px; }
    #hmOptVolPanel .ovSymcell .ovChev { color:#8b949e; font-size:9px; transition:transform .15s; display:inline-block; }
    #hmOptVolPanel tr.ovRow.sel .ovSymcell .ovChev { transform:rotate(90deg); color:#58a6ff; }
    #hmOptVolPanel .ovPctOk { color:#3fb950; }
    #hmOptVolPanel .ovPctMid { color:#e3b341; }
    #hmOptVolPanel .ovPctLo { color:#8b949e; }
    #hmOptVolPanel .ovErr { color:#f85149; }
    /* 2026-07-30: 🎯 选股 sub-nav (K线图墙 + 从 mbrief 迁过来的 3 sub) */
    .hm-sub-pill { background:transparent; color:#8b949e; border:1px solid #30363d; padding:5px 14px; border-radius:4px; cursor:pointer; font-size:12px; font-weight:600; }
    .hm-sub-pill.active { background:#1f6feb; color:#fff; border-color:#1f6feb; }
    /* 精选选股 loading 进度条动画 (放这里而不是 JS 内 style 标签, 避开 server.py:2192 的 close-style-tag 全局 replace) */
    @keyframes hmPkPulse { 0% { left:-35%; } 100% { left:100%; } }
    .hm-pk-progress { position:relative; height:6px; background:#161b22; border-radius:3px; overflow:hidden; }
    .hm-pk-progress > div { position:absolute; top:0; bottom:0; width:35%; background:linear-gradient(90deg,#1f6feb,#58a6ff); border-radius:3px; animation:hmPkPulse 1.4s ease-in-out infinite; }
  </style>
  <!-- 🎯 选股 sub-nav: K线图墙 + 热力图 + 大盘判断
       大盘判断 has its own header (🔄 Re-analyze) and three sub-tabs (brief / sector rotation /
       options sentiment); reorgTabs moves them into #hmSubMbriefHost below. -->
  <div id="hmSubNav" style="display:flex;gap:6px;padding:6px 14px;border-bottom:1px solid #30363d;background:#0d1117;flex-shrink:0">
    <button data-name="kline" onclick="hmSubView('kline')" class="hm-sub-pill active">📈 K线图墙</button>
    <button data-name="heatmap" onclick="hmSubView('heatmap')" class="hm-sub-pill" title="热力图：按板块 → 行业排列，面积 = 市值，颜色 = 今日涨跌；点格子看单票详情">🟩 热力图</button>
    <button data-name="brief" onclick="hmSubView('brief')" class="hm-sub-pill">🧭 大盘判断</button>
  </div>
  <!-- host: reorgTabs 里 re-parent tab-mbrief 内容进来 -->
  <div id="hmSubMbriefHost" style="display:none;padding:14px 20px;overflow-y:auto;flex:1"></div>
  <div id="hmToolbar" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid #21262d">
    <strong id="hmTitle" style="color:#e6edf3;font-size:13px;white-space:nowrap">🎯 选股 · K线图墙</strong>
    <button id="hmCollapseBtn" class="hm-collapse-btn" onclick="hmToggleToolbar()" title="收起/展开设置控件(给热图腾地方)">▴ 收起</button>
    <!-- 数据模式选择(固定位置: 紧跟标题+收起按钮左对齐, 像顶部 PWA 导航;
         不随当前模式/状态文字/控件换行而跳位置 — 快速切换不会点错) -->
    <!-- 2026-07-30 轻量化: dataMode 按钮组隐藏, 锁定 stocks + graph. 保留 DOM 让原逻辑仍有 fallback. -->
    <div id="hmDataMode" style="display:none;border:1px solid #30363d;border-radius:4px;overflow:hidden">
      <button type="button" data-dm="stocks" onclick="hmSetDataMode('stocks')"
        style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">📈 股票</button>
      <button type="button" data-dm="etf" onclick="hmSetDataMode('etf')"
        style="display:none;background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">🗂 ETF趋势</button>
      <button type="button" data-dm="risk" onclick="hmSetDataMode('risk')"
        title="每只股票的 β / 相关性 / 波动率 (vs SPY·QQQ), 非实时, 每周更新"
        style="display:none;background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">📈 β/相关</button>
      <button type="button" data-dm="perf" onclick="hmSetDataMode('perf')"
        title="3天/1周/1月/3月/1年 涨跌排行, 可缩放蜂群图, 非实时, 每日更新"
        style="display:none;background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">🏁 涨跌榜</button>
      <!-- 期权波动实验 tab 暂停使用(结论已收敛, 代码保留)
      <button type="button" data-dm="opt" onclick="hmSetDataMode('opt')"
        title="期权波动实验: top市值标的 ATM 月期权 IV / 跨式 / 买卖价差 + 盘中啥时候买便宜/好成交, 盘中按 cadence 采集"
        style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">💰 期权波动</button>
      -->
      <button type="button" data-dm="optvol" onclick="hmSetDataMode('optvol')"
        title="期权量稳定性: 合并 universe 每日成交量, ≥90% 交易日达标分档(🥇每天≥5000 / 🥈每天≥2000, 已排除 ADR), 点行展开 vol/OI/IV/PC 趋势"
        style="display:none;background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">💹 期权量</button>
    </div>

    <span style="flex:1"></span>
    <span id="hmStatus" style="color:#8b949e;font-size:11px">Loading…</span>
    <!-- Drill-down breadcrumb: shown only after clicking a sector title -->
    <span id="hmCrumb" style="display:none;align-items:center;gap:6px">
      <button onclick="hmDrillClear()"
        style="background:#1f6feb;color:#fff;border:none;border-radius:4px;
               padding:3px 10px;font-size:11px;cursor:pointer;font-weight:600">← 全部板块</button>
      <span id="hmCrumbName" style="color:#e6edf3;font-size:12px;font-weight:600"></span>
      <span id="hmCrumbCount" style="color:#8b949e;font-size:11px"></span>
    </span>

    <!-- 永久换行: 模式条永远独占第一行(固定), 下面才是 per-mode 控件(收起时隐藏) -->
    <div style="flex-basis:100%;height:0;margin:0;padding:0;border:0"></div>

    <!-- Force the mode-specific controls + view toggle onto their own wrapped
         row so the toolbar never overflows the panel / gets clipped.
         hm-coll: 收起时连同这个换行一起隐藏, 让 view toggle 上移、热图变大。 -->
    <div class="hm-coll" style="flex-basis:100%;height:0;margin:0;padding:0;border:0"></div>

    <!-- 2026-07-30 作者轻量化: 股票/ETF 按钮隐藏(点击会切 heatmap dataMode, 破坏 graph 锁定) -->
    <div id="hmAssetToggle" class="hm-coll" title="切换: 股票(实时) / ETF趋势(每日更新)"
         style="display:none;border:1px solid #30363d;border-radius:4px;overflow:hidden">
      <button type="button" data-at="stocks" onclick="hmSetDataMode('stocks')"
        style="background:#1f6feb;color:#fff;border:none;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">股票</button>
      <button type="button" data-at="etf" onclick="hmSetDataMode('etf')"
        style="background:transparent;color:#8b949e;border:none;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">ETF</button>
    </div>
    <!-- ── 股票模式控件 ── -->
    <span id="hmStockCtrls" class="hm-coll" style="display:contents">
    <!-- Group(固定 Sector › Industry) / Size(固定 √市值, 缩小 mega-cap 碾压) / Range(固定 ±3%)
         控件已按用户要求删除, 值在 render()/buildHierarchy 里硬编码。 -->

    <!-- 2026-07-30 作者轻量化: 市值筛选按钮隐藏 -->
    <div id="hmMcapToggle" title="按市值筛选大公司。大盘≥100B = compact 精选(每天开盘/收盘必看那一批)"
         style="display:none;border:1px solid #30363d;border-radius:4px;overflow:hidden">
      <button type="button" data-mc="0" onclick="hmSetMcapMin(0)"
        style="background:#1f6feb;color:#fff;border:none;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">全部</button>
      <button type="button" data-mc="10" onclick="hmSetMcapMin(10)"
        style="background:transparent;color:#8b949e;border:none;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">≥10B</button>
      <button type="button" data-mc="100" onclick="hmSetMcapMin(100)"
        style="background:transparent;color:#8b949e;border:none;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">大盘≥100B</button>
    </div>

    <!-- 2026-07-30 作者轻量化: 盘中/盘后按钮隐藏 (作者都盯盘) -->
    <div id="hmPriceMode" title="盘中/收盘=今日相对昨收的涨跌(盘后冻结在收盘价); 盘前盘后=延长时段相对今日收盘的涨跌"
         style="display:none;border:1px solid #30363d;border-radius:4px;overflow:hidden">
      <button type="button" data-pm="reg" onclick="hmSetPriceMode('reg')"
        style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">🕘 盘中/收盘</button>
      <button type="button" data-pm="ext" onclick="hmSetPriceMode('ext')"
        style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">🌙 盘前盘后</button>
    </div>

    <label style="color:#8b949e;font-size:11px">Auto:</label>
    <select id="hmAutoSel" onchange="hmSetAutoInterval()" title="前端多久重拉一次快照。后端已 DXLink 实时推送, /data 是本地读取(很便宜), 想看实时就选 5s/15s。"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
      <option value="0">Off</option>
      <option value="5" selected>5s ⚡</option>
      <option value="15">15s</option>
      <option value="60">1m</option>
      <option value="300">5m</option>
      <option value="900">15m</option>
      <option value="3600">1h</option>
    </select>

    <!-- ── 🔎 筛选: β/ρ + 财报(TA条件/Barchart/动量反弹回撤/预设 已按作者要求删除) ── -->
    <div style="flex-basis:100%;height:0;margin:0;padding:0;border:0"></div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
      <span style="color:#6e7681;font-size:10px;font-weight:600;letter-spacing:.5px">🔎 筛选</span>
      <!-- 2026-07-30 作者轻量化: β/ρ 筛选隐藏 -->
      <select id="hmBetaFilter" onchange="hmSetBetaFilter(this.value)" title="按 β/ρ 筛选(基于风险数据, 每周更新)"
        style="display:none;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="">β/ρ:全部</option>
        <option value="spy_low_corr">SPY弱相关 ρ&lt;0.5</option>
        <option value="qqq_low_corr">QQQ弱相关 ρ&lt;0.5</option>
        <option value="spy_high_beta">高β SPY β&gt;1.2</option>
        <option value="spy_low_beta">低β SPY β&lt;0.8</option>
      </select>
      <select id="hmTaEarn" onchange="hmSetTaEarn(this.value)" title="财报临近(14天内)筛选"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="">财报:全部</option>
        <option value="ex">排除临财报</option>
        <option value="only">只看临财报</option>
      </select>
      <input id="hmEarnDays" type="number" min="0" max="120" step="1" value="28"
        onchange="hmSetEarnDays(this.value)" title="「临财报」算几天内(默认 28 天)"
        style="width:52px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
      <span style="color:#8b949e;font-size:11px;margin-left:-3px">天内</span>
    </div>

    <!-- ── EOD 期权链爬取状态 + 「⚡现在盘后爬取」(累积 EOD 期权链历史库, 喂 backtest + P/C) ── -->
    <div style="flex-basis:100%;height:0;margin:0;padding:0;border:0"></div>
    <div id="eodStatus" style="display:none;align-items:center;gap:8px;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:5px 10px;font-size:11px;color:#e6edf3"></div>
    <div id="eodCrawlProg" style="display:none;align-items:center;gap:8px">
      <div style="flex:1;height:7px;max-width:300px;background:#21262d;border-radius:4px;overflow:hidden"><div id="eodCrawlFill" style="height:100%;width:0%;background:linear-gradient(90deg,#1f6feb,#3fb950);transition:width .4s"></div></div>
      <span id="eodCrawlMsg" style="font-size:11px;color:#8b949e">爬取中…</span>
    </div>
    </span>

    <!-- ── ETF 趋势模式控件 ── -->
    <span id="hmEtfCtrls" class="hm-coll" style="display:none;align-items:center;gap:10px">
      <div id="hmEtfCat" style="display:inline-flex;border:1px solid #30363d;border-radius:4px;overflow:hidden">
        <button type="button" data-cat="SECTOR" onclick="hmSetEtfCat('SECTOR')"
          style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">板块</button>
        <button type="button" data-cat="GLOBAL" onclick="hmSetEtfCat('GLOBAL')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">国家/全球</button>
      </div>
      <label style="color:#8b949e;font-size:11px">强度</label>
      <select id="hmEtfPeriod" onchange="hmSetEtfPeriod(this.value)"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="r3d">3天</option>
        <option value="r7d">7天</option>
        <option value="r1mo" selected>1月</option>
        <option value="r3mo">3月</option>
        <option value="r1yr">1年</option>
      </select>
    </span>

    <!-- ── β / 相关性 模式控件 ── -->
    <span id="hmRiskCtrls" class="hm-coll" style="display:none;align-items:center;gap:10px">
      <div id="hmRiskUniv" style="display:inline-flex;border:1px solid #30363d;border-radius:4px;overflow:hidden">
        <button type="button" data-uni="stocks" onclick="hmSetRiskUniverse('stocks')"
          style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">📈 股票</button>
        <button type="button" data-uni="etf" onclick="hmSetRiskUniverse('etf')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">🗂 ETF</button>
      </div>
      <label style="color:#8b949e;font-size:11px">基准</label>
      <div id="hmRiskBench" style="display:inline-flex;border:1px solid #30363d;border-radius:4px;overflow:hidden">
        <button type="button" data-bn="SPY" onclick="hmSetRiskBench('SPY')"
          style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">SPY</button>
        <button type="button" data-bn="QQQ" onclick="hmSetRiskBench('QQQ')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">QQQ</button>
      </div>
      <label style="color:#8b949e;font-size:11px">周期</label>
      <div id="hmRiskWindow" style="display:inline-flex;border:1px solid #30363d;border-radius:4px;overflow:hidden">
        <button type="button" data-wn="3m" onclick="hmSetRiskWindow('3m')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">3月</button>
        <button type="button" data-wn="6m" onclick="hmSetRiskWindow('6m')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">6月</button>
        <button type="button" data-wn="1y" onclick="hmSetRiskWindow('1y')"
          style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">1年</button>
      </div>
    </span>

    <!-- ── 涨跌榜 模式控件 ── -->
    <span id="hmPerfCtrls" class="hm-coll" style="display:none;align-items:center;gap:10px">
      <label style="color:#8b949e;font-size:11px">周期</label>
      <div id="hmPerfPeriod" style="display:inline-flex;border:1px solid #30363d;border-radius:4px;overflow:hidden">
        <button type="button" data-pp="r3d" onclick="hmSetPerfPeriod('r3d')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">3天</button>
        <button type="button" data-pp="r1w" onclick="hmSetPerfPeriod('r1w')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">1周</button>
        <button type="button" data-pp="r1mo" onclick="hmSetPerfPeriod('r1mo')"
          style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">1月</button>
        <button type="button" data-pp="r3mo" onclick="hmSetPerfPeriod('r3mo')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">3月</button>
        <button type="button" data-pp="r1yr" onclick="hmSetPerfPeriod('r1yr')"
          style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">1年</button>
      </div>
    </span>

    <!-- ── 共享: β/相关 + 涨跌榜 的 搜索高亮 & sector/industry 筛选 ── -->
    <span id="hmAnaCtrls" class="hm-coll" style="display:none;align-items:center;gap:8px">
      <input id="hmAnaSearch" type="text" placeholder="🔍 高亮 ticker…" oninput="hmAnaSearch(this.value)"
        title="输入 ticker 高亮; 切换 散点/表格 都保留, 直到清空"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 10px;font-size:11px;width:128px">
      <select id="hmAnaSector" onchange="hmAnaSetSector(this.value)"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 6px;font-size:11px;max-width:150px">
        <option value="">全部板块</option>
      </select>
      <select id="hmAnaIndustry" onchange="hmAnaSetIndustry(this.value)"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 6px;font-size:11px;max-width:170px">
        <option value="">全部行业</option>
      </select>
      <button type="button" onclick="hmAnaClear()" title="清空搜索 + 板块/行业筛选 (蜂群图同时复位缩放)"
        style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">⟲ 复位</button>
    </span>

    <!-- 2026-07-30 轻量化: view toggle 按钮组隐藏, 只剩 graph 一个 view. 保留 DOM 让 hmSetView 仍可 sync 状态. -->
    <div id="hmViewToggle" data-view="graph"
         style="display:none;border:1px solid #30363d;border-radius:4px;overflow:hidden">
      <button type="button" data-view="treemap" onclick="hmSetView('treemap')"
        style="background:#1f6feb;color:#fff;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">🟩 Treemap</button>
      <button id="hmBarViewBtn" type="button" data-view="bar" onclick="hmSetView('bar')"
        style="display:none;background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">📊 柱状</button>
      <button id="hmGraphViewBtn" type="button" data-view="graph" onclick="hmSetView('graph')"
        title="K线图墙:每只票一张自绘 candle + S/R + EMA + 评分(炒 TA / 抄底用)。配合「大盘≥100B」最好看。"
        style="background:transparent;color:#8b949e;border:none;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">📈 Graph</button>
    </div>

    <!-- 📰 Mover News 已升级为数据模式(#hmDataMode 的「📰 异动新闻」), 与总览/期权量等互斥;
         不再用独立按钮(原来叠在面板模式上会混淆视图)。 -->

    <button id="hmRefreshBtn" onclick="hmRefresh()"
      style="background:#1f6feb;color:#fff;border:none;border-radius:4px;
             padding:4px 12px;cursor:pointer;font-size:11px;font-weight:600">
      Refresh
    </button>
  </div>

  <div id="hmCanvas" style="flex:1;min-height:0;position:relative;background:#010409">
    <svg id="hmSvg" style="width:100%;height:100%;display:block"></svg>
    <div id="hmTip"
      style="position:absolute;pointer-events:none;background:#161b22;
             border:1px solid #30363d;border-radius:5px;padding:6px 9px;
             font-size:11px;color:#e6edf3;line-height:1.4;display:none;
             box-shadow:0 4px 12px rgba(0,0,0,0.45);z-index:5;
             font-variant-numeric:tabular-nums;white-space:nowrap"></div>
  </div>

  <!-- (原 #hmTableView 表格视图整块已删 —— hmSetView 的 VIEWS 里虽然列着 'table',
       但工具栏只有 Treemap/柱状/Graph 三个按钮, 没有任何代码路径会传 'table' 进去
       (state.view 只在 hmSetView 里赋值, 实测 5 个数据模式下始终是 treemap),
       所以它连同 compact movers / 新闻列 / risk·perf·ETF 的表格版渲染器和表头排序
       回调一起都是死的。risk/perf/etf 三个模式看到的是 treemap, 数据加载器保留。) -->

  <!-- 📈 Graph view (hidden by default; toggled by hmSetView('graph')).
       K线图墙:3-5/行,每张复用 ta_screener 自绘 candle + S/R zones + EMA + 评分。
       懒拉 /api/ta/ohlc(IntersectionObserver,卡片进视口才拉,并发节流)。 -->
  <div id="hmGraphView" style="display:none;flex:1;min-height:0;flex-direction:column;background:#010409;overflow:hidden">
    <div style="display:flex;gap:10px;align-items:center;padding:8px 14px;flex-wrap:wrap;border-bottom:1px solid #21262d">
      <span style="color:#e6edf3;font-size:12px;font-weight:600">📈 K线图墙</span>
      <span id="hmGraphCount" style="color:#8b949e;font-size:11px"></span>
      <label style="color:#8b949e;font-size:11px;margin-left:6px">周期</label>
      <select id="hmGraphDays" onchange="hmSetGraphDays(this.value)"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="21">1月</option>
        <option value="63">3月</option>
        <option value="126" selected>6月</option>
        <option value="252">1年</option>
        <option value="504">2年</option>
      </select>
      <label style="color:#8b949e;font-size:11px;margin-left:6px">排序</label>
      <select id="hmGraphSort" onchange="hmSetGraphSort(this.value)"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="mcap" selected>市值</option>
        <option value="change">今日涨跌</option>
        <option value="score">TA 评分</option>
      </select>
      <label style="color:#8b949e;font-size:11px;margin-left:6px">每行</label>
      <select id="hmGraphCols" onchange="hmSetGraphCols(this.value)" title="每行几张图(自动=按窗口宽度自适应)"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="auto" selected>自动</option>
        <option value="3">3</option>
        <option value="4">4</option>
        <option value="5">5</option>
        <option value="6">6</option>
      </select>
      <label style="color:#8b949e;font-size:11px;margin-left:6px">AI</label>
      <select id="hmGraphAiFilter" onchange="hmSetGraphAiFilter(this.value)" title="按 AI 选股分类筛选"
        style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="all" selected>全部</option>
        <option value="看涨">▲ 看涨(全)</option>
        <option value="看涨·低点抄底">▲ 低点抄底</option>
        <option value="看涨·趋势追涨">▲ 趋势追涨</option>
        <option value="不交易">— 不交易</option>
        <option value="看跌">▼ 看跌(全)</option>
        <option value="看跌·高点阻力">▼ 高点阻力</option>
        <option value="看跌·颓势追跌">▼ 颓势追跌</option>
      </select>
      <button id="hmAiPicksBtn" onclick="hmAiPicksReport()" title="打开 AI 选股报告弹窗(读当天缓存, 5 分类分组 + 每票技术/基本面/新闻理由); 弹窗内可「重新分析」全量重跑。徽章也挂在每张卡片上, 点徽章看理由"
        style="background:#da3633;color:#fff;border:1px solid #f85149;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">🚀 AI选股</button>
      <button id="hmExpandAllBtn" onclick="hmToggleExpandAll()" title="一键展开/收起所有卡片的 AI 选股 + 多空全文"
        style="background:transparent;color:#a371f7;border:1px solid #a371f766;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">⊕ 全展开</button>
      <span id="hmAiPicksStatus" style="color:#8b949e;font-size:11px"></span>
      <!-- 📊 Barchart 榜单多选(按类别分组 + 交集/并集): 勾选 → hmState.taBcTickers → 图墙筛选 -->
      <span id="hmBcWrap" style="position:relative;display:inline-flex;align-items:center;gap:5px;margin-left:8px">
        <button id="hmBcBtn" type="button" onclick="hmBcTogglePanel()" title="按 Barchart 榜单多选筛选图墙(可切交集/并集)"
          style="background:#161b22;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">📊 Barchart ▾</button>
        <span id="hmBcSummary" style="color:#8b949e;font-size:10px"></span>
        <div id="hmBcPanel" style="display:none;position:absolute;top:calc(100% + 5px);left:0;z-index:60;width:320px;max-height:440px;overflow:auto;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px;box-shadow:0 8px 24px rgba(0,0,0,.55)">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
            <div id="hmBcModeToggle" style="display:inline-flex;border:1px solid #30363d;border-radius:4px;overflow:hidden">
              <button type="button" data-bcmode="intersect" onclick="hmBcSetMode('intersect')"
                style="background:#1f6feb;color:#fff;border:none;padding:3px 10px;font-size:11px;cursor:pointer;font-weight:600">交集</button>
              <button type="button" data-bcmode="union" onclick="hmBcSetMode('union')"
                style="background:transparent;color:#8b949e;border:none;padding:3px 10px;font-size:11px;cursor:pointer;font-weight:600">并集</button>
            </div>
            <button type="button" onclick="hmBcClear()" title="清除所有勾选"
              style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 10px;font-size:11px;cursor:pointer;font-weight:600">清除</button>
            <span style="flex:1"></span>
            <span id="hmBcHits" style="color:#8b949e;font-size:10px"></span>
          </div>
          <div id="hmBcList" style="display:flex;flex-direction:column;gap:8px"><div style="color:#6e7681;font-size:11px;padding:8px">载入中…</div></div>
        </div>
      </span>
      <span style="flex:1"></span>
      <span style="color:#6e7681;font-size:10px;margin-left:4px">⭐=强 S/R · 绿线支撑/红线阻力 · 橙EMA50 蓝EMA200 · 点AI徽章看理由</span>
    </div>
    <div id="hmGraphGrid" style="flex:1;min-height:0;overflow-y:auto;padding:10px 12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;align-content:start"></div>
  </div>

  <!-- 📰 Mover News view (hidden by default; toggled by hmOpenMoverNews()).
       当天 ±1% 异动票 + 双语新闻(digest 匹配 / finviz 有情绪标题)。每天 4 档弹窗提醒。 -->
  <div id="hmMoverNewsView" style="display:none;flex:1;min-height:0;flex-direction:column;background:#010409;overflow:hidden">
    <div style="display:flex;gap:10px;align-items:center;padding:8px 14px;flex-wrap:wrap;border-bottom:1px solid #21262d">
      <span style="color:#e6edf3;font-size:12px;font-weight:600">📰 异动新闻</span>
      <span id="hmMvStatus" style="color:#8b949e;font-size:11px">Loading…</span>
      <span style="flex:1"></span>
      <label style="color:#8b949e;font-size:11px;cursor:pointer;user-select:none">
        <input type="checkbox" id="hmMvOnlyNews" onchange="hmMvToggleOnlyNews(this.checked)" style="vertical-align:middle"> 只看有新闻</label>
      <label style="color:#8b949e;font-size:11px;cursor:pointer;user-select:none"
             title="finviz 标题无原生 sentiment,用关键词规则判利好/利空;中性的默认隐藏">
        <input type="checkbox" id="hmMvShowNeutral" onchange="hmMvToggleNeutral(this.checked)" style="vertical-align:middle"> finviz 中性标题</label>
      <button onclick="moverAiSummary()" title="用最便宜的 Haiku 总结今天异动票的新闻(同题材多只合并); 后台跑, 可关窗口"
        style="background:#6e40c9;color:#fff;border:1px solid #8957e5;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">🤖 AI总结</button>
      <button onclick="hmRenderMoverNews(true)" title="按当前实时行情重新算异动 + 重拉新闻"
        style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">↻ 刷新</button>
    </div>
    <div id="hmMvList" style="flex:1;min-height:0;overflow-y:auto;padding:6px 12px 16px 12px"></div>
  </div>

  <!-- ⚖️ 多空 view (dataMode='narrative'): 纯 ticker 网格 → 点 ticker 内联展开多空(不跳页),
       切到别的 ticker/再点收起。详情读 /api/narrative, 网格读 /api/narrative/list。 -->
  <div id="hmNarrativeView" style="display:none;flex:1;min-height:0;flex-direction:column;background:#010409;overflow:hidden">
    <div style="display:flex;gap:10px;align-items:center;padding:8px 14px;flex-wrap:wrap;border-bottom:1px solid #21262d">
      <span style="color:#e6edf3;font-size:12px;font-weight:600">⚖️ 多空叙事</span>
      <span id="nbStatus" style="color:#8b949e;font-size:11px">Loading…</span>
      <span id="nbFilterCount" style="color:#6e7681;font-size:11px"></span>
      <span style="flex:1"></span>
      <input id="nbInput" placeholder="搜任意股票 如 RIVN(不限票池)" oninput="nbFilterGrid()"
        onkeydown="if(event.key==='Enter')nbSearch(this.value)"
        style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 8px;font-size:11px;width:170px">
      <select id="nbFilter" onchange="nbSetFilter(this.value)" title="按多空类型筛选"
        style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="all">全部</option>
        <option value="bull">▲ 纯多</option>
        <option value="bear">▼ 纯空</option>
        <option value="mix">⚖ 多空兼有</option>
        <option value="netpos">Net &gt; 0(多头占优)</option>
        <option value="netneg">Net &lt; 0(空头占优)</option>
        <option value="none">无叙事</option>
      </select>
      <select id="nbSort" onchange="nbSetSort(this.value)" title="排序"
        style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="ticker">A→Z</option>
        <option value="netdesc">Net ▲-▼ ↓</option>
        <option value="netasc">Net ▲-▼ ↑</option>
        <option value="nbdesc">▲ 多头数 ↓</option>
        <option value="nsdesc">▼ 空头数 ↓</option>
        <option value="totaldesc">总条数 ▲+▼ ↓</option>
      </select>
      <div style="display:flex;gap:0;border:1px solid #30363d;border-radius:4px;overflow:hidden">
        <button id="nbViewGridBtn" onclick="nbSetView('grid')" title="卡片网格"
          style="background:#1f6feb;color:#fff;border:none;padding:3px 8px;font-size:11px;cursor:pointer;font-weight:600">▦ 网格</button>
        <button id="nbViewTableBtn" onclick="nbSetView('table')" title="表格视图"
          style="background:#0d1117;color:#8b949e;border:none;border-left:1px solid #30363d;padding:3px 8px;font-size:11px;cursor:pointer;font-weight:600">☰ 表格</button>
      </div>
      <select id="nbWin" onchange="nbRender()"
        style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:11px">
        <option value="7">近1周</option><option value="14">近2周</option><option value="30" selected>近1月</option>
      </select>
      <button onclick="nbRenderGrid(true)" title="刷新列表"
        style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">↻</button>
    </div>
    <div id="nbScroll" style="flex:1;min-height:0;overflow-y:auto;padding:10px 12px 16px 12px">
      <!-- 搜过的票(票池外)存 localStorage; 点回去走 nbOpen 读库秒显, 不重烧 Haiku -->
      <div id="nbRecent" style="display:none;gap:5px;align-items:center;flex-wrap:wrap;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #21262d"></div>
      <!-- 输入了票池里没有的 ticker → 出提示条(否则网格筛空, 看起来像"不支持") -->
      <div id="nbHint" style="display:none;margin-bottom:8px;padding:8px 10px;background:rgba(163,113,247,.08);border:1px solid #30363d;border-radius:6px;font-size:11px;color:#8b949e"></div>
      <div id="nbGrid" style="display:flex;flex-wrap:wrap;gap:5px"></div>
    </div>
    <!-- 内联多空详情(docked 底部; 点 ticker 展开, 网格在上方保持可见便于快速切换; 切换/再点收起) -->
    <div id="nbDetail" style="display:none;flex:none;max-height:56vh;overflow-y:auto;border-top:2px solid #30363d;background:#0d1117">
        <div style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid #21262d;background:#161b22">
          <strong style="color:#e6edf3;font-size:13px">⚖️ <span id="nbSym"></span></strong>
          <div id="nbTabBar" style="display:flex;gap:2px;margin-left:8px">
            <button class="nb-tab active" data-tab="narr" onclick="nbSwitchTab('narr')" style="background:#1f6feb;color:#fff;border:none;border-radius:4px 4px 0 0;padding:3px 10px;font-size:10px;cursor:pointer;font-weight:600">⚖️多空</button>
            <button class="nb-tab" data-tab="profile" onclick="nbSwitchTab('profile')" style="background:#21262d;color:#8b949e;border:1px solid #30363d;border-bottom:none;border-radius:4px 4px 0 0;padding:3px 10px;font-size:10px;cursor:pointer;font-weight:600">🏢公司</button>
          </div>
          <span id="nbAsof" style="color:#6e7681;font-size:10px"></span>
          <span style="flex:1"></span>
          <button id="nbRefreshBtn" onclick="nbRefresh()" title="用最新新闻立刻重建这只票(约10秒)"
            style="background:rgba(63,185,80,.12);color:#3fb950;border:1px solid #30363d;border-radius:4px;padding:3px 9px;font-size:11px;cursor:pointer;font-weight:600">🔄 更新</button>
          <button onclick="nbClose()" title="收起"
            style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 9px;font-size:11px;cursor:pointer;font-weight:600">✕</button>
        </div>
        <!-- 多空 tab content -->
        <div id="nbNarrContent">
          <!-- 多周期涨跌一览 (从 /api/heatmap/stock_perf 找该票) -->
          <div id="nbPerfStrip" style="display:flex;gap:6px;padding:7px 14px;border-bottom:1px solid #21262d;background:#0f141a;font-size:11px;flex-wrap:wrap"></div>
          <!-- K 线图 + 时间段切换 (复用 _hmDrawCandles;懒加载 /api/ta/ohlc) -->
          <div style="padding:6px 14px 8px;border-bottom:1px solid #21262d;background:#0d1117">
            <div id="nbChartPeriods" style="display:flex;gap:4px;margin-bottom:4px;align-items:center">
              <span style="color:#6e7681;font-size:10px;margin-right:4px">📈 时段</span>
              <button type="button" data-d="5"   onclick="nbSetChartDays(5)"   style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:11px;padding:1px 9px;font-size:10px;cursor:pointer">1周</button>
              <button type="button" data-d="22"  onclick="nbSetChartDays(22)"  style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:11px;padding:1px 9px;font-size:10px;cursor:pointer">1月</button>
              <button type="button" data-d="66"  onclick="nbSetChartDays(66)"  style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:11px;padding:1px 9px;font-size:10px;cursor:pointer">3月</button>
              <button type="button" data-d="126" onclick="nbSetChartDays(126)" style="background:#1f6feb;color:#fff;border:1px solid #1f6feb;border-radius:11px;padding:1px 9px;font-size:10px;cursor:pointer">6月</button>
              <button type="button" data-d="252" onclick="nbSetChartDays(252)" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:11px;padding:1px 9px;font-size:10px;cursor:pointer">1年</button>
              <button type="button" data-d="504" onclick="nbSetChartDays(504)" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:11px;padding:1px 9px;font-size:10px;cursor:pointer">2年</button>
              <span id="nbChartRange" style="color:#484f58;font-size:10px;margin-left:auto"></span>
            </div>
            <canvas id="nbChart" style="width:100%;height:180px;display:block"></canvas>
          </div>
          <div id="nbNet" style="padding:8px 14px;color:#c9d1d9;font-size:12px;border-bottom:1px solid #21262d;line-height:1.5"></div>
          <div style="display:flex">
            <div id="nbBull" style="flex:1;border-right:1px solid #21262d;padding:12px 14px;background:rgba(63,185,80,0.04)"></div>
            <div id="nbBear" style="flex:1;padding:12px 14px;background:rgba(248,81,73,0.04)"></div>
          </div>
          <div style="border-top:1px solid #21262d;padding:10px 14px">
            <div style="color:#6e7681;font-size:10px;margin-bottom:4px">📎 来源新闻（绿=利好 / 红=利空 / 灰=中性·benzinga）</div>
            <div id="nbSrc"></div>
          </div>
        </div>
        <!-- 公司简介 tab content -->
        <div id="nbProfileContent" style="display:none;padding:14px 16px">
          <div id="nbProfileBody" style="color:#c9d1d9;font-size:12px;line-height:1.7"></div>
        </div>
      </div>
  </div>


  <!-- 期权波动实验 panel 暂停使用(结论已收敛, 代码保留)
  <div id="hmOptPanel" style="display:none;flex:1;min-height:0;overflow:auto;background:#010409;padding:14px"></div>
  -->

  <!-- 期权量稳定性 panel (hidden by default; shown in dataMode='optvol').
       合并 universe 单表(gold/silver 量稳定性), 点行内联展开 vol/OI/IV/PC 趋势(1M/3M). -->
  <div id="hmOptVolPanel" style="display:none;flex:1;min-height:0;overflow:auto;background:#010409;padding:14px"></div>

  <!-- 单票详情 overlay (复用 /ta/?ticker= 详情页; 点格子/行/卡片打开, 替代原 TA tab 跳转).
       iframe flex:1 单滚动, 别在外层叠同级块(journal iframe 撑高坑)。 -->
  <div id="hmDetailOverlay" style="display:none;position:absolute;inset:0;z-index:70;background:#0d1117;flex-direction:column">
    <div style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid #21262d;background:#161b22">
      <strong style="color:#e6edf3;font-size:13px">📊 <span id="hmDetailSym"></span> · 单票详情</strong>
      <span style="color:#6e7681;font-size:10px">TA + 新闻 + Finviz + SEC</span>
      <span style="flex:1"></span>
      <button onclick="hmCloseTickerDetail()"
        style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">✕ 关闭</button>
    </div>
    <iframe id="hmDetailFrame" style="flex:1;width:100%;border:none;background:#0d1117" title="单票详情"></iframe>
  </div>

</div>
"""

HEATMAP_JS = r"""
// Market Heatmap — d3 treemap, polls /api/heatmap/data, no framework
(function () {
  if (window.__hmInit) return;
  window.__hmInit = true;

  // Lazy-load d3 (~280 KB). Cached after first hit.
  let _d3Ready = null;
  function ensureD3() {
    if (window.d3) return Promise.resolve(window.d3);
    if (_d3Ready) return _d3Ready;
    _d3Ready = new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js';
      s.onload = () => res(window.d3);
      s.onerror = () => rej(new Error('d3 failed to load'));
      document.head.appendChild(s);
    });
    return _d3Ready;
  }

  window.hmState = {
    polling: false,
    pollTimer: null,
    es: null,               // EventSource for the live SSE stream
    lastData: null,
    lastFetchAt: 0,
    resizeObserver: null,
    view: 'graph',          // 'graph' (K线图墙+AI选股) — 2026-07-30 轻量化后成为唯一视图
    moverNews: null,        // 📰 /api/movers/news 缓存 payload
    moverNewsAt: 0,         // 上次拉取时间戳(ms)
    moverNewsDate: null,    // 缓存对应交易日(换天/force 重拉)
    moverNewsInflight: false,
    moverNewsOnlyNews: false,   // 「只看有新闻」(默认显示全部 ±1%,含暂无新闻)
    moverNewsShowNeutral: false, // finviz 中性标题默认隐藏(可开关)
    betaFilter: '',         // β/ρ 筛选: '' | 'spy_low_corr' | 'qqq_low_corr' | 'spy_high_beta' | 'spy_low_beta'
    mcapMin: 0,             // 市值下限(美元):0=全部 · 1e11=大盘≥100B(compact 精选)
    graphInit: false,       // 📈 Graph 视图是否已建网格(懒建)
    graphCells: null,       // {ticker -> {el, canvas, loaded, loading}} 图视图卡片索引
    graphIO: null,          // IntersectionObserver(可见才拉 OHLC)
    graphQueue: [],         // 待拉队列 + 并发节流
    graphActive: 0,
    graphCols: 'auto',      // 图墙每行张数:'auto'(自适应) | '3'|'4'|'5'|'6'
    graphAiFilter: 'all',   // 🚀 AI选股分类筛选: 'all' | 5 类叶子 | '看涨'/'看跌' 组前缀
    tableMode: 'compact',   // 'compact' | 'full' — Table view sub-mode, default compact
    tableSort: 'change_pct',// active sort column
    tableDir: -1,           // 1=asc, -1=desc — default newest movers on top
    taEarn: 'ex',           // 财报筛选: ''(全部) | 'ex'(排除临财报, 默认) | 'only'(只看临财报)
    earnDays: 28,           // 「临财报」阈值天数(用户可改); 0<=days<=earnDays 视为临近 (2026-07-30 从 14 改成 28)
    taBcTickers: null,      // 📊 Barchart 榜单交集 tickers(null=不筛; 由 hmBc* 面板写)
    dataMode: 'stocks',     // 'stocks' = 实时个股热图 · 'etf' = ETF 趋势强度(非实时)
    etfCat: 'SECTOR',       // ETF 模式分类: 'SECTOR' | 'GLOBAL'
    etfPeriod: 'r1mo',      // ETF treemap 着色周期: r3d/r7d/r1mo/r3mo/r1yr
    etfData: null,          // /api/heatmap/etf_trends 缓存
    etfSort: null,          // {key, dir} ETF 表排序
    riskData: null,         // /api/heatmap/risk_stats 缓存 (股票 β/相关/波动)
    riskEtfData: null,      // 同上, ETF 版 (?universe=etf)
    riskUniverse: 'stocks', // 'stocks' | 'etf' — β/相关 看股票还是 ETF
    riskBench: 'SPY',       // 'SPY' | 'QQQ' — 相关性/β 的基准
    riskWindow: '1y',       // '3m' | '6m' | '1y' — 统计窗口
    riskSort: null,         // {key, dir} β/相关 表排序
    riskHover: null,        // 当前 hover 的 ticker (散点高亮)
    perfData: null,         // /api/heatmap/stock_perf 缓存 (多周期涨跌, 非实时)
    perfPeriod: 'r1mo',     // r3d | r1w | r1mo | r3mo | r1yr — 蜂群图周期
    perfSort: null,         // {key, dir} 涨跌榜表排序
    perfLayout: null,       // 缓存的蜂群布局 {key,W,H,n,nodes} — 复位/resize 复用
    perfZoom: null,         // d3.zoom behavior (复位用)
    // 期权波动实验 tab 暂停使用(结论已收敛, 代码保留)
    // optData: null,          // /api/heatmap/options_lab 缓存 (期权波动实验, 盘中采集)
    // optSort: null,          // {key, dir} 期权波动表排序 (默认 iv_avg 降序)
    // optDate: null,          // 日历选中的交易日 'YYYY-MM-DD' (null = 最新)
    // optView: 'table',       // 期权波动子视图: 'table' = ATM 波动表 · 'smile' = IV 微笑/偏斜 · 'exp' = 关系/实验
    // optSmileData: null,     // /api/heatmap/options_smile 缓存 (IV 微笑/偏斜)
    // optSmileTicker: null,   // 微笑图聚焦的标的 (null = 后端选 skew 最大者)
    // optSkewSort: null,      // {key, dir} 横截面 skew 表排序 (默认 skew_25d 降序)
    // optExpData: null,       // /api/heatmap/options_experiments 缓存 (关系/实验: IV 规律)
    // optExpSort: null,       // {cardId, key, dir} 某实验卡内表格排序
    optVolData: null,       // /api/optvol/summary 缓存 (期权量稳定性单表: gold/silver 行)
    optVolByTicker: {},     // {ticker -> row} 便于查 meta
    optVolSort: { key: 'median_vol', dir: -1 },  // 默认中位量降序
    optVolExpand: { ticker: null, range: '1M', series: null, pc: null }, // 当前展开行 + 拉到的 vol/oi/iv 序列 + P/C 序列
    optVolHist: {},         // {ticker -> [{date,vol,oi,iv}]} /api/optvol/history 缓存
    optVolPcAll: null,      // /api/optvol/eod_pc 整份缓存 ({tickers:{T:{...,history:[]}}})
    optVolPcInflight: false,// eod_pc 拉取中标记 (避免并发重复拉)
    anaSearch: '',          // β相关/涨跌榜 共享: 高亮搜索词 (切视图/模式都保留)
    anaSector: '',          // 共享: 选中 sector ('' = 全部)
    anaIndustry: '',        // 共享: 选中 industry ('' = 全部)
    anaPickKey: '',         // 下拉已按哪个数据集填充过
    priceMode: 'reg',       // 'reg' = 盘中/今日收盘(vs 昨收) · 'ext' = 盘前盘后(vs 今收)
    drillSector: null,      // when set, treemap zooms into this one sector
    drillIndustry: null,    // when set (with drillSector), zooms into one industry
    autoIntervalSecs: 5,     // 2026-07-30: default 5m → 5s (用户要「实时」)
  };

  // ── Color: green ↑ red ↓, clamped to ±scale% ─────────────────────────
  function colorFor(pct, scale) {
    if (pct === null || pct === undefined || isNaN(pct)) return '#21262d';
    const x = Math.max(-1, Math.min(1, pct / scale));
    if (Math.abs(x) < 0.001) return '#30363d';
    // GitHub-style red/green
    if (x > 0) {
      // 0..1 → dark green → vivid green
      const r = Math.round(34  + (63 - 34)  * (1 - x));   // 34→63
      const g = Math.round(96  + (185 - 96) * x);          // 96→185 lighter as ↑
      const b = Math.round(34  + (80 - 34)  * (1 - x));
      return `rgb(${r},${g},${b})`;
    } else {
      const a = -x;
      const r = Math.round(96  + (248 - 96) * a);          // 96→248
      const g = Math.round(34  + (81 - 34)  * (1 - a));
      const b = Math.round(34  + (73 - 34)  * (1 - a));
      return `rgb(${r},${g},${b})`;
    }
  }

  function fmtMcap(v) {
    if (!v) return '—';
    if (v >= 1e12) return (v / 1e12).toFixed(2) + 'T';
    if (v >= 1e9)  return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6)  return (v / 1e6).toFixed(1) + 'M';
    return String(Math.round(v));
  }
  function fmtPct(p) {
    if (p === null || p === undefined || isNaN(p)) return '—';
    const s = (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
    return s;
  }
  function fmtPrice(p) {
    if (!p) return '—';
    if (p >= 1000) return p.toFixed(0);
    if (p >= 10)   return p.toFixed(2);
    return p.toFixed(2);
  }

  // ── Price mode: 盘中/今日收盘 (reg) vs 盘前盘后 (ext) ──────────────────
  // reg = today's % change vs yesterday's close (RTH last; frozen at close
  // after hours). ext = pre/post-market move vs today's close. Everything that
  // renders a % or price reads through these so one toggle flips both views.
  function _hmChg(it) {
    return window.hmState.priceMode === 'ext'
      ? (it.ext_change_pct || 0) : (it.change_pct || 0);
  }
  function _hmPx(it) {
    return window.hmState.priceMode === 'ext'
      ? (it.ext_price || it.last || 0) : (it.last || 0);
  }
  function _hmUpdatePriceModeBtns() {
    const tog = document.getElementById('hmPriceMode');
    if (!tog) return;
    tog.querySelectorAll('button').forEach(b => {
      const on = b.dataset.pm === window.hmState.priceMode;
      b.style.background = on ? '#1f6feb' : 'transparent';
      b.style.color      = on ? '#fff'    : '#8b949e';
    });
  }
  window.hmSetPriceMode = function (m) {
    window.hmState.priceMode = (m === 'ext') ? 'ext' : 'reg';
    _hmUpdatePriceModeBtns();
    render();
  };

  // ── ETF trend mode (not real-time): leaders vs laggards over 3d/7d/1mo/3mo/1yr
  const _ETF_PERIOD_LABEL = {r3d:'3天', r7d:'7天', r1mo:'1月', r3mo:'3月', r1yr:'1年'};
  const _ETF_PERIOD_LABEL_EN = {r3d:'3-day', r7d:'7-day', r1mo:'1-month', r3mo:'3-month', r1yr:'1-year'};
  function _etfScale(p) { return {r3d:3, r7d:5, r1mo:10, r3mo:20, r1yr:50}[p] || 10; }
  function _etfPct(v) { return (v === null || v === undefined || isNaN(v)) ? '—' : ((v >= 0 ? '+' : '') + v.toFixed(1) + '%'); }

  // ── 工具栏收起/展开(给热图腾地方; 收起=只留模式 tab+视图切换+刷新+本按钮, 隐藏 .hm-coll 细控件; 状态记 localStorage) ──
  window.hmToggleToolbar = function () {
    const tb = document.getElementById('hmToolbar'); if (!tb) return;
    const on = tb.classList.toggle('hm-tb-collapsed');
    const btn = document.getElementById('hmCollapseBtn');
    if (btn) btn.textContent = on ? '▾ 展开' : '▴ 收起';
    try { localStorage.setItem('hmToolbarCollapsed', on ? '1' : '0'); } catch (e) {}
    // 工具栏高度变了 → 热图容器变大/小, 重绘当前 treemap/散点(table/面板模式靠 CSS flex 自适应)
    setTimeout(function () { try { if (window.hmState && typeof render === 'function') render(); } catch (e) {} }, 60);
  };
  function _hmApplyToolbarCollapsed() {
    const tb = document.getElementById('hmToolbar'); if (!tb) return;
    let on = false;
    try { on = localStorage.getItem('hmToolbarCollapsed') === '1'; } catch (e) {}
    tb.classList.toggle('hm-tb-collapsed', on);
    const btn = document.getElementById('hmCollapseBtn');
    if (btn) btn.textContent = on ? '▾ 展开' : '▴ 收起';
  }

  window.hmSetDataMode = function (m) {
    if (window.hmCloseTickerDetail) window.hmCloseTickerDetail();   // 同 hmSetView:换模式先关详情浮层
    const st = window.hmState;
    st.dataMode = ['etf', 'risk', 'perf', 'optvol', 'movernews', 'narrative'].includes(m) ? m : 'stocks';
    const isStocks = (st.dataMode === 'stocks');
    const isOptVol = (st.dataMode === 'optvol');
    const isMoverNews = (st.dataMode === 'movernews');
    const isNarrative = (st.dataMode === 'narrative');
    // 期权量 & 异动新闻 & 多空 & SR记录 are standalone panels — hide the svg/table +
    // the view toggle, and restore them per the active view when leaving for any
    // other (svg/table-based) mode.
    const isPanelMode = isOptVol || isMoverNews || isNarrative;
    const optVolPanel = document.getElementById('hmOptVolPanel');
    const mvView = document.getElementById('hmMoverNewsView');
    const nbView = document.getElementById('hmNarrativeView');
    const vtog = document.getElementById('hmViewToggle');
    if (optVolPanel) optVolPanel.style.display = isOptVol ? 'block' : 'none';
    if (mvView) mvView.style.display = isMoverNews ? 'flex' : 'none';   // 异动新闻视图: 进它才显示, 离开必隐藏(修视图互斥 bug)
    if (nbView) nbView.style.display = isNarrative ? 'flex' : 'none';   // ⚖️ 多空视图: 同理严格互斥
    if (vtog) vtog.style.display = 'none';   // 2026-07-30 轻量化: 只剩 graph 一个 view, view toggle 永远隐藏
    // panel 模式(多空/SR记录/异动新闻/期权量):工具栏精简为单行(只留标题+模式按钮)
    var _hmStat = document.getElementById('hmStatus'),
        _hmColl = document.getElementById('hmCollapseBtn'),
        _hmRefr = document.getElementById('hmRefreshBtn');
    if (_hmStat) _hmStat.style.display = isPanelMode ? 'none' : '';
    if (_hmColl) _hmColl.style.display = isPanelMode ? 'none' : '';
    if (_hmRefr) _hmRefr.style.display = isPanelMode ? 'none' : '';
    if (isPanelMode) {
      document.getElementById('hmCanvas').style.display = 'none';
    } else {
      document.getElementById('hmCanvas').style.display = '';
    }
    // Drop any d3-zoom handlers the perf swarm bound to the shared svg, now,
    // so they can't fire on the treemap/scatter we're about to draw.
    try { if (typeof d3 !== 'undefined') d3.select('#hmSvg').on('.zoom', null); } catch (e) {}
    const sc = document.getElementById('hmStockCtrls'),
          ec = document.getElementById('hmEtfCtrls'),
          rc = document.getElementById('hmRiskCtrls'),
          pc = document.getElementById('hmPerfCtrls');
    const atToggle = document.getElementById('hmAssetToggle');
    if (atToggle) atToggle.style.display = (isStocks || st.dataMode === 'etf') ? 'inline-flex' : 'none';
    if (sc) sc.style.display = isStocks ? 'contents' : 'none';
    if (ec) ec.style.display = (st.dataMode === 'etf') ? 'inline-flex' : 'none';
    if (rc) rc.style.display = 'none';   // β/相关控件已合并为筛选器
    if (pc) pc.style.display = 'none';   // 涨跌榜控件已合并为 Table 历史模式
    // shared search + sector/industry filter — hidden (merged into stocks filters)
    const ac = document.getElementById('hmAnaCtrls');
    if (ac) ac.style.display = 'none';
    // (表格视图的三组子控件 hmTableModeToggle / hmThreshToggle / hmResortBtn 已随
    //  #hmTableView 一起删, 这里的显隐同步也不需要了)
    // 柱状图 view is ETF-only; show its toggle button only in ETF mode.
    const bb = document.getElementById('hmBarViewBtn');
    if (bb) bb.style.display = (st.dataMode === 'etf') ? '' : 'none';
    // Only ETF has a bar view — if we were on it elsewhere, fall back.
    if (st.dataMode !== 'etf' && st.view === 'bar') hmSetView('treemap');
    // 📈 Graph view(K线图墙)是个股专属;只在 stocks 模式显示其按钮。
    const gb = document.getElementById('hmGraphViewBtn');
    if (gb) gb.style.display = isStocks ? '' : 'none';
    if (!isStocks && st.view === 'graph') hmSetView('treemap');
    // Relabel the primary view button per mode: treemap / scatter / beeswarm.
    const tmBtn = document.querySelector('#hmViewToggle button[data-view="treemap"]');
    if (tmBtn) tmBtn.innerHTML = (st.dataMode === 'risk') ? '🟢 散点图'
      : (st.dataMode === 'perf') ? '🐝 蜂群图' : '🟩 Treemap';
    // ETF/risk/perf are now sub-modes of stocks — highlight 股票 button for all of them
    const dmForBtn = ['etf','risk','perf'].includes(st.dataMode) ? 'stocks' : st.dataMode;
    document.querySelectorAll('#hmDataMode button').forEach(b => {
      if (b.style.display === 'none') return;
      const on = b.dataset.dm === dmForBtn;
      b.style.background = on ? '#1f6feb' : 'transparent';
      b.style.color = on ? '#fff' : '#8b949e';
    });
    // Asset type toggle (股票/ETF) highlighting
    const atType = (st.dataMode === 'etf') ? 'etf' : 'stocks';
    document.querySelectorAll('#hmAssetToggle button').forEach(b => {
      const on = b.dataset.at === atType;
      b.style.background = on ? '#1f6feb' : 'transparent';
      b.style.color = on ? '#fff' : '#8b949e';
    });
    if (isOptVol) {
      const cr = document.getElementById('hmCrumb'); if (cr) cr.style.display = 'none';
      hmStopStream(); stopPolling();          // 期权量稳定性是非实时 HTML 面板
      if (!st.optVolData) { _hmLoadOptVol(); return; }
      _hmRenderOptVolTable(); return;
    }
    if (isMoverNews) {
      const cr = document.getElementById('hmCrumb'); if (cr) cr.style.display = 'none';
      hmStopStream(); stopPolling();          // 异动新闻是独立面板(自带刷新按钮)
      // 保险: 即便上面 graph→treemap 复位把 canvas 显示回来, 这里强制只留异动新闻视图
      document.getElementById('hmCanvas').style.display = 'none';
      const gv = document.getElementById('hmGraphView'); if (gv) gv.style.display = 'none';
      if (mvView) mvView.style.display = 'flex';
      hmRenderMoverNews();   // 用缓存(同日不重拉); 面板内「↻ 刷新」才强制
      return;
    }
    if (isNarrative) {
      const cr = document.getElementById('hmCrumb'); if (cr) cr.style.display = 'none';
      hmStopStream(); stopPolling();          // ⚖️ 多空是独立面板(读库, 非实时)
      document.getElementById('hmCanvas').style.display = 'none';
      const gv2 = document.getElementById('hmGraphView'); if (gv2) gv2.style.display = 'none';
      if (nbView) nbView.style.display = 'flex';
      nbRenderGrid();        // ticker 网格(缓存); 顶栏「↻」强制刷新列表
      return;
    }
    if (isStocks) {
      hmStartStream(); startPolling();
      _hmEnsureScores();        // 预载 TA 行(🔎 全局筛选 / graph 共用), 非阻塞
      hmLoadEodStatus();        // EOD 爬取状态条
    } else {
      const cr = document.getElementById('hmCrumb'); if (cr) cr.style.display = 'none';
      hmStopStream(); stopPolling();   // ETF / risk / perf views are not real-time
      if (st.dataMode === 'etf' && !st.etfData) { _hmLoadEtf(); return; }
      if (st.dataMode === 'risk' && !_riskActiveData()) { _hmLoadRisk(); return; }
      if (st.dataMode === 'perf' && !st.perfData) { _hmLoadPerf(); return; }
    }
    render();
  };
  window.hmSetEtfCat = function (c) {
    window.hmState.etfCat = (c === 'GLOBAL') ? 'GLOBAL' : 'SECTOR';
    window.hmState.etfSort = null;
    document.querySelectorAll('#hmEtfCat button').forEach(b => {
      const on = b.dataset.cat === window.hmState.etfCat;
      b.style.background = on ? '#1f6feb' : 'transparent';
      b.style.color = on ? '#fff' : '#8b949e';
    });
    render();
  };
  window.hmSetEtfPeriod = function (p) {
    window.hmState.etfPeriod = p;
    render();
  };
  function _hmLoadEtf() {
    const status = document.getElementById('hmStatus');
    if (status) status.textContent = '加载 ETF 趋势中…';
    fetch('/api/heatmap/etf_trends').then(r => r.json()).then(d => {
      window.hmState.etfData = d;
      if (window.hmState.dataMode === 'etf') {
        render();
      }
    }).catch(e => { if (status) status.innerHTML = '<span style="color:#f85149">ETF 加载失败: ' + e.message + '</span>'; });
  }

  // ── 期权波动实验 (options_lab) 暂停使用(结论已收敛, 代码保留) ─────────
  // 恢复时: 解注释下方整块 + 按钮 + panel div + state vars + API routes + launchd daemon
  /*
  function _hmLoadOpt(date) {
    const panel = document.getElementById('hmOptPanel');
    if (panel && !window.hmState.optData) {
      panel.innerHTML = '<div style="color:#8b949e;font-size:13px;padding:20px">加载期权波动实验中…</div>';
    }
    const url = '/api/heatmap/options_lab' + (date ? ('?date=' + encodeURIComponent(date)) : '');
    fetch(url).then(r => r.json()).then(d => {
      window.hmState.optData = d;
      window.hmState.optDate = d.trading_date || date || null;
      if (window.hmState.dataMode === 'opt') _hmRenderOptTable();
    }).catch(e => {
      if (panel) panel.innerHTML = '<div style="color:#f85149;font-size:13px;padding:20px">期权波动加载失败: ' + e.message + '</div>';
    });
  }
  // 日历点选某交易日 (全局, 供 date input onchange / chip onclick 调用)
  window.hmOptPickDate = function (date) {
    if (!date) return;
    window.hmState.optDate = date;
    window.hmState.optSmileData = null;     // 微笑也是按交易日, 换日清缓存
    window.hmState.optExpData = null;       // 关系/实验也按交易日, 换日清缓存
    _hmLoadOpt(date);
    if (window.hmState.optView === 'smile') _hmLoadSmile(undefined, date);
    if (window.hmState.optView === 'exp') _hmLoadExp(date);
  };
  window.hmOptSort = function (key) {
    const s = window.hmState.optSort;
    const txt = (key === 'ticker' || key === 'expiration');
    window.hmState.optSort = { key, dir: (s && s.key === key) ? -s.dir : (txt ? 1 : -1) };
    _hmRenderOptTable();
  };
  // 期权波动子视图切换: 表格 (ATM 波动) ↔ 微笑偏斜 (IV smile/skew)
  window.hmOptSetView = function (v) {
    if (window.hmState.optView === v) return;
    window.hmState.optView = v;
    _hmRenderOptTable();    // 先重绘 (切按钮高亮 + 子视图 body 占位), 再按需懒加载
    if (v === 'smile' && !window.hmState.optSmileData) _hmLoadSmile();
    if (v === 'exp' && !window.hmState.optExpData) _hmLoadExp();
  };
  // 小数 → 百分比文本 (IV/价差/跨式占比 后端按小数存, 展示 ×100)
  function _optPct(v, dp) {
    if (v == null || isNaN(v)) return '—';
    return (v * 100).toFixed(dp == null ? 1 : dp) + '%';
  }
  function _optNum(v, dp) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toFixed(dp == null ? 2 : dp);
  }
  // 盘中 IV/价差 时间小图 (mini bars)。数据缺失/出错时返回 '' (跳过, 不崩)。
  function _hmOptIntradayChart(intr) {
    try {
      if (!intr || !intr.buckets || !intr.buckets.length) return '';
      const bars = (label, vals, color, asPct, dp) => {
        const nums = (vals || []).map(v => (v == null || isNaN(v)) ? null : Number(v));
        const valid = nums.filter(v => v != null);
        if (!valid.length) return '';
        const mx = Math.max.apply(null, valid), mn = Math.min.apply(null, valid);
        const span = (mx - mn) || 1;
        const cells = intr.buckets.map((b, i) => {
          const v = nums[i];
          const h = (v == null) ? 0 : Math.round(6 + 30 * (v - mn) / span);
          const disp = (v == null) ? '—' : (asPct ? (v * 100).toFixed(dp) + '%' : v.toFixed(dp));
          return '<div title="' + hmEsc(b) + ' · ' + disp + '" style="flex:1;min-width:5px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:2px">' +
            '<span style="width:70%;height:' + h + 'px;background:' + color + ';border-radius:2px 2px 0 0"></span>' +
            '<span style="font-size:7px;color:#6e7681;writing-mode:vertical-rl;line-height:1">' + hmEsc(b) + '</span>' +
          '</div>';
        }).join('');
        return '<div style="flex:1;min-width:240px"><div style="font-size:11px;color:#8b949e;margin-bottom:4px">' + label +
          ' <span style="color:#6e7681">(低 ' + (asPct ? (mn * 100).toFixed(dp) + '%' : mn.toFixed(dp)) + ' · 高 ' + (asPct ? (mx * 100).toFixed(dp) + '%' : mx.toFixed(dp)) + ')</span></div>' +
          '<div style="display:flex;align-items:flex-end;gap:2px;height:54px">' + cells + '</div></div>';
      };
      const ivBars = bars('盘中平均 IV (越低买越便宜)', intr.iv_today, '#58a6ff', true, 1);
      const spBars = bars('盘中平均 买卖价差% (越低越好成交)', intr.spread_today, '#d29922', true, 1);
      if (!ivBars && !spBars) return '';
      return '<div style="display:flex;gap:18px;flex-wrap:wrap;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 14px;margin-bottom:12px">' + ivBars + spBars + '</div>';
    } catch (e) { return ''; }
  }
  function _hmRenderOptTable() {
    const panel = document.getElementById('hmOptPanel');
    if (!panel) return;
    const d = window.hmState.optData;
    if (!d) { _hmLoadOpt(); return; }
    const conc = d.conclusions || {};
    const items = (d.latest && d.latest.items) ? d.latest.items.slice() : [];
    const meta = d.meta || {};
    // ── 结论框 (绿=最便宜买点 · 琥珀=最好成交 · 红/绿 = 最贵/最便宜榜) ──
    const box = (accent, title, body) =>
      '<div style="background:' + accent + '14;border:1px solid ' + accent + '55;border-radius:8px;padding:10px 14px">' +
        '<div style="font-size:11px;font-weight:700;color:' + accent + ';margin-bottom:4px">' + title + '</div>' +
        '<div style="font-size:13px;color:#e6edf3;line-height:1.6">' + (body || '<span style="color:#6e7681">数据积累中…</span>') + '</div>' +
      '</div>';
    const listBox = (accent, title, arr) => {
      const lis = (arr && arr.length)
        ? arr.map(s => '<div style="font-size:12px;color:#e6edf3;padding:2px 0;font-variant-numeric:tabular-nums">' + hmEsc(s) + '</div>').join('')
        : '<div style="font-size:12px;color:#6e7681">数据积累中…</div>';
      return '<div style="flex:1;min-width:220px;background:' + accent + '12;border:1px solid ' + accent + '44;border-radius:8px;padding:10px 14px">' +
        '<div style="font-size:11px;font-weight:700;color:' + accent + ';margin-bottom:6px">' + title + '</div>' + lis + '</div>';
    };
    const conclusions =
      '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px">' +
        box('#3fb950', '🟢 啥时候买最便宜 (IV 最低)', conc.cheapest_iv_window) +
        box('#d29922', '🎯 啥时候最好成交 (买卖价差最窄)', conc.best_fill_window) +
      '</div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">' +
        listBox('#f85149', '🔴 最贵 Top 5 (IV / 跨式占比)', conc.most_expensive) +
        listBox('#3fb950', '🟢 最便宜 Top 5 (IV / 跨式占比)', conc.cheapest) +
      '</div>' +
      (conc.summary ? '<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px 14px;font-size:13px;color:#c9d1d9;line-height:1.65;margin-bottom:12px">' + hmEsc(conc.summary) + '</div>' : '');
    // ── 排序 ──
    const sort = window.hmState.optSort || { key: 'iv_avg', dir: -1 };
    items.sort((a, b) => {
      if (sort.key === 'ticker' || sort.key === 'expiration')
        return sort.dir * String(a[sort.key] || '').localeCompare(String(b[sort.key] || ''));
      let va = a[sort.key], vb = b[sort.key];
      va = (va == null || isNaN(va)) ? -1e9 : va; vb = (vb == null || isNaN(vb)) ? -1e9 : vb;
      return sort.dir * (va - vb);
    });
    // ── 可排序表 (sticky 表头, 点列头排序) ──
    const cols = [['代码', 'ticker'], ['现价', 'spot'], ['到期', 'expiration'], ['DTE', 'dte'], ['ATM', 'atm_strike'], ['Call中价', 'call_mid'], ['Put中价', 'put_mid'], ['跨式', 'straddle_mid'], ['IV', 'iv_avg'], ['价差%', 'avg_spread_pct'], ['跨式/现价%', 'straddle_pct_spot'], ['IV分位', 'iv_percentile']];
    const arrow = k => sort.key === k ? (sort.dir > 0 ? ' ▲' : ' ▼') : '';
    const left = k => (k === 'ticker' || k === 'expiration');
    const th = cols.map(c => '<th onclick="hmOptSort(\'' + c[1] + '\')" style="cursor:pointer;user-select:none;position:sticky;top:0;background:#161b22;color:#8b949e;padding:6px 10px;border-bottom:1px solid #30363d;text-align:' + (left(c[1]) ? 'left' : 'right') + '">' + c[0] + arrow(c[1]) + '</th>').join('');
    const cell = (it, k) => {
      if (k === 'ticker') return hmTk(it.ticker);
      if (k === 'expiration') return '<span style="color:#8b949e">' + hmEsc(it.expiration || '') + '</span>';
      if (k === 'spot' || k === 'atm_strike' || k === 'call_mid' || k === 'put_mid' || k === 'straddle_mid') return _optNum(it[k], 2);
      if (k === 'dte') return (it.dte == null) ? '—' : String(it.dte);
      if (k === 'iv_avg') return (it.iv_avg == null || isNaN(it.iv_avg)) ? '<span style="color:#6e7681">—</span>' : '<span style="color:#58a6ff;font-weight:700">' + _optPct(it.iv_avg, 1) + '</span>';
      if (k === 'avg_spread_pct') return _optPct(it.avg_spread_pct, 1);
      if (k === 'straddle_pct_spot') return _optPct(it.straddle_pct_spot, 1);
      if (k === 'iv_percentile') return (it.iv_percentile == null) ? '<span style="color:#6e7681">—</span>' : '<span style="font-weight:600">' + Math.round(it.iv_percentile) + '</span>';
      return '—';
    };
    const trs = items.map(it =>
      '<tr>' + cols.map(c => '<td style="padding:4px 10px;border-bottom:1px solid #161b22;text-align:' + (left(c[1]) ? 'left' : 'right') + ';color:#e6edf3">' + cell(it, c[1]) + '</td>').join('') + '</tr>').join('');
    const table = items.length
      ? '<div style="width:100%;overflow:auto"><table style="width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums"><thead><tr>' + th + '</tr></thead><tbody>' + trs + '</tbody></table></div>'
      : '<div style="padding:20px;color:#8b949e">暂无标的数据 (' + hmEsc(meta.note || '数据积累中…') + ')</div>';
    // ── 日历: 原生 date 选择器 + 最近交易日快捷 chips ──
    const adates = d.available_dates || [];
    const curDate = d.trading_date || '';
    const recent = adates.slice().reverse().slice(0, 14);
    const dchip = dt => {
      const on = dt === curDate;
      return '<span onclick="hmOptPickDate(\'' + dt + '\')" style="cursor:pointer;font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid ' + (on ? '#1f6feb' : '#30363d') + ';background:' + (on ? '#1f6feb' : 'transparent') + ';color:' + (on ? '#fff' : '#8b949e') + '">' + hmEsc(dt.slice(5)) + '</span>';
    };
    const dinput = '<input type="date" id="hmOptDate"' + (curDate ? (' value="' + curDate + '"') : '') +
      (adates.length ? (' min="' + adates[0] + '" max="' + adates[adates.length - 1] + '"') : '') +
      ' onchange="hmOptPickDate(this.value)" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 8px;font-size:12px;color-scheme:dark">';
    const dateRow = '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:2px 0 8px">' +
      '<span style="font-size:11px;color:#8b949e">📅 交易日:</span>' + dinput +
      (adates.length ? recent.map(dchip).join('') : '<span style="font-size:11px;color:#6e7681">明天开盘后开始采集, 采到的交易日会出现在这里</span>') +
      (adates.length > 14 ? '<span style="font-size:10px;color:#6e7681">共 ' + adates.length + ' 天, 上面 date 框可选更早</span>' : '') +
    '</div>';
    // ── 状态行 + 说明 ──
    const statusLine = '更新: ' + hmEsc(d.generated_at || '—') + ' · ' + hmEsc(d.trading_date || '') +
      ' · 当日采集 ' + (d.snapshot_count_today != null ? d.snapshot_count_today : 0) + ' 次' +
      ' · 历史 ' + (d.days_of_history != null ? d.days_of_history : 0) + ' 天' +
      ' · 每 ' + (d.cadence_min != null ? d.cadence_min : 15) + ' 分钟一采' +
      ' · ' + (d.universe_size != null ? d.universe_size : items.length) + ' 只标的' +
      (meta.note ? ' · ' + hmEsc(meta.note) : '');
    // ── 子视图切换: 表格 (ATM 波动) | 微笑偏斜 (IV smile/skew) ──
    const subToggle = _hmOptSubToggle();
    // 微笑偏斜 / 关系实验 子视图: 整段各自渲染 (自带结论/图/表/盘中)
    const body = (window.hmState.optView === 'smile')
      ? _hmRenderSmileBody()
      : (window.hmState.optView === 'exp')
      ? _hmRenderExpBody()
      : (conclusions + _hmOptIntradayChart(d.intraday) + table);
    panel.innerHTML =
      '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px">' +
        '<div style="font-size:16px;font-weight:800;color:#e6edf3">💰 期权波动实验</div>' +
        '<button onclick="hmRefresh()" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">↻ 刷新</button>' +
        subToggle +
      '</div>' +
      '<div style="font-size:11px;color:#6e7681;margin:2px 0 6px;line-height:1.6">实验性: 盘中按节奏给 top市值标的的 ATM 月期权拍快照, 算 IV / 跨式(call中价+put中价) / 买卖价差%, 答两件事 — <b style="color:#3fb950">啥时候买便宜</b> (IV 低) + <b style="color:#d29922">啥时候好成交</b> (价差窄)。IV/价差按小数存, 表里已 ×100。样本不足时结论保守, 别当信号源, 仅作择时参考。</div>' +
      dateRow +
      '<div style="font-size:11px;color:#6e7681;margin-bottom:10px">' + statusLine + '</div>' +
      body;
    if (window.hmState.optView === 'smile') _hmDrawSmileCanvas();
    if (window.hmState.optView === 'exp') _hmDrawExpCanvases();
    const status = document.getElementById('hmStatus');
    if (status) status.textContent = '期权波动实验 · ' + items.length + ' 只 · 今日采集 ' + (d.snapshot_count_today || 0) + ' 次 (非实时)';
  }

  // ════════════════════════════════════════════════════════════════════════
  // IV 微笑/偏斜 (options_smile): 结论 + 微笑曲线 (canvas) + 横截面 skew 表 + 盘中
  // 同属 期权波动 面板的子视图。GET /api/heatmap/options_smile?date=&ticker= 懒加载。
  // 契约: smile{points[],atm_iv,skew_25d…} / cross{items[]} / intraday / conclusions。
  // ════════════════════════════════════════════════════════════════════════
  function _hmOptSubToggle() {
    const v = window.hmState.optView || 'table';
    const btn = (key, label) => {
      const on = v === key;
      return '<button onclick="hmOptSetView(\'' + key + '\')" style="background:' + (on ? '#1f6feb' : 'transparent') +
        ';color:' + (on ? '#fff' : '#8b949e') + ';border:1px solid ' + (on ? '#1f6feb' : '#30363d') +
        ';border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600">' + label + '</button>';
    };
    return '<div style="display:inline-flex;gap:6px;margin-left:auto">' +
      btn('table', '📊 ATM 波动表') + btn('smile', '😀 IV 微笑/偏斜') + btn('exp', '🔬 关系/实验') + '</div>';
  }
  // skew (IV 点) 文本: 0.04 → "+4.0pt"。带符号; 缺失 → '—'。
  function _optSkewPt(v, dp) {
    if (v == null || isNaN(v)) return '—';
    const x = Number(v) * 100;
    return (x >= 0 ? '+' : '') + x.toFixed(dp == null ? 1 : dp) + 'pt';
  }
  function _hmLoadSmile(ticker, date) {
    const st = window.hmState;
    if (ticker !== undefined) st.optSmileTicker = ticker || null;
    const dt = (date !== undefined) ? date : (st.optDate || null);
    const panel = document.getElementById('hmOptPanel');
    // 仅当微笑视图当前可见且还没数据时, 给个占位 (不抢表格视图的渲染)
    const smileBody = document.getElementById('hmSmileBody');
    if (smileBody && !st.optSmileData) {
      smileBody.innerHTML = '<div style="color:#8b949e;font-size:13px;padding:20px">加载 IV 微笑/偏斜中…</div>';
    }
    const qs = [];
    if (dt) qs.push('date=' + encodeURIComponent(dt));
    if (st.optSmileTicker) qs.push('ticker=' + encodeURIComponent(st.optSmileTicker));
    const url = '/api/heatmap/options_smile' + (qs.length ? ('?' + qs.join('&')) : '');
    fetch(url).then(r => r.json()).then(d => {
      st.optSmileData = d;
      if (d && d.focus_ticker) st.optSmileTicker = d.focus_ticker;
      if (st.dataMode === 'opt' && st.optView === 'smile') _hmRenderOptTable();
    }).catch(e => {
      const sb = document.getElementById('hmSmileBody');
      if (sb) sb.innerHTML = '<div style="color:#f85149;font-size:13px;padding:20px">IV 微笑加载失败: ' + hmEsc(e.message) + '</div>';
    });
  }
  // 全局: 切换微笑聚焦标的 (点横截面表某行 / 下拉选择都走这里), 重新 ?ticker= 拉取
  window.hmSmileFocus = function (ticker) {
    if (!ticker) return;
    window.hmState.optSmileData = null;     // 换 focus → 重拉 (后端按 ticker 重算 smile)
    _hmLoadSmile(ticker);
  };
  window.hmSmileSort = function (key) {
    const s = window.hmState.optSkewSort;
    const txt = (key === 'ticker' || key === 'name' || key === 'sector');
    window.hmState.optSkewSort = { key, dir: (s && s.key === key) ? -s.dir : (txt ? 1 : -1) };
    _hmRenderOptTable();
  };
  // 微笑视图整段 body (结论 + 标的选择 + 微笑画布 + 横截面表 + 盘中 skew)
  function _hmRenderSmileBody() {
    const sd = window.hmState.optSmileData;
    if (!sd) { return '<div id="hmSmileBody"><div style="color:#8b949e;font-size:13px;padding:20px">加载 IV 微笑/偏斜中…</div></div>'; }
    const help = '<div style="font-size:11px;color:#6e7681;margin-bottom:10px;line-height:1.6">' +
      '25Δ 偏斜 = IV(25Δ put) − IV(25Δ call)。<b style="color:#f0883e">>0 = 看跌保护更贵 (下行偏斜, 市场怕跌)</b>; ' +
      '<b style="color:#3fb950">&lt;0 = 看涨更贵 (上行偏斜)</b>。蝶式>0 = 两翼相对 ATM 上扬 (尾部更贵)。IV 按小数存, 显示 ×100 波动点。</div>';
    return '<div id="hmSmileBody">' + help +
      _hmSmileConclusions(sd.conclusions || {}) +
      _hmSmileChartShell(sd.smile || {}, (sd.cross || {}).items || []) +
      _hmSkewTable((sd.cross || {}).items || [], (sd.meta || {})) +
      _hmSmileIntradayChart(sd.intraday || {}) +
    '</div>';
  }
  // ── 结论框: skew_overall / intraday_skew_pattern / summary + 最陡/最平 Top5 ──
  function _hmSmileConclusions(conc) {
    const box = (accent, title, body) =>
      '<div style="background:' + accent + '14;border:1px solid ' + accent + '55;border-radius:8px;padding:10px 14px">' +
        '<div style="font-size:11px;font-weight:700;color:' + accent + ';margin-bottom:4px">' + title + '</div>' +
        '<div style="font-size:13px;color:#e6edf3;line-height:1.6">' + (body ? hmEsc(body) : '<span style="color:#6e7681">微笑数据积累中…</span>') + '</div>' +
      '</div>';
    const listBox = (accent, title, arr) => {
      const lis = (arr && arr.length)
        ? arr.map(s => '<div style="font-size:12px;color:#e6edf3;padding:2px 0;font-variant-numeric:tabular-nums">' + hmEsc(s) + '</div>').join('')
        : '<div style="font-size:12px;color:#6e7681">微笑数据积累中…</div>';
      return '<div style="flex:1;min-width:220px;background:' + accent + '12;border:1px solid ' + accent + '44;border-radius:8px;padding:10px 14px">' +
        '<div style="font-size:11px;font-weight:700;color:' + accent + ';margin-bottom:6px">' + title + '</div>' + lis + '</div>';
    };
    return '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px">' +
        box('#f0883e', '📐 全市场偏斜', conc.skew_overall) +
        box('#58a6ff', '🕒 盘中偏斜规律', conc.intraday_skew_pattern) +
      '</div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">' +
        listBox('#f85149', '🔴 最陡偏斜 Top 5 (最怕跌)', conc.steepest_skew) +
        listBox('#3fb950', '🟢 最平偏斜 Top 5', conc.flattest_skew) +
      '</div>' +
      (conc.summary ? '<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px 14px;font-size:13px;color:#c9d1d9;line-height:1.65;margin-bottom:12px">' + hmEsc(conc.summary) + '</div>' : '');
  }
  // ── 微笑图外壳: 标题 + 标的下拉 + <canvas> (实际曲线由 _hmDrawSmileCanvas 画) ──
  function _hmSmileChartShell(smile, crossItems) {
    const pts = (smile && smile.points) ? smile.points : [];
    // 标的下拉 (横截面里所有票) — 选中即 hmSmileFocus 重拉
    const cur = (smile && smile.ticker) || window.hmState.optSmileTicker || '';
    const opts = (crossItems || []).map(it => {
      const tk = it.ticker || '';
      return '<option value="' + hmEsc(tk) + '"' + (tk === cur ? ' selected' : '') + '>' + hmEsc(tk) + (it.name ? (' · ' + hmEsc(it.name)) : '') + '</option>';
    }).join('');
    const sel = '<select id="hmSmileTk" onchange="hmSmileFocus(this.value)" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 8px;font-size:12px;color-scheme:dark;max-width:240px">' +
      (cur && !crossItems.some(it => it.ticker === cur) ? '<option value="' + hmEsc(cur) + '" selected>' + hmEsc(cur) + '</option>' : '') +
      '<option value="">— 选标的 —</option>' + opts + '</select>';
    const title = (smile && smile.ticker)
      ? hmTk(smile.ticker, ';font-size:14px') +
        (smile.name ? ' <span style="color:#8b949e;font-size:11px">' + hmEsc(smile.name) + '</span>' : '') +
        ' <span style="color:#6e7681;font-size:11px">到期 ' + hmEsc(smile.expiration || '—') + ' · DTE ' + (smile.dte != null ? smile.dte : '—') +
        ' · 现价 ' + _optNum(smile.spot, 2) + ' · ATM IV ' + _optPct(smile.atm_iv, 1) +
        ' · 25Δ偏斜 ' + _optSkewPt(smile.skew_25d, 1) +
        (smile.asof_hhmm ? (' · ' + hmEsc(smile.asof_hhmm)) : '') + '</span>'
      : '<span style="color:#8b949e;font-size:13px">微笑数据积累中…</span>';
    // canvas: 曲线由 _hmDrawSmileCanvas 直接从 window.hmState.optSmileData.smile 读取绘制。
    // (不走 data 属性 —— JSON 里的双引号会把 HTML 属性在第一个 " 处截断)
    const legend = '<div style="display:flex;gap:14px;font-size:10px;color:#8b949e;margin-top:4px">' +
      '<span><span style="display:inline-block;width:10px;height:3px;background:#f85149;vertical-align:middle;margin-right:3px"></span>Put 翼 (K&lt;现价)</span>' +
      '<span><span style="display:inline-block;width:10px;height:3px;background:#3fb950;vertical-align:middle;margin-right:3px"></span>Call 翼 (K&gt;现价)</span>' +
      '<span><span style="display:inline-block;width:10px;height:3px;background:#58a6ff;vertical-align:middle;margin-right:3px"></span>代表 IV</span>' +
      '<span><span style="color:#d29922">┊</span> ATM (moneyness=1)</span></div>';
    const canvasArea = pts.length
      ? '<canvas id="hmSmileCanvas" width="720" height="280" style="width:100%;max-width:760px;height:280px;display:block;margin-top:8px"></canvas>' + legend
      : '<div style="padding:24px;color:#6e7681;font-size:13px">微笑数据积累中… (该标的有效 strike &lt; 5 或暂无快照)</div>';
    return '<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 14px;margin-bottom:12px">' +
      '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">' +
        '<span style="font-size:11px;color:#8b949e">聚焦标的:</span>' + sel +
        '<div style="flex:1;min-width:200px">' + title + '</div>' +
      '</div>' + canvasArea + '</div>';
  }
  // canvas 实绘: IV(×100, y) vs moneyness(x)。put_iv(K<spot)红, call_iv(K>spot)绿,
  // 代表 iv 蓝细线打底; ATM 竖线琥珀。纯 2D context, 无 d3 (preview 沙箱屏蔽 CDN)。
  function _hmDrawSmileCanvas() {
    try {
      const cv = document.getElementById('hmSmileCanvas');
      if (!cv || !cv.getContext) return;
      const sd = (window.hmState.optSmileData && window.hmState.optSmileData.smile) || {};
      const pts = (sd.points || []).map(p => ({ m: p.moneyness, iv: p.iv, c: p.call_iv, p: p.put_iv }))
        .filter(p => p && p.m != null && (p.iv != null || p.c != null || p.p != null));
      if (pts.length < 2) return;
      const ctx = cv.getContext('2d');
      const W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      const padL = 46, padR = 12, padT = 14, padB = 26;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      // x 域: moneyness; y 域: IV×100
      const ms = pts.map(p => p.m);
      let xmin = Math.min.apply(null, ms), xmax = Math.max.apply(null, ms);
      if (xmax - xmin < 0.02) { xmin -= 0.05; xmax += 0.05; }
      const ivAll = [];
      pts.forEach(p => { [p.iv, p.c, p.p].forEach(v => { if (v != null && !isNaN(v)) ivAll.push(v * 100); }); });
      let ymin = Math.min.apply(null, ivAll), ymax = Math.max.apply(null, ivAll);
      const ypad = (ymax - ymin) * 0.12 || 2; ymin = Math.max(0, ymin - ypad); ymax = ymax + ypad;
      const X = m => padL + (m - xmin) / (xmax - xmin) * plotW;
      const Y = iv => padT + (1 - (iv - ymin) / (ymax - ymin)) * plotH;
      // 网格 + y 轴刻度 (IV%)
      ctx.strokeStyle = '#21262d'; ctx.fillStyle = '#6e7681'; ctx.font = '10px -apple-system,sans-serif';
      ctx.lineWidth = 1; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      for (let i = 0; i <= 4; i++) {
        const iv = ymin + (ymax - ymin) * i / 4, y = Y(iv);
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
        ctx.fillText(iv.toFixed(0) + '%', padL - 5, y);
      }
      // x 轴刻度 (moneyness)
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      for (let i = 0; i <= 4; i++) {
        const m = xmin + (xmax - xmin) * i / 4;
        ctx.fillText(m.toFixed(2), X(m), H - padB + 5);
      }
      // ATM 竖线 (moneyness = 1)
      if (xmin <= 1 && xmax >= 1) {
        ctx.strokeStyle = '#d29922'; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.moveTo(X(1), padT); ctx.lineTo(X(1), H - padB); ctx.stroke();
        ctx.setLineDash([]);
      }
      const line = (key, color, w) => {
        const seg = pts.filter(p => p[key] != null && !isNaN(p[key])).sort((a, b) => a.m - b.m);
        if (seg.length < 2) return;
        ctx.strokeStyle = color; ctx.lineWidth = w; ctx.beginPath();
        seg.forEach((p, i) => { const x = X(p.m), y = Y(p[key] * 100); if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
        ctx.stroke();
        ctx.fillStyle = color;
        seg.forEach(p => { ctx.beginPath(); ctx.arc(X(p.m), Y(p[key] * 100), 2.2, 0, Math.PI * 2); ctx.fill(); });
      };
      // 代表 iv 蓝细线打底, 再叠 put 翼(红) / call 翼(绿)
      line('iv', '#58a6ff', 1.4);
      // put 翼: 只画 K<spot (moneyness<1) 的 put_iv
      const putSeg = pts.filter(p => p.m <= 1 && p.p != null && !isNaN(p.p));
      const callSeg = pts.filter(p => p.m >= 1 && p.c != null && !isNaN(p.c));
      const wing = (seg, key, color) => {
        if (seg.length < 2) { // 翼太短就用点标
          ctx.fillStyle = color; seg.forEach(p => { ctx.beginPath(); ctx.arc(X(p.m), Y(p[key] * 100), 2.6, 0, Math.PI * 2); ctx.fill(); });
          return;
        }
        ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
        seg.sort((a, b) => a.m - b.m).forEach((p, i) => { const x = X(p.m), y = Y(p[key] * 100); if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
        ctx.stroke();
        ctx.fillStyle = color; seg.forEach(p => { ctx.beginPath(); ctx.arc(X(p.m), Y(p[key] * 100), 2.6, 0, Math.PI * 2); ctx.fill(); });
      };
      wing(putSeg, 'p', '#f85149');
      wing(callSeg, 'c', '#3fb950');
    } catch (e) { } // 画图失败不崩面板
  }
  // ── 横截面 skew 表 (可排序, 点行设为微笑 focus) ──
  function _hmSkewTable(items, meta) {
    items = (items || []).slice();
    const sort = window.hmState.optSkewSort || { key: 'skew_25d', dir: -1 };
    items.sort((a, b) => {
      if (sort.key === 'ticker' || sort.key === 'name' || sort.key === 'sector')
        return sort.dir * String(a[sort.key] || '').localeCompare(String(b[sort.key] || ''));
      let va = a[sort.key], vb = b[sort.key];
      va = (va == null || isNaN(va)) ? -1e9 : va; vb = (vb == null || isNaN(vb)) ? -1e9 : vb;
      return sort.dir * (va - vb);
    });
    const cols = [['代码', 'ticker'], ['板块', 'sector'], ['现价', 'spot'], ['ATM IV%', 'atm_iv'], ['25Δ偏斜(pt)', 'skew_25d'], ['偏斜%', 'skew_pct'], ['蝶式(pt)', 'butterfly_25d'], ['Put-Call价差(pt)', 'put_call_iv_spread'], ['有效strike数', 'n_strikes']];
    const arrow = k => sort.key === k ? (sort.dir > 0 ? ' ▲' : ' ▼') : '';
    const left = k => (k === 'ticker' || k === 'sector');
    const th = cols.map(c => '<th onclick="hmSmileSort(\'' + c[1] + '\')" style="cursor:pointer;user-select:none;position:sticky;top:0;background:#161b22;color:#8b949e;padding:6px 10px;border-bottom:1px solid #30363d;text-align:' + (left(c[1]) ? 'left' : 'right') + '">' + c[0] + arrow(c[1]) + '</th>').join('');
    const skewCell = v => {
      if (v == null || isNaN(v)) return '<span style="color:#6e7681">—</span>';
      const col = v > 0 ? '#f0883e' : (v < 0 ? '#3fb950' : '#8b949e');
      return '<span style="color:' + col + ';font-weight:700">' + _optSkewPt(v, 1) + '</span>';
    };
    const cell = (it, k) => {
      if (k === 'ticker') return hmTk(it.ticker);
      if (k === 'sector') return '<span style="color:#8b949e">' + hmEsc(it.sector || '') + '</span>';
      if (k === 'spot') return _optNum(it.spot, 2);
      if (k === 'atm_iv') return (it.atm_iv == null || isNaN(it.atm_iv)) ? '<span style="color:#6e7681">—</span>' : '<span style="color:#58a6ff;font-weight:700">' + _optPct(it.atm_iv, 1) + '</span>';
      if (k === 'skew_25d') return skewCell(it.skew_25d);
      if (k === 'skew_pct') return _optPct(it.skew_pct, 1);
      if (k === 'butterfly_25d') return _optSkewPt(it.butterfly_25d, 1);
      if (k === 'put_call_iv_spread') return _optSkewPt(it.put_call_iv_spread, 1);
      if (k === 'n_strikes') return (it.n_strikes == null) ? '—' : String(it.n_strikes);
      return '—';
    };
    const curFocus = window.hmState.optSmileTicker || '';
    const trs = items.map(it => {
      const on = it.ticker === curFocus;
      // 点行 → 设为微笑 focus (ticker 经 hmEsc, onclick 用 \' 转义, 同 hmOptPickDate)
      return '<tr onclick="hmSmileFocus(\'' + hmEsc(it.ticker) + '\')" style="cursor:pointer;background:' + (on ? '#1f6feb22' : 'transparent') + '">' +
        cols.map(c => '<td style="padding:4px 10px;border-bottom:1px solid #161b22;text-align:' + (left(c[1]) ? 'left' : 'right') + ';color:#e6edf3">' + cell(it, c[1]) + '</td>').join('') + '</tr>';
    }).join('');
    const head = '<div style="font-size:13px;font-weight:700;color:#e6edf3;margin-bottom:6px">📋 横截面偏斜表 <span style="font-size:10px;color:#6e7681;font-weight:400">(点行切换上方微笑聚焦标的; 默认按 25Δ偏斜 降序)</span></div>';
    return head + (items.length
      ? '<div style="width:100%;overflow:auto"><table style="width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums"><thead><tr>' + th + '</tr></thead><tbody>' + trs + '</tbody></table></div>'
      : '<div style="padding:20px;color:#8b949e">微笑数据积累中… (' + hmEsc((meta && meta.note) || '明天开盘后开始采集') + ')</div>');
  }
  // ── 盘中偏斜 mini bars: skew_today + atm_iv_today 按时间桶 (复用 opt 盘中样式) ──
  function _hmSmileIntradayChart(intr) {
    try {
      if (!intr || !intr.buckets || !intr.buckets.length) return '';
      const bars = (label, vals, color, mode, dp) => {
        // mode: 'pct' = IV×100 ; 'pt' = skew ×100 带符号
        const nums = (vals || []).map(v => (v == null || isNaN(v)) ? null : Number(v));
        const valid = nums.filter(v => v != null);
        if (!valid.length) return '';
        const mx = Math.max.apply(null, valid), mn = Math.min.apply(null, valid);
        const span = (mx - mn) || 1;
        const fmt = v => (v == null) ? '—' : (mode === 'pt' ? _optSkewPt(v, dp) : (v * 100).toFixed(dp) + '%');
        const cells = intr.buckets.map((b, i) => {
          const v = nums[i];
          const h = (v == null) ? 0 : Math.round(6 + 30 * (v - mn) / span);
          return '<div title="' + hmEsc(b) + ' · ' + fmt(v) + '" style="flex:1;min-width:5px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:2px">' +
            '<span style="width:70%;height:' + h + 'px;background:' + color + ';border-radius:2px 2px 0 0"></span>' +
            '<span style="font-size:7px;color:#6e7681;writing-mode:vertical-rl;line-height:1">' + hmEsc(b) + '</span>' +
          '</div>';
        }).join('');
        return '<div style="flex:1;min-width:240px"><div style="font-size:11px;color:#8b949e;margin-bottom:4px">' + label +
          ' <span style="color:#6e7681">(低 ' + fmt(mn) + ' · 高 ' + fmt(mx) + ')</span></div>' +
          '<div style="display:flex;align-items:flex-end;gap:2px;height:54px">' + cells + '</div></div>';
      };
      const skewBars = bars('盘中平均 25Δ 偏斜 (全宇宙, >0=怕跌)', intr.skew_today, '#f0883e', 'pt', 1);
      const ivBars = bars('盘中平均 ATM IV (全宇宙)', intr.atm_iv_today, '#58a6ff', 'pct', 1);
      if (!skewBars && !ivBars) return '';
      return '<div style="display:flex;gap:18px;flex-wrap:wrap;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 14px;margin-top:12px">' + skewBars + ivBars + '</div>';
    } catch (e) { return ''; }
  }

  // ════════════════════════════════════════════════════════════════════════
  // 关系/实验 (options_experiments): 一批找 IV 可交易规律的实验, 每个=一张卡。
  // 同属 期权波动 面板的子视图。GET /api/heatmap/options_experiments?date= 懒加载。
  // 契约: experiments[{id,title,desc,conclusion,chart{type scatter|line,points/series,guide},table,stat}] + summary。
  // canvas 数据从 window.hmState.optExpData.experiments[i] 直接读 (不走 data 属性 — JSON 双引号会截断属性)。
  // ════════════════════════════════════════════════════════════════════════
  function _hmLoadExp(date) {
    const st = window.hmState;
    const dt = (date !== undefined) ? date : (st.optDate || null);
    // 仅当实验视图当前可见且还没数据时给占位 (不抢其它子视图渲染)
    const expBody = document.getElementById('hmExpBody');
    if (expBody && !st.optExpData) {
      expBody.innerHTML = '<div style="color:#8b949e;font-size:13px;padding:20px">加载 关系/实验 中…</div>';
    }
    const url = '/api/heatmap/options_experiments' + (dt ? ('?date=' + encodeURIComponent(dt)) : '');
    fetch(url).then(r => r.json()).then(d => {
      st.optExpData = d || {};
      if (st.dataMode === 'opt' && st.optView === 'exp') _hmRenderOptTable();
    }).catch(e => {
      // 端点未就绪也优雅降级: 存一个合法空骨架, 渲染成"数据积累中…"占位卡
      st.optExpData = { experiments: [], summary: '', meta: { note: String(e && e.message || e), ok: false } };
      if (st.dataMode === 'opt' && st.optView === 'exp') _hmRenderOptTable();
    });
  }
  // 卡内表格排序: 记 {cardId, key, dir}, 重绘整个 exp body (轻量, 卡数有限)
  window.hmExpSort = function (cardId, key) {
    const s = window.hmState.optExpSort;
    const same = s && s.cardId === cardId && s.key === key;
    window.hmState.optExpSort = { cardId: cardId, key: key, dir: same ? -s.dir : -1 };
    _hmRenderOptTable();
  };
  // 实验视图整段 body: 顶部 summary + 每个实验一张卡 (标题/desc/结论/图/表/stat)
  function _hmRenderExpBody() {
    const ed = window.hmState.optExpData;
    if (!ed) { return '<div id="hmExpBody"><div style="color:#8b949e;font-size:13px;padding:20px">加载 关系/实验 中…</div></div>'; }
    const help = '<div style="font-size:11px;color:#6e7681;margin-bottom:10px;line-height:1.6">' +
      '这些实验找 IV 的可交易规律 (卖谁的权利金划算、隐含vs实际、IV与beta/市值/偏斜的关系…), 数据攒越多越准。</div>';
    const meta = ed.meta || {};
    const exps = ed.experiments || [];
    // ── 顶部 summary 块 (风格同其它 opt 结论框) ──
    const summaryBox = ed.summary
      ? '<div style="background:#58a6ff14;border:1px solid #58a6ff55;border-radius:8px;padding:10px 14px;margin-bottom:12px">' +
          '<div style="font-size:11px;font-weight:700;color:#58a6ff;margin-bottom:4px">🔬 实验汇总 · 可操作规律</div>' +
          '<div style="font-size:13px;color:#e6edf3;line-height:1.65">' + hmEsc(ed.summary) + '</div>' +
        '</div>'
      : '';
    // ── 每个实验一张卡 ──
    const cards = exps.length
      ? exps.map((e, i) => _hmExpCard(e, i)).join('')
      : '<div style="padding:20px;color:#8b949e">关系/实验 数据积累中… (' + hmEsc(meta.note || '明天开盘后开始采集') + ')</div>';
    return '<div id="hmExpBody">' + help + summaryBox + cards + '</div>';
  }
  // 单张实验卡: 边框盒 (深色 #0d1117) + 标题(粗) + desc(灰) + 结论(突出, 数字高亮) + 图 + 表 + stat。
  function _hmExpCard(e, idx) {
    e = e || {};
    const cid = e.id || ('exp' + idx);
    const title = '<div style="font-size:14px;font-weight:800;color:#e6edf3">' + hmEsc(e.title || ('实验 ' + (idx + 1))) + '</div>';
    const desc = e.desc ? '<div style="font-size:11px;color:#8b949e;margin:3px 0 8px;line-height:1.55">' + hmEsc(e.desc) + '</div>' : '';
    // 结论框: 数字 (含 % / pt / 小数 / 比值) 高亮成蓝色; 空/占位 → 灰字"数据积累中…"
    const concRaw = e.conclusion || '';
    const placeholder = !concRaw || /数据积累中/.test(concRaw);
    const concHtml = placeholder
      ? '<span style="color:#6e7681">数据积累中…</span>'
      : _hmExpHighlightNums(concRaw);
    const concBox = '<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:9px 12px;margin-bottom:10px">' +
      '<div style="font-size:13px;color:#e6edf3;line-height:1.65">' + concHtml + '</div></div>';
    const chartHtml = _hmExpChartShell(e.chart, cid, idx);
    const tableHtml = _hmExpTable(e.table, cid);
    const statHtml = _hmExpStatLine(e.stat);
    return '<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px 14px;margin-bottom:12px">' +
      title + desc + concBox + chartHtml + tableHtml + statHtml + '</div>';
  }
  // 把结论里的数字 (含 +/- 号、%、pt、x 倍、小数、千分位) 高亮成蓝色加粗; 其余文本经 hmEsc。
  function _hmExpHighlightNums(s) {
    const txt = String(s == null ? '' : s);
    let out = '', last = 0;
    const re = /[+\-−]?\d[\d,]*\.?\d*\s*(?:%|pt|倍|x|×)?/g;
    let m;
    while ((m = re.exec(txt)) !== null) {
      out += hmEsc(txt.slice(last, m.index));
      out += '<b style="color:#58a6ff">' + hmEsc(m[0]) + '</b>';
      last = m.index + m[0].length;
      if (re.lastIndex === m.index) re.lastIndex++;   // 防零宽死循环
    }
    out += hmEsc(txt.slice(last));
    return out;
  }
  // 图外壳: scatter / line 都给一个 <canvas id=hmExpCanvas_<cid>>; 无图或空点 → 占位文字。
  // canvas 不带任何 data 属性: 数据由 _hmDrawExpCanvases 从 window.hmState.optExpData.experiments[idx].chart 读。
  function _hmExpChartShell(chart, cid, idx) {
    if (!chart) return '';
    const isScatter = chart.type === 'scatter';
    const isLine = chart.type === 'line';
    const hasPts = isScatter && chart.points && chart.points.length >= 1;
    const hasSer = isLine && chart.series && chart.series.some(s => s && (s.y || []).some(v => v != null && !isNaN(v)));
    if (!hasPts && !hasSer) {
      return '<div style="padding:18px;color:#6e7681;font-size:12px;background:#0d1117;border:1px solid #21262d;border-radius:6px;margin-bottom:10px">📈 图数据积累中…</div>';
    }
    const axis = '<div style="font-size:10px;color:#6e7681;margin-top:3px">x: ' + hmEsc(chart.x_label || '') +
      ' · y: ' + hmEsc(chart.y_label || '') + '</div>';
    // 散点: 给个 sector 图例 (有 color 时); line: 给 series 名图例。
    let legend = '';
    if (isScatter) {
      const secs = {};
      (chart.points || []).forEach(p => { if (p && p.color) secs[p.color] = _hmExpColor(p.color); });
      const keys = Object.keys(secs).slice(0, 8);
      if (keys.length) legend = '<div style="display:flex;gap:10px;flex-wrap:wrap;font-size:10px;color:#8b949e;margin-top:4px">' +
        keys.map(k => '<span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + secs[k] + ';vertical-align:middle;margin-right:3px"></span>' + hmEsc(k) + '</span>').join('') + '</div>';
      const g = chart.guide;
      if (g) {
        const gl = g.type === 'diagonal' ? 'y = x 参考线 (线上=隐含偏贵, 线下=偏便宜)'
          : g.type === 'hline' ? ('水平参考线 y = ' + _optNum(g.value, 2))
          : g.type === 'fit' ? '线性拟合线' : '';
        if (gl) legend += '<div style="font-size:10px;color:#6e7681;margin-top:2px"><span style="display:inline-block;width:12px;height:2px;background:#d29922;vertical-align:middle;margin-right:3px"></span>' + hmEsc(gl) + '</div>';
      }
    } else if (isLine) {
      const names = (chart.series || []).map((s, i) => ({ n: s.name || ('系列' + (i + 1)), c: _hmExpSeriesColor(i) }));
      if (names.length) legend = '<div style="display:flex;gap:12px;flex-wrap:wrap;font-size:10px;color:#8b949e;margin-top:4px">' +
        names.map(o => '<span><span style="display:inline-block;width:12px;height:3px;background:' + o.c + ';vertical-align:middle;margin-right:3px"></span>' + hmEsc(o.n) + '</span>').join('') + '</div>';
    }
    return '<canvas id="hmExpCanvas_' + hmEsc(cid) + '" width="720" height="260" style="width:100%;max-width:760px;height:260px;display:block;margin-bottom:2px"></canvas>' +
      axis + legend;
  }
  // 卡内排行/明细小表: 紧凑, 可排序 (点列头走 hmExpSort)。无表 → ''。
  function _hmExpTable(tbl, cid) {
    if (!tbl || !tbl.columns || !tbl.columns.length) return '';
    let rows = (tbl.rows || []).slice();
    const cols = tbl.columns;
    const so = window.hmState.optExpSort;
    if (so && so.cardId === cid && so.key) {
      const k = so.key, dir = so.dir;
      rows.sort((a, b) => {
        let va = a[k], vb = b[k];
        const na = (va == null || va === '' || isNaN(va)), nb = (vb == null || vb === '' || isNaN(vb));
        if (na && nb) return 0;
        if (na || nb) {   // 非数字 (字符串列) → localeCompare
          return dir * String(va == null ? '' : va).localeCompare(String(vb == null ? '' : vb));
        }
        return dir * (Number(va) - Number(vb));
      });
    }
    const arrow = k => (so && so.cardId === cid && so.key === k) ? (so.dir > 0 ? ' ▲' : ' ▼') : '';
    // onclick 转义同 hmOptSort/hmSmileSort: 字符串实参用 \' 包裹 (Python 字符串里写成 \\' 才能落地成 \')。
    const th = cols.map(c => '<th onclick="hmExpSort(\'' + hmEsc(cid) + '\',\'' + hmEsc(c.key) + '\')" style="cursor:pointer;user-select:none;background:#161b22;color:#8b949e;padding:5px 9px;border-bottom:1px solid #30363d;text-align:left;font-size:11px">' + hmEsc(c.label || c.key) + arrow(c.key) + '</th>').join('');
    const fmtCell = v => {
      if (v == null || v === '') return '<span style="color:#6e7681">—</span>';
      if (typeof v === 'number') return Number.isInteger(v) ? String(v) : _optNum(v, 2);
      return hmEsc(v);
    };
    const trs = rows.map(r =>
      '<tr>' + cols.map((c, ci) =>
        '<td style="padding:3px 9px;border-bottom:1px solid #161b22;color:' + (ci === 0 ? '#58a6ff' : '#e6edf3') + (ci === 0 ? ';font-weight:600' : '') + '">' + fmtCell(r[c.key]) + '</td>').join('') + '</tr>').join('');
    const note = tbl.note ? '<div style="font-size:10px;color:#6e7681;margin-top:4px">' + hmEsc(tbl.note) + '</div>' : '';
    return '<div style="width:100%;overflow:auto;margin-top:8px"><table style="width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums"><thead><tr>' + th + '</tr></thead><tbody>' + trs + '</tbody></table></div>' + note;
  }
  // stat 摘要行: 拟合 R² / 斜率 / 市场均值 / 样本数 (有哪个画哪个)。
  function _hmExpStatLine(stat) {
    if (!stat) return '';
    const parts = [];
    if (stat.r2 != null && !isNaN(stat.r2)) parts.push('拟合 R²=' + _optNum(stat.r2, 2));
    if (stat.slope != null && !isNaN(stat.slope)) parts.push('斜率=' + _optNum(stat.slope, 3));
    if (stat.market_avg != null && !isNaN(stat.market_avg)) parts.push('市场均值=' + _optNum(stat.market_avg, 2));
    if (stat.n != null && !isNaN(stat.n)) parts.push('样本 n=' + Math.round(stat.n));
    if (!parts.length) return '';
    return '<div style="font-size:11px;color:#8b949e;margin-top:8px;font-variant-numeric:tabular-nums">📊 ' + hmEsc(parts.join(' · ')) + '</div>';
  }
  // sector → 稳定颜色 (字符串哈希取色板), 同 sector 同色。
  function _hmExpColor(key) {
    const pal = ['#58a6ff', '#3fb950', '#f0883e', '#db61a2', '#a371f7', '#d29922', '#39c5cf', '#f85149', '#bc8cff', '#56d364'];
    const s = String(key || '');
    let h = 0;
    for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
    return pal[Math.abs(h) % pal.length];
  }
  // line 视图各系列颜色 (按序)。
  function _hmExpSeriesColor(i) {
    const pal = ['#58a6ff', '#f0883e', '#3fb950', '#db61a2', '#a371f7', '#d29922'];
    return pal[i % pal.length];
  }
  // canvas 实绘: 遍历所有实验卡, 每张按 chart.type 画 (scatter=点+参考线, line=多折线)。
  // 数据从 window.hmState.optExpData.experiments[i].chart 直接读 (NOT data 属性)。纯 2D, 无 d3。
  function _hmDrawExpCanvases() {
    try {
      const ed = window.hmState.optExpData;
      if (!ed || !ed.experiments) return;
      ed.experiments.forEach((e, idx) => {
        if (!e || !e.chart) return;
        const cid = e.id || ('exp' + idx);
        const cv = document.getElementById('hmExpCanvas_' + cid);
        if (!cv || !cv.getContext) return;
        if (e.chart.type === 'scatter') _hmDrawExpScatter(cv, e.chart);
        else if (e.chart.type === 'line') _hmDrawExpLine(cv, e.chart);
      });
    } catch (err) { } // 画图失败不崩面板
  }
  // 散点图: points[{x,y,label,color}] + guide(diagonal/hline/fit)。点按 color(sector) 上色;
  // 标注 y 最高/最低两个极值点的 ticker。纯 2D context。
  function _hmDrawExpScatter(cv, chart) {
    try {
      const ctx = cv.getContext('2d');
      const W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      const pts = (chart.points || []).filter(p => p && p.x != null && p.y != null && !isNaN(p.x) && !isNaN(p.y));
      if (!pts.length) return;
      const padL = 50, padR = 16, padT = 14, padB = 30;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
      let xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
      let ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
      const g = chart.guide;
      // 对角线 y=x: 让两轴同域, 才能直观比较隐含 vs 实际
      if (g && g.type === 'diagonal') {
        const lo = Math.min(xmin, ymin), hi = Math.max(xmax, ymax);
        xmin = ymin = lo; xmax = ymax = hi;
      }
      const xpad = (xmax - xmin) * 0.08 || 1; xmin -= xpad; xmax += xpad;
      const ypad = (ymax - ymin) * 0.08 || 1; ymin -= ypad; ymax += ypad;
      if (g && g.type === 'hline' && g.value != null) { ymin = Math.min(ymin, g.value); ymax = Math.max(ymax, g.value); }
      const X = v => padL + (v - xmin) / (xmax - xmin) * plotW;
      const Y = v => padT + (1 - (v - ymin) / (ymax - ymin)) * plotH;
      // 网格 + 轴刻度
      ctx.strokeStyle = '#21262d'; ctx.fillStyle = '#6e7681'; ctx.font = '10px -apple-system,sans-serif'; ctx.lineWidth = 1;
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      for (let i = 0; i <= 4; i++) {
        const yv = ymin + (ymax - ymin) * i / 4, y = Y(yv);
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
        ctx.fillText(_hmExpAxisFmt(yv), padL - 5, y);
      }
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      for (let i = 0; i <= 4; i++) {
        const xv = xmin + (xmax - xmin) * i / 4;
        ctx.fillText(_hmExpAxisFmt(xv), X(xv), H - padB + 5);
      }
      // 参考线
      if (g) {
        ctx.strokeStyle = '#d29922'; ctx.setLineDash([5, 4]); ctx.lineWidth = 1.3; ctx.beginPath();
        if (g.type === 'diagonal') {
          const lo = Math.max(xmin, ymin), hi = Math.min(xmax, ymax);
          ctx.moveTo(X(lo), Y(lo)); ctx.lineTo(X(hi), Y(hi));
        } else if (g.type === 'hline' && g.value != null) {
          ctx.moveTo(X(xmin), Y(g.value)); ctx.lineTo(X(xmax), Y(g.value));
        } else if (g.type === 'fit' && g.slope != null) {
          const b = (g.intercept != null) ? g.intercept : 0;
          ctx.moveTo(X(xmin), Y(g.slope * xmin + b)); ctx.lineTo(X(xmax), Y(g.slope * xmax + b));
        }
        ctx.stroke(); ctx.setLineDash([]);
      }
      // 点
      pts.forEach(p => {
        ctx.fillStyle = p.color ? _hmExpColor(p.color) : '#58a6ff';
        ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), 3, 0, Math.PI * 2); ctx.fill();
      });
      // 标注 y 极值两点的 ticker (避免全标拥挤)
      const labeled = [];
      const byY = pts.slice().sort((a, b) => b.y - a.y);
      if (byY.length) labeled.push(byY[0]);
      if (byY.length > 1) labeled.push(byY[byY.length - 1]);
      ctx.fillStyle = '#c9d1d9'; ctx.font = '10px -apple-system,sans-serif';
      ctx.textAlign = 'left'; ctx.textBaseline = 'bottom';
      labeled.forEach(p => {
        if (!p.label) return;
        let lx = X(p.x) + 5, ly = Y(p.y) - 3;
        if (lx > W - padR - 40) { ctx.textAlign = 'right'; lx = X(p.x) - 5; } else ctx.textAlign = 'left';
        ctx.fillText(String(p.label), lx, ly);
      });
    } catch (e) { } // ignore
  }
  // 折线图: series[{name,x:[],y:[]}], x 为时间桶 (类目轴, 等距)。多线叠画 + y 轴刻度。
  function _hmDrawExpLine(cv, chart) {
    try {
      const ctx = cv.getContext('2d');
      const W = cv.width, H = cv.height;
      ctx.clearRect(0, 0, W, H);
      const series = (chart.series || []).filter(s => s && s.y && s.y.length);
      if (!series.length) return;
      // 类目: 取最长 x 作横轴标签
      let cats = [];
      series.forEach(s => { if ((s.x || []).length > cats.length) cats = s.x; });
      const n = Math.max.apply(null, series.map(s => s.y.length));
      if (!n) return;
      const padL = 50, padR = 16, padT = 14, padB = 30;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const allY = [];
      series.forEach(s => s.y.forEach(v => { if (v != null && !isNaN(v)) allY.push(Number(v)); }));
      if (!allY.length) return;
      let ymin = Math.min.apply(null, allY), ymax = Math.max.apply(null, allY);
      const ypad = (ymax - ymin) * 0.12 || 1; ymin -= ypad; ymax += ypad;
      const X = i => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
      const Y = v => padT + (1 - (v - ymin) / (ymax - ymin)) * plotH;
      // 网格 + y 刻度
      ctx.strokeStyle = '#21262d'; ctx.fillStyle = '#6e7681'; ctx.font = '10px -apple-system,sans-serif'; ctx.lineWidth = 1;
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      for (let i = 0; i <= 4; i++) {
        const yv = ymin + (ymax - ymin) * i / 4, y = Y(yv);
        ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
        ctx.fillText(_hmExpAxisFmt(yv), padL - 5, y);
      }
      // x 类目标签 (最多 ~8 个, 抽稀)
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      const step = Math.max(1, Math.ceil(n / 8));
      for (let i = 0; i < n; i += step) {
        const lab = (cats && cats[i] != null) ? String(cats[i]) : String(i);
        ctx.fillText(lab, X(i), H - padB + 5);
      }
      // 各系列折线 + 点
      series.forEach((s, si) => {
        const color = _hmExpSeriesColor(si);
        ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.beginPath();
        let started = false;
        s.y.forEach((v, i) => {
          if (v == null || isNaN(v)) { started = false; return; }
          const x = X(i), y = Y(Number(v));
          if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
        });
        ctx.stroke();
        ctx.fillStyle = color;
        s.y.forEach((v, i) => { if (v != null && !isNaN(v)) { ctx.beginPath(); ctx.arc(X(i), Y(Number(v)), 2.2, 0, Math.PI * 2); ctx.fill(); } });
      });
    } catch (e) { } // ignore
  }
  // 轴刻度数字格式: 大数千分位/小数 2 位, 让 scatter/line 轴标签更易读。
  function _hmExpAxisFmt(v) {
    if (v == null || isNaN(v)) return '';
    const a = Math.abs(v);
    if (a >= 1000) return Math.round(v).toLocaleString('en-US');
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }
  */ // end 期权波动实验 block

  // ════════════════════════════════════════════════════════════════════════
  // 💹 期权量稳定性 (optvol): 合并 universe 单表 + 点行内联展开 vol/OI/IV/PC 趋势。
  // 整表移植自 options_vol/page.py (原独立 iframe tab), 改成 heatmap 一个数据模式。
  // 数据: GET /api/optvol/{summary,status,history/{T}} + GET /api/optvol/eod_pc (P/C)。
  // 四张图全是手写 inline SVG (无 d3/CDN 依赖)。函数/状态全部 hmOptVol 前缀避免撞名。
  // ════════════════════════════════════════════════════════════════════════

  // ── 数值人性化: 881927 → "882K" / 1.2M; IV 用百分比 ──
  function _hmOvHumanInt(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    v = Number(v);
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 1 : 2).replace(/\.0+$/, '') + 'M';
    if (a >= 1e3) return Math.round(v / 1e3) + 'K';
    return String(Math.round(v));
  }
  function _hmOvFmtIv(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (Number(v) * 100).toFixed(1) + '%';
  }
  function _hmOvFmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Math.round(Number(v) * 100) + '%';
  }
  function _hmOvFmtNum2(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return Number(v).toFixed(2);
  }
  function _hmOvEsc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ── 手写 SVG 折线 (移植自 page.py drawLine; 无任何外部库) ──────────────
  // points = 数值数组(可含 null,跳过)。画折线 + 末值点 + y 轴 min/max 标签。
  // opts.baseline (可选) = 画一条横向参考虚线(P/C 用 1.0 平衡线)。viewBox 400x130。
  const _HMOV_SVGNS = 'http://www.w3.org/2000/svg';
  function _hmOvEl(tag, attrs) {
    const e = document.createElementNS(_HMOV_SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function _hmOptVolSvg(svg, points, opts) {
    opts = opts || {};
    const color = opts.color || '#58a6ff';
    const yfmt = opts.yfmt || function (v) { return _hmOvHumanInt(v); };
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const W = 400, H = 130, padL = 4, padR = 40, padT = 10, padB = 16;
    const vals = points.filter(function (v) { return v !== null && v !== undefined && !isNaN(v); });
    if (!vals.length) {
      const t = _hmOvEl('text', { x: W / 2, y: H / 2, fill: '#8b949e', 'font-size': '11', 'text-anchor': 'middle' });
      t.textContent = '无数据';
      svg.appendChild(t);
      return;
    }
    let mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    // 若有 baseline(如 P/C 1.0), 把它纳入 y 域以便那条参考线可见
    if (opts.baseline != null && !isNaN(opts.baseline)) {
      mn = Math.min(mn, opts.baseline); mx = Math.max(mx, opts.baseline);
    }
    if (mn === mx) { mn = mn - 1; mx = mx + 1; }   // 平线也给点高度
    const n = points.length;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    function X(i) { return padL + (n <= 1 ? 0 : (i / (n - 1)) * plotW); }
    function Y(v) { return padT + (1 - (v - mn) / (mx - mn)) * plotH; }

    // 横向基线(底)
    svg.appendChild(_hmOvEl('line', { x1: padL, y1: padT + plotH, x2: padL + plotW, y2: padT + plotH, stroke: '#21262d', 'stroke-width': '1' }));

    // 可选参考线 (P/C 1.0 平衡线) — 横向虚线 + 右侧标注
    if (opts.baseline != null && !isNaN(opts.baseline)) {
      const by = Y(opts.baseline);
      svg.appendChild(_hmOvEl('line', { x1: padL, y1: by.toFixed(1), x2: (padL + plotW).toFixed(1), y2: by.toFixed(1), stroke: '#6e7681', 'stroke-width': '0.8', 'stroke-dasharray': '3 3', opacity: '0.8' }));
      const bt = _hmOvEl('text', { x: (padL + plotW + 2).toFixed(1), y: (by + 3).toFixed(1), fill: '#6e7681', 'font-size': '8', 'text-anchor': 'start' });
      bt.textContent = String(opts.baseline);
      svg.appendChild(bt);
    }

    // 折线 (遇 null 断开 → 多段 polyline)
    let seg = [], lastPt = null, lastVal = null;
    function flush() {
      if (seg.length >= 2) {
        svg.appendChild(_hmOvEl('polyline', { points: seg.join(' '), fill: 'none', stroke: color, 'stroke-width': '1.6', 'stroke-linejoin': 'round' }));
      }
      seg = [];
    }
    for (let i = 0; i < n; i++) {
      const v = points[i];
      if (v === null || v === undefined || isNaN(v)) { flush(); continue; }
      seg.push(X(i).toFixed(1) + ',' + Y(v).toFixed(1));
      lastPt = [X(i), Y(v)]; lastVal = v;
    }
    flush();

    // 末值点 + 横虚线指引
    if (lastPt) {
      svg.appendChild(_hmOvEl('line', { x1: padL, y1: lastPt[1].toFixed(1), x2: lastPt[0].toFixed(1), y2: lastPt[1].toFixed(1), stroke: color, 'stroke-width': '0.6', 'stroke-dasharray': '2 3', opacity: '0.5' }));
      svg.appendChild(_hmOvEl('circle', { cx: lastPt[0].toFixed(1), cy: lastPt[1].toFixed(1), r: '2.6', fill: color }));
      const lt = _hmOvEl('text', { x: (lastPt[0] + 4).toFixed(1), y: (lastPt[1] + 3).toFixed(1), fill: color, 'font-size': '10', 'text-anchor': 'start' });
      lt.textContent = yfmt(lastVal);
      svg.appendChild(lt);
    }
    // y 轴 max(左上) / min(左下) 标签
    const tmax = _hmOvEl('text', { x: padL, y: padT + 3, fill: '#8b949e', 'font-size': '9', 'text-anchor': 'start' });
    tmax.textContent = yfmt(mx);
    svg.appendChild(tmax);
    const tmin = _hmOvEl('text', { x: padL, y: padT + plotH, fill: '#8b949e', 'font-size': '9', 'text-anchor': 'start' });
    tmin.textContent = yfmt(mn);
    svg.appendChild(tmin);
  }

  // ── 表列定义: [key, 列名, 渲染函数] ──
  function _hmOvPctCls(v) { if (v == null) return 'ovPctLo'; if (v >= 0.9) return 'ovPctOk'; if (v >= 0.5) return 'ovPctMid'; return 'ovPctLo'; }
  const _HMOV_COLS = [
    ['ticker', 'Symbol', function (r) {
      const b = r.tier === 'gold' ? '<span class="ovBadge ovBGold">🥇</span>'
        : r.tier === 'silver' ? '<span class="ovBadge ovBSilver">🥈</span>' : '';
      return '<span class="ovSymcell"><span class="ovChev">▶</span>'
        + '<span class="ovSym">' + _hmOvEsc(r.ticker) + '</span>' + b + '</span>';
    }],
    ['latest_vol', '最新量', function (r) { return _hmOvHumanInt(r.latest_vol); }],
    ['median_vol', '中位(1M)', function (r) { return _hmOvHumanInt(r.median_vol); }],
    ['pct_days_hi', '%日≥5k', function (r) { return '<span class="' + _hmOvPctCls(r.pct_days_hi) + '">' + _hmOvFmtPct(r.pct_days_hi) + '</span>'; }],
    ['pct_days_lo', '%日≥2k', function (r) { return '<span class="' + _hmOvPctCls(r.pct_days_lo) + '">' + _hmOvFmtPct(r.pct_days_lo) + '</span>'; }],
    ['latest_oi', 'OI', function (r) { return _hmOvHumanInt(r.latest_oi); }],
    ['latest_iv', 'IV', function (r) { return _hmOvFmtIv(r.latest_iv); }],
    ['iv_rank', 'IVrank', function (r) { return (r.iv_rank == null || isNaN(r.iv_rank)) ? '—' : Math.round(r.iv_rank); }],
    ['latest_pc_vol', 'P/C', function (r) { return _hmOvFmtNum2(r.latest_pc_vol); }]
  ];

  // ── 排序后的行 (默认中位量降序) ──
  function _hmOptVolSortedRows() {
    const st = window.hmState.optVolSort || { key: 'median_vol', dir: -1 };
    const rows = (window.hmState.optVolData || []).slice();
    rows.sort(function (a, b) {
      let x = a[st.key], y = b[st.key];
      if (st.key === 'ticker') { x = String(x); y = String(y); return (x < y ? -1 : x > y ? 1 : 0) * st.dir; }
      x = (x == null || isNaN(x)) ? -Infinity : Number(x);
      y = (y == null || isNaN(y)) ? -Infinity : Number(y);
      return (x < y ? -1 : x > y ? 1 : 0) * st.dir;
    });
    return rows;
  }

  // ── 展开行的整宽单元格: 头部(symbol+1M/3M 切换) + 四张 SVG (vol/OI/IV/PC) ──
  function _hmOptVolExpandRowHtml(r) {
    const st = window.hmState;
    const meta = '中位(1M) ' + _hmOvHumanInt(r.median_vol) + ' · 最新 ' + _hmOvHumanInt(r.latest_vol)
      + ' · IV ' + _hmOvFmtIv(r.latest_iv) + ' · ' + (r.n_days_total || '?') + ' 天历史';
    const tierTxt = r.tier === 'gold' ? '🥇 Gold' : r.tier === 'silver' ? '🥈 Silver' : '';
    const rg = st.optVolExpand.range;
    return '<tr class="ovExpand" data-ovexpand="' + _hmOvEsc(r.ticker) + '"><td colspan="' + _HMOV_COLS.length + '">'
      + '<div class="ovExpandInner">'
      + '<div class="ovExpandHead">'
      +   '<span class="ovSym">' + _hmOvEsc(r.ticker) + '</span>'
      +   '<span class="ovMuted">' + _hmOvEsc(tierTxt) + ' · ' + _hmOvEsc(meta) + '</span>'
      +   '<span class="ovRangeToggle" data-ovroletoggle="1">'
      +     '<button class="ovChip' + (rg === '1M' ? ' on' : '') + '" data-ovrange="1M">1M</button>'
      +     '<button class="ovChip' + (rg === '3M' ? ' on' : '') + '" data-ovrange="3M">3M</button>'
      +   '</span>'
      + '</div>'
      + '<div class="ovCharts">'
      +   '<div class="ovChartbox"><div class="ovCtitle"><span>Options Vol</span>'
      +     '<span class="ovClast" id="hmOvLastVol">…</span></div>'
      +     '<svg id="hmOvSvgVol" viewBox="0 0 400 130" preserveAspectRatio="none"></svg></div>'
      +   '<div class="ovChartbox"><div class="ovCtitle"><span>Total OI</span>'
      +     '<span class="ovClast" id="hmOvLastOi">…</span></div>'
      +     '<svg id="hmOvSvgOi" viewBox="0 0 400 130" preserveAspectRatio="none"></svg></div>'
      +   '<div class="ovChartbox"><div class="ovCtitle"><span>IV</span>'
      +     '<span class="ovClast" id="hmOvLastIv">…</span></div>'
      +     '<svg id="hmOvSvgIv" viewBox="0 0 400 130" preserveAspectRatio="none"></svg></div>'
      +   '<div class="ovChartbox"><div class="ovCtitle"><span>P/C 量比 (EOD)</span>'
      +     '<span class="ovClast" id="hmOvLastPc">…</span></div>'
      +     '<svg id="hmOvSvgPc" viewBox="0 0 400 130" preserveAspectRatio="none"></svg></div>'
      + '</div></div></td></tr>';
  }

  // ── 渲染合并表 ──
  function _hmRenderOptVolTable() {
    const panel = document.getElementById('hmOptVolPanel');
    if (!panel) return;
    const st = window.hmState;
    const rows0 = st.optVolData;
    if (rows0 == null) { _hmLoadOptVol(); return; }   // 还没拉到 summary
    const sort = st.optVolSort || { key: 'median_vol', dir: -1 };

    // 计数 (反映合并后实际行)
    let nGold = 0, nSilver = 0;
    rows0.forEach(function (r) { if (r.tier === 'gold') nGold++; else if (r.tier === 'silver') nSilver++; });
    const gen = st.optVolGen;
    const genTxt = gen ? ('数据 ' + String(gen).replace('T', ' ')) : '尚未爬取';

    if (!rows0.length) {
      panel.innerHTML = '<div class="ovInner">'
        + '<h1>💹 期权量稳定性 <span class="ovMuted">· ' + _hmOvEsc(genTxt) + '</span></h1>'
        + '<div style="text-align:center;padding:64px 20px;color:#8b949e">'
        + '<div style="font-size:15px;color:#e6edf3;margin-bottom:6px">尚未爬取</div>'
        + '<div>options 历史量数据尚未生成 (周更 run_optvol)。</div></div></div>';
      return;
    }

    // 表头 (点击排序; data-ovkey, 非 inline onclick)
    let htr = '<tr>';
    _HMOV_COLS.forEach(function (c) {
      const arr = (sort.key === c[0]) ? (sort.dir < 0 ? ' ▼' : ' ▲') : '';
      htr += '<th data-ovkey="' + c[0] + '">' + _hmOvEsc(c[1]) + '<span class="arr">' + arr + '</span></th>';
    });
    htr += '</tr>';

    const rows = _hmOptVolSortedRows();
    const openTk = st.optVolExpand.ticker;
    let body = '';
    rows.forEach(function (r) {
      const sel = (openTk === r.ticker) ? ' sel' : '';
      body += '<tr class="ovRow' + sel + '" data-ovticker="' + _hmOvEsc(r.ticker) + '">';
      _HMOV_COLS.forEach(function (c) { body += '<td>' + c[2](r) + '</td>'; });
      body += '</tr>';
      if (openTk === r.ticker) body += _hmOptVolExpandRowHtml(r);   // 紧贴该行下方插入展开行
    });

    panel.innerHTML = '<div class="ovInner">'
      + '<h1>💹 期权量稳定性 <span class="ovMuted">· ' + _hmOvEsc(genTxt) + '</span></h1>'
      + '<div class="ovSubhead">合并 universe 每日期权成交量 · ≥90% 交易日达标(最近 ~1 个月)分档 · '
      +   '🥇每天≥5000 / 🥈每天≥2000(已排除 ADR) · 点 Symbol 在该行下方展开 vol/OI/IV/PC 趋势</div>'
      + '<div class="ovStatusBar">'
      +   '<span class="pill" id="hmOvPillTime">最近爬取 <b>' + _hmOvEsc(gen ? String(gen).replace('T', ' ') : '—') + '</b></span>'
      +   '<span class="pill">🥇 ' + nGold + '</span>'
      +   '<span class="pill">🥈 ' + nSilver + '</span>'
      + '</div>'
      + '<table class="ovTable"><thead>' + htr + '</thead><tbody>' + body + '</tbody></table>'
      + '</div>';

    const status = document.getElementById('hmStatus');
    if (status) status.textContent = '期权量稳定性 · 🥇' + nGold + ' / 🥈' + nSilver + ' · ' + genTxt + ' (非实时)';

    // DOM 插好后再渲染展开图 (此时 svg 已在文档里)
    if (openTk) _hmOptVolRenderCharts();
  }

  // ── 展开图渲染 (读 expand.series + expand.pc; 未到 → 占位"…") ──
  function _hmOvRangeSlice(series, range) {
    if (!series) return [];
    if (range === '1M') return series.slice(-21);   // 尾 21 点 ≈ 1 个月
    return series;                                   // 3M = 全量
  }
  function _hmOptVolRenderCharts() {
    const svgVol = document.getElementById('hmOvSvgVol');
    const svgOi = document.getElementById('hmOvSvgOi');
    const svgIv = document.getElementById('hmOvSvgIv');
    const svgPc = document.getElementById('hmOvSvgPc');
    if (!svgVol || !svgOi || !svgIv || !svgPc) return;   // 展开行不在 DOM(已收起)
    const d = window.hmState.optVolExpand;
    if (d.series == null) {    // 还在 fetch: 占位空图
      _hmOptVolSvg(svgVol, [], {}); _hmOptVolSvg(svgOi, [], {}); _hmOptVolSvg(svgIv, [], {});
    } else {
      const pts = _hmOvRangeSlice(d.series, d.range);
      const vol = pts.map(function (p) { return p.vol; });
      const oi = pts.map(function (p) { return p.oi; });
      const iv = pts.map(function (p) { return (p.iv == null ? null : p.iv); });
      _hmOptVolSvg(svgVol, vol, { color: '#58a6ff', yfmt: _hmOvHumanInt });
      _hmOptVolSvg(svgOi, oi, { color: '#3fb950', yfmt: _hmOvHumanInt });
      _hmOptVolSvg(svgIv, iv, { color: '#e3b341', yfmt: function (v) { return _hmOvFmtIv(v); } });
      function lastOf(arr, fmt) { for (let i = arr.length - 1; i >= 0; i--) { if (arr[i] != null && !isNaN(arr[i])) return fmt(arr[i]); } return '—'; }
      document.getElementById('hmOvLastVol').textContent = lastOf(vol, _hmOvHumanInt);
      document.getElementById('hmOvLastOi').textContent = lastOf(oi, _hmOvHumanInt);
      document.getElementById('hmOvLastIv').textContent = lastOf(iv, _hmOvFmtIv);
    }
    // P/C 图 (独立的 eod_pc 历史; null=加载中, []=无数据)
    if (d.pc == null) {
      _hmOptVolSvg(svgPc, [], {});
    } else {
      const pcPts = _hmOvRangeSlice(d.pc, d.range);     // [{date,pc_vol_ratio,total_opt_vol,weighted_iv}]
      const pc = pcPts.map(function (p) { return (p.pc_vol_ratio == null ? null : p.pc_vol_ratio); });
      _hmOptVolSvg(svgPc, pc, { color: '#db61a2', yfmt: _hmOvFmtNum2, baseline: 1.0 });
      let lastPc = '—';
      for (let i = pc.length - 1; i >= 0; i--) { if (pc[i] != null && !isNaN(pc[i])) { lastPc = _hmOvFmtNum2(pc[i]); break; } }
      const elPc = document.getElementById('hmOvLastPc'); if (elPc) elPc.textContent = lastPc;
    }
  }

  // ── eod_pc 整份缓存 (441 票一个 JSON, 一次拉缓存, 按 ticker 取 history[]) ──
  function _hmOptVolEnsurePc(ticker) {
    const st = window.hmState;
    // 已有整份缓存 → 直接取该票, 写进当前展开
    if (st.optVolPcAll) {
      const rec = st.optVolPcAll[ticker];
      st.optVolExpand.pc = (rec && Array.isArray(rec.history)) ? rec.history : [];
      _hmOptVolRenderCharts();
      return;
    }
    if (st.optVolPcInflight) return;   // 正在拉 → 等回来统一刷
    st.optVolPcInflight = true;
    fetch('/api/optvol/eod_pc').then(function (r) { return r.json(); }).then(function (j) {
      st.optVolPcInflight = false;
      st.optVolPcAll = (j && j.tickers) || {};
      // 若当前还展开着某票, 填它的 P/C 并重画
      const tk = st.optVolExpand.ticker;
      if (tk) {
        const rec = st.optVolPcAll[tk];
        st.optVolExpand.pc = (rec && Array.isArray(rec.history)) ? rec.history : [];
        _hmOptVolRenderCharts();
      }
    }).catch(function () {
      st.optVolPcInflight = false;
      st.optVolPcAll = {};
      if (st.optVolExpand.ticker) { st.optVolExpand.pc = []; _hmOptVolRenderCharts(); }
    });
  }

  // ── 展开某行 (重渲整表把展开行插到对应行下); 拉 vol/oi/iv 历史 + P/C 填图 ──
  function _hmOptVolExpandRow(ticker) {
    const st = window.hmState;
    st.optVolExpand.ticker = ticker;
    st.optVolExpand.series = null;   // 标记"加载中"
    st.optVolExpand.pc = null;
    _hmRenderOptVolTable();          // 重渲: 插入展开行(图先占位)
    // vol/OI/IV 历史 (按票缓存)
    if (st.optVolHist[ticker]) {
      st.optVolExpand.series = st.optVolHist[ticker];
      _hmOptVolRenderCharts();
    } else {
      fetch('/api/optvol/history/' + encodeURIComponent(ticker)).then(function (r) { return r.json(); })
        .then(function (j) {
          const series = (j && j.series) || [];
          st.optVolHist[ticker] = series;
          if (st.optVolExpand.ticker !== ticker) return;   // 已切走/收起
          st.optVolExpand.series = series;
          _hmOptVolRenderCharts();
        })
        .catch(function () {
          if (st.optVolExpand.ticker !== ticker) return;
          st.optVolExpand.series = [];
          _hmOptVolRenderCharts();
        });
    }
    // P/C 历史 (整份 eod_pc 缓存, 按 ticker 取)
    _hmOptVolEnsurePc(ticker);
  }
  function _hmOptVolCollapse() {
    const st = window.hmState;
    st.optVolExpand.ticker = null;
    st.optVolExpand.series = null;
    st.optVolExpand.pc = null;
    _hmRenderOptVolTable();
  }

  // ── 主加载: /api/optvol/summary → 过滤 gold/silver 且非 ADR → 渲染表 ──
  function _hmLoadOptVol() {
    const panel = document.getElementById('hmOptVolPanel');
    const st = window.hmState;
    if (panel && st.optVolData == null) {
      panel.innerHTML = '<div style="color:#8b949e;font-size:13px;padding:20px">加载期权量稳定性中…</div>';
    }
    fetch('/api/optvol/summary').then(function (r) { return r.json(); }).then(function (j) {
      const tickers = (j && j.tickers) || [];
      // 合并表只收 gold/silver 且 非 ADR; aside/nodata/ADR 全部剔除
      const rows = tickers.filter(function (r) {
        return (r.tier === 'gold' || r.tier === 'silver') && r.is_adr !== true;
      });
      st.optVolData = rows;
      st.optVolByTicker = {};
      rows.forEach(function (r) { st.optVolByTicker[r.ticker] = r; });
      st.optVolGen = (j && j.generated_at) || null;
      if (st.dataMode === 'optvol') _hmRenderOptVolTable();
    }).catch(function (e) {
      st.optVolData = [];
      if (panel) panel.innerHTML = '<div class="ovErr" style="font-size:13px;padding:20px">期权量加载失败: ' + _hmOvEsc(e && e.message) + '</div>';
    });
  }

  // ── 事件委托: 限定在 #hmOptVolPanel 内 (排序 / 行展开 / 1M-3M 切换) ──
  (function () {
    const panel = document.getElementById('hmOptVolPanel');
    if (!panel || panel.__hmOvBound) return;
    panel.__hmOvBound = true;
    panel.addEventListener('click', function (ev) {
      const t = ev.target;
      const st = window.hmState;
      // 1M / 3M 切换 (在展开行内; 先拦, 避免被当成点行)
      const rb = t.closest && t.closest('button[data-ovrange]');
      if (rb) {
        const rg = rb.getAttribute('data-ovrange');
        if (rg !== st.optVolExpand.range) {
          st.optVolExpand.range = rg;
          // 仅更新该展开行内按钮高亮 + 重画图 (不重渲整表, 避免闪)
          const tg = rb.closest('[data-ovroletoggle]');
          if (tg) Array.prototype.forEach.call(tg.querySelectorAll('button'), function (x) { x.classList.toggle('on', x === rb); });
          _hmOptVolRenderCharts();
        }
        return;
      }
      // 展开行内部(非按钮)点击 → 不收起、不冒泡成切行
      if (t.closest && t.closest('tr.ovExpand')) return;
      // 表头排序
      const th = t.closest && t.closest('th[data-ovkey]');
      if (th) {
        const key = th.getAttribute('data-ovkey');
        const s = st.optVolSort || { key: 'median_vol', dir: -1 };
        if (s.key === key) s.dir = -s.dir; else { s.key = key; s.dir = (key === 'ticker') ? 1 : -1; }
        st.optVolSort = s;
        _hmRenderOptVolTable();
        return;
      }
      // 行 → 手风琴切换: 点已开的收起, 点别的先收旧再开新
      const row = t.closest && t.closest('tr.ovRow[data-ovticker]');
      if (row) {
        const tk = row.getAttribute('data-ovticker');
        if (!tk) return;
        if (st.optVolExpand.ticker === tk) _hmOptVolCollapse();
        else _hmOptVolExpandRow(tk);
        return;
      }
    });
  })();

  // ── 共享分析筛选 (β/相关 + 涨跌榜): 搜索高亮 + sector/industry 单选 ───────
  // State (anaSearch / anaSector / anaIndustry) is shared across both modes and
  // both views, so a highlight or filter survives every toggle until cleared.
  function _anaActiveItems() {
    if (window.hmState.dataMode === 'risk') return (_riskActiveData() || {}).items || [];
    if (window.hmState.dataMode === 'perf') return (window.hmState.perfData || {}).items || [];
    return [];
  }
  function _anaSearching() { return !!(window.hmState.anaSearch || '').trim(); }
  // Exact ticker match (case-insensitive). Typing "T" highlights only T, not
  // every ticker containing a T.
  function _anaHit(ticker) {
    const q = (window.hmState.anaSearch || '').trim().toUpperCase();
    return q ? String(ticker || '').toUpperCase() === q : false;
  }
  // Notice when a search matches nothing visible: distinguishes "filtered out"
  // from "no such ticker". Returns null when not searching or a hit is shown.
  function _anaNotice(anyHit) {
    if (!_anaSearching() || anyHit) return null;
    const q = (window.hmState.anaSearch || '').trim().toUpperCase();
    const exists = _anaActiveItems().some(it => String(it.ticker || '').toUpperCase() === q);
    return exists ? '“' + q + '” 不在当前板块/行业筛选里 (点 ⟲ 复位 看全部)' : '没有 “' + q + '” 这只 ticker';
  }
  // Draw a centered "not found" banner on a chart svg when the search misses.
  function _anaBanner(svg, cx, cy, anyHit) {
    const msg = _anaNotice(anyHit);
    if (!msg) return;
    const g = svg.append('g');
    const t = g.append('text').attr('x', cx).attr('y', cy).attr('text-anchor', 'middle')
      .attr('fill', '#f0883e').attr('font-size', 14).attr('font-weight', 700).text('⚠ ' + msg);
    let bb; try { bb = t.node().getBBox(); } catch (e) { return; }
    g.insert('rect', 'text').attr('x', bb.x - 12).attr('y', bb.y - 7)
      .attr('width', bb.width + 24).attr('height', bb.height + 14)
      .attr('rx', 6).attr('fill', 'rgba(13,17,23,0.95)').attr('stroke', '#f0883e').attr('stroke-width', 1);
  }
  function _anaFiltered() { return !!(window.hmState.anaSector || window.hmState.anaIndustry); }
  function _anaFilter(items) {
    const sec = window.hmState.anaSector, ind = window.hmState.anaIndustry;
    if (!sec && !ind) return items;
    return items.filter(it => (!sec || it.sector === sec) && (!ind || it.industry === ind));
  }
  function _anaRerender() { render(); }
  window.hmAnaSearch = function (v) { window.hmState.anaSearch = v || ''; _anaRerender(); };
  window.hmAnaSetSector = function (v) {
    window.hmState.anaSector = v || '';
    window.hmState.anaIndustry = '';        // reset industry when sector changes
    window.hmState.perfLayout = null;       // filter change → re-lay the swarm
    const ss = document.getElementById('hmAnaSector'); if (ss) ss.value = window.hmState.anaSector;
    _anaPopulateIndustry();
    _anaRerender();
  };
  window.hmAnaSetIndustry = function (v) {
    window.hmState.anaIndustry = v || '';
    window.hmState.perfLayout = null;
    const si = document.getElementById('hmAnaIndustry'); if (si) si.value = window.hmState.anaIndustry;
    _anaRerender();
  };
  function _anaPopulateSector() {
    const sel = document.getElementById('hmAnaSector'); if (!sel) return;
    const secs = Array.from(new Set(_anaActiveItems().map(it => it.sector).filter(s => s && s !== 'Unknown'))).sort();
    const cur = window.hmState.anaSector;
    sel.innerHTML = '<option value="">全部板块</option>' + secs.map(s => '<option value="' + hmEsc(s) + '"' + (s === cur ? ' selected' : '') + '>' + hmEsc(s) + '</option>').join('');
  }
  function _anaPopulateIndustry() {
    const sel = document.getElementById('hmAnaIndustry'); if (!sel) return;
    const sec = window.hmState.anaSector;
    const pool = sec ? _anaActiveItems().filter(it => it.sector === sec) : _anaActiveItems();
    const inds = Array.from(new Set(pool.map(it => it.industry).filter(s => s && s !== 'Unknown'))).sort();
    const cur = window.hmState.anaIndustry;
    sel.innerHTML = '<option value="">全部行业</option>' + inds.map(s => '<option value="' + hmEsc(s) + '"' + (s === cur ? ' selected' : '') + '>' + hmEsc(s) + '</option>').join('');
  }
  // Fill the dropdowns once per (mode,dataset) so options reflect the loaded data.
  function _anaEnsurePickers() {
    const key = window.hmState.dataMode + ':' + _anaActiveItems().length;
    if (window.hmState.anaPickKey === key) return;
    window.hmState.anaPickKey = key;
    _anaPopulateSector();
    _anaPopulateIndustry();
  }
  // Scroll the first highlighted row into view in a table container.
  function _anaScrollToHit(container) {
    if (!_anaSearching() || !container) return;
    const row = container.querySelector('tr[data-ana-hit="1"]');
    if (row && row.scrollIntoView) { try { row.scrollIntoView({ block: 'center' }); } catch (e) {} }
  }

  // ── β / 相关性 模式 (非实时, 每周更新) ─────────────────────────────────
  const _RISK_WIN_LABEL = {'3m':'3月', '6m':'6月', '1y':'1年'};
  const _RISK_WIN_LABEL_EN = {'3m':'3 mo', '6m':'6 mo', '1y':'1 yr'};
  window.hmSetRiskBench = function (b) {
    window.hmState.riskBench = (b === 'QQQ') ? 'QQQ' : 'SPY';
    document.querySelectorAll('#hmRiskBench button').forEach(x => {
      const on = x.dataset.bn === window.hmState.riskBench;
      x.style.background = on ? '#1f6feb' : 'transparent'; x.style.color = on ? '#fff' : '#8b949e';
    });
    render();
  };
  window.hmSetRiskWindow = function (w) {
    window.hmState.riskWindow = ['3m', '6m', '1y'].includes(w) ? w : '1y';
    document.querySelectorAll('#hmRiskWindow button').forEach(x => {
      const on = x.dataset.wn === window.hmState.riskWindow;
      x.style.background = on ? '#1f6feb' : 'transparent'; x.style.color = on ? '#fff' : '#8b949e';
    });
    render();
  };
  // The β/相关 data for the active universe (股票 vs ETF) — same shape, swapped data.
  function _riskActiveData() {
    return window.hmState.riskUniverse === 'etf' ? window.hmState.riskEtfData : window.hmState.riskData;
  }
  window.hmSetRiskUniverse = function (u) {
    window.hmState.riskUniverse = (u === 'etf') ? 'etf' : 'stocks';
    // sectors differ between 股票 and ETF, so clear the filter on switch.
    window.hmState.anaSector = ''; window.hmState.anaIndustry = ''; window.hmState.anaPickKey = '';
    const se = document.getElementById('hmAnaSector'); if (se) se.value = '';
    const ie = document.getElementById('hmAnaIndustry'); if (ie) ie.value = '';
    document.querySelectorAll('#hmRiskUniv button').forEach(x => {
      const on = x.dataset.uni === window.hmState.riskUniverse;
      x.style.background = on ? '#1f6feb' : 'transparent'; x.style.color = on ? '#fff' : '#8b949e';
    });
    if (!_riskActiveData()) { _hmLoadRisk(); return; }
    render();
  };
  function _hmLoadRisk() {
    const u = window.hmState.riskUniverse, slot = u === 'etf' ? 'riskEtfData' : 'riskData';
    if (window.hmState[slot]) { render(); return; }
    const status = document.getElementById('hmStatus');
    if (status) status.textContent = '计算 β / 相关性 中… (首次需下载日线, 约几秒)';
    fetch('/api/heatmap/risk_stats?universe=' + u).then(r => r.json()).then(d => {
      window.hmState[slot] = d;
      if (window.hmState.dataMode === 'risk') {
        render();
      }
    }).catch(e => { if (status) status.innerHTML = '<span style="color:#f85149">β/相关 加载失败: ' + e.message + '</span>'; });
  }
  // Pull {beta,corr,r2,vol} for the active benchmark + window off one item.
  function _riskRec(it) {
    const b = (it[window.hmState.riskBench] || {})[window.hmState.riskWindow] || null;
    const vol = (it.vol || {})[window.hmState.riskWindow];
    return { beta: b ? b.beta : null, corr: b ? b.corr : null, r2: b ? b.r2 : null,
             vol: (vol == null || isNaN(vol)) ? null : vol };
  }
  // β → color. Blue (defensive <1) → grey (≈ market =1) → red (amplifier >1).
  function _betaColor(beta) {
    if (beta == null || isNaN(beta)) return '#6e7681';
    const stops = [[88, 166, 255], [180, 190, 200], [248, 81, 73]];
    const x = Math.max(0, Math.min(2, beta));
    const i = x <= 1 ? 0 : 1, t = x <= 1 ? x : x - 1;
    const c0 = stops[i], c1 = stops[i + 1];
    return `rgb(${c0.map((c, j) => Math.round(c + (c1[j] - c) * t)).join(',')})`;
  }

  // Scatter "风险地图": X = 相关性 ρ, Y = 波动率 σ, 气泡=市值, 颜色=β.
  async function _hmRenderRiskScatter() {
    const d3 = await ensureD3();
    const svgEl = document.getElementById('hmSvg');
    const canvas = document.getElementById('hmCanvas');
    const tip = document.getElementById('hmTip');
    if (!svgEl || !canvas) return;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (W < 60 || H < 60) return;
    const svg = d3.select(svgEl).attr('viewBox', `0 0 ${W} ${H}`).attr('preserveAspectRatio', 'none');
    svg.selectAll('*').remove();
    svg.on('.zoom', null);   // drop any d3-zoom handlers left by the perf swarm
    const data = _riskActiveData();
    if (!data) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text('计算 β / 相关性 中…'); return; }
    const bench = window.hmState.riskBench, win = window.hmState.riskWindow, winLabel = _RISK_WIN_LABEL[win], winEn = _RISK_WIN_LABEL_EN[win];
    const isEtf = window.hmState.riskUniverse === 'etf';
    _anaEnsurePickers();

    const pts = [];
    _anaFilter(data.items || []).forEach(it => {
      const r = _riskRec(it);
      if (r.corr == null || r.vol == null || r.beta == null) return;
      pts.push({ it, corr: r.corr, vol: r.vol, beta: r.beta, r2: r.r2, mcap: it.market_cap || it.mcap || 0 });
    });
    if (!pts.length) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text(_anaFiltered() ? '该筛选下无数据' : '无数据'); return; }

    const m = { top: 72, right: 134, bottom: 56, left: 66 };
    const pw = W - m.left - m.right, ph = H - m.top - m.bottom;
    const cMin = Math.min(-0.25, d3.min(pts, p => p.corr));
    // A handful of meme / crypto / binary-biotech names have 300–700% vol and
    // would squash everyone else into a sliver. Cap the axis at the 95th pct
    // (those few clamp to the top edge) and use a sqrt scale so the bulk of the
    // universe — where 15–80% vol lives — actually spreads out and is readable.
    const sortedVol = pts.map(p => p.vol).slice().sort((a, b) => a - b);
    const pctile = q => sortedVol[Math.min(sortedVol.length - 1, Math.floor(sortedVol.length * q))];
    const medVol = pctile(0.5);
    const vMax = Math.max(50, pctile(0.95) * 1.1);
    const x = d3.scaleLinear().domain([cMin, 1.0]).range([m.left, m.left + pw]);
    const y = d3.scaleSqrt().domain([0, vMax]).range([m.top + ph, m.top]).clamp(true);
    // ETFs have no market cap → uniform bubble size; stocks → size by mcap.
    const uniformSize = isEtf || !pts.some(p => p.mcap > 1e6);
    const rad = uniformSize ? (function () { return 12; }) : d3.scaleSqrt().domain([1e8, d3.max(pts, p => p.mcap) || 1e12]).range([3.5, 26]).clamp(true);

    const benchVol = ((data.bench_vol || {})[bench] || {})[win];
    const yMid = y(medVol);            // split the cloud at its median volatility
    const xMid = x(0.5);

    // faint quadrant tints
    [[m.left, m.top, xMid - m.left, yMid - m.top, 'rgba(240,136,62,0.05)'],
     [xMid, m.top, m.left + pw - xMid, yMid - m.top, 'rgba(210,153,34,0.07)'],
     [m.left, yMid, xMid - m.left, m.top + ph - yMid, 'rgba(88,166,255,0.045)'],
     [xMid, yMid, m.left + pw - xMid, m.top + ph - yMid, 'rgba(63,185,80,0.05)']]
      .forEach(q => svg.append('rect').attr('x', q[0]).attr('y', q[1]).attr('width', Math.max(0, q[2])).attr('height', Math.max(0, q[3])).attr('fill', q[4]));

    // grid + tick labels
    x.ticks(7).forEach(t => {
      const px = x(t);
      svg.append('line').attr('x1', px).attr('x2', px).attr('y1', m.top).attr('y2', m.top + ph).attr('stroke', '#30363d').attr('stroke-width', 0.5).attr('opacity', 0.5);
      svg.append('text').attr('x', px).attr('y', m.top + ph + 16).attr('fill', '#8b949e').attr('font-size', 10).attr('text-anchor', 'middle').text(t.toFixed(1));
    });
    y.ticks(6).forEach(t => {
      const py = y(t);
      svg.append('line').attr('x1', m.left).attr('x2', m.left + pw).attr('y1', py).attr('y2', py).attr('stroke', '#30363d').attr('stroke-width', 0.5).attr('opacity', 0.5);
      svg.append('text').attr('x', m.left - 8).attr('y', py + 3).attr('fill', '#8b949e').attr('font-size', 10).attr('text-anchor', 'end').text(t.toFixed(0) + '%');
    });
    // emphasized reference lines: ρ=0.5 (跟盘紧/松) and median volatility (高/低波动)
    svg.append('line').attr('x1', xMid).attr('x2', xMid).attr('y1', m.top).attr('y2', m.top + ph).attr('stroke', '#484f58').attr('stroke-dasharray', '4 3');
    svg.append('line').attr('x1', m.left).attr('x2', m.left + pw).attr('y1', yMid).attr('y2', yMid).attr('stroke', '#484f58').attr('stroke-dasharray', '4 3');
    svg.append('text').attr('x', m.left + pw - 4).attr('y', yMid - 5).attr('fill', '#8b949e').attr('font-size', 10).attr('text-anchor', 'end').text(zt(`中位波动 ${medVol.toFixed(0)}%`, `median volatility ${medVol.toFixed(0)}%`));
    // benchmark's own volatility, for reference (where the index itself sits)
    if (benchVol != null && y(benchVol) < m.top + ph - 2) {
      svg.append('line').attr('x1', m.left).attr('x2', m.left + pw).attr('y1', y(benchVol)).attr('y2', y(benchVol)).attr('stroke', '#21262d').attr('stroke-dasharray', '2 4');
      svg.append('text').attr('x', m.left + 4).attr('y', y(benchVol) - 4).attr('fill', '#6e7681').attr('font-size', 9).text(zt(`${bench} 自身 ${benchVol.toFixed(0)}%`, `${bench} itself ${benchVol.toFixed(0)}%`));
    }

    // axis titles
    svg.append('text').attr('x', m.left + pw / 2).attr('y', H - 16).attr('fill', '#c9d1d9').attr('font-size', 12).attr('font-weight', 600).attr('text-anchor', 'middle').text(`与 ${bench} 的相关性 ρ  →  越右越跟大盘同步`);
    svg.append('text').attr('transform', `translate(18,${m.top + ph / 2}) rotate(-90)`).attr('fill', '#c9d1d9').attr('font-size', 12).attr('font-weight', 600).attr('text-anchor', 'middle').text('年化波动率 σ  →  越高越野');

    // quadrant captions
    const cap = (tx, ty, anchor, txt, col) => svg.append('text').attr('x', tx).attr('y', ty).attr('fill', col).attr('font-size', 10.5).attr('text-anchor', anchor).attr('opacity', 0.9).text(txt);
    cap(m.left + 6, m.top + 16, 'start', '高波动 · 独立 (特异性风险)', '#f0883e');
    cap(m.left + pw - 6, m.top + 16, 'end', '高波动 · 跟盘紧 (高β放大器 ⚠)', '#e3b341');
    cap(m.left + 6, m.top + ph - 8, 'start', '低波动 · 独立 (防御/分散)', '#58a6ff');
    cap(m.left + pw - 6, m.top + ph - 8, 'end', '低波动 · 稳跟大盘 (稳健)', '#3fb950');

    // title + how-to-read
    svg.append('text').attr('x', m.left).attr('y', 24).attr('fill', '#e6edf3').attr('font-size', 14).attr('font-weight', 700).text(zt(`风险地图 · ${isEtf ? 'ETF · ' : ''}vs ${bench} · ${winLabel} · ${pts.length} 只`, `Risk map · ${isEtf ? 'ETF · ' : ''}vs ${bench} · ${winEn} · ${pts.length} tickers`));
    svg.append('text').attr('x', m.left).attr('y', 44).attr('fill', '#8b949e').attr('font-size', 11)
      .text('↑波动越大  →越跟大盘同步  ●气泡=市值  颜色=β (蓝=防御<1 · 灰≈大盘 · 红=放大>1)');
    const nClamp = pts.filter(p => p.vol > vMax).length;
    if (nClamp) svg.append('text').attr('x', m.left).attr('y', 60).attr('fill', '#6e7681').attr('font-size', 10)
      .text(zt(`贴顶的 ${nClamp} 只波动 >${vMax.toFixed(0)}% (压缩在顶部, 悬停看真实值)`, `${nClamp} with volatility >${vMax.toFixed(0)}% are pinned to the top (hover for the real value)`));
    const status = document.getElementById('hmStatus');
    if (status) status.textContent = zt(`β/相关 · vs ${bench} · ${winLabel} · ${pts.length} 只 (非实时, 每周更新)`, `β / correlation · vs ${bench} · ${winEn} · ${pts.length} tickers (not live, updated weekly)`);

    // legend (right gutter): β color scale + bubble size hint
    const lx = m.left + pw + 18, lyTop = m.top + 4;
    svg.append('text').attr('x', lx).attr('y', lyTop).attr('fill', '#8b949e').attr('font-size', 10).text('β 颜色');
    [[0, zt('防御', 'defensive')], [0.5, ''], [1, zt('≈大盘', '≈ market')], [1.5, ''], [2, zt('放大', 'amplifies')]].forEach((bv, i) => {
      svg.append('rect').attr('x', lx).attr('y', lyTop + 8 + i * 16).attr('width', 12).attr('height', 12).attr('rx', 2).attr('fill', _betaColor(bv[0]));
      svg.append('text').attr('x', lx + 17).attr('y', lyTop + 18 + i * 16).attr('fill', '#8b949e').attr('font-size', 10).text(bv[0].toFixed(1) + (bv[1] ? '  ' + bv[1] : ''));
    });
    svg.append('text').attr('x', lx).attr('y', lyTop + 8 + 5 * 16 + 16).attr('fill', '#8b949e').attr('font-size', 10).text(uniformSize ? '● ETF (等大小)' : '● 气泡 = 市值');

    // bubbles — when the searched ticker is present, it keeps full colour + a
    // white ring and everyone else dims. If nothing matches we DON'T dim (a
    // sea of grey looks broken) and show a notice instead.
    const hov = window.hmState.riskHover;
    const searching = _anaSearching();
    const anyHit = searching && pts.some(p => _anaHit(p.it.ticker));
    svg.selectAll('circle.pt').data(pts, p => p.it.ticker).enter().append('circle').attr('class', 'pt')
      .attr('cx', p => x(Math.max(cMin, Math.min(1, p.corr)))).attr('cy', p => y(Math.min(vMax, p.vol)))
      .attr('r', p => rad(Math.max(1e8, p.mcap)))
      .attr('fill', p => _betaColor(p.beta))
      .attr('fill-opacity', p => anyHit ? (_anaHit(p.it.ticker) ? 0.95 : 0.1) : 0.72)
      .attr('stroke', p => (anyHit && _anaHit(p.it.ticker)) || p.it.ticker === hov ? '#fff' : 'rgba(1,4,9,0.8)')
      .attr('stroke-width', p => (anyHit && _anaHit(p.it.ticker)) ? 2.5 : (p.it.ticker === hov ? 2 : 0.8))
      .style('cursor', 'pointer')
      .on('click', function (event, p) { if (window.openTaTicker) window.openTaTicker(p.it.ticker); })
      .on('mousemove', function (event, p) {
        window.hmState.riskHover = p.it.ticker;
        d3.select(this).attr('stroke', '#fff').attr('stroke-width', 2).raise();
        const rect = canvas.getBoundingClientRect();
        tip.innerHTML = `<div style="font-weight:700;color:#58a6ff;font-size:12px">${hmEsc(p.it.ticker)} <span style="color:#8b949e;font-weight:400">${hmEsc(p.it.sector || '')}</span></div>` +
          `<div style="color:#8b949e;max-width:220px;white-space:normal">${hmEsc(p.it.name || '')}</div>` +
          `<div style="margin-top:4px">β <b>${p.beta.toFixed(2)}</b> · ρ <b>${p.corr.toFixed(2)}</b> · R² ${p.r2 != null ? p.r2.toFixed(2) : '—'}</div>` +
          `<div>年化波动 σ <b>${p.vol.toFixed(1)}%</b> · 市值 ${fmtMcap(p.mcap)}</div>` +
          `<div style="color:#6e7681;font-size:10px;margin-top:2px">vs ${bench} · ${winLabel}</div>`;
        tip.style.display = 'block';
        tip.style.left = Math.min(event.clientX - rect.left + 12, rect.width - tip.offsetWidth - 8) + 'px';
        tip.style.top = Math.min(event.clientY - rect.top + 12, rect.height - tip.offsetHeight - 8) + 'px';
      })
      .on('mouseleave', function () { window.hmState.riskHover = null; d3.select(this).attr('stroke', 'rgba(1,4,9,0.8)').attr('stroke-width', 0.8); tip.style.display = 'none'; });

    // ticker labels: big-caps by default; when filtered to a sector/industry
    // (few points) label them all so you can read each name; always label a
    // search hit.
    const fewEnough = pts.length <= 60;
    const labeled = pts.filter(p => _anaHit(p.it.ticker)
      || ((_anaFiltered() || uniformSize) && fewEnough)
      || rad(Math.max(1e8, p.mcap)) >= (_anaFiltered() ? 9 : 14));
    svg.selectAll('text.lbl').data(labeled, p => p.it.ticker).enter().append('text').attr('class', 'lbl')
      .attr('x', p => x(Math.max(cMin, Math.min(1, p.corr)))).attr('y', p => y(Math.min(vMax, p.vol)) + 3)
      .attr('text-anchor', 'middle').attr('pointer-events', 'none')
      .attr('fill', p => (anyHit && _anaHit(p.it.ticker)) ? '#ffd33d' : '#fff').attr('font-size', 10).attr('font-weight', 700)
      .attr('paint-order', 'stroke').attr('stroke', 'rgba(1,4,9,0.7)').attr('stroke-width', 2.5)
      .text(p => p.it.ticker);

    // "not found" banner on top (drawn last so nothing covers it)
    _anaBanner(svg, m.left + pw / 2, m.top + 30, anyHit);
  }


  // ── 涨跌榜 模式: 可缩放蜂群图 (非实时, 每日更新) ───────────────────────
  const _PERF_LABEL = { r3d: '3天', r1w: '1周', r1mo: '1月', r3mo: '3月', r1yr: '1年' };
  function _perfScale(key) { return { r3d: 6, r1w: 10, r1mo: 18, r3mo: 30, r1yr: 60 }[key] || 15; }
  window.hmSetPerfPeriod = function (k) {
    window.hmState.perfPeriod = _PERF_LABEL[k] ? k : 'r1mo';
    window.hmState.perfLayout = null;   // new period → re-lay the swarm
    document.querySelectorAll('#hmPerfPeriod button').forEach(x => {
      const on = x.dataset.pp === window.hmState.perfPeriod;
      x.style.background = on ? '#1f6feb' : 'transparent'; x.style.color = on ? '#fff' : '#8b949e';
    });
    render();
  };
  // 复位: clear the search highlight + sector/industry filter (and, in the
  // beeswarm, reset zoom — the re-render starts at identity). Shared by both
  // analytic modes.
  window.hmAnaClear = function () {
    window.hmState.anaSearch = '';
    window.hmState.anaSector = '';
    window.hmState.anaIndustry = '';
    window.hmState.perfLayout = null;
    const se = document.getElementById('hmAnaSearch'); if (se) se.value = '';
    const ss = document.getElementById('hmAnaSector'); if (ss) ss.value = '';
    const si = document.getElementById('hmAnaIndustry'); if (si) si.value = '';
    _anaPopulateIndustry();
    _anaRerender();
  };
  function _hmLoadPerf() {
    const status = document.getElementById('hmStatus');
    if (status) status.textContent = '加载涨跌榜中… (首次需下载日线, 约几秒)';
    return fetch('/api/heatmap/stock_perf').then(r => r.json()).then(d => {
      window.hmState.perfData = d;
      window.hmState.perfLayout = null;
      if (window.hmState.dataMode === 'perf') {
        render();
      }
    }).catch(e => { if (status) status.innerHTML = '<span style="color:#f85149">涨跌榜加载失败: ' + e.message + '</span>'; });
  }

  async function _hmRenderPerfSwarm() {
    const d3 = await ensureD3();
    const svgEl = document.getElementById('hmSvg'), canvas = document.getElementById('hmCanvas'), tip = document.getElementById('hmTip');
    if (!svgEl || !canvas) return;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    if (W < 60 || H < 60) return;
    const svg = d3.select(svgEl).attr('viewBox', `0 0 ${W} ${H}`).attr('preserveAspectRatio', 'none');
    svg.selectAll('*').remove();
    svg.on('.zoom', null);   // drop any d3-zoom handlers left by the perf swarm
    const data = window.hmState.perfData;
    if (!data) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text('加载涨跌榜中…'); return; }
    const key = window.hmState.perfPeriod, scale = _perfScale(key), pLabel = _PERF_LABEL[key] || key;
    _anaEnsurePickers();
    const pts = _anaFilter(data.items || []).filter(it => it[key] != null && !isNaN(it[key])).map(it => ({ it, ret: it[key], mcap: it.market_cap || 0 }));
    if (!pts.length) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text(_anaFiltered() ? '该筛选下无数据' : '无数据'); return; }

    const m = { top: 68, right: 20, bottom: 46, left: 20 };
    const pw = W - m.left - m.right, ph = H - m.top - m.bottom;
    const small = pts.length <= 140 || _anaFiltered();
    // X domain. For a small/filtered set use the ACTUAL range so every ticker is
    // on-screen (no clamping → nothing gets cut off at the edges). For the full
    // universe, bound at the 90th pct of |return| so a few +600% names don't
    // flatten the bulk (those clamp to the edges, noted below).
    const absR = pts.map(p => Math.abs(p.ret)).sort((a, b) => a - b);
    const bound = small
      ? Math.max(2, d3.max(pts, p => Math.abs(p.ret)) * 1.06)
      : Math.max(2, absR[Math.floor((absR.length - 1) * 0.90)]);
    // Inner horizontal padding so an edge bubble (and its label) stays fully
    // inside the clip instead of being sliced off at the frame.
    const padX = small ? 48 : 30;
    const x = d3.scaleLinear().domain([-bound, bound]).range([m.left + padX, m.left + pw - padX]);
    // Bigger bubbles when filtered/small → the band fills the height and names
    // are readable; small bubbles for the dense full universe.
    const rad = d3.scaleSqrt().domain([1e8, d3.max(pts, p => p.mcap) || 1e12]).range(small ? [5, 30] : [2.5, 17]).clamp(true);
    const cy = m.top + ph / 2;
    const clampRet = r => Math.max(-bound, Math.min(bound, r));

    // beeswarm layout — cached so 复位/resize don't re-jitter (re-sim only on
    // period or size change).
    let nodes;
    const lay = window.hmState.perfLayout;
    if (lay && lay.key === key && lay.W === W && lay.H === H && lay.n === pts.length) {
      nodes = lay.nodes;
    } else {
      nodes = pts.map(p => ({ it: p.it, ret: p.ret, mcap: p.mcap, r: rad(Math.max(1e8, p.mcap)) }));
      const sim = d3.forceSimulation(nodes)
        .force('x', d3.forceX(d => x(clampRet(d.ret))).strength(1))
        .force('y', d3.forceY(cy).strength(0.04))
        .force('collide', d3.forceCollide(d => d.r + 0.8).iterations(3))
        .stop();
      for (let i = 0; i < 200; i++) sim.tick();
      // Stretch the swarm vertically to fill ~82% of the plot height. A wide
      // universe otherwise packs into a thin ribbon with lots of dead space.
      // Stretching only increases gaps (never creates overlap), so it's safe.
      const ys = nodes.map(d => d.y);
      const yLo = Math.min.apply(null, ys), yHi = Math.max.apply(null, ys);
      const bandH = yHi - yLo, target = ph * 0.82;
      if (bandH > 1 && bandH < target) {
        const sc = Math.min(3.5, target / bandH), mid = (yLo + yHi) / 2;
        nodes.forEach(d => { d.y = cy + (d.y - mid) * sc; });
      }
      nodes.forEach(d => { d.y = Math.max(m.top + d.r, Math.min(m.top + ph - d.r, d.y)); });
      window.hmState.perfLayout = { key, W, H, n: pts.length, nodes };
    }

    svg.append('clipPath').attr('id', 'hmPerfClip').append('rect').attr('x', m.left).attr('y', m.top).attr('width', pw).attr('height', ph);
    const gAxis = svg.append('g');
    const gZoom = svg.append('g').attr('clip-path', 'url(#hmPerfClip)');
    const gLabels = svg.append('g').attr('clip-path', 'url(#hmPerfClip)').attr('pointer-events', 'none');

    const pr = (it, k) => { const v = it[k]; return (v == null || isNaN(v)) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%'; };
    const searching = _anaSearching();
    const anyHit = searching && nodes.some(d => _anaHit(d.it.ticker));
    gZoom.selectAll('circle').data(nodes, d => d.it.ticker).enter().append('circle')
      .attr('cx', d => x(clampRet(d.ret))).attr('cy', d => d.y).attr('r', d => d.r)
      .attr('fill', d => colorFor(d.ret, scale))
      .attr('fill-opacity', d => anyHit ? (_anaHit(d.it.ticker) ? 0.95 : 0.1) : 0.86)
      .attr('stroke', d => (anyHit && _anaHit(d.it.ticker)) ? '#fff' : 'rgba(1,4,9,0.6)')
      .attr('stroke-width', d => (anyHit && _anaHit(d.it.ticker)) ? 2.5 : 0.5).style('cursor', 'pointer')
      .on('click', function (event, d) { if (window.openTaTicker) window.openTaTicker(d.it.ticker); })
      .on('mousemove', function (event, d) {
        d3.select(this).attr('stroke', '#fff').attr('stroke-width', 1.5).raise();
        const it = d.it, rect = canvas.getBoundingClientRect();
        tip.innerHTML = `<div style="font-weight:700;color:#58a6ff;font-size:12px">${hmEsc(it.ticker)} <span style="color:#8b949e;font-weight:400">${hmEsc(it.sector || '')}</span></div>` +
          `<div style="color:#8b949e;max-width:220px;white-space:normal">${hmEsc(it.name || '')}</div>` +
          `<div style="margin-top:4px">3天 ${pr(it, 'r3d')} · 1周 ${pr(it, 'r1w')}</div>` +
          `<div>1月 ${pr(it, 'r1mo')} · 3月 ${pr(it, 'r3mo')} · 1年 ${pr(it, 'r1yr')}</div>` +
          `<div style="color:#6e7681;font-size:10px;margin-top:2px">市值 ${fmtMcap(it.market_cap)}</div>`;
        tip.style.display = 'block';
        tip.style.left = Math.min(event.clientX - rect.left + 12, rect.width - tip.offsetWidth - 8) + 'px';
        tip.style.top = Math.min(event.clientY - rect.top + 12, rect.height - tip.offsetHeight - 8) + 'px';
      })
      .on('mouseleave', function () { d3.select(this).attr('stroke', 'rgba(1,4,9,0.6)').attr('stroke-width', 0.5); tip.style.display = 'none'; });

    function drawAxis(t) {
      gAxis.selectAll('*').remove();
      const xz = t ? t.rescaleX(x) : x;
      xz.ticks(9).forEach(v => {
        const px = xz(v); if (px < m.left - 1 || px > m.left + pw + 1) return;
        const zero = Math.abs(v) < 1e-9;
        gAxis.append('line').attr('x1', px).attr('x2', px).attr('y1', m.top).attr('y2', m.top + ph)
          .attr('stroke', zero ? '#484f58' : '#1c2128').attr('stroke-width', zero ? 1 : 0.5).attr('stroke-dasharray', zero ? '4 3' : null);
        gAxis.append('text').attr('x', px).attr('y', m.top + ph + 16).attr('fill', zero ? '#8b949e' : '#6e7681').attr('font-size', 10).attr('text-anchor', 'middle').text((v > 0 ? '+' : '') + v.toFixed(0) + '%');
      });
    }
    function drawLabels(t) {
      const k = t ? t.k : 1;
      // big-caps by default; when filtered to a small set, label them all; a
      // search hit is always labelled (and tinted gold).
      const fewEnough = nodes.length <= 60;
      const thr = _anaFiltered() ? 6 : 9;
      const big = nodes.filter(d => _anaHit(d.it.ticker) || (_anaFiltered() && fewEnough) || d.r * k >= thr)
        .sort((a, b) => b.r - a.r).slice(0, 200);
      const sel = gLabels.selectAll('text').data(big, d => d.it.ticker);
      sel.exit().remove();
      sel.enter().append('text').attr('text-anchor', 'middle').attr('font-size', 10).attr('font-weight', 700)
        .attr('paint-order', 'stroke').attr('stroke', 'rgba(1,4,9,0.8)').attr('stroke-width', 2.5)
        .merge(sel)
        .attr('fill', d => (anyHit && _anaHit(d.it.ticker)) ? '#ffd33d' : '#fff')
        .attr('x', d => t ? t.applyX(x(clampRet(d.ret))) : x(clampRet(d.ret)))
        .attr('y', d => (t ? t.applyY(d.y) : d.y) + 3)
        .text(d => d.it.ticker);
    }

    const zoom = d3.zoom().scaleExtent([1, 15])
      .translateExtent([[m.left, m.top], [m.left + pw, m.top + ph]])
      .extent([[m.left, m.top], [m.left + pw, m.top + ph]])
      .on('zoom', (ev) => { gZoom.attr('transform', ev.transform); drawAxis(ev.transform); drawLabels(ev.transform); });
    svg.call(zoom).on('dblclick.zoom', null);
    window.hmState.perfZoom = zoom;
    // Start every (re)render at identity so the stored transform and the drawn
    // view never drift apart (a fresh render must not inherit a stale zoom).
    // This fires the zoom handler once → draws the axis + labels at k=1.
    svg.call(zoom.transform, d3.zoomIdentity);

    // title / how-to-read / gainers·losers tally (fixed, above the plot)
    const gTop = svg.append('g');
    gTop.append('text').attr('x', m.left).attr('y', 24).attr('fill', '#e6edf3').attr('font-size', 14).attr('font-weight', 700).text(`涨跌榜 · ${pLabel} · ${pts.length} 只`);
    gTop.append('text').attr('x', m.left).attr('y', 44).attr('fill', '#8b949e').attr('font-size', 11).text('每个气泡=一只股 · 横轴=涨跌% (左跌·右涨) · 大小=市值 · 滚轮缩放·拖拽平移看清重叠');
    const up = pts.filter(p => p.ret > 0).length, dn = pts.filter(p => p.ret < 0).length;
    gTop.append('text').attr('x', m.left + pw).attr('y', 22).attr('text-anchor', 'end').attr('font-size', 12).attr('font-weight', 700).attr('fill', '#3fb950').text(`▲ ${up}`);
    gTop.append('text').attr('x', m.left + pw).attr('y', 40).attr('text-anchor', 'end').attr('font-size', 12).attr('font-weight', 700).attr('fill', '#f85149').text(`▼ ${dn}`);
    const nClamp = pts.filter(p => Math.abs(p.ret) > bound).length;
    if (nClamp) gTop.append('text').attr('x', m.left).attr('y', 60).attr('fill', '#6e7681').attr('font-size', 10).text(`贴边的 ${nClamp} 只涨跌超 ±${bound.toFixed(0)}% (压在两端, 悬停看真实值)`);

    _anaBanner(svg, m.left + pw / 2, m.top + 30, anyHit);
    const status = document.getElementById('hmStatus');
    if (status) status.textContent = `涨跌榜 · ${pLabel} · ${pts.length} 只 · ▲${up} ▼${dn} (非实时, 每日更新)`;
  }


  async function _hmRenderEtf() {
    const d3 = await ensureD3();
    const svgEl = document.getElementById('hmSvg');
    const canvas = document.getElementById('hmCanvas');
    const tip = document.getElementById('hmTip');
    if (!svgEl || !canvas) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (w < 20 || h < 20) return;
    const svg = d3.select(svgEl).attr('viewBox', `0 0 ${w} ${h}`).attr('preserveAspectRatio', 'none');
    svg.selectAll('*').remove();
    svg.on('.zoom', null);   // drop any d3-zoom handlers left by the perf swarm
    const data = window.hmState.etfData;
    if (!data) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text('加载 ETF 趋势中…'); return; }
    const cat = window.hmState.etfCat, period = window.hmState.etfPeriod, scale = _etfScale(period);
    let items = (data.items || []).filter(it => it.category === cat && it.has_data);
    items = items.slice().sort((a, b) => (b[period] == null ? -1e9 : b[period]) - (a[period] == null ? -1e9 : a[period]));
    if (!items.length) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text('无数据'); return; }
    const root = d3.hierarchy({children: items.map(it => ({it, value: 1}))}).sum(d => d.value || 0);
    d3.treemap().size([w, h]).paddingInner(2).round(true)(root);
    const g = svg.selectAll('g.etf').data(root.leaves()).enter().append('g').attr('class', 'etf');
    g.append('rect')
      .attr('x', d => d.x0).attr('y', d => d.y0)
      .attr('width', d => Math.max(0, d.x1 - d.x0)).attr('height', d => Math.max(0, d.y1 - d.y0))
      .attr('fill', d => colorFor(d.data.it[period], scale))
      .attr('stroke', '#010409').attr('stroke-width', 0.8).attr('rx', 2)
      .style('cursor', 'pointer')
      .on('click', function (event, d) { if (window.openTaTicker) window.openTaTicker(d.data.it.ticker); })
      .on('mousemove', function (event, d) {
        const it = d.data.it; const rect = canvas.getBoundingClientRect();
        tip.innerHTML = `<div style="font-weight:700;color:#58a6ff;font-size:12px">${it.ticker}</div>` +
          `<div style="color:#8b949e;max-width:220px;white-space:normal">${it.name}</div>` +
          `<div style="margin-top:3px">3天 ${_etfPct(it.r3d)} · 7天 ${_etfPct(it.r7d)}</div>` +
          `<div>1月 ${_etfPct(it.r1mo)} · 3月 ${_etfPct(it.r3mo)} · 1年 ${_etfPct(it.r1yr)}</div>`;
        tip.style.display = 'block';
        tip.style.left = Math.min(event.clientX - rect.left + 12, rect.width - tip.offsetWidth - 8) + 'px';
        tip.style.top = Math.min(event.clientY - rect.top + 12, rect.height - tip.offsetHeight - 8) + 'px';
      }).on('mouseleave', () => { tip.style.display = 'none'; });
    g.append('text').attr('x', d => (d.x0 + d.x1) / 2).attr('y', d => (d.y0 + d.y1) / 2 - 3)
      .attr('text-anchor', 'middle').attr('font-weight', 700).attr('fill', '#fff').attr('pointer-events', 'none')
      .each(function (d) { const ww = d.x1 - d.x0, hh = d.y1 - d.y0; if (ww < 26 || hh < 20) { this.remove(); return; } d3.select(this).attr('font-size', Math.min(15, Math.max(9, Math.min(ww / 4, hh / 2.4)))).text(d.data.it.ticker); });
    g.append('text').attr('x', d => (d.x0 + d.x1) / 2).attr('y', d => (d.y0 + d.y1) / 2 + 12)
      .attr('text-anchor', 'middle').attr('font-weight', 600).attr('fill', 'rgba(255,255,255,0.9)').attr('pointer-events', 'none')
      .each(function (d) { const ww = d.x1 - d.x0, hh = d.y1 - d.y0; if (ww < 40 || hh < 34) { this.remove(); return; } d3.select(this).attr('font-size', Math.min(12, ww / 6)).text(_etfPct(d.data.it[period])); });
    const status = document.getElementById('hmStatus');
    if (status) { const t = data.generated_at ? data.generated_at.replace('T', ' ').replace('Z', ' UTC') : ''; status.innerHTML = zt(`ETF 趋势 · ${cat === 'SECTOR' ? '板块' : '国家/全球'} · 按 ${_ETF_PERIOD_LABEL[period]} 强度 · ${items.length} 只 · ${t} <span style="color:#8b949e">(非实时, 绿=leader 红=lagger)</span>`, `ETF trends · ${cat === 'SECTOR' ? 'sectors' : 'country / global'} · by ${_ETF_PERIOD_LABEL_EN[period]} strength · ${items.length} ETFs · ${t} <span style="color:#8b949e">(not live; green = leader, red = laggard)</span>`); }
  }

  // ETF bar chart (finviz-groups style): ranked diverging horizontal bars,
  // leader (top) → laggard (bottom), by the selected period. Compact.
  async function _hmRenderEtfBar() {
    const d3 = await ensureD3();
    const svgEl = document.getElementById('hmSvg');
    const canvas = document.getElementById('hmCanvas');
    const tip = document.getElementById('hmTip');
    if (!svgEl || !canvas) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (w < 20 || h < 20) return;
    const svg = d3.select(svgEl).attr('viewBox', `0 0 ${w} ${h}`).attr('preserveAspectRatio', 'none');
    svg.selectAll('*').remove();
    svg.on('.zoom', null);   // drop any d3-zoom handlers left by the perf swarm
    const data = window.hmState.etfData;
    if (!data) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text('加载 ETF 趋势中…'); return; }
    const cat = window.hmState.etfCat, period = window.hmState.etfPeriod, scale = _etfScale(period);
    let items = (data.items || []).filter(it => it.category === cat && it.has_data && it[period] != null);
    items = items.slice().sort((a, b) => b[period] - a[period]);
    if (!items.length) { svg.append('text').attr('x', 16).attr('y', 28).attr('fill', '#8b949e').attr('font-size', 13).text('无数据'); return; }
    const n = items.length, padTop = 6;
    const rowH = Math.max(12, Math.min(30, (h - padTop) / n));
    const labelW = 60, valW = 56, bz0 = labelW + 8, bz1 = w - valW - 6;
    const center = (bz0 + bz1) / 2, halfW = Math.max(10, (bz1 - bz0) / 2);
    const maxAbs = Math.max.apply(null, items.map(it => Math.abs(it[period])).concat([1]));
    svg.append('line').attr('x1', center).attr('x2', center).attr('y1', padTop).attr('y2', padTop + n * rowH).attr('stroke', '#30363d').attr('stroke-width', 1);
    const g = svg.selectAll('g.bar').data(items).enter().append('g').attr('class', 'bar')
      .style('cursor', 'pointer')
      .on('click', function (event, it) { if (window.openTaTicker) window.openTaTicker(it.ticker); })
      .on('mousemove', function (event, it) {
        const rect = canvas.getBoundingClientRect();
        tip.innerHTML = `<div style="font-weight:700;color:#58a6ff;font-size:12px">${it.ticker}</div><div style="color:#8b949e;max-width:220px;white-space:normal">${it.name}</div>` +
          `<div style="margin-top:3px">3天 ${_etfPct(it.r3d)} · 7天 ${_etfPct(it.r7d)}</div><div>1月 ${_etfPct(it.r1mo)} · 3月 ${_etfPct(it.r3mo)} · 1年 ${_etfPct(it.r1yr)}</div>`;
        tip.style.display = 'block';
        tip.style.left = Math.min(event.clientX - rect.left + 12, rect.width - tip.offsetWidth - 8) + 'px';
        tip.style.top = Math.min(event.clientY - rect.top + 12, rect.height - tip.offsetHeight - 8) + 'px';
      }).on('mouseleave', () => { tip.style.display = 'none'; });
    g.each(function (it, i) {
      const sel = d3.select(this);
      const cy = padTop + i * rowH + rowH / 2, bh = Math.max(6, rowH * 0.66);
      const v = it[period], len = Math.abs(v) / maxAbs * halfW, pos = v >= 0;
      sel.append('rect').attr('x', pos ? center : center - len).attr('y', cy - bh / 2)
        .attr('width', Math.max(1, len)).attr('height', bh).attr('fill', colorFor(v, scale)).attr('rx', 1);
      sel.append('text').attr('x', 4).attr('y', cy + 3).attr('fill', '#e6edf3')
        .attr('font-size', Math.min(12, rowH * 0.55)).attr('font-weight', 700).text(it.ticker);
      sel.append('text').attr('x', w - 6).attr('y', cy + 3).attr('text-anchor', 'end')
        .attr('fill', pos ? '#3fb950' : '#f85149').attr('font-size', Math.min(11, rowH * 0.52)).attr('font-weight', 600).text(_etfPct(v));
    });
    const status = document.getElementById('hmStatus');
    if (status) { const t = data.generated_at ? data.generated_at.replace('T', ' ').replace('Z', ' UTC') : ''; status.innerHTML = `ETF 趋势(柱状) · ${cat === 'SECTOR' ? '板块' : '国家/全球'} · 按 ${_ETF_PERIOD_LABEL[period]} 排序 · ${n} 只 · ${t} <span style="color:#8b949e">(非实时)</span>`; }
  }


  // ── Build nested d3 hierarchy ────────────────────────────────────────
  function buildHierarchy(items, groupBy, sizeMode, compress) {
    // size accessor per mode
    const sizeOf = (it) => {
      if (sizeMode === 'equal') return 1;
      if (sizeMode === 'absmove') return Math.max(0.05, Math.abs(_hmChg(it)));
      // mcap default — floor so tiny caps still get visible rects.
      // 始终立方根(∛)压缩(作者要求): 比 √ 压得更狠, mega-cap 对小盘的碾压更弱,
      // 保留市值排序但差距进一步缩小、更均匀。(compress 参数保留但不再区分顶层/下钻。)
      const mc = Math.max(it.market_cap || 0, 1e8);
      return Math.cbrt(mc);
    };

    const root = { name: 'Market', children: [] };

    if (groupBy === 'sector_industry') {
      const bySec = new Map();
      items.forEach(it => {
        if (!bySec.has(it.sector)) bySec.set(it.sector, new Map());
        const ind = bySec.get(it.sector);
        if (!ind.has(it.industry)) ind.set(it.industry, []);
        ind.get(it.industry).push(it);
      });
      for (const [sec, indMap] of bySec) {
        const secNode = { name: sec, children: [] };
        for (const [ind, list] of indMap) {
          secNode.children.push({
            name: ind,
            children: list.map(it => ({ name: it.ticker, value: sizeOf(it), it }))
          });
        }
        root.children.push(secNode);
      }
    } else {
      const key = groupBy === 'sector' ? 'sector' : 'industry';
      const groups = new Map();
      items.forEach(it => {
        const g = it[key];
        if (!groups.has(g)) groups.set(g, []);
        groups.get(g).push(it);
      });
      for (const [g, list] of groups) {
        root.children.push({
          name: g,
          children: list.map(it => ({ name: it.ticker, value: sizeOf(it), it }))
        });
      }
    }
    return root;
  }

  // ── Render ───────────────────────────────────────────────────────────
  async function render() {
    // 📈 Graph 视图是按需渲染(切视图/筛选/排序时),实时 poll 不重画整墙 → 直接跳过。
    if (window.hmState.view === 'graph') return;
    if (window.hmState.dataMode === 'risk') {
      return _hmRenderRiskScatter();
    }
    if (window.hmState.dataMode === 'perf') {
      return _hmRenderPerfSwarm();
    }
    if (window.hmState.dataMode === 'etf') {
      return (window.hmState.view === 'bar') ? _hmRenderEtfBar() : _hmRenderEtf();
    }
    const data = window.hmState.lastData;
    if (!data) return;

    const d3 = await ensureD3();
    const svgEl = document.getElementById('hmSvg');
    const canvas = document.getElementById('hmCanvas');
    const tip = document.getElementById('hmTip');
    if (!svgEl || !canvas) return;

    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (w < 20 || h < 20) return;

    let groupBy = 'sector_industry';   // Group 控件已删, 固定 Sector › Industry
    const sizeMode = 'mcap';           // Size 控件已删, 固定 √市值(buildHierarchy 里 sqrt)
    const eqMode = false;
    const scale = 3;                   // Range 控件已删, 固定 ±3%

    // Only include tickers we actually have a quote for; otherwise the
    // treemap is dominated by grey "unknown" rects from yfinance gaps.
    // Then apply the user's Sector / Industry filter, if any.
    let items = _hmApplyFilter((data.items || []).filter(it =>
      it.market_cap > 0 && it.sector && it.sector !== 'Unknown' && it.has_quote
    ));

    // Drill-down: click a sector header → zoom into that sector (grouped by
    // industry); click an industry header → zoom into that one industry (flat,
    // every ticker labelled). Auto-exit a level if it vanished from the data.
    const drill = window.hmState.drillSector;
    if (drill) {
      const sub = items.filter(it => it.sector === drill);
      if (sub.length === 0) { window.hmState.drillSector = null; window.hmState.drillIndustry = null; }
      else {
        items = sub; groupBy = 'industry';
        const ind = window.hmState.drillIndustry;
        if (ind) {
          const sub2 = items.filter(it => it.industry === ind);
          if (sub2.length === 0) window.hmState.drillIndustry = null;
          else items = sub2;
        }
      }
    } else if (window.hmState.drillIndustry) {
      window.hmState.drillIndustry = null;   // industry can't be drilled without a sector
    }
    _hmRenderCrumb(window.hmState.drillSector, window.hmState.drillIndustry, items.length);

    // 筛选把票筛空了 → 给个明确提示, 别画一张空白 SVG(作者分不清是没数据还是筛太狠)。
    // graph 视图早有「当前筛选无票」提示, treemap 一直是纯白页 → 对齐。
    const svgSel = d3.select(svgEl);
    if (!items.length) {
      svgSel.selectAll('*').remove();
      svgSel.attr('viewBox', '0 0 ' + w + ' ' + h);
      svgSel.append('text').attr('x', w / 2).attr('y', h / 2)
        .attr('text-anchor', 'middle').attr('fill', '#6e7681').attr('font-size', 14)
        .text('当前筛选无票 — 放宽筛选或点「全部」');
      return;
    }

    const root = d3.hierarchy(buildHierarchy(items, groupBy, sizeMode, !!drill))
      .sum(d => d.value || 0)
      .sort((a, b) => (b.value || 0) - (a.value || 0));

    d3.treemap()
      .size([w, h])
      .paddingOuter(eqMode ? 1 : 2)
      .paddingTop(d => {
        if (eqMode) return d.depth === 1 ? 11 : (d.depth === 2 ? 9 : 0);
        return d.depth === 1 ? 16 : (d.depth === 2 ? 12 : 1);
      })
      .paddingInner(eqMode ? 0.5 : 1)
      .round(true)(root);

    const svg = d3.select(svgEl)
      .attr('viewBox', `0 0 ${w} ${h}`)
      .attr('preserveAspectRatio', 'none');
    svg.selectAll('*').remove();
    svg.on('.zoom', null);   // drop any d3-zoom handlers left by the perf swarm

    // ── Group frames (sector + industry) ──
    const frames = root.descendants().filter(d => d.depth >= 1 && d.children);
    svg.selectAll('g.frame')
      .data(frames)
      .enter().append('g').attr('class', 'frame')
      .each(function (d) {
        const g = d3.select(this);
        g.append('rect')
          .attr('x', d.x0).attr('y', d.y0)
          .attr('width', Math.max(0, d.x1 - d.x0))
          .attr('height', Math.max(0, d.y1 - d.y0))
          .attr('fill', 'none')
          .attr('stroke', d.depth === 1 ? '#58a6ff' : '#30363d')
          .attr('stroke-width', d.depth === 1 ? 1.2 : 0.6)
          .attr('rx', 2);
        const label = d.data.name;
        const fontSize = d.depth === 1 ? (eqMode ? 8 : 11) : (eqMode ? 7 : 9);
        const labelW = (d.x1 - d.x0);
        if (labelW > (eqMode ? 30 : 60)) {
          g.append('text')
            .attr('x', d.x0 + (eqMode ? 3 : 5))
            .attr('y', d.y0 + (d.depth === 1 ? (eqMode ? 8 : 11) : (eqMode ? 7 : 9)))
            .attr('fill', d.depth === 1 ? '#58a6ff' : '#8b949e')
            .attr('font-size', fontSize)
            .attr('font-weight', d.depth === 1 ? 700 : 600)
            .attr('font-family', 'system-ui, sans-serif')
            .text(label.length > Math.floor(labelW / 6) ? label.slice(0, Math.floor(labelW / 6) - 1) + '…' : label);
        }
        // Drill affordance on the depth-1 header band: in the overview the
        // frames are SECTORS (click → drill into sector); once drilled into a
        // sector the frames are INDUSTRIES (click → drill into that industry).
        if (d.depth === 1) {
          const gb = 'sector_industry';   // Group 控件已删, 固定
          const st = window.hmState;
          let drillFn = null;
          if (!st.drillSector && (gb === 'sector_industry' || gb === 'sector')) {
            const secName = d.data.name; drillFn = () => window.hmDrillSector(secName);
          } else if (st.drillSector && !st.drillIndustry) {
            const indName = d.data.name; drillFn = () => window.hmDrillIndustry(indName);
          }
          if (drillFn) {
            g.append('rect')
              .attr('x', d.x0).attr('y', d.y0)
              .attr('width', Math.max(0, d.x1 - d.x0))
              .attr('height', Math.min(eqMode ? 11 : 16, Math.max(0, d.y1 - d.y0)))
              .attr('fill', 'transparent')
              .style('cursor', 'zoom-in')
              .on('mouseover', function () { d3.select(this).attr('fill', 'rgba(88,166,255,0.14)'); })
              .on('mouseout',  function () { d3.select(this).attr('fill', 'transparent'); })
              .on('click', function (event) { event.stopPropagation(); drillFn(); });
          }
        }
      });

    // ── Leaves (tickers) ──
    const leaves = root.leaves();
    const leafG = svg.selectAll('g.leaf')
      .data(leaves)
      .enter().append('g').attr('class', 'leaf');

    leafG.append('rect')
      .attr('x', d => d.x0)
      .attr('y', d => d.y0)
      .attr('width', d => Math.max(0, d.x1 - d.x0))
      .attr('height', d => Math.max(0, d.y1 - d.y0))
      .attr('fill', d => colorFor(_hmChg(d.data.it), scale))
      .attr('stroke', '#010409')
      .attr('stroke-width', 0.5)
      .style('cursor', 'pointer')
      .on('click', function (event, d) {
        const it = d.data.it;
        if (it && it.ticker && window.openTaTicker) { event.stopPropagation(); window.openTaTicker(it.ticker); }
      })
      .on('mousemove', function (event, d) {
        const it = d.data.it;
        const rect = canvas.getBoundingClientRect();
        tip.innerHTML =
          `<div style="font-weight:700;color:#58a6ff;font-size:12px">${it.ticker}</div>` +
          `<div style="color:#8b949e;max-width:240px;white-space:normal">${it.name}</div>` +
          `<div>${fmtPrice(_hmPx(it))} &nbsp; <span style="color:${_hmChg(it) >= 0 ? '#3fb950' : '#f85149'}">${fmtPct(_hmChg(it))}</span></div>` +
          `<div style="color:#8b949e">Mcap: ${fmtMcap(it.market_cap)}</div>` +
          `<div style="color:#8b949e">${it.sector} › ${it.industry}</div>`;
        tip.style.display = 'block';
        const tx = event.clientX - rect.left + 12;
        const ty = event.clientY - rect.top + 12;
        tip.style.left = Math.min(tx, rect.width - tip.offsetWidth - 8) + 'px';
        tip.style.top  = Math.min(ty, rect.height - tip.offsetHeight - 8) + 'px';
      })
      .on('mouseleave', () => { tip.style.display = 'none'; });

    // ticker text label (only if rect big enough)
    leafG.append('text')
      .attr('x', d => (d.x0 + d.x1) / 2)
      .attr('y', d => (d.y0 + d.y1) / 2 - 2)
      .attr('text-anchor', 'middle')
      .attr('font-family', 'system-ui, sans-serif')
      .attr('font-weight', 700)
      .attr('fill', '#ffffff')
      .attr('pointer-events', 'none')
      .each(function (d) {
        const w = d.x1 - d.x0, h = d.y1 - d.y0;
        if (w < 6 || h < 5) { this.remove(); return; }
        const tk = d.data.it.ticker;
        // 字号严格按盒子宽度缩到「刚好放得下、不超出去」(每字符≈0.66×fs, 留 3px 边),
        // 无下限 → 宁可极小也不溢出重叠。小于 2.6px 看不清就放弃。
        const fsW = (w - 3) / (Math.max(2, tk.length) * 0.66);
        const fs = Math.min(13, Math.min(fsW, h / 2.3));
        if (fs < 2.6) { this.remove(); return; }
        d3.select(this).attr('font-size', fs).text(tk);
      });

    // % change text (under ticker, only if very big)
    leafG.append('text')
      .attr('x', d => (d.x0 + d.x1) / 2)
      .attr('y', d => (d.y0 + d.y1) / 2 + 11)
      .attr('text-anchor', 'middle')
      .attr('font-family', 'system-ui, sans-serif')
      .attr('font-weight', 600)
      .attr('fill', 'rgba(255,255,255,0.85)')
      .attr('pointer-events', 'none')
      .each(function (d) {
        const w = d.x1 - d.x0, h = d.y1 - d.y0;
        const mw = eqMode ? 28 : 44, mh = eqMode ? 22 : 36;
        if (w < mw || h < mh) { this.remove(); return; }
        d3.select(this).attr('font-size', Math.min(11, Math.max(eqMode ? 5 : 8, w / 6))).text(fmtPct(_hmChg(d.data.it)));
      });
  }

  // ── Sector drill-down ────────────────────────────────────────────────
  // Click a sector title in the treemap → zoom into just that sector so every
  // ticker gets a labelled tile (the overview is too dense to label small caps).
  // The breadcrumb's back button returns to the full overview.
  window.hmDrillSector = function (name) {
    if (!name) return;
    window.hmState.drillSector = name;
    window.hmState.drillIndustry = null;
    if (window.hmState.view !== 'treemap') hmSetView('treemap'); // also renders
    else render();
  };
  window.hmDrillIndustry = function (name) {
    if (!name) return;
    window.hmState.drillIndustry = name;
    if (window.hmState.view !== 'treemap') hmSetView('treemap');
    else render();
  };
  window.hmDrillToSector = function () {   // step back up: industry → sector level
    window.hmState.drillIndustry = null;
    render();
  };
  window.hmDrillClear = function () {
    window.hmState.drillSector = null;
    window.hmState.drillIndustry = null;
    _hmRenderCrumb(null, null, 0);
    render();
  };
  function _hmRenderCrumb(sector, industry, count) {
    const el = document.getElementById('hmCrumb');
    if (!el) return;
    if (!sector) { el.style.display = 'none'; return; }
    el.style.display = 'inline-flex';
    const nm = document.getElementById('hmCrumbName');
    const ct = document.getElementById('hmCrumbCount');
    if (nm) {
      nm.innerHTML = industry
        ? '<span style="color:#58a6ff;cursor:pointer;text-decoration:underline;text-underline-offset:2px" onclick="hmDrillToSector()">' + hmEsc(sector) + '</span>'
          + '<span style="color:#8b949e"> › </span><span style="color:#e6edf3">' + hmEsc(industry) + '</span>'
        : '<span style="color:#e6edf3">' + hmEsc(sector) + '</span>';
    }
    if (ct) ct.textContent = count ? '· ' + count + ' 只' : '';
  }

  // 市值筛选(compact 精选)。0=全部 / 10=≥10B / 100=大盘≥100B。重渲当前视图。
  window.hmSetMcapMin = function (billions) {
    const b = Number(billions) || 0;
    window.hmState.mcapMin = b * 1e9;
    document.querySelectorAll('#hmMcapToggle button').forEach(btn => {
      const on = Number(btn.dataset.mc) === b;
      btn.style.background = on ? '#1f6feb' : 'transparent';
      btn.style.color      = on ? '#fff'    : '#8b949e';
    });
    const v = window.hmState.view;
    if (v === 'graph') hmRenderGraph();
    else render();
  };

  // ── View toggle: treemap ↔ bar ↔ table ↔ graph ──────────────────────
  window.hmSetView = function (mode) {
    // ⚠️ 切视图必须先关单票详情浮层 —— 它是 position:absolute;inset:0;z-index:70,
    //    不关就直接盖在新渲染的视图上(作者报的「点股票→点Graph→再点股票 重叠」)。
    //    hmSetView 没有自动刷新调用方(调用点全是按钮/模式自校正), 这里关不会误关。
    if (window.hmCloseTickerDetail) window.hmCloseTickerDetail();
    const VIEWS = ['treemap', 'bar', 'graph', 'movernews'];
    const view = VIEWS.indexOf(mode) >= 0 ? mode : 'treemap';
    window.hmState.view = view;
    const ttl = document.getElementById('hmTitle');
    if (ttl) ttl.textContent = (view === 'treemap') ? '🎯 选股 · 热力图' : '🎯 选股 · K线图墙';
    const isGraph = (view === 'graph');
    const isMv    = (view === 'movernews');
    document.getElementById('hmCanvas').style.display = (isGraph || isMv) ? 'none' : '';
    const gv = document.getElementById('hmGraphView');
    if (gv) gv.style.display = isGraph ? 'flex' : 'none';
    const mv = document.getElementById('hmMoverNewsView');
    if (mv) mv.style.display = isMv ? 'flex' : 'none';
    // Style the toggle pills (treemap/bar/graph 互斥组)
    const tog = document.getElementById('hmViewToggle');
    if (tog) {
      tog.dataset.view = view;
      tog.querySelectorAll('button').forEach(b => {
        const active = b.dataset.view === view;
        b.style.background = active ? '#1f6feb' : 'transparent';
        b.style.color      = active ? '#fff'    : '#8b949e';
      });
    }
    // 📰 Mover News 是独立按钮(不在互斥组里),单独高亮
    const mb = document.getElementById('hmMoverNewsBtn');
    if (mb) {
      mb.style.background = isMv ? '#1f6feb' : 'transparent';
      mb.style.color      = isMv ? '#fff'    : '#8b949e';
    }
    if (isGraph) {
      hmRenderGraph();          // 📈 candle 网格(3-5/行,懒拉 OHLC)
      if (_hmAiPicks === null && window.hmLoadAiPicks) window.hmLoadAiPicks();  // 🚀 首次进图墙载 AI 徽章
    } else if (isMv) {
      hmRenderMoverNews();      // 📰 异动新闻表格
    } else {
      render();   // treemap or (ETF) bar — dispatched inside render()
    }
  };

  // ════════════════════════════════════════════════════════════════════
  // 📈 Graph view — K线图墙(复用 ta_screener 自绘 candle + S/R + EMA + 评分)
  //   · 数据:/api/ta/ohlc/{t}(TA 池读 parquet 缓存,池外大盘股按需 yfinance)
  //   · 懒加载:IntersectionObserver,卡片进视口才拉;并发节流 ≤4
  //   · 评分:/api/ta/results 一次性 join(多/空 + 0-100)
  // ════════════════════════════════════════════════════════════════════
  const _hmOhlcCache = {};        // 'TICKER_126' -> ohlc json(含 zones)
  let _hmGraphScores = null;       // ticker -> 完整 TA 行(alias of _hmTaRows; graph 读 .score / .score_detail.dir)
  let _hmTaRows = null;            // ticker -> 完整 slim TA 行(score/score_detail/touch/setup/earnings) — 🔎 全局筛选 + graph 共用
  const _HM_GRAPH_MAXCONC = 4;     // 同时在拉的 OHLC 上限(别打爆 yfinance)

  window.hmSetGraphDays = function (v) { window.hmState.graphDays = Number(v) || 126; hmRenderGraph(); };
  window.hmSetGraphSort = function (v) { window.hmState.graphSort = v || 'mcap'; hmRenderGraph(); };
  window.hmSetGraphCols = function (v) { window.hmState.graphCols = v || 'auto'; _hmApplyGraphCols(); hmRenderGraph(); };
  window.hmSetGraphAiFilter = function (v) { window.hmState.graphAiFilter = v || 'all'; hmRenderGraph(); };

  // ── 📊 Barchart 榜单多选(图墙): 按类别分组勾选 → 交集/并集 → hmState.taBcTickers ──
  //   数据: /api/barchart/screeners/tagged(consolidate 产出), 首次点开控件时懒拉一次缓存。
  //   选中 tickers 写 hmState.taBcTickers(大写字符串数组, 无选中=null), 复用 _hmApplyTaFilter。
  let _hmBcData = null;         // 端点缓存 {screeners,categories,by_ticker,ai_universe_add}
  let _hmBcBySlug = null;       // slug -> screener 对象(含 tickers)
  const _hmBcSel = new Set();   // 勾选的 screener slug
  let _hmBcMode = 'intersect';  // 'intersect' | 'union'
  let _hmBcLoaded = false;

  window.hmLoadBarchartTagged = function () {
    if (_hmBcData) return Promise.resolve(_hmBcData);
    return fetch('/api/barchart/screeners/tagged').then(function (r) { return r.json(); }).then(function (d) {
      _hmBcData = d || {};
      _hmBcBySlug = {};
      (_hmBcData.screeners || []).forEach(function (s) { if (s && s.slug) _hmBcBySlug[s.slug] = s; });
      _hmBcLoaded = true;
      _hmBcRenderList();
      return _hmBcData;
    }).catch(function () {
      _hmBcData = { screeners: [] }; _hmBcBySlug = {}; _hmBcLoaded = true; _hmBcRenderList();
      return _hmBcData;
    });
  };

  function _hmBcRenderList() {
    const box = document.getElementById('hmBcList');
    if (!box) return;
    const scr = (_hmBcData && _hmBcData.screeners) || [];
    if (!scr.length) {
      box.innerHTML = '<div style="color:#6e7681;font-size:11px;padding:8px">暂无榜单数据：先到 TA 扫描页(/ta/)的 📊 Barchart 菜单里爬取你保存的 screener</div>';
      return;
    }
    const cats = (_hmBcData && _hmBcData.categories) || {};
    // 按 category 归组; 组顺序: 先按 categories 字典顺序(registry category_order), 再补未归类
    const byCat = {};
    scr.forEach(function (s) { const c = s.category || 'other'; (byCat[c] = byCat[c] || []).push(s); });
    const order = Object.keys(cats).filter(function (c) { return byCat[c]; });
    Object.keys(byCat).forEach(function (c) { if (order.indexOf(c) < 0) order.push(c); });
    let html = '';
    order.forEach(function (c) {
      const list = byCat[c]; const meta = cats[c] || {};
      const label = hmEsc((list[0] && list[0].category_label) || meta.label || c);
      const col = (list[0] && list[0].color) || meta.color || '#8b949e';
      html += '<div>'
        + '<div style="font-size:10px;font-weight:700;letter-spacing:.4px;color:' + col
        + ';border-left:3px solid ' + col + ';padding:2px 0 2px 6px;margin-bottom:3px">' + label + '</div>';
      list.forEach(function (s) {
        const checked = _hmBcSel.has(s.slug) ? ' checked' : '';
        const cnt = (s.count != null) ? s.count : ((s.tickers || []).length);
        html += '<label style="display:flex;align-items:center;gap:6px;padding:2px 6px;cursor:pointer;font-size:11px;color:#e6edf3">'
          + '<input type="checkbox" data-bc-slug="' + hmEsc(s.slug) + '"' + checked + ' style="cursor:pointer;flex:none">'
          + '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + hmEsc(s.name || s.slug) + '</span>'
          + '<span style="color:#6e7681;font-size:10px;flex:none">(' + cnt + ')</span>'
          + '</label>';
      });
      html += '</div>';
    });
    box.innerHTML = html;
    // 事件委托: 勾选/取消 → 重算(避免内联 onclick 转义坑, 用 data-bc-slug)
    box.querySelectorAll('input[data-bc-slug]').forEach(function (cb) {
      cb.onchange = function () {
        const slug = cb.getAttribute('data-bc-slug');
        if (cb.checked) _hmBcSel.add(slug); else _hmBcSel.delete(slug);
        _hmBcRecompute();
      };
    });
  }

  function _hmBcComputeTickers() {
    if (!_hmBcSel.size || !_hmBcBySlug) return null;
    let acc = null;
    _hmBcSel.forEach(function (slug) {
      const s = _hmBcBySlug[slug];
      const set = new Set(((s && s.tickers) || []).map(function (t) { return String(t).toUpperCase(); }));
      if (acc === null) { acc = set; return; }
      if (_hmBcMode === 'union') { set.forEach(function (t) { acc.add(t); }); }
      else { acc = new Set([...acc].filter(function (t) { return set.has(t); })); }
    });
    return acc ? [...acc] : null;
  }

  function _hmBcRecompute() {
    const tk = _hmBcComputeTickers();
    // 大写数组 / null(未选,不过滤) / 哨兵(已选但交集为空 → 图墙显空而非显全)
    window.hmState.taBcTickers = (tk && tk.length) ? tk : (_hmBcSel.size ? ['__BC_NO_MATCH__'] : null);
    _hmBcUpdateSummary(tk);
    const v = window.hmState.view;
    if (v === 'graph') hmRenderGraph();          // 图墙专用重渲染(render() 在 graph 视图 no-op)
    else render();
  }

  function _hmBcUpdateSummary(tk) {
    const n = _hmBcSel.size;
    const hits = tk ? tk.length : 0;
    const modeTxt = (_hmBcMode === 'union') ? '并集' : '交集';
    const sm = document.getElementById('hmBcSummary');
    if (sm) sm.textContent = n ? ('· ' + n + '选 ' + modeTxt + ' · ' + hits + '票') : '';
    const hb = document.getElementById('hmBcHits');
    if (hb) hb.textContent = n ? (modeTxt + ' → ' + hits + ' 票') : '未选';
    const btn = document.getElementById('hmBcBtn');
    if (btn) {
      const on = n > 0;
      btn.style.background = on ? '#1f6feb' : '#161b22';
      btn.style.color = on ? '#fff' : '#e6edf3';
      btn.style.borderColor = on ? '#1f6feb' : '#30363d';
    }
  }

  window.hmBcSetMode = function (m) {
    _hmBcMode = (m === 'union') ? 'union' : 'intersect';
    const tog = document.getElementById('hmBcModeToggle');
    if (tog) tog.querySelectorAll('button').forEach(function (b) {
      const active = b.getAttribute('data-bcmode') === _hmBcMode;
      b.style.background = active ? '#1f6feb' : 'transparent';
      b.style.color = active ? '#fff' : '#8b949e';
    });
    _hmBcRecompute();
  };

  window.hmBcClear = function () {
    _hmBcSel.clear();
    _hmBcRenderList();
    _hmBcRecompute();
  };

  window.hmBcTogglePanel = function () {
    const p = document.getElementById('hmBcPanel');
    if (!p) return;
    const open = (p.style.display === 'none' || !p.style.display);
    p.style.display = open ? 'block' : 'none';
    if (open) {
      // 按钮在工具栏右侧时, 左对齐的 320px 面板会伸出窗口 → 改成右对齐, 并且不超过窗口宽
      p.style.maxWidth = 'calc(100vw - 24px)';
      p.style.left = '0'; p.style.right = 'auto';
      if (p.getBoundingClientRect().right > window.innerWidth - 8) { p.style.left = 'auto'; p.style.right = '0'; }
      if (!_hmBcLoaded) hmLoadBarchartTagged(); else _hmBcRenderList();
    }
  };

  // 点面板外 → 关闭(捕获冒泡; 点按钮/面板内部不关)
  document.addEventListener('click', function (e) {
    const wrap = document.getElementById('hmBcWrap');
    const p = document.getElementById('hmBcPanel');
    if (!wrap || !p || p.style.display === 'none') return;
    if (!wrap.contains(e.target)) p.style.display = 'none';
  });

  // ── 🚀 AI 选股(5 分类) ────────────────────────────────────────────────
  let _hmAiPicks = null;           // ticker -> {verdict,confidence,tech,fundamental,news,summary}
  let _hmAiPicksMeta = null;       // {date, ts, n}
  let _hmAiPollTimer = null;
  // 5 类叶子: 短标签 + 边框/文字色 + 组(看涨/看跌/不交易)
  const _HM_AI_META = {
    '看涨·低点抄底': { short: '抄底', col: '#3fb950', mark: '▲', group: '看涨' },
    '看涨·趋势追涨': { short: '追涨', col: '#2ea043', mark: '▲', group: '看涨' },
    '不交易':       { short: '观望', col: '#8b949e', mark: '—', group: '不交易' },
    '看跌·高点阻力': { short: '阻力', col: '#f85149', mark: '▼', group: '看跌' },
    '看跌·颓势追跌': { short: '追跌', col: '#da3633', mark: '▼', group: '看跌' }
  };

  window.hmLoadAiPicks = function () {
    return fetch('/api/ai_picks').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.picks && !d.empty) { _hmAiPicks = d.picks; _hmAiPicksMeta = { date: d.date, ts: d.ts, n: d.n }; }
      else { _hmAiPicks = _hmAiPicks || null; }
      _hmAiUpdateStatus();
      if (window.hmState.view === 'graph') hmRenderGraph();
    }).catch(function () {});
  };

  function _hmAiUpdateStatus(runningTxt) {
    const el = document.getElementById('hmAiPicksStatus');
    if (!el) return;
    if (runningTxt) { el.textContent = runningTxt; el.style.color = '#d29922'; return; }
    if (_hmAiPicks && _hmAiPicksMeta) {
      let bull = 0, bear = 0, flat = 0;
      Object.keys(_hmAiPicks).forEach(function (t) {
        const g = (_HM_AI_META[(_hmAiPicks[t] || {}).verdict] || {}).group;
        if (g === '看涨') bull++; else if (g === '看跌') bear++; else if (g === '不交易') flat++;
      });
      const when = (_hmAiPicksMeta.ts || '').replace('T', ' ').slice(5, 16);
      el.textContent = '✓ ' + (_hmAiPicksMeta.date || '') + ' · ▲' + bull + ' —' + flat + ' ▼' + bear + (when ? (' · ' + when) : '');
      el.style.color = '#8b949e';
    } else { el.textContent = ''; }
  }

  // ── 报告弹窗 ──────────────────────────────────────────────────────────
  // 2026-07-30 作者反馈: 5 类分类 (all) 太散 (56+46+95+4+6=207 tickers), 打开默认要看 ⭐精选
  let _hmAiReportTab = 'picks';

  // 点工具栏 🚀AI选股 = 打开报告弹窗(读缓存, 不自动重跑); 重跑靠弹窗内「重新分析」
  window.hmAiPicksReport = function () {
    _hmAiEnsureModal();
    document.getElementById('hmAiModal').style.display = 'flex';
    // 每次点开都拉一次实时价 (含 ETF), 拉完 re-render 卡片
    _hmPkFetchLive().then(function () { if (_hmAiReportTab === 'picks') _hmAiRenderReport(); });
    // 首次打开时确保 ⭐精选选股 tab 触发 fetch (stock_selector engine, 10-15 只精选)
    if (_hmAiReportTab === 'picks') _hmPkEnsureLoaded();
    if (_hmAiPicks === null) { hmLoadAiPicks().then(_hmAiRenderReport); }
    else { _hmAiRenderReport(); }
  };
  window.hmAiCloseReport = function () {
    const m = document.getElementById('hmAiModal'); if (m) m.style.display = 'none';
  };

  function _hmAiEnsureModal() {
    if (document.getElementById('hmAiModal')) return;
    const ov = document.createElement('div');
    ov.id = 'hmAiModal';
    ov.style.cssText = 'display:none;position:fixed;inset:0;z-index:9999;background:rgba(1,4,9,0.72);align-items:center;justify-content:center';
    ov.onclick = function (e) { if (e.target === ov) window.hmAiCloseReport(); };
    // ⭐ 精选选股面板的复制按钮走事件委托(data-ticket 属性, 不用内联 onclick 塞动态
    // 字符串 —— 见 gotcha_js_in_python_string_escaping, 反复踩的坑)
    ov.addEventListener('click', function (e) {
      const btn = e.target.closest && e.target.closest('.hm-pk-copy');
      if (!btn) return;
      const text = btn.getAttribute('data-ticket') || '';
      if (text && navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () {
          const orig = btn.textContent;
          btn.textContent = '✅';
          setTimeout(function () { btn.textContent = orig; }, 1200);
        });
      }
    });
    ov.innerHTML =
      '<div style="background:#0d1117;border:1px solid #30363d;border-radius:10px;width:min(880px,94vw);max-height:86vh;display:flex;flex-direction:column;overflow:hidden">'
      + '<div style="display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid #21262d">'
      +   '<span style="color:#e6edf3;font-size:14px;font-weight:700">🚀 AI 选股</span>'
      +   '<span id="hmAiModalMeta" style="color:#8b949e;font-size:11px"></span>'
      +   '<span style="flex:1"></span>'
      +   '<button id="hmAiRerunBtn" onclick="hmAiPicksRerun()" style="background:#da3633;color:#fff;border:1px solid #f85149;border-radius:5px;padding:5px 12px;font-size:12px;cursor:pointer;font-weight:600">🚀 重新分析</button>'
      +   '<button onclick="hmAiCloseReport()" title="关闭" style="background:transparent;color:#8b949e;border:none;font-size:18px;cursor:pointer;padding:0 4px;line-height:1">✕</button>'
      + '</div>'
      + '<div id="hmAiModalTabs" style="display:flex;gap:4px;flex-wrap:wrap;padding:8px 16px;border-bottom:1px solid #21262d"></div>'
      + '<div id="hmAiModalProg" style="display:none;padding:8px 16px"></div>'
      + '<div id="hmAiModalBody" style="flex:1;overflow-y:auto;padding:10px 16px 16px"></div>'
      + '</div>';
    document.body.appendChild(ov);
  }

  function _hmAiCounts() {
    const c = { '看涨·低点抄底': 0, '看涨·趋势追涨': 0, '不交易': 0, '看跌·高点阻力': 0, '看跌·颓势追跌': 0 };
    if (_hmAiPicks) Object.keys(_hmAiPicks).forEach(function (t) { const v = (_hmAiPicks[t] || {}).verdict; if (c[v] != null) c[v]++; });
    return c;
  }

  function _hmAiRenderReport() {
    const meta = document.getElementById('hmAiModalMeta');
    const tabs = document.getElementById('hmAiModalTabs');
    const body = document.getElementById('hmAiModalBody');
    if (!body) return;
    // tabs 永远渲染(不依赖 _hmAiPicks 是否已加载) —— ⭐精选选股 是独立引擎(stock_selector/),
    // 就算今天广谱 160 只分类还没跑, 精选面板也该能点开用。
    const cnt = _hmAiCounts();
    const TABS = [
      { key: 'picks', label: '⭐精选选股', col: '#58a6ff' },
      { key: 'all', label: '全部', col: '#8b949e' },
      { key: '看涨·低点抄底', label: '▲抄底 ' + cnt['看涨·低点抄底'], col: '#3fb950' },
      { key: '看涨·趋势追涨', label: '▲追涨 ' + cnt['看涨·趋势追涨'], col: '#2ea043' },
      { key: '不交易', label: '—观望 ' + cnt['不交易'], col: '#8b949e' },
      { key: '看跌·高点阻力', label: '▼阻力 ' + cnt['看跌·高点阻力'], col: '#f85149' },
      { key: '看跌·颓势追跌', label: '▼追跌 ' + cnt['看跌·颓势追跌'], col: '#da3633' }
    ];
    if (tabs) {
      tabs.innerHTML = TABS.map(function (t) {
        const on = _hmAiReportTab === t.key;
        return '<button data-ai-tab="' + t.key + '" style="background:' + (on ? t.col : '#161b22')
          + ';color:' + (on ? '#fff' : '#8b949e') + ';border:1px solid ' + (on ? t.col : '#30363d')
          + ';border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600">' + t.label + '</button>';
      }).join('');
      tabs.querySelectorAll('[data-ai-tab]').forEach(function (b) {
        b.onclick = function () {
          _hmAiReportTab = b.getAttribute('data-ai-tab');
          if (_hmAiReportTab === 'picks') _hmPkEnsureLoaded();
          _hmAiRenderReport();
        };
      });
    }

    if (_hmAiReportTab === 'picks') {
      if (meta) meta.textContent = '';
      body.innerHTML = _hmPkPanelHtml();
      body.querySelectorAll('[data-ai-open]').forEach(function (el) {
        el.onclick = function () { if (window.openTaTicker) window.openTaTicker(el.getAttribute('data-ai-open')); };
      });
      return;
    }

    if (!_hmAiPicks || !_hmAiPicksMeta) {
      if (meta) meta.textContent = '';
      body.innerHTML = '<div style="color:#6e7681;padding:30px;text-align:center;font-size:13px">还没有分析结果 — 点右上角「🚀 重新分析」跑一次(160 只, 约 3 分钟)</div>';
      return;
    }
    const when = (_hmAiPicksMeta.ts || '').replace('T', ' ').slice(0, 16);
    if (meta) meta.textContent = (_hmAiPicksMeta.date || '') + ' · ' + (_hmAiPicksMeta.n || 0) + ' 只 · ' + when;
    const ORDER = ['看涨·低点抄底', '看涨·趋势追涨', '看跌·高点阻力', '看跌·颓势追跌', '不交易'];
    const cats = _hmAiReportTab === 'all' ? ORDER : [_hmAiReportTab];
    let html = '';
    cats.forEach(function (cat) {
      const rows = Object.keys(_hmAiPicks).filter(function (t) { return _hmAiPicks[t].verdict === cat; })
        .map(function (t) { return { t: t, p: _hmAiPicks[t] }; })
        .sort(function (a, b) { return (b.p.confidence || 0) - (a.p.confidence || 0); });
      if (!rows.length) return;
      const m = _HM_AI_META[cat] || {};
      if (_hmAiReportTab === 'all')
        html += '<div style="color:' + m.col + ';font-weight:700;font-size:12px;margin:12px 0 6px">' + m.mark + ' ' + cat + ' (' + rows.length + ')</div>';
      html += rows.map(function (r) { return _hmAiReportRow(r.t, r.p, m); }).join('');
    });
    body.innerHTML = html || '<div style="color:#6e7681;padding:24px;text-align:center">该类暂无票</div>';
    body.querySelectorAll('[data-ai-open]').forEach(function (el) {
      el.onclick = function () { if (window.openTaTicker) window.openTaTicker(el.getAttribute('data-ai-open')); };
    });
  }

  function _hmAiReportRow(ticker, p, m) {
    m = m || _HM_AI_META[p.verdict] || { col: '#8b949e', mark: '' };
    function line(lbl, txt) {
      if (!txt || txt === '—') return '';
      return '<div style="margin:2px 0"><span style="color:#6e7681">' + lbl + '</span> <span style="color:#c9d1d9">' + hmEsc(txt) + '</span></div>';
    }
    const conf = (p.confidence != null) ? p.confidence : '';
    return '<div style="border:1px solid #21262d;border-left:3px solid ' + m.col + ';border-radius:6px;padding:7px 10px;margin-bottom:6px;background:#010409">'
      + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">'
      +   '<span data-ai-open="' + ticker + '" title="开 TA 详情页" style="font-weight:700;color:#e6edf3;font-size:13px;cursor:pointer">' + ticker + '</span>'
      +   '<span style="color:' + m.col + ';font-size:11px;font-weight:700">' + (m.mark || '') + (m.short || '') + '</span>'
      +   '<span style="color:#6e7681;font-size:10px">信心 ' + conf + '</span>'
      + '</div>'
      + (p.summary ? '<div style="color:#e6edf3;font-size:11px;margin-bottom:3px;line-height:1.5">' + hmEsc(p.summary) + '</div>' : '')
      + '<div style="font-size:10.5px;line-height:1.5">' + line('技术', p.tech) + line('基本面', p.fundamental) + line('新闻', p.news) + '</div>'
      + '</div>';
  }

  // 重新分析(弹窗内): 后台全量重跑 + 进度条; 完成刷新徽章 + 报告
  window.hmAiPicksRerun = function () {
    const rb = document.getElementById('hmAiRerunBtn');
    if (rb) { rb.disabled = true; rb.style.opacity = '0.6'; }
    _hmAiUpdateStatus('🚀 启动中…');
    _hmAiShowProg('🚀 启动中…', 4);
    fetch('/api/ai_picks/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'manual' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d.status === 'done') { _hmAiPollStop(); _hmAiRerunDone(); } else { _hmAiPoll(); } })
      .catch(function () { _hmAiUpdateStatus('❌ 启动失败'); _hmAiShowProg('❌ 启动失败', 0); if (rb) { rb.disabled = false; rb.style.opacity = '1'; } });
  };

  function _hmAiShowProg(txt, pct) {
    const p = document.getElementById('hmAiModalProg');
    if (!p) return;
    if (txt == null) { p.style.display = 'none'; return; }
    p.style.display = 'block';
    p.innerHTML = '<div style="color:#8b949e;font-size:11px;margin-bottom:4px">' + hmEsc(txt) + '</div>'
      + '<div style="height:6px;border-radius:4px;background:#21262d;overflow:hidden"><div style="height:100%;width:' + (pct || 0) + '%;background:linear-gradient(90deg,#da3633,#f85149);transition:width .5s"></div></div>';
  }

  function _hmAiRerunDone() {
    const rb = document.getElementById('hmAiRerunBtn');
    if (rb) { rb.disabled = false; rb.style.opacity = '1'; }
    _hmAiShowProg(null);
    hmLoadAiPicks().then(_hmAiRenderReport);
  }

  function _hmAiPollStop() { if (_hmAiPollTimer) { clearTimeout(_hmAiPollTimer); _hmAiPollTimer = null; } }
  function _hmAiPoll() {
    fetch('/api/ai_picks/status').then(function (r) { return r.json(); }).then(function (d) {
      if (d.running) {
        const txt = '🚀 分析中 ' + (d.n_done || 0) + '/' + (d.n_total || '?') + ' · ' + (d.pct || 0) + '%';
        _hmAiUpdateStatus(txt);
        _hmAiShowProg(txt, Math.max(4, d.pct || 0));
        _hmAiPollTimer = setTimeout(_hmAiPoll, 2000);
      } else {
        _hmAiPollStop();
        if (d.error) {
          _hmAiUpdateStatus('❌ ' + String(d.error).slice(0, 40));
          _hmAiShowProg('❌ ' + String(d.error).slice(0, 60), 0);
          const rb = document.getElementById('hmAiRerunBtn'); if (rb) { rb.disabled = false; rb.style.opacity = '1'; }
        } else { _hmAiRerunDone(); }
      }
    }).catch(function () { _hmAiPollTimer = setTimeout(_hmAiPoll, 3000); });
  }

  function _hmAiReasonHTML(p) {
    const m = _HM_AI_META[p.verdict] || { col: '#8b949e', mark: '' };
    const conf = (p.confidence != null) ? (' · 信心 ' + p.confidence) : '';
    function row(label, txt) {
      if (!txt || txt === '—') return '';
      return '<div style="margin:3px 0;line-height:1.5"><span style="color:#6e7681">' + label + '</span> '
        + '<span style="color:#c9d1d9">' + hmEsc(txt) + '</span></div>';
    }
    return '<div style="border:1px solid ' + m.col + '55;border-left:3px solid ' + m.col
      + ';border-radius:5px;padding:6px 8px;background:#0d1117;font-size:10.5px">'
      + '<div style="color:' + m.col + ';font-weight:700;margin-bottom:3px">' + m.mark + ' ' + hmEsc(p.verdict) + conf + '</div>'
      + (p.summary ? '<div style="color:#e6edf3;margin-bottom:4px;line-height:1.5">' + hmEsc(p.summary) + '</div>' : '')
      + row('技术', p.tech) + row('基本面', p.fundamental) + row('新闻', p.news)
      + '</div>';
  }

  function _hmAiToggleReason(ticker) {
    const box = document.querySelector('.hm-ai-reason[data-ai-reason="' + ticker + '"]');
    const p = _hmAiPicks && _hmAiPicks[ticker];
    if (!box || !p) return;
    if (box.style.display === 'none' || !box.style.display) {
      box.innerHTML = _hmAiReasonHTML(p);
      box.style.display = 'block';
    } else {
      box.style.display = 'none';
    }
  }
  // ── ⭐ 精选选股(stock_selector/: 策略A趋势跟踪 + 策略B抄底, 融合进 AI选股弹窗的 tab) ──
  // 与上面 160 只广谱 5 分类是两个独立引擎: 广谱=Haiku 覆盖全 TA universe 的粗评级,
  // 精选=技术硬过滤+打分+选strike+Sonnet新闻复核 的 ≤30 只可执行候选(带 strike/POP/挂单)。
  let _hmPkData = null, _hmPkLoading = false, _hmPkLoaded = false;

  function _hmPkEnsureLoaded() {
    if (_hmPkLoaded) return;
    _hmPkLoaded = true;
    _hmPkFetch(false, false);   // 首次默认带 Sonnet 复核; 用后端当日缓存(不 force refresh)
  }

  // 2026-07-30: 当日缓存 (基本面一天不会变, Sonnet 跑一次就复用). refresh=true = 强制重跑.
  // 加进度反馈: 用时计数 tick + 按钮禁用 + 进度条动画 (loading 状态 body 里画).
  let _hmPkStartTs = 0;
  let _hmPkTickTimer = null;
  function _hmPkStartTick() {
    _hmPkStartTs = Date.now();
    if (_hmPkTickTimer) clearInterval(_hmPkTickTimer);
    _hmPkTickTimer = setInterval(function () {
      const el = document.getElementById('hmPkLoadElapsed');
      if (el) {
        const s = Math.floor((Date.now() - _hmPkStartTs) / 1000);
        el.textContent = s + 's';
      }
    }, 500);
  }
  function _hmPkStopTick() {
    if (_hmPkTickTimer) { clearInterval(_hmPkTickTimer); _hmPkTickTimer = null; }
  }
  function _hmPkFetch(noSonnet, refresh) {
    _hmPkLoading = true;
    _hmPkStartTick();
    _hmAiRenderReport();
    const q = '?no_sonnet=' + (noSonnet ? 'true' : 'false') + '&refresh=' + (refresh ? 'true' : 'false');
    return fetch('/api/stock_selector' + q)
      .then(function (r) { return r.json(); })
      .then(function (d) { _hmPkData = d; _hmPkLoading = false; _hmPkStopTick(); _hmAiRenderReport(); })
      .catch(function (e) { _hmPkLoading = false; _hmPkStopTick(); _hmPkData = { error: String(e && e.message || e) }; _hmAiRenderReport(); });
  }

  window.hmPkRefresh = function () {
    const noSonnet = !!(document.getElementById('hmPkNoSonnet') || {}).checked;
    _hmPkFetch(noSonnet, true);   // 用户显式点「🔄 重跑」 → 跳过当日缓存重新跑
  };

  function _hmPkPct(v) {
    if (v == null) return '<span style="color:#6e7681">?</span>';
    const c = v > 0 ? '#3fb950' : (v < 0 ? '#f85149' : '#8b949e');
    return '<span style="color:' + c + '">' + (v > 0 ? '+' : '') + v.toFixed(2) + '%</span>';
  }

  const _HM_PK_MACRO_COLOR = { low: '#3fb950', med: '#d29922', high: '#f85149' };

  function _hmPkTicketRow(label, ticket) {
    if (!ticket) return '';
    return '<div style="display:flex;align-items:center;gap:6px;margin-top:4px">'
      + '<span style="color:#6e7681;font-size:10px;min-width:28px">' + label + '</span>'
      + '<code style="flex:1;background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:3px 6px;'
      +   'font-size:10.5px;color:#e6a94c;overflow-x:auto;white-space:nowrap">' + hmEsc(ticket) + '</code>'
      + '<button class="hm-pk-copy" data-ticket="' + hmEsc(ticket) + '" title="复制到剪贴板" '
      +   'style="background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:2px 7px;'
      +   'font-size:10px;cursor:pointer;flex-shrink:0">📋</button>'
      + '</div>';
  }

  // 2026-07-30 作者: 精选选股缓存, 但每次点开卡片价格要实时.
  // 数据源 = /api/live_prices (heatmap.live_prices() 722 票 universe, 含 ETF —
  // hmState.lastData.items 只覆盖 stocks 160 只, SCHD/XLB/PM 等 ETF 找不到).
  let _hmPkLivePrices = {};
  let _hmPkLivePricesAt = 0;
  function _hmPkFetchLive() {
    // 5s 内不重复拉
    if (Date.now() - _hmPkLivePricesAt < 5000) return Promise.resolve();
    return fetch('/api/live_prices').then(function (r) { return r.json(); })
      .then(function (d) {
        const map = {};
        (d.signals || []).forEach(function (s) { map[s.ticker] = { last: s.price, pct: s.change_pct }; });
        _hmPkLivePrices = map;
        _hmPkLivePricesAt = Date.now();
      }).catch(function () { /* 网络挂了兜底 c.latest, 别报错 */ });
  }
  function _hmPkLive(sym) {
    return _hmPkLivePrices[sym] || null;
  }

  function _hmPkCard(c) {
    const verdict = c.sonnet_verdict;
    const vColor = verdict === 'OPEN' ? '#3fb950' : (verdict === 'SKIP' ? '#f85149' : '#6e7681');
    const vLabel = verdict === 'OPEN' ? '🟢 OPEN' : (verdict === 'SKIP' ? '🔴 SKIP' : (verdict ? hmEsc(verdict) : '—'));
    const conf = (c.confidence != null) ? (c.confidence + '/5') : '';
    const touchTag = (c.touch_ema != null)
      ? ('<span style="color:#58a6ff;font-size:10px;border:1px solid #1f6feb55;border-radius:3px;padding:0 4px;margin-left:5px">'
          + c.touch_ema + 'D EMA</span>')
      : '';
    const macroColor = _HM_PK_MACRO_COLOR[c.macro_risk] || '#6e7681';
    const earn = c.upcoming_earnings;
    const earnWarn = !!(earn && earn.days != null && earn.days < 45);
    const hasStrike = (c.long_strike != null && c.short_strike != null);
    const pop = (c.pop_pct != null) ? c.pop_pct.toFixed(1) + '%' : '?';
    const debitLine = hasStrike
      ? ('strikes ' + c.long_strike + '/' + c.short_strike + ' · ' + (c.expiry || '?')
          + ' (' + (c.dte != null ? c.dte + 'd' : '?') + ') · POP ' + pop
          + ' · debit $' + (c.est_debit != null ? c.est_debit.toFixed(2) : '?')
          + ' ×' + (c.n_contracts != null ? c.n_contracts : '?') + ' 张')
      : '<span style="color:#6e7681">期权归档缺失, 无法出 strike</span>';
    const flags = (c.news_flags || []).map(function (f) {
      return '<span style="color:#f85149;font-size:10px;border:1px solid #f8514955;border-radius:3px;padding:0 4px;margin-right:4px">'
        + hmEsc(f) + '</span>';
    }).join('');
    return '<div style="background:#0d1117;border:1px solid #21262d;border-left:3px solid ' + vColor + ';'
      + 'border-radius:6px;padding:9px 11px;margin-bottom:7px'
      + (earnWarn ? ';box-shadow:0 0 0 1px #d2992266' : '') + '">'
      + '<div style="display:flex;align-items:baseline;gap:7px;flex-wrap:wrap">'
      +   '<span data-ai-open="' + hmEsc(c.symbol) + '" title="开 TA 详情页" '
      +     'style="font-weight:700;color:#e6edf3;font-size:12.5px;cursor:pointer">' + hmEsc(c.symbol) + '</span>'
      +   (function() {
             const live = _hmPkLive(c.symbol);
             const px = (live && live.last != null) ? live.last : c.latest;
             const isLive = !!(live && live.last != null);
             const pctStr = (live && live.pct != null) ? (' <span style="color:' + (live.pct >= 0 ? '#3fb950' : '#f85149')
               + '">' + (live.pct >= 0 ? '+' : '') + live.pct.toFixed(2) + '%</span>') : '';
             const dot = isLive ? '<span title="TastyTrade DXLink 实时" style="color:#3fb950;font-size:8px;margin-right:3px">●</span>' : '';
             return '<span style="color:#8b949e;font-size:11.5px">' + dot + '$'
               + (px != null ? px.toFixed(2) : '?') + pctStr + '</span>';
           })()
      +   touchTag
      +   '<span style="flex:1"></span>'
      +   '<span style="color:' + vColor + ';font-size:11px;font-weight:700">' + vLabel + '</span>'
      +   (conf ? '<span style="color:#6e7681;font-size:10.5px">&nbsp;' + conf + '</span>' : '')
      + '</div>'
      + '<div style="color:#6e7681;font-size:10.5px;margin-top:3px">'
      +   '1M ' + _hmPkPct(c['1m_chg']) + ' · 3M ' + _hmPkPct(c['3m_chg'])
      +   ' · RSI ' + (c.rsi != null ? c.rsi.toFixed(1) : '?') + '</div>'
      + '<div style="color:#8b949e;font-size:10.5px;margin-top:3px">' + debitLine + '</div>'
      + (earn ? ('<div style="color:' + (earnWarn ? '#d29922' : '#6e7681') + ';font-size:10.5px;margin-top:3px">'
          + (earnWarn ? '⚠️ ' : '') + '下次财报: ' + hmEsc(earn.date) + ' (距今 ' + earn.days + ' 天)</div>') : '')
      + (c.reason_zh ? '<div style="color:#c9d1d9;font-size:11px;line-height:1.5;margin-top:5px;'
          + 'border-top:1px solid #161b22;padding-top:5px">' + hmEsc(c.reason_zh) + '</div>' : '')
      + (flags ? '<div style="margin-top:4px">' + flags + '</div>' : '')
      + (c.macro_risk ? '<div style="margin-top:3px"><span style="color:' + macroColor + ';font-size:9.5px">'
          + '宏观风险: ' + hmEsc(c.macro_risk) + '</span></div>' : '')
      + _hmPkTicketRow('挂单', c.ticket)
      + _hmPkTicketRow('止盈', c.ticket_tp)
      + '</div>';
  }

  function _hmPkToolbarHtml() {
    const noSonnetChecked = !!(document.getElementById('hmPkNoSonnet') || {}).checked;
    return '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">'
      + '<label style="color:#8b949e;font-size:11px;display:flex;align-items:center;gap:4px;cursor:pointer" '
      +   'title="只出技术候选, 不调 Sonnet 新闻/宏观复核 —— 快很多(几秒 vs 1-2 分钟)">'
      +   '<input type="checkbox" id="hmPkNoSonnet"' + (noSonnetChecked ? ' checked' : '') + '> 跳过 Sonnet(更快)</label>'
      + '<button onclick="hmPkRefresh()" id="hmPkRefreshBtn"'
      +   (_hmPkLoading ? ' disabled' : '')
      +   ' style="background:' + (_hmPkLoading ? '#161b22' : '#21262d')
      +   ';color:' + (_hmPkLoading ? '#6e7681' : '#e6edf3')
      +   ';border:1px solid #30363d;border-radius:4px;padding:4px 12px;font-size:11px;'
      +   'cursor:' + (_hmPkLoading ? 'wait' : 'pointer') + '">'
      +   (_hmPkLoading ? '⏳ 跑中...' : '🔄 重跑') + '</button>'
      + '</div>';
  }

  function _hmPkPanelHtml() {
    const toolbar = _hmPkToolbarHtml();
    if (_hmPkLoading) {
      const skipS = !!(document.getElementById('hmPkNoSonnet') || {}).checked;
      const eta = skipS ? '几秒' : '约 60-120s (Sonnet 复核)';
      // indeterminate 进度条 (CSS 动画从左往右扫), 不是真实进度但能明确「在跑」
      return toolbar
        + '<div style="padding:24px 16px;text-align:center">'
        +   '<div style="color:#e6edf3;font-size:13px;margin-bottom:10px">'
        +     '⏳ 分析中… 已用时 <span id="hmPkLoadElapsed" style="color:#58a6ff;font-weight:700">0s</span>'
        +     ' <span style="color:#6e7681">· 预估 ' + eta + '</span></div>'
        +   '<div class="hm-pk-progress"><div></div></div>'
        + '</div>';
    }
    const d = _hmPkData;
    if (!d) {
      return toolbar + '<div style="color:#6e7681;padding:30px;text-align:center;font-size:13px">点「🔄 重跑」加载精选选股(策略A趋势跟踪 + 策略B抄底)</div>';
    }
    if (d.error) {
      return toolbar + '<div style="color:#f85149;padding:24px">' + hmEsc(d.error) + '</div>';
    }
    const mc = d.market_context || {};
    const topics = (mc.top_topics || []).join(', ');
    // 2026-07-30 作者: 大盘 header 里 SPY/QQQ 用 live_prices 实时今日涨跌;
    // VIX 保留 stock_selector 缓存 (live_prices 无 VIX index).
    // 5日/20日 累计涨跌保留 (stock_selector 每日 refresh, 那个是趋势不是当前价).
    const spyLive = _hmPkLive('SPY');
    const qqqLive = _hmPkLive('QQQ');
    const liveDot = '<span title="TT DXLink 实时" style="color:#3fb950;font-size:8px;margin-right:2px">●</span>';
    const spyToday = spyLive ? (liveDot + '今日 ' + _hmPkPct(spyLive.pct)) : '';
    const qqqToday = qqqLive ? (liveDot + '今日 ' + _hmPkPct(qqqLive.pct)) : '';
    const ctxHtml = '<div style="color:#8b949e;font-size:11.5px;margin-bottom:8px;padding:7px 9px;'
      + 'background:#0d1117;border:1px solid #21262d;border-radius:6px">'
      + '大盘: VIX ' + (mc.vix != null ? mc.vix.toFixed(1) : '?')
      + ' · SPY ' + (spyToday ? spyToday + ' · ' : '') + '5日 ' + _hmPkPct(mc.spy_5d) + ' / 20日 ' + _hmPkPct(mc.spy_20d)
      + ' · QQQ ' + (qqqToday ? qqqToday + ' · ' : '') + '5日 ' + _hmPkPct(mc.qqq_5d)
      + (topics ? ' · 热点: ' + hmEsc(topics) : '') + '</div>';
    const trendObj = d.trend_follow || {}, bottomObj = d.bottom_fish || {};
    // 2026-07-30 作者反馈: 15+15=30 精选太多, 想 10-15; 优先高 POP.
    // T6 已硬过滤期权量 ≥500(高流动性), 这里再按 pop_pct desc 排序取 top 5 每策略 = 双栏 10 张.
    const _hmPkTopN = 5;
    const sortByPop = function (arr) {
      return (arr || []).slice().sort(function (a, b) { return (b.pop_pct || 0) - (a.pop_pct || 0); }).slice(0, _hmPkTopN);
    };
    const trend = sortByPop(trendObj.candidates);
    const bottom = sortByPop(bottomObj.candidates);
    const trendHtml = trend.length ? trend.map(function (c) { return _hmPkCard(c); }).join('')
      : '<div style="color:#6e7681;font-size:11.5px;padding:14px;text-align:center">今天没有符合条件的候选</div>';
    const bottomHtml = bottom.length ? bottom.map(function (c) { return _hmPkCard(c); }).join('')
      : '<div style="color:#6e7681;font-size:11.5px;padding:14px;text-align:center">今天没有符合条件的候选</div>';
    const sst = { ok: 'Sonnet 已复核', skipped: '跳过 Sonnet', error: 'Sonnet 失败(仅技术候选)' }[d.sonnet_status]
      || d.sonnet_status || '';
    return toolbar + ctxHtml
      + '<div style="color:#6e7681;font-size:10.5px;margin-bottom:6px">' + (d.today || '') + ' · ' + sst + '</div>'
      + '<div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start">'
      + '<div style="flex:1;min-width:280px">'
      +   '<div style="color:#8b949e;font-size:11.5px;font-weight:700;border-bottom:1px solid #21262d;'
      +     'padding-bottom:4px;margin-bottom:7px">📈 策略 A 趋势跟踪 · 按 POP 精选 top ' + _hmPkTopN + ' / ' + (trendObj.count || 0) + '</div>'
      +   trendHtml
      + '</div>'
      + '<div style="flex:1;min-width:280px">'
      +   '<div style="color:#8b949e;font-size:11.5px;font-weight:700;border-bottom:1px solid #21262d;'
      +     'padding-bottom:4px;margin-bottom:7px">📉 策略 B 抄底 · 按 POP 精选 top ' + _hmPkTopN + ' / ' + (bottomObj.count || 0) + '</div>'
      +   bottomHtml
      + '</div>'
      + '</div>';
  }

  // ── 点卡片就地展开全文(不跳详情页) ─────────────────────────────
  const _hmCardFullCache = {};   // {ticker: full JSON}, 会话内缓存, 反复开关不重复 fetch
  function _hmDetailSection(title, color, bodyHTML) {
    if (!bodyHTML) return '';
    return '<div style="margin-top:5px;border-left:2px solid ' + color + ';padding-left:7px">'
      + '<div style="color:' + color + ';font-weight:700;margin-bottom:2px">' + title + '</div>'
      + '<div style="color:#c9d1d9;line-height:1.55">' + bodyHTML + '</div></div>';
  }
  function _hmRenderCardFull(f) {
    if (!f) return '<span style="color:#6e7681">暂无数据</span>';
    let h = '';
    if (f.ai) {
      const a = f.ai;
      const conf = (a.confidence != null) ? (' · 信心 ' + a.confidence) : '';
      let b = '';
      if (a.verdict) b += '<div style="color:#a371f7;font-weight:700">' + hmEsc(a.verdict) + conf + '</div>';
      if (a.summary) b += '<div style="margin:2px 0">' + hmEsc(a.summary) + '</div>';
      if (a.tech) b += '<div style="margin:2px 0"><span style="color:#6e7681">技术 </span>' + hmEsc(a.tech) + '</div>';
      if (a.fundamental) b += '<div style="margin:2px 0"><span style="color:#6e7681">基本面 </span>' + hmEsc(a.fundamental) + '</div>';
      if (a.news) b += '<div style="margin:2px 0"><span style="color:#6e7681">新闻 </span>' + hmEsc(a.news) + '</div>';
      h += _hmDetailSection('🚀 AI 选股', '#a371f7', b);
    }
    if (f.nb) {
      let b = '';
      (f.nb.bull || []).forEach(x => { b += '<div style="margin:1px 0"><span style="color:#3fb950">＋</span> ' + hmEsc(x) + '</div>'; });
      (f.nb.bear || []).forEach(x => { b += '<div style="margin:1px 0"><span style="color:#f85149">－</span> ' + hmEsc(x) + '</div>'; });
      if (f.nb.net) b += '<div style="margin-top:3px;color:#8b949e">综合: ' + hmEsc(f.nb.net) + '</div>';
      h += _hmDetailSection('⚖️ 多空', '#8b949e', b);
    }
    if (f.sr) {
      const d = f.sr.dir === 'sup' ? '支撑' : (f.sr.dir === 'res' ? '阻力' : (f.sr.dir || ''));
      h += _hmDetailSection('📍 今日 SR', '#d29922',
        hmEsc((f.sr.hhmm || '') + ' ' + d + (f.sr.zone ? ' ' + f.sr.zone : '') + (f.sr.why ? ' · ' + f.sr.why : '')));
    }
    // 公司简介不进就地展开(作者: 点开不需要简介) —— 简介在「深度」详情页看。
    return h || '<span style="color:#6e7681">暂无数据</span>';
  }
  function _hmToggleCardDetail(ticker, card) {
    const box = card.querySelector('.hm-card-detail[data-detail="' + ticker + '"]');
    if (!box) return;
    if (box.style.display !== 'none') { box.style.display = 'none'; return; }
    box.style.display = 'block';
    if (_hmCardFullCache[ticker]) { box.innerHTML = _hmRenderCardFull(_hmCardFullCache[ticker]); return; }
    box.innerHTML = '<span style="color:#6e7681">加载中…</span>';
    fetch('/api/heatmap/card_full/' + encodeURIComponent(ticker))
      .then(r => r.json())
      .then(f => { _hmCardFullCache[ticker] = f; box.innerHTML = _hmRenderCardFull(f); })
      .catch(e => { box.innerHTML = '<span style="color:#f85149">加载失败: ' + hmEsc('' + e) + '</span>'; });
  }

  // ── ⊕ 一键全展开: 所有卡片同时展开 AI + 多空全文 ──────────────────
  let _hmAllExpanded = false;
  window.hmToggleExpandAll = function () {
    const btn = document.getElementById('hmExpandAllBtn');
    const boxes = document.querySelectorAll('#hmGraphGrid .hm-card-detail');
    if (_hmAllExpanded) {                       // 收起全部
      boxes.forEach(b => { b.style.display = 'none'; });
      _hmAllExpanded = false;
      if (btn) { btn.textContent = '⊕ 全展开'; btn.style.background = 'transparent'; btn.style.color = '#a371f7'; }
      return;
    }
    if (btn) btn.textContent = '⊕ 加载中…';
    // 一次批量拉全票全文 → 灌缓存 → 全部展开(缺内容的卡片留空)
    const paint = () => {
      boxes.forEach(b => {
        const tk = b.getAttribute('data-detail');
        const f = _hmCardFullCache[tk];
        if (f) { b.innerHTML = _hmRenderCardFull(f); b.style.display = 'block'; }
      });
      _hmAllExpanded = true;
      if (btn) { btn.textContent = '⊖ 收起'; btn.style.background = '#a371f733'; btn.style.color = '#a371f7'; }
    };
    fetch('/api/heatmap/card_full_all').then(r => r.json()).then(all => {
      Object.keys(all || {}).forEach(tk => { _hmCardFullCache[tk] = all[tk]; });
      paint();
    }).catch(e => { if (btn) btn.textContent = '⊕ 全展开'; });
  };

  // 每行张数:'auto'=自适应(minmax 300px);3/4/5/6=固定列(等宽,candle 按列宽自绘)
  function _hmApplyGraphCols() {
    const grid = document.getElementById('hmGraphGrid');
    if (!grid) return;
    const v = window.hmState.graphCols || 'auto';
    grid.style.gridTemplateColumns = (v === 'auto')
      ? 'repeat(auto-fill,minmax(300px,1fr))'
      : 'repeat(' + v + ',minmax(0,1fr))';
  }

  let _hmTaRowsAt = 0;             // _hmTaRows 拉取时刻(单调时钟), 过 TTL 重拉
  const _HM_TAROWS_TTL = 1800000; // 30 分钟。TA 结果 EOD 4 次重跑, 会话内基本不变,
                                  //  但跨天/长挂着会陈旧 → 超时重拉(旧实现一缓存就永不刷新)。
  async function _hmEnsureScores(force) {
    const now = Date.now();
    if (!force && _hmTaRows && (now - _hmTaRowsAt) < _HM_TAROWS_TTL) return _hmTaRows;
    try {
      const d = await (await fetch('/api/ta/results')).json();
      const m = {};
      (d.rows || []).forEach(r => { if (r.ticker) m[r.ticker] = r; });   // 存完整 slim 行(筛选 + graph 共用)
      _hmTaRows = m; _hmGraphScores = m; _hmTaRowsAt = now;
    } catch (e) { if (!_hmTaRows) { _hmTaRows = {}; _hmGraphScores = {}; } }  // 拉失败保留旧数据别清空
    return _hmTaRows;
  }

  function _hmGraphItems() {
    const data = window.hmState.lastData;
    if (!data || !data.items) return [];
    let items = _hmApplyFilter(data.items.filter(it =>
      it.has_quote && !it.is_etf));
    // 📊 Barchart 榜单筛选: graph 视图不受 dataMode 门控(与 AI 筛选一致);
    //    stocks 模式下 _hmApplyTaFilter 已筛过, 此处对同一集合再筛=幂等; 非 stocks 模式靠这里生效。
    const bcSel = window.hmState.taBcTickers;
    if (bcSel && bcSel.length) {
      const bcs = new Set(bcSel.map(t => String(t).toUpperCase()));
      items = items.filter(it => bcs.has(String(it.ticker).toUpperCase()));
    }
    const aiF = window.hmState.graphAiFilter || 'all';
    if (aiF !== 'all' && _hmAiPicks) {
      items = items.filter(function (it) {
        const p = _hmAiPicks[it.ticker];
        if (!p) return false;
        if (aiF === '看涨' || aiF === '看跌') return (_HM_AI_META[p.verdict] || {}).group === aiF;
        return p.verdict === aiF;
      });
    }
    const sort = window.hmState.graphSort || 'mcap';
    const sc = _hmGraphScores || {};
    items = items.slice();
    if (sort === 'change') items.sort((a, b) => _hmChg(b) - _hmChg(a));
    else if (sort === 'score') items.sort((a, b) => (((sc[b.ticker] || {}).score) || -1) - (((sc[a.ticker] || {}).score) || -1));
    else items.sort((a, b) => (b.market_cap || 0) - (a.market_cap || 0));
    return items;
  }

  function _hmMcapLabel() {
    const m = window.hmState.mcapMin || 0;
    return m >= 1e11 ? zt('大盘≥100B', 'large caps ≥100B') : m >= 1e10 ? '≥10B' : zt('全部', 'all');
  }

  window.hmRenderGraph = async function () {
    const grid = document.getElementById('hmGraphGrid');
    if (!grid) return;
    await _hmEnsureScores();
    // 记下这次画的时候行情到没到: 没到就是「0 票」, 行情一到要整墙重画(_hmGraphCatchUp)
    window.hmState.graphDrawnWithData = !!(((window.hmState.lastData || {}).items) || []).length;
    const items = _hmGraphItems();
    const cnt = document.getElementById('hmGraphCount');
    // 空结果时原来把计数器置成空字符串 —— 而旁边 Barchart 面板还写着「·1选 交集 ·3票」
    //    (那是过滤前的原始名单), 两个计数器对不上又没有一个说"这里是 0"。明写 0。
    if (cnt) cnt.textContent = zt(items.length + ' 票 · ', items.length + ' tickers · ') + _hmMcapLabel();
    // 拆掉上一轮的 observer / 队列
    if (window.hmState.graphIO) { try { window.hmState.graphIO.disconnect(); } catch (e) {} }
    window.hmState.graphQueue = []; window.hmState.graphActive = 0;
    if (!items.length) {
      // 提示要指向**这个视图真正存在的**筛选器。原文写「放宽板块筛选」, 但 Graph
      //    视图压根没有板块筛选控件, 照着找会找不到; 实际会把结果筛空的是财报/
      //    Barchart/AI 这三个。
      grid.innerHTML = '<div style="grid-column:1/-1;color:#6e7681;padding:30px;text-align:center;line-height:1.7">'
        + '当前筛选无票<br><span style="font-size:12px;color:#565f6a">'
        + '检查上方: 📅财报过滤 · 📊Barchart 榜单 · 🚀AI选股, 或把市值调回「全部」</span></div>';
      return;
    }
    const days = window.hmState.graphDays || 126;
    const sc = _hmGraphScores || {};
    grid.innerHTML = items.map(it => _hmGraphCardHTML(it, sc[it.ticker])).join('');
    _hmApplyGraphCols();   // 应用「每行张数」(自动/3/4/5/6)
    // 重渲染(排序/筛选/每行数变化)会重建所有 .hm-card-detail(display:none) →
    // 若处于「全展开」态, 按钮仍说「收起」但实际全收起了(状态说谎)。这里跟随:
    // 全展开态下重渲后, 用缓存把新一批卡片重新展开(缓存已有则秒开, 没有的留空)。
    if (_hmAllExpanded) {
      grid.querySelectorAll('.hm-card-detail').forEach(b => {
        const f = _hmCardFullCache[b.getAttribute('data-detail')];
        if (f) { b.innerHTML = _hmRenderCardFull(f); b.style.display = 'block'; }
      });
    }
    // 点开卡片 → TA 详情页(避免内联 onclick 转义坑,JS 里挂)
    grid.querySelectorAll('.hm-gcard').forEach(card => {
      // 点卡片 → **就地展开**全文(AI理由+新闻/多空全bullet/公司简介), 不再跳详情页。
      // 作者: 「点进去很浪费时间」——iframe 加载慢; 这里 fetch 一个小 JSON 就地渲染。
      card.onclick = () => _hmToggleCardDetail(card.dataset.ticker, card);
    });
    // 🔎 深度 → 跳 /ta/ 详情页(公司简介/财报/SEC 那些「有的没的」); 阻止冒泡不触发就地展开
    grid.querySelectorAll('.hm-card-deep').forEach(b => {
      b.onclick = (ev) => { ev.stopPropagation();
        if (window.openTaTicker) window.openTaTicker(b.getAttribute('data-deep')); };
    });
    // 进视口才拉图(rootMargin 提前 400px 预拉)
    const io = new IntersectionObserver((ents) => {
      ents.forEach(e => { if (e.isIntersecting) { io.unobserve(e.target); _hmQueueCard(e.target, days); } });
    }, { root: grid, rootMargin: '400px' });
    grid.querySelectorAll('canvas[data-ticker]').forEach(cv => io.observe(cv));
    window.hmState.graphIO = io;
  };

  // ── Graph 卡片下方三行: 📍SR(当天) · ⚖️多空 · 🚀AI(选股+新闻) ──
  // 作者口径: 分类各一行, 缺席就不画那行(不占位、不显示「无数据」——160 张卡
  // 全是空行会很难看)。全文点卡片进单票详情看。
  function _hmCardLine(icon, color, text, title) {
    // 摘要行不再截断(作者要看全名) —— 允许折行, 完整显示。
    return '<div style="color:' + color
      + ';font-size:9.5px;line-height:1.45;margin-bottom:2px;word-break:break-word">'
      + icon + ' ' + hmEsc(text) + '</div>';
  }
  function _hmCardLinesHTML(it) {
    const c = it && it.card;
    if (!c) return '';
    let h = '';
    if (c.sr) {
      const d = c.sr.dir === 'sup' ? '支撑' : (c.sr.dir === 'res' ? '阻力' : c.sr.dir || '');
      const head = (c.sr.hhmm || '') + ' ' + d + (c.sr.zone ? ' ' + c.sr.zone : '');
      h += _hmCardLine('📍', '#d29922', head + (c.sr.why ? ' · ' + c.sr.why : ''),
                       'SR 告警 ' + head + (c.sr.why ? '\n' + c.sr.why : ''));
    }
    if (c.nb) h += _hmCardLine('⚖️', '#8b949e', c.nb, '多空净判断:\n' + c.nb);
    if (c.ai) {
      const v = c.ai.verdict || '';
      const conf = (c.ai.confidence != null) ? (' ' + c.ai.confidence) : '';
      const body = c.ai.news || c.ai.why || '';
      h += _hmCardLine('🚀', '#a371f7', (v ? v + conf + ' · ' : '') + body,
                       'AI 选股: ' + v + conf + (c.ai.why ? '\n理由: ' + c.ai.why : '')
                       + (c.ai.news ? '\n新闻: ' + c.ai.news : ''));
    }
    return h;
  }

  // 📅 财报徽章: 'Aug 20 (100d) 盘后'。≤7d 红 · ≤14d 橙 · 其余灰; 已过去的显示 '已报'。
  // ETF 不显示(没有财报); 公司拿不到数据显示灰色 '?' —— 宁可让数据缺口可见, 不静默隐藏。
  const _HM_MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function _hmEarnBadgeHTML(it) {
    if (!it || it.is_etf || it.no_earnings) return '';   // ETF/基金本就没有财报
    const e = it.earnings;
    if (!e) return '<div style="color:#484f58;font-size:9.5px;margin-bottom:4px">📅 财报 ?</div>';
    // 没有下一次日期但知道上一次 → 刚报完, 对「别撞上财报」而言是最安全的一档,
    // 显示「Jul 14 已报」比无信息的「?」有用得多。
    if (!e.date || e.days == null) {
      if (!e.last_date) return '<div style="color:#484f58;font-size:9.5px;margin-bottom:4px">📅 财报 ?</div>';
      const lp = String(e.last_date).split('-');
      const lmd = (lp.length === 3) ? (_HM_MON[+lp[1] - 1] + ' ' + (+lp[2])) : e.last_date;
      return '<div title="最近一次财报 ' + hmEsc(e.last_date) + ' — 下一季日期尚未公布(通常约三个月后)" '
        + 'style="color:#3fb950;font-size:9.5px;margin-bottom:4px">📅 ' + hmEsc(lmd) + ' 已报</div>';
    }
    const p = String(e.date).split('-');
    const md = (p.length === 3) ? (_HM_MON[+p[1] - 1] + ' ' + (+p[2])) : e.date;
    const d = e.days;
    const when = e.time === 'am' ? ' 盘前' : (e.time === 'pm' ? ' 盘后' : '');
    const est = e.estimated ? '约 ' : '';   // 按季度推算的粗估日期, 标出来别当确认日用
    let col = '#6e7681', txt;
    if (d < 0) { txt = md + ' (已报)'; }
    else {
      txt = est + md + ' (' + d + 'd)' + when;
      if (d <= 7) col = '#f85149';
      else if (d <= (window.hmState.earnDays == null ? 14 : window.hmState.earnDays)) col = '#d29922';
    }
    const bold = (d >= 0 && d <= 14) ? ';font-weight:700' : '';
    return '<div title="' + (e.estimated ? '按季度推算的预估日期(上次 ' + hmEsc(e.last_date || '') + ')' : '财报 ' + hmEsc(e.date)) + (when ? ' ' + when.trim() : '')
      + '" style="color:' + col + ';font-size:9.5px;margin-bottom:4px' + bold + '">📅 ' + hmEsc(txt) + '</div>';
  }

  function _hmGraphCardHTML(it, scoreObj) {
    const chg = _hmChg(it), up = chg >= 0;
    const chgTxt = (up ? '+' : '') + (chg || 0).toFixed(2) + '%';
    const sName = hmEsc(it.sector || '');
    const px = _hmPx(it);
    const pxTxt = (px != null) ? fmtPrice(px) : '—';
    // 🚀 AI 选股徽章(点它展开理由, stopPropagation 不触发整卡开详情页)
    let aiBadge = '';
    const ap = _hmAiPicks && _hmAiPicks[it.ticker];
    if (ap && _HM_AI_META[ap.verdict]) {
      const m = _HM_AI_META[ap.verdict];
      const conf = (ap.confidence != null) ? (' ' + ap.confidence) : '';
      aiBadge = '<span class="hm-ai-badge" data-ai-ticker="' + it.ticker + '" title="AI选股:' + hmEsc(ap.verdict) + ' — 点看理由" '
        + 'style="background:' + m.col + '22;border:1px solid ' + m.col + ';color:' + m.col
        + ';border-radius:3px;padding:0 5px;font-size:10px;font-weight:700;cursor:pointer">' + m.mark + m.short + conf + '</span>';
    }
    return '<div class="hm-gcard" data-ticker="' + it.ticker + '" '
      + 'style="background:#0d1117;border:1px solid #21262d;border-radius:7px;padding:8px 9px;cursor:pointer;transition:border-color .15s" '
      + 'onmouseover="this.style.borderColor=&quot;#388bfd&quot;" onmouseout="this.style.borderColor=&quot;#21262d&quot;">'
      + '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">'
      +   '<span style="font-weight:700;color:#e6edf3;font-size:13px">' + it.ticker + '</span>'
      +   '<span style="color:' + (up ? '#3fb950' : '#f85149') + ';font-size:11px;font-weight:600">' + chgTxt + '</span>'
      +   aiBadge
      +   '<span style="margin-left:auto;color:#8b949e;font-size:10px;font-variant-numeric:tabular-nums">' + pxTxt + ' · ' + hmFmtMcap(it.market_cap) + '</span>'
      +   '<button class="hm-card-deep" data-deep="' + it.ticker + '" title="深度: TA + 公司简介 + 财报 + 新闻 + SEC" '
      +     'style="background:transparent;border:1px solid #30363d;color:#8b949e;border-radius:3px;padding:0 5px;font-size:10px;cursor:pointer;flex-shrink:0">🔎 深度</button>'
      + '</div>'
      + _hmEarnBadgeHTML(it)
      + _hmCardLinesHTML(it)
      + '<div style="color:#6e7681;font-size:9.5px;margin-bottom:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + sName + '</div>'
      + '<div class="hm-ai-reason" data-ai-reason="' + it.ticker + '" style="display:none;margin-bottom:6px"></div>'
      + '<div class="hm-card-detail" data-detail="' + it.ticker + '" style="display:none;margin:4px 0 6px;font-size:10px"></div>'
      + '<canvas data-ticker="' + it.ticker + '" style="width:100%;height:172px;display:block"></canvas>'
      + '</div>';
  }

  function _hmQueueCard(cv, days) {
    window.hmState.graphQueue.push({ cv, days });
    _hmPumpGraph();
  }
  function _hmPumpGraph() {
    const st = window.hmState;
    while (st.graphActive < _HM_GRAPH_MAXCONC && st.graphQueue.length) {
      const job = st.graphQueue.shift();
      st.graphActive++;
      _hmLoadGraphCard(job.cv, job.days).finally(() => { st.graphActive--; _hmPumpGraph(); });
    }
  }
  async function _hmLoadGraphCard(cv, days, attempt) {
    attempt = attempt || 0;
    const t = cv.dataset.ticker, key = t + '_' + days;
    const cx = cv.getContext('2d');
    const dpr = window.devicePixelRatio || 1, W = cv.clientWidth || 300, H = 172;
    function _msg(text) {
      cv.width = W * dpr; cv.height = H * dpr; cx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cx.clearRect(0, 0, W, H); cx.fillStyle = '#6e7681'; cx.font = '12px sans-serif'; cx.fillText(text, 10, 22);
    }
    try {
      let d = _hmOhlcCache[key];
      if (!d) {
        d = await (await fetch('/api/ta/ohlc/' + encodeURIComponent(t) + '?days=' + days)).json();
        if (d && d.c && d.c.length) _hmOhlcCache[key] = d;
      }
      if (!d || !d.c || !d.c.length) {
        // 池外大盘股首拉可能被 Yahoo 限流返空 → 退避重试(后台预热也在并行灌缓存,几秒后多半就有了)
        if (attempt < 3) { _msg('加载中…'); setTimeout(() => _hmLoadGraphCard(cv, days, attempt + 1), 1500 * (attempt + 1)); return; }
        _msg('暂无数据'); return;
      }
      cv.width = W * dpr; cv.height = H * dpr; cx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cx.clearRect(0, 0, W, H);
      _hmDrawCandles(cx, d, W, H);
    } catch (e) {
      if (attempt < 3) { _msg('加载中…'); setTimeout(() => _hmLoadGraphCard(cv, days, attempt + 1), 1500 * (attempt + 1)); }
      else { _msg('暂无数据'); }
    }
  }

  // ↓ ta_screener/page.py drawCandles 的等价移植(S/R band + ⭐共振 + EMA + 量 + 末价标 + 底部日期)
  function _hmDrawCandles(cx, d, W, H) {
    const n = d.c.length, padR = 46, padL = 4, padT = 6;
    const dateH = 10, volH = 26, gap = 4;
    const priceB = H - volH - gap - dateH, priceH = priceB - padT;
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < n; i++) { if (d.l[i] != null && d.l[i] < lo) lo = d.l[i]; if (d.h[i] != null && d.h[i] > hi) hi = d.h[i]; }
    [d.sma50, d.sma200].forEach(s => s && s.forEach(v => { if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; } }));
    if (!(hi > lo)) return;
    const pad = (hi - lo) * 0.04; lo -= pad; hi += pad;
    const plotW = W - padL - padR;
    const X = i => padL + (n <= 1 ? plotW / 2 : i / (n - 1) * plotW);
    const Y = v => padT + (1 - (v - lo) / (hi - lo)) * priceH;
    const bw = Math.max(1, Math.min(7, plotW / n * 0.66));
    cx.font = '9px -apple-system,monospace'; cx.textAlign = 'left';
    cx.strokeStyle = 'rgba(120,130,150,0.12)';
    for (let k = 0; k <= 3; k++) { const v = lo + (hi - lo) * k / 3, y = Y(v); cx.beginPath(); cx.moveTo(padL, y); cx.lineTo(W - padR, y); cx.stroke(); cx.fillStyle = '#6e7681'; cx.fillText(v.toFixed(v >= 100 ? 0 : 1), W - padR + 3, y + 3); }
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
    const volTop = H - dateH;    // 量柱底 = 日期行上沿
    let vmax = 1; for (let i = 0; i < n; i++) if (d.v[i] > vmax) vmax = d.v[i];
    for (let i = 0; i < n; i++) { const up = d.c[i] >= d.o[i]; const vh = (d.v[i] / vmax) * volH; cx.fillStyle = up ? 'rgba(63,185,80,0.45)' : 'rgba(248,81,73,0.45)'; cx.fillRect(X(i) - bw / 2, volTop - vh, bw, vh); }
    // 底部日期行(4-6 个均分, 8px monospace, 若 d.d 缺失则跳过)
    if (d.d && d.d.length === n && n > 1) {
      const nLbl = Math.min(6, Math.max(3, Math.floor(plotW / 60)));
      cx.fillStyle = '#6e7681'; cx.font = '8px -apple-system,monospace'; cx.textAlign = 'center';
      for (let k = 0; k < nLbl; k++) {
        const i = Math.round((n - 1) * k / (nLbl - 1));
        const lbl = String(d.d[i] || '').replace(/^\d{4}-/, '');  // 只留 MM-DD
        cx.fillText(lbl, X(i), H - 1);
      }
      cx.textAlign = 'left';
    }
    for (let i = 0; i < n; i++) {
      if (d.o[i] == null) continue;
      const up = d.c[i] >= d.o[i], col = up ? '#3fb950' : '#f85149';
      cx.strokeStyle = col; cx.fillStyle = col;
      cx.beginPath(); cx.moveTo(X(i), Y(d.h[i])); cx.lineTo(X(i), Y(d.l[i])); cx.stroke();
      const yo = Y(d.o[i]), yc = Y(d.c[i]); cx.fillRect(X(i) - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
    }
    const line = (s, color) => {
      if (!s) return; cx.strokeStyle = color; cx.lineWidth = 1.2; cx.beginPath(); let started = false;
      for (let i = 0; i < n; i++) { if (s[i] == null) { started = false; continue; } const x = X(i), y = Y(s[i]); if (!started) { cx.moveTo(x, y); started = true; } else cx.lineTo(x, y); }
      cx.stroke(); cx.lineWidth = 1;
    };
    line(d.sma50, '#e3a008'); line(d.sma200, '#2f81f7');
    const last = d.c[n - 1]; if (last != null) { const y = Y(last); cx.fillStyle = '#e6b800'; cx.fillRect(W - padR, y - 7, padR, 14); cx.fillStyle = '#000'; cx.textAlign = 'left'; cx.fillText(last.toFixed(last >= 100 ? 0 : 1), W - padR + 3, y + 3); }
  }

  // ── Table view ───────────────────────────────────────────────────────
  function hmFmtMcap(v) {
    if (!v) return '—';
    if (v >= 1e12) return (v / 1e12).toFixed(2) + 'T';
    if (v >= 1e9)  return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6)  return (v / 1e6).toFixed(1) + 'M';
    return String(Math.round(v));
  }
  function hmFmtPct(p) {
    if (p === null || p === undefined || isNaN(p)) return '—';
    return (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  }
  function hmFmtPrice(p) {
    if (!p) return '—';
    if (p >= 1000) return p.toFixed(0);
    return p.toFixed(2);
  }
  function hmEsc(s) {
    return (s == null ? '' : String(s))
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // Wrap a ticker into a clickable deep-link → opens the TA single-ticker page.
  // A single capture-phase listener (installed in the parent PWA, see
  // optionstrat_server openTaTicker) catches clicks on [data-ta-tk] anywhere, so
  // every table/list ticker becomes a link without per-row handlers. Keeps the
  // original blue/bold look; pass extra inline style (e.g. a fixed width) via
  // `extra`. SVG charts wire clicks separately via d3 .on('click').
  function hmTk(t, extra) {
    const s = hmEsc(String(t == null ? '' : t).toUpperCase());
    return '<span class="hm-ta-tk" data-ta-tk="' + s + '"'
      + ' style="color:#58a6ff;font-weight:700;cursor:pointer' + (extra || '') + '"'
      + ' title="点击看 ' + s + ' 单票详情">' + s + '</span>';
  }








  // Compact view (the default "movers"): two ranked columns — gainers (≥ +2%)
  // left, losers (≤ -2%) right. Each row is just ticker · price · % change.
  // Unlike the full table this INCLUDES ETFs (per user spec — ETFs like SOXL /
  // TQQQ are big movers); they have no sector so the full table omits them.

  // Threshold: |chg|≥2% movers vs 全部. Changing it forces a re-sort.
  // Manual re-sort: re-rank by the current live prices right now.


  // Table view is compact "movers" only now — the detailed full table (and its
  // News column) was removed per user. hmRenderTable just renders the compact
  // gainers/losers columns; kept as the public name everything already calls.

  // ── 历史表现 Table (stocks 模式, 多周期涨跌排行) ──

  // ── β/ρ 筛选: 懒加载风险数据, 按 beta/correlation 过滤 ──
  let _hmRiskByTicker = null;   // {ticker -> {SPY: {beta,corr}, QQQ: {beta,corr}}}
  async function _hmLoadRiskForFilter() {
    if (_hmRiskByTicker) return;
    try {
      const d = await (await fetch('/api/heatmap/risk_stats')).json();
      const m = {};
      (d.items || []).forEach(it => { if (it.ticker) m[it.ticker] = it; });
      _hmRiskByTicker = m;
    } catch (e) { _hmRiskByTicker = {}; }
  }
  window.hmSetBetaFilter = async function (val) {
    window.hmState.betaFilter = val;
    const sel = document.getElementById('hmBetaFilter');
    if (sel && sel.value !== val) sel.value = val;
    if (val) await _hmLoadRiskForFilter();
    const v = window.hmState.view;
    if (v === 'graph') hmRenderGraph();
    else render();
  };

  // ── Table 模式切换: 今日涨跌 / 历史表现 ──

  // ── Sector + Industry multi-select filters ──────────────────────────
  // Apply to whichever view is active. Filtering happens before the
  // treemap/table even sees the data so both views share the same logic.
  function _hmApplyFilter(items) {
    const st = window.hmState;
    const mcapMin = st.mcapMin || 0;
    let out = items;
    // β/ρ 筛选(stocks 模式, 需要风险数据已加载; 数据嵌套: SPY.3m.corr)
    if (st.dataMode === 'stocks' && st.betaFilter && _hmRiskByTicker) {
      const bf = st.betaFilter;
      out = out.filter(it => {
        const r = _hmRiskByTicker[it.ticker]; if (!r) return false;
        const sp = r.SPY && r.SPY['3m'], qp = r.QQQ && r.QQQ['3m'];
        if (bf === 'spy_low_corr') return sp && sp.corr != null && Math.abs(sp.corr) < 0.5;
        if (bf === 'qqq_low_corr') return qp && qp.corr != null && Math.abs(qp.corr) < 0.5;
        if (bf === 'spy_high_beta') return sp && sp.beta != null && sp.beta > 1.2;
        if (bf === 'spy_low_beta') return sp && sp.beta != null && sp.beta < 0.8;
        return true;
      });
    }
    // 市值下限 (compact 精选)：大盘≥100B。ETF 无市值,留给 ETF 模式自己处理。
    if (mcapMin > 0) out = out.filter(it => (it.market_cap || 0) >= mcapMin);
    // (Sector/Industry 两个 Set 的过滤已随选择器一起删 —— 它们恒为空集, 这个条件永远为假)
    out = _hmApplyEarnFilter(out);  // 📅 财报筛选 — 三视图/各 dataMode 统一生效
    return _hmApplyTaFilter(out);   // 📊 Barchart 榜单交集(stocks 模式)
  }

  // ── 📅 财报过滤 ───────────────────────────────────────────────────
  // 从 _hmPassTa 里搬出来: 以前挂在 TA 管线上, 只覆盖有 TA 行的 132/160 只,
  // 没 TA 行的票在筛选激活时被整个排除, ETF 也会误伤。现在直接用 payload 的
  // it.earnings(_build_response 带上, 全 universe 覆盖), 三个视图一致生效。
  function _hmIsEarnSoon(it) {
    const e = it && it.earnings;
    if (!e || e.days == null) return false;
    const n = window.hmState.earnDays;
    return e.days >= 0 && e.days <= (n == null ? 14 : n);
  }
  function _hmApplyEarnFilter(items) {
    const em = window.hmState.taEarn;
    if (!em) return items;
    return items.filter(it => {
      if (it && it.is_etf) return true;          // ETF 没有财报, 永不过滤
      const soon = _hmIsEarnSoon(it);
      return em === 'ex' ? !soon : soon;
    });
  }
  // stocks 模式 + 有激活的 TA 筛选时, 用 _hmTaRows(按 ticker) 过滤; 无 TA 数据的票在激活筛选下排除。
  // momentum (rebound/pullback) 不依赖 _hmTaRows, 走独立 Set 查找, 和 BC/TA 自然交集。
  function _hmApplyTaFilter(items) {
    const st = window.hmState;
    if (st.dataMode !== 'stocks') return items;        // 榜单以个股 ticker 为键, 别误筛 ETF/risk/perf
    const bc = st.taBcTickers;                          // 📊 Barchart 榜单交集 tickers(null=不筛)
    if (!(bc && bc.length)) return items;
    const s = new Set(bc);
    return items.filter(it => s.has(String(it.ticker).toUpperCase()));
  }

  // (原 window.taFilterApi 桥接已删 —— 它只服务于 /static/ta_filters.js 渲染的
  //  #taFilterDD/#taPresetBar 两个下拉, 那两个容器随 9f8c6d3 一并删了, 主页面的
  //  <script src=ta_filters.js> 也已移除。📊Barchart 榜单走 heatmap 自己的 hmBc*
  //  面板直接写 hmState.taBcTickers, 不经过这座桥, 不受影响。)
  window.hmSetTaEarn = function (v) {
    window.hmState.taEarn = v || '';
    _hmEarnReRender();
  };
  window.hmSetEarnDays = function (v) {
    const n = parseInt(v, 10);
    window.hmState.earnDays = (isNaN(n) || n < 0) ? 14 : n;
    _hmEarnReRender();
  };
  // 财报筛选不依赖 TA 行, 不必等 _hmEnsureScores; 各视图都要重渲。
  function _hmEarnReRender() {
    if (window.hmState.view === 'graph') hmRenderGraph();
    else render();
  }
  // 默认「排除临财报 14 天内」是 hmState 的初值, 但 <select>/<input> 的 HTML 默认
  // 是「全部」/14 → 启动时把控件同步到 state, 否则界面显示与实际筛选不一致。
  (function _hmSyncEarnControls() {
    const sel = document.getElementById('hmTaEarn');
    if (sel) sel.value = window.hmState.taEarn || '';
    const inp = document.getElementById('hmEarnDays');
    if (inp) inp.value = window.hmState.earnDays;
  })();

  // (📈反弹/📉回撤 动量信号整块已删 —— 9f8c6d3 删掉两个按钮后就再没有入口,
  //  但 hmLoadMomentum 仍在每次进 heatmap 时被调用, 白跑一次
  //  /api/heatmap/momentum_signals 让后端算 402 只票。后端接口和 heatmap/momentum.py
  //  纯函数包保留(harness 还在跑), 只摘掉前端这条死链。)

  // ═══ EOD 期权链爬取状态条 + 「⚡现在盘后爬取」(移植自 ta_screener/page.py; 端点 /api/ta/eod_*) ═══
  let _hmEodS = null, _hmEodPoll = null;
  window.hmLoadEodStatus = async function () {
    const el = document.getElementById('eodStatus');
    if (!el) return;
    let s = null;
    try { s = await fetch('/api/ta/eod_status').then(r => r.json()); } catch (e) {}
    _hmEodS = s;
    if (!s || !s.ok) { el.style.display = 'none'; return; }
    const col = s.state === 'done' ? '#3fb950' : (s.state === 'pending' ? '#d8943a' : '#8b949e');
    const icon = s.state === 'done' ? '🟢' : (s.state === 'pending' ? '🟠' : '⚪');
    const tip = (s.summary || []).join(' · ');
    let btn = '';
    if (s.state === 'pending') btn = '<button onclick="hmRunEodCrawl()" style="margin-left:8px;background:#1f6feb;color:#fff;border:none;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;font-weight:600;white-space:nowrap">⚡ 现在盘后爬取</button>';
    else if (s.state === 'done') btn = '<button onclick="hmRunEodCrawl()" title="重爬今天的 EOD 期权链" style="margin-left:8px;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer;white-space:nowrap">↻ 重爬</button>';
    el.innerHTML = '<span style="font-size:13px">' + icon + '</span>' +
      '<b style="color:' + col + '">EOD 爬取</b>' +
      '<span>' + zt(s.label || '', s.label_en || s.label || '') + '</span>' +
      (tip ? '<span style="color:#8b949e;cursor:help" title="' + tip.replace(/"/g, '&quot;') + '">· ' + tip + '</span>' : '') +
      (s.file_mtime ? '<span style="color:#8b949e;margin-left:auto">数据文件 ' + s.file_mtime + '</span>' : '') +
      '<span onclick="hmLoadEodStatus()" title="刷新状态" style="cursor:pointer;opacity:.6;margin-left:' + (s.file_mtime ? '6px' : 'auto') + '">↻</span>' +
      btn;
    el.style.display = 'flex';
    try {
      const cs = await fetch('/api/ta/eod_crawl/status').then(r => r.json());
      if (cs && cs.running) { document.getElementById('eodCrawlProg').style.display = 'flex'; hmEodCrawlPoll(); }
    } catch (e) {}
  };
  window.hmRunEodCrawl = async function () {
    const s = _hmEodS || {};
    if (s.before_close) {
      if (!confirm('市场还没收盘(收盘 15:00 德州时间)。现在爬到的是盘中快照, 会被当成今天的 EOD 永久写入 —— 之后的自动任务发现今天已有数据就不再爬了。确定现在就爬?')) return;
    }
    document.getElementById('eodCrawlProg').style.display = 'flex';
    document.getElementById('eodCrawlFill').style.width = '0%';
    document.getElementById('eodCrawlMsg').textContent = '启动爬取…';
    try { await fetch('/api/ta/eod_crawl/run', { method: 'POST' }); } catch (e) {}
    hmEodCrawlPoll();
  };
  async function hmEodCrawlPoll() {
    if (_hmEodPoll) clearTimeout(_hmEodPoll);
    let st = null;
    try { st = await fetch('/api/ta/eod_crawl/status').then(r => r.json()); } catch (e) {}
    const fill = document.getElementById('eodCrawlFill'), msg = document.getElementById('eodCrawlMsg');
    if (st && st.running) {
      if (fill) fill.style.width = (st.pct || 0) + '%';
      if (msg) msg.textContent = st.msg || '爬取中…';
      _hmEodPoll = setTimeout(hmEodCrawlPoll, 2000);
      return;
    }
    if (st && st.phase === 'error') {
      if (fill) { fill.style.width = '100%'; fill.style.background = '#f85149'; }
      if (msg) msg.textContent = '❌ ' + (st.error || st.msg || '失败');
      setTimeout(() => { const p = document.getElementById('eodCrawlProg'); if (p) p.style.display = 'none'; if (fill) fill.style.background = 'linear-gradient(90deg,#1f6feb,#3fb950)'; }, 5000);
      return;
    }
    if (fill) fill.style.width = '100%';
    if (msg) msg.textContent = '✅ 完成';
    setTimeout(() => { const p = document.getElementById('eodCrawlProg'); if (p) p.style.display = 'none'; }, 2500);
    hmLoadEodStatus();
  }

  // ── 单票详情 overlay(复用 /ta/?ticker= 详情页; 替代原 TA tab 跳转) ──
  window.hmOpenTickerDetail = function (sym) {
    sym = String(sym || '').toUpperCase().replace(/^\$/, '').trim();
    if (!sym) return;
    const ov = document.getElementById('hmDetailOverlay');
    const fr = document.getElementById('hmDetailFrame');
    const lb = document.getElementById('hmDetailSym');
    if (lb) lb.textContent = sym;
    if (fr) fr.src = '/ta/?ticker=' + encodeURIComponent(sym);
    const bc = document.getElementById('hmBcPanel');      // 收起 Barchart 下拉, 免得从详情里穿出来
    if (bc) bc.style.display = 'none';
    if (ov) ov.style.display = 'flex';
  };
  // ESC 关详情(唯一出口原本只有右上角「✕ 关闭」按钮)。监听只绑一次, 不随开关累加。
  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Escape') return;
    const ov = document.getElementById('hmDetailOverlay');
    if (ov && ov.style.display !== 'none') { ev.stopPropagation(); window.hmCloseTickerDetail(); }
  });
  window.hmCloseTickerDetail = function () {
    const ov = document.getElementById('hmDetailOverlay');
    if (ov) ov.style.display = 'none';
    const fr = document.getElementById('hmDetailFrame');
    if (fr) fr.src = 'about:blank';   // 停掉后台 iframe(别留着跑)
  };

  // (Sectors/Industries 两个多选下拉整块已删 —— 9f8c6d3 按作者「反馈没用」删了
  //  #hmSectorBtn/#hmIndustryBtn 两个入口, 但 _hmPopulatePickers 仍在每次数据
  //  刷新时被调用(3 处), 空转遍历全 universe 算 sector/industry 去重列表,
  //  再往两个不存在的 panel 写 innerHTML。sectorFilter/industryFilter 恒为空集,
  //  过滤器里那两行条件永远为真。)

  // Auto-poll interval selector — default 1h per user spec
  window.hmSetAutoInterval = function () {
    const sel = document.getElementById('hmAutoSel');
    const secs = sel ? parseInt(sel.value, 10) || 0 : 0;
    window.hmState.autoIntervalSecs = secs;
    stopPolling();
    if (secs > 0) startPolling();
  };

  // Status line: session / freeze / stream state. Frozen = market closed and
  // the close is served from disk (no upstream requests).
  function _hmStatusHTML(payload) {
    const t = payload.generated_at ? payload.generated_at.replace('T',' ').replace('Z',' UTC') : 'never';
    const quoted = (payload.items || []).filter(it => it.has_quote).length;
    const errBit = payload.last_poll_ok ? '' : ` · <span style="color:#f85149">poll error</span>`;
    const sm = payload.stream || {};
    let bit;
    if (payload.frozen) {
      const d = payload.trading_date ? ' ' + payload.trading_date : '';
      bit = ` · <span style="color:#d29922">🔒 已收盘冻结${d}</span>`;
    } else if (sm.live) {
      const sess = { PRE: '🌅 盘前实时', POST: '🌙 盘后实时' }[payload.session] || '🟢 实时流';
      bit = ` · <span style="color:#3fb950">${sess} ${sm.symbols}</span>`;
    } else {
      const sess = { PRE: '🌅 盘前', POST: '🌙 盘后', RTH: '盘中' }[payload.session] || 'REST 轮询';
      bit = ` · <span style="color:#8b949e">${sess}</span>`;
    }
    return `${quoted} / ${payload.count} quotes · last ${t}${bit}${errBit}`;
  }

  // When the close is frozen there's nothing new to fetch — drop the periodic
  // REST poll. The SSE stays open (slow cadence) and resumes live at the open.
  function _hmManagePolling(payload) {
    if (payload && payload.frozen) stopPolling();
    else if (window.hmState.dataMode === 'stocks') startPolling();
  }

  // 新行情到了: 图墙若是在行情到之前画的(显示「0 票」)就整墙重画, 否则只轻更新每张卡的价格。
  // 以前只做轻更新 → 刚启动、或开盘前后第一次进选股时, 图墙会一直停在「0 票」。
  function _hmGraphCatchUp() {
    if (window.hmState.view !== 'graph') return;
    if (!window.hmState.graphDrawnWithData) hmRenderGraph();
    else _hmGraphUpdatePrices();
  }

  // ── Data fetch ───────────────────────────────────────────────────────
  async function fetchAndRender(showLoading) {
    const status = document.getElementById('hmStatus');
    if (showLoading && status) status.textContent = 'Fetching…';
    try {
      const r = await fetch('/api/heatmap/data');
      if (!r.ok) throw new Error(r.statusText);
      const payload = await r.json();
      window.hmState.lastData = payload;
      window.hmState.lastFetchAt = Date.now();
      if (status) status.innerHTML = _hmStatusHTML(payload);
      _hmManagePolling(payload);
      await render();
      // 2026-07-30 作者要求「K 线图墙价格实时」: render() 在 graph view 直接 return
      // 不重画整墙, 这里加轻更新只改每张卡 header 的 price + change_pct (不动 canvas).
      _hmGraphCatchUp();
    } catch (e) {
      if (status) status.innerHTML = `<span style="color:#f85149">${e.message}</span>`;
    }
  }

  // K 线卡 header 轻更新: 每张卡的价格 + 今日涨跌% 从 hmState.lastData.items lookup 刷新,
  // 不重画 canvas (整墙 73 张 canvas 重画很贵, 作者故意跳过).
  function _hmGraphUpdatePrices() {
    const grid = document.getElementById('hmGraphGrid');
    if (!grid) return;
    const items = (window.hmState.lastData || {}).items || [];
    const lookup = {};
    items.forEach(it => { lookup[it.ticker] = it; });
    grid.querySelectorAll('.hm-gcard[data-ticker]').forEach(card => {
      const t = card.dataset.ticker;
      const it = lookup[t];
      if (!it) return;
      const topLine = card.firstElementChild;
      if (!topLine) return;
      const spans = topLine.querySelectorAll('span');
      // spans[0] = ticker, spans[1] = change_pct (green/red)
      const chg = _hmChg(it);
      if (spans[1] && chg != null) {
        const up = chg >= 0;
        spans[1].style.color = up ? '#3fb950' : '#f85149';
        spans[1].textContent = (up ? '+' : '') + chg.toFixed(2) + '%';
      }
      // 最后一个 span (margin-left:auto) = "$price · mcap"
      const priceSpan = topLine.querySelector('span[style*="margin-left:auto"]');
      const px = _hmPx(it);
      if (priceSpan && px != null) {
        const pxTxt = fmtPrice(px);
        const orig = priceSpan.textContent;
        const dotIdx = orig.indexOf('·');
        if (dotIdx > 0) priceSpan.textContent = pxTxt + ' ·' + orig.slice(dotIdx + 1);
        else priceSpan.textContent = pxTxt;
      }
    });
  }

  window.hmRefresh = function () {
    const btn = document.getElementById('hmRefreshBtn');
    const dm = window.hmState.dataMode;
    if (dm === 'opt') {
      // 期权波动只有 GET 端点 (后端实时聚合) — 清缓存重拉当前选中交易日。
      window.hmState.optData = null;
      window.hmState.optSmileData = null;   // 微笑子视图同步重拉
      window.hmState.optExpData = null;     // 关系/实验子视图同步重拉
      _hmLoadOpt(window.hmState.optDate || null);
      if (window.hmState.optView === 'smile') _hmLoadSmile();
      if (window.hmState.optView === 'exp') _hmLoadExp();
      return;
    }
    if (dm === 'etf' || dm === 'risk' || dm === 'perf') {
      const ru = window.hmState.riskUniverse, rslot = ru === 'etf' ? 'riskEtfData' : 'riskData';
      const cfg = {
        etf:  { url: '/api/heatmap/etf_trends/refresh',  set: d => { window.hmState.etfData = d; } },
        risk: { url: '/api/heatmap/risk_stats/refresh?universe=' + ru,  set: d => { window.hmState[rslot] = d; } },
        perf: { url: '/api/heatmap/stock_perf/refresh',  set: d => { window.hmState.perfData = d; window.hmState.perfLayout = null; } },
      }[dm];
      if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
      fetch(cfg.url, { method: 'POST' })
        .then(r => r.json()).then(d => {
          cfg.set(d);
          if (cfg.render) cfg.render();
          else render();
        }).catch(() => {})
        .finally(() => { if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; } });
      return;
    }
    if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
    fetch('/api/heatmap/refresh', { method: 'POST' })
      .then(r => r.json())
      .then(payload => {
        window.hmState.lastData = payload;
        const status = document.getElementById('hmStatus');
        if (status) status.innerHTML = _hmStatusHTML(payload);
        _hmManagePolling(payload);
        return Promise.resolve(render()).then(_hmGraphCatchUp);
      })
      .catch(e => {
        const status = document.getElementById('hmStatus');
        if (status) status.innerHTML = `<span style="color:#f85149">${e.message}</span>`;
      })
      .finally(() => {
        if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
      });
  };

  function startPolling() {
    if (window.hmState.polling) return;
    const secs = window.hmState.autoIntervalSecs || 3600;
    if (secs <= 0) return;
    window.hmState.polling = true;
    window.hmState.pollTimer = setInterval(() => fetchAndRender(false), secs * 1000);
  }
  function stopPolling() {
    window.hmState.polling = false;
    if (window.hmState.pollTimer) {
      clearInterval(window.hmState.pollTimer);
      window.hmState.pollTimer = null;
    }
  }

  // ── Real-time push via SSE ───────────────────────────────────────────
  // The backend streams DXLink quotes; this SSE just relays the snapshot to
  // the browser ~every 2s so the heatmap is live without REST polling. The
  // Auto poll stays as a backstop if the stream drops.
  function _hmApplyPayload(payload) {
    if (window.hmState.dataMode !== 'stocks') return;   // ETF / risk views ignore live stock pushes
    window.hmState.lastData = payload;
    window.hmState.lastFetchAt = Date.now();
    const status = document.getElementById('hmStatus');
    if (status) status.innerHTML = _hmStatusHTML(payload);
    _hmManagePolling(payload);
    render();
    if (!window.hmState.graphDrawnWithData) _hmGraphCatchUp();
  }
  function hmStartStream() {
    if (window.hmState.dataMode !== 'stocks') return;   // ETF / risk views are not real-time
    if (window.hmState.es) return;
    if (typeof EventSource === 'undefined') return;
    try {
      const es = new EventSource('/api/heatmap/stream');
      es.onmessage = (e) => {
        let payload; try { payload = JSON.parse(e.data); } catch (_) { return; }
        if (payload && payload.items) _hmApplyPayload(payload);
      };
      es.onerror = () => { /* EventSource auto-reconnects; Auto poll covers gaps */ };
      window.hmState.es = es;
      // Auto poll (5m) stays as a harmless backstop in case the stream stalls.
    } catch (_) {}
  }
  function hmStopStream() {
    if (window.hmState.es) { try { window.hmState.es.close(); } catch (_) {} window.hmState.es = null; }
  }

  // ── 🎯 选股 sub-view: K线图墙(kline) vs mbrief 3 sub(brief/rot/opt) ──────
  // 2026-07-30: 作者要求把 mbrief tab 内容合并进 🎯 选股. 逻辑:
  //   - 'kline' → 显示 heatmap 主内容 (hmToolbar + hmGraphView + hmSvg 等)
  //   - 'brief'/'rot'/'opt' → 隐藏 heatmap 主内容, 显示 #hmSubMbriefHost, 触发 mbSub()
  // mbrief 3 sub 的 DOM 由 reorgTabs 一次性 re-parent 到 hmSubMbriefHost.
  window.hmSubView = function (name) {
    // 首次点击时确保 reorgTabs 已 re-parent mbrief 3 sub 到 hmSubMbriefHost
    if (typeof window.reorgTabs === 'function') window.reorgTabs();
    // 'kline' = K线图墙, 'heatmap' = 热力图(treemap): 选股自己的两个视图, 共用 hmToolbar
    const isKline = (name === 'kline' || name === 'heatmap');
    const tb = document.getElementById('hmToolbar');
    if (tb) tb.style.display = isKline ? '' : 'none';
    // dataMode 锁死 stocks 后, 这几个 panel 永不该显示 (bug: 之前给它们 display='' 会 fallback 到 CSS block)
    ['hmMoverNewsView', 'hmOptVolPanel', 'hmNarrativeView'].forEach(id => {
      const e = document.getElementById(id); if (e) e.style.display = 'none';
    });
    if (isKline) {
      // hmSetView 负责显示 hmGraphView(flex, 内部 grid 靠它撑高滚动)或 hmCanvas(热力图)并重画
      window.hmSetView(name === 'heatmap' ? 'treemap' : 'graph');
    } else {
      ['hmGraphView', 'hmCanvas'].forEach(id => { const e = document.getElementById(id); if (e) e.style.display = 'none'; });
    }
    const host = document.getElementById('hmSubMbriefHost');
    if (host) host.style.display = isKline ? 'none' : 'block';
    if (!isKline && typeof window.mbSub === 'function') window.mbSub(name);
    if (!isKline && typeof window.mbInit === 'function' && !window.__mbInited) {
      window.__mbInited = true;
      window.mbInit();
    }
    document.querySelectorAll('#hmSubNav button').forEach(b => {
      b.classList.toggle('active', b.dataset.name === name);
    });
    window.hmState._subView = name;
  };

  // Called by switchTopTab when this tab becomes active
  window.hmTabBecameActive = function () {
    _hmApplyToolbarCollapsed();   // 应用上次的工具栏收起/展开状态
    // 2026-07-30 轻量化: default view='graph' 只改了 state, DOM display 得走 hmSetView 才 sync.
    // 每次切回来强制同步一次(经 hmSubView → hmSetView), 避免 view=graph 但 hmGraphView 隐藏的空白态;
    // 首次进 tab 默认 K线图墙; 停在大盘判断时保持不动(以前这里会把图墙叠在大盘判断上)。
    // hmSubView 同时把 mover/narrative/optvol 等独立 panel 隐藏(它们 default = block, 需要显式 hide)
    const _sv = window.hmState._subView;
    if (window.hmSubView && (!_sv || _sv === 'kline' || _sv === 'heatmap'))
      window.hmSubView(window.hmState.view === 'treemap' ? 'heatmap' : 'kline');
    if (window.hmState.dataMode === 'etf') {
      if (!window.hmState.etfData) _hmLoadEtf();
      else { render(); }
    } else if (window.hmState.dataMode === 'risk') {
      if (!_riskActiveData()) _hmLoadRisk();
      else { render(); }
    } else if (window.hmState.dataMode === 'perf') {
      if (!window.hmState.perfData) _hmLoadPerf();
      else { render(); }
    } else if (window.hmState.dataMode === 'opt') {
      if (!window.hmState.optData) _hmLoadOpt();
      else _hmRenderOptTable();
    } else {
      // First render / refresh, then go live via SSE (poll is the backstop)
      fetchAndRender(true);
      hmStartStream();
      startPolling();
    }

    // Re-render on resize + on group/size/scale change
    const canvas = document.getElementById('hmCanvas');
    if (canvas && !window.hmState.resizeObserver) {
      window.hmState.resizeObserver = new ResizeObserver(() => render());
      window.hmState.resizeObserver.observe(canvas);
    }
    ['hmGroupBy','hmSize','hmScale'].forEach(id => {
      const el = document.getElementById(id);
      if (el && !el.dataset.hmBound) {
        el.dataset.hmBound = '1';
        el.addEventListener('change', () => render());
      }
    });
  };

  // Called by switchTopTab when leaving the heatmap tab
  window.hmTabBecameInactive = function () {
    hmStopStream();
    stopPolling();
    // ⚠️ 个股详情浮层也要收掉。它是 position:absolute;inset:0;z-index:70, 只有
    //    「✕ 关闭」和 ESC 两个出口 —— 开着它切到 Digest 再切回来, 浮层原样盖在
    //    整个 heatmap 上(工具栏/treemap 全被挡, 点不到), 而底下 hmTabBecameActive
    //    还照常重启 SSE/轮询在刷新你看不见的图。切走就关, 回来是干净的 heatmap。
    //    (顺带停掉 iframe, 不留后台跑。)
    if (typeof window.hmCloseTickerDetail === 'function') window.hmCloseTickerDetail();
  };

  // Stop streaming/polling when tab is hidden — saves bandwidth and server load
  document.addEventListener('visibilitychange', () => {
    const onHeatmap = document.getElementById('tab-heatmap')?.classList.contains('active');
    if (document.hidden) { hmStopStream(); stopPolling(); }
    else if (onHeatmap && window.hmState.dataMode === 'stocks') { fetchAndRender(false); hmStartStream(); startPolling(); }
    if (!document.hidden) _hmMaybeDailyCatchup();   // 回到电脑/睡眠唤醒 → 每天必看补弹
  });

  // ════════════════════════════════════════════════════════════════════
  // 📰 Mover News —— 当天 ±1% 异动票 + 双语新闻(digest 匹配 / finviz 有情绪标题)
  //   · 数据:/api/movers/news(后端现算 movers + 匹配 + 标题预翻)
  //   · digest 条目点标题展开双语正文(/api/digest/translate_article);finviz 只标题+外链
  //   · 「只看有新闻」过滤暂无新闻的票;「finviz 中性标题」默认隐藏中性 finviz
  // ════════════════════════════════════════════════════════════════════
  function _mvSentDot(s) {
    const c = s > 0 ? '#3fb950' : (s < 0 ? '#f85149' : '#6e7681');
    const t = s > 0 ? '利好' : (s < 0 ? '利空' : '中性');
    return '<span title="' + t + '" style="display:inline-block;width:7px;height:7px;border-radius:50%;background:' + c + ';margin:5px 0 0 0;flex:none"></span>';
  }
  function _mvChip(txt, color) {
    return '<span style="font-size:10px;color:' + color + ';border:1px solid ' + color + '55;border-radius:8px;padding:0 6px;line-height:16px;white-space:nowrap;flex:none">' + hmEsc(txt) + '</span>';
  }
  window.hmMvToggleOnlyNews = function (on) { window.hmState.moverNewsOnlyNews = !!on; _mvDraw(); };
  window.hmMvToggleNeutral = function (on) { window.hmState.moverNewsShowNeutral = !!on; _mvDraw(); };

  window.hmOpenMoverNews = function (slot) {
    try {
      _hmMarkSeenToday();
      if (window.switchTopTab) window.switchTopTab('heatmap');
      // 「📰 异动新闻」顶层模式已删(2026-07-23) —— 异动新闻现在以每票一行的形式
      // 长在 Graph 卡片上(🚀 AI 行含当天新闻总结), 所以通知深链改为直接开图墙。
      // 不能再切 movernews: 那个模式没有 chip 可以切回来, 作者会被卡住。
      if (window.hmSetDataMode) window.hmSetDataMode('stocks');
      // through hmSubView, so the Picker's sub-tabs follow: hmSetView alone drew the chart wall
      // under an open 🧭 Market Brief, the two stacked on one page
      if (window.hmSubView) window.hmSubView('kline');
      else if (window.hmSetView) window.hmSetView('graph');
      if (slot) _hmPopupToast(slot);
    } catch (e) {}
  };

  window.hmRenderMoverNews = function (force) {
    const st = window.hmState;
    const view = document.getElementById('hmMoverNewsView');
    if (view && view.style.display === 'none') return;   // 不在该视图,不拉
    const o = document.getElementById('hmMvOnlyNews'); if (o) o.checked = !!st.moverNewsOnlyNews;
    const nb = document.getElementById('hmMvShowNeutral'); if (nb) nb.checked = !!st.moverNewsShowNeutral;
    const today = _hmLocalDay();
    const fresh = st.moverNews && st.moverNewsDate === today && (Date.now() - st.moverNewsAt) < 120000;
    if (!force && fresh) { _mvDraw(); return; }
    if (st.moverNewsInflight) return;
    st.moverNewsInflight = true;
    const status = document.getElementById('hmMvStatus');
    if (status) status.textContent = '加载中…';
    fetch('/api/movers/news?threshold=1' + (force ? '&force=1' : '')).then(function (r) { return r.json(); }).then(function (d) {
      st.moverNews = d || {};
      st.moverNewsAt = Date.now();
      st.moverNewsDate = today;
      st.moverNewsInflight = false;
      _mvDraw();
    }).catch(function (e) {
      st.moverNewsInflight = false;
      if (status) status.textContent = '加载失败';
      const list = document.getElementById('hmMvList');
      if (list) list.innerHTML = '<div style="color:#f85149;padding:20px;font-size:12px">加载失败:' + hmEsc(String(e)) + '</div>';
    });
  };

  // ── 🤖 异动多空 AI 总结(复用 digest 弹窗 _dgAiModal/_dgAiMd; 后台跑+进度条+轮询; 按天缓存 scope=movers) ──
  var _mvAiPollTimer = null;
  window.moverAiSummary = function () {
    if (typeof _dgAiModal !== 'function') { alert('AI 总结组件未就绪'); return; }
    var ov = _dgAiModal();
    ov.style.display = 'flex';
    var sub = document.getElementById('dgAiSub'); if (sub) sub.textContent = '今日异动新闻 · 速读 · Haiku';
    _mvAiKick('auto');
  };
  function _mvAiKick(mode) {
    var bd = document.getElementById('dgAiBody');
    if (mode !== 'auto') _mvAiRunning(0);
    var movers = [];
    try {
      movers = (((window.hmState || {}).moverNews || {}).movers || []).map(function (m) {
        return {
          ticker: m.ticker, change_pct: m.change_pct, sector: m.sector,
          news: (m.news || []).slice(0, 4).map(function (n) { return { title_cn: n.title_cn, title_en: n.title_en, sentiment: n.sentiment }; })
        };
      });
    } catch (e) {}
    fetch('/api/movers/ai_summary', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: mode, movers: movers }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'empty') { bd.innerHTML = '<div style="color:#8b949e;padding:24px;text-align:center">今天暂无异动票可总结</div>'; return; }
        _mvAiPoll();
      })
      .catch(function (e) { bd.innerHTML = '<div style="color:#f85149;padding:20px">请求失败：' + _dgAiEsc('' + e) + '</div>'; });
  }
  function _mvAiPoll() {
    var ov = document.getElementById('dgAiOverlay');
    if (!ov || ov.style.display === 'none') return;   // 窗口关了停轮询(后台任务照跑)
    fetch('/api/digest/ai_summary/status?scope=movers')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'running') {
          _mvAiRunning(d.elapsed || 0);
          if (_mvAiPollTimer) clearTimeout(_mvAiPollTimer);
          _mvAiPollTimer = setTimeout(_mvAiPoll, 1500);
        } else if (d.status === 'done') {
          _mvAiDone(d);
        } else if (d.status === 'error') {
          var bd = document.getElementById('dgAiBody');
          bd.innerHTML = '<div style="color:#f85149;padding:20px">总结失败：' + _dgAiEsc(d.error || '') + '</div>' + _mvAiFoot();
          _mvAiWire();
        } else {
          _mvAiKick('full');   // idle → 启动
        }
      })
      .catch(function () { if (_mvAiPollTimer) clearTimeout(_mvAiPollTimer); _mvAiPollTimer = setTimeout(_mvAiPoll, 2500); });
  }
  function _mvAiRunning(elapsed) {
    var sub = document.getElementById('dgAiSub'); var bd = document.getElementById('dgAiBody');
    var pct = Math.min(96, Math.round(100 * (1 - Math.exp(-(elapsed || 0) / 8)))); if (pct < 6) pct = 6;
    if (sub) sub.textContent = '正在总结今日异动新闻 · ' + Math.round(elapsed || 0) + 's';
    bd.innerHTML = '<div style="padding:26px 8px;text-align:center"><div style="color:#8b949e;font-size:13px;margin-bottom:12px">⏳ 正在用 Haiku 总结今日异动新闻…</div><div style="height:8px;border-radius:5px;background:#21262d;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#6e40c9,#a371f7);border-radius:5px;transition:width .6s ease"></div></div><div style="color:#6e7681;font-size:11px;margin-top:10px">可以关掉窗口去干别的，后台会继续，完成后再点开就有了</div></div>';
  }
  function _mvAiFoot() {
    return '<div style="display:flex;gap:8px;align-items:center;margin-top:14px;padding-top:10px;border-top:1px solid #21262d"><button id="mvAiFull" style="background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">♻️ 重新总结</button><span style="color:#6e7681;font-size:11px">基于实时异动快照</span></div>';
  }
  function _mvAiWire() { var b = document.getElementById('mvAiFull'); if (b) b.onclick = function () { _mvAiKick('full'); }; }
  function _mvAiDone(d) {
    var sub = document.getElementById('dgAiSub'); var bd = document.getElementById('dgAiBody');
    if (_mvAiPollTimer) { clearTimeout(_mvAiPollTimer); _mvAiPollTimer = null; }
    var when = (d.ts || '').replace('T', ' ').slice(5, 16);
    if (sub) sub.textContent = (d.n || 0) + ' 只异动 · ' + _dgAiEsc(d.model || '') + (when ? (' · ' + when) : '');
    bd.innerHTML = _dgAiMd(d.summary || '(空)') + _mvAiFoot();
    _mvAiWire();
  }

  // ── 🎯 AI 抄底推荐 ──────────────────────────────────────────
  var _dipPollTimer = null;
  window.moverDipPicks = function () {
    if (typeof _dgAiModal !== 'function') { alert('AI 组件未就绪'); return; }
    var ov = _dgAiModal();
    ov.style.display = 'flex';
    var sub = document.getElementById('dgAiSub'); if (sub) sub.textContent = '🎯 AI 抄底推荐 · 分析下跌票 · Haiku';
    _dipKick('auto');
  };
  function _dipKick(mode) {
    var bd = document.getElementById('dgAiBody');
    if (mode !== 'auto') _dipRunning(0);
    var movers = [];
    try {
      movers = (((window.hmState || {}).moverNews || {}).movers || []).map(function (m) {
        return {
          ticker: m.ticker, change_pct: m.change_pct, sector: m.sector,
          news: (m.news || []).slice(0, 4).map(function (n) { return { title_cn: n.title_cn, title_en: n.title_en, sentiment: n.sentiment }; })
        };
      });
    } catch (e) {}
    fetch('/api/movers/dip_picks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: mode, movers: movers }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'empty') { bd.innerHTML = '<div style="color:#8b949e;padding:24px;text-align:center">今天没有跌超1%的票</div>'; return; }
        _dipPoll();
      })
      .catch(function (e) { bd.innerHTML = '<div style="color:#f85149;padding:20px">请求失败：' + _dgAiEsc('' + e) + '</div>'; });
  }
  function _dipPoll() {
    var ov = document.getElementById('dgAiOverlay');
    if (!ov || ov.style.display === 'none') return;
    fetch('/api/digest/ai_summary/status?scope=dip')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'running') {
          _dipRunning(d.elapsed || 0);
          if (_dipPollTimer) clearTimeout(_dipPollTimer);
          _dipPollTimer = setTimeout(_dipPoll, 1500);
        } else if (d.status === 'done') {
          _dipDone(d);
        } else if (d.status === 'error') {
          var bd = document.getElementById('dgAiBody');
          bd.innerHTML = '<div style="color:#f85149;padding:20px">分析失败：' + _dgAiEsc(d.error || '') + '</div>' + _dipFoot();
          _dipWire();
        } else {
          _dipKick('full');
        }
      })
      .catch(function () { if (_dipPollTimer) clearTimeout(_dipPollTimer); _dipPollTimer = setTimeout(_dipPoll, 2500); });
  }
  function _dipRunning(elapsed) {
    var sub = document.getElementById('dgAiSub'); var bd = document.getElementById('dgAiBody');
    var pct = Math.min(96, Math.round(100 * (1 - Math.exp(-(elapsed || 0) / 8)))); if (pct < 6) pct = 6;
    if (sub) sub.textContent = '正在分析下跌票的抄底机会 · ' + Math.round(elapsed || 0) + 's';
    bd.innerHTML = '<div style="padding:26px 8px;text-align:center"><div style="color:#8b949e;font-size:13px;margin-bottom:12px">🎯 正在用 Haiku 分析哪些下跌票值得抄底…</div><div style="height:8px;border-radius:5px;background:#21262d;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#1a7f37,#3fb950);border-radius:5px;transition:width .6s ease"></div></div><div style="color:#6e7681;font-size:11px;margin-top:10px">可以关掉窗口去干别的，完成后再点开就有了</div></div>';
  }
  function _dipFoot() {
    return '<div style="display:flex;gap:8px;align-items:center;margin-top:14px;padding-top:10px;border-top:1px solid #21262d"><button id="dipFull" style="background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">♻️ 重新分析</button><span style="color:#6e7681;font-size:11px">基于实时异动快照</span></div>';
  }
  function _dipWire() { var b = document.getElementById('dipFull'); if (b) b.onclick = function () { _dipKick('full'); }; }
  function _dipDone(d) {
    var sub = document.getElementById('dgAiSub'); var bd = document.getElementById('dgAiBody');
    if (_dipPollTimer) { clearTimeout(_dipPollTimer); _dipPollTimer = null; }
    var when = (d.ts || '').replace('T', ' ').slice(5, 16);
    if (sub) sub.textContent = '🎯 ' + (d.n || 0) + ' 只下跌票 · ' + _dgAiEsc(d.model || '') + (when ? (' · ' + when) : '');
    bd.innerHTML = _dgAiMd(d.summary || '(空)') + _dipFoot();
    _dipWire();
  }

  // ── 🚀 AI 选股(两个 Haiku 并行 + 四档 tab 切换 + 日历历史) ──────────────
  var _picksPollTimer = null;
  var _picksDateCache = {};
  var _PICKS_TABS = [
    {key:'all', label:'全部', bg:'#6e40c9'},
    {key:'up', label:'🚀 追涨', bg:'#da3633'},
    {key:'caution', label:'⏸️ 回调', bg:'#d29922'},
    {key:'dip', label:'🎯 抄底', bg:'#1a7f37'},
    {key:'down', label:'⚠️ 继续跌', bg:'#8b1a1a'}
  ];
  function _picksSplit(md) {
    // 接受两种 section header 格式: **🚀 / ## 🚀 (Haiku 在 H2 vs 粗体之间会变)。
    // 用 emoji 作 key, 行首允许 #*空白 前缀, 然后必须紧接 emoji。这样:
    //   '**🚀 追涨...'   匹配
    //   '## 🚀 追涨...'  匹配
    //   '# 🎯 ...'      匹配
    //   '- $XYZ ⚠️ ...' 不匹配(非行首位置)
    var marks = [{key:'up',m:'🚀'},{key:'caution',m:'⏸'},{key:'dip',m:'🎯'},{key:'down',m:'⚠'}];
    var pos = [];
    var lines = (md || '').split('\n');
    var off = 0;
    for (var li = 0; li < lines.length; li++) {
      var t = lines[li].replace(/^[#*\s]+/, '');   // 剥掉行首 ##/**/空白
      for (var mi = 0; mi < marks.length; mi++) {
        if (t.indexOf(marks[mi].m) === 0) { pos.push({key: marks[mi].key, idx: off}); break; }
      }
      off += lines[li].length + 1;  // +1 for the consumed '\n'
    }
    pos.sort(function(a,b) { return a.idx - b.idx; });
    var r = {};
    for (var i = 0; i < pos.length; i++) {
      var s = pos[i].idx;
      var e = i + 1 < pos.length ? pos[i+1].idx : md.length;
      var lines = md.substring(s, e).split('\n');
      while (lines.length) { var last = lines[lines.length-1].trim(); if (!last || last === '---' || last.charAt(0) === '>') lines.pop(); else break; }
      r[pos[i].key] = lines.join('\n').trim();
    }
    return r;
  }
  window._picksTab = function(key) {
    _PICKS_TABS.forEach(function(t) {
      var b = document.querySelector('[data-picks-tab="' + t.key + '"]');
      if (!b) return;
      var on = t.key === key;
      b.style.background = on ? t.bg : '#21262d';
      b.style.color = on ? '#fff' : '#8b949e';
      b.style.borderColor = on ? t.bg : '#30363d';
    });
    document.querySelectorAll('[data-picks-sec]').forEach(function(el) {
      el.style.display = (key === 'all' || el.dataset.picksSec === key) ? '' : 'none';
    });
  };
  window._picksLoadDate = function(date) {
    var bd = document.getElementById('dgAiBody');
    var sub = document.getElementById('dgAiSub');
    if (!date) return;
    if (sub) sub.textContent = '🚀 加载 ' + date + ' …';
    bd.innerHTML = '<div style="color:#8b949e;padding:24px;text-align:center">加载中…</div>';
    fetch('/api/movers/picks/history?date=' + encodeURIComponent(date))
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.status === 'done') {
          _picksRender(d, date);
        } else {
          bd.innerHTML = '<div style="color:#8b949e;padding:24px;text-align:center">' + date + ' 没有 AI 选股记录</div>' + _picksFoot(date);
          _picksWire(date);
        }
      })
      .catch(function(e) { bd.innerHTML = '<div style="color:#f85149;padding:20px">加载失败</div>'; });
  };
  function _picksDateBar(activeDate) {
    var dates = _picksDateCache.dates || [];
    if (dates.length <= 1) return '';
    var html = '<select id="picksDateSel" onchange="_picksLoadDate(this.value)" style="background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 8px;font-size:11px;margin-left:auto">';
    dates.forEach(function(dt) {
      var sel = dt === activeDate ? ' selected' : '';
      var label = dt;
      if (dt === dates[0]) label += ' (最新)';
      html += '<option value="' + dt + '"' + sel + '>' + label + '</option>';
    });
    html += '</select>';
    return html;
  }
  // 🚀 旧的轻量版 AI 选股(异动新闻·两个 Haiku·±1% 票)已被 Graph 图墙的全量 5 分类版取代。
  // 入口(window.moverPicks)已删; 下面 _picks* helper 现为死代码(与 🤖AI总结 共用 _dgAi*, 未清以免误伤)。
  window.moverPicks = function () { if (window.hmSetView) window.hmSetView('graph'); };
  function _picksKick(mode) {
    var bd = document.getElementById('dgAiBody');
    if (mode !== 'auto') _picksRunning(0);
    var movers = [];
    try {
      movers = (((window.hmState || {}).moverNews || {}).movers || []).map(function (m) {
        return {
          ticker: m.ticker, change_pct: m.change_pct, last: m.last, sector: m.sector,
          news: (m.news || []).slice(0, 4).map(function (n) { return { title_cn: n.title_cn, title_en: n.title_en, sentiment: n.sentiment }; })
        };
      });
    } catch (e) {}
    fetch('/api/movers/picks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: mode, movers: movers }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'empty') { bd.innerHTML = '<div style="color:#8b949e;padding:24px;text-align:center">今天没有涨跌超1%的异动票</div>'; return; }
        _picksPoll();
      })
      .catch(function (e) { bd.innerHTML = '<div style="color:#f85149;padding:20px">请求失败：' + _dgAiEsc('' + e) + '</div>'; });
  }
  function _picksPoll() {
    var ov = document.getElementById('dgAiOverlay');
    if (!ov || ov.style.display === 'none') return;
    fetch('/api/digest/ai_summary/status?scope=picks')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'running') {
          _picksRunning(d.elapsed || 0);
          if (_picksPollTimer) clearTimeout(_picksPollTimer);
          _picksPollTimer = setTimeout(_picksPoll, 1500);
        } else if (d.status === 'done') {
          _picksRender(d);
        } else if (d.status === 'error') {
          var bd = document.getElementById('dgAiBody');
          bd.innerHTML = '<div style="color:#f85149;padding:20px">分析失败：' + _dgAiEsc(d.error || '') + '</div>' + _picksFoot();
          _picksWire();
        } else {
          _picksKick('full');
        }
      })
      .catch(function () { if (_picksPollTimer) clearTimeout(_picksPollTimer); _picksPollTimer = setTimeout(_picksPoll, 2500); });
  }
  function _picksRunning(elapsed) {
    var sub = document.getElementById('dgAiSub'); var bd = document.getElementById('dgAiBody');
    var pct = Math.min(96, Math.round(100 * (1 - Math.exp(-(elapsed || 0) / 12)))); if (pct < 6) pct = 6;
    if (sub) sub.textContent = '🚀 两个 Haiku 并行分析中 · ' + Math.round(elapsed || 0) + 's';
    bd.innerHTML = '<div style="padding:26px 8px;text-align:center"><div style="color:#8b949e;font-size:13px;margin-bottom:12px">🚀 两个 Haiku 同时跑：追涨 + 抄底分析…</div><div style="height:8px;border-radius:5px;background:#21262d;overflow:hidden"><div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,#da3633,#f85149);border-radius:5px;transition:width .6s ease"></div></div><div style="color:#6e7681;font-size:11px;margin-top:10px">可以关掉窗口去干别的，完成后再点开就有了</div></div>';
  }
  function _picksFoot(activeDate) {
    var today = new Date(); var y = today.getFullYear(); var mo = String(today.getMonth()+1).padStart(2,'0'); var da = String(today.getDate()).padStart(2,'0');
    var todayStr = y + '-' + mo + '-' + da;
    var isToday = !activeDate || activeDate === todayStr;
    var btn = isToday ? '<button id="picksFull" style="background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px">♻️ 重新分析</button>' : '';
    var hint = isToday ? '两个 Haiku 并行 · 基于实时异动' : ('历史记录 · ' + (activeDate || ''));
    return '<div style="display:flex;gap:8px;align-items:center;margin-top:14px;padding-top:10px;border-top:1px solid #21262d">' + btn + '<span style="color:#6e7681;font-size:11px">' + hint + '</span></div>';
  }
  function _picksWire(activeDate) {
    var b = document.getElementById('picksFull');
    if (b) b.onclick = function () { _picksKick('full'); };
  }
  function _picksRender(d, forcedDate) {
    var sub = document.getElementById('dgAiSub'); var bd = document.getElementById('dgAiBody');
    if (_picksPollTimer) { clearTimeout(_picksPollTimer); _picksPollTimer = null; }
    var activeDate = forcedDate || d.date || '';
    var when = (d.ts || '').replace('T', ' ').slice(5, 16);
    var info = '';
    if (d.n_up || d.n_down) info = (d.n_up || 0) + ' 涨 + ' + (d.n_down || 0) + ' 跌';
    else if (d.n) info = d.n + ' 只';
    if (sub) sub.textContent = '🚀 ' + (activeDate ? activeDate + ' · ' : '') + info + ' · ' + _dgAiEsc(d.model || '') + (when ? (' · ' + when) : '');
    var secs = _picksSplit(d.summary || '');
    var tabHtml = '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #21262d;position:sticky;top:0;background:#161b22;z-index:1;padding-top:4px">';
    _PICKS_TABS.forEach(function(t, i) {
      var on = i === 0;
      var has = t.key === 'all' || !!secs[t.key];
      tabHtml += '<button data-picks-tab="' + t.key + '" onclick="_picksTab(this.dataset.picksTab)"' +
        ' style="background:' + (on ? t.bg : '#21262d') + ';color:' + (on ? '#fff' : (has ? '#8b949e' : '#484f58')) +
        ';border:1px solid ' + (on ? t.bg : '#30363d') + ';border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-weight:600' +
        (has ? '' : ';opacity:0.4;pointer-events:none') + '">' + t.label + '</button>';
    });
    tabHtml += _picksDateBar(activeDate);
    tabHtml += '</div>';
    var contentHtml = '';
    ['up','caution','dip','down'].forEach(function(key) {
      if (secs[key]) contentHtml += '<div data-picks-sec="' + key + '">' + _dgAiMd(secs[key]) + '</div>';
    });
    bd.innerHTML = tabHtml + contentHtml + _picksFoot(activeDate);
    _picksWire(activeDate);
  }

  function _mvVisibleNews(news) {
    const showNeutral = window.hmState.moverNewsShowNeutral;
    return (news || []).filter(function (n) {
      if (n.src === 'finviz' && !n.sentiment && !showNeutral) return false;
      return true;
    });
  }

  function _mvDraw() {
    const st = window.hmState;
    const d = st.moverNews || {};
    const list = document.getElementById('hmMvList');
    const status = document.getElementById('hmMvStatus');
    if (!list) return;
    const movers = d.movers || [];
    let rows = movers.map(function (m) { return { m: m, news: _mvVisibleNews(m.news) }; });
    if (st.moverNewsOnlyNews) rows = rows.filter(function (r) { return r.news.length > 0; });
    const withNews = movers.filter(function (m) { return _mvVisibleNews(m.news).length > 0; }).length;
    if (status) {
      const c = d.counts || {};
      var thrLbl = (d.threshold != null ? d.threshold : 1);
      status.textContent = (d.date || '') + ' · ±' + thrLbl + '% 异动 ' + (c.movers != null ? c.movers : movers.length) + ' 只 · 有新闻 ' + withNews + ' 只';
    }
    if (!rows.length) {
      list.innerHTML = '<div style="color:#8b949e;padding:24px;font-size:12px;text-align:center">' + (movers.length ? '当前筛选下没有可显示的票' : '今天暂无 ±1% 异动票(或行情未就绪)') + '</div>';
      return;
    }
    list.innerHTML = rows.map(function (r) { return _mvBlock(r.m, r.news); }).join('');
  }

  function _mvBlock(m, news) {
    const up = (m.change_pct || 0) >= 0;
    const col = up ? '#3fb950' : '#f85149';
    const chg = (up ? '+' : '') + (m.change_pct != null ? Number(m.change_pct).toFixed(2) : '0.00') + '%';
    const head =
      '<div style="display:flex;align-items:center;gap:10px;padding:8px 8px 6px 8px;border-bottom:1px solid #21262d;position:sticky;top:0;background:#010409;z-index:1">'
      + '<span onclick="if(window.openTaTicker)window.openTaTicker(\'' + hmEsc(m.ticker) + '\')" style="font-weight:700;font-size:14px;color:#e6edf3;cursor:pointer" title="看 ' + hmEsc(m.ticker) + ' 单票详情">' + hmEsc(m.ticker) + '</span>'
      + '<button onclick="nbOpen(\'' + hmEsc(m.ticker) + '\')" title="多空叙事" style="background:rgba(163,113,247,.12);color:#a371f7;border:1px solid #30363d;border-radius:4px;padding:1px 7px;font-size:10px;cursor:pointer;font-weight:600;flex:none">⚖️多空</button>'
      + '<span style="font-weight:700;font-size:13px;color:' + col + ';font-variant-numeric:tabular-nums">' + chg + '</span>'
      + '<span style="color:#8b949e;font-size:11px">$' + (m.last != null ? Number(m.last).toFixed(2) : '—') + '</span>'
      + (m.sector ? '<span style="color:#6e7681;font-size:10px">' + hmEsc(m.sector) + '</span>' : '')
      + '</div>';
    const body = news.length ? news.map(function (n) { return _mvNewsRow(m, n); }).join('')
      : '<div style="color:#6e7681;font-size:11px;padding:6px 10px 10px 10px">暂无新闻</div>';
    return '<div style="margin-bottom:14px">' + head + body + '</div>';
  }

  // ── ⚖️ 多空 tab (dataMode='narrative'): 纯 ticker 网格, 点 ticker 内联展开多空,
  //    切换/再点收起。读 /api/narrative(/list); 每天 8点 Haiku 生成, 点🔄手动补单票。
  window.nbState = { data: null, list: null, active: null, listAt: 0, filter: 'all', sort: 'ticker', view: 'grid', recent: [] };
  try {
    var _nbSaved = localStorage.getItem('nbView');
    if (_nbSaved === 'grid' || _nbSaved === 'table') window.nbState.view = _nbSaved;
    var _nbSortSaved = localStorage.getItem('nbSort');
    if (_nbSortSaved) window.nbState.sort = _nbSortSaved;
    var _nbRec = JSON.parse(localStorage.getItem('nbRecent') || '[]');
    if (Array.isArray(_nbRec)) window.nbState.recent = _nbRec.slice(0, 12);
  } catch (e) {}
  if (!window.nbState.recent) window.nbState.recent = [];
  function nbEsc(s) { return (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

  window.nbRenderGrid = function (force) {
    const grid = document.getElementById('nbGrid'); if (!grid) return;
    // sync toolbar 状态与恢复的 localStorage 值
    var _sortSel = document.getElementById('nbSort'); if (_sortSel) _sortSel.value = window.nbState.sort || 'ticker';
    window.nbSetView && window.nbSetView(window.nbState.view || 'grid');
    if (window.nbState.list && !force) { nbPaintGrid(); return; }
    const st = document.getElementById('nbStatus'); if (st) st.textContent = '加载列表…';
    fetch('/api/narrative/list?_t=' + Date.now())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        window.nbState.list = d.tickers || [];
        if (st) st.textContent = (d.built || 0) + ' / ' + (d.count || 0) + ' 只已生成 · ' + (d.date || '');
        nbPaintGrid();
      })
      .catch(function () { if (st) st.textContent = '列表加载失败'; });
  };
  function nbTypeMatch(t, f) {
    if (f === 'all') return true;
    if (f === 'none') return !t.built || (t.nb === 0 && t.ns === 0);
    if (!t.built) return false;
    if (f === 'bull') return t.nb > 0 && t.ns === 0;   // 纯多
    if (f === 'bear') return t.ns > 0 && t.nb === 0;   // 纯空
    if (f === 'mix') return t.nb > 0 && t.ns > 0;      // 多空兼有
    if (f === 'netpos') return (t.nb - t.ns) > 0;      // 多头占优
    if (f === 'netneg') return (t.nb - t.ns) < 0;      // 空头占优
    return true;
  }
  function nbSortList(list, sort) {
    var arr = list.slice();
    var net = function (t) { return (t.nb || 0) - (t.ns || 0); };
    var tot = function (t) { return (t.nb || 0) + (t.ns || 0); };
    if (sort === 'netdesc')   arr.sort(function (a, b) { return net(b) - net(a) || a.ticker.localeCompare(b.ticker); });
    else if (sort === 'netasc')  arr.sort(function (a, b) { return net(a) - net(b) || a.ticker.localeCompare(b.ticker); });
    else if (sort === 'nbdesc')  arr.sort(function (a, b) { return (b.nb || 0) - (a.nb || 0) || a.ticker.localeCompare(b.ticker); });
    else if (sort === 'nsdesc')  arr.sort(function (a, b) { return (b.ns || 0) - (a.ns || 0) || a.ticker.localeCompare(b.ticker); });
    else if (sort === 'totaldesc') arr.sort(function (a, b) { return tot(b) - tot(a) || a.ticker.localeCompare(b.ticker); });
    else arr.sort(function (a, b) { return a.ticker.localeCompare(b.ticker); });
    return arr;
  }
  function nbVisibleList() {
    var q = (document.getElementById('nbInput').value || '').trim().toUpperCase();
    var f = window.nbState.filter || 'all';
    var sort = window.nbState.sort || 'ticker';
    var list = (window.nbState.list || []).filter(function (t) {
      return (!q || t.ticker.indexOf(q) === 0) && nbTypeMatch(t, f);
    });
    return nbSortList(list, sort);
  }
  function nbPaintGrid() {
    var grid = document.getElementById('nbGrid'); if (!grid) return;
    var visible = nbVisibleList();
    var view = window.nbState.view || 'grid';
    if (view === 'table') {
      grid.style.display = 'block';
      grid.innerHTML = _nbRenderTable(visible);
    } else {
      grid.style.display = 'flex';
      grid.innerHTML = visible.map(function (t) {
        var built = t.built;
        var counts = built
          ? ('<span style="color:#3fb950">▲' + t.nb + '</span> <span style="color:#f85149">▼' + t.ns + '</span>')
          : '<span style="color:#484f58">·</span>';
        return '<span data-nb-tk="' + t.ticker + '" data-built="' + (built ? '1' : '0') + '" onclick="nbOpen(\'' + t.ticker + '\')" '
          + 'style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;'
          + 'border:1px solid #30363d;border-radius:5px;padding:3px 8px;font-size:11px;'
          + 'background:' + (built ? '#161b22' : '#0d1117') + ';color:' + (built ? '#e6edf3' : '#6e7681') + '">'
          + '<b style="font-size:11px">' + t.ticker + '</b> ' + counts + '</span>';
      }).join('');
    }
    var q = (document.getElementById('nbInput').value || '').trim().toUpperCase();
    var f = window.nbState.filter || 'all';
    var fl = document.getElementById('nbFilterCount');
    if (fl) fl.textContent = (f === 'all' && !q) ? '' : ('· 显示 ' + visible.length + ' 只');
    if (window.nbState.active) nbHighlightChip(window.nbState.active);
    nbPaintHint(visible.length);   // 筛空且像 ticker → 出「不在票池, 按 Enter 深搜」提示条
    nbRenderRecent();
  }
  function _nbRenderTable(rows) {
    // 密集表格视图: 4 列(Ticker / ▲多 / ▼空 / Net),点行 = nbOpen
    var head = ''
      + '<div style="display:grid;grid-template-columns:1fr 60px 60px 70px 70px;gap:0;padding:6px 10px;'
      + 'background:#161b22;border:1px solid #30363d;border-radius:5px 5px 0 0;'
      + 'color:#8b949e;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.03em">'
      + '<div style="cursor:pointer" onclick="nbSetSort(\'ticker\')">Ticker</div>'
      + '<div style="cursor:pointer;text-align:right" onclick="nbSetSort(\'nbdesc\')"><span style="color:#3fb950">▲多</span></div>'
      + '<div style="cursor:pointer;text-align:right" onclick="nbSetSort(\'nsdesc\')"><span style="color:#f85149">▼空</span></div>'
      + '<div style="cursor:pointer;text-align:right" onclick="nbSetSort(\'netdesc\')">Net(▲-▼)</div>'
      + '<div style="cursor:pointer;text-align:right" onclick="nbSetSort(\'totaldesc\')">总条数</div>'
      + '</div>';
    var body = rows.map(function (t, i) {
      var built = t.built;
      var net = (t.nb || 0) - (t.ns || 0);
      var tot = (t.nb || 0) + (t.ns || 0);
      var netColor = net > 0 ? '#3fb950' : (net < 0 ? '#f85149' : '#8b949e');
      var netSign = net > 0 ? '+' : '';
      var bg = i % 2 ? '#0d1117' : '#161b22';
      var tkColor = built ? '#e6edf3' : '#6e7681';
      return ''
        + '<div data-nb-tk="' + t.ticker + '" data-built="' + (built ? '1' : '0') + '" '
        + 'onclick="nbOpen(\'' + t.ticker + '\')" '
        + 'style="display:grid;grid-template-columns:1fr 60px 60px 70px 70px;gap:0;padding:5px 10px;'
        + 'border-left:1px solid #30363d;border-right:1px solid #30363d;border-bottom:1px solid #21262d;'
        + 'background:' + bg + ';cursor:pointer;font-size:11px">'
        + '<div style="color:' + tkColor + ';font-weight:700">' + t.ticker + '</div>'
        + '<div style="text-align:right;color:#3fb950">' + (built ? t.nb : '·') + '</div>'
        + '<div style="text-align:right;color:#f85149">' + (built ? t.ns : '·') + '</div>'
        + '<div style="text-align:right;color:' + netColor + ';font-weight:700">' + (built ? (netSign + net) : '·') + '</div>'
        + '<div style="text-align:right;color:#8b949e">' + (built ? tot : '·') + '</div>'
        + '</div>';
    }).join('');
    if (!body) body = '<div style="padding:20px;text-align:center;color:#6e7681;border:1px solid #30363d;border-top:none;border-radius:0 0 5px 5px">无符合筛选的 ticker</div>';
    else body += '<div style="height:1px;border:1px solid #30363d;border-top:none;border-radius:0 0 5px 5px"></div>';
    return head + body;
  }
  window.nbFilterGrid = function () { nbPaintGrid(); };
  window.nbSetFilter = function (v) { window.nbState.filter = v || 'all'; nbPaintGrid(); };
  window.nbSetSort = function (v) {
    window.nbState.sort = v || 'ticker';
    try { localStorage.setItem('nbSort', window.nbState.sort); } catch (e) {}
    var sel = document.getElementById('nbSort'); if (sel && sel.value !== v) sel.value = v;
    nbPaintGrid();
  };
  window.nbSetView = function (v) {
    window.nbState.view = (v === 'table') ? 'table' : 'grid';
    try { localStorage.setItem('nbView', window.nbState.view); } catch (e) {}
    var gb = document.getElementById('nbViewGridBtn'), tb = document.getElementById('nbViewTableBtn');
    if (gb && tb) {
      var on = window.nbState.view;
      gb.style.background = on === 'grid' ? '#1f6feb' : '#0d1117';
      gb.style.color = on === 'grid' ? '#fff' : '#8b949e';
      tb.style.background = on === 'table' ? '#1f6feb' : '#0d1117';
      tb.style.color = on === 'table' ? '#fff' : '#8b949e';
    }
    nbPaintGrid();
  };
  function nbHighlightChip(tk) {
    document.querySelectorAll('#nbGrid [data-nb-tk]').forEach(function (el) {
      const on = el.getAttribute('data-nb-tk') === tk;
      el.style.outline = on ? '2px solid #1f6feb' : 'none';
      el.style.background = on ? 'rgba(31,111,235,.18)' : (el.dataset.built === '1' ? '#161b22' : '#0d1117');
    });
  }

  // ── 搜索: 票池外的票也能搜(benzinga 近60天全文 + primary 分层 → Haiku ~15s) ──
  window.nbSearch = function (raw) {
    var tk = (raw || '').trim().toUpperCase().replace(/^\$/, '');
    if (!tk || !/^[A-Z][A-Z.\-]{0,6}$/.test(tk)) return;
    var inUni = (window.nbState.list || []).some(function (x) { return x.ticker === tk; });
    if (!inUni) nbPushRecent(tk);
    nbOpen(tk, { autoBuild: true });     // 先探缓存(GET), 库里没有才烧 Haiku(POST)
  };
  window.nbPushRecent = function (tk) {
    var r = (window.nbState.recent || []).filter(function (x) { return x !== tk; });
    r.unshift(tk);
    window.nbState.recent = r.slice(0, 12);
    try { localStorage.setItem('nbRecent', JSON.stringify(window.nbState.recent)); } catch (e) {}
    nbRenderRecent();
  };
  window.nbClearRecent = function () {
    window.nbState.recent = [];
    try { localStorage.removeItem('nbRecent'); } catch (e) {}
    nbRenderRecent();
  };
  window.nbRenderRecent = function () {
    var el = document.getElementById('nbRecent'); if (!el) return;
    var r = window.nbState.recent || [];
    if (!r.length) { el.style.display = 'none'; return; }
    el.style.display = 'flex';
    el.innerHTML = '<span style="color:#6e7681;font-size:10px;font-weight:600">🔍 最近搜索</span>'
      + r.map(function (t) {
        return '<span onclick="nbOpen(\'' + hmEsc(t) + '\')" title="点开(已缓存, 秒显)" '
          + 'style="background:rgba(163,113,247,.12);color:#a371f7;border:1px solid #30363d;border-radius:10px;'
          + 'padding:1px 9px;font-size:10px;cursor:pointer;font-weight:600">' + hmEsc(t) + '</span>';
      }).join('')
      + '<span onclick="nbClearRecent()" title="清空" style="color:#484f58;font-size:10px;cursor:pointer;margin-left:2px">✕清空</span>';
  };
  // 输入了票池里没有的 ticker → 出提示条(否则网格筛空, 看起来像"不支持搜")
  window.nbPaintHint = function (shown) {
    var el = document.getElementById('nbHint'); if (!el) return;
    var q = ((document.getElementById('nbInput') || {}).value || '').trim().toUpperCase();
    if (!q || shown > 0 || !/^[A-Z][A-Z.\-]{0,6}$/.test(q)) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    el.innerHTML = '<strong style="color:#a371f7">' + hmEsc(q) + '</strong> 不在票池 · '
      + '<button onclick="nbSearch(\'' + hmEsc(q) + '\')" style="background:#a371f7;color:#fff;border:none;'
      + 'border-radius:4px;padding:2px 10px;font-size:10px;cursor:pointer;font-weight:600">🔍 深度搜索</button>'
      + ' <span style="color:#6e7681">读 benzinga 近60天全文, 约30秒</span>';
  };
  // 真正跑深度搜索(POST, ~15s)。nbOpen 探到库里没有时自动调, 提示条按钮也调。
  window.nbRunSearch = function (tk) {
    document.getElementById('nbAsof').textContent = '';
    document.getElementById('nbNet').innerHTML = '';
    document.getElementById('nbBear').innerHTML = '';
    document.getElementById('nbSrc').innerHTML = '';
    document.getElementById('nbBull').innerHTML =
      '<div style="color:#a371f7;font-size:12px;padding:24px 8px;text-align:center">'
      + '🔍 正在深度搜索 <strong>' + hmEsc(tk) + '</strong>…<br>'
      + '<span style="color:#6e7681;font-size:10px">读 benzinga 近60天全文 + 实时 finviz → Haiku 整理, 约30秒</span></div>';
    fetch('/api/narrative/search?ticker=' + encodeURIComponent(tk), { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (d) { window.nbState.data = d; nbRender(); })
      .catch(function () {
        document.getElementById('nbBull').innerHTML =
          '<div style="color:#f85149;font-size:12px;padding:24px 8px;text-align:center">搜索失败(网络)</div>';
      });
  };

  window.nbOpen = function (ticker, opts) {
    ticker = (ticker || '').trim().toUpperCase().replace(/^\$/, '');
    if (!ticker) return;
    window._nbAutoBuild = !!(opts && opts.autoBuild);
    if (window.hmState.dataMode !== 'narrative') hmSetDataMode('narrative');   // 从异动新闻等处点进来 → 切到多空 tab
    const det = document.getElementById('nbDetail');
    // 手风琴: 点已展开的同一只 → 收起
    if (window.nbState.active === ticker && det && det.style.display !== 'none') { nbClose(); return; }
    window.nbState.active = ticker;
    nbHighlightChip(ticker);
    if (window._nbTab !== 'narr') nbSwitchTab('narr');
    document.getElementById('nbSym').textContent = ticker;
    document.getElementById('nbAsof').textContent = '加载中…';
    document.getElementById('nbNet').innerHTML = '';
    document.getElementById('nbBull').innerHTML = '';
    document.getElementById('nbBear').innerHTML = '';
    document.getElementById('nbSrc').innerHTML = '';
    if (det) { det.style.display = 'block'; det.style.flex = '1'; det.style.maxHeight = 'none'; }
    var _nbScr = document.getElementById('nbScroll'); if (_nbScr) _nbScr.style.display = 'none';
    fetch('/api/narrative?ticker=' + encodeURIComponent(ticker) + '&_t=' + Date.now())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        window.nbState.data = d;
        // 显式搜索且库里没有 → 自动深搜。网格里点票的空态**不**自动烧 Haiku, 仍显示「点🔄更新」
        if (d && d.empty && window._nbAutoBuild) { window._nbAutoBuild = false; nbRunSearch(ticker); return; }
        nbRender();
      })
      .catch(function () { document.getElementById('nbAsof').textContent = '加载失败'; });
  };
  window.nbClose = function () {
    const det = document.getElementById('nbDetail');
    if (det) { det.style.display = 'none'; det.style.flex = 'none'; det.style.maxHeight = '56vh'; }
    var _nbScr = document.getElementById('nbScroll'); if (_nbScr) _nbScr.style.display = '';
    window.nbState.active = null;
    nbHighlightChip(null);
  };

  // 🔄 漏跑/刚开机时手动重建当前票(后端 headless 调 Haiku, ~10s)
  window.nbRefresh = function () {
    const d = window.nbState.data;
    const tk = (d && d.ticker) || (document.getElementById('nbSym').textContent || '').trim();
    if (!tk) return;
    const btn = document.getElementById('nbRefreshBtn');
    const old = btn ? btn.textContent : '';
    if (btn) { btn.textContent = '更新中…'; btn.disabled = true; }
    document.getElementById('nbAsof').textContent = '正在用最新新闻重建…';
    fetch('/api/narrative/refresh?ticker=' + encodeURIComponent(tk), { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        if (resp && resp.error && !(resp.bull && resp.bull.length)) {
          document.getElementById('nbAsof').textContent = '更新失败: ' + resp.error;
        } else {
          window.nbState.data = resp; nbRender();
          // 同步网格里该 chip 的计数/已生成态
          const row = (window.nbState.list || []).find(function (x) { return x.ticker === tk; });
          if (row) { row.built = true; row.nb = (resp.bull || []).length; row.ns = (resp.bear || []).length; nbPaintGrid(); }
        }
      })
      .catch(function () { document.getElementById('nbAsof').textContent = '更新失败(网络)'; })
      .finally(function () { if (btn) { btn.textContent = old || '🔄 更新'; btn.disabled = false; } });
  };

  function nbBullets(arr) {
    if (!arr || !arr.length) return '<div style="color:#6e7681;font-size:11px">—</div>';
    return '<ul style="margin:0;padding-left:16px">' + arr.map(function (b) {
      return '<li style="color:#c9d1d9;font-size:12px;line-height:1.55;margin-bottom:6px">' + nbEsc(b) + '</li>';
    }).join('') + '</ul>';
  }
  function nbSrcRows(ev) {
    if (!ev || !ev.length) return '<div style="color:#6e7681;font-size:11px">—</div>';
    return ev.map(function (e) {
      const s = e.sent || 0;
      const dot = s > 0 ? '#3fb950' : (s < 0 ? '#f85149' : '#6e7681');
      const src = e.src === 'finviz_ai' ? '✦AI' : (e.src === 'benzinga' ? 'BZ' : 'finviz');
      const t = nbEsc(e.title_cn || e.title_en || '');
      const link = e.link ? '<a href="' + nbEsc(e.link) + '" target="_blank" rel="noopener" style="color:#6e7681;text-decoration:none;flex:none">↗</a>' : '';
      return '<div style="display:flex;gap:7px;align-items:flex-start;padding:4px 0;border-top:1px solid #161b22;font-size:11px">'
        + '<span style="width:7px;height:7px;border-radius:50%;background:' + dot + ';flex:none;margin-top:4px"></span>'
        + '<span style="color:#6e7681;flex:none;min-width:58px">' + nbEsc(e.date || e.raw || '') + '</span>'
        + '<span style="flex:1;color:#8b949e;line-height:1.45;min-width:0">' + t + ' <span style="color:#484f58">[' + src + ']</span></span>'
        + link + '</div>';
    }).join('');
  }
  // ── 多空面板 · 涨跌条 + K线图 (从 stock_perf + /api/ta/ohlc 拉, 复用 graph 视图 loader) ──
  async function _nbEnsurePerf() {
    if (window.hmState.perfData) return;
    try {
      const r = await fetch('/api/heatmap/stock_perf');
      window.hmState.perfData = await r.json();
    } catch (e) { window.hmState.perfData = { items: [] }; }
  }
  function _nbFmtPct(v) {
    if (v == null || isNaN(v)) return '<span style="color:#484f58">—</span>';
    const c = v > 0 ? '#3fb950' : v < 0 ? '#f85149' : '#8b949e';
    const s = (v > 0 ? '+' : '') + Number(v).toFixed(1) + '%';
    return '<span style="color:' + c + ';font-weight:600">' + s + '</span>';
  }
  async function _nbRenderPerf(ticker) {
    const strip = document.getElementById('nbPerfStrip');
    if (!strip) return;
    strip.innerHTML = '<span style="color:#6e7681">📊 涨跌…</span>';
    await _nbEnsurePerf();
    const items = (window.hmState.perfData || {}).items || [];
    const it = items.find(function (x) { return x.ticker === ticker; }) || {};
    const cells = [['1周', it.r1w], ['1月', it.r1mo], ['3月', it.r3mo], ['1年', it.r1yr]];
    const pxTxt = (it.last != null) ? ('$' + Number(it.last).toFixed(2)) : '';
    strip.innerHTML =
      '<span style="color:#8b949e;font-weight:700">📊 表现</span>'
      + (pxTxt ? '<span style="color:#8b949e">· ' + pxTxt + '</span>' : '')
      + cells.map(function (c) {
        return '<span style="display:inline-flex;align-items:center;gap:5px;padding:2px 9px;background:#161b22;border:1px solid #21262d;border-radius:11px">'
          + '<span style="color:#6e7681">' + c[0] + '</span>' + _nbFmtPct(c[1]) + '</span>';
      }).join('');
  }
  // nb 图上一次切换的时段(独立于 Graph 视图的 hmState.graphDays)
  if (window.nbState.chartDays == null) window.nbState.chartDays = 126;
  function _nbRenderChart(ticker) {
    const cv = document.getElementById('nbChart');
    if (!cv) return;
    cv.dataset.ticker = ticker;
    const days = window.nbState.chartDays || 126;
    _hmLoadGraphCard(cv, days).catch(function () {});
    // 更新时段 chip 高亮 + 右上角显示实际时段
    document.querySelectorAll('#nbChartPeriods button').forEach(function (b) {
      const on = Number(b.dataset.d) === days;
      b.style.background = on ? '#1f6feb' : 'transparent';
      b.style.color = on ? '#fff' : '#8b949e';
      b.style.borderColor = on ? '#1f6feb' : '#30363d';
    });
    // 拉到的首日/末日会在 canvas 底部画; 顶部只显示天数概述
    const lbl = { 5: '近 1 周', 22: '近 1 月', 66: '近 3 月', 126: '近 6 月', 252: '近 1 年', 504: '近 2 年' }[days] || (days + '天');
    const rEl = document.getElementById('nbChartRange');
    if (rEl) rEl.textContent = lbl;
  }
  window.nbSetChartDays = function (days) {
    window.nbState.chartDays = Number(days) || 126;
    const d = window.nbState.data;
    if (d && d.ticker) _nbRenderChart(d.ticker);
  };

  // 是不是票池外的票 → 决定要不要打「🔍搜索」徽章。**前端自己算**而不是读 d.in_universe:
  // POST /search 才带那个字段, GET /api/narrative(点「最近搜索」chip 走的缓存路径)不带 ——
  // 读字段的话徽章会在点回缓存时诡异消失。网格列表还没加载时(length=0)不打徽章。
  function _nbIsOffUniverse(d) {
    var list = window.nbState.list || [];
    if (!d || !d.ticker || !list.length) return false;
    return !list.some(function (x) { return x.ticker === d.ticker; });
  }

  window.nbRender = function () {
    const d = window.nbState.data; if (!d) return;
    document.getElementById('nbSym').textContent = d.ticker || '';
    if (d.ticker) {
      _nbRenderPerf(d.ticker);
      _nbRenderChart(d.ticker);
    }
    if (d.empty || d.error) {
      document.getElementById('nbAsof').textContent = '';
      document.getElementById('nbNet').innerHTML = '';
      document.getElementById('nbBear').innerHTML = '';
      document.getElementById('nbSrc').innerHTML = '';
      var msg;
      if (d.error) {
        msg = '搜索出错: ' + hmEsc(d.error);
      } else if (d.searched) {
        // 真搜过了、benzinga 近60天就是没这只票的新闻 → 别再叫作者点🔄(点了也是一样的路径)
        msg = hmEsc(d.reason || ('近60天没有 ' + (d.ticker || '') + ' 的新闻'))
          + '<br><span style="font-size:10px">冷门票/代号打错了?</span>';
      } else {
        msg = '暂无多空叙事<br><span style="font-size:10px">点右上「🔄 更新」立刻用最新新闻生成(约30秒)</span>';
      }
      document.getElementById('nbBull').innerHTML =
        '<div style="color:#6e7681;font-size:12px;padding:24px 8px;text-align:center">' + msg + '</div>';
      return;
    }
    // ⚠️ 必须用**本地**日期: 后端 store 的 date 是 datetime.date.today()(本地),
    //    而 toISOString() 是 UTC —— 本地 CT 晚 19:00 后 UTC 已跳到明天, 会把当天刚
    //    生成的叙事全判成「⚠️可能过期」, 骗作者白烧一次 30s Haiku。
    const _n = new Date();
    const today = _n.getFullYear() + '-' + String(_n.getMonth() + 1).padStart(2, '0')
      + '-' + String(_n.getDate()).padStart(2, '0');
    const stale = d.date && d.date < today;
    const upd = d.asof ? (' · 更新 ' + String(d.asof).replace('T', ' ').slice(0, 16)) : '';
    document.getElementById('nbAsof').innerHTML = (d.date || '') + upd
      + (stale ? ' <span style="color:#d29922">⚠️可能过期, 点 🔄 更新</span>' : '')
      + (_nbIsOffUniverse(d)
          ? ' <span style="background:rgba(163,113,247,.15);color:#a371f7;border-radius:3px;padding:1px 6px;font-size:9px;font-weight:600">🔍 搜索 · 不在票池</span>'
          : '')
      + ' <button onclick="goToSecFiling(\'' + hmEsc(d.ticker) + '\')" style="margin-left:6px;background:rgba(88,166,255,.1);color:#58a6ff;border:1px solid #30363d;border-radius:4px;padding:1px 8px;cursor:pointer;font-size:10px;font-weight:600">📑 财报</button>';
    document.getElementById('nbNet').innerHTML = d.net ? ('<span style="color:#6e7681">综合：</span>' + nbEsc(d.net)) : '';
    const hBull = '<div style="font-weight:700;font-size:11px;color:#3fb950;letter-spacing:.5px;margin-bottom:8px">▲ 多 · 利好</div>';
    const hBear = '<div style="font-weight:700;font-size:11px;color:#f85149;letter-spacing:.5px;margin-bottom:8px">▼ 空 · 利空</div>';
    document.getElementById('nbBull').innerHTML = hBull + nbBullets(d.bull);
    document.getElementById('nbBear').innerHTML = hBear + nbBullets(d.bear);
    // 来源新闻按窗口(近1周/2周/1月)筛日期
    const win = parseInt((document.getElementById('nbWin') || {}).value, 10) || 30;
    const cut = new Date(Date.now() - win * 86400000).toISOString().slice(0, 10);
    const ev = (d.evidence || []).filter(function (e) { return !e.date || String(e.date) >= cut; });
    document.getElementById('nbSrc').innerHTML = nbSrcRows(ev);
  };

  // ── 跳转 SEC 财报 ──
  window.goToSecFiling = function (ticker) {
    if (typeof switchTopTab === 'function') switchTopTab('events');
    setTimeout(function () {
      if (typeof evNav === 'function') evNav('sec');
      setTimeout(function () {
        var inp = document.getElementById('secTicker');
        if (inp) inp.value = ticker;
        if (typeof secLoad === 'function') secLoad();
      }, 100);
    }, 100);
  };

  // ── 公司简介 tab (10-Q MD&A + Risk Factors 摘要) ──
  window._nbTab = 'narr';
  window._nbProfileCache = {};
  window.nbSwitchTab = function (tab) {
    window._nbTab = tab;
    document.querySelectorAll('#nbTabBar .nb-tab').forEach(function (b) {
      var isActive = b.getAttribute('data-tab') === tab;
      b.style.background = isActive ? '#1f6feb' : '#21262d';
      b.style.color = isActive ? '#fff' : '#8b949e';
      b.style.border = isActive ? 'none' : '1px solid #30363d';
      b.style.borderBottom = 'none';
    });
    document.getElementById('nbNarrContent').style.display = tab === 'narr' ? '' : 'none';
    document.getElementById('nbProfileContent').style.display = tab === 'profile' ? '' : 'none';
    if (tab === 'profile') nbLoadProfile();
    if (tab === 'narr') {
      document.getElementById('nbRefreshBtn').style.display = '';
      document.getElementById('nbAsof').style.display = '';
    } else {
      document.getElementById('nbRefreshBtn').style.display = 'none';
      document.getElementById('nbAsof').style.display = 'none';
    }
  };
  window.nbLoadProfile = function () {
    var tk = window.nbState.active;
    if (!tk) return;
    var body = document.getElementById('nbProfileBody');
    if (window._nbProfileCache[tk]) { nbRenderProfile(window._nbProfileCache[tk]); return; }
    body.innerHTML = '<div style="color:#8b949e;padding:20px;text-align:center">加载公司简介…</div>';
    fetch('/api/company_profile?ticker=' + encodeURIComponent(tk) + '&_t=' + Date.now())
      .then(function (r) { return r.json(); })
      .then(function (d) { window._nbProfileCache[tk] = d; nbRenderProfile(d); })
      .catch(function () { body.innerHTML = '<div style="color:#f85149;padding:20px">加载失败</div>'; });
  };
  window.nbRenderProfile = function (d) {
    var body = document.getElementById('nbProfileBody');
    if (d.empty || d.error) {
      body.innerHTML = '<div style="color:#8b949e;padding:20px;text-align:center">'
        + (d.error ? ('错误: ' + hmEsc(d.error)) : '暂无公司简介')
        + '<br><button onclick="nbRefreshProfile()" style="margin-top:10px;background:#238636;color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:11px">🔄 从 10-Q 生成</button>'
        + '</div>';
      return;
    }
    var summary = hmEsc(d.summary || '').replace(/\n/g, '<br>');
    var tk = window.nbState.active || '';
    var meta = '<div style="color:#6e7681;font-size:10px;margin-bottom:10px">'
      + '📋 ' + hmEsc(d.company || '') + ' · 10-Q ' + hmEsc(d.filing_date || '') + ' · 生成 ' + hmEsc((d.generated_at || '').replace('T', ' ').slice(0, 16))
      + ' <button onclick="nbRefreshProfile()" style="margin-left:8px;background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px">🔄 更新</button>'
      + ' <button onclick="goToSecFiling(\'' + hmEsc(tk) + '\')" style="margin-left:4px;background:rgba(88,166,255,.1);color:#58a6ff;border:1px solid #30363d;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:10px;font-weight:600">📑 财报</button>'
      + '</div>';
    body.innerHTML = meta + '<div style="line-height:1.75">' + summary + '</div>';
  };
  window.nbRefreshProfile = function () {
    var tk = window.nbState.active;
    if (!tk) return;
    var body = document.getElementById('nbProfileBody');
    body.innerHTML = '<div style="color:#8b949e;padding:20px;text-align:center">正在从最新 10-Q 生成(约30秒)…</div>';
    fetch('/api/company_profile/refresh?ticker=' + encodeURIComponent(tk), { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (d) { window._nbProfileCache[tk] = d; nbRenderProfile(d); })
      .catch(function () { body.innerHTML = '<div style="color:#f85149;padding:20px">生成失败</div>'; });
  };

  function _mvNewsRow(m, n) {
    const isAi = n.src === 'finviz_ai';
    const srcCol = isAi ? '#a371f7' : (n.src === 'finviz' ? '#d29922' : '#58a6ff');
    const srcLabel = isAi ? '✦AI' : (n.src === 'finviz' ? 'finviz' : 'digest');
    const clickable = !!n.has_body;
    const titleCn = hmEsc(n.title_cn || '');
    // ✦AI「为何异动」条目: 左紫边 + 淡紫底高亮, 与普通新闻区分
    const accent = isAi
      ? 'border-left:2px solid #a371f7;background:rgba(163,113,247,0.07);'
      : 'border-left:2px solid transparent;';
    const head =
      '<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 8px;border-bottom:1px solid #161b22;' + accent + 'cursor:' + (clickable ? 'pointer' : 'default') + '"'
      + (clickable ? ' onclick="hmMvToggleNews(this,\'' + hmEsc(m.ticker) + '\',\'' + hmEsc(n.id) + '\')"' : '') + '>'
      + _mvSentDot(n.sentiment || 0)
      + '<span style="color:#6e7681;font-size:10px;flex:none;min-width:40px;padding-top:2px">' + hmEsc(n.hhmm || '') + '</span>'
      + '<span style="flex:1;min-width:0">'
      +   '<span style="color:#c9d1d9;font-size:12px;line-height:1.45">' + hmEsc(n.title_en || '') + '</span>'
      +   (titleCn ? '<br><span style="color:#8b949e;font-size:11px;line-height:1.45">' + titleCn + '</span>' : '')
      + '</span>'
      + (isAi && n.catalyst ? _mvChip('催化', '#f0883e') : '')
      + _mvChip(srcLabel, srcCol)
      + (n.link ? '<a href="' + hmEsc(n.link) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="原文" style="color:#6e7681;font-size:12px;text-decoration:none;flex:none">↗</a>' : '')
      + '</div>';
    const bodyAccent = isAi ? 'border-left:2px solid #a371f7;background:rgba(163,113,247,0.04);' : '';
    const bodyDiv = clickable ? '<div data-loaded="0" style="display:none;padding:4px 10px 10px 56px;' + bodyAccent + '"></div>' : '';
    return '<div>' + head + bodyDiv + '</div>';
  }

  window.hmMvToggleNews = function (el, ticker, id) {
    try {
      const body = el.nextElementSibling;
      if (!body) return;
      if (body.style.display !== 'none') { body.style.display = 'none'; return; }
      body.style.display = 'block';
      if (body.dataset.loaded === '1') return;
      const n = _mvFindNews(ticker, id);
      const parasEn = (n && n.paras_en) || [];
      if (!parasEn.length) { body.innerHTML = '<span style="color:#6e7681;font-size:11px">无正文(点 ↗ 看原文)</span>'; body.dataset.loaded = '1'; return; }
      // ✦AI 条目: 正文中文已随响应内联(paras_cn), 直接渲染, 不走 translate_article(无 digest id)
      if (n && n.src === 'finviz_ai') {
        body.innerHTML = _mvParasHtml(parasEn, (n.paras_cn) || []);
        body.dataset.loaded = '1';
        return;
      }
      body.innerHTML = _mvParasHtml(parasEn, []) + '<div style="color:#6e7681;font-size:11px;margin-top:4px">翻译中…</div>';
      const date = (window.hmState.moverNews || {}).date || '';
      fetch('/api/digest/translate_article', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: date, id: id })
      }).then(function (r) { return r.json(); }).then(function (j) {
        body.innerHTML = _mvParasHtml(parasEn, j.body_cn || []);
        body.dataset.loaded = '1';
      }).catch(function () {
        body.innerHTML = _mvParasHtml(parasEn, []);
        body.dataset.loaded = '1';
      });
    } catch (e) {}
  };

  function _mvFindNews(ticker, id) {
    const d = window.hmState.moverNews || {};
    const m = (d.movers || []).find(function (x) { return x.ticker === ticker; });
    if (!m) return null;
    return (m.news || []).find(function (x) { return String(x.id) === String(id); });
  }

  function _mvParasHtml(en, cn) {
    const rows = [];
    for (let i = 0; i < en.length; i++) {
      rows.push('<div style="margin:0 0 7px 0">'
        + '<div style="color:#c9d1d9;font-size:12px;line-height:1.55">' + hmEsc(en[i]) + '</div>'
        + (cn[i] ? '<div style="color:#8b949e;font-size:11px;line-height:1.55;margin-top:2px">' + hmEsc(cn[i]) + '</div>' : '')
        + '</div>');
    }
    return rows.join('');
  }

  // ════════════════════════════════════════════════════════════════════
  // ⏰ 定时弹窗:本地 9/11/14/20 点 自动跳到 Mover News(±1% 异动票 + 双语新闻)
  //   · 服务端 heatmap_popup_loop 触发原生通知 + 置「待弹」状态(kind=movernews)
  //   · 点通知(terminal-notifier -open)→ 打开 /?mvnews=slot 深链
  //   · 已开着的 PWA → 轮询 /api/heatmap/popup_state 自动跳
  //   去重:localStorage 记 已弹过的「日期:slot」,一天每时段只自动弹一次
  // ════════════════════════════════════════════════════════════════════
  function _hmPopupToast(slot) {
    try {
      const M = { s9: '开盘 9:00', s11: '午盘 11:00', s14: '盘尾 14:00', s20: '盘后 20:00', catchup: '今日异动' };
      const M_EN = { s9: 'Open 9:00', s11: 'Midday 11:00', s14: 'Late session 14:00', s20: 'After hours 20:00', catchup: "Today's movers" };
      const lbl = M[slot] || '异动新闻';
      let t = document.getElementById('hmPopupToast');
      if (!t) {
        // bottom centre and pointer-events:none: in the top right it sat on the Market Brief's
        // 🔄 Re-analyze button and swallowed clicks for its 4.5 s on screen
        t = document.createElement('div'); t.id = 'hmPopupToast';
        t.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:99999;background:#1f6feb;color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;font-weight:600;box-shadow:0 6px 22px rgba(0,0,0,.55);max-width:300px;pointer-events:none';
        document.body.appendChild(t);
      }
      t.textContent = zt('📰 ' + lbl + ' — 今日 ±1% 异动票新闻已为你打开',
                         '📰 ' + (M_EN[slot] || 'Movers news') + ': news for tickers that moved ±1% today is open for you');
      t.style.display = 'block'; t.style.transition = ''; t.style.opacity = '1';
      clearTimeout(t._ht);
      t._ht = setTimeout(() => { t.style.transition = 'opacity .6s'; t.style.opacity = '0'; setTimeout(() => { t.style.display = 'none'; }, 700); }, 4500);
    } catch (e) {}
  }

  // 数据异步到位后补渲 graph(切 tab 时 lastData 可能还没拉到)
  function _hmGraphRenderWhenReady(tries) {
    if (!window.hmState || window.hmState.view !== 'graph') return;   // 用户已切走
    const d = window.hmState.lastData;
    if (d && (d.items || []).length) { hmRenderGraph(); return; }
    if (tries > 0) setTimeout(() => _hmGraphRenderWhenReady(tries - 1), 500);
  }

  window.hmOpenCompactPopup = function (slot) {
    try {
      _hmMarkSeenToday();   // 今天看过了 → 当天不再自动补弹
      if (window.switchTopTab) window.switchTopTab('heatmap');   // → hmTabBecameActive 拉数据
      if (window.hmSetDataMode && window.hmState && window.hmState.dataMode !== 'stocks')
        window.hmSetDataMode('stocks');
      if (window.hmSetMcapMin) window.hmSetMcapMin(100);          // compact 大盘≥100B
      if (window.hmSubView) window.hmSubView('kline');           // K线图墙 (sub-tabs follow)
      else if (window.hmSetView) window.hmSetView('graph');
      _hmGraphRenderWhenReady(16);                                // ~8s 内等数据补渲
      _hmPopupToast(slot);
    } catch (e) {}
  };

  function _hmShownIds() { try { return JSON.parse(localStorage.getItem('hmPopupShown') || '[]'); } catch (e) { return []; } }
  function _hmPopupAlreadyShown(id) { return _hmShownIds().indexOf(id) >= 0; }
  function _hmMarkPopupShown(id) { try { const a = _hmShownIds(); if (a.indexOf(id) < 0) { a.push(id); while (a.length > 8) a.shift(); localStorage.setItem('hmPopupShown', JSON.stringify(a)); } } catch (e) {} }

  // ── 「每天必看」补弹:开机/回到电脑/打开 PWA 时,工作日开盘后还没看过 → 自动弹一次 ──
  //   覆盖:电脑关机后重开(server 重启)、睡眠唤醒(PWA tab 重新可见)、单纯打开 PWA。
  //   口径=本地日期 + 本地 9点 + 工作日;hmSeenDate 一天一记,任何来源弹过都算「看过」。
  function _hmLocalDay() { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function _hmSeenToday() { try { return localStorage.getItem('hmSeenDate') === _hmLocalDay(); } catch (e) { return false; } }
  function _hmMarkSeenToday() { try { localStorage.setItem('hmSeenDate', _hmLocalDay()); } catch (e) {} }
  function _hmMaybeDailyCatchup() {
    try {
      const now = new Date(), wd = now.getDay();
      if (wd < 1 || wd > 5) return;          // 仅工作日(周末没盘可看)
      if (now.getHours() < 9) return;         // 本地 9点(开盘)后才弹
      if (_hmSeenToday()) return;             // 今天已看过/弹过
      _hmMarkPopupShown(_hmLocalDay() + ':catchup');   // 与服务端 catchup 通知去重(免双弹)
      window.hmOpenMoverNews('catchup');                // 内部还会标 seen
    } catch (e) {}
  }

  async function _hmPopupPoll() {
    try {
      const r = await (await fetch('/api/heatmap/popup_state')).json();
      if (r && r.due && r.id && !_hmPopupAlreadyShown(r.id)) {
        _hmMarkPopupShown(r.id);
        if ((r.kind || 'movernews') === 'movernews') window.hmOpenMoverNews(r.slot);
        else window.hmOpenCompactPopup(r.slot);
      }
    } catch (e) {}
  }

  // 深链:点通知打开 /?hmpopup=slot(按 当天:slot 去重,刷新不重复弹)
  function _hmCheckDeepLink() {
    try {
      const sp = new URLSearchParams(location.search);
      const pmv = sp.get('mvnews');
      const pg = sp.get('hmpopup');     // 兼容旧深链(开 Graph)
      const p = pmv || pg;
      if (!p) return;
      const id = new Date().toISOString().slice(0, 10) + ':' + p;
      if (_hmPopupAlreadyShown(id)) return;
      _hmMarkPopupShown(id);
      if (pmv) window.hmOpenMoverNews(pmv);
      else window.hmOpenCompactPopup(pg);
    } catch (e) {}
  }

  setTimeout(_hmCheckDeepLink, 1200);     // 等主 PWA 就绪再查深链
  setTimeout(_hmMaybeDailyCatchup, 1700); // 「每天必看」:开页后今天还没看过 → 自动弹
  setTimeout(_hmPopupPoll, 8000);         // 开页 8s 先查一次「待弹」
  setInterval(_hmPopupPoll, 60000);       // 之后每分钟查一次

  // (📍 SR 记录视图整块已删 —— 它挂在 dataMode='sralert' 下, 而数据模式按钮行里
  //  从来没有这一项(作者要求 heatmap 收敛后 SR 记录改在 Graph 卡片行里看)。
  //  SR 告警通知的深链是 /ta/?ticker=(见 sr_alert/message.py deep_link), 不经过
  //  这个视图。后端 sr_alert 包 + /api/sr_alerts/log 全部保留, 只摘掉这条死前端,
  //  顺带干掉它那个每 30s 无条件跑的 setInterval。)

})();
"""
