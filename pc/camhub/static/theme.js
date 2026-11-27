// Loaded first, before the page draws, so it never flashes the wrong colours.
(function () {
  var KEY = 'asd-theme';
  function get() { try { return localStorage.getItem(KEY) || 'dark'; } catch (e) { return 'dark'; } }
  function apply(t) {
    var root = document.documentElement;
    root.setAttribute('data-theme', t);
    var light = t === 'light' || (t === 'auto' && window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', light ? '#FFFFFF' : '#0B1020');
  }
  window.asdTheme = {
    get: get,
    set: function (t) { try { localStorage.setItem(KEY, t); } catch (e) {} apply(t); }
  };
  apply(get());
  // Inside the Android app, which keeps the page clear of the phone's status and gesture bars.
  if (window.HomeCamera) document.documentElement.classList.add('in-app');
})();
