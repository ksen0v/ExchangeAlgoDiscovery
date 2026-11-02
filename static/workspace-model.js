/* Data and geometry only. The UI, server and charts do not participate in a drag calculation. */
(function (root) {
  "use strict";
  const TYPES = Object.freeze({
    tape: {name:"Лента сделок",description:"Принты, фильтры сторон и размера сделки",symbol:"≋",minW:350,minH:200},
    cvd: {name:"CVD",description:"Дельта спота и фьючерсов с ценой",symbol:"∿",minW:280,minH:190},
    oiChart: {name:"График OI",description:"История открытого интереса выбранной биржи",symbol:"⌁",minW:280,minH:190},
    oi: {name:"Открытый интерес",description:"OI и его изменение по доступным площадкам",symbol:"◫",minW:320,minH:155},
    delta: {name:"Вклад в дельту",description:"Покупки и продажи по каждой бирже",symbol:"Δ",minW:220,minH:165},
    streams: {name:"Биржи",description:"Цены, объёмы, алго и плиты на площадках",symbol:"▤",minW:350,minH:170},
    walls: {name:"Плиты",description:"Крупные заявки у цены и события по ним",symbol:"▰",minW:280,minH:145},
    alerts: {name:"Алерты",description:"События детектора по текущей монете",symbol:"◉",minW:280,minH:145}
  });
  const clone = value => JSON.parse(JSON.stringify(value));
  const finite = (n,fallback=0) => typeof n === "number" && Number.isFinite(n) ? n : fallback;
  const clamp = (n,lo,hi) => Math.max(lo,Math.min(hi,n));
  const id = () => typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `w-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  function rect(value) {
    const w=clamp(finite(value?.w,.4),.02,1), h=clamp(finite(value?.h,.3),.02,1);
    return {x:clamp(finite(value?.x),0,1-w),y:clamp(finite(value?.y),0,1-h),w,h};
  }
  function normalize(raw) {
    if (!raw || raw.schema !== 1 || !Array.isArray(raw.spaces)) return null;
    const seenSpaces=new Set(), seenModules=new Set();
    const spaces=raw.spaces.filter(s=>s && typeof s.id==="string" && s.id.length>0 && s.id.length<=80 && !seenSpaces.has(s.id) && seenSpaces.add(s.id)).map(s=>({
      id:s.id,name:String(s.name||"Рабочее пространство").trim().slice(0,80)||"Рабочее пространство",mode:s.mode==="free"?"free":"magnet",
      modules:(Array.isArray(s.modules)?s.modules:[]).filter(m=>m && Object.hasOwn(TYPES,m.type) && typeof m.id==="string" && m.id.length>0 && m.id.length<=80 && !seenModules.has(m.id) && seenModules.add(m.id)).map(m=>({
        id:m.id,type:m.type,title:typeof m.title==="string"?m.title.slice(0,80):"",rect:rect(m.rect),
        settings:m.settings && typeof m.settings==="object" && !Array.isArray(m.settings)?clone(m.settings):{}
      }))
    }));
    return {schema:1,updatedAt:Math.floor(clamp(finite(raw.updatedAt),0,Number.MAX_SAFE_INTEGER)),activeId:spaces.some(s=>s.id===raw.activeId)?raw.activeId:spaces[0]?.id||null,spaces};
  }
  function blank(name="Рабочее пространство",mode="magnet") { return {id:id(),name,mode,modules:[]}; }
  function makeModule(type,position) {return {id:id(),type,title:"",rect:rect(position),settings:{}};}
  function initial() {
    const space=blank("Мой радар");
    const positions=[
      ["tape",.012,.015,.43,.65], ["cvd",.458,.015,.53,.33],
      ["oiChart",.458,.365,.312,.335], ["delta",.785,.365,.203,.62],
      ["oi",.012,.685,.43,.30], ["walls",.458,.72,.312,.265]
    ];
    space.modules=positions.map(([type,x,y,w,h])=>makeModule(type,{x,y,w,h}));
    return {schema:1,updatedAt:0,activeId:space.id,spaces:[space]};
  }
  function pixels(normal,bounds,type) {
    const t=TYPES[type]||TYPES.cvd;
    const w=clamp(normal.w*bounds.w,Math.min(t.minW,bounds.w),bounds.w),h=clamp(normal.h*bounds.h,Math.min(t.minH,bounds.h),bounds.h);
    return {x:clamp(normal.x*bounds.w,0,bounds.w-w),y:clamp(normal.y*bounds.h,0,bounds.h-h),w,h};
  }
  const relative = (r,b) => rect({x:r.x/b.w,y:r.y/b.h,w:r.w/b.w,h:r.h/b.h});
  const intersects = (a,b,gap=0) => a.x < b.x+b.w+gap-.001 && a.x+a.w+gap > b.x+.001 && a.y < b.y+b.h+gap-.001 && a.y+a.h+gap > b.y+.001;
  function move(start,dx,dy,bounds) {return {...start,x:clamp(start.x+dx,0,bounds.w-start.w),y:clamp(start.y+dy,0,bounds.h-start.h)};}
  function resize(start,dx,dy,edges,bounds,min={w:180,h:100}) {
    let l=start.x,r=start.x+start.w,t=start.y,b=start.y+start.h;
    if(edges.includes("w"))l=clamp(l+dx,0,r-Math.min(min.w,bounds.w));
    if(edges.includes("e"))r=clamp(r+dx,l+Math.min(min.w,bounds.w),bounds.w);
    if(edges.includes("n"))t=clamp(t+dy,0,b-Math.min(min.h,bounds.h));
    if(edges.includes("s"))b=clamp(b+dy,t+Math.min(min.h,bounds.h),bounds.h);
    return {x:l,y:t,w:r-l,h:b-t};
  }
  function magnet(input,others,bounds,{threshold=10,edges="",min={w:180,h:100}}={}) {
    let result={...input}; const guides=[];
    const near = (a,b,axis) => {
      const end=axis==="x"?"w":"h";
      return a[axis] <= b[axis]+b[end]+threshold && a[axis]+a[end]+threshold >= b[axis];
    };
    for(const axis of ["x","y"]) {
      const len=axis==="x"?"w":"h", size=bounds[len], orth=axis==="x"?"y":"x";
      const startEdge=axis==="x"?"w":"n",endEdge=axis==="x"?"e":"s";
      const sourceEdges=edges ? [edges.includes(startEdge)?0:null,edges.includes(endEdge)?1:null].filter(v=>v!==null) : [0,1];
      const targets=[0,size];
      for(const other of others)if(near(result,other,orth))targets.push(other[axis],other[axis]+other[len]);
      let best=null;
      for(const edge of sourceEdges)for(const target of targets) {
        const delta=target-(result[axis]+edge*result[len]);
        if(Math.abs(delta)>threshold || (best && Math.abs(delta)>=best.distance))continue;
        const candidate={...result};
        if(!edges)candidate[axis]+=delta;
        else if(edge===0){candidate[axis]+=delta;candidate[len]-=delta;}
        else candidate[len]+=delta;
        const minimum=edges?Math.min(min[len],size):result[len];
        if(candidate[axis]<-.01 || candidate[axis]+candidate[len]>size+.01 || candidate[len]<minimum-.01)continue;
        // Aligning to a distant matching x or y must not create a new overlap.
        if(others.some(o=>!intersects(result,o) && intersects(candidate,o)))continue;
        best={candidate,target,distance:Math.abs(delta)};
      }
      if(best){result=best.candidate;guides.push({axis,at:best.target});}
    }
    return {rect:result,guides};
  }
  function place(modules,type,bounds,mode="magnet") {
    const spec=TYPES[type],w=Math.min(bounds.w,Math.max(spec.minW,Math.min(530,bounds.w*.43))),h=Math.min(bounds.h,Math.max(spec.minH,Math.min(340,bounds.h*.43)));
    const gap=mode==="magnet"?0:12, others=modules.map(m=>pixels(m.rect,bounds,m.type));
    const xs=[0,...others.map(r=>r.x+r.w+gap)],ys=[0,...others.map(r=>r.y+r.h+gap)];
    for(const y of ys.sort((a,b)=>a-b))for(const x of xs.sort((a,b)=>a-b)) {
      const candidate={x,y,w,h};
      if(x+w<=bounds.w && y+h<=bounds.h && !others.some(r=>intersects(candidate,r,gap)))return relative(candidate,bounds);
    }
    const offset=(modules.length%7)*18;
    return relative({x:clamp((bounds.w-w)/2+offset,0,bounds.w-w),y:clamp((bounds.h-h)/2+offset,0,bounds.h-h),w,h},bounds);
  }
  function tile(modules,bounds,mode="magnet") {
    if(!modules.length)return [];
    const gap=mode==="magnet"?0:10;
    const cols=Math.max(1,Math.round(Math.sqrt(modules.length*bounds.w/bounds.h*.7))), rows=Math.ceil(modules.length/cols);
    const h=(bounds.h-gap*(rows-1))/rows;
    return modules.map((m,i)=>{
      const row=Math.floor(i/cols),count=Math.min(cols,modules.length-row*cols),col=i%cols,w=(bounds.w-gap*(count-1))/count;
      return relative({x:col*(w+gap),y:row*(h+gap),w,h},bounds);
    });
  }
  function tapeThreshold(p,coin,key) {const value=p.minByCoin?.[coin]?.[key]??p.minUsd;return typeof value==='number'&&Number.isFinite(value)?clamp(value,0,1e12):3000;}
  function tapeAccepts(p,key) {return (!p.keys?.length||p.keys.includes(key))&&(p.market==='all'||!p.market||key.endsWith(':'+p.market));}
  function visibleTrade(row,p,coin) {return row.usd>=tapeThreshold(p,coin,row.key)&&tapeAccepts(p,row.key)&&(!p.side||p.side==='all'||p.side===row.side);}
  function subscription(tapes,universe,coin,walls=false) {
    const minByKey={},keys=[];
    for(const key of universe) {
      const matching=tapes.filter(p=>tapeAccepts(p,key));
      if(matching.length){keys.push(key);minByKey[key]=Math.min(...matching.map(p=>tapeThreshold(p,coin,key)));}
      else if(walls)minByKey[key]=1e12;
    }
    const min=tapes.length?Math.min(...tapes.map(p=>typeof p.minUsd==='number'?p.minUsd:3000),...Object.values(minByKey)):1e12;
    return {tape:tapes.length>0&&keys.length>0,min_usd:min,min_by_key:minByKey,keys:walls?[]:keys,walls};
  }
  const exported={TYPES,clone,id,normalize,blank,module:makeModule,initial,rect,pixels,relative,intersects,move,resize,magnet,place,tile,tapeThreshold,tapeAccepts,visibleTrade,subscription};
  if(typeof module!=="undefined" && module.exports)module.exports=exported;
  else root.WorkspaceModel=exported;
})(typeof globalThis!=="undefined"?globalThis:this);
