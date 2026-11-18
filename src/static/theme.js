// The Light/Dark and Bubbles buttons in the sidebar. The choice is kept in localStorage
// (`cc-theme`, `cc-bubbles`); the small script in <head> applies it before the first paint.
// Without a saved choice the page follows the system (prefers-color-scheme), and bubbles
// are on unless the person prefers reduced motion (theme.css hides them then).
(function () {
  var root = document.documentElement;
  var themeBtn = document.getElementById("theme-btn");
  var bubblesBtn = document.getElementById("bubbles-btn");
  if (!themeBtn || !bubblesBtn) return;

  function isDark() {
    if (root.dataset.theme) return root.dataset.theme === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function remember(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode: still works for this page */ }
  }
  function sync() {
    document.getElementById("theme-label").textContent = isDark() ? "Light" : "Dark";
    bubblesBtn.setAttribute("aria-pressed", root.dataset.bubbles === "off" ? "false" : "true");
  }

  themeBtn.addEventListener("click", function () {
    root.dataset.theme = isDark() ? "light" : "dark";
    remember("cc-theme", root.dataset.theme);
    sync();
  });
  bubblesBtn.addEventListener("click", function () {
    if (root.dataset.bubbles === "off") delete root.dataset.bubbles;
    else root.dataset.bubbles = "off";
    remember("cc-bubbles", root.dataset.bubbles || "on");
    sync();
  });

  themeBtn.hidden = false;
  bubblesBtn.hidden = false;
  sync();
})();
