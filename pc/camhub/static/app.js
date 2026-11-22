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
  if (name === 'recordings') loadDays();
  if (name === 'settings') loadSettings();
}
$$('nav button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
$('#logout').addEventListener('click', async () => {
  await fetch('/api/logout', {method: 'POST'});
  if (window.HomeCamera && window.HomeCamera.signOut) window.HomeCamera.signOut();
  location.href = '/login';
});

// ---------- live ----------
function buildLive() {
  const wrap = $('#live'); wrap.innerHTML = '';
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
    $('.ptz', el).classList.toggle('hidden', !c.ptz);
    $('.ptz-speed', el).classList.toggle('hidden', !c.ptz);
  }
  const s = status.storage;
  if (s) $('#storage').textContent = `Recordings ${fmtBytes(s.recordings_bytes)} · ${fmtBytes(s.disk_free)} free`;
}

function setupPtz(el, camId) {
  let moving = false;
  const speed = () => parseFloat($('.ptz-speed input', el).value);
  const send = (body) => api(`/api/cameras/${camId}/ptz`, {method: 'POST', body: JSON.stringify(body)})
    .catch(e => toast('Pan and tilt: ' + e.message));
  $$('.ptz button', el).forEach(b => {
    const [x, y] = b.dataset.d.split(',').map(Number);
    const start = (ev) => {
      ev.preventDefault(); b.setPointerCapture && ev.pointerId != null && b.setPointerCapture(ev.pointerId);
      moving = true; b.classList.add('held');
      send({action: 'move', pan: x * speed(), tilt: y * speed()});
    };
    const stop = () => {
      if (!moving) return; moving = false; b.classList.remove('held');
      send({action: 'stop'});
    };
    b.addEventListener('pointerdown', start);
    b.addEventListener('pointerup', stop);
    b.addEventListener('pointercancel', stop);
    b.addEventListener('lostpointercapture', stop);
  });
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
      <span class="muted">${fmtDur(c.duration)}</span></div>`;
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
    ? `${camName(c.camera)} · ${when.toLocaleString()} · ${c.person ? 'Person' : 'Motion'}` : id;
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
  for (const c of status.cameras) {
    const s = await api(`/api/cameras/${c.id}/settings`);
    wrap.appendChild(settingsCard(c, s.motion));
  }
  const st = status.storage || {};
  const info = document.createElement('div'); info.className = 'card';
  info.innerHTML = `<h2>Storage</h2>
    <p>Recordings use <b>${fmtBytes(st.recordings_bytes)}</b>. The disk has <b>${fmtBytes(st.disk_free)}</b> free of ${fmtBytes(st.disk_total)}.</p>
    <p class="muted">Clips older than ${status.retention_days} days are deleted automatically, and the oldest clips go first if free space drops below ${status.min_free_gb} GB. Change these in config.json.</p>
    <p class="muted">Person detection: ${status.cameras[0] && status.cameras[0].person_detection ? 'ready' : 'model not installed, so every motion records'}.</p>`;
  wrap.appendChild(info);
}

function settingsCard(cam, m) {
  const card = document.createElement('div'); card.className = 'card';
  card.innerHTML = `<h2>${cam.name}</h2>`;
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
      p.innerHTML = `${z.name} <button title="Remove">✕</button>`;
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
    refreshStatus();
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
