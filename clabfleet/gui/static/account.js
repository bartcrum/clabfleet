"use strict";

// ---------------------------------------------------------------------------
// Login and password forms, and the Users dialog (operators, named-user
// mode).
//
// Uses app.js globals: $, h, api, toast, confirmDialog.
// ---------------------------------------------------------------------------

// ``creds`` is {username, password} or {token}. Login links carry the
// token in the URL fragment (#token=...), which the browser does not send
// to the server. It is posted to /login instead and dropped from the
// address bar.
async function postLogin(creds, switchUser = false) {
  const res = await fetch("/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...creds, switch: switchUser }),
  });
  if (res.status === 409) {
    // Logged in as someone else: only switch if the user says so
    const { user } = await res.json();
    if (await confirmDialog({
      title: "Switch user?",
      body: `You are logged in as ${user}. ` +
        `Log in as ${creds.username || "the user of this link"} instead?`,
      ok: "Switch user",
    })) {
      await postLogin(creds, true);
    }
    return;
  }
  if (!res.ok) throw new Error((await res.text()) || res.statusText);
}

// The login form asks for a name and password when the GUI has named users
// ("password"), else for the start-up token ("token"); named users can
// switch to a token too
const login = { mode: "token", useToken: false };

function renderLoginForm() {
  const token = login.mode === "token" || login.useToken;
  $("#login-fields").hidden = token;
  $("#login-token").hidden = !token;
  $("#login-user").required = $("#login-pass").required = !token;
  $("#login-token").required = token;
  $("#login-hint").textContent = token
    ? "Open the GUI with your login link, or paste your token."
    : "Log in with your user name and password.";
  const sw = $("#login-switch");
  sw.hidden = login.mode !== "password";
  sw.textContent = login.useToken ? "Use a name and password" : "Use a login token instead";
}

function showLogin(message, mode) {
  if (mode) login.mode = mode;
  renderLoginForm();
  $("#login").hidden = false;
  const err = $("#login-error");
  err.textContent = message || "";
  err.hidden = !message;
  (login.mode === "token" || login.useToken ? $("#login-token")
    : $("#login-user").value ? $("#login-pass") : $("#login-user")).focus();
}

// Change your own password. ``forced``: the first login, which may do
// nothing else (the server refuses everything but this until it is done).
function showPasswordForm(forced) {
  $("#pwchange").hidden = false;
  $("#pw-title").textContent = forced ? "Set a new password" : "Change your password";
  $("#pw-hint").textContent = forced
    ? "You logged in with a temporary password. Choose your own to continue."
    : "Your other browsers are logged out when the password changes.";
  $("#pw-cancel").hidden = forced;
  $("#pw-form").dataset.forced = forced ? "1" : "";
  for (const id of ["#pw-current", "#pw-new", "#pw-again"]) $(id).value = "";
  $("#pw-error").hidden = true;
  $("#pw-current").focus();
}

async function submitPassword(ev) {
  ev.preventDefault();
  const err = $("#pw-error");
  const fail = (msg) => { err.textContent = msg; err.hidden = false; };
  if ($("#pw-new").value !== $("#pw-again").value) return fail("The new passwords do not match.");
  const res = await fetch("/api/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current: $("#pw-current").value, new: $("#pw-new").value }),
  });
  if (!res.ok) return fail((await res.text()) || res.statusText);
  if ($("#pw-form").dataset.forced) { location.replace("/"); return; }
  $("#pwchange").hidden = true;
  toast("Password changed");
}

// ---------------------------------------------------------------------------
// Users (operators, named-user mode)
// ---------------------------------------------------------------------------

async function openUsers() {
  $("#users-secret").hidden = true;
  $("#users-error").hidden = true;
  const dlg = $("#users-dlg");
  if (!dlg.open) dlg.showModal();
  await loadUsers();
  $("#users-name").focus();
}

async function loadUsers() {
  try {
    const { users, you } = await api("/api/users");
    renderUsers(users, you);
  } catch (e) {
    usersError(e.message);
  }
}

function usersError(msg) {
  const el = $("#users-error");
  el.textContent = msg;
  el.hidden = !msg;
}

const LOGIN_TEXT = (u) => [u.password ? (u.must_change ? "password (to be set)" : "password") : null,
  u.token ? "token" : null].filter(Boolean).join(" + ");

function renderUsers(users, you) {
  $("#users-rows").replaceChildren(...users.map((u) => {
    const me = u.name === you;
    return h("tr", {},
      h("td", {}, h("strong", {}, u.name), me ? h("span", { class: "you" }, " (you)") : null),
      h("td", {}, h("select", {
        "aria-label": `Role of ${u.name}`, disabled: me,
        title: me ? "You cannot change your own role" : "",
        onchange: (ev) => userCall("PATCH", u.name, "", { role: ev.target.value }),
      }, ["operator", "viewer"].map((r) => h("option", { value: r, selected: u.role === r }, r)))),
      h("td", { class: "muted" }, LOGIN_TEXT(u)),
      h("td", {}, me ? null : h("span", { class: "acts" },
        h("button", {
          class: "btn small", type: "button",
          title: u.password ? "Give a new temporary password; their sessions end" : "Give a new login token; their sessions end",
          onclick: () => resetUser(u),
        }, u.password ? "Reset password" : "New token"),
        h("button", {
          class: "btn small danger", type: "button",
          onclick: () => removeUser(u),
        }, "Remove"))));
  }));
}

async function userCall(method, name, suffix, body) {
  usersError("");
  try {
    const res = await api(`/api/users/${encodeURIComponent(name)}${suffix}`, {
      method, body: body ? JSON.stringify(body) : undefined,
    });
    await loadUsers();
    return res;
  } catch (e) {
    usersError(e.message);
    await loadUsers();
    return null;
  }
}

// The new password or token, shown once, to hand to the user
function showSecret(name, res) {
  const box = $("#users-secret");
  const value = res.password || res.link;
  if (!value) { box.hidden = true; return; }
  box.replaceChildren(
    h("span", {}, res.password
      ? `Temporary password for ${name}. Give it to them; they choose their own at the first login. It is not shown again.`
      : `Login link for ${name}. Give it to them; it is not shown again.`),
    h("span", { class: "val" }, h("code", {}, value),
      h("button", {
        class: "btn small", type: "button",
        onclick: async (ev) => {
          try { await navigator.clipboard.writeText(value); ev.target.textContent = "Copied"; } catch { /* select it by hand */ }
        },
      }, "Copy")));
  box.hidden = false;
}

async function resetUser(u) {
  const what = u.password ? "a new temporary password" : "a new login token";
  if (!(await confirmDialog({
    title: `Reset ${u.name}'s login?`,
    body: [`${u.name} gets ${what}; their current one stops working and their sessions end.`],
    ok: u.password ? "Reset password" : "New token", danger: true,
  }))) return;
  const res = await userCall("POST", u.name, "/reset", { login: u.password ? "password" : "token" });
  if (res) showSecret(u.name, res);
}

async function removeUser(u) {
  if (!(await confirmDialog({
    title: `Remove ${u.name}?`,
    body: [`${u.name} can no longer log in, and their open sessions and terminals end.`],
    ok: "Remove user", danger: true,
  }))) return;
  $("#users-secret").hidden = true;
  await userCall("DELETE", u.name, "");
}

async function addUser(ev) {
  ev.preventDefault();
  usersError("");
  const name = $("#users-name").value.trim();
  try {
    const res = await api("/api/users", {
      method: "POST",
      body: JSON.stringify({ name, role: $("#users-role").value, login: $("#users-login").value }),
    });
    $("#users-name").value = "";
    showSecret(name, res);
    await loadUsers();
  } catch (e) {
    usersError(e.message);
  }
}

async function logout() {
  try {
    await api("/logout", { method: "POST" });
  } catch (e) { /* the session is gone either way */ }
  location.href = "/";
}
