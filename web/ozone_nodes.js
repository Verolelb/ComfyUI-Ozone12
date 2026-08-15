// ComfyUI-Ozone12 frontend widgets.
//
// - OzoneLevelMeter: a live, animated level meter rendered in the node. The
//   node writes a temp WAV on execution; this widget decodes it and animates
//   a peak/RMS meter while the audio plays (play/pause + time display).
// - OzoneABCompare: an embedded audio player with two tracks (A and B) and a
//   seamless A/B switch button that swaps tracks *during* playback.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const METER_NODE = "OzoneLevelMeter";
const AB_NODE = "OzoneABCompare";
const GLOBAL_NODE = "OzoneGlobalMastering";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function first(v) {
  // Executed ui values arrive as lists ([value] per execution).
  return Array.isArray(v) ? v[0] : v;
}

function viewUrl(file) {
  if (!file) return null;
  const q = new URLSearchParams({
    filename: file.filename || file.name || "",
    type: file.type || "temp",
    subfolder: file.subfolder || "",
  });
  return api.fileURL("/view?" + q.toString());
}

function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m + ":" + String(s).padStart(2, "0");
}

function dbOf(amp) {
  if (!(amp > 1e-9)) return -120;
  return 20 * Math.log10(amp);
}

function clamp01(v) {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

function barColor(db) {
  if (db < -18) return "#40e060";
  if (db < -6) return "#f0e040";
  return "#f06060";
}

function makeBtn(text, title) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = text;
  b.title = title || "";
  return b;
}

const STYLE_ID = "comfyui-ozone12-style";
function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const s = document.createElement("style");
  s.id = STYLE_ID;
  s.textContent = `
.oz-widget { box-sizing: border-box; }
.oz-widget button {
  background: #2a2d33; color: #e6e6e6; border: 1px solid #454a52;
  border-radius: 5px; padding: 4px 10px; font-size: 12px;
  font-family: inherit; cursor: pointer; line-height: 1.2;
}
.oz-widget button:hover { background: #363a41; }
.oz-widget button:disabled { opacity: 0.4; cursor: default; }
.oz-widget button.active { background: #2e5e3a; border-color: #40e060; color: #d7ffd7; }
.oz-widget .oz-status { color: #9aa0a8; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.oz-ab-sel { display: flex; gap: 8px; }
.oz-ab-sel button { flex: 1; font-weight: 700; font-size: 13px; padding: 6px 0; }
.oz-ab-sel button.active { background: #2e5e3a; border-color: #40e060; }
.oz-ab-transport { display: flex; align-items: center; gap: 8px; }
.oz-ab-transport input[type="range"] { flex: 1; accent-color: #40e060; min-width: 0; }
`;
  document.head.appendChild(s);
}

// ---------------------------------------------------------------------------
// Live level meter (OzoneLevelMeter)
// ---------------------------------------------------------------------------

class LiveMeter {
  constructor(node) {
    this.node = node;
    this.ctx = null;          // AudioContext
    this.buffer = null;       // decoded AudioBuffer
    this.source = null;
    this.gainNode = null;
    this.playing = false;
    this.muted = false;
    this.rafId = null;
    this.pos = 0;
    this.playOffset = 0;
    this.startTime = 0;
    this.lastSlicePos = -1;
    this.slices = [];         // {p:[L,R], r:[L,R]}
    this.windowSec = 3;
    this.maxSlices = 44;
    this.canvasH = 240;
    this.height = 240 + 50;
    this.buildDom();
    this.addWidget();
  }

  buildDom() {
    ensureStyle();
    const c = document.createElement("div");
    c.className = "oz-widget";
    c.style.cssText =
      "display:flex;flex-direction:column;gap:7px;background:#17181c;" +
      "border:1px solid #33363d;border-radius:6px;padding:8px;box-sizing:border-box;" +
      "font-family:Inter,system-ui,sans-serif;color:#e6e6e6;font-size:12px;pointer-events:auto;";

    const canvas = document.createElement("canvas");
    canvas.style.cssText =
      "width:100%;height:" + this.canvasH + "px;display:block;background:#101114;border-radius:4px;";
    this.canvas = canvas;

    const controls = document.createElement("div");
    controls.style.cssText = "display:flex;align-items:center;gap:8px;";

    this.playBtn = makeBtn("▶", "Play / pause");
    this.playBtn.style.minWidth = "44px";
    this.playBtn.addEventListener("click", () => this.togglePlay());

    this.muteBtn = makeBtn("🔊", "Mute / unmute");
    this.muteBtn.addEventListener("click", () => {
      this.muted = !this.muted;
      this.muteBtn.textContent = this.muted ? "🔇" : "🔊";
      if (this.gainNode) this.gainNode.gain.value = this.muted ? 0 : 1;
    });

    this.timeLabel = document.createElement("span");
    this.timeLabel.textContent = "0:00 / 0:00";
    this.timeLabel.style.cssText = "font-variant-numeric:tabular-nums;white-space:nowrap;";

    this.status = document.createElement("span");
    this.status.className = "oz-status";
    this.status.textContent = "Run the workflow to load audio";

    controls.append(this.playBtn, this.muteBtn, this.timeLabel, this.status);
    c.append(canvas, controls);

    this.container = c;
    c.style.setProperty("--comfy-widget-height", this.height + "px");

    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(canvas);
  }

  addWidget() {
    if (typeof this.node.addDOMWidget !== "function") {
      console.warn("[Ozone] addDOMWidget unavailable, live meter disabled");
      return;
    }
    const widget = this.node.addDOMWidget("ozone_live_meter", "div", this.container, {
      serialize: false,
      hideOnZoom: false,
      getHeight: () => this.height,
    });
    widget.getHeight = () => this.height;
    this.widget = widget;

    setTimeout(() => {
      try {
        this.node.size = this.node.size || [0, 0];
        this.node.size[0] = Math.max(this.node.size[0], 640);
        this.node.setSize && this.node.setSize(this.node.size);
        app.canvas && app.canvas.setDirty(true);
      } catch (e) {
        /* non-fatal */
      }
    }, 60);
  }

  setStatus(text) {
    if (this.status) this.status.textContent = text;
  }

  async load(payload) {
    this.stopPlayback();
    const file = payload && payload.file;
    const url = viewUrl(file);
    this.slices = [];
    this.pos = 0;
    this.lastSlicePos = -1;
    if (!url) {
      this.setStatus("No audio - run the workflow");
      this.draw();
      return;
    }
    this.setStatus("Loading…");
    try {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const ab = await resp.arrayBuffer();
      if (!this.ctx || this.ctx.state === "closed") {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      }
      this.buffer = await this.ctx.decodeAudioData(ab);
      this.duration = this.buffer.duration;
      this.updateTime();
      this.setStatus("Ready - press ▶ for a live view");
      this.draw();
    } catch (e) {
      console.warn("[Ozone] meter load failed:", e);
      this.setStatus("Load failed");
      this.draw();
    }
  }

  togglePlay() {
    if (!this.buffer) return;
    if (this.playing) this.pausePlayback();
    else this.startPlayback();
  }

  startPlayback() {
    const ctx = this.ctx;
    if (!this.buffer || !ctx || this.playing) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    this.stopSource();
    const src = ctx.createBufferSource();
    src.buffer = this.buffer;
    this.gainNode = ctx.createGain();
    this.gainNode.gain.value = this.muted ? 0 : 1;
    src.connect(this.gainNode).connect(ctx.destination);
    src.start(0, this.pos % this.buffer.duration);
    src.onended = () => this.onSourceEnded();
    this.source = src;
    this.startTime = ctx.currentTime;
    this.playOffset = this.pos;
    this.lastSlicePos = -1;
    this.playing = true;
    this.playBtn.textContent = "⏸";
    this.playBtn.classList.add("active");
    this.setStatus("Live");
    this.rafTick();
  }

  rafTick() {
    if (!this.playing) return;
    const ctx = this.ctx;
    this.pos = this.playOffset + (ctx.currentTime - this.startTime);
    if (this.pos >= this.buffer.duration - 0.03) {
      this.stopPlayback();
      this.pos = 0;
      this.updateTime();
      this.setStatus("Ended - press ▶ to replay");
      this.draw();
      return;
    }
    const winSec = Math.min(this.windowSec, this.buffer.duration);
    if (this.pos - this.lastSlicePos >= winSec / this.maxSlices) {
      this.pushSlice();
      this.lastSlicePos = this.pos;
    }
    this.updateTime();
    this.draw();
    this.rafId = requestAnimationFrame(() => this.rafTick());
  }

  pushSlice() {
    const buf = this.buffer;
    const sr = buf.sampleRate;
    const winSec = Math.min(this.windowSec, buf.duration);
    const startIdx = Math.max(0, Math.floor((this.pos - winSec) * sr));
    const endIdx = Math.min(buf.length, Math.floor(this.pos * sr));
    const nCh = Math.min(buf.numberOfChannels, 2);
    const p = [0, 0];
    const r = [0, 0];
    const span = endIdx - startIdx;
    const stride = Math.max(1, Math.floor(span / 3000));
    for (let ch = 0; ch < nCh; ch++) {
      const data = buf.getChannelData(ch);
      let sum = 0;
      let peak = 0;
      let cnt = 0;
      for (let i = startIdx; i < endIdx; i += stride) {
        const v = data[i];
        const a = v < 0 ? -v : v;
        if (a > peak) peak = a;
        sum += v * v;
        cnt++;
      }
      p[ch] = peak;
      r[ch] = cnt ? Math.sqrt(sum / cnt) : 0;
    }
    if (nCh === 1) {
      p[1] = p[0];
      r[1] = r[0];
    }
    this.slices.push({ p: p, r: r });
    if (this.slices.length > this.maxSlices) this.slices.shift();
  }

  onSourceEnded() {
    // Natural end (or stop): finish the animation loop cleanly.
    if (this.playing) {
      this.playing = false;
      this.playBtn.textContent = "▶";
      this.playBtn.classList.remove("active");
      this.pos = 0;
      this.updateTime();
      this.setStatus("Ended - press ▶ to replay");
      this.draw();
    }
  }

  pausePlayback() {
    if (!this.playing) return;
    this.playing = false;
    const ctx = this.ctx;
    if (ctx) this.pos = this.playOffset + (ctx.currentTime - this.startTime);
    this.stopSource();
    this.playBtn.textContent = "▶";
    this.playBtn.classList.remove("active");
    this.setStatus("Paused");
    this.updateTime();
    this.draw();
  }

  stopPlayback() {
    this.playing = false;
    if (this.rafId) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }
    this.stopSource();
    if (this.playBtn) {
      this.playBtn.textContent = "▶";
      this.playBtn.classList.remove("active");
    }
  }

  stopSource() {
    if (this.source) {
      try {
        this.source.onended = null;
        this.source.stop();
      } catch (e) {
        /* already stopped */
      }
      this.source = null;
    }
  }

  updateTime() {
    if (this.timeLabel) {
      this.timeLabel.textContent = fmtTime(this.pos) + " / " + fmtTime(this.duration || 0);
    }
  }

  draw() {
    const cv = this.canvas;
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || this.node.size[0] - 24 || 600;
    const h = this.canvasH;
    const W = Math.round(w * dpr);
    const H = Math.round(h * dpr);
    if (cv.width !== W || cv.height !== H) {
      cv.width = W;
      cv.height = H;
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = "#101114";
    g.fillRect(0, 0, w, h);

    const left = 42;
    const right = 8;
    const top = 8;
    const bottom = 8;
    const gap = 12;
    const rows = 2;
    const rowH = (h - top - bottom - gap * (rows - 1)) / rows;
    const labels = ["L", "R"];
    const grid = [-60, -48, -36, -24, -12, 0];
    const n = this.slices.length;
    const barGap = 1.5;
    const barW = Math.max(1, (w - left - right - barGap * (this.maxSlices - 1)) / this.maxSlices);

    for (let c = 0; c < rows; c++) {
      const y0 = top + c * (rowH + gap);
      g.font = "600 11px Inter, system-ui, sans-serif";
      g.textAlign = "left";
      g.textBaseline = "middle";
      g.fillStyle = "#e6e6e6";
      g.fillText(labels[c], 8, y0 + 10);

      for (const db of grid) {
        const y = y0 + rowH - ((db + 60) / 60) * rowH;
        g.strokeStyle = "#26282e";
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(left, y);
        g.lineTo(w - right, y);
        g.stroke();
        g.font = "10px Inter, system-ui, sans-serif";
        g.textAlign = "right";
        g.fillStyle = "#8a8f98";
        g.fillText(String(db), left - 5, y);
      }

      let hold = 0;
      for (let i = 0; i < n; i++) {
        const s = this.slices[i];
        const x = left + i * (barW + barGap);
        const rmsFrac = clamp01((dbOf(s.r[c]) + 60) / 60);
        const peakFrac = clamp01((dbOf(s.p[c]) + 60) / 60);
        const barH = rmsFrac * (rowH - 6);
        g.fillStyle = barColor(dbOf(s.r[c]));
        g.fillRect(x, y0 + rowH - barH - 2, barW, Math.max(1, barH));
        const py = y0 + rowH - peakFrac * (rowH - 6) - 2;
        g.fillStyle = "#ffffff";
        g.fillRect(x, py, barW, 2);
        if (s.p[c] > hold) hold = s.p[c];
      }

      if (n > 0) {
        const hy = y0 + rowH - clamp01((dbOf(hold) + 60) / 60) * rowH;
        g.strokeStyle = "rgba(255,120,120,0.85)";
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(left, hy);
        g.lineTo(w - right, hy);
        g.stroke();
      }
    }
  }

  dispose() {
    this.stopPlayback();
    if (this.ro) this.ro.disconnect();
    if (this.ctx && this.ctx.state !== "closed") {
      this.ctx.close().catch(() => {});
    }
    if (this.widget && this.widget.element && this.widget.element.parentElement) {
      this.widget.element.parentElement.removeChild(this.widget.element);
    }
  }
}

// ---------------------------------------------------------------------------
// A/B playback widget (OzoneABCompare)
// ---------------------------------------------------------------------------

class ABPlayer {
  constructor(node) {
    this.node = node;
    this.tracks = { a: null, b: null };
    this.active = null; // "a" | "b"
    this.playing = false;
    this.pos = 0;
    this.height = 224;
    this.buildDom();
    this.addWidget();
  }

  buildDom() {
    ensureStyle();
    const c = document.createElement("div");
    c.className = "oz-widget";
    c.style.cssText =
      "display:flex;flex-direction:column;gap:8px;background:#17181c;" +
      "border:1px solid #33363d;border-radius:6px;padding:8px;box-sizing:border-box;" +
      "font-family:Inter,system-ui,sans-serif;color:#e6e6e6;font-size:12px;pointer-events:auto;";

    const sel = document.createElement("div");
    sel.className = "oz-ab-sel";
    this.btnA = makeBtn("Track A", "Listen to track A");
    this.btnB = makeBtn("Track B", "Listen to track B");
    this.btnA.addEventListener("click", () => this.setActive("a"));
    this.btnB.addEventListener("click", () => this.setActive("b"));
    sel.append(this.btnA, this.btnB);

    const transport = document.createElement("div");
    transport.className = "oz-ab-transport";
    this.playBtn = makeBtn("▶", "Play / pause");
    this.playBtn.style.minWidth = "48px";
    this.playBtn.addEventListener("click", () => this.togglePlay());
    const stopBtn = makeBtn("■", "Stop and rewind");
    stopBtn.addEventListener("click", () => this.stop());
    this.seek = document.createElement("input");
    this.seek.type = "range";
    this.seek.min = "0";
    this.seek.max = "0";
    this.seek.step = "0.05";
    this.seek.value = "0";
    this.seek.addEventListener("input", () => this.onSeek());
    this.timeLabel = document.createElement("span");
    this.timeLabel.textContent = "0:00 / 0:00";
    this.timeLabel.style.cssText = "font-variant-numeric:tabular-nums;white-space:nowrap;";
    transport.append(this.playBtn, stopBtn, this.seek, this.timeLabel);

    this.status = document.createElement("div");
    this.status.className = "oz-status";
    this.status.textContent = "Run the workflow to load the two tracks";

    c.append(sel, transport, this.status);
    this.container = c;
    c.style.setProperty("--comfy-widget-height", this.height + "px");
  }

  addWidget() {
    if (typeof this.node.addDOMWidget !== "function") {
      console.warn("[Ozone] addDOMWidget unavailable, A/B player disabled");
      return;
    }
    const widget = this.node.addDOMWidget("ozone_ab_player", "div", this.container, {
      serialize: false,
      hideOnZoom: false,
      getHeight: () => this.height,
    });
    widget.getHeight = () => this.height;
    this.widget = widget;

    setTimeout(() => {
      try {
        this.node.size = this.node.size || [0, 0];
        this.node.size[0] = Math.max(this.node.size[0], 520);
        this.node.setSize && this.node.setSize(this.node.size);
        app.canvas && app.canvas.setDirty(true);
      } catch (e) {
        /* non-fatal */
      }
    }, 60);
  }

  setTrack(side, file) {
    const old = this.tracks[side];
    if (old) {
      old.pause();
      old.removeAttribute("src");
      old.load();
    }
    const el = document.createElement("audio");
    el.preload = "auto";
    if (file) {
      el.src = viewUrl(file);
      el.addEventListener("timeupdate", () => this.onTimeUpdate(side));
      el.addEventListener("ended", () => this.onEnded(side));
      el.addEventListener("loadedmetadata", () => this.updateTransport());
      el.addEventListener("error", () => {
        this.setStatus("Track " + side.toUpperCase() + " failed to load");
      });
    }
    this.tracks[side] = el;
  }

  load(payload) {
    this.pausePlayback(true);
    const p = payload || {};
    this.setTrack("a", p.a);
    this.setTrack("b", p.b);
    this.active = this.tracks.a && this.tracks.a.src ? "a" : this.tracks.b && this.tracks.b.src ? "b" : null;
    this.pos = 0;
    this.seek.value = "0";
    this.seek.max = "0";
    this.timeLabel.textContent = "0:00 / 0:00";
    this.updateSelect();
    this.setStatus(this.describe());
  }

  describe() {
    const ready = (side) =>
      this.tracks[side] && this.tracks[side].src ? "ready" : "empty";
    return (
      "Listening: " + (this.active ? this.active.toUpperCase() : "—") +
      " · A: " + ready("a") + " · B: " + ready("b")
    );
  }

  setStatus(text) {
    if (this.status) this.status.textContent = text;
  }

  setActive(side) {
    if (side === this.active || !this.tracks[side] || !this.tracks[side].src) return;
    const cur = this.active ? this.tracks[this.active] : null;
    const tgt = this.tracks[side];
    if (cur && tgt) tgt.currentTime = cur.currentTime;
    this.active = side;
    this.applyMutes();
    this.updateSelect();
    this.updateTransport();
    this.setStatus(this.describe());
  }

  applyMutes() {
    for (const side of ["a", "b"]) {
      const el = this.tracks[side];
      if (el) el.muted = side !== this.active;
    }
  }

  togglePlay() {
    if (this.playing) this.pausePlayback();
    else this.play();
  }

  play() {
    const act = this.active ? this.tracks[this.active] : null;
    if (!act || !act.src) return;
    const other = this.active === "a" ? this.tracks.b : this.tracks.a;
    act.currentTime = this.pos;
    if (other && other.src) other.currentTime = this.pos;
    act.play().catch(() => {});
    if (other && other.src) other.play().catch(() => {});
    this.playing = true;
    this.applyMutes();
    this.playBtn.textContent = "⏸";
    this.playBtn.classList.add("active");
    this.setStatus(this.describe());
  }

  pausePlayback(quiet) {
    this.playing = false;
    for (const side of ["a", "b"]) {
      const el = this.tracks[side];
      if (el) el.pause();
    }
    const act = this.active ? this.tracks[this.active] : null;
    if (act) this.pos = act.currentTime;
    this.playBtn.textContent = "▶";
    this.playBtn.classList.remove("active");
    if (!quiet) this.setStatus("Paused");
  }

  stop() {
    this.pausePlayback();
    this.pos = 0;
    for (const side of ["a", "b"]) {
      const el = this.tracks[side];
      if (el && el.src) el.currentTime = 0;
    }
    this.seek.value = "0";
    this.updateTime();
    this.setStatus("Stopped");
  }

  onTimeUpdate(side) {
    const el = this.tracks[side];
    if (!el) return;
    if (side === this.active) {
      this.pos = el.currentTime;
      if (el.duration && isFinite(el.duration)) {
        this.seek.max = String(el.duration);
      }
      this.seek.value = String(el.currentTime);
      this.updateTime();
    }
    if (this.playing) {
      const other = side === "a" ? this.tracks.b : this.tracks.a;
      if (other && other.src && Math.abs(other.currentTime - el.currentTime) > 0.5) {
        other.currentTime = el.currentTime;
      }
    }
  }

  onEnded(side) {
    if (side !== this.active) return;
    this.pausePlayback();
    this.pos = 0;
    for (const s of ["a", "b"]) {
      const el = this.tracks[s];
      if (el && el.src) el.currentTime = 0;
    }
    this.seek.value = "0";
    this.updateTime();
    this.setStatus("Ended - press ▶ to replay");
  }

  onSeek() {
    const v = parseFloat(this.seek.value) || 0;
    this.pos = v;
    for (const side of ["a", "b"]) {
      const el = this.tracks[side];
      if (el && el.src) el.currentTime = v;
    }
    this.updateTime();
  }

  updateTime() {
    const act = this.active ? this.tracks[this.active] : null;
    const dur = act && act.duration && isFinite(act.duration) ? act.duration : 0;
    this.timeLabel.textContent = fmtTime(this.pos) + " / " + fmtTime(dur);
  }

  updateSelect() {
    this.btnA.classList.toggle("active", this.active === "a");
    this.btnB.classList.toggle("active", this.active === "b");
    this.btnA.disabled = !(this.tracks.a && this.tracks.a.src);
    this.btnB.disabled = !(this.tracks.b && this.tracks.b.src);
  }

  updateTransport() {
    if (this.active) this.updateTime();
  }

  dispose() {
    this.pausePlayback(true);
    for (const side of ["a", "b"]) {
      const el = this.tracks[side];
      if (el) {
        el.pause();
        el.removeAttribute("src");
        el.load();
      }
    }
    if (this.widget && this.widget.element && this.widget.element.parentElement) {
      this.widget.element.parentElement.removeChild(this.widget.element);
    }
  }
}

// ---------------------------------------------------------------------------
// Active-module badges (OzoneGlobalMastering)
// ---------------------------------------------------------------------------

const MODULE_NAMES = {
  eq: "Equalizer 1",
  eq2: "Equalizer 2",
  dynamiceq: "Dynamic EQ",
  matcheq: "Match EQ",
  vintageeq: "Vintage EQ",
  exciter: "Exciter",
  stabilizer: "Stabilizer",
  spectralshaper: "Spectral Shaper",
  imager: "Imager",
  impact: "Impact",
  masterrebalance: "Master Rebalance",
  lowendfocus: "Low End Focus",
  clarity: "Clarity",
  dynamics: "Dynamics",
  vintagecompressor: "Vintage Compressor",
  vintagetape: "Vintage Tape",
  vintagelimiter: "Vintage Limiter",
  maximizer: "Maximizer",
};

function moduleName(section) {
  if (MODULE_NAMES[section]) return MODULE_NAMES[section];
  return String(section)
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

class ModuleBadges {
  constructor(node) {
    this.node = node;
    this.payload = null;
    this.buildDom();
    this.addWidget();
  }

  buildDom() {
    ensureStyle();
    const c = document.createElement("div");
    c.className = "oz-widget oz-mod";
    c.style.cssText =
      "width: 100%; background: #14171d; border: 1px solid #2a2e36; border-radius: 6px; padding: 8px;";
    c.innerHTML =
      '<div style="display:flex; align-items:center; gap:6px; margin-bottom:6px;">' +
      '<span style="font-weight:700; color:#dfe3ea; font-size:12px;">Modules actifs</span>' +
      '<span class="oz-status oz-mod-preset" style="flex:1; text-align:right; font-size:11px;"></span>' +
      "</div>" +
      '<div class="oz-mod-list" style="display:flex; flex-direction:column; gap:3px; max-height:200px; overflow:auto;"></div>' +
      '<div class="oz-status oz-mod-hint" style="margin-top:6px;">' +
      "Aucune donnée — exécute le node pour voir les modules actifs du preset." +
      "</div>";
    this.container = c;
    this.listEl = c.querySelector(".oz-mod-list");
    this.presetEl = c.querySelector(".oz-mod-preset");
    this.hintEl = c.querySelector(".oz-mod-hint");
  }

  addWidget() {
    if (typeof this.node.addDOMWidget !== "function") {
      console.warn("[Ozone] addDOMWidget unavailable, module badges disabled");
      return;
    }
    this.widget = this.node.addDOMWidget("ozone_modules_view", "div", this.container, {
      serialize: false,
      hideOnZoom: false,
      getValue: () => this.payload,
      setValue: (v) => {
        if (v) this.render(v);
      },
    });
  }

  render(payload) {
    this.payload = payload || null;
    const mods = (payload && payload.modules) || [];
    if (payload && payload.preset) {
      const name = String(payload.preset).split("/").pop();
      this.presetEl.textContent = name;
      this.presetEl.title = String(payload.preset);
    } else {
      this.presetEl.textContent = "";
    }
    this.listEl.innerHTML = "";
    const enabled = mods.filter((m) => m && m.enabled);
    if (!enabled.length) {
      this.hintEl.style.display = "";
      this.hintEl.textContent = payload && payload.bypassed
        ? "Preset désactivé (bypass) — active 'Preset On' pour traiter."
        : "Aucun module actif pour ce preset.";
      return;
    }
    this.hintEl.style.display = "none";
    for (const m of enabled) this.listEl.appendChild(this.rowEl(m));
  }

  rowEl(m) {
    const row = document.createElement("div");
    row.style.cssText =
      "display:flex; align-items:center; gap:6px; padding:3px 6px; border-radius:4px; background:#1f232b;";

    const nameEl = document.createElement("span");
    nameEl.textContent = moduleName(m.section);
    nameEl.style.cssText =
      "flex:1; color:#dfe3ea; font-weight:600; font-size:11px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;";
    row.appendChild(nameEl);

    const nParams = m.params || 0;
    let badge;
    let color;
    if (nParams === 0) {
      badge = "défauts";
      color = "#6b7280";
    } else {
      const rms = typeof m.rms === "number" ? m.rms : 0;
      if (Math.abs(rms) >= 0.5) {
        badge = (rms >= 0 ? "+" : "") + rms.toFixed(1) + " dB";
        color = "#40e060";
      } else {
        badge = "subtil";
        color = "#f0e040";
      }
    }

    const badgeEl = document.createElement("span");
    badgeEl.textContent = badge;
    badgeEl.style.cssText = "color:" + color + "; font-weight:700; font-size:11px; white-space:nowrap;";
    const tips = [nParams + " paramètre(s) appliqué(s)"];
    if (typeof m.rms === "number") tips.push("Δ RMS " + (m.rms >= 0 ? "+" : "") + m.rms.toFixed(2) + " dB");
    if (typeof m.crest === "number") tips.push("Δ crest " + (m.crest >= 0 ? "+" : "") + m.crest.toFixed(2) + " dB");
    badgeEl.title = tips.join("\n");
    row.appendChild(badgeEl);
    return row;
  }

  dispose() {
    if (this.widget && this.widget.element && this.widget.element.parentElement) {
      this.widget.element.parentElement.removeChild(this.widget.element);
    }
  }
}

// ---------------------------------------------------------------------------
// Extension registration
// ---------------------------------------------------------------------------

app.registerExtension({
  name: "ComfyUI-Ozone12.Widgets",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name === METER_NODE) {
      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
        try {
          if (!this.ozoneMeter) this.ozoneMeter = new LiveMeter(this);
        } catch (e) {
          console.error("[Ozone] meter widget creation failed:", e);
        }
        return r;
      };

      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        const r = onExecuted ? onExecuted.apply(this, arguments) : undefined;
        try {
          const payload = first(message && message.ozone_meter);
          if (payload && this.ozoneMeter) this.ozoneMeter.load(payload);
        } catch (e) {
          console.error("[Ozone] meter onExecuted failed:", e);
        }
        return r;
      };

      const onRemoved = nodeType.prototype.onRemoved;
      nodeType.prototype.onRemoved = function () {
        const r = onRemoved ? onRemoved.apply(this, arguments) : undefined;
        try {
          if (this.ozoneMeter) {
            this.ozoneMeter.dispose();
            this.ozoneMeter = null;
          }
        } catch (e) {
          /* non-fatal */
        }
        return r;
      };
    }

    if (nodeData.name === AB_NODE) {
      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
        try {
          if (!this.ozoneAB) this.ozoneAB = new ABPlayer(this);
        } catch (e) {
          console.error("[Ozone] A/B widget creation failed:", e);
        }
        return r;
      };

      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        const r = onExecuted ? onExecuted.apply(this, arguments) : undefined;
        try {
          const payload = first(message && message.ozone_ab);
          if (payload && this.ozoneAB) this.ozoneAB.load(payload);
        } catch (e) {
          console.error("[Ozone] A/B onExecuted failed:", e);
        }
        return r;
      };

      const onRemoved = nodeType.prototype.onRemoved;
      nodeType.prototype.onRemoved = function () {
        const r = onRemoved ? onRemoved.apply(this, arguments) : undefined;
        try {
          if (this.ozoneAB) {
            this.ozoneAB.dispose();
            this.ozoneAB = null;
          }
        } catch (e) {
          /* non-fatal */
        }
        return r;
      };
    }

    if (nodeData.name === GLOBAL_NODE) {
      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
        try {
          if (!this.ozoneModules) this.ozoneModules = new ModuleBadges(this);
        } catch (e) {
          console.error("[Ozone] module badges creation failed:", e);
        }
        return r;
      };

      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        const r = onExecuted ? onExecuted.apply(this, arguments) : undefined;
        try {
          const payload = first(message && message.ozone_modules);
          if (payload && this.ozoneModules) this.ozoneModules.render(payload);
        } catch (e) {
          console.error("[Ozone] module badges onExecuted failed:", e);
        }
        return r;
      };

      const onRemoved = nodeType.prototype.onRemoved;
      nodeType.prototype.onRemoved = function () {
        const r = onRemoved ? onRemoved.apply(this, arguments) : undefined;
        try {
          if (this.ozoneModules) {
            this.ozoneModules.dispose();
            this.ozoneModules = null;
          }
        } catch (e) {
          /* non-fatal */
        }
        return r;
      };
    }
  },
});
