"""Embedded HTML + JS for the SEC filings sub-pane (lives inside the Events tab).

Mirrors heatmap.py: two strings spliced into optionstrat_server.py — the pane
markup at the Events-tab placeholder, and the JS into the injection block.

The Events tab owns a small sub-nav (📅 Calendar | 📄 SEC) that toggles between
#evCalPane (the existing calendar) and #evSecPane (this). evSubTab() drives it.
"""

SEC_PANE_HTML = r"""
<div id="evSecPane" style="display:none;flex-direction:column;gap:10px">
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <input id="secTicker" type="text" placeholder="输入 ticker，回车拉取（如 AAPL）"
      onkeydown="if(event.key==='Enter')secLoad()"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 10px;font-size:13px;width:240px;text-transform:uppercase;font-weight:600">
    <select id="secFormSel" onchange="secLoad()"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 8px;font-size:12px">
      <option value="10-Q,10-K" selected>10-Q + 10-K</option>
      <option value="10-Q">仅 10-Q（季报）</option>
      <option value="10-K">仅 10-K（年报）</option>
    </select>
    <select id="secCountSel" onchange="secLoad()" title="拉取多少份历史"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 8px;font-size:12px">
      <option value="12">近 12 份</option>
      <option value="25" selected>近 25 份</option>
      <option value="50">近 50 份</option>
    </select>
    <button onclick="secLoad()"
      style="background:#1f6feb;color:#fff;border:none;border-radius:4px;padding:6px 14px;cursor:pointer;font-size:12px;font-weight:600">拉取</button>
    <select id="secFilingSel" onchange="secOpenFromSel()" title="选择具体哪一份财报"
      style="display:none;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:6px 8px;font-size:12px;max-width:230px"></select>
    <button id="secBiToggle" onclick="secToggleBilingual()" title="滚到哪翻到哪：正文 + 表格文字下插中文（数字/日期不翻）"
      style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:6px 12px;cursor:pointer;font-size:12px;font-weight:600">双语 EN/中</button>
    <!-- ⚠️ 文案别承诺「当前财报对应的电话会」。get_transcript_html() 只按 ticker
         搜 YouTube 取**最近一期**并按 ticker 缓存, 拿不到指定季度 —— 选 2020 年的
         10-Q 点开, 出来的仍是最新一期(实测 AAPL 选 2020-05-01 的 10-Q, 显示的是
         Q2 FY26 2026-04-30 的电话会, 差了 6 年)。原 title 写「↔ 当前财报」会让人
         以为两者对应。技术上无法按季回溯, 那就把话说准。 -->
    <button id="secTrToggle" onclick="secToggleTranscript()" title="切换：YouTube 上该公司「最近一期」财报电话会字幕 ↔ 当前财报（注意：字幕永远是最新一期，不随左侧选中的财报期变化；双语，首次约 10-30s）"
      style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:6px 10px;cursor:pointer;font-size:12px">📞 电话会<span style="opacity:.6;font-size:10px">·最近期</span></button>
    <button onclick="secOpenCache()" title="打开翻译缓存文件夹，可删掉不想要的 ticker"
      style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:6px 10px;cursor:pointer;font-size:12px">🗂 缓存</button>
    <button id="secProfileBtn" onclick="secToggleProfile()" title="查看/生成公司简介（基于最新10-Q MD&A + Risk Factors）"
      style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:6px 10px;cursor:pointer;font-size:12px">📋 简介</button>
    <span id="secStatus" style="color:#8b949e;font-size:11px"></span>
  </div>
  <div id="secProfilePane" style="display:none;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:14px 18px;max-height:50vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <span id="secProfileMeta" style="color:#6e7681;font-size:11px"></span>
      <div style="display:flex;gap:6px">
        <button id="secProfileRefresh" onclick="secRefreshProfile()" style="background:#1f6feb;color:#fff;border:none;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:11px;font-weight:600;display:none">🔄 重新生成</button>
        <button onclick="secToggleProfile()" style="background:transparent;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:11px">收起</button>
      </div>
    </div>
    <div id="secProfileBody" style="color:#c9d1d9;font-size:13px;line-height:1.8;white-space:pre-wrap"></div>
  </div>
  <div style="display:flex;gap:10px;min-height:0">
    <div id="secToc" style="width:230px;flex:none;overflow-y:auto;height:78vh;border:1px solid #21262d;border-radius:6px;background:#0d1117">
      <div id="secDocs"></div>
      <div id="secTocList"><div style="padding:16px;color:#6e7681;font-size:12px">拉取财报后，这里显示这份文件的目录，点击可跳转。</div></div>
    </div>
    <iframe id="secFrame" title="SEC filing"
      style="flex:1;min-width:0;height:78vh;border:1px solid #21262d;border-radius:6px;background:#fff"></iframe>
  </div>
</div>
"""

SEC_JS = r"""
// SEC filings sub-pane (inside Events tab). Vanilla, window-scoped.
(function () {
  if (window.__secInit) return;
  window.__secInit = true;

  function _esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

  // Toggle the Events tab between the calendar pane and the SEC pane.
  window.evSubTab = function (name) {
    var cal = document.getElementById('evCalPane');
    var sec = document.getElementById('evSecPane');
    var isSec = (name === 'sec');
    if (cal) cal.style.display = isSec ? 'none' : '';
    if (sec) sec.style.display = isSec ? 'flex' : 'none';
    var nav = document.getElementById('evSubNav');
    if (nav) nav.querySelectorAll('button[data-ev]').forEach(function (b) {
      var a = (b.dataset.ev === (isSec ? 'sec' : 'cal'));
      b.style.background = a ? '#1f6feb' : 'transparent';
      b.style.color = a ? '#fff' : '#8b949e';
    });
  };

  function _setStatus(msg, err) {
    var el = document.getElementById('secStatus');
    if (el) { el.textContent = msg || ''; el.style.color = err ? '#f85149' : '#8b949e'; }
  }

  window.secOpen = function (cik, accession, doc, idx) {
    window.__secMode = 'filing';            // opening a filing leaves transcript mode
    _setTrBtn();
    window.__secCur = { cik: cik, accession: accession, primary: doc };
    var sel = document.getElementById('secFilingSel');
    if (sel && String(idx) !== sel.value) sel.value = String(idx);
    var docsEl = document.getElementById('secDocs');
    if (docsEl) docsEl.innerHTML = '';
    var tocEl = document.getElementById('secTocList');
    if (tocEl) tocEl.innerHTML = '<div style="padding:14px;color:#6e7681;font-size:12px">加载…</div>';
    // Fetch the filing's document list. 8-K earnings content lives in the EX-99.1
    // exhibit (bigger than the thin cover), so default-open the biggest doc.
    fetch('/api/sec/docs?cik=' + encodeURIComponent(cik) + '&accession=' + encodeURIComponent(accession))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var docs = (j && j.docs) || [];
        window.__secDocs = docs;
        var best = docs.length ? docs[0].name : doc;
        _renderDocList(docs, doc, best);
        secLoadDoc(best);
      })
      .catch(function () {
        window.__secDocs = [];
        _renderDocList([], doc, doc);
        secLoadDoc(doc);
      });
  };

  function secLoadDoc(name) {
    var cur = window.__secCur || {};
    if (!cur.cik) return;
    window.__secActiveDoc = name;           // remember which doc so we can return to it
    var frame = document.getElementById('secFrame');
    var tocEl = document.getElementById('secTocList');
    if (tocEl) tocEl.innerHTML = '<div style="padding:14px;color:#6e7681;font-size:12px">加载目录…</div>';
    if (frame) {
      frame.onload = function () {
        try { secBuildToc(); } catch (e) {}
        if (_biOn()) { try { secInitTranslate(); } catch (e) {} }
      };
      frame.src = '/api/sec/document?cik=' + encodeURIComponent(cur.cik)
        + '&accession=' + encodeURIComponent(cur.accession)
        + '&doc=' + encodeURIComponent(name);
    }
  }

  function _renderDocList(docs, primary, active) {
    var el = document.getElementById('secDocs');
    if (!el) return;
    if (!docs || docs.length <= 1) { el.innerHTML = ''; return; }   // single doc → no chooser
    function kb(n) { return n >= 1024 ? Math.round(n / 1024) + 'KB' : (n || 0) + 'B'; }
    function label(nm) {
      if (nm === primary) return '主文档（封面）';
      if (/ex.?99/i.test(nm)) return '业绩稿 / 新闻稿 (EX-99)';
      var m = nm.match(/ex.?(\d{1,2})/i);
      return m ? ('附件 EX-' + m[1]) : nm;
    }
    el.innerHTML = '<div style="padding:8px 10px;font-size:11px;color:#6e7681;border-bottom:1px solid #21262d;background:#0d1117">本份文档 · 点击切换</div>'
      + docs.map(function (d) {
        var on = (d.name === active);
        return '<div class="sec-doc-item" data-doc="' + _esc(d.name) + '" onclick="secDocClick(this)" '
          + 'style="padding:6px 10px;cursor:pointer;border-bottom:1px solid #161b22;border-left:3px solid '
          + (on ? '#58a6ff' : 'transparent') + ';font-size:11px;color:' + (on ? '#e6edf3' : '#9da7b3') + '">'
          + _esc(label(d.name)) + ' <span style="color:#6e7681">· ' + kb(d.size) + '</span></div>';
      }).join('');
  }

  window.secDocClick = function (el) {
    var name = el.getAttribute('data-doc');
    var cur = window.__secCur || {};
    _renderDocList(window.__secDocs || [], cur.primary, name);
    secLoadDoc(name);
  };

  window.secLoad = function () {
    var input = document.getElementById('secTicker');
    var ticker = (input && input.value || '').trim().toUpperCase();
    if (!ticker) { _setStatus('请输入 ticker', true); return; }
    window.__secMode = 'filing'; _setTrBtn();   // fresh pull → filing browsing
    window.__secTicker = ticker;
    var forms = (document.getElementById('secFormSel') || {}).value || '10-Q,10-K';
    var count = (document.getElementById('secCountSel') || {}).value || '25';
    var sel = document.getElementById('secFilingSel');
    var toc = document.getElementById('secTocList');   // inner TOC area — NOT #secToc (that would wipe #secDocs/#secTocList)
    var docsEl = document.getElementById('secDocs');
    _setStatus('拉取中…');
    if (sel) sel.style.display = 'none';
    if (docsEl) docsEl.innerHTML = '';
    if (toc) toc.innerHTML = '<div style="padding:16px;color:#8b949e;font-size:12px">拉取中…</div>';

    fetch('/api/sec/filings?ticker=' + encodeURIComponent(ticker)
          + '&forms=' + encodeURIComponent(forms) + '&limit=' + encodeURIComponent(count))
      .then(function (r) { return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || r.statusText); return j; }); })
      .then(function (data) {
        var fs = data.filings || [];
        window.__secFilings = fs;
        if (!fs.length) {
          _setStatus('未找到 10-Q/10-K', true);
          if (toc) toc.innerHTML = '<div style="padding:16px;color:#6e7681;font-size:12px">没有符合条件的财报文件。</div>';
          return;
        }
        _setStatus((data.company || ticker) + ' · ' + fs.length + ' 份');
        if (sel) {
          sel.innerHTML = fs.map(function (f, i) {
            return '<option value="' + i + '">' + _esc(f.form) + ' · ' + _esc(f.filing_date)
              + ' (报告期 ' + _esc(f.report_date || '—') + ')</option>';
          }).join('');
          sel.style.display = '';
          sel.value = '0';
        }
        secOpenIdx(0);   // 默认打开最新一份
        // refresh profile pane if open
        var pp = document.getElementById('secProfilePane');
        if (pp && pp.style.display !== 'none') {
          delete _secProfileCache[ticker];
          _secFetchProfile(ticker);
        }
      })
      .catch(function (e) {
        _setStatus('失败: ' + e.message, true);
        if (toc) toc.innerHTML = '<div style="padding:16px;color:#f85149;font-size:12px">' + _esc(e.message) + '</div>';
        // ⚠️ 也要清右边的正文。原来只改状态栏和左侧目录, #secFrame 保持不动 ——
        //    于是搜一个不存在的 ticker 后, 上一只票的 10-Q/电话会字幕还整版挂在
        //    右边, 看着就像是这个 ticker 的搜索结果(错误提示很容易被忽略)。
        var _fr = document.getElementById('secFrame');
        if (_fr) {
          try { _fr.removeAttribute('src'); } catch (_) {}
          _fr.srcdoc = '<div style="padding:40px;text-align:center;color:#8b949e;'
            + 'font-family:-apple-system,sans-serif;font-size:13px">'
            + _esc(e.message) + '</div>';
        }
        var _sel = document.getElementById('secFilingSel');
        if (_sel) _sel.innerHTML = '';
      });
  };

  window.secOpenFromSel = function () {
    var sel = document.getElementById('secFilingSel');
    secOpenIdx(sel ? (parseInt(sel.value, 10) || 0) : 0);
  };
  function secOpenIdx(i) {
    var fs = window.__secFilings || [];
    var f = fs[i];
    if (f) secOpen(f.cik, f.accession, f.primary_document, i);
  }

  // ─── Bilingual viewer: scroll-driven, skip tables/XBRL/hidden ──────────────
  // The SEC doc is served same-origin (/api/sec/document), so we can walk the
  // iframe DOM directly. As blocks scroll into the iframe viewport we batch
  // their text to /api/sec/translate and insert a Chinese line under each.
  var BI_KEY = 'sec_bilingual';
  var _secObs = null, _secQueue = [], _secFlushT = null;
  var _secCand = [], _secDraining = false, _secDrainT = null;   // 后台预取(边读边翻后面)
  var _secPhase = 0, _secToTranslate = [];                       // drain: 0=lookup 扫缓存, 1=翻 miss
  window.__secTicker = window.__secTicker || '';

  function _biOn() { try { return localStorage.getItem(BI_KEY) === '1'; } catch (e) { return false; } }

  function _renderBiBtn(on) {
    var b = document.getElementById('secBiToggle');
    if (!b) return;
    b.style.background = on ? '#1f6feb' : 'transparent';
    b.style.color = on ? '#fff' : '#8b949e';
    b.style.borderColor = on ? '#1f6feb' : '#30363d';
  }

  window.secToggleBilingual = function () {
    var on = !_biOn();
    try { localStorage.setItem(BI_KEY, on ? '1' : '0'); } catch (e) {}
    _renderBiBtn(on);
    if (on) { try { secInitTranslate(); } catch (e) {} } else { secClearTranslate(); }
  };

  window.secOpenCache = function () {
    fetch('/api/sec/cache/open', { method: 'POST' })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || r.statusText); return j; }); })
      .then(function () { _setStatus('已在 Finder 打开缓存文件夹'); })
      .catch(function (e) { _setStatus('打开缓存失败: ' + e.message, true); });
  };

  // 📞 Earnings-call transcript (from YouTube) ↔ the filing you were viewing.
  // The button is a TOGGLE: click once → transcript; click again → back to the
  // exact filing/doc you came from (state saved on the way out, so the return is
  // instant, no re-fetch). Transcript loads into the same iframe and reuses the
  // bilingual translator. No filing/TOC; it's a flat document.
  window.__secMode = window.__secMode || 'filing';   // 'filing' | 'transcript'

  function _setTrBtn() {
    var b = document.getElementById('secTrToggle');
    if (!b) return;
    var inTr = (window.__secMode === 'transcript');
    b.textContent = inTr ? '📄 返回财报' : '📞 电话会';
    b.style.background = inTr ? '#1f6feb' : 'transparent';
    b.style.color = inTr ? '#fff' : '#8b949e';
    b.style.borderColor = inTr ? '#1f6feb' : '#30363d';
    b.title = inTr ? '点此切回刚才那份财报'
                   : '切换：YouTube 财报电话会字幕 ↔ 当前财报（双语，首次约 10-30s）';
  }

  window.secToggleTranscript = function () {
    if (window.__secMode === 'transcript') secBackToFiling();
    else secOpenTranscript();
  };

  // Restore the filing the user was viewing before opening the transcript —
  // instant, no re-fetch (we stashed it in __secFilingState on the way out).
  function secBackToFiling() {
    var st = window.__secFilingState;
    window.__secMode = 'filing';
    _setTrBtn();
    if (!st || !st.cur || !st.cur.cik) { _setStatus('没有可返回的财报，请先拉取一份'); return; }
    window.__secCur = st.cur;
    window.__secDocs = st.docs || [];
    _renderDocList(window.__secDocs, st.cur.primary, st.doc || st.cur.primary);
    _setStatus('');
    secLoadDoc(st.doc || st.cur.primary);
  }

  window.secOpenTranscript = function () {
    var tk = (window.__secTicker || (document.getElementById('secTicker') || {}).value || '').trim().toUpperCase();
    if (!tk) { _setStatus('先输入 ticker', true); return; }
    // Stash the filing we're leaving so the button can switch straight back to it.
    if ((window.__secCur || {}).cik) {
      window.__secFilingState = {
        cur: window.__secCur,
        doc: window.__secActiveDoc || window.__secCur.primary || '',
        docs: window.__secDocs || []
      };
    }
    window.__secMode = 'transcript';
    _setTrBtn();
    _setStatus('找电话会视频 + 抓字幕中…（首次约 10–30s）');
    window.__secCur = { cik: '', accession: '', primary: '' };
    var docsEl = document.getElementById('secDocs');
    if (docsEl) docsEl.innerHTML = '';
    var tocEl = document.getElementById('secTocList');
    if (tocEl) tocEl.innerHTML = '<div style="padding:14px;color:#6e7681;font-size:12px">📞 电话会 transcript（来自 YouTube，无目录）。点上方「📄 返回财报」切回财报。</div>';
    var frame = document.getElementById('secFrame');
    if (frame) {
      frame.onload = function () {
        _setStatus(tk + ' 电话会 transcript 已加载');
        if (_biOn()) { try { secInitTranslate(); } catch (e) {} }
      };
      frame.src = '/api/sec/transcript?ticker=' + encodeURIComponent(tk);
    }
  };

  function _secDoc() {
    var f = document.getElementById('secFrame');
    try { return (f && f.contentDocument) ? f.contentDocument : null; } catch (e) { return null; }
  }

  window.secClearTranslate = function () {
    _secStopDrain();
    if (_secObs) { try { _secObs.disconnect(); } catch (e) {} _secObs = null; }
    _secQueue = [];
    var d = _secDoc();
    if (!d) return;
    d.querySelectorAll('.zh-tr, .zh-tr-btn').forEach(function (el) { el.remove(); });
    d.querySelectorAll('[data-sec-tr]').forEach(function (el) { el.removeAttribute('data-sec-tr'); });
  };

  // Textual content of a candidate block, WITH the reload-button & any injected
  // Chinese sibling stripped. Keeps the hash we send to the server stable
  // regardless of what we've spliced into the block for UX. Note: the 🔄 button
  // is appended INSIDE the block (so it flows with the text), which means we
  // MUST filter it out here — otherwise every hash includes "🔄" and nothing
  // hits the cache.
  function _elText(el) {
    var clone = el.cloneNode(true);
    clone.querySelectorAll('.zh-tr-btn, .zh-tr, .zh-tr-c').forEach(function (n) { n.remove(); });
    return (clone.textContent || '').replace(/\s+/g, ' ').trim();
  }

  // Append a small 🔄 reload chip to a candidate block. Always visible (per user
  // ask), tucked inline at the end so it doesn't disturb layout. Clicking it
  // wipes the block's existing translation and forces a fresh /translate call
  // with force=true (server bypasses the cache and overwrites it) — recovers
  // from stuck-English blocks where the cached zh was actually still English.
  function _secAddReloadBtn(el) {
    if (!el || el.querySelector(':scope > .zh-tr-btn')) return;
    var doc = el.ownerDocument;
    var b = doc.createElement('span');
    b.className = 'zh-tr-btn';
    b.textContent = '↻';
    b.title = '重翻这段（跳过缓存）';
    b.setAttribute('data-sec-btn', '1');
    // Onclick is set via property (not attribute) because the iframe document
    // is served same-origin but doesn't inherit the parent's global handlers.
    b.onclick = function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      _secReloadOne(el, b);
    };
    el.appendChild(b);
  }

  // Reload one block: strip its zh, force-refresh via /translate {force:true}.
  function _secReloadOne(el, btn) {
    if (!el) return;
    var orig = btn ? btn.textContent : '';
    if (btn) { btn.textContent = '⟳'; btn.style.opacity = '0.5'; }
    // Remove existing Chinese output (both sibling .zh-tr and in-cell .zh-tr-c).
    var nx = el.nextSibling;
    if (nx && nx.classList && nx.classList.contains('zh-tr')) nx.remove();
    el.querySelectorAll(':scope > .zh-tr-c').forEach(function (n) { n.remove(); });
    el.removeAttribute('data-sec-tr');
    var text = _elText(el);
    if (!text) { if (btn) { btn.textContent = orig; btn.style.opacity = ''; } return; }
    fetch('/api/sec/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: window.__secTicker || '', texts: [text], force: true })
    }).then(function (r) { return r.json(); }).then(function (j) {
      var zh = ((j && j.zh) || [])[0] || '';
      _secInject(el, zh);
      if (btn) { btn.textContent = orig || '↻'; btn.style.opacity = ''; }
    }).catch(function () {
      if (btn) { btn.textContent = '!'; btn.style.opacity = ''; setTimeout(function () { btn.textContent = orig || '↻'; }, 1500); }
    });
  }

  function _isHidden(el, win) {
    try {
      var s = win.getComputedStyle(el);
      if (s && (s.display === 'none' || s.visibility === 'hidden')) return true;
    } catch (e) {}
    return (el.offsetParent === null) && (!el.getClientRects || el.getClientRects().length === 0);
  }

  // A table cell that is essentially just a date → leave it in English (no point translating).
  function _isDateish(t) {
    var s = (t || '').trim();
    return /^[(]?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z.]*\s+\d{1,2},?\s+\d{4}[)]?$/i.test(s)
      || /^\d{1,2}\/\d{1,2}\/\d{2,4}$/.test(s)
      || /^\d{4}-\d{2}-\d{2}$/.test(s)
      || /^(q[1-4]\s+)?(fy\s*)?\d{4}$/i.test(s);
  }

  window.secInitTranslate = function () {
    var d = _secDoc();
    if (!d || !d.body) return;
    var win = d.defaultView || window;
    _secStopDrain();
    if (_secObs) { try { _secObs.disconnect(); } catch (e) {} _secObs = null; }
    _secQueue = []; _secCand = [];
    if (!d.getElementById('__zhTrStyle')) {
      var st = d.createElement('style');
      st.id = '__zhTrStyle';
      st.textContent = '.zh-tr{color:#1d4ed8;border-left:3px solid #93c5fd;padding-left:8px;margin:2px 0 8px;font-size:0.95em;line-height:1.5;display:block;font-family:inherit}'
        + '.zh-tr-c{color:#1d4ed8;font-size:0.85em;line-height:1.3;display:block;margin-top:1px;font-weight:normal}'
        + '.zh-tr-btn{display:inline-block;margin-left:6px;padding:0 5px;font-size:11px;line-height:1.2;color:#8b949e;background:transparent;border:1px solid #d0d7de;border-radius:3px;cursor:pointer;user-select:none;vertical-align:middle;font-family:sans-serif;opacity:.55;transition:opacity .15s,color .15s,border-color .15s}'
        + '.zh-tr-btn:hover{opacity:1;color:#1f6feb;border-color:#1f6feb}';
      (d.head || d.body).appendChild(st);
    }
    var cand = [];
    d.body.querySelectorAll('p, li, div, span, td, th').forEach(function (el) {
      if (el.getAttribute('data-sec-tr')) return;
      if ((el.tagName || '').indexOf(':') !== -1) return;        // XBRL ix:/xbrli:/xbrldi:
      if (el.querySelector('p, li, div, table, td, th')) return; // leaf blocks only
      var t = _elText(el);
      var inTbl = !!el.closest('table');                          // table cells: short labels OK; numbers/dates filtered
      if (t.length < (inTbl ? 2 : 25) || !/[A-Za-z]{3,}/.test(t)) return;  // need a real word → pure-number cells skipped
      if (inTbl && _isDateish(t)) return;                         // leave date headers (e.g. March 28, 2026) in English
      if (_isHidden(el, win)) return;
      cand.push(el);
    });
    cand = cand.filter(function (el) {
      return !cand.some(function (o) { return o !== el && o.contains(el); });   // keep outermost leaf
    });
    if (!cand.length) return;
    // Attach the always-visible 🔄 chip to every candidate up front so the user
    // can force-refresh any block, translated or not. Cheap DOM op — one span
    // each, ~700 blocks for a typical 10-Q.
    cand.forEach(_secAddReloadBtn);
    var IO = win.IntersectionObserver || window.IntersectionObserver;
    _secObs = new IO(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var el = en.target;
        try { _secObs.unobserve(el); } catch (e) {}
        if (el.getAttribute('data-sec-tr')) return;
        el.setAttribute('data-sec-tr', 'q');
        _secQueue.push(el);
      });
      _scheduleFlush();
    }, { root: null, rootMargin: '300px 0px' });
    cand.forEach(function (el) { _secObs.observe(el); });
    _secCand = cand;
    _secPhase = 0; _secToTranslate = [];           // reset drain state for this doc
    _secStartDrain();                              // 后台把整篇(从前往后)逐步预翻 + 喂满缓存
  };

  function _scheduleFlush() {
    if (_secFlushT) return;
    _secFlushT = setTimeout(function () { _secFlushT = null; _flush(); }, 300);
  }

  function _secInject(el, cn) {
    // Only mark 'done' when we actually injected Chinese — an empty response
    // means the provider gave us back English (Google Translate sometimes
    // returns the source unchanged on short/proper-noun-heavy strings) and
    // we blanked it via _zh_looks_english. Leaving the attribute off lets the
    // reload button (or a later drain pass) try again instead of freezing the
    // block in English forever.
    if (!cn) { el.removeAttribute('data-sec-tr'); return; }
    el.setAttribute('data-sec-tr', 'done');
    var doc = el.ownerDocument;
    if (el.tagName === 'TD' || el.tagName === 'TH') {
      var existing = el.querySelector(':scope > .zh-tr-c');
      if (existing) { existing.textContent = cn; return; }
      var dc = doc.createElement('div');
      dc.className = 'zh-tr-c'; dc.textContent = cn;
      // Insert BEFORE the reload button so the button stays at the bottom.
      var btn = el.querySelector(':scope > .zh-tr-btn');
      if (btn) el.insertBefore(dc, btn); else el.appendChild(dc);
    } else {
      var nx = el.nextSibling;
      if (nx && nx.classList && nx.classList.contains('zh-tr')) { nx.textContent = cn; return; }
      var div = doc.createElement('div');
      div.className = 'zh-tr'; div.textContent = cn;
      if (el.parentNode) el.parentNode.insertBefore(div, el.nextSibling);
    }
  }

  // Translate one batch → inject. Resolves to the server's cache-hit count so the
  // prefetch drainer can speed up when a re-opened filing is already fully cached.
  // `force` bypasses the server cache (reload-button path).
  function _secXlate(batch, force) {
    if (!batch.length) return Promise.resolve(0);
    var texts = batch.map(_elText);
    var body = { ticker: window.__secTicker || '', texts: texts };
    if (force) body.force = true;
    return fetch('/api/sec/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (j) {
      var zh = (j && j.zh) || [];
      batch.forEach(function (el, i) { _secInject(el, zh[i]); });
      return (j && j.cached) || 0;
    }).catch(function () {
      batch.forEach(function (el) { if (el.getAttribute('data-sec-tr') === 'q') el.removeAttribute('data-sec-tr'); });
      return -1;
    });
  }

  // Cache-only sweep — pings the server's disk cache without triggering a real
  // translation call. Cached blocks get their Chinese injected immediately; the
  // returned `misses` list is what the phase-2 drainer will actually translate.
  // Fast (no rate limit — pure disk read), so we can push 40 at a time.
  function _secLookup(batch) {
    if (!batch.length) return Promise.resolve({ misses: [], hits: 0 });
    var texts = batch.map(_elText);
    return fetch('/api/sec/translate/lookup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: window.__secTicker || '', texts: texts })
    }).then(function (r) { return r.json(); }).then(function (j) {
      var zh = (j && j.zh) || [];
      var hits = (j && j.hits) || [];
      var misses = [];
      batch.forEach(function (el, i) {
        if (hits[i] && zh[i]) _secInject(el, zh[i]);
        else misses.push(el);
      });
      return { misses: misses, hits: (j && j.cached) || 0 };
    }).catch(function () {
      // Lookup failure = treat all as misses so phase 2 still tries them.
      return { misses: batch.slice(), hits: 0 };
    });
  }

  // Viewport (scroll) flush — what the user is looking at gets translated first.
  function _flush() {
    if (!_secQueue.length) return;
    var batch = _secQueue.splice(0, 24);
    _secXlate(batch).then(function () { if (_secQueue.length) _scheduleFlush(); });
  }

  // Background prefetch, two phases:
  //   Phase 0 (lookup): sweep every candidate through the cache-only endpoint
  //     in 40-block batches. Previously-viewed filings light up almost instantly.
  //   Phase 1 (translate): whatever missed phase 0 goes into _secToTranslate
  //     and is drained 20 at a time with a gentle 1.8 s delay — provider limit.
  // Splitting the two lets the reader see cached content immediately instead of
  // waiting a full translate round-trip on re-opens, and keeps the translation
  // pass small (only real misses go over the wire).
  function _secStartDrain() {
    _secDraining = true;
    if (_secDrainT) clearTimeout(_secDrainT);
    _secDrainT = setTimeout(_secDrain, 800);        // let the viewport do the first screen first
  }
  function _secStopDrain() {
    _secDraining = false;
    if (_secDrainT) { clearTimeout(_secDrainT); _secDrainT = null; }
  }
  function _secDrain() {
    if (!_secDraining) return;

    if (_secPhase === 0) {
      // Grab the next unprocessed slice for a lookup ping.
      var batch = [];
      for (var k = 0; k < _secCand.length && batch.length < 40; k++) {
        var el = _secCand[k];
        if (el && el.isConnected && !el.getAttribute('data-sec-tr')) {
          el.setAttribute('data-sec-tr', 'lu');
          batch.push(el);
        }
      }
      if (!batch.length) {
        // Nothing left to look up → move to phase 1 (translate misses).
        _secPhase = 1;
        _secDrainT = setTimeout(_secDrain, 200);
        return;
      }
      _secLookup(batch).then(function (res) {
        // Hits are already injected & marked 'done' by _secInject. Anything
        // still tagged 'lu' is a miss — clear the tag and queue it for phase 1.
        batch.forEach(function (el) {
          if (el.getAttribute('data-sec-tr') === 'lu') {
            el.removeAttribute('data-sec-tr');
            _secToTranslate.push(el);
          }
        });
        if (!_secDraining) return;
        _secDrainT = setTimeout(_secDrain, 80);      // lookups are cheap
      });
      return;
    }

    // Phase 1: real translation for cache misses (rate-sensitive).
    var batch2 = [];
    while (batch2.length < 20 && _secToTranslate.length) {
      var e2 = _secToTranslate.shift();
      if (e2 && e2.isConnected && !e2.getAttribute('data-sec-tr')) {
        e2.setAttribute('data-sec-tr', 'q');
        batch2.push(e2);
      }
    }
    if (!batch2.length) { _secDraining = false; return; }
    _secXlate(batch2).then(function () {
      if (!_secDraining) return;
      _secDrainT = setTimeout(_secDrain, 1800);       // gentle — provider rate limit
    });
  }

  // ─── Table of contents (built from the filing's own in-doc #-anchors) ──────
  // Modern 10-Q/10-K carry a TOC of <a href="#id"> links (Part / Item rows).
  // We group them by target id, drop bare page numbers, and render a clickable
  // rail; clicking scrollIntoViews the matching id/name anchor in the iframe.
  window.secBuildToc = function () {
    var d = _secDoc();
    var rail = document.getElementById('secTocList');
    if (!rail) return;
    if (!d || !d.body) { rail.innerHTML = '<div style="padding:14px;color:#6e7681;font-size:12px">无法读取此文件目录。</div>'; return; }
    var order = [], byId = {};
    var links = d.querySelectorAll('a[href^="#"]');
    Array.prototype.forEach.call(links, function (a) {
      var id = (a.getAttribute('href') || '').slice(1);
      if (!id) return;
      var txt = (a.textContent || '').replace(/\s+/g, ' ').trim();
      if (!txt || /^\d{1,4}$/.test(txt)) return;        // drop bare page numbers
      if (!byId[id]) { byId[id] = { id: id, parts: [] }; order.push(byId[id]); }
      if (byId[id].parts.indexOf(txt) === -1) byId[id].parts.push(txt);
    });
    var toc = order.map(function (e) {
      return { id: e.id, label: e.parts.join(' ').replace(/\s+/g, ' ').trim() };
    }).filter(function (e) { return e.label.length > 1 && e.label.length < 160; });
    if (!toc.length) {
      rail.innerHTML = '<div style="padding:14px;color:#6e7681;font-size:12px">这份文件没有可识别的目录。</div>';
      return;
    }
    rail.innerHTML = '<div style="padding:8px 10px;font-size:11px;color:#6e7681;border-bottom:1px solid #21262d;position:sticky;top:0;background:#0d1117">目录 · 点击跳转</div>'
      + toc.map(function (e) {
        var isPart = /^part\s+[ivx]/i.test(e.label);
        var pad = isPart ? '7px 10px;margin-top:2px;font-weight:700;color:#e6edf3;background:#10161f' : '6px 10px 6px 16px;color:#9da7b3';
        return '<div class="sec-toc-item" data-target="' + _esc(e.id) + '" onclick="secTocJump(this)" '
          + 'style="padding:' + pad + ';font-size:' + (isPart ? '12px' : '11px')
          + ';cursor:pointer;border-bottom:1px solid #161b22;border-left:3px solid transparent;line-height:1.35">'
          + _esc(e.label) + '</div>';
      }).join('');
  };

  window.secTocJump = function (el) {
    var d = _secDoc();
    if (!d) return;
    var id = el.getAttribute('data-target');
    var t = null;
    try { t = d.getElementById(id); } catch (e) {}
    if (!t) { try { t = d.querySelector('[name="' + id + '"]'); } catch (e) {} }
    if (t && t.scrollIntoView) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var rail = document.getElementById('secToc');
    if (rail) rail.querySelectorAll('.sec-toc-item').forEach(function (it) {
      it.style.borderLeftColor = (it === el) ? '#58a6ff' : 'transparent';
    });
  };

  if (!document.getElementById('__secTocStyle')) {
    var _ts = document.createElement('style');
    _ts.id = '__secTocStyle';
    _ts.textContent = '.sec-toc-item:hover,.sec-doc-item:hover{background:#1f2a3d !important}';
    document.head.appendChild(_ts);
  }

  // ─── Company profile panel ──────────────────────────────────────────────────
  var _secProfileCache = {};

  window.secToggleProfile = function () {
    var pane = document.getElementById('secProfilePane');
    if (!pane) return;
    var vis = pane.style.display !== 'none';
    if (vis) { pane.style.display = 'none'; _renderProfileBtn(false); return; }
    pane.style.display = '';
    _renderProfileBtn(true);
    var tk = (window.__secTicker || '').toUpperCase();
    if (!tk) { _setProfileBody('请先输入 ticker 并拉取财报'); return; }
    if (_secProfileCache[tk]) { _showProfile(_secProfileCache[tk]); return; }
    _secFetchProfile(tk);
  };

  function _secFetchProfile(tk) {
    _setProfileBody('加载中…');
    fetch('/api/company_profile?ticker=' + encodeURIComponent(tk))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.summary) { _secProfileCache[tk] = d; _showProfile(d); }
        else { _setProfileBody('暂无简介'); _showGenBtn(tk); }
      })
      .catch(function () { _setProfileBody('加载失败'); });
  }

  function _showProfile(d) {
    var meta = document.getElementById('secProfileMeta');
    if (meta) meta.textContent = (d.company || '') + ' · ' + (d.filing_date || '') + ' · 生成 ' + (d.generated_at || '').slice(0, 16);
    _setProfileBody(d.summary);
    var btn = document.getElementById('secProfileRefresh');
    if (btn) btn.style.display = '';
  }

  function _showGenBtn(tk) {
    var body = document.getElementById('secProfileBody');
    if (!body) return;
    body.innerHTML = '<div style="text-align:center;padding:20px"><div style="color:#6e7681;margin-bottom:12px">该 ticker 尚未生成公司简介（基于最新 10-Q）</div>'
      + '<button onclick="secGenProfile()" style="background:#1f6feb;color:#fff;border:none;border-radius:6px;padding:8px 24px;cursor:pointer;font-size:13px;font-weight:600">📋 生成简介</button></div>';
    var btn = document.getElementById('secProfileRefresh');
    if (btn) btn.style.display = 'none';
  }

  window.secGenProfile = function () { secRefreshProfile(); };

  window.secRefreshProfile = function () {
    var tk = (window.__secTicker || '').toUpperCase();
    if (!tk) return;
    _setProfileBody('正在从 SEC 拉取最新 10-Q 并生成中文简介，约需 30-60 秒…');
    var btn = document.getElementById('secProfileRefresh');
    if (btn) btn.style.display = 'none';
    fetch('/api/company_profile/refresh?ticker=' + encodeURIComponent(tk), { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.summary) { _secProfileCache[tk] = d; _showProfile(d); }
        else { _setProfileBody('生成失败（可能该 ticker 没有 10-Q 文件）'); }
      })
      .catch(function (e) { _setProfileBody('生成失败: ' + e.message); });
  };

  function _setProfileBody(text) {
    var el = document.getElementById('secProfileBody');
    if (el) el.textContent = text;
  }

  function _renderProfileBtn(on) {
    var b = document.getElementById('secProfileBtn');
    if (b) { b.style.background = on ? '#1f6feb' : 'transparent'; b.style.color = on ? '#fff' : '#8b949e'; }
  }

  _renderBiBtn(_biOn());
  _setTrBtn();
})();
"""
