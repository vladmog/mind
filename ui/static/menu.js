(() => {
  function attach(button) {
    const id = button.getAttribute('aria-controls');
    if (!id) return;
    const menu = document.getElementById(id);
    if (!menu) return;

    function open() {
      menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
    }

    function close() {
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
    }

    function toggle() {
      if (menu.hidden) open(); else close();
    }

    button.addEventListener('click', (e) => {
      e.stopPropagation();
      toggle();
    });

    menu.addEventListener('click', (e) => {
      if (e.target.closest('a')) close();
    });

    document.addEventListener('click', (e) => {
      if (menu.hidden) return;
      if (button.contains(e.target) || menu.contains(e.target)) return;
      close();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !menu.hidden) {
        close();
        button.focus();
      }
    });
  }

  function init() {
    document.querySelectorAll('.nav-menu-button').forEach(attach);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
