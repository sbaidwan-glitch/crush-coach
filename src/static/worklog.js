// Glue for the work log side panel.
//
// The panel is a <dialog> (#worklog) that is always in the page for signed-in members; its
// content comes from the server (/worklog/panel) when it is opened. Links with
// [data-worklog-open] open it: the top bar button, the Home banner. After "Mark step done"
// the address carries ?log=CODE and the dialog has data-open-url, so it opens on load.
// Inside the panel htmx swaps #worklog-body for each step (tidy, post, retry, tabs).
//
// Without JavaScript (or if the panel can't load) those links go to /worklog, the same
// content as a page. Esc and the Close button shut the dialog. After a note is posted the
// page behind is reloaded when the dialog closes, so the task's History shows it.
(function () {
  var dialog = document.getElementById("worklog");
  if (!dialog || typeof dialog.showModal !== "function") return;
  var body = document.getElementById("worklog-body");
  var posted = false;

  function show(html) {
    body.innerHTML = html;
    if (window.htmx) window.htmx.process(body);
    if (!dialog.open) dialog.showModal();
    var title = document.getElementById("worklog-title");
    if (title) title.focus();
    if (body.querySelector("[data-posted]")) posted = true;
  }

  function open(url, fallback) {
    // The server sends the same fragment htmx asks for.
    fetch(url, { credentials: "same-origin", headers: { "HX-Request": "true" } })
      .then(function (response) {
        if (!response.ok) throw new Error("panel " + response.status);
        return response.text();
      })
      .then(show)
      .catch(function () {
        // Couldn't load the panel: the page version still works.
        if (fallback) window.location.href = fallback;
      });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-worklog-open]");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    open(link.getAttribute("data-panel-url") || link.href, link.href);
  });

  // A note posted inside the panel: remember it, so closing refreshes the page behind.
  body.addEventListener("htmx:afterSwap", function () {
    if (body.querySelector("[data-posted]")) posted = true;
    var title = document.getElementById("worklog-title");
    if (title) title.focus();
  });

  dialog.addEventListener("close", function () {
    if (!posted) return;
    posted = false;
    var url = new URL(window.location.href);
    url.searchParams.delete("log");
    window.location.href = url.toString();
  });

  // Clicking the dimmed area outside the panel closes it.
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) dialog.close();
  });

  var onLoad = dialog.getAttribute("data-open-url");
  if (onLoad) {
    // Drop ?log= so reloading doesn't open it again.
    var here = new URL(window.location.href);
    if (here.searchParams.has("log")) {
      here.searchParams.delete("log");
      window.history.replaceState(null, "", here.pathname + here.search + here.hash);
    }
    open(onLoad, null);
  }
})();
