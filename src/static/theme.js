// The Light/Dark button in the sidebar. The choice is kept in localStorage (`cc-theme`);
// the small script in <head> applies it before the first paint. Without a saved choice
// the page follows the system (prefers-color-scheme) until the button is pressed.
(function () {
  var root = document.documentElement;
  var themeBtn = document.getElementById("theme-btn");
  if (!themeBtn) return;

  function isDark() {
    if (root.dataset.theme) return root.dataset.theme === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function sync() {
    // The label names the mode you would switch to.
    document.getElementById("theme-label").textContent = isDark() ? "Light" : "Dark";
  }

  themeBtn.addEventListener("click", function () {
    root.dataset.theme = isDark() ? "light" : "dark";
    try { localStorage.setItem("cc-theme", root.dataset.theme); } catch (e) { /* private mode: still works for this page */ }
    sync();
  });

  themeBtn.hidden = false;
  sync();
})();
