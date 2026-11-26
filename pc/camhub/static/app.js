'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let status = {cameras: []};
let clipList = [];
let currentDay = null;
let filter = 'all';
let camFilter = '';
let playing = null;

async function api(path, opts = {}) {
  const r = await fetch(path, {credentials: 'same-origin', ...opts,
    headers: {'Content-Type': 'application/json', ...(opts.headers || {})}});
  if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || r.statusText);
  return j;
}

function fmtBytes(n) {
  if (n == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i > 2 ? 1 : 0)} ${u[i]}`;
}
function fmtDur(s) {
  if (s == null) return '';
  s = Math.round(s); const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, '0')}s` : `${s}s`;
}
function fmtDay(d) {
  const today = new Date(); const dt = new Date(d + 'T12:00:00');
  const diff = Math.round((new Date(today.toDateString()) - new Date(dt.toDateString())) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return dt.toLocaleDateString(undefined, {weekday: 'short', day: 'numeric', month: 'short'});
}
function toast(text) {
  const t = document.createElement('div'); t.className = 'toast'; t.textContent = text;
  document.body.appendChild(t); setTimeout(() => t.remove(), 5000);
}

// ---------- tabs ----------
function showTab(name) {
  $$('nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $$('main > section').forEach(s => s.classList.toggle('hidden', s.id !== 'tab-' + name));
  // Only stream live video while the Live tab is open, to save bandwidth on phones.
  $$('#live img').forEach(img => {
    if (name === 'live') img.src = img.dataset.src + '?t=' + Date.now();
    else img.removeAttribute('src');
  });
  if (name !== 'live') $$('#live .cam').forEach(c => c.stopListening && c.stopListening());
  if (name === 'recordings') loadDays();
  if (name === 'settings') loadSettings();
  if (name === 'ai' && window.loadAi) window.loadAi();
  if (name === 'smart' && window.loadSmart) window.loadSmart();
}
$$('nav button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
$('#logout').addEventListener('click', async () => {
  await fetch('/api/logout', {method: 'POST'});
  if (window.HomeCamera && window.HomeCamera.signOut) window.HomeCamera.signOut();
  location.href = '/login';
});

// ---------- live ----------
function buildLive() {
  const wrap = $('#live');
  $$('.cam', wrap).forEach(c => c.stopListening && c.stopListening());
  wrap.innerHTML = '';
  if (!status.cameras.length) {
    wrap.innerHTML = '<div class="card empty">No camera is set up yet. <button class="primary" id="go-settings">Set up a camera</button></div>';
    $('#go-settings').addEventListener('click', () => showTab('settings'));
  }
  for (const c of status.cameras) {
    const el = $('#cam-tpl').content.firstElementChild.cloneNode(true);
    el.dataset.cam = c.id;
    $('.name', el).textContent = c.name;
    const img = $('img', el);
    img.dataset.src = `/api/cameras/${c.id}/live.mjpg`;
    img.src = img.dataset.src;
    img.addEventListener('error', () => {
      $('.msg', el).classList.remove('hidden');
      setTimeout(() => { if (img.getAttribute('src')) img.src = img.dataset.src + '?t=' + Date.now(); }, 3000);
    });
    img.addEventListener('load', () => $('.msg', el).classList.add('hidden'));
    $('.snap', el).addEventListener('click', () => {
      const a = document.createElement('a');
      a.href = `/api/cameras/${c.id}/snapshot.jpg`;
      a.download = `${c.id}-${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
      a.click();
    });
    $('.full', el).addEventListener('click', () => {
      const v = $('.video-wrap', el);
      (v.requestFullscreen || v.webkitRequestFullscreen || (() => {})).call(v);
    });
    setupPtz(el, c.id);
    setupZoom(el, c.id);
    setupListen(el, c.id);
    setupTalk(el, c.id);
    setupPositions(el, c.id);
    wrap.appendChild(el);
  }
  updateLive();
}

function updateLive() {
  for (const c of status.cameras) {
    const el = $(`#live .cam[data-cam="${c.id}"]`);
    if (!el) { buildLive(); return; }
    const pill = $('.state', el);
    pill.className = 'pill state ' + (c.recording ? 'rec' : c.online ? 'ok' : 'off');
    pill.textContent = c.recording ? '● Recording' : c.online ? 'Watching' : 'Offline';
    const why = $('.why', el);
    why.textContent = c.online ? '' : (c.problem || '');
    why.classList.toggle('hidden', c.online || !c.problem);
    $('.ptz', el).classList.toggle('hidden', !c.ptz);
    $('.ptz-speed', el).classList.toggle('hidden', !c.ptz);
    $('.lens', el).classList.toggle('hidden', !c.ptz_zoom);
    $('.talk', el).classList.toggle('hidden', !c.talk);
    $('.positions', el).classList.toggle('hidden', !c.ptz);
    if (c.ptz && el.renderPositions) el.renderPositions(c);
  }
  const s = status.storage;
  if (s) $('#storage').textContent = `Recordings ${fmtBytes(s.recordings_bytes)} · ${fmtBytes(s.disk_free)} free`;
  $('#version').textContent = 'v' + ((status.version || {}).version || 'dev');
}

function setupPtz(el, camId) {
  const speed = () => parseFloat($('.ptz-speed input', el).value);
  const send = (body) => api(`/api/cameras/${camId}/ptz`, {method: 'POST', body: JSON.stringify(body)})
    .catch(e => toast('Pan and tilt: ' + e.message));
  $$('.ptz button[data-d]', el).forEach(b => {
    const [x, y] = b.dataset.d.split(',').map(Number);
    holdButton(b, () => send({action: 'move', pan: x * speed(), tilt: y * speed()}), () => send({action: 'stop'}));
  });
  $('.ptz .home', el).addEventListener('click', () => send({action: 'home'}));
  for (const [cls, dir] of [['.lens-in', 1], ['.lens-out', -1]]) {
    holdButton($(cls, el), () => send({action: 'move', pan: 0, tilt: 0, zoom: dir * speed()}), () => send({action: 'stop'}));
  }
}

// Calls start when a button is pressed and stop when it's let go, for mouse and touch alike.
function holdButton(b, start, stop) {
  let held = false;
  const down = (ev) => {
    ev.preventDefault(); b.setPointerCapture && ev.pointerId != null && b.setPointerCapture(ev.pointerId);
    if (held) return;
    held = true; b.classList.add('held'); start();
  };
  const up = () => { if (!held) return; held = false; b.classList.remove('held'); stop(); };
  b.addEventListener('pointerdown', down);
  b.addEventListener('pointerup', up);
  b.addEventListener('pointercancel', up);
  b.addEventListener('lostpointercapture', up);
  b.addEventListener('contextmenu', ev => ev.preventDefault());
}

// ---------- zoom ----------
// Zooms into the picture itself, which works with every camera. Cameras with a zoom lens
// also get Lens buttons (see setupPtz).
function setupZoom(el, camId) {
  const wrap = $('.video-wrap', el), img = $('img', wrap), level = $('.zoom .zoom-level', el);
  let z = 1, ox = 0, oy = 0;          // scale and offset in pixels
  const apply = () => {
    const w = wrap.clientWidth, h = wrap.clientHeight;
    z = Math.max(1, Math.min(8, z));
    ox = Math.min(0, Math.max(w - w * z, ox));
    oy = Math.min(0, Math.max(h - h * z, oy));
    img.style.transform = z === 1 ? '' : `translate(${ox}px, ${oy}px) scale(${z})`;
    wrap.classList.toggle('zoomed', z > 1);
    level.textContent = (Math.round(z * 10) / 10) + '×';
  };
  const zoomAt = (nz, cx, cy) => {      // keep the point under (cx, cy) still
    nz = Math.max(1, Math.min(8, nz));
    ox = cx - (cx - ox) * nz / z; oy = cy - (cy - oy) * nz / z; z = nz; apply();
  };
  const centre = () => [wrap.clientWidth / 2, wrap.clientHeight / 2];
  $('.zoom-in', el).addEventListener('click', () => zoomAt(z * 1.5, ...centre()));
  $('.zoom-out', el).addEventListener('click', () => zoomAt(z / 1.5, ...centre()));
  level.addEventListener('click', () => { z = 1; apply(); });
  wrap.addEventListener('wheel', ev => {
    ev.preventDefault();
    const r = wrap.getBoundingClientRect();
    zoomAt(z * (ev.deltaY < 0 ? 1.2 : 1 / 1.2), ev.clientX - r.left, ev.clientY - r.top);
  }, {passive: false});
  wrap.addEventListener('dblclick', ev => {
    const r = wrap.getBoundingClientRect();
    if (z > 1) { z = 1; apply(); } else zoomAt(2.5, ev.clientX - r.left, ev.clientY - r.top);
  });
  // Drag to look around when zoomed in; pinch with two fingers to zoom.
  const pts = new Map();
  let pinch = null;
  wrap.addEventListener('pointerdown', ev => {
    wrap.setPointerCapture(ev.pointerId);
    pts.set(ev.pointerId, [ev.clientX, ev.clientY]);
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = {d: Math.hypot(a[0] - b[0], a[1] - b[1]), z};
    }
  });
  wrap.addEventListener('pointermove', ev => {
    const p = pts.get(ev.pointerId);
    if (!p) return;
    const r = wrap.getBoundingClientRect();
    if (pts.size === 2 && pinch) {
      pts.set(ev.pointerId, [ev.clientX, ev.clientY]);
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      zoomAt(pinch.z * d / pinch.d, (a[0] + b[0]) / 2 - r.left, (a[1] + b[1]) / 2 - r.top);
      return;
    }
    if (z > 1) { ox += ev.clientX - p[0]; oy += ev.clientY - p[1]; apply(); }
    pts.set(ev.pointerId, [ev.clientX, ev.clientY]);
  });
  const end = ev => { pts.delete(ev.pointerId); if (pts.size < 2) pinch = null; };
  wrap.addEventListener('pointerup', end);
  wrap.addEventListener('pointercancel', end);
  window.addEventListener('resize', apply);
}

// ---------- saved positions and patrol ----------
function setupPositions(el, camId) {
  const box = $('.positions', el), chips = $('.chips', box), form = $('.add-form', box);
  let shown = '';
  const call = (path, opts) => api(`/api/cameras/${camId}${path}`, opts);
  const render = (c) => {
    const key = JSON.stringify([c.presets, c.patrol, c.patrol_seconds, c.follow_motion]);
    if (key === shown) return;
    shown = key;
    chips.innerHTML = '';
    for (const p of c.presets || []) {
      const b = document.createElement('button');
      b.className = 'chip';
      b.textContent = p.name;
      const x = document.createElement('span'); x.className = 'x'; x.textContent = '✕';
      b.appendChild(x);
      b.addEventListener('click', async () => {
        if (box.classList.contains('editing')) {
          if (!confirm(`Forget the position "${p.name}"?`)) return;
          await call(`/presets/${encodeURIComponent(p.token)}`, {method: 'DELETE'}).catch(e => toast(e.message));
        } else {
          await call(`/presets/${encodeURIComponent(p.token)}/go`, {method: 'POST'}).catch(e => toast('Position: ' + e.message));
        }
        refreshStatus().catch(() => {});
      });
      chips.appendChild(b);
    }
    const n = (c.presets || []).length;
    $('.edit-pos', box).classList.toggle('hidden', !n);
    if (!n) box.classList.remove('editing');
    $('.patrol', box).checked = !!c.patrol;
    $('.patrol', box).disabled = n < 2;
    $('.patrol', box).parentElement.title = n < 2 ? 'Save at least two positions to patrol between them.' : $('.patrol', box).parentElement.title;
    const secs = $('.patrol-secs', box);
    if (![...secs.options].some(o => o.value === String(c.patrol_seconds))) {
      secs.add(new Option(`${c.patrol_seconds} s`, c.patrol_seconds));
    }
    secs.value = String(c.patrol_seconds || 30);
    $('.follow', box).checked = !!c.follow_motion;
  };
  el.renderPositions = render;
  $('.add-pos', box).addEventListener('click', () => {
    form.classList.remove('hidden'); $('.add-pos', box).classList.add('hidden'); $('.pos-name', box).focus();
  });
  const close = () => { form.classList.add('hidden'); $('.add-pos', box).classList.remove('hidden'); $('.pos-name', box).value = ''; };
  $('.cancel-pos', box).addEventListener('click', close);
  const save = async () => {
    const name = $('.pos-name', box).value.trim();
    if (!name) { $('.pos-name', box).focus(); return; }
    try {
      await call('/presets', {method: 'POST', body: JSON.stringify({name})});
      toast(`Saved "${name}". Tap it to turn the camera back there.`);
      close();
      shown = '';
      await refreshStatus();
    } catch (e) { toast('Save position: ' + e.message); }
  };
  $('.save-pos', box).addEventListener('click', save);
  $('.pos-name', box).addEventListener('keydown', ev => { if (ev.key === 'Enter') save(); if (ev.key === 'Escape') close(); });
  $('.edit-pos', box).addEventListener('click', () => {
    box.classList.toggle('editing');
    $('.edit-pos', box).textContent = box.classList.contains('editing') ? 'Done' : 'Edit';
  });
  const patrol = (body) => call('/patrol', {method: 'PUT', body: JSON.stringify(body)})
    .then(() => { shown = ''; return refreshStatus(); }).catch(e => toast(e.message));
  $('.patrol', box).addEventListener('change', ev => patrol({enabled: ev.target.checked}));
  $('.patrol-secs', box).addEventListener('change', ev => patrol({dwell_seconds: parseInt(ev.target.value, 10)}));
  $('.follow', box).addEventListener('change', ev => patrol({follow_motion: ev.target.checked}));
}

// ---------- listen ----------
// The recorder sends plain 16-bit samples; we play them as they arrive to keep the delay short.
function setupListen(el, camId) {
  const b = $('.listen', el);
  let ctl = null;
  const off = () => {
    if (ctl) { ctl.abort(); ctl = null; }
    b.setAttribute('aria-pressed', 'false'); b.textContent = '🔈 Listen';
  };
  b.addEventListener('click', async () => {
    if (ctl) { off(); return; }
    ctl = new AbortController();
    const mine = ctl;
    b.setAttribute('aria-pressed', 'true'); b.textContent = '🔊 Listening…';
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    mine.signal.addEventListener('abort', () => ac.close());
    try {
      const r = await fetch(`/api/cameras/${camId}/listen`, {credentials: 'same-origin', signal: mine.signal});
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || r.statusText);
      }
      b.textContent = '🔊 Stop listening';
      const rate = parseInt(r.headers.get('X-Sample-Rate') || '8000', 10);
      const reader = r.body.getReader();
      let next = 0, spare = null;
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        let bytes = value;
        if (spare) { bytes = new Uint8Array(spare.length + value.length); bytes.set(spare); bytes.set(value, spare.length); spare = null; }
        if (bytes.length % 2) { spare = bytes.slice(-1); bytes = bytes.slice(0, -1); }
        if (!bytes.length) continue;
        const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
        const buf = ac.createBuffer(1, pcm.length, rate);
        const ch = buf.getChannelData(0);
        for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
        const src = ac.createBufferSource(); src.buffer = buf; src.connect(ac.destination);
        // Start a little behind so small hiccups in the network don't cause gaps,
        // and catch up if we've fallen more than a second behind.
        if (next < ac.currentTime || next > ac.currentTime + 1) next = ac.currentTime + 0.25;
        src.start(next); next += buf.duration;
      }
      if (ctl === mine) toast('The sound from the camera stopped.');
    } catch (e) {
      if (e.name !== 'AbortError') toast('Listen: ' + e.message);
    }
    if (ctl === mine) off();
  });
  // Stop when leaving the Live tab.
  document.addEventListener('visibilitychange', () => { if (document.hidden) off(); });
  el.stopListening = off;
}

// ---------- talk ----------
// Hold the button to speak through the camera's speaker. In the phone app the app records
// the microphone itself, because Android only lets secure (https) pages use it.
function setupTalk(el, camId) {
  const b = $('.talk', el);
  let session = null;
  const start = async () => {
    if (window.HomeCamera && window.HomeCamera.startTalk) {
      window.HomeCamera.startTalk(camId);
      session = {native: true};
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('Talk: this browser only allows the microphone on a secure (https) page or on this PC itself. Use the phone app, or open Home Camera at http://localhost:8080 on the PC.');
      return;
    }
    const s = session = {queue: [], stopped: false, seq: 0};
    try {
      s.stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}});
    } catch (e) {
      toast('Talk: the microphone was not allowed (' + e.message + ').');
      session = null; return;
    }
    if (s.stopped) { s.stream.getTracks().forEach(t => t.stop()); return; }
    s.ac = new (window.AudioContext || window.webkitAudioContext)();
    const src = s.ac.createMediaStreamSource(s.stream);
    const proc = s.ac.createScriptProcessor(2048, 1, 1);
    const ratio = s.ac.sampleRate / 8000;
    let pos = 0;
    proc.onaudioprocess = ev => {
      const inp = ev.inputBuffer.getChannelData(0);
      const out = [];
      for (; pos < inp.length; pos += ratio) {
        const v = Math.max(-1, Math.min(1, inp[Math.floor(pos)]));
        out.push(v < 0 ? v * 32768 : v * 32767);
      }
      pos -= inp.length;
      s.queue.push(Int16Array.from(out));
    };
    src.connect(proc); proc.connect(s.ac.destination);
    const flush = async () => {
      if (!s.queue.length) return;
      const n = s.queue.reduce((a, q) => a + q.length, 0);
      const all = new Int16Array(n); let o = 0;
      for (const q of s.queue.splice(0)) { all.set(q, o); o += q.length; }
      try {
        const r = await fetch(`/api/cameras/${camId}/talk?seq=${s.seq++}`, {method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/octet-stream'}, body: all.buffer});
        if (!r.ok && !s.failed) {
          s.failed = true;
          const j = await r.json().catch(() => ({}));
          toast('Talk: ' + (j.detail || r.statusText));
        }
      } catch (e) { /* the next chunk will try again */ }
    };
    s.timer = setInterval(flush, 200);
    s.flush = flush;
  };
  const stop = () => {
    const s = session; session = null;
    if (!s) return;
    if (s.native) { window.HomeCamera.stopTalk(); return; }
    s.stopped = true;
    clearInterval(s.timer);
    if (s.flush) s.flush();
    if (s.stream) s.stream.getTracks().forEach(t => t.stop());
    if (s.ac) s.ac.close();
  };
  holdButton(b, () => { if (!session) start(); }, stop);
}

// ---------- recordings ----------
async function loadDays() {
  const days = await api('/api/days' + (camFilter ? `?camera=${camFilter}` : ''));
  const wrap = $('#days'); wrap.innerHTML = '';
  if (!days.length) { $('#clips').innerHTML = '<div class="empty">No recordings yet. Clips appear here when the camera sees movement.</div>'; return; }
  if (!currentDay || !days.find(d => d.date === currentDay)) currentDay = days[0].date;
  for (const d of days) {
    const b = document.createElement('button');
    b.textContent = `${fmtDay(d.date)} (${d.count})`;
    b.classList.toggle('active', d.date === currentDay);
    b.addEventListener('click', () => { currentDay = d.date; loadDays(); });
    wrap.appendChild(b);
  }
  await loadClips();
}

async function loadClips() {
  const q = new URLSearchParams({date: currentDay}); if (camFilter) q.set('camera', camFilter);
  clipList = await api('/api/clips?' + q);
  renderClips();
}

function visibleClips() { return clipList.filter(c => filter === 'all' || c.person); }

function renderClips() {
  const wrap = $('#clips'); wrap.innerHTML = '';
  const list = visibleClips();
  if (!list.length) { wrap.innerHTML = '<div class="empty">Nothing here for this day.</div>'; return; }
  const multi = status.cameras.length > 1;
  for (const c of list) {
    const b = document.createElement('button'); b.className = 'clip';
    b.classList.toggle('current', playing === c.id);
    const t = new Date(c.start);
    b.innerHTML = `<div class="thumb" style="background-image:url('/api/clips/${c.id}/thumb')">
        <span class="badge ${c.person ? 'person' : ''}">${c.person ? 'Person' : 'Motion'}</span></div>
      <div class="meta"><span>${t.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'})}${multi ? ' · ' + camName(c.camera) : ''}</span>
      <span class="muted">${fmtDur(c.duration)}</span></div>
      ${c.summary ? `<div class="ai-summary" title="Described by AI">${esc(c.summary)}</div>` : ''}
      ${(c.seen || []).length ? `<div class="clip-seen">${c.seen.map(s => `<span class="pill">${esc(s)}</span>`).join(' ')}</div>` : ''}`;
    b.addEventListener('click', () => openClip(c.id));
    wrap.appendChild(b);
  }
}
function camName(id) { const c = status.cameras.find(x => x.id === id); return c ? c.name : id; }

function openClip(id) {
  playing = id;
  const c = clipList.find(x => x.id === id);
  const v = $('#video');
  v.src = `/api/clips/${id}/video`;
  v.play().catch(() => {});
  const when = c ? new Date(c.start) : null;
  $('#player-title').textContent = when
    ? `${camName(c.camera)} · ${when.toLocaleString()} · ${c.summary || (c.person ? 'Person' : 'Motion')}` : id;
  $('#describe').classList.toggle('hidden', !c || !!c.summary || !window.describeClip);
  $('#download').href = `/api/clips/${id}/video?download=1`;
  $('#player').classList.remove('hidden');
  const list = visibleClips(); const i = list.findIndex(x => x.id === id);
  $('#prev').disabled = i <= 0; $('#next').disabled = i < 0 || i >= list.length - 1;
  history.replaceState(null, '', '#clip=' + id);
}
function closeClip() {
  const v = $('#video'); v.pause(); v.removeAttribute('src'); v.load();
  $('#player').classList.add('hidden'); playing = null;
  history.replaceState(null, '', location.pathname);
  renderClips();
}
function step(d) {
  const list = visibleClips(); const i = list.findIndex(x => x.id === playing);
  if (list[i + d]) openClip(list[i + d].id);
}
$('#close').addEventListener('click', closeClip);
$('#prev').addEventListener('click', () => step(-1));
$('#next').addEventListener('click', () => step(1));
$('#delete').addEventListener('click', async () => {
  if (!playing || !confirm('Delete this recording?')) return;
  const id = playing; const list = visibleClips(); const i = list.findIndex(x => x.id === id);
  await api('/api/clips/' + id, {method: 'DELETE'});
  clipList = clipList.filter(x => x.id !== id);
  const nextClip = visibleClips()[Math.min(i, visibleClips().length - 1)];
  if (nextClip) openClip(nextClip.id); else { closeClip(); loadDays(); }
});
document.addEventListener('keydown', (e) => {
  if ($('#player').classList.contains('hidden')) return;
  if (e.key === 'Escape') closeClip();
  if (e.key === 'ArrowLeft' && e.altKey) step(-1);
  if (e.key === 'ArrowRight' && e.altKey) step(1);
});
$$('#filter button').forEach(b => b.addEventListener('click', () => {
  filter = b.dataset.f; $$('#filter button').forEach(x => x.classList.toggle('active', x === b)); renderClips();
}));
$('#cam-filter').addEventListener('change', (e) => { camFilter = e.target.value; currentDay = null; loadDays(); });

// ---------- settings ----------
const FIELDS = [
  ['person_only', 'Only record when a person is seen', 'bool',
    'Ignores cars, animals, trees and rain. Recording still continues while anything moves.'],
  ['sensitivity', 'Motion sensitivity', 'range', 'Higher picks up smaller changes. 1 to 10.', 1, 10, 1],
  ['min_area_percent', 'Minimum movement size (% of picture)', 'number', 'How much of the picture must change.', 0.05, 50, 0.05],
  ['person_confidence', 'Person detection certainty', 'range', 'Higher means fewer false alarms but may miss people far away.', 0.2, 0.95, 0.05],
  ['pre_seconds', 'Seconds before motion to keep', 'number', 'Held in memory, so nothing is written until motion.', 0, 30, 1],
  ['post_seconds', 'Seconds to keep recording after motion stops', 'number', '', 3, 120, 1],
  ['max_clip_minutes', 'Longest single clip (minutes)', 'number', 'Long activity is split into clips of this length.', 1, 60, 1],
];

async function loadSettings() {
  const wrap = $('#settings'); wrap.innerHTML = '';
  wrap.appendChild(await camerasCard());
  for (const c of status.cameras) {
    const s = await api(`/api/cameras/${c.id}/settings`);
    wrap.appendChild(settingsCard(c, s.motion));
    if (window.privacyCard) wrap.appendChild(await window.privacyCard(c));
  }
  if (window.alertCards) for (const card of await window.alertCards()) wrap.appendChild(card);
  if (window.homeLinksCard) wrap.appendChild(await window.homeLinksCard());
  wrap.appendChild(storageCard());
  if (window.backupCard) wrap.appendChild(await backupCard());
  if (window.updatesCard) wrap.appendChild(await window.updatesCard());
  wrap.appendChild(aboutCard());
}

function versionText() {
  const v = (status.version || {}).version || 'dev';
  return v === 'dev' ? 'development copy' : 'version ' + v;
}

function aboutCard() {
  const v = status.version || {};
  const card = document.createElement('div'); card.className = 'card';
  const phone = window.HomeCamera && window.HomeCamera.appVersion ? window.HomeCamera.appVersion() : '';
  const built = v.built ? new Date(v.built) : null;
  card.innerHTML = `<h2>About</h2>
    <p><b>Home Camera ${esc(versionText())}</b> on this PC${built && !isNaN(built) ?
      `, built ${esc(built.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}))}` : ''}.</p>
    ${phone ? `<p>Phone app: version ${esc(phone)}.</p>` : ''}
    <p class="muted">Each new download has a higher version number. Updates install by themselves (see
      Updates above), or run the newest HomeCameraSetup.exe from the
      <a href="https://github.com/alastairstewartduncan/homeawcurity/releases/tag/latest" target="_blank" rel="noopener">download page</a>.
      Your settings and recordings are kept.</p>
    ${v.commit ? `<p class="muted">Build ${esc(v.commit)}</p>` : ''}`;
  return card;
}

function esc(t) { const d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; }

async function camerasCard() {
  const cams = await api('/api/cameras');
  const card = document.createElement('div'); card.className = 'card wide';
  card.innerHTML = `<h2>Cameras</h2>
    <p class="muted" style="margin-top:0">Enter the camera's IP address and login, then press <b>Find and save</b>.
    Home Camera searches the camera for its video and starts using it straight away.
    Find the IP address in the Hiseeu app's device info, or your router's list of devices.</p>`;
  for (const c of cams) card.appendChild(cameraForm(c));
  const add = document.createElement('button'); add.textContent = '+ Add a camera';
  add.addEventListener('click', () => { card.insertBefore(cameraForm(null), add); add.remove(); });
  if (cams.length) card.appendChild(add); else card.appendChild(cameraForm(null));
  return card;
}

function cameraForm(c) {
  const f = document.createElement('div'); f.className = 'cam-form';
  const id = c ? c.id : 'new';
  f.innerHTML = `
    <div class="cam-form-head"><b>${c ? esc(c.name) : 'New camera'}</b>
      <span class="pill ${c && c.configured ? 'ok' : 'off'}">${c ? (c.configured ? 'Set up' : 'Not found yet') : 'New'}</span></div>
    <div class="grid-form">
      <label>Name<input name="name" value="${esc(c ? c.name : 'Front')}" maxlength="40"></label>
      <label>IP address<input name="ip" value="${esc(c ? c.ip : '')}" placeholder="192.168.1.50" inputmode="decimal"></label>
      <label>User name<input name="user" value="${esc(c ? c.user || 'admin' : 'admin')}" autocomplete="off"></label>
      <label>Password<input name="password" type="password" autocomplete="new-password"
        placeholder="${c && c.has_password ? 'Unchanged' : 'Camera password'}"></label>
      <label>ONVIF port<input name="onvif_port" value="${esc(c ? c.onvif_port : 8888)}" inputmode="numeric"></label>
    </div>
    <div class="toolbar"><button class="primary find">Find and save</button>
      ${c && c.configured ? '<button class="diag">Check video</button>' : ''}
      ${c ? '<button class="danger remove">Remove camera</button>' : ''}</div>
    <pre class="find-log hidden"></pre>`;
  const val = (n) => $(`[name=${n}]`, f).value;
  $('.find', f).addEventListener('click', async (ev) => {
    const btn = ev.target; const log = $('.find-log', f);
    btn.disabled = true; btn.textContent = 'Searching… (up to a minute)';
    log.classList.remove('hidden'); log.textContent = 'Looking for the camera…';
    try {
      const r = await api('/api/cameras/setup', {method: 'POST', body: JSON.stringify({
        id: c ? c.id : null, name: val('name'), ip: val('ip'), user: val('user'),
        password: val('password'), onvif_port: val('onvif_port')})});
      log.textContent = r.log.join('\n');
      if (r.ok) {
        toast('Camera saved');
        await refreshStatus(); buildLive();
        setTimeout(() => loadSettings(), 1500);
      }
    } catch (e) { log.textContent = e.message; }
    btn.disabled = false; btn.textContent = 'Find and save';
  });
  const diag = $('.diag', f);
  if (diag) diag.addEventListener('click', async () => {
    const log = $('.find-log', f);
    diag.disabled = true; diag.textContent = 'Checking… (a few minutes)';
    log.classList.remove('hidden');
    log.textContent = 'Trying different ways to read the camera\'s video. The camera pauses while this runs.';
    try {
      const r = await api(`/api/cameras/${c.id}/diagnose`, {method: 'POST'});
      log.textContent = r.log.join('\n');
      try { await navigator.clipboard.writeText(log.textContent); toast('Result copied, ready to paste'); } catch (e) {}
    } catch (e) { log.textContent = e.message; }
    diag.disabled = false; diag.textContent = 'Check video';
  });
  const rm = $('.remove', f);
  if (rm) rm.addEventListener('click', async () => {
    if (!confirm(`Remove ${c.name}? Its recordings are kept.`)) return;
    await api('/api/cameras/' + c.id, {method: 'DELETE'});
    await refreshStatus(); buildLive(); loadSettings();
  });
  return f;
}

function storageCard() {
  const st = status.storage || {};
  const card = document.createElement('div'); card.className = 'card';
  card.innerHTML = `<h2>Storage</h2>
    <p>Recordings use <b>${fmtBytes(st.recordings_bytes)}</b>. The disk has <b>${fmtBytes(st.disk_free)}</b> free of ${fmtBytes(st.disk_total)}.</p>
    <div class="field"><label for="keep-days">Keep recordings for (days)</label>
      <input type="number" id="keep-days" min="0" max="3650" value="${status.retention_days}">
      <div class="hint">0 keeps them until space runs low.</div></div>
    <div class="field"><label for="min-free">Always leave free (GB)</label>
      <input type="number" id="min-free" min="0" max="10000" value="${status.min_free_gb}">
      <div class="hint">The oldest clips are deleted first if free space drops below this.</div></div>
    <button class="primary save-storage">Save</button>
    <p class="muted">Person detection: ${status.person_detection ? 'ready' : 'model not installed, so every motion records'}.</p>`;
  $('.save-storage', card).addEventListener('click', async () => {
    try {
      const r = await api('/api/storage', {method: 'PUT', body: JSON.stringify({
        retention_days: $('#keep-days', card).value, min_free_gb: $('#min-free', card).value})});
      status.retention_days = r.retention_days; status.min_free_gb = r.min_free_gb;
      toast('Storage settings saved');
    } catch (e) { toast('Could not save: ' + e.message); }
  });
  return card;
}

function settingsCard(cam, m) {
  const card = document.createElement('div'); card.className = 'card';
  card.innerHTML = `<h2>Motion: ${esc(cam.name)}</h2>`;
  const inputs = {};
  for (const [key, label, type, hint, min, max, stepv] of FIELDS) {
    const f = document.createElement('div'); f.className = 'field';
    const id = `${cam.id}-${key}`;
    let input;
    if (type === 'bool') {
      input = Object.assign(document.createElement('input'), {type: 'checkbox', id, checked: !!m[key]});
    } else {
      input = Object.assign(document.createElement('input'), {type, id, min, max, step: stepv, value: m[key]});
    }
    const lab = Object.assign(document.createElement('label'), {htmlFor: id, textContent: label});
    f.append(lab);
    if (type === 'range') {
      const out = document.createElement('span'); out.className = 'muted'; out.textContent = m[key];
      input.addEventListener('input', () => out.textContent = input.value);
      f.append(out, input);
    } else f.append(input);
    if (hint) { const h = document.createElement('div'); h.className = 'hint'; h.textContent = hint; f.append(h); }
    inputs[key] = input;
    card.appendChild(f);
  }

  // Ignore zones
  const zones = JSON.parse(JSON.stringify(m.ignore_zones || []));
  let draft = [];
  const zh = document.createElement('div');
  zh.innerHTML = `<h2 style="margin-top:8px">Areas to ignore</h2>
    <p class="muted" style="margin-top:0">Tap around an area (a road, a tree, next door's garden) to outline it, then press Finish area. Movement and people there are ignored.</p>
    <div class="zone-editor"><img alt=""><canvas></canvas></div>
    <div class="zone-list"></div>
    <div class="toolbar"><button class="finish primary">Finish area</button><button class="undo">Undo point</button></div>`;
  card.appendChild(zh);
  const img = $('img', zh); const canvas = $('canvas', zh); const editor = $('.zone-editor', zh);
  img.src = `/api/cameras/${cam.id}/snapshot.jpg?t=${Date.now()}`;
  img.addEventListener('load', () => { editor.style.aspectRatio = `${img.naturalWidth}/${img.naturalHeight}`; draw(); });
  function draw() {
    const r = canvas.getBoundingClientRect();
    canvas.width = r.width * devicePixelRatio; canvas.height = r.height * devicePixelRatio;
    const ctx = canvas.getContext('2d'); const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H); ctx.lineWidth = 2 * devicePixelRatio;
    for (const z of zones) {
      ctx.beginPath(); z.points.forEach(([x, y], i) => i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H));
      ctx.closePath(); ctx.fillStyle = 'rgba(255,77,79,.35)'; ctx.fill(); ctx.strokeStyle = '#ff4d4f'; ctx.stroke();
    }
    if (draft.length) {
      ctx.beginPath(); draft.forEach(([x, y], i) => i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H));
      ctx.strokeStyle = '#4ea1ff'; ctx.stroke();
      for (const [x, y] of draft) { ctx.beginPath(); ctx.arc(x * W, y * H, 4 * devicePixelRatio, 0, 7); ctx.fillStyle = '#4ea1ff'; ctx.fill(); }
    }
    const list = $('.zone-list', zh); list.innerHTML = '';
    zones.forEach((z, i) => {
      const p = document.createElement('span'); p.className = 'pill';
      p.innerHTML = `${esc(z.name)} <button title="Remove">✕</button>`;
      $('button', p).addEventListener('click', () => { zones.splice(i, 1); draw(); });
      list.appendChild(p);
    });
  }
  canvas.addEventListener('pointerdown', (e) => {
    const r = canvas.getBoundingClientRect();
    draft.push([+((e.clientX - r.left) / r.width).toFixed(4), +((e.clientY - r.top) / r.height).toFixed(4)]);
    draw();
  });
  $('.finish', zh).addEventListener('click', () => {
    if (draft.length < 3) { toast('Tap at least 3 points to make an area.'); return; }
    zones.push({name: `Area ${zones.length + 1}`, points: draft}); draft = []; draw();
  });
  $('.undo', zh).addEventListener('click', () => { draft.pop(); draw(); });
  window.addEventListener('resize', draw);

  const save = document.createElement('button'); save.className = 'primary'; save.textContent = 'Save settings';
  save.style.marginTop = '12px';
  save.addEventListener('click', async () => {
    if (draft.length >= 3) { zones.push({name: `Area ${zones.length + 1}`, points: draft}); draft = []; }
    const motion = {ignore_zones: zones};
    for (const [key, , type] of FIELDS) motion[key] = type === 'bool' ? inputs[key].checked : parseFloat(inputs[key].value);
    try { await api(`/api/cameras/${cam.id}/settings`, {method: 'PUT', body: JSON.stringify({motion})}); toast('Settings saved'); draw(); }
    catch (e) { toast('Could not save: ' + e.message); }
  });
  card.appendChild(save);
  return card;
}

// ---------- status and live events ----------
async function refreshStatus() {
  try {
    status = await api('/api/status');
    updateLive();
    if (window.onAlertStatus) window.onAlertStatus(status);
    const sel = $('#cam-filter');
    sel.classList.toggle('hidden', status.cameras.length < 2);
    if (sel.options.length !== status.cameras.length + 1) {
      sel.innerHTML = '<option value="">All cameras</option>' +
        status.cameras.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
    }
  } catch (e) { /* offline: try again shortly */ }
}

function listen() {
  const es = new EventSource('/api/events');
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (e.type === 'clip_started') toast(`${e.camera_name}: ${e.person ? 'person seen' : 'movement'}, recording`);
    if (e.type === 'person') toast(`${e.camera_name}: person seen`);
    if (e.type === 'backup') toast(e.ok ? e.message : 'Off-site backup problem: ' + e.message);
    if (e.type === 'clip_described') {
      const c = clipList.find(x => x.id === e.clip);
      if (c) { c.summary = e.summary; c.tags = e.tags; if (!playing) renderClips(); }
    }
    if (e.type === 'ai_digest') toast('Morning digest: ' + (e.text || '').split('\n')[0]);
    if (e.type === 'ai_alert') {
      toast(`${e.camera_name} · ${e.agent_name}: ${e.message}`);
      if (window.refreshAiActivity && !$('#tab-ai').classList.contains('hidden')) window.refreshAiActivity();
    }
    refreshStatus();
    if (window.onAlertEvent) window.onAlertEvent(e);
    if (e.smart && window.onSmartEvent) window.onSmartEvent(e);
    if (e.type === 'clip_saved' && !$('#tab-recordings').classList.contains('hidden') && !playing) loadDays();
  };
}

(async function init() {
  await refreshStatus();
  buildLive();
  listen();
  setInterval(refreshStatus, 15000);
  const m = location.hash.match(/clip=([\w-]+)/);
  if (m) {
    const id = m[1]; const d = id.split('_')[1];
    currentDay = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
    showTab('recordings');
    await loadDays();
    openClip(id);
  }
})();
