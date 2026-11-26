'use strict';
// Home/away mode and health alerts: the header switch, the problem banner on the Live tab,
// and the two cards in Settings. Uses helpers from app.js ($, api, esc, toast, status).

const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const phone = window.HomeCamera && window.HomeCamera.locationInfo ? window.HomeCamera : null;

function ago(epoch) {
  if (!epoch) return 'never';
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(epoch * 1000).toLocaleDateString([], {day: 'numeric', month: 'short'});
}

function alertsText(p) {
  if (p.alerts === 'all') return 'alerts on';
  if (p.alerts === 'people') return 'people alerts only';
  return 'alerts paused';
}

// ---------- header switch and Live banner ----------
function renderPresencePill(p) {
  const pill = $('#presence-pill');
  if (!pill || !p) return;
  pill.classList.remove('hidden');
  pill.classList.toggle('home', p.where === 'home');
  pill.textContent = `${p.where === 'home' ? 'Home' : 'Away'} · ${alertsText(p)}`;
  pill.title = p.reason + (p.mode !== 'auto' ? ' (press to change)' : '. Press to change.');
}

function renderHealthBanner(problems) {
  const b = $('#health-banner');
  if (!b) return;
  b.classList.toggle('hidden', !problems || !problems.length);
  b.innerHTML = (problems || []).map(p =>
    `<div><b>${esc(p.title)}</b> <span>${esc(p.message)}</span></div>`).join('');
}

window.onAlertStatus = function (st) {
  renderPresencePill(st.presence);
  renderHealthBanner(st.health);
};

window.onAlertEvent = function (e) {
  if (e.type === 'health' && e.state === 'problem') toast(`${e.title}. ${e.message}`);
  if (e.type === 'health' && e.state === 'ok') toast(e.title);
  if (e.type === 'presence') {
    toast(`${e.where === 'home' ? 'Home' : 'Away'}: ${alertsText(e)}`);
    const card = $('#presence-card');
    if (card && document.body.contains(card)) refreshPresenceCard(card);
  }
};

const pill = $('#presence-pill');
if (pill) pill.addEventListener('click', async () => {
  showTab('settings');
  // Settings are rebuilt when the tab opens; wait for the card, then bring it into view.
  for (let i = 0; i < 30; i++) {
    const card = $('#presence-card');
    if (card) { card.scrollIntoView({behavior: 'smooth', block: 'start'}); break; }
    await new Promise(r => setTimeout(r, 100));
  }
});

// ---------- Settings: Home and away ----------
function scheduleRow(r) {
  const row = document.createElement('div'); row.className = 'sched-row';
  const days = r ? r.days : [0, 1, 2, 3, 4, 5, 6];
  row.innerHTML = `<div class="sched-days">${DAY_NAMES.map((d, i) =>
    `<label><input type="checkbox" value="${i}" ${days.includes(i) ? 'checked' : ''}>${d}</label>`).join('')}</div>
    <label>From <input type="time" class="s-start" value="${esc(r ? r.start : '23:00')}"></label>
    <label>to <input type="time" class="s-end" value="${esc(r ? r.end : '06:30')}"></label>
    <button class="danger s-rm" title="Remove">✕</button>`;
  $('.s-rm', row).addEventListener('click', () => row.remove());
  return row;
}

function readSchedule(card) {
  return $$('.sched-row', card).map(row => ({
    days: $$('.sched-days input', row).filter(i => i.checked).map(i => Number(i.value)),
    start: $('.s-start', row).value, end: $('.s-end', row).value,
  })).filter(r => r.days.length && r.start && r.end);
}

function phoneSection(card, p) {
  const box = $('.this-phone', card);
  if (!phone) {
    box.innerHTML = `<p class="muted">To switch automatically, open the <b>A.S.D Home Security</b> app on each phone,
      go to Settings > Home and away, and press <b>Use this phone's location</b>.</p>`;
    return;
  }
  let info = {};
  try { info = JSON.parse(phone.locationInfo()); } catch (e) {}
  const listed = (p.phones || []).some(x => x.id === info.phoneId);
  if (!info.available) {
    box.innerHTML = `<p class="muted">This phone can't share its location: it needs Google Play services.
      You can still switch between Home and Away above.</p>`;
    return;
  }
  if (info.on && info.homeSet) {
    box.innerHTML = `<p><b>This phone</b> (${esc(info.name)}) tells A.S.D Home Security when it arrives home or leaves.
      ${info.permission !== 'always' ? '<br><span class="warn-text">Location is not allowed "all the time" yet, so it cannot switch by itself. Press Fix.</span>' : ''}</p>
      <div class="toolbar">
        <label>Home area <select class="radius">${[100, 150, 250, 500].map(m =>
          `<option value="${m}" ${m === info.radius ? 'selected' : ''}>${m} m around the house</option>`).join('')}</select></label>
        <button class="set-home">I'm at home: set it here</button>
        ${info.permission !== 'always' ? '<button class="primary fix">Fix</button>' : ''}
        <button class="danger loc-off">Stop using this phone</button>
      </div>`;
  } else {
    box.innerHTML = `<p><b>This phone</b> isn't sharing its location${listed ? '' : ' yet'}.
      While you're at home, press the button. Your phone will ask to use your location
      "all the time" so it can tell A.S.D Home Security when you come and go. Only "home" or "out" is sent to your PC,
      never where you are.</p>
      <div class="toolbar">
        <label>Home area <select class="radius">${[100, 150, 250, 500].map(m =>
          `<option value="${m}" ${m === 150 ? 'selected' : ''}>${m} m around the house</option>`).join('')}</select></label>
        <button class="primary set-home">Use this phone's location</button>
      </div>`;
  }
  const radius = () => Number(($('.radius', box) || {}).value || 150);
  const setHome = $('.set-home', box);
  if (setHome) setHome.addEventListener('click', () => { setHome.disabled = true; phone.locationSetHome(radius()); });
  const fix = $('.fix', box);
  if (fix) fix.addEventListener('click', () => phone.locationSetHome(radius()));
  const off = $('.loc-off', box);
  if (off) off.addEventListener('click', async () => {
    phone.locationOff();
    try { await api('/api/presence/phones/' + encodeURIComponent(info.phoneId), {method: 'DELETE'}); } catch (e) {}
    refreshPresenceCard(card);
  });
}

// Called by the phone app when setting up its location finishes (or fails).
window.onPhoneLocation = function (result) {
  let r = {};
  try { r = typeof result === 'string' ? JSON.parse(result) : result; } catch (e) {}
  if (r.message) toast(r.message);
  const card = $('#presence-card');
  if (card) refreshPresenceCard(card);
};

async function refreshPresenceCard(card) {
  let p;
  try { p = await api('/api/presence'); } catch (e) { return; }
  status.presence = p; renderPresencePill(p);
  $('.where', card).innerHTML = `<b>${p.where === 'home' ? 'Home' : 'Away'}</b>, ${esc(alertsText(p))}.
    <span class="muted">${esc(p.reason)}.</span>`;
  $$('.mode button', card).forEach(b => b.classList.toggle('active', b.dataset.mode === p.mode));
  $('.mode-hint', card).textContent = p.mode === 'auto'
    ? 'Follows your phones: alerts pause while anyone is home.'
    : `Set by hand. It goes back to automatic when a phone arrives home or leaves.`;
  const list = $('.phones', card);
  list.innerHTML = p.phones.length ? p.phones.map(x =>
    `<div class="phone-row"><span>${esc(x.name)}</span>
      <span class="pill ${x.stale ? 'off' : x.home ? 'ok' : ''}">${x.stale ? 'not heard from' : x.home ? 'home' : 'out'}</span>
      <span class="muted">${esc(ago(x.seen))}</span>
      <button class="forget" data-id="${esc(x.id)}" title="Stop counting this phone">✕</button></div>`).join('')
    : '<p class="muted">No phones yet.</p>';
  $$('.forget', list).forEach(b => b.addEventListener('click', async () => {
    if (!confirm('Stop counting this phone? Set it up again in the app to add it back.')) return;
    await api('/api/presence/phones/' + encodeURIComponent(b.dataset.id), {method: 'DELETE'});
    refreshPresenceCard(card);
  }));
  phoneSection(card, p);
  return p;
}

async function presenceCard() {
  const card = document.createElement('div'); card.className = 'card'; card.id = 'presence-card';
  card.innerHTML = `<h2>Home and away</h2>
    <p class="where"></p>
    <div class="seg mode">
      <button data-mode="auto">Automatic</button><button data-mode="home">Home</button><button data-mode="away">Away</button>
    </div>
    <div class="hint mode-hint"></div>
    <h3>Phones</h3>
    <div class="phones"></div>
    <div class="this-phone"></div>
    <h3>While someone is home</h3>
    <div class="field"><label for="home-alerts">Still alert me for</label>
      <select id="home-alerts"><option value="none">Nothing</option><option value="people">People only</option></select>
      <div class="hint">Recording carries on as normal. Only the phone alerts are held back.</div></div>
    <h3>Alert hours</h3>
    <p class="hint" style="margin-top:0">Alerts are always on at these times, even when you're home. Handy overnight.</p>
    <div class="sched"></div>
    <div class="toolbar"><button class="add-sched">+ Add alert hours</button>
      <div style="flex:1"></div><button class="primary save-presence">Save</button></div>`;
  $$('.mode button', card).forEach(b => b.addEventListener('click', async () => {
    try { await api('/api/presence', {method: 'PUT', body: JSON.stringify({mode: b.dataset.mode})}); }
    catch (e) { toast('Could not change: ' + e.message); }
    refreshPresenceCard(card);
  }));
  $('.add-sched', card).addEventListener('click', () => $('.sched', card).appendChild(scheduleRow(null)));
  $('.save-presence', card).addEventListener('click', async () => {
    try {
      const p = await api('/api/presence', {method: 'PUT', body: JSON.stringify({
        home_alerts: $('#home-alerts', card).value, schedule: readSchedule(card)})});
      $('.sched', card).innerHTML = '';
      p.schedule.forEach(r => $('.sched', card).appendChild(scheduleRow(r)));
      toast('Home and away settings saved');
      refreshPresenceCard(card);
    } catch (e) { toast('Could not save: ' + e.message); }
  });
  const p = await refreshPresenceCard(card);
  if (p) {
    $('#home-alerts', card).value = p.home_alerts;
    p.schedule.forEach(r => $('.sched', card).appendChild(scheduleRow(r)));
  }
  return card;
}

// ---------- Settings: Health alerts ----------
async function healthCard() {
  const card = document.createElement('div'); card.className = 'card';
  let h = {problems: [], settings: {}};
  try { h = await api('/api/health'); } catch (e) {}
  const s = h.settings;
  card.innerHTML = `<h2>Health alerts</h2>
    <p class="muted" style="margin-top:0">Your phone is told when something needs looking at, whether you're home or away.</p>
    <div class="problems">${h.problems.length ? h.problems.map(p =>
      `<p class="warn-text"><b>${esc(p.title)}</b><br>${esc(p.message)}</p>`).join('') : '<p>Everything is working.</p>'}</div>
    <div class="field"><label for="offline-min">Camera offline for (minutes)</label>
      <input type="number" id="offline-min" min="0" max="1440" value="${esc(s.camera_offline_minutes)}">
      <div class="hint">Alert when a camera has sent no video for this long. 0 turns it off.</div></div>
    <div class="field"><label for="disk-alerts">Disk nearly full, or recordings folder missing</label>
      <input type="checkbox" id="disk-alerts" ${s.disk_alerts ? 'checked' : ''}></div>
    <div class="field"><label for="rec-alerts">A recording couldn't be saved</label>
      <input type="checkbox" id="rec-alerts" ${s.recording_alerts ? 'checked' : ''}></div>
    <p class="hint">The phone app also warns you itself if it can't reach this PC for 15 minutes,
      for example after a power cut. You can turn that off in the phone's notification settings
      ("Can't reach the PC").</p>
    <button class="primary save-health">Save</button>`;
  $('.save-health', card).addEventListener('click', async () => {
    try {
      await api('/api/health', {method: 'PUT', body: JSON.stringify({
        camera_offline_minutes: $('#offline-min', card).value,
        disk_alerts: $('#disk-alerts', card).checked, recording_alerts: $('#rec-alerts', card).checked})});
      toast('Health alert settings saved');
    } catch (e) { toast('Could not save: ' + e.message); }
  });
  return card;
}

window.alertCards = async function () {
  return [await presenceCard(), await healthCard()];
};
