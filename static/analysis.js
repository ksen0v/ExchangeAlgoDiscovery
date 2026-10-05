"use strict";
// Screens "Анализ рынка" (М1–М4), "Журнал сигналов" (М15), "Здоровье" (ТЗ 3.5) and the regime badge.
// Uses the helpers of app.js: $, esc, api, toast, fmtUsd, fmtPrice, fmtTime, splitKey, kindBadge, S, sendFilter.

const A = {
  view: store.get("view", "tape"),
  snap: null,
  venueWin: store.get("venueWin", "5m"),
  venueSort: store.get("venueSort", "abs"),
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
  renderCvd(s.cvd);
  renderDelta(s.delta);
  renderVenues(s.delta);
  renderOi(s.oi);
  renderFunding(s.funding);
  renderLiq(s.liq);
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

function renderCvd(points) {
  const el = $("cvdChart");
  if (!points || points.length < 2) {
    el.innerHTML = '<div class="empty muted">Копим данные: линия появится через минуту после выбора монеты</div>';
    return;
  }
  const W = Math.max(320, el.clientWidth - 16), H = 240, L = 58, R = 64, T = 10, B = 22;
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const xs = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const cvals = points.flatMap((p) => [p[1], p[2]]);
  let cmin = Math.min(0, ...cvals), cmax = Math.max(0, ...cvals);
  if (cmax - cmin < 1) { cmax += 1; cmin -= 1; }
  const ys = (v) => T + (1 - (v - cmin) / (cmax - cmin)) * (H - T - B);
  const prices = points.map((p) => p[3]).filter((p) => p);
  let pmin = Math.min(...prices), pmax = Math.max(...prices);
  if (!(pmax > pmin)) { pmax = pmax * 1.001 || 1; pmin = pmin * 0.999; }
  const yp = (v) => T + (1 - (v - pmin) / (pmax - pmin)) * (H - T - B);
  const line = (idx, f) => points.filter((p) => p[idx] != null).map((p, i) => `${i ? "L" : "M"}${xs(p[0]).toFixed(1)},${f(p[idx]).toFixed(1)}`).join("");
  let g = "";
  for (const v of niceTicks(cmin, cmax)) {
    g += `<line x1="${L}" x2="${W - R}" y1="${ys(v)}" y2="${ys(v)}" stroke="#2a323d" stroke-width="${v === 0 ? 1.2 : 0.6}"/>`
      + `<text x="${L - 6}" y="${ys(v) + 3}" text-anchor="end">${v < 0 ? "−" : ""}${fmtUsd(Math.abs(v))}</text>`;
  }
  for (const v of niceTicks(pmin, pmax, 3)) {
    g += `<text x="${W - R + 6}" y="${yp(v) + 3}">${fmtPrice(v)}</text>`;
  }
  const span = t1 - t0, stepT = span > 4 * 3600 ? 3600 : span > 2 * 3600 ? 1800 : span > 3600 ? 900 : span > 1200 ? 300 : 60;
  for (let t = Math.ceil(t0 / stepT) * stepT; t <= t1; t += stepT) {
    g += `<text x="${xs(t)}" y="${H - 6}" text-anchor="middle">${new Date(t * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</text>`;
  }
  const last = points[points.length - 1];
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="CVD спот и фьючерсы с ценой">${g}
    <path d="${line(3, yp)}" fill="none" stroke="#e6edf3" stroke-width="1" stroke-dasharray="3 3" opacity="0.7"/>
    <path d="${line(1, ys)}" fill="none" stroke="#4fd1d9" stroke-width="2"/>
    <path d="${line(2, ys)}" fill="none" stroke="#f0b90b" stroke-width="2"/>
    <text x="${W - R - 4}" y="${ys(last[1]) - 4}" text-anchor="end" style="fill:#4fd1d9">спот ${last[1] < 0 ? "−" : ""}${fmtUsd(Math.abs(last[1]))}</text>
    <text x="${W - R - 4}" y="${ys(last[2]) + 12}" text-anchor="end" style="fill:#f0b90b">фьюч. ${last[2] < 0 ? "−" : ""}${fmtUsd(Math.abs(last[2]))}</text>
  </svg>`;
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
window.addEventListener("resize", () => { if (A.view === "analysis" && A.snap) { renderCvd(A.snap.cvd); renderLiq(A.snap.liq); } });

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
