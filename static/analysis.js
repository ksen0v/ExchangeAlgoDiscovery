"use strict";
// Only visible screens are rendered; hidden windows request lightweight snapshots.
const A = { view: store.get("view", "tape"), snap: null,
  venueWin: store.get("venueWin", "5m"), venueSort: store.get("venueSort", "abs"), healthTimer: null };
window.A = A;
const WIN_RU = { "1m": "1м", "5m": "5м", "15m": "15м", "1h": "1ч", "30s": "30с" };
const winRu = (w) => WIN_RU[w] || w;

function fmtPct(v, d = 2) {
  if (v == null || !isFinite(v)) return '<span class="dim">—</span>';
  return `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : ""}${v.toFixed(d)}%</span>`;
}
function fmtSigned(v) {
  if (v == null) return '<span class="dim">—</span>';
  return `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtUsd(Math.abs(v))}</span>`;
}
function dbar(v, max) {
  if (v == null || !max) return '<span class="dbar"></span>';
  const w = Math.min(50, (Math.abs(v) / max) * 50);
  return `<span class="dbar"><i class="${v >= 0 ? "pos" : "neg"}" style="${v >= 0 ? "left:50%" : `left:${50 - w}%`};width:${w}%"></i></span>`;
}

function setView(view) {
  A.view = ["tape", "analysis", "health"].includes(view) ? view : "tape";
  store.set("view", A.view);
  for (const [v, id] of [["tape", "viewTape"], ["analysis", "viewAnalysis"], ["health", "viewHealth"]]) $(id).hidden = v !== A.view;
  document.querySelectorAll(".view-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === A.view));
  sendFilter();
  clearInterval(A.healthTimer);
  if (document.hidden) return;
  if (A.view === "analysis") {
    if (A.snap) renderAnalysis();
    api("/api/analytics").then(onAnalytics).catch((e) => toast(e.message));
  } else if (A.view === "health") {
    loadHealth();
    A.healthTimer = setInterval(loadHealth, 5000);
  } else { renderStreams(); rebuildTape(); }
}
function onAnalytics(msg) {
  if (msg.coin !== S.coin) return;
  A.snap = msg;
  if (A.view === "analysis" && !document.hidden) renderAnalysis();
}
function renderAnalysis() {
  if (!A.snap) return;
  renderCvd(A.snap.cvd);
  renderVenues(A.snap.delta);
  renderOi(A.snap.oi);
}
document.querySelectorAll(".view-btn").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
document.addEventListener("visibilitychange", () => setView(A.view));
function niceTicks(lo, hi, n = 4) {
  if (!(hi > lo)) return [lo];
  const step0 = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= step0) || step0;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(v);
  return out;
}

// time axis labels shared by the stacked charts
function timeTicks(t0, t1) {
  const span = t1 - t0, step = span > 4 * 3600 ? 3600 : span > 2 * 3600 ? 1800 : span > 3600 ? 900 : span > 1200 ? 300 : 60;
  const out = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return out;
}
const hhmm = (t) => new Date(t * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
const signedUsd = (v) => `${v < 0 ? "−" : v > 0 ? "+" : ""}${fmtUsd(Math.abs(v))}`;

// Crosshair + tooltip over a chart: points = [[t, ...]], html(point) -> tooltip text.
function attachHover(el, svg, xOf, points, html, plot) {
  const tip = el.querySelector(".tip");
  const cross = svg.querySelector(".cross");
  const show = (clientX) => {
    const r = svg.getBoundingClientRect();
    const x = (clientX - r.left) * (svg.viewBox.baseVal.width / r.width);
    if (x < plot.L || x > plot.R) { tip.hidden = true; cross.setAttribute("opacity", 0); return; }
    let best = points[0];
    for (const p of points) if (Math.abs(xOf(p[0]) - x) < Math.abs(xOf(best[0]) - x)) best = p;
    const cx = xOf(best[0]);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("opacity", 1);
    tip.innerHTML = html(best);
    tip.hidden = false;
    const px = (cx / svg.viewBox.baseVal.width) * r.width;
    tip.style.left = `${Math.min(r.width - tip.offsetWidth - 4, Math.max(4, px + 12))}px`;
  };
  svg.addEventListener("mousemove", (e) => { el.dataset.hx = e.clientX; show(e.clientX); });
  svg.addEventListener("mouseleave", () => { delete el.dataset.hx; tip.hidden = true; cross.setAttribute("opacity", 0); });
  // the chart is redrawn every second: keep the tooltip where the mouse is
  if (el.dataset.hx && el.matches(":hover")) show(Number(el.dataset.hx));
}

// Price on top, CVD of spot and perp below: one y-scale per panel, shared time axis (no dual axis).
function renderCvd(points) {
  const el = $("cvdChart");
  if (!points || points.length < 2) {
    el.innerHTML = '<div class="empty muted">Копим данные: линии появятся через минуту после выбора монеты</div>';
    return;
  }
  const W = Math.max(320, el.clientWidth - 16), L = 64, R = W - 16;
  const PT = 8, PH = 70, GAP = 18, CT = PT + PH + GAP, CH = 150, B = 20, H = CT + CH + B;
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const xs = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (R - L);
  const prices = points.map((p) => p[3]).filter((p) => p);
  let pmin = Math.min(...prices), pmax = Math.max(...prices);
  if (!(pmax > pmin)) { pmax = pmax * 1.001 || 1; pmin = pmin * 0.999; }
  const yp = (v) => PT + (1 - (v - pmin) / (pmax - pmin)) * PH;
  const cvals = points.flatMap((p) => [p[1], p[2]]);
  let cmin = Math.min(0, ...cvals), cmax = Math.max(0, ...cvals);
  if (cmax - cmin < 1) { cmax += 1; cmin -= 1; }
  const ys = (v) => CT + (1 - (v - cmin) / (cmax - cmin)) * CH;
  const line = (idx, f) => points.filter((p) => p[idx] != null).map((p, i) => `${i ? "L" : "M"}${xs(p[0]).toFixed(1)},${f(p[idx]).toFixed(1)}`).join("");
  let g = "";
  for (const v of niceTicks(pmin, pmax, 2)) {
    g += `<line class="grid" x1="${L}" x2="${R}" y1="${yp(v)}" y2="${yp(v)}"/><text x="${L - 6}" y="${yp(v) + 3}" text-anchor="end">${fmtPrice(v)}</text>`;
  }
  for (const v of niceTicks(cmin, cmax)) {
    g += `<line class="${v === 0 ? "zero" : "grid"}" x1="${L}" x2="${R}" y1="${ys(v)}" y2="${ys(v)}"/>`
      + `<text x="${L - 6}" y="${ys(v) + 3}" text-anchor="end">${signedUsd(v)}</text>`;
  }
  for (const t of timeTicks(t0, t1)) g += `<text x="${xs(t)}" y="${H - 5}" text-anchor="middle">${hhmm(t)}</text>`;
  g += `<text x="${L}" y="${PT + 9}" class="ptitle">цена</text><text x="${L}" y="${CT - 4}" class="ptitle">CVD, $</text>`;
  const last = points[points.length - 1];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Цена и CVD спота и фьючерсов">${g}
    <path d="${line(3, yp)}" class="s-price"/>
    <path d="${line(1, ys)}" class="s-spot"/><path d="${line(2, ys)}" class="s-perp"/>
    <text x="${R - 2}" y="${ys(last[1]) + (last[1] >= last[2] ? -6 : 14)}" text-anchor="end" class="lbl">спот ${signedUsd(last[1])}</text>
    <text x="${R - 2}" y="${ys(last[2]) + (last[2] > last[1] ? -6 : 14)}" text-anchor="end" class="lbl">фьюч. ${signedUsd(last[2])}</text>
    <line class="cross" x1="0" x2="0" y1="${PT}" y2="${CT + CH}" opacity="0"/>
  </svg><div class="tip" hidden></div>`;
  attachHover(el, el.querySelector("svg"), xs, points, (p) =>
    `<b>${hhmm(p[0])}</b><br>цена ${fmtPrice(p[3])}<br><i class="lg spot"></i>спот ${signedUsd(p[1])}<br><i class="lg perp"></i>фьюч. ${signedUsd(p[2])}`,
  { L, R });
}

function renderVenues(d) {
  if (!d) { $("venueBody").innerHTML = ""; return; }
  const wins = d.windows.map((r) => r.w);
  if (!wins.includes(A.venueWin)) A.venueWin = wins.includes("5m") ? "5m" : wins[0];
  $("venueWin").innerHTML = wins.map((w) => `<button class="${w === A.venueWin ? "active" : ""}" data-w="${w}">${winRu(w)}</button>`).join("");
  const rows = (d.venues[A.venueWin] || []).slice();
  const key = A.venueSort;
  rows.sort((a, b) => key === "key" ? a.key.localeCompare(b.key) : key === "abs" ? Math.abs(b.delta) - Math.abs(a.delta) : b[key] - a[key]);
  const totalAbs = rows.reduce((s, r) => s + Math.abs(r.delta), 0) || 1;
  const max = Math.max(1, ...rows.map((r) => Math.abs(r.delta)));
  document.querySelectorAll("#venueHead th").forEach((th) => th.classList.toggle("sorted", th.dataset.sort === key));
  $("venueBody").innerHTML = rows.map((r) => {
    const [venue, kind] = splitKey(r.key);
    return `<tr><td class="venue">${esc(venue)}${kindBadge(kind)}</td><td class="r pos">${fmtUsd(r.buy)}</td><td class="r neg">${fmtUsd(r.sell)}</td>
      <td class="r">${fmtSigned(r.delta)}</td><td>${dbar(r.delta, max)} <span class="zb">${(Math.abs(r.delta) / totalAbs * 100).toFixed(0)}%</span></td>
      </tr>`;
  }).join("") || '<tr><td colspan="5" class="empty">Сделок за окно нет</td></tr>';
}
$("venueWin").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-w]");
  if (b) { A.venueWin = b.dataset.w; store.set("venueWin", A.venueWin); renderVenues(A.snap && A.snap.delta); }
});
$("venueHead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-sort]");
  if (th) { A.venueSort = th.dataset.sort; store.set("venueSort", A.venueSort); renderVenues(A.snap && A.snap.delta); }
});

function fmtCoins(v) {
  return v == null ? '<span class="dim">—</span>' : fmtQty(v);
}
function renderOi(oi) {
  if (!oi) { $("oiBody").innerHTML = '<tr><td colspan="9" class="empty">Ждём данные открытого интереса</td></tr>'; return; }
  const t = oi.totals;
  $("oiHint").textContent = "По включённым биржам · обновление 5–10 с";
  const row = (r, total) => {
    const [venue, kind] = total ? ["Все биржи", ""] : splitKey(r.key);
    const share = r.share && r.share["5m"];
    return `<tr class="${total ? "total" : ""}"><td class="venue">${esc(venue)}${kind ? kindBadge(kind) : ""}</td>
      <td class="r">${fmtCoins(r.coins)}</td><td class="r">${r.usd ? fmtUsd(r.usd) : '<span class="dim">—</span>'}</td>
      ${["1m", "5m", "15m", "1h"].map((w) => `<td class="r">${fmtPct((r.pct || {})[w])}</td>`).join("")}
      <td>${total ? "" : dbar(share, 1)} ${share != null ? `<span class="zb">${(share * 100).toFixed(0)}%</span>` : ""}</td>
      <td class="small muted">${total ? "" : esc(r.coins == null && r.oi_age != null ? "данные устарели" : r.oi)}</td></tr>`;
  };
  const totalUsd = oi.venues.reduce((s, r) => s + (r.usd || 0), 0);
  $("oiBody").innerHTML = oi.venues.length
    ? oi.venues.map((r) => row(r, false)).join("") + row({ ...t, usd: totalUsd }, true)
    : '<tr><td colspan="9" class="empty">Ждём данные ОИ: опрос раз в 5–10 с. Биржи без публичного ОИ — на экране «Подключения»</td></tr>';
}


async function loadHealth() {
  try {
    const h = await api("/api/health");
    if (A.view !== "health" || document.hidden) return;
    const rows = h.streams.filter((s) => $("healthAll").checked || !["na", "noapi"].includes(s.status));
    $("healthBody").innerHTML = rows.map((s) => `<tr><td>${esc(s.key)}</td><td>${esc(s.status)}</td><td>${esc(s.transport)}</td><td class="r">${s.latency_ms ?? "—"}</td><td class="r">${s.jitter_ms ?? "—"}</td><td class="r">${s.last_msg_age ?? "—"}</td><td class="r">${s.reconnects}</td><td class="r">${s.gaps}</td><td class="r">${s.backfilled}</td><td>${esc(s.book_status)}</td><td>${esc(s.error || s.book_error)}</td></tr>`).join("");
    $("feedsBody").innerHTML = h.feeds.map((s) => `<tr><td>${esc(s.key)}</td><td>${esc(s.oi)}</td><td class="r">${s.oi_age == null ? "—" : s.oi_age + "с"}</td></tr>`).join("");
  } catch (err) { toast(err.message); }
}
$("healthAll").addEventListener("change", loadHealth);
setView(A.view);
