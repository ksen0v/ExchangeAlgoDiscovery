"use strict";
const A={view:store.get('view','dashboard'),snap:null,healthTimer:null,oiTimer:null,oiCache:new Map()};
window.A=A;
const WIN_RU={'1m':'1м','5m':'5м','15m':'15м','1h':'1ч'};
const winRu=w=>WIN_RU[w]||w;
const signedUsd=v=>`${v<0?'−':v>0?'+':''}${fmtUsd(Math.abs(v))}`;
function fmtPct(v){return v==null||!Number.isFinite(v)?'<span class="dim">—</span>':`<span class="${v>0?'pos':v<0?'neg':''}">${v>0?'+':''}${v.toFixed(2)}%</span>`;}
function fmtSigned(v){return v==null?'<span class="dim">—</span>':`<span class="${v>0?'pos':v<0?'neg':''}">${signedUsd(v)}</span>`;}
function dbar(v,max){if(v==null||!max)return '<span class="dbar"></span>';const width=Math.min(50,Math.abs(v)/max*50);return `<span class="dbar"><i class="${v>=0?'pos':'neg'}" style="left:${v>=0?50:50-width}%;width:${width}%"></i></span>`;}

function setView(view){
  A.view=view==='health'?'health':'dashboard';store.set('view',A.view);
  $('viewTape').hidden=A.view!=='dashboard';$('viewHealth').hidden=A.view!=='health';
  document.querySelectorAll('.view-btn').forEach(b=>b.classList.toggle('active',b.dataset.view===A.view));
  const visible=!document.hidden&&A.view==='dashboard';Desk.setVisible(visible);sendFilter();refreshOiHistory();
  clearInterval(A.healthTimer);A.healthTimer=null;
  if(document.hidden)return;
  if(A.view==='dashboard'){
    renderStreams();rebuildTape();if(A.snap)renderAnalysis();
    if(Desk.filter().analytics)api('/api/analytics').then(onAnalytics).catch(e=>toast(e.message));
  }else{loadHealth();A.healthTimer=setInterval(loadHealth,5000);}
}
window.setView=setView;
function onAnalytics(msg){if(msg.coin!==S.coin)return;A.snap=msg;if(A.view==='dashboard'&&!document.hidden)renderAnalysis();}
function renderAnalysis(){if(!A.snap)return;Desk.update('analytics');refreshOiHistory();}
function resetOiChart(){
  for(const entry of A.oiCache.values())entry.abort?.abort();A.oiCache.clear();
  for(const instance of Desk.instances.values())if(instance.type==='cvd'||instance.type==='oiChart')instance.reset();
  clearInterval(A.oiTimer);A.oiTimer=null;
}
window.resetOiChart=resetOiChart;

// One archive request per distinct active OI selection, shared by any number of chart windows.
function refreshOiHistory(){
  const visible=A.view==='dashboard'&&!document.hidden&&A.snap?.coin===S.coin;
  const allowed=new Set((A.snap?.oi?.venues||[]).filter(r=>r.coins!=null||r.oi_age!=null).map(r=>r.key));
  if(allowed.size)allowed.add('__total__');
  const wanted=new Set(visible?Desk.ofType('oiChart').map(m=>m.p.oiKey).filter(key=>allowed.has(key)):[]);
  for(const [key,entry] of A.oiCache)if(!wanted.has(key)){entry.abort?.abort();A.oiCache.delete(key);}
  if(wanted.size&&!A.oiTimer)A.oiTimer=setInterval(refreshOiHistory,10000);
  if(!wanted.size){clearInterval(A.oiTimer);A.oiTimer=null;return;}
  for(const key of wanted){
    let entry=A.oiCache.get(key);
    if(entry?.loading||entry&&Date.now()-entry.fetched<10000)continue;
    if(!entry){entry={data:null,fetched:0,error:'',loading:false};A.oiCache.set(key,entry);}
    entry.abort=new AbortController();entry.loading=true;entry.error='';const controller=entry.abort,coin=S.coin;
    api(`/api/oi/history?coin=${encodeURIComponent(coin)}&key=${encodeURIComponent(key)}&seconds=86400`,{signal:controller.signal}).then(data=>{
      if(controller.signal.aborted||A.oiCache.get(key)!==entry||coin!==S.coin||data.coin!==coin||data.key!==key)return;
      entry.data=data;entry.fetched=Date.now();Desk.ofType('oiChart').filter(m=>m.p.oiKey===key).forEach(m=>m.render());
    }).catch(error=>{if(!controller.signal.aborted){entry.error=error.message;entry.fetched=Date.now();Desk.ofType('oiChart').filter(m=>m.p.oiKey===key).forEach(m=>m.render());}}).finally(()=>entry.loading=false);
  }
}
window.refreshOiHistory=refreshOiHistory;

async function loadHealth(){
  try{
    const health=await api('/api/health');if(A.view!=='health'||document.hidden)return;
    const rows=health.streams.filter(s=>$('healthAll').checked||!['na','noapi'].includes(s.status));
    $('healthBody').innerHTML=rows.map(s=>`<tr><td>${esc(s.key)}</td><td>${esc(s.status)}</td><td>${esc(s.transport)}</td><td class="r">${s.latency_ms??'—'}</td><td class="r">${s.jitter_ms??'—'}</td><td class="r">${s.last_msg_age??'—'}</td><td class="r">${s.reconnects}</td><td class="r">${s.gaps}</td><td class="r">${s.backfilled}</td><td>${esc(s.book_status)}</td><td>${esc(s.error||s.book_error)}</td></tr>`).join('');
    $('feedsBody').innerHTML=health.feeds.map(s=>`<tr><td>${esc(s.key)}</td><td>${esc(s.oi)}</td><td class="r">${s.oi_age==null?'—':s.oi_age+'с'}</td></tr>`).join('');
  }catch(error){toast(error.message);}
}
$('healthAll').addEventListener('change',loadHealth);
document.querySelectorAll('.view-btn').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
document.addEventListener('visibilitychange',()=>{setView(A.view);if(document.hidden)Desk.flush();});
Desk.init();setView(A.view);
