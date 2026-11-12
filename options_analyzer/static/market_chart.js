/* 📈 大盘图 — SPY/QQQ 全历史日线蜡烛(自绘 canvas, 无外部库)。
 *
 * 交互:拖动/滚轮/触控平移缩放(TradingView 手感)、crosshair 读数;
 * 点某根蜡烛 → 透明弹窗显示那天的 AI 新闻总结(复用页面全局 _dgAiMd 渲染 markdown)。
 *
 * 对外: window.MarketChart = { open, refresh, destroy, setSymbol }
 * 数据: GET /api/digest/market_chart?symbol=SPY  → { bars:[[YYYY-MM-DD,o,h,l,c,v]...], summary_days:[...] }
 * 弹窗: GET /api/digest/ai_summary/status?date=YYYY-MM-DD&scope=  → { status:done, summary, n, model, ts }
 */
(function () {
  'use strict';

  var GREEN = '#26a69a', GREEN_D = '#1f8a7d';
  var RED = '#ef5350', RED_D = '#c0392b';
  var GRID = '#1c2330', AXIS = '#8b949e', BG = '#0d1117';
  var SMA50C = '#e3b341', SMA200C = '#a371f7';
  var MIN_BARS = 20, MAX_PAD = 0.06;

  var S = {
    sym: 'SPY',
    cache: {},            // sym -> { bars, sumSet, pivots }
    bars: [],
    sumSet: null,         // Set of date strings that have an AI summary
    pivots: [],           // ZigZag 转折点 [{i,date,price,kind,move_pct,prom}]
    sma50: [], sma200: [],
    view: { start: 0, count: 120 },   // leftmost bar index (float) + visible bar count (float)
    showSMA: true,
    showVol: true,
    showPivots: true,
    maxPivotsOnScreen: 14,
    canvas: null, ctx: null, wrap: null,
    W: 0, H: 0, dpr: 1,
    plot: { x: 0, y: 0, w: 0, h: 0 },   // price panel rect (css px)
    vol: { x: 0, y: 0, w: 0, h: 0 },    // volume panel rect
    mouse: { x: -1, y: -1, in: false },
    drag: null,           // { x0, startAt, moved }
    ro: null, loading: false, err: '',
    inited: false
  };

  // ── helpers ───────────────────────────────────────────────────────────────
  function fmtVol(v) {
    v = +v || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
    return '' + v;
  }
  function fmtPx(p) { return (+p).toFixed(2); }
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function esc(s) { return (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  function rollingMean(bars, n) {
    var out = new Array(bars.length).fill(null), sum = 0;
    for (var i = 0; i < bars.length; i++) {
      sum += bars[i][4];
      if (i >= n) sum -= bars[i - n][4];
      if (i >= n - 1) out[i] = sum / n;
    }
    return out;
  }

  // ── data ────────────────────────────────────────────────────────────────
  function load(sym, cb, force) {
    if (S.cache[sym] && !force) { cb(S.cache[sym]); return; }
    S.loading = true; S.err = ''; render();
    fetch('/api/digest/market_chart?symbol=' + encodeURIComponent(sym) + (force ? '&refresh=1' : '') + '&_t=' + Date.now())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        S.loading = false;
        if (d.error || !d.bars || !d.bars.length) { S.err = d.error || '无数据'; render(); return; }
        var obj = { bars: d.bars, sumSet: new Set(d.summary_days || []), pivots: d.pivots || [] };
        S.cache[sym] = obj; cb(obj);
      })
      .catch(function (e) { S.loading = false; S.err = '加载失败: ' + e; render(); });
  }

  function useData(obj) {
    S.bars = obj.bars; S.sumSet = obj.sumSet; S.pivots = obj.pivots || [];
    S.sma50 = rollingMean(S.bars, 50);
    S.sma200 = rollingMean(S.bars, 200);
    var n = S.bars.length;
    S.view.count = clamp(120, MIN_BARS, n);
    S.view.start = Math.max(0, n - S.view.count);
    render();
  }

  // ── layout / sizing ───────────────────────────────────────────────────────
  function resize() {
    if (!S.canvas || !S.wrap) return;
    var rect = S.wrap.getBoundingClientRect();
    var cssW = Math.max(320, Math.floor(rect.width));
    var cssH = Math.max(360, Math.floor(rect.height));
    S.dpr = window.devicePixelRatio || 1;
    S.W = cssW; S.H = cssH;
    S.canvas.style.width = cssW + 'px';
    S.canvas.style.height = cssH + 'px';
    S.canvas.width = Math.round(cssW * S.dpr);
    S.canvas.height = Math.round(cssH * S.dpr);
    var padL = 8, padR = 62, padT = 10, padB = 26;
    var volH = S.showVol ? Math.round((cssH - padT - padB) * 0.20) : 0;
    var gap = S.showVol ? 8 : 0;
    S.plot = { x: padL, y: padT, w: cssW - padL - padR, h: (cssH - padT - padB) - volH - gap };
    S.vol = { x: padL, y: padT + S.plot.h + gap, w: cssW - padL - padR, h: volH };
  }

  // ── view math ─────────────────────────────────────────────────────────────
  function barW() { return S.plot.w / S.view.count; }
  function xOfIdx(i) { return S.plot.x + (i - S.view.start + 0.5) * barW(); }
  function idxOfX(x) { return S.view.start + (x - S.plot.x) / barW() - 0.5; }
  function visRange() {
    var lo = Math.max(0, Math.floor(S.view.start) - 1);
    var hi = Math.min(S.bars.length - 1, Math.ceil(S.view.start + S.view.count) + 1);
    return [lo, hi];
  }
  function priceRange() {
    var r = visRange(), min = Infinity, max = -Infinity;
    for (var i = r[0]; i <= r[1]; i++) {
      var b = S.bars[i]; if (!b) continue;
      if (b[3] < min) min = b[3];
      if (b[2] > max) max = b[2];
      if (S.showSMA) {
        if (S.sma50[i] != null) { min = Math.min(min, S.sma50[i]); max = Math.max(max, S.sma50[i]); }
        if (S.sma200[i] != null) { min = Math.min(min, S.sma200[i]); max = Math.max(max, S.sma200[i]); }
      }
    }
    if (!isFinite(min) || !isFinite(max)) { min = 0; max = 1; }
    var pad = (max - min) * MAX_PAD || 1;
    return [min - pad, max + pad];
  }
  function yOfPx(p, pr) { return S.plot.y + (pr[1] - p) / (pr[1] - pr[0]) * S.plot.h; }
  function pxOfY(y, pr) { return pr[1] - (y - S.plot.y) / S.plot.h * (pr[1] - pr[0]); }

  // ── render ──────────────────────────────────────────────────────────────
  function render() {
    if (!S.ctx) return;
    var ctx = S.ctx;
    ctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    ctx.clearRect(0, 0, S.W, S.H);
    ctx.fillStyle = BG; ctx.fillRect(0, 0, S.W, S.H);

    if (S.loading) { center('加载中…', AXIS); return; }
    if (S.err) { center(S.err, RED); return; }
    if (!S.bars.length) { center('无数据', AXIS); return; }

    var pr = priceRange();
    drawGrid(pr);
    drawCandles(pr);
    if (S.showSMA) { drawSMA(S.sma50, SMA50C, pr); drawSMA(S.sma200, SMA200C, pr); }
    if (S.showVol) drawVolume();
    if (S.showPivots) drawPivots(pr);
    drawAxes(pr);
    drawCrosshair(pr);
    drawLegend();
  }

  // 当前可见范围内、按突出度取前 N 的关节点(缩放自适应: 拉远只留大转折, 放大露出小转折)
  function visiblePivots() {
    if (!S.showPivots || !S.pivots.length) return [];
    var r = visRange();
    var inRange = S.pivots.filter(function (p) { return p.i >= r[0] && p.i <= r[1]; });
    inRange.sort(function (a, b) { return (b.prom || 0) - (a.prom || 0); });
    var top = inRange.slice(0, S.maxPivotsOnScreen);
    top.sort(function (a, b) { return a.i - b.i; });   // 画线要按时间序
    return top;
  }

  function drawPivots(pr) {
    var ctx = S.ctx, ps = visiblePivots();
    if (!ps.length) return;
    // 细 zigzag 骨架线(把显示的关节点按时间连起来)
    ctx.save();
    ctx.strokeStyle = 'rgba(139,148,158,0.45)'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    ctx.beginPath();
    for (var i = 0; i < ps.length; i++) {
      var x = xOfIdx(ps[i].i), y = yOfPx(ps[i].price, pr);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke(); ctx.setLineDash([]);
    // 标记 + 标签
    ctx.font = '10px ui-monospace,monospace'; ctx.textBaseline = 'middle';
    for (var k = 0; k < ps.length; k++) {
      var p = ps[k], px = xOfIdx(p.i), py = yOfPx(p.price, pr);
      var high = p.kind === 'high';
      var mcol = high ? RED : GREEN;
      var ty = high ? py - 12 : py + 12;   // top 的标记在蜡烛上方, bottom 在下方
      // 三角标记
      ctx.fillStyle = mcol; ctx.beginPath();
      if (high) { ctx.moveTo(px, ty + 6); ctx.lineTo(px - 5, ty - 2); ctx.lineTo(px + 5, ty - 2); }
      else { ctx.moveTo(px, ty - 6); ctx.lineTo(px - 5, ty + 2); ctx.lineTo(px + 5, ty + 2); }
      ctx.closePath(); ctx.fill();
      // 标签: 日期 + swing%(只在标记够稀疏时画, 避免重叠)
      if (ps.length <= 10) {
        var lbl = (p.date || '').slice(2) + (p.move_pct != null ? ('  ' + (p.move_pct >= 0 ? '+' : '') + Math.round(p.move_pct) + '%') : '');
        ctx.textAlign = 'center';
        var lw = ctx.measureText(lbl).width;
        var lx = Math.max(S.plot.x + lw / 2 + 2, Math.min(px, S.plot.x + S.plot.w - lw / 2 - 2));
        var ly = high ? ty - 12 : ty + 12;
        ctx.fillStyle = 'rgba(13,17,23,0.7)'; ctx.fillRect(lx - lw / 2 - 3, ly - 7, lw + 6, 14);
        ctx.fillStyle = high ? '#ff8079' : '#56d364';
        ctx.fillText(lbl, lx, ly);
      }
    }
    ctx.restore();
  }

  // 命中测试: 屏幕点是否落在某个可见关节点标记附近(±14px)。返回 pivot 或 null。
  function pivotAt(mx, my) {
    if (!S.showPivots) return null;
    var pr = priceRange(), ps = visiblePivots(), best = null, bestD = 22 * 22;
    for (var i = 0; i < ps.length; i++) {
      var p = ps[i], px = xOfIdx(p.i), py = yOfPx(p.price, pr);
      // 命中范围覆盖「三角标记 ↔ 转折那根蜡烛」整段, 作者点转折点附近即可
      var yLo = Math.min(py, p.kind === 'high' ? py - 14 : py + 14);
      var yHi = Math.max(py, p.kind === 'high' ? py - 14 : py + 14);
      var yClamp = my < yLo ? yLo : (my > yHi ? yHi : my);
      var d = (mx - px) * (mx - px) + (my - yClamp) * (my - yClamp);
      if (d < bestD) { bestD = d; best = p; }
    }
    return best;
  }

  function center(txt, color) {
    var ctx = S.ctx; ctx.fillStyle = color; ctx.font = '13px ui-sans-serif,system-ui';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(txt, S.W / 2, S.H / 2); ctx.textAlign = 'left';
  }

  function niceTicks(min, max, n) {
    var span = max - min; if (span <= 0) return [min];
    var raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag, step;
    step = norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1;
    step *= mag;
    var out = [], t = Math.ceil(min / step) * step;
    for (; t <= max; t += step) out.push(t);
    return out;
  }

  function drawGrid(pr) {
    var ctx = S.ctx;
    ctx.strokeStyle = GRID; ctx.lineWidth = 1; ctx.font = '10px ui-monospace,monospace';
    var ticks = niceTicks(pr[0], pr[1], 6);
    ctx.beginPath();
    for (var i = 0; i < ticks.length; i++) {
      var y = Math.round(yOfPx(ticks[i], pr)) + 0.5;
      if (y < S.plot.y - 1 || y > S.plot.y + S.plot.h + 1) continue;
      ctx.moveTo(S.plot.x, y); ctx.lineTo(S.plot.x + S.plot.w, y);
    }
    ctx.stroke();
  }

  function drawCandles(pr) {
    var ctx = S.ctx, r = visRange(), bw = barW();
    var cw = Math.max(1, Math.min(bw * 0.72, 16));
    var thin = bw < 3;
    for (var i = r[0]; i <= r[1]; i++) {
      var b = S.bars[i]; if (!b) continue;
      var x = xOfIdx(i);
      var up = b[4] >= b[1];
      var body = up ? GREEN : RED, edge = up ? GREEN_D : RED_D;
      var yO = yOfPx(b[1], pr), yC = yOfPx(b[4], pr), yH = yOfPx(b[2], pr), yL = yOfPx(b[3], pr);
      // wick
      ctx.strokeStyle = edge; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, yH); ctx.lineTo(Math.round(x) + 0.5, yL); ctx.stroke();
      // body
      if (thin) {
        ctx.strokeStyle = body; ctx.lineWidth = Math.max(1, bw * 0.72);
        ctx.beginPath(); ctx.moveTo(x, yO); ctx.lineTo(x, yC); ctx.stroke();
      } else {
        var top = Math.min(yO, yC), hgt = Math.max(1, Math.abs(yC - yO));
        ctx.fillStyle = body;
        ctx.fillRect(Math.round(x - cw / 2), Math.round(top), Math.round(cw), Math.round(hgt));
      }
    }
  }

  function drawSMA(arr, color, pr) {
    var ctx = S.ctx, r = visRange(), started = false;
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    for (var i = r[0]; i <= r[1]; i++) {
      if (arr[i] == null) { started = false; continue; }
      var x = xOfIdx(i), y = yOfPx(arr[i], pr);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  function drawVolume() {
    var ctx = S.ctx, r = visRange(), bw = barW(), maxV = 0;
    for (var i = r[0]; i <= r[1]; i++) { var b = S.bars[i]; if (b && b[5] > maxV) maxV = b[5]; }
    if (maxV <= 0) return;
    var cw = Math.max(1, Math.min(bw * 0.72, 16));
    for (var j = r[0]; j <= r[1]; j++) {
      var bb = S.bars[j]; if (!bb) continue;
      var x = xOfIdx(j), up = bb[4] >= bb[1];
      var h = (bb[5] / maxV) * S.vol.h;
      ctx.fillStyle = up ? 'rgba(38,166,154,0.45)' : 'rgba(239,83,80,0.45)';
      ctx.fillRect(Math.round(x - cw / 2), Math.round(S.vol.y + S.vol.h - h), Math.round(cw), Math.round(h));
    }
  }

  function drawAxes(pr) {
    var ctx = S.ctx;
    ctx.font = '10px ui-monospace,monospace'; ctx.textBaseline = 'middle';
    // price labels (right)
    ctx.fillStyle = AXIS; ctx.textAlign = 'left';
    var ticks = niceTicks(pr[0], pr[1], 6);
    for (var i = 0; i < ticks.length; i++) {
      var y = yOfPx(ticks[i], pr);
      if (y < S.plot.y - 1 || y > S.plot.y + S.plot.h + 1) continue;
      ctx.fillText(fmtPx(ticks[i]), S.plot.x + S.plot.w + 6, y);
    }
    // date labels (bottom)
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    var r = visRange(), span = S.view.count;
    var every = Math.max(1, Math.round(span / 7));
    var lastLabel = '';
    for (var k = r[0]; k <= r[1]; k++) {
      if (k % every !== 0) continue;
      var b = S.bars[k]; if (!b) continue;
      var x = xOfIdx(k);
      if (x < S.plot.x || x > S.plot.x + S.plot.w) continue;
      var lbl = span > 500 ? b[0].slice(0, 7) : (span > 120 ? b[0].slice(0, 7) : b[0].slice(5));
      if (lbl === lastLabel) continue; lastLabel = lbl;
      ctx.fillStyle = AXIS;
      ctx.fillText(lbl, x, S.H - 22);
    }
  }

  function drawLegend() {
    var ctx = S.ctx;
    var last = S.bars[S.bars.length - 1];
    ctx.font = '11px ui-monospace,monospace'; ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    var t = S.sym + '  ' + (last ? last[0] : '') + '  C ' + (last ? fmtPx(last[4]) : '');
    ctx.fillStyle = '#c9d1d9'; ctx.fillText(t, S.plot.x + 4, S.plot.y + 2);
    if (S.showSMA) {
      ctx.fillStyle = SMA50C; ctx.fillText('— MA50', S.plot.x + 4, S.plot.y + 18);
      ctx.fillStyle = SMA200C; ctx.fillText('— MA200', S.plot.x + 62, S.plot.y + 18);
    }
  }

  function drawCrosshair(pr) {
    if (!S.mouse.in) return;
    var mx = S.mouse.x, my = S.mouse.y, ctx = S.ctx;
    if (mx < S.plot.x || mx > S.plot.x + S.plot.w) return;
    var idx = Math.round(idxOfX(mx));
    idx = clamp(idx, 0, S.bars.length - 1);
    var b = S.bars[idx]; if (!b) return;
    var cx = xOfIdx(idx);
    ctx.save();
    ctx.strokeStyle = '#4b5563'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(Math.round(cx) + 0.5, S.plot.y); ctx.lineTo(Math.round(cx) + 0.5, S.vol.y + S.vol.h); ctx.stroke();
    if (my >= S.plot.y && my <= S.plot.y + S.plot.h) {
      ctx.beginPath(); ctx.moveTo(S.plot.x, Math.round(my) + 0.5); ctx.lineTo(S.plot.x + S.plot.w, Math.round(my) + 0.5); ctx.stroke();
      var pxv = pxOfY(my, pr);
      ctx.setLineDash([]); ctx.fillStyle = '#30363d';
      ctx.fillRect(S.plot.x + S.plot.w, my - 8, 60, 16);
      ctx.fillStyle = '#e6edf3'; ctx.font = '10px ui-monospace,monospace'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(fmtPx(pxv), S.plot.x + S.plot.w + 6, my);
    }
    ctx.restore();
    // OHLC readout box (top-left)
    var up = b[4] >= b[1], chg = b[4] - b[1], chgp = b[1] ? (chg / b[1] * 100) : 0;
    var has = !S.sumSet || S.sumSet.has(b[0]);
    var lines = [
      b[0] + (has ? '   📰 点击看新闻总结' : ''),
      'O ' + fmtPx(b[1]) + '  H ' + fmtPx(b[2]) + '  L ' + fmtPx(b[3]) + '  C ' + fmtPx(b[4]),
      (up ? '▲ ' : '▼ ') + (chg >= 0 ? '+' : '') + fmtPx(chg) + ' (' + (chgp >= 0 ? '+' : '') + chgp.toFixed(2) + '%)   Vol ' + fmtVol(b[5])
    ];
    ctx.font = '11px ui-monospace,monospace'; ctx.textBaseline = 'top'; ctx.textAlign = 'left';
    var bw2 = 0;
    for (var i = 0; i < lines.length; i++) bw2 = Math.max(bw2, ctx.measureText(lines[i]).width);
    var bx = S.plot.x + 4, by = S.plot.y + 34;
    ctx.fillStyle = 'rgba(13,17,23,0.82)'; ctx.fillRect(bx, by, bw2 + 14, 50);
    ctx.strokeStyle = '#30363d'; ctx.strokeRect(bx + 0.5, by + 0.5, bw2 + 14, 50);
    ctx.fillStyle = '#9cc3ff'; ctx.fillText(lines[0], bx + 7, by + 5);
    ctx.fillStyle = '#c9d1d9'; ctx.fillText(lines[1], bx + 7, by + 19);
    ctx.fillStyle = up ? GREEN : RED; ctx.fillText(lines[2], bx + 7, by + 34);
  }

  // ── interaction ─────────────────────────────────────────────────────────
  function onWheel(e) {
    e.preventDefault();
    var rect = S.canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    // 横向手势(trackpad 两指左右)→ 平移;纵向 → 缩放
    if (Math.abs(e.deltaX) > Math.abs(e.deltaY) && !e.ctrlKey) {
      S.view.start += e.deltaX / barW();
      clampView();
    } else {
      var anchorIdx = idxOfX(mx);
      var factor = (e.deltaY > 0 ? 1.12 : 1 / 1.12);
      var n = S.bars.length;
      S.view.count = clamp(S.view.count * factor, MIN_BARS, n);
      // 保持光标下的 bar 不动
      S.view.start = anchorIdx - (mx - S.plot.x) / barW() + 0.5;
      clampView();
    }
    render();
  }

  function clampView() {
    var n = S.bars.length;
    S.view.count = clamp(S.view.count, MIN_BARS, n);
    S.view.start = clamp(S.view.start, 0, Math.max(0, n - S.view.count));
  }

  // mousedown 只在 canvas 上,拿到 canvas 相对坐标后开始 drag 记录。
  // 拖动/抬起用 window,并在 onDown 里挂、onUp 里摘 —— 否则 tab 切走后 window
  // listener 还留着,任何页面上的 click 都会跑 onUp 里的落点判定(canvas 隐藏时
  // getBoundingClientRect 是 0/0/0/0,e.clientX/Y 直接落进过期的 S.plot),开出
  // 张冠李戴的历史 candle 弹窗(比如点日历 29 弹出 2022-08-16 的 SPY 总结)。
  function onDown(e) {
    var rect = S.canvas.getBoundingClientRect();
    S.drag = { x0: e.clientX - rect.left, startAt: S.view.start, moved: 0 };
    window.addEventListener('mousemove', onDragMove);
    window.addEventListener('mouseup', onUp);
  }
  function onDragMove(e) {
    if (!S.drag) return;
    var rect = S.canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var dx = mx - S.drag.x0;
    S.drag.moved = Math.max(S.drag.moved, Math.abs(dx));
    S.view.start = S.drag.startAt - dx / barW();
    clampView();
    S.canvas.style.cursor = 'grabbing';
    render();
  }
  // canvas 内 hover: 只在 canvas 上跑,画 crosshair + OHLC 读数框。
  function onHover(e) {
    var rect = S.canvas.getBoundingClientRect();
    S.mouse.x = e.clientX - rect.left;
    S.mouse.y = e.clientY - rect.top;
    S.mouse.in = true;
    if (!S.drag) S.canvas.style.cursor = 'crosshair';
    render();
  }
  function onUp(e) {
    window.removeEventListener('mousemove', onDragMove);
    window.removeEventListener('mouseup', onUp);
    var wasDrag = S.drag && S.drag.moved > 4;
    S.drag = null;
    S.canvas.style.cursor = 'crosshair';
    if (!wasDrag) {
      var rect = S.canvas.getBoundingClientRect();
      var mx = e.clientX - rect.left, my = e.clientY - rect.top;
      var piv = pivotAt(mx, my);
      if (piv) { openPivotPopup(piv); }
      else if (mx >= S.plot.x && mx <= S.plot.x + S.plot.w && my >= S.plot.y && my <= S.vol.y + S.vol.h) {
        var idx = clamp(Math.round(idxOfX(mx)), 0, S.bars.length - 1);
        var b = S.bars[idx];
        if (b) openPopup(b);
      }
    }
    render();
  }
  function onLeave() { S.mouse.in = false; render(); }

  // 触控:单指平移 / 双指捏合缩放
  var _touch = null;
  function tDist(t) { var dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY; return Math.hypot(dx, dy); }
  function onTouchStart(e) {
    var rect = S.canvas.getBoundingClientRect();
    if (e.touches.length === 1) {
      _touch = { mode: 'pan', x0: e.touches[0].clientX - rect.left, startAt: S.view.start, moved: 0, downX: e.touches[0].clientX - rect.left, downY: e.touches[0].clientY - rect.top };
    } else if (e.touches.length === 2) {
      _touch = { mode: 'pinch', d0: tDist(e.touches), count0: S.view.count, cx: (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left, anchor: idxOfX((e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left) };
    }
  }
  function onTouchMove(e) {
    if (!_touch) return;
    e.preventDefault();
    var rect = S.canvas.getBoundingClientRect();
    if (_touch.mode === 'pan' && e.touches.length === 1) {
      var mx = e.touches[0].clientX - rect.left;
      _touch.moved = Math.max(_touch.moved, Math.abs(mx - _touch.x0));
      S.view.start = _touch.startAt - (mx - _touch.x0) / barW();
      clampView(); render();
    } else if (_touch.mode === 'pinch' && e.touches.length === 2) {
      var d = tDist(e.touches), n = S.bars.length;
      S.view.count = clamp(_touch.count0 * (_touch.d0 / Math.max(1, d)), MIN_BARS, n);
      S.view.start = _touch.anchor - (_touch.cx - S.plot.x) / barW() + 0.5;
      clampView(); render();
    }
  }
  function onTouchEnd(e) {
    if (_touch && _touch.mode === 'pan' && _touch.moved <= 5) {
      var piv = pivotAt(_touch.downX, _touch.downY);
      if (piv) { openPivotPopup(piv); }
      else {
        var b = S.bars[clamp(Math.round(idxOfX(_touch.downX)), 0, S.bars.length - 1)];
        if (b && _touch.downX >= S.plot.x && _touch.downX <= S.plot.x + S.plot.w) openPopup(b);
      }
    }
    _touch = null;
  }

  // ── popup (透明弹窗) ───────────────────────────────────────────────────────
  function ensurePopup() {
    var ov = document.getElementById('mcPopOverlay');
    if (ov) return ov;
    ov = document.createElement('div');
    ov.id = 'mcPopOverlay';
    ov.style.cssText = 'display:none;position:fixed;inset:0;z-index:100001;background:rgba(0,0,0,0.35);align-items:center;justify-content:center;backdrop-filter:blur(1.5px)';
    ov.onclick = function (e) { if (e.target === ov) closePopup(); };
    ov.innerHTML =
      '<div id="mcPopCard" style="background:rgba(22,27,34,0.86);border:1px solid #30363d;border-radius:14px;max-width:720px;width:90%;max-height:82vh;display:flex;flex-direction:column;box-shadow:0 16px 50px rgba(0,0,0,.65);backdrop-filter:blur(9px)">'
      + '<div style="display:flex;align-items:center;gap:10px;padding:13px 18px;border-bottom:1px solid rgba(48,54,61,0.7)">'
      + '<span id="mcPopTitle" style="font-size:15px;font-weight:700;color:#e6edf3"></span>'
      + '<span id="mcPopSub" style="font-size:11px;color:#8b949e"></span>'
      + '<button onclick="MarketChart._closePopup()" style="margin-left:auto;background:none;border:none;color:#8b949e;font-size:24px;cursor:pointer;line-height:1;padding:0 4px">&times;</button>'
      + '</div>'
      + '<div id="mcPopBody" style="padding:15px 20px;overflow-y:auto;font-size:13px"></div>'
      + '</div>';
    document.body.appendChild(ov);
    return ov;
  }
  function closePopup() { var ov = document.getElementById('mcPopOverlay'); if (ov) ov.style.display = 'none'; if (_pivPoll) { clearTimeout(_pivPoll); _pivPoll = null; } }

  function openPopup(bar) {
    var date = bar[0];
    var ov = ensurePopup();
    var up = bar[4] >= bar[1], chg = bar[4] - bar[1], chgp = bar[1] ? chg / bar[1] * 100 : 0;
    var col = up ? '#3fb950' : '#f85149';
    document.getElementById('mcPopTitle').innerHTML = '📅 ' + esc(date) + ' · ' + esc(S.sym)
      + ' <span style="color:' + col + ';font-weight:600;font-size:13px">' + fmtPx(bar[4])
      + ' (' + (chgp >= 0 ? '+' : '') + chgp.toFixed(2) + '%)</span>';
    document.getElementById('mcPopSub').textContent = 'O ' + fmtPx(bar[1]) + ' H ' + fmtPx(bar[2]) + ' L ' + fmtPx(bar[3]) + ' · Vol ' + fmtVol(bar[5]);
    var body = document.getElementById('mcPopBody');
    body.innerHTML = '<div style="color:#8b949e;padding:26px;text-align:center">读取当天 AI 新闻总结…</div>';
    ov.style.display = 'flex';
    var mdFn = (typeof window._dgAiMd === 'function') ? window._dgAiMd : fallbackMd;
    fetch('/api/digest/ai_summary/status?date=' + encodeURIComponent(date) + '&scope=&_t=' + Date.now())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.status === 'done' && d.summary) {
          var when = d.ts ? (' · ' + esc(d.ts.replace('T', ' '))) : '';
          document.getElementById('mcPopSub').textContent += '  ·  ' + (d.n || 0) + ' 条新闻' + when;
          body.innerHTML = mdFn(d.summary);
        } else if (d && d.status === 'running') {
          body.innerHTML = '<div style="color:#e3b341;padding:26px;text-align:center">这天的总结正在后台生成，稍后再点。</div>';
        } else {
          body.innerHTML = '<div style="color:#8b949e;padding:26px;text-align:center">这天没有缓存的新闻总结。<br><span style="font-size:11px">(周末/休市或当天清单内无新闻)</span></div>';
        }
      })
      .catch(function (e) {
        body.innerHTML = '<div style="color:#f85149;padding:26px;text-align:center">读取失败：' + esc('' + e) + '</div>';
      });
  }
  function fallbackMd(md) {
    return '<div style="white-space:pre-wrap;color:#c9d1d9;line-height:1.6">' + esc(md) + '</div>';
  }

  // 📍 关节点转折归因弹窗:POST 起后台归因任务 → 轮询进度 → 渲染「是什么消息导致这个转折」
  var _pivPoll = null;
  function openPivotPopup(p) {
    if (_pivPoll) { clearTimeout(_pivPoll); _pivPoll = null; }
    var ov = ensurePopup();
    var high = p.kind === 'high';
    var kindTxt = high ? '见顶 · 涨转跌' : '见底 · 跌转涨';
    var col = high ? '#f85149' : '#3fb950';
    var mv = (p.move_pct != null) ? ((p.move_pct >= 0 ? '+' : '') + Math.round(p.move_pct) + '%') : '';
    document.getElementById('mcPopTitle').innerHTML = '📍 <span style="color:#a371f7;font-weight:700">关节点</span> · ' + esc(S.sym) + ' · ' + esc(p.date)
      + ' <span style="color:' + col + ';font-weight:600;font-size:13px">' + kindTxt + '</span>';
    document.getElementById('mcPopSub').textContent = '大盘历史转折点 · 非今日新闻总结 · ' + (high ? '此前涨幅 ' : '此前跌幅 ') + mv + ' · 收 ' + fmtPx(p.price) + ' · 分析导致转折的新闻催化…';
    var body = document.getElementById('mcPopBody');
    var mdFn = (typeof window._dgAiMd === 'function') ? window._dgAiMd : fallbackMd;
    ov.style.display = 'flex';

    function paint(pct, note) {
      body.innerHTML = '<div style="padding:24px 8px">'
        + '<div style="color:#8b949e;font-size:12px;margin-bottom:10px;text-align:center">' + esc(note || '') + '</div>'
        + '<div style="height:6px;background:#21262d;border-radius:3px;overflow:hidden">'
        + '<div style="height:100%;width:' + Math.round(pct * 100) + '%;background:linear-gradient(90deg,#6e40c9,#a371f7);transition:width .4s"></div></div>'
        + '<div style="color:#6e7681;font-size:11px;margin-top:8px;text-align:center">Haiku 读取转折点前后几天的新闻摘要中，可关窗口后台继续</div></div>';
    }
    paint(0.08, '正在分析这个转折点的新闻催化…');

    var t0 = Date.now();
    function poll() {
      var q = '?symbol=' + encodeURIComponent(S.sym) + '&date=' + encodeURIComponent(p.date) + '&kind=' + encodeURIComponent(p.kind) + '&_t=' + Date.now();
      fetch('/api/digest/pivot_summary/status' + q)
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (ov.style.display === 'none') return;      // 用户已关
          if (d.status === 'done' && d.summary) {
            var sub = document.getElementById('mcPopSub');
            if (sub) sub.textContent = '大盘历史转折点 · 非今日新闻总结 · ' + (high ? '此前涨幅 ' : '此前跌幅 ') + mv + ' · 收 ' + fmtPx(p.price) + '  ·  据 ' + (d.n_days || 0) + ' 天摘要归因';
            body.innerHTML = mdFn(d.summary);
          } else if (d.status === 'error') {
            body.innerHTML = '<div style="color:#f85149;padding:24px;text-align:center">归因失败：' + esc(d.error || '') + '</div>';
          } else {
            var el = (Date.now() - t0) / 1000;
            paint(1 - Math.exp(-el / 10), '正在分析这个转折点的新闻催化…（' + Math.round(el) + 's）');
            _pivPoll = setTimeout(poll, 1500);
          }
        })
        .catch(function () { _pivPoll = setTimeout(poll, 2000); });
    }
    fetch('/api/digest/pivot_summary', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: S.sym, date: p.date, kind: p.kind, move_pct: p.move_pct })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.status === 'done') poll();      // 已有缓存 → 立即取
      else poll();
    }).catch(function () { poll(); });
  }

  function onKey(e) { if (e.key === 'Escape') closePopup(); }

  // ── public ────────────────────────────────────────────────────────────────
  function bind() {
    if (S.inited) return; S.inited = true;
    S.canvas.addEventListener('wheel', onWheel, { passive: false });
    S.canvas.addEventListener('mousedown', onDown);
    S.canvas.addEventListener('mousemove', onHover);
    S.canvas.addEventListener('mouseleave', onLeave);
    S.canvas.addEventListener('touchstart', onTouchStart, { passive: true });
    S.canvas.addEventListener('touchmove', onTouchMove, { passive: false });
    S.canvas.addEventListener('touchend', onTouchEnd);
    window.addEventListener('keydown', onKey);
    S.ro = new ResizeObserver(function () { resize(); render(); });
    S.ro.observe(S.wrap);
  }

  function open(opts) {
    opts = opts || {};
    S.wrap = document.getElementById(opts.wrapId || 'mcChartWrap');
    S.canvas = document.getElementById(opts.canvasId || 'mcChartCanvas');
    if (!S.wrap || !S.canvas) return;
    S.ctx = S.canvas.getContext('2d');
    S.canvas.style.cursor = 'crosshair';
    bind();
    resize();
    load(S.sym, function (obj) { useData(obj); });
    syncSymBtns();
  }

  function setSymbol(sym) {
    sym = (sym || 'SPY').toUpperCase();
    if (sym === S.sym && S.bars.length) return;
    S.sym = sym; syncSymBtns();
    load(sym, function (obj) { useData(obj); });
  }
  function syncSymBtns() {
    document.querySelectorAll('#mcSymBar .mc-sym').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-sym') === S.sym);
    });
  }
  function toggleSMA() { S.showSMA = !S.showSMA; syncToggleBtns(); render(); }
  function toggleVol() { S.showVol = !S.showVol; resize(); syncToggleBtns(); render(); }
  function togglePivots() { S.showPivots = !S.showPivots; syncToggleBtns(); render(); }
  function resetZoom() { var n = S.bars.length; S.view.count = clamp(120, MIN_BARS, n); S.view.start = Math.max(0, n - S.view.count); render(); }
  function fitAll() { S.view.count = S.bars.length; S.view.start = 0; render(); }
  function syncToggleBtns() {
    var a = document.getElementById('mcSMABtn'); if (a) a.classList.toggle('active', S.showSMA);
    var b = document.getElementById('mcVolBtn'); if (b) b.classList.toggle('active', S.showVol);
    var c = document.getElementById('mcPivBtn'); if (c) c.classList.toggle('active', S.showPivots);
  }
  function refresh() { load(S.sym, function (obj) { useData(obj); }, true); }
  function relayout() { if (!S.ctx) return; resize(); render(); }   // 面板刚 display 后重新量尺寸

  window.MarketChart = {
    open: open, relayout: relayout, setSymbol: setSymbol, refresh: refresh,
    toggleSMA: toggleSMA, toggleVol: toggleVol, togglePivots: togglePivots,
    resetZoom: resetZoom, fitAll: fitAll, _closePopup: closePopup,
    _pivotHits: function () {
      var pr = priceRange();
      return visiblePivots().map(function (p) {
        return { date: p.date, kind: p.kind, move_pct: p.move_pct,
                 x: Math.round(xOfIdx(p.i)),
                 y: Math.round(yOfPx(p.price, pr) + (p.kind === 'high' ? -12 : 12)) };
      });
    }
  };
})();
