'use strict';
// Timeline under each live picture: drag back through the day's recordings.
// Recordings are only made when something moves, so the bar shows each clip as a mark with gaps
// between them. Dropping the handle in a gap plays the next recording after that time.
(function () {
  const HOUR = 3600e3;
  const ZOOMS = [24, 6, 1];
  const q = (s, el) => el.querySelector(s);
  const pad = (n) => String(n).padStart(2, '0');
  const dayOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const today = () => dayOf(new Date());
  const dayStart = (day) => new Date(day + 'T00:00:00').getTime();
  const hm = (t) => { const d = new Date(t); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const hms = (t) => { const d = new Date(t); return `${hm(t)}:${pad(d.getSeconds())}`; };
  const getJson = (path) => (typeof api === 'function' ? api(path) : fetch(path).then(r => r.json()));
  const camStatus = (id) => ((typeof status === 'object' && status && status.cameras) || []).find(c => c.id === id) || {};
  const dayLabel = (day) => (typeof fmtDay === 'function' ? fmtDay(day) : day);
  const all = [];

  function idTime(clipId) {
    // "<camera>_<YYYYMMDD>_<HHMMSS>" → time in ms
    const m = /_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(clipId || '');
    return m ? new Date(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}`).getTime() : null;
  }

  function setup(el) {
    const camId = el.dataset.cam;
    const wrap = q('.video-wrap', el);
    if (!camId || !wrap || q('.tl', el)) return;

    const player = document.createElement('div');
    player.className = 'tl-player hidden';
    player.innerHTML = `<video controls playsinline preload="auto"></video>
      <div class="tl-label"></div>`;
    wrap.after(player);

    const box = document.createElement('div');
    box.className = 'tl';
    box.innerHTML = `
      <div class="tl-row">
        <button class="tl-prev" aria-label="Earlier day" title="Earlier day">‹</button>
        <span class="tl-day"></span>
        <button class="tl-next" aria-label="Later day" title="Later day">›</button>
        <span class="tl-info"></span>
        <div class="seg tl-zoom" title="How much of the day the bar shows">
          ${ZOOMS.map(h => `<button data-h="${h}">${h}h</button>`).join('')}
        </div>
        <button class="tl-live primary hidden">● Back to live</button>
      </div>
      <div class="tl-bar" tabindex="0" role="slider" aria-label="Recordings timeline. Drag to go back in time.">
        <div class="tl-marks"></div>
        <div class="tl-future"></div>
        <div class="tl-cursor hidden"></div>
        <div class="tl-head hidden"></div>
        <div class="tl-bubble hidden"><div class="tl-thumb"></div><span></span></div>
      </div>
      <div class="tl-ticks"></div>`;
    player.after(box);

    const s = {
      camId, el, wrap, player, box, video: q('video', player), bar: q('.tl-bar', box),
      day: today(), days: [], clips: [], hours: 24, center: null,
      playing: null, head: null, loadedAt: 0,
    };
    all.push(s);

    q('.tl-prev', box).addEventListener('click', () => stepDay(s, -1));
    q('.tl-next', box).addEventListener('click', () => stepDay(s, 1));
    q('.tl-live', box).addEventListener('click', () => goLive(s));
    box.querySelectorAll('.tl-zoom button').forEach(b => b.addEventListener('click', () => {
      setZoom(s, +b.dataset.h, s.head != null ? s.head : null);
    }));
    setupBar(s);
    setupVideo(s);
    load(s);
  }

  // ---------- data ----------
  async function load(s, keepView) {
    try {
      const [days, clips] = await Promise.all([
        getJson('/api/days?camera=' + encodeURIComponent(s.camId)),
        getJson(`/api/clips?date=${s.day}&camera=${encodeURIComponent(s.camId)}`),
      ]);
      s.days = days.map(d => d.date);
      s.clips = clips.map(c => {
        const t = new Date(c.start).getTime();
        return {...c, t, end: t + 1000 * (c.duration || 30)};
      }).filter(c => !isNaN(c.t)).sort((a, b) => a.t - b.t);
      s.loadedAt = Date.now();
    } catch (e) {
      info(s, 'Couldn\'t load the recordings.');
    }
    if (!keepView && s.hours < 24 && s.center == null) s.center = defaultCenter(s);
    render(s);
  }

  function defaultCenter(s) {
    if (s.day === today()) return Date.now() - s.hours * HOUR / 4;
    const last = s.clips[s.clips.length - 1];
    return last ? last.t : dayStart(s.day) + 12 * HOUR;
  }

  function stepDay(s, dir) {
    const t = today();
    const list = [...new Set([...s.days, t])].sort();
    const i = list.indexOf(s.day);
    let next;
    if (i < 0) next = dir < 0 ? list.filter(d => d < s.day).pop() : list.find(d => d > s.day);
    else next = list[i + dir];
    if (!next) return;
    s.day = next; s.center = null; s.clips = [];
    if (s.playing) stopVideo(s);
    s.head = null;
    load(s);
  }

  // ---------- the visible stretch of the day ----------
  function view(s) {
    const d0 = dayStart(s.day), d1 = d0 + 24 * HOUR;
    if (s.hours >= 24) return [d0, d1];
    const span = s.hours * HOUR;
    let c = s.center != null ? s.center : defaultCenter(s);
    let a = c - span / 2;
    a = Math.max(d0, Math.min(d1 - span, a));
    return [a, a + span];
  }

  function setZoom(s, hours, around) {
    s.hours = hours;
    s.center = around != null ? around : (s.center != null ? s.center : defaultCenter(s));
    render(s);
  }

  function timeAt(s, clientX) {
    const r = s.bar.getBoundingClientRect();
    const [a, b] = view(s);
    const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    return a + f * (b - a);
  }

  const clipAt = (s, t) => s.clips.find(c => t >= c.t && t < c.end);

  // ---------- drawing ----------
  function render(s) {
    const [a, b] = view(s);
    const span = b - a;
    const pct = (t) => `${(100 * (t - a) / span).toFixed(3)}%`;
    const isToday = s.day === today();

    q('.tl-day', s.box).textContent = dayLabel(s.day);
    q('.tl-live', s.box).classList.toggle('hidden', !s.playing && isToday);
    q('.tl-next', s.box).disabled = s.day >= today();
    q('.tl-prev', s.box).disabled = !s.days.some(d => d < s.day);
    s.box.querySelectorAll('.tl-zoom button').forEach(x => x.classList.toggle('active', +x.dataset.h === s.hours));

    const marks = q('.tl-marks', s.box);
    const parts = [];
    for (const c of s.clips) {
      if (c.end < a || c.t > b) continue;
      const left = Math.max(a, c.t), right = Math.min(b, c.end);
      parts.push(`<div class="tl-mark${c.person ? ' person' : ''}${s.playing && s.playing.id === c.id ? ' current' : ''}"
        style="left:${pct(left)};width:${(100 * (right - left) / span).toFixed(3)}%"></div>`);
    }
    const st = camStatus(s.camId);
    if (isToday && st.recording && st.recording_clip) {
      const t0 = idTime(st.recording_clip);
      if (t0 && t0 < b) {
        const left = Math.max(a, t0), right = Math.min(b, Date.now());
        if (right > left) parts.push(`<div class="tl-mark rec" style="left:${pct(left)};width:${(100 * (right - left) / span).toFixed(3)}%"></div>`);
      }
    }
    marks.innerHTML = parts.join('');

    const fut = q('.tl-future', s.box);
    const now = Date.now();
    if (isToday && now < b) {
      fut.style.left = pct(Math.max(a, now)); fut.classList.remove('hidden');
    } else fut.classList.add('hidden');

    const ticks = q('.tl-ticks', s.box);
    const w = s.bar.clientWidth || 320;
    const steps = [5, 10, 15, 30, 60, 120, 180, 240, 360].map(m => m * 60e3);
    const step = steps.find(x => (x / span) * w >= 56) || 6 * HOUR;
    // Ticks follow local time: count from local midnight.
    const d0 = dayStart(s.day);
    const out = [];
    for (let t = d0 + Math.ceil((a - d0) / step) * step; t <= b; t += step) {
      const left = 100 * (t - a) / span;
      if (left > 97) break;
      out.push(`<span style="left:${left.toFixed(2)}%">${hm(t)}</span>`);
    }
    ticks.innerHTML = out.join('');

    drawHead(s);
    if (!s.playing) {
      const n = s.clips.length;
      info(s, n ? `${n} recording${n === 1 ? '' : 's'}` : (isToday ? 'Nothing recorded yet today' : 'No recordings'));
    }
  }

  function drawHead(s) {
    const head = q('.tl-head', s.box);
    if (s.head == null) { head.classList.add('hidden'); return; }
    const [a, b] = view(s);
    if (s.head < a || s.head > b) {
      if (s.hours < 24 && !s.dragging) { s.center = s.head; render(s); return; }
      head.classList.add('hidden'); return;
    }
    head.style.left = `${(100 * (s.head - a) / (b - a)).toFixed(3)}%`;
    head.classList.remove('hidden');
  }

  function info(s, text) { q('.tl-info', s.box).textContent = text || ''; }

  // ---------- dragging and tapping the bar ----------
  function setupBar(s) {
    const bar = s.bar;
    const bubble = q('.tl-bubble', s.box);
    const cursor = q('.tl-cursor', s.box);

    function showBubble(clientX) {
      const t = timeAt(s, clientX);
      const r = bar.getBoundingClientRect();
      const x = Math.max(0, Math.min(r.width, clientX - r.left));
      const c = clipAt(s, t);
      const next = c || s.clips.find(k => k.t > t);
      const thumb = q('.tl-thumb', bubble);
      thumb.style.backgroundImage = next ? `url('/api/clips/${next.id}/thumb')` : 'none';
      thumb.classList.toggle('hidden', !next);
      q('span', bubble).textContent = c ? `${hms(t)}${c.person ? ' · Person' : ''}`
        : next ? `${hm(t)} · nothing recorded, next at ${hm(next.t)}` : `${hm(t)} · nothing recorded`;
      bubble.style.left = `${x}px`;
      bubble.classList.toggle('flip', x > r.width - 90);
      bubble.classList.toggle('flop', x < 90);
      bubble.classList.remove('hidden');
      cursor.style.left = `${x}px`;
      cursor.classList.remove('hidden');
      return t;
    }
    function hide() { bubble.classList.add('hidden'); cursor.classList.add('hidden'); }

    bar.addEventListener('pointerdown', (e) => {
      if (e.button) return;
      e.preventDefault();
      bar.setPointerCapture && bar.setPointerCapture(e.pointerId);
      s.dragging = true;
      s.head = showBubble(e.clientX); drawHead(s);
    });
    bar.addEventListener('pointermove', (e) => {
      if (s.dragging) { s.head = showBubble(e.clientX); drawHead(s); }
      else if (e.pointerType === 'mouse') showBubble(e.clientX);
    });
    const finish = (e) => {
      if (!s.dragging) return;
      s.dragging = false;
      hide();
      // Marks can be only a few pixels wide on a phone, so a tap close to one counts as on it.
      const [a, b] = view(s);
      seek(s, timeAt(s, e.clientX), 12 * (b - a) / Math.max(1, bar.clientWidth));
    };
    bar.addEventListener('pointerup', finish);
    bar.addEventListener('pointercancel', () => { s.dragging = false; hide(); drawHead(s); });
    bar.addEventListener('pointerleave', (e) => { if (!s.dragging && e.pointerType === 'mouse') hide(); });
    bar.addEventListener('wheel', (e) => {
      e.preventDefault();
      const i = ZOOMS.indexOf(s.hours);
      const j = Math.max(0, Math.min(ZOOMS.length - 1, i + (e.deltaY < 0 ? 1 : -1)));
      if (j !== i) setZoom(s, ZOOMS[j], timeAt(s, e.clientX));
    }, {passive: false});
    bar.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const at = s.playing ? s.playing.t : (s.head != null ? s.head : Date.now());
      const c = e.key === 'ArrowLeft' ? [...s.clips].reverse().find(k => k.t < at) : s.clips.find(k => k.t > at);
      if (c) play(s, c, 0);
    });
  }

  // ---------- playing recordings ----------
  function seek(s, t, slack) {
    const c = clipAt(s, t);
    if (c) { play(s, c, (t - c.t) / 1000); return; }
    if (slack) {
      let best = null, gap = slack;
      for (const k of s.clips) {
        const d = t < k.t ? k.t - t : t - k.end;
        if (d <= gap) { best = k; gap = d; }
      }
      if (best) { play(s, best, 0); return; }
    }
    const next = s.clips.find(k => k.t > t);
    const prev = [...s.clips].reverse().find(k => k.end <= t);
    if (next) { play(s, next, 0, `Nothing recorded at ${hm(t)}, playing ${hm(next.t)}`); return; }
    if (s.day === today()) {
      goLive(s);
      info(s, 'Nothing recorded after that, so here\'s the live picture');
      return;
    }
    if (prev) { play(s, prev, 0, `Nothing recorded after ${hm(prev.t)} that day`); return; }
    s.head = null; drawHead(s);
    info(s, 'No recordings on this day');
  }

  function play(s, clip, offset, note) {
    const v = s.video;
    const same = s.playing && s.playing.id === clip.id && v.getAttribute('src');
    s.playing = clip;
    s.head = clip.t + offset * 1000;
    if (s.hours < 24) s.center = s.head;
    // Stop the live picture while a recording plays, to save data on phones.
    const img = q('img', s.wrap);
    if (img && img.getAttribute('src')) img.removeAttribute('src');
    s.wrap.classList.add('hidden');
    s.player.classList.remove('hidden');
    q('.tl-live', s.box).classList.remove('hidden');
    label(s);
    if (same) {
      v.currentTime = offset;
    } else {
      v.src = `/api/clips/${clip.id}/video`;
      v.addEventListener('loadedmetadata', function once() {
        v.removeEventListener('loadedmetadata', once);
        if (offset > 0 && offset < (v.duration || Infinity)) v.currentTime = offset;
      });
    }
    v.play().catch(() => {});
    info(s, note || `Playing ${hms(s.head)}`);
    render(s);
  }

  function label(s) {
    const c = s.playing;
    if (!c) return;
    q('.tl-label', s.player).textContent =
      `${dayLabel(s.day)} ${hms(s.head != null ? s.head : c.t)} · ${c.person ? 'Person' : 'Motion'}`;
  }

  function setupVideo(s) {
    const v = s.video;
    v.addEventListener('timeupdate', () => {
      if (!s.playing || s.dragging) return;
      s.head = s.playing.t + v.currentTime * 1000;
      label(s);
      drawHead(s);
    });
    v.addEventListener('loadedmetadata', () => {
      if (s.playing && isFinite(v.duration) && v.duration > 0) {
        s.playing.end = s.playing.t + v.duration * 1000;
        render(s);
      }
    });
    v.addEventListener('ended', () => {
      const cur = s.playing;
      const next = cur && s.clips.find(k => k.t > cur.t);
      if (next) { play(s, next, 0, `Skipped to the next recording at ${hm(next.t)}`); return; }
      if (s.day === today()) { goLive(s); info(s, 'That was the latest recording. Back to live.'); }
      else info(s, 'That was the last recording of the day');
    });
    v.addEventListener('error', () => {
      if (!s.playing) return;
      info(s, 'This recording won\'t play here. Try Download in the Recordings tab.');
    });
  }

  function stopVideo(s) {
    const v = s.video;
    v.pause(); v.removeAttribute('src'); v.load();
    s.playing = null;
  }

  function goLive(s) {
    stopVideo(s);
    s.head = null;
    s.player.classList.add('hidden');
    s.wrap.classList.remove('hidden');
    q('.tl-live', s.box).classList.add('hidden');
    const img = q('img', s.wrap);
    const liveTabOpen = !document.getElementById('tab-live') || !document.getElementById('tab-live').classList.contains('hidden');
    if (img && img.dataset.src && liveTabOpen) img.src = img.dataset.src + '?t=' + Date.now();
    if (s.day !== today()) { s.day = today(); s.center = null; load(s); } else render(s);
  }

  // ---------- keeping up to date ----------
  function liveTabOpen() {
    const t = document.getElementById('tab-live');
    return !t || !t.classList.contains('hidden');
  }

  setInterval(() => {
    for (const s of all) {
      if (!document.body.contains(s.el)) continue;
      if (!liveTabOpen()) continue;
      if (s.day === today() && !s.dragging && Date.now() - s.loadedAt > 30e3) load(s, true);
      else if (!s.dragging) render(s);
    }
    for (let i = all.length - 1; i >= 0; i--) if (!document.body.contains(all[i].el)) { stopVideo(all[i]); all.splice(i, 1); }
  }, 5000);

  // The Live tab streams pictures only while it's open; pause recordings too when it closes,
  // and keep the live picture off while a recording is showing.
  function watchTab() {
    const tab = document.getElementById('tab-live');
    if (!tab) return;
    new MutationObserver(() => {
      const open = !tab.classList.contains('hidden');
      for (const s of all) {
        if (!s.playing) continue;
        if (!open) s.video.pause();
        else { const img = q('img', s.wrap); if (img) img.removeAttribute('src'); }
      }
    }).observe(tab, {attributes: true, attributeFilter: ['class']});
  }

  // Add a timeline to each camera on the Live tab, including ones added later.
  function scan() { document.querySelectorAll('#live .cam[data-cam]').forEach(setup); }
  function start() {
    const live = document.getElementById('live');
    if (!live) return;
    new MutationObserver(scan).observe(live, {childList: true});
    scan();
    watchTab();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
  window.setupTimeline = setup;
})();
