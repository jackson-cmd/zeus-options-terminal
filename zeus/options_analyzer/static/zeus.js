/* ZEUS Terminal shell: settings button, settings overlay, three interface languages
 * (中文 / 双语 bilingual / English), manual-mode onboarding, and the ? help (zeus_help.js).
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
  // A dictionary key is a whole sentence. On the page it may carry line breaks from the HTML
  // source, or an emoji, bullet or colon around it ("📭 还没扫描过 — …", "近期≥2%跌: "): the
  // sentence is looked up without them and they are put back around the English.
  var EDGE_L = /^[^A-Za-z0-9一-鿿(（「『【《"“$]+/;
  var EDGE_R = /[^A-Za-z0-9一-鿿)）」』】》"”%]+$/;
  var EDGE_EN = { '：': ':', '，': ',', '。': '.', '；': ';', '！': '!', '？': '?', '、': ',', '——': '—' };
  // Parts of one line that the pages join with these separators are separate sentences.
  var SEG_RE = /( · | —{1,2} | \| |\n\s*)/;

  function runEn(run) {
    if (DICT[run]) return DICT[run];
    var t = run.replace(/[\s　]+$/, '');
    return DICT[t] ? DICT[t] + run.slice(t.length) : null;
  }
  function edgeEn(e) { return e.replace(/——|[：，。；！？、]/g, function (c) { return EDGE_EN[c]; }); }
  function lookup(key) {
    var en = lookupKey(key);
    // a Chinese colon or comma at the end carries its own space; the English one needs a real one
    // before the next element ("手动盯的慢变量：<span>…" → "…by hand: Treasury…")
    if (en && /[：，；]$/.test(key) && /[:,;]$/.test(en)) en += ' ';
    return en;
  }
  function lookupKey(key) {
    if (DICT[key]) return DICT[key];
    var k = key.replace(/\s+/g, ' ');
    if (k !== key && DICT[k]) return DICT[k];
    var l = k.match(EDGE_L), core = l ? k.slice(l[0].length) : k;
    var r = core.match(EDGE_R);
    if (r) core = core.slice(0, core.length - r[0].length);
    if (core === k || !HAS_CJK.test(core) || !DICT[core]) return null;
    return (l ? edgeEn(l[0]) : '') + DICT[core] + (r ? edgeEn(r[0]) : '');
  }
  function trWhole(s) {
    var key = s.trim(), en = key ? lookup(key) : null;
    return en == null ? null : s.replace(key, function () { return en; });
  }
  // Word by word, the last resort. A space goes in where an English word meets a letter, digit or
  // colon of the page ("SPY跌" → "SPY down", "行业:全部" → "Industry: all"), never inside the
  // page's own Latin text.
  function trRuns(s) {
    return s.replace(RUN_RE, function (run, at) {
      var r = runEn(run);
      if (r == null) return run;
      if (/[A-Za-z0-9%)\]:,;]/.test(s.charAt(at - 1)) && /^[A-Za-z]{2}/.test(r)) r = ' ' + r;
      if (/[A-Za-z0-9(]/.test(s.charAt(at + run.length)) && /[A-Za-z]$/.test(r)) r += ' ';
      return r;
    });
  }
  function trString(s) {
    if (!s || !HAS_CJK.test(s)) return s;
    var w = trWhole(s);
    if (w != null) return w;
    var parts = s.split(SEG_RE);
    if (parts.length < 2) return trRuns(s);
    for (var i = 0; i < parts.length; i += 2) {
      if (!HAS_CJK.test(parts[i])) continue;
      var p = trWhole(parts[i]);
      parts[i] = p != null ? p : trRuns(parts[i]);
    }
    return parts.join('');
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
    var txt = labelText(el).trim();
    var en = '';
    if (txt && HAS_CJK.test(txt) && txt.length <= 26) {
      // a count badge after the label ("👀 今日重点关注(在仓) (10)") is not part of its key, and the
      // Chinese label already shows it
      var cnt = txt.match(/^(.*?)\s*\(\d+\)$/);
      var whole = lookup(txt) || (cnt ? lookup(cnt[1]) : null);
      if (!whole && SEG_RE.test(txt)) { whole = trString(txt); if (HAS_CJK.test(whole)) whole = null; }
      var runs = txt.match(RUN_RE) || [];
      if (whole) en = whole;
      else if (runs.length && runs.length <= 3) {
        // numbers or Latin words around the Chinese words stay in place ("7天", "近期≥2%跌",
        // "隐藏 ETF 组"); a label of Chinese words only, or one made of several elements (a
        // table header with the date over "0d·周"), is glossed word by word
        if (/[A-Za-z0-9]/.test(txt.replace(RUN_RE, '')) && !labelKids(el)) en = trRuns(txt);
        else {
          var parts = runs.map(runEn);
          if (parts.every(function (x) { return x; })) en = parts.join(' ');
        }
      }
      // the gloss repeats no emoji, bullet or arrow the Chinese label already shows
      en = en.replace(/\s+/g, ' ').replace(EDGE_L, '').replace(/[^A-Za-z0-9)%"”]+$/, '').trim();
    }
    if (en && !HAS_CJK.test(en)) { if (el.getAttribute('data-zs-en') !== en) el.setAttribute('data-zs-en', en); }
    else if (el.hasAttribute('data-zs-en')) el.removeAttribute('data-zs-en');
  }
  // A label's own text and child elements, leaving out what it holds that is not interface text
  // (the ? help icon in a column header)
  function noI18n(n) { return !!(n && n.nodeType === 1 && n.hasAttribute && n.hasAttribute('data-no-i18n')); }
  function labelText(el) {
    if (!el.querySelector || !el.querySelector('[data-no-i18n]')) return el.textContent || '';
    var s = '', w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), n, p;
    while ((n = w.nextNode())) {
      for (p = n.parentNode; p && p !== el && !noI18n(p); p = p.parentNode) {}
      if (p === el) s += n.nodeValue;
    }
    return s;
  }
  function labelKids(el) {
    var c = el.children || [], k = 0;
    for (var i = 0; i < c.length; i++) if (!noI18n(c[i])) k++;
    return k;
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
  function openSettings(tab, anchor) {
    var ov = document.getElementById('zsSettingsOverlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'zsSettingsOverlay'; ov.setAttribute('data-no-i18n', '');
      ov.innerHTML = '<div class="zs-ov-box"><iframe id="zsSettingsFrame" title="Settings"></iframe></div>';
      ov.addEventListener('click', function (e) { if (e.target === ov) closeSettings(); });
      document.body.appendChild(ov);
    }
    document.getElementById('zsSettingsFrame').src = '/settings' + (tab ? ('?tab=' + encodeURIComponent(tab)) : '') +
      (anchor ? '#' + encodeURIComponent(anchor) : '');
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

  // ------------------------------------------------------------------ nav: language, settings
  // The broker mode (tastytrade live, or manual with the number of trades) is shown in Settings
  // and in the ⚙ tooltip; a badge for it in the top bar crowded out the tabs.
  var STATUS = null;
  function navRight() { return document.querySelector('#topNav .top-nav-right'); }
  function ensureButtons() {
    var right = navRight();
    if (!right || document.getElementById('zsGearBtn')) return;
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
  }
  function renderMode() {
    var g = document.getElementById('zsGearBtn');
    if (!g || !STATUS) return;
    var mode, bm = STATUS.broker_mode, ms = STATUS.market_source;
    var quotes = {
      tastytrade: L('DXLink 实时推送', 'DXLink streaming'),
      alpaca: L('Alpaca（股票 IEX 实时，期权参考价）', 'Alpaca (IEX stocks live, indicative options)'),
      public: L('Public 实时', 'Public real time'),
      free: L('Cboe 期权 + Yahoo 股票，约延迟 15 分钟', 'Cboe options + Yahoo stocks, ~15 min delayed')
    }[ms] || '';
    if (bm === 'tastytrade') {
      mode = L('● tastytrade 实时:实时持仓与 DXLink 报价推送', '● tastytrade live: live positions and DXLink streaming');
    } else if (bm === 'alpaca' || bm === 'public') {
      var name = bm === 'alpaca' ? ('Alpaca ' + (STATUS.alpaca_paper ? L('模拟盘', 'paper') : L('实盘', 'live'))) : 'Public';
      mode = '● ' + name + L(' 持仓 · 行情:', ' positions · quotes: ') + quotes;
    } else {
      var n = (STATUS.manual && STATUS.manual.trades) || 0;
      mode = '◐ ' + L('手动上传模式' + (n ? ',' + n + ' 笔交易' : ''), 'Manual upload mode' + (n ? ', ' + n + ' trades' : '')) +
        L(' · 行情:', ' · quotes: ') + quotes;
    }
    g.title = L('设置:券商、行情、AI、新闻源、手动交易', 'Settings: broker, quotes, AI, news sources, manual trades') + '\n' + mode;
  }
  // First visit with nothing to show: three ways in, the quickest first (a sample portfolio priced from
  // today's quotes). With sample trades loaded, a slim bar says so and removes them in one click.
  function manualHint() {
    var old = document.getElementById('zsManualHint');
    var bar = document.getElementById('zsSampleBar');
    var panel = document.getElementById('tab-positions');
    var manual = STATUS && STATUS.broker_mode === 'manual';
    var n = manual ? ((STATUS.manual && STATUS.manual.trades) || 0) : -1;
    var ns = manual ? ((STATUS.manual && STATUS.manual.sample) || 0) : 0;
    if (!panel || !STATUS) return;
    var B = function (zh, en) { return LANG === 'en' ? en : (LANG === 'bi' ? zh + '<br><span class="zs-sub-en">' + en + '</span>' : zh); };
    // sample bar
    if (ns > 0) {
      if (!bar) {
        bar = document.createElement('div');
        bar.id = 'zsSampleBar'; bar.setAttribute('data-no-i18n', '');
        bar.innerHTML = '<span>' + B('🧪 正在看示例组合：持仓是编的，报价是今天的真实行情。', '🧪 This is the sample portfolio: made-up trades, priced with today\'s real quotes.') + '</span>' +
          '<span class="zs-hint-actions"><button class="zs-btn" id="zsSampleOff">' + L('清除示例', 'Remove the sample') + '</button>' +
          '<button class="zs-btn zs-primary" id="zsSampleBroker">' + L('换成我自己的', 'Use my own') + '</button></span>';
        panel.insertBefore(bar, panel.firstChild);
        document.getElementById('zsSampleOff').onclick = function () { sampleCall('DELETE', this); };
        document.getElementById('zsSampleBroker').onclick = function () { openSettings('broker'); };
      }
    } else if (bar) bar.remove();
    if (n !== 0) { if (old) old.remove(); return; }
    if (old) return;
    var hint = document.createElement('div');
    hint.id = 'zsManualHint'; hint.setAttribute('data-no-i18n', '');
    var card = function (icon, title, body, buttons) {
      return '<div class="zs-start-card"><div class="zs-start-h">' + icon + ' ' + title + '</div><div class="zs-start-b">' + body +
        '</div><div class="zs-hint-actions">' + buttons + '</div></div>';
    };
    hint.innerHTML =
      '<div class="zs-hint-title">' + L('欢迎来到 ZEUS Terminal · 宙斯期权终端', 'Welcome to ZEUS Terminal') + '</div>' +
      '<div class="zs-hint-body">' + B('还没有任何持仓。选一种开局方式，随时可以换：', 'No positions yet. Pick a way in; you can switch any time:') + '</div>' +
      '<div class="zs-start-grid">' +
      card('🧪', L('先看示例组合', 'Try the sample portfolio'),
        B('一键载入 6 个示例持仓和 1 笔已平仓交易，行权价和价格取自今天的真实期权报价。每个页面立刻有内容，看完一键清除。',
          'Six sample positions and one closed trade, with strikes and prices taken from today\'s real option quotes. Every page fills up at once; remove it in one click.'),
        '<button class="zs-btn zs-primary" id="zsHintSample">' + L('载入示例组合', 'Load the sample') + '</button>') +
      card('🔌', L('连接券商', 'Connect a broker'),
        B('tastytrade（实时推送）、Alpaca（免费注册，自带模拟盘）或 Public。ZEUS 只读取持仓和行情，从不下单。',
          'tastytrade (streaming), Alpaca (free sign-up, paper account included) or Public. ZEUS only reads positions and quotes; it never places an order.'),
        '<button class="zs-btn" id="zsHintTT">' + L('连接券商', 'Connect a broker') + '</button>') +
      card('📄', L('上传我的成交', 'Upload my trades'),
        B('任何券商导出的 CSV（股票和期权都行），或手动录入一笔。行情走免费源，不用开户。',
          'A CSV export from any broker (stocks and options), or type one in. Quotes come from the free sources, no account needed.'),
        '<button class="zs-btn" id="zsHintCSV">' + L('上传 / 录入交易', 'Upload / add trades') + '</button>' +
        '<a class="zs-btn" href="/api/trades/template.csv">' + L('下载 CSV 模板', 'CSV template') + '</a>') +
      '</div>' +
      '<div class="zs-hint-foot">' + B('不连账户也能直接用：🎯 选股、📋 Digest、📅 Events 和 TA 扫描都用免费数据。每页右上角的 ? 讲解每个按钮和数字是什么、怎么算、怎么用。',
        'Without any account you can still use 🎯 Picker, 📋 Digest, 📅 Events and the TA scanner on free data. The ? at the top right of every page explains each button and number: what it is, how it is worked out, how to use it.') + '</div>';
    panel.insertBefore(hint, panel.firstChild);
    document.getElementById('zsHintSample').onclick = function () { sampleCall('POST', this); };
    document.getElementById('zsHintTT').onclick = function () { openSettings('broker'); };
    document.getElementById('zsHintCSV').onclick = function () { openSettings('broker', 'tradesSec'); };
  }
  function sampleCall(method, btn) {
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = method === 'POST' ? L('正在按今天的报价生成…', 'Pricing it with today\'s quotes…') : L('清除中…', 'Removing…');
    fetch('/api/trades/sample', { method: method, headers: { 'X-Zeus-Client': '1' } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok === false) throw new Error(j.error || 'failed');
        var h = document.getElementById('zsManualHint'); if (h) h.remove();
        var b = document.getElementById('zsSampleBar'); if (b) b.remove();
        return refreshStatus(false).then(function () {
          if (typeof softRefresh === 'function') softRefresh(); else location.reload();
        });
      })
      .catch(function (e) { btn.disabled = false; btn.textContent = old; alert(L('没成功：', 'That did not work: ') + (e && e.message || e)); });
  }
  function refreshStatus(reloadData) {
    return fetch('/api/zeus/status').then(function (r) { return r.json(); }).then(function (s) {
      var prevMode = STATUS && STATUS.broker_mode, prevTrades = STATUS && STATUS.manual && STATUS.manual.trades;
      STATUS = s;
      ensureButtons(); renderMode(); manualHint(); barchartButton();
      var strip = document.getElementById('topNavTabs');
      if (strip) strip.dispatchEvent(new Event('scroll'));   // the right-hand buttons may have changed width
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
  // Pop-ups the page's appBack() does not know about. Esc closes the topmost one; switching tabs closes
  // them all; the small drop-down panels (PANELS: panel → the button that opens it) also close on a
  // click anywhere outside them.
  var POPUPS = { posAiOverlay: '', hmAiModal: 'hmAiCloseReport', dgAiOverlay: '_dgAiClose',
    earningsModal: 'closeEarningsModal', trStatusPanel: '' };
  var PANELS = { trStatusPanel: 'trStatusBtn' };
  function shown(el) {
    return !!el && el.style.display !== 'none' && getComputedStyle(el).display !== 'none';
  }
  function closeOne(id, fnName) {
    var el = document.getElementById(id), fn = fnName && window[fnName];
    try { if (typeof fn === 'function') fn(); else el.style.display = 'none'; } catch (e) { el.style.display = 'none'; }
  }
  function closeOverlays() {
    Object.keys(OVERLAYS).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el || !el.style.display || el.style.display === 'none') return;
      closeOne(id, OVERLAYS[id]);
    });
    Object.keys(POPUPS).forEach(function (id) { if (shown(document.getElementById(id))) closeOne(id, POPUPS[id]); });
  }
  function closeTopPopup() {     // → true when a pop-up was closed
    var open = Object.keys(POPUPS).filter(function (id) { return shown(document.getElementById(id)); });
    if (!open.length) return false;
    var z = function (id) { return parseInt(getComputedStyle(document.getElementById(id)).zIndex, 10) || 0; };
    open.sort(function (a, b) { return z(b) - z(a); });
    closeOne(open[0], POPUPS[open[0]]);
    return true;
  }
  var POPUP_SEL = '#' + Object.keys(OVERLAYS).concat(Object.keys(POPUPS)).join(', #');
  function initPopups() {
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      var t = e.target;
      // Esc in a text box on the page is left alone; in a pop-up (the checkup puts the cursor in its
      // ticker box when it opens) it still closes the pop-up
      var inPopup = t && t.closest && t.closest(POPUP_SEL);
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable) && !inPopup) return;
      var ov = document.getElementById('zsSettingsOverlay');
      if (ov && ov.style.display === 'flex') return;   // the settings overlay closes itself
      if (inPopup && t.blur) t.blur();
      if (closeTopPopup()) return;
      if (typeof window.appBack === 'function') window.appBack();
    }, true);   // capture: runs before the settings overlay's own Escape handler closes it
    document.addEventListener('click', function (e) {
      Object.keys(PANELS).forEach(function (id) {
        var p = document.getElementById(id), b = document.getElementById(PANELS[id]);
        if (!shown(p) || p.contains(e.target) || (b && b.contains(e.target))) return;
        p.style.display = 'none';
      });
    }, true);
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

  // ------------------------------------------------------------------ help
  // zeus_help.js: the ? cards beside columns and scores, and the page guide behind the ? button.
  // Loaded after the page is up so it never delays it.
  function loadHelp() {
    if (window.zeusHelp || document.getElementById('zsHelpJs') || !document.body) return;
    var s = document.createElement('script');
    s.id = 'zsHelpJs'; s.src = '/static/zeus_help.js'; s.async = true;
    document.body.appendChild(s);
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
    loadHelp();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
