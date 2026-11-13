// Copy key button: needs the clipboard API, so it stays hidden without it.
(function () {
  var button = document.getElementById("copy-key");
  if (!button || !navigator.clipboard) return;
  button.hidden = false;
  button.addEventListener("click", function () {
    var field = document.getElementById(button.dataset.copyFrom);
    if (!field) return;
    navigator.clipboard.writeText(field.value).then(function () {
      button.textContent = "Copied";
    });
  });
})();
