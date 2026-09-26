"use strict";

// ---------- small utils ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};
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
function fmtBps(v, cls = true) {
  if (v == null) return '<span class="dim">—</span>';
  const s = (v > 0 ? "+" : "") + v.toFixed(0);
  return cls ? `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${s}</span>` : s;
}
const fmtTime = (ts, ms = false) => {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString("ru-RU", { hour12: false });
  return ms ? `${t}.${String(d.getMilliseconds()).padStart(3, "0")}` : t;
};
const splitKey = (key) => { const i = key.lastIndexOf(":"); return [key.slice(0, i), key.slice(i + 1)]; };
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
  telegram: false,
  streams: [],
  selected: new Set(store.get("selected", [])),
  minUsd: store.get("minUsd", 3000),
  side: "all",
  paused: false,
  sound: store.get("sound", true),
  hideQuiet: store.get("hideQuiet", false),
  tape: [], // raw rows, newest first
  pending: [],
  ws: null,
};
const TAPE_KEEP = 2000;
const TAPE_DOM = 400;

// ---------- streams table ----------
function sparkSvg(spark) {
  const W = 92, H = 26, mid = H / 2, bw = 2, gap = 1;
  const max = Math.max(1, ...spark.map(([b, s]) => Math.max(b, s)));
  let bars = `<line x1="0" x2="${W}" y1="${mid}" y2="${mid}" stroke="#2a323d" stroke-width="1"/>`;
  spark.forEach(([b, s], i) => {
    const x = i * (bw + gap) + 1;
    const hb = b ? Math.max(1, (b / max) * (mid - 1)) : 0;
    const hs = s ? Math.max(1, (s / max) * (mid - 1)) : 0;
    const secs = (spark.length - i) * 10;
    bars += `<g><title>−${secs}с: покупки ${fmtUsd(b)}, продажи ${fmtUsd(s)}</title>`
      + `<rect x="${x - 0.5}" y="0" width="${bw + gap}" height="${H}" fill="transparent"/>`
      + (hb ? `<rect x="${x}" y="${mid - hb}" width="${bw}" height="${hb}" rx="0.5" fill="var(--buy)"/>` : "")
      + (hs ? `<rect x="${x}" y="${mid}" width="${bw}" height="${hs}" rx="0.5" fill="var(--sell)"/>` : "")
      + "</g>";
  });
  return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Объём за 5 минут">${bars}</svg>`;
}

function isActive(s) {
  return (s.status === "live" || s.status === "polling") && s.price != null;
}

function renderStreams() {
  const cfg = S.config;
  const active = S.streams.filter(isActive).filter((s) => !S.hideQuiet || s.vol_w > 0);
  active.sort((a, b) => (b.score - a.score) || (b.vol_w - a.vol_w));
  const rows = active.map((s) => {
    const [venue] = splitKey(s.key);
    const hot = s.score >= cfg.alert_score;
    const warm = !hot && s.score >= cfg.alert_score / 2;
    const cls = [hot ? "hot" : warm ? "warm" : "", S.selected.has(s.key) ? "selected" : ""].join(" ");
    const ratio = s.warming ? '<span class="warming" title="Копится история для базы">прогрев</span>'
      : s.ratio == null ? "—" : `<span class="${s.ratio >= cfg.spike_ratio ? "flag" : ""}">×${s.ratio >= 100 ? s.ratio.toFixed(0) : s.ratio.toFixed(1)}</span>`;
    const bs = s.buy_share;
    const buy = bs == null ? '<span class="dim">—</span>'
      : `<span class="buybar"><i style="width:${(bs * 100).toFixed(0)}%"></i></span>${(bs * 100).toFixed(0)}%`;
    const algo = s.algo
      ? `<span class="${s.algo.side === "buy" ? "pos" : "neg"}" title="${s.algo.count} сделок ≈${fmtUsd(s.algo.avg_usd)}${s.algo.regular ? `, шаг ~${s.algo.interval.toFixed(1)}с` : ""}">${s.algo.count}×${fmtUsd(s.algo.avg_usd)}${s.algo.regular ? " ⏱" : ""}</span>`
      : '<span class="dim">—</span>';
    const share = s.share == null ? "—" : (s.share * 100).toFixed(s.share < 0.1 ? 1 : 0) + "%";
    const stale = s.age != null && s.age > 60 ? ` <span class="dim" title="Последняя сделка ${Math.round(s.age)}с назад">·</span>` : "";
    let html = `<tr class="${cls}" data-key="${esc(s.key)}">
      <td class="venue">${esc(venue)}${kindBadge(s.kind)}</td>
      <td class="r">${fmtPrice(s.price)}${stale}</td>
      <td class="r">${fmtBps(s.ret_bps)}</td>
      <td class="r">${fmtBps(s.dev_bps)}</td>
      <td class="r">${fmtUsd(s.vol_w)}</td>
      <td class="r">${ratio}</td>
      <td class="r">${buy}</td>
      <td>${algo}</td>
      <td class="r">${share}</td>
      <td class="r score"><b>${s.score.toFixed(0)}</b></td>
      <td>${sparkSvg(s.spark || [])}</td>
    </tr>`;
    if ((hot || warm) && s.reasons && s.reasons.length) {
      html += `<tr class="reasons-row" data-key="${esc(s.key)}"><td colspan="11">${s.reasons.map((r) => `<span>${esc(r)}</span>`).join("")}</td></tr>`;
    }
    return html;
  });
  $("streamsBody").innerHTML = rows.join("") || `<tr><td colspan="11" class="empty">Подключаемся к биржам…</td></tr>`;

  const inactive = S.streams.filter((s) => !isActive(s));
  const label = { na: "нет пары", connecting: "подключение", init: "ожидание", error: "ошибка", live: "нет сделок", polling: "нет сделок" };
  $("inactiveSummary").textContent = `Не торгуется / подключение / ошибки (${inactive.length})`;
  $("inactiveList").innerHTML = inactive
    .sort((a, b) => a.status.localeCompare(b.status) || a.key.localeCompare(b.key))
    .map((s) => {
      const [venue] = splitKey(s.key);
      const err = s.error ? ` <span class="err" title="${esc(s.error)}">⚠</span>` : "";
      return `<div>${esc(venue)}${kindBadge(s.kind)} <span class="${s.status === "error" ? "err" : ""}">${label[s.status] || s.status}</span>${err}</div>`;
    }).join("");

  $("liveCount").textContent = `${active.length}/${S.streams.length}`;
  $("windowHint").textContent = `окно ${cfg.window_sec}с · база ${Math.round(cfg.baseline_sec / 60)} мин`;
}

$("streamsBody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-key]");
  if (!tr) return;
  toggleSelected(tr.dataset.key);
});

function toggleSelected(key, only = false) {
  if (only) {
    S.selected = new Set([key]);
  } else if (S.selected.has(key)) {
    S.selected.delete(key);
  } else {
    S.selected.add(key);
  }
  store.set("selected", [...S.selected]);
  renderChips();
  rebuildTape();
  renderStreams();
}

// ---------- tape ----------
function tapeVisible(r) {
  if (r.usd < S.minUsd) return false;
  if (S.side !== "all" && r.side !== S.side) return false;
  if (S.selected.size && !S.selected.has(r.key)) return false;
  return true;
}

function tapeRowHtml(r) {
  const [venue, kind] = splitKey(r.key);
  const pct = Math.min(100, (r.usd / (S.minUsd * 10 || 1)) * 100);
  const big = r.usd >= S.minUsd * 5 ? " big" : "";
  const fills = r.fills > 1 ? ` <span class="fills" title="Склеено исполнений одного ордера">×${r.fills}</span>` : "";
  return `<div class="tape-row ${r.side === "buy" ? "buy" : r.side === "sell" ? "sell" : ""}${big}">`
    + `<div class="bg" style="width:${pct.toFixed(1)}%"></div>`
    + `<span>${fmtTime(r.ts, true)}</span>`
    + `<span class="ven">${esc(venue)}${kindBadge(kind)}</span>`
    + `<span class="r">${fmtPrice(r.price)}</span>`
    + `<span class="r">${fmtQty(r.amount)}${fills}</span>`
    + `<span class="r usd">${fmtUsd(r.usd)}</span></div>`;
}

function addTrades(rows) {
  // rows arrive oldest -> newest
  for (const r of rows) S.tape.unshift(r);
  if (S.tape.length > TAPE_KEEP) S.tape.length = TAPE_KEEP;
  if (S.paused) return;
  const vis = rows.filter(tapeVisible);
  if (!vis.length) return;
  const list = $("tapeList");
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();
  list.insertAdjacentHTML("afterbegin", vis.reverse().map(tapeRowHtml).join(""));
  while (list.childElementCount > TAPE_DOM) list.lastElementChild.remove();
}

function rebuildTape() {
  const vis = S.tape.filter(tapeVisible).slice(0, TAPE_DOM);
  $("tapeList").innerHTML = vis.map(tapeRowHtml).join("")
    || `<div class="empty">Нет принтов от ${fmtUsd(S.minUsd)}${S.selected.size ? " на выбранных биржах" : ""}. Ждём…</div>`;
}

function renderChips() {
  $("tapeFilters").innerHTML = [...S.selected].map((k) => {
    const [v, kind] = splitKey(k);
    return `<button class="chip" data-key="${esc(k)}" title="Убрать фильтр">${esc(v)} ${kind}</button>`;
  }).join("");
}
$("tapeFilters").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (b) toggleSelected(b.dataset.key);
});

$("minUsd").value = S.minUsd;
$("minUsd").addEventListener("change", () => {
  S.minUsd = Math.max(0, Number($("minUsd").value) || 0);
  store.set("minUsd", S.minUsd);
  sendFilter();
  rebuildTape();
});
$("sideFilter").addEventListener("change", () => { S.side = $("sideFilter").value; rebuildTape(); });
$("pauseBtn").addEventListener("click", () => {
  S.paused = !S.paused;
  $("pauseBtn").textContent = S.paused ? "▶ Продолжить" : "⏸ Пауза";
  if (!S.paused) rebuildTape();
});
$("hideQuiet").checked = S.hideQuiet;
$("hideQuiet").addEventListener("change", () => { S.hideQuiet = $("hideQuiet").checked; store.set("hideQuiet", S.hideQuiet); renderStreams(); });

// ---------- alerts ----------
function alertHtml(a, fresh) {
  return `<li class="${fresh ? "fresh" : ""}" data-key="${esc(a.key)}">
    <div class="alert-top"><span class="t">${fmtTime(a.ts)}</span>
    <span class="v">${esc(a.venue)}</span>${kindBadge(a.kind)}
    <span class="muted">${esc(a.coin)}</span>
    <span class="s">${Math.round(a.score)}</span></div>
    <ul class="alert-reasons">${a.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></li>`;
}
function renderAlerts(list) {
  $("alertsList").innerHTML = list.map((a) => alertHtml(a, false)).join("")
    || '<li class="empty">Пока тихо. Алерт появится, когда скор биржи превысит порог.</li>';
}
function addAlert(a) {
  const list = $("alertsList");
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();
  list.insertAdjacentHTML("afterbegin", alertHtml(a, true));
  while (list.childElementCount > 200) list.lastElementChild.remove();
  beep();
}
$("alertsList").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-key]");
  if (li) toggleSelected(li.dataset.key, true);
});
$("clearAlerts").addEventListener("click", () => renderAlerts([]));
async function loadAlerts() {
  try { renderAlerts(await api(`/api/alerts?coin=${encodeURIComponent(S.coin)}&limit=100`)); } catch { /* ignore */ }
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
  if (coin === S.coin) return;
  S.coin = coin;
  $("coinLabel").textContent = coin;
  $("coinInput").placeholder = coin;
  document.title = `${coin} · Manipulation Radar`;
  S.tape = [];
  S.selected.clear();
  store.set("selected", []);
  renderChips();
  rebuildTape();
  loadAlerts();
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
];
function openSettings() {
  const c = S.config;
  $("settingsGrid").innerHTML = FIELDS.map(([k, label, hint]) =>
    `<label>${label}<input name="${k}" type="number" step="any" value="${c[k]}"><small>${hint}</small></label>`).join("")
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
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "filter", min_usd: S.minUsd }));
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
    } else if (msg.type === "alert") {
      if (msg.alert.coin === S.coin) addAlert(msg.alert);
    } else if (msg.type === "coin") {
      setCoin(msg.coin);
    }
  };
}

async function init() {
  renderSound();
  renderChips();
  try {
    const st = await api("/api/state");
    S.config = st.config;
    S.telegram = st.telegram;
    setCoin(st.coin);
  } catch (err) {
    toast("Сервер недоступен: " + err.message);
  }
  rebuildTape();
  connect();
}
init();
