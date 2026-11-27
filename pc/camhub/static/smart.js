'use strict';
// The Smart tab: family faces and bodies, visitors, your cars, parcels, lines and loitering, and recent smart alerts.
// Uses $, api, esc, toast and showTab from app.js.
(function () {
  const h = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const q = (s, el) => el.querySelector(s);
  const qa = (s, el) => [...el.querySelectorAll(s)];
  const pic = (name) => `/api/smart/pics/${encodeURIComponent(name)}`;
  let data = null;

  function when(iso) {
    const d = new Date(iso); if (isNaN(d)) return iso || '';
    const today = new Date().toDateString() === d.toDateString();
    return d.toLocaleString([], today ? {hour: '2-digit', minute: '2-digit'} : {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
  }
  function busy(btn, text) { const old = btn.textContent; btn.disabled = true; btn.textContent = text; return () => { btn.disabled = false; btn.textContent = old; }; }

  async function call(path, opts) {
    const r = await api(path, opts);
    if (r && r.settings) data = r;
    return r;
  }
  const send = (path, method, body) => call(path, {method, body: JSON.stringify(body || {})});

  // A picture from the phone or PC, made smaller before it's sent.
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

  async function loadSmart() {
    const wrap = $('#smart'); if (!wrap) return;
    try { await call('/api/smart'); } catch (e) { wrap.innerHTML = `<div class="card">${esc(e.message)}</div>`; return; }
    render();
  }
  function render() {
    const wrap = $('#smart'); wrap.innerHTML = '';
    wrap.append(facesCard(), visitorsCard(), carsCard());
    for (const [id, cam] of Object.entries(data.cameras)) wrap.appendChild(rulesCard(id, cam));
    wrap.appendChild(activityCard());
  }
  window.loadSmart = loadSmart;

  let refreshTimer = null;
  window.onSmartEvent = (e) => {
    if (e.type !== 'known_person' || e.alert) toast(e.title + (e.message ? ' · ' + e.message : ''));
    const tab = $('#tab-smart');
    if (tab && !tab.classList.contains('hidden')) {
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => { if (!document.activeElement || !document.activeElement.closest('#smart input, #smart select')) loadSmart(); }, 1500);
    }
  };

  // ---------- family faces ----------
  function facesCard() {
    const f = data.settings.faces;
    const card = h(`<div class="card wide smart-card">
      <h2>Family and friends</h2>
      <p class="muted" style="margin-top:0">Add the people who live here or visit often. With face recognition on, you get
        a <b>stranger</b> alert instead of "person seen", and no alert at all for the people you've named.
        Faces are recognised on this PC. No pictures are sent anywhere.</p>
      ${data.faces_available ? '' : `<p class="warn-box">Face recognition isn't installed on this PC yet. Install the newest
        HomeCameraSetup.exe and it will be added.</p>`}
      <div class="smart-opts">
        <label class="check"><input type="checkbox" class="f-on" ${f.enabled ? 'checked' : ''}> Recognise faces (alert only for strangers)</label>
        <label class="check">If a face can't be seen
          <select class="f-noface"><option value="alert">still alert ("person seen")</option><option value="quiet">stay quiet</option></select></label>
        <label class="check">Matching
          <select class="f-strict"><option value="relaxed">Relaxed</option><option value="normal">Normal</option><option value="strict">Strict</option></select></label>
      </div>
      <div class="smart-opts">
        <label class="check"><input type="checkbox" class="b-on" ${data.settings.bodies.enabled ? 'checked' : ''} ${data.bodies_available ? '' : 'disabled'}>
          Also know them from behind or in the dark, by build, height, clothes and walk</label>
        <label class="check">Matching
          <select class="b-strict"><option value="relaxed">Relaxed</option><option value="normal">Normal</option><option value="strict">Strict</option></select></label>
      </div>
      <p class="muted small" style="margin-top:0">Each time a face is recognised, the PC keeps a picture of that person's build and
        clothes, so it learns by itself. When someone's back is to the camera and they match, there's no "person seen" alert.
        A clear face it doesn't know always counts as a stranger, whatever they're wearing. Needs face recognition on.</p>
      ${data.bodies_available ? '' : `<p class="warn-box">Body recognition isn't installed on this PC yet. Install the newest
        HomeCameraSetup.exe and it will be added.</p>`}
      <h3>People you know</h3>
      <div class="people"></div>
      <div class="smart-add">
        <input class="p-name" placeholder="Name, e.g. Alastair" maxlength="40">
        <label class="file-btn">Choose a photo<input type="file" class="p-photo" accept="image/*" hidden></label>
        <span class="muted p-file"></span>
        <button class="primary p-add">Add person</button>
      </div>
      <p class="muted small">Tip: add 3 to 5 photos of each person, facing the camera, plus faces from "Seen lately"
        below. Faces picked up by this camera work best, because they look like what it sees.</p>
      <h3>Faces seen lately</h3>
      <div class="seen-grid faces-seen"></div>
    </div>`);
    q('.f-noface', card).value = f.no_face;
    q('.f-strict', card).value = f.strictness;
    q('.b-strict', card).value = data.settings.bodies.strictness;
    const save = async () => {
      try {
        await send('/api/smart/settings', 'PUT', {faces: {enabled: q('.f-on', card).checked,
          no_face: q('.f-noface', card).value, strictness: q('.f-strict', card).value},
          bodies: {enabled: q('.b-on', card).checked, strictness: q('.b-strict', card).value}});
        toast('Saved');
      } catch (e) { toast('Could not save: ' + e.message); }
    };
    qa('.f-on, .f-noface, .f-strict, .b-on, .b-strict', card).forEach(i => i.addEventListener('change', save));

    const people = q('.people', card);
    if (!data.people.length) people.innerHTML = '<p class="muted">Nobody yet.</p>';
    for (const p of data.people) {
      const row = h(`<div class="known-row">
        <b class="kname">${esc(p.name)}</b>
        <span class="thumbs">${p.faces.map(x => `<span class="thumb-x">${x.pic ? `<img src="${pic(x.pic)}" alt="">` : '<span class="nopic">☺</span>'}
          <button data-f="${esc(x.id)}" title="Remove this photo" aria-label="Remove this photo">✕</button></span>`).join('')}
          ${p.faces.length ? '' : '<span class="muted">No photos yet</span>'}</span>
        ${(p.bodies || []).length ? `<span class="thumbs bodies" title="Learnt by itself, for knowing ${esc(p.name)} from behind">
          <span class="muted small">From behind:</span>${p.bodies.filter(x => x.pic).slice(-8).map(x => `<span class="thumb-x body">
          <img src="${pic(x.pic)}" alt=""><i>${x.night ? '☾' : '☀'}</i>
          <button data-b="${esc(x.id)}" title="Remove this picture" aria-label="Remove this picture">✕</button></span>`).join('')}
          <span class="muted small">${p.bodies.length} kept</span></span>` : ''}
        <label class="file-btn">＋ Photo<input type="file" accept="image/*" multiple hidden></label>
        <button class="ren">Rename</button><button class="danger del">Remove</button></div>`);
      q('input[type=file]', row).addEventListener('change', async (ev) => {
        const files = [...ev.target.files]; let added = 0;
        for (const file of files) {
          try { await send(`/api/smart/people/${p.id}/photos`, 'POST', {photo: await readPhoto(file)}); added++; }
          catch (e) { toast(`${file.name}: ${e.message}`); }
        }
        if (added) { toast(`Added ${added} photo${added > 1 ? 's' : ''} of ${p.name}`); render(); }
      });
      qa('.thumb-x button', row).forEach(b => b.addEventListener('click', async () => {
        const path = b.dataset.b ? `bodies/${b.dataset.b}` : `photos/${b.dataset.f}`;
        try { await send(`/api/smart/people/${p.id}/${path}`, 'DELETE'); render(); } catch (e) { toast(e.message); }
      }));
      q('.ren', row).addEventListener('click', async () => {
        const name = prompt('New name', p.name); if (!name) return;
        try { await send(`/api/smart/people/${p.id}`, 'PUT', {name}); render(); } catch (e) { toast(e.message); }
      });
      q('.del', row).addEventListener('click', async () => {
        if (!confirm(`Forget ${p.name} and their photos?`)) return;
        try { await send(`/api/smart/people/${p.id}`, 'DELETE'); render(); } catch (e) { toast(e.message); }
      });
      people.appendChild(row);
    }
    const fileIn = q('.p-photo', card);
    fileIn.addEventListener('change', () => { q('.p-file', card).textContent = fileIn.files[0] ? fileIn.files[0].name : ''; });
    q('.p-add', card).addEventListener('click', async (ev) => {
      const name = q('.p-name', card).value.trim();
      if (!name) { toast('Type their name first'); return; }
      const done = busy(ev.target, 'Adding…');
      try {
        const body = {name};
        if (fileIn.files[0]) body.photo = await readPhoto(fileIn.files[0]);
        await send('/api/smart/people', 'POST', body);
        toast(`Added ${name}`); render();
      } catch (e) { toast('Could not add: ' + e.message); done(); }
    });
    seenGrid(q('.faces-seen', card), 'face', data.people, 'Someone new…');
    return card;
  }

  function seenGrid(grid, kind, targets, newLabel) {
    const items = data.seen.filter(s => s.kind === kind);
    if (!items.length) {
      grid.innerHTML = `<p class="muted">${kind === 'face' ? 'Faces the camera picks up that it doesn\'t know appear here, so you can say who they are.'
        : 'Vehicles that stop and aren\'t known appear here, so you can mark yours.'}</p>`;
      return;
    }
    for (const s of items) {
      const tile = h(`<div class="seen-tile">
        <img src="${pic(s.pic)}" alt="" loading="lazy">
        <div class="muted small">${esc(when(s.time))} · ${esc(s.camera_name || s.camera)}</div>
        <select><option value="">${kind === 'face' ? 'Who is this?' : 'Whose is this?'}</option>
          ${targets.map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('')}
          <option value="__new">${esc(newLabel)}</option></select>
        <button class="forget" title="Forget this picture">Forget</button></div>`);
      q('select', tile).addEventListener('change', async (ev) => {
        let body = {id: ev.target.value};
        if (!body.id) return;
        if (body.id === '__new') {
          const name = prompt(kind === 'face' ? 'Their name' : 'A name for this car, e.g. Blue Golf');
          if (!name) { ev.target.value = ''; return; }
          body = {name};
        }
        try { await send(`/api/smart/seen/${s.id}/assign`, 'POST', body); toast('Saved. Thanks!'); render(); }
        catch (e) { toast(e.message); }
      });
      q('.forget', tile).addEventListener('click', async () => {
        try { await send(`/api/smart/seen/${s.id}`, 'DELETE'); render(); } catch (e) { toast(e.message); }
      });
      grid.appendChild(tile);
    }
  }

  // ---------- visitors who keep coming back ----------
  function visitorsCard() {
    const vs = data.settings.visitors;
    const nums = (list, v, unit) => list.map(n => `<option value="${n}" ${n === v ? 'selected' : ''}>${n} ${unit}</option>`).join('');
    const card = h(`<div class="card wide smart-card">
      <h2>Visitors</h2>
      <p class="muted" style="margin-top:0">The PC remembers people it doesn't know: their face if it was seen, otherwise their
        build, height and clothes. Someone who keeps coming back is flagged as a <b>returning stranger</b>, with their visits
        side by side. Only people standing on your property are remembered (draw it on each camera below), never anyone in a
        privacy area, and visitors are forgotten after a while. Nothing leaves this PC.</p>
      <div class="smart-opts">
        <label class="check"><input type="checkbox" class="vi-on" ${vs.enabled ? 'checked' : ''}> Remember visitors and flag returning strangers</label>
        <label class="check">Flag after <select class="vi-visits">${nums([2, 3, 4, 5, 7, 10], vs.visits, 'visits')}</select></label>
        <label class="check">within <select class="vi-days">${nums([1, 2, 3, 7, 14, 30], vs.days, 'days')}</select></label>
        <label class="check">Forget after <select class="vi-forget">${nums([7, 14, 30, 60, 90], vs.forget_days, 'days')}</select> unseen</label>
      </div>
      ${data.settings.faces.enabled ? '' : '<p class="warn-box">Turn on "Recognise faces" above first: it\'s how the PC tells family from visitors.</p>'}
      <div class="visitors"></div>
    </div>`);
    const save = async () => {
      try {
        await send('/api/smart/settings', 'PUT', {visitors: {enabled: q('.vi-on', card).checked,
          visits: +q('.vi-visits', card).value, days: +q('.vi-days', card).value, forget_days: +q('.vi-forget', card).value}});
        toast('Saved'); render();
      } catch (e) { toast('Could not save: ' + e.message); }
    };
    qa('.vi-on, .vi-visits, .vi-days, .vi-forget', card).forEach(i => i.addEventListener('change', save));
    const list = q('.visitors', card);
    if (!data.visitors.length) {
      list.innerHTML = `<p class="muted">${vs.enabled ? 'Nobody yet. Unknown visitors appear here after they leave.' : 'Turn this on to start remembering visitors.'}</p>`;
      return card;
    }
    const people = data.people.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    for (const v of data.visitors) {
      const returning = !v.regular && v.recent >= vs.visits;
      const name = v.label || `Visitor ${v.number}`;
      const row = h(`<div class="visitor ${returning ? 'returning' : ''}">
        <div class="visitor-head">
          ${v.pic ? `<img class="visitor-pic" src="${pic(v.pic)}" alt="">` : '<span class="visitor-pic nopic">?</span>'}
          <div class="visitor-info">
            <b>${esc(name)}</b> ${returning ? '<span class="badge warn">Returning stranger</span>' : ''}
            ${v.regular ? '<span class="badge">Regular, no alerts</span>' : ''}
            <div class="muted small">${v.recent} visit${v.recent === 1 ? '' : 's'} in the last ${vs.days} days · ${v.total} in all ·
              last seen ${esc(when(v.last))} · ${v.face ? 'face seen' : 'known by build and clothes only'}</div>
          </div>
        </div>
        <div class="visit-strip">${v.visits.map(x => `<div class="visit-tile">
          <img src="${x.clip ? `/api/clips/${encodeURIComponent(x.clip)}/thumb` : (x.pic ? pic(x.pic) : '')}" alt="" loading="lazy"
            ${x.pic ? `data-fallback="${esc(pic(x.pic))}"` : ''}>
          <div class="muted small">${esc(when(x.time))} · ${esc(x.camera_name || x.camera || '')}</div>
          ${x.clip ? `<button class="watch" data-clip="${esc(x.clip)}">Watch</button>` : ''}</div>`).join('')}</div>
        <div class="visitor-actions">
          <input class="vi-label" value="${esc(v.label)}" placeholder="A note, e.g. Postman" maxlength="40" aria-label="Note">
          <label class="check"><input type="checkbox" class="vi-regular" ${v.regular ? 'checked' : ''}> Regular (no alerts)</label>
          <select class="vi-who"><option value="">This is someone we know…</option>${people}<option value="__new">Someone new…</option></select>
          <button class="danger vi-forget-one">Forget</button>
        </div></div>`);
      qa('img[data-fallback]', row).forEach(img => img.addEventListener('error', () => {
        if (img.src !== new URL(img.dataset.fallback, location.href).href) img.src = img.dataset.fallback;
      }, {once: true}));
      qa('.watch', row).forEach(b => b.addEventListener('click', () => { location.hash = 'clip=' + b.dataset.clip; location.reload(); }));
      const upd = async (body) => { try { await send(`/api/smart/visitors/${v.id}`, 'PUT', body); toast('Saved'); render(); } catch (e) { toast(e.message); } };
      q('.vi-label', row).addEventListener('change', (e) => upd({label: e.target.value}));
      q('.vi-regular', row).addEventListener('change', (e) => upd({regular: e.target.checked}));
      q('.vi-who', row).addEventListener('change', async (ev) => {
        let body = {id: ev.target.value};
        if (!body.id) return;
        if (body.id === '__new') {
          const nm = prompt('Their name'); if (!nm) { ev.target.value = ''; return; }
          body = {name: nm};
        }
        try { await send(`/api/smart/visitors/${v.id}/family`, 'POST', body); toast('Saved. They\'re family now.'); render(); }
        catch (e) { toast(e.message); }
      });
      q('.vi-forget-one', row).addEventListener('click', async () => {
        if (!confirm(`Forget ${name} and their pictures?`)) return;
        try { await send(`/api/smart/visitors/${v.id}`, 'DELETE'); render(); } catch (e) { toast(e.message); }
      });
      list.appendChild(row);
    }
    return card;
  }

  // ---------- your cars ----------
  function carsCard() {
    const v = data.settings.vehicles;
    const camOpts = Object.entries(data.cameras).map(([id, c]) => `<option value="${esc(id)}">${esc(c.name)}</option>`).join('');
    const card = h(`<div class="card wide smart-card">
      <h2>Your cars</h2>
      <p class="muted" style="margin-top:0">Mark your own cars, and you'll get an alert only when a vehicle that isn't yours
        stops in view (cars driving past don't count). Cars are told apart by their colour, so a car just like yours
        can be mistaken for it, and at night (black and white picture) it compares brightness only.
        Add a daytime and a night-time picture of each car.</p>
      <div class="smart-opts">
        <label class="check"><input type="checkbox" class="v-on" ${v.enabled ? 'checked' : ''}> Alert when an unknown vehicle stops</label>
        <label class="check"><input type="checkbox" class="v-known" ${v.alert_known ? 'checked' : ''}> Also tell me when our car arrives</label>
        <label class="check">Matching
          <select class="v-strict"><option value="relaxed">Relaxed</option><option value="normal">Normal</option><option value="strict">Strict</option></select></label>
      </div>
      <h3>Cars you own</h3>
      <div class="cars"></div>
      <div class="smart-add">
        <input class="c-name" placeholder="Name, e.g. Blue Golf" maxlength="40">
        <label class="check">from the live picture of <select class="c-cam">${camOpts}</select></label>
        <button class="primary c-add">Add car</button>
      </div>
      <p class="muted small">Park the car where the camera can see it, then press Add car. Or mark it below once it has been seen.</p>
      <h3>Vehicles seen lately</h3>
      <div class="seen-grid cars-seen"></div>
    </div>`);
    q('.v-strict', card).value = v.strictness;
    const save = async () => {
      try {
        await send('/api/smart/settings', 'PUT', {vehicles: {enabled: q('.v-on', card).checked,
          alert_known: q('.v-known', card).checked, strictness: q('.v-strict', card).value}});
        toast('Saved');
      } catch (e) { toast('Could not save: ' + e.message); }
    };
    qa('.v-on, .v-known, .v-strict', card).forEach(i => i.addEventListener('change', save));
    const cars = q('.cars', card);
    if (!data.cars.length) cars.innerHTML = '<p class="muted">No cars yet.</p>';
    for (const c of data.cars) {
      const row = h(`<div class="known-row">
        <b class="kname">${esc(c.name)}</b>
        <span class="thumbs">${c.samples.map(x => `<span class="thumb-x" title="${x.night ? 'Night picture' : 'Daytime picture'}">
          <img src="${pic(x.pic)}" alt=""><i>${x.night ? '☾' : '☀'}</i>
          <button data-s="${esc(x.id)}" title="Remove this picture" aria-label="Remove this picture">✕</button></span>`).join('')}</span>
        <button class="more">＋ Picture from live</button><button class="ren">Rename</button><button class="danger del">Remove</button></div>`);
      q('.more', row).addEventListener('click', async (ev) => {
        const done = busy(ev.target, 'Looking…');
        try { await send(`/api/smart/cars/${c.id}/from_live`, 'POST', {camera: q('.c-cam', card).value}); toast('Picture added'); render(); }
        catch (e) { toast(e.message); done(); }
      });
      qa('.thumb-x button', row).forEach(b => b.addEventListener('click', async () => {
        try { await send(`/api/smart/cars/${c.id}/samples/${b.dataset.s}`, 'DELETE'); render(); } catch (e) { toast(e.message); }
      }));
      q('.ren', row).addEventListener('click', async () => {
        const name = prompt('New name', c.name); if (!name) return;
        try { await send(`/api/smart/cars/${c.id}`, 'PUT', {name}); render(); } catch (e) { toast(e.message); }
      });
      q('.del', row).addEventListener('click', async () => {
        if (!confirm(`Forget ${c.name}?`)) return;
        try { await send(`/api/smart/cars/${c.id}`, 'DELETE'); render(); } catch (e) { toast(e.message); }
      });
      cars.appendChild(row);
    }
    q('.c-add', card).addEventListener('click', async (ev) => {
      const name = q('.c-name', card).value.trim();
      if (!name) { toast('Type a name for the car first'); return; }
      const done = busy(ev.target, 'Looking…');
      try { await send('/api/smart/cars', 'POST', {name, camera: q('.c-cam', card).value}); toast(`Added ${name}`); render(); }
      catch (e) { toast(e.message); done(); }
    });
    seenGrid(q('.cars-seen', card), 'vehicle', data.cars, 'A new car of ours…');
    return card;
  }

  // ---------- doorstep, lines and areas (one card per camera) ----------
  const SECS = [[10, '10 s'], [20, '20 s'], [30, '30 s'], [60, '1 min'], [120, '2 min'], [300, '5 min'], [600, '10 min'], [1800, '30 min']];
  const WHAT = [['person', 'People'], ['vehicle', 'Vehicles'], ['both', 'People and vehicles']];

  function rulesCard(camId, cam) {
    const rules = {parcel: JSON.parse(JSON.stringify(cam.parcel)), lines: JSON.parse(JSON.stringify(cam.lines)),
      areas: JSON.parse(JSON.stringify(cam.areas)), property: JSON.parse(JSON.stringify(cam.property || []))};
    let tool = 'line', draft = [], drag = null;
    const ps = cam.parcel_status || {};
    const card = h(`<div class="card wide smart-card">
      <h2>Parcels, lines, loitering and property: ${esc(cam.name)}</h2>
      <p class="muted" style="margin-top:0">Pick what to draw, then draw on the picture. These work best with the camera
        kept still (patrol off), because the drawings stay in the same place on the picture.</p>
      <div class="seg smart-tools">
        <button data-t="line">Line</button><button data-t="area">Loitering area</button><button data-t="parcel">Doorstep</button>
        <button data-t="property">Our property</button>
      </div>
      <p class="muted small tool-help"></p>
      <div class="zone-editor smart-editor"><img alt=""><canvas></canvas></div>
      <div class="toolbar"><button class="finish">Finish area</button><button class="undo">Undo point</button></div>

      <h3>Parcel watch</h3>
      <div class="parcel-opts">
      <label class="check"><input type="checkbox" class="pc-on"> Tell me when a parcel is left on the doorstep, and if it's taken</label>
      <label class="check">When a parcel is taken
        <select class="pc-taken"><option value="away">alert only if nobody's home</option><option value="always">always alert</option></select></label>
      </div>
      <div class="parcel-status"></div>

      <h3>Our property</h3>
      <p class="muted small" style="margin-top:0">People's build and clothes, and visitors, are only learnt from people standing
        here, so neighbours and passers-by on the pavement aren't remembered. Leave it empty to use the whole picture.</p>
      <div class="property-status"></div>

      <h3>Lines</h3>
      <p class="muted small" style="margin-top:0">Get an alert when someone (or a car) crosses the line, such as into the drive.</p>
      <div class="rule-list lines"></div>
      <h3>Loitering areas</h3>
      <p class="muted small" style="margin-top:0">Get an alert when someone stays in the area too long, such as by the car.</p>
      <div class="rule-list areas"></div>
      <div class="toolbar"><div style="flex:1"></div><button class="primary save">Save</button></div>
    </div>`);

    const img = q('img', card), canvas = q('canvas', card), editor = q('.smart-editor', card);
    img.addEventListener('load', () => { editor.style.aspectRatio = `${img.naturalWidth}/${img.naturalHeight}`; draw(); });
    img.addEventListener('error', () => { editor.classList.add('no-picture'); draw(); });
    img.src = `/api/cameras/${camId}/snapshot.jpg?t=${Date.now()}`;

    q('.pc-on', card).checked = !!rules.parcel.enabled;
    q('.pc-taken', card).value = data.settings.parcels.taken_alert;
    q('.pc-taken', card).addEventListener('change', async (ev) => {
      try { await send('/api/smart/settings', 'PUT', {parcels: {taken_alert: ev.target.value}}); toast('Saved'); } catch (e) { toast(e.message); }
    });
    const status = q('.parcel-status', card);
    if (rules.parcel.enabled) {
      const waiting = ps.state === 'parcel';
      status.innerHTML = `<p>${waiting ? `📦 <b>A parcel is waiting</b>${ps.since ? ' since ' + esc(when(ps.since)) : ''}.`
        : ps.state === 'learning' ? 'Learning what the empty doorstep looks like…' : 'No parcel on the doorstep.'}
        <button class="clear">Doorstep is clear now</button></p>
        ${waiting && ps.picture ? `<img class="parcel-pic" src="${pic(ps.picture)}" alt="The parcel">` : ''}`;
      q('.clear', status).addEventListener('click', async () => {
        try { await api(`/api/smart/cameras/${camId}/parcel/clear`, {method: 'POST'}); toast('OK. It will learn the empty doorstep again.'); loadSmart(); }
        catch (e) { toast(e.message); }
      });
    }

    const HELP = {
      line: 'Line: press where it starts and drag to where it ends (or tap the two ends). The arrow shows the "in" direction.',
      area: 'Loitering area: drag across the picture to draw a box, or tap points around any shape and press Finish area.',
      parcel: 'Doorstep: drag a box (or tap points around it) where parcels are left. Drawing again replaces it.',
      property: 'Our property: tap points around your drive, garden and doorstep (not the pavement) and press Finish area, or drag a box. Drawing again replaces it.',
    };
    function setTool(t) {
      tool = t; draft = [];
      qa('.smart-tools button', card).forEach(b => b.classList.toggle('active', b.dataset.t === t));
      q('.tool-help', card).textContent = HELP[t];
      q('.finish', card).classList.toggle('hidden', t === 'line');
      draw();
    }
    qa('.smart-tools button', card).forEach(b => b.addEventListener('click', () => setTool(b.dataset.t)));

    function poly(ctx, pts, W, H, close) {
      ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
      if (close) ctx.closePath();
    }
    function label(ctx, text, x, y) {
      ctx.font = `${12 * devicePixelRatio}px system-ui, sans-serif`;
      const w = ctx.measureText(text).width + 8 * devicePixelRatio;
      ctx.fillStyle = 'rgba(0,0,0,.6)'; ctx.fillRect(x, y - 14 * devicePixelRatio, w, 18 * devicePixelRatio);
      ctx.fillStyle = '#fff'; ctx.fillText(text, x + 4 * devicePixelRatio, y);
    }
    function arrowLine(ctx, ln, W, H, color) {
      const ax = ln.a[0] * W, ay = ln.a[1] * H, bx = ln.b[0] * W, by = ln.b[1] * H;
      ctx.strokeStyle = color; ctx.lineWidth = 3 * devicePixelRatio;
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
      // The arrow points to the "in" side: crossing towards it counts as crossing in.
      const dx = ln.b[0] - ln.a[0], dy = ln.b[1] - ln.a[1];
      let nx = -dy * W, ny = dx * H; const len = Math.hypot(nx, ny) || 1; nx /= len; ny /= len;
      const mx = (ax + bx) / 2, my = (ay + by) / 2, L = 26 * devicePixelRatio;
      const tx = mx + nx * L, ty = my + ny * L;
      ctx.beginPath(); ctx.moveTo(mx, my); ctx.lineTo(tx, ty); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(tx, ty);
      ctx.lineTo(tx - nx * 9 * devicePixelRatio - ny * 6 * devicePixelRatio, ty - ny * 9 * devicePixelRatio + nx * 6 * devicePixelRatio);
      ctx.lineTo(tx - nx * 9 * devicePixelRatio + ny * 6 * devicePixelRatio, ty - ny * 9 * devicePixelRatio - nx * 6 * devicePixelRatio);
      ctx.closePath(); ctx.fillStyle = color; ctx.fill();
      if (ln.direction === 'both') {
        const ux = mx - nx * L, uy = my - ny * L;
        ctx.beginPath(); ctx.moveTo(mx, my); ctx.lineTo(ux, uy); ctx.stroke();
      }
      label(ctx, ln.name, Math.min(ax, bx) + 4, Math.min(ay, by) - 6);
    }
    function draw() {
      const r = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, r.width * devicePixelRatio); canvas.height = Math.max(1, r.height * devicePixelRatio);
      const ctx = canvas.getContext('2d'); const W = canvas.width, H = canvas.height;
      ctx.clearRect(0, 0, W, H); ctx.lineWidth = 2 * devicePixelRatio;
      if (rules.property.length) {
        poly(ctx, rules.property, W, H, true); ctx.fillStyle = 'rgba(78,161,255,.10)'; ctx.fill();
        ctx.strokeStyle = '#4ea1ff'; ctx.setLineDash([3 * devicePixelRatio, 3 * devicePixelRatio]); ctx.stroke(); ctx.setLineDash([]);
        label(ctx, 'Our property', rules.property[0][0] * W + 4, rules.property[0][1] * H + 16 * devicePixelRatio);
      }
      if (rules.parcel.area.length) {
        poly(ctx, rules.parcel.area, W, H, true); ctx.fillStyle = 'rgba(255,170,50,.18)'; ctx.fill();
        ctx.strokeStyle = '#ffaa32'; ctx.setLineDash([6 * devicePixelRatio, 4 * devicePixelRatio]); ctx.stroke(); ctx.setLineDash([]);
        label(ctx, 'Doorstep', rules.parcel.area[0][0] * W + 4, rules.parcel.area[0][1] * H - 6);
      }
      for (const a of rules.areas) {
        poly(ctx, a.points, W, H, true); ctx.fillStyle = 'rgba(255,77,79,.15)'; ctx.fill();
        ctx.strokeStyle = '#ff4d4f'; ctx.lineWidth = 2 * devicePixelRatio; ctx.stroke();
        label(ctx, a.name, a.points[0][0] * W + 4, a.points[0][1] * H - 6);
      }
      for (const ln of rules.lines) arrowLine(ctx, ln, W, H, '#3ccf91');
      ctx.lineWidth = 2 * devicePixelRatio;
      if (drag && drag.moved) {
        if (tool === 'line') arrowLine(ctx, {a: drag.a, b: drag.b, name: '', direction: 'arrow'}, W, H, '#4ea1ff');
        else { poly(ctx, box(drag), W, H, true); ctx.strokeStyle = '#4ea1ff'; ctx.stroke(); }
      } else if (draft.length) {
        poly(ctx, draft, W, H, false); ctx.strokeStyle = '#4ea1ff'; ctx.stroke();
        for (const [x, y] of draft) { ctx.beginPath(); ctx.arc(x * W, y * H, 4 * devicePixelRatio, 0, 7); ctx.fillStyle = '#4ea1ff'; ctx.fill(); }
      }
    }
    const box = (d) => { const [x1, y1] = d.a, [x2, y2] = d.b; return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]; };
    const at = (e) => {
      const r = canvas.getBoundingClientRect(); const f = (v) => +Math.max(0, Math.min(1, v)).toFixed(4);
      return [f((e.clientX - r.left) / r.width), f((e.clientY - r.top) / r.height)];
    };
    const uid = (p) => p + Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
    function addShape(pts) {
      if (tool === 'parcel') { rules.parcel.area = pts; rules.parcel.enabled = true; q('.pc-on', card).checked = true; }
      else if (tool === 'property') rules.property = pts;
      else if (tool === 'area') rules.areas.push({id: uid('area'), name: `Area ${rules.areas.length + 1}`, points: pts, seconds: 30, what: 'person', enabled: true});
      lists(); draw();
    }
    function addLine(a, b) {
      if (Math.hypot(a[0] - b[0], a[1] - b[1]) < 0.03) return;
      rules.lines.push({id: uid('line'), name: `Line ${rules.lines.length + 1}`, a, b, direction: 'both', what: 'person', enabled: true});
      lists(); draw();
    }
    canvas.addEventListener('pointerdown', (e) => {
      e.preventDefault(); canvas.setPointerCapture && canvas.setPointerCapture(e.pointerId);
      drag = {a: at(e), b: at(e), x: e.clientX, y: e.clientY, moved: false};
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!drag) return; drag.b = at(e);
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 10) drag.moved = true;
      if (drag.moved) draw();
    });
    canvas.addEventListener('pointerup', (e) => {
      if (!drag) return; const d = drag; drag = null; d.b = at(e);
      if (d.moved && !draft.length) {
        if (tool === 'line') addLine(d.a, d.b);
        else if (Math.abs(d.b[0] - d.a[0]) > 0.01 && Math.abs(d.b[1] - d.a[1]) > 0.01) addShape(box(d));
      } else {
        draft.push(d.a);
        if (tool === 'line' && draft.length === 2) { addLine(draft[0], draft[1]); draft = []; }
      }
      draw();
    });
    canvas.addEventListener('pointercancel', () => { drag = null; draw(); });
    q('.finish', card).addEventListener('click', () => {
      if (draft.length < 3) { toast('Tap at least 3 points around the area, or drag to draw a box.'); return; }
      addShape(draft); draft = [];
    });
    q('.undo', card).addEventListener('click', () => { draft.pop(); draw(); });
    window.addEventListener('resize', draw);

    const opts = (list, v) => list.map(([k, t]) => `<option value="${k}" ${String(k) === String(v) ? 'selected' : ''}>${esc(t)}</option>`).join('');
    function lists() {
      const P = q('.property-status', card);
      P.innerHTML = rules.property.length ? '<span>Drawn. Press Save to keep it.</span> <button class="clear-prop">Clear</button>'
        : '<span class="muted">Not drawn: the whole picture counts. Choose Our property above and draw it.</span>';
      const cp = q('.clear-prop', P);
      if (cp) cp.addEventListener('click', () => { rules.property = []; lists(); draw(); });
      const L = q('.lines', card); L.innerHTML = rules.lines.length ? '' : '<span class="muted">No lines yet. Choose Line above and draw one.</span>';
      rules.lines.forEach((ln, i) => {
        const row = h(`<div class="rule-row">
          <input type="checkbox" class="on" title="On" ${ln.enabled ? 'checked' : ''}>
          <input class="nm" value="${esc(ln.name)}" maxlength="40" aria-label="Name">
          <select class="what">${opts(WHAT, ln.what)}</select>
          <select class="dir">${opts([['both', 'crossing either way'], ['arrow', 'crossing in the arrow\'s direction']], ln.direction)}</select>
          <button class="flip" title="Point the arrow the other way">⇄ Flip</button>
          <button class="danger rm" aria-label="Remove">✕</button></div>`);
        q('.on', row).addEventListener('change', (e) => { ln.enabled = e.target.checked; });
        q('.nm', row).addEventListener('input', (e) => { ln.name = e.target.value; draw(); });
        q('.what', row).addEventListener('change', (e) => { ln.what = e.target.value; });
        q('.dir', row).addEventListener('change', (e) => { ln.direction = e.target.value; draw(); });
        q('.flip', row).addEventListener('click', () => { [ln.a, ln.b] = [ln.b, ln.a]; draw(); });
        q('.rm', row).addEventListener('click', () => { rules.lines.splice(i, 1); lists(); draw(); });
        L.appendChild(row);
      });
      const A = q('.areas', card); A.innerHTML = rules.areas.length ? '' : '<span class="muted">No areas yet. Choose Loitering area above and draw one.</span>';
      rules.areas.forEach((ar, i) => {
        const secs = SECS.some(([s]) => s === ar.seconds) ? SECS : [...SECS, [ar.seconds, ar.seconds + ' s']];
        const row = h(`<div class="rule-row">
          <input type="checkbox" class="on" title="On" ${ar.enabled ? 'checked' : ''}>
          <input class="nm" value="${esc(ar.name)}" maxlength="40" aria-label="Name">
          <select class="what">${opts(WHAT, ar.what)}</select>
          <label class="check">staying longer than <select class="secs">${opts(secs, ar.seconds)}</select></label>
          <button class="danger rm" aria-label="Remove">✕</button></div>`);
        q('.on', row).addEventListener('change', (e) => { ar.enabled = e.target.checked; });
        q('.nm', row).addEventListener('input', (e) => { ar.name = e.target.value; draw(); });
        q('.what', row).addEventListener('change', (e) => { ar.what = e.target.value; });
        q('.secs', row).addEventListener('change', (e) => { ar.seconds = +e.target.value; });
        q('.rm', row).addEventListener('click', () => { rules.areas.splice(i, 1); lists(); draw(); });
        A.appendChild(row);
      });
    }
    q('.pc-on', card).addEventListener('change', (e) => {
      rules.parcel.enabled = e.target.checked;
      if (e.target.checked && !rules.parcel.area.length) { setTool('parcel'); toast('Now draw the doorstep on the picture, then press Save.'); }
    });
    q('.save', card).addEventListener('click', async (ev) => {
      if (draft.length >= 3 && tool !== 'line') { addShape(draft); draft = []; }
      if (rules.parcel.enabled && !rules.parcel.area.length) { toast('Draw the doorstep first (choose Doorstep above).'); return; }
      const done = busy(ev.target, 'Saving…');
      try {
        const saved = await api(`/api/smart/cameras/${camId}`, {method: 'PUT', body: JSON.stringify(rules)});
        Object.assign(rules, saved); toast('Saved'); lists(); draw();
      } catch (e) { toast('Could not save: ' + e.message); }
      done();
    });
    setTool(rules.lines.length || !rules.parcel.area.length ? 'line' : 'area');
    lists();
    return card;
  }

  // ---------- recent smart alerts ----------
  function activityCard() {
    const card = h(`<div class="card wide smart-card"><h2>Recent smart alerts</h2><div class="smart-activity"></div></div>`);
    const list = q('.smart-activity', card);
    if (!data.activity.length) list.innerHTML = '<p class="muted">Nothing yet.</p>';
    for (const a of data.activity) {
      const row = h(`<div class="act-row">
        ${a.picture ? `<img src="${esc(a.picture)}" alt="" loading="lazy">` : '<span class="noimg"></span>'}
        <div><b>${esc(a.title)}</b><div class="muted">${esc(a.message || '')}</div>
        <div class="muted small">${esc(when(a.time))}${a.alert === false ? ' · no alert sent' : ''}</div></div>
        ${a.clip ? '<button class="watch">Watch</button>' : ''}</div>`);
      const w = q('.watch', row);
      if (w) w.addEventListener('click', () => { location.hash = 'clip=' + a.clip; location.reload(); });
      list.appendChild(row);
    }
    return card;
  }
})();
