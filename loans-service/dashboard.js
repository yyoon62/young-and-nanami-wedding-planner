async function openDashboard({credential}) {
  const status = document.getElementById("status");
  try {
    const response = await fetch("/api/shared-loans", {
      headers: {Authorization: `Bearer ${credential}`},
      cache: "no-store"
    });
    if (!response.ok) {
      status.textContent = response.status === 403 ? "This account does not have access."
        : response.status === 401 ? "Please sign in again." : "Dashboard unavailable. Try again later.";
      return;
    }
    const {loans} = await response.json();
    status.textContent = loans.length ? `${loans.length} shared loans.` : "No loans have been shared.";
    document.getElementById("signin").hidden = true;
    document.getElementById("signout").hidden = false;
  } catch {
    status.textContent = "Could not reach the dashboard. Please try again.";
  }
}

document.getElementById("signout").onclick = () => {
  google.accounts.id.disableAutoSelect();
  location.reload();
};
