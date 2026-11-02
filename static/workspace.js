"use strict";
window.Desk = new (class WorkspaceDesk {
  constructor() {
    this.model=WorkspaceModel.normalize(store.get('workspaces.v1',null))||WorkspaceModel.initial();
    if(window.radarOverlay){const spec=WorkspaceModel.clone(window.radarOverlay.module);spec.rect={x:0,y:0,w:1,h:1};this.model={schema:1,updatedAt:0,activeId:'overlay',spaces:[{id:'overlay',name:'Оверлей',mode:'free',modules:[spec]}]};document.body.classList.add('overlay-view');}
    this.instances=new Map();this.visible=true;this.bounds={w:1200,h:700};this.focusId=null;this.tapeId=null;this.z=10;
    this.mutations=0;this.ready=false;this.loading=false;this.dirty=false;this.saving=false;this.gesture=null;this.frame=null;
  }
  get active(){return this.model.spaces.find(s=>s.id===this.model.activeId)||null;}
  init() {
    this.surface=$('deskSurface');
    $('moduleCatalog').innerHTML=Object.entries(WorkspaceModel.TYPES).map(([type,t])=>`<button class="catalog-item" data-add-type="${type}"><span class="catalog-symbol" aria-hidden="true">${t.symbol}</span><span><b>${t.name}</b><small>${t.description}</small></span><i aria-hidden="true">+</i></button>`).join('');
    $('newWorkspaceBtn').addEventListener('click',()=>this.nameDialog('create'));
    $('sidebarVenuesBtn').addEventListener('click',()=>$('venuesBtn').click());
    $('addModuleBtn').addEventListener('click',()=>this.picker());$('emptyAddModuleBtn').addEventListener('click',()=>this.active?this.picker():this.nameDialog('create'));
    $('workspaceMenuBtn').addEventListener('click',e=>this.context('workspaceMenu',e.currentTarget,this.active?.id));
    $('appMenuBtn').addEventListener('click',e=>this.context('appMenu',e.currentTarget,'app'));
    $('appMenu').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.desktopAction)window.radarDesktop?.appAction(b.dataset.desktopAction);this.closeMenus();});
    $('tileWorkspaceBtn').addEventListener('click',()=>this.arrange());
    $('workspaceList').addEventListener('click',e=>{const menu=e.target.closest('[data-space-menu]'),button=e.target.closest('[data-space-id]');if(menu)this.context('workspaceMenu',menu,menu.dataset.spaceMenu);else if(button)this.switch(button.dataset.spaceId);});
    $('modulePicker').addEventListener('click',e=>{const button=e.target.closest('[data-add-type]');if(button){this.add(button.dataset.addType);this.closeMenus();}if(e.target.closest('[data-close-picker]'))this.closeMenus();});
    document.querySelectorAll('[data-placement]').forEach(b=>b.addEventListener('click',()=>{if(!this.active)return;this.active.mode=b.dataset.placement;this.chrome();this.changed();}));
    $('workspaceMenu').addEventListener('click',e=>{const b=e.target.closest('[data-space-action]');if(!b)return;const id=$('workspaceMenu').dataset.target;this.closeMenus();if(b.dataset.spaceAction==='create')this.nameDialog('create');else if(b.dataset.spaceAction==='delete')this.deleteDialog(id);else this.nameDialog(b.dataset.spaceAction,id);});
    $('moduleMenu').addEventListener('click',e=>{const b=e.target.closest('[data-module-action]');if(!b)return;const id=$('moduleMenu').dataset.target;this.closeMenus();const action=b.dataset.moduleAction;
      if(action==='overlay')this.openOverlay(id);else if(action==='lock')this.lockOverlay(id);else if(action==='closeOverlay')this.closeOverlay(id);else if(action==='remove')this.remove(id);else if(action==='duplicate')this.duplicateModule(id);else this.nameDialog('module',id);
    });
    $('workspaceNameForm').addEventListener('submit',e=>{e.preventDefault();this.submitName();});
    $('deleteWorkspaceForm').addEventListener('submit',e=>{e.preventDefault();const id=$('deleteWorkspaceDlg').dataset.target;$('deleteWorkspaceDlg').close();this.deleteSpace(id);});
    document.querySelectorAll('[data-close-dialog]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
    this.surface.addEventListener('pointerdown',e=>this.pointerDown(e));
    this.surface.addEventListener('click',e=>{const root=e.target.closest('.desk-module');if(!root)return;if(e.target.closest('[data-module-close]')){if(window.radarOverlay)window.radarDesktop?.closeModuleOverlay(window.radarOverlay.id);else this.remove(root.dataset.moduleId);}else if(e.target.closest('[data-module-menu]'))this.context('moduleMenu',e.target.closest('button'),root.dataset.moduleId);});
    this.surface.addEventListener('contextmenu',e=>{const root=e.target.closest('.desk-module');if(!root||e.target.closest('input,select,textarea'))return;e.preventDefault();this.focus(root.dataset.moduleId);this.context('moduleMenu',{getBoundingClientRect:()=>({left:e.clientX,bottom:e.clientY}),setAttribute:()=>{}},root.dataset.moduleId);});
    this.surface.addEventListener('keydown',e=>this.keyMove(e));
    document.addEventListener('pointermove',e=>this.pointerMove(e));document.addEventListener('pointerup',e=>this.pointerEnd(e));document.addEventListener('pointercancel',()=>this.cancelGesture());
    document.addEventListener('pointerdown',e=>{if(!e.target.closest('.floating-menu,#appMenuBtn,#addModuleBtn,#emptyAddModuleBtn,#workspaceMenuBtn,[data-module-menu],[data-space-menu]'))this.closeMenus();});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){this.cancelGesture();this.closeMenus();}});
    this.observer=new ResizeObserver(()=>{if(!this.surface.clientWidth||!this.surface.clientHeight)return;this.bounds={w:this.surface.clientWidth,h:this.surface.clientHeight};this.cancelGesture();this.applyRects();});this.observer.observe(this.surface);
    window.addEventListener('pagehide',()=>{if(window.radarOverlay)return;this.cache();if(this.ready&&this.dirty)fetch('/api/workspaces',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(this.model),keepalive:true}).catch(()=>{});});
    window.addEventListener('online',()=>this.ready?this.flush():this.load());
    document.addEventListener('radar-desktop-ready',()=>{this.overlayButtons();window.radarDesktop.overlayState(raw=>this.receiveOverlays(raw));window.radarDesktop.overlaysChanged.connect(raw=>this.receiveOverlays(raw));});
    this.overlayButtons();this.mount();if(!window.radarOverlay)this.load();
    api('/api/version').then(info=>$('appVersion').textContent=info.version).catch(()=>{});
  }
  templates(type) {return {tape:'.tape',streams:'.streams',cvd:'.a-cvd',delta:'.a-venues',oi:'.a-oi',oiChart:'.a-oi-chart',walls:'.alerts',alerts:'.alerts'}[type];}
  mount() {
    this.cancelGesture();this.closeMenus();
    if($('tapeSizeDlg').open)$('tapeSizeDlg').close();
    for(const instance of this.instances.values())instance.destroy();this.instances.clear();
    this.surface.querySelectorAll('.desk-module').forEach(el=>el.remove());
    this.focusId=null;this.tapeId=null;
    for(const spec of this.active?.modules||[])this.mountModule(spec);
    this.chrome();this.update('streams');this.update('analytics');this.applyRects();this.subscriptionsChanged();
    if(this.has('walls'))loadWalls();if(this.has('alerts'))loadAlerts();
  }
  mountModule(spec) {
    const root=$('moduleTemplates').content.querySelector(this.templates(spec.type)).cloneNode(true);
    root.querySelectorAll('[id]').forEach(el=>{el.dataset.role=el.id;el.removeAttribute('id');});
    root.classList.add('desk-module');root.dataset.moduleId=spec.id;root.dataset.moduleType=spec.type;
    const content=document.createElement('div');content.className='module-content';content.append(...root.childNodes);root.append(content);
    content.querySelectorAll('.panel-head h2').forEach(h=>h.remove());
    const bar=document.createElement('header');bar.className='module-titlebar';bar.tabIndex=0;bar.setAttribute('aria-label',`${WorkspaceModel.TYPES[spec.type].name}. Стрелки — перемещение, Alt и стрелки — размер.`);
    bar.innerHTML=`<span class="window-dots" aria-hidden="true"><i></i><i></i><i></i></span><h2>${esc(spec.title||WorkspaceModel.TYPES[spec.type].name)}</h2><span class="module-coin">${esc(S.coin)}</span><span class="spacer"></span><button class="module-icon" data-module-menu aria-label="Меню модуля" aria-expanded="false">⋯</button><button class="module-icon" data-module-close aria-label="Удалить модуль">×</button>`;
    content.querySelectorAll('[data-overlay]').forEach(button=>button.remove());
    root.prepend(bar);
    for(const edge of ['n','s','e','w','ne','nw','se','sw']){const handle=document.createElement('div');handle.className=`module-resize resize-${edge}`;handle.dataset.resize=edge;handle.setAttribute('aria-hidden','true');root.append(handle);}
    this.surface.append(root);
    const instance=RadarModules.create(spec,root,()=>{this.changed();this.subscriptionsChanged();});
    this.instances.set(spec.id,instance);root.style.zIndex=++this.z;instance.setActive(this.visible&&!document.hidden);instance.render();
    this.overlayButtons();return instance;
  }
  chrome() {
    $('workspaceList').innerHTML=this.model.spaces.map(space=>`<div class="workspace-entry ${space.id===this.model.activeId?'active':''}"><button class="workspace-link" data-space-id="${esc(space.id)}" ${space.id===this.model.activeId?'aria-current="page"':''}><span aria-hidden="true">⊞</span><span class="workspace-name">${esc(space.name)}</span><small>${space.modules.length}</small></button><button class="icon-button workspace-entry-menu" data-space-menu="${esc(space.id)}" aria-label="Настройки ${esc(space.name)}">⋯</button></div>`).join('');
    $('workspaceTitle').textContent=this.active?.name||'Рабочие пространства';
    $('deskModuleCount').textContent=`${this.instances.size} модулей`;
    $('deskEmpty').hidden=this.instances.size>0;$('emptyAddModuleBtn').textContent=this.active?'+ Добавить модуль':'+ Создать рабочее пространство';
    $('deskEmpty').querySelector('p').textContent=this.active?'Добавьте нужные модули и расположите их как удобно.':'Создайте рабочее пространство и добавьте в него модули.';
    $('addModuleBtn').disabled=!this.active;$('tileWorkspaceBtn').disabled=!this.instances.size;$('workspaceMenuBtn').disabled=!this.active;
    document.querySelectorAll('[data-placement]').forEach(b=>{const selected=b.dataset.placement===(this.active?.mode||'magnet');b.classList.toggle('active',selected);b.setAttribute('aria-pressed',String(selected));b.disabled=!this.active;});
    $('placementHint').textContent=this.active?.mode==='free'?'Свободное размещение модулей':'Магнит стыкует соседние края при перемещении';
    this.surface.dataset.mode=this.active?.mode||'magnet';
  }
  updateSidebar() {
    renderInstruments();
    const grouped=new Map();for(const stream of S.streams.filter(isActive)){const venue=splitKey(stream.key)[0];if(!grouped.has(venue))grouped.set(venue,[]);grouped.get(venue).push(stream.kind);}
    $('deskConnections').innerHTML=[...grouped].map(([name,kinds])=>`<button class="connection-entry" data-connection-venue="${esc(name)}"><span class="connection-dot"></span><span>${esc(name)}</span><small>${kinds.map(k=>k==='perp'?'F':'S').join(' / ')}</small></button>`).join('')||'<span class="muted small">Ждём активные площадки</span>';
    if(!this.sidebarBound){this.sidebarBound=true;$('deskConnections').addEventListener('click',()=>$('venuesBtn').click());}
    $('deskClock').textContent=new Date().toLocaleTimeString('ru-RU');
  }
  update(event) {
    if(!this.visible||document.hidden)return;
    if(event==='streams')this.updateSidebar();
    const relevant=event==='streams'?['streams']:event==='analytics'?['cvd','delta','oi','oiChart']:[event];
    for(const instance of this.instances.values())if(relevant.includes(instance.type))instance.render();
  }
  has(type){return [...this.instances.values()].some(m=>m.type===type);}
  ofType(type){return [...this.instances.values()].filter(m=>m.type===type);}
  focusedTape(){return this.instances.get(this.tapeId)||this.ofType('tape')[0]||null;}
  focus(id) {const instance=this.instances.get(id);if(!instance)return;this.focusId=id;if(instance.type==='tape')this.tapeId=id;instance.root.style.zIndex=++this.z;this.surface.querySelectorAll('.desk-module').forEach(el=>el.classList.toggle('module-focused',el===instance.root));}
  trades(rows){if(this.visible&&!document.hidden)for(const instance of this.ofType('tape'))instance.trades(rows);}
  resetCoin(){for(const m of this.instances.values()){m.root.querySelector('.module-coin').textContent=S.coin;m.reset();}this.updateSidebar();this.update('streams');this.update('analytics');}
  setVisible(value){this.visible=value;for(const m of this.instances.values())m.setActive(value&&!document.hidden);this.closeMenus();if(!value)this.cancelGesture();else{this.update('streams');this.update('analytics');this.ofType('tape').forEach(m=>m.render());requestAnimationFrame(()=>this.applyRects());}}
  overlayButtons(){document.querySelectorAll('[data-native]').forEach(b=>b.hidden=!window.radarDesktop);}
  overlayKey(id){return window.radarOverlay?.id||`${S.coin}:${id}`;}
  receiveOverlays(raw){try{this.overlays=JSON.parse(raw);}catch{this.overlays={};}if(!$('moduleMenu').hidden)this.overlayMenu($('moduleMenu').dataset.target);}
  overlayMenu(id){const state=this.overlays?.[this.overlayKey(id)],locked=!!state?.locked,open=!!state?.visible,own=!!window.radarOverlay;
    this.overlayButtons();
    const menu=$('moduleMenu');menu.querySelector('[data-module-action="overlay"]').textContent=open?'↗ Показать оверлей':'↗ Разместить оверлей';
    const lock=menu.querySelector('[data-module-action="lock"]');lock.disabled=!open;lock.setAttribute('aria-checked',String(locked));lock.textContent=`${locked?'✓ ':''}Сквозные клики`;
    menu.querySelector('[data-module-action="closeOverlay"]').disabled=!open;
    for(const action of ['rename','duplicate','remove','overlay'])menu.querySelector(`[data-module-action="${action}"]`).hidden=own;
  }
  openOverlay(id){const module=this.instances.get(id);if(module&&S.coin)window.radarDesktop?.openModuleOverlay(JSON.stringify({coin:S.coin,module:module.spec}));}
  lockOverlay(id){const key=this.overlayKey(id);window.radarDesktop?.setModuleOverlayLocked(key,!this.overlays?.[key]?.locked);}
  closeOverlay(id){window.radarDesktop?.closeModuleOverlay(this.overlayKey(id));}
  subscriptionsChanged(){sendFilter();window.refreshOiHistory?.();}
  filter() {
    const active=this.visible&&!document.hidden;
    const tapes=active?this.ofType('tape').filter(m=>!m.paused).map(m=>m.p):[];
    const universe=S.venues.flatMap(v=>['spot','perp'].filter(kind=>v[kind]).map(kind=>`${v.name}:${kind}`));
    return {...WorkspaceModel.subscription(tapes,universe,S.coin,active&&this.has('walls')),analytics:active&&['cvd','delta','oi','oiChart'].some(t=>this.has(t)),lite:!(active&&this.has('streams'))};
  }
  switch(id){if(!this.model.spaces.some(s=>s.id===id)||id===this.model.activeId)return;this.model.activeId=id;this.mount();this.changed();}
  add(type,settings=null) {
    if(!this.active||!Object.hasOwn(WorkspaceModel.TYPES,type))return null;
    const spec=WorkspaceModel.module(type,WorkspaceModel.place(this.active.modules,type,this.bounds,this.active.mode));if(settings)spec.settings=WorkspaceModel.clone(settings);
    this.active.modules.push(spec);const instance=this.mountModule(spec);this.focus(spec.id);this.applyRects();this.chrome();this.changed();this.subscriptionsChanged();if(type==='walls')loadWalls();if(type==='alerts')loadAlerts();return instance;
  }
  remove(id) {
    const instance=this.instances.get(id);if(!instance)return;
    this.cancelGesture();if($('tapeSizeDlg').open)$('tapeSizeDlg').close();instance.destroy();instance.root.remove();this.instances.delete(id);
    this.active.modules=this.active.modules.filter(m=>m.id!==id);if(this.tapeId===id)this.tapeId=null;if(this.focusId===id)this.focusId=null;
    this.chrome();this.changed();this.subscriptionsChanged();
  }
  duplicateModule(id){const m=this.instances.get(id);if(m){const instance=this.add(m.type,m.p);if(instance){instance.spec.title=m.spec.title;instance.root.querySelector('.module-titlebar h2').textContent=m.spec.title||WorkspaceModel.TYPES[m.type].name;this.changed();}}}
  openOi(key){let target=this.instances.get(this.focusId);if(target?.type!=='oiChart')target=this.ofType('oiChart')[0];if(!target)target=this.add('oiChart',{oiKey:key,unit:'usd'});target?.selectOi(key);if(target)this.focus(target.id);}
  arrange(){if(!this.active?.modules.length)return;const positions=WorkspaceModel.tile(this.active.modules,this.bounds,this.active.mode);this.active.modules.forEach((m,i)=>m.rect=positions[i]);this.applyRects();this.changed();}
  picker(){if(!this.active)return;const menu=$('modulePicker');const show=menu.hidden;this.closeMenus();if(show){menu.hidden=false;$('addModuleBtn').setAttribute('aria-expanded','true');menu.querySelector('button[data-add-type]')?.focus({preventScroll:true});}}
  context(id,button,target){const menu=$(id),show=menu.hidden||menu.dataset.target!==target;this.closeMenus();if(!show)return;menu.dataset.target=target||'';if(id==='moduleMenu')this.overlayMenu(target);menu.hidden=false;const box=button.getBoundingClientRect();menu.style.left=`${Math.min(Math.max(8,box.left),innerWidth-menu.offsetWidth-8)}px`;menu.style.top=`${Math.max(8,Math.min(box.bottom+5,innerHeight-menu.offsetHeight-8))}px`;button.setAttribute('aria-expanded','true');menu.querySelector('button:not([hidden]):not(:disabled)')?.focus({preventScroll:true});}
  closeMenus(){for(const id of ['modulePicker','workspaceMenu','moduleMenu','appMenu'])if($(id))$(id).hidden=true;document.querySelectorAll('[aria-expanded="true"]').forEach(b=>b.setAttribute('aria-expanded','false'));}
  nameDialog(action,target=null){this.nameIntent={action,target};const form=$('workspaceNameForm'),input=$('workspaceNameInput');form.reset();const space=this.model.spaces.find(s=>s.id===target),module=this.instances.get(target);
    $('workspaceNameTitle').textContent={create:'Новое рабочее пространство',rename:'Переименовать пространство',duplicate:'Копия рабочего пространства',module:'Название модуля'}[action];
    input.value=action==='create'?`Рабочее пространство ${this.model.spaces.length+1}`:action==='module'?module?.spec.title||WorkspaceModel.TYPES[module?.type]?.name||'Модуль':action==='duplicate'?`${space?.name||'Рабочее пространство'} — копия`:space?.name||'';
    $('workspaceNameDlg').showModal();input.focus();input.select();
  }
  submitName(){const name=$('workspaceNameInput').value.trim();if(!name)return;const {action,target}=this.nameIntent;const space=this.model.spaces.find(s=>s.id===target);
    if(action==='module'){const m=this.instances.get(target);if(m){m.spec.title=name;m.root.querySelector('.module-titlebar h2').textContent=name;}}
    else if(action==='rename'){if(space)space.name=name;}
    else {let fresh;if(action==='duplicate'&&space){fresh=WorkspaceModel.clone(space);fresh.id=WorkspaceModel.id();fresh.name=name;fresh.modules.forEach(m=>m.id=WorkspaceModel.id());}else fresh=WorkspaceModel.blank(name);this.model.spaces.push(fresh);this.model.activeId=fresh.id;this.mount();}
    $('workspaceNameDlg').close();this.chrome();this.changed();
  }
  deleteDialog(id){const space=this.model.spaces.find(s=>s.id===id);if(!space)return;$('deleteWorkspaceDlg').dataset.target=id;$('deleteWorkspaceText').textContent=`«${space.name}» и его модули будут удалены. Остальные пространства и настройки бирж сохранятся.`;$('deleteWorkspaceDlg').showModal();}
  deleteSpace(id){const index=this.model.spaces.findIndex(s=>s.id===id);if(index<0)return;this.model.spaces.splice(index,1);if(this.model.activeId===id){this.model.activeId=this.model.spaces[Math.min(index,this.model.spaces.length-1)]?.id||null;this.mount();}this.chrome();this.changed();}
  applyRect(instance,r){Object.assign(instance.root.style,{left:r.x+'px',top:r.y+'px',width:r.w+'px',height:r.h+'px'});}
  applyRects(){for(const m of this.instances.values())this.applyRect(m,WorkspaceModel.pixels(m.spec.rect,this.bounds,m.type));}
  compact(){return !!window.radarOverlay||matchMedia('(max-width:1180px), (max-height:590px)').matches;}
  pointerDown(e){const root=e.target.closest('.desk-module');if(!root)return;this.focus(root.dataset.moduleId);if(e.button!==0||this.compact())return;const handle=e.target.closest('[data-resize]'),bar=e.target.closest('.module-titlebar');if(!handle&&(!bar||e.target.closest('button,input,select,a')))return;
    const module=this.instances.get(root.dataset.moduleId),rect=WorkspaceModel.pixels(module.spec.rect,this.bounds,module.type);this.cancelGesture();
    this.gesture={id:e.pointerId,module,handle:handle||bar,edges:handle?.dataset.resize||'',startX:e.clientX,startY:e.clientY,rect,current:rect,original:{...module.spec.rect}};
    this.gesture.handle.setPointerCapture(e.pointerId);root.classList.add('module-moving');document.body.classList.add('desk-gesture');e.preventDefault();
  }
  pointerMove(e){const g=this.gesture;if(!g||g.id!==e.pointerId)return;const t=WorkspaceModel.TYPES[g.module.type],min={w:t.minW,h:t.minH};const dx=e.clientX-g.startX,dy=e.clientY-g.startY;
    let r=g.edges?WorkspaceModel.resize(g.rect,dx,dy,g.edges,this.bounds,min):WorkspaceModel.move(g.rect,dx,dy,this.bounds);let guides=[];
    if(this.active.mode==='magnet'&&!e.altKey){const others=[...this.instances.values()].filter(m=>m!==g.module).map(m=>WorkspaceModel.pixels(m.spec.rect,this.bounds,m.type));const snapped=WorkspaceModel.magnet(r,others,this.bounds,{edges:g.edges,min});r=snapped.rect;guides=snapped.guides;}
    g.current=r;g.guides=guides;if(!this.frame)this.frame=requestAnimationFrame(()=>{this.frame=null;if(this.gesture!==g)return;this.applyRect(g.module,g.current);this.showGuides(g.guides);});e.preventDefault();
  }
  showGuides(guides=[]){for(const [axis,id] of [['x','snapGuideX'],['y','snapGuideY']]){const el=$(id),guide=guides.find(g=>g.axis===axis);el.hidden=!guide;if(guide)el.style[axis==='x'?'left':'top']=guide.at+'px';}}
  pointerEnd(e){if(!this.gesture||e.pointerId!==this.gesture.id)return;const g=this.gesture;this.cancelGesture();if(!this.instances.has(g.module.id))return;g.module.spec.rect=WorkspaceModel.relative(g.current,this.bounds);this.applyRects();this.changed();}
  cancelGesture(){const g=this.gesture;if(this.frame){cancelAnimationFrame(this.frame);this.frame=null;}if(g){g.module.root.classList.remove('module-moving');if(g.handle.hasPointerCapture(g.id))g.handle.releasePointerCapture(g.id);this.applyRect(g.module,g.rect);this.gesture=null;}document.body.classList.remove('desk-gesture');this.showGuides();}
  keyMove(e){if(e.target!==e.target.closest('.module-titlebar')||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)||this.compact())return;e.preventDefault();const module=this.instances.get(e.target.closest('.desk-module').dataset.moduleId),r=WorkspaceModel.pixels(module.spec.rect,this.bounds,module.type),step=e.shiftKey?1:10;let dx=e.key==='ArrowLeft'?-step:e.key==='ArrowRight'?step:0,dy=e.key==='ArrowUp'?-step:e.key==='ArrowDown'?step:0;
    let next=e.altKey?WorkspaceModel.resize(r,dx,dy,'se',this.bounds,{w:WorkspaceModel.TYPES[module.type].minW,h:WorkspaceModel.TYPES[module.type].minH}):WorkspaceModel.move(r,dx,dy,this.bounds);
    if(this.active.mode==='magnet'&&!e.altKey)next=WorkspaceModel.magnet(next,[...this.instances.values()].filter(m=>m!==module).map(m=>WorkspaceModel.pixels(m.spec.rect,this.bounds,m.type)),this.bounds).rect;
    module.spec.rect=WorkspaceModel.relative(next,this.bounds);this.applyRects();this.changed();
  }
  cache(){if(window.radarOverlay)return true;try{localStorage.setItem('workspaces.v1',JSON.stringify(this.model));return true;}catch{return false;}}
  changed(){if(window.radarOverlay){window.radarDesktop?.saveModuleOverlay(JSON.stringify(this.active.modules[0]));return;}this.mutations++;this.model.updatedAt=Math.max(Date.now(),this.model.updatedAt+1);this.dirty=true;const cached=this.cache();$('workspaceSaveStatus').textContent=cached?'Сохраняем компоновку…':'Сохраняем на сервере…';if(this.ready)this.flush();else if(!this.loading)this.load();}
  async load(){if(this.loading)return;this.loading=true;const revision=this.mutations;try{const raw=await api('/api/workspaces');const remote=WorkspaceModel.normalize(raw);if(revision===this.mutations&&remote&&remote.updatedAt>=this.model.updatedAt){this.model=remote;this.mount();}this.cache();this.ready=true;this.dirty=!remote||this.model.updatedAt>remote.updatedAt;if(this.dirty)this.flush();else $('workspaceSaveStatus').textContent='Компоновка сохранена';}catch{$('workspaceSaveStatus').textContent='Локальная копия · сервер недоступен';}finally{this.loading=false;}}
  async flush(){if(this.saving||!this.ready||!this.dirty)return;this.saving=true;try{while(this.dirty){this.dirty=false;const snapshot=WorkspaceModel.clone(this.model);try{await api('/api/workspaces',{method:'PUT',body:JSON.stringify(snapshot)});$('workspaceSaveStatus').textContent=this.dirty?'Сохраняем компоновку…':'Компоновка сохранена';}catch{this.dirty=true;$('workspaceSaveStatus').textContent='Локальная копия · не сохранено на сервере';break;}}}finally{this.saving=false;}}
})();
