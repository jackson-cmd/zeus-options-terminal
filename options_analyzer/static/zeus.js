/* ZEUS Terminal shell: mode badge, settings overlay, three interface languages
 * (中文 / 双语 bilingual / English), manual-mode onboarding.
 * Loaded after the original app scripts; touches nothing unless needed. */
(function () {
  'use strict';
  if (window.__zeusLoaded) return;
  window.__zeusLoaded = true;

  // ------------------------------------------------------------------ language: zh | bi | en
  var LANGS = ['zh', 'bi', 'en'];
  function pickLang(serverPref) {
    try {
      var s = localStorage.getItem('zeusLang');
      if (LANGS.indexOf(s) >= 0) return s;
    } catch (e) {}
    if (LANGS.indexOf(serverPref) >= 0) return serverPref;
    return (navigator.language || '').toLowerCase().indexOf('zh') === 0 ? 'zh' : 'en';
  }
  var LANG = pickLang();
  window.zeusLang = function () { return LANG; };
  var L = function (zh, en) {
    if (LANG === 'en') return en;
    if (LANG === 'bi') return zh + ' · ' + en;
    return zh;
  };
  function setLangClass() {
    var h = document.documentElement;
    LANGS.forEach(function (l) { h.classList.toggle('zs-lang-' + l, l === LANG); });
    h.lang = LANG === 'en' ? 'en' : 'zh';
  }

  // ------------------------------------------------------------------ dictionary (zh → en)
  var DICT = null, RUN_RE = /[　-〿一-鿿！-～][　-〿一-鿿！-～··]*/g;
  var HAS_CJK = /[一-鿿]/;
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1, NOSCRIPT: 1 };
  var ATTRS = ['title', 'placeholder', 'aria-label', 'data-tip'];
  var BI_SEL = 'button, .top-nav-tab, th, h1, h2, h3, h4, h5, label, legend, summary, .ic-title, .summary-label, .sidebar-title, [role=tab], [onclick]:not(tr):not(td):not(div), .df-chip, .df-pill';
  var BI_MARK = '\n— ';

  function runEn(run) {
    if (DICT[run]) return DICT[run];
    var t = run.replace(/[\s　]+$/, '');
    return DICT[t] ? DICT[t] + run.slice(t.length) : null;
  }
  function trString(s) {
    if (!s || !HAS_CJK.test(s)) return s;
    var key = s.trim();
    if (DICT[key]) return s.replace(key, DICT[key]);
    return s.replace(RUN_RE, function (run) { var r = runEn(run); return r == null ? run : r; })
      .replace(/([A-Za-z0-9%)\]])([A-Z][a-z])/g, '$1 $2');
  }
  function skip(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentNode) {
      if (SKIP_TAGS[n.tagName] || (n.hasAttribute && n.hasAttribute('data-no-i18n')) || n.isContentEditable) return true;
      // Chinese translations of news are content, not interface text
      if (n.classList && (n.classList.contains('ttl-cn') || n.classList.contains('dg-cn') || n.classList.contains('dg-zh'))) return true;
    }
    return false;
  }

  // ---- English: translate every interface string in place
  function enNode(node) {
    if (node.nodeType === 3) {
      var v = node.nodeValue;
      if (!v || !HAS_CJK.test(v)) return;
      var p = node.parentNode;
      if (!p || skip(p)) return;
      var out = trString(v);
      if (out !== v) node.nodeValue = out;
      return;
    }
    if (node.nodeType !== 1 || SKIP_TAGS[node.tagName] || skip(node)) return;
    var all = [node].concat([].slice.call(node.querySelectorAll ? node.querySelectorAll('*') : []));
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (SKIP_TAGS[el.tagName]) continue;
      for (var j = 0; j < ATTRS.length; j++) {
        var a = el.getAttribute(ATTRS[j]);
        if (a && HAS_CJK.test(a) && !skip(el)) el.setAttribute(ATTRS[j], trString(a));
      }
      if (el.tagName === 'INPUT' && (el.type === 'button' || el.type === 'submit') && HAS_CJK.test(el.value)) el.value = trString(el.value);
    }
    var w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, null), n;
    while ((n = w.nextNode())) enNode(n);
  }

  // ---- Bilingual: keep Chinese, add English beside labels and inside tooltips
  function biLabel(el) {
    if (!el || el.nodeType !== 1 || skip(el)) return;
    var txt = (el.textContent || '').trim();
    var en = '';
    if (txt && HAS_CJK.test(txt) && txt.length <= 26) {
      var runs = txt.match(RUN_RE) || [];
      if (runs.length && runs.length <= 3) {
        var parts = runs.map(runEn);
        if (parts.every(function (x) { return x; })) en = parts.join(' ').replace(/\s+/g, ' ').trim();
      }
    }
    if (en && !HAS_CJK.test(en)) { if (el.getAttribute('data-zs-en') !== en) el.setAttribute('data-zs-en', en); }
    else if (el.hasAttribute('data-zs-en')) el.removeAttribute('data-zs-en');
  }
  function biTitle(el) {
    var t = el.getAttribute && el.getAttribute('title');
    if (!t || !HAS_CJK.test(t) || t.indexOf(BI_MARK) >= 0 || skip(el)) return;
    var en = trString(t);
    if (en && en !== t && !HAS_CJK.test(en)) el.setAttribute('title', t + BI_MARK + en);
  }
  function biOption(el) {
    if (el.getAttribute('data-zs-bi') || skip(el)) return;
    var t = el.textContent;
    if (!t || !HAS_CJK.test(t)) return;
    var en = trString(t);
    if (en && en !== t && !HAS_CJK.test(en)) { el.setAttribute('data-zs-bi', '1'); el.textContent = t + ' · ' + en.trim(); }
  }
  function biNode(node) {
    var el = node.nodeType === 3 ? node.parentNode : node;
    if (!el || el.nodeType !== 1 || SKIP_TAGS[el.tagName]) return;
    var host = el.closest ? el.closest(BI_SEL) : null;
    if (host) biLabel(host);
    if (node.nodeType === 3) return;
    if (el.matches && el.matches('[title]')) biTitle(el);
    if (el.tagName === 'OPTION') biOption(el);
    if (!el.querySelectorAll) return;
    var labels = el.querySelectorAll(BI_SEL);
    for (var i = 0; i < labels.length; i++) biLabel(labels[i]);
    var titled = el.querySelectorAll('[title]');
    for (var j = 0; j < titled.length; j++) biTitle(titled[j]);
    var opts = el.querySelectorAll('option');
    for (var k = 0; k < opts.length; k++) biOption(opts[k]);
  }

  var pending = [], scheduled = false, handler = null;
  function flush() {
    scheduled = false;
    var list = pending; pending = [];
    for (var i = 0; i < list.length; i++) { try { handler(list[i]); } catch (e) {} }
  }
  function queue(n) {
    pending.push(n);
    if (scheduled) return;
    scheduled = true;
    // A frame callback translates before the text is painted, but frames stop in a window the
    // browser is not drawing (covered or minimized) while it still counts as visible; the timer
    // makes sure the text gets translated anyway.
    var timer = setTimeout(flush, 50);
    if (document.visibilityState === 'visible' && window.requestAnimationFrame)
      requestAnimationFrame(function () { clearTimeout(timer); flush(); });
  }
  function startI18n() {
    if (LANG === 'zh') return;
    fetch('/api/zeus/i18n/en').then(function (r) { return r.json(); }).then(function (d) {
      DICT = d || {};
      handler = LANG === 'en' ? enNode : biNode;
      if (LANG === 'en') document.title = 'ZEUS Terminal · Self-hosted Options Trading Terminal';
      handler(document.body);
      new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          var m = muts[i];
          if (m.type === 'characterData' || m.type === 'attributes') queue(m.target);
          else for (var j = 0; j < m.addedNodes.length; j++) queue(m.addedNodes[j]);
        }
      }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
      if (LANG === 'en') {
        ['alert', 'confirm', 'prompt'].forEach(function (fn) {
          var orig = window[fn];
          window[fn] = function (msg) { var a = [].slice.call(arguments); a[0] = typeof msg === 'string' ? trString(msg) : msg; return orig.apply(window, a); };
        });
      }
    }).catch(function () {});
  }

  // ------------------------------------------------------------------ settings overlay
  function openSettings(tab) {
    var ov = document.getElementById('zsSettingsOverlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'zsSettingsOverlay'; ov.setAttribute('data-no-i18n', '');
      ov.innerHTML = '<div class="zs-ov-box"><iframe id="zsSettingsFrame" title="Settings"></iframe></div>';
      ov.addEventListener('click', function (e) { if (e.target === ov) closeSettings(); });
      document.body.appendChild(ov);
    }
    document.getElementById('zsSettingsFrame').src = '/settings' + (tab ? ('?tab=' + encodeURIComponent(tab)) : '');
    ov.style.display = 'flex';
  }
  function closeSettings() {
    var ov = document.getElementById('zsSettingsOverlay');
    if (ov) ov.style.display = 'none';
    if (pickLang() !== LANG) { location.reload(); return; }
    refreshStatus(true);
  }
  window.zeusOpenSettings = openSettings;
  window.addEventListener('message', function (e) {
    if (e.origin === location.origin && e.data && e.data.zeus === 'close-settings') closeSettings();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { var ov = document.getElementById('zsSettingsOverlay'); if (ov && ov.style.display === 'flex') closeSettings(); }
  });

  // ------------------------------------------------------------------ nav: mode badge, language, settings
  var STATUS = null;
  function navRight() { return document.querySelector('#topNav .top-nav-right'); }
  function ensureButtons() {
    var right = navRight();
    if (!right || document.getElementById('zsModeBadge')) return;
    var badge = document.createElement('button');
    badge.id = 'zsModeBadge'; badge.className = 'privacy-btn zs-badge'; badge.setAttribute('data-no-i18n', '');
    badge.onclick = function () { openSettings('broker'); };
    var sel = document.createElement('select');
    sel.id = 'zsLangSel'; sel.className = 'zs-lang-sel'; sel.setAttribute('data-no-i18n', '');
    sel.title = '界面语言 · Interface language';
    [['zh', '中文'], ['bi', '双语 · Bilingual'], ['en', 'English']].forEach(function (o) {
      var op = document.createElement('option'); op.value = o[0]; op.textContent = o[1]; if (o[0] === LANG) op.selected = true; sel.appendChild(op);
    });
    sel.onchange = function () { try { localStorage.setItem('zeusLang', sel.value); } catch (e) {} location.reload(); };
    var gear = document.createElement('button');
    gear.id = 'zsGearBtn'; gear.className = 'privacy-btn'; gear.textContent = '⚙'; gear.style.fontSize = '15px';
    gear.title = L('设置:账号、AI、新闻源、手动交易', 'Settings: accounts, AI, news sources, manual trades');
    gear.setAttribute('data-no-i18n', '');
    gear.onclick = function () { openSettings(); };
    right.insertBefore(gear, right.firstChild);
    right.insertBefore(sel, right.firstChild);
    right.insertBefore(badge, right.firstChild);
  }
  function renderBadge() {
    var b = document.getElementById('zsModeBadge');
    if (!b || !STATUS) return;
    if (STATUS.broker_mode === 'tastytrade') {
      b.textContent = '● tastytrade';
      b.className = 'privacy-btn zs-badge zs-live';
      b.title = L('已连接 tastytrade API:实时持仓与 DXLink 报价推送', 'tastytrade API connected: live positions and DXLink streaming');
    } else {
      var n = (STATUS.manual && STATUS.manual.trades) || 0;
      b.textContent = (LANG === 'en' ? '◐ Manual' : (LANG === 'bi' ? '◐ 手动 Manual' : '◐ 手动模式')) + (n ? ' · ' + n : '');
      b.className = 'privacy-btn zs-badge zs-manual';
      b.title = L('手动上传模式:期权报价来自 Cboe、股票来自 Yahoo Finance(约延迟 15 分钟)。连接 tastytrade 体验更顺滑。点此设置。',
        'Manual upload mode: option quotes from Cboe, stocks from Yahoo Finance (~15 min delayed). Connect tastytrade for the smoothest experience. Click to set up.');
    }
  }
  function manualHint() {
    var old = document.getElementById('zsManualHint');
    if (!STATUS || STATUS.broker_mode !== 'manual') { if (old) old.remove(); return; }
    var n = (STATUS.manual && STATUS.manual.trades) || 0;
    var panel = document.getElementById('tab-positions');
    if (!panel) return;
    if (n > 0) { if (old) old.remove(); return; }
    if (old) return;
    var B = function (zh, en) { return LANG === 'en' ? en : (LANG === 'bi' ? zh + '<br><span class="zs-sub-en">' + en + '</span>' : zh); };
    var hint = document.createElement('div');
    hint.id = 'zsManualHint'; hint.setAttribute('data-no-i18n', '');
    hint.innerHTML =
      '<div class="zs-hint-title">' + L('欢迎来到 ZEUS Terminal · 宙斯期权终端', 'Welcome to ZEUS Terminal') + '</div>' +
      '<div class="zs-hint-body">' + B(
        '还没有任何持仓。两种开局方式:<br><b>① 连接 tastytrade API</b>(推荐,实时持仓 + 实时报价推送 + 交易日记自动同步);<br><b>② 免费模式</b>:上传你自己的股票 / 期权成交 CSV,或者手动录入一笔。',
        'No positions yet. Two ways to start:<br><b>① Connect the tastytrade API</b> (recommended: live positions, streaming quotes, automatic journal);<br><b>② Free mode</b>: upload a CSV of your own stock and option fills, or type one in.') + '</div>' +
      '<div class="zs-hint-actions"><button class="zs-btn zs-primary" id="zsHintTT">' + L('连接 tastytrade', 'Connect tastytrade') + '</button>' +
      '<button class="zs-btn" id="zsHintCSV">' + L('上传 / 录入交易', 'Upload / add trades') + '</button>' +
      '<a class="zs-btn" href="/api/trades/template.csv">' + L('下载 CSV 模板', 'CSV template') + '</a></div>';
    panel.insertBefore(hint, panel.firstChild);
    document.getElementById('zsHintTT').onclick = function () { openSettings('broker'); };
    document.getElementById('zsHintCSV').onclick = function () { openSettings('broker'); };
  }
  function refreshStatus(reloadData) {
    return fetch('/api/zeus/status').then(function (r) { return r.json(); }).then(function (s) {
      var prevMode = STATUS && STATUS.broker_mode, prevTrades = STATUS && STATUS.manual && STATUS.manual.trades;
      STATUS = s;
      ensureButtons(); renderBadge(); manualHint(); barchartButton();
      var changed = (prevMode && prevMode !== s.broker_mode) || (prevTrades != null && s.manual && prevTrades !== s.manual.trades);
      if (reloadData && changed) setTimeout(function () { if (typeof softRefresh === 'function') softRefresh(); else location.reload(); }, 1500);
    }).catch(function () {});
  }

  // Picker → 📊 Barchart lists need a Barchart login (Settings → Other data); without one the
  // button only opened an empty menu, so it stays hidden.
  function barchartButton() {
    var w = document.getElementById('hmBcWrap');
    if (w && STATUS) w.style.display = STATUS.barchart ? 'inline-flex' : 'none';
  }

  // ------------------------------------------------------------------ top bar: tabs scroll sideways
  function initTabStrip() {
    var strip = document.getElementById('topNavTabs');
    if (!strip || strip.getAttribute('data-zs-strip')) return;
    strip.setAttribute('data-zs-strip', '1');
    var edges = function () {
      var room = strip.scrollWidth - strip.clientWidth;
      strip.classList.toggle('zs-more-l', room > 2 && strip.scrollLeft > 2);
      strip.classList.toggle('zs-more-r', room > 2 && strip.scrollLeft < room - 2);
    };
    var showActive = function () {
      var a = strip.querySelector('.top-nav-tab.active');
      if (a) {
        var s = strip.getBoundingClientRect(), r = a.getBoundingClientRect();
        if (r.left < s.left) strip.scrollLeft -= (s.left - r.left) + 32;
        else if (r.right > s.right) strip.scrollLeft += (r.right - s.right) + 32;
      }
      edges();
    };
    strip.addEventListener('scroll', edges, { passive: true });
    window.addEventListener('resize', showActive);
    // a mouse wheel moves the tabs sideways when they do not all fit
    strip.addEventListener('wheel', function (e) {
      if (strip.scrollWidth <= strip.clientWidth || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
      strip.scrollLeft += e.deltaY;
      e.preventDefault();
    }, { passive: false });
    new MutationObserver(showActive).observe(strip, { subtree: true, attributes: true, attributeFilter: ['class', 'data-zs-en'] });
    // the strip also narrows when the buttons on the right grow (bilingual labels, a longer badge)
    if (window.ResizeObserver) new ResizeObserver(edges).observe(strip);
    setTimeout(showActive, 0);
  }

  // ------------------------------------------------------------------ pop-ups
  // Escape closes the open pop-up (risk radar, checkup, liquidity, alerts, chart) or leaves the
  // open sub-view, the same way the browser's Back button does (appBack). Switching to another
  // top tab closes the pop-ups too, so one opened on a page does not stay over the next page.
  var OVERLAYS = { liquidityOverlay: 'closeLiquidity', checkupOverlay: 'closeCheckup', riskRadarOverlay: 'closeRiskRadar',
    alertOverlay: 'closeAlertPanel', tvOverlay: 'closeTvChart' };
  function closeOverlays() {
    Object.keys(OVERLAYS).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el || !el.style.display || el.style.display === 'none') return;
      var fn = window[OVERLAYS[id]];
      try { if (typeof fn === 'function') fn(); else el.style.display = 'none'; } catch (e) { el.style.display = 'none'; }
    });
  }
  function initPopups() {
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      var t = e.target;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      var ov = document.getElementById('zsSettingsOverlay');
      if (ov && ov.style.display === 'flex') return;   // the settings overlay closes itself
      if (typeof window.appBack === 'function') window.appBack();
    }, true);   // capture: runs before the settings overlay's own Escape handler closes it
    var orig = window.switchTopTab;
    if (typeof orig === 'function' && !orig.__zs) {
      var wrapped = function () { closeOverlays(); return orig.apply(this, arguments); };
      wrapped.__zs = true;
      window.switchTopTab = wrapped;
    }
  }

  // English UI: Chinese headline translations hidden by default (one click to show)
  function cnToggle() {
    if (LANG !== 'en') return;
    var show = false;
    try { show = localStorage.getItem('zeusShowCn') === '1'; } catch (e) {}
    document.documentElement.classList.toggle('zs-hide-cn', !show);
    var meta = document.getElementById('digestMeta');
    if (!meta || document.getElementById('zsCnBtn')) return;
    var b = document.createElement('button');
    b.id = 'zsCnBtn'; b.className = 'df-pill'; b.setAttribute('data-no-i18n', '');
    var paint = function () { b.textContent = show ? 'EN + 中文 ✓' : 'EN + 中文'; b.title = show ? 'Hide Chinese headlines' : 'Show Chinese headlines under each English one'; };
    paint();
    b.onclick = function () {
      show = !show;
      try { localStorage.setItem('zeusShowCn', show ? '1' : '0'); } catch (e) {}
      document.documentElement.classList.toggle('zs-hide-cn', !show); paint();
    };
    meta.parentNode.insertBefore(b, meta);
  }

  function boot() {
    LANG = pickLang();
    setLangClass();
    cnToggle();
    ensureButtons();
    initTabStrip();
    initPopups();
    refreshStatus(false);
    setInterval(function () { refreshStatus(true); }, 60000);
    startI18n();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
