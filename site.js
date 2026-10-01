/* CorridorIQ.pro public site — navigation toggle and small niceties only.
 * No data is loaded on public pages. */
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    var nav = document.querySelector(".site-nav");
    var burger = document.querySelector(".site-burger");
    if (nav && burger) {
      burger.addEventListener("click", function () {
        var open = nav.classList.toggle("open");
        burger.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }
    var y = document.getElementById("year");
    if (y) y.textContent = String(new Date().getFullYear());
  });
})();
