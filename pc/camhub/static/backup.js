'use strict';
// Settings > Off-site backup. Uses $, api, esc, fmtBytes and toast from app.js.

async function backupCard() {
  const card = document.createElement('div'); card.className = 'card';
  card.innerHTML = '<h2>Off-site backup</h2><p class="muted">Loading…</p>';
  let data;
  try { data = await api('/api/backup'); } catch (e) {
    card.innerHTML = `<h2>Off-site backup</h2><p class="muted">Not available: ${esc(e.message)}</p>`;
    return card;
  }
  drawBackup(card, data);
  return card;
}

function backupStatusText(s) {
  if (!s.enabled) return '<span class="muted">Off. Recordings are only on this PC.</span>';
  if (s.state === 'error') return `<span class="warn-text">⚠ ${esc(s.message)}</span>`;
  const last = s.last_copy ? new Date(s.last_copy).toLocaleString(undefined,
    {weekday: 'short', hour: '2-digit', minute: '2-digit'}) : 'nothing new yet';
  return `<span class="ok-text">✓ Working.</span> ${s.clips || 0} clips (${fmtBytes(s.bytes || 0)}) backed up.
    Last copy: ${esc(last)}.`;
}

function drawBackup(card, data) {
  const s = data.status; const sug = data.suggestions || [];
  const known = sug.some(x => x.path === s.folder);
  const opts = sug.map(x => `<option value="${esc(x.path)}" ${x.path === s.folder ? 'selected' : ''}>${esc(x.label)}</option>`).join('');
  card.innerHTML = `<h2>Off-site backup</h2>
    <p style="margin-top:0">Copies your recordings to OneDrive or Google Drive, so you still have them if this PC
      is stolen or breaks. The picture from the start of each recording is copied within seconds, and the
      video as soon as the recording ends. Deleting a clip here doesn't delete its backup.</p>
    <p class="bk-status">${backupStatusText(s)}</p>
    <div class="field"><label for="bk-on">Back up recordings</label>
      <input type="checkbox" id="bk-on" ${s.enabled ? 'checked' : ''}></div>
    <div class="field bk-where"><label for="bk-pick">Back up to</label>
      <select id="bk-pick">${opts}<option value="" ${known ? '' : 'selected'}>Another folder…</option></select>
      <input type="text" id="bk-folder" class="bk-path ${known ? 'hidden' : ''}" placeholder="C:\\Users\\you\\OneDrive"
        value="${known ? '' : esc(s.folder || '')}">
      <div class="hint">${sug.length ? '' : `No OneDrive or Google Drive folder was found on this PC. OneDrive comes
        with Windows: open it from the Start menu and sign in. For Google Drive, install Google Drive for desktop,
        sign in, and in its settings choose <b>Mirror files</b>. Then reopen this page. `}A folder called
        <b>Home Camera recordings</b> is made inside it.</div></div>
    <div class="field"><label for="bk-days">Keep backups for (days)</label>
      <input type="number" id="bk-days" min="0" max="3650" value="${s.keep_days}">
      <div class="hint">0 keeps them until the size limit below is reached.</div></div>
    <div class="field"><label for="bk-gb">Largest backup size (GB)</label>
      <input type="number" id="bk-gb" min="0" max="100000" step="0.5" value="${s.max_gb}">
      <div class="hint">The oldest backups are removed first to stay under this. Free OneDrive has 5 GB and free
        Google Drive 15 GB (shared with Gmail). 0 means no limit.</div></div>
    <div class="field"><label for="bk-people">Only back up recordings with a person</label>
      <input type="checkbox" id="bk-people" ${s.people_only ? 'checked' : ''}>
      <div class="hint">Saves space. Pictures are still copied straight away.</div></div>
    <div class="toolbar"><button class="primary bk-save">Save</button>
      <button class="bk-now" ${s.enabled ? '' : 'disabled'}>Back up now</button></div>`;
  const pick = $('#bk-pick', card); const other = $('#bk-folder', card);
  pick.addEventListener('change', () => other.classList.toggle('hidden', pick.value !== ''));
  $('.bk-save', card).addEventListener('click', async (ev) => {
    const btn = ev.target; btn.disabled = true;
    try {
      const r = await api('/api/backup', {method: 'PUT', body: JSON.stringify({
        enabled: $('#bk-on', card).checked,
        folder: pick.value || other.value,
        keep_days: $('#bk-days', card).value,
        max_gb: $('#bk-gb', card).value,
        people_only: $('#bk-people', card).checked,
      })});
      drawBackup(card, {status: r.status, suggestions: sug});
      toast(r.status.enabled ? 'Backup settings saved. Copying starts now.' : 'Backup turned off');
      setTimeout(() => refreshBackup(card, sug), 4000);
    } catch (e) { toast('Could not save: ' + e.message); btn.disabled = false; }
  });
  $('.bk-now', card).addEventListener('click', async () => {
    try { await api('/api/backup/run', {method: 'POST'}); toast('Backing up now'); } catch (e) { toast(e.message); }
    setTimeout(() => refreshBackup(card, sug), 4000);
  });
}

async function refreshBackup(card, sug) {
  if (!card.isConnected) return;
  try {
    const d = await api('/api/backup');
    $('.bk-status', card).innerHTML = backupStatusText(d.status);
  } catch (e) { /* try again next time */ }
}
