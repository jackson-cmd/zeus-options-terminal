"""Embedded HTML + JS for the Signals real-time pane (a Benzinga sub-tab).

Polls /api/signals/recent (a server-side ring buffer fed by ONE persistent WS).
The row renderer is ADAPTIVE — it resolves common field-name variants and falls
back to raw JSON — because the exact live signal-event shape couldn't be
captured off-hours; this displays correctly whatever Benzinga pushes intraday.
"""

SIGNALS_PANE_HTML = r"""
<div id="sigPane" style="display:flex;flex-direction:column;height:100%;min-height:0;gap:10px">
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <strong style="color:#e6edf3;font-size:14px">📡 Signals 实时</strong>
    <span id="sigConn" style="font-size:11px;color:#8b949e">连接中…</span>
    <input id="sigFilter" type="text" placeholder="过滤 ticker / 关键词"
      oninput="sigRender()"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 10px;font-size:12px;width:200px;text-transform:uppercase">
    <select id="sigAuto" onchange="sigRestart()"
      style="background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 6px;font-size:11px">
      <option value="3000" selected>3s</option>
      <option value="5000">5s</option>
      <option value="10000">10s</option>
      <option value="0">手动</option>
    </select>
    <button onclick="sigPoll()" style="background:#1f6feb;color:#fff;border:none;border-radius:4px;padding:5px 12px;cursor:pointer;font-size:11px;font-weight:600">刷新</button>
    <span id="sigStatus" style="color:#8b949e;font-size:11px;margin-left:auto"></span>
  </div>
  <div id="sigBody" style="flex:1;min-height:0;overflow:auto;border:1px solid #21262d;border-radius:6px;background:#0d1117">
    <div style="padding:16px;color:#6e7681;font-size:12px">连接信号流…</div>
  </div>
  <style>
    #sigTable{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
    #sigTable th{position:sticky;top:0;background:#161b22;color:#8b949e;text-align:left;padding:7px 10px;border-bottom:1px solid #30363d;white-space:nowrap}
    #sigTable td{padding:6px 10px;border-bottom:1px solid #161b22;color:#c9d1d9}
  </style>
</div>
"""

SIGNALS_JS = r"""
// Signals real-time pane. Polls the server ring buffer; adaptive row renderer.
(function () {
  if (window.__sigInit) return;
  window.__sigInit = true;

  var sigData = [];
  var sigTimer = null;

  function _esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

  // Resolve a field from the event or its payload, trying name variants.
  function _f(ev){
    var p = (ev && ev.payload && typeof ev.payload==='object') ? ev.payload : {};
    function pick(){ for (var i=0;i<arguments.length;i++){ var k=arguments[i];
      if (p[k]!=null && p[k]!=='') return p[k]; if (ev[k]!=null && ev[k]!=='') return ev[k]; } return null; }
    var tms = ev._recv_ms ? new Date(ev._recv_ms) : null;
    return {
      symbol: pick('symbol','ticker','sym','Symbol'),
      signal: pick('signal','signalType','signal_type','kind','type','name','Signal'),
      dir:    pick('direction','dir','side'),
      stype:  pick('securityType','security_type','assetType','asset_type','type2'),
      price:  pick('price','last','value'),
      desc:   pick('description','desc','message','text','Description'),
      time:   pick('time','timestamp','created_at','date') || (tms ? tms.toLocaleTimeString() : ''),
      raw:    ev,
    };
  }

  function _dirCell(d){
    if (d==null) return '';
    var u=String(d).toUpperCase();
    var up = u.indexOf('UP')>=0 || u==='1' || u==='BULL' || u==='BULLISH';
    var dn = u.indexOf('DOWN')>=0 || u==='-1' || u==='BEAR' || u==='BEARISH';
    var c = up?'#3fb950':(dn?'#f85149':'#8b949e');
    var arrow = up?'▲':(dn?'▼':'');
    return '<span style="color:'+c+';font-weight:700">'+arrow+' '+_esc(d)+'</span>';
  }

  window.sigRender = function(){
    var q = ((document.getElementById('sigFilter')||{}).value||'').trim().toUpperCase();
    var body = document.getElementById('sigBody');
    if (!body) return;
    var rows = sigData.map(_f);
    if (q) rows = rows.filter(function(r){
      return [r.symbol,r.signal,r.desc,r.stype].some(function(v){ return v && String(v).toUpperCase().indexOf(q)>=0; })
        || JSON.stringify(r.raw).toUpperCase().indexOf(q)>=0; });
    if (!rows.length){
      body.innerHTML = '<div style="padding:16px;color:#6e7681;font-size:12px">'
        + (sigData.length? '无匹配' : '暂无信号(盘外无实时推送;盘中会实时出现)') + '</div>';
      return;
    }
    var head = '<tr><th>时间</th><th>代码</th><th>信号</th><th>方向</th><th>类型</th><th>价格</th><th>说明</th></tr>';
    var trs = rows.map(function(r){
      var anyResolved = r.symbol||r.signal||r.desc;
      if (!anyResolved){
        return '<tr><td colspan="7" style="color:#8b949e;font-family:monospace;font-size:11px">'
          + _esc(JSON.stringify(r.raw).slice(0,300)) + '</td></tr>';
      }
      return '<tr>'
        + '<td style="white-space:nowrap;color:#8b949e">'+_esc(r.time)+'</td>'
        + '<td style="color:#58a6ff;font-weight:700">'+_esc(r.symbol||'—')+'</td>'
        + '<td>'+_esc(r.signal||'—')+'</td>'
        + '<td>'+_dirCell(r.dir)+'</td>'
        + '<td style="color:#8b949e">'+_esc(r.stype||'')+'</td>'
        + '<td style="text-align:right">'+_esc(r.price!=null?r.price:'')+'</td>'
        + '<td>'+_esc(r.desc||'')+'</td>'
        + '</tr>';
    }).join('');
    body.innerHTML = '<table id="sigTable"><thead>'+head+'</thead><tbody>'+trs+'</tbody></table>';
  };

  window.sigPoll = function(){
    fetch('/api/signals/recent?limit=300').then(function(r){return r.json();})
      .then(function(d){
        sigData = d.signals || [];
        var conn = document.getElementById('sigConn');
        if (conn){
          conn.innerHTML = d.connected
            ? '<span style="color:#3fb950">● LIVE 已连接</span>'
            : '<span style="color:#f85149">○ 未连接</span>'
              + (d.last_error? ' <span style="color:#8b949e">('+_esc(String(d.last_error).slice(0,40))+')</span>':'');
        }
        var st=document.getElementById('sigStatus');
        if (st) st.textContent = (d.buffered||0)+' 条缓冲 · '
          + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})+' 更新';
        sigRender();
      })
      .catch(function(e){ var st=document.getElementById('sigStatus'); if(st){st.textContent='失败: '+e.message; st.style.color='#f85149';} });
  };

  window.sigRestart = function(){
    if (sigTimer){ clearInterval(sigTimer); sigTimer=null; }
    var ms = parseInt((document.getElementById('sigAuto')||{}).value||'3000',10);
    if (ms>0) sigTimer = setInterval(sigPoll, ms);
  };

  window.sigStart = function(){ sigPoll(); sigRestart(); };
})();
"""
