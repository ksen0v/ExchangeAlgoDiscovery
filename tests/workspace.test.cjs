const test=require('node:test');
const assert=require('node:assert/strict');
const W=require('../static/workspace-model.js');
const bounds={w:1200,h:800};

test('workspace round trip preserves separate module settings, modes and empty workspaces',()=>{
  const state=W.initial(),copy=W.clone(state.spaces[0]);copy.id=W.id();copy.name='Only OI';copy.mode='free';copy.modules=copy.modules.filter(m=>m.type==='oiChart');copy.modules[0].id=W.id();copy.modules[0].settings={oiKey:'Bybit:perp',unit:'usd',seconds:900};
  state.spaces.push(copy,W.blank('Empty'));state.activeId=copy.id;
  const restored=W.normalize(JSON.parse(JSON.stringify(state)));
  assert.equal(restored.activeId,copy.id);assert.deepEqual(restored.spaces[1],copy);assert.equal(restored.spaces[0].modules.find(m=>m.type==='oiChart').settings.unit,undefined);assert.equal(restored.spaces[2].modules.length,0);
  assert.deepEqual(W.normalize({schema:1,updatedAt:0,activeId:null,spaces:[]}).spaces,[]);
});
test('workspaces have no fixed count limit and damaged IDs cannot alias another module',()=>{
  const state=W.initial();for(let i=0;i<250;i++)state.spaces.push(W.blank('Space '+i));
  state.spaces[0].modules.push({...state.spaces[0].modules[0]},{id:'bad',type:'constructor',rect:{}});
  const restored=W.normalize(state);assert.equal(restored.spaces.length,251);assert.equal(restored.spaces[0].modules.length,6);
  assert.equal(W.normalize({schema:9,spaces:[]}),null);
});
test('free movement is exact, bounded and independent of nearby modules',()=>{
  const start={x:20,y:40,w:300,h:200};assert.deepEqual(W.move(start,37,19,bounds),{x:57,y:59,w:300,h:200});
  assert.deepEqual(W.move(start,5000,-5000,bounds),{x:900,y:0,w:300,h:200});
});
test('magnet joins adjacent edges and aligns the tops without leaving a gap',()=>{
  const other={x:500,y:200,w:300,h:250};
  const snapped=W.magnet({x:193,y:205,w:300,h:200},[other],bounds);
  assert.equal(snapped.rect.x+snapped.rect.w,other.x);assert.equal(snapped.rect.y,other.y);assert.equal(snapped.guides.length,2);assert.equal(W.intersects(snapped.rect,other),false);
});
test('magnet has a finite capture distance and ignores distant matching edges',()=>{
  const input={x:180,y:30,w:300,h:150};
  assert.deepEqual(W.magnet(input,[{x:500,y:30,w:300,h:150}],bounds).rect,input);
  const separated={x:198,y:20,w:300,h:150};assert.equal(W.magnet(separated,[{x:500,y:600,w:300,h:150}],bounds).rect.x,separated.x);
});
test('resizing snaps the moving edge and keeps the opposite edge fixed',()=>{
  const start={x:100,y:100,w:300,h:200},other={x:500,y:100,w:300,h:200};
  const grown=W.resize(start,94,0,'e',bounds,{w:200,h:100});
  const snapped=W.magnet(grown,[other],bounds,{edges:'e',min:{w:200,h:100}}).rect;
  assert.equal(snapped.x,100);assert.equal(snapped.x+snapped.w,500);
  const west=W.resize(start,-95,0,'w',bounds,{w:200,h:100});const joined=W.magnet(west,[],bounds,{edges:'w',min:{w:200,h:100}}).rect;
  assert.equal(joined.x,0);assert.equal(joined.x+joined.w,400);
});
test('resize cannot invert a module or escape the desk',()=>{
  const r=W.resize({x:200,y:100,w:300,h:250},900,900,'nw',bounds,{w:220,h:140});
  assert.deepEqual(r,{x:280,y:210,w:220,h:140});
  const max=W.resize({x:200,y:100,w:300,h:250},900,900,'se',bounds,{w:220,h:140});assert.equal(max.x+max.w,bounds.w);assert.equal(max.y+max.h,bounds.h);
});
test('arrange fills complete rows in magnet mode and adding uses a free area',()=>{
  const modules=W.initial().spaces[0].modules.slice(0,3);const packed=W.tile(modules,bounds,'magnet');
  const area=packed.reduce((sum,r)=>sum+r.w*r.h,0);assert.ok(Math.abs(area-1)<1e-10);
  for(let i=0;i<packed.length;i++)for(let j=i+1;j<packed.length;j++)assert.equal(W.intersects(W.pixels(packed[i],bounds,'cvd'),W.pixels(packed[j],bounds,'cvd')),false);
  const newRect=W.place([{...modules[0],rect:{x:0,y:0,w:.3,h:.3}}],'cvd',bounds,'magnet');assert.ok(newRect.x>=.3||newRect.y>=.3);
});
test('one feed satisfies independent thresholds and market filters of multiple tapes',()=>{
  const keys=['A:spot','A:perp','B:spot'];
  const tapes=[{minUsd:3000,market:'spot',keys:[],minByCoin:{X:{'A:spot':500}}},{minUsd:10000,market:'perp',keys:['A:perp'],minByCoin:{}}];
  const filter=W.subscription(tapes,keys,'X');assert.deepEqual(filter.keys,keys);assert.equal(filter.min_by_key['A:spot'],500);assert.equal(filter.min_by_key['A:perp'],10000);assert.equal(filter.min_by_key['B:spot'],3000);
  const row={key:'A:perp',usd:6000,side:'sell'};assert.equal(W.visibleTrade(row,tapes[0],'X'),false);assert.equal(W.visibleTrade(row,tapes[1],'X'),false);
  assert.equal(W.visibleTrade({key:'A:spot',usd:600,side:'buy'},tapes[0],'X'),true);
});
test('removing all tapes unsubscribes prints while walls keep independent venue coverage',()=>{
  assert.equal(W.subscription([],['A:spot'],'X',false).tape,false);
  const walls=W.subscription([{minUsd:3000,market:'spot',keys:['A:spot']}],['A:spot','B:perp'],'X',true);
  assert.deepEqual(walls.keys,[]);assert.equal(walls.walls,true);assert.equal(walls.min_by_key['B:perp'],1e12);
  assert.equal(W.subscription([{minUsd:100,market:'spot',keys:['missing:spot']}],['A:perp'],'X').tape,false);
});
