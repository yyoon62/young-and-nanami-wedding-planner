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

// Transactions: filters apply as they change (no submit button). Without JS a noscript button appears.
(() => {
  const form = document.querySelector("form[data-autofilter]");
  if (!form) return;
  let timer = 0;
  const go = () => { window.clearTimeout(timer); form.requestSubmit(); };
  form.addEventListener("change", event => {
    if (event.target.matches("input[type=search], input[name=q]")) return; // q applies on input
    go();
  });
  const find = form.querySelector("input[name=q]");
  if (find) {
    find.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => { sessionStorage.setItem("txFocus", "q"); form.requestSubmit(); }, 350);
    });
    if (sessionStorage.getItem("txFocus") === "q") {
      sessionStorage.removeItem("txFocus");
      find.focus();
      find.setSelectionRange(find.value.length, find.value.length);
    }
  }
})();

// L26 Transactions: a row's category chip opens a menu like the Accounts section chip. The menu is cloned from
// one <template> when first opened (a menu per row would triple the page). Without JS the Edit drawer still works.
(() => {
  const template = document.getElementById("tx-pick-tpl");
  if (!template) return;
  document.addEventListener("toggle", event => {
    const pop = event.target;
    if (!(pop instanceof HTMLDetailsElement) || !pop.matches("details.tx-pick") || !pop.open) return;
    let menu = pop.querySelector(".sec-menu");
    if (!menu) {
      menu = template.content.firstElementChild.cloneNode(true);
      menu.querySelector("input[name=row]").value = pop.dataset.row;
      menu.querySelector(".tx-apply-who")?.replaceChildren(document.createTextNode(pop.dataset.who));
      if (pop.dataset.rule !== "1") menu.querySelector(".tx-apply")?.remove();
      menu.querySelectorAll("button[name=pick]").forEach(b => {
        if (b.value === pop.dataset.cat) b.classList.add("active");
      });
      pop.appendChild(menu);
    }
    // Open downward unless that runs past the window and there is room above. Where both fit, the server's hint
    // (data-up: the last rows) wins, so the page itself never grows a scrollbar under the final rows.
    pop.classList.remove("up");
    const room = menu.getBoundingClientRect(), chip = pop.querySelector("summary").getBoundingClientRect();
    const below = room.bottom <= window.innerHeight, above = chip.top - room.height >= 0;
    pop.classList.toggle("up", below && above ? pop.dataset.up === "1" : !below && above ? true : below ? false
                                                                                           : pop.dataset.up === "1");
    menu.querySelector("button.sec-opt")?.focus({preventScroll: true});
  }, true);
  document.addEventListener("click", event => {      // a click anywhere else closes an open category menu
    document.querySelectorAll("details.tx-pick[open]").forEach(d => { if (!d.contains(event.target)) d.open = false; });
  });
})();
