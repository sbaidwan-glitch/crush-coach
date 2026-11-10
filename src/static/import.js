// Import review: after htmx swaps in the re-checked rows, keep keyboard focus useful.
// A field keeps focus by its id. If the focused control is gone (for example the
// "Use this code" button after the row was fixed), move focus to the summary so
// a screen reader reads the new counts.
document.body.addEventListener("htmx:afterSettle", (event) => {
  if (event.detail.target.id !== "review") return;
  const active = document.activeElement;
  if (!active || active === document.body) {
    const summary = document.getElementById("review-summary");
    if (summary) summary.focus();
  }
});
