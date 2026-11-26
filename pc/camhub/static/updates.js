'use strict';
// Settings > Updates. Uses $, api, esc and toast from app.js.

async function updatesCard() {
  const card = document.createElement('div'); card.className = 'card';
  card.innerHTML = '<h2>Updates</h2><p class="muted">Loading…</p>';
  let s;
  try { s = await api('/api/updates'); } catch (e) {
    card.innerHTML = `<h2>Updates</h2><p class="muted">Not available: ${esc(e.message)}</p>`;
    return card;
  }
  drawUpdates(card, s);
  return card;
}

function phoneInfo() {
  try {
    return window.HomeCamera && window.HomeCamera.updateInfo ? JSON.parse(window.HomeCamera.updateInfo()) : null;
  } catch (e) { return null; }
}

function when(t) {
  return t ? new Date(t * 1000).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : '';
}

function hourOptions(sel) {
  let o = '';
  for (let h = 0; h < 24; h++) o += `<option value="${h}" ${h === sel ? 'selected' : ''}>${String(h).padStart(2, '0')}:00</option>`;
  return o;
}

function updateStatusText(s) {
  if (s.installing) return '<span class="ok-text">Installing now. Home Camera restarts in a minute or two.</span>';
  if (s.state === 'checking') return '<span class="muted">Checking for updates…</span>';
  if (s.state === 'error') return `<span class="warn-text">⚠ ${esc(s.message)}</span>`;
  if (s.state === 'ready') return `<span class="ok-text">${esc(s.message)}</span>`;
  if (s.state === 'ok') return `<span class="ok-text">✓ ${esc(s.message)}</span>`;
  return '<span class="muted">Not checked yet. Home Camera checks every few hours.</span>';
}

function drawUpdates(card, s) {
  const phone = phoneInfo();
  const last = (s.history || [])[0];
  const phoneNewer = phone && s.phone_version && s.phone_version !== phone.version;
  card.innerHTML = `<h2>Updates</h2>
    <p style="margin-top:0">Home Camera updates itself from the download page. Your settings and
      recordings are kept.</p>
    <p class="up-status">${updateStatusText(s)}${s.checked_at ? ` <span class="muted">Checked ${esc(when(s.checked_at))}.</span>` : ''}</p>
    ${s.last_failed ? `<p class="warn-text">⚠ ${esc(s.last_failed)}</p>` : ''}
    ${last ? `<p class="muted">Updated from ${esc(last.from)} to ${esc(last.to)} on ${esc(when(last.at))}.</p>` : ''}
    <p>This PC: <b>${esc(s.current)}</b>${s.latest ? ` · newest: ${esc(s.latest)}` : ''}</p>
    ${phone ? `<p>This phone: <b>${esc(phone.version)}</b>${s.phone_version ? ` · newest: ${esc(s.phone_version)}` : ''}</p>
      ${phone.problem ? `<p class="warn-text">⚠ ${esc(phone.problem)}</p>` : ''}` : ''}
    <div class="field"><label for="up-auto">Install PC updates by themselves</label>
      <input type="checkbox" id="up-auto" ${s.auto_install ? 'checked' : ''} ${s.can_install ? '' : 'disabled'}>
      <div class="hint">Installs between
        <select id="up-from">${hourOptions(s.night_start)}</select> and
        <select id="up-to">${hourOptions(s.night_end)}</select>, and never while a camera is recording.
        Recording pauses for about a minute while it installs.
        ${s.can_install ? '' : '<br>Only the installed Windows app can update itself.'}</div></div>
    <div class="field"><label for="up-key">GitHub key (read-only)</label>
      <input type="password" id="up-key" autocomplete="off" placeholder="${s.has_token ? 'Saved. Paste a new one to replace it.' : 'github_pat_…'}">
      <div class="hint">Needed because the Home Camera project on GitHub is private.
        On github.com go to <b>Settings › Developer settings › Personal access tokens › Fine-grained tokens</b>,
        press <b>Generate new token</b>, choose <b>Only select repositories</b> and pick
        <b>homeawcurity</b>, then under <b>Repository permissions</b> set <b>Contents</b> to <b>Read-only</b>.
        Give it the longest expiry offered. The key is kept on this PC only.
        ${s.has_token ? '<br><a href="#" class="up-forget">Remove the saved key</a>' : ''}</div></div>
    <div class="toolbar"><button class="primary up-save">Save</button>
      <button class="up-check">Check now</button>
      ${s.pc_ready && s.can_install ? `<button class="up-install">Install ${esc(s.pc_ready)} now</button>` : ''}
      ${phone ? `<button class="up-phone" ${phoneNewer ? '' : 'disabled'}>Update phone app</button>` : ''}</div>`;

  const refresh = async () => drawUpdates(card, await api('/api/updates'));
  const busy = (ev, fn) => async () => {
    const btn = ev; btn.disabled = true;
    try { await fn(); } catch (e) { toast(e.message); } finally { btn.disabled = false; }
  };
  const save = $('.up-save', card);
  save.addEventListener('click', busy(save, async () => {
    const body = {auto_install: $('#up-auto', card).checked,
      night_start: +$('#up-from', card).value, night_end: +$('#up-to', card).value};
    const key = $('#up-key', card).value.trim();
    if (key) body.token = key;
    drawUpdates(card, await api('/api/updates', {method: 'PUT', body: JSON.stringify(body)}));
    toast(key ? 'Saved. Checking for updates…' : 'Saved');
    if (key) { await api('/api/updates/check', {method: 'POST'}); poll(); }
  }));
  const forget = $('.up-forget', card);
  if (forget) forget.addEventListener('click', async (e) => {
    e.preventDefault();
    drawUpdates(card, await api('/api/updates', {method: 'PUT', body: JSON.stringify({clear_token: true})}));
  });
  const check = $('.up-check', card);
  check.addEventListener('click', busy(check, async () => {
    drawUpdates(card, await api('/api/updates/check', {method: 'POST'}));
    poll();
  }));
  const inst = $('.up-install', card);
  if (inst) inst.addEventListener('click', busy(inst, async () => {
    if (!confirm('Install the update now? Recording pauses for about a minute.')) return;
    drawUpdates(card, await api('/api/updates/install', {method: 'POST'}));
  }));
  const ph = $('.up-phone', card);
  if (ph) ph.addEventListener('click', () => {
    ph.disabled = true;
    window.HomeCamera.updatePhoneApp();
  });

  // Follow a check that's still running (downloads can take a few minutes).
  function poll() {
    let n = 0;
    const t = setInterval(async () => {
      if (!card.isConnected || ++n > 120) return clearInterval(t);
      const s2 = await api('/api/updates').catch(() => null);
      if (s2 && s2.state !== 'checking') { clearInterval(t); drawUpdates(card, s2); }
    }, 3000);
  }
  if (s.state === 'checking') poll();
}

// The phone app reports how its update went.
window.onPhoneUpdate = (json) => {
  try { const r = JSON.parse(json); if (r.message) toast(r.message); } catch (e) { /* ignore */ }
};
window.updatesCard = updatesCard;
