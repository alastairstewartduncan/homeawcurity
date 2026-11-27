'use strict';
// The "Who's been" tab: everyone and every number plate seen on your property, and the recordings they're in.
// Search by name, plate, date, camera or a photo. Uses $, api, esc, toast, openClip and clipList from app.js.
(function () {
  const h = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const q = (s, el) => el.querySelector(s);
  const qa = (s, el) => [...el.querySelectorAll(s)];
  const pic = (name) => `/api/smart/pics/${encodeURIComponent(name)}`;
  const st = {kind: '', q: '', from: '', to: '', camera: '', view: 'who', group: null, photo: null};
  let smart = null;
  let wrap = null;

  function when(iso, withDay = true) {
    const d = new Date(iso); if (isNaN(d)) return iso || '';
    const today = new Date().toDateString() === d.toDateString();
    return d.toLocaleString([], today || !withDay ? {hour: '2-digit', minute: '2-digit'}
      : {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
  }
  function day(iso) { return iso ? fmtDay(iso.slice(0, 10)) : ''; }
  function times(n) { return n === 1 ? 'once' : n === 2 ? 'twice' : `${n} times`; }
  function busy(btn, text) { const old = btn.textContent; btn.disabled = true; btn.textContent = text; return () => { btn.disabled = false; btn.textContent = old; }; }

  function readPhoto(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const s = Math.min(1, 1600 / Math.max(img.width, img.height));
        const c = document.createElement('canvas');
        c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(img.src);
        resolve(c.toDataURL('image/jpeg', 0.9));
      };
      img.onerror = () => reject(new Error('That file is not a picture'));
      img.src = URL.createObjectURL(file);
    });
  }

  async function load() {
    wrap = $('#sightings'); if (!wrap) return;
    try { smart = await api('/api/smart'); } catch (e) { wrap.innerHTML = `<div class="card">${esc(e.message)}</div>`; return; }
    wrap.innerHTML = '';
    wrap.append(searchBar(), h('<div class="log-results"></div>'), settingsCard());
    results();
  }
  window.loadSightings = load;

  // ---------- search bar ----------
  function searchBar() {
    const cams = Object.entries(smart.cameras).map(([id, c]) => `<option value="${esc(id)}">${esc(c.name)}</option>`).join('');
    const bar = h(`<div class="card wide log-search">
      <div class="log-row">
        <input type="search" class="ls-q" placeholder="Search a name or number plate, e.g. AB12 CDE" value="${esc(st.q)}">
        <label class="file-btn" title="Find someone from a photo of them">📷 Search by photo<input type="file" accept="image/*" class="ls-photo" hidden></label>
      </div>
      <div class="log-row">
        <div class="seg ls-kind">
          <button data-k="">Everyone</button><button data-k="person">People</button><button data-k="vehicle">Vehicles</button>
        </div>
        <div class="seg ls-view">
          <button data-v="who" title="Each person or vehicle once">Who</button><button data-v="all" title="Every time anyone was seen">Every sighting</button>
        </div>
        <label class="check">From <input type="date" class="ls-from" value="${esc(st.from)}"></label>
        <label class="check">to <input type="date" class="ls-to" value="${esc(st.to)}"></label>
        <select class="ls-cam"><option value="">All cameras</option>${cams}</select>
      </div>
    </div>`);
    q('.ls-cam', bar).value = st.camera;
    const mark = () => {
      qa('.ls-kind button', bar).forEach(b => b.classList.toggle('active', b.dataset.k === st.kind));
      qa('.ls-view button', bar).forEach(b => b.classList.toggle('active', b.dataset.v === st.view));
    };
    mark();
    let timer = null;
    q('.ls-q', bar).addEventListener('input', (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => { st.q = e.target.value.trim(); st.photo = null; st.group = null; results(); }, 300);
    });
    qa('.ls-kind button', bar).forEach(b => b.addEventListener('click', () => { st.kind = b.dataset.k; st.photo = null; st.group = null; mark(); results(); }));
    qa('.ls-view button', bar).forEach(b => b.addEventListener('click', () => { st.view = b.dataset.v; st.photo = null; st.group = null; mark(); results(); }));
    const dates = () => {
      st.from = q('.ls-from', bar).value; st.to = q('.ls-to', bar).value; st.camera = q('.ls-cam', bar).value;
      if ((st.from || st.to || st.camera) && st.view === 'who' && !st.group) { st.view = 'all'; mark(); }
      results();
    };
    qa('.ls-from, .ls-to, .ls-cam', bar).forEach(i => i.addEventListener('change', dates));
    q('.ls-photo', bar).addEventListener('change', async (e) => {
      const f = e.target.files[0]; e.target.value = ''; if (!f) return;
      const out = $('.log-results', wrap); out.innerHTML = '<div class="card wide muted">Looking for them…</div>';
      try {
        const r = await api('/api/smart/log/photo', {method: 'POST', body: JSON.stringify({photo: await readPhoto(f)})});
        st.photo = r; st.group = null; results();
      } catch (err) { toast(err.message); results(); }
    });
    return bar;
  }

  // ---------- results ----------
  async function results() {
    const out = $('.log-results', wrap); if (!out) return;
    try {
      if (st.group) return await groupView(out, st.group);
      if (st.photo) return photoView(out, st.photo);
      if (st.view === 'all') return await entriesView(out);
      return await whoView(out);
    } catch (e) { out.innerHTML = `<div class="card wide">${esc(e.message)}</div>`; }
  }

  function emptyText() {
    const lg = smart.settings.log || {};
    if (!lg.people && !lg.plates) return 'The log is off. Turn it on under "Log settings" below.';
    if (st.q || st.from || st.to || st.camera) return 'Nobody matches that search.';
    return 'Nobody yet. People and vehicles appear here once they have been on your property.';
  }

  function whoTile(g, extra = '') {
    const t = h(`<button class="who-tile ${g.kind}">
      ${g.pic ? `<img src="${pic(g.pic)}" alt="">` : `<span class="nopic">${g.kind === 'vehicle' ? '🚗' : '?'}</span>`}
      <b>${esc(g.name)}</b>
      ${g.kind === 'vehicle' && g.plate && g.plate !== g.name ? `<span class="plate">${esc(g.plate)}</span>` : ''}
      <span class="muted small">Seen ${times(g.count)}${g.days > 1 ? ` on ${g.days} days` : ''}</span>
      <span class="muted small">Last ${esc(day(g.last))} ${esc(when(g.last, false))}</span>
      <span class="tags">${g.family ? '<span class="badge">Family</span>' : ''}${g.known && !g.family ? '<span class="badge">Ours</span>' : ''}
        ${g.visitor ? '<span class="badge">Visitor</span>' : ''}${!g.known && g.count > 1 ? '<span class="badge warn">Been before</span>' : ''}${extra}</span>
    </button>`);
    t.addEventListener('click', () => { st.group = g.group; results(); window.scrollTo({top: 0}); });
    return t;
  }

  async function whoView(out) {
    const r = await api(`/api/smart/log/who?${new URLSearchParams({kind: st.kind, q: st.q})}`);
    out.innerHTML = '';
    const card = h(`<div class="card wide"><h2>Who's been</h2>
      <p class="muted" style="margin-top:0">Everyone and every vehicle seen on your property in the last ${smart.settings.log.days} days,
        newest first. Tap one to see every time they were here and play the recordings.</p>
      <div class="who-grid"></div></div>`);
    const grid = q('.who-grid', card);
    if (!r.who.length) grid.innerHTML = `<p class="muted">${esc(emptyText())}</p>`;
    for (const g of r.who) grid.appendChild(whoTile(g));
    out.appendChild(card);
  }

  function photoView(out, r) {
    out.innerHTML = '';
    const card = h(`<div class="card wide"><h2>People like your photo</h2>
      <p class="muted" style="margin-top:0">${r.found === 'face' ? 'Compared by face.' : 'No face could be seen, so compared by build and clothes, which is only a rough guide.'}
        Closest first. Tap one to see their recordings.</p>
      <div class="who-grid"></div><p><button class="back">◀ Back</button></p></div>`);
    const grid = q('.who-grid', card);
    if (!r.matches.length) grid.innerHTML = '<p class="muted">Nobody in the log looks like that photo.</p>';
    for (const g of r.matches) grid.appendChild(whoTile(g, `<span class="badge">${Math.round(g.likeness * 100)}% alike</span>`));
    q('.back', card).addEventListener('click', () => { st.photo = null; results(); });
    out.appendChild(card);
  }

  function entryParams(extra) {
    return new URLSearchParams({kind: st.kind, q: st.q, day_from: st.from, day_to: st.to, camera: st.camera, ...(extra || {})});
  }

  async function entriesView(out) {
    const r = await api('/api/smart/log/entries?' + entryParams());
    out.innerHTML = '';
    const card = h(`<div class="card wide"><h2>Every sighting</h2><div class="entries"></div></div>`);
    fillEntries(q('.entries', card), r.entries, true);
    out.appendChild(card);
  }

  async function groupView(out, grp) {
    const [who, r] = await Promise.all([
      api('/api/smart/log/who'),
      api('/api/smart/log/entries?' + new URLSearchParams({group: grp, day_from: st.from, day_to: st.to, camera: st.camera})),
    ]);
    const g = who.who.find(x => x.group === grp);
    out.innerHTML = '';
    if (!g) { st.group = null; return results(); }
    const card = h(`<div class="card wide">
      <div class="group-head">
        ${g.pic ? `<img class="group-pic" src="${pic(g.pic)}" alt="">` : '<span class="group-pic nopic">?</span>'}
        <div class="group-info">
          <h2 style="margin:0">${esc(g.name)}${g.kind === 'vehicle' && g.plate && g.plate !== g.name ? ` <span class="plate">${esc(g.plate)}</span>` : ''}</h2>
          <div class="muted">Seen ${times(g.count)}${g.days > 1 ? ` on ${g.days} days` : ''}. First ${esc(day(g.first))} ${esc(when(g.first, false))},
            last ${esc(day(g.last))} ${esc(when(g.last, false))}.</div>
          <div class="group-actions">
            <button class="back">◀ Back</button>
            <button class="play-all primary">▶ Play their recordings</button>
            ${g.family ? '' : `<button class="ren">${g.named ? 'Rename' : 'Give a name'}</button>`}
            <button class="danger forget">Delete all of ${g.kind === 'vehicle' ? 'this vehicle' : 'this person'}</button>
          </div>
        </div>
      </div>
      ${g.family ? '<p class="muted small">Family are named in the Smart tab.</p>'
        : g.kind === 'person' ? '<p class="muted small">Named people can be searched by name. To stop alerts about them, add them to Family and friends in the Smart tab.</p>' : ''}
      <div class="entries"></div>
    </div>`);
    const clips = r.entries.filter(e => e.clip && e.clip_exists);
    const play = q('.play-all', card);
    if (!clips.length) { play.disabled = true; play.title = 'Their recordings have been deleted'; }
    play.addEventListener('click', () => playClips(clips, clips[0].clip, g.name));
    q('.back', card).addEventListener('click', () => { st.group = null; results(); });
    const ren = q('.ren', card);
    if (ren) ren.addEventListener('click', async () => {
      const name = prompt(g.kind === 'vehicle' ? 'Name this vehicle, e.g. Postman\'s van' : 'Name this person, e.g. Postman', g.named ? g.name : '');
      if (name == null) return;
      try { await api(`/api/smart/log/groups/${encodeURIComponent(grp)}`, {method: 'PUT', body: JSON.stringify({name})}); results(); }
      catch (e) { toast(e.message); }
    });
    q('.forget', card).addEventListener('click', async () => {
      if (!confirm(`Delete every sighting of ${g.name} and their pictures from the log? Recordings are kept.`)) return;
      try { await api(`/api/smart/log/groups/${encodeURIComponent(grp)}`, {method: 'DELETE'}); st.group = null; toast('Deleted'); results(); }
      catch (e) { toast(e.message); }
    });
    fillEntries(q('.entries', card), r.entries, false);
    out.appendChild(card);
  }

  function fillEntries(list, entries, showName) {
    if (!entries.length) { list.innerHTML = `<p class="muted">${esc(emptyText())}</p>`; return; }
    const clips = entries.filter(e => e.clip && e.clip_exists);
    let lastDay = null;
    for (const e of entries) {
      const d = e.time.slice(0, 10);
      if (d !== lastDay) { list.appendChild(h(`<h3 class="entry-day">${esc(fmtDay(d))}</h3>`)); lastDay = d; }
      const row = h(`<div class="entry">
        ${e.pic ? `<img class="entry-pic" src="${pic(e.pic)}" alt="" loading="lazy">` : '<span class="entry-pic nopic">?</span>'}
        ${e.plate_pic ? `<img class="entry-plate" src="${pic(e.plate_pic)}" alt="Number plate" loading="lazy">` : ''}
        <div class="entry-info">
          ${showName ? `<b class="entry-name">${esc(e.name)}</b>` : ''}
          ${e.plate ? (e.plate !== e.name || !showName ? `<span class="plate">${esc(e.plate)}</span>` : '') : e.kind === 'vehicle' ? '<span class="muted small">Plate not readable</span>' : ''}
          <div class="muted small">${esc(when(e.time, false))} · ${esc(e.camera_name || e.camera || '')}
            ${e.kind === 'person' ? (e.face ? ' · face seen' : ' · face not seen') : ''}${e.by === 'body' ? ' · known by build' : ''}</div>
        </div>
        <div class="entry-actions">
          ${e.clip ? (e.clip_exists ? '<button class="play primary">▶ Play</button>' : '<span class="muted small" title="Deleted by the recordings clean-up, or by hand">Recording deleted</span>')
            : '<span class="muted small">No recording</span>'}
          <button class="del" title="Delete this sighting from the log" aria-label="Delete this sighting">🗑</button>
        </div>
      </div>`);
      const pb = q('.play', row);
      if (pb) pb.addEventListener('click', () => playClips(clips, e.clip, e.name));
      if (showName && e.group) {
        const nm = q('.entry-name', row); nm.classList.add('link');
        nm.addEventListener('click', () => { st.group = e.group; results(); window.scrollTo({top: 0}); });
      }
      q('.del', row).addEventListener('click', async () => {
        if (!confirm('Delete this sighting from the log? The recording is kept.')) return;
        try { await api(`/api/smart/log/entries/${encodeURIComponent(e.id)}`, {method: 'DELETE'}); row.remove(); }
        catch (err) { toast(err.message); }
      });
      list.appendChild(row);
    }
  }

  // Plays one clip in the usual player, with Newer and Older stepping through this person's clips.
  function playClips(entries, first, name) {
    const seen = new Set();
    clipList = entries.filter(e => !seen.has(e.clip) && seen.add(e.clip))
      .map(e => ({id: e.clip, camera: e.camera, start: e.time, summary: name + (e.plate && e.plate !== name ? ` (${e.plate})` : '')}));
    filter = 'all';
    openClip(first);
  }

  // ---------- settings ----------
  function settingsCard() {
    const lg = smart.settings.log;
    const s = smart.log_stats || {};
    const noProperty = Object.values(smart.cameras).filter(c => !(c.property || []).length).map(c => c.name);
    const card = h(`<details class="card wide log-settings" ${lg.people || lg.plates ? '' : 'open'}>
      <summary><b>Log settings and privacy</b></summary>
      <p class="muted">Keeps a log of the people and number plates seen <b>on your property</b>, with the best picture of
        each and the recording, so you can search them later. Everything stays on this PC.</p>
      <div class="smart-opts">
        <label class="check"><input type="checkbox" class="lg-people" ${lg.people ? 'checked' : ''}> Log people</label>
        <label class="check"><input type="checkbox" class="lg-plates" ${lg.plates ? 'checked' : ''}> Log vehicles and read number plates</label>
        <label class="check">Keep the log for
          <select class="lg-days">${[7, 14, 30, 60, 90, 180, 365].map(d => `<option value="${d}">${d} days</option>`).join('')}</select></label>
      </div>
      ${lg.people && !smart.settings.faces.enabled ? '<p class="warn-box">People are only logged while "Recognise faces" is on in the Smart tab.</p>' : ''}
      ${smart.faces_available ? '' : '<p class="warn-box">Face recognition isn\'t installed on this PC yet, so people can\'t be logged. Install the newest HomeCameraSetup.exe.</p>'}
      ${lg.plates && !smart.plates_available ? '<p class="warn-box">Number plate reading isn\'t installed on this PC yet, so vehicles are logged without their plate. Install the newest HomeCameraSetup.exe.</p>' : ''}
      ${noProperty.length ? `<p class="warn-box">Nothing is logged on ${esc(noProperty.join(', '))} until you draw your property for it
        (Smart tab, Our property). That keeps people in the street out of the log.</p>` : ''}
      <p class="muted small">UK GDPR: a camera that records beyond your boundary and logs who it sees makes you responsible for that
        information. Keep the property drawn tight to your own land, keep the log no longer than you need, put up a sign saying
        the camera is in use, and delete someone's entries if they ask. People in privacy areas are never logged.</p>
      <p class="muted small">${s.entries || 0} entries (${s.people || 0} people, ${s.vehicles || 0} vehicles)${s.oldest ? `, oldest ${esc(day(s.oldest))}` : ''}.
        <button class="danger lg-clear">Delete the whole log</button></p>
    </details>`);
    q('.lg-days', card).value = String(lg.days);
    const save = async () => {
      try {
        smart = await api('/api/smart/settings', {method: 'PUT', body: JSON.stringify({log: {
          people: q('.lg-people', card).checked, plates: q('.lg-plates', card).checked, days: +q('.lg-days', card).value}})});
        toast('Saved');
        if ((q('.lg-people', card).checked || q('.lg-plates', card).checked) && noProperty.length && confirm(
          `Draw your property on ${noProperty.join(', ')} now? Nothing is logged there until you do.`)) {
          showTab('smart');
          return;
        }
        load();
      } catch (e) { toast('Could not save: ' + e.message); }
    };
    qa('.lg-people, .lg-plates, .lg-days', card).forEach(i => i.addEventListener('change', save));
    q('.lg-clear', card).addEventListener('click', async () => {
      if (!confirm('Delete every entry in the log, and their pictures? Recordings are kept.')) return;
      try { await api('/api/smart/log', {method: 'DELETE'}); toast('Log deleted'); st.group = null; load(); } catch (e) { toast(e.message); }
    });
    return card;
  }
})();
