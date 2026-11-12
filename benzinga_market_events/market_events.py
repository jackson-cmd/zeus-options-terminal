"""Embedded HTML + JS for the Market Events pane (lives as a Benzinga sub-tab).

Sub-nav switches among 5 REST event types; each renders its own column set.
Mirrors heatmap.py / sec_filings.py / squawk.py (Python r-strings spliced in).
"""

ME_PANE_HTML = r"""
<div id="mePane" style="display:flex;flex-direction:column;height:100%;min-height:0;gap:10px">
  <div class="me-subnav" style="display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid #21262d;padding-bottom:8px">
    <button data-me="option_activity" onclick="meSubTab('option_activity')" class="me-pill">⚡ 异动期权</button>
    <button data-me="ma" onclick="meSubTab('ma')" class="me-pill">🤝 M&amp;A</button>
    <button data-me="ipos" onclick="meSubTab('ipos')" class="me-pill">🆕 IPO</button>
    <button data-me="offerings" onclick="meSubTab('offerings')" class="me-pill">📤 增发</button>
    <button data-me="splits" onclick="meSubTab('splits')" class="me-pill">✂️ 拆股</button>
    <input id="meTickers" type="text" placeholder="过滤 ticker(逗号;留空=全部)"
      onkeydown="if(event.key==='Enter')meReload()"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 10px;font-size:12px;width:170px;text-transform:uppercase;margin-left:auto">
    <input id="meOaPrem" type="text" placeholder="min premium 如 100k/1m" title="只看单笔 premium ≥ 此值(支持 100k / 1m / 数字; 留空=不限)"
      onkeydown="if(event.key==='Enter')meOaApplyFilter()"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 8px;font-size:11px;width:140px;display:none">
    <input id="meOaDte" type="number" min="0" value="60" placeholder="≤天到期" title="只看 DTE ≤ 此值的逐笔(默认 60; 留空=不限)"
      onchange="meOaApplyFilter()" onkeydown="if(event.key==='Enter')meOaApplyFilter()"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 8px;font-size:11px;width:88px;display:none">
    <select id="meRange" onchange="meReload()" title="时间范围"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 6px;font-size:11px">
      <option value="7">最近1周</option>
      <option value="31" selected>最近1月</option>
      <option value="92">最近3月</option>
      <option value="365">最近1年</option>
      <option value="7000">全部</option>
    </select>
    <select id="meSort" onchange="meReload()" title="排序"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 6px;font-size:11px">
      <option value="">默认排序</option>
      <option value="date_desc">时间 新→旧</option>
      <option value="date_asc">时间 旧→新</option>
      <option value="value_desc">规模/市值 高→低</option>
    </select>
    <button onclick="meReload(true)" style="background:#1f6feb;color:#fff;border:none;border-radius:4px;padding:5px 12px;cursor:pointer;font-size:11px;font-weight:600">刷新</button>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <span id="meStatus" style="color:#8b949e;font-size:11px"></span>
  </div>
  <div id="meBody" style="flex:1;min-height:0;overflow:auto;border:1px solid #21262d;border-radius:6px;background:#0d1117">
    <div style="padding:16px;color:#6e7681;font-size:12px">加载中…</div>
  </div>
  <style>
    .me-pill{background:transparent;color:#8b949e;border:1px solid #30363d;padding:6px 14px;border-radius:5px;cursor:pointer;font-size:12px;font-weight:600}
    .me-pill.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
    #meTable{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
    #meTable th{position:sticky;top:0;background:#161b22;color:#8b949e;text-align:left;padding:8px 10px;border-bottom:1px solid #30363d;white-space:nowrap}
    #meTable td{padding:6px 10px;border-bottom:1px solid #161b22;color:#c9d1d9}
    #meTable.compact th{padding:3px 8px;font-size:11px}
    #meTable.compact td{padding:2px 8px;font-size:11px}
  </style>
</div>
"""

ME_JS = r"""
// Market Events pane — 5 REST event types, per-type columns. Window-scoped.
(function () {
  if (window.__meInit) return;
  window.__meInit = true;

  var meKind = 'option_activity';
  var meOaTicker = null;   // option_activity: null=ticker 汇总; 否则=该 ticker 明细
  var meOaView = 'chart';  // 单票下钻视图: 'chart'=只看图 | 'table'=只看逐笔表
  var meOaExp = null;       // null=L1(各到期日) | 到期日字符串=L2(该到期日各行权价)
  var meRows = [];         // 当前类型已拉取的行(facet 前)
  var meOaRows = [];       // 当前 ticker 下钻的订单(facet 前)
  window.meFacets = {};    // {field: {value:true}} 字段过滤选择
  var _meCache = {};        // 客户端缓存: key→{events|rows, count, t}; 切类瞬切, 不再每次空等
  var ME_CACHE_TTL = 60000; // <60s 切回直接用缓存; 更旧则先显缓存、后台刷新
  var _meOaTimer = null;    // 异动期权"今天"视图实时刷新定时器(45s)
  function _meOaPollStop(){ if(_meOaTimer){clearInterval(_meOaTimer); _meOaTimer=null;} }
  function _meOaPollStart(){
    _meOaPollStop();
    _meOaTimer = setInterval(function(){
      if (meKind==='option_activity' && !meOaTicker) meOaSummary(true);   // 强制拉最新
      else _meOaPollStop();
    }, 45000);
  }
  var _meOaDate = null;     // 当前查看日期(默认今天); 今天=实时全市场, 过去=watchlist 归档
  var _meOaLastSig = null;  // 上次渲染的数据签名; 一样就不重渲(避免刷新跳顶/整页刷)
  function _today(){ var d=new Date(),p=function(n){return(n<10?'0':'')+n;}; return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate()); }
  function _daysAgo(n){ var d=new Date(Date.now()-n*864e5),p=function(x){return(x<10?'0':'')+x;}; return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate()); }
  window.meOaPick = function(d){ meOaSummary(true, d); };   // 日期选择器/今天按钮入口(全局)

  // ── premium / 到期日 过滤(逐笔级, 服务端做; summary 与 drill 传同一组值 → 完全一致) ──
  function _meOaParsePrem(){   // 支持 100k / 1m / 500000; 无效/空=0(不限)
    var v=((document.getElementById('meOaPrem')||{}).value||'').trim().toLowerCase().replace(/[$,\s]/g,'');
    if(!v) return 0;
    var m=v.match(/^([0-9]*\.?[0-9]+)([km]?)$/);
    if(!m) return 0;
    var n=parseFloat(m[1])||0;
    if(m[2]==='k') n*=1e3; else if(m[2]==='m') n*=1e6;
    return n>0?n:0;
  }
  function _meOaDteVal(){   // 空/无效=null(不限到期)
    var v=((document.getElementById('meOaDte')||{}).value||'').trim();
    if(v==='') return null;
    var n=parseInt(v,10);
    return (isNaN(n)||n<0)?null:n;
  }
  function _meOaFilterQS(){   // 拼成 &min_premium=..&max_dte=.. (空项不带)
    var p=_meOaParsePrem(), d=_meOaDteVal();
    return (p>0?('&min_premium='+p):'') + (d!=null?('&max_dte='+d):'');
  }
  window.meOaApplyFilter = function(){   // 过滤输入变化: 轻量重取(不走15s归档重刷)
    if (meOaTicker) return meOaDrill(meOaTicker);   // 下钻中 → 用新过滤重拉该票, 逐笔跟着变
    return meOaSummary(false);                       // 汇总 → 新缓存键 → 用新过滤重取
  };

  // 各类型可按钮筛选的分类字段
  var FACETS = {   // 分类多选(chip)
    option_activity: [['C/P','put_call'],['情绪','sentiment'],['类型','option_activity_type']],
    ma:        [['状态','deal_status'],['类型','deal_type']],
    ipos:      [['交易所','exchange'],['状态','deal_status']],
    offerings: [['类型','offering_type'],['交易所','exchange']],
    splits:    [['类型','split_type'],['交易所','exchange']],
  };
  var RANGES = {   // 数值区间筛选 [label, field]: min/max(留空=不限; =X→min=max=X; >X→只填min; 区间→都填)
    option_activity: [['行权价','strike_price'],['规模$','cost_basis']],
  };

  function _esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

  function _num(v){  // big-dollar abbreviation
    var n = parseFloat(v); if (isNaN(n)) return _esc(v);
    var a = Math.abs(n);
    if (a >= 1e9) return (n/1e9).toFixed(2)+'B';
    if (a >= 1e6) return (n/1e6).toFixed(1)+'M';
    if (a >= 1e3) return (n/1e3).toFixed(0)+'K';
    return String(n);
  }
  function _sent(v){ var t=(v||'').toUpperCase();
    var c = t.indexOf('BULL')>=0?'#3fb950':(t.indexOf('BEAR')>=0?'#f85149':'#8b949e');
    return '<span style="color:'+c+';font-weight:600">'+_esc(v||'—')+'</span>'; }

  // column config per kind: [label, field, renderer?]
  var COLS = {
    option_activity: [
      ['日期','date'], ['时间','time'], ['代码','ticker','tk'], ['C/P','put_call'],
      ['情绪','sentiment','sent'], ['行权价','strike_price'], ['到期','date_expiration'],
      ['规模$','cost_basis','num'], ['类型','option_activity_type'],
    ],
    ma: [
      ['日期','date'], ['标的','target_ticker','tk'], ['标的名','target_name'],
      ['收购方','acquirer_name'], ['规模','deal_size','num'], ['类型','deal_type'],
      ['状态','deal_status'],
    ],
    ipos: [
      ['日期','date'], ['代码','ticker','tk'], ['名称','name'],
      ['市值','market_cap_at_offer','num'], ['发行价','price_public_offering'],
      ['募资','offering_value','num'], ['状态','deal_status'], ['交易所','exchange'],
    ],
    offerings: [
      ['日期','date'], ['代码','ticker','tk'], ['名称','name'],
      ['类型','offering_type'], ['价格','price'], ['股数','number_shares','num'],
      ['募资','proceeds','num'],
    ],
    splits: [
      ['除权日','date_ex'], ['代码','ticker','tk'], ['名称','name'],
      ['比例','ratio'], ['类型','split_type'], ['派发日','date_distribution'],
    ],
  };

  function _cell(row, col){
    var v = row[col[1]];
    if (col[2]==='tk')   return '<span style="color:#58a6ff;font-weight:700">'+_esc(v||'—')+'</span>';
    if (col[2]==='num')  return _num(v);
    if (col[2]==='sent') return _sent(v);
    return _esc(v==null||v===''?'—':v);
  }

  function _applyFacets(rows, skipField){
    var fk=Object.keys(window.meFacets||{}).filter(function(f){return f!==skipField;}), rk=Object.keys(window.meRanges||{});
    if(!fk.length && !rk.length) return rows;
    return rows.filter(function(r){
      for(var i=0;i<fk.length;i++){ var f=fk[i]; if(!window.meFacets[f][String(r[f]==null?'':r[f])]) return false; }
      for(var j=0;j<rk.length;j++){ var k=rk[j], g=window.meRanges[k], v=parseFloat(r[k]);
        if(isNaN(v)) return false;
        if(g.min!=null && v<g.min) return false;
        if(g.max!=null && v>g.max) return false;
      }
      return true;
    });
  }

  function _facetBar(rows, kind){
    var defs=FACETS[kind]||[], rdefs=RANGES[kind]||[];
    if(!rows.length) return '';
    var groups=defs.map(function(d){
      var label=d[0], field=d[1], vals={};
      rows.forEach(function(r){ var v=String(r[field]==null?'':r[field]); if(v) vals[v]=(vals[v]||0)+1; });
      var order=Object.keys(vals).sort(function(a,b){return vals[b]-vals[a];}).slice(0,12);
      if(!order.length) return '';
      var sel=window.meFacets[field]||{};
      var chips=order.map(function(v){ var on=!!sel[v];
        return '<button data-v="'+_esc(v)+'" onclick="meFacetToggle(\''+field+'\',this.dataset.v)" '
          +'style="background:'+(on?'#1f6feb':'#161b22')+';color:'+(on?'#fff':'#8b949e')+';border:1px solid '+(on?'#1f6feb':'#30363d')+';border-radius:12px;padding:2px 9px;font-size:11px;cursor:pointer;margin:2px 4px 2px 0">'+_esc(v)+' '+vals[v]+'</button>';
      }).join('');
      return '<div style="display:flex;align-items:center;flex-wrap:wrap"><span style="color:#6e7681;font-size:11px;margin-right:4px">'+_esc(label)+':</span>'+chips+'</div>';
    }).filter(Boolean).join('');
    var inp='background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:2px 6px;font-size:11px;width:66px';
    var rng=rdefs.map(function(d){
      var label=d[0], field=d[1], g=(window.meRanges||{})[field]||{};
      return '<div style="display:flex;align-items:center;gap:3px"><span style="color:#6e7681;font-size:11px">'+_esc(label)+':</span>'
        +'<input type="number" placeholder="min" value="'+(g.min!=null?g.min:'')+'" onchange="meRangeSet(\''+field+'\',\'min\',this.value)" style="'+inp+'">'
        +'<span style="color:#6e7681">–</span>'
        +'<input type="number" placeholder="max" value="'+(g.max!=null?g.max:'')+'" onchange="meRangeSet(\''+field+'\',\'max\',this.value)" style="'+inp+'"></div>';
    }).join('');
    var active=Object.keys(window.meFacets||{}).length || Object.keys(window.meRanges||{}).length;
    var clr=active?'<button onclick="meFacetClear()" style="background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:2px 10px;font-size:11px;cursor:pointer">清除筛选</button>':'';
    if(!groups && !rng) return '';
    return '<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #21262d">'+groups+rng+clr+'</div>';
  }

  window.meFacetToggle = function(field, val){
    var f=window.meFacets[field]||(window.meFacets[field]={});
    if(f[val]) delete f[val]; else f[val]=true;
    if(!Object.keys(f).length) delete window.meFacets[field];
    meRenderCurrent();
  };
  window.meRangeSet = function(field, which, val){   // 数值区间筛选(min/max)
    window.meRanges=window.meRanges||{};
    var g=window.meRanges[field]||(window.meRanges[field]={});
    if(val===''||val==null||isNaN(parseFloat(val))) delete g[which]; else g[which]=parseFloat(val);
    if(g.min==null && g.max==null) delete window.meRanges[field];
    meRenderCurrent();
  };
  window.meFacetClear = function(){ window.meFacets={}; window.meRanges={}; meRenderCurrent(); };

  function meRenderCurrent(){
    if(meKind==='option_activity'){ if(meOaTicker) _renderOaDrill(); return; }
    _renderGenericTable();
  }

  function _render(rows){ meRows = rows||[]; _renderGenericTable(); }

  function _renderGenericTable(){
    var cols=COLS[meKind], body=document.getElementById('meBody'); if(!body) return;
    var rows=_applyFacets(meRows), bar=_facetBar(meRows, meKind);
    if(!rows.length){ body.innerHTML=bar+'<div style="padding:16px;color:#6e7681;font-size:12px">无结果(盘外/该过滤无数据)。</div>'; return; }
    var head='<tr>'+cols.map(function(c){return '<th>'+_esc(c[0])+'</th>';}).join('')+'</tr>';
    var trs=rows.map(function(r){return '<tr>'+cols.map(function(c){return '<td>'+_cell(r,c)+'</td>';}).join('')+'</tr>';}).join('');
    body.innerHTML=bar+'<table id="meTable"><thead>'+head+'</thead><tbody>'+trs+'</tbody></table>';
  }

  window.meReload = function(force){
    if (meKind === 'corp') { if(force) _corpCache=null; return meCorpStart(); }
    if (meKind === 'option_activity') { return meOaLoad(force); }
    var kind = meKind;
    var tk = ((document.getElementById('meTickers')||{}).value || '').trim().toUpperCase();
    var days = (document.getElementById('meRange')||{}).value || '31';
    var sort = (document.getElementById('meSort')||{}).value || '';
    var key = 'ev|'+kind+'|'+days+'|'+sort+'|'+tk;
    var url = '/api/market-events/'+kind+'?limit=500&days_back='+encodeURIComponent(days)
      + (sort?'&sort='+encodeURIComponent(sort):'')
      + (tk?'&tickers='+encodeURIComponent(tk):'');
    var st = document.getElementById('meStatus');
    var c = _meCache[key];
    if (!force && c) {
      _render(c.events);                                 // 瞬间显示缓存
      if (st) st.textContent = c.count + ' 条 · 缓存';
      if (Date.now() - c.t < ME_CACHE_TTL) return;       // 还新 → 不再请求
    } else {
      var body = document.getElementById('meBody');
      if (body) body.innerHTML = '<div style="padding:16px;color:#6e7681;font-size:12px">加载中…</div>';
      if (st) { st.textContent = '加载中…'; st.style.color = '#8b949e'; }
    }
    fetch(url).then(function(r){ return r.json().then(function(j){ if(!r.ok) throw new Error(j.detail||r.statusText); return j; }); })
      .then(function(data){
        _meCache[key] = { events: data.events||[], count: data.count, t: Date.now() };
        if (meKind === kind && !meOaTicker) {            // 仍在本类才渲染(防切走后乱填)
          _render(data.events||[]);
          if (st) st.textContent = data.count + ' 条 · ' + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
        }
      })
      .catch(function(e){ if(meKind===kind && st){st.textContent='失败: '+e.message; st.style.color='#f85149';} });
  };

  // ===== 异动期权:ticker 汇总 → 下钻(图 + 订单) =====
  function _oaDays(){ var d=parseInt((document.getElementById('meRange')||{}).value||'7',10)||7; return Math.min(d,31); }

  window.meOaLoad = function(force){
    if (meOaTicker) return meOaDrill(meOaTicker);
    if (force && (_meOaDate||_today()) >= _today()) return meOaRefresh();  // 今天手动刷新 → 拉完整归档
    return meOaSummary(force);
  };
  function meOaRefresh(){
    var st=document.getElementById('meStatus'); if(st){st.textContent='刷新中(分批拉完整, 约15s)…';st.style.color='#8b949e';}
    fetch('/api/market-events/option_activity/refresh',{method:'POST'})
      .then(function(r){return r.json();}).then(function(){ var pre='oa|'+(_meOaDate||_today())+'|'; Object.keys(_meCache).forEach(function(k){ if(k.indexOf(pre)===0) delete _meCache[k]; }); meOaSummary(true); })
      .catch(function(){ meOaSummary(true); });
  }

  // 今天的异动期权: 左 Bull Flow / 右 Bear Flow, 各按 premium 降序, 不重合。
  function _renderOaSummary(d){
    var q=((document.getElementById('meTickers')||{}).value||'').trim().toUpperCase();
    var st=document.getElementById('meStatus');
    var body=document.getElementById('meBody'); if(!body) return;
    var date=d.date||_today(), isToday=(date>=_today());
    var bull=(d.bull||[]), bear=(d.bear||[]);
    if(q){ var f=function(r){return (r.ticker||'').indexOf(q)>=0;}; bull=bull.filter(f); bear=bear.filter(f); }
    if(st) st.textContent=date+' · '+(d.source==='live'?'全市场采样':'watchlist完整')+' · 看多 '+bull.length+' · 看空 '+bear.length+' 票 · '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})+' · 点击查逐笔';
    var sig=date+'|'+q+'|'+JSON.stringify(bull.map(function(x){return x.ticker+':'+x.count;}))+JSON.stringify(bear.map(function(x){return x.ticker+':'+x.count;}));
    if(sig===_meOaLastSig && document.getElementById('meOaColL')) return;   // 数据未变+已渲染 → 不动 DOM(不整页刷新/不跳顶)
    _meOaLastSig=sig;
    function col(rows, isBull){
      var head='<thead><tr><th>代码</th><th>'+(isBull?'看多$':'看空$')+'</th><th>笔</th><th>C/P</th></tr></thead>';
      var trs=rows.map(function(r){
        var prem=isBull?r.bull_prem:r.bear_prem;
        return '<tr style="cursor:pointer" onclick="meOaDrill(\''+_esc(r.ticker)+'\')">'
          +'<td style="color:#58a6ff;font-weight:700">'+_esc(r.ticker)+'</td>'
          +'<td style="font-weight:600;color:'+(isBull?'#3fb950':'#f85149')+'">'+_num(prem)+'</td>'
          +'<td>'+r.count+'</td><td style="color:#8b949e">'+r.calls+'C/'+r.puts+'P</td></tr>';
      }).join('');
      return rows.length ? ('<table id="meTable">'+head+'<tbody>'+trs+'</tbody></table>')
                         : '<div style="color:#6e7681;padding:12px;font-size:12px">无</div>';
    }
    var picker='<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;font-size:12px;flex-wrap:wrap">'
      +'<span style="color:#8b949e">日期:</span>'
      +'<input type="date" value="'+date+'" min="'+_daysAgo(7)+'" max="'+_today()+'" onchange="meOaPick(this.value)" style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:3px 8px;font-size:12px">'
      +'<button onclick="meOaPick(\''+_today()+'\')" style="background:'+(isToday?'#1f6feb':'#21262d')+';color:'+(isToday?'#fff':'#8b949e')+';border:1px solid #30363d;border-radius:4px;padding:3px 12px;cursor:pointer;font-size:11px">今天</button>'
      +'<span style="color:#6e7681">'+(isToday?'· 实时(全市场~5000笔采样)':'· 归档(watchlist · 仅近一周完整)')+'</span></div>';
    var COLH='max-height:calc(100vh - 295px);overflow:auto;border:1px solid #21262d;border-radius:6px';
    var _sl=(document.getElementById('meOaColL')||{}).scrollTop||0, _sr=(document.getElementById('meOaColR')||{}).scrollTop||0;
    body.innerHTML=picker+'<div style="display:flex;gap:12px;align-items:flex-start">'
      +'<div style="flex:1;min-width:0"><div style="color:#3fb950;font-weight:700;font-size:13px;margin-bottom:6px">🟢 Bull Flow（看多 · 按 premium）</div><div id="meOaColL" style="'+COLH+'">'+col(bull,true)+'</div></div>'
      +'<div style="flex:1;min-width:0"><div style="color:#f85149;font-weight:700;font-size:13px;margin-bottom:6px">🔴 Bear Flow（看空 · 按 premium）</div><div id="meOaColR" style="'+COLH+'">'+col(bear,false)+'</div></div>'
      +'</div>';
    var _e; _e=document.getElementById('meOaColL'); if(_e)_e.scrollTop=_sl; _e=document.getElementById('meOaColR'); if(_e)_e.scrollTop=_sr;
  }

  function meOaSummary(force, date){
    var today=_today();
    date = date || _meOaDate || today;
    _meOaDate = date;
    var isToday = (date >= today);
    if (isToday) _meOaPollStart(); else _meOaPollStop();   // 只今天实时刷新, 过去是静态归档
    var key='oa|'+date+'|'+_meOaParsePrem()+'|'+(_meOaDteVal()==null?'':_meOaDteVal());   // 过滤值进键, 否则改过滤显旧缓存
    var st=document.getElementById('meStatus');
    var c=_meCache[key];
    if (!force && c) {
      _renderOaSummary(c.data);                          // 瞬间显示缓存
      if (Date.now() - c.t < ME_CACHE_TTL) return;
    } else if (!document.getElementById('meOaColL')) {   // 仅冷加载显「加载中」; 后台刷新静默(保留滚动, 不清屏)
      var body=document.getElementById('meBody');
      if (body) body.innerHTML='<div style="padding:16px;color:#6e7681;font-size:12px">加载中…</div>';
      if(st){st.textContent='加载中…';st.style.color='#8b949e';}
    }
    var _qs=(isToday?'':('date='+encodeURIComponent(date)));
    var _fq=_meOaFilterQS().replace(/^&/,'');   // 去掉前导& 以便和 date 拼
    if(_fq) _qs = _qs ? (_qs+'&'+_fq) : _fq;
    var url='/api/market-events/option_activity/summary'+(_qs?('?'+_qs):'');
    fetch(url)
      .then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.detail||r.statusText);return j;});}).then(function(d){
        _meCache[key]={data:d, t:Date.now()};
        if (meKind==='option_activity' && !meOaTicker && _meOaDate===date) _renderOaSummary(d);
      }).catch(function(e){ if(st){st.textContent='失败: '+e.message;st.style.color='#f85149';} });
  }

  // 通用水平条形图行(左红 / 右绿, 中分隔)。紧凑, 一次看很多根。
  function _oaBarRow(label, leftVal, rightVal, mx, n, opts){
    opts=opts||{};
    var lw=(100*leftVal/mx).toFixed(1), rw=(100*rightVal/mx).toFixed(1);
    var net=rightVal>=leftVal
      ? '<span style="color:#3fb950">'+opts.rTag+' '+_num(rightVal)+'</span>'
      : '<span style="color:#f85149">'+opts.lTag+' '+_num(leftVal)+'</span>';
    var click=opts.onclick?(' onclick="'+opts.onclick+'" title="'+(opts.title||'')+'" style="cursor:pointer;'):'  style="';
    return '<div'+click+'display:flex;align-items:center;gap:6px;font-size:11px;height:15px;border-radius:3px;padding:0 3px"'
      +(opts.onclick?' onmouseover="this.style.background=\'#161b22\'" onmouseout="this.style.background=\'transparent\'"':'')+'>'
      +'<span style="width:82px;color:#8b949e;text-align:right;flex-shrink:0;white-space:nowrap;overflow:hidden">'+_esc(label)+'</span>'
      +'<div style="flex:1;display:flex;justify-content:flex-end"><div title="'+opts.lTag+' '+_num(leftVal)+' · '+n+'笔" style="height:10px;width:'+lw+'%;min-width:'+(leftVal>0?'2px':'0')+';background:#f85149;border-radius:2px 0 0 2px"></div></div>'
      +'<div style="width:1px;height:11px;background:#484f58;flex-shrink:0"></div>'
      +'<div style="flex:1;display:flex"><div title="'+opts.rTag+' '+_num(rightVal)+' · '+n+'笔" style="height:10px;width:'+rw+'%;min-width:'+(rightVal>0?'2px':'0')+';background:#3fb950;border-radius:0 2px 2px 0"></div></div>'
      +'<span style="width:84px;flex-shrink:0">'+net+'</span></div>';
  }

  // L1 图: 各到期日 看多/看空(紧凑柱, 容器大 → 一次看很多个到期日; 点柱进入该到期日)
  function _oaChartByExp(orders){
    if(!orders.length) return '<div style="color:#6e7681;padding:12px;font-size:12px">无数据</div>';
    var agg={};
    orders.forEach(function(o){
      var e=o.date_expiration||'?', cb=Math.max(0,+o.cost_basis||0), s=(o.sentiment||'').toUpperCase();
      var a=agg[e]||(agg[e]={k:e,bull:0,bear:0,n:0}); a.n++;
      if(s.indexOf('BULL')>=0) a.bull+=cb; else if(s.indexOf('BEAR')>=0) a.bear+=cb;
    });
    var arr=Object.keys(agg).map(function(k){return agg[k];}).sort(function(x,y){return (x.k||'').localeCompare(y.k||'');});
    var mx=Math.max.apply(null, arr.map(function(a){return Math.max(a.bull,a.bear);}).concat([1]));
    var hint='<div style="color:#8b949e;font-size:11px;margin-bottom:6px">'+_esc(meOaTicker)+' 各到期日多空力量(按 premium) · <span style="color:#f85149">←看空</span> | <span style="color:#3fb950">看多→</span> · 点柱进入该到期日看各行权价</div>';
    return hint+arr.map(function(a){
      return _oaBarRow(a.k, a.bear, a.bull, mx, a.n,
        {lTag:'空', rTag:'多', onclick:"meOaExpPick('"+_esc(a.k)+"')", title:'进入 '+_esc(a.k)});
    }).join('');
  }

  // L2 图: 单个到期日 各行权价 call/put(按 premium)
  function _oaChartByStrike(orders){
    if(!orders.length) return '<div style="color:#6e7681;padding:12px;font-size:12px">无数据</div>';
    var agg={};
    orders.forEach(function(o){
      var k=(o.strike_price==null||o.strike_price==='')?'?':o.strike_price, cb=Math.max(0,+o.cost_basis||0);
      var a=agg[k]||(agg[k]={k:k,call:0,put:0,n:0}); a.n++;
      if(String(o.put_call||'').toUpperCase().indexOf('C')===0) a.call+=cb; else a.put+=cb;
    });
    var arr=Object.keys(agg).map(function(k){return agg[k];}).sort(function(x,y){return (parseFloat(x.k)||0)-(parseFloat(y.k)||0);});
    var mx=Math.max.apply(null, arr.map(function(a){return Math.max(a.call,a.put);}).concat([1]));
    var hint='<div style="color:#8b949e;font-size:11px;margin-bottom:6px">'+_esc(meOaTicker)+' · '+_esc(meOaExp)+' 各行权价 call/put(按 premium) · <span style="color:#f85149">←Put</span> | <span style="color:#3fb950">Call→</span></div>';
    return hint+arr.map(function(a){
      return _oaBarRow(a.k, a.put, a.call, mx, a.n, {lTag:'P', rTag:'C'});
    }).join('');
  }

  window.meOaExpPick = function(exp){ meOaExp=exp; meOaView='chart'; window.meOaSort=null; _renderOaDrill(); };  // 进入该到期日(L2)
  window.meOaExpClear = function(){ meOaExp=null; meOaView='chart'; window.meOaSort=null; _renderOaDrill(); };      // 回到全部到期日(L1)
  window.meOaSetView = function(v){ meOaView = (v==='chart')?'chart':'table'; _renderOaDrill(); };

  window.meOaDrill = function(ticker){
    _meOaPollStop();           // 下钻单票 → 停实时刷新(返回汇总再开)
    meOaTicker=ticker; meOaExp=null; window.meFacets={}; window.meRanges={}; window.meOaSort=null; meOaView='chart';
    var st=document.getElementById('meStatus'); if(st){st.textContent=ticker+' 加载中…';st.style.color='#8b949e';}
    var dt=_meOaDate||_today();
    fetch('/api/market-events/option_activity/ticker?ticker='+encodeURIComponent(ticker)+'&date='+encodeURIComponent(dt)+_meOaFilterQS())
      .then(function(r){return r.json();}).then(function(d){
        meOaRows=d.events||[]; if(st) st.textContent=ticker+' · '+meOaRows.length+' 笔'+(_meOaParsePrem()>0||_meOaDteVal()!=null?'(过滤后)':'(完整)');
        _renderOaDrill();
      }).catch(function(e){ if(st){st.textContent='失败: '+e.message;st.style.color='#f85149';} });
  };

  function _renderOaDrill(){
    var body=document.getElementById('meBody'); if(!body) return;
    // L2 限定到该到期日
    var scope = meOaExp ? meOaRows.filter(function(r){return String(r.date_expiration||'')===meOaExp;}) : meOaRows;
    var rows=_applyFacets(scope), cols=COLS.option_activity;
    var so=window.meOaSort;
    if(so && so.field){ var f=so.field, dir=(so.dir==='asc')?1:-1;
      rows=rows.slice().sort(function(a,b){ var nx=parseFloat(a[f]), ny=parseFloat(b[f]);
        if(!isNaN(nx)&&!isNaN(ny)) return (nx-ny)*dir;
        return String(a[f]==null?'':a[f]).localeCompare(String(b[f]==null?'':b[f]))*dir; }); }
    var head='<tr>'+cols.map(function(c){
      var act=so&&so.field===c[1], ar=act?(so.dir==='asc'?' ▲':' ▼'):'';
      return '<th onclick="meOaSortBy(\''+c[1]+'\')" style="cursor:pointer;user-select:none" title="点击排序">'+_esc(c[0])+ar+'</th>';
    }).join('')+'</tr>';
    var trs=rows.map(function(r){return '<tr>'+cols.map(function(c){return '<td>'+_cell(r,c)+'</td>';}).join('')+'</tr>';}).join('');
    // 图/表 视图切换 → 只看一个, 占据更大空间
    function vbtn(v,lbl){ var on=meOaView===v;
      return '<button onclick="meOaSetView(\''+v+'\')" style="background:'+(on?'#1f6feb':'transparent')+';color:'+(on?'#fff':'#8b949e')+';border:none;padding:3px 12px;font-size:11px;cursor:pointer;font-weight:600">'+lbl+'</button>'; }
    var vt='<span style="display:inline-flex;border:1px solid #30363d;border-radius:5px;overflow:hidden;margin-left:10px">'+vbtn('chart','📊 图')+vbtn('table','📋 表')+'</span>';
    // 面包屑: 标的 [› 到期日 ← 全部到期日]
    var crumb='<button onclick="meOaBack()" style="background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:12px">← 返回标的</button>'
      +'<strong style="color:#58a6ff;margin-left:4px">'+_esc(meOaTicker)+'</strong>';
    if(meOaExp){ crumb+='<span style="color:#8b949e">›</span><strong style="color:#58a6ff">'+_esc(meOaExp)+'</strong>'
      +'<button onclick="meOaExpClear()" style="background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:3px 10px;cursor:pointer;font-size:11px;margin-left:2px">← 全部到期日</button>'; }
    crumb+='<span style="color:#8b949e;font-size:11px">'+rows.length+'/'+scope.length+' 笔</span>'+vt;
    var header='<div style="margin-bottom:6px;display:flex;align-items:center;flex-wrap:wrap;gap:6px">'+crumb+'</div>';
    var facet=_facetBar(scope,'option_activity');
    if(meOaView==='chart'){
      var chartHtml = meOaExp ? _oaChartByStrike(_applyFacets(scope)) : _oaChartByExp(_applyFacets(meOaRows));
      body.innerHTML=header+facet+'<div id="meOaChart" style="background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px 10px;max-height:calc(100vh - 290px);overflow:auto">'+chartHtml+'</div>';
    } else {
      var _st=(document.getElementById('meOaTbl')||{}).scrollTop||0;
      body.innerHTML=header+facet
        +'<div id="meOaTbl" style="max-height:calc(100vh - 290px);overflow:auto;border:1px solid #21262d;border-radius:6px"><table id="meTable" class="compact"><thead>'+head+'</thead><tbody>'+trs+'</tbody></table></div>';
      var _e=document.getElementById('meOaTbl'); if(_e)_e.scrollTop=_st;
    }
  }

  window.meOaSortBy = function(field){   // 点表头排序: 同列切升/降, 换列默认降序
    var s=window.meOaSort||{};
    window.meOaSort = (s.field===field) ? {field:field, dir:(s.dir==='asc'?'desc':'asc')} : {field:field, dir:'desc'};
    _renderOaDrill();
  };
  window.meOaBack = function(){ meOaTicker=null; meOaExp=null; meOaSummary(); };

  window.meSubTab = function(kind){
    _meOaPollStop();           // 切类 → 停掉异动期权实时刷新(进 OA 时 meOaSummary 再开)
    meKind = kind;
    meOaTicker = null;
    window.meFacets = {};
    document.querySelectorAll('.me-subnav .me-pill').forEach(function(b){
      b.classList.toggle('active', b.dataset.me === kind);
    });
    var isOA=(kind==='option_activity');   // 异动期权用日期选择器, 隐藏"最近N月/排序"(逐笔不适用)
    ['meRange','meSort'].forEach(function(id){ var el=document.getElementById(id); if(el) el.style.display=isOA?'none':''; });
    ['meOaPrem','meOaDte'].forEach(function(id){ var el=document.getElementById(id); if(el) el.style.display=isOA?'':'none'; });   // premium/到期日 仅异动期权
    meReload();   // meReload 自己决定: 有缓存瞬切, 无缓存才显「加载中…」
  };

  window.meStart = function(){ meSubTab(meKind); };

  // ===== 合并企业事件(M&A/IPO/增发/拆股) 统一表 =====
  var _corpRows = [];
  var _corpCache = null;   // {rows, t}
  var _corpFacets = {};
  var _corpSort = null;

  var CORP_COLS = [
    ['类型','_type_label'],
    ['日期','_sort_date'],
    ['代码','_ticker','tk'],
    ['名称','_name'],
    ['详情','_detail'],
    ['规模/金额','_value','num'],
    ['状态','_status'],
  ];

  function _corpNormalize(rows){
    return rows.map(function(r){
      var t=r._type||'';
      var row = Object.assign({}, r);
      if(t==='ma'){
        row._ticker = r.target_ticker||'';
        row._name = r.target_name||'';
        row._detail = (r.acquirer_name?'收购方: '+r.acquirer_name:'')+(r.deal_type?' · '+r.deal_type:'');
        row._value = r.deal_size||'';
        row._status = r.deal_status||'';
      } else if(t==='ipos'){
        row._ticker = r.ticker||'';
        row._name = r.company_name||r.name||'';
        row._detail = r.exchange||'';
        row._value = r.market_cap_at_offer||r.offering_value||'';
        row._status = r.deal_status||'';
      } else if(t==='offerings'){
        row._ticker = r.ticker||'';
        row._name = r.company_name||r.name||'';
        row._detail = r.offering_type||'';
        row._value = r.proceeds||'';
        row._status = r.deal_status||'';
      } else if(t==='splits'){
        row._ticker = r.ticker||'';
        row._name = r.company_name||r.name||'';
        row._detail = (r.ratio?'比例 '+r.ratio:'')+(r.split_type?' · '+r.split_type:'');
        row._value = '';
        row._status = '';
      }
      return row;
    });
  }

  function _corpFacetBar(rows){
    if(!rows.length) return '';
    var vals={};
    rows.forEach(function(r){ var v=r._type_label||''; if(v) vals[v]=(vals[v]||0)+1; });
    var order=Object.keys(vals).sort(function(a,b){return vals[b]-vals[a];});
    var sel=_corpFacets['_type_label']||{};
    var chips=order.map(function(v){ var on=!!sel[v];
      return '<button data-v="'+_esc(v)+'" onclick="corpFacetToggle(this.dataset.v)" '
        +'style="background:'+(on?'#1f6feb':'#161b22')+';color:'+(on?'#fff':'#8b949e')+';border:1px solid '+(on?'#1f6feb':'#30363d')+';border-radius:12px;padding:2px 9px;font-size:11px;cursor:pointer;margin:2px 4px 2px 0">'+_esc(v)+' '+vals[v]+'</button>';
    }).join('');
    var active=Object.keys(_corpFacets).length;
    var clr=active?'<button onclick="corpFacetClear()" style="background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:2px 10px;font-size:11px;cursor:pointer">清除</button>':'';
    return '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #21262d"><span style="color:#6e7681;font-size:11px;margin-right:4px">类型:</span>'+chips+clr+'</div>';
  }

  window.corpFacetToggle = function(val){
    var f=_corpFacets['_type_label']||({}); if(!_corpFacets['_type_label']) _corpFacets['_type_label']=f;
    if(f[val]) delete f[val]; else f[val]=true;
    if(!Object.keys(f).length) delete _corpFacets['_type_label'];
    _renderCorpTable();
  };
  window.corpFacetClear = function(){ _corpFacets={}; _renderCorpTable(); };

  window.corpSortBy = function(field){
    var s=_corpSort||{};
    _corpSort = (s.field===field) ? {field:field, dir:(s.dir==='asc'?'desc':'asc')} : {field:field, dir:'desc'};
    _renderCorpTable();
  };

  function _renderCorpTable(){
    var body=document.getElementById('meBody'); if(!body) return;
    var rows=_corpRows.slice();
    var fk=Object.keys(_corpFacets);
    if(fk.length){ rows=rows.filter(function(r){ for(var i=0;i<fk.length;i++){ var f=fk[i]; if(!_corpFacets[f][String(r[f]||'')]) return false; } return true; }); }
    var so=_corpSort;
    if(so && so.field){ var f=so.field, dir=(so.dir==='asc')?1:-1;
      rows=rows.slice().sort(function(a,b){ var nx=parseFloat(a[f]), ny=parseFloat(b[f]);
        if(!isNaN(nx)&&!isNaN(ny)) return (nx-ny)*dir;
        return String(a[f]==null?'':a[f]).localeCompare(String(b[f]==null?'':b[f]))*dir; }); }
    var bar=_corpFacetBar(_corpRows);
    if(!rows.length){ body.innerHTML=bar+'<div style="padding:16px;color:#6e7681;font-size:12px">无结果。</div>'; return; }
    var head='<tr>'+CORP_COLS.map(function(c){
      var act=so&&so.field===c[1], ar=act?(so.dir==='asc'?' ▲':' ▼'):'';
      return '<th onclick="corpSortBy(\''+c[1]+'\')" style="cursor:pointer;user-select:none">'+_esc(c[0])+ar+'</th>';
    }).join('')+'</tr>';
    var TYPE_COLORS = {'🤝 M&A':'#d2a8ff','🆕 IPO':'#7ee787','📤 增发':'#f0883e','✂️ 拆股':'#79c0ff'};
    var trs=rows.map(function(r){
      return '<tr>'+CORP_COLS.map(function(c){
        var v=r[c[1]];
        if(c[1]==='_type_label'){ var clr=TYPE_COLORS[v]||'#8b949e'; return '<td><span style="color:'+clr+';font-weight:600;font-size:11px;white-space:nowrap">'+_esc(v||'—')+'</span></td>'; }
        if(c[2]==='tk')  return '<td><span style="color:#58a6ff;font-weight:700">'+_esc(v||'—')+'</span></td>';
        if(c[2]==='num') return '<td>'+_num(v)+'</td>';
        return '<td>'+_esc(v==null||v===''?'—':v)+'</td>';
      }).join('')+'</tr>';
    }).join('');
    body.innerHTML=bar+'<table id="meTable"><thead>'+head+'</thead><tbody>'+trs+'</tbody></table>';
    var st=document.getElementById('meStatus'); if(st) st.textContent=rows.length+'/'+_corpRows.length+' 条';
  }

  window.meCorpStart = function(){
    meKind='corp'; meOaTicker=null; _corpFacets={}; _corpSort=null;
    var tk=((document.getElementById('meTickers')||{}).value||'').trim().toUpperCase();
    var days=(document.getElementById('meRange')||{}).value||'31';
    var sort=(document.getElementById('meSort')||{}).value||'';
    // 显示 range/sort、隐藏 OA 专属控件
    ['meRange','meSort'].forEach(function(id){ var el=document.getElementById(id); if(el) el.style.display=''; });
    ['meOaPrem','meOaDte'].forEach(function(id){ var el=document.getElementById(id); if(el) el.style.display='none'; });
    var st=document.getElementById('meStatus');
    var body=document.getElementById('meBody');
    if(_corpCache && (Date.now()-_corpCache.t)<60000){
      _corpRows=_corpCache.rows; _renderCorpTable(); return;
    }
    if(body) body.innerHTML='<div style="padding:16px;color:#6e7681;font-size:12px">加载中…</div>';
    if(st){st.textContent='加载中…';st.style.color='#8b949e';}
    var url='/api/market-events/corp/all?limit=500&days_back='+encodeURIComponent(days)
      +(sort?'&sort='+encodeURIComponent(sort):'')
      +(tk?'&tickers='+encodeURIComponent(tk):'');
    fetch(url).then(function(r){return r.json();}).then(function(d){
      _corpRows=_corpNormalize(d.events||[]);
      _corpCache={rows:_corpRows, t:Date.now()};
      _renderCorpTable();
      if(st) st.textContent=_corpRows.length+' 条 · '+new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    }).catch(function(e){ if(st){st.textContent='失败: '+e.message;st.style.color='#f85149';} });
  };
})();
"""
