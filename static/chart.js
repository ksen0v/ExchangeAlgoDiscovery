"use strict";
// Small local canvas chart: no CDN, animation loop, framework or exchange requests.
class RadarChart {
  constructor(el, options = {}) {
    this.el = el;
    this.options = options;
    this.points = [];
    this.span = options.span || 3600;
    this.end = 0;
    this.follow = true;
    this.yZoom = 1;
    this.active = true;
    this.canvas = document.createElement("canvas");
    this.cross = document.createElement("canvas");
    this.cross.className = "chart-cross";
    this.tip = document.createElement("div");
    this.tip.className = "tip";
    this.tip.hidden = true;
    el.replaceChildren(this.canvas, this.cross, this.tip);
    this.cross.tabIndex = 0;
    this.cross.setAttribute("role", "img");
    this.cross.setAttribute("aria-label", options.label || "Интерактивный график");
    this.cross.title = "Тяни график — перемещение · колесо — масштаб · тяни правую шкалу — масштаб значений · двойной клик — к текущему времени";
    this.cross.addEventListener("pointerdown", e => this.down(e));
    this.cross.addEventListener("pointermove", e => this.move(e));
    this.cross.addEventListener("pointerup", e => this.up(e));
    this.cross.addEventListener("pointercancel", e => this.up(e));
    this.cross.addEventListener("pointerleave", () => { if (!this.drag) { this.pointer = null; this.request(false); } });
    this.cross.addEventListener("dblclick", () => this.live());
    this.cross.addEventListener("wheel", e => this.wheel(e), {passive: false});
    this.cross.addEventListener("keydown", e => {
      if (!["ArrowLeft", "ArrowRight", "+", "=", "-", "Home"].includes(e.key)) return;
      e.preventDefault();
      if (e.key === "Home") this.live();
      else if (e.key === "+" || e.key === "=" || e.key === "-") this.zoom(e.key === "-" ? 1.25 : .8, .5);
      else { this.follow = false; this.end += this.span * (e.key === "ArrowLeft" ? -.15 : .15); this.bound(); this.request(); }
    });
    this.observer = new ResizeObserver(() => this.request());
    this.observer.observe(el);
    document.addEventListener("radar-theme", () => this.request());
  }
  update(points, options = {}) {
    this.options = {...this.options, ...options};
    // Server observations are ordered; collapse the current tick at an identical timestamp.
    const clean = [];
    for (const p of points || []) {
      if (!Number.isFinite(p[0])) continue;
      if (clean.length && p[0] === clean[clean.length - 1][0]) clean[clean.length - 1] = p;
      else if (!clean.length || p[0] > clean[clean.length - 1][0]) clean.push(p);
    }
    this.points = clean;
    if (this.follow || !this.end) this.end = this.latest();
    this.bound();
    this.request();
  }
  latest() { return this.options.now || this.points[this.points.length - 1]?.[0] || Date.now() / 1000; }
  live(span) {
    if (span) this.span = span;
    this.follow = true;
    this.yZoom = 1;
    this.end = this.latest();
    this.request();
  }
  reset() { this.points = []; this.live(); }
  setActive(active) { this.active = active; if (active) this.request(); }
  bound() {
    const first = this.points[0]?.[0];
    if (first == null) return;
    this.span = Math.max(30, Math.min(86400, this.span));
    this.end = Math.max(first + Math.min(30, this.span), Math.min(this.latest() + this.span * .15, this.end));
  }
  xy(e) { const r = this.cross.getBoundingClientRect(); return {x: e.clientX - r.left, y: e.clientY - r.top}; }
  down(e) {
    if (e.button !== 0 || !this.plot) return;
    const p = this.xy(e);
    this.drag = {id: e.pointerId, ...p, end: this.end, scale: this.yZoom, axis: p.x > this.plot.R};
    this.cross.setPointerCapture(e.pointerId);
    this.cross.classList.add("dragging");
    this.tip.hidden = true;
    this.cross.focus({preventScroll: true});
    e.preventDefault();
  }
  move(e) {
    this.pointer = this.xy(e);
    if (this.drag) {
      if (e.pointerId !== this.drag.id) return;
      if (this.drag.axis) this.yZoom = Math.max(.05, Math.min(30, this.drag.scale * Math.exp((this.pointer.y - this.drag.y) / 130)));
      else { this.follow = false; this.end = this.drag.end - (this.pointer.x - this.drag.x) * this.span / (this.plot.R - this.plot.L); this.bound(); }
      this.request();
    } else this.request(false);
  }
  up(e) {
    if (this.drag?.id !== e.pointerId) return;
    this.drag = null;
    this.cross.classList.remove("dragging");
    if (this.cross.hasPointerCapture(e.pointerId)) this.cross.releasePointerCapture(e.pointerId);
    this.request(false);
  }
  wheel(e) {
    if (!this.plot) return;
    e.preventDefault();
    const p = this.xy(e);
    if (p.x > this.plot.R) {
      this.yZoom = Math.max(.05, Math.min(30, this.yZoom * Math.exp(e.deltaY * .002)));
      this.request();
    } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      this.follow = false;
      this.end += (e.deltaX || e.deltaY) * this.span / (this.plot.R - this.plot.L);
      this.bound(); this.request();
    } else this.zoom(Math.exp(Math.max(-.5, Math.min(.5, e.deltaY * .0015))),
      Math.max(0, Math.min(1, (p.x - this.plot.L) / (this.plot.R - this.plot.L))));
  }
  zoom(factor, anchor) {
    const old = this.span;
    this.span = Math.max(30, Math.min(86400, old * factor));
    this.end += (this.span - old) * (1 - anchor);
    this.follow = false;
    this.bound(); this.request();
  }
  request(data = true) {
    this.dirty = this.dirty || data;
    if (this.frame || !this.active || document.hidden) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      if (!this.active || !this.el.clientWidth || !this.el.clientHeight) return;
      if (this.dirty) { this.dirty = false; this.draw(); }
      this.hover();
    });
  }
  ticks(lo, hi, n) {
    const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(v => v >= raw) || raw;
    if (!(step > 0 && Number.isFinite(step))) return [];
    const result = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi && result.length < 20; v += step) result.push(v);
    return result;
  }
  draw() {
    const W = this.el.clientWidth, H = this.el.clientHeight, dpr = Math.min(2, window.devicePixelRatio || 1);
    for (const c of [this.canvas, this.cross]) {
      c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
      c.style.width = `${W}px`; c.style.height = `${H}px`;
      c.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    this.W = W; this.H = H;
    const style = getComputedStyle(this.el);
    const color = name => style.getPropertyValue(name).trim();
    this.colors = {text: color("--text"), muted: color("--muted"), line: color("--line"), panel: color("--panel")};
    const ctx = this.canvas.getContext("2d");
    ctx.font = "10px Consolas, monospace";
    ctx.clearRect(0, 0, W, H);
    const L = 8, R = Math.max(50, W - 86), T = 10, B = H - 26;
    const start = this.end - this.span;
    const xs = t => L + (t - start) / this.span * (R - L);
    this.plot = {L, R, T, B, xs, start};
    const series = this.options.series || [];
    const panes = Math.max(1, ...series.map(s => (s.pane || 0) + 1));
    const paneHeight = (B - T) / panes;
    const visible = this.points.filter((p, i, a) => p[0] <= this.end + this.span * .05 &&
      (p[0] >= start || a[i + 1]?.[0] >= start));
    this.scales = [];
    for (let pane = 0; pane < panes; pane++) {
      const group = series.filter(s => (s.pane || 0) === pane);
      let lo = Infinity, hi = -Infinity;
      for (const p of visible) for (const s of group) if (p[s.index] != null && Number.isFinite(p[s.index])) {
        lo = Math.min(lo, p[s.index]); hi = Math.max(hi, p[s.index]);
      }
      if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
      const center = (lo + hi) / 2;
      const pad = Math.max((hi - lo) * .6, Math.abs(hi) * .0001, 1e-8) * this.yZoom;
      lo = center - pad; hi = center + pad;
      const top = T + paneHeight * pane + 4, bottom = T + paneHeight * (pane + 1) - (pane === panes - 1 ? 0 : 14);
      const ys = v => bottom - (v - lo) / (hi - lo) * (bottom - top);
      this.scales.push({top, bottom, ys, lo, hi, group});
      ctx.strokeStyle = this.colors.line; ctx.lineWidth = .6;
      for (const v of this.ticks(lo, hi, Math.max(2, Math.floor(paneHeight / 48)))) {
        const y = Math.round(ys(v)) + .5;
        ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(R, y); ctx.stroke();
        ctx.fillStyle = this.colors.muted; ctx.textAlign = "left";
        ctx.fillText(group[0]?.format?.(v) || v.toPrecision(4), R + 7, y + 3, 76);
      }
      ctx.fillStyle = this.colors.muted; ctx.textAlign = "left";
      ctx.fillText(group.map(s => s.label).join(" / "), L + 4, top + 9);
      for (const s of group) {
        ctx.save(); ctx.beginPath(); ctx.rect(L, top, R - L, bottom - top); ctx.clip();
        ctx.strokeStyle = color(s.color); ctx.lineWidth = s.width || 1.7; ctx.lineJoin = "round";
        ctx.beginPath();
        let previous = null;
        const maxGap = this.options.maxGap || 90;
        for (const p of visible) {
          const v = p[s.index];
          if (v == null || !Number.isFinite(v)) { previous = null; continue; }
          const gap = !previous || p[0] - previous[0] > maxGap ||
            (this.options.membersIndex != null && p[this.options.membersIndex] !== previous[this.options.membersIndex]);
          if (gap) ctx.moveTo(xs(p[0]), ys(v)); else ctx.lineTo(xs(p[0]), ys(v));
          previous = p;
        }
        ctx.stroke();
        const last = visible[visible.length - 1];
        if (last?.[s.index] != null) {
          ctx.fillStyle = color(s.color); ctx.beginPath(); ctx.arc(xs(last[0]), ys(last[s.index]), 2.5, 0, Math.PI * 2); ctx.fill();
        }
        ctx.restore();
      }
    }
    const timeFormat = t => new Date(t * 1000).toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit", ...(this.span < 300 ? {second:"2-digit"} : {})});
    for (let i = 0; i <= 4; i++) {
      const t = start + this.span * i / 4, x = xs(t);
      ctx.strokeStyle = this.colors.line; ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, B); ctx.stroke();
      ctx.fillStyle = this.colors.muted; ctx.textAlign = i === 0 ? "left" : i === 4 ? "right" : "center";
      ctx.fillText(timeFormat(t), x, H - 8);
    }
    ctx.strokeStyle = this.colors.line; ctx.beginPath(); ctx.moveTo(R + .5, 0); ctx.lineTo(R + .5, H); ctx.stroke();
    if (!this.points.length) {
      ctx.textAlign = "center"; ctx.fillStyle = this.colors.muted; ctx.font = "12px Segoe UI, sans-serif";
      ctx.fillText(this.options.empty || "Ждём первые данные", (L + R) / 2, (T + B) / 2, R - L - 20);
    } else if (!visible.length) {
      ctx.textAlign = "center"; ctx.fillStyle = this.colors.muted;
      ctx.fillText("Нет данных в этом диапазоне · Home — к текущему времени", (L + R) / 2, (T + B) / 2, R - L - 20);
    }
    this.el.classList.toggle("chart-history", !this.follow);
    this.options.onView?.(this.follow);
  }
  hover() {
    const ctx = this.cross.getContext("2d");
    ctx.clearRect(0, 0, this.W || 0, this.H || 0);
    const p = this.pointer, box = this.plot;
    if (!p || !box || this.drag || p.x < box.L || p.x > box.R || p.y < box.T || p.y > box.B || !this.points.length) {
      this.tip.hidden = true; return;
    }
    const t = box.start + (p.x - box.L) / (box.R - box.L) * this.span;
    let lo = 0, hi = this.points.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (this.points[mid][0] < t) lo = mid + 1; else hi = mid; }
    let row = this.points[lo];
    if (lo && Math.abs(this.points[lo - 1][0] - t) < Math.abs(row[0] - t)) row = this.points[lo - 1];
    const x = box.xs(row[0]);
    if (x < box.L || x > box.R || Math.abs(row[0] - t) > Math.max(this.options.maxGap || 90, this.span * .04)) { this.tip.hidden = true; return; }
    ctx.strokeStyle = this.colors.muted; ctx.lineWidth = .7; ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, box.T); ctx.lineTo(x, box.B); ctx.moveTo(box.L, p.y); ctx.lineTo(box.R, p.y); ctx.stroke();
    ctx.setLineDash([]);
    const lines = [new Date(row[0] * 1000).toLocaleString("ru-RU")];
    for (const s of this.options.series || []) if (row[s.index] != null) lines.push(`${s.label}: ${s.format?.(row[s.index]) || row[s.index]}`);
    this.tip.textContent = lines.join("\n");
    this.tip.hidden = false;
    this.tip.style.left = `${Math.max(4, Math.min(this.W - this.tip.offsetWidth - 4, x + 12))}px`;
    this.tip.style.top = `${Math.max(4, Math.min(this.H - this.tip.offsetHeight - 4, p.y + 12))}px`;
  }
}
window.RadarChart = RadarChart;
