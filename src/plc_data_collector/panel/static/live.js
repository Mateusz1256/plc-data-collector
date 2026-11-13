// Live tags: when the every-second refresh of #live-body fails, say so and
// mark every freshness bar stale; the next successful swap clears both
// (the swapped-in body brings its own bars).
(function () {
  function isLiveBody(event) {
    var elt = event.detail && event.detail.elt;
    return Boolean(elt && elt.id === "live-body");
  }

  function setStale(stale) {
    var alert = document.getElementById("live-stale");
    if (alert) alert.hidden = !stale;
    if (!stale) return;
    var bars = document.querySelectorAll("#live-body .bar");
    for (var i = 0; i < bars.length; i++) bars[i].classList.add("stale");
  }

  function onError(event) {
    if (isLiveBody(event)) setStale(true);
  }

  document.addEventListener("htmx:responseError", onError);
  document.addEventListener("htmx:sendError", onError);
  document.addEventListener("htmx:afterSwap", function (event) {
    if (isLiveBody(event) || document.getElementById("live-body") === event.target) {
      setStale(false);
    }
  });
})();
