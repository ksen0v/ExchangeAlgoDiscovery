"use strict";
// Screens "Анализ рынка" (М1–М4), "Журнал сигналов" (М15), "Здоровье" (ТЗ 3.5) and the regime badge.
// Uses the helpers of app.js: $, esc, api, toast, fmtUsd, fmtPrice, fmtTime, splitKey, kindBadge, S, sendFilter.

const A = {
  view: store.get("view", "tape"),
  snap: null,
  venueWin: store.get("venueWin", "5m"),
  venueSort: store.get("venueSort", "abs"),
  indexSrc: store.get("indexSrc", "Binance"),
  modules: null,
  healthTimer: null,
  journalTimer: null,
  healthAll: false,
};
window.A = A;

const TONE_ARROW = { bull: "▲", bear: "▼", warn: "◆", flat: "■", none: "·" };
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
function fmtZ(z) {
  if (z == null) return "";
  return ` <span class="zb${Math.abs(z) >= 3 ? " hi" : ""}" title="робастный z-score к норме">z${z > 0 ? "+" : ""}${z.toFixed(1)}</span>`;
}
function fmtNum(v, d = 3) {
  if (v == null || !isFinite(v)) return '<span class="dim">—</span>';
  return `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : ""}${v.toFixed(d)}</span>`;
}
function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)} с`;
  if (s < 3600) return `${Math.floor(s / 60)} мин`;
  return `${Math.floor(s / 3600)} ч ${Math.floor((s % 3600) / 60)} мин`;
}
function dbar(v, max) {
  if (v == null || !max) return '<span class="dbar"></span>';
  const w = Math.min(50, (Math.abs(v) / max) * 50);
  return `<span class="dbar"><i class="${v >= 0 ? "pos" : "neg"}" style="${v >= 0 ? "left:50%" : `left:${50 - w}%`};width:${w}%"></i></span>`;
}
function arrow(x) {
  if (x == null) return '<span class="dim">ждём</span>';
  if (x > 0) return '<span class="arrow-up">↑</span>';
  if (x < 0) return '<span class="arrow-down">↓</span>';
  return "≈0";
}

// ---------- views ----------
function setView(v) {
  if (!["tape", "analysis", "journal", "health"].includes(v)) v = "tape";
  A.view = v;
  store.set("view", v);
  document.querySelectorAll(".view-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $("viewTape").hidden = v !== "tape";
  $("viewAnalysis").hidden = v !== "analysis";
  $("viewJournal").hidden = v !== "journal";
  $("viewHealth").hidden = v !== "health";
  sendFilter();
  clearInterval(A.healthTimer);
  clearInterval(A.journalTimer);
  if (v === "analysis") {
    if (A.snap) renderAnalysis();
    else api("/api/analytics").then((s) => { if (s.delta !== undefined) { A.snap = s; renderAnalysis(); } }).catch(() => {});
  } else if (v === "journal") {
    loadJournal();
    A.journalTimer = setInterval(loadJournal, 15000);
  } else if (v === "health") {
    loadHealth();
    A.healthTimer = setInterval(loadHealth, 2000);
  }
}
document.querySelectorAll(".view-btn").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$("regimeStrip").addEventListener("click", () => setView("analysis"));

// ---------- regime badge (header) ----------
function renderRegimeStrip(windows, main) {
  $("regimeStrip").innerHTML = (windows || []).map((w) =>
    `<span class="rbadge tone-${w.tone}${w.w === main ? " main" : ""}" title="${esc(`${winRu(w.w)}: ${w.label}, ${ago(w.since)}`)}">`
    + `<b>${winRu(w.w)}</b>${TONE_ARROW[w.tone] || ""} ${esc(w.label.split(" · ")[0])}</span>`).join("");
}
function onRegime(msg) {
  if (msg.coin !== S.coin) return;
  renderRegimeStrip(msg.windows, msg.main);
}
function onAnalytics(msg) {
  if (msg.coin !== S.coin) return;
  A.snap = msg;
  if (msg.regime) renderRegimeStrip(msg.regime.windows, msg.regime.main);
  if (A.view === "analysis") renderAnalysis();
}

// ---------- analysis ----------
function renderAnalysis() {
  const s = A.snap;
  if (!s) return;
  const notes = [];
  if (s.collect_only) notes.push("Режим «только сбор данных»: метрики и журнал пишутся, алертов нет.");
  if (s.low_history) {
    notes.push(`Мало истории (${(s.history_days || 0).toFixed(1)} дн. из 7): нормы и перцентили считаются по текущей сессии, `
      + "сигналы помечаются «мало истории». Первые ~5 минут после выбора монеты нормы ещё нет — сигналы молчат.");
  }
  $("analysisNote").hidden = !notes.length;
  $("analysisNote").textContent = notes.join(" ");
  renderRegime(s.regime);
  renderSignals(s.signals || []);
  renderCvd(s.cvd, s.book && s.book.icebergs);
  renderDelta(s.delta);
  renderVenues(s.delta);
  renderOi(s.oi);
  renderFunding(s.funding);
  renderLiq(s.liq);
  renderBook(s.book);
  renderBorrow(s.borrow);
  renderIndex(s.index);
}

function renderRegime(r) {
  if (!r) {
    $("regimeCards").innerHTML = '<p class="muted">Модуль «режим» выключен в настройках.</p>';
    return;
  }
  $("regimeHint").textContent = "P — цена, O — открытый интерес, Dp/Ds — дельта фьючерсов/спота, L — ликвидации";
  $("regimeCards").innerHTML = r.windows.map((w) => {
    const i = w.inputs;
    const miss = w.missing.length ? `<p class="muted small">${w.missing.map(esc).join("<br>")}</p>` : "";
    return `<div class="rcard tone-${w.tone}${w.w === r.main ? " main" : ""}">
      <div class="w"><span>окно ${winRu(w.w)}${w.w === r.main ? " · основное" : ""}</span><span>${ago(w.since)}</span></div>
      <div class="lbl">${esc(w.label)}</div>
      <div class="ins">
        <span>P цена</span><b>${arrow(i.P)} ${fmtPct(i.ret)} <span class="zb">зона ±${i.price_deadzone.toFixed(2)}%</span></b>
        <span>O ОИ</span><b>${arrow(i.O)} ${fmtPct(i.oi_pct)}</b>
        <span>Dp фьюч.</span><b>${arrow(i.Dp)}${fmtZ(i.z_perp)}</b>
        <span>Ds спот</span><b>${arrow(i.Ds)}${fmtZ(i.z_spot)}</b>
        <span>L ликв.</span><b>${i.L ? '<span class="warn">да</span>' : "нет"} <span class="zb">${fmtUsd(i.liq_usd)}</span></b>
      </div>${miss}</div>`;
  }).join("");
  $("regimeDiverge").hidden = !r.divergence;
  $("regimeDiverge").textContent = r.divergence ? "⚠ " + r.divergence : "";
  $("regimeHistory").innerHTML = (r.history || []).map((h) =>
    `<li><span class="num muted">${fmtTime(h.ts)}</span> ${winRu(h.window)}: ${esc(h.from)} → <b class="tone-${h.tone}" style="background:none;border:0">${esc(h.to)}</b></li>`).join("")
    || '<li class="muted">Смен пока не было</li>';
}

function renderSignals(list) {
  $("signalsHint").textContent = list.length ? `активно: ${list.length}` : "";
  $("signalsList").innerHTML = list.map((g) => {
    const [venue, kind] = g.key.includes(":") ? splitKey(g.key) : ["Все биржи", ""];
    return `<li class="signal-item"><div class="alert-top"><span class="t">${fmtTime(g.since)}</span>
      <span class="v">${esc(g.title)}</span><span class="muted">${esc(g.module)} · ${esc(venue)}${kind ? " " + kind : ""}</span>
      <span class="s">${ago(g.since)}</span></div>
      <ul class="alert-reasons">${(g.reasons || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></li>`;
  }).join("") || '<li class="empty">Активных сигналов нет. Каждый сигнал объясняет себя: модуль, биржа, значение, норма, отклонение.</li>';
}

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
function renderCvd(points, icebergs) {
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
  // icebergs (М5): hidden buyer ▲ / hidden seller ▼ at their price
  const ice = (icebergs || []).filter((e) => e[0] >= t0 && e[1] >= pmin && e[1] <= pmax).map((e) =>
    `<g class="ice"><title>${hhmm(e[0])} айсберг на ${e[2] === "bid" ? "покупку" : "продажу"} по ${fmtPrice(e[1])}</title>`
    + `<text x="${xs(e[0])}" y="${yp(e[1]) + (e[2] === "bid" ? 12 : -4)}" text-anchor="middle">${e[2] === "bid" ? "▲" : "▼"}</text></g>`).join("");
  const last = points[points.length - 1];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Цена и CVD спота и фьючерсов">${g}
    <path d="${line(3, yp)}" class="s-price"/>${ice}
    <path d="${line(1, ys)}" class="s-spot"/><path d="${line(2, ys)}" class="s-perp"/>
    <text x="${R - 2}" y="${ys(last[1]) + (last[1] >= last[2] ? -6 : 14)}" text-anchor="end" class="lbl">спот ${signedUsd(last[1])}</text>
    <text x="${R - 2}" y="${ys(last[2]) + (last[2] > last[1] ? -6 : 14)}" text-anchor="end" class="lbl">фьюч. ${signedUsd(last[2])}</text>
    <line class="cross" x1="0" x2="0" y1="${PT}" y2="${CT + CH}" opacity="0"/>
  </svg><div class="tip" hidden></div>`;
  attachHover(el, el.querySelector("svg"), xs, points, (p) =>
    `<b>${hhmm(p[0])}</b><br>цена ${fmtPrice(p[3])}<br><i class="lg spot"></i>спот ${signedUsd(p[1])}<br><i class="lg perp"></i>фьюч. ${signedUsd(p[2])}`,
  { L, R });
}

function renderDelta(d) {
  if (!d) { $("deltaBody").innerHTML = '<tr><td colspan="8" class="empty">Модуль М1 выключен</td></tr>'; return; }
  const dep = d.depth || {};
  $("depthHint").textContent = `depth_1%: спот ${dep.spot ? fmtUsd(dep.spot) : "—"}, фьючерсы ${dep.perp ? fmtUsd(dep.perp) : "—"} (медиана за час, сумма бирж)`;
  $("deltaBody").innerHTML = d.windows.map((r) => `<tr>
    <td>${winRu(r.w)}</td>
    <td class="r">${fmtSigned(r.delta_spot)}</td>
    <td class="r">${fmtSigned(r.delta_perp)}</td>
    <td class="r" title="${esc(r.norm_spot || "нет нормировки")}">${fmtNum(r.nd_spot)}${fmtZ(r.z_spot)}</td>
    <td class="r" title="${esc(r.norm_perp || "нет нормировки")}">${fmtNum(r.nd_perp)}${fmtZ(r.z_perp)}</td>
    <td class="r">${fmtNum(r.div)}</td>
    <td class="r">${fmtPct(r.ret)}</td>
    <td class="r">${fmtNum(r.eff, 2)}</td></tr>`).join("");
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
      <td class="r">${fmtSigned(r.delta)}</td><td>${dbar(r.delta, max)} <span class="zb">${(Math.abs(r.delta) / totalAbs * 100).toFixed(0)}%</span></td></tr>`;
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
  if (!oi) { $("oiBody").innerHTML = '<tr><td colspan="9" class="empty">Модуль М2 выключен</td></tr>'; return; }
  const t = oi.totals;
  $("oiHint").textContent = t.d5_depth != null ? `ΔОИ 5м всех бирж = ${t.d5_depth.toFixed(2)} × depth_1% спота` : "";
  const row = (r, total) => {
    const [venue, kind] = total ? ["Все биржи", ""] : splitKey(r.key);
    const share = r.share && r.share["5m"];
    return `<tr class="${total ? "total" : ""}"><td class="venue">${esc(venue)}${kind ? kindBadge(kind) : ""}</td>
      <td class="r">${fmtCoins(r.coins)}</td><td class="r">${r.usd ? fmtUsd(r.usd) : '<span class="dim">—</span>'}</td>
      ${["1m", "5m", "15m", "1h"].map((w) => `<td class="r">${fmtPct((r.pct || {})[w])}</td>`).join("")}
      <td>${total ? "" : dbar(share, 1)} ${share != null ? `<span class="zb">${(share * 100).toFixed(0)}%</span>` : ""}</td>
      <td class="r">${r.z5 != null ? `<span class="${Math.abs(r.z5) >= 3 ? "warn" : ""}">${r.z5 > 0 ? "+" : ""}${r.z5.toFixed(1)}</span>` : '<span class="dim">—</span>'}</td></tr>`;
  };
  const totalUsd = oi.venues.reduce((s, r) => s + (r.usd || 0), 0);
  $("oiBody").innerHTML = oi.venues.length
    ? oi.venues.map((r) => row(r, false)).join("") + row({ ...t, usd: totalUsd }, true)
    : '<tr><td colspan="9" class="empty">Ждём данные ОИ: опрос раз в 5–10 с. Биржи без публичного ОИ — на экране «Здоровье»</td></tr>';
}

function heat(v, scale) {
  if (v == null) return "";
  const a = Math.min(0.55, Math.abs(v) / scale * 0.55);
  return `background:${v > 0 ? `rgba(46,189,133,${a})` : `rgba(246,70,93,${a})`}`;
}
function renderFunding(f) {
  if (!f) { $("fundBody").innerHTML = '<tr><td colspan="8" class="empty">Модуль М4 выключен</td></tr>'; return; }
  const parts = [];
  if (f.f8_median != null) parts.push(`медиана f8 ${f.f8_median.toFixed(4)}%`);
  if (f.f8_spread != null) parts.push(`разброс ${f.f8_spread.toFixed(4)}%`);
  if (f.cross_basis != null) parts.push(`базис ${f.cross_ref} перп к споту всех бирж ${f.cross_basis > 0 ? "+" : ""}${f.cross_basis.toFixed(3)}%`);
  if (f.premium != null) parts.push(`премия ${f.premium > 0 ? "+" : ""}${f.premium.toFixed(3)}%`);
  $("fundHint").textContent = parts.join(" · ");
  const f8max = Math.max(0.01, ...f.venues.map((r) => Math.abs(r.f8 || 0)));
  const bmax = Math.max(0.05, ...f.venues.map((r) => Math.abs(r.basis || 0)));
  $("fundBody").innerHTML = f.venues.map((r) => {
    const [venue] = splitKey(r.key);
    const left = r.next_ts ? Math.max(0, r.next_ts - Date.now() / 1000) : null;
    return `<tr><td class="venue">${esc(venue)}</td>
      <td class="r">${r.rate != null ? (r.rate * 100).toFixed(4) + "%" : '<span class="dim">—</span>'}</td>
      <td class="r">${r.interval_h ? r.interval_h + "ч" : "—"}${r.interval_assumed ? ' <span class="zb" title="Биржа не сообщила период, принято 8 ч">?</span>' : ""}</td>
      <td class="r h" style="${heat(r.f8, f8max)}">${r.f8 != null ? (r.f8 > 0 ? "+" : "") + r.f8.toFixed(4) + "%" : '<span class="dim">—</span>'}</td>
      <td class="r">${r.annual != null ? r.annual.toFixed(1) + "%" : '<span class="dim">—</span>'}</td>
      <td class="r">${fmtPct(r.premium, 3)}</td>
      <td class="r h" style="${heat(r.basis, bmax)}">${fmtPct(r.basis, 3)}</td>
      <td class="r">${left != null ? `${Math.floor(left / 3600)}:${String(Math.floor((left % 3600) / 60)).padStart(2, "0")}` : "—"}</td></tr>`;
  }).join("") || '<tr><td colspan="8" class="empty">Ждём фандинг: опрос раз в 30 с</td></tr>';
}

function renderLiq(l) {
  if (!l) { $("liqChart").innerHTML = ""; $("liqFeed").innerHTML = '<li class="empty">Модуль ликвидаций выключен</li>'; return; }
  const t = l.totals || {};
  const p = (w) => t[w] ? `${winRu(w)}: лонги ${fmtUsd(t[w][0])} / шорты ${fmtUsd(t[w][1])}` : "";
  $("liqHint").textContent = [p("1m"), p("5m")].filter(Boolean).join(" · ")
    + (l.norm_1m != null ? ` · 1м = ${(l.norm_1m * 100).toFixed(1)}% depth_1% фьюч.` : "");
  const el = $("liqChart");
  const hist = l.hist || [];
  if (!hist.length) {
    el.innerHTML = '<div class="empty muted">Ликвидаций пока не было. Поток есть у Binance, Bybit, BitMEX и Lighter</div>';
  } else {
    const W = Math.max(300, el.clientWidth - 16), H = 110, mid = H / 2 - 4;
    const max = Math.max(1, ...hist.flatMap((h) => [h[1], h[2]]));
    const bw = W / hist.length;
    let bars = `<line x1="0" x2="${W}" y1="${mid}" y2="${mid}" stroke="#2a323d"/>`;
    hist.forEach((h, i) => {
      const hl = (h[1] / max) * (mid - 4), hs = (h[2] / max) * (mid - 4);
      bars += `<g><title>${fmtTime(h[0])}: лонги ${fmtUsd(h[1])}, шорты ${fmtUsd(h[2])}</title>`
        + `<rect x="${i * bw}" y="0" width="${bw}" height="${H}" fill="transparent"/>`
        + (hl ? `<rect x="${i * bw + 0.5}" y="${mid - hl}" width="${Math.max(1, bw - 1)}" height="${hl}" fill="var(--sell)"/>` : "")
        + (hs ? `<rect x="${i * bw + 0.5}" y="${mid}" width="${Math.max(1, bw - 1)}" height="${hs}" fill="var(--buy)"/>` : "") + "</g>";
    });
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Ликвидации по минутам за час">${bars}
      <text x="2" y="10">лонги ↑</text><text x="2" y="${H - 2}">шорты ↓</text><text x="${W - 2}" y="${H - 2}" text-anchor="end">60 мин</text></svg>`;
  }
  $("liqFeed").innerHTML = (l.feed || []).map((e) =>
    `<li class="${e.big ? "big" : ""}"><span class="t">${fmtTime(e.ts)}</span><span>${esc(e.venue)}</span>
      <span class="${e.side === "long" ? "neg" : "pos"}">${e.side === "long" ? "лонг" : "шорт"}</span>
      <span class="num">${fmtUsd(e.usd)}</span><span class="muted num">${e.price ? fmtPrice(e.price) : ""}</span>${e.big ? ' <span class="warn">крупная</span>' : ""}</li>`).join("")
    || '<li class="empty">Лента ликвидаций пуста</li>';
}
// ---------- М5: order books ----------
const fmtUsdOr = (v) => (v == null ? '<span class="dim">—</span>' : fmtUsd(v));
function renderBook(b) {
  if (!b) {
    $("bookChart").innerHTML = '<div class="empty muted">Модуль М5 выключен</div>';
    $("ctmBody").innerHTML = ""; $("ctmVenues").innerHTML = "";
    $("defendedList").innerHTML = ""; $("spoofList").innerHTML = ""; $("bookEvents").innerHTML = "";
    return;
  }
  const a = b.all;
  $("bookHint").textContent = a ? `сводный стакан ${a.venues} бирж · корзины по 0.1% до ±5%` : "ждём стаканы";
  renderProfile(b.profile);
  const row = (name, c, norm, cls = "") => {
    if (!c) return "";
    const lowUp = norm && norm.up && c.up["2.0"] < norm.up.p10;
    const lowDn = norm && norm.down && c.down["2.0"] < norm.down.p10;
    return `<tr class="${cls}"><td>${name}</td>${["1.0", "2.0", "5.0"].map((x) => `<td class="r${x === "2.0" && lowUp ? " warn" : ""}">${fmtUsdOr(c.up[x])}</td>`).join("")}`
      + `${["1.0", "2.0", "5.0"].map((x) => `<td class="r${x === "2.0" && lowDn ? " warn" : ""}">${fmtUsdOr(c.down[x])}</td>`).join("")}</tr>`;
  };
  const n = b.norm || {};
  const normRow = n.up || n.down
    ? `<tr><td class="muted">норма 2% (p10 / p50)</td><td></td><td class="r muted">${n.up ? `${fmtUsd(n.up.p10)} / ${fmtUsd(n.up.p50)}` : "—"}</td><td></td><td></td>`
      + `<td class="r muted">${n.down ? `${fmtUsd(n.down.p10)} / ${fmtUsd(n.down.p50)}` : "—"}</td><td></td></tr>` : "";
  $("ctmBody").innerHTML = (row("Все биржи (сводный)", b.all, b.norm, "agg") + row("Спот", b.spot) + row("Фьючерсы", b.perp) + normRow)
    || '<tr><td colspan="7" class="empty">Ждём стаканы бирж</td></tr>';
  $("ctmVenues").innerHTML = (b.venues || []).map((v) => {
    const [venue, kind] = splitKey(v.key);
    return `<tr><td class="venue">${esc(venue)}${kindBadge(kind)}</td><td class="r">${fmtUsd(v.up["1.0"])}</td><td class="r">${fmtUsd(v.up["2.0"])}</td>`
      + `<td class="r">${fmtUsd(v.down["1.0"])}</td><td class="r">${fmtUsd(v.down["2.0"])}</td><td class="r">${v.spread_bps.toFixed(1)}</td>`
      + `<td class="r">+${v.reach_up.toFixed(1)}% / −${v.reach_down.toFixed(1)}%</td></tr>`;
  }).join("");
  const SIDE = { bid: "покупку", ask: "продажу" };
  $("defendedList").innerHTML = (b.defended || []).length
    ? `<p class="defended"><b>Защищаемые уровни</b> (айсберги и быстрые восстановления на одной цене):<br>` + b.defended.map((d) => {
      const [venue, kind] = splitKey(d.key);
      return `${esc(venue)} ${kind}: на ${SIDE[d.side]} ${fmtPrice(d.price)} — ${d.count}× (айсбергов ${d.icebergs}, восстановлений ${d.recoveries})`;
    }).join("<br>") + "</p>"
    : '<p class="defended muted">Защищаемых уровней нет</p>';
  const sp = Object.entries(b.spoofs || {}).sort((x, y) => (y[1].bid + y[1].ask) - (x[1].bid + x[1].ask));
  $("spoofList").innerHTML = sp.length
    ? `<p class="spoofs"><b>Исчезающие крупные заявки за час</b> (сняли без исполнения у цены):<br>` + sp.slice(0, 8).map(([key, c]) => {
      const [venue, kind] = splitKey(key);
      return `${esc(venue)} ${kind}: на покупку ${c.bid} (${fmtUsd(c.usd_bid)}), на продажу ${c.ask} (${fmtUsd(c.usd_ask)})`;
    }).join("<br>") + "</p>"
    : '<p class="spoofs muted">Исчезающих заявок за час нет</p>';
  const KIND = { iceberg: "айсберг", recovery: "уровень восстановили", spoof: "заявку сняли" };
  $("bookEvHint").textContent = (b.events || []).length ? `за 30 мин: ${b.events.length}` : "";
  $("bookEvents").innerHTML = (b.events || []).map((e) => {
    const [venue, kind] = splitKey(e.key);
    const extra = e.kind === "iceberg" ? ` · исполнено ${fmtUsd(e.usd)} при видимых ${fmtUsd(e.visible)}`
      : e.kind === "recovery" ? ` · вернули за ${e.seconds} с` : ` · ${e.dist_pct}% от цены`;
    return `<li><span class="t">${fmtTime(e.ts)}</span><span>${esc(venue)} ${kind}</span><span class="${e.side === "bid" ? "pos" : "neg"}">${KIND[e.kind]} на ${SIDE[e.side]}</span>`
      + `<span class="num">${fmtPrice(e.price)}</span><span class="muted">${e.kind === "iceberg" ? "" : fmtUsd(e.usd)}${extra}</span></li>`;
  }).join("") || '<li class="empty">Событий пока нет: айсберги, быстро восстановленные уровни и исчезающие заявки появятся здесь</li>';
}

// Depth profile of the aggregated book: bids left of the price, asks right, 0.1 % buckets up to 5 %.
function renderProfile(p) {
  const el = $("bookChart");
  if (!p) { el.innerHTML = '<div class="empty muted">Ждём стаканы бирж</div>'; return; }
  const n = p.bid.length;
  const W = Math.max(320, el.clientWidth - 16), H = 170, L = 56, R = W - 10, T = 10, B = 22;
  const mid = (L + R) / 2, half = (R - L) / 2, bw = half / n;
  const max = Math.max(1, ...p.bid, ...p.ask);
  const y = (v) => T + (1 - v / max) * (H - T - B);
  let g = "";
  for (const v of niceTicks(0, max, 3)) {
    g += `<line class="grid" x1="${L}" x2="${R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${fmtUsd(v)}</text>`;
  }
  const bars = [];
  const gapRuns = [];
  for (const side of ["bid", "ask"]) {
    const vals = p[side], gaps = p[`gaps_${side}`], seen = p[`seen_${side}`];
    let run = null;
    for (let i = 0; i < n; i++) {
      const x0 = side === "bid" ? mid - (i + 1) * bw : mid + i * bw;
      const lo = (i * p.bucket_pct).toFixed(1), hi = ((i + 1) * p.bucket_pct).toFixed(1);
      const where = side === "bid" ? `−${hi}…−${lo}%` : `+${lo}…+${hi}%`;
      if (!seen[i]) {
        bars.push(`<line class="nodata" x1="${x0}" x2="${x0 + bw}" y1="${y(0)}" y2="${y(0)}"/>`);
        continue;
      }
      const h = Math.max(0, y(0) - y(vals[i]));
      const title = `${where}: ${side === "bid" ? "биды" : "аски"} ${fmtUsd(vals[i])}${gaps[i] ? " — пусто (меньше 20% медианы корзин)" : ""}`;
      bars.push(`<g><title>${title}</title><rect x="${x0}" y="${T}" width="${bw}" height="${H - T - B}" fill="transparent"/>`
        + (h ? `<rect class="${side}" x="${x0 + 0.5}" y="${y(vals[i])}" width="${Math.max(0.5, bw - 1)}" height="${h}" rx="${Math.min(2, bw / 3)}"/>` : "") + "</g>");
      if (gaps[i]) {
        if (!run) run = { side, from: i, to: i };
        else run.to = i;
      } else if (run) { gapRuns.push(run); run = null; }
    }
    if (run) gapRuns.push(run);
  }
  const gapsSvg = gapRuns.map((r) => {
    const a = r.side === "bid" ? mid - (r.to + 1) * bw : mid + r.from * bw;
    const w = (r.to - r.from + 1) * bw;
    const from = (r.from * p.bucket_pct).toFixed(1), to = ((r.to + 1) * p.bucket_pct).toFixed(1);
    return `<g><title>Пусто ${r.side === "bid" ? "снизу" : "сверху"}: ${from}–${to}%</title><rect class="gapmark" x="${a}" y="${T + 2}" width="${w}" height="${H - T - B - 2}"/>`
      + (w >= 26 ? `<text class="gaplbl" x="${a + w / 2}" y="${T + 12}" text-anchor="middle">пусто</text>` : "") + "</g>";
  }).join("");
  let ax = `<line class="zero" x1="${mid}" x2="${mid}" y1="${T}" y2="${H - B}"/>`;
  for (const pct of [-5, -2, -1, 1, 2, 5]) {
    const x = mid + (pct / (n * p.bucket_pct)) * half;
    ax += `<text x="${x}" y="${H - 6}" text-anchor="middle">${pct > 0 ? "+" : "−"}${Math.abs(pct)}%</text>`;
  }
  ax += `<text x="${mid}" y="${H - 6}" text-anchor="middle">цена</text>`
    + `<text x="${L + 4}" y="${T + 9}" class="ptitle">биды ↓</text><text x="${R - 4}" y="${T + 9}" class="ptitle" text-anchor="end">аски ↑</text>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Профиль глубины сводного стакана">${g}${bars.join("")}${gapsSvg}${ax}</svg>`;
}

// ---------- М6: borrowing ----------
function renderBorrow(b) {
  if (!b) { $("borrowBody").innerHTML = '<tr><td colspan="11" class="empty">Модуль М6 выключен</td></tr>'; return; }
  const st = Object.entries(b.status || {}).filter(([, v]) => v !== "ok").map(([k, v]) => `${k}: ${v}`);
  $("borrowHint").textContent = `Binance: ${b.key}`;
  const rows = b.rows.map((r) => {
    const chg = (v) => fmtPct(v, 0);
    const hot = r.rate_ratio != null && r.rate_ratio >= 3;
    return `<tr><td>${esc(r.venue)} <span class="zb">${r.kind}</span></td>
      <td class="r">${r.available != null ? fmtQty(r.available) : '<span class="dim">—</span>'}</td>
      <td class="r">${fmtUsdOr(r.available_usd)}</td>
      <td class="r">${r.share_day_volume != null ? (r.share_day_volume * 100).toFixed(0) + "%" : '<span class="dim">—</span>'}</td>
      <td class="r">${chg(r.chg_1h)}</td><td class="r">${chg(r.chg_4h)}</td><td class="r">${chg(r.chg_24h)}</td>
      <td class="r">${r.rate_apr != null ? r.rate_apr.toFixed(2) + "%" : '<span class="dim">—</span>'}</td>
      <td class="r ${hot ? "warn" : ""}">${r.rate_ratio != null ? "×" + r.rate_ratio.toFixed(1) : '<span class="dim">—</span>'}</td>
      <td class="r">${r.utilization != null ? (r.utilization * 100).toFixed(0) + "%" : ""}</td>
      <td class="why">${esc(r.note || "")}${r.quota != null ? ` · лимит на пользователя ${fmtQty(r.quota)}` : ""}${r.market_rate_apr != null ? ` · кредитный рынок ${r.market_rate_apr.toFixed(2)}% год.` : ""}</td></tr>`;
  }).join("");
  $("borrowBody").innerHTML = rows + (st.length ? `<tr><td colspan="11" class="why muted">${esc(st.join(" · "))}</td></tr>` : "")
    || '<tr><td colspan="11" class="empty">Ждём данные займов (опрос раз в минуту)</td></tr>';
}

// ---------- М7: index constituents ----------
function renderIndex(ix) {
  if (!ix) { $("indexBody").innerHTML = '<tr><td colspan="7" class="empty">Модуль М7 выключен</td></tr>'; $("indexChart").innerHTML = ""; return; }
  const baskets = ix.baskets || [];
  if (!baskets.some((b) => b.source === A.indexSrc)) A.indexSrc = baskets[0] && baskets[0].source;
  $("indexSrc").innerHTML = baskets.map((b) => `<button class="${b.source === A.indexSrc ? "active" : ""}" data-src="${b.source}">${b.source}</button>`).join("");
  const b = baskets.find((x) => x.source === A.indexSrc);
  if (!b) {
    const st = Object.entries(ix.status || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
    $("indexHint").textContent = "";
    $("indexBody").innerHTML = `<tr><td colspan="7" class="empty">Состав индекса ещё не загружен${st ? ` (${esc(st)})` : ""}. Нужен перп монеты на Binance, OKX или Bybit</td></tr>`;
    $("indexChart").innerHTML = "";
    return;
  }
  const err = b.model_error_pct;
  $("indexHint").textContent = `индекс ${b.name}: наш ${b.index_model ? fmtPrice(b.index_model) : "—"}, биржи ${b.index_exchange ? fmtPrice(b.index_exchange) : "—"}`
    + (err != null ? ` · расхождение ${err > 0 ? "+" : ""}${err.toFixed(3)}%${Math.abs(err) > 0.2 ? " ⚠ модель неточна" : ""}` : "")
    + (b.equal_weights ? " · биржа не дала веса: считаем поровну" : "");
  const depths = b.rows.map((r) => r.ctm1).filter((v) => v).sort((x, y) => x - y);
  const p10 = depths.length >= 3 ? depths[Math.floor(depths.length * 0.1)] : depths[0];
  $("indexBody").innerHTML = b.rows.map((r) => `<tr class="${r.ctm1 && p10 && r.ctm1 <= p10 ? "low" : ""}">
      <td class="venue">${esc(r.venue)}${r.live ? "" : ' <span class="zb" title="Этой биржи нет в наших потоках: цена из ответа индекса, обновляется редко">API</span>'}</td>
      <td class="r">${(r.weight * 100).toFixed(1)}%</td><td class="r">${r.price ? fmtPrice(r.price) : "—"}</td>
      <td class="r h" style="${heat(r.dev, 0.6)}">${fmtPct(r.dev, 3)}</td>
      <td class="r">${r.persist ? `<span class="${r.persist >= 10 ? "warn" : ""}">${Math.round(r.persist)} с</span>` : ""}</td>
      <td class="r lowcell">${fmtUsdOr(r.ctm1)}</td><td class="r">${fmtNum(r.influence, 3)}</td></tr>`).join("");
  renderIndexChart(b);
}
$("indexSrc").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-src]");
  if (btn) { A.indexSrc = btn.dataset.src; if (A.snap) renderIndex(A.snap.index); }
});

// Deviation of each constituent over 30 min: the three largest in colour, the rest as grey context.
function renderIndexChart(b) {
  const el = $("indexChart");
  const hist = b.dev_hist || {};
  const names = Object.keys(hist).filter((k) => hist[k].length >= 2);
  if (!names.length) { el.innerHTML = '<div class="empty muted">Отклонения появятся через несколько секунд</div>'; return; }
  const lastAbs = (k) => Math.abs(hist[k][hist[k].length - 1][1]);
  names.sort((x, y) => lastAbs(y) - lastAbs(x));
  const top = names.slice(0, 3);
  const all = names.flatMap((k) => hist[k]);
  const t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
  const thr = Number((A.modules && A.modules.data.index && A.modules.data.index.dev_alert_pct) || 0.3);
  const ext = Math.max(thr * 1.5, ...all.map((p) => Math.abs(p[1])));
  const W = Math.max(300, el.clientWidth - 16), H = 190, L = 50, R = W - 70, T = 8, B = 20;
  const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (R - L);
  const y = (v) => T + (1 - (v + ext) / (2 * ext)) * (H - T - B);
  let g = "";
  for (const v of niceTicks(-ext, ext, 4)) {
    g += `<line class="${Math.abs(v) < 1e-12 ? "zero" : "grid"}" x1="${L}" x2="${R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${v > 0 ? "+" : ""}${v.toFixed(2)}%</text>`;
  }
  g += `<line class="thr" x1="${L}" x2="${R}" y1="${y(thr)}" y2="${y(thr)}"/><line class="thr" x1="${L}" x2="${R}" y1="${y(-thr)}" y2="${y(-thr)}"/>`;
  for (const t of timeTicks(t0, t1)) g += `<text x="${x(t)}" y="${H - 5}" text-anchor="middle">${hhmm(t)}</text>`;
  const path = (pts) => pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const ctx = names.filter((k) => !top.includes(k)).map((k) => `<path class="s-ctx" d="${path(hist[k])}"><title>${esc(k)}</title></path>`).join("");
  // direct labels at the line ends, pushed apart so they never overlap
  const labels = top.map((k, i) => ({ k, i, v: hist[k][hist[k].length - 1][1], y: y(hist[k][hist[k].length - 1][1]) + 3 }))
    .sort((a, b2) => a.y - b2.y);
  for (let j = 1; j < labels.length; j++) labels[j].y = Math.max(labels[j].y, labels[j - 1].y + 12);
  const lines = top.map((k, i) => `<path class="s${i + 1}" d="${path(hist[k])}"/>`).join("")
    + labels.map((l) => `<text class="lbl" x="${R + 4}" y="${l.y}">${esc(l.k)} ${l.v > 0 ? "+" : ""}${l.v.toFixed(2)}%</text>`).join("");
  const legend = top.map((k, i) => `<i class="lg s${i + 1}"></i>${esc(k)}`).join(" ") + (ctx ? ' <i class="lg ctx"></i>остальные' : "");
  el.innerHTML = `<div class="legend small">${legend} · пунктир — порог ±${thr}%</div>
    <svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Отклонение составляющих индекса от остальных за 30 минут">${g}${ctx}${lines}</svg>`;
}

window.addEventListener("resize", () => { if (A.view === "analysis" && A.snap) renderAnalysis(); });

// ---------- journal (М15) ----------
function fmtMove(v) { return fmtPct(v, 2); }
async function loadJournal() {
  const coin = $("jCoin").value === "cur" ? S.coin : "";
  const days = $("jDays").value;
  const type = $("jType").value;
  const q = `coin=${encodeURIComponent(coin)}&days=${days}`;
  try {
    const [rep, rows] = await Promise.all([
      api(`/api/journal/report?${q}`),
      api(`/api/journal?${q}&limit=300${type ? `&type=${encodeURIComponent(type)}` : ""}`),
    ]);
    renderReport(rep);
    renderJournal(rows);
  } catch (err) {
    toast(err.message);
  }
}
function renderReport(rep) {
  const sel = $("jType");
  const cur = sel.value;
  sel.innerHTML = '<option value="">все сигналы</option>' + rep.types.map((t) => `<option value="${esc(t.type)}">${esc(t.title)}</option>`).join("");
  sel.value = rep.types.some((t) => t.type === cur) ? cur : "";
  $("reportBody").innerHTML = rep.types.map((t) => {
    const hmax = Math.max(1, ...t.hist_r15);
    const hist = t.hist_r15.map((n, i) => `<i class="${i < 4 ? "neg" : "pos"}" style="height:${Math.max(1, (n / hmax) * 18)}px" title="${["< −2%", "−2…−1%", "−1…−0.5%", "−0.5…0%", "0…0.5%", "0.5…1%", "1…2%", "> 2%"][i]}: ${n}"></i>`).join("");
    const dir = t.direction > 0 ? " ↑" : t.direction < 0 ? " ↓" : "";
    return `<tr><td>${esc(t.module)} · <b>${esc(t.title)}</b>${dir}${t.low_history ? ` <span class="zb" title="Из них при малой истории">мало ист. ${t.low_history}</span>` : ""}</td>
      <td class="r">${t.count}</td>${[1, 5, 15, 60].map((h) => `<td class="r">${fmtMove(t[`avg_r${h}`])}</td>`).join("")}
      <td class="r">${t.hit_rate != null ? (t.hit_rate * 100).toFixed(0) + "%" : '<span class="dim">—</span>'} <span class="zb">из ${t.measured}</span></td>
      <td class="r">${fmtMove(t.mfe)}</td><td class="r">${fmtMove(t.mae)}</td><td><span class="hist">${hist}</span></td></tr>`;
  }).join("") || '<tr><td colspan="10" class="empty">Сигналов за период нет. Сюда попадает каждое срабатывание: и алерты ленты, и сигналы модулей (даже в режиме «только сбор данных»)</td></tr>';
}
function renderJournal(rows) {
  $("jHint").textContent = rows.length ? `${rows.length} последних` : "";
  $("journalBody").innerHTML = rows.map((r) => {
    const why = (r.data && r.data.reasons) || [];
    const flags = (r.collect_only ? ' <span class="zb">сбор данных</span>' : "") + (r.low_history ? ' <span class="zb">мало истории</span>' : "");
    return `<tr><td class="num">${new Date(r.ts * 1000).toLocaleString("ru-RU", { hour12: false })}</td><td>${esc(r.coin)}</td>
      <td>${esc(r.module)}</td><td><b>${esc(r.title)}</b>${r.direction > 0 ? " ↑" : r.direction < 0 ? " ↓" : ""}${flags}</td>
      <td class="r">${r.price ? fmtPrice(r.price) : "—"}</td>
      ${["r1", "r5", "r15", "r60", "up", "down"].map((c) => `<td class="r">${fmtMove(r[c])}</td>`).join("")}
      <td class="why">${esc(why.slice(0, 4).join(" · "))}</td></tr>`;
  }).join("") || '<tr><td colspan="12" class="empty">Журнал пуст</td></tr>';
}
["jCoin", "jDays", "jType"].forEach((id) => $(id).addEventListener("change", loadJournal));

// ---------- health ----------
const ST_RU = { live: "онлайн", polling: "опрос", connecting: "подключение", init: "ожидание", error: "ошибка", na: "нет пары", noapi: "нет публичного API" };
async function loadHealth() {
  try {
    renderHealth(await api("/api/health"));
  } catch (err) {
    $("healthCards").innerHTML = `<div class="card bad"><h4>Сервер</h4><p>${esc(err.message)}</p></div>`;
  }
}
function healthWarnings(h) {
  const w = [];
  if (h.clock && h.clock.warn) w.push("часы");
  if (h.config && h.config.errors && h.config.errors.length) w.push("конфиг");
  if (h.recorder && h.recorder.error) w.push("запись");
  return w;
}
function renderHealth(h) {
  const warn = healthWarnings(h);
  $("healthWarn").textContent = warn.length ? "!" : "";
  const c = h.clock || {};
  const r = h.recorder || {};
  const errs = (h.config && h.config.errors) || [];
  const live = h.streams.filter((s) => s.status === "live" || s.status === "polling").length;
  $("healthCards").innerHTML = `
    <div class="card"><h4>Потоки ${esc(h.coin)}</h4><div class="big">${live} / ${h.streams.length}</div><p>онлайн / всего. Фьючерсных данных: ${h.feeds.filter((f) => f.oi === "ok").length} бирж с ОИ, ${h.feeds.filter((f) => f.funding === "ok").length} с фандингом</p></div>
    <div class="card ${c.warn ? "bad" : ""}"><h4>Часы компьютера</h4><div class="big">${c.offset_ms != null ? `${c.offset_ms > 0 ? "+" : ""}${c.offset_ms.toFixed(0)} мс` : "—"}</div>
      <p>${c.warn ? "⚠ Смещение больше 50 мс: включите синхронизацию времени в Windows (Параметры → Время → Синхронизировать)" : c.source ? `по ${esc(c.source)}, ${ago(c.checked_at)} назад` : esc(c.error || (h.demo ? "в демо-режиме не проверяется" : "проверка при запуске и раз в час"))}</p></div>
    <div class="card ${r.error ? "bad" : ""}"><h4>Запись сырых данных</h4><div class="big">${r.enabled ? (r.disk_gb != null ? r.disk_gb.toFixed(2) + " ГБ" : "вкл") : "выкл"}</div>
      <p>${r.enabled ? `записано ${Number(r.written).toLocaleString("ru-RU")} строк${r.dropped ? `, потеряно ${r.dropped}` : ""}` : "включается в настройках"}<br><span class="muted">${esc(r.dir || "")}</span>${r.error ? `<br><span class="warn">${esc(r.error)}</span>` : ""}</p></div>
    <div class="card ${h.borrow && /отклонён|нет права/.test(h.borrow.key) ? "bad" : ""}"><h4>Займы (М6) и индекс (М7)</h4>
      <div class="big">${h.books || 0} стаканов</div>
      <p>Binance-ключ: ${esc((h.borrow && h.borrow.key) || "—")}<br>${esc(Object.entries((h.borrow && h.borrow.status) || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "займы: ждём опроса")}
      <br>Индекс: ${esc(Object.entries((h.index && h.index.status) || {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "ждём перп монеты")}</p></div>
    <div class="card ${errs.length ? "bad" : ""}"><h4>Настройки модулей</h4><div class="big">${h.collect_only ? "только сбор" : "алерты вкл"}</div>
      <p>история норм: ${h.baseline_days.toFixed(1)} дн.<br><span class="muted">${esc(h.config.path)}</span>${errs.map((e) => `<br><span class="warn">${esc(e)}</span>`).join("")}</p></div>`;
  const rows = h.streams.filter((s) => A.healthAll || !["na", "noapi"].includes(s.status));
  rows.sort((a, b) => a.status.localeCompare(b.status) || a.key.localeCompare(b.key));
  $("healthBody").innerHTML = rows.map((s) => {
    const [venue, kind] = splitKey(s.key);
    const lat = s.latency_ms != null ? `${s.latency_ms} мс` : "—";
    return `<tr><td class="venue">${esc(venue)}${kindBadge(kind)}</td><td class="st-${s.status}">${ST_RU[s.status] || s.status}</td>
      <td>${s.transport === "ws" ? "WebSocket" : s.transport === "rest" ? "REST" : "—"}</td>
      <td class="r">${lat}</td><td class="r">${s.jitter_ms != null ? s.jitter_ms + " мс" : "—"}</td>
      <td class="r">${s.last_msg_age != null ? ago(Date.now() / 1000 - s.last_msg_age) : "—"}</td>
      <td class="r">${s.reconnects || ""}</td><td class="r">${s.gaps || ""}</td><td class="r">${s.backfilled || ""}</td>
      <td>${esc(s.book_status || "")}</td><td class="why">${esc(s.error || s.book_error || "")}</td></tr>`;
  }).join("");
  const okc = (v) => (v === "ok" ? '<span class="pos">есть</span>' : esc(v || "—"));
  $("feedsBody").innerHTML = h.feeds.map((f) => {
    const [venue] = splitKey(f.key);
    return `<tr><td>${esc(venue)}</td><td>${okc(f.oi)}</td><td class="r">${f.oi_age != null ? f.oi_age + " с" : ""}</td>
      <td>${okc(f.funding)}</td><td class="r">${f.funding_age != null ? f.funding_age + " с" : ""}</td><td>${okc(f.liq)}</td></tr>`;
  }).join("") || '<tr><td colspan="6" class="empty">Фьючерсы этой монеты пока не подключены</td></tr>';
}
$("healthAll").addEventListener("change", () => { A.healthAll = $("healthAll").checked; loadHealth(); });
// the header badge warns about the clock / config even when the screen is closed
setInterval(() => { if (A.view !== "health") api("/api/health").then((h) => { $("healthWarn").textContent = healthWarnings(h).length ? "!" : ""; }).catch(() => {}); }, 60000);

// ---------- module settings (in the settings dialog) ----------
const MODULE_FIELDS = [
  ["collect_only", "Только сбор данных (без алертов, для калибровки)"],
  ["modules.delta", "М1 — дельта спот против фьючерсов"],
  ["modules.open_interest", "М2 — открытый интерес по биржам"],
  ["modules.regime", "М3 — режим рынка"],
  ["modules.funding", "М4 — фандинг, премия, базис"],
  ["modules.liquidations", "М4 — ликвидации"],
  ["modules.orderbook", "М5 — стаканы: глубина, пустоты, айсберги"],
  ["modules.borrow", "М6 — займы для шортов"],
  ["modules.index", "М7 — составляющие индекса"],
  ["modules.journal", "М15 — журнал сигналов"],
  ["modules.recorder", "Запись сырых данных на диск"],
  ["alerts.telegram", "Алерты модулей в Telegram"],
];
const MODULE_NUMS = [
  ["alert_z", "Порог z-score сигналов"],
  ["alerts.cooldown_min", "Пауза алертов модуля, мин"],
  ["regime.price_deadzone_pct", "Режим: цена «на месте», %"],
  ["regime.oi_deadzone_pct", "Режим: ОИ «на месте», %"],
];
const getPath = (o, p) => p.split(".").reduce((x, k) => (x == null ? x : x[k]), o);
window.renderModulesSettings = async function () {
  try {
    A.modules = await api("/api/modules");
  } catch (err) {
    $("modulesGrid").innerHTML = `<p class="warn">${esc(err.message)}</p>`;
    return;
  }
  const d = A.modules.data;
  $("modulesGrid").innerHTML = MODULE_FIELDS.map(([p, label]) =>
    `<label class="check"><input type="checkbox" data-mod="${p}" ${getPath(d, p) ? "checked" : ""}> ${esc(label)}</label>`).join("")
    + MODULE_NUMS.map(([p, label]) => `<label>${esc(label)}<input type="number" step="any" data-mod="${p}" value="${getPath(d, p)}"></label>`).join("");
  $("modulesPath").textContent = `Все пороги, окна и веса — в файле ${A.modules.path} (перечитывается сам, без перезапуска).`
    + (A.modules.errors.length ? " Ошибки: " + A.modules.errors.join("; ") : "");
};
window.saveModulesSettings = async function () {
  if (!A.modules) return;
  const body = {};
  document.querySelectorAll("#modulesGrid [data-mod]").forEach((el) => {
    const p = el.dataset.mod;
    const v = el.type === "checkbox" ? el.checked : Number(el.value);
    if (v !== getPath(A.modules.data, p)) body[p] = v;
  });
  if (!Object.keys(body).length) return;
  A.modules = await api("/api/modules", { method: "PUT", body: JSON.stringify(body) });
};

setView(A.view);
