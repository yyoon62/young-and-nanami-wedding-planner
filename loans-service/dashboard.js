const byId = id => document.getElementById(id);
const form = byId("entry");
let editing, submission;

function resetEntry() {
  form.reset();
  editing = null;
  submission = crypto.randomUUID();
  byId("heading").textContent = "Add a private account";
  byId("cancel").hidden = true;
}

function signedOut() {
  byId("records").replaceChildren();
  byId("panel").hidden = byId("signout").hidden = true;
  byId("signin").hidden = false;
  resetEntry();
}

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method, credentials: "same-origin", cache: "no-store",
    headers: method === "GET" ? {} : {"X-Dashboard-Action": "1", "Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) {
    if (response.status === 401) signedOut();
    if (response.status === 409) resetEntry();
    throw new Error(({401: "Please sign in.", 403: "Access denied.", 404: "Account unavailable. Refresh the list.",
      409: "Already saved. Refresh the list before adding another account.",
      422: "Check the name, amount (up to two decimals), currency and observed date."})[response.status]
      || "Request failed. Refresh to check the saved state.");
  }
  return response.status === 204 ? null : response.json();
}

// Serialize UI actions so a late refresh cannot repaint records after logout.
let queue = Promise.resolve();
function run(action) {
  queue = queue.then(action).catch(error => { byId("status").textContent = error.message; });
  return queue;
}

function control(li, label, action) {
  const button = document.createElement("button");
  button.textContent = label;
  button.onclick = () => run(action);
  li.append(" ", button);
}

async function refresh() {
  const {accounts, member, other} = await api("/api/accounts");
  byId("signin").hidden = true;
  byId("panel").hidden = byId("signout").hidden = false;
  byId("status").textContent = `${member} · ${accounts.length} visible account(s)`;
  const list = byId("records");
  list.replaceChildren();
  for (const row of accounts) {
    const li = document.createElement("li");
    const label = !row.mine ? "Shared with you · read-only" : row.shared ? `Shared with ${other}` : "Private";
    li.textContent = `${row.name} — ${row.currency} ${row.amount} as of ${row.as_of} · ${label}`;
    if (row.mine) {
      const path = `/api/accounts/${row.id}`;
      control(li, row.shared ? "Unshare" : `Share read-only with ${other}`, async () => {
        await api(`${path}/share`, row.shared ? "DELETE" : "PUT");
        await refresh();
      });
      control(li, "Edit", () => {
        editing = path;
        for (const key of ["name", "amount", "currency", "as_of"]) form.elements[key].value = row[key];
        byId("heading").textContent = "Edit manual record";
        byId("cancel").hidden = false;
        form.elements.name.focus();
      });
      control(li, "Delete", async () => {
        if (!window.confirm("Delete this manual record?")) return;
        await api(path, "DELETE");
        if (editing === path) resetEntry();
        await refresh();
      });
    }
    list.append(li);
  }
}

function openDashboard({credential}) {
  return run(async () => { await api("/api/login", "POST", {credential}); await refresh(); });
}
byId("signout").onclick = () => run(async () => {
  await api("/api/logout", "POST");
  signedOut();
  byId("status").textContent = "Signed out.";
  window.google?.accounts.id.disableAutoSelect();
});
byId("refresh").onclick = () => run(refresh);
byId("cancel").onclick = resetEntry;
form.onsubmit = event => {
  event.preventDefault();
  const path = editing, body = Object.fromEntries(new FormData(form));
  if (!path) body.create_key = submission;
  run(async () => {
    await api(path || "/api/accounts", path ? "PUT" : "POST", body);
    resetEntry();
    await refresh();
  });
};
window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
resetEntry();
run(refresh);
