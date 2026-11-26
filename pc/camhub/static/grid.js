'use strict';
// Camera grid: every camera side by side on the Live tab, with one combined timeline below.
// The timeline has a row per camera. Dragging back plays every camera's recordings from that
// moment together, so you can follow someone from the drive to the back door. Gaps where no
// camera recorded anything are skipped.
(function () {
  const HOUR = 3600e3;
  const ZOOMS = [24, 6, 1];
  const RATES = [1, 2, 4];
  const COLORS = ['#4ea1ff', '#3ccf91', '#c084fc', '#f472b6', '#22d3ee', '#a3e635', '#fb923c', '#e2e8f0'];
  const pad = (n) => String(n).padStart(2, '0');
  const dayOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const today = () => dayOf(new Date());
  const dayStart = (day) => new Date(day + 'T00:00:00').getTime();
  const hm = (t) => { const d = new Date(t); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const hms = (t) => `${hm(t)}:${pad(new Date(t).getSeconds())}`;
  const q = (s, el = document) => el.querySelector(s);
  const qa = (s, el = document) => [...el.querySelectorAll(s)];
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} return null; };

  const live = document.getElementById('live');
  const tab = document.getElementById('tab-live');

  // ---------- the bar above the cameras: Each camera | Grid, and TV ----------
  const bar = document.createElement('div');
  bar.className = 'toolbar live-bar';
  bar.innerHTML = `<div class="seg view-seg hidden" title="How to show the cameras">
      <button data-v="each">Each camera</button><button data-v="grid">Grid</button></div>
    <div style="flex:1"></div>
    <button class="tv-btn" title="Show the cameras on a Chromecast or smart TV">📺 TV</button>`;
  tab.insertBefore(bar, live);

  const view = document.createElement('div');
  view.id = 'grid-view';
  view.className = 'hidden';
  view.innerHTML = `<div class="gv-tiles"></div>
    <div class="gv-tl">
      <div class="tl-row">
        <button class="tl-prev" aria-label="Earlier day" title="Earlier day">‹</button>
        <span class="tl-day"></span>
        <button class="tl-next" aria-label="Later day" title="Later day">›</button>
        <button class="gv-play hidden" title="Pause or play">❚❚</button>
        <div class="seg gv-rate hidden" title="Playback speed">${RATES.map(r => `<button data-r="${r}">${r}×</button>`).join('')}</div>
        <span class="tl-info"></span>
        <div class="seg tl-zoom" title="How much of the day the bar shows">${ZOOMS.map(h => `<button data-h="${h}">${h}h</button>`).join('')}</div>
        <button class="tl-live primary hidden">● Back to live</button>
      </div>
      <div class="tl-bar gv-bar" tabindex="0" role="slider" aria-label="Recordings from all cameras. Drag to go back in time.">
        <div class="gv-lanes"></div>
        <div class="tl-future"></div>
        <div class="tl-cursor hidden"></div>
        <div class="tl-head hidden"></div>
        <div class="tl-bubble hidden"><span></span></div>
      </div>
      <div class="tl-ticks"></div>
    </div>`;
  live.after(view);

  const s = {
    on: false, day: today(), days: [], clips: [], hours: 24, center: null,
    head: null, playing: false, rate: 1, loadedAt: 0, dragging: false, lastTick: 0,
  };
  const tiles = q('.gv-tiles', view);
  const lanes = q('.gv-lanes', view);
  const tlBar = q('.gv-bar', view);

  const cams = () => (typeof status === 'object' && status && status.cameras) || [];
  const colorOf = (id) => COLORS[Math.max(0, cams().findIndex(c => c.id === id)) % COLORS.length];
  const liveTabOpen = () => !tab.classList.contains('hidden');

  // ---------- switching between Each camera and Grid ----------
  function setMode(grid) {
    s.on = grid;
    store('homecamera.view', grid ? 'grid' : 'each');
    document.body.classList.toggle('grid-mode', grid);
    qa('.view-seg button', bar).forEach(b => b.classList.toggle('active', (b.dataset.v === 'grid') === grid));
    live.classList.toggle('hidden', grid);
    view.classList.toggle('hidden', !grid);
    if (grid) {
      // Stop the single-camera pictures and recordings so the grid has the bandwidth.
      qa('#live .cam').forEach(c => c.stopListening && c.stopListening());
      qa('#live img').forEach(img => img.removeAttribute('src'));
      qa('#live video').forEach(v => v.pause());
      build();
      load();
    } else {
      stopAll();
      qa('.gv-tile img', view).forEach(img => img.removeAttribute('src'));
      if (liveTabOpen()) qa('#live .video-wrap:not(.hidden) img').forEach(img => { img.src = img.dataset.src + '?t=' + Date.now(); });
    }
  }
  qa('.view-seg button', bar).forEach(b => b.addEventListener('click', () => setMode(b.dataset.v === 'grid')));
  q('.tv-btn', bar).addEventListener('click', () => {
    if (window.HomeCamera && window.HomeCamera.castMedia) pickTvSource();
    else if (window.tvHelp) window.tvHelp();
  });

  function pickTvSource() {
    const list = cams();
    if (list.length < 2) { window.castTo('live', list[0] ? list[0].id : 'all'); return; }
    const box = document.createElement('div');
    box.className = 'player ns-help';
    box.innerHTML = `<div class="card" style="max-width:420px;margin:auto"><h2>Show on TV</h2><div class="gv-pick"></div>
      <div class="toolbar" style="margin-top:12px"><button>Cancel</button></div></div>`;
    const pick = q('.gv-pick', box);
    for (const [id, name] of [['all', 'All cameras together'], ...list.map(c => [c.id, c.name])]) {
      const b = document.createElement('button');
      b.textContent = name;
      b.addEventListener('click', () => { box.remove(); window.castTo('live', id, name); });
      pick.appendChild(b);
    }
    q('.toolbar button', box).addEventListener('click', () => box.remove());
    document.body.appendChild(box);
  }

  // ---------- tiles ----------
  function build() {
    const list = cams();
    const key = list.map(c => c.id).join(',');
    if (tiles.dataset.key === key) { updateTiles(); return; }
    stopAll();
    tiles.dataset.key = key;
    tiles.innerHTML = '';
    const cols = Math.max(1, Math.min(list.length, list.length <= 4 ? 2 : 3));
    tiles.style.setProperty('--cols', cols);
    tiles.style.setProperty('--rows', Math.max(1, Math.ceil(list.length / cols)));
    for (const c of list) {
      const t = document.createElement('div');
      t.className = 'gv-tile';
      t.dataset.cam = c.id;
      t.innerHTML = `<img alt=""><video playsinline muted preload="auto" class="hidden"></video>
        <div class="gv-tag"><span class="gv-dot"></span><span class="gv-name"></span><span class="gv-time"></span></div>
        <div class="gv-msg hidden"></div>
        <button class="gv-full" title="Full screen" aria-label="Full screen">⤢</button>`;
      q('.gv-name', t).textContent = c.name;
      q('.gv-dot', t).style.background = colorOf(c.id);
      const img = q('img', t);
      img.dataset.src = `/api/cameras/${encodeURIComponent(c.id)}/live.mjpg`;
      img.addEventListener('error', () => setTimeout(() => { if (img.getAttribute('src')) img.src = img.dataset.src + '?t=' + Date.now(); }, 3000));
      q('.gv-full', t).addEventListener('click', () => (t.requestFullscreen || t.webkitRequestFullscreen || (() => {})).call(t));
      t.addEventListener('dblclick', () => (t.requestFullscreen || t.webkitRequestFullscreen || (() => {})).call(t));
      tiles.appendChild(t);
    }
    if (s.head == null) showLive();
    updateTiles();
  }

  function updateTiles() {
    for (const c of cams()) {
      const t = q(`.gv-tile[data-cam="${c.id}"]`, tiles);
      if (!t) continue;
      t.classList.toggle('rec', !!c.recording);
      t.classList.toggle('off', !c.online);
      if (s.head == null) {
        const m = q('.gv-msg', t);
        m.textContent = c.online ? '' : (c.problem || 'Offline');
        m.classList.toggle('hidden', !!c.online);
      }
    }
  }

  function showLive() {
    for (const t of qa('.gv-tile', tiles)) {
      const v = q('video', t), img = q('img', t);
      stopVideo(t);
      v.classList.add('hidden');
      img.classList.remove('hidden');
      if (liveTabOpen() && s.on) img.src = img.dataset.src + '?t=' + Date.now();
      q('.gv-time', t).textContent = '';
      q('.gv-msg', t).classList.add('hidden');
    }
    updateTiles();
  }

  function stopVideo(t) {
    const v = q('video', t);
    if (v.getAttribute('src')) { v.pause(); v.removeAttribute('src'); v.load(); }
    t.dataset.clip = '';
  }
  function stopAll() { qa('.gv-tile', tiles).forEach(stopVideo); s.playing = false; }

  // ---------- recordings for the day, all cameras ----------
  async function load(keepView) {
    try {
      const [days, clips] = await Promise.all([api('/api/days'), api('/api/clips?date=' + s.day)]);
      s.days = days.map(d => d.date);
      s.clips = clips.map(c => {
        const t = new Date(c.start).getTime();
        return {...c, t, end: t + 1000 * (c.duration || 30)};
      }).filter(c => !isNaN(c.t)).sort((a, b) => a.t - b.t);
      s.loadedAt = Date.now();
    } catch (e) { info('Couldn\'t load the recordings.'); }
    if (!keepView && s.hours < 24 && s.center == null) s.center = defaultCenter();
    render();
  }

  function defaultCenter() {
    if (s.day === today()) return Date.now() - s.hours * HOUR / 4;
    const last = s.clips[s.clips.length - 1];
    return last ? last.t : dayStart(s.day) + 12 * HOUR;
  }

  function stepDay(dir) {
    const list = [...new Set([...s.days, today()])].sort();
    const i = list.indexOf(s.day);
    const next = i < 0 ? (dir < 0 ? list.filter(d => d < s.day).pop() : list.find(d => d > s.day)) : list[i + dir];
    if (!next) return;
    s.day = next; s.center = null; s.clips = [];
    if (s.head != null) goLive(true);
    load();
  }

  function viewRange() {
    const d0 = dayStart(s.day), d1 = d0 + 24 * HOUR;
    if (s.hours >= 24) return [d0, d1];
    const span = s.hours * HOUR;
    const c = s.center != null ? s.center : defaultCenter();
    const a = Math.max(d0, Math.min(d1 - span, c - span / 2));
    return [a, a + span];
  }

  function timeAt(clientX) {
    const r = tlBar.getBoundingClientRect();
    const [a, b] = viewRange();
    return a + Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * (b - a);
  }

  const clipAt = (cam, t) => s.clips.find(c => c.camera === cam && t >= c.t && t < c.end);
  const anyAt = (t) => s.clips.some(c => t >= c.t && t < c.end);

  // ---------- drawing the combined timeline ----------
  function render() {
    if (!s.on) return;
    const [a, b] = viewRange();
    const span = b - a;
    const pct = (t) => `${(100 * (t - a) / span).toFixed(3)}%`;
    const isToday = s.day === today();
    const list = cams();
    const n = Math.max(1, list.length);
    tlBar.style.height = `${Math.max(40, n * 14 + 12)}px`;

    q('.tl-day', view).textContent = fmtDay(s.day);
    q('.tl-live', view).classList.toggle('hidden', s.head == null && isToday);
    q('.tl-next', view).disabled = s.day >= today();
    q('.tl-prev', view).disabled = !s.days.some(d => d < s.day);
    q('.gv-play', view).classList.toggle('hidden', s.head == null);
    q('.gv-rate', view).classList.toggle('hidden', s.head == null);
    q('.gv-play', view).textContent = s.playing ? '❚❚' : '▶';
    qa('.gv-rate button', view).forEach(x => x.classList.toggle('active', +x.dataset.r === s.rate));
    qa('.tl-zoom button', view).forEach(x => x.classList.toggle('active', +x.dataset.h === s.hours));

    const parts = [];
    list.forEach((cam, row) => {
      const top = `calc(6px + ${row} * (100% - 12px) / ${n})`;
      const h = `calc((100% - 12px) / ${n} - 2px)`;
      const color = colorOf(cam.id);
      for (const c of s.clips) {
        if (c.camera !== cam.id || c.end < a || c.t > b) continue;
        const l = Math.max(a, c.t), r = Math.min(b, c.end);
        parts.push(`<div class="tl-mark gv-mark${c.person ? ' person' : ''}" title="${cam.name.replace(/"/g, '')}"
          style="top:${top};height:${h};left:${pct(l)};width:${(100 * (r - l) / span).toFixed(3)}%;background:${color}"></div>`);
      }
      if (isToday && cam.recording && cam.recording_clip) {
        const m = /_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(cam.recording_clip);
        const t0 = m ? new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`).getTime() : null;
        if (t0 && t0 < b) {
          const l = Math.max(a, t0), r = Math.min(b, Date.now());
          if (r > l) parts.push(`<div class="tl-mark rec" style="top:${top};height:${h};left:${pct(l)};width:${(100 * (r - l) / span).toFixed(3)}%"></div>`);
        }
      }
    });
    lanes.innerHTML = parts.join('');

    const fut = q('.tl-future', view);
    if (isToday && Date.now() < b) { fut.style.left = pct(Math.max(a, Date.now())); fut.classList.remove('hidden'); }
    else fut.classList.add('hidden');

    const w = tlBar.clientWidth || 320;
    const step = [5, 10, 15, 30, 60, 120, 180, 240, 360].map(m => m * 60e3).find(x => (x / span) * w >= 56) || 6 * HOUR;
    const d0 = dayStart(s.day);
    const ticks = [];
    for (let t = d0 + Math.ceil((a - d0) / step) * step; t <= b; t += step) {
      const left = 100 * (t - a) / span;
      if (left > 97) break;
      ticks.push(`<span style="left:${left.toFixed(2)}%">${hm(t)}</span>`);
    }
    q('.tl-ticks', view).innerHTML = ticks.join('');
    drawHead();
    if (s.head == null) {
      const k = s.clips.length;
      info(k ? `${k} recording${k === 1 ? '' : 's'} from ${new Set(s.clips.map(c => c.camera)).size} camera${new Set(s.clips.map(c => c.camera)).size === 1 ? '' : 's'}`
        : (isToday ? 'Nothing recorded yet today' : 'No recordings'));
    }
  }

  function drawHead() {
    const head = q('.tl-head', view);
    if (s.head == null) { head.classList.add('hidden'); return; }
    const [a, b] = viewRange();
    if (s.head < a || s.head > b) {
      if (s.hours < 24 && !s.dragging) { s.center = s.head; render(); return; }
      head.classList.add('hidden'); return;
    }
    head.style.left = `${(100 * (s.head - a) / (b - a)).toFixed(3)}%`;
    head.classList.remove('hidden');
  }

  function info(text) { q('.tl-info', view).textContent = text || ''; }

  // ---------- dragging and tapping the bar ----------
  const bubble = q('.tl-bubble', view), cursor = q('.tl-cursor', view);
  function showBubble(clientX) {
    const t = timeAt(clientX);
    const r = tlBar.getBoundingClientRect();
    const x = Math.max(0, Math.min(r.width, clientX - r.left));
    const here = cams().filter(c => clipAt(c.id, t)).map(c => c.name);
    const next = s.clips.find(k => k.t > t);
    q('span', bubble).textContent = here.length ? `${hms(t)} · ${here.join(', ')}`
      : next ? `${hm(t)} · nothing recorded, next at ${hm(next.t)}` : `${hm(t)} · nothing recorded`;
    bubble.style.left = `${x}px`;
    bubble.classList.toggle('flip', x > r.width - 90);
    bubble.classList.toggle('flop', x < 90);
    bubble.classList.remove('hidden');
    cursor.style.left = `${x}px`;
    cursor.classList.remove('hidden');
    return t;
  }
  const hideBubble = () => { bubble.classList.add('hidden'); cursor.classList.add('hidden'); };
  tlBar.addEventListener('pointerdown', (e) => {
    if (e.button) return;
    e.preventDefault();
    tlBar.setPointerCapture && tlBar.setPointerCapture(e.pointerId);
    s.dragging = true;
    s.head = showBubble(e.clientX); drawHead();
  });
  tlBar.addEventListener('pointermove', (e) => {
    if (s.dragging) { s.head = showBubble(e.clientX); drawHead(); }
    else if (e.pointerType === 'mouse') showBubble(e.clientX);
  });
  tlBar.addEventListener('pointerup', (e) => {
    if (!s.dragging) return;
    s.dragging = false;
    hideBubble();
    const [a, b] = viewRange();
    seek(timeAt(e.clientX), 12 * (b - a) / Math.max(1, tlBar.clientWidth));
  });
  tlBar.addEventListener('pointercancel', () => { s.dragging = false; hideBubble(); drawHead(); });
  tlBar.addEventListener('pointerleave', (e) => { if (!s.dragging && e.pointerType === 'mouse') hideBubble(); });
  tlBar.addEventListener('wheel', (e) => {
    e.preventDefault();
    const i = ZOOMS.indexOf(s.hours);
    const j = Math.max(0, Math.min(ZOOMS.length - 1, i + (e.deltaY < 0 ? 1 : -1)));
    if (j !== i) { s.hours = ZOOMS[j]; s.center = timeAt(e.clientX); render(); }
  }, {passive: false});
  tlBar.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const at = s.head != null ? s.head : Date.now();
    const c = e.key === 'ArrowLeft' ? [...s.clips].reverse().find(k => k.t < at - 1000) : s.clips.find(k => k.t > at);
    if (c) seek(c.t + 1);
  });

  q('.tl-prev', view).addEventListener('click', () => stepDay(-1));
  q('.tl-next', view).addEventListener('click', () => stepDay(1));
  q('.tl-live', view).addEventListener('click', () => goLive());
  qa('.tl-zoom button', view).forEach(b => b.addEventListener('click', () => {
    s.hours = +b.dataset.h; s.center = s.head != null ? s.head : (s.center != null ? s.center : defaultCenter()); render();
  }));
  q('.gv-play', view).addEventListener('click', () => { s.playing = !s.playing; s.lastTick = performance.now(); sync(); render(); });
  qa('.gv-rate button', view).forEach(b => b.addEventListener('click', () => { s.rate = +b.dataset.r; sync(); render(); }));

  // ---------- playing every camera together ----------
  function seek(t, slack) {
    if (!anyAt(t) && slack) {
      let best = null, gap = slack;
      for (const k of s.clips) {
        const d = t < k.t ? k.t - t : t - k.end;
        if (d <= gap) { best = k; gap = d; }
      }
      if (best) t = best.t;
    }
    if (!anyAt(t)) {
      const next = s.clips.find(k => k.t > t);
      if (next) { info(`Nothing recorded at ${hm(t)}, playing ${hm(next.t)}`); t = next.t; }
      else if (s.day === today()) { goLive(); info('Nothing recorded after that, so here\'s the live picture'); return; }
      else { s.head = null; drawHead(); info('Nothing recorded after that on this day'); return; }
    } else info(`Playing ${hms(t)}`);
    s.head = t;
    if (s.hours < 24) s.center = t;
    s.playing = true;
    s.lastTick = performance.now();
    qa('.gv-tile img', tiles).forEach(img => { img.removeAttribute('src'); img.classList.add('hidden'); });
    sync(true);
    render();
  }

  function goLive(keepDay) {
    s.head = null; s.playing = false;
    stopAll();
    showLive();
    if (!keepDay && s.day !== today()) { s.day = today(); s.center = null; load(); } else render();
  }

  // Puts each tile on the recording (and moment) the timeline is at.
  function sync(force) {
    if (s.head == null) return;
    for (const t of qa('.gv-tile', tiles)) {
      const cam = t.dataset.cam;
      const c = clipAt(cam, s.head);
      const v = q('video', t), msg = q('.gv-msg', t);
      q('.gv-time', t).textContent = ' ' + hms(s.head);
      if (!c) {
        stopVideo(t);
        v.classList.add('hidden');
        msg.textContent = 'No recording at this time';
        msg.classList.remove('hidden');
        continue;
      }
      msg.classList.add('hidden');
      v.classList.remove('hidden');
      const want = (s.head - c.t) / 1000;
      if (t.dataset.clip !== c.id) {
        t.dataset.clip = c.id;
        v.src = `/api/clips/${c.id}/video`;
        v.addEventListener('loadedmetadata', function once() {
          v.removeEventListener('loadedmetadata', once);
          const now = s.head != null ? (s.head - c.t) / 1000 : want;
          if (now > 0 && now < (v.duration || Infinity)) v.currentTime = now;
          v.playbackRate = s.rate;
          if (s.playing) v.play().catch(() => {});
        });
        continue;
      }
      if (v.playbackRate !== s.rate) v.playbackRate = s.rate;
      if (v.readyState >= 1 && (force || Math.abs(v.currentTime - want) > 1.5 * s.rate)) v.currentTime = want;
      if (s.playing && v.paused && !v.ended) v.play().catch(() => {});
      if (!s.playing && !v.paused) v.pause();
    }
  }

  // The shared clock: moves on in real time (times the speed) while every visible video keeps up.
  function tick() {
    const now = performance.now();
    const dt = now - (s.lastTick || now);
    s.lastTick = now;
    if (!s.on || s.head == null || s.dragging || !s.playing || !liveTabOpen()) return;
    const waiting = qa('.gv-tile video', tiles).some(v => v.getAttribute('src') && !v.classList.contains('hidden')
      && (v.readyState < 3 || v.seeking) && !v.ended);
    if (!waiting) s.head += Math.min(dt, 1000) * s.rate;
    if (!anyAt(s.head)) {
      const next = s.clips.find(k => k.t > s.head);
      if (next) { s.head = next.t; info(`Skipped to the next recording at ${hm(next.t)}`); }
      else if (s.day === today()) { goLive(); info('That was the latest recording. Back to live.'); return; }
      else { s.playing = false; info('That was the last recording of the day'); render(); return; }
    }
    sync();
    drawHead();
  }
  setInterval(tick, 250);

  // ---------- keeping up to date ----------
  setInterval(() => {
    if (!s.on || !liveTabOpen()) return;
    build();
    if (s.day === today() && !s.dragging && s.head == null && Date.now() - s.loadedAt > 30e3) load(true);
    else if (!s.dragging) render();
  }, 5000);

  // Leaving the Live tab stops the pictures; coming back starts them again.
  new MutationObserver(() => {
    if (!s.on) return;
    if (!liveTabOpen()) {
      qa('.gv-tile img', tiles).forEach(img => img.removeAttribute('src'));
      qa('.gv-tile video', tiles).forEach(v => v.pause());
      // showTab() restarts the single-camera pictures; keep them off while the grid is showing.
    } else {
      qa('#live img').forEach(img => img.removeAttribute('src'));
      if (s.head == null) showLive(); else sync(true);
    }
  }).observe(tab, {attributes: true, attributeFilter: ['class']});

  // Shows the Each camera / Grid choice once there's more than one camera.
  function onStatus() {
    const many = cams().length > 1;
    q('.view-seg', bar).classList.toggle('hidden', !many);
    if (!many && s.on) setMode(false);
    else if (many && !s.on && store('homecamera.view') === 'grid' && !s.tried) { s.tried = true; setMode(true); }
    if (s.on) updateTiles();
  }
  const prev = window.onAlertStatus;
  window.onAlertStatus = (st) => { if (prev) prev(st); onStatus(); };
  window.gridActive = () => s.on;
})();
