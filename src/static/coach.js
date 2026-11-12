// Glue for the coach chat.
//
// Sending: the form (and each Retry button) posts to ".../coach/stream" and the answer
// is shown AS IT IS WRITTEN. The server sends one JSON object per line:
//   step    what the coach is doing now (shown in the working slot)
//   text    the answer so far, already rendered as safe HTML
//   reset   the model started over; clear the text
//   done    the finished answer, as the HTML for the thread
//   failed  what failed, as the HTML for the thread (with a Retry button)
//   rejected  nothing was saved (too long, over the daily limit): a plain message
// The member's message is shown at once and never removed by a failure.
//
// Without fetch streaming (or JavaScript) the forms post normally and the server
// answers the old way: htmx for the main form, a redirect for Retry.
(function () {
  var WORKING_TEXT = "Searching the Drive index…";
  var canStream = !!(window.fetch && window.ReadableStream && window.TextDecoder);
  var busy = false;

  function byId(id) {
    return document.getElementById(id);
  }

  function showError(text) {
    var box = byId("coach-error");
    var message = byId("coach-error-text");
    if (box && message) {
      message.textContent = text;
      box.hidden = false;
    }
  }

  function hideError() {
    var box = byId("coach-error");
    if (box) box.hidden = true;
  }

  function setStatus(text) {
    var status = byId("coach-status");
    // Only touch the live region when the text changes, so it isn't re-announced.
    if (status && status.textContent !== text) status.textContent = text;
  }

  function showPending(visible) {
    var pending = byId("coach-pending");
    if (pending) pending.hidden = !visible;
  }

  function echoMemberMessage(text) {
    var template = byId("coach-user-template");
    var pending = byId("coach-pending");
    if (!template || !pending) return null;
    // "No messages yet" is no longer true.
    var empty = document.querySelector("#coach-log .empty");
    if (empty) empty.parentNode.removeChild(empty);
    var node = template.content.firstElementChild.cloneNode(true);
    // Plain text only: the member's message is data, never parsed as markup.
    node.querySelector(".msg-body").textContent = text;
    pending.parentNode.insertBefore(node, pending);
    return node;
  }

  // The coach's answer while it is being written: the same markup as a finished one,
  // marked busy so screen readers wait for the final version instead of reading every
  // partial update.
  function liveArticle() {
    var live = byId("coach-live");
    if (live) return live;
    var pending = byId("coach-pending");
    if (!pending) return null;
    live = document.createElement("article");
    live.id = "coach-live";
    live.className = "msg msg--coach";
    live.setAttribute("aria-busy", "true");
    var who = document.createElement("div");
    who.className = "msg__who";
    who.textContent = "Coach";
    live.appendChild(who);
    var body = document.createElement("div");
    body.className = "msg-md";
    live.appendChild(body);
    pending.parentNode.insertBefore(live, pending);
    return live;
  }

  function removeLive() {
    var live = byId("coach-live");
    if (live) live.parentNode.removeChild(live);
  }

  function insertHtml(html) {
    var pending = byId("coach-pending");
    if (pending) pending.insertAdjacentHTML("beforebegin", html);
  }

  function setBusy(on) {
    busy = on;
    var fields = document.querySelector("#coach-form fieldset");
    if (fields) fields.disabled = on;
    var retries = document.querySelectorAll("[data-coach-retry] button");
    for (var i = 0; i < retries.length; i++) retries[i].disabled = on;
  }

  function handle(event, state) {
    switch (event.type) {
      case "step":
        setStatus(event.text);
        break;
      case "text": {
        var live = liveArticle();
        if (!live) break;
        showPending(false);
        live.querySelector(".msg-md").innerHTML = event.html;
        state.sawText = true;
        break;
      }
      case "reset": {
        var existing = byId("coach-live");
        if (existing) existing.querySelector(".msg-md").innerHTML = "";
        showPending(true);
        break;
      }
      case "done":
        removeLive();
        insertHtml(event.html);
        state.finished = true;
        if (!state.isRetry) {
          var box = byId("coach-message");
          if (box) box.value = "";
        }
        break;
      case "failed":
        removeLive();
        insertHtml(event.html);
        state.finished = true;
        if (!state.isRetry) {
          // The message is saved in the thread now; Retry sends it again.
          var field = byId("coach-message");
          if (field) field.value = "";
        }
        break;
      case "rejected":
        removeLive();
        showError(event.message);
        state.finished = true;
        state.rejected = true;
        break;
    }
  }

  function readLines(response, state) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function lines(final) {
      var parts = buffer.split("\n");
      buffer = final ? "" : parts.pop();
      if (final && parts[parts.length - 1] === "") parts.pop();
      parts.forEach(function (line) {
        if (!line.trim()) return;
        try {
          handle(JSON.parse(line), state);
        } catch (err) {
          /* a damaged line is skipped; the final line decides the outcome */
        }
      });
    }

    function pump() {
      return reader.read().then(function (result) {
        if (result.done) {
          buffer += decoder.decode();
          lines(true);
          return;
        }
        buffer += decoder.decode(result.value, { stream: true });
        lines(false);
        return pump();
      });
    }
    return pump();
  }

  function send(form, isRetry) {
    if (busy) return;
    var body = new URLSearchParams();
    var echoed = null;
    if (isRetry) {
      body.set("retry", form.querySelector('input[name="retry"]').value);
      var failed = form.closest("[data-failed]");
      if (failed) failed.parentNode.removeChild(failed);
    } else {
      var field = byId("coach-message");
      var text = field ? field.value.trim() : "";
      if (!text) return;
      body.set("message", text);
      echoed = echoMemberMessage(text);
    }
    hideError();
    setBusy(true);
    setStatus(WORKING_TEXT);
    showPending(true);

    var state = { isRetry: isRetry, sawText: false, finished: false, rejected: false };
    var url = form.getAttribute("action") + "/stream";
    fetch(url, {
      method: "POST",
      body: body,
      headers: { Accept: "application/x-ndjson" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok || !response.body) {
          throw new Error("http " + response.status);
        }
        return readLines(response, state);
      })
      .then(function () {
        if (!state.finished) {
          throw new Error("the answer ended early");
        }
        if (state.rejected && echoed && echoed.parentNode) {
          // Nothing was saved; the text is still in the box, so show it only once.
          echoed.parentNode.removeChild(echoed);
        }
      })
      .catch(function () {
        removeLive();
        if (echoed && echoed.parentNode && !state.sawText) {
          echoed.parentNode.removeChild(echoed);
        }
        showError(
          state.sawText
            ? "The connection dropped before the answer finished. Reload the page to see what was saved."
            : "Couldn't reach the server. Your message is still in the box; check your connection and send it again."
        );
      })
      .then(function () {
        showPending(false);
        setStatus("");
        setBusy(false);
        var box = byId("coach-message");
        if (box) box.focus();
      });
  }

  // Capture phase, so this runs before htmx sees the submit and stops it: the stream
  // replaces htmx for sending. Where streaming isn't available the event goes on.
  document.addEventListener(
    "submit",
    function (event) {
      if (!canStream) return;
      var form = event.target;
      var isRetry = form.hasAttribute && form.hasAttribute("data-coach-retry");
      if (form.id !== "coach-form" && !isRetry) return;
      event.preventDefault();
      event.stopPropagation();
      send(form, isRetry);
    },
    true
  );

  // --- the htmx path, for browsers that can't stream ---

  function isCoachForm(event) {
    var elt = event.detail && event.detail.elt;
    return !!elt && elt.id === "coach-form";
  }

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    if (!isCoachForm(event)) return;
    hideError();
    var field = byId("coach-message");
    var text = field ? field.value.trim() : "";
    if (text) echoMemberMessage(text);
    setStatus(WORKING_TEXT);
    showPending(true);
  });

  // Runs on success, server error and network failure alike.
  document.body.addEventListener("htmx:afterRequest", function (event) {
    if (!isCoachForm(event)) return;
    showPending(false);
    setStatus("");
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
