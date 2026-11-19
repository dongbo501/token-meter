const form = document.getElementById("login-form");
const password = document.getElementById("password");
const toggle = document.getElementById("show-password");
const submit = document.getElementById("login-submit");
const message = document.getElementById("login-message");
toggle.hidden = false;
toggle.addEventListener("click", () => {
  const show = password.type === "password";
  password.type = show ? "text" : "password";
  toggle.setAttribute("aria-pressed", String(show));
});
form.addEventListener("input", () => {
  password.removeAttribute("aria-invalid");
  message.textContent = "";
});
if (new URLSearchParams(location.search).has("expired")) {
  message.textContent = "登录已过期，请重新登录。";
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submit.disabled) return;
  submit.disabled = true;
  submit.firstElementChild.textContent = "正在登录…";
  message.textContent = "";
  try {
    const response = await fetch(form.action, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(form))),
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        password.setAttribute("aria-invalid", "true");
        password.focus();
      }
      throw new Error(result.error || "登录失败，请重试");
    }
    password.value = "";
    location.replace("/" + location.hash);
  } catch (error) {
    message.textContent =
      error instanceof TypeError
        ? "连接失败，请检查网络后重试。"
        : error.message;
    submit.disabled = false;
    submit.firstElementChild.textContent = "登录控制台";
  }
});
