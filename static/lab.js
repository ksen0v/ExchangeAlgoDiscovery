"use strict";
// Screen "Лаборатория": own model trained on the recorded history (see app/analytics/labmodel.py).
// Loaded before analysis.js; uses its A / fmtPct / ago at call time and the helpers of app.js.

const LAB_TARGETS = [["up", "Рост", "pos"], ["down", "Падение", "neg"]];
const LAB_VERDICT = {
  ok: ["Закономерность есть", "pos"], weak: ["Слабая", "warn"], none: ["Не найдено", "dim"], few: ["Мало данных", "warn"],
};
let labFormFilled = false;

const pct0 = (v) => (v == null || !isFinite(v) ? "—" : `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`);
function labVal(v) {
  if (v == null || !isFinite(v)) return "нет данных";
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString("ru-RU", { maximumFractionDigits: 0 });
  if (a >= 100) return v.toFixed(0);
  return Number(v.toPrecision(3)).toString();
}
function labMove(m) {
  return m ? `${m.move_pct}% за ${m.horizon_min} мин` : "";
}

async function loadLab(fillForm) {
  try {
    const d = await api("/api/lab");
    if (fillForm || !labFormFilled) fillLabForm(d);
    renderLab(d);
  } catch (err) {
    $("labCards").innerHTML = `<div class="card bad"><h4>Лаборатория</h4><p>${esc(err.message)}</p></div>`;
  }
}
window.loadLab = loadLab;

function fillLabForm(d) {
  const f = $("labForm");
  const m = d.model || {};
  f.move_pct.value = m.move_pct || d.settings.move_pct;
  f.horizon_min.value = m.horizon_min || d.settings.horizon_min;
  f.days.value = d.settings.train_days;
  f.scope.value = m.scope || d.settings.scope;
  labFormFilled = true;
}

function renderLab(d) {
  const st = d.status || {};
  const cov = d.coverage || {};
  const m = d.model;
  const running = st.state === "running";
  $("labTrain").disabled = running || !d.enabled;
  $("labProgress").hidden = !running;
  $("labProgress").firstElementChild.style.width = `${Math.round((st.progress || 0) * 100)}%`;
  $("labStatus").innerHTML = running ? `идёт обучение: ${esc(st.text || "")}`
    : st.state === "error" ? `<span class="warn">${esc(st.error)}</span>`
    : st.state === "done" ? `обучено ${ago(st.finished)} назад` : (d.enabled ? "" : "модуль выключен в настройках");

  const days = Math.max(cov.lab_days || 0, cov.metric_days || 0);
  const need = d.settings.min_days;
  const others = (cov.coins || []).filter((c) => c.coin !== d.coin && c.days >= 1);
  let cards = `<div class="card ${days < need ? "bad" : ""}"><h4>История ${esc(d.coin)}</h4>
    <div class="big">${days.toFixed(1)} дн.</div>
    <p>снимки показателей: ${(cov.metric_days || 0).toFixed(1)} дн., полный снимок Лаборатории: ${(cov.lab_days || 0).toFixed(1)} дн.,
    сырые сделки: ${cov.raw_days || 0} дн.${days < need ? `<br>Нужно от ${need} дн. — программа копит историю сама, пока работает` : ""}
    ${others.length ? `<br>Другие монеты: ${others.slice(0, 6).map((c) => `${esc(c.coin)} ${c.days.toFixed(1)} дн.`).join(", ")}` : ""}</p></div>`;
  if (m) {
    cards += `<div class="card ${m.stale ? "bad" : ""}"><h4>Модель</h4><div class="big">${esc(labMove(m))}</div>
      <p>обучена ${ago(m.trained_at)} назад${m.auto ? " (сама)" : ""} за ${m.took_sec} с на ${m.days.toFixed(1)} дн.
      (${m.rows.toLocaleString("ru-RU")} минут${m.scope === "all" ? `, монеты: ${esc(m.coins.join(", "))}` : ""}),
      показателей: ${m.features.length}${d.settings.auto_retrain_hours > 0 ? `<br>переобучение раз в ${d.settings.auto_retrain_hours} ч` : ""}
      ${m.stale ? `<br><span class="warn">${esc(m.stale)}</span>` : ""}</p></div>`;
    for (const [t, name, cls] of LAB_TARGETS) {
      const r = m.targets[t];
      const lv = (d.live && d.live.targets[t]) || null;
      const [vtxt, vcls] = LAB_VERDICT[r.verdict] || ["", ""];
      const hot = lv && lv.thr && lv.p >= lv.thr;
      cards += `<div class="card ${hot ? "lab-hot" : ""}"><h4>${name} сейчас</h4>
        <div class="big ${hot ? cls : ""}">${lv ? pct0(lv.p) : "—"}</div>
        <p>обычно ${pct0(r.base)}${lv && lv.thr ? ` · порог сигнала ${pct0(lv.thr)}` : ""}<br>проверка: <span class="${vcls}">${vtxt}</span></p></div>`;
    }
  } else {
    cards += `<div class="card"><h4>Модель</h4><div class="big">нет</div><p>${days >= need
      ? "История есть — нажмите «Обучить»" : "Появится, когда наберётся история"}${d.demo ? "<br>В демо-режиме цены случайные: закономерностей не будет" : ""}</p></div>`;
  }
  $("labCards").innerHTML = cards;

  renderLabLive(d);
  renderLabCheck(m);
  renderLabImportance(m);
  renderLabRelations(m);
}

function renderLabLive(d) {
  const m = d.model;
  const live = d.live;
  $("labLiveHint").textContent = live ? `обновлён ${ago(live.ts)} назад, раз в минуту` : "";
  if (!m) {
    $("labLive").innerHTML = '<p class="pad muted">Прогноза нет: модель ещё не обучена.</p>';
    return;
  }
  if (!live) {
    $("labLive").innerHTML = '<p class="pad muted">Прогноз появится в начале следующей минуты.</p>';
    return;
  }
  $("labLive").innerHTML = LAB_TARGETS.map(([t, name, cls]) => {
    const lv = live.targets[t];
    if (!lv) return "";
    const scale = Math.max(lv.p, lv.thr || 0, lv.base) * 1.15 || 1;
    const mark = (v, c, title) => (v == null ? "" : `<b class="${c}" style="left:${(v / scale) * 100}%" title="${title}"></b>`);
    const maxEff = Math.max(...lv.reasons.map((r) => Math.abs(r.effect)), 1e-9);
    const word = t === "up" ? "рост" : "падение";
    return `<div class="lab-live-row">
      <div class="lab-live-head"><b>${name} на ${live.move_pct}% за ${live.horizon_min} мин</b>
        <span class="${lv.thr && lv.p >= lv.thr ? cls : ""}">${pct0(lv.p)}</span>
        <span class="muted small">обычно ${pct0(lv.base)}${lv.thr ? `, сигнал от ${pct0(lv.thr)}` : ""}${lv.verdict !== "ok" ? " · сигналы выключены: проверка не пройдена" : ""}</span></div>
      <div class="lab-meter"><i class="${cls}" style="width:${Math.min(100, (lv.p / scale) * 100)}%"></i>${mark(lv.base, "base", "обычно")}${mark(lv.thr, "thr", "порог сигнала")}</div>
      ${lv.hist_rate != null ? `<p class="small muted">На проверке при таких прогнозах ${word} случался в ${pct0(lv.hist_rate)} случаев.</p>` : ""}
      <table class="grid-table lab-why"><tbody>${lv.reasons.map((r) => `<tr><td>${esc(r.ru)}</td><td class="r num">${esc(labVal(r.value))}</td>
        <td class="lab-eff">${dbar(r.effect, maxEff)}</td><td class="small ${r.effect > 0 ? cls : "dim"}">${r.effect > 0 ? `за ${word}` : `против`}</td></tr>`).join("")}</tbody></table>
    </div>`;
  }).join("");
}

function renderLabCheck(m) {
  if (!m) {
    $("labCheck").innerHTML = '<p class="pad muted">Здесь будет отчёт проверки: насколько модель угадывала на периодах, которых не видела.</p>';
    return;
  }
  const date = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  $("labCheck").innerHTML = LAB_TARGETS.map(([t, name, cls]) => {
    const r = m.targets[t];
    const [vtxt, vcls] = LAB_VERDICT[r.verdict] || ["", ""];
    const sig = r.signal ? r.signal.frac : null;
    const levels = (r.levels || []).map((x) => `<tr${x.frac === sig ? ' class="selected" title="этот уровень — порог сигналов"' : ""}>
      <td>${(x.frac * 100).toFixed(0)}% самых уверенных</td><td class="r">${pct0(x.thr)}</td><td class="r">${pct0(x.hit)}</td>
      <td class="r">${x.lift != null ? `×${x.lift.toFixed(1)}` : "—"}</td><td class="r">${x.episodes}</td>
      <td class="r">${pct0(x.episode_hit)}</td><td class="r">${fmtPct(x.episode_fwd)}</td></tr>`).join("");
    const cal = r.calib || [];
    const cmax = Math.max(...cal.map((c) => Math.max(c.rate, c.p)), 1e-9);
    const calib = cal.map((c, i) => `<span class="lab-cal" title="группа ${i + 1}: прогноз ${pct0(c.p)}, случалось ${pct0(c.rate)} (${c.n} мин)">
      <i class="p" style="height:${(c.p / cmax) * 40}px"></i><i class="${cls}" style="height:${(c.rate / cmax) * 40}px"></i></span>`).join("");
    const folds = (r.folds || []).map((f) => `<span class="zb" title="${f.rows} минут, движений ${f.pos}, деревьев ${f.trees}">${date(f.from)}–${date(f.to)}: AUC ${f.auc != null ? f.auc.toFixed(2) : "—"}</span>`).join(" ");
    return `<div class="lab-check-col">
      <h3>${name} на ${m.move_pct}% за ${m.horizon_min} мин · <span class="${vcls}">${vtxt}</span></h3>
      <p class="small">${esc(r.verdict_text)}</p>
      <p class="small muted">AUC ${r.auc != null ? r.auc.toFixed(3) : "—"} (0.5 — монетка, 1 — всегда угадывает) · на проверке ${r.test_rows.toLocaleString("ru-RU")} минут,
        движение случилось в ${pct0(r.test_base)} (${r.test_pos}) · деревьев в модели ${r.trees}</p>
      <div class="table-wrap"><table class="grid-table"><thead><tr><th>Минуты</th><th class="r" title="Прогноз модели от">Прогноз от</th>
        <th class="r" title="Как часто в эти минуты движение случалось">Сбылось</th><th class="r" title="Во сколько раз чаще обычного">Раз</th>
        <th class="r" title="Соседние минуты объединены в эпизоды: пауза дольше горизонта — новый эпизод">Эпизодов</th>
        <th class="r" title="Как часто сбывался первый сигнал эпизода — то, что получил бы трейдер, действуя по первому алерту">Первый сбылся</th>
        <th class="r" title="Средний ход цены через горизонт после первого сигнала эпизода">Ход цены</th></tr></thead>
        <tbody>${levels || '<tr><td colspan="7" class="empty">—</td></tr>'}</tbody></table></div>
      ${calib ? `<div class="lab-calib"><span class="small muted">Прогноз (серый) против того, как часто сбывалось (цвет), от самых неуверенных к самым уверенным:</span><div>${calib}</div></div>` : ""}
      ${folds ? `<p class="small muted">По периодам: ${folds}</p>` : ""}
    </div>`;
  }).join("");
}

function renderLabImportance(m) {
  if (!m) {
    $("labImportance").innerHTML = '<p class="pad muted">—</p>';
    return;
  }
  $("labImportance").innerHTML = LAB_TARGETS.map(([t, name, cls]) => {
    const imp = m.targets[t].importance || [];
    const max = Math.max(...imp.map((x) => x.share), 1e-9);
    return `<div><h3>${name}</h3>${imp.map((x) => `<div class="lab-bar"><span>${esc(x.ru)}</span>
      <i class="${cls}" style="width:${(x.share / max) * 100}%"></i><b>${(x.share * 100).toFixed(0)}%</b></div>`).join("") || '<p class="muted">—</p>'}</div>`;
  }).join("");
}

function labWhen(r) {
  const range = `${labVal(r.lo)} … ${labVal(r.hi)}`;
  if (r.q_hi >= 0.999) return `в верхних ${Math.round((1 - r.q_lo) * 100)}% (от ${labVal(r.lo)})`;
  if (r.q_lo <= 0.001) return `в нижних ${Math.round(r.q_hi * 100)}% (до ${labVal(r.hi)})`;
  return `между ${Math.round(r.q_lo * 100)}-м и ${Math.round(r.q_hi * 100)}-м перцентилем (${range})`;
}

function renderLabRelations(m) {
  const rel = (m && m.relations) || { up: [], down: [] };
  const rows = [];
  for (const [t, name, cls] of LAB_TARGETS) {
    for (const r of rel[t] || []) rows.push({ ...r, t, name, cls });
  }
  $("labRelHint").textContent = m ? `${labMove(m)}; выборка ${m.days.toFixed(1)} дн.` : "";
  $("labRelations").innerHTML = rows.map((r) => `<tr><td class="${r.cls}">${r.name}</td><td>${esc(r.ru)}</td><td>${esc(labWhen(r))}</td>
    <td class="r">${r.n.toLocaleString("ru-RU")}</td><td class="r">${pct0(r.rate)}</td><td class="r">${pct0(r.base)}</td>
    <td class="r"><b>×${r.lift.toFixed(1)}</b></td><td class="r">${fmtPct(r.fwd)}</td><td class="r">${r.z.toFixed(1)}</td>
    <td title="первые 3/4: ×${r.early != null ? r.early.toFixed(1) : "—"}, последняя 1/4: ×${r.late != null ? r.late.toFixed(1) : "—"}">${r.stable ? '<span class="pos">да</span>' : '<span class="dim">нет</span>'}
      <span class="small muted">×${r.early != null ? r.early.toFixed(1) : "—"} / ×${r.late != null ? r.late.toFixed(1) : "—"}</span></td></tr>`).join("")
    || `<tr><td colspan="10" class="empty">${m ? "Устойчивых взаимосвязей не найдено" : "Появятся после обучения"}</td></tr>`;
}

$("labForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { move_pct: Number(f.move_pct.value), horizon_min: Number(f.horizon_min.value), days: Number(f.days.value), scope: f.scope.value };
  try {
    await api("/api/lab/train", { method: "POST", body: JSON.stringify(body) });
    toast("Обучение запущено — это займёт от нескольких секунд до пары минут");
    loadLab();
    clearInterval(A.labTimer);
    A.labTimer = setInterval(loadLab, 2000);
  } catch (err) {
    toast(err.message);
  }
});
