'use strict';
// Settings > Away from home. Uses $, api, esc and toast from app.js.
// The phone app adds window.HomeCamera.pairRemote / remoteInfo; a PC browser doesn't have them.

const remotePhone = window.HomeCamera && window.HomeCamera.pairRemote ? window.HomeCamera : null;

async function remoteCard() {
  const card = document.createElement('div'); card.className = 'card rm-card';
  card.innerHTML = '<h2>Away from home</h2><p class="muted">Loading…</p>';
  let s;
  try { s = await api('/api/remote'); } catch (e) {
    card.innerHTML = `<h2>Away from home</h2><p class="muted">Not available: ${esc(e.message)}</p>`;
    return card;
  }
  drawRemote(card, s);
  return card;
}

function phoneRemoteInfo() {
  try { return remotePhone && remotePhone.remoteInfo ? JSON.parse(remotePhone.remoteInfo()) : null; } catch (e) { return null; }
}

function remoteStatusText(s) {
  if (!s.enabled) return '<span class="muted">Off. The cameras can only be seen at home.</span>';
  if (s.listening.state === 'error') return `<span class="warn-text">⚠ ${esc(s.listening.message)}</span>`;
  if (s.warning) return `<span class="warn-text">⚠ ${esc(s.warning)}</span>`;
  if (s.upnp && s.router.state === 'error') return `<span class="warn-text">⚠ ${esc(s.router.message)}</span>`;
  if (!s.away_host) return `<span class="warn-text">⚠ Your home's internet address isn't known yet. Enter your own
    address below, or turn on UPnP on your router.</span>`;
  const how = s.upnp ? esc(s.router.message) : 'You forward the port on your router yourself.';
  return `<span class="ok-text">✓ On.</span> Paired phones reach this PC at <b>${esc(s.away_host)}:${s.port}</b>. ${how}`;
}

function fmtWhen(t) {
  if (!t) return 'never';
  const d = new Date(t); if (isNaN(d)) return t;
  return d.toLocaleString(undefined, {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
}

function drawRemote(card, s) {
  const me = phoneRemoteInfo();
  const devices = s.devices.map(d => `<li data-id="${esc(d.id)}"><b>${esc(d.name)}</b>
      ${d.id === s.this_device ? '<span class="pill">this phone</span>' : ''}
      <span class="muted">set up ${esc(fmtWhen(d.created))}, last seen ${esc(fmtWhen(d.last_seen))}${d.last_ip ? ' from ' + esc(d.last_ip) : ''}</span>
      <button class="danger rm-remove">Remove</button></li>`).join('');
  let phoneBit;
  if (!s.enabled) phoneBit = '';
  else if (remotePhone) {
    phoneBit = me && me.paired
      ? `<p class="ok-text">✓ This phone is set up. It uses ${esc(me.route === 'away' ? 'the away address now' : 'the home address now')}.</p>
         <div class="toolbar"><button class="rm-pair">Set up this phone again</button></div>`
      : `<div class="toolbar"><button class="primary rm-pair">Set up this phone for away from home</button></div>
         <p class="hint muted">Do this now, while the phone is on your home Wi-Fi.</p>`;
  } else {
    phoneBit = `<p class="muted">To add a phone: open the A.S.D Home Security app on the phone while it's on your
      home Wi-Fi, go to Settings > Away from home and press <b>Set up this phone</b>.</p>`;
  }
  const manual = s.enabled && (!s.upnp || s.router.state === 'error') ? `<div class="rm-manual">
      <h3>Forwarding the port by hand</h3>
      <p>In your router's settings (often under <i>Port forwarding</i>, <i>Virtual servers</i> or <i>NAT</i>), add:</p>
      <ul><li>Protocol <b>TCP</b>, outside port <b>${s.port}</b></li>
        <li>To this PC, <b>${esc(s.lan_ip || 'this PC')}</b>, port <b>${s.port}</b></li></ul>
      <p class="muted">Give this PC a fixed address on the router (often <i>DHCP reservation</i>) so the rule keeps working.</p></div>` : '';
  card.innerHTML = `<h2>Away from home</h2>
    <p style="margin-top:0">See your cameras and get alerts when you're out. The phone app connects straight to this
      PC: no outside company, cloud or extra software is involved. Everything is encrypted with this PC's own
      certificate, which the phone checks every time, and only phones you set up here at home can connect.
      Anyone else gets no answer, and addresses that keep trying are blocked.</p>
    <p class="rm-status">${remoteStatusText(s)}</p>
    <div class="field"><label for="rm-on">Allow away-from-home access</label>
      <input type="checkbox" id="rm-on" ${s.enabled ? 'checked' : ''}></div>
    <div class="field"><label for="rm-port">Port</label>
      <input type="number" id="rm-port" min="1024" max="65535" value="${s.port}">
      <div class="hint">The encrypted port phones use. 8443 is fine unless something else on this PC uses it.</div></div>
    <div class="field"><label for="rm-upnp">Open the port on my router automatically</label>
      <input type="checkbox" id="rm-upnp" ${s.upnp ? 'checked' : ''}>
      <div class="hint">Uses UPnP, which most home routers have. Only the router is spoken to. Turn this off if you'd
        rather forward the port on your router yourself.</div></div>
    <div class="field"><label for="rm-addr">Your own address (optional)</label>
      <input type="text" id="rm-addr" class="bk-path" placeholder="${esc(s.router.external_ip || 'e.g. myhome.example.com')}" value="${esc(s.address)}">
      <div class="hint">Leave empty to use your home's internet address${s.router.external_ip ? ` (now <b>${esc(s.router.external_ip)}</b>)` : ''},
        read from your router. If your provider changes it, phones pick up the new one next time they're home. To
        avoid that, ask your provider for a fixed address, or enter a name your router keeps up to date.</div></div>
    <div class="toolbar"><button class="primary rm-save">Save</button></div>
    ${manual}
    ${phoneBit}
    ${s.enabled ? `<h3>Phones set up for away</h3>
      ${devices ? `<ul class="rm-devices">${devices}</ul>` : '<p class="muted">None yet.</p>'}
      <p class="muted">Lost a phone? Remove it here and it can't connect any more.</p>
      <p class="muted">This PC's certificate: <code>${esc(s.fingerprint)}</code></p>
      <p class="muted">Turned away in the last 24 hours: ${s.refused_24h} request${s.refused_24h === 1 ? '' : 's'}
        without a key${s.blocked_now ? `, ${s.blocked_now} address${s.blocked_now === 1 ? '' : 'es'} blocked now` : ''}.</p>` : ''}`;

  $('.rm-save', card).addEventListener('click', async (ev) => {
    const btn = ev.target; btn.disabled = true;
    try {
      const r = await api('/api/remote', {method: 'PUT', body: JSON.stringify({
        enabled: $('#rm-on', card).checked, port: $('#rm-port', card).value,
        upnp: $('#rm-upnp', card).checked, address: $('#rm-addr', card).value,
      })});
      drawRemote(card, {...r, this_device: s.this_device});
      toast(r.enabled ? 'Saved. Checking your router…' : 'Away access is off');
      if (r.enabled) setTimeout(() => refreshRemote(card), 5000);
    } catch (e) { toast('Could not save: ' + e.message); btn.disabled = false; }
  });
  const pairBtn = $('.rm-pair', card);
  if (pairBtn) pairBtn.addEventListener('click', async () => {
    pairBtn.disabled = true;
    try {
      const name = remotePhone.deviceName ? remotePhone.deviceName() : 'Phone';
      const r = await api('/api/remote/pair', {method: 'POST', body: JSON.stringify({name})});
      toast('Setting up this phone…');
      remotePhone.pairRemote(JSON.stringify(r));  // the app reloads over the encrypted connection
    } catch (e) { toast('Could not set up: ' + e.message); pairBtn.disabled = false; }
  });
  card.querySelectorAll('.rm-remove').forEach(b => b.addEventListener('click', async () => {
    const li = b.closest('li'); const id = li.dataset.id;
    const self = id === s.this_device;
    if (!confirm(self ? 'Remove this phone? It will stop working away from home until you set it up again.'
                      : `Remove ${li.querySelector('b').textContent}? It won't be able to connect any more.`)) return;
    try {
      await api(`/api/remote/devices/${encodeURIComponent(id)}`, {method: 'DELETE'});
      if (self && remotePhone && remotePhone.unpairRemote) { remotePhone.unpairRemote(); return; }
      toast('Removed'); refreshRemote(card, true);
    } catch (e) { toast(e.message); }
  }));
}

async function refreshRemote(card, redraw) {
  if (!card.isConnected) return;
  try {
    const s = await api('/api/remote');
    if (redraw) drawRemote(card, s); else $('.rm-status', card).innerHTML = remoteStatusText(s);
  } catch (e) { /* next time */ }
}

// Called by the phone app after pairing: {"ok": bool, "message": "..."}.
window.onRemotePaired = (json) => {
  try { const r = JSON.parse(json); toast(r.message); } catch (e) { /* ignore */ }
};
window.remoteCard = remoteCard;
