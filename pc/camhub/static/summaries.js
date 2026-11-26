'use strict';
// Night summaries: a strip at the top of the Recordings tab with each morning's sped-up video
// of the night, a player that lists what happened (tap one to open the full recording), and a
// Settings card. Also the "Watch on TV" pieces shared with grid.js: castTo() and tvHelp().
(function () {
  const el = (html) => { const d = document.createElement('div'); d.innerHTML = html.trim(); return d.firstElementChild; };
  const pad = (n) => String(n).padStart(2, '0');
  const hm = (iso) => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const canCast = () => !!(window.HomeCamera && window.HomeCamera.castMedia);
  // A summary is named after the morning it was made; people think of it as the night before.
  const nightLabel = (date) => {
    if (date === dayOf(new Date())) return 'Last night';
    const d = new Date(date + 'T12:00:00'); d.setDate(d.getDate() - 1);
    return d.toLocaleDateString(undefined, {weekday: 'short', day: 'numeric', month: 'short'}) + ' night';
  };
  const dayOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  let data = null;

  // ---------- strip on the Recordings tab ----------
  const strip = el('<div class="ns-strip hidden"><div class="ns-head"><b>Night summaries</b><span class="muted ns-note"></span></div><div class="ns-list"></div></div>');
  const recTab = document.getElementById('tab-recordings');
  recTab.insertBefore(strip, recTab.querySelector('.toolbar'));

  async function load() {
    try { data = await api('/api/summaries'); } catch (e) { return; }
    render();
    if (data.busy) setTimeout(load, 4000);
  }

  function render() {
    const list = strip.querySelector('.ns-list');
    const items = data.items || [];
    strip.classList.toggle('hidden', !items.length && !data.busy);
    strip.querySelector('.ns-note').textContent = data.busy
      ? `Making the summary of ${nightLabel(data.busy).toLowerCase()}… ${Math.round((data.progress || 0) * 100)}%` : '';
    list.innerHTML = '';
    for (const s of items.slice(0, 14)) {
      const b = el(`<button class="ns-card${s.video ? '' : ' quiet'}">
          <div class="ns-thumb"></div>
          <div class="ns-meta"><b></b><span class="muted"></span></div></button>`);
      if (s.picture) b.querySelector('.ns-thumb').style.backgroundImage = `url('/api/summaries/${s.date}/picture.jpg')`;
      else b.querySelector('.ns-thumb').textContent = s.count ? '' : 'Quiet night';
      b.querySelector('b').textContent = nightLabel(s.date);
      b.querySelector('.muted').textContent = s.count
        ? `${s.count} recording${s.count === 1 ? '' : 's'}${s.video ? ' · ' + secs(s.seconds) : ''}` : 'Nothing recorded';
      b.addEventListener('click', () => open(s.date));
      list.appendChild(b);
    }
  }
  const secs = (n) => { n = Math.round(n || 0); return n >= 60 ? `${Math.floor(n / 60)}m ${pad(n % 60)}s` : `${n}s`; };

  // ---------- player ----------
  const player = el(`<div class="player hidden ns-player">
      <video controls playsinline preload="auto"></video>
      <div class="ns-chapters"></div>
      <div class="player-bar">
        <div class="title"></div>
        <button class="ns-cast hidden">📺 Play on TV</button>
        <a class="ns-dl"><button>Download</button></a>
        <button class="primary ns-close">Close</button>
      </div></div>`);
  document.body.appendChild(player);
  const video = player.querySelector('video');
  let current = null;

  async function open(date) {
    let s;
    try { s = await api('/api/summaries/' + date); } catch (e) { toast(e.message); return; }
    current = s;
    player.querySelector('.title').textContent = `Night of ${new Date(s.from).toLocaleDateString(undefined, {weekday: 'long', day: 'numeric', month: 'short'})} · ${s.text}`;
    player.querySelector('.ns-dl').href = `/api/summaries/${date}/video?download=1`;
    player.querySelector('.ns-dl').classList.toggle('hidden', !s.video);
    player.querySelector('.ns-cast').classList.toggle('hidden', !s.video || !canCast());
    const ch = player.querySelector('.ns-chapters');
    ch.innerHTML = '';
    if (s.video) {
      video.classList.remove('hidden');
      video.src = `/api/summaries/${date}/video`;
      video.play().catch(() => {});
    } else {
      video.classList.add('hidden');
      ch.innerHTML = '<div class="empty">Nothing was recorded that night.</div>';
    }
    const names = Object.fromEntries((status.cameras || []).map(c => [c.id, c.name]));
    for (const c of s.chapters || []) {
      const b = el('<button class="chip"></button>');
      b.textContent = `${hm(c.start)} ${names[c.camera] || c.camera}${c.person ? ' · Person' : ''}`;
      b.title = 'Jump here. Double-click (or long-press) to open the full recording.';
      b.addEventListener('click', () => { video.currentTime = c.t; video.play().catch(() => {}); });
      b.addEventListener('dblclick', () => openFull(c.clip));
      b.addEventListener('contextmenu', ev => { ev.preventDefault(); openFull(c.clip); });
      b.dataset.t = c.t;
      ch.appendChild(b);
    }
    if ((s.chapters || []).length) {
      const full = el('<button class="ns-full">Open this recording ▶</button>');
      full.addEventListener('click', () => {
        const c = chapterAt(video.currentTime);
        if (c) openFull(c.clip);
      });
      ch.appendChild(full);
    }
    player.classList.remove('hidden');
    history.replaceState(null, '', '#summary=' + date);
  }

  function chapterAt(t) {
    const list = (current && current.chapters) || [];
    let found = list[0];
    for (const c of list) if (c.t <= t + 0.05) found = c;
    return found;
  }

  video.addEventListener('timeupdate', () => {
    const c = chapterAt(video.currentTime);
    player.querySelectorAll('.ns-chapters .chip').forEach(b => b.classList.toggle('active', c && +b.dataset.t === c.t));
  });

  function close() {
    video.pause(); video.removeAttribute('src'); video.load();
    player.classList.add('hidden');
    current = null;
    history.replaceState(null, '', location.pathname);
  }

  async function openFull(clipId) {
    close();
    const d = clipId.split('_')[1];
    currentDay = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
    showTab('recordings');
    await loadDays();
    openClip(clipId);
  }

  player.querySelector('.ns-close').addEventListener('click', close);
  player.querySelector('.ns-cast').addEventListener('click', () => {
    if (current) castTo('summary', current.date, `A.S.D Home Security: ${nightLabel(current.date).toLowerCase()}`);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !player.classList.contains('hidden')) close(); });

  // ---------- Settings card ----------
  window.summaryCard = async function () {
    if (!data) await load();
    const s = (data && data.settings) || {};
    const card = el(`<div class="card"><h2>Night summary</h2>
      <p class="muted" style="margin-top:0">Each morning, one short sped-up video of everything recorded overnight
        from every camera, with the time on the picture. It's on the Recordings tab, and your phone gets a
        notification when it's ready.</p>
      <label class="check" style="margin-bottom:12px"><input type="checkbox" class="ns-on"> Make a night summary every morning</label>
      <div class="field"><label>Night starts</label><input type="time" class="ns-start"></div>
      <div class="field"><label>Night ends</label><input type="time" class="ns-end"></div>
      <div class="field"><label>Ready by</label><input type="time" class="ns-ready">
        <div class="hint">When it's made. Making it takes a few minutes on a busy night.</div></div>
      <div class="field"><label>Aim for a video of about</label>
        <select class="ns-secs"><option value="30">30 seconds</option><option value="60">1 minute</option>
          <option value="120">2 minutes</option><option value="300">5 minutes</option></select>
        <div class="hint">Busier nights play faster to fit (from 4× up to 120× normal speed).</div></div>
      <div class="field"><label>Keep summaries for (days)</label><input type="number" class="ns-keep" min="1" max="365"></div>
      <div class="toolbar"><button class="primary ns-save">Save</button><button class="ns-now">Make last night's now</button></div>
    </div>`);
    const q = (c) => card.querySelector(c);
    q('.ns-on').checked = s.enabled !== false;
    q('.ns-start').value = s.start || '21:00';
    q('.ns-end').value = s.end || '07:00';
    q('.ns-ready').value = s.ready || '07:30';
    q('.ns-secs').value = String(s.seconds || 60);
    if (!q('.ns-secs').value) q('.ns-secs').add(new Option(`${s.seconds} seconds`, s.seconds, true, true));
    q('.ns-keep').value = s.keep_days || 30;
    q('.ns-save').addEventListener('click', async () => {
      try {
        await api('/api/summaries/settings', {method: 'PUT', body: JSON.stringify({
          enabled: q('.ns-on').checked, start: q('.ns-start').value, end: q('.ns-end').value,
          ready: q('.ns-ready').value, seconds: parseInt(q('.ns-secs').value, 10),
          keep_days: parseInt(q('.ns-keep').value, 10)})});
        toast('Night summary settings saved');
      } catch (e) { toast('Could not save: ' + e.message); }
    });
    q('.ns-now').addEventListener('click', async () => {
      try {
        await api('/api/summaries/make', {method: 'POST', body: '{}'});
        toast('Making it now. It appears on the Recordings tab in a few minutes.');
        setTimeout(load, 1500);
      } catch (e) { toast(e.message); }
    });
    return card;
  };

  // ---------- Watch on TV (shared with the grid) ----------
  // In the phone app, casts to a Chromecast or Google TV. Elsewhere, explains the TV page.
  window.castTo = async function (kind, id, title) {
    if (!canCast()) { tvHelp(); return; }
    try {
      const link = await api('/api/tv/link', {method: 'POST', body: '{}'});
      let path, live;
      if (kind === 'summary') { path = `${link.summary}${id}.mp4`; live = false; }
      else {
        const src = link.sources.find(s => s.id === id) || link.sources[0];
        if (!src) { toast('No camera to show'); return; }
        path = src.live; live = true; title = title || src.name;
      }
      window.HomeCamera.castMedia(location.origin + path, title || 'A.S.D Home Security', live);
    } catch (e) { toast('Cast: ' + e.message); }
  };

  window.tvHelp = async function () {
    let url = `${location.origin}/tv`;
    if (['localhost', '127.0.0.1'].includes(location.hostname)) {
      // On the PC itself: give the address other devices at home use.
      try { url = (await api('/api/tv/info')).lan_url || url; } catch (e) {}
    }
    const box = el(`<div class="player ns-help"><div class="card" style="max-width:560px;margin:auto">
      <h2>Watch on TV</h2>
      <p><b>Chromecast or Google TV:</b> in the A.S.D Home Security phone app, press <b>📺 TV</b> on the Live tab and pick your TV.</p>
      <p><b>Smart TV with a web browser</b> (Samsung, LG, Fire TV and others): open the TV's browser, go to
        <code class="u"></code> and sign in once. Every camera shows full screen; use the remote's arrows and OK to pick one.</p>
      <p><b>From this computer:</b> open <a class="tvlink" href="/tv" target="_blank">the TV page</a> in Google Chrome,
        then Chrome's menu (⋮) &gt; <b>Cast…</b> &gt; your TV.</p>
      <div class="toolbar"><button class="primary">Close</button></div></div></div>`);
    box.querySelector('.u').textContent = url;
    box.querySelector('button').addEventListener('click', () => box.remove());
    box.addEventListener('click', ev => { if (ev.target === box) box.remove(); });
    document.body.appendChild(box);
  };

  window.tvCard = function () {
    const card = el(`<div class="card"><h2>Watch on TV</h2>
      <p class="muted" style="margin-top:0">Show the cameras live on a Chromecast, Google TV or smart TV.
        Night summaries can be played on the TV too.</p>
      <button class="primary">How to watch on TV</button></div>`);
    card.querySelector('button').addEventListener('click', tvHelp);
    return card;
  };

  // ---------- hooks into the rest of the viewer ----------
  document.querySelector('nav button[data-tab="recordings"]').addEventListener('click', load);
  window.onSummaryEvent = (e) => {
    if (e.type !== 'night_summary') return;
    toast('Night summary ready: ' + e.text);
    load();
  };
  const m = location.hash.match(/summary=(\d{4}-\d{2}-\d{2})/);
  if (m) setTimeout(() => { showTab('recordings'); open(m[1]); }, 300);
  load();
})();
