'use strict';
// Settings > Privacy: draw areas of the picture that are always blurred or blacked out.
(function () {
  const q = (s, el) => el.querySelector(s);
  const escText = (t) => { const d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; };
  const say = (t) => (typeof toast === 'function' ? toast(t) : alert(t));
  const call = (path, opts) => (typeof api === 'function' ? api(path, opts)
    : fetch(path, {headers: {'Content-Type': 'application/json'}, ...(opts || {})}).then(r => r.json()));

  async function privacyCard(cam) {
    const card = document.createElement('div');
    card.className = 'card privacy-card';
    let saved = {style: 'blur', masks: []};
    try { saved = await call(`/api/cameras/${cam.id}/privacy`); } catch (e) { /* show empty */ }
    const masks = JSON.parse(JSON.stringify(saved.masks || []));
    let style = saved.style || 'blur';
    let draft = [];
    let drag = null;

    card.innerHTML = `<h2>Privacy: ${escText(cam.name)}</h2>
      <p class="muted" style="margin-top:0">Cover parts of the picture you shouldn't see, such as a neighbour's
        window or the street. They are hidden in the live picture, saved pictures, phone alerts and new recordings.</p>
      <p class="muted">Drag across the picture to draw a box, or tap points around any shape and press Finish area.</p>
      <div class="seg privacy-style" style="margin-bottom:10px">
        <button data-s="blur">Blur</button><button data-s="black">Black out</button>
      </div>
      <div class="zone-editor privacy-editor"><img alt=""><canvas></canvas></div>
      <div class="zone-list"></div>
      <div class="toolbar"><button class="finish">Finish area</button><button class="undo">Undo point</button>
        <div style="flex:1"></div><button class="primary save">Save privacy areas</button></div>
      <p class="hint muted">Recordings with privacy areas take a little longer to appear, because the PC covers the
        areas once each recording ends. Older recordings are not changed. If the camera is turned, the areas stay
        in the same place on the picture, so check them after turning it.</p>`;

    const img = q('img', card), canvas = q('canvas', card), editor = q('.privacy-editor', card);
    const reload = () => { img.src = `/api/cameras/${cam.id}/snapshot.jpg?t=${Date.now()}`; };
    img.addEventListener('load', () => { editor.style.aspectRatio = `${img.naturalWidth}/${img.naturalHeight}`; draw(); });
    img.addEventListener('error', () => { editor.classList.add('no-picture'); draw(); });
    reload();

    function setStyle(v) {
      style = v;
      card.querySelectorAll('.privacy-style button').forEach(b => b.classList.toggle('active', b.dataset.s === v));
      draw();
    }
    card.querySelectorAll('.privacy-style button').forEach(b => b.addEventListener('click', () => setStyle(b.dataset.s)));

    function path(ctx, pts, W, H) {
      ctx.beginPath();
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
    }
    function draw() {
      const r = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, r.width * devicePixelRatio); canvas.height = Math.max(1, r.height * devicePixelRatio);
      const ctx = canvas.getContext('2d'); const W = canvas.width, H = canvas.height;
      ctx.clearRect(0, 0, W, H); ctx.lineWidth = 2 * devicePixelRatio;
      for (const m of masks) {
        path(ctx, m.points, W, H); ctx.closePath();
        ctx.fillStyle = style === 'black' ? 'rgba(0,0,0,.85)' : 'rgba(120,120,140,.55)'; ctx.fill();
        ctx.setLineDash([6 * devicePixelRatio, 4 * devicePixelRatio]); ctx.strokeStyle = '#c9a7ff'; ctx.stroke(); ctx.setLineDash([]);
      }
      const pts = drag ? box(drag) : draft;
      if (pts.length) {
        path(ctx, pts, W, H); if (drag) ctx.closePath();
        ctx.strokeStyle = '#4ea1ff'; ctx.stroke();
        if (!drag) for (const [x, y] of pts) { ctx.beginPath(); ctx.arc(x * W, y * H, 4 * devicePixelRatio, 0, 7); ctx.fillStyle = '#4ea1ff'; ctx.fill(); }
      }
      const list = q('.zone-list', card); list.innerHTML = '';
      if (!masks.length) list.innerHTML = '<span class="muted">No privacy areas yet.</span>';
      masks.forEach((m, i) => {
        const p = document.createElement('span'); p.className = 'pill';
        p.innerHTML = `${escText(m.name)} <button title="Remove" aria-label="Remove ${escText(m.name)}">✕</button>`;
        q('button', p).addEventListener('click', () => { masks.splice(i, 1); draw(); });
        list.appendChild(p);
      });
    }
    const box = (d) => {
      const [x1, y1] = d.a, [x2, y2] = d.b;
      return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
    };
    const at = (e) => {
      const r = canvas.getBoundingClientRect();
      const f = (v) => +Math.max(0, Math.min(1, v)).toFixed(4);
      return [f((e.clientX - r.left) / r.width), f((e.clientY - r.top) / r.height)];
    };
    const addArea = (pts) => masks.push({name: `Private area ${masks.length + 1}`, points: pts});

    canvas.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      canvas.setPointerCapture && canvas.setPointerCapture(e.pointerId);
      drag = {a: at(e), b: at(e), x: e.clientX, y: e.clientY, moved: false};
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!drag) return;
      drag.b = at(e);
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) > 10) drag.moved = true;
      if (drag.moved) draw();
    });
    canvas.addEventListener('pointerup', (e) => {
      if (!drag) return;
      const d = drag; drag = null;
      d.b = at(e);
      if (d.moved && !draft.length) {
        const [x1, y1] = d.a, [x2, y2] = d.b;
        if (Math.abs(x2 - x1) > 0.01 && Math.abs(y2 - y1) > 0.01) addArea(box(d));
      } else draft.push(d.a);
      draw();
    });
    canvas.addEventListener('pointercancel', () => { drag = null; draw(); });
    q('.finish', card).addEventListener('click', () => {
      if (draft.length < 3) { say('Tap at least 3 points around the area, or drag to draw a box.'); return; }
      addArea(draft); draft = []; draw();
    });
    q('.undo', card).addEventListener('click', () => { draft.pop(); draw(); });
    q('.save', card).addEventListener('click', async () => {
      if (draft.length >= 3) { addArea(draft); draft = []; }
      try {
        const r = await call(`/api/cameras/${cam.id}/privacy`, {method: 'PUT', body: JSON.stringify({style, masks})});
        masks.splice(0, masks.length, ...r.masks);
        say(r.masks.length ? 'Privacy areas saved' : 'Privacy areas removed');
        setTimeout(reload, 800);
      } catch (e) { say('Could not save: ' + e.message); }
      draw();
    });
    window.addEventListener('resize', draw);
    setStyle(style);
    return card;
  }

  window.privacyCard = privacyCard;
})();
