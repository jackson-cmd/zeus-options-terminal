"""Embedded HTML and JS for the History News tab.

Mirrors strategy_builder.py — two Python r-strings exported for
optionstrat_server.py to splice into the single-page HTML response
and the JS injection block, respectively.
"""

HISTORY_NEWS_HTML = r"""
<div id="tab-history-news" class="top-tab-panel" style="overflow-y:auto;padding:14px">
  <!-- Merged News sub-nav: Radar | History (History active here) -->
  <div class="news-subnav" style="display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid #21262d;padding-bottom:8px">
    <button onclick="bzNewsView('radar')" style="background:transparent;color:#8b949e;border:1px solid #30363d;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600">📊 News Radar</button>
    <button onclick="bzNewsView('history')" style="background:#1f6feb;color:#fff;border:none;padding:6px 16px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600">📜 History News</button>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px">
    <input id="hnTicker" type="text" placeholder="Ticker (e.g. AAPL)"
      style="width:120px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;
             border-radius:4px;padding:4px 8px;font-size:12px;text-transform:uppercase"
      maxlength="5" oninput="this.value=this.value.toUpperCase()"
      onkeydown="if(event.key==='Enter')hnSearch(0)">
    <input id="hnKeyword" type="text" placeholder="Title keyword (optional)"
      style="width:220px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;
             border-radius:4px;padding:4px 8px;font-size:12px"
      maxlength="100"
      onkeydown="if(event.key==='Enter')hnSearch(0)">
    <input id="hnSince" type="date"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;
             border-radius:4px;padding:4px 6px;font-size:12px"
      title="From date (UTC)">
    <input id="hnUntil" type="date"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;
             border-radius:4px;padding:4px 6px;font-size:12px"
      title="To date (UTC)">
    <select id="hnSent"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;
             border-radius:4px;padding:4px 6px;font-size:12px">
      <option value="any">Any sentiment</option>
      <option value="bull">Bullish</option>
      <option value="bear">Bearish</option>
      <option value="neutral">Neutral</option>
    </select>
    <button onclick="hnSearch(0)"
      style="background:#1f6feb;color:#fff;border:none;border-radius:4px;
             padding:4px 14px;cursor:pointer;font-size:12px;font-weight:600">
      Search
    </button>
    <button onclick="hnReset()"
      style="background:transparent;color:#8b949e;border:1px solid #30363d;
             border-radius:4px;padding:4px 10px;cursor:pointer;font-size:12px">
      Reset
    </button>
    <!-- Translation toggle — DOM slot reserved, hidden in v1 -->
    <div id="hnLangToggle" style="display:none;margin-left:auto">
      <button data-lang="en">EN</button><button data-lang="zh">中</button><button data-lang="both">双语</button>
    </div>
  </div>
  <div id="hnStatus" style="font-size:12px;color:#8b949e;margin-bottom:8px">
    Enter a ticker or keyword above and press Search.
  </div>
  <div id="hnList"></div>
  <div id="hnPager" style="display:flex;gap:8px;justify-content:center;margin-top:14px"></div>
</div>
"""

HISTORY_NEWS_JS = r"""
// History News tab — vanilla JS, window-scoped state, no framework
window.hnState = { lastQuery: null, items: {}, total: 0, offset: 0, limit: 50 };

function hnTabBecameActive() {
  // Focus the ticker input the first time the tab opens
  const t = document.getElementById('hnTicker');
  if (t && !window.hnState.lastQuery) t.focus();
}

function hnReset() {
  ['hnTicker','hnKeyword','hnSince','hnUntil'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('hnSent').value = 'any';
  document.getElementById('hnList').innerHTML = '';
  document.getElementById('hnPager').innerHTML = '';
  document.getElementById('hnStatus').textContent = 'Enter a ticker or keyword above and press Search.';
  window.hnState = { lastQuery: null, items: {}, total: 0, offset: 0, limit: 50 };
}

async function hnSearch(offset) {
  offset = offset || 0;
  const ticker  = document.getElementById('hnTicker').value.trim();
  const keyword = document.getElementById('hnKeyword').value.trim();
  const since   = document.getElementById('hnSince').value;
  const until   = document.getElementById('hnUntil').value;
  const sent    = document.getElementById('hnSent').value;

  if (!ticker && !keyword) {
    document.getElementById('hnStatus').innerHTML =
      '<span style="color:#f85149">Need a ticker or a keyword.</span>';
    return;
  }

  const params = new URLSearchParams();
  if (ticker)  params.set('ticker', ticker);
  if (keyword) params.set('keyword', keyword);
  if (since)   params.set('since', since);
  if (until)   params.set('until', until);
  if (sent && sent !== 'any') params.set('sentiment', sent);
  params.set('offset', offset);
  params.set('limit', window.hnState.limit);

  document.getElementById('hnStatus').textContent = 'Searching…';
  try {
    const r = await fetch('/api/history-news/search?' + params.toString());
    if (!r.ok) {
      const err = await r.json().catch(() => ({detail: r.statusText}));
      document.getElementById('hnStatus').innerHTML =
        '<span style="color:#f85149">Error: ' + (err.detail || 'request failed') + '</span>';
      return;
    }
    const payload = await r.json();
    window.hnState.lastQuery = params.toString();
    window.hnState.total = payload.total;
    window.hnState.offset = payload.offset;
    payload.items.forEach(it => { window.hnState.items[it.id] = it; });
    hnRender(payload);
  } catch (e) {
    document.getElementById('hnStatus').innerHTML =
      '<span style="color:#f85149">Network error: ' + e.message + '</span>';
  }
}

function hnRender(payload) {
  const list = document.getElementById('hnList');
  const pager = document.getElementById('hnPager');
  const status = document.getElementById('hnStatus');

  const note = payload.note ? payload.note + ' — ' : '';
  if (payload.total === 0) {
    status.textContent = note + 'No matches. Try a different ticker or remove the keyword.';
    list.innerHTML = '';
    pager.innerHTML = '';
    return;
  }

  const pageNum = Math.floor(payload.offset / payload.limit) + 1;
  const pageMax = Math.ceil(payload.total / payload.limit);
  status.textContent = note + payload.total.toLocaleString() + ' results — page ' + pageNum + ' of ' + pageMax;

  const sentDot = (s) => {
    if (s > 0)  return '<span style="color:#3fb950" title="bullish '+s+'">●</span>';
    if (s < 0)  return '<span style="color:#f85149" title="bearish '+s+'">●</span>';
    return       '<span style="color:#6e7681" title="neutral">●</span>';
  };

  const rows = payload.items.map(it => {
    const dt = it.published_at.replace('T',' ').replace('Z','');
    const chips = (it.tickers || []).slice(0, 4).map(t =>
      '<span onclick="event.stopPropagation();document.getElementById(\'hnTicker\').value=\''+t+'\';hnSearch(0)" '+
      'style="background:#21262d;color:#58a6ff;border-radius:3px;padding:1px 6px;'+
      'font-size:10px;margin-right:4px;cursor:pointer">'+t+'</span>').join('');
    return '<div class="hn-row" data-id="'+it.id+'" onclick="hnExpand('+it.id+', this)" '+
           'style="border-bottom:1px solid #21262d;padding:8px 4px;cursor:pointer;line-height:1.4">'+
           '<div style="display:flex;gap:10px;align-items:flex-start">'+
             '<span style="color:#8b949e;font-size:11px;white-space:nowrap;min-width:120px">'+dt+'</span>'+
             '<span>'+sentDot(it.sentiment)+'</span>'+
             '<span style="flex:1">'+chips+'<span style="color:#e6edf3">'+escapeHtml(it.title)+'</span></span>'+
           '</div>'+
           '<div class="hn-body" data-id="'+it.id+'" style="display:none;margin:8px 0 4px 130px;'+
             'padding:10px 14px;background:#0d1117;border-left:2px solid #1f6feb;border-radius:4px;'+
             'white-space:pre-wrap;color:#c9d1d9;font-size:12.5px;line-height:1.55"></div>'+
           '</div>';
  }).join('');
  list.innerHTML = rows;

  // Pager
  const btn = (label, target, disabled) =>
    '<button onclick="hnSearch('+target+')" '+(disabled?'disabled':'')+' '+
    'style="background:'+(disabled?'#161b22':'#1f6feb')+';color:'+(disabled?'#484f58':'#fff')+';'+
    'border:none;border-radius:4px;padding:4px 12px;cursor:'+(disabled?'not-allowed':'pointer')+';'+
    'font-size:12px">'+label+'</button>';
  const prevOff = Math.max(0, payload.offset - payload.limit);
  const nextOff = payload.offset + payload.limit;
  pager.innerHTML =
    btn('← Prev', prevOff, payload.offset === 0) +
    btn('Next →', nextOff, !payload.has_more);
}

async function hnExpand(id, rowEl) {
  const body = rowEl.querySelector('.hn-body[data-id="' + id + '"]');
  if (!body) return;
  if (body.style.display === 'block') {
    body.style.display = 'none';
    return;
  }
  if (body.dataset.loaded === '1') {
    body.style.display = 'block';
    return;
  }
  body.textContent = 'Loading…';
  body.style.display = 'block';
  try {
    const r = await fetch('/api/history-news/article/' + id);
    if (!r.ok) {
      body.innerHTML = '<span style="color:#f85149">Could not load article.</span>';
      return;
    }
    const art = await r.json();
    body.textContent = art.body || '(empty body)';
    body.dataset.loaded = '1';
  } catch (e) {
    body.innerHTML = '<span style="color:#f85149">Network error.</span>';
  }
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
"""
