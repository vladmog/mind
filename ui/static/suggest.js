(() => {
  const DEBOUNCE_MS = 120;

  function attach(form) {
    const input = form.querySelector('input[type="search"][name="q"]');
    if (!input) return;

    const menu = document.createElement('ul');
    menu.className = 'suggest-menu';
    menu.setAttribute('role', 'listbox');
    menu.hidden = true;
    form.appendChild(menu);

    input.setAttribute('autocomplete', 'off');
    input.setAttribute('aria-autocomplete', 'list');

    let items = [];
    let active = -1;
    let debounceTimer = null;
    let latestQuery = '';
    let blurTimer = null;

    function close() {
      menu.hidden = true;
      menu.innerHTML = '';
      items = [];
      active = -1;
    }

    function highlight(i) {
      if (items.length === 0) return;
      if (active >= 0 && items[active]) items[active].removeAttribute('aria-selected');
      active = (i + items.length) % items.length;
      items[active].setAttribute('aria-selected', 'true');
      items[active].scrollIntoView({ block: 'nearest' });
    }

    function render(results) {
      menu.innerHTML = '';
      items = [];
      active = -1;
      if (!results.length) {
        menu.hidden = true;
        return;
      }
      for (const r of results) {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        const a = document.createElement('a');
        a.href = '/p/' + encodeURIComponent(r.slug);
        a.textContent = r.title || r.slug;
        a.addEventListener('mousedown', (e) => {
          e.preventDefault();
          window.location.href = a.href;
        });
        li.appendChild(a);
        menu.appendChild(li);
        items.push(li);
      }
      menu.hidden = false;
    }

    async function fetchSuggestions(q) {
      try {
        const res = await fetch('/api/suggest?q=' + encodeURIComponent(q));
        if (!res.ok) return;
        const data = await res.json();
        if (q !== latestQuery) return;
        render(data);
      } catch {
        // network error: silently ignore
      }
    }

    input.addEventListener('input', () => {
      const q = input.value.trim();
      latestQuery = q;
      clearTimeout(debounceTimer);
      if (!q) {
        close();
        return;
      }
      debounceTimer = setTimeout(() => fetchSuggestions(q), DEBOUNCE_MS);
    });

    input.addEventListener('keydown', (e) => {
      if (menu.hidden || items.length === 0) {
        if (e.key === 'Escape') input.blur();
        return;
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        highlight(active + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        highlight(active - 1);
      } else if (e.key === 'Enter') {
        if (active >= 0 && items[active]) {
          e.preventDefault();
          const a = items[active].querySelector('a');
          if (a) window.location.href = a.href;
        }
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close();
      }
    });

    input.addEventListener('blur', () => {
      blurTimer = setTimeout(close, 120);
    });
    input.addEventListener('focus', () => {
      clearTimeout(blurTimer);
      if (input.value.trim() && items.length) menu.hidden = false;
    });
  }

  function init() {
    document.querySelectorAll('form[data-suggest]').forEach(attach);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
