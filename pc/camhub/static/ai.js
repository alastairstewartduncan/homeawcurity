'use strict';
// The AI tab: AI sources (provider + model), skills, agents, AI drafting and the activity list.
// Uses $, $$, api, esc and toast from app.js.

let aiData = null;

function el(html) { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; }
function busy(btn, text) { const old = btn.textContent; btn.disabled = true; btn.textContent = text; return () => { btn.disabled = false; btn.textContent = old; }; }
function fmtWhen(iso) {
  const d = new Date(iso); if (isNaN(d)) return iso || '';
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString([], today ? {hour: '2-digit', minute: '2-digit'} : {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
}

async function loadAi() {
  const wrap = $('#ai'); if (!wrap) return;
  try { aiData = await api('/api/ai'); } catch (e) { wrap.innerHTML = `<div class="card">${esc(e.message)}</div>`; return; }
  wrap.innerHTML = '';
  wrap.append(createCard(), agentsCard(), skillsCard(), sourcesCard(), activityCard());
}
window.loadAi = loadAi;

function sourceName(id) {
  if (!id) { const d = aiData.sources.find(s => s.id === aiData.default_source) || aiData.sources[0]; return d ? `Default (${d.name})` : 'Default'; }
  const s = aiData.sources.find(x => x.id === id); return s ? s.name : 'Default';
}
function needSource() {
  if (aiData.sources.length) return false;
  toast('Add an AI source first, under AI sources below.');
  const s = $('#ai-sources'); if (s) s.scrollIntoView({behavior: 'smooth'});
  return true;
}

// ---------- create with AI ----------
function createCard() {
  const card = el(`<div class="card wide ai-hero">
    <h2>Create an agent with AI</h2>
    <p class="muted" style="margin-top:0">Describe in your own words what you'd like watched and when you want to hear about it.
      AI writes the agent (and any new skills it needs). You can check and change everything before saving.</p>
    <textarea rows="3" class="desc" placeholder="For example: Tell me when a parcel is left on the doorstep or someone tries the car door at night. Don't bother me about people walking past."></textarea>
    <div class="toolbar"><button class="primary go">Create with AI</button>
      <button class="blank">Or start from blank</button>
      <span class="muted small">${aiData.sources.length ? 'Uses ' + esc(sourceName('')) : 'Add an AI source below first.'}</span></div>
  </div>`);
  $('.go', card).addEventListener('click', async (ev) => {
    if (needSource()) return;
    const desc = $('.desc', card).value.trim();
    if (desc.length < 3) { toast('Describe what you want first.'); return; }
    const done = busy(ev.target, 'Writing your agent…');
    try {
      const d = await api('/api/ai/draft', {method: 'POST', body: JSON.stringify({kind: 'agent', description: desc})});
      openAgentEditor(d.agent, d.new_skills);
    } catch (e) { toast('AI could not write it: ' + e.message); }
    done();
  });
  $('.blank', card).addEventListener('click', () => openAgentEditor(null, []));
  return card;
}

// ---------- agents ----------
const ALERTS = {ai_decides: 'AI decides when to alert me', always: 'Alert me every time', never: 'Never alert, just keep notes'};

function agentsCard() {
  const card = el(`<div class="card wide" id="ai-agents"><h2>Agents</h2>
    <p class="muted" style="margin-top:0">Each agent looks at the camera picture when something happens, uses its skills, and
      sends a phone alert when it decides you should know.</p><div class="ai-list"></div><div class="editor-slot"></div></div>`);
  const list = $('.ai-list', card);
  if (!aiData.agents.length) list.append(el('<div class="muted">No agents yet. Create one above.</div>'));
  for (const a of aiData.agents) {
    const trig = a.triggers.map(t => t === 'schedule' ? `every ${a.every_minutes} min` : (aiData.triggers[t] || t).toLowerCase()).join(', ');
    const row = el(`<div class="ai-row">
      <label class="switch" title="On or off"><input type="checkbox" ${a.enabled ? 'checked' : ''}><span></span></label>
      <div class="ai-row-main"><b>${esc(a.name)}</b>
        <div class="muted small">When ${esc(trig)} · ${esc(ALERTS[a.alert])} · ${esc(sourceName(a.source))}${a.model ? ' · ' + esc(a.model) : ''}</div>
        <div class="small">${esc(a.goal).slice(0, 220)}</div></div>
      <div class="ai-row-tools"><button class="test">Test now</button><button class="edit">Edit</button></div>
    </div>`);
    $('input', row).addEventListener('change', async (e) => {
      try { await api('/api/ai/agents', {method: 'POST', body: JSON.stringify({...a, enabled: e.target.checked})});
        a.enabled = e.target.checked; toast(`${a.name} ${a.enabled ? 'on' : 'off'}`); }
      catch (err) { toast('Could not save: ' + err.message); e.target.checked = !e.target.checked; }
    });
    $('.edit', row).addEventListener('click', () => openAgentEditor(a, []));
    $('.test', row).addEventListener('click', (ev) => testAgent(a, ev.target, row));
    list.append(row);
  }
  return card;
}

async function testAgent(a, btn, after) {
  if (needSource()) return;
  const done = busy(btn, 'Looking…');
  try {
    const r = await api(`/api/ai/agents/${a.id}/test`, {method: 'POST', body: '{}'});
    const box = runView(r, true);
    const old = after.nextElementSibling; if (old && old.classList.contains('run')) old.remove();
    after.after(box);
    if (window.refreshAiActivity) window.refreshAiActivity();
  } catch (e) { toast('Test failed: ' + e.message); }
  done();
}

function runView(r, isTest) {
  const pic = r.picture && r.id ? `<img class="run-pic" src="/api/ai/runs/${r.id}/picture.jpg" alt="" loading="lazy">` : '';
  const badge = r.error ? '<span class="pill off">Problem</span>' :
    r.alert ? `<span class="pill rec">${isTest ? 'Would alert' : 'Alerted'}</span>` : '<span class="pill">No alert</span>';
  return el(`<div class="run">${pic}<div class="run-body">
    <div class="small muted">${esc(fmtWhen(r.time))} · ${esc(r.agent_name)} · ${esc(r.camera_name || r.camera)}${r.model ? ' · ' + esc(r.model) : ''}${r.seconds ? ` · ${r.seconds}s` : ''}</div>
    <div>${badge} ${r.message && r.alert ? '<b>' + esc(r.message) + '</b>' : ''}</div>
    <div class="small">${esc(r.error || r.summary || '')}</div>
    ${r.alert_skipped ? `<div class="small muted">${esc(r.alert_skipped)}</div>` : ''}</div></div>`);
}

function openAgentEditor(a, newSkills) {
  if (!a) a = {id: '', name: '', enabled: true, goal: '', skills: [], cameras: [], triggers: ['person'],
    every_minutes: 30, source: '', model: '', alert: 'ai_decides', cooldown_minutes: 5, memory: true};
  newSkills = newSkills || [];
  const slot = $('#ai-agents .editor-slot');
  const srcOpts = `<option value="">${esc(sourceName(''))}</option>` +
    aiData.sources.map(s => `<option value="${esc(s.id)}" ${s.id === a.source ? 'selected' : ''}>${esc(s.name)}</option>`).join('');
  const f = el(`<div class="ai-editor">
    <h3>${a.id ? 'Edit agent' : 'New agent'}</h3>
    <label class="lbl">Name<input name="name" value="${esc(a.name)}" maxlength="60"></label>
    <label class="lbl">What should it watch for, and when should it tell you?
      <textarea name="goal" rows="4">${esc(a.goal)}</textarea></label>
    <div class="lbl">Skills it uses
      <div class="checks skills">${aiData.skills.map(s => `<label title="${esc(s.description)}"><input type="checkbox" value="${esc(s.id)}" ${a.skills.includes(s.id) ? 'checked' : ''}> ${esc(s.name)}</label>`).join('')}
      ${newSkills.map((s, i) => `<label class="new" title="${esc(s.description)}"><input type="checkbox" data-new="${i}" checked> ${esc(s.name)} <span class="pill ok">new</span></label>`).join('')}</div></div>
    <div class="lbl">Run when
      <div class="checks trig">${Object.entries(aiData.triggers).map(([k, v]) => `<label><input type="checkbox" value="${k}" ${a.triggers.includes(k) ? 'checked' : ''}> ${esc(v)}</label>`).join('')}
        <label>every <input name="every_minutes" type="number" min="1" max="1440" value="${a.every_minutes}" class="num"> minutes</label></div></div>
    <div class="lbl">Cameras <span class="muted small">(none ticked means all)</span>
      <div class="checks cams">${aiData.cameras.map(c => `<label><input type="checkbox" value="${esc(c.id)}" ${a.cameras.includes(c.id) ? 'checked' : ''}> ${esc(c.name)}</label>`).join('') || '<span class="muted small">No cameras yet</span>'}</div></div>
    <div class="grid-form">
      <label>AI source<select name="source">${srcOpts}</select></label>
      <label>Model <span class="muted small">(blank uses the source's)</span><input name="model" value="${esc(a.model)}" list="agent-models" placeholder="Source default"></label>
      <label>Alerts<select name="alert">${Object.entries(ALERTS).map(([k, v]) => `<option value="${k}" ${a.alert === k ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>
      <label>Quiet time between alerts (min)<input name="cooldown_minutes" type="number" min="0" max="1440" value="${a.cooldown_minutes}"></label>
    </div>
    <datalist id="agent-models"></datalist>
    <label class="inline"><input type="checkbox" name="memory" ${a.memory ? 'checked' : ''}> Remember its last few observations, so it can notice changes</label>
    <label class="inline"><input type="checkbox" name="enabled" ${a.enabled ? 'checked' : ''}> Agent is on</label>
    <details class="ai-help"><summary>Change it with AI</summary>
      <textarea rows="2" class="change" placeholder="For example: only alert me between 10pm and 6am"></textarea>
      <button class="redo">Update with AI</button></details>
    <div class="toolbar"><button class="primary save">Save agent</button>
      ${a.id ? '<button class="danger del">Delete</button>' : ''}<button class="cancel">Cancel</button></div>
  </div>`);
  slot.innerHTML = ''; slot.append(f);
  f.scrollIntoView({behavior: 'smooth', block: 'start'});
  const v = (n) => $(`[name=${n}]`, f);
  const ticked = (sel) => $$(`${sel} input[type=checkbox]:checked`, f).filter(i => !i.dataset.new).map(i => i.value);

  v('model').addEventListener('focus', async () => {
    const dl = $('#agent-models', f); if (dl.childElementCount || !aiData.sources.length) return;
    const sid = v('source').value || aiData.default_source;
    const s = aiData.sources.find(x => x.id === sid) || aiData.sources[0];
    try { const r = await api('/api/ai/sources/models', {method: 'POST', body: JSON.stringify(s)});
      dl.innerHTML = r.models.map(m => `<option value="${esc(m)}">`).join(''); } catch (e) { /* typing still works */ }
  });
  v('source').addEventListener('change', () => { $('#agent-models', f).innerHTML = ''; });

  function current() {
    return {id: a.id, name: v('name').value, goal: v('goal').value, skills: ticked('.skills'),
      triggers: ticked('.trig'), every_minutes: v('every_minutes').value, cameras: ticked('.cams'),
      source: v('source').value, model: v('model').value, alert: v('alert').value,
      cooldown_minutes: v('cooldown_minutes').value, memory: v('memory').checked, enabled: v('enabled').checked};
  }

  $('.redo', f).addEventListener('click', async (ev) => {
    if (needSource()) return;
    const change = $('.change', f).value.trim(); if (!change) { toast('Say what to change.'); return; }
    const done = busy(ev.target, 'Updating…');
    try {
      const d = await api('/api/ai/draft', {method: 'POST', body: JSON.stringify({kind: 'agent', description: change, current: current()})});
      done(); openAgentEditor(d.agent, d.new_skills); return;
    } catch (e) { toast('AI could not change it: ' + e.message); }
    done();
  });
  $('.cancel', f).addEventListener('click', () => { slot.innerHTML = ''; });
  const del = $('.del', f);
  if (del) del.addEventListener('click', async () => {
    if (!confirm(`Delete the agent "${a.name}"?`)) return;
    await api('/api/ai/agents/' + a.id, {method: 'DELETE'}); toast('Agent deleted'); loadAi();
  });
  $('.save', f).addEventListener('click', async (ev) => {
    const body = current();
    if (!body.name.trim()) { toast('Give the agent a name.'); return; }
    if (!body.triggers.length) { toast('Choose when the agent should run.'); return; }
    const done = busy(ev.target, 'Saving…');
    try {
      // Save any new skills the AI suggested and that are still ticked.
      for (const i of $$('.skills input[data-new]:checked', f)) {
        const sk = await api('/api/ai/skills', {method: 'POST', body: JSON.stringify(newSkills[+i.dataset.new])});
        body.skills.push(sk.id);
      }
      await api('/api/ai/agents', {method: 'POST', body: JSON.stringify(body)});
      toast('Agent saved'); loadAi();
    } catch (e) { toast('Could not save: ' + e.message); done(); }
  });
}

// ---------- skills ----------
function skillsCard() {
  const card = el(`<div class="card" id="ai-skills"><h2>Skills</h2>
    <p class="muted" style="margin-top:0">Reusable know-how your agents can share, written in plain words.</p>
    <div class="ai-list"></div><div class="toolbar"><button class="add">+ New skill</button></div><div class="editor-slot"></div></div>`);
  const list = $('.ai-list', card);
  for (const s of aiData.skills) {
    const used = aiData.agents.filter(a => a.skills.includes(s.id)).length;
    const row = el(`<div class="ai-row"><div class="ai-row-main"><b>${esc(s.name)}</b>
      <div class="small muted">${esc(s.description)}${used ? ` · used by ${used} agent${used > 1 ? 's' : ''}` : ''}</div></div>
      <div class="ai-row-tools"><button class="edit">Edit</button></div></div>`);
    $('.edit', row).addEventListener('click', () => openSkillEditor(card, s));
    list.append(row);
  }
  $('.add', card).addEventListener('click', () => openSkillEditor(card, null));
  return card;
}

function openSkillEditor(card, s) {
  s = s || {id: '', name: '', description: '', instructions: ''};
  const slot = $('.editor-slot', card);
  const f = el(`<div class="ai-editor"><h3>${s.id ? 'Edit skill' : 'New skill'}</h3>
    <details class="ai-help" ${s.id ? '' : 'open'}><summary>${s.id ? 'Change it with AI' : 'Write it with AI'}</summary>
      <textarea rows="2" class="desc" placeholder="${s.id ? 'What should change?' : 'For example: recognise our two cats and say which one it is'}"></textarea>
      <button class="draft">${s.id ? 'Update with AI' : 'Write with AI'}</button></details>
    <label class="lbl">Name<input name="name" value="${esc(s.name)}" maxlength="60"></label>
    <label class="lbl">Short description<input name="description" value="${esc(s.description)}" maxlength="300"></label>
    <label class="lbl">Instructions for the AI<textarea name="instructions" rows="6">${esc(s.instructions)}</textarea></label>
    <div class="toolbar"><button class="primary save">Save skill</button>
      ${s.id ? '<button class="danger del">Delete</button>' : ''}<button class="cancel">Cancel</button></div></div>`);
  slot.innerHTML = ''; slot.append(f);
  const v = (n) => $(`[name=${n}]`, f);
  $('.draft', f).addEventListener('click', async (ev) => {
    if (needSource()) return;
    const desc = $('.desc', f).value.trim(); if (!desc) { toast('Describe the skill first.'); return; }
    const done = busy(ev.target, 'Writing…');
    try {
      const cur = s.id ? {name: v('name').value, description: v('description').value, instructions: v('instructions').value} : null;
      const d = await api('/api/ai/draft', {method: 'POST', body: JSON.stringify({kind: 'skill', description: desc, current: cur})});
      v('name').value = d.skill.name; v('description').value = d.skill.description; v('instructions').value = d.skill.instructions;
      toast('Draft ready. Check it, then save.');
    } catch (e) { toast('AI could not write it: ' + e.message); }
    done();
  });
  $('.cancel', f).addEventListener('click', () => { slot.innerHTML = ''; });
  const del = $('.del', f);
  if (del) del.addEventListener('click', async () => {
    if (!confirm(`Delete the skill "${s.name}"? Agents using it will stop using it.`)) return;
    await api('/api/ai/skills/' + s.id, {method: 'DELETE'}); toast('Skill deleted'); loadAi();
  });
  $('.save', f).addEventListener('click', async () => {
    try {
      await api('/api/ai/skills', {method: 'POST', body: JSON.stringify({id: s.id, name: v('name').value,
        description: v('description').value, instructions: v('instructions').value})});
      toast('Skill saved'); loadAi();
    } catch (e) { toast('Could not save: ' + e.message); }
  });
}

// ---------- AI sources ----------
function sourcesCard() {
  const card = el(`<div class="card" id="ai-sources"><h2>AI sources</h2>
    <p class="muted" style="margin-top:0">Choose which AI does the thinking: Claude, ChatGPT, Gemini, a model running on
      your own PC with Ollama or LM Studio, or any service that works like OpenAI. Pick a model that can look at pictures.
      API keys stay on this PC and are never shown again.</p>
    <div class="ai-list"></div><div class="toolbar"><button class="add">+ Add AI source</button></div><div class="editor-slot"></div></div>`);
  const list = $('.ai-list', card);
  for (const s of aiData.sources) {
    const isDef = s.id === aiData.default_source;
    const row = el(`<div class="ai-row"><div class="ai-row-main"><b>${esc(s.name)}</b> ${isDef ? '<span class="pill ok">default</span>' : ''}
      <div class="small muted">${esc((aiData.kinds[s.kind] || {}).label || s.kind)} · ${esc(s.model || aiData.kinds[s.kind].model || 'no model chosen')}
      ${s.has_key ? ' · key ' + esc(s.key_hint || 'saved') : ''}</div></div>
      <div class="ai-row-tools"><button class="edit">Edit</button></div></div>`);
    $('.edit', row).addEventListener('click', () => openSourceEditor(card, s));
    list.append(row);
  }
  if (!aiData.sources.length) list.append(el('<div class="muted">No AI source yet. Add one to start using agents.</div>'));
  $('.add', card).addEventListener('click', () => openSourceEditor(card, null));
  return card;
}

function openSourceEditor(card, s) {
  const isNew = !s;
  s = s || {id: '', name: '', kind: 'anthropic', base_url: '', model: '', has_key: false};
  const slot = $('.editor-slot', card);
  const f = el(`<div class="ai-editor"><h3>${isNew ? 'Add AI source' : 'Edit AI source'}</h3>
    <div class="grid-form">
      <label>Type<select name="kind">${Object.entries(aiData.kinds).map(([k, v]) => `<option value="${k}" ${k === s.kind ? 'selected' : ''}>${esc(v.label)}</option>`).join('')}</select></label>
      <label>Name<input name="name" value="${esc(s.name)}" maxlength="60"></label>
      <label>Address<input name="base_url" value="${esc(s.base_url)}"></label>
      <label>API key<input name="api_key" type="password" autocomplete="new-password"></label>
      <label>Model<input name="model" value="${esc(s.model)}" list="src-models"></label>
    </div>
    <datalist id="src-models"></datalist>
    <label class="inline"><input type="checkbox" name="make_default" ${isNew || s.id === aiData.default_source ? 'checked' : ''}> Use as the default for agents and AI writing</label>
    <pre class="find-log hidden"></pre>
    <div class="toolbar"><button class="primary save">Save</button><button class="models">Load models</button><button class="try">Test</button>
      ${isNew ? '' : '<button class="danger del">Remove</button>'}<button class="cancel">Cancel</button></div></div>`);
  slot.innerHTML = ''; slot.append(f);
  const v = (n) => $(`[name=${n}]`, f);
  const out = $('.find-log', f);
  function hints() {
    const k = aiData.kinds[v('kind').value];
    v('base_url').placeholder = k.base_url; v('model').placeholder = k.model || 'model name';
    v('api_key').placeholder = s.has_key && v('kind').value === s.kind ? `Unchanged (${s.key_hint || 'saved'})` : (k.needs_key ? 'Paste your API key' : 'Not needed usually');
    if (!v('name').value || Object.values(aiData.kinds).some(x => x.label === v('name').value)) v('name').value = k.label;
  }
  v('kind').addEventListener('change', () => { $('#src-models', f).innerHTML = ''; hints(); });
  hints();
  const body = () => ({id: s.id, kind: v('kind').value, name: v('name').value, base_url: v('base_url').value,
    api_key: v('api_key').value, model: v('model').value, make_default: v('make_default').checked});
  $('.models', f).addEventListener('click', async (ev) => {
    const done = busy(ev.target, 'Loading…');
    try {
      const r = await api('/api/ai/sources/models', {method: 'POST', body: JSON.stringify(body())});
      $('#src-models', f).innerHTML = r.models.map(m => `<option value="${esc(m)}">`).join('');
      out.classList.remove('hidden');
      out.textContent = r.models.length ? `${r.models.length} models found. Click the Model box to pick one:\n` + r.models.slice(0, 40).join('\n') : 'No models found.';
    } catch (e) { out.classList.remove('hidden'); out.textContent = e.message; }
    done();
  });
  $('.try', f).addEventListener('click', async (ev) => {
    const done = busy(ev.target, 'Testing…');
    out.classList.remove('hidden'); out.textContent = 'Asking the AI to reply…';
    try { const r = await api('/api/ai/sources/test', {method: 'POST', body: JSON.stringify(body())});
      out.textContent = `It works. The AI replied: "${r.reply}"`; }
    catch (e) { out.textContent = e.message; }
    done();
  });
  $('.cancel', f).addEventListener('click', () => { slot.innerHTML = ''; });
  const del = $('.del', f);
  if (del) del.addEventListener('click', async () => {
    if (!confirm(`Remove ${s.name}? Its API key is deleted from this PC.`)) return;
    await api('/api/ai/sources/' + s.id, {method: 'DELETE'}); toast('AI source removed'); loadAi();
  });
  $('.save', f).addEventListener('click', async () => {
    try { await api('/api/ai/sources', {method: 'POST', body: JSON.stringify(body())}); toast('AI source saved'); loadAi(); }
    catch (e) { toast('Could not save: ' + e.message); }
  });
}

// ---------- activity ----------
function activityCard() {
  const card = el(`<div class="card wide" id="ai-activity"><h2>What the agents saw</h2>
    <div class="toolbar"><select class="who"><option value="">All agents</option>
      ${aiData.agents.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('')}</select>
      <button class="refresh">Refresh</button></div><div class="runs"></div></div>`);
  const runs = $('.runs', card);
  async function load() {
    try {
      const items = await api('/api/ai/activity?limit=60' + ($('.who', card).value ? '&agent=' + encodeURIComponent($('.who', card).value) : ''));
      runs.innerHTML = '';
      if (!items.length) runs.append(el('<div class="muted">Nothing yet. Agents add an entry each time they look.</div>'));
      for (const r of items) runs.append(runView({...r, picture: r.has_picture}, r.trigger === 'manual'));
    } catch (e) { runs.textContent = e.message; }
  }
  $('.refresh', card).addEventListener('click', load);
  $('.who', card).addEventListener('change', load);
  load();
  window.refreshAiActivity = load;
  return card;
}
