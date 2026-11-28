"use strict";
(async () => {
  const status = document.getElementById("status");
  const button = document.getElementById("google-button");
  function failed() {
    button.replaceChildren();
    status.textContent = "Could not sign in. Use an approved Google account and reload to try again.";
  }
  try {
    if (!window.google?.accounts?.id) throw new Error();
    const response = await fetch("/auth/challenge", {method: "POST", mode: "same-origin"});
    if (!response.ok) throw new Error();
    const {nonce} = await response.json();
    google.accounts.id.initialize({
      client_id: button.dataset.client,
      nonce,
      ux_mode: "popup",
      callback: async ({credential}) => {
        try {
          const result = await fetch("/auth/exchange", {
            method: "POST",
            mode: "same-origin",
            body: new URLSearchParams({credential}),
          });
          if (!result.ok) throw new Error();
          location.replace("/");
        } catch {
          failed();
        }
      },
    });
    google.accounts.id.renderButton(button, {theme: "outline", size: "large"});
    status.textContent = "Use an approved household Google account. Reload if the button expires.";
  } catch {
    failed();
  }
})();
