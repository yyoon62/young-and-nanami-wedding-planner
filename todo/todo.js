/* Org-mode TODO Board UI controller.
 * Connects to yyoon62/notes via TodoSync (or runs an in-memory demo board).
 */
(function () {
  "use strict";

  var Org = window.OrgTodo;
  var Sync = window.TodoSync;

  var DEMO_AGENDA = [
    "#+TITLE: Agenda: Now / Waitlist",
    "#+STARTUP: overview",
    "",
    "Demo priorities (fictional sample tasks).",
    "",
    "* Now (NEXT)",
    "** NEXT Pick weekend trail for Saturday hike",
    "   Compare drive time and trailhead parking. See [[file:trail_notes.org][trail_notes.org]].",
    "",
    "* Waitlist",
    "** WAITING Order replacement coffee grinder burr",
    "   Waiting on part number confirmation from support.",
    "** WAITING Schedule annual bike tune-up",
    "   Blocked on checking shop hours for next month.",
    "",
    "* Done",
    "** DONE Renew library card",
    "   CLOSED: [2026-11-24]",
    "   Renewed online.",
    "** CANCELLED Print paper train schedule",
    "   Not needed: transit app works offline.",
    "",
    "* Someday / ideas",
    ""
  ].join("\n");

  var DEMO_INBOX = [
    "#+TITLE: Inbox",
    "#+STARTUP: overview",
    "",
    "Quick capture lands here.",
    "",
    "* Inbox",
    "** TODO Replace kitchen water filter",
    "   captured [2026-11-25 Wed 15:19]",
    "** TODO Book museum timed-entry tickets",
    "   SCHEDULED: <2026-12-18 Fri>",
    "   Check morning slots one week before.",
    ""
  ].join("\n");

  var state = {
    cfg: Sync.loadConfig(),
    demo: false,
    agendaDoc: null,
    inboxDoc: null,
    headSha: null,
    agendaSha: null,
    inboxSha: null,
    lastSyncedAt: null,
    busy: false,
    showAllDone: false,
    mobileCol: "now",
    dragItemId: null,
    pendingWait: null,
    pendingSwap: null,
    editingItemId: null
  };

  var $ = function (id) { return document.getElementById(id); };
  var toastTimer = null;

  function setSyncBadge(kind, text) {
    var badge = $("syncBadge");
    badge.className = "sync" + (kind ? " " + kind : "");
    $("syncText").textContent = text;
  }

  function showToast(msg, isError) {
    var el = $("toast");
    el.textContent = msg;
    el.className = "toast" + (isError ? " bad" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.classList.add("hide");
    }, isError ? 4200 : 2400);
  }

  function formatRelativeTime(date) {
    if (!date) return "";
    var diff = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (diff < 15) return "just now";
    if (diff < 60) return diff + "s ago";
    var mins = Math.floor(diff / 60);
    if (mins < 60) return mins + "m ago";
    return Math.floor(mins / 60) + "h ago";
  }

  function openModal(id) {
    $(id).classList.remove("hide");
  }
  function closeModal(id) {
    $(id).classList.add("hide");
  }

  function showConnectView(errMsg) {
    $("connectView").classList.remove("hide");
    $("boardView").classList.add("hide");
    $("connectOwner").value = state.cfg.owner || "yyoon62";
    $("connectRepo").value = state.cfg.repo || "notes";
    $("connectBranch").value = state.cfg.branch || "main";
    $("connectToken").value = state.cfg.token || "";
    var err = $("connectError");
    if (errMsg) {
      err.textContent = errMsg;
      err.classList.remove("hide");
    } else {
      err.classList.add("hide");
    }
    setSyncBadge("", "Not connected");
  }

  function showBoardView() {
    $("connectView").classList.add("hide");
    $("boardView").classList.remove("hide");
    $("demoBanner").classList.toggle("hide", !state.demo);
  }

  function loadDemo() {
    state.demo = true;
    state.agendaDoc = Org.parseOrgFile(DEMO_AGENDA, "agenda");
    state.inboxDoc = Org.parseOrgFile(DEMO_INBOX, "inbox");
    state.lastSyncedAt = new Date();
    showBoardView();
    setSyncBadge("ok", "Demo mode");
    render();
  }

  function fetchRemote(silent) {
    if (state.demo) {
      render();
      return Promise.resolve();
    }
    if (!state.cfg.token) {
      showConnectView();
      return Promise.resolve();
    }
    state.busy = true;
    $("refreshBtn").disabled = true;
    setSyncBadge("", "Syncing…");

    return Sync.fetchOrgFiles(state.cfg).then(function (data) {
      state.headSha = data.headSha;
      state.agendaSha = data.agenda.sha;
      state.inboxSha = data.inbox.sha;
      state.agendaDoc = Org.parseOrgFile(data.agenda.text, "agenda");
      state.inboxDoc = Org.parseOrgFile(data.inbox.text, "inbox");
      state.lastSyncedAt = data.fetchedAt;
      state.busy = false;
      $("refreshBtn").disabled = false;
      showBoardView();
      setSyncBadge("ok", "Synced · " + state.cfg.owner + "/" + state.cfg.repo);
      render();
      if (!silent) showToast("Synced with " + state.cfg.repo);
    }).catch(function (err) {
      state.busy = false;
      $("refreshBtn").disabled = false;
      setSyncBadge("bad", "Sync error");
      if (!state.agendaDoc) {
        showConnectView(err.message);
      } else {
        showToast(err.message, true);
      }
      throw err;
    });
  }

  /* Execute a mutation against state.agendaDoc / state.inboxDoc and commit it.
   * On GitHub CONFLICT, automatically reloads fresh files once and replays the
   * mutation by item fingerprint so concurrent Emacs/agent edits are never lost.
   */
  function runMutation(mutator, toastSuccess) {
    if (state.busy) return Promise.resolve();

    if (state.demo) {
      var demoRes = mutator(state);
      if (demoRes && demoRes.ok) {
        render();
        if (toastSuccess) showToast(toastSuccess + " (demo)");
      }
      return Promise.resolve(demoRes);
    }

    // Snapshot previous text so we can roll back cleanly if non-conflict save fails
    var prevAgendaText = Org.serializeOrgFile(state.agendaDoc);
    var prevInboxText = Org.serializeOrgFile(state.inboxDoc);

    function attempt(isRetry) {
      var res = mutator(state);
      if (!res || !res.ok) return Promise.resolve(res);

      var files = [];
      if (res.changedFiles.indexOf("agenda") !== -1) {
        files.push({
          path: "agenda.org",
          text: Org.serializeOrgFile(state.agendaDoc),
          sha: state.agendaSha
        });
      }
      if (res.changedFiles.indexOf("inbox") !== -1) {
        files.push({
          path: "inbox.org",
          text: Org.serializeOrgFile(state.inboxDoc),
          sha: state.inboxSha
        });
      }

      state.busy = true;
      setSyncBadge("", "Saving…");
      render();

      return Sync.commitChanges(state.cfg, files, state.headSha, res.commitMessage).then(function (commit) {
        if (commit.headSha) state.headSha = commit.headSha;
        if (commit.fileShas["agenda.org"]) state.agendaSha = commit.fileShas["agenda.org"];
        if (commit.fileShas["inbox.org"]) state.inboxSha = commit.fileShas["inbox.org"];
        state.lastSyncedAt = new Date();
        state.busy = false;
        setSyncBadge("ok", "Synced · just now");
        render();
        if (toastSuccess) showToast(toastSuccess);
        return res;
      }).catch(function (err) {
        if (err.code === "CONFLICT" && !isRetry) {
          showToast("Remote changed — reloading & retrying…");
          return Sync.fetchOrgFiles(state.cfg).then(function (fresh) {
            state.headSha = fresh.headSha;
            state.agendaSha = fresh.agenda.sha;
            state.inboxSha = fresh.inbox.sha;
            state.agendaDoc = Org.parseOrgFile(fresh.agenda.text, "agenda");
            state.inboxDoc = Org.parseOrgFile(fresh.inbox.text, "inbox");
            state.busy = false;
            return attempt(true);
          });
        }
        // Roll back local state on error
        state.agendaDoc = Org.parseOrgFile(prevAgendaText, "agenda");
        state.inboxDoc = Org.parseOrgFile(prevInboxText, "inbox");
        state.busy = false;
        setSyncBadge("bad", "Save failed");
        render();
        showToast(err.message || "Could not save change", true);
        return { ok: false, error: err.code || "SAVE_FAILED" };
      });
    }

    return attempt(false);
  }

  function isOverdue(isoDate) {
    if (!isoDate) return false;
    var today = Org.formatOrgDate(new Date());
    return isoDate <= today;
  }

  function renderNoteNode(noteLines) {
    var wrap = document.createElement("div");
    wrap.className = "card-note";
    noteLines.forEach(function (line) {
      if (!line) return;
      var p = document.createElement("p");
      var segs = Org.parseOrgLinks(line);
      segs.forEach(function (seg) {
        if (seg.type === "url") {
          var a = document.createElement("a");
          a.href = seg.target;
          a.target = "_blank";
          a.rel = "noopener noreferrer";
          a.textContent = seg.text;
          p.appendChild(a);
        } else if (seg.type === "file") {
          var chip = document.createElement("span");
          chip.className = "pill file-chip";
          chip.textContent = "📄 " + seg.text;
          p.appendChild(chip);
        } else {
          p.appendChild(document.createTextNode(seg.text));
        }
      });
      wrap.appendChild(p);
    });
    return wrap;
  }

  function buildCard(item, colKey) {
    var card = document.createElement("article");
    card.className = "card" +
      (item.keyword === "DONE" ? " is-done" : "") +
      (item.keyword === "CANCELLED" ? " is-cancelled" : "");
    card.dataset.itemId = item.id;
    card.dataset.col = colKey;
    card.draggable = true;

    var top = document.createElement("div");
    top.className = "card-top";

    var handle = document.createElement("span");
    handle.className = "drag-handle";
    handle.textContent = "⋮⋮";
    handle.title = "Drag to move";
    handle.setAttribute("aria-label", "Drag to move");
    top.appendChild(handle);

    var body = document.createElement("div");
    body.className = "card-body";

    var title = document.createElement("div");
    title.className = "card-title";
    title.textContent = item.title;
    title.addEventListener("click", function () { openEditModal(item.id); });
    body.appendChild(title);

    // Metadata pills
    var meta = document.createElement("div");
    meta.className = "card-meta";
    if (item.keyword === "CANCELLED") {
      var cPill = document.createElement("span");
      cPill.className = "kw-pill kw-cancelled";
      cPill.textContent = "CANCELLED";
      meta.appendChild(cPill);
    }
    if (item.scheduled) {
      var sPill = document.createElement("span");
      sPill.className = "pill sched" + (isOverdue(item.scheduled) && colKey !== "done" ? " overdue" : "");
      sPill.textContent = "📅 " + item.scheduled;
      meta.appendChild(sPill);
    }
    if (item.closed) {
      var dPill = document.createElement("span");
      dPill.className = "pill closed";
      dPill.textContent = "✓ " + item.closed;
      meta.appendChild(dPill);
    }
    if (item.tags) {
      var tPill = document.createElement("span");
      tPill.className = "pill";
      tPill.textContent = item.tags.trim();
      meta.appendChild(tPill);
    }
    if (meta.childNodes.length > 0) {
      body.appendChild(meta);
    }

    if (item.noteLines && item.noteLines.length > 0) {
      body.appendChild(renderNoteNode(item.noteLines));
    }

    top.appendChild(body);
    card.appendChild(top);

    // Quick one-tap action buttons (great on phone!)
    var actions = document.createElement("div");
    actions.className = "card-actions";

    if (colKey !== "done") {
      var doneBtn = document.createElement("button");
      doneBtn.type = "button";
      doneBtn.className = "cbtn done-btn";
      doneBtn.textContent = "✓ Done";
      doneBtn.addEventListener("click", function () { handleMoveRequest(item.id, "done"); });
      actions.appendChild(doneBtn);
    }

    if (colKey !== "now") {
      var nextBtn = document.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "cbtn next-btn";
      nextBtn.textContent = "→ Now";
      nextBtn.addEventListener("click", function () { handleMoveRequest(item.id, "now"); });
      actions.appendChild(nextBtn);
    }

    if (colKey !== "waitlist" && colKey !== "done") {
      var waitBtn = document.createElement("button");
      waitBtn.type = "button";
      waitBtn.className = "cbtn wait-btn";
      waitBtn.textContent = "⏸ Wait";
      waitBtn.addEventListener("click", function () { handleMoveRequest(item.id, "waitlist"); });
      actions.appendChild(waitBtn);
    }

    if (colKey === "done") {
      var reopenBtn = document.createElement("button");
      reopenBtn.type = "button";
      reopenBtn.className = "cbtn";
      reopenBtn.textContent = "↩ Inbox";
      reopenBtn.addEventListener("click", function () { handleMoveRequest(item.id, "inbox"); });
      actions.appendChild(reopenBtn);
    }

    var editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "cbtn subtle";
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", function () { openEditModal(item.id); });
    actions.appendChild(editBtn);

    card.appendChild(actions);

    // HTML5 drag events (desktop)
    card.addEventListener("dragstart", function (e) {
      state.dragItemId = item.id;
      card.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", item.id);
      }
      showDragDock(true);
    });
    card.addEventListener("dragend", function () {
      state.dragItemId = null;
      card.classList.remove("dragging");
      clearDropHighlights();
      showDragDock(false);
    });

    // Touch / pointer drag on the handle (mobile-friendly without blocking scroll)
    attachTouchDrag(handle, card, item.id);

    return card;
  }

  function showDragDock(on) {
    var dock = $("dragDock");
    dock.classList.toggle("hide", !on);
    dock.setAttribute("aria-hidden", on ? "false" : "true");
  }

  function clearDropHighlights() {
    document.querySelectorAll(".drag-over").forEach(function (el) {
      el.classList.remove("drag-over");
    });
  }

  function attachTouchDrag(handle, card, itemId) {
    handle.addEventListener("pointerdown", function (e) {
      if (e.pointerType === "mouse") return; // desktop uses native HTML5 DnD
      e.preventDefault();
      state.dragItemId = itemId;
      card.classList.add("dragging");
      showDragDock(true);

      var rect = card.getBoundingClientRect();
      var ghost = card.cloneNode(true);
      ghost.classList.remove("dragging");
      ghost.classList.add("drag-ghost");
      ghost.style.left = rect.left + "px";
      ghost.style.top = rect.top + "px";
      document.body.appendChild(ghost);

      var offsetX = e.clientX - rect.left;
      var offsetY = e.clientY - rect.top;
      var activeDrop = null;

      function move(ev) {
        ghost.style.left = (ev.clientX - offsetX) + "px";
        ghost.style.top = (ev.clientY - offsetY) + "px";
        clearDropHighlights();
        var el = document.elementFromPoint(ev.clientX, ev.clientY);
        var dropTarget = el ? el.closest("[data-drop-col]") : null;
        if (dropTarget) {
          activeDrop = dropTarget.dataset.dropCol;
          var box = dropTarget.closest(".col") || dropTarget;
          box.classList.add("drag-over");
        } else {
          activeDrop = null;
        }
      }

      function end() {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", end);
        window.removeEventListener("pointercancel", end);
        ghost.remove();
        card.classList.remove("dragging");
        clearDropHighlights();
        showDragDock(false);
        var targetCol = activeDrop;
        state.dragItemId = null;
        if (targetCol) {
          handleMoveRequest(itemId, targetCol);
        }
      }

      window.addEventListener("pointermove", move, { passive: true });
      window.addEventListener("pointerup", end);
      window.addEventListener("pointercancel", end);
    });
  }

  function setupDropZones() {
    var zones = document.querySelectorAll("[data-drop-col]");
    zones.forEach(function (zone) {
      var highlightEl = zone.closest(".col") || zone;
      zone.addEventListener("dragover", function (e) {
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
        highlightEl.classList.add("drag-over");
      });
      zone.addEventListener("dragleave", function (e) {
        if (!highlightEl.contains(e.relatedTarget)) {
          highlightEl.classList.remove("drag-over");
        }
      });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        clearDropHighlights();
        showDragDock(false);
        var itemId = state.dragItemId || (e.dataTransfer && e.dataTransfer.getData("text/plain"));
        var targetCol = zone.dataset.dropCol;
        if (itemId && targetCol) {
          handleMoveRequest(itemId, targetCol);
        }
      });
    });
  }

  function render() {
    if (!state.agendaDoc || !state.inboxDoc) return;
    var summary = Org.getBoardSummary(state);

    var counts = {
      now: summary.now.length + " / " + Org.MAX_NEXT,
      inbox: String(summary.inbox.length),
      waitlist: String(summary.waitlist.length),
      done: String(summary.done.length)
    };

    $("count-now").textContent = counts.now;
    $("count-now").classList.toggle("full", summary.now.length >= Org.MAX_NEXT);
    $("count-inbox").textContent = counts.inbox;
    $("count-waitlist").textContent = counts.waitlist;
    $("count-done").textContent = counts.done;

    document.querySelectorAll("[data-count]").forEach(function (el) {
      el.textContent = counts[el.dataset.count] || "0";
    });

    renderColumn("now", summary.now, "No active NEXT tasks. Promote one from Inbox or Waitlist.");
    renderColumn("inbox", summary.inbox, "Inbox is empty — add a task above.");
    renderColumn("waitlist", summary.waitlist, "Nothing parked or blocked.");

    var DONE_LIMIT = 8;
    var doneItems = state.showAllDone ? summary.done : summary.done.slice(0, DONE_LIMIT);
    renderColumn("done", doneItems, "Drag completed tasks here.");
    var toggleBtn = $("toggleDoneBtn");
    if (summary.done.length > DONE_LIMIT) {
      toggleBtn.classList.remove("hide");
      toggleBtn.textContent = state.showAllDone
        ? "Show fewer"
        : "Show all " + summary.done.length + " completed";
    } else {
      toggleBtn.classList.add("hide");
    }

    // Apply mobile active column
    var boardEl = $("board");
    boardEl.classList.add("mobile-filtered");
    boardEl.querySelectorAll(".col").forEach(function (col) {
      col.classList.toggle("mobile-active", col.dataset.col === state.mobileCol);
    });
    document.querySelectorAll("#mobileTabs .mtab").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.col === state.mobileCol);
    });
  }

  function renderColumn(colKey, items, emptyText) {
    var list = $("list-" + colKey);
    list.textContent = "";
    if (items.length === 0) {
      var empty = document.createElement("div");
      empty.className = "empty-col";
      empty.textContent = emptyText;
      list.appendChild(empty);
      return;
    }
    items.forEach(function (item) {
      list.appendChild(buildCard(item, colKey));
    });
  }

  /* Move orchestration: enforces Max-3 NEXT and Waitlist reason prompt */
  function handleMoveRequest(itemId, targetCol) {
    var loc = Org.findItemLocation(state, itemId);
    if (!loc) return;
    if (loc.section.kind === targetCol && !(targetCol === "done" && loc.item.keyword !== "DONE")) {
      return;
    }

    // Waitlist requires a one-line reason if none exists yet
    if (targetCol === "waitlist" && (!loc.item.noteLines || loc.item.noteLines.length === 0)) {
      state.pendingWait = { fp: Org.fingerprintItem(loc), targetCol: "waitlist" };
      $("reasonTitle").textContent = "What is it waiting on?";
      $("reasonSubtitle").textContent = loc.item.title;
      $("reasonInput").value = "";
      openModal("reasonModal");
      setTimeout(function () { $("reasonInput").focus(); }, 30);
      return;
    }

    // Check max-3 NEXT before committing
    if (targetCol === "now" && loc.section.kind !== "now") {
      var currentNow = Org.getBoardSummary(state).now;
      if (currentNow.length >= Org.MAX_NEXT) {
        openMaxNextSwapModal(loc, currentNow);
        return;
      }
    }

    commitMove(Org.fingerprintItem(loc), targetCol, {});
  }

  function commitMove(fp, targetCol, opts) {
    var labels = {
      now: "Moved to Now",
      inbox: "Moved to Inbox",
      waitlist: "Moved to Waitlist",
      done: "Marked DONE",
      cancelled: "Marked CANCELLED"
    };
    return runMutation(function (st) {
      var id = Org.findItemByFingerprint(st, fp);
      if (!id) return { ok: false, error: "NOT_FOUND" };
      return Org.moveItem(st, id, targetCol, opts);
    }, labels[targetCol] || "Updated");
  }

  function openMaxNextSwapModal(incomingLoc, currentNow) {
    state.pendingSwap = { incomingFp: Org.fingerprintItem(incomingLoc) };
    $("maxNextSubtitle").textContent =
      "To move \"" + incomingLoc.item.title + "\" into Now, pick one existing NEXT item to park in Waitlist first:";
    var list = $("maxNextChoices");
    list.textContent = "";

    currentNow.forEach(function (existing) {
      var existingLoc = Org.findItemLocation(state, existing.id);
      var existingFp = Org.fingerprintItem(existingLoc);

      var row = document.createElement("div");
      row.className = "swap-item";
      var label = document.createElement("span");
      label.className = "swap-item-title";
      label.textContent = existing.title;
      row.appendChild(label);

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pbtn";
      btn.textContent = "Park in Waitlist";
      btn.addEventListener("click", function () {
        closeModal("maxNextModal");
        runMutation(function (st) {
          var outId = Org.findItemByFingerprint(st, existingFp);
          var inId = Org.findItemByFingerprint(st, state.pendingSwap.incomingFp);
          if (!outId || !inId) return { ok: false, error: "NOT_FOUND" };
          var r1 = Org.moveItem(st, outId, "waitlist", { reason: "Parked to make room in Now." });
          if (!r1.ok) return r1;
          var r2 = Org.moveItem(st, inId, "now");
          if (!r2.ok) return r2;
          var files = r1.changedFiles.concat(r2.changedFiles).filter(function (v, i, a) { return a.indexOf(v) === i; });
          return {
            ok: true,
            changedFiles: files,
            commitMessage: "todo: swap NEXT — " + incomingLoc.item.title.slice(0, 40)
          };
        }, "Swapped into Now");
      });
      row.appendChild(btn);
      list.appendChild(row);
    });

    openModal("maxNextModal");
  }

  function openEditModal(itemId) {
    var loc = Org.findItemLocation(state, itemId);
    if (!loc) return;
    state.editingItemId = itemId;
    $("editTitleInput").value = loc.item.title;
    $("editDateInput").value = loc.item.scheduled || "";
    $("editNoteInput").value = (loc.item.noteLines || []).join("\n");

    var metaParts = [];
    metaParts.push("File: " + (loc.file === "inbox" ? "inbox.org" : "agenda.org"));
    if (loc.item.captured) metaParts.push("Captured: " + loc.item.captured);
    if (loc.item.closed) metaParts.push("Closed: " + loc.item.closed);
    $("editMeta").textContent = metaParts.join(" · ");

    $("cancelTaskBtn").classList.toggle("hide", loc.item.keyword === "CANCELLED");
    openModal("editModal");
  }

  function bindEvents() {
    // Connect form
    $("connectForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var token = $("connectToken").value.trim();
      if (!token) return;
      state.demo = false;
      state.cfg = Sync.saveConfig({
        owner: $("connectOwner").value,
        repo: $("connectRepo").value,
        branch: $("connectBranch").value,
        token: token
      });
      $("connectError").classList.add("hide");
      fetchRemote(false).catch(function () {});
    });

    $("demoBtn").addEventListener("click", loadDemo);
    $("exitDemoBtn").addEventListener("click", function () {
      state.demo = false;
      state.agendaDoc = null;
      state.inboxDoc = null;
      showConnectView();
    });

    // Header actions
    $("refreshBtn").addEventListener("click", function () {
      fetchRemote(false).catch(function () {});
    });
    $("settingsBtn").addEventListener("click", function () {
      $("setToken").value = state.cfg.token || "";
      $("setOwner").value = state.cfg.owner || "yyoon62";
      $("setRepo").value = state.cfg.repo || "notes";
      $("setBranch").value = state.cfg.branch || "main";
      $("settingsError").classList.add("hide");
      openModal("settingsModal");
    });

    // Settings form
    $("settingsForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var token = $("setToken").value.trim();
      if (!token) {
        $("settingsError").textContent = "Please enter a GitHub token (or click Forget token).";
        $("settingsError").classList.remove("hide");
        return;
      }
      state.demo = false;
      state.cfg = Sync.saveConfig({
        owner: $("setOwner").value,
        repo: $("setRepo").value,
        branch: $("setBranch").value,
        token: token
      });
      closeModal("settingsModal");
      fetchRemote(false).catch(function () {});
    });

    $("disconnectBtn").addEventListener("click", function () {
      Sync.clearConfig();
      state.cfg = Sync.loadConfig();
      state.demo = false;
      state.agendaDoc = null;
      state.inboxDoc = null;
      closeModal("settingsModal");
      showConnectView();
      showToast("Token removed from this browser");
    });

    // Quick capture
    $("captureMoreBtn").addEventListener("click", function () {
      $("captureExtras").classList.toggle("hide");
    });
    $("captureForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var title = $("captureInput").value.trim();
      if (!title) return;
      var note = $("captureNote").value.trim();
      var scheduled = $("captureDate").value || null;
      runMutation(function (st) {
        return Org.addInboxItem(st.inboxDoc, {
          title: title,
          note: note,
          scheduled: scheduled,
          now: new Date()
        });
      }, "Added to Inbox").then(function (res) {
        if (res && res.ok) {
          $("captureInput").value = "";
          $("captureNote").value = "";
          $("captureDate").value = "";
          $("captureExtras").classList.add("hide");
        }
      });
    });

    // Reason modal (Waitlist / Cancel)
    $("reasonForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var pending = state.pendingWait;
      if (!pending) return;
      var reason = $("reasonInput").value.trim();
      state.pendingWait = null;
      closeModal("reasonModal");
      commitMove(pending.fp, pending.targetCol, { reason: reason });
    });

    // Edit modal
    $("editForm").addEventListener("submit", function (e) {
      e.preventDefault();
      var loc = Org.findItemLocation(state, state.editingItemId);
      if (!loc) return;
      var fp = Org.fingerprintItem(loc);
      var newTitle = $("editTitleInput").value.trim();
      var newDate = $("editDateInput").value || null;
      var newNote = $("editNoteInput").value;
      closeModal("editModal");
      runMutation(function (st) {
        var id = Org.findItemByFingerprint(st, fp);
        if (!id) return { ok: false, error: "NOT_FOUND" };
        return Org.updateItem(st, id, {
          title: newTitle,
          scheduled: newDate,
          note: newNote
        });
      }, "Saved changes");
    });

    $("cancelTaskBtn").addEventListener("click", function () {
      var loc = Org.findItemLocation(state, state.editingItemId);
      if (!loc) return;
      closeModal("editModal");
      state.pendingWait = { fp: Org.fingerprintItem(loc), targetCol: "cancelled" };
      $("reasonTitle").textContent = "Why cancel this task?";
      $("reasonSubtitle").textContent = loc.item.title;
      $("reasonInput").value = "";
      openModal("reasonModal");
      setTimeout(function () { $("reasonInput").focus(); }, 30);
    });

    // Show all done toggle
    $("toggleDoneBtn").addEventListener("click", function () {
      state.showAllDone = !state.showAllDone;
      render();
    });

    // Mobile section tabs
    document.querySelectorAll("#mobileTabs .mtab").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.mobileCol = btn.dataset.col;
        render();
      });
    });

    // Generic modal close buttons & backdrop click & Escape
    document.querySelectorAll("[data-close-modal]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        closeModal(btn.dataset.closeModal);
      });
    });
    document.querySelectorAll(".tmodal").forEach(function (modal) {
      modal.addEventListener("click", function (e) {
        if (e.target === modal) modal.classList.add("hide");
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        document.querySelectorAll(".tmodal:not(.hide)").forEach(function (m) {
          m.classList.add("hide");
        });
      }
    });

    // Refresh when returning to tab if >30s old
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && !state.demo && state.cfg.token && state.lastSyncedAt) {
        if (Date.now() - state.lastSyncedAt.getTime() > 30000 && !state.busy) {
          fetchRemote(true).catch(function () {});
        }
      }
    });

    // Keep relative sync time fresh
    setInterval(function () {
      if (state.lastSyncedAt && !state.busy && !state.demo && state.cfg.token) {
        setSyncBadge("ok", "Synced · " + formatRelativeTime(state.lastSyncedAt));
      }
    }, 15000);
  }

  function init() {
    setupDropZones();
    bindEvents();
    if (state.cfg.token) {
      fetchRemote(true).catch(function () {});
    } else {
      showConnectView();
    }
  }

  init();
})();
