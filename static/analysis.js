"use strict";
const A = {view: store.get("view", "dashboard"), snap: null,
  venueWin: store.get("venueWin", "5m"), venueSort: store.get("venueSort", "abs"), healthTimer: null,
  oiKey: store.get("oiKey", "__total__"), oiUnit: store.get("oiUnit", "coins"),
  oiSeconds: store.get("oiSeconds", 3600), cvdSeconds: store.get("cvdSeconds", 3600),
  oiTimer: null, oiAbort: null, oiHistory: null, oiOptions: "", oiRequestedCoin: null};
window.A = A;
const WIN_RU = {"1m":"1м", "5m":"5м", "15m":"15м", "1h":"1ч"};
const winRu = w => WIN_RU[w] || w;
const signedUsd = v => `${v < 0 ? "−" : v > 0 ? "+" : ""}${fmtUsd(Math.abs(v))}`;
function fmtPct(v) {
  if (v == null || !Number.isFinite(v)) return '<span class="dim">—</span>';
  return `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : ""}${v.toFixed(2)}%</span>`;
}
function fmtSigned(v) { return v == null ? '<span class="dim">—</span>' : `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${signedUsd(v)}</span>`; }
function dbar(v, max) {
  if (v == null || !max) return '<span class="dbar"></span>';
  const w = Math.min(50, Math.abs(v) / max * 50);
  return `<span class="dbar"><i class="${v >= 0 ? "pos" : "neg"}" style="left:${v >= 0 ? 50 : 50 - w}%;width:${w}%"></i></span>`;
}
for (const name of ["oiSeconds", "cvdSeconds"]) if (![900,3600,14400,86400].includes(A[name])) A[name] = 3600;
if (!["coins", "usd"].includes(A.oiUnit)) A.oiUnit = "coins";
const cvdChart = new RadarChart($("cvdChart"), {span: A.cvdSeconds, label: "Цена, CVD спота и фьючерсов",
  series: [{index: 3, label: "Цена", color: "--text-2", format: fmtPrice},
    {index: 1, pane: 1, label: "CVD спот", color: "--series-spot", format: signedUsd},
    {index: 2, pane: 1, label: "CVD фьючерсы", color: "--series-perp", format: signedUsd}],
  onView: follow => $("cvdLive").classList.toggle("history", !follow)});
const oiChart = new RadarChart($("oiChart"), {span: A.oiSeconds, label: "Открытый интерес",
  onView: follow => $("oiLive").classList.toggle("history", !follow)});

function setView(view) {
  A.view = view === "health" ? "health" : "dashboard";
  store.set("view", A.view);
  $("viewTape").hidden = A.view !== "dashboard";
  $("viewHealth").hidden = A.view !== "health";
  document.querySelectorAll(".view-btn").forEach(b => b.classList.toggle("active", b.dataset.view === A.view));
  const visible = !document.hidden && A.view === "dashboard";
  cvdChart.setActive(visible); oiChart.setActive(visible);
  sendFilter();
  clearInterval(A.healthTimer); clearInterval(A.oiTimer);
  if (!visible) A.oiAbort?.abort();
  if (document.hidden) return;
  if (A.view === "dashboard") {
    renderStreams(); rebuildTape();
    if (A.snap) renderAnalysis();
    api("/api/analytics").then(onAnalytics).catch(e => toast(e.message));
    loadOiHistory(); A.oiTimer = setInterval(loadOiHistory, 10000);
  } else { loadHealth(); A.healthTimer = setInterval(loadHealth, 5000); }
}
function onAnalytics(msg) {
  if (msg.coin !== S.coin) return;
  A.snap = msg;
  if (A.view === "dashboard" && !document.hidden) renderAnalysis();
}
function renderAnalysis() {
  if (!A.snap) return;
  cvdChart.update(A.snap.cvd, {now: A.snap.ts});
  renderVenues(A.snap.delta); renderOi(A.snap.oi);
}
function renderVenues(d) {
  if (!d) { $("venueBody").innerHTML = ""; return; }
  const wins = d.windows.map(r => r.w);
  if (!wins.includes(A.venueWin)) A.venueWin = wins.includes("5m") ? "5m" : wins[0];
  const sig = wins.join() + A.venueWin;
  if ($("venueWin").dataset.sig !== sig) {
    $("venueWin").dataset.sig = sig;
    $("venueWin").innerHTML = wins.map(w => `<button class="${w === A.venueWin ? "active" : ""}" data-w="${w}">${winRu(w)}</button>`).join("");
  }
  const rows = (d.venues[A.venueWin] || []).slice(), key = A.venueSort;
  rows.sort((a,b) => key === "key" ? a.key.localeCompare(b.key) : key === "abs" ? Math.abs(b.delta) - Math.abs(a.delta) : b[key] - a[key]);
  const total = rows.reduce((s,r) => s + Math.abs(r.delta), 0) || 1, max = Math.max(1, ...rows.map(r => Math.abs(r.delta)));
  document.querySelectorAll("#venueHead th").forEach(th => th.classList.toggle("sorted", th.dataset.sort === key));
  $("venueBody").innerHTML = rows.map(r => {
    const [venue, kind] = splitKey(r.key);
    return `<tr><td class="venue">${esc(venue)}${kindBadge(kind)}</td><td class="r pos">${fmtUsd(r.buy)}</td><td class="r neg">${fmtUsd(r.sell)}</td><td class="r">${fmtSigned(r.delta)}</td><td>${dbar(r.delta,max)} <span class="zb">${(Math.abs(r.delta)/total*100).toFixed(0)}%</span></td></tr>`;
  }).join("") || '<tr><td colspan="5" class="empty">Нет сделок за выбранное окно</td></tr>';
}
$("venueWin").addEventListener("click", e => { const b=e.target.closest("button[data-w]"); if(b){ A.venueWin=b.dataset.w; store.set("venueWin",A.venueWin); renderVenues(A.snap?.delta); } });
$("venueHead").addEventListener("click", e => { const th=e.target.closest("th[data-sort]"); if(th){ A.venueSort=th.dataset.sort; store.set("venueSort",A.venueSort); renderVenues(A.snap?.delta); } });

function renderOi(oi) {
  const rows = (oi?.venues || []).filter(r => r.coins != null || r.oi_age != null);
  $("oiHint").textContent = rows.length ? `${oi.totals.count} бирж · 5–10 с` : "";
  const row = (r,total) => {
    const name = total ? "Все биржи" : splitKey(r.key)[0];
    const title = total ? "Сумма доступных свежих данных" : `${r.key} · ${r.coins == null ? "данные устарели" : "обновлено " + r.oi_age + " с назад"} · Δ15м: ${r.pct?.["15m"]?.toFixed(2) ?? "—"}%`;
    return `<tr class="${total ? "total" : ""}" title="${esc(title)}"><td class="venue"><button class="ghost small oi-open" data-oi-key="${total ? "__total__" : esc(r.key)}">${esc(name)}${!total && r.coins == null ? ' <span class="warn">·</span>' : ""}</button></td><td class="r">${r.coins == null ? "—" : fmtQty(r.coins)}</td><td class="r">${fmtUsd(r.usd)}</td>${["1m","5m","1h"].map(w => `<td class="r">${fmtPct(r.pct?.[w])}</td>`).join("")}</tr>`;
  };
  $("oiBody").innerHTML = rows.length ? rows.map(r => row(r,false)).join("") + row({...oi.totals, coins:oi.totals.count ? oi.totals.coins : null, usd:oi.totals.count ? oi.totals.usd : null},true)
    : '<tr><td colspan="6" class="empty">Публичных данных OI пока нет. Здесь появятся только биржи, передавшие OI этой монеты.</td></tr>';
  syncOiSelector(rows);
}
function syncOiSelector(rows) {
  const keys = rows.map(r => r.key).sort();
  if (keys.length) keys.unshift("__total__");
  const signature = JSON.stringify(keys);
  if (A.oiOptions !== signature) {
    A.oiOptions = signature;
    $("oiVenue").innerHTML = keys.map(k => `<option value="${esc(k)}">${k === "__total__" ? "Все биржи" : esc(splitKey(k)[0])}</option>`).join("");
    $("oiVenue").disabled = !keys.length;
  }
  const previous = A.oiKey;
  if (!keys.includes(A.oiKey)) A.oiKey = keys[0] || "";
  $("oiVenue").value = A.oiKey;
  if (previous !== A.oiKey || A.oiRequestedCoin !== S.coin) {
    A.oiHistory = null;
    renderOiChart(); loadOiHistory();
  }
}
function resetOiChart() {
  A.oiAbort?.abort(); A.oiHistory = null; A.oiRequestedCoin = null; A.oiOptions = "";
  oiChart.reset(); cvdChart.reset();
  renderVenues(null); renderOi(null); renderOiChart();
}
window.resetOiChart = resetOiChart;
async function loadOiHistory() {
  if (document.hidden || A.view !== "dashboard" || !A.oiKey || A.snap?.coin !== S.coin) return;
  A.oiAbort?.abort();
  const controller = new AbortController(); A.oiAbort = controller;
  const coin = S.coin, key = A.oiKey; A.oiRequestedCoin = coin;
  try {
    // Fetch the bounded archive so dragging into the past never clears the viewport.
    const data = await api(`/api/oi/history?coin=${encodeURIComponent(coin)}&key=${encodeURIComponent(key)}&seconds=86400`, {signal:controller.signal});
    if (controller.signal.aborted || data.coin !== S.coin || data.key !== A.oiKey) return;
    A.oiHistory = data; renderOiChart();
  } catch(err) { if(!controller.signal.aborted) $("oiChartHint").textContent=err.message; }
}
function renderOiChart() {
  $("oiUnit").value = A.oiUnit;
  const data = A.oiHistory, index = A.oiUnit === "usd" ? 2 : 1;
  const valid = data && data.coin === S.coin && data.key === A.oiKey;
  const name = A.oiKey === "__total__" ? "Все биржи" : A.oiKey ? splitKey(A.oiKey)[0] : "";
  const last = valid ? data.points.filter(p => p[index] != null).at(-1) : null;
  const fmt = A.oiUnit === "usd" ? fmtUsd : fmtQty;
  $("oiChartHint").textContent = name ? `${name} · ${S.coin}${last ? " · " + fmt(last[index]) : ""}${data?.stale ? " · данные устарели" : ""}` : "Нет доступных бирж с OI";
  $("oiChartHint").title = "История собирается во время работы. Разрыв линии означает пропуск данных; для суммы — также изменение состава бирж.";
  oiChart.update(valid ? data.points : [], {now: valid ? data.ts : Date.now()/1000, maxGap:90,
    membersIndex: A.oiKey === "__total__" ? 3 : null,
    series:[{index, label:A.oiUnit === "usd" ? "OI, $" : "OI, монет", color:"--accent", format:fmt}],
    empty:A.oiKey ? "Ждём первые данные OI" : "Нет доступного открытого интереса"});
}
function selectOiVenue(key) {
  A.oiKey=key; store.set("oiKey",key); $("oiVenue").value=key;
  A.oiHistory=null; oiChart.reset(); renderOiChart(); loadOiHistory();
}
$("oiVenue").addEventListener("change", () => selectOiVenue($("oiVenue").value));
$("oiBody").addEventListener("click", e => { const b=e.target.closest("button[data-oi-key]"); if(b) selectOiVenue(b.dataset.oiKey); });
$("oiUnit").addEventListener("change", () => { A.oiUnit=$("oiUnit").value; store.set("oiUnit",A.oiUnit); oiChart.yZoom=1; renderOiChart(); });
for (const [id,chart,field] of [["oiRange",oiChart,"oiSeconds"],["cvdRange",cvdChart,"cvdSeconds"]]) {
  const sync=() => $(id).querySelectorAll("button").forEach(b => b.classList.toggle("active",Number(b.dataset.seconds)===A[field]));
  sync();
  $(id).addEventListener("click", e => { const b=e.target.closest("button[data-seconds]"); if(!b)return; A[field]=Number(b.dataset.seconds); store.set(field,A[field]); chart.live(A[field]); sync(); });
}
$("oiLive").addEventListener("click",()=>oiChart.live(A.oiSeconds));
$("cvdLive").addEventListener("click",()=>cvdChart.live(A.cvdSeconds));
document.querySelectorAll(".view-btn").forEach(b=>b.addEventListener("click",()=>setView(b.dataset.view)));
document.addEventListener("visibilitychange",()=>setView(A.view));
function enableOverlayButtons() {
  document.querySelectorAll("[data-overlay]").forEach(b => b.hidden = !window.radarDesktop);
}
document.addEventListener("radar-desktop-ready", enableOverlayButtons);
enableOverlayButtons();
document.querySelectorAll("[data-overlay]").forEach(b => b.addEventListener("click", () => window.radarDesktop?.toggleOverlay(b.dataset.overlay)));
async function loadHealth() {
  try {
    const h=await api("/api/health"); if(A.view!=="health"||document.hidden)return;
    const rows=h.streams.filter(s=>$("healthAll").checked||!["na","noapi"].includes(s.status));
    $("healthBody").innerHTML=rows.map(s=>`<tr><td>${esc(s.key)}</td><td>${esc(s.status)}</td><td>${esc(s.transport)}</td><td class="r">${s.latency_ms??"—"}</td><td class="r">${s.jitter_ms??"—"}</td><td class="r">${s.last_msg_age??"—"}</td><td class="r">${s.reconnects}</td><td class="r">${s.gaps}</td><td class="r">${s.backfilled}</td><td>${esc(s.book_status)}</td><td>${esc(s.error||s.book_error)}</td></tr>`).join("");
    $("feedsBody").innerHTML=h.feeds.map(s=>`<tr><td>${esc(s.key)}</td><td>${esc(s.oi)}</td><td class="r">${s.oi_age==null?"—":s.oi_age+"с"}</td></tr>`).join("");
  }catch(err){toast(err.message);}
}
$("healthAll").addEventListener("change",loadHealth);
setView(A.view);
