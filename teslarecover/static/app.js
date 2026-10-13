'use strict';

const $ = (sel) => document.querySelector(sel);
const CAMERAS = ['front', 'left_repeater', 'right_repeater', 'back', 'left_pillar', 'right_pillar'];
// HW3 cars record four cameras (no pillars), all at 1280-ish width; HW4 cars six, with a
// 2896-px front and 1448-px side cameras.
const HW3_CAMERAS = CAMERAS.slice(0, 4);
function isHW3(minute) {
  const clips = Object.entries(minute.clips);
  return !clips.some(([cam, c]) => cam.includes('pillar') || c.width === 2896 || c.width === 1448);
}
const CAMERA_LABELS = {
  front: 'Front', left_repeater: 'Left repeater', right_repeater: 'Right repeater', back: 'Back',
  left_pillar: 'Left pillar', right_pillar: 'Right pillar', side: 'Side (unknown)',
};
const GEARS = ['P', 'D', 'R', 'N'];
const AUTOPILOT = ['', 'FSD', 'Autosteer', 'TACC'];
const CLIP_SECONDS = 60;
// Above this speed the browser can't decode every frame (the 2896-px front camera stalls at 4x),
// so faster playback jumps from keyframe to keyframe instead (see viewer.tick).
const NATIVE_MAX_RATE = 2;
const SESSION_GAP = 5 * 60;   // seconds between clips that start a new recording in the lists

const app = {
  step: 'drive',
  drive: null,
  clips: [],          // scan results
  range: null,        // [startUtc, endUtc] or null for all
  pollTimer: null,
  jobHandlers: {},
};

// ---------- helpers ----------

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, ms = 4000) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add('hidden'), ms);
}

const fmtSize = (b) => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : (b / 1e6).toFixed(0) + ' MB';
const fmtDate = (t) => new Date(t * 1000).toLocaleDateString(undefined, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric' });
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const fmtDateTime = (t) => fmtDate(t) + ' ' + fmtTime(t);

function toLocalInput(t) {
  const d = new Date(t * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
const fromLocalInput = (s) => s ? new Date(s).getTime() / 1000 : null;

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

/** Split time-sorted items into groups separated by more than gap seconds. */
function sessions(items, timeOf, gap = SESSION_GAP) {
  const out = [];
  for (const it of items) {
    const t = timeOf(it);
    const last = out[out.length - 1];
    if (last && t - last.end <= gap) { last.items.push(it); last.end = t; }
    else out.push({ start: t, end: t, items: [it] });
  }
  return out;
}

// ---------- navigation ----------

function show(step) {
  if (step !== 'viewer' && typeof viewer !== 'undefined' && viewer.playing) viewer.pause();
  app.step = step;
  document.querySelectorAll('.screen').forEach((s) => s.classList.toggle('active', s.id === 'screen-' + step));
  document.querySelectorAll('#steps button').forEach((b) => b.classList.toggle('active', b.dataset.step === step));
  if (step === 'drive') refreshDrives();
  if (step === 'select') renderSelect();
  if (step === 'copy') renderCopy();
  if (step === 'viewer') viewer.load();
  if (location.hash !== '#' + step) history.replaceState(null, '', '#' + step);
}

document.querySelectorAll('#steps button').forEach((b) => b.addEventListener('click', () => show(b.dataset.step)));

// ---------- job polling ----------

function renderProgress(container, job, extra) {
  container.innerHTML = '';
  if (!job) return;
  const pct = job.total ? Math.min(100, 100 * job.done / job.total) : (job.status === 'done' ? 100 : 0);
  const bar = el('div', { class: 'bar' }, el('div'));
  bar.firstChild.style.width = pct + '%';
  container.append(el('div', { class: 'phase' }, job.phase + (job.total > 1 && job.status === 'running' ? ` — ${pct.toFixed(0)}%` : '')), bar);
  if (job.status === 'running') {
    container.append(el('button', { onclick: () => api('/api/cancel', {}) }, 'Cancel'));
  } else if (job.status === 'error' || job.status === 'auth') {
    container.append(el('div', { class: 'error' }, job.error));
  } else if (job.status === 'cancelled') {
    container.append(el('div', { class: 'error' }, 'Cancelled.'));
  }
  if (extra) container.append(extra);
}

async function poll() {
  let st;
  try { st = await api('/api/state'); } catch (e) { return; }
  app.state = st;
  const job = st.job;
  if (job) {
    const handler = app.jobHandlers[job.kind];
    if (handler) handler(job);
    if (job.status !== 'running' && app.watching === job.kind) {
      app.watching = null;
      const done = app.jobHandlers[job.kind + ':done'];
      if (done) done(job);
    }
  }
  markDoneSteps(st);
}

function markDoneSteps(st) {
  const done = {
    drive: !!st.drive, scan: st.scanned, select: st.scanned && !!st.out_dir, copy: !!st.library,
    signin: st.signed_in, decrypt: st.library && (st.library.counts.ok || 0) > 0,
  };
  document.querySelectorAll('#steps button').forEach((b) => b.classList.toggle('done', !!done[b.dataset.step]));
}

const JOB_SCREENS = { scan: 'scan', export: 'copy', signin: 'signin', decrypt: 'decrypt', repair: 'decrypt' };

async function startJob(kind, path, body) {
  try {
    await api(path, body);
    app.watching = kind;
    poll();
  } catch (e) {
    // Another step is still running: show it (with its Cancel button) instead of failing quietly.
    const st = await api('/api/state').catch(() => null);
    const running = st && st.job && st.job.status === 'running' ? st.job : null;
    if (running && JOB_SCREENS[running.kind]) {
      app.watching = running.kind;
      show(JOB_SCREENS[running.kind]);
      toast(`“${running.phase}” is still running. Wait for it to finish or cancel it first.`, 8000);
    } else {
      toast(e.message, 8000);
    }
    throw e;
  }
}

setInterval(poll, 500);

// ---------- 1. drive ----------

async function refreshDrives() {
  if (app.step !== 'drive') return;
  let drives = [];
  try { drives = await api('/api/drives'); } catch (e) { /* ignore */ }
  const list = $('#drive-list');
  list.innerHTML = '';
  const teslaDrives = drives.filter((d) => d.teslacam);
  if (!app.drive && teslaDrives.length === 1) app.drive = teslaDrives[0].drive;
  if (app.drive && !drives.some((d) => d.drive === app.drive)) app.drive = null;
  for (const d of drives) {
    const card = el('div', { class: 'card' + (d.drive === app.drive ? ' selected' : ''), onclick: () => { app.drive = d.drive; refreshDrives(); } },
      el('div', { class: 'big' }, `${d.drive} ${d.label || 'USB drive'}`),
      el('div', { class: 'muted' }, `${d.fs} · ${fmtSize(d.size)}`),
      d.teslacam ? el('div', { class: 'success' }, 'Tesla dashcam drive') : el('div', { class: 'muted' }, 'No TeslaCam folder'),
      d.supported ? null : el('div', { class: 'warn' }, 'Unsupported file system (needs exFAT or FAT32)'));
    list.append(card);
  }
  $('#drive-empty').textContent = drives.length ? '' : 'No USB drive found yet — plug in the Tesla USB drive.';
  const sel = drives.find((d) => d.drive === app.drive);
  $('#scan-btn').disabled = !(sel && sel.supported);
}
setInterval(refreshDrives, 2000);

$('#scan-btn').addEventListener('click', async () => {
  await startJob('scan', '/api/scan', { drive: app.drive });
  show('scan');
});

// ---------- 2. scan ----------

app.jobHandlers.scan = (job) => {
  let extra = null;
  if (job.status === 'error' && /denied|permission|access/i.test(job.error || '')) {
    extra = el('div', {}, el('p', { class: 'muted' }, 'Windows refused direct access to the drive. Restart the app with administrator rights:'),
      el('button', { class: 'primary', onclick: () => api('/api/elevate', {}) }, 'Restart as administrator'));
  }
  renderProgress($('#scan-progress'), job, extra);
};
app.jobHandlers['scan:done'] = async (job) => {
  if (job.status !== 'done') return;
  app.clips = await api('/api/clips');
  show('select');
};

// ---------- 3. select ----------

function selectedClips() {
  if (!app.range) return app.clips;
  const [a, b] = app.range;
  return app.clips.filter((c) => c.start_utc != null && c.start_utc <= b && c.start_utc + CLIP_SECONDS >= a);
}

function updateSelectionInfo() {
  const sel = selectedClips();
  const size = sel.reduce((s, c) => s + c.size, 0);
  const deleted = sel.filter((c) => c.source !== 'live').length;
  $('#selection-info').textContent = `${sel.length} clips selected (${deleted} recovered from deleted data) · ${fmtSize(size)}`;
  document.querySelectorAll('.session').forEach((row) => {
    const s = app.sessionList[row.dataset.i];
    row.classList.toggle('selected', !!app.range && s.start <= app.range[1] && s.end + CLIP_SECONDS >= app.range[0]);
  });
  drawTimeline();
}

function setRange(range) {
  app.range = range;
  document.querySelector(`input[name=range][value=${range ? 'range' : 'all'}]`).checked = true;
  if (range) {
    $('#range-from').value = toLocalInput(range[0]);
    $('#range-to').value = toLocalInput(range[1]);
  }
  updateSelectionInfo();
}

function renderSelect() {
  const clips = app.clips;
  if (!clips.length) {
    $('#select-summary').textContent = 'Scan a drive first.';
    return;
  }
  const timed = clips.filter((c) => c.start_utc != null);
  const live = clips.filter((c) => c.source === 'live').length;
  $('#select-summary').textContent =
    `Found ${clips.length} clips: ${live} still on the drive and ${clips.length - live} recovered from deleted data` +
    (timed.length ? `, from ${fmtDateTime(timed[0].start_utc)} to ${fmtDateTime(timed[timed.length - 1].start_utc)}.` : '.');

  app.sessionList = sessions(timed, (c) => c.start_utc);
  const list = $('#session-list');
  list.innerHTML = '';
  app.sessionList.slice().reverse().forEach((s) => {
    const i = app.sessionList.indexOf(s);
    const minutes = new Set(s.items.map((c) => Math.round(c.start_utc / 60))).size;
    const del = s.items.filter((c) => c.source !== 'live').length;
    const mix = el('div', { class: 'mix' }, el('div', { class: 'l' }), el('div', { class: 'd' }));
    mix.children[0].style.width = (100 * (s.items.length - del) / s.items.length) + '%';
    mix.children[1].style.width = (100 * del / s.items.length) + '%';
    const row = el('div', { class: 'session', 'data-i': i, onclick: (ev) => {
      if (ev.shiftKey && app.range) setRange([Math.min(app.range[0], s.start), Math.max(app.range[1], s.end + CLIP_SECONDS)]);
      else setRange([s.start, s.end + CLIP_SECONDS]);
    } },
    el('div', { class: 'when' }, fmtDate(s.start) + ' ' + fmtTime(s.start).slice(0, 5)),
    el('div', {}, `${minutes} min · ${s.items.length} clips` + (del ? ` · ${del} deleted` : '')), mix,
    el('div', { class: 'muted' }, fmtSize(s.items.reduce((a, c) => a + c.size, 0))));
    list.append(row);
  });
  const untimed = clips.length - timed.length;
  if (untimed) list.append(el('p', { class: 'muted' }, `${untimed} clips without a readable time are only included with “All clips”.`));
  updateSelectionInfo();
}

// Timeline: a bar per time bucket; drag to choose a range.
function drawTimeline() {
  const canvas = $('#timeline');
  const timed = app.clips.filter((c) => c.start_utc != null);
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * devicePixelRatio; canvas.height = h * devicePixelRatio;
  const ctx = canvas.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.clearRect(0, 0, w, h);
  if (!timed.length) return;
  // Compress long gaps: lay out recordings side by side, separated by small gaps.
  const segs = sessions(timed, (c) => c.start_utc, 3600);
  const gapPx = 8;
  const total = segs.reduce((a, s) => a + (s.end - s.start + CLIP_SECONDS), 0);
  const scale = (w - gapPx * (segs.length - 1)) / total;
  let x = 0;
  app.timelineMap = [];
  const css = getComputedStyle(document.documentElement);
  for (const s of segs) {
    const width = (s.end - s.start + CLIP_SECONDS) * scale;
    app.timelineMap.push({ x0: x, x1: x + width, t0: s.start, t1: s.end + CLIP_SECONDS });
    const buckets = Math.max(1, Math.floor(width / 3));
    const counts = Array.from({ length: buckets }, () => [0, 0]);
    for (const c of s.items) {
      const b = Math.min(buckets - 1, Math.floor((c.start_utc - s.start) / (s.end - s.start + CLIP_SECONDS) * buckets));
      counts[b][c.source === 'live' ? 0 : 1]++;
    }
    const max = 12;
    counts.forEach(([l, d], i) => {
      const bx = x + i * width / buckets, bw = Math.max(1, width / buckets - 0.5);
      const lh = Math.min(1, l / max) * (h - 20), dh = Math.min(1, d / max) * (h - 20);
      ctx.fillStyle = css.getPropertyValue('--live'); ctx.fillRect(bx, h - 16 - lh, bw, lh);
      ctx.fillStyle = css.getPropertyValue('--deleted'); ctx.fillRect(bx, h - 16 - lh - dh, bw, dh);
    });
    if (width > 50) {
      ctx.fillStyle = css.getPropertyValue('--muted'); ctx.font = '10px Segoe UI';
      ctx.fillText(new Date(s.start * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), x + 2, h - 3);
    }
    x += width + gapPx;
  }
  if (app.range) {
    const [a, b] = [timeToX(app.range[0]), timeToX(app.range[1])];
    ctx.fillStyle = 'rgba(62,142,247,.18)'; ctx.fillRect(a, 0, Math.max(2, b - a), h - 16);
    ctx.strokeStyle = css.getPropertyValue('--accent'); ctx.strokeRect(a, 0.5, Math.max(2, b - a), h - 17);
  }
}

function timeToX(t) {
  const m = app.timelineMap;
  for (const s of m) if (t <= s.t1) return t < s.t0 ? s.x0 : s.x0 + (t - s.t0) / (s.t1 - s.t0) * (s.x1 - s.x0);
  return m.length ? m[m.length - 1].x1 : 0;
}
function xToTime(x) {
  const m = app.timelineMap;
  for (const s of m) if (x <= s.x1) return x < s.x0 ? s.t0 : s.t0 + (x - s.x0) / (s.x1 - s.x0) * (s.t1 - s.t0);
  return m.length ? m[m.length - 1].t1 : 0;
}

(() => {
  const canvas = $('#timeline');
  let dragStart = null;
  const xOf = (ev) => ev.clientX - canvas.getBoundingClientRect().left;
  canvas.addEventListener('mousedown', (ev) => { dragStart = xOf(ev); });
  window.addEventListener('mousemove', (ev) => {
    if (dragStart == null) return;
    const [a, b] = [dragStart, xOf(ev)].sort((p, q) => p - q);
    if (b - a > 3) setRange([xToTime(a), xToTime(b)]);
  });
  window.addEventListener('mouseup', () => { dragStart = null; });
  window.addEventListener('resize', () => { if (app.step === 'select') drawTimeline(); });
})();

document.querySelectorAll('input[name=range]').forEach((r) => r.addEventListener('change', () => {
  if (r.checked && r.value === 'all') setRange(null);
  if (r.checked && r.value === 'range') {
    const a = fromLocalInput($('#range-from').value), b = fromLocalInput($('#range-to').value);
    if (a && b) setRange([a, b]);
  }
}));
['#range-from', '#range-to'].forEach((id) => $(id).addEventListener('change', () => {
  const a = fromLocalInput($('#range-from').value), b = fromLocalInput($('#range-to').value);
  if (a && b) setRange([Math.min(a, b), Math.max(a, b)]);
}));

$('#to-copy-btn').addEventListener('click', () => {
  if (!selectedClips().length) return toast('No clips in the selected range.');
  show('copy');
});

// ---------- 4. copy ----------

function renderCopy() {
  const sel = selectedClips();
  $('#copy-summary').textContent = app.clips.length
    ? `${sel.length} clips (${fmtSize(sel.reduce((s, c) => s + c.size, 0))}) will be copied. Choose an empty folder with enough space; decrypted copies need about the same space again.`
    : 'Scan a drive and choose clips first.';
  if (!$('#out-dir').value && app.state && app.state.last_out_dir) $('#out-dir').value = app.state.last_out_dir;
}

$('#pick-out-btn').addEventListener('click', async () => {
  const r = await api('/api/pick-folder', { initial: $('#out-dir').value || null });
  if (r.path) $('#out-dir').value = r.path;
});

$('#copy-btn').addEventListener('click', async () => {
  const out = $('#out-dir').value.trim();
  if (!out) return toast('Choose an output folder first.');
  const body = { out_dir: out, start_utc: app.range ? app.range[0] : null, end_utc: app.range ? app.range[1] : null };
  await startJob('export', '/api/export', body);
});
app.jobHandlers.export = (job) => renderProgress($('#copy-progress'), job);
app.jobHandlers['export:done'] = (job) => {
  if (job.status !== 'done') return;
  toast(`Copied ${job.result.copied} clips.`);
  show(app.state && app.state.signed_in ? 'decrypt' : 'signin');
};

// ---------- 5. sign in ----------

$('#signin-btn').addEventListener('click', () => startJob('signin', '/api/signin', {}));
app.jobHandlers.signin = (job) => renderProgress($('#signin-progress'), job);
app.jobHandlers['signin:done'] = (job) => {
  if (job.status === 'done') { toast('Signed in to Tesla.'); show('decrypt'); }
};
$('#token-btn').addEventListener('click', async () => {
  try {
    await api('/api/token', { token: $('#token-input').value });
    $('#token-input').value = '';
    toast('Token saved for this session.');
    show('decrypt');
  } catch (e) { toast(e.message); }
});

// ---------- 6. decrypt ----------

$('#decrypt-btn').addEventListener('click', () => {
  $('#decrypt-result').innerHTML = '';
  startJob('decrypt', '/api/decrypt', { keep_encrypted: $('#keep-encrypted').checked });
});
app.jobHandlers.decrypt = (job) => renderProgress($('#decrypt-progress'), job);
app.jobHandlers['decrypt:done'] = async (job) => {
  if (job.status === 'auth') {
    toast('Please sign in to Tesla again; decryption continues where it stopped.', 8000);
    show('signin');
    return;
  }
  const st = await api('/api/state');
  const counts = (st.library && st.library.counts) || {};
  const box = $('#decrypt-result');
  box.innerHTML = '';
  const grid = el('div', { class: 'result-grid' });
  const labels = { ok: 'ready to watch', unplayable: 'damaged (kept in “unplayable”)', key_error: 'no key from Tesla', encrypted: 'still encrypted' };
  for (const [k, label] of Object.entries(labels)) if (counts[k]) grid.append(el('div', {}, el('b', {}, String(counts[k])), label));
  box.append(grid,
    el('div', { class: 'actions' },
      el('button', { class: 'primary', onclick: () => { viewer.loaded = false; show('viewer'); } }, 'Watch footage'),
      counts.unplayable ? el('button', { onclick: startRepair }, 'Try to repair damaged clips') : null,
      el('button', { onclick: () => api('/api/open-folder', {}) }, 'Open folder')));
};

function startRepair() {
  show('decrypt');
  $('#decrypt-result').innerHTML = '';
  startJob('repair', '/api/repair', {});
}
app.jobHandlers.repair = (job) => renderProgress($('#decrypt-progress'), job);
app.jobHandlers['repair:done'] = (job) => {
  viewer.loaded = false;
  if (job.status === 'done') toast('Repair finished.');
  app.jobHandlers['decrypt:done'](job);
};

// ---------- open an existing folder ----------

$('#open-folder-btn').addEventListener('click', async () => {
  const r = await api('/api/pick-folder', {});
  if (!r.path) return;
  try {
    await api('/api/open-library', { folder: r.path });
    viewer.loaded = false;
    show('viewer');
  } catch (e) { toast(e.message); }
});

// ---------- 7. viewer ----------

const viewer = {
  loaded: false,
  minutes: [],      // [{t, clips: {camera: clip}, title, comment}]
  index: -1,
  focus: null,      // camera shown large, or null for the grid
  rate: 1,
  clock: 0,         // playback position (s) in keyframe-stepping mode
  lastTick: 0,
  keyframes: new Map(),   // file -> keyframe times

  // Keyframe stepping is used for fast playback and while dragging the seek bar.
  stepMode() { return this.rate > NATIVE_MAX_RATE || this.scrubbing; },

  // Dragging the seek bar: exact seeks on every move pile up in the decoders (each decodes up to
  // 18 frames) and freeze the cameras, so show keyframes while dragging and seek exactly on release.
  scrub(t) {
    if (!this.scrubbing) {
      this.scrubbing = true;
      this.resumeAfterScrub = this.playing;
      for (const v of this.videos()) v.pause();
    }
    this.clock = t;
  },
  endScrub() {
    if (!this.scrubbing) return;
    this.scrubbing = false;
    const t = this.clock;
    if (this.rate <= NATIVE_MAX_RATE) {
      this.seek(t);
      if (this.resumeAfterScrub) this.play();
    }
  },
  position() { const m = this.master(); return this.stepMode() ? this.clock : (m ? m.currentTime : 0); },

  async loadKeyframes(file) {
    if (this.keyframes.has(file)) return;
    this.keyframes.set(file, null);
    try { this.keyframes.set(file, await api('/api/keyframes?file=' + encodeURIComponent(file))); } catch (e) { /* exact seeks then */ }
  },

  setRate(rate) {
    const wasStep = this.stepMode();
    const t = this.position();
    this.rate = rate;
    for (const v of this.videos()) v.playbackRate = this.stepMode() ? 1 : rate;
    if (this.stepMode() && !wasStep) {
      this.clock = t;
      for (const v of this.videos()) v.pause();
    } else if (!this.stepMode() && wasStep) {
      this.seek(t);
      if (this.playing) this.play();
    }
  },
  playing: false,
  telemetry: null,
  telemetryFile: null,
  marked: new Set(),

  async load() {
    if (this.loaded) return;
    let lib;
    try { lib = await api('/api/library'); } catch (e) {
      $('#clip-list').innerHTML = '<div class="other">No recovered clips open yet. Finish the steps, or use “Open recovered folder…”.</div>';
      return;
    }
    this.loaded = true;
    // One entry per recording minute; all six cameras share the same mvhd time.
    const byTime = new Map();
    for (const c of lib.clips) {
      const key = Math.round(c.recorded_utc);
      if (!byTime.has(key)) byTime.set(key, { t: c.recorded_utc, clips: {}, title: c.title, comment: c.comment });
      const m = byTime.get(key);
      let cam = c.camera || 'side';
      if (m.clips[cam]) cam = cam + '_' + c.id;   // unexpected duplicate: keep, but not in a tile
      m.clips[cam] = c;
      if (!m.title && c.title) { m.title = c.title; m.comment = c.comment; }
    }
    this.minutes = [...byTime.values()].sort((a, b) => a.t - b.t);
    this.renderList(lib);
    this.buildTiles();
    if (this.minutes.length) this.open(0, false);
  },

  renderList(lib) {
    const list = $('#clip-list');
    list.innerHTML = '';
    let day = null;
    for (const s of sessions(this.minutes, (m) => m.t, SESSION_GAP)) {
      const d = fmtDate(s.start);
      if (d !== day) { day = d; list.append(el('div', { class: 'day' }, d)); }
      list.append(el('div', { class: 'group' }, `${fmtTime(s.start).slice(0, 5)} – ${fmtTime(s.end + CLIP_SECONDS).slice(0, 5)} · ${s.items.length} min`));
      for (const m of s.items) {
        const i = this.minutes.indexOf(m);
        const n = Object.keys(m.clips).length;
        const total = isHW3(m) ? HW3_CAMERAS.length : CAMERAS.length;
        const row = el('div', { class: 'minute', 'data-i': i, title: [m.title, m.comment].filter(Boolean).join(', '),
          onclick: (ev) => ev.ctrlKey || ev.shiftKey ? this.mark(i, ev.shiftKey) : this.open(i, this.playing) },
        el('span', { class: 't' }, fmtTime(m.t)),
        el('span', { class: 'loc' }, m.comment || m.title || ''),
        el('span', { class: 'n' }, n < total ? `${n}/${total}` : ''));
        list.append(row);
      }
    }
    if (lib.other.length) {
      list.append(el('div', { class: 'other' }, `${lib.other.length} damaged or undecrypted clips are not shown (see the “unplayable” folder). `,
        el('button', { class: 'small', onclick: startRepair }, 'Try to repair them')));
    }
    list.append(el('div', { class: 'other' }, 'Ctrl-click minutes to mark them, shift-click to mark a range, then “Save…”.'));
  },

  mark(i, range) {
    if (range && this.lastMarked != null) {
      const [a, b] = [Math.min(i, this.lastMarked), Math.max(i, this.lastMarked)];
      for (let k = a; k <= b; k++) this.marked.add(k);
    } else if (this.marked.has(i)) this.marked.delete(i);
    else this.marked.add(i);
    this.lastMarked = i;
    document.querySelectorAll('.clip-list .minute').forEach((r) => r.classList.toggle('marked', this.marked.has(+r.dataset.i)));
  },

  buildTiles() {
    const box = $('#videos');
    box.innerHTML = '';
    this.tiles = {};
    for (const cam of CAMERAS) {
      const video = el('video', { muted: '', playsinline: '', preload: 'auto' });
      video.muted = true;
      const tile = el('div', { class: 'tile', 'data-cam': cam, onclick: () => this.setFocus(this.focus === cam ? null : cam) },
        video, el('div', { class: 'missing hidden' }, 'no clip'), el('div', { class: 'label' }, CAMERA_LABELS[cam]),
        el('div', { class: 'ended hidden' }, 'Recovered footage of this camera ends here'));
      box.append(tile);
      this.tiles[cam] = { tile, video };
      video.sync = { lastSeek: 0, lastTime: -1, stuckSince: 0, reloads: 0 };
      video.addEventListener('ended', () => { if (video === this.master() && video.ended && !this.stepMode()) this.next(); });
      video.addEventListener('error', () => this.recover(video));
    }
    this.applyLayout();
  },

  cameras() { return this.hw3 ? HW3_CAMERAS : CAMERAS; },

  applyLayout() {
    const box = $('#videos');
    if (this.focus && !this.cameras().includes(this.focus)) this.focus = null;
    box.classList.toggle('grid', !this.focus);
    box.classList.toggle('focus', !!this.focus);
    box.classList.toggle('hw3', !!this.hw3);
    let t = 1;
    for (const cam of CAMERAS) {
      const { tile } = this.tiles[cam];
      tile.classList.toggle('hidden', !this.cameras().includes(cam));
      tile.style.gridArea = this.focus ? (cam === this.focus ? 'main' : 't' + t++) : cam;
    }
  },

  setFocus(cam) {
    this.focus = cam;
    this.applyLayout();
  },

  // The video that drives the clock: the large one, else front, else any tile that has a clip
  // (a minute may only have clips of unknown camera, shown in spare tiles).
  master() {
    const loaded = CAMERAS.filter((c) => this.tiles[c].video.dataset.src && !this.tiles[c].video.sync.dead);
    if (this.focus && loaded.includes(this.focus)) return this.tiles[this.focus].video;
    // The longest clip, so a shorter (e.g. repaired) camera doesn't end the minute early.
    let best = null;
    for (const c of loaded) {
      const v = this.tiles[c].video;
      if (!best || (v.duration || 0) > (best.duration || 0) + 0.5) best = v;
    }
    return best;
  },

  open(i, autoplay) {
    if (i < 0 || i >= this.minutes.length) return;
    this.index = i;
    const m = this.minutes[i];
    this.hw3 = isHW3(m);
    this.applyLayout();
    // Clips of unknown camera ("side") fill empty side tiles so nothing is hidden.
    const extra = Object.keys(m.clips).filter((k) => !CAMERAS.includes(k)).map((k) => m.clips[k]);
    for (const cam of CAMERAS) {
      const { tile, video } = this.tiles[cam];
      let clip = m.clips[cam];
      let label = CAMERA_LABELS[cam];
      if (!clip && cam !== 'front' && extra.length && this.cameras().includes(cam)) { clip = extra.shift(); label = CAMERA_LABELS.side; }
      tile.querySelector('.label').textContent = label;
      tile.querySelector('.missing').textContent = 'no clip';
      tile.querySelector('.missing').classList.toggle('hidden', !!clip);
      const src = clip ? '/media/' + clip.file.split('/').map(encodeURIComponent).join('/') : '';
      if (clip) { video.dataset.file = clip.file; this.loadKeyframes(clip.file); }
      if (video.dataset.src !== src) {
        video.dataset.src = src;
        video.sync = { lastSeek: 0, lastTime: -1, stuckSince: 0, reloads: 0 };
        if (src) video.src = src; else video.removeAttribute('src');
        video.load();
      }
      video.playbackRate = this.stepMode() ? 1 : this.rate;
    }
    this.clock = 0;
    document.querySelectorAll('.clip-list .minute').forEach((r) => {
      const cur = +r.dataset.i === i;
      r.classList.toggle('current', cur);
      if (cur) r.scrollIntoView({ block: 'nearest' });
    });
    this.loadTelemetry(m);
    if (autoplay) this.play(); else this.pause();
  },

  next() { if (this.index + 1 < this.minutes.length) this.open(this.index + 1, true); else this.pause(); },
  prev() { this.open(Math.max(0, this.index - 1), this.playing); },

  videos() { return CAMERAS.map((c) => this.tiles[c].video).filter((v) => v.dataset.src); },

  play() {
    this.playing = true;
    $('#play-btn').textContent = '⏸';
    this.lastTick = performance.now();
    if (this.stepMode()) return;
    for (const v of this.videos()) if (!v.sync.dead) v.play().catch(() => {});
  },

  // A video that reported an error: reload it at the current time (twice at most, then mark the
  // tile as unplayable so the other cameras carry on).
  recover(video) {
    const s = video.sync;
    const tile = video.parentElement;
    if (!video.dataset.src) return;
    if (s.reloads >= 2) {
      s.dead = true;
      tile.querySelector('.missing').textContent = 'can’t play this clip';
      tile.querySelector('.missing').classList.remove('hidden');
      return;
    }
    s.reloads++;
    s.stuckSince = 0;
    const t = this.position();
    video.load();
    video.addEventListener('loadedmetadata', () => {
      video.currentTime = Math.min(t, video.duration || t);
      video.playbackRate = this.stepMode() ? 1 : this.rate;
      if (this.playing && !this.stepMode()) video.play().catch(() => {});
    }, { once: true });
  },
  pause() {
    this.playing = false;
    $('#play-btn').textContent = '▶';
    for (const v of this.videos()) v.pause();
  },
  seek(t) {
    this.clock = t;
    for (const v of this.videos()) {
      v.currentTime = Math.min(t, (v.duration || t));
      v.sync.lastSeek = performance.now();
      v.sync.stuckSince = 0;
    }
  },

  // Keep one camera on the master's time. Small drift is corrected by nudging the playback rate;
  // only large drift seeks, at most once every 1.5 s, and never while the video is still seeking
  // (seeking every frame is what left a camera frozen).
  syncVideo(v, master, t, now) {
    const s = v.sync;
    if (s.dead || v.readyState < 1) return;
    const dur = v.duration || 0;
    if (t >= dur - 0.05) { v.playbackRate = this.rate; return; }   // this camera's clip is shorter
    if (this.playing && v.paused && !v.ended && !v.seeking) v.play().catch(() => {});
    if (v.seeking || v.readyState < 2) return;
    const drift = v.currentTime - t;
    if (!this.playing) {
      if (Math.abs(drift) > 0.05) { v.currentTime = t; s.lastSeek = now; }
      return;
    }
    if (Math.abs(drift) > 1 && now - s.lastSeek > 1500) {
      v.currentTime = t + 0.1 * this.rate;
      s.lastSeek = now;
      v.playbackRate = this.rate;
    } else if (Math.abs(drift) > 0.04) {
      v.playbackRate = this.rate * (drift > 0 ? 0.9 : 1.1);
    } else {
      v.playbackRate = this.rate;
    }
    this.watchdog(v, now);
  },

  // A playing video whose time hasn't moved for 2 s has a stalled decoder (it fell behind).
  // Re-seeking to where it is flushes the decoder; reloading would only add more work.
  watchdog(v, now) {
    const s = v.sync;
    if (!this.playing || v.paused || v.ended || v.seeking) { s.stuckSince = 0; s.lastTime = v.currentTime; return; }
    if (v.currentTime !== s.lastTime) { s.lastTime = v.currentTime; s.stuckSince = 0; return; }
    if (!s.stuckSince) s.stuckSince = now;
    else if (now - s.stuckSince > 2000) {
      s.stuckSince = 0;
      v.currentTime = v.currentTime + 0.001;
    }
  },

  // Fast playback: the videos stay paused; the clock runs at the chosen speed and each camera
  // shows the keyframe at or before it. A keyframe needs no other frame decoded, so this stays
  // light even at 4x, unlike native playback which must decode every frame.
  stepTick(master, now) {
    const dt = Math.min(0.25, (now - this.lastTick) / 1000);
    this.lastTick = now;
    const dur = master.duration || 0;
    if (this.playing && !this.scrubbing) {
      this.clock += dt * this.rate;
      if (dur && this.clock >= dur - 0.05) { this.next(); return; }
    }
    for (const v of this.videos()) {
      if (v.sync.dead || v.readyState < 1 || v.seeking) continue;
      if (!v.paused) v.pause();
      const keys = this.keyframes.get(v.dataset.file);
      let target = this.clock;
      if (keys && keys.length) {
        let lo = 0, hi = keys.length - 1;
        while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (keys[mid] <= this.clock) lo = mid; else hi = mid - 1; }
        target = keys[lo];
      }
      target = Math.min(target, (v.duration || target) - 0.05);
      if (Math.abs(v.currentTime - target) > 0.001) v.currentTime = target;
    }
  },

  // Keep the other cameras on the master's time and update the clock / overlay.
  tick() {
    // Schedule first, so an error in one frame can't stop the clock for good.
    requestAnimationFrame(() => this.tick());
    const master = this.master();
    const m = this.minutes[this.index];
    if (master && m) {
      const now = performance.now();
      if (this.stepMode()) {
        this.stepTick(master, now);
      } else {
        this.lastTick = now;
        for (const v of this.videos()) {
          if (v === master) this.watchdog(v, now);
          else this.syncVideo(v, master, master.currentTime, now);
        }
      }
      if (this.minutes[this.index] !== m) return;   // stepTick moved on to the next minute
      const t = this.position();
      // A camera whose clip is shorter than the minute (usually a repaired clip: only its first
      // part survived) would otherwise just sit on its last picture and look frozen.
      for (const c of CAMERAS) {
        const v = this.tiles[c].video;
        const over = !!v.dataset.src && v.duration > 0 && t > v.duration + 0.2;
        this.tiles[c].tile.querySelector('.ended').classList.toggle('hidden', !over);
      }
      const seek = $('#seek');
      if (master.duration) seek.max = master.duration;
      if (!this.seeking) seek.value = t;
      $('#clock').textContent = new Date((m.t + t) * 1000).toLocaleDateString() + ' ' + fmtTime(m.t + t);
      this.drawTelemetry(t);
    }
  },

  async loadTelemetry(m) {
    const clip = m.clips.front || Object.values(m.clips)[0];
    this.telemetry = null;
    this.telemetryFile = clip && clip.file;
    if (!clip || !$('#tm-toggle').checked) return;
    try {
      const data = await api('/api/telemetry?file=' + encodeURIComponent(clip.file));
      if (this.telemetryFile === clip.file) this.telemetry = data;
    } catch (e) { /* no telemetry for this clip */ }
  },

  drawTelemetry(t) {
    const box = $('#telemetry');
    const d = this.telemetry;
    const on = $('#tm-toggle').checked && d && d.t.length;
    box.classList.toggle('hidden', !on);
    if (!on) return;
    // Sit just above the controls (they wrap to two rows in a narrow window), and above the
    // thumbnail row when one camera is enlarged.
    const thumbs = this.focus ? $('#videos .tile:not(.hidden):not([style*="main"])') : null;
    box.style.bottom = ($('.controls').offsetHeight + (thumbs ? thumbs.offsetHeight : 0) + 12) + 'px';
    // Last frame at or before t.
    let lo = 0, hi = d.t.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (d.t[mid] <= t) lo = mid; else hi = mid - 1; }
    const v = (k) => d[k] ? d[k][lo] : null;
    if (v('speed') == null) { box.classList.add('hidden'); return; }
    $('#tm-speed').textContent = Math.round(v('speed') * 3.6);
    $('#tm-gear').textContent = GEARS[v('gear')] || '?';
    $('#tm-bl').classList.toggle('on', !!v('blinker_left'));
    $('#tm-br').classList.toggle('on', !!v('blinker_right'));
    $('#tm-wheel').style.transform = `rotate(${v('steering')}deg)`;
    $('#tm-steer').textContent = Math.round(v('steering')) + '°';
    $('#tm-acc').style.width = Math.max(0, Math.min(100, v('accelerator'))) + '%';
    $('#tm-brake').classList.toggle('on', !!v('brake'));
    $('#tm-ap').textContent = AUTOPILOT[v('autopilot')] || '';
    const lat = v('lat'), lon = v('lon');
    $('#tm-gps').textContent = lat ? `${lat.toFixed(5)}, ${lon.toFixed(5)} · ${Math.round(v('heading'))}°` : '';
  },
};

$('#play-btn').addEventListener('click', () => viewer.playing ? viewer.pause() : viewer.play());
$('#next-btn').addEventListener('click', () => viewer.next());
$('#prev-btn').addEventListener('click', () => viewer.prev());
$('#seek').addEventListener('input', (e) => { viewer.seeking = true; viewer.scrub(+e.target.value); });
$('#seek').addEventListener('change', () => { viewer.seeking = false; viewer.endScrub(); });
$('#speed').addEventListener('change', (e) => viewer.setRate(+e.target.value));
$('#layout-btn').addEventListener('click', () => viewer.setFocus(viewer.focus ? null : 'front'));
$('#tm-toggle').addEventListener('change', () => { const m = viewer.minutes[viewer.index]; if (m) viewer.loadTelemetry(m); });
$('#save-btn').addEventListener('click', async () => {
  const idx = viewer.marked.size ? [...viewer.marked].sort((a, b) => a - b) : [viewer.index];
  const files = idx.flatMap((i) => Object.values(viewer.minutes[i] ? viewer.minutes[i].clips : {}).map((c) => c.file));
  if (!files.length) return;
  const r = await api('/api/pick-folder', {});
  if (!r.path) return;
  await startJob('save', '/api/save', { files, dest: r.path });
  toast(`Saving ${files.length} clips from ${idx.length} minute(s)…`);
});
app.jobHandlers['save:done'] = (job) => toast(job.status === 'done' ? `Saved ${job.result.saved} clips to ${job.result.dest}` : (job.error || 'Save failed'));

document.addEventListener('keydown', (e) => {
  if (app.step !== 'viewer' || e.target.matches('input, select, textarea')) return;
  if (e.code === 'Space') { e.preventDefault(); viewer.playing ? viewer.pause() : viewer.play(); }
  if (e.code === 'ArrowRight') viewer.seek(viewer.position() + 5);
  if (e.code === 'ArrowLeft') viewer.seek(Math.max(0, viewer.position() - 5));
  if (e.code === 'PageDown' || e.code === 'ArrowDown') { e.preventDefault(); viewer.next(); }
  if (e.code === 'PageUp' || e.code === 'ArrowUp') { e.preventDefault(); viewer.prev(); }
  const n = parseInt(e.key, 10);
  if (n >= 1 && n <= viewer.cameras().length) viewer.setFocus(viewer.cameras()[n - 1]);
  if (e.key === '0' || e.key === 'Escape') viewer.setFocus(null);
});

requestAnimationFrame(() => viewer.tick());

// ---------- start ----------

(async () => {
  await poll();
  const st = app.state || {};
  if (st.scanned) app.clips = await api('/api/clips');
  const step = location.hash.slice(1);
  if (step && document.getElementById('screen-' + step)) show(step);
  else show(st.library ? 'viewer' : 'drive');
})();

window.addEventListener('hashchange', () => {
  const step = location.hash.slice(1);
  if (step !== app.step && document.getElementById('screen-' + step)) show(step);
});
