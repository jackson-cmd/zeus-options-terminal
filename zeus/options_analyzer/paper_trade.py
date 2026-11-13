"""
Paper Trade tab — OptionStrat-style strategy builder for option and stock legs,
saved simulated trades and live profit/loss.

Nothing here sends an order to a broker: "Save Paper Trade" writes a JSON record
through /api/paper-trade (paper_trade_routes.py -> paper_trade_api.py), and open
trades are marked to market from the terminal's own quotes. Exported as
PAPER_TRADE_JS and injected into the main page; it renders into
#papersim-container and reuses the page's option math (analyzeStrategy,
computePLMatrix, bsmPrice, nyClose, pnlColor).
"""

PAPER_TRADE_JS = r"""
/* ═══════════════════════════════════════════════════════════════════
   PAPER TRADE — OptionStrat-style Strategy Builder + Saved Trades
   ═══════════════════════════════════════════════════════════════════ */
const PT = (function() {
'use strict';

// ── State ──
let view = 'list'; // 'list' | 'build' | 'detail'
let trades=[], groups=['Default'], selGroup=null, sortCol='dte', sortAsc=true, filterSt='active';
// Build
let bTicker='', bPrice=0, bPrev=0, bChgPct=0, bIV=0.3, bRfr=0.045, bName='';
let bEarn={date:'',time:''}, bExp='', bExps=[], bChain=null, bLegs=[];
let bLoading=false, bChainLoading=false, bAddMenuOpen=false, sbarZoom=100, bViewMode='table';
let _dragThrottle=0; // throttle drag renders
// Detail
let dTrade=null;
// Live marks for open trades (filled by loadMarks)
let marksAt='', marksErr='';
// PL
let valMode=0, plRange=0.15;
const C=document.getElementById('papersim-container');

// ── CSS ──
function injectCSS(){
if(document.getElementById('pt-css'))return;
const s=document.createElement('style');s.id='pt-css';
s.textContent=`
#papersim-container{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e6edf3}
/* Buttons */
.pt-btn{background:#1f6feb;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600}
.pt-btn:hover{background:#388bfd}
.pt-btn-out{background:transparent;border:1px solid #30363d;color:#e6edf3;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px}
.pt-btn-out:hover{border-color:#58a6ff;color:#58a6ff}
.pt-btn-grn{background:#238636}.pt-btn-grn:hover{background:#2ea043}
.pt-btn-red{background:#da3633}.pt-btn-red:hover{background:#f85149}
.pt-sel{background:#161b22;color:#e6edf3;border:1px solid #30363d;padding:4px 8px;border-radius:6px;font-size:12px;cursor:pointer}
.pt-badge{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:600}

/* ── List View ── */
.pt-hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d}
.pt-hdr h2{font-size:18px;font-weight:700;margin:0}
.pt-ctrl{display:flex;align-items:center;gap:10px;padding:10px 20px;background:#0d1117;border-bottom:1px solid #21262d;flex-wrap:wrap}
.pt-tbl{width:100%;border-collapse:collapse}
.pt-tbl th{background:#1c2333;color:#8b949e;font-size:10px;text-transform:uppercase;padding:8px 12px;white-space:nowrap;cursor:pointer;user-select:none;text-align:left}
.pt-tbl th:hover{color:#e6edf3}
.pt-tbl td{padding:8px 12px;border-top:1px solid #21262d;font-size:13px}
.pt-tbl tr:hover{background:#161b22}
.pt-tbl tr{cursor:pointer}
.pt-empty{text-align:center;padding:60px 20px;color:#8b949e}
.pt-empty .ic{font-size:48px;margin-bottom:12px}
.pt-empty .ti{font-size:16px;font-weight:600;color:#e6edf3;margin-bottom:6px}

/* ── Build View ── */
.pt-top{display:flex;align-items:center;gap:12px;padding:10px 20px;background:#161b22;border-bottom:1px solid #30363d;flex-wrap:wrap}
.pt-ticker{background:#0d1117;color:#e6edf3;border:1px solid #30363d;padding:6px 10px;border-radius:6px;font-size:15px;font-weight:700;width:80px;text-transform:uppercase}
.pt-ticker:focus{border-color:#58a6ff;outline:none}
.pt-price{font-size:22px;font-weight:700}
.pt-chg{font-size:12px;margin-left:4px}
/* Add dropdown */
.pt-add-wrap{position:relative;display:inline-block}
.pt-add-menu{position:absolute;top:100%;left:0;background:#161b22;border:1px solid #30363d;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.5);z-index:200;min-width:140px;padding:4px 0;margin-top:4px}
/* Build strategy mega-menu */
.pt-build-wrap{position:relative;display:inline-block}
.pt-build-menu{position:absolute;top:100%;left:0;background:#161b22;border:1px solid #30363d;border-radius:8px;box-shadow:0 8px 30px rgba(0,0,0,.6);z-index:200;padding:8px 0;margin-top:4px;display:flex;gap:0;max-height:70vh;overflow-y:auto}
.pt-build-col{min-width:160px;padding:0 4px;border-right:1px solid #21262d}
.pt-build-col:last-child{border-right:none}
.pt-build-cat{padding:4px 10px;font-size:10px;color:#8b949e;text-transform:uppercase;font-weight:700}
.pt-build-sub{padding:2px 10px;font-size:10px;color:#58a6ff;font-weight:600;margin-top:4px}
.pt-build-it{padding:3px 10px;font-size:12px;color:#e6edf3;cursor:pointer;white-space:nowrap}
.pt-build-it:hover{background:#21262d;border-radius:4px}
/* Controls bar */
.pt-ctrls{display:flex;align-items:center;gap:12px;padding:6px 20px;background:#161b22;border-top:1px solid #30363d;flex-wrap:wrap;font-size:12px}
.pt-ctrls .cl{color:#8b949e;text-transform:uppercase;font-size:10px;font-weight:600}
.pt-ctrls .cv{color:#e6edf3;font-weight:600;min-width:36px}
.pt-ctrls input[type=range]{width:80px;accent-color:#58a6ff}
.pt-ctrls input[type=number]{width:65px;background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:2px 5px;font-size:11px}
/* Graph */
.pt-graph-wrap{position:relative;width:100%;height:400px;background:#0d1117}
.pt-graph-wrap canvas{width:100%;height:100%}
/* ── Compact overrides for Paper Trade PL section ── */
#papersim-container .pl-table{font-size:10px}
#papersim-container .pl-table th,#papersim-container .pl-table td{padding:1px 1px;font-size:9px;height:auto;line-height:1.3}
#papersim-container .pl-table .price-col{width:46px;min-width:46px;max-width:46px;font-size:9px}
#papersim-container .pl-table .pct-col{width:30px;min-width:30px;max-width:30px;font-size:8px}
#papersim-container .pl-table-wrap{max-width:100%;margin:0 auto}
#papersim-container .graph-container{height:320px;padding:4px}
#papersim-container .controls-bar{padding:3px 16px;gap:10px}
#papersim-container .controls-bar input[type=range]{width:100px}
#papersim-container .greeks-bar{padding:3px 16px;font-size:10px}
#papersim-container .tab-bar,#papersim-container .value-tabs{padding:2px 16px}
#papersim-container .date-slider-wrap{padding:3px 16px}
.pt-add-menu-hdr{padding:6px 12px;font-size:10px;color:#8b949e;text-transform:uppercase;font-weight:600}
.pt-add-menu-item{padding:6px 12px;font-size:13px;cursor:pointer;color:#e6edf3}
.pt-add-menu-item:hover{background:#21262d}
.pt-add-menu-item .g{color:#3fb950}.pt-add-menu-item .r{color:#f85149}

/* ── Two-tier Expiration Timeline (compact) ── */
.pt-exp{background:#0d1117;border-bottom:1px solid #21262d;padding:2px 0 0}
.pt-exp-label{padding:2px 20px;font-size:12px;color:#8b949e}
.pt-exp-months{display:flex;overflow-x:auto;padding:0 12px;gap:0;-webkit-overflow-scrolling:touch}
.pt-exp-months::-webkit-scrollbar{height:0}
.pt-exp-month{flex:none;text-align:center;padding:4px 0;font-size:12px;font-weight:600;color:#8b949e;background:#161b22;border:1px solid #21262d;border-bottom:none;border-radius:6px 6px 0 0;min-width:60px}
.pt-exp-month.near{min-width:auto;flex:1}
.pt-exp-dates{display:flex;overflow-x:auto;padding:0 12px 6px;gap:0;-webkit-overflow-scrolling:touch}
.pt-exp-dates::-webkit-scrollbar{height:3px}
.pt-exp-dates::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px}
.pt-exp-d{padding:4px 8px;font-size:13px;color:#8b949e;cursor:pointer;text-align:center;min-width:32px;border-bottom:2px solid transparent;transition:all .12s}
.pt-exp-d:hover{color:#e6edf3;background:#161b22}
.pt-exp-d.act{color:#58a6ff;background:#1f6feb20;border-radius:4px;border-bottom-color:#1f6feb}

/* ── Strike Bar ── */
.pt-strike{padding:8px 20px;background:#0d1117;border-bottom:1px solid #21262d}
.pt-strike-lbl{font-size:11px;color:#8b949e;margin-bottom:4px}
.pt-sbar{position:relative;height:80px;margin:0 10px}
.pt-sbar-axis{position:absolute;top:45px;left:0;right:0;height:1px;background:#30363d}
.pt-sbar-tick{position:absolute;top:43px;width:1px;height:5px;background:#30363d;transform:translateX(-50%)}
.pt-sbar-plbl{position:absolute;top:52px;font-size:10px;color:#8b949e;transform:translateX(-50%)}
/* Volume dots */
.pt-sbar-cdot{position:absolute;width:4px;border-radius:1px;transform:translateX(-50%);cursor:pointer}
.pt-sbar-cdot.call{background:#3fb950;bottom:37px}
.pt-sbar-cdot.put{background:#f85149;top:49px}
.pt-sbar-cdot:hover{opacity:.7}
/* Current price marker */
.pt-sbar-cur{position:absolute;top:32px;transform:translateX(-50%);text-align:center;font-size:10px;color:#8b949e;pointer-events:none}
.pt-sbar-cur::after{content:'';display:block;width:0;height:0;margin:0 auto;border-left:4px solid transparent;border-right:4px solid transparent;border-bottom:4px solid #8b949e}
/* Leg labels */
.pt-sbar-leg{position:absolute;transform:translateX(-50%);cursor:grab;z-index:5;text-align:center;user-select:none}
.pt-sbar-leg.dragging{cursor:grabbing;opacity:.8;z-index:10}
.pt-sbar-leg.long{bottom:38px}
.pt-sbar-leg.short{top:50px}
.pt-sbar-leg-pill{padding:2px 8px;border-radius:5px;font-size:12px;font-weight:700;white-space:nowrap;display:inline-block}
.pt-sbar-leg-pill.long{background:#238636;color:#fff}
/* Price edit inline */
.pt-price-edit{background:#0d1117;color:#58a6ff;border:1px solid #58a6ff;border-radius:4px;width:60px;padding:2px 4px;font-family:monospace;font-size:12px;text-align:center}
.pt-leg .pr{cursor:pointer}.pt-leg .pr:hover{text-decoration:underline}
.pt-sbar-leg-pill.short{background:#da3633;color:#fff}
.pt-sbar-leg .arr{width:0;height:0;margin:0 auto;border-left:4px solid transparent;border-right:4px solid transparent}
.pt-sbar-leg.long .arr{border-top:4px solid #238636}
.pt-sbar-leg.short .arr{border-bottom:4px solid #da3633}

/* ── Summary Bar ── */
.pt-sum{display:flex;gap:0;background:#161b22;border-bottom:1px solid #30363d;overflow-x:auto}
.pt-sum-i{flex:1;min-width:80px;padding:4px 8px;border-right:1px solid #21262d;text-align:center}
.pt-sum-i:last-child{border-right:none}
.pt-sum-l{font-size:8px;color:#8b949e;text-transform:uppercase;margin-bottom:1px}
.pt-sum-v{font-size:12px;font-weight:700}

/* ── Legs Panel ── */
.pt-legs{display:flex;gap:8px;padding:8px 20px;background:#0d1117;border-bottom:1px solid #21262d;flex-wrap:wrap;align-items:center}
.pt-leg{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:6px 10px;font-size:12px;display:flex;align-items:center;gap:6px;cursor:pointer}
.pt-leg:hover{border-color:#58a6ff}
.pt-leg .act{font-weight:700;font-size:10px;padding:2px 5px;border-radius:4px}
.pt-leg .buy{background:#23883033;color:#3fb950}
.pt-leg .sell{background:#f8514933;color:#f85149}
.pt-leg .sk{font-weight:700;font-size:14px}
.pt-leg .tp{font-size:10px;color:#8b949e}
.pt-leg .pr{font-family:monospace;color:#58a6ff;font-size:12px}
.pt-leg .rm{color:#8b949e;cursor:pointer;margin-left:auto;font-size:14px}
.pt-leg .rm:hover{color:#f85149}
.pt-leg-qty{display:inline-flex;align-items:center;gap:3px}
.pt-leg-qty button{background:none;border:1px solid #30363d;color:#8b949e;width:16px;height:16px;border-radius:50%;cursor:pointer;font-size:10px;display:flex;align-items:center;justify-content:center;padding:0}
.pt-leg-qty button:hover{border-color:#58a6ff;color:#58a6ff}

/* ── P/L Table ── */
.pt-pl{background:#0d1117}
.pt-pl table{border-collapse:collapse;font-size:10px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;table-layout:fixed;width:100%}
.pt-pl th{background:#1c2333;padding:2px 1px;color:#8b949e;font-weight:500;white-space:nowrap;border:1px solid #21262d;text-align:center;font-size:9px;overflow:hidden;text-overflow:ellipsis;height:18px}
.pt-pl td{padding:1px 1px;text-align:center;border:1px solid #21262d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;height:18px;font-size:10px}
.pt-pl .pc{background:#161b22;font-weight:700;text-align:right;color:#e6edf3;padding-right:3px;font-size:10px}
.pt-pl .pp{background:#161b22;color:#8b949e;font-size:8px;text-align:left;padding-left:2px}
.pt-pl .mh{text-align:center;font-weight:600;color:#e6edf3;background:#282e38;font-size:10px}
.pt-pl .crow td{border-top:2px dashed #58a6ff;border-bottom:2px dashed #58a6ff}

/* Value tabs */
.pt-vtabs{display:flex;gap:0;padding:4px 20px;background:#0d1117}
.pt-vt{padding:4px 10px;font-size:11px;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent}
.pt-vt:hover{color:#e6edf3}
.pt-vt.act{color:#58a6ff;border-bottom-color:#1f6feb}

/* Greeks */
.pt-greeks{display:flex;gap:16px;padding:8px 20px;background:#161b22;border-top:1px solid #30363d;font-size:12px}
.pt-greeks .l{color:#8b949e;margin-right:3px}
.pt-greeks .v{font-family:monospace;font-weight:600}

/* Detail header */
.pt-dhdr{display:flex;align-items:center;gap:12px;padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d}

/* Modal */
.pt-modal-ov{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:1000;display:flex;justify-content:center;align-items:center}
.pt-modal{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;min-width:320px;max-width:400px;box-shadow:0 8px 30px rgba(0,0,0,.5)}
.pt-modal h3{margin:0 0 10px;font-size:15px}
.pt-modal-warn{background:#d2992220;color:#d29922;font-size:11px;padding:8px 10px;border-radius:6px;margin-bottom:12px;line-height:1.4}
.pt-modal-g{display:grid;grid-template-columns:1fr 1fr;gap:5px 14px;font-size:12px;margin:10px 0}
.pt-modal-g .lb{color:#8b949e}
.pt-modal-g .vl{font-weight:600;font-family:monospace;text-align:right}
.pt-modal-a{margin-top:14px;display:flex;flex-direction:column;gap:6px}
.pt-modal-ai{display:flex;align-items:center;gap:8px;padding:7px 10px;cursor:pointer;border-radius:6px;font-size:13px;color:#e6edf3}
.pt-modal-ai:hover{background:#21262d}

/* Gear menu */
.pt-gmenu{position:absolute;right:12px;background:#161b22;border:1px solid #30363d;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.4);z-index:100;min-width:140px;padding:4px 0}
.pt-gmenu-i{padding:7px 14px;font-size:12px;cursor:pointer;color:#e6edf3}
.pt-gmenu-i:hover{background:#21262d}
.pt-gmenu-i.dng{color:#f85149}

/* P/L summary + notes */
.pt-sub{font-size:11px;color:#8b949e;margin-top:2px}
.pt-pnlbar{display:flex;gap:28px;flex-wrap:wrap;padding:10px 20px;background:#0d1117;border-bottom:1px solid #21262d}
.pt-pnlbar .l{display:block;font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.4px}
.pt-pnlbar .v{display:block;font-size:17px;font-weight:700;font-family:monospace;margin-top:2px}
.pt-note{font-size:11px;color:#8b949e;padding:8px 20px}

/* Loading */
.pt-load{text-align:center;padding:40px;color:#8b949e}
@keyframes pt-sp{to{transform:rotate(360deg)}}
.pt-spinner{display:inline-block;width:20px;height:20px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:pt-sp .6s linear infinite}
`;
document.head.appendChild(s);
}

// ── Helpers ──
const G='#3fb950',R='#f85149',B='#58a6ff';
function cls(v){return v>=0?'color:'+G:'color:'+R}
function sgn(v){return v>=0?'+':''}
function fmt(v,d){d=d||0;return Math.abs(v).toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g,',')}
function fmtD(iso){if(!iso)return'-';const d=new Date(iso);return d.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})+', '+d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})}
function dte(exp){if(!exp)return null;const d=Math.round((nyClose(exp)-new Date())/864e5);return isNaN(d)?null:Math.max(0,d)}
function fmtT(iso){if(!iso)return'';const d=new Date(iso);return isNaN(d)?'':d.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'})}
function money(v,d){v=+v||0;return(v<0?'-':'+')+'$'+fmt(v,d||0)}
function pctS(v){v=+v||0;return(v<0?'-':'+')+Math.abs(v).toFixed(0)+'%'}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
// A stock leg counts shares (multiplier 1); an option leg counts contracts of 100.
function _mult(l){return l.type==='S'?1:(l.mult||100)}
// The page's option math (analyzeStrategy, computePLMatrix) only knows calls and
// puts. A share of stock is exactly a call struck at 0: worth the spot price at
// any date, delta 1, no gamma, theta or vega. Handing stock legs over in that form
// makes max profit/loss, breakevens, chance of profit and the Greeks include them.
function _az(s){return{...s,legs:s.legs.map(l=>l.type==='S'?{...l,type:'C',strike:0,mult:1}:l)}}
// Every leg of a paper trade shares one expiration, so the payoff at expiration is
// piecewise linear: its extremes sit at a price of 0, at a strike, or at infinity
// (slope _upSlope). The page's analyzer scans only down to half the lowest strike,
// which misses the bottom for shares, short puts and long puts.
function _extremes(s){
  const ks=[0,...new Set(s.legs.filter(l=>l.type!=='S').map(l=>+l.strike))].sort((a,b)=>a-b);
  const pay=p=>s.legs.reduce((a,l)=>{const v=l.type==='S'?p:(l.type==='C'?Math.max(0,p-l.strike):Math.max(0,l.strike-p));return a+(v-l.entry)*l.qty*_mult(l)},0);
  const vals=ks.map(pay),up=_upSlope(s);
  return{max:up>0?Infinity:Math.max(...vals),min:up<0?-Infinity:Math.min(...vals)};
}
function _A(s){const a=analyzeStrategy(_az(s));const e=_extremes(s);a.maxProfit=e.max;a.maxLoss=e.min;return a}
function _farExp(){const d=new Date();d.setFullYear(d.getFullYear()+1);return d.toISOString().slice(0,10)}
function _stockOnly(legs){return legs.length>0&&legs.every(l=>l.type==='S')}
// Upside slope of the payoff: calls and shares make it rise without limit.
function _upSlope(s){return s.legs.reduce((a,l)=>a+(l.type==='P'?0:l.qty*_mult(l)),0)}
function nameSt(legs){
  if(!legs||!legs.length)return'Empty';
  if(legs===bLegs&&bName)return bName;
  const c=legs.filter(l=>l.type==='C'),p=legs.filter(l=>l.type==='P');
  const sk=legs.filter(l=>l.type==='S');
  if(sk.length){
    const lsk=sk.some(l=>l.qty>0),scl=c.filter(l=>l.qty<0).length,lpt=p.filter(l=>l.qty>0).length;
    if(!c.length&&!p.length)return lsk?'Long Stock':'Short Stock';
    if(lsk&&c.length===1&&scl===1&&!p.length)return'Covered Call';
    if(lsk&&p.length===1&&lpt===1&&!c.length)return'Protective Put';
    if(lsk&&c.length===1&&scl===1&&p.length===1&&lpt===1)return'Collar';
    return'Custom';
  }
  const lc=c.filter(l=>l.qty>0),sc=c.filter(l=>l.qty<0),lp=p.filter(l=>l.qty>0),sp=p.filter(l=>l.qty<0);
  if(c.length===2&&!p.length&&lc.length===1&&sc.length===1)return lc[0].strike<sc[0].strike?'Bull Call Spread':'Bear Call Spread';
  if(p.length===2&&!c.length&&lp.length===1&&sp.length===1)return lp[0].strike>sp[0].strike?'Bear Put Spread':'Bull Put Spread';
  if(c.length===2&&p.length===2)return'Iron Condor';
  if(c.length===1&&!p.length)return lc.length?'Long Call':'Short Call';
  if(p.length===1&&!c.length)return lp.length?'Long Put':'Short Put';
  return'Custom';
}
function dispName(t){
  const ol=t.legs.filter(l=>l.type!=='S'),sh=t.legs.filter(l=>l.type==='S').reduce((a,l)=>a+l.qty,0);
  const ss=[...new Set(ol.map(l=>l.strike))].sort((a,b)=>a-b).map(s=>s%1===0?s.toFixed(0):String(s)).join('/');
  let el=t.expiration||'';try{const d=new Date(t.expiration+'T12:00:00');const dy=d.getDate();if(!isNaN(dy)){const sf=[,'st','nd','rd'][dy%10>3?0:dy%100-dy%10!=10?dy%10:0]||'th';el=d.toLocaleDateString('en-US',{month:'short'})+' '+dy+sf}}catch(e){}
  return t.underlying+(ol.length?' '+el+' '+ss:'')+(sh?' '+(sh>0?'+':'')+sh+' sh':'')+' '+t.name;
}
// Saved trade -> analyzer input. Open trades use the live marks from loadMarks();
// closed trades keep the prices they were closed at.
function mkStrat(t){
  const open=t.status==='active';
  const px=open?(t.spot||t.current_price):(t.close_price||t.current_price);
  const exp=t.expiration||_farExp();
  return{name:t.name,underlying:t.underlying,current_price:px,iv:t.iv||.3,risk_free_rate:t.risk_free_rate||.045,
    earnings_date:t.earnings_date||'',earnings_time:t.earnings_time||'',expiration:exp,dte:dte(exp),
    legs:t.legs.map(l=>{const cur=(open&&l.mark!=null)?l.mark:(l.current!=null?l.current:(l.mid||l.entry));
      return{symbol:l.symbol,streamer_symbol:l.streamer_symbol,type:l.type,strike:l.type==='S'?0:l.strike,qty:l.qty,
        entry:l.entry,current:cur,exp:l.exp||exp,mult:_mult(l),bid:l.bid||0,ask:l.ask||0,mid:cur,
        prev_close:open?(l.prev_close||0):0,created_at:t.created_at}}),created_at:t.created_at};
}
function grpMonths(flat){if(!flat||!flat.length)return[];const r=[];let c=flat[0],n=1;for(let i=1;i<flat.length;i++){if(flat[i]===c)n++;else{r.push({label:c,span:n});c=flat[i];n=1}}r.push({label:c,span:n});return r}

async function api(url,o){
  o=o||{};
  const r=await fetch(url,{...o,headers:{'Content-Type':'application/json','X-Zeus-Client':'1'},body:o.body?JSON.stringify(o.body):undefined});
  let j=null;try{j=await r.json()}catch(e){}
  if(!r.ok)throw new Error((j&&(j.detail||j.error))||('HTTP '+r.status));
  return j;
}

// ═══════════════════════════════════════════════════════════════════
// LIST VIEW
// ═══════════════════════════════════════════════════════════════════
function renderList(){
  view='list';let h='';
  const nAct=trades.filter(t=>t.status==='active').length;
  h+=`<div class="pt-hdr"><div><h2>Paper Trades</h2><div class="pt-sub">Simulated orders with live P/L. Nothing is sent to your broker.</div></div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="pt-btn-out" onclick="PT.refresh()" title="Re-pull quotes and recompute P/L for open trades">&#8635; Refresh P/L</button>
      <button class="pt-btn" onclick="PT.newTrade()">+ New Trade</button></div></div>`;
  h+=`<div class="pt-ctrl">
    <span style="font-size:11px;color:#8b949e">Group:</span>
    <select class="pt-sel" onchange="PT.setGrp(this.value)">${'<option value="">All</option>'+groups.map(g=>'<option value="'+esc(g)+'"'+(g===selGroup?' selected':'')+'>'+esc(g)+'</option>').join('')}</select>
    <button class="pt-btn-out" onclick="PT.addGrp()" style="padding:3px 8px" title="New group">+</button>
    <span style="flex:1"></span>
    <span style="font-size:11px;color:#8b949e">Sort:</span>
    <select class="pt-sel" onchange="PT.setSort(this.value)">
      <option value="dte"${sortCol==='dte'?' selected':''}>Expiration</option>
      <option value="created_at"${sortCol==='created_at'?' selected':''}>Created</option>
      <option value="pnl"${sortCol==='pnl'?' selected':''}>P/L</option>
      <option value="name"${sortCol==='name'?' selected':''}>Name</option></select>
    <span style="font-size:11px;color:#8b949e">Show:</span>
    <select class="pt-sel" onchange="PT.setFilt(this.value)">
      <option value="active"${filterSt==='active'?' selected':''}>Open</option>
      <option value="closed"${filterSt==='closed'?' selected':''}>Closed</option>
      <option value="all"${filterSt==='all'?' selected':''}>All</option></select>
    <button class="pt-btn-out" onclick="PT.csv()">Export CSV &#8595;</button>
  </div>`;
  let fl=trades;if(selGroup)fl=fl.filter(t=>t.group===selGroup);if(filterSt!=='all')fl=fl.filter(t=>t.status===filterSt);
  const rows=fl.map(t=>{const open=t.status==='active';let a=null;
    if(!open||t._marked){try{a=_A(mkStrat(t))}catch(e){}}
    return{t,open,nm:dispName(t),pnl:a?a.unrealizedPnl:null,pct:a?a.unrealizedPct:null,
      today:(open&&a&&!t._todayUnknown)?a.todayPnl:null,dte:_stockOnly(t.legs)?null:dte(t.expiration),cr:t.created_at}});
  const nz=v=>v==null?-Infinity:v;
  rows.sort((a,b)=>{let c=0;if(sortCol==='name')c=a.nm.localeCompare(b.nm);else if(sortCol==='dte')c=nz(a.dte)-nz(b.dte);else if(sortCol==='pnl')c=nz(a.pnl)-nz(b.pnl);else c=(a.cr||'').localeCompare(b.cr||'');return sortAsc?c:-c});
  if(rows.length){
    const s={open:0,today:0,realized:0,nOpen:0,nClosed:0,unpriced:0,noToday:0};
    rows.forEach(r=>{if(r.open){s.nOpen++;if(r.pnl==null)s.unpriced++;else s.open+=r.pnl;if(r.today==null)s.noToday++;else s.today+=r.today}else{s.nClosed++;if(r.pnl!=null)s.realized+=r.pnl}});
    const v=(x,ok)=>ok?`<span class="v" style="${cls(x)}">${money(x)}</span>`:'<span class="v" style="color:#8b949e">&#8212;</span>';
    h+=`<div class="pt-pnlbar">
      <div><span class="l">Open P/L</span>${v(s.open,s.nOpen>s.unpriced)}</div>
      <div><span class="l">Today</span>${v(s.today,s.nOpen>s.noToday)}</div>
      <div><span class="l">Realized (closed)</span>${v(s.realized,s.nClosed>0)}</div>
      <div><span class="l">Trades</span><span class="v">${s.nOpen} open &middot; ${s.nClosed} closed</span></div>
    </div>`;
  }
  if(!rows.length){h+=`<div class="pt-empty"><div class="ic">&#128203;</div><div class="ti">${trades.length?'No trades match this filter':'No paper trades yet'}</div><div>Click "+ New Trade" to build a strategy with option or stock legs</div></div>`}
  else{
    h+='<div style="overflow-x:auto"><table class="pt-tbl"><thead><tr><th onclick="PT.tgSort(\'name\')">Name</th><th onclick="PT.tgSort(\'pnl\')" style="text-align:right">Total Return</th><th style="text-align:right">Today\'s Return</th><th onclick="PT.tgSort(\'created_at\')">Created At</th><th onclick="PT.tgSort(\'dte\')" style="text-align:right">Days Until Exp</th><th style="width:30px"></th></tr></thead><tbody>';
    const na='<span style="color:#8b949e" title="Waiting for a quote">&#8212;</span>';
    rows.forEach(r=>{
      h+=`<tr onclick="PT.detail('${esc(r.t.id)}')"><td style="font-weight:600">${esc(r.nm)}${r.open?'':' <span class="pt-badge" style="background:#d2992233;color:#d29922;margin-left:4px">CLOSED</span>'}${r.t.group&&r.t.group!=='Default'?' <span class="pt-badge" style="background:#1f6feb22;color:#58a6ff;margin-left:4px">'+esc(r.t.group)+'</span>':''}</td>
        <td style="text-align:right;font-family:monospace">${r.pnl==null?na:`<span style="${cls(r.pnl)}">${money(r.pnl)} (${pctS(r.pct)})</span>`}</td>
        <td style="text-align:right;font-family:monospace">${r.today==null?na:`<span style="${cls(r.today)}">${money(r.today)}</span>`}</td>
        <td style="color:#8b949e;font-size:12px">${fmtD(r.cr)}</td>
        <td style="text-align:right;font-weight:700">${r.dte==null?na:r.dte+'d <span style="color:#8b949e;font-size:11px">('+esc(r.t.expiration)+')</span>'}</td>
        <td style="position:relative;white-space:nowrap">
          <span style="cursor:pointer;color:#8b949e;font-size:15px" onclick="event.stopPropagation();PT.gear(event,'${esc(r.t.id)}')" title="Options">&#9881;</span>
          <span style="cursor:pointer;color:#f85149;font-size:14px;margin-left:6px" onclick="event.stopPropagation();PT.delTr('${esc(r.t.id)}')" title="Delete">&#128465;</span>
        </td></tr>`});
    h+='</tbody></table></div>'}
  if(nAct)h+=`<div class="pt-note">${marksErr?'Quotes unavailable: '+esc(marksErr)+'. ':''}${marksAt?'Open trades priced at '+fmtT(marksAt)+'.':''} Today's Return counts from yesterday's close, or from the entry price for trades opened today.</div>`;
  C.innerHTML=h;
}

// ═══════════════════════════════════════════════════════════════════
// BUILD VIEW
// ═══════════════════════════════════════════════════════════════════
function renderBuild(){
  view='build';let h='';
  // Top bar
  // Strategy name
  const sName=bLegs.length?nameSt(bLegs):'Empty';
  h+=`<div class="pt-top">
    <div class="pt-build-wrap"><button class="pt-btn-out" onclick="PT.tgBuild()" style="font-weight:700;font-size:14px">${esc(sName)} &#9662;</button>
      <div id="pt-build-menu" class="pt-build-menu" style="display:none">
        <div class="pt-build-col">
          <div class="pt-build-cat">Novice</div>
          <div class="pt-build-sub">Basic</div>
          <div class="pt-build-it" onclick="PT.tplN('long_call')">Long Call</div>
          <div class="pt-build-it" onclick="PT.tplN('long_put')">Long Put</div>
          <div class="pt-build-sub">Income</div>
          <div class="pt-build-it" onclick="PT.tplN('covered_call')">Covered Call</div>
          <div class="pt-build-it" onclick="PT.tplN('cash_secured_put')">Cash-Secured Put</div>
          <div class="pt-build-sub">Other</div>
          <div class="pt-build-it" onclick="PT.tplN('protective_put')">Protective Put</div>
        </div>
        <div class="pt-build-col">
          <div class="pt-build-cat">Intermediate</div>
          <div class="pt-build-sub">Credit Spreads</div>
          <div class="pt-build-it" onclick="PT.tplN('bull_put')">Bull Put Spread</div>
          <div class="pt-build-it" onclick="PT.tplN('bear_call')">Bear Call Spread</div>
          <div class="pt-build-sub">Neutral</div>
          <div class="pt-build-it" onclick="PT.tplN('iron_butterfly')">Iron Butterfly</div>
          <div class="pt-build-it" onclick="PT.tplN('iron_condor')">Iron Condor</div>
          <div class="pt-build-it" onclick="PT.tplN('long_put_bfly')">Long Put Butterfly</div>
          <div class="pt-build-it" onclick="PT.tplN('long_call_bfly')">Long Call Butterfly</div>
          <div class="pt-build-sub">Debit Spreads</div>
          <div class="pt-build-it" onclick="PT.tplN('bull_call')">Bull Call Spread</div>
          <div class="pt-build-it" onclick="PT.tplN('bear_put')">Bear Put Spread</div>
          <div class="pt-build-sub">Directional</div>
          <div class="pt-build-it" onclick="PT.tplN('straddle')">Straddle</div>
          <div class="pt-build-it" onclick="PT.tplN('strangle')">Strangle</div>
        </div>
        <div class="pt-build-col">
          <div class="pt-build-cat">Advanced</div>
          <div class="pt-build-sub">Naked</div>
          <div class="pt-build-it" onclick="PT.tplN('short_put')">Short Put</div>
          <div class="pt-build-it" onclick="PT.tplN('short_call')">Short Call</div>
          <div class="pt-build-sub">Neutral</div>
          <div class="pt-build-it" onclick="PT.tplN('short_straddle')">Short Straddle</div>
          <div class="pt-build-it" onclick="PT.tplN('short_strangle')">Short Strangle</div>
          <div class="pt-build-sub">Ratio</div>
          <div class="pt-build-it" onclick="PT.tplN('call_ratio_back')">Call Ratio Backspread</div>
          <div class="pt-build-it" onclick="PT.tplN('put_ratio_back')">Put Ratio Backspread</div>
          <div class="pt-build-sub">Other</div>
          <div class="pt-build-it" onclick="PT.tplN('jade_lizard')">Jade Lizard</div>
          <div class="pt-build-it" onclick="PT.tplN('reverse_jade')">Reverse Jade Lizard</div>
        </div>
      </div>
    </div>
    <input class="pt-ticker" id="pt-tk" value="${esc(bTicker)}" placeholder="AAPL" oninput="this.value=this.value.toUpperCase()" onkeydown="if(event.key==='Enter')PT.load()">
    <button class="pt-btn-out" onclick="PT.load()">Load</button>
    ${bPrice>0?`<span class="pt-price">$${bPrice.toFixed(2)}</span>${bPrev>0?`<span class="pt-chg" style="${cls(bChgPct)}">${bChgPct<0?'-':'+'}${Math.abs(bChgPct).toFixed(2)}%</span>`:''}`:''}
    <span style="flex:1"></span>
    <div class="pt-add-wrap"><button class="pt-btn" onclick="PT.tgAdd()" ${!bChain?'disabled':''}>Add +</button>
      <div id="pt-add-menu" class="pt-add-menu" style="display:none">
        <div class="pt-add-menu-hdr">Options:</div>
        <div class="pt-add-menu-item" onclick="PT.addAtm('C',1)">Buy <span class="g">Call</span></div>
        <div class="pt-add-menu-item" onclick="PT.addAtm('C',-1)">Sell <span class="r">Call</span></div>
        <div class="pt-add-menu-item" onclick="PT.addAtm('P',1)">Buy <span class="g">Put</span></div>
        <div class="pt-add-menu-item" onclick="PT.addAtm('P',-1)">Sell <span class="r">Put</span></div>
        <div class="pt-add-menu-hdr">Underlying:</div>
        <div class="pt-add-menu-item" onclick="PT.addStock(100)">Buy 100 <span class="g">${esc(bTicker)||'shares'}</span></div>
        <div class="pt-add-menu-item" onclick="PT.addStock(-100)">Sell 100 <span class="r">${esc(bTicker)||'shares'}</span></div>
      </div>
    </div>
    <button class="pt-btn-out" onclick="PT.positions()">&#8592; Saved trades (${trades.filter(t=>t.status==='active').length} open)</button>
    <button class="pt-btn pt-btn-grn" onclick="PT.place()" ${!bLegs.length?'disabled':''} title="Record a simulated trade at the prices shown. Nothing is sent to your broker.">Save Paper Trade &#9745;</button>
  </div>`;

  if(!bChain){
    if(bLoading)h+=`<div class="pt-load"><div class="pt-spinner"></div><div style="margin-top:8px">Loading...</div></div>`;
    else h+=`<div class="pt-empty"><div class="ic">&#128269;</div><div class="ti">Enter a ticker to start</div><div>Type a symbol and press Enter</div></div>`;
    C.innerHTML=h;return;
  }

  // Expiration label
  h+=`<div style="padding:4px 20px;font-size:12px;color:#8b949e;background:#0d1117">EXPIRATION: ${bExp?'<strong style="color:#e6edf3">'+dte(bExp)+'d</strong>':'Select below'}${bEarn.date?' &nbsp;&#128227; Earnings: '+bEarn.date+' '+bEarn.time:''}</div>`;

  // Two-tier expiration timeline
  h+=renderExpTimeline();

  // Strike bar
  if(bChain.strikes&&bChain.strikes.length)h+=renderStrikeBar();

  // (templates now in Build dropdown menu)

  // Legs panel
  if(bLegs.length){
    h+='<div class="pt-legs" id="pt-legs-el">';
    bLegs.forEach((l,i)=>{
      const a=l.qty>0?'BUY':'SELL',ac=l.qty>0?'buy':'sell';
      h+=`<div class="pt-leg" onclick="PT.legPop(${i})">
        <span class="act ${ac}">${a}</span>
        <div class="pt-leg-qty"><button onclick="event.stopPropagation();PT.adjQ(${i},-1)">&#8722;</button><span style="${cls(l.qty)}">${l.qty>0?'+':''}${l.qty}</span><button onclick="event.stopPropagation();PT.adjQ(${i},1)">+</button></div>
        ${l.type==='S'?`<span class="sk">${esc(bTicker)}</span><span class="tp">shares</span>`:`<span class="sk">${l.strike}</span><span class="tp">${l.type}</span>`}
        <span class="pr" id="pt-pr-${i}" onclick="event.stopPropagation();PT.editPrice(${i})" title="Click to type a different price">$${l.mid>0?l.mid.toFixed(2):'--'}</span>
        <span class="rm" onclick="event.stopPropagation();PT.rmLeg(${i})" title="Remove">&#10005;</span></div>`;
    });
    h+=`</div>`;
    h+=renderSummary(mkBuildStrat());
    h+=renderPL(mkBuildStrat());
    // Greeks are shown inside renderPL
  } else if(bExp&&bChain.strikes){
    h+=`<div style="padding:14px 20px;text-align:center;color:#8b949e;font-size:13px;background:#0d1117">
      <strong>This strategy has no enabled items in it.</strong><br>Add a stock or option to view stats and the predicted profit/loss.</div>`;
    h+=renderPL(mkBuildStrat()); // show empty grid
  }
  if(bChainLoading)h+=`<div class="pt-load"><div class="pt-spinner"></div></div>`;
  C.innerHTML=h;
}

function renderExpTimeline(){
  const n=bExps.length;if(!n)return'';
  // Check which month has earnings
  let earnMonth='';
  if(bEarn.date){try{earnMonth=new Date(bEarn.date+'T12:00:00').toLocaleDateString('en-US',{month:'short'})}catch(e){}}
  // Group by month
  const months=[];let cur=null;
  bExps.forEach((e,i)=>{const d=new Date(e.date+'T12:00:00');const k=d.toLocaleDateString('en-US',{month:'short'})+(d.getFullYear()!==new Date().getFullYear()?' \''+String(d.getFullYear()).slice(2):'');
    if(!cur||cur.key!==k){cur={key:k,start:i,count:1,hasActive:e.date===bExp,hasEarn:earnMonth&&k.startsWith(earnMonth)};months.push(cur)}else{cur.count++;if(e.date===bExp)cur.hasActive=true}});
  let h=`<div class="pt-exp"><div style="display:grid;grid-template-columns:repeat(${n},minmax(26px,1fr));overflow-x:auto;padding:0 8px;-webkit-overflow-scrolling:touch;font-size:0">`;
  // Month row (compact)
  months.forEach(m=>{
    const earn=m.hasEarn?'&#128227;':'';
    h+=`<div style="grid-column:${m.start+1}/span ${m.count};text-align:center;padding:2px 0;font-size:11px;font-weight:600;color:${m.hasActive?'#e6edf3':'#8b949e'};background:${m.hasActive?'#1f6feb30':'#161b22'};border:1px solid ${m.hasActive?'#1f6feb50':'#21262d'};border-bottom:none;border-radius:4px 4px 0 0;line-height:1.2">${m.key}${earn}</div>`;
  });
  // Date row (compact)
  bExps.forEach((e,i)=>{
    const d=new Date(e.date+'T12:00:00');const day=d.getDate();const isW=e.type!=='Regular';const act=e.date===bExp;
    h+=`<div style="grid-column:${i+1};text-align:center;padding:2px 1px;font-size:11px;color:${act?'#58a6ff':'#8b949e'};cursor:pointer;border-bottom:2px solid ${act?'#1f6feb':'transparent'};${act?'background:#1f6feb20;border-radius:3px;':''}line-height:1.2" onclick="PT.selExp('${e.date}')" title="${e.date} (${e.dte}d)">${day}${isW?'<sup style=\"font-size:7px\">w</sup>':''}</div>`;
  });
  h+='</div></div>';
  return h;
}

let sbarMode='vol'; // 'vol' or 'oi'
function renderStrikeBar(){
  const st=bChain.strikes;if(!st||st.length<2)return'';
  const vMin=st[0].strike,vMax=st[st.length-1].strike,vRng=vMax-vMin||1;
  const maxVal=Math.max(1,...st.map(s=>Math.max(sbarMode==='vol'?(s.call.vol||0):(s.call.oi||0),sbarMode==='vol'?(s.put.vol||0):(s.put.oi||0))));

  // Fixed width, no scrolling — compact like OptionStrat
  let h=`<div class="pt-strike"><div style="display:flex;align-items:center;gap:8px;margin-bottom:2px">
    <span class="pt-strike-lbl" style="margin:0">STRIKE:</span>
    <button class="pt-btn-out" style="padding:1px 6px;font-size:10px" onclick="PT.tgSbarMode()">${sbarMode==='vol'?'Volume':'OI'} &#8644;</button>
  </div>
  <div class="pt-sbar" id="pt-sbar-el" style="position:relative;height:70px">`;
  h+='<div class="pt-sbar-axis"></div>';

  // Current price marker
  if(bPrice>=vMin&&bPrice<=vMax){
    const pct=((bPrice-vMin)/vRng)*100;
    h+=`<div class="pt-sbar-cur" style="left:${pct}%">${bTicker}<br></div>`;
  }

  // ~10 evenly spaced labels across the full range
  const nLabels=10;
  const rawStep=vRng/nLabels;
  // Nice step: round to 5, 10, 25, 50, 100
  const niceSteps=[1,2,5,10,20,25,50,100,200,500];
  const labelStep=niceSteps.reduce((a,b)=>Math.abs(b-rawStep)<Math.abs(a-rawStep)?b:a);

  const firstLabel=Math.ceil(vMin/labelStep)*labelStep;
  for(let p=firstLabel;p<=vMax;p+=labelStep){
    const pct=((p-vMin)/vRng)*100;
    h+=`<div class="pt-sbar-tick" style="left:${pct}%"></div>`;
    h+=`<div class="pt-sbar-plbl" style="left:${pct}%">${p}</div>`;
  }

  // Volume/OI dots for each strike
  st.forEach(s=>{
    const pct=((s.strike-vMin)/vRng)*100;
    const cv=sbarMode==='vol'?(s.call.vol||0):(s.call.oi||0);
    const pv=sbarMode==='vol'?(s.put.vol||0):(s.put.oi||0);
    if(cv>0){const h2=Math.max(1,(cv/maxVal)*18);h+=`<div class="pt-sbar-cdot call" style="left:${pct}%;height:${h2}px" onclick="PT.addFromBar(${s.strike},'C',1)" title="${s.strike}C ${sbarMode}:${cv}"></div>`}
    if(pv>0){const h2=Math.max(1,(pv/maxVal)*18);h+=`<div class="pt-sbar-cdot put" style="left:${pct}%;height:${h2}px" onclick="PT.addFromBar(${s.strike},'P',1)" title="${s.strike}P ${sbarMode}:${pv}"></div>`}
  });

  // Leg labels (draggable)
  bLegs.forEach((l,i)=>{
    if(l.strike<vMin||l.strike>vMax)return;
    const pct=((l.strike-vMin)/vRng)*100;const isL=l.qty>0;
    h+=`<div class="pt-sbar-leg ${isL?'long':'short'}" style="left:${pct}%" onmousedown="PT.dragStart(event,${i})">
      <span class="pt-sbar-leg-pill ${isL?'long':'short'}">${l.strike}${l.type}</span>
      <div class="arr"></div></div>`;
  });

  h+='</div></div>';
  return h;
}
function tgSbarMode(){sbarMode=sbarMode==='vol'?'oi':'vol';renderBuild()}

function mkBuildStrat(){
  const exp=bExp||_farExp();
  return{name:nameSt(bLegs),underlying:bTicker,current_price:bPrice,iv:bIV,risk_free_rate:bRfr,
    earnings_date:bEarn.date,earnings_time:bEarn.time,expiration:exp,dte:dte(exp),
    legs:bLegs.map(l=>({symbol:l.type==='S'?bTicker:(l.symbol||''),streamer_symbol:l.streamer||'',type:l.type,strike:l.type==='S'?0:l.strike,qty:l.qty,
      entry:l.mid||0,current:l.mid||0,exp:exp,mult:_mult(l),bid:l.bid||0,ask:l.ask||0,mid:l.mid||0}))};
}

function renderSummary(strat){
  let a;try{a=_A(strat)}catch(e){return''}
  const nd=a.netDebit,nl=nd>=0?'NET DEBIT':'NET CREDIT',ni=nd>=0?'&#129472;':'&#128181;';
  const be=a.breakevens||[];let bs='-';
  if(be.length>0&&strat.current_price>0){const cl=be.reduce((x,b)=>Math.abs(b-strat.current_price)<Math.abs(x-strat.current_price)?b:x);const bp=((cl-strat.current_price)/strat.current_price*100);bs=(bp>=0?'Above':'Below')+' $'+cl.toFixed(2)+' ('+(bp>=0?'+':'')+bp.toFixed(1)+'%)';}
  const mp=isFinite(a.maxProfit)?(a.maxProfit<0?'-':'')+'$'+fmt(a.maxProfit):'&#8734;';
  const ml=isFinite(a.maxLoss)?(a.maxLoss>0?'+':'')+'$'+fmt(a.maxLoss):'&#8734;';
  return`<div class="pt-sum" id="pt-sum-el">
    <div class="pt-sum-i"><div class="pt-sum-l">${nl}:</div><div class="pt-sum-v">${ni} $${fmt(Math.abs(nd),2)}</div></div>
    <div class="pt-sum-i"><div class="pt-sum-l">MAX LOSS:</div><div class="pt-sum-v" style="color:${R}">&#8595; ${ml}</div></div>
    <div class="pt-sum-i"><div class="pt-sum-l">MAX PROFIT:</div><div class="pt-sum-v" style="color:${G}">&#8599; ${mp}</div></div>
    <div class="pt-sum-i"><div class="pt-sum-l">CHANCE OF PROFIT:</div><div class="pt-sum-v">&#127922; ${a.cop>0?a.cop.toFixed(0)+'%':'-'}</div></div>
    <div class="pt-sum-i"><div class="pt-sum-l">BREAKEVEN:</div><div class="pt-sum-v" style="font-size:12px">&#8594; ${bs}</div></div>
    <div class="pt-sum-i"><div class="pt-sum-l">UNREALIZED:</div><div class="pt-sum-v" style="${cls(a.unrealizedPnl)}">&#128293; ${money(a.unrealizedPnl,2)} (${a.unrealizedPct<0?'-':'+'}${Math.abs(a.unrealizedPct).toFixed(1)}%)</div></div>
  </div>`;
}

// ── Self-contained P/L renderer (uses same CSS classes as Positions tab) ──
let ptRange=10, ptVM='pnl', ptCustomPrice=null;

function renderPL(strat){
  let h='<div style="background:#0d1117">';
  // Tab bar
  h+=`<div class="tab-bar" style="padding:2px 16px">
    <div class="tab${bViewMode==='table'?' active':''}" onclick="PT.setView('table')">Table</div>
    <div class="tab${bViewMode==='graph'?' active':''}" onclick="PT.setView('graph')">Graph</div></div>`;
  h+=`<div class="value-tabs" style="padding:2px 16px">
    <div class="vtab${ptVM==='pnl'?' active':''}" onclick="PT.setVM('pnl')">Profit / Loss $</div>
    <div class="vtab${ptVM==='pnlpct'?' active':''}" onclick="PT.setVM('pnlpct')">Profit / Loss %</div>
    <div class="vtab${ptVM==='value'?' active':''}" onclick="PT.setVM('value')">Contract Value</div>
    <div class="vtab${ptVM==='maxrisk'?' active':''}" onclick="PT.setVM('maxrisk')">% of Max Risk</div></div>`;

  // Compute PL matrix, then ALWAYS resample to exactly 20 cols x 20 rows
  let m=null,a=null;
  if(strat.legs&&strat.legs.length){try{m=computePLMatrix(_az(strat),ptRange)}catch(e){};try{a=_A(strat)}catch(e){}}
  if(!m||!m.prices||!m.dateLabels||!m.dates){h+='<div style="padding:20px;text-align:center;color:#8b949e">Add legs to see P/L</div>';h+='</div>';return h}
  // ── ALWAYS resample to exactly 20 cols using BSM pricing ──
  const NC=20;
  if(m.dates.length!==NC){
    const od=m.dates;
    const t0=od[0].getTime(),tN=od[od.length-1].getTime(),tR=tN-t0||1;
    const totalH=tR/3600000;
    const expDate=nyClose(strat.expiration);
    const iv=strat.iv||0.3,r=strat.risk_free_rate||0.045,S=strat.current_price;
    // Pre-compute leg adjustments (market vs BSM at current time)
    const T_now=Math.max(0.001,(expDate-new Date())/86400000/365);
    const legAdj=strat.legs.map(l=>{
      const bsm0=bsmPrice(S,l.strike,T_now,r,iv,l.type);
      return(l.mid||l.current||bsm0)-bsm0;
    });
    const nd=[],nl=[],nm=[],nv=[];
    for(let c=0;c<NC;c++){
      const t=t0+c/(NC-1)*tR;const dt=new Date(t);nd.push(dt);
      if(totalH<120)nl.push(dt.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'}).replace(' ',''));
      else nl.push(dt.getDate().toString());
      nm.push(dt.toLocaleDateString('en-US',{month:'short'}));
    }
    // BSM price each cell: for each price row × each time col
    m.prices.forEach((price,pi)=>{
      const row=[];
      nd.forEach((dt,ci)=>{
        const dte2=Math.max(0,(expDate-dt)/86400000);
        const T2=dte2/365;
        const decay=T_now>0?Math.min(1,dte2/(T_now*365)):0;
        let pnl=0;
        strat.legs.forEach((l,li)=>{
          if(l.type==='S'){pnl+=(price-l.entry)*l.qty;return}
          const tp=T2>0.0001?bsmPrice(price,l.strike,T2,r,iv,l.type):Math.max(0,l.type==='C'?price-l.strike:l.strike-price);
          pnl+=(tp+legAdj[li]*decay-l.entry)*l.qty*_mult(l);
        });
        row.push(Math.round(pnl));
      });
      nv.push(row);
    });
    m.dateLabels=nl;m.monthLabels=nm;m.dates=nd;m.values=nv;
    if(strat.earnings_date){const eT=new Date(strat.earnings_date+'T16:00:00').getTime();let bi=0;nd.forEach((d,i)=>{if(Math.abs(d.getTime()-eT)<Math.abs(nd[bi].getTime()-eT))bi=i});m.earningsColIdx=bi}
  }

  const nd=Math.abs(a?a.netDebit:0), ml2=(a&&isFinite(a.maxLoss))?Math.abs(a.maxLoss):0;
  let mx=0;m.values.forEach(r=>r.forEach(v=>{if(Math.abs(v)>mx)mx=Math.abs(v)}));
  const S=strat.current_price||1;

  // Month headers
  let monthSpans=[],cs2={month:'',count:0};
  m.monthLabels.forEach(ml=>{if(ml!==cs2.month){if(cs2.count)monthSpans.push({...cs2});cs2={month:ml,count:1}}else cs2.count++});
  if(cs2.count)monthSpans.push(cs2);

  if(bViewMode==='graph'){
    h+=`<div class="graph-container" style="height:320px;padding:4px;max-width:100%"><canvas id="pt-graph-cv" style="width:100%;height:100%"></canvas></div>`;
    setTimeout(()=>{
      _ptDrawGraph(strat,a,m);
      // Update date slider
      const ds=document.getElementById('pt-dateSlider');if(ds)ds.max=NC-1;
      const dw=document.getElementById('pt-dateSliderWrap');if(dw)dw.style.display='';
      // Set date label WITHOUT re-rendering (avoid infinite loop)
      const expD=nyClose(strat.expiration),nowD=new Date();
      const tFrac=ptDateIdx/Math.max(1,NC-1);
      const tDate=new Date(nowD.getTime()+tFrac*(expD.getTime()-nowD.getTime()));
      const dRem=Math.max(0,(expD-tDate)/86400000);
      const el=document.getElementById('pt-dateVal');
      if(el)el.textContent=tDate.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'})+' ('+(dRem<1?dRem.toFixed(1):Math.round(dRem))+'d remaining)';
    },0);
  } else {
  // Table view
  h+=`<div class="pl-table-wrap" style="max-width:100%;margin:0 auto;overflow-x:auto"><table class="pl-table" style="table-layout:fixed;width:100%">`;
  h+='<thead><tr><th class="price-col" rowspan="2" style="vertical-align:bottom">Price</th><th class="pct-col" rowspan="2" style="vertical-align:bottom">%</th>';
  monthSpans.forEach(ms=>{h+=`<th class="month-header" colspan="${ms.count}">${ms.month}</th>`});
  h+='</tr><tr>';
  m.dateLabels.forEach((dl,i)=>{
    const isE=i===m.earningsColIdx;
    h+=`<th${isE?' style="color:var(--yellow)"':''}>${dl}${isE?' &#128227;':''}</th>`});
  h+='</tr></thead><tbody>';

  m.prices.forEach((price,pi)=>{
    const isCur=pi===m.currentIdx;
    const pctVal=((price-S)/S*100);
    h+=`<tr${isCur?' class="current-price-row"':''}>`;
    h+=`<td class="price-col">${isCur?'&#9654; ':''}$${price.toFixed(price>=100?0:(price>=10?1:2))}</td>`;
    h+=`<td class="pct-col">${(pctVal>=0?'+':'')+pctVal.toFixed(1)}%</td>`;
    m.values[pi].forEach((val,di)=>{
      let dv;
      if(ptVM==='pnl')dv=Math.round(val).toLocaleString();
      else if(ptVM==='pnlpct')dv=nd>0?(val/nd*100).toFixed(0)+'%':'N/A';
      else if(ptVM==='value')dv=(val+nd).toFixed(0);
      else dv=ml2>0?(val/ml2*100).toFixed(0)+'%':'N/A';
      const isE=di===m.earningsColIdx;
      h+=`<td style="background:${pnlColor(val,mx)};color:${Math.abs(val)<mx*0.05?'var(--text2)':'#fff'}"${isE?' class="earnings-col"':''}>${dv}</td>`});
    h+='</tr>'});
  h+='</tbody></table></div>';
  } // end table view

  // Controls bar — exact copy of Positions tab structure
  const _iv=strat.iv||0.3;
  const emPct=_iv*Math.sqrt(Math.max(1,(nyClose(strat.expiration)-new Date())/86400000)/365)*100;
  const emDol=S*emPct/100;
  const ivPct=Math.round(_iv*100);
  h+=`<div class="controls-bar">
    <div class="control-group">
      <span class="control-label">RANGE:</span>
      <span class="control-value" id="pt-rangeVal">&plusmn;${ptRange}%</span>
      <input type="range" id="pt-rangeSlider" min="2" max="50" value="${ptRange}" step="1"
        oninput="document.getElementById('pt-rangeVal').textContent='±'+this.value+'%'"
        onchange="PT.setRange(+this.value)">
      <span style="font-size:11px;color:var(--text2);margin-left:4px">EM: &plusmn;$${emDol.toFixed(2)} (${emPct.toFixed(1)}%)</span>
    </div>
    <div class="control-group">
      <span class="control-label">PRICE:</span>
      <input type="number" id="pt-priceInput" style="width:70px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:12px" placeholder="Center" value="${ptCustomPrice||''}" onkeydown="if(event.key==='Enter')PT.goPrice()">
      <button onclick="PT.goPrice()" style="background:var(--accent);color:#fff;border:none;border-radius:4px;padding:3px 8px;font-size:11px;cursor:pointer">Go</button>
      <button id="pt-priceClearBtn" onclick="PT.clearPrice()" style="background:var(--bg3);color:var(--text2);border:1px solid var(--border);border-radius:4px;padding:2px 8px;font-size:10px;cursor:pointer;display:${ptCustomPrice?'':'none'}">Clear</button>
    </div>
    <div class="control-group">
      <span class="control-label">IMPLIED VOLATILITY:</span>
      <span class="control-value" id="pt-ivVal">${ivPct}%</span>
      <input type="range" id="pt-ivSlider" min="5" max="150" value="${ivPct}" step="1"
        oninput="document.getElementById('pt-ivVal').textContent=this.value+'%'"
        onchange="PT.setIV(+this.value)">
    </div>
  </div>`;
  // Date slider (for graph view)
  h+=`<div class="date-slider-wrap" id="pt-dateSliderWrap" style="${bViewMode==='graph'?'':'display:none'}">
    <span class="control-label">DATE:</span>
    <span class="control-value" id="pt-dateVal"></span>
    <input type="range" id="pt-dateSlider" min="0" max="19" value="0"
      oninput="PT.setDateIdx(+this.value)"
      onchange="PT.setDateIdx(+this.value)">
  </div>`;

  // Greeks
  if(a){h+=`<div class="greeks-bar" style="padding:4px 16px;font-size:11px">
    <div>&Delta; <span style="font-weight:700;${cls(a.greeks.delta)}">${a.greeks.delta.toFixed(2)}</span></div>
    <div>&Gamma; <span style="font-weight:700">${a.greeks.gamma.toFixed(4)}</span></div>
    <div>&Theta; <span style="font-weight:700;${cls(a.greeks.theta)}">${a.greeks.theta.toFixed(2)}</span></div>
    <div>Vega <span style="font-weight:700">${a.greeks.vega.toFixed(2)}</span></div>
    <div>&rho; <span style="font-weight:700">${a.greeks.rho.toFixed(2)}</span></div></div>`}
  h+='</div>';
  return h;
}
function setVM(vm){ptVM=vm;if(view==='build')renderBuild();else if(view==='detail')renderDetail()}
function setRange(v){ptRange=v;if(view==='build')renderBuild();else if(view==='detail')renderDetail()}
function setIV(v){bIV=v/100;if(view==='build')renderBuild();else if(view==='detail')renderDetail()}
function goPrice(){
  const inp=document.getElementById('pt-priceInput');if(!inp)return;
  const v=parseFloat(inp.value);if(!v||v<=0)return;
  ptCustomPrice=v;
  document.getElementById('pt-priceClearBtn').style.display='';
  if(view==='build')renderBuild();else if(view==='detail')renderDetail();
}
function clearPrice(){
  ptCustomPrice=null;
  const inp=document.getElementById('pt-priceInput');if(inp)inp.value='';
  const btn=document.getElementById('pt-priceClearBtn');if(btn)btn.style.display='none';
  if(view==='build')renderBuild();else if(view==='detail')renderDetail();
}
let ptDateIdx=0;
function setDateIdx(idx){
  ptDateIdx=idx;
  const s=view==='build'?mkBuildStrat():(dTrade?mkStrat(dTrade):null);
  if(!s)return;
  // Update date label
  const expDate=nyClose(s.expiration),nowD=new Date();
  const t=new Date(nowD.getTime()+(idx/19)*(expDate.getTime()-nowD.getTime()));
  const dRem=Math.max(0,(expDate-t)/86400000);
  const el=document.getElementById('pt-dateVal');
  if(el)el.textContent=t.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'})+' ('+(dRem<1?dRem.toFixed(1):Math.round(dRem))+'d remaining)';
  // Redraw graph canvas only (no full re-render to avoid loop)
  if(bViewMode==='graph'){
    let m=null,a=null;
    try{m=computePLMatrix(_az(s),ptRange)}catch(e){}
    try{a=_A(s)}catch(e){}
    if(m)_ptDrawGraph(s,a,m);
  }
}
function setView(v){bViewMode=v;if(view==='build')renderBuild();else if(view==='detail')renderDetail()}

// ── Simple P/L graph (expiration payoff + current curve) ──
function _ptDrawGraph(strat,a,m){
  const cv=document.getElementById('pt-graph-cv');if(!cv)return;
  const ctx=cv.getContext('2d');
  const dpr=window.devicePixelRatio||1;
  const rect=cv.parentElement.getBoundingClientRect();
  cv.width=rect.width*dpr;cv.height=rect.height*dpr;
  cv.style.width=rect.width+'px';cv.style.height=rect.height+'px';
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const W=rect.width,H=rect.height;
  const pad={top:20,right:20,bottom:30,left:60};
  const gW=W-pad.left-pad.right,gH=H-pad.top-pad.bottom;
  if(!m||!m.prices||!m.values)return;

  // Selected date column (from DATE slider) and expiration column (last)
  const curCol=Math.min(ptDateIdx,m.values[0].length-1);
  const expCol=m.values[0].length-1;
  const prices=m.prices;
  let minV=Infinity,maxV=-Infinity;
  prices.forEach((p,i)=>{const ve=m.values[i][expCol],vc=m.values[i][curCol];
    minV=Math.min(minV,ve,vc);maxV=Math.max(maxV,ve,vc)});
  const vRng=maxV-minV||1;
  const pMin=prices[prices.length-1],pMax=prices[0],pRng=pMax-pMin||1;
  const sx=p=>pad.left+(p-pMin)/pRng*gW;
  const sy=v=>pad.top+(maxV-v)/vRng*gH;

  // Background
  ctx.fillStyle='#0d1117';ctx.fillRect(0,0,W,H);
  // Grid
  ctx.strokeStyle='#21262d';ctx.lineWidth=0.5;
  for(let i=0;i<=4;i++){const y=pad.top+gH*i/4;ctx.beginPath();ctx.moveTo(pad.left,y);ctx.lineTo(W-pad.right,y);ctx.stroke()}
  // Zero line
  if(minV<0&&maxV>0){ctx.strokeStyle='#8b949e';ctx.lineWidth=1;ctx.setLineDash([4,4]);const y0=sy(0);ctx.beginPath();ctx.moveTo(pad.left,y0);ctx.lineTo(W-pad.right,y0);ctx.stroke();ctx.setLineDash([])}
  // Price labels
  ctx.fillStyle='#8b949e';ctx.font='10px sans-serif';ctx.textAlign='center';
  for(let i=0;i<=5;i++){const p=pMin+pRng*i/5;ctx.fillText('$'+Math.round(p),sx(p),H-8)}
  // Value labels
  ctx.textAlign='right';
  for(let i=0;i<=4;i++){const v=minV+vRng*i/4;ctx.fillText('$'+Math.round(v),pad.left-4,sy(v)+3)}

  // Expiration curve (red dashed)
  ctx.strokeStyle='#f85149';ctx.lineWidth=2;ctx.setLineDash([6,4]);
  ctx.beginPath();
  prices.forEach((p,i)=>{const x=sx(p),y=sy(m.values[i][expCol]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  ctx.stroke();ctx.setLineDash([]);

  // Current curve (green solid) with fill
  ctx.strokeStyle='#3fb950';ctx.lineWidth=2;
  ctx.beginPath();
  prices.forEach((p,i)=>{const x=sx(p),y=sy(m.values[i][curCol]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  ctx.stroke();
  // Fill profit area
  ctx.globalAlpha=0.15;
  const y0=sy(0);
  ctx.fillStyle='#3fb950';ctx.beginPath();ctx.moveTo(sx(prices[0]),y0);
  prices.forEach((p,i)=>{const v=m.values[i][curCol];if(v>0)ctx.lineTo(sx(p),sy(v));else ctx.lineTo(sx(p),y0)});
  ctx.lineTo(sx(prices[prices.length-1]),y0);ctx.closePath();ctx.fill();
  ctx.fillStyle='#f85149';ctx.beginPath();ctx.moveTo(sx(prices[0]),y0);
  prices.forEach((p,i)=>{const v=m.values[i][curCol];if(v<0)ctx.lineTo(sx(p),sy(v));else ctx.lineTo(sx(p),y0)});
  ctx.lineTo(sx(prices[prices.length-1]),y0);ctx.closePath();ctx.fill();
  ctx.globalAlpha=1;

  // Current price marker
  const S=strat.current_price;
  if(S>=pMin&&S<=pMax){ctx.strokeStyle='#58a6ff';ctx.lineWidth=1;ctx.setLineDash([2,2]);ctx.beginPath();ctx.moveTo(sx(S),pad.top);ctx.lineTo(sx(S),H-pad.bottom);ctx.stroke();ctx.setLineDash([])}

  // Legend
  ctx.font='11px sans-serif';
  ctx.fillStyle='#3fb950';ctx.fillRect(W-140,8,12,3);ctx.fillText('Current',W-122,13);
  ctx.fillStyle='#f85149';ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(W-140,24);ctx.lineTo(W-128,24);ctx.strokeStyle='#f85149';ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle='#f85149';ctx.fillText('Expiration',W-122,28);
}

// ═══════════════════════════════════════════════════════════════════
// DETAIL VIEW
// ═══════════════════════════════════════════════════════════════════
function renderDetail(){
  if(!dTrade){renderList();return}view='detail';const t=dTrade,s=mkStrat(t),open=t.status==='active';
  let a=null;if(!open||t._marked){try{a=_A(s)}catch(e){}}
  const px=+s.current_price||0,dd=dte(t.expiration);
  let h=`<div class="pt-dhdr">
    <button class="pt-btn-out" onclick="PT.back()">&#8592; Back</button>
    <div><div style="font-size:16px;font-weight:700">${esc(dispName(t))}</div>
    <div style="font-size:12px;color:#8b949e">${esc(t.underlying)} $${px.toFixed(2)}${_stockOnly(t.legs)?'':' &middot; expires '+esc(t.expiration)+(dd==null?'':' ('+dd+'d)')} &middot; ${esc(t.group||'Default')} &middot; opened ${fmtD(t.created_at)}${open?'':' &middot; closed '+fmtD(t.closed_at)}</div></div>
    <span style="flex:1"></span>
    ${open?`<button class="pt-btn-out" onclick="PT.refresh()">&#8635; Refresh</button><button class="pt-btn pt-btn-red" onclick="PT.closeTr('${esc(t.id)}')">Close at market</button>`:'<span class="pt-badge" style="background:#d2992233;color:#d29922">CLOSED</span>'}
  </div>`;
  if(open&&!t._marked)h+=`<div class="pt-note">No quote yet for every leg${marksErr?' ('+esc(marksErr)+')':''}. P/L appears once quotes load; try &#8635; Refresh.</div>`;
  h+='<div class="pt-legs">';
  s.legs.forEach(l=>{
    const ac=l.qty>0?'buy':'sell',an=l.qty>0?'BUY':'SELL',lp=(l.current-l.entry)*l.qty*l.mult;
    h+=`<div class="pt-leg"><span class="act ${ac}">${an}</span><span style="font-weight:700">${Math.abs(l.qty)}</span>
      ${l.type==='S'?`<span class="sk">${esc(t.underlying)}</span><span class="tp">shares</span>`:`<span class="sk">${l.strike}</span><span class="tp">${l.type}</span>`}
      <span style="font-size:10px;color:#8b949e">Entry $${(+l.entry||0).toFixed(2)}</span>
      <span class="pr">$${(+l.current||0).toFixed(2)}</span>
      <span style="font-family:monospace;font-size:11px;${cls(lp)}">${money(lp)}</span></div>`});
  h+='</div>';
  if(a){
    h+=renderSummary(s);
    h+=`<div class="pt-greeks">
      <div><span class="l">&Delta;</span><span class="v" style="${cls(a.greeks.delta)}">${a.greeks.delta.toFixed(2)}</span></div>
      <div><span class="l">&Gamma;</span><span class="v">${a.greeks.gamma.toFixed(4)}</span></div>
      <div><span class="l">&Theta;</span><span class="v" style="${cls(a.greeks.theta)}">${a.greeks.theta.toFixed(2)}</span></div>
      <div><span class="l">V</span><span class="v">${a.greeks.vega.toFixed(2)}</span></div>
      <div><span class="l">&rho;</span><span class="v">${a.greeks.rho.toFixed(2)}</span></div></div>`}
  h+=renderPL(s);
  C.innerHTML=h;
}

// ═══════════════════════════════════════════════════════════════════
// LEG POPUP
// ═══════════════════════════════════════════════════════════════════
function legPop(idx){if(_dragged){_dragged=false;return} // skip if just dragged
  const l=bLegs[idx];if(!l)return;
  if(l.type==='S'){
    let ov=document.getElementById('pt-mo');if(!ov){ov=document.createElement('div');ov.id='pt-mo';ov.className='pt-modal-ov';ov.onclick=e=>{if(e.target===ov)ov.remove()};document.body.appendChild(ov)}
    ov.innerHTML=`<div class="pt-modal"><h3>${esc(bTicker)} shares</h3>
      <div style="display:flex;justify-content:space-between;margin:10px 0">
        <div><div style="color:#8b949e;font-size:10px;text-transform:uppercase">Shares</div>
          <div class="pt-leg-qty" style="margin-top:4px"><button onclick="PT._mq(${idx},-1)">&#9664;</button><span style="font-size:16px;font-weight:700;${cls(l.qty)}">${l.qty}</span><button onclick="PT._mq(${idx},1)">&#9654;</button></div></div>
        <div style="text-align:right"><div style="color:#8b949e;font-size:10px;text-transform:uppercase">Price</div>
          <div style="font-size:20px;font-weight:700;margin-top:4px">$${(l.mid||0).toFixed(2)}</div></div></div>
      <div class="pt-modal-a">
        <div class="pt-modal-ai" onclick="PT._mflip(${idx})">&#8597; ${l.qty>0?'Make it a short sale':'Make it a purchase'}</div>
        <div class="pt-modal-ai" onclick="PT.rmLeg(${idx});document.getElementById('pt-mo').remove()"><span style="color:${R}">&#10005; Remove</span></div>
      </div></div>`;
    return;
  }
  const warns=[];
  if(l.oi!==undefined&&l.oi<10)warns.push('Low open-interest ('+l.oi+')');
  if(l.vol!==undefined&&l.vol<5)warns.push('Low volume ('+l.vol+')');
  if(l.bid>0&&l.ask>0){const sp=((l.ask-l.bid)/(l.mid||1)*100);if(sp>10)warns.push('Wide bid/ask spread ('+sp.toFixed(0)+'%)')}
  if(!l.iv||l.iv===0)warns.push('IV could not be calculated');
  let ov=document.getElementById('pt-mo');if(!ov){ov=document.createElement('div');ov.id='pt-mo';ov.className='pt-modal-ov';ov.onclick=e=>{if(e.target===ov)ov.remove()};document.body.appendChild(ov)}
  const exp=(l.exp||bExp||'').replace(/-/g,'/').slice(2);
  let h=`<div class="pt-modal"><h3>${bTicker} ${l.strike}${l.type} ${exp}</h3>`;
  if(warns.length)h+=`<div class="pt-modal-warn">&#9888; ${warns.join(', ')}</div>`;
  h+=`<div style="display:flex;justify-content:space-between;margin:10px 0">
    <div><div style="color:#8b949e;font-size:10px;text-transform:uppercase">Quantity</div>
      <div class="pt-leg-qty" style="margin-top:4px"><button onclick="PT._mq(${idx},-1)">&#9664;</button><span style="font-size:16px;font-weight:700;${cls(l.qty)}">${l.qty}</span><button onclick="PT._mq(${idx},1)">&#9654;</button></div></div>
    <div style="text-align:right"><div style="color:#8b949e;font-size:10px;text-transform:uppercase">Price</div>
      <div style="font-size:20px;font-weight:700;margin-top:4px">$${(l.mid||0).toFixed(2)}</div></div></div>`;
  h+=`<div class="pt-modal-g">
    <div class="lb">Bid</div><div class="vl" style="color:${G}">$${(l.bid||0).toFixed(2)}</div>
    <div class="lb">Ask</div><div class="vl" style="color:${R}">$${(l.ask||0).toFixed(2)}</div>
    <div class="lb">Volume</div><div class="vl">${l.vol!==undefined?l.vol:'-'}</div>
    <div class="lb">OI</div><div class="vl">${l.oi!==undefined?l.oi:'-'}</div>
    <div class="lb">IV</div><div class="vl">${l.iv?(l.iv*100).toFixed(1)+'%':'&#8212;'}</div>
    <div class="lb">Delta</div><div class="vl">${l.delta?l.delta.toFixed(4):'0.0000'}</div>
    <div class="lb">Theta</div><div class="vl">${l.theta?l.theta.toFixed(4):'0.0000'}</div>
    <div class="lb">Gamma</div><div class="vl">${l.gamma?l.gamma.toFixed(4):'0.0000'}</div>
    <div class="lb">Vega</div><div class="vl">${l.vega?l.vega.toFixed(4):'0.0000'}</div>
    <div class="lb">Rho</div><div class="vl">${l.rho?l.rho.toFixed(4):'0.0000'}</div></div>`;
  h+=`<div class="pt-modal-a">
    <div class="pt-modal-ai" onclick="PT._mswitch(${idx})">&#8644; Switch to ${l.type==='C'?'Put':'Call'}</div>
    <div class="pt-modal-ai" onclick="PT._mflip(${idx})">&#8597; ${l.qty>0?'Sell to Open':'Buy to Open'}</div>
    <div class="pt-modal-ai" onclick="PT.rmLeg(${idx});document.getElementById('pt-mo').remove()"><span style="color:${R}">&#10005; Remove</span></div>
  </div></div>`;
  ov.innerHTML=h;
}

// ═══════════════════════════════════════════════════════════════════
// ACTIONS
// ═══════════════════════════════════════════════════════════════════
function setGrp(g){selGroup=g||null;renderList()}
function setSort(c){sortCol=c;renderList()}
function setFilt(s){filterSt=s;renderList()}
function tgSort(c){if(sortCol===c)sortAsc=!sortAsc;else{sortCol=c;sortAsc=c==='name'}renderList()}
function addGrp(){const n=(prompt('New group name:')||'').trim();if(!n)return;api('/api/paper-trade/groups',{method:'POST',body:{action:'create',name:n}}).then(gs=>{groups=gs;selGroup=n;renderList()}).catch(e=>alert('Could not add the group: '+e.message))}
function gear(ev,id){
  document.querySelectorAll('.pt-gmenu').forEach(m=>m.remove());
  const td=ev.target.closest('td'),m=document.createElement('div');m.className='pt-gmenu';
  m.innerHTML=`<div class="pt-gmenu-i" onclick="PT.detail('${id}')">View Details</div>
    <div class="pt-gmenu-i" onclick="PT.chgGrp('${id}')">Change Group</div>
    <div class="pt-gmenu-i dng" onclick="PT.closeTr('${id}')">Close Trade</div>
    <div class="pt-gmenu-i dng" onclick="PT.delTr('${id}')">Delete</div>`;
  td.appendChild(m);setTimeout(()=>document.addEventListener('click',function _r(){m.remove();document.removeEventListener('click',_r)}),10);
}

// Zoom
function setZoom(v){sbarZoom=v;renderBuild()}

// Build strategy mega-menu toggle
function tgBuild(){
  const m=document.getElementById('pt-build-menu');if(!m)return;
  const show=m.style.display==='none';m.style.display=show?'flex':'none';
  if(show){setTimeout(()=>document.addEventListener('click',function _cl(e){
    if(!e.target.closest('.pt-build-wrap')){const m2=document.getElementById('pt-build-menu');if(m2)m2.style.display='none';document.removeEventListener('click',_cl)}
  }),10)}
}

// Strategy templates — all strategies, ATM-based
function _mkLeg(so,type,qty){
  const side=type==='C'?so.call:so.put;
  return{strike:so.strike,type,qty,symbol:side.symbol||'',streamer:side.streamer||'',
    bid:side.bid,ask:side.ask,mid:side.mid,vol:side.vol,oi:side.oi,iv:side.iv};
}
const TPL={long_call:'Long Call',long_put:'Long Put',covered_call:'Covered Call',cash_secured_put:'Cash-Secured Put',protective_put:'Protective Put',
  bull_put:'Bull Put Spread',bear_call:'Bear Call Spread',iron_butterfly:'Iron Butterfly',iron_condor:'Iron Condor',long_put_bfly:'Long Put Butterfly',
  long_call_bfly:'Long Call Butterfly',bull_call:'Bull Call Spread',bear_put:'Bear Put Spread',straddle:'Long Straddle',strangle:'Long Strangle',
  short_put:'Short Put',short_call:'Short Call',short_straddle:'Short Straddle',short_strangle:'Short Strangle',call_ratio_back:'Call Ratio Backspread',
  put_ratio_back:'Put Ratio Backspread',jade_lizard:'Jade Lizard',reverse_jade:'Reverse Jade Lizard'};
function tpl(name){tplN(name)}
function tplN(name){
  if(!bChain||!bChain.strikes)return;
  // Close menu
  const bm=document.getElementById('pt-build-menu');if(bm)bm.style.display='none';
  const st=bChain.strikes;
  const atm=st.reduce((a,b)=>Math.abs(b.strike-bPrice)<Math.abs(a.strike-bPrice)?b:a);
  const ai=st.indexOf(atm);
  const o=n=>st[Math.min(Math.max(ai+n,0),st.length-1)];
  bLegs=[];
  const T={
    // Novice
    long_call:()=>{bLegs.push(_mkLeg(atm,'C',1))},
    long_put:()=>{bLegs.push(_mkLeg(atm,'P',1))},
    covered_call:()=>{bLegs.push({strike:0,type:'S',qty:100,symbol:bTicker,streamer:bTicker,bid:bPrice,ask:bPrice,mid:bPrice,vol:0,oi:0,iv:0});bLegs.push(_mkLeg(o(2),'C',-1))},
    cash_secured_put:()=>{bLegs.push(_mkLeg(o(-2),'P',-1))},
    protective_put:()=>{bLegs.push({strike:0,type:'S',qty:100,symbol:bTicker,streamer:bTicker,bid:bPrice,ask:bPrice,mid:bPrice,vol:0,oi:0,iv:0});bLegs.push(_mkLeg(o(-2),'P',1))},
    // Intermediate — Credit
    bull_put:()=>{bLegs.push(_mkLeg(o(-1),'P',-1));bLegs.push(_mkLeg(o(-3),'P',1))},
    bear_call:()=>{bLegs.push(_mkLeg(o(1),'C',-1));bLegs.push(_mkLeg(o(3),'C',1))},
    // Neutral
    iron_butterfly:()=>{bLegs.push(_mkLeg(atm,'P',-1));bLegs.push(_mkLeg(o(-3),'P',1));bLegs.push(_mkLeg(atm,'C',-1));bLegs.push(_mkLeg(o(3),'C',1))},
    iron_condor:()=>{bLegs.push(_mkLeg(o(-1),'P',-1));bLegs.push(_mkLeg(o(-3),'P',1));bLegs.push(_mkLeg(o(1),'C',-1));bLegs.push(_mkLeg(o(3),'C',1))},
    long_put_bfly:()=>{bLegs.push(_mkLeg(o(-3),'P',1));bLegs.push(_mkLeg(atm,'P',-2));bLegs.push(_mkLeg(o(3),'P',1))},
    long_call_bfly:()=>{bLegs.push(_mkLeg(o(-3),'C',1));bLegs.push(_mkLeg(atm,'C',-2));bLegs.push(_mkLeg(o(3),'C',1))},
    // Debit
    bull_call:()=>{bLegs.push(_mkLeg(atm,'C',1));bLegs.push(_mkLeg(o(3),'C',-1))},
    bear_put:()=>{bLegs.push(_mkLeg(atm,'P',1));bLegs.push(_mkLeg(o(-3),'P',-1))},
    // Directional
    straddle:()=>{bLegs.push(_mkLeg(atm,'C',1));bLegs.push(_mkLeg(atm,'P',1))},
    strangle:()=>{bLegs.push(_mkLeg(o(2),'C',1));bLegs.push(_mkLeg(o(-2),'P',1))},
    // Advanced — Naked
    short_put:()=>{bLegs.push(_mkLeg(o(-2),'P',-1))},
    short_call:()=>{bLegs.push(_mkLeg(o(2),'C',-1))},
    short_straddle:()=>{bLegs.push(_mkLeg(atm,'C',-1));bLegs.push(_mkLeg(atm,'P',-1))},
    short_strangle:()=>{bLegs.push(_mkLeg(o(2),'C',-1));bLegs.push(_mkLeg(o(-2),'P',-1))},
    call_ratio_back:()=>{bLegs.push(_mkLeg(atm,'C',-1));bLegs.push(_mkLeg(o(3),'C',2))},
    put_ratio_back:()=>{bLegs.push(_mkLeg(atm,'P',-1));bLegs.push(_mkLeg(o(-3),'P',2))},
    jade_lizard:()=>{bLegs.push(_mkLeg(o(2),'C',-1));bLegs.push(_mkLeg(o(-1),'P',-1));bLegs.push(_mkLeg(o(-3),'P',1))},
    reverse_jade:()=>{bLegs.push(_mkLeg(o(-2),'P',-1));bLegs.push(_mkLeg(o(1),'C',-1));bLegs.push(_mkLeg(o(3),'C',1))},
  };
  if(T[name]){T[name]();bName=TPL[name]||''}
  renderBuild();
}

// Build
function newTrade(){bTicker='';bPrice=0;bPrev=0;bChgPct=0;bName='';bChain=null;bLegs=[];bExp='';bExps=[];bLoading=false;sbarZoom=100;bViewMode='table';ptRange=10;ptCustomPrice=null;renderBuild();setTimeout(()=>{const i=document.getElementById('pt-tk');if(i)i.focus()},50)}
function tgAdd(){
  const m=document.getElementById('pt-add-menu');if(!m)return;
  const show=m.style.display==='none';
  m.style.display=show?'block':'none';
  if(show){setTimeout(()=>document.addEventListener('click',function _cl(e){
    if(!e.target.closest('.pt-add-wrap')){const m2=document.getElementById('pt-add-menu');if(m2)m2.style.display='none';document.removeEventListener('click',_cl)}
  }),10)}
}
function addAtm(type,qty){
  if(!bChain||!bChain.strikes)return;
  const st=bChain.strikes;
  const atm=st.reduce((a,b)=>Math.abs(b.strike-bPrice)<Math.abs(a.strike-bPrice)?b:a);
  let strike=atm.strike;
  // For sell legs, auto-offset OTM to make a spread
  if(qty<0&&bLegs.length>0){
    const ai=st.indexOf(atm);
    if(type==='C'){const t=st[Math.min(ai+3,st.length-1)];if(t)strike=t.strike}
    else{const t=st[Math.max(ai-3,0)];if(t)strike=t.strike}
  }
  // Close menu
  const m=document.getElementById('pt-add-menu');if(m)m.style.display='none';
  addFromBar(strike,type,qty);
}
function addStock(shares){
  const m=document.getElementById('pt-add-menu');if(m)m.style.display='none';
  bName='';// stock leg: type 'S', strike 0, qty = shares
  bLegs.push({strike:0,type:'S',qty:shares,symbol:bTicker,streamer:bTicker,bid:bPrice,ask:bPrice,mid:bPrice,vol:0,oi:0,iv:0});
  renderBuild();
}
async function _loadChain(tk){
  bTicker=tk;bLoading=true;bChain=null;bLegs=[];bExp='';bName='';bPrev=0;bChgPct=0;renderBuild();
  const d=await api('/api/paper-trade/chain/'+encodeURIComponent(tk));
  bPrice=d.price||0;bPrev=d.prev_close||0;bChgPct=bPrev>0&&bPrice>0?(bPrice-bPrev)/bPrev*100:0;
  bIV=d.iv||.3;bEarn={date:d.earnings_date||'',time:d.earnings_time||''};bExps=d.expirations||[];bChain=d;bLoading=false;
}
async function load(){
  const inp=document.getElementById('pt-tk');const tk=(inp?inp.value:bTicker).trim().toUpperCase();
  if(!tk)return;
  try{
    await _loadChain(tk);
    const tgt=bExps.find(e=>e.dte>=25&&e.dte<=50&&e.type==='Regular')||bExps.find(e=>e.dte>=14&&e.type==='Regular')||bExps[0];
    if(tgt)await selExp(tgt.date,'bull_call');else renderBuild();
  }catch(e){bLoading=false;renderBuild();alert('Could not load '+tk+': '+e.message)}
}
// Open a ready-made strategy, e.g. the spread the TA screener recommends.
// spec = {ticker, expiration, legs:[{type:'C'|'P'|'S', strike, qty}]}
async function loadSpec(sp){
  if(!sp||!sp.ticker)return;
  if(typeof switchTopTab==='function')switchTopTab('paper');
  injectCSS();
  const tk=String(sp.ticker).toUpperCase();
  try{
    await _loadChain(tk);
    const want=String(sp.expiration||''),wt=new Date(want+'T12:00:00').getTime();
    const tgt=bExps.find(e=>e.date===want)||(isNaN(wt)?bExps[0]:bExps.slice().sort((x,y)=>Math.abs(new Date(x.date+'T12:00:00')-wt)-Math.abs(new Date(y.date+'T12:00:00')-wt))[0]);
    if(tgt)await selExp(tgt.date);
    bLegs=[];
    const st=(bChain&&bChain.strikes)||[];
    (sp.legs||[]).forEach(l=>{
      if(l.type==='S'){bLegs.push({strike:0,type:'S',qty:+l.qty||100,symbol:bTicker,streamer:bTicker,bid:bPrice,ask:bPrice,mid:bPrice,vol:0,oi:0,iv:0});return}
      if(!st.length)return;
      const k=st.reduce((x,y)=>Math.abs(y.strike-l.strike)<Math.abs(x.strike-l.strike)?y:x).strike;
      addFromBar(k,l.type==='P'?'P':'C',+l.qty||1);
    });
    bName='';renderBuild();
  }catch(e){bLoading=false;renderBuild();alert('Could not load '+tk+': '+e.message)}
}
async function selExp(date,autoTpl){
  const prevLegs=bLegs.slice(); // preserve existing legs
  bExp=date;bChainLoading=true;renderBuild();
  try{const d=await api('/api/paper-trade/chain/'+bTicker+'?expiration='+date);bChain=d;if(d.price)bPrice=d.price;bChainLoading=false;
    // Remap preserved legs to nearest strikes in new chain
    if(prevLegs.length&&bChain.strikes&&bChain.strikes.length){
      bLegs=prevLegs.map(l=>{
        if(l.type==='S')return{...l,exp:date}; // stock leg unchanged
        const near=bChain.strikes.reduce((a,b)=>Math.abs(b.strike-l.strike)<Math.abs(a.strike-l.strike)?b:a);
        const side=l.type==='C'?near.call:near.put;
        return{...l,strike:near.strike,symbol:side.symbol||'',streamer:side.streamer||'',bid:side.bid,ask:side.ask,mid:side.mid,vol:side.vol,oi:side.oi,iv:side.iv,exp:date};
      });
      renderBuild();
    } else if(autoTpl&&bChain.strikes&&bChain.strikes.length)tplN(autoTpl);else renderBuild()}
  catch(e){bChainLoading=false;renderBuild()}
}
function addFromBar(strike,type,qty){
  if(!bChain||!bChain.strikes)return;
  const s=bChain.strikes.find(x=>x.strike===strike);if(!s)return;bName='';
  const side=type==='C'?s.call:s.put;
  const ex=bLegs.find(l=>l.strike===strike&&l.type===type);
  if(ex){ex.qty+=qty;if(ex.qty===0)bLegs=bLegs.filter(l=>l!==ex)}
  else bLegs.push({strike,type,qty,symbol:side.symbol||'',streamer:side.streamer||'',bid:side.bid,ask:side.ask,mid:side.mid,vol:side.vol,oi:side.oi,iv:side.iv});
  renderBuild();
}
function rmLeg(i){bLegs.splice(i,1);bName='';renderBuild()}
function adjQ(i,d){const st=bLegs[i].type==='S'?100:1;bLegs[i].qty+=d*st;if(bLegs[i].qty===0)bLegs[i].qty+=d*st;bName='';renderBuild()}

// ── Drag leg on strike bar ── (throttled to 100ms)
let _dragged=false;
function dragStart(ev,idx){
  ev.preventDefault();ev.stopPropagation();
  _dragged=false;
  const pill=ev.target.closest('.pt-sbar-leg');if(!pill)return;
  const bar=document.getElementById('pt-sbar-el');if(!bar)return;
  const leg=bLegs[idx];if(!leg)return;
  const st=bChain.strikes;if(!st||!st.length)return;
  const vMin=st[0].strike,vMax=st[st.length-1].strike,vRng=vMax-vMin||1;
  let lastStrike=leg.strike;
  const barRect=bar.getBoundingClientRect();

  function onMove(e){
    _dragged=true;
    const now=Date.now();if(now-_dragThrottle<60)return;_dragThrottle=now;
    const x=Math.max(0,Math.min(1,(e.clientX-barRect.left)/barRect.width));
    const price=vMin+x*vRng;
    const nearest=st.reduce((a,b)=>Math.abs(b.strike-price)<Math.abs(a.strike-price)?b:a);
    if(nearest.strike!==lastStrike){
      lastStrike=nearest.strike;
      const side=leg.type==='C'?nearest.call:nearest.put;
      leg.strike=nearest.strike;leg.symbol=side.symbol||'';leg.streamer=side.streamer||'';
      leg.bid=side.bid;leg.ask=side.ask;leg.mid=side.mid;leg.vol=side.vol;leg.oi=side.oi;leg.iv=side.iv;
      // Just move THIS pill, update leg card text and summary — no full re-render
      const pct=((nearest.strike-vMin)/vRng)*100;
      pill.style.left=pct+'%';
      pill.querySelector('.pt-sbar-leg-pill').textContent=nearest.strike+leg.type;
      // Update leg card inline
      const legEl=document.getElementById('pt-legs-el');
      if(legEl){const cards=legEl.children;if(cards[idx]){
        cards[idx].querySelector('.sk').textContent=nearest.strike;
        cards[idx].querySelector('.pr').textContent='$'+(leg.mid>0?leg.mid.toFixed(2):'--');
      }}
    }
  }
  function onUp(){
    document.removeEventListener('mousemove',onMove);
    document.removeEventListener('mouseup',onUp);
    if(_dragged){bName='';renderBuild()} // full re-render only on drop
  }
  document.addEventListener('mousemove',onMove);
  document.addEventListener('mouseup',onUp);
}
// ── Click price to edit ──
function editPrice(idx){
  const leg=bLegs[idx];if(!leg)return;
  const el=document.getElementById('pt-pr-'+idx);if(!el)return;
  const cur=leg.mid||0;
  el.innerHTML=`<input class="pt-price-edit" type="number" step="0.01" value="${cur.toFixed(2)}" id="pt-pre-${idx}"
    onblur="PT.savePrice(${idx})" onkeydown="if(event.key==='Enter')this.blur()">`;
  const inp=document.getElementById('pt-pre-'+idx);if(inp){inp.focus();inp.select()}
}
function savePrice(idx){
  const inp=document.getElementById('pt-pre-'+idx);if(!inp)return;
  const v=parseFloat(inp.value);if(!isNaN(v)&&v>=0){bLegs[idx].mid=v;bLegs[idx].entry=v}
  renderBuild();
}
function vm(m){valMode=m;if(view==='build')renderBuild();else if(view==='detail')renderDetail()}
async function place(){
  if(!bLegs.length)return;
  const noPx=bLegs.filter(l=>!(l.mid>0)).map(l=>l.type==='S'?bTicker+' shares':l.strike+l.type);
  if(noPx.length){alert('No price for '+noPx.join(', ')+'. Click the price on the leg to type one in, or pick another strike.');return}
  const td={underlying:bTicker,name:nameSt(bLegs),current_price:bPrice,iv:bIV,risk_free_rate:bRfr,
    earnings_date:bEarn.date,earnings_time:bEarn.time,expiration:bExp,group:selGroup||'Default',
    legs:bLegs.map(l=>({symbol:l.type==='S'?bTicker:(l.symbol||''),streamer_symbol:l.streamer||'',type:l.type,strike:l.type==='S'?0:l.strike,qty:l.qty,
      entry:l.mid||0,current:l.mid||0,exp:bExp,mult:_mult(l),bid:l.bid||0,ask:l.ask||0,mid:l.mid||0}))};
  try{await api('/api/paper-trade/open',{method:'POST',body:td});filterSt='active';await loadTr();renderList()}catch(e){alert('Could not save: '+e.message)}
}
function back(){renderList()}
function positions(){renderList()}

// Detail
function detail(id){dTrade=trades.find(t=>t.id===id);if(dTrade)renderDetail()}
async function closeTr(id){
  if(!confirm('Close this paper trade at the current market prices?'))return;
  try{await api('/api/paper-trade/close/'+encodeURIComponent(id),{method:'POST',body:{}})}
  catch(e){alert('Could not close: '+e.message);return}
  await loadTr();if(view==='detail')renderDetail();else renderList();
}
async function delTr(id){
  if(!confirm('Delete this paper trade permanently?'))return;
  try{await api('/api/paper-trade/'+encodeURIComponent(id),{method:'DELETE'})}catch(e){alert('Could not delete: '+e.message);return}
  if(dTrade&&dTrade.id===id){dTrade=null;view='list'}
  await loadTr();renderList();
}
async function chgGrp(id){
  const n=(prompt('Move to group:',selGroup||'Default')||'').trim();if(!n)return;
  try{await api('/api/paper-trade/'+encodeURIComponent(id),{method:'PUT',body:{group:n}})}catch(e){alert('Could not move: '+e.message);return}
  await loadTr();renderList();
}
function csv(){
  let fl=trades;if(selGroup)fl=fl.filter(t=>t.group===selGroup);if(filterSt!=='all')fl=fl.filter(t=>t.status===filterSt);
  const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
  let c="Name,Group,Status,Opened,Closed,Total Return,Today's Return,Max Profit,Max Loss,Chance of Profit,Expiration\n";
  fl.forEach(t=>{const open=t.status==='active',s=mkStrat(t);let a=null;if(!open||t._marked){try{a=_A(s)}catch(e){}}
    c+=[q(dispName(t)),q(t.group||'Default'),q(open?'open':'closed'),q(t.created_at||''),q(t.closed_at||''),
      q(a?a.unrealizedPnl.toFixed(2):''),q(open&&a&&!t._todayUnknown?a.todayPnl.toFixed(2):''),
      q(a?(isFinite(a.maxProfit)?a.maxProfit.toFixed(2):'unlimited'):''),q(a?(isFinite(a.maxLoss)?a.maxLoss.toFixed(2):'unlimited'):''),
      q(a&&a.cop>0?a.cop.toFixed(0)+'%':''),q(t.expiration||'')].join(',')+'\n'});
  const b=new Blob([c],{type:'text/csv'});const el=document.createElement('a');el.href=URL.createObjectURL(b);el.download='paper_trades_'+new Date().toISOString().slice(0,10)+'.csv';el.click();
}

// Modal helpers
function _mq(i,d){const st=bLegs[i].type==='S'?100:1;bLegs[i].qty+=d*st;if(bLegs[i].qty===0)bLegs[i].qty+=d*st;bName='';legPop(i);renderBuild()}
function _mswitch(i){
  bName='';const l=bLegs[i],nt=l.type==='C'?'P':'C';
  const s=bChain?bChain.strikes.find(x=>x.strike===l.strike):null;
  if(s){const sd=nt==='C'?s.call:s.put;l.type=nt;l.symbol=sd.symbol||'';l.streamer=sd.streamer||'';l.bid=sd.bid;l.ask=sd.ask;l.mid=sd.mid;l.vol=sd.vol;l.oi=sd.oi;l.iv=sd.iv}else l.type=nt;
  legPop(i);renderBuild();
}
function _mflip(i){bLegs[i].qty=-bLegs[i].qty;bName='';legPop(i);renderBuild()}

// Data
async function loadTr(){
  try{const d=await api('/api/paper-trade/trades?status=all');trades=d.trades||[];groups=d.groups||['Default']}catch(e){}
  await loadMarks();
  if(dTrade)dTrade=trades.find(t=>t.id===dTrade.id)||null;
}
// Mark open trades to market: option legs at the broker mark, stock legs at the
// last price. Today's return needs yesterday's close for every leg; a trade opened
// today counts from its entry prices instead.
async function loadMarks(){
  marksErr='';
  if(!trades.some(t=>t.status==='active')){marksAt='';return}
  let m;
  try{m=await api('/api/paper-trade/marks')}catch(e){marksErr=e.message;return}
  marksAt=m.asof||'';
  const today=new Date().toLocaleDateString('en-CA');
  trades.forEach(t=>{
    if(t.status!=='active')return;
    const sp=(m.spots||{})[String(t.underlying||'').toUpperCase()]||{};
    const fresh=(t.created_at||'').slice(0,10)===today;
    let ok=true,allPrev=true;
    if(sp.price>0)t.spot=sp.price;
    t.legs.forEach(l=>{
      let px=0,pc=0,valid=false;
      if(l.type==='S'){px=sp.price||0;pc=sp.prev_close||0;valid=px>0}
      else{const q=(m.legs||{})[l.symbol]||{};px=q.mark||0;pc=q.prev_close||0;valid=px>0||!!q.expired}
      if(valid)l.mark=px;else{ok=false;delete l.mark}
      l.prev_close=pc;if(!(pc>0))allPrev=false;
    });
    if(fresh||!allPrev)t.legs.forEach(l=>{l.prev_close=0});
    t._marked=ok;t._todayUnknown=!fresh&&!allPrev;
  });
}
async function refresh(){await loadTr();if(view==='detail')renderDetail();else if(view==='list')renderList()}

// Init
async function init(){injectCSS();await loadTr();if(view==='list'||!C.innerHTML)renderList();else if(view==='detail')renderDetail()}

return{init,refresh,loadSpec,renderList,renderBuild,renderDetail,newTrade,load,selExp,back,place,positions,
  addAtm,addStock,addFromBar,rmLeg,adjQ,tgAdd,legPop,vm,dragStart,editPrice,savePrice,tgSbarMode,setZoom,tpl,tplN,tgBuild,setView,setVM,setRange,setIV,goPrice,clearPrice,setDateIdx,
  setGrp,setSort,setFilt,tgSort,addGrp,gear,detail,closeTr,delTr,chgGrp,csv,
  _mq,_mswitch,_mflip};
})();
window.PT=PT;
"""
