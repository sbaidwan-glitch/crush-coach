// Glue for the Notebook: photos, and the side panel.
//
// Photos. Each file chosen in an entry's "Add photos" boxes is shrunk here, in the browser,
// before it is sent: drawn onto a canvas no larger than 1,600 px on its longer side and saved
// as a JPEG of at most 1.5 MB (the canvas drops the photo's location data and camera details
// on the way). Then the entry is saved with them (the hidden "send" button), so a photo is
// kept as soon as it is chosen. If the browser can't shrink a file the original is sent and
// the server decides: it checks the real file type, shrinks it and strips its location data
// itself. On a phone the first box opens the camera.
//
// The panel. The panel is a <dialog> (#notebook) that is always in the page for signed-in
// members; its content comes from the server (/notebook/panel) when it is opened. Links with
// [data-notebook-open] open it: the top bar button, "+ Log today's work", the leaving nudge. After "Mark step done"
// the address carries ?log=CODE and the dialog has data-open-url, so it opens on load. Inside
// the panel htmx swaps #notebook-body for each step (tabs, tidy, save, publish, retry).
//
// The leaving nudge (the second block, at the end of this file) is a small prompt the server
// renders, hidden, on every page where the member worked on a task today and hasn't written it
// up. It is shown on exit intent only, at most once per local day.
//
// Without JavaScript (or if the panel can't load) those links go to /notebook, the same
// content as a page. Esc and the Close button shut the dialog. After an entry is published
// the page behind is reloaded when the dialog closes, so the task's History shows it.
(function () {
  var MAX_SIDE = 1600;
  var MAX_BYTES = 1500000;
  var SIDES = [MAX_SIDE, 1280, 1024];
  var QUALITIES = [0.85, 0.75, 0.65, 0.55, 0.45];

  function say(form, text) {
    var status = form.querySelector("[data-photo-status]");
    if (status) status.textContent = text;
  }

  function loadImage(file) {
    if (window.createImageBitmap) {
      return window
        .createImageBitmap(file, { imageOrientation: "from-image" })
        .catch(function () {
          return window.createImageBitmap(file);
        });
    }
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        resolve(img);
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error("not an image"));
      };
      img.src = url;
    });
  }

  function toBlob(canvas, quality) {
    return new Promise(function (resolve) {
      canvas.toBlob(resolve, "image/jpeg", quality);
    });
  }

  // Draw at each side length, lowering the quality until the file is small enough.
  function encode(image) {
    var width = image.width;
    var height = image.height;
    var attempts = [];
    SIDES.forEach(function (side) {
      QUALITIES.forEach(function (quality) {
        attempts.push([side, quality]);
      });
    });
    function next(index) {
      if (index >= attempts.length) return Promise.reject(new Error("too big"));
      var side = attempts[index][0];
      var scale = Math.min(1, side / Math.max(width, height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(width * scale));
      canvas.height = Math.max(1, Math.round(height * scale));
      var context = canvas.getContext("2d");
      context.fillStyle = "#ffffff";
      context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      return toBlob(canvas, attempts[index][1]).then(function (blob) {
        if (blob && blob.size <= MAX_BYTES) return blob;
        return next(index + 1);
      });
    }
    return next(0);
  }

  function shrink(file) {
    return loadImage(file)
      .then(encode)
      .then(function (blob) {
        var name = (file.name || "photo").replace(/\.[^.]*$/, "") + ".jpg";
        return new File([blob], name, { type: "image/jpeg" });
      })
      .catch(function () {
        return file; // the server will decide
      });
  }

  function choose(input) {
    var form = input.form;
    if (!form || !input.files || !input.files.length) return;
    var box = form.querySelector("[data-photo-add]");
    var left = box ? parseInt(box.getAttribute("data-photos-left"), 10) : 0;
    var files = Array.prototype.slice.call(input.files);
    if (left <= 0) {
      input.value = "";
      say(form, "This entry already has its photos. Remove one to add another.");
      return;
    }
    var note = "";
    if (files.length > left) {
      files = files.slice(0, left);
      note = " Only " + left + " more fit, so the rest were left out.";
    }
    say(form, "Shrinking " + files.length + (files.length === 1 ? " photo…" : " photos…") + note);
    Promise.all(files.map(shrink)).then(function (shrunk) {
      var send = form.querySelector("[data-photo-send]");
      try {
        var transfer = new DataTransfer();
        shrunk.forEach(function (file) {
          transfer.items.add(file);
        });
        input.files = transfer.files;
      } catch (err) {
        // An older browser can't swap the files: the originals go, and the server shrinks them.
      }
      if (send) {
        say(form, "Adding…" + note);
        send.click();
      } else {
        say(form, "Ready. Save the draft to keep the photos." + note);
      }
    });
  }

  document.addEventListener("change", function (event) {
    var input = event.target.closest && event.target.closest("[data-photo-input]");
    if (input) choose(input);
  });

  // The panel: only where there is a <dialog> (not on /notebook itself).
  var dialog = document.getElementById("notebook");
  if (!dialog || typeof dialog.showModal !== "function") return;
  var body = document.getElementById("notebook-body");
  var published = false;

  function show(html) {
    body.innerHTML = html;
    if (window.htmx) window.htmx.process(body);
    if (!dialog.open) dialog.showModal();
    var title = document.getElementById("notebook-title");
    if (title) title.focus();
    if (body.querySelector("[data-posted]")) published = true;
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
    var link = event.target.closest("[data-notebook-open]");
    if (!link || event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    open(link.getAttribute("data-panel-url") || link.href, link.href);
  });

  // The server answers a refused form (a limit, a missing task, a bad photo) with a 4xx
  // that carries the panel again with the reason in it. htmx doesn't swap errors unless
  // told to, so tell it for the panel.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail.xhr;
    var target = event.detail.target;
    if (xhr && xhr.status >= 400 && xhr.status < 600 && target && target.id === "notebook-body") {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  function problem(text) {
    var notice = document.createElement("p");
    notice.className = "notice notice--error";
    notice.setAttribute("role", "alert");
    notice.textContent = text;
    var area = body.querySelector(".sidepanel__body") || body;
    area.insertBefore(notice, area.firstChild);
  }

  body.addEventListener("htmx:sendError", function () {
    problem("Couldn't reach the server. Nothing was lost on this page; check your connection and try again.");
  });
  body.addEventListener("htmx:timeout", function () {
    problem("That took too long. Check your connection and try again.");
  });

  // An entry published inside the panel: remember it, so closing refreshes the page behind.
  body.addEventListener("htmx:afterSwap", function () {
    if (body.querySelector("[data-posted]")) published = true;
    var title = document.getElementById("notebook-title");
    if (title) title.focus();
  });

  dialog.addEventListener("close", function () {
    if (!published) return;
    published = false;
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

// The leaving nudge: "You worked on M06 today. Write today's entry while you remember it."
// The server renders #notebook-nudge, hidden, only when there is something to write up (and
// never for signed-out visitors). It is revealed by exit intent only, with no timers and no
// polling:
//   - desktop: the pointer leaves the top of the window with no button pressed;
//   - any device: the tab becomes hidden, and the prompt waits for the next return to the tab.
// At most once per local day per member (the localStorage key carries both), never while the
// Notebook is open, and never again after "Not today" or after the Notebook was opened.
// It doesn't take focus. Esc closes it. Without JavaScript it simply stays hidden.
(function () {
  var nudge = document.getElementById("notebook-nudge");
  if (!nudge) return;
  var key = nudge.getAttribute("data-key") || "cc-nudge";
  var dialog = document.getElementById("notebook");
  var finished = false; // shown, dismissed or the Notebook was opened: nothing more today
  var leftWhileHidden = false; // the tab was hidden: show it when the member comes back

  function stored() {
    try {
      return window.localStorage.getItem(key) === "1";
    } catch (err) {
      return false;
    }
  }
  function remember() {
    finished = true;
    try {
      window.localStorage.setItem(key, "1");
    } catch (err) {
      // Private mode: it is still once for this page load.
    }
  }
  function notebookOpen() {
    return !!(dialog && dialog.open);
  }
  function hide() {
    nudge.hidden = true;
  }
  function reveal() {
    if (finished || stored() || notebookOpen()) return;
    remember(); // shown now: not again today, even if they ignore it
    nudge.hidden = false;
  }

  // After "Mark step done" the Notebook opens by itself (?log=CODE): they are writing already.
  if (stored() || (dialog && dialog.hasAttribute("data-open-url"))) {
    remember();
    return;
  }

  // Desktop: the pointer leaves through the top of the window (toward the tabs, address bar or
  // close button), with no mouse button held (so not mid-drag or mid-selection).
  document.addEventListener("mouseleave", function (event) {
    if (event.clientY <= 0 && event.buttons === 0) reveal();
  });

  // Any device: hidden tab now, shown on the next return.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      leftWhileHidden = !finished && !stored();
    } else if (leftWhileHidden) {
      leftWhileHidden = false;
      reveal();
    }
  });

  // Opening the Notebook (from the nudge or anywhere) ends it for the day.
  document.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest("[data-notebook-open]")) {
      remember();
      hide();
    }
  });

  // Esc closes it (the Notebook, when open, handles its own Esc; the nudge is never shown then).
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !nudge.hidden) hide();
  });

  // "Not today": the form still posts to /notebook/dismiss (the server hides it until
  // tomorrow). Here it is sent in the background so the page isn't reloaded under them; if that
  // fails, the form is submitted the ordinary way.
  var form = nudge.querySelector("form");
  if (form && window.fetch) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      remember();
      hide();
      window
        .fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          redirect: "manual", // the 303 back to the page isn't wanted
          body: new URLSearchParams(new FormData(form)),
        })
        .then(function (response) {
          if (!response.ok && response.type !== "opaqueredirect") throw new Error("dismiss " + response.status);
        })
        .catch(function () {
          form.submit();
        });
    });
  }
})();
