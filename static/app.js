"use strict";

// ---------- small utils ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
let theme = new URLSearchParams(location.search).get("theme") || store.get("theme", "dark");
function applyTheme(value, notify = false) {
  theme = value === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  store.set("theme", theme);
  $("themeBtn").textContent = theme === "dark" ? "Светлая тема" : "Тёмная тема";
  document.dispatchEvent(new Event("radar-theme"));
  if (window.A) rebuildTape();
  if (notify && window.radarDesktop) window.radarDesktop.setTheme(theme);
}
window.applyTheme = applyTheme;
applyTheme(theme);
$("themeBtn").addEventListener("click", () => applyTheme(theme === "dark" ? "light" : "dark", true));
const fmtPrice = (p) => (p == null ? "—" : p.toLocaleString("en-US", { maximumSignificantDigits: 6 }));
function fmtUsd(v) {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return "$" + (v / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "k";
  return "$" + v.toFixed(0);
}
function fmtQty(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
  return v.toLocaleString("en-US", { maximumSignificantDigits: 5 });
}
const fmtTime = (ts, ms = false) => {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString("ru-RU", { hour12: false });
  return ms ? `${t}.${String(d.getMilliseconds()).padStart(3, "0")}` : t;
};
const splitKey = (key) => { const i = key.lastIndexOf(":"); return [key.slice(0, i), key.slice(i + 1)]; };
// Colours of repeated-size groups (one algorithm = one colour); same list as the desktop overlay.
const REP_COLORS = ["#f0b90b", "#8fb3ff", "#c38fff", "#4fd1d9", "#ff9f43", "#ff7eb6", "#b5e853", "#e6edf3"];
const REP_LIGHT = ["#987000", "#3158bd", "#8345b0", "#14739c", "#b35900", "#ad3475", "#4d7510", "#444444"];
const kindBadge = (kind) => `<span class="kind ${kind}">${kind === "perp" ? "perp" : "spot"}</span>`;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 3000);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

// ---------- state ----------
const S = {
  coin: "",
  config: {},
  venues: [],
  venueSelection: {},
  watch: [],
  telegram: false,
  streams: [],
  selected: new Set(store.get("selected", [])),
  minUsd: store.get("minUsd", 3000),
  tapeMinByCoin: store.get("tapeMinByCoin", {}),
  side: "all",
  paused: false,
  sound: store.get("sound", true),
  hideQuiet: store.get("hideQuiet", false),
  tab: store.get("tab", "alerts"),
  unseenWalls: 0,
  alerts: [], walls: [], alertRequest: 0, wallRequest: 0,
  tape: [], // raw rows, newest first
  pending: [],
  ws: null,
};
const TAPE_KEEP = 2000;
const TAPE_DOM = 400;

// ---------- streams table ----------
function wallCell(s) {
  const o = s.wall_override;
  const gear = o ? ` <span class="gear" title="Свои настройки плит для этой биржи">⚙</span>` : "";
  if (o && o.off) return `<span class="dim" title="Плиты на этой бирже не ищутся (своя настройка). Клик — изменить">выкл ⚙</span>`;
  const mm = s.mm
    ? ` <span class="dim" title="С обеих сторон долго стоят плотности похожего размера — это маркетмейкер, не плита">ММ ▲${fmtUsd(s.mm.bid)} ▼${fmtUsd(s.mm.ask)}</span>` : "";
  const pulls = s.pulls
    ? ` <span class="warn" title="Сняли без исполнения за 2 мин: ${s.pulls} плит на ${fmtUsd(s.pulled_usd)}">↯${s.pulls}</span>` : "";
  const w = s.wall;
  if (w) {
    const bid = w.side === "bid";
    const title = `Плита на ${bid ? "покупку" : "продажу"} ${fmtUsd(w.usd)} по ${fmtPrice(w.price)}: `
      + `${(w.dist_bps / 100).toFixed(2)}% от цены, ×${w.ratio.toFixed(0)} к обычному уровню стакана, стоит ${Math.round(w.age)} с`
      + (w.moves ? `, переставляли ${w.moves}× (за ценой ${w.push}×)` : "");
    const push = w.push ? ` <span class="push">${bid ? "⇡" : "⇣"}${w.push}</span>` : "";
    return `<span class="${bid ? "pos" : "neg"}" title="${esc(title)}">${bid ? "▲" : "▼"}${fmtUsd(w.usd)} `
      + `<span class="dim">${(w.dist_bps / 100).toFixed(2)}%</span></span>${push}${pulls}${mm}${gear}`;
  }
  if (pulls || mm) return (pulls + mm + gear).trim();
  if (s.book_status === "error") return `<span class="dim" title="Стакан: ${esc(s.book_error)}">стакан ⚠</span>`;
  return `<span class="dim">—</span>${gear}`;
}

// ---------- per-venue wall settings (click on the "Плита" cell) ----------
let wallDlgKey = "";
function openWallDlg(key) {
  const s = S.streams.find((x) => x.key === key) || {};
  const o = s.wall_override || {};
  const [venue, kind] = splitKey(key);
  wallDlgKey = key;
  $("wallDlgTitle").textContent = `Плиты: ${venue} ${kind === "perp" ? "фьючерс" : "спот"} · ${S.coin}`;
  $("wallCommon").textContent = `как в общих настройках (от ${fmtUsd(S.config.wall_min_usd)}, ×${S.config.wall_ratio} к стакану)`;
  const f = $("wallForm").elements;
  f.mode.value = o.off ? "off" : (o.min_usd || o.ratio) ? "own" : "common";
  f.min_usd.value = o.min_usd ?? "";
  f.ratio.value = o.ratio ?? "";
  $("wallDlg").showModal();
}
$("wallForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  const f = $("wallForm").elements;
  const own = f.mode.value === "own";
  const num = (v) => (v === "" ? null : Number(v));
  const body = { key: wallDlgKey, coin: S.coin, off: f.mode.value === "off",
    min_usd: own ? num(f.min_usd.value) : null, ratio: own ? num(f.ratio.value) : null };
  try {
    await api("/api/wall-overrides", { method: "PUT", body: JSON.stringify(body) });
    toast("Настройка плит для биржи сохранена");
  } catch (err) {
    toast(err.message);
  }
});

function isActive(s) {
  return (s.status === "live" || s.status === "polling") && s.price != null;
}

function renderStreams() {
  const active=S.streams.filter(isActive);
  $('liveCount').textContent=active.length+'/'+S.streams.length;
  $('venuesBtn').textContent='Биржи: '+(S.venueSelection[S.coin]?.length??S.venues.length)+'/'+S.venues.length;
  window.Desk?.update('streams');
}

function toggleSelected(key,only=false) {
  const target=window.Desk?.focusedTape();
  if(!target){toast('Добавьте модуль ленты, чтобы выбрать для неё биржу');return;}
  target.p.keys=only?[key]:target.p.keys.includes(key)?target.p.keys.filter(k=>k!==key):[...target.p.keys,key];
  target.render();target.changed();window.Desk.update('streams');
}

// ---------- tape ----------
function tapeThresholds(prefs=null) {return (prefs?.minByCoin??S.tapeMinByCoin)?.[S.coin]||{};}
function tapeThreshold(key,prefs=null) {return tapeThresholds(prefs)[key]??prefs?.minUsd??S.minUsd;}
function tapeVisible(row,prefs=null) {return WorkspaceModel.visibleTrade(row,prefs||{minUsd:S.minUsd,minByCoin:S.tapeMinByCoin,keys:[...S.selected],market:'all',side:S.side},S.coin);}

function tapeRowHtml(r, prefs = null) {
  const [venue, kind] = splitKey(r.key);
  const floor = Math.max(1, tapeThreshold(r.key, prefs));
  const pct = Math.min(100, (r.usd / (floor * 10)) * 100);
  const big = r.usd >= floor * 5 ? " big" : "";
  const fills = r.fills > 1 ? ` <span class="fills" title="Склеено исполнений одного ордера">×${r.fills}</span>` : "";
  const color = r.rep ? (theme === "light" ? REP_LIGHT : REP_COLORS)[r.grp % REP_COLORS.length] : "";
  const rep = r.rep ? `<i class="rep" title="${r.rep}-й ордер такого же размера и стороны за минуту — похоже на алгоритм">×${r.rep}</i>` : "";
  return `<div class="tape-row ${r.side === "buy" ? "buy" : r.side === "sell" ? "sell" : ""}${big}${r.rep ? " repeat" : ""}"${color ? ` style="--rep:${color}"` : ""}>`
    + `<div class="bg" style="width:${pct.toFixed(1)}%"></div>`
    + `<span>${fmtTime(r.ts, true)}</span>`
    + `<span class="ven">${esc(venue)}${kindBadge(kind)}</span>`
    + `<span class="r">${fmtPrice(r.price)}</span>`
    + `<span class="r">${fmtQty(r.amount)}${fills}</span>`
    + `<span class="r usd"${r.inferred ? ' title="Сторона оценена по изменению цены, а не передана биржей"' : ""}>${r.inferred ? "≈" : ""}${fmtUsd(r.usd)}</span>`
    + `<span class="r">${rep}</span></div>`;
}

function addTrades(rows) {
  S.tape=rows.slice().reverse().concat(S.tape).slice(0,TAPE_KEEP);
  window.Desk?.trades(rows);
}
function rebuildTape() {window.Desk?.ofType('tape').forEach(m=>m.render());}
function renderChips() {rebuildTape();}
let tapeSizeTarget=null,tapeSizeCoin='';
function openTapeSizeDialog(module) {
  tapeSizeTarget=module;tapeSizeCoin=S.coin;
  $('tapeSizeTitle').textContent='Размер сделок · '+S.coin+' · '+(module.spec.title||'Лента');
  const chosen=S.venueSelection[S.coin],values=tapeThresholds(module.p);
  $('tapeSizeBody').innerHTML=S.venues.filter(v=>!chosen||chosen.includes(v.name)).map(v=>'<tr data-venue="'+esc(v.name.toLowerCase())+'"><td>'+esc(v.name)+'</td>'+['spot','perp'].map(kind=>{
    const key=v.name+':'+kind;return '<td>'+(v[kind]?'<input type="number" min="0" max="1000000000000" step="any" data-key="'+esc(key)+'" placeholder="'+module.p.minUsd+'" value="'+(values[key]??'')+'" aria-label="'+esc(v.name)+' '+kind+', USD">':'—')+'</td>';
  }).join('')+'</tr>').join('');
  $('tapeSizeSearch').value='';$('tapeSizeDlg').showModal();
}
$('tapeSizeSearch').addEventListener('input',()=>{const q=$('tapeSizeSearch').value.toLowerCase();$('tapeSizeBody').querySelectorAll('tr').forEach(row=>row.hidden=!row.dataset.venue.includes(q));});
$('tapeSizeSearch').addEventListener('keydown',e=>{if(e.key==='Enter')e.preventDefault();});
$('tapeSizeReset').addEventListener('click',()=>$('tapeSizeBody').querySelectorAll('input').forEach(input=>input.value=''));
$('tapeSizeCancel').addEventListener('click',()=>$('tapeSizeDlg').close());
$('tapeSizeForm').addEventListener('submit',e=>{
  e.preventDefault();const target=tapeSizeTarget;
  if(tapeSizeCoin!==S.coin||!target||window.Desk.instances.get(target.id)!==target){$('tapeSizeDlg').close();toast('Откройте фильтры заново для текущей монеты и модуля');return;}
  const values={...tapeThresholds(target.p)};
  for(const input of $('tapeSizeBody').querySelectorAll('input')){
    if(input.value==='')delete values[input.dataset.key];
    else {const n=Number(input.value);if(!Number.isFinite(n)||n<0||n>1e12)return;values[input.dataset.key]=n;}
  }
  target.p.minByCoin={...target.p.minByCoin,[S.coin]:values};$('tapeSizeDlg').close();target.render();target.changed();toast('Фильтры этой ленты сохранены');
});

// ---------- alerts ----------
function alertHtml(a, fresh) {
  // module signals (М1–М4) open the analysis screen; tape alerts filter the tape by their venue
  const target = a.module ? `data-module="${esc(a.module)}"` : `data-key="${esc(a.key)}"`;
  return `<li class="${fresh ? "fresh" : ""}" ${target}>
    <div class="alert-top"><span class="t">${fmtTime(a.ts)}</span>
    <span class="v">${esc(a.venue)}</span>${a.kind ? kindBadge(a.kind) : ""}
    <span class="muted">${esc(a.coin)}</span>
    <span class="s">${Math.round(a.score)}</span></div>
    <ul class="alert-reasons">${a.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></li>`;
}
function renderAlerts(list) {S.alerts=list.slice(0,200);window.Desk?.update('alerts');}
function addAlert(alert) {S.alerts=[alert,...S.alerts].slice(0,200);window.Desk?.update('alerts');beep();}
async function loadAlerts() {
  if(!window.Desk?.has('alerts'))return;
  const coin=S.coin,request=++S.alertRequest;
  try {const rows=await api('/api/alerts?coin='+encodeURIComponent(coin)+'&limit=100');if(coin!==S.coin||request!==S.alertRequest)return;
    const combined=[...S.alerts,...rows],seen=new Set();renderAlerts(combined.filter(row=>{const key=row.id??JSON.stringify([row.ts,row.key,row.score]);if(seen.has(key))return false;seen.add(key);return true;}).sort((a,b)=>b.ts-a.ts));
  }catch{/* existing live alerts remain */}
}

// ---------- walls (large orders near the price) ----------
const WALL_EVENT = { new: "поставили", moved: "переставили", pulled: "сняли, не исполнив", eaten: "съели" };
function wallHtml(e, fresh) {
  const [venue, kind] = splitKey(e.key);
  const bid = e.side === "bid";
  let what = WALL_EVENT[e.event] || e.event;
  if (e.event === "moved") what += e.towards ? ` ${bid ? "⇡" : "⇣"} за ценой` : ` ${bid ? "⇣" : "⇡"}`;
  return `<li class="wall ${esc(e.event)}${fresh ? " fresh" : ""}" data-key="${esc(e.key)}">`
    + `<span class="t">${fmtTime(e.ts)}</span><span class="v">${esc(venue)}</span>${kindBadge(kind)}`
    + `<span class="${bid ? "pos" : "neg"} sz">${bid ? "▲" : "▼"} ${fmtUsd(e.usd)}</span>`
    + `<span class="num">${fmtPrice(e.price)}</span>`
    + `<span class="muted">${(e.dist_bps / 100).toFixed(2)}% · ×${e.ratio.toFixed(0)}${e.age >= 1 ? ` · ${Math.round(e.age)} с` : ""}</span>`
    + `<span class="ev">${what}</span></li>`;
}
function renderWalls(list) {S.walls=list.slice(0,300);window.Desk?.update('walls');}
function addWalls(rows) {S.walls=rows.slice().reverse().concat(S.walls).slice(0,300);window.Desk?.update('walls');}
async function loadWalls() {
  if(!window.Desk?.has('walls'))return;
  const coin=S.coin,request=++S.wallRequest;
  try {const rows=await api('/api/walls?limit=150');if(coin!==S.coin||request!==S.wallRequest)return;
    const seen=new Set();renderWalls([...S.walls,...rows].filter(row=>{const key=JSON.stringify([row.ts,row.key,row.event,row.price,row.usd]);if(seen.has(key))return false;seen.add(key);return true;}).sort((a,b)=>b.ts-a.ts));
  }catch{/* existing live wall events remain */}
}

// ---------- sound ----------
let audioCtx = null;
function beep() {
  if (!S.sound) return;
  try {
    audioCtx = audioCtx || new AudioContext();
    const t = audioCtx.currentTime;
    [880, 1320].forEach((f, i) => {
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + i * 0.14);
      g.gain.exponentialRampToValueAtTime(0.25, t + i * 0.14 + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.14 + 0.12);
      o.connect(g).connect(audioCtx.destination);
      o.start(t + i * 0.14);
      o.stop(t + i * 0.14 + 0.13);
    });
  } catch { /* audio blocked */ }
}
function renderSound() { $("soundBtn").textContent = S.sound ? "🔔 Звук" : "🔕 Без звука"; }
$("soundBtn").addEventListener("click", () => { S.sound = !S.sound; store.set("sound", S.sound); renderSound(); if (S.sound) beep(); });

// ---------- coin ----------
function setCoin(coin) {
  if(coin===S.coin)return;
  S.coin=coin;$('coinLabel').textContent=coin;$('coinInput').placeholder=coin;document.title=coin+' · Manipulation Radar';
  S.tape=[];S.alerts=[];S.walls=[];S.alertRequest++;S.wallRequest++;
  if(window.A)A.snap=null;window.resetOiChart?.();S.selected.clear();store.set('selected',[]);
  window.Desk?.resetCoin();sendFilter();loadAlerts();loadWalls();
}

$("coinForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const coin = $("coinInput").value.trim().toUpperCase();
  if (!coin) return;
  try {
    await api("/api/coin", { method: "POST", body: JSON.stringify({ coin }) });
    $("coinInput").value = "";
    S.streams = [];
    setCoin(coin);
    renderStreams();
    toast(`Переключаемся на ${coin}…`);
  } catch (err) {
    toast(err.message);
  }
});

// Per-coin collection settings (independent of tape display filters).
const V = { coin: "", selected: new Set(), all: true, request: 0 };
function renderVenueSelection() {
  const query = $("venuesSearch").value.toLowerCase();
  $("venuesAll").checked = V.all;
  $("venuesList").innerHTML = S.venues.filter((v) => v.name.toLowerCase().includes(query)).map((v) => `<label class="check"><input type="checkbox" data-venue="${esc(v.name)}" ${V.all || V.selected.has(v.name) ? "checked" : ""}> <span>${esc(v.name)}<small>${[v.spot && "спот", v.perp && "фьючерсы"].filter(Boolean).join(" · ")}</small></span></label>`).join("");
  $("venuesCount").textContent = `${V.all ? S.venues.length : V.selected.size} / ${S.venues.length}`;
}
async function loadVenueSelection() {
  const request = ++V.request;
  const coin = $("venuesCoin").value.trim().toUpperCase();
  $("venuesSave").disabled = true;
  V.coin = "";
  if (!/^[A-Z0-9]{1,20}$/.test(coin)) { toast("Введите тикер монеты"); return; }
  try {
    const data = await api(`/api/venues?coin=${encodeURIComponent(coin)}`);
    if (request !== V.request) return;
    V.coin = data.coin;
    V.all = data.venues === null;
    V.selected = new Set(data.venues || []);
    renderVenueSelection();
    $("venuesSave").disabled = false;
  } catch (err) { toast(err.message); }
}
$("venuesBtn").addEventListener("click", () => {
  $("venuesCoin").value = S.coin;
  $("venuesCoins").innerHTML = [...new Set([S.coin, ...S.watch, ...Object.keys(S.venueSelection)])].map((c) => `<option value="${esc(c)}"></option>`).join("");
  $("venuesSearch").value = "";
  $("venuesDlg").showModal();
  loadVenueSelection();
});
$("venuesCoin").addEventListener("input", () => {
  $("venuesSave").disabled = true;
  ++V.request;
  clearTimeout(V.timer);
  V.timer = setTimeout(loadVenueSelection, 300);
});
$("venuesCoin").addEventListener("change", loadVenueSelection);
$("venuesSearch").addEventListener("input", renderVenueSelection);
$("venuesSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
$("venuesAll").addEventListener("change", () => { V.all = $("venuesAll").checked; V.selected = new Set(V.all ? [] : S.venues.map((v) => v.name)); renderVenueSelection(); });
$("venuesNone").addEventListener("click", () => { V.all = false; V.selected.clear(); renderVenueSelection(); });
$("venuesList").addEventListener("change", (e) => {
  const name = e.target.dataset.venue;
  if (!name) return;
  if (V.all) { V.selected = new Set(S.venues.map((v) => v.name)); V.all = false; }
  if (e.target.checked) V.selected.add(name); else V.selected.delete(name);
  renderVenueSelection();
});
$("venuesCancel").addEventListener("click", () => $("venuesDlg").close());
$("venuesForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!V.coin || $("venuesSave").disabled || V.coin !== $("venuesCoin").value.trim().toUpperCase()) return;
  $("venuesSave").disabled = true;
  try {
    const data = await api("/api/venues", { method: "PUT", body: JSON.stringify({ coin: V.coin, venues: V.all ? null : [...V.selected] }) });
    if (data.venues === null) delete S.venueSelection[data.coin]; else S.venueSelection[data.coin] = data.venues;
    $("venuesDlg").close();
    toast(`Биржи для ${data.coin} сохранены`);
  } catch (err) { toast(err.message); } finally { $("venuesSave").disabled = false; }
});

// ---------- settings ----------
const FIELDS = [
  ["window_sec", "Окно анализа, сек", "за сколько последних секунд ищем аномалию"],
  ["baseline_sec", "База, сек", "с какой историей сравниваем (600 = 10 мин)"],
  ["spike_ratio", "Всплеск объёма, ×", "во сколько раз темп выше базы"],
  ["min_window_usd", "Мин. объём окна, $", "меньше — не считаем активностью"],
  ["imbalance", "Перекос сторон, доля", "0.75 = 75% объёма в одну сторону"],
  ["algo_min_repeats", "Алго: мин. сделок", "одинаковых по размеру подряд"],
  ["algo_size_tolerance", "Алго: допуск размера", "0.03 = ±3%"],
  ["algo_min_trade_usd", "Алго: мин. сделка, $", "мелочь не учитываем"],
  ["lead_bps", "Лидерство цены, bps", "уход от обычной премии к рынку"],
  ["alert_score", "Порог алерта, скор", "0–100"],
  ["alert_cooldown_sec", "Пауза алертов, сек", "для одной биржи"],
  ["wall_min_usd", "Плита: от, $", "меньше — не плита (для BTC ставь $500k+)"],
  ["wall_ratio", "Плита: × к стакану", "во сколько раз больше обычного уровня"],
  ["wall_band_bps", "Плита: близость, bps", "50 = не дальше 0.5% от цены"],
];
function openSettings() {
  const c = S.config;
  $("settingsGrid").innerHTML = FIELDS.map(([k, label, hint]) =>
    `<label>${label}<input name="${k}" type="number" step="any" value="${c[k]}"><small>${hint}</small></label>`).join("")
    + `<label class="check"><input name="walls" type="checkbox" ${c.walls ? "checked" : ""}> Искать плиты в стаканах</label>`
    + `<label class="check"><input name="wall_ignore_mm" type="checkbox" ${c.wall_ignore_mm ? "checked" : ""}> Не считать плитами плотности маркетмейкера (с обеих сторон, похожего размера)</label>`
    + `<p class="muted small">Пороги плит для отдельной биржи — клик по ячейке «Плита» в таблице.</p>`
    + `<label class="check"><input name="telegram" type="checkbox" ${c.telegram ? "checked" : ""}> Отправлять алерты в Telegram</label>`;
  $("tgStatus").textContent = S.telegram
    ? "Telegram настроен."
    : "Telegram не настроен: укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env и перезапустите.";
  $("settingsDlg").showModal();
}
$("settingsBtn").addEventListener("click", openSettings);
$("settingsForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  const body = {};
  for (const [k] of FIELDS) body[k] = Number($("settingsForm").elements[k].value);
  body.telegram = $("settingsForm").elements.telegram.checked;
  body.walls = $("settingsForm").elements.walls.checked;
  body.wall_ignore_mm = $("settingsForm").elements.wall_ignore_mm.checked;
  try {
    S.config = await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    toast("Настройки сохранены");
    renderStreams();
  } catch (err) {
    toast(err.message);
  }
});
$("tgTest").addEventListener("click", async () => {
  try { await api("/api/telegram/test", { method: "POST" }); toast("Тестовое сообщение отправлено"); } catch (err) { toast(err.message); }
});

// ---------- websocket ----------
function sendFilter() {
  if(S.ws&&S.ws.readyState===1){const filter=window.Desk?window.Desk.filter():{tape:false,walls:false,analytics:false,lite:true};S.ws.send(JSON.stringify({type:'filter',...filter}));}
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { $("conn").className = "conn on"; sendFilter(); };
  ws.onclose = () => { $("conn").className = "conn off"; setTimeout(connect, 2000); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "snapshot") {
      if (msg.coin !== S.coin) setCoin(msg.coin);
      S.streams = msg.streams;
      $("consensus").textContent = fmtPrice(msg.consensus);
      renderStreams();
    } else if (msg.type === "trades") {
      addTrades(msg.rows);
    } else if (msg.type === "walls") {
      addWalls(msg.rows);
    } else if (msg.type === "alert") {
      if (msg.alert.coin === S.coin) addAlert(msg.alert);
    } else if (msg.type === "coin") {
      setCoin(msg.coin);
    } else if (msg.type === "analytics") {
      onAnalytics(msg);
    } else if (msg.type === "watch") {
      S.watch = msg.coins || [];
    } else if (msg.type === "venues") {
      if (msg.venues === null) delete S.venueSelection[msg.coin];
      else S.venueSelection[msg.coin] = msg.venues;
      if (msg.coin === S.coin) { S.selected.clear(); renderChips(); rebuildTape(); sendFilter(); }
    }
  };
}

async function init() {
  renderSound();
  renderChips();
  try {
    const st = await api("/api/state");
    S.config = st.config;
    S.venues = st.venues;
    S.watch = st.watch || [];
    S.venueSelection = st.venue_selection || {};
    S.telegram = st.telegram;
    $("demoBadge").hidden = !st.demo;
    setCoin(st.coin);
  } catch (err) {
    toast("Сервер недоступен: " + err.message);
  }
  rebuildTape();
  connect();
}
document.addEventListener('DOMContentLoaded',init,{once:true});
