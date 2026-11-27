'use strict';
// Appearance (dark, light or follow the device), nicer toasts, and a light tap you can feel
// on buttons inside the Android app. The theme itself is applied early by theme.js.

(function () {
  const app = window.HomeCamera;

  // ---------- toasts: one at a time, tap to dismiss ----------
  window.toast = function (text) {
    document.querySelectorAll('.toast').forEach(t => t.remove());
    const t = document.createElement('div');
    t.className = 'toast'; t.setAttribute('role', 'status'); t.textContent = text;
    const close = () => { t.classList.add('leaving'); setTimeout(() => t.remove(), 220); };
    t.addEventListener('click', close);
    document.body.appendChild(t);
    setTimeout(close, Math.min(8000, 3000 + text.length * 40));
  };

  // ---------- haptics (Android app only) ----------
  if (app && app.haptic) {
    document.addEventListener('click', e => {
      const b = e.target.closest('button, .chip, label.switch');
      if (!b || b.disabled) return;
      try { app.haptic(b.classList.contains('primary') ? 'confirm' : 'tap'); } catch (_) {}
    }, true);
  }

  // ---------- Settings > Appearance ----------
  window.appearanceCard = function () {
    const card = document.createElement('div'); card.className = 'card';
    const cur = window.asdTheme ? window.asdTheme.get() : 'dark';
    card.innerHTML = `<h2>Appearance</h2>
      <div class="seg theme-seg" role="radiogroup" aria-label="Appearance">
        <button data-t="dark">Dark</button><button data-t="light">Light</button><button data-t="auto">Match device</button>
      </div>
      <p class="muted small" style="margin-bottom:0">Only changes this ${app ? 'phone' : 'browser'}.</p>`;
    const mark = t => card.querySelectorAll('.theme-seg button').forEach(b => {
      b.classList.toggle('active', b.dataset.t === t); b.setAttribute('aria-checked', b.dataset.t === t);
    });
    mark(cur);
    card.querySelectorAll('.theme-seg button').forEach(b => b.addEventListener('click', () => {
      window.asdTheme.set(b.dataset.t); mark(b.dataset.t);
    }));
    return card;
  };
})();
