"use strict";
// A controller exists only for a module on the active workspace. All controllers share S and A.
window.RadarModules = (() => {
  const finite = (v,d,min=0,max=1e12) => Number.isFinite(Number(v)) ? Math.min(max,Math.max(min,Number(v))) : d;
  const roles = (root,id) => root.querySelector(`[data-role="${id}"]`);
  const validKeys = value => Array.isArray(value) ? [...new Set(value.filter(v=>typeof v==="string" && v.length<=160))] : [];
  const thresholds = value => Object.fromEntries(Object.entries(value && typeof value==="object"?value:{}).filter(([key,n])=>key.length<=160 && typeof n==="number" && Number.isFinite(n) && n>=0 && n<=1e12));
  function preferences(spec) {
    const p=spec.settings;
    if(spec.type==="tape") {
      p.minUsd=finite(p.minUsd,S.minUsd);p.side=["all","buy","sell"].includes(p.side)?p.side:"all";
      p.market=["all","spot","perp"].includes(p.market)?p.market:"all";p.keys=validKeys(p.keys??[...S.selected]);
      p.minByCoin=Object.fromEntries(Object.entries(p.minByCoin??S.tapeMinByCoin??{}).filter(([coin])=>/^[A-Z0-9]{1,20}$/.test(coin)).map(([coin,values])=>[coin,thresholds(values)]));
    }
    if(spec.type==="streams")p.hideQuiet=Boolean(p.hideQuiet??S.hideQuiet);
    if(spec.type==="delta"){p.window=["1m","5m","15m","1h"].includes(p.window)?p.window:store.get("venueWin","5m");p.sort=["key","buy","sell","delta","abs"].includes(p.sort)?p.sort:"abs";}
    if(spec.type==="cvd" || spec.type==="oiChart")p.seconds=[900,3600,14400,86400].includes(p.seconds)?p.seconds:3600;
    if(spec.type==="oiChart"){p.oiKey=typeof p.oiKey==="string"?p.oiKey:"__total__";p.unit=p.unit==="usd"?"usd":"coins";}
    if(spec.type==="walls" || spec.type==="alerts")p.clearedAt=finite(p.clearedAt,0);
    return p;
  }
  function create(spec,root,changed) {
    const abort=new AbortController(), p=preferences(spec), get=id=>roles(root,id), charts=[];
    const on=(el,event,fn)=>el?.addEventListener(event,fn,{signal:abort.signal});
    const module={id:spec.id,type:spec.type,spec,root,p,charts,paused:false,frozen:[],
      get,changed:()=>changed(module),render:()=>{},trades:()=>{},reset:()=>{},
      setActive:active=>charts.forEach(chart=>chart.setActive(active)),
      destroy:()=>{abort.abort();charts.forEach(chart=>chart.destroy());module.frozen=[];}
    };
    if(spec.type==="tape") {
      const head=root.querySelector('.tape-head');
      const market=document.createElement('select');market.className='small tape-market';market.setAttribute('aria-label','Рынок этой ленты');
      market.innerHTML='<option value="all">Все рынки</option><option value="spot">Спот</option><option value="perp">Фьючерсы</option>';market.value=p.market;head.insertBefore(market,get('pauseBtn'));
      get('minUsd').value=p.minUsd;get('minUsd').setAttribute('aria-label','Минимальный размер сделки в этой ленте');get('sideFilter').value=p.side;
      const visible=r=>WorkspaceModel.visibleTrade(r,p,S.coin);
      module.render=()=>{
        const rows=(module.paused?module.frozen:S.tape).filter(visible).slice(0,TAPE_DOM);
        get('tapeList').innerHTML=rows.map(r=>tapeRowHtml(r,p)).join('')||'<div class="empty">Ждём сделки по фильтрам этой ленты.</div>';
        get('tapeFilters').innerHTML=p.keys.map(k=>{const [name,kind]=splitKey(k);return `<button class="chip" data-key="${esc(k)}">${esc(name)} ${kind}</button>`;}).join('');
      };
      module.trades=rows=>{
        if(module.paused)return;
        const shown=rows.filter(visible);if(!shown.length)return;
        const list=get('tapeList');list.querySelector('.empty')?.remove();
        list.insertAdjacentHTML('afterbegin',shown.slice().reverse().map(r=>tapeRowHtml(r,p)).join(''));
        while(list.childElementCount>TAPE_DOM)list.lastElementChild.remove();
      };
      module.reset=()=>{module.frozen=[];module.render();};
      on(get('minUsd'),'change',()=>{p.minUsd=finite(get('minUsd').value,3000);get('minUsd').value=p.minUsd;module.render();module.changed();});
      on(get('sideFilter'),'change',()=>{p.side=get('sideFilter').value;module.render();module.changed();});
      on(market,'change',()=>{p.market=market.value;module.render();module.changed();});
      on(get('tapeFilters'),'click',e=>{const b=e.target.closest('[data-key]');if(b){p.keys=p.keys.filter(k=>k!==b.dataset.key);module.render();module.changed();}});
      on(get('pauseBtn'),'click',()=>{module.paused=!module.paused;module.frozen=module.paused?S.tape.slice():[];get('pauseBtn').textContent=module.paused?'▶ Продолжить':'⏸ Пауза';module.render();window.Desk.subscriptionsChanged();});
      on(get('tapeSizeBtn'),'click',()=>openTapeSizeDialog(module));
    } else if(spec.type==="streams") {
      get('hideQuiet').checked=p.hideQuiet;
      module.render=()=>{
        const selected=window.Desk.focusedTape()?.p.keys||[];
        const rows=S.streams.filter(isActive).filter(s=>!p.hideQuiet||s.vol_w>0).sort((a,b)=>(b.score-a.score)||(b.vol_w-a.vol_w));
        get('streamsBody').innerHTML=rows.map(s=>{
          const [venue]=splitKey(s.key),hot=s.score>=S.config.alert_score,warm=!hot&&s.score>=S.config.alert_score/2;
          const algo=s.algo?`<span class="${s.algo.side==='buy'?'pos':'neg'}">${s.algo.count}×${fmtUsd(s.algo.avg_usd)}${s.algo.regular?' ⏱':''}</span>`:'<span class="dim">—</span>';
          return `<tr data-key="${esc(s.key)}" class="${hot?'hot':warm?'warm':''} ${selected.includes(s.key)?'selected':''}"><td class="venue">${esc(venue)}${kindBadge(s.kind)}${s.side_source==='price_tick_fallback'?' ≈':''}</td><td class="r">${fmtPrice(s.price)}</td><td class="r">${fmtUsd(s.vol_w)}</td><td>${algo}</td><td class="wallcell">${wallCell(s)}</td><td class="r score"><b>${Math.round(s.score||0)}</b></td></tr>`;
        }).join('')||'<tr><td colspan="6" class="empty">Нет активных потоков. Проверьте выбор бирж.</td></tr>';
        const inactive=S.streams.filter(s=>!isActive(s));
        get('inactiveSummary').textContent=`Нет пары / подключение / ошибки (${inactive.length})`;
        get('inactiveList').innerHTML=inactive.map(s=>`<div>${esc(splitKey(s.key)[0])}${kindBadge(s.kind)} <span class="muted">${esc(s.status)}</span>${s.error?` <span class="err" title="${esc(s.error)}">⚠</span>`:''}</div>`).join('');
        get('windowHint').textContent=`окно ${S.config.window_sec}с`;
      };
      on(get('hideQuiet'),'change',()=>{p.hideQuiet=get('hideQuiet').checked;module.changed();module.render();});
      on(get('streamsBody'),'mousedown',e=>{const td=e.button===0&&e.target.closest('td.wallcell');if(td){e.preventDefault();openWallDlg(td.closest('[data-key]').dataset.key);}});
      on(get('streamsBody'),'click',e=>{const row=e.target.closest('tr[data-key]');if(row&&!e.target.closest('.wallcell'))toggleSelected(row.dataset.key);});
    } else if(spec.type==="delta") {
      module.render=()=>{
        const data=window.A?.snap?.delta;
        if(!data){get('venueBody').innerHTML='<tr><td colspan="5" class="empty">Ждём данные дельты</td></tr>';return;}
        const wins=data.windows.map(r=>r.w);if(!wins.includes(p.window))p.window=wins[0];
        get('venueWin').innerHTML=wins.map(w=>`<button class="${w===p.window?'active':''}" data-window="${w}">${winRu(w)}</button>`).join('');
        const rows=(data.venues[p.window]||[]).slice();rows.sort((a,b)=>p.sort==='key'?a.key.localeCompare(b.key):p.sort==='abs'?Math.abs(b.delta)-Math.abs(a.delta):b[p.sort]-a[p.sort]);
        const total=rows.reduce((s,r)=>s+Math.abs(r.delta),0)||1,max=Math.max(1,...rows.map(r=>Math.abs(r.delta)));
        get('venueHead').querySelectorAll('th').forEach(th=>th.classList.toggle('sorted',th.dataset.sort===p.sort));
        get('venueBody').innerHTML=rows.map(r=>{const [name,kind]=splitKey(r.key);return `<tr><td class="venue">${esc(name)}${kindBadge(kind)}</td><td class="r pos">${fmtUsd(r.buy)}</td><td class="r neg">${fmtUsd(r.sell)}</td><td class="r">${fmtSigned(r.delta)}</td><td>${dbar(r.delta,max)} <span class="zb">${(Math.abs(r.delta)/total*100).toFixed(0)}%</span></td></tr>`;}).join('')||'<tr><td colspan="5" class="empty">Нет сделок за выбранное окно</td></tr>';
      };
      on(get('venueWin'),'click',e=>{const b=e.target.closest('[data-window]');if(b){p.window=b.dataset.window;module.render();module.changed();}});
      on(get('venueHead'),'click',e=>{const b=e.target.closest('[data-sort]');if(b){p.sort=b.dataset.sort;module.render();module.changed();}});
    } else if(spec.type==="oi") {
      module.render=()=>{
        const oi=window.A?.snap?.oi,rows=(oi?.venues||[]).filter(r=>r.coins!=null||r.oi_age!=null);
        get('oiHint').textContent=rows.length?`${oi.totals.count} бирж · 5–10 с`:'';
        const row=(r,total)=>`<tr class="${total?'total':''}"><td class="venue"><button class="ghost small oi-open" data-oi-key="${total?'__total__':esc(r.key)}">${total?'Все биржи':esc(splitKey(r.key)[0])}${!total&&r.coins==null?' ·':''}</button></td><td class="r">${r.coins==null?'—':fmtQty(r.coins)}</td><td class="r">${fmtUsd(r.usd)}</td>${['1m','5m','1h'].map(w=>`<td class="r">${fmtPct(r.pct?.[w])}</td>`).join('')}</tr>`;
        get('oiBody').innerHTML=rows.length?rows.map(r=>row(r,false)).join('')+row({...oi.totals,coins:oi.totals.count?oi.totals.coins:null,usd:oi.totals.count?oi.totals.usd:null},true):'<tr><td colspan="6" class="empty">Публичных данных OI пока нет. Здесь появятся только доступные площадки.</td></tr>';
      };
      on(get('oiBody'),'click',e=>{const b=e.target.closest('[data-oi-key]');if(b)window.Desk.openOi(b.dataset.oiKey);});
    } else if(spec.type==="cvd" || spec.type==="oiChart") {
      const cvd=spec.type==='cvd',range=get(cvd?'cvdRange':'oiRange'),live=get(cvd?'cvdLive':'oiLive');
      const chart=new RadarChart(get(cvd?'cvdChart':'oiChart'),{span:p.seconds,label:cvd?'Цена, CVD спота и фьючерсов':'Открытый интерес',onView:follow=>live.classList.toggle('history',!follow)});charts.push(chart);module.chart=chart;
      const syncRange=()=>range.querySelectorAll('button').forEach(b=>b.classList.toggle('active',Number(b.dataset.seconds)===p.seconds));syncRange();
      on(range,'click',e=>{const b=e.target.closest('[data-seconds]');if(b){p.seconds=Number(b.dataset.seconds);chart.live(p.seconds);syncRange();module.changed();}});
      on(live,'click',()=>chart.live(p.seconds));
      if(cvd) {
        module.render=()=>chart.update(window.A?.snap?.cvd||[],{now:window.A?.snap?.ts,series:[{index:3,label:'Цена',color:'--text-2',format:fmtPrice},{index:1,pane:1,label:'CVD спот',color:'--series-spot',format:signedUsd},{index:2,pane:1,label:'CVD фьючерсы',color:'--series-perp',format:signedUsd}]});
      } else {
        let signature='';
        module.render=()=>{
          const rows=(window.A?.snap?.oi?.venues||[]).filter(r=>r.coins!=null||r.oi_age!=null),keys=rows.map(r=>r.key).sort();if(keys.length)keys.unshift('__total__');
          const sig=JSON.stringify(keys);if(signature!==sig){signature=sig;get('oiVenue').innerHTML=keys.map(k=>`<option value="${esc(k)}">${k==='__total__'?'Все биржи':esc(splitKey(k)[0])}</option>`).join('');}
          if(keys.length&&!keys.includes(p.oiKey))p.oiKey=keys[0];
          get('oiVenue').disabled=!keys.length;get('oiVenue').value=p.oiKey;get('oiUnit').value=p.unit;
          const entry=window.A?.oiCache.get(p.oiKey),data=entry?.data,valid=data?.coin===S.coin&&data?.key===p.oiKey,index=p.unit==='usd'?2:1,fmt=p.unit==='usd'?fmtUsd:fmtQty;
          const name=keys.includes(p.oiKey)?p.oiKey==='__total__'?'Все биржи':splitKey(p.oiKey)[0]:'';
          const last=valid?data.points.filter(row=>row[index]!=null).at(-1):null;
          get('oiChartHint').textContent=entry?.error||`${name?`${name} · ${S.coin}${last?' · '+fmt(last[index]):''}${data?.stale?' · данные устарели':''}`:'Нет доступных бирж с OI'}`;
          chart.update(valid?data.points:[],{now:valid?data.ts:Date.now()/1000,maxGap:90,membersIndex:p.oiKey==='__total__'?3:null,series:[{index,label:p.unit==='usd'?'OI, $':'OI, монет',color:'--accent',format:fmt}],empty:name?'Ждём первые данные OI':'Нет доступного открытого интереса'});
        };
        module.selectOi=key=>{p.oiKey=key;chart.reset();module.render();module.changed();window.refreshOiHistory?.();};
        on(get('oiVenue'),'change',()=>module.selectOi(get('oiVenue').value));
        on(get('oiUnit'),'change',()=>{p.unit=get('oiUnit').value;chart.yZoom=1;module.render();module.changed();});
      }
      module.reset=()=>{chart.reset();module.render();};
    } else if(spec.type==='walls'||spec.type==='alerts') {
      const walls=spec.type==='walls',list=get(walls?'wallsList':'alertsList');
      get(walls?'alertsList':'wallsList').remove();root.querySelector('.tabs').remove();list.hidden=false;
      module.render=()=>{const rows=(walls?S.walls:S.alerts).filter(r=>r.ts>p.clearedAt);list.innerHTML=rows.map(r=>walls?wallHtml(r,false):alertHtml(r,false)).join('')||`<li class="empty">${walls?'Плит пока нет. Здесь появятся события крупных заявок у цены.':'Новых алертов пока нет.'}</li>`;};
      on(list,'click',e=>{const row=e.target.closest('[data-key]');if(row)toggleSelected(row.dataset.key,true);});
      on(get('clearAlerts'),'click',()=>{p.clearedAt=Date.now()/1000;module.changed();module.render();});
    }
    return module;
  }
  return {create};
})();
