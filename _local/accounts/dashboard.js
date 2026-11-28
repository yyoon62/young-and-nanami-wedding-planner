"use strict";
(() => {
  // Progressive enhancement: open account edit drawers in place without leaving the dashboard.
  function closeAll(exceptId) {
    document.querySelectorAll("tr.edit-drawer:not([hidden])").forEach(row => {
      if (row.id !== exceptId) row.hidden = true;
    });
    document.querySelectorAll("a.edit-trigger[aria-expanded='true']").forEach(link => {
      if ("edit-row-" + link.dataset.edit !== exceptId) link.setAttribute("aria-expanded", "false");
    });
  }

  document.addEventListener("click", event => {
    const trigger = event.target.closest("a.edit-trigger[data-edit]");
    if (trigger && !event.metaKey && !event.ctrlKey && !event.shiftKey) {
      const drawer = document.getElementById("edit-row-" + trigger.dataset.edit);
      if (drawer) {
        event.preventDefault();
        const opening = drawer.hidden;
        closeAll(opening ? drawer.id : null);
        drawer.hidden = !opening;
        trigger.setAttribute("aria-expanded", opening ? "true" : "false");
        if (opening) {
          const input = drawer.querySelector("input[name='nickname']");
          if (input) input.focus();
        }
      }
      return;
    }
    const cancel = event.target.closest("button.drawer-cancel");
    if (cancel) {
      const drawer = cancel.closest("tr.edit-drawer");
      if (drawer) {
        drawer.hidden = true;
        const link = document.querySelector(`a.edit-trigger[data-edit="${drawer.dataset.id}"]`);
        if (link) {
          link.setAttribute("aria-expanded", "false");
          link.focus();
        }
      }
    }
  });

  // Close other inline <details> popovers when opening a new one so the table stays clean.
  document.addEventListener("toggle", event => {
    const opened = event.target;
    if (opened.matches("details.inline-pop") && opened.open) {
      document.querySelectorAll("details.inline-pop[open]").forEach(other => {
        if (other !== opened) other.open = false;
      });
    }
  }, true);

  document.addEventListener("keydown", event => {
    if (event.key === "Escape") {
      const openDrawer = document.querySelector("tr.edit-drawer:not([hidden])");
      if (openDrawer) {
        openDrawer.hidden = true;
        const link = document.querySelector(`a.edit-trigger[data-edit="${openDrawer.dataset.id}"]`);
        if (link) {
          link.setAttribute("aria-expanded", "false");
          link.focus();
        }
      }
      document.querySelectorAll("details.inline-pop[open]").forEach(d => { d.open = false; });
    }
  });
})();
