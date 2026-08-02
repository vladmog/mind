(function () {
  const KEY = 'mind-theme';
  const root = document.documentElement;
  const mql = window.matchMedia('(prefers-color-scheme: dark)');

  function readPref() {
    try {
      const v = localStorage.getItem(KEY);
      return v === 'light' || v === 'dark' ? v : null;
    } catch (e) {
      return null;
    }
  }

  function writePref(value) {
    try {
      if (value === null) localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, value);
    } catch (e) {}
  }

  function effectiveFor(pref) {
    if (pref === 'light' || pref === 'dark') return pref;
    return mql.matches ? 'dark' : 'light';
  }

  function apply(pref) {
    const eff = effectiveFor(pref);
    root.setAttribute('data-theme', eff);
    document.dispatchEvent(new CustomEvent('mind:themechange', {
      detail: { pref: pref, effective: eff }
    }));
  }

  function labelFor(pref) {
    if (pref === 'light') return 'theme: light';
    if (pref === 'dark') return 'theme: dark';
    return 'theme: system';
  }

  function updateButtons(pref) {
    const text = labelFor(pref);
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      btn.textContent = text;
    });
  }

  function cycle(pref) {
    if (pref === null) return 'light';
    if (pref === 'light') return 'dark';
    return null;
  }

  function bind() {
    const pref = readPref();
    updateButtons(pref);
    document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const next = cycle(readPref());
        writePref(next);
        apply(next);
        updateButtons(next);
      });
    });
  }

  mql.addEventListener('change', function () {
    if (readPref() === null) apply(null);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
