// Timeline and /profile (docs/timeline.md). No build step, no dependencies.
//
// Timeline: on a phone the chart scrolls sideways inside its own box, so open it with
// today in view (the label column stays pinned at the left). On a wide screen the whole
// chart fits and nothing moves.
// /profile: the preview follows the icon and name as they are picked or typed.
(function () {
  var chart = document.getElementById("timeline");
  if (chart && window.matchMedia("(max-width: 820px)").matches) {
    var track = chart.querySelector(".tl-track--axis");
    var label = chart.querySelector(".tl-axis .tl-label");
    var today = parseFloat(chart.dataset.today);
    if (track && label && !isNaN(today)) {
      var x = track.offsetLeft + (track.offsetWidth * today) / 100;
      // Centre today in the part of the box the pinned labels don't cover.
      chart.scrollLeft = x - label.offsetWidth - (chart.clientWidth - label.offsetWidth) / 2;
    }
  }

  var form = document.querySelector("[data-profile-form]");
  if (form) {
    var use = form.querySelector("[data-preview-chip] use");
    var shown = form.querySelector("[data-preview-name]");
    var input = form.querySelector("input[name=display_name]");
    var update = function () {
      var picked = form.querySelector("input[name=icon]:checked");
      if (picked && use) use.setAttribute("href", "#av-" + picked.value);
      if (shown && input) shown.textContent = input.value.trim() || shown.dataset.fallback;
    };
    form.addEventListener("change", update);
    form.addEventListener("input", update);
  }
})();
