'use strict';
// The Stats tab: busiest times, visitors per day, hot spots, system health and the weekly report.
// On phones it opens from the "Who's been" tab (Who's been | Stats), so the bottom bar keeps six tabs.
// Uses $, api, esc, toast, fmtBytes and showTab from app.js. Charts are drawn here as plain SVG and HTML.
(function () {
  const h = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const q = (s, el) => el.querySelector(s);
  const qa = (s, el) => [...el.querySelectorAll(s)];
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const st = {days: 7, camera: '', heat: 'people'};
  let data = null, health = null, wrap = null;

  const hourName = (hr) => hr === 0 ? '12am' : hr < 12 ? `${hr}am` : hr === 12 ? '12pm' : `${hr - 12}pm`;
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;
  const dayLabel = (d) => new Date(d + 'T12:00:00').toLocaleDateString([], {weekday: 'short', day: 'numeric', month: 'short'});
  const narrow = () => matchMedia('(max-width: 760px)').matches;

  // ---------- tooltip (one for every chart) ----------
  const tip = h('<div class="st-tip" role="status" hidden></div>');
  document.body.appendChild(tip);
  function showTip(e, html) {
    tip.innerHTML = html; tip.hidden = false;
    const pt = e.touches ? e.touches[0] : e;
    const r = tip.getBoundingClientRect();
    let x = pt.clientX + 14, y = pt.clientY + 14;
    if (x + r.width > innerWidth - 8) x = pt.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = pt.clientY - r.height - 14;
    tip.style.left = Math.max(8, x) + 'px'; tip.style.top = Math.max(8, y) + 'px';
  }
  const hideTip = () => { tip.hidden = true; };
  function hover(el, html) {
    el.addEventListener('mousemove', (e) => showTip(e, html()));
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('touchstart', (e) => showTip(e, html()), {passive: true});
  }
  document.addEventListener('scroll', hideTip, true);

  // ---------- loading ----------
  async function load() {
    wrap = $('#stats'); if (!wrap) return;
    if (!wrap.children.length) wrap.innerHTML = '<div class="card wide muted">Working out the stats…</div>';
    try {
      [data, health] = await Promise.all([
        api(`/api/stats?${new URLSearchParams({days: st.days, camera: st.camera})}`),
        api('/api/stats/health')]);
    } catch (e) { wrap.innerHTML = `<div class="card wide">${esc(e.message)}</div>`; return; }
    render();
  }
  window.loadStats = load;

  function render() {
    wrap.innerHTML = '';
    wrap.append(toolbar(), tiles(), heatCard(), visitorsCard(), hotSpotsCard(), healthCard(), weeklyCard());
  }

  let resizeTimer = null;
  addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (data && wrap && !$('#tab-stats').classList.contains('hidden')) render(); }, 200);
  });

  // ---------- period and camera ----------
  function toolbar() {
    const cams = data.cameras.length > 1
      ? `<select class="st-cam" aria-label="Camera"><option value="">All cameras</option>${data.cameras.map(c =>
        `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>` : '';
    const bar = h(`<div class="card wide st-bar">
      <div><h2>Stats</h2><p class="muted">${esc(dayLabel(data.from))} to ${esc(dayLabel(data.to))}</p></div>
      <div class="st-controls">
        <div class="seg st-days" role="group" aria-label="Period">
          <button data-d="7">7 days</button><button data-d="30">30 days</button><button data-d="90">90 days</button>
        </div>${cams}
      </div></div>`);
    qa('.st-days button', bar).forEach(b => {
      b.classList.toggle('active', +b.dataset.d === st.days);
      b.addEventListener('click', () => { st.days = +b.dataset.d; load(); });
    });
    const sel = q('.st-cam', bar);
    if (sel) { sel.value = st.camera; sel.addEventListener('change', () => { st.camera = sel.value; load(); }); }
    return bar;
  }

  // ---------- headline numbers ----------
  function tiles() {
    const t = data.totals, b = data.busiest;
    const items = [
      ['Visitors', t.people, 'People seen on camera'],
      ...(data.face_recognition ? [['Strangers', t.stranger, 'People not recognised as family']] : []),
      ['Vehicles', t.vehicle, 'Vehicles that stopped'],
      ['Parcels', t.parcel, 'Parcels left'],
      ['Recordings', t.activity, 'Times recording started'],
    ];
    const card = h(`<div class="card wide st-tiles">${items.map(([k, v, title]) =>
      `<div class="st-tile" title="${esc(title)}"><span class="muted">${k}</span><b>${v.toLocaleString()}</b></div>`).join('')}
      <div class="st-tile busiest" title="When the most ${b && b.of === 'people' ? 'people came' : 'recordings started'}">
        <span class="muted">Busiest time</span><b>${b ? `${esc(b.weekday.slice(0, 3))} ${hourName(b.hour)}` : '–'}</b></div></div>`);
    return card;
  }

  // ---------- busiest times: week by hour ----------
  function heatCard() {
    const card = h(`<div class="card wide"><div class="st-head"><h2>Busiest times</h2>
      <div class="seg st-heat-kind" role="group" aria-label="Count">
        <button data-k="people">People</button><button data-k="activity">All recordings</button></div></div>
      <div class="st-week"></div>
      <div class="st-scale muted"><span>Fewer</span>${[0, 1, 2, 3, 4].map(i => `<i class="l${i}"></i>`).join('')}<span>More</span></div></div>`);
    const draw = () => {
      qa('.st-heat-kind button', card).forEach(b => b.classList.toggle('active', b.dataset.k === st.heat));
      const grid = data.heatmap[st.heat];
      const top = Math.max(1, ...grid.flat());
      const week = q('.st-week', card);
      week.innerHTML = '<span></span>' + Array.from({length: 24}, (_, hr) =>
        `<span class="st-hr">${hr % 3 === 0 ? hourName(hr) : ''}</span>`).join('');
      grid.forEach((row, d) => {
        week.appendChild(h(`<span class="st-dow">${DAYS[d]}</span>`));
        row.forEach((n, hr) => {
          const lvl = n === 0 ? 0 : Math.min(4, 1 + Math.floor(3.999 * n / top));
          const c = h(`<i class="l${lvl}" aria-label="${DAYS[d]} ${hourName(hr)}: ${n}"></i>`);
          hover(c, () => `<b>${DAYS[d]} ${hourName(hr)}–${hourName((hr + 1) % 24)}</b><br>${plural(n, st.heat === 'people' ? 'visitor' : 'recording')}`
            + ` in the last ${data.days} days`);
          week.appendChild(c);
        });
      });
    };
    qa('.st-heat-kind button', card).forEach(b => b.addEventListener('click', () => { st.heat = b.dataset.k; draw(); }));
    draw();
    return card;
  }

  // ---------- visitors per day: stacked bars ----------
  function stackedBars(rows, series, height = 180) {
    const box = h('<div class="st-chart"></div>');
    requestAnimationFrame(() => {
      const W = Math.max(260, box.clientWidth || 600), H = height, padL = 30, padB = 22, padT = 8;
      const totals = rows.map(r => series.reduce((s, x) => s + r[x.key], 0));
      const max = Math.max(1, ...totals);
      const step = niceStep(max);
      const top = Math.ceil(max / step) * step;
      const n = rows.length, slot = (W - padL) / n, bw = Math.max(2, Math.min(28, slot * 0.7));
      const y = (v) => padT + (H - padT - padB) * (1 - v / top);
      let svg = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map(s => s.label).join(', '))} per day">`;
      for (let v = 0; v <= top; v += step) {
        svg += `<line class="grid" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${padL - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
      }
      const every = Math.ceil(n / Math.max(1, Math.floor((W - padL) / 56)));
      rows.forEach((r, i) => {
        const cx = padL + slot * i + slot / 2;
        let base = 0;
        series.forEach((s) => {
          const v = r[s.key]; if (!v) return;
          const y1 = y(base + v), y0 = y(base);
          svg += `<rect class="${s.cls}" x="${cx - bw / 2}" y="${y1}" width="${bw}" height="${Math.max(1, y0 - y1 - (base ? 2 : 0))}"/>`;
          base += v;
        });
        if (i % every === 0 || i === n - 1 && n <= 10) {
          const d = new Date(r.day + 'T12:00:00');
          const label = n <= 10 ? DAYS[(d.getDay() + 6) % 7] : d.toLocaleDateString([], {day: 'numeric', month: 'short'});
          svg += `<text class="axis" x="${cx}" y="${H - 6}" text-anchor="middle">${label}</text>`;
        }
        svg += `<rect class="hit" data-i="${i}" x="${padL + slot * i}" y="0" width="${slot}" height="${H - padB}"/>`;
      });
      svg += '</svg>';
      box.innerHTML = svg;
      qa('.hit', box).forEach(el => {
        const r = rows[+el.dataset.i];
        hover(el, () => `<b>${esc(dayLabel(r.day))}</b>` + series.map(s =>
          `<br><i class="sw ${s.cls}"></i>${esc(s.label)}: ${r[s.key]}`).join(''));
      });
    });
    return box;
  }
  function niceStep(max) {
    const raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw)));
    return Math.max(1, [1, 2, 5, 10].map(m => m * p).find(s => s >= raw));
  }
  const legend = (series) => `<div class="st-legend">${series.map(s => `<span><i class="sw ${s.cls}"></i>${esc(s.label)}</span>`).join('')}</div>`;

  function visitorsCard() {
    const what = [{key: 'people', label: 'People', cls: 's1'}, {key: 'vehicle', label: 'Vehicles', cls: 's2'},
      {key: 'parcel', label: 'Parcels', cls: 's3'}];
    const who = [{key: 'family', label: 'Family', cls: 's1'}, {key: 'stranger', label: 'Strangers', cls: 's2'}];
    const card = h(`<div class="card wide"><h2>Visitors per day</h2>${legend(what)}<div class="st-a"></div>
      <h3>Family and strangers</h3><div class="st-b"></div><details class="st-table"><summary>Show as a table</summary></details></div>`);
    q('.st-a', card).appendChild(stackedBars(data.daily, what));
    if (data.face_recognition) {
      q('.st-b', card).append(h(legend(who)), stackedBars(data.daily, who, 140));
    } else {
      q('.st-b', card).innerHTML = '<p class="muted">Turn on recognising family in the Smart tab to see family and strangers here.</p>';
    }
    const rows = data.daily.slice().reverse().filter(r => r.people || r.vehicle || r.parcel || r.activity);
    q('.st-table', card).appendChild(h(`<table><thead><tr><th>Day</th><th>People</th>${data.face_recognition ? '<th>Family</th><th>Strangers</th>' : ''}
      <th>Vehicles</th><th>Parcels</th><th>Recordings</th></tr></thead><tbody>${rows.map(r => `<tr><td>${esc(dayLabel(r.day))}</td><td>${r.people}</td>
      ${data.face_recognition ? `<td>${r.family}</td><td>${r.stranger}</td>` : ''}<td>${r.vehicle}</td><td>${r.parcel}</td><td>${r.activity}</td></tr>`).join('')
      || '<tr><td colspan="7" class="muted">Nothing recorded in this period.</td></tr>'}</tbody></table>`));
    return card;
  }

  // ---------- hot spots ----------
  function hotSpotsCard() {
    const cams = st.camera ? data.cameras.filter(c => c.id === st.camera) : data.cameras;
    const card = h(`<div class="card wide"><div class="st-head"><h2>Hot spots</h2>
      <label class="check"><input type="checkbox" class="st-glow" checked> Show the glow</label></div>
      <p class="muted">Where movement happens most in each camera's picture. Brighter means more. Older movement fades over a few weeks.</p>
      <div class="st-spots"></div></div>`);
    const list = q('.st-spots', card);
    if (!cams.length) list.innerHTML = '<p class="muted">No camera is set up yet.</p>';
    for (const c of cams) {
      const id = encodeURIComponent(c.id);
      const f = h(`<figure class="st-spot"><div class="st-spot-pic">
          <img class="snap" alt="" src="/api/cameras/${id}/snapshot.jpg?t=${Date.now()}">
          <img class="glow" alt="Hot spots for ${esc(c.name)}" src="/api/stats/hotspots/${id}.png?t=${Date.now()}"></div>
        <figcaption><b>${esc(c.name)}</b><button class="ghost small st-clear" title="Start the hot spots again, for example after moving the camera">Start again</button></figcaption></figure>`);
      q('.snap', f).addEventListener('error', (e) => { e.target.replaceWith(h('<div class="snap nopic muted">No picture right now</div>')); });
      q('.st-clear', f).addEventListener('click', async () => {
        if (!confirm(`Start the hot spots for ${c.name} again?`)) return;
        try { await api(`/api/stats/hotspots/${id}`, {method: 'DELETE'}); q('.glow', f).src = `/api/stats/hotspots/${id}.png?t=${Date.now()}`; }
        catch (e) { toast(e.message); }
      });
      list.appendChild(f);
    }
    q('.st-glow', card).addEventListener('change', (e) => card.classList.toggle('no-glow', !e.target.checked));
    return card;
  }

  // ---------- system health ----------
  function ago(sec) {
    const d = Math.floor(sec / 86400), hr = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    return d ? `${plural(d, 'day')}${hr ? ' ' + plural(hr, 'hour') : ''}` : hr ? `${plural(hr, 'hour')}${m ? ' ' + m + ' min' : ''}` : `${m} min`;
  }
  const SIGNAL = ['No signal', 'Very weak', 'Weak', 'Good', 'Very good'];

  function healthCard() {
    const d = health.disk;
    const card = h(`<div class="card wide"><h2>System health</h2><div class="st-tiles st-disk"></div><div class="st-cams"></div></div>`);
    const room = d.levels_off ? `<b>Plenty</b><span class="muted">Keeps ${plural(d.retention_days, 'day')}, oldest deleted first</span>`
      : d.days_left != null ? `<b>${d.days_left > 999 ? '999+' : d.days_left} days</b><span class="muted">At the usual rate</span>`
        : '<b>–</b><span class="muted">Needs a day of recordings</span>';
    const used = d.total ? Math.round(100 * (1 - d.free / d.total)) : null;
    q('.st-disk', card).innerHTML = `
      <div class="st-tile"><span class="muted">Disk space left</span><b>${d.free != null ? fmtBytes(d.free) : '–'}</b>
        ${used != null ? `<span class="st-meter" role="meter" aria-valuenow="${used}" aria-valuemin="0" aria-valuemax="100" aria-label="Disk used"><i style="width:${used}%" class="${used > 90 ? 'bad' : used > 75 ? 'warn' : ''}"></i></span>
        <span class="muted">${used}% of ${fmtBytes(d.total)} used</span>` : ''}</div>
      <div class="st-tile"><span class="muted">Recorded a day</span><b>${d.per_day != null ? fmtBytes(d.per_day) : '–'}</b><span class="muted">Average, last week</span></div>
      <div class="st-tile"><span class="muted">Room for recordings</span>${room}</div>
      <div class="st-tile"><span class="muted">Recordings kept</span><b>${fmtBytes(d.recordings || 0)}</b><span class="muted">${d.oldest_day ? 'Since ' + esc(dayLabel(d.oldest_day)) : 'None yet'}</span></div>`;
    const list = q('.st-cams', card);
    const now = Date.now() / 1000;
    if (!health.cameras.length) list.innerHTML = '<p class="muted">No camera is set up yet.</p>';
    for (const c of health.cameras) {
      const sig = c.signal != null ? SIGNAL[Math.round(c.signal)] : null;
      const el = h(`<div class="st-cam"><h3><span class="dot ${c.online ? 'ok' : 'bad'}" aria-hidden="true"></span>${esc(c.name)}
          <span class="muted">${c.online ? (c.online_since ? 'online for ' + ago(now - c.online_since) : 'online') : 'offline'}</span></h3>
        <div class="st-tiles small">
          <div class="st-tile"><span class="muted">Online, last 24 hours</span><b>${c.uptime_24h != null ? c.uptime_24h + '%' : '–'}</b></div>
          <div class="st-tile"><span class="muted">Online, last 7 days</span><b>${c.uptime_7d != null ? c.uptime_7d + '%' : '–'}</b></div>
          <div class="st-tile"><span class="muted">Wi-Fi signal</span><b>${sig || '–'}</b>${sig ? '' : '<span class="muted">The camera doesn\'t report it</span>'}</div>
          <div class="st-tile"><span class="muted">Response time</span><b>${c.rtt_24h != null ? Math.round(c.rtt_24h) + ' ms' : '–'}</b><span class="muted">Typical, last 24 hours</span></div>
        </div>
        <div class="st-label muted">Online, hour by hour (last 7 days)</div><div class="st-strip"></div>
        <div class="st-strip-key muted"><span><i class="sw ok"></i>Online</span><span><i class="sw warn"></i>Dropped out</span><span><i class="sw bad"></i>Offline</span><span><i class="sw none"></i>No data (PC off)</span></div>
        <div class="st-label muted">${c.signal_supported ? 'Wi-Fi signal' : 'Response time on your network (lower is better)'}, last 7 days</div>
        <div class="st-line"></div></div>`);
      strip(q('.st-strip', el), c.timeline);
      q('.st-line', el).appendChild(c.signal_supported
        ? lineChart(c.timeline, 'signal', (v) => SIGNAL[Math.round(v)] || '', 4)
        : lineChart(c.timeline, 'rtt', (v) => Math.round(v) + ' ms'));
      list.appendChild(el);
    }
    if (!health.cameras.some(c => c.timeline.length)) {
      list.appendChild(h(`<p class="muted">Health is checked every ${health.sample_minutes} minutes, so the charts fill in over the next hour or so.</p>`));
    }
    return card;
  }

  function strip(box, timeline) {
    const byHour = new Map(timeline.map(t => [t.t, t]));
    const end = Math.floor(Date.now() / 3600000) * 3600, start = end - 167 * 3600;
    for (let t = start; t <= end; t += 3600) {
      const s = byHour.get(t);
      const cls = !s ? 'none' : s.online >= 0.99 ? 'ok' : s.online > 0 ? 'warn' : 'bad';
      const c = h(`<i class="${cls}"></i>`);
      const when = new Date(t * 1000);
      hover(c, () => `<b>${when.toLocaleString([], {weekday: 'short', hour: 'numeric'})}</b><br>` +
        (!s ? 'No data (the PC was off or just started)' : `Online ${Math.round(s.online * 100)}% of the hour` +
          (s.fps != null ? `<br>${s.fps.toFixed(1)} pictures a second` : '')));
      box.appendChild(c);
    }
  }

  function lineChart(timeline, key, fmt, fixedMax) {
    const box = h('<div class="st-chart"></div>');
    const pts = timeline.filter(t => t[key] != null);
    if (pts.length < 2) { box.innerHTML = '<p class="muted">Not enough checks yet.</p>'; return box; }
    requestAnimationFrame(() => {
      const W = Math.max(260, box.clientWidth || 600), H = 120, padL = 44, padB = 20, padT = 8;
      const end = Math.floor(Date.now() / 3600000) * 3600, start = end - 167 * 3600;
      const max = fixedMax || Math.max(10, ...pts.map(p => p[key])) * 1.15;
      const x = (t) => padL + (W - padL - 4) * (t - start) / (end - start);
      const y = (v) => padT + (H - padT - padB) * (1 - v / max);
      let svg = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img">`;
      const ticks = fixedMax ? [0, 2, 4] : [0, max / 2, max].map(v => Math.round(v));
      ticks.forEach(v => { svg += `<line class="grid" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${padL - 6}" y="${y(v) + 4}" text-anchor="end">${fixedMax ? esc(fmt(v).split(' ')[0]) : v}</text>`; });
      for (let t = start + 3600 * 24 - (start % 86400 + new Date().getTimezoneOffset() * 60 + 86400) % 86400; t <= end; t += 86400) {
        svg += `<text class="axis" x="${x(t)}" y="${H - 5}" text-anchor="middle">${DAYS[(new Date(t * 1000).getDay() + 6) % 7]}</text>`;
      }
      // Break the line where there are gaps of more than two hours.
      let d = '', prev = null;
      for (const p of pts) {
        d += `${prev && p.t - prev.t <= 7200 ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`;
        prev = p;
      }
      svg += `<path class="line" d="${d}"/><line class="cross" y1="${padT}" y2="${H - padB}" x1="-10" x2="-10"/><circle class="dotm" r="4" cx="-10" cy="-10"/>`;
      svg += `<rect class="hit" x="${padL}" y="0" width="${W - padL}" height="${H}"/></svg>`;
      box.innerHTML = svg;
      const hit = q('.hit', box), cross = q('.cross', box), dot = q('.dotm', box);
      const move = (e) => {
        const pt = e.touches ? e.touches[0] : e;
        const r = box.querySelector('svg').getBoundingClientRect();
        const t = start + (pt.clientX - r.left - padL) / (W - padL - 4) * (end - start);
        const p = pts.reduce((a, b) => Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a);
        cross.setAttribute('x1', x(p.t)); cross.setAttribute('x2', x(p.t));
        dot.setAttribute('cx', x(p.t)); dot.setAttribute('cy', y(p[key]));
        showTip(e, `<b>${new Date(p.t * 1000).toLocaleString([], {weekday: 'short', hour: 'numeric'})}</b><br>${esc(fmt(p[key]))}`);
      };
      hit.addEventListener('mousemove', move);
      hit.addEventListener('touchmove', move, {passive: true});
      hit.addEventListener('touchstart', move, {passive: true});
      hit.addEventListener('mouseleave', () => { hideTip(); cross.setAttribute('x1', -10); cross.setAttribute('x2', -10); dot.setAttribute('cx', -10); });
    });
    return box;
  }

  // ---------- weekly report ----------
  function weeklyCard() {
    const s = data.settings;
    const card = h(`<div class="card wide"><h2>Weekly report</h2>
      <p class="muted">Every Monday morning your phone gets a short summary of the week before, for example
        "42 visitors, 3 strangers. Busiest: Saturday 2pm."</p>
      <div class="st-row">
        <label class="check"><input type="checkbox" class="wr-on"> Send the weekly report</label>
        <label class="check">on Monday at <input type="time" class="wr-time" value="${esc(s.weekly_time)}"></label>
      </div>
      <div class="st-row"><button class="wr-preview">Show last week's report</button><button class="wr-send">Send it to my phone now</button></div>
      <div class="wr-out"></div>
      ${data.reports.length ? `<h3>Sent</h3><ul class="wr-list">${data.reports.map(r =>
        `<li><span class="muted">Week of ${esc(dayLabel(r.week))}</span><b>${esc(r.title)}</b><span>${esc(r.text)}</span></li>`).join('')}</ul>` : ''}
    </div>`);
    const on = q('.wr-on', card), time = q('.wr-time', card);
    on.checked = !!s.weekly_report;
    const save = async () => {
      try { data.settings = await api('/api/stats/settings', {method: 'PUT', body: JSON.stringify({weekly_report: on.checked, weekly_time: time.value})}); toast('Saved'); }
      catch (e) { toast(e.message); }
    };
    on.addEventListener('change', save); time.addEventListener('change', save);
    const show = (r) => {
      q('.wr-out', card).innerHTML = `<div class="wr-preview-box"><span class="muted">Week of ${esc(dayLabel(r.week))}</span><b>${esc(r.title)}</b><span>${esc(r.text)}</span></div>`;
    };
    q('.wr-preview', card).addEventListener('click', async () => { try { show(await api('/api/stats/weekly')); } catch (e) { toast(e.message); } });
    q('.wr-send', card).addEventListener('click', async (e) => {
      e.target.disabled = true;
      try { show(await api('/api/stats/weekly/send', {method: 'POST'})); toast('Sent to your phones'); }
      catch (err) { toast(err.message); }
      e.target.disabled = false;
    });
    return card;
  }

  // ---------- Who's been | Stats switch (phones) ----------
  function addSwitch(section, active) {
    const s = h(`<div class="seg st-switch" role="group" aria-label="Show"><button data-t="seen">Who's been</button><button data-t="stats">Stats</button></div>`);
    qa('button', s).forEach(b => {
      b.classList.toggle('active', b.dataset.t === active);
      b.addEventListener('click', () => { if (b.dataset.t !== active) showTab(b.dataset.t); });
    });
    section.prepend(s);
  }
  addSwitch($('#tab-seen'), 'seen');
  addSwitch($('#tab-stats'), 'stats');

  // A tap on the weekly report notification opens #stats.
  const fromHash = () => { if (/^#stats\b/.test(location.hash)) { showTab('stats'); history.replaceState(null, '', location.pathname); } };
  addEventListener('hashchange', fromHash);
  setTimeout(fromHash, 0);
})();
