// Small glue for the coach chat (htmx does the posting).
// Be honest when the network or server fails: htmx doesn't swap error responses.
(function () {
  function showError(text) {
    var box = document.getElementById("coach-error");
    var message = document.getElementById("coach-error-text");
    if (box && message) {
      message.textContent = text;
      box.hidden = false;
    }
  }

  document.body.addEventListener("htmx:beforeRequest", function () {
    var box = document.getElementById("coach-error");
    if (box) box.hidden = true;
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
    var box = document.getElementById("coach-message");
    if (box) {
      box.value = "";
      box.focus();
    }
  });
})();
