// Small glue for the coach chat (htmx does the posting).
//
// On submit: show the member's message at once, show the coach's "working" slot, and
// poll for what the coach is doing. When the answer (or a failure) arrives: hide the
// slot and give the form back. Be honest when the network or server fails: htmx
// doesn't swap error responses, so say so here.
(function () {
  var POLL_MS = 1000;
  var WORKING_TEXT = "Searching the team Drive…";
  var pollTimer = null;
  var lastStep = "";

  function byId(id) {
    return document.getElementById(id);
  }

  function isCoachForm(event) {
    var elt = event.detail && event.detail.elt;
    return !!elt && elt.id === "coach-form";
  }

  function showError(text) {
    var box = byId("coach-error");
    var message = byId("coach-error-text");
    if (box && message) {
      message.textContent = text;
      box.hidden = false;
    }
  }

  function setStatus(text) {
    var status = byId("coach-status");
    // Only touch the live region when the text changes, so it isn't re-announced.
    if (status && status.textContent !== text) status.textContent = text;
  }

  function echoMemberMessage(text) {
    var template = byId("coach-user-template");
    var pending = byId("coach-pending");
    if (!template || !pending) return;
    var node = template.content.firstElementChild.cloneNode(true);
    // Plain text only: the member's message is data, never parsed as markup.
    node.querySelector(".msg-body").textContent = text;
    pending.parentNode.insertBefore(node, pending);
  }

  function poll(url) {
    fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        // An empty step means nothing new to say; keep what is showing.
        if (data && data.step && data.step !== lastStep) {
          lastStep = data.step;
          setStatus(data.step);
        }
      })
      .catch(function () {
        /* the generic text stays; the POST reports real failures */
      });
  }

  function startWaiting(form) {
    var pending = byId("coach-pending");
    lastStep = "";
    setStatus(WORKING_TEXT);
    if (pending) pending.hidden = false;
    var url = form.getAttribute("data-progress-url");
    if (url) pollTimer = window.setInterval(function () { poll(url); }, POLL_MS);
  }

  function stopWaiting() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    var pending = byId("coach-pending");
    if (pending) pending.hidden = true;
    setStatus("");
  }

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    if (!isCoachForm(event)) return;
    var box = byId("coach-error");
    if (box) box.hidden = true;
    var field = byId("coach-message");
    var text = field ? field.value.trim() : "";
    if (text) echoMemberMessage(text);
    startWaiting(event.detail.elt);
  });

  // Runs on success, server error and network failure alike.
  document.body.addEventListener("htmx:afterRequest", function (event) {
    if (!isCoachForm(event)) return;
    stopWaiting();
    // htmx re-enables the form as the request ends; focus after that.
    window.setTimeout(function () {
      var field = byId("coach-message");
      if (field) field.focus();
    }, 0);
  });

  document.body.addEventListener("htmx:sendError", function () {
    showError("Couldn't reach the server. Check your connection and try again.");
  });
  document.body.addEventListener("htmx:responseError", function () {
    showError("The coach hit a server problem. Try again in a moment.");
  });
  // The server sends this only after a successful answer; on a failure the
  // message stays in the box so it can be sent again.
  document.body.addEventListener("coachSent", function () {
    var box = byId("coach-message");
    if (box) box.value = "";
  });
})();
