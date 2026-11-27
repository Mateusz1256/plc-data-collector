// Connection form with a Transport select (M-Bus): show only the fields of the
// chosen transport. Without scripts every field shows; the server ignores the
// fields of the transport that is not chosen.
(function () {
  var select = document.querySelector('select[name="transport"]');
  if (!select) return;
  function update() {
    var boxes = select.form.querySelectorAll("[data-transport]");
    boxes.forEach(function (box) {
      box.hidden = box.dataset.transport !== select.value;
    });
  }
  select.addEventListener("change", update);
  update();
})();
