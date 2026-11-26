'use strict';
// Settings > Smart home: lights, sirens and Alexa routines that switch on when the camera sees
// something. Uses $, $$, api, esc, toast, status from app.js and scheduleRow/readSchedule from alerts.js.
(function () {
  const ACTION_NAMES = {on: 'Turn on', off: 'Turn off', flash: 'Flash', run: 'Start it', send: 'Send a message'};
  const newId = (p) => p + Math.random().toString(16).slice(2, 10);

  const HELP = {
    home_assistant: `In Home Assistant, click your name (bottom left), open <b>Security</b>, and under
      <b>Long-lived access tokens</b> press <b>Create token</b>. Copy it in here. The address is usually
      <b>http://homeassistant.local:8123</b>. Alexa and Google devices linked to Home Assistant work too.`,
    hue: `Enter the Hue Bridge's address (press <b>Find</b> to look for it), then press the round button on top
      of the bridge and press <b>Pair</b> within 30 seconds.`,
    alexa: `Alexa can't be switched on directly, so use a free helper such as <b>Voice Monkey</b>
      (voicemonkey.io): add a "monkey", make an Alexa routine that starts when it's pressed (for example,
      "turn on the porch light" or "announce: someone is at the door"), then copy its <b>trigger link</b>
      (API URL) in here. Add one of these for each routine.`,
    webhook: `Calls a web address, for other systems such as IFTTT, Node-RED or a smart plug with a web link.
      You can put {camera}, {event} and {time} in the address or message.`,
    mqtt: `Sends a message to an MQTT broker (for example Mosquitto, used by Home Assistant, Node-RED or
      Zigbee2MQTT). "Send a message" sends the details as JSON; "Turn on" and "Turn off" send ON or OFF.`,
  };
  const FIELD_INFO = {
    home_assistant: [['url', 'Address', 'http://homeassistant.local:8123'], ['token', 'Access token', '', 'secret']],
    hue: [['host', 'Bridge address', '192.168.0.10']],
    alexa: [['url', 'Trigger link', 'https://api-v2.voicemonkey.io/trigger?…', 'secret']],
    webhook: [['url', 'Web address', 'https://…'], ['method', 'Send as', '', 'method'],
      ['body', 'Message (optional)', 'Leave empty to send the details as JSON']],
    mqtt: [['host', 'Broker address', '192.168.0.20'], ['port', 'Port', '1883'], ['username', 'User name', ''],
      ['password', 'Password', '', 'secret'], ['topic', 'Topic', 'homecamera/alert']],
  };
  const SECRET = {home_assistant: 'token', hue: 'username', alexa: 'url', mqtt: 'password'};

  let M = null;           // {devices, rules, kinds, events, activity}
  const targets = {};     // device id -> [{id, name}] or {error}
  let card = null;
  let editing = null;     // device id open for editing

  const devById = (id) => M.devices.find(d => d.id === id);
  const kindActions = (dev) => (dev && M.kinds[dev.kind] ? M.kinds[dev.kind].actions : []);
  const needsTarget = (dev) => dev && (dev.kind === 'home_assistant' || dev.kind === 'hue');
  const canTurnOff = (dev) => kindActions(dev).includes('off');

  async function loadTargets(dev, force) {
    if (!needsTarget(dev) || (targets[dev.id] && !force)) return;
    targets[dev.id] = {loading: true};
    try {
      const r = await api('/api/homelinks/targets', {method: 'POST', body: JSON.stringify({device: dev})});
      targets[dev.id] = r.ok ? r.targets : {error: r.message};
    } catch (e) { targets[dev.id] = {error: e.message}; }
  }

  // ---------- devices ----------
  function deviceRow(d) {
    const open = editing === d.id;
    const t = targets[d.id];
    const state = !needsTarget(d) ? '' : (t && t.error ? `<span class="warn-text" title="${esc(t.error)}">⚠ Can't connect (press Edit)</span>`
      : Array.isArray(t) ? `<span class="ok-text">✓ Connected, ${t.length} found</span>` : '');
    let html = `<div class="hl-dev" data-id="${esc(d.id)}"><div class="hl-dev-head">
      <b>${esc(d.name)}</b> <span class="muted">${esc(M.kinds[d.kind].name)}</span> ${state}
      <span style="flex:1"></span>
      <button class="hl-edit">${open ? 'Done' : 'Edit'}</button><button class="danger hl-rm" title="Remove">✕</button></div>`;
    if (open) {
      if (t && t.error) html += `<p class="warn-text">${esc(t.error)}</p>`;
      html += `<p class="hint">${HELP[d.kind]}</p>
        <div class="field"><label>Name</label><input type="text" data-f="name" value="${esc(d.name)}"></div>`;
      for (const [f, label, ph, type] of FIELD_INFO[d.kind]) {
        if (type === 'method') {
          html += `<div class="field"><label>${label}</label><select data-f="method">
            <option value="POST" ${d.method !== 'GET' ? 'selected' : ''}>POST (with message)</option>
            <option value="GET" ${d.method === 'GET' ? 'selected' : ''}>GET (just open the link)</option></select></div>`;
        } else if (type === 'secret') {
          const has = d['has_' + f];
          html += `<div class="field"><label>${label}</label><input type="password" data-f="${f}" autocomplete="off"
            placeholder="${has ? 'Saved (type to replace)' : esc(ph)}" value="${esc(d[f] || '')}"></div>`;
        } else {
          html += `<div class="field"><label>${label}</label><input type="text" data-f="${f}"
            placeholder="${esc(ph)}" value="${esc(d[f] == null ? '' : d[f])}"></div>`;
        }
      }
      if (d.kind === 'hue') {
        html += `<div class="toolbar"><button class="hl-find">Find</button><button class="hl-pair">Pair</button>
          <span class="muted">${d.has_username || d.username ? '✓ Paired' : 'Not paired yet'}</span></div>`;
      }
      html += `<div class="toolbar">${needsTarget(d) ? '<button class="hl-check">Check connection</button>'
        : `<button class="hl-try">Try it now</button>`}</div>`;
    }
    return html + '</div>';
  }

  function bindDevice(row) {
    const d = devById(row.dataset.id);
    $('.hl-edit', row).addEventListener('click', () => {
      editing = editing === d.id ? null : d.id;
      if (editing === null) loadTargets(d, true).then(draw); else draw();
    });
    $('.hl-rm', row).addEventListener('click', () => {
      if (!confirm(`Remove ${d.name}? Rules that use it lose those steps.`)) return;
      M.devices = M.devices.filter(x => x !== d);
      M.rules.forEach(r => { r.actions = r.actions.filter(a => a.device !== d.id); });
      draw();
    });
    $$('[data-f]', row).forEach(inp => inp.addEventListener('input', () => {
      d[inp.dataset.f] = inp.value;
      if (inp.dataset.f === 'name') $('.hl-dev-head b', row).textContent = inp.value;
    }));
    const find = $('.hl-find', row);
    if (find) find.addEventListener('click', async () => {
      find.disabled = true;
      try {
        const r = await api('/api/homelinks/hue/find');
        if (r.bridges.length) { d.host = r.bridges[0]; toast(`Found a Hue Bridge at ${d.host}`); draw(); }
        else toast('No Hue Bridge found. Its address is in the Hue app: Settings > Bridges > (i).');
      } catch (e) { toast(e.message); }
      find.disabled = false;
    });
    const pair = $('.hl-pair', row);
    if (pair) pair.addEventListener('click', async () => {
      pair.disabled = true;
      try {
        const r = await api('/api/homelinks/hue/pair', {method: 'POST', body: JSON.stringify({host: d.host})});
        if (r.ok) { d.username = r.username; toast('Paired with the Hue Bridge. Press Save to keep it.');
          await loadTargets(d, true); draw(); }
        else toast(r.message);
      } catch (e) { toast(e.message); }
      pair.disabled = false;
    });
    const check = $('.hl-check', row);
    if (check) check.addEventListener('click', async () => {
      check.disabled = true; await loadTargets(d, true); draw();
      const t = targets[d.id];
      toast(t.error ? t.error : `Connected. Found ${t.length} lights, switches and scenes.`);
    });
    const tryIt = $('.hl-try', row);
    if (tryIt) tryIt.addEventListener('click', async () => {
      tryIt.disabled = true;
      try {
        const r = await api('/api/homelinks/try', {method: 'POST',
          body: JSON.stringify({device: d, do: kindActions(d)[0]})});
        toast(r.ok ? `Done: ${r.message}` : r.message);
      } catch (e) { toast(e.message); }
      tryIt.disabled = false;
    });
  }

  // ---------- rules ----------
  function actionRow(a, i) {
    const dev = devById(a.device);
    const acts = kindActions(dev);
    const t = dev ? targets[dev.id] : null;
    let target = '';
    if (needsTarget(dev)) {
      if (Array.isArray(t)) {
        const known = t.some(x => x.id === a.target);
        target = `<select class="a-target"><option value="">Choose…</option>${t.map(x =>
          `<option value="${esc(x.id)}" ${x.id === a.target ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}
          ${a.target && !known ? `<option value="${esc(a.target)}" selected>${esc(a.target)}</option>` : ''}</select>`;
      } else {
        target = `<input type="text" class="a-target" value="${esc(a.target || '')}"
          placeholder="${dev.kind === 'hue' ? 'light:1 or group:1' : 'light.porch'}"
          title="${t && t.error ? esc(t.error) : 'Press Check connection on the device to choose from a list'}">`;
      }
    } else if (dev && dev.kind === 'mqtt') {
      target = `<input type="text" class="a-target" value="${esc(a.target || '')}" placeholder="Topic: ${esc(dev.topic || 'homecamera/alert')}">`;
    }
    const timed = canTurnOff(dev) && (a.do === 'on' || a.do === 'flash');
    return `<div class="hl-action" data-i="${i}">
      <select class="a-dev">${M.devices.map(d =>
        `<option value="${esc(d.id)}" ${d.id === a.device ? 'selected' : ''}>${esc(d.name)}</option>`).join('')}</select>
      <select class="a-do">${acts.map(x =>
        `<option value="${x}" ${x === a.do ? 'selected' : ''}>${ACTION_NAMES[x]}</option>`).join('')}</select>
      ${target}
      ${timed ? `<label class="check">then off after <input type="number" class="a-mins num" min="0" max="720"
        value="${a.for_minutes || 0}"> min</label>` : ''}
      <button class="danger a-rm" title="Remove this step">✕</button></div>`;
  }

  function ruleBox(r) {
    const cams = status.cameras || [];
    return `<div class="hl-rule" data-id="${esc(r.id)}">
      <div class="hl-dev-head"><label class="check"><input type="checkbox" class="r-on" ${r.enabled ? 'checked' : ''}></label>
        <input type="text" class="r-name" value="${esc(r.name)}" aria-label="Rule name">
        <span style="flex:1"></span><button class="r-try">Try now</button><button class="danger r-rm" title="Remove rule">✕</button></div>
      <div class="hl-sub">When the camera sees</div>
      <div class="checks">${M.events.map(e => `<label><input type="checkbox" class="r-ev" value="${e.id}"
        ${r.events.includes(e.id) ? 'checked' : ''}>${esc(e.name)}</label>`).join('')}</div>
      ${cams.length > 1 ? `<div class="hl-sub">On</div><div class="checks">${cams.map(c =>
        `<label><input type="checkbox" class="r-cam" value="${esc(c.id)}" ${!r.cameras.length || r.cameras.includes(c.id) ? 'checked' : ''}>${esc(c.name)}</label>`).join('')}</div>` : ''}
      <div class="hl-sub">Active</div>
      <div class="seg r-when"><button data-w="always" class="${r.when === 'always' ? 'active' : ''}">Any time</button>
        <button data-w="hours" class="${r.when === 'hours' ? 'active' : ''}">Only at these times</button></div>
      <div class="sched ${r.when === 'hours' ? '' : 'hidden'}"></div>
      ${r.when === 'hours' ? '<button class="link r-add-sched">＋ Add times</button>' : ''}
      <div class="checks"><label><input type="checkbox" class="r-away" ${r.only_away ? 'checked' : ''}>
        Only when nobody's home</label>
        <label>Don't repeat for <input type="number" class="r-cool num" min="0" max="240" value="${r.cooldown_minutes}"> min</label></div>
      <div class="hl-sub">Then</div>
      ${r.actions.map(actionRow).join('') || '<p class="muted">Nothing yet.</p>'}
      ${M.devices.length ? '<button class="link r-add-act">＋ Add a step</button>'
        : '<p class="muted">Add a device above first.</p>'}
    </div>`;
  }

  function readRuleSchedule(box, r) {
    if (r.when === 'hours') r.schedule = readSchedule($('.sched', box));
  }

  function bindRule(box) {
    const r = M.rules.find(x => x.id === box.dataset.id);
    if (r.when === 'hours') r.schedule.forEach(s => $('.sched', box).appendChild(scheduleRow(s)));
    const sync = () => readRuleSchedule(box, r);
    $('.sched', box).addEventListener('change', sync);
    $('.sched', box).addEventListener('click', () => setTimeout(sync));
    $('.r-on', box).addEventListener('change', e => { r.enabled = e.target.checked; });
    $('.r-name', box).addEventListener('input', e => { r.name = e.target.value; });
    $('.r-away', box).addEventListener('change', e => { r.only_away = e.target.checked; });
    $('.r-cool', box).addEventListener('input', e => { r.cooldown_minutes = Number(e.target.value) || 0; });
    $$('.r-ev', box).forEach(c => c.addEventListener('change', () => {
      r.events = $$('.r-ev', box).filter(x => x.checked).map(x => x.value); }));
    $$('.r-cam', box).forEach(c => c.addEventListener('change', () => {
      const on = $$('.r-cam', box).filter(x => x.checked).map(x => x.value);
      r.cameras = on.length === $$('.r-cam', box).length ? [] : on; }));
    $$('.r-when button', box).forEach(b => b.addEventListener('click', () => {
      sync(); r.when = b.dataset.w;
      if (r.when === 'hours' && !r.schedule.length) r.schedule = JSON.parse(JSON.stringify(M.default_rule.schedule));
      draw();
    }));
    const addSched = $('.r-add-sched', box);
    if (addSched) addSched.addEventListener('click', () => { $('.sched', box).appendChild(scheduleRow(null)); sync(); });
    $('.r-rm', box).addEventListener('click', () => {
      if (!confirm(`Remove the rule "${r.name}"?`)) return;
      M.rules = M.rules.filter(x => x !== r); draw();
    });
    $('.r-try', box).addEventListener('click', async (ev) => {
      ev.target.disabled = true;
      try {
        await save(true);
        const res = await api(`/api/homelinks/rules/${encodeURIComponent(r.id)}/try`, {method: 'POST'});
        const bad = res.results.filter(x => !x.ok);
        toast(!res.results.length ? 'This rule has no steps yet'
          : bad.length ? `${bad[0].device}: ${bad[0].message}` : 'Done. Everything in the rule worked.');
        refreshActivity();
      } catch (e) { toast(e.message); }
      ev.target.disabled = false;
    });
    const addAct = $('.r-add-act', box);
    if (addAct) addAct.addEventListener('click', () => {
      const dev = M.devices[0];
      r.actions.push({device: dev.id, do: kindActions(dev)[0], target: '', for_minutes: dev.kind === 'alexa' ? 0 : 5});
      draw();
    });
    $$('.hl-action', box).forEach(row => {
      const a = r.actions[Number(row.dataset.i)];
      $('.a-dev', row).addEventListener('change', async e => {
        a.device = e.target.value; const dev = devById(a.device);
        if (!kindActions(dev).includes(a.do)) a.do = kindActions(dev)[0];
        a.target = ''; await loadTargets(dev); draw();
      });
      $('.a-do', row).addEventListener('change', e => { a.do = e.target.value; draw(); });
      const tg = $('.a-target', row);
      if (tg) tg.addEventListener(tg.tagName === 'SELECT' ? 'change' : 'input', e => { a.target = e.target.value; });
      const mins = $('.a-mins', row);
      if (mins) mins.addEventListener('input', e => { a.for_minutes = Number(e.target.value) || 0; });
      $('.a-rm', row).addEventListener('click', () => { r.actions.splice(Number(row.dataset.i), 1); draw(); });
    });
  }

  // ---------- card ----------
  function activityHtml() {
    const rows = (M.activity || []).slice(0, 8);
    if (!rows.length) return '<p class="muted">Nothing has been switched yet.</p>';
    return rows.map(x => `<div class="hl-log ${x.ok ? '' : 'warn-text'}">${x.ok ? '✓' : '⚠'}
      ${esc(new Date(x.time).toLocaleString([], {weekday: 'short', hour: '2-digit', minute: '2-digit'}))}:
      ${esc(x.rule)} → ${esc(x.device)}${x.ok ? '' : ': ' + esc(x.message)}</div>`).join('');
  }

  async function refreshActivity() {
    try { M.activity = (await api('/api/homelinks')).activity; } catch (e) { return; }
    const box = card && $('.hl-activity', card);
    if (box) box.innerHTML = activityHtml();
  }

  function draw() {
    // Keep what's typed into time boxes before rebuilding.
    $$('.hl-rule', card).forEach(box => {
      const r = M.rules.find(x => x.id === box.dataset.id); if (r) readRuleSchedule(box, r);
    });
    card.innerHTML = `<h2>Smart home</h2>
      <p class="muted" style="margin-top:0">Switch on lights, sound a siren or start an Alexa routine when the
        camera sees something, for example the porch light when a stranger comes to the door at night.</p>
      <h3>Your devices</h3>
      <div class="hl-devs">${M.devices.map(deviceRow).join('') || '<p class="muted">None yet.</p>'}</div>
      <div class="toolbar"><select class="hl-kind">${Object.entries(M.kinds).map(([k, v]) =>
        `<option value="${k}">${esc(v.name)}</option>`).join('')}</select><button class="hl-add">＋ Add</button></div>
      <h3>Rules</h3>
      <div class="hl-rules">${M.rules.map(ruleBox).join('') || '<p class="muted">No rules yet.</p>'}</div>
      <button class="link hl-add-rule">＋ Add a rule</button>
      <h3>Recently</h3><div class="hl-activity">${activityHtml()}</div>
      <div class="toolbar" style="margin-top:12px"><button class="primary hl-save">Save</button></div>`;
    $$('.hl-dev', card).forEach(bindDevice);
    $$('.hl-rule', card).forEach(bindRule);
    $('.hl-add', card).addEventListener('click', () => {
      const kind = $('.hl-kind', card).value;
      const d = {id: newId('d'), kind, name: M.kinds[kind].name};
      if (kind === 'webhook') d.method = 'POST';
      if (kind === 'mqtt') { d.port = 1883; d.topic = 'homecamera/alert'; }
      M.devices.push(d); editing = d.id; draw();
    });
    $('.hl-add-rule', card).addEventListener('click', () => {
      const r = JSON.parse(JSON.stringify(M.default_rule)); r.id = newId('r');
      if (M.rules.length) r.name = 'New rule';
      M.rules.push(r); draw();
    });
    $('.hl-save', card).addEventListener('click', () => save(false));
  }

  async function save(quiet) {
    $$('.hl-rule', card).forEach(box => {
      const r = M.rules.find(x => x.id === box.dataset.id); if (r) readRuleSchedule(box, r);
    });
    const bad = M.rules.find(r => r.enabled && r.actions.length && !r.events.length);
    if (bad && !quiet) { toast(`Choose what sets off "${bad.name}"`); return; }
    const res = await api('/api/homelinks', {method: 'PUT',
      body: JSON.stringify({devices: M.devices, rules: M.rules})});
    const kept = {kinds: M.kinds, events: M.events, default_rule: M.default_rule};
    M = Object.assign(res, kept);
    if (!quiet) { toast('Smart home settings saved'); draw(); }
  }

  window.homeLinksCard = async function () {
    card = document.createElement('div'); card.className = 'card hl-card';
    editing = null;
    try { M = await api('/api/homelinks'); } catch (e) {
      card.innerHTML = `<h2>Smart home</h2><p class="muted">Not available: ${esc(e.message)}</p>`;
      return card;
    }
    draw();
    let touched = false;
    card.addEventListener('input', () => { touched = true; });
    // Fetch the lights and switches in the background, so rules can show their names.
    Promise.all(M.devices.map(d => loadTargets(d))).then(() => { if (!editing && !touched) draw(); });
    return card;
  };
})();
