// Форматирование телефона и показ пароля на страницах входа/регистрации.
document.addEventListener("DOMContentLoaded", function () {
  // --- Маска телефона +7 (999) 999-99-99 ---
  var phone = document.getElementById("phone");
  if (phone) {
    function format(value) {
      var d = value.replace(/\D/g, "");
      if (d.startsWith("8")) d = "7" + d.slice(1);
      if (!d.startsWith("7")) d = "7" + d;
      d = d.slice(0, 11);

      var out = "+7";
      if (d.length > 1) out += " (" + d.substring(1, 4);
      if (d.length >= 5) out += ") " + d.substring(4, 7);
      if (d.length >= 8) out += "-" + d.substring(7, 9);
      if (d.length >= 10) out += "-" + d.substring(9, 11);
      return out;
    }

    phone.addEventListener("input", function (e) {
      e.target.value = format(e.target.value);
    });

    phone.addEventListener("focus", function (e) {
      if (!e.target.value) e.target.value = "+7 (";
    });
  }

  // --- Показ/скрытие пароля ---
  document.querySelectorAll(".peek-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.dataset.peek);
      if (!input) return;
      var hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      btn.classList.toggle("is-on", hidden);
      btn.setAttribute("aria-label", hidden ? "Скрыть пароль" : "Показать пароль");
      input.focus();
    });
  });
});
