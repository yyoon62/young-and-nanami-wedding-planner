(function () {
  "use strict";
  var CFG = window.CONFIG || {};
  var PEOPLE = CFG.PEOPLE || ["Young", "Nanami"];
  var LS_WHO = "wv_who", LS_VOTES = "wv_votes_v2", LS_REQ = "wv_requests", SS_BANNER = "wv_banner_dismissed";
  var venues = [];
  var votes = load(LS_VOTES, {}); // votes[slug][person] = {vote, note, dirty?}
  var who = localStorage.getItem(LS_WHO);
  if (PEOPLE.indexOf(who) < 0) who = null;
  // A link like ?as=Nanami pre-selects who you are (handy for sending your partner a link).
  var asParam = new URLSearchParams(location.search).get("as");
  if (asParam && PEOPLE.indexOf(asParam) >= 0) { who = asParam; localStorage.setItem(LS_WHO, who); }
  var view = "venues";      // venues | compare | requests
  var filter = "all";       // all | Boston | NYC | both | todo
  var sortMode = "default"; // compare sort: default | loved | cost
  var syncState = null;     // null = local only, true = synced, false = offline
  var requests = load(LS_REQ, []); // [{id, person, name, url, note, status, created}]
  var FILTERS = ["all", "Boston", "NYC", "both", "todo"];
  var VOTES = ["love", "maybe", "pass"];
  var VLABEL = { love: "Love", maybe: "Maybe", pass: "Pass" };
  var VICON = { love: "♥", maybe: "~", pass: "✕" };
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function $(id) { return document.getElementById(id); }
  function load(k, d) { try { return JSON.parse(localStorage.getItem(k)) || d; } catch (e) { return d; } }
  function saveLocal() { localStorage.setItem(LS_VOTES, JSON.stringify(votes)); }
  function other() { return PEOPLE.filter(function (p) { return p !== who; })[0]; }
  function get(slug, p) { return (votes[slug] && votes[slug][p]) || { vote: "", note: "" }; }
  function isHttp(u) { return /^https?:\/\//i.test(u || ""); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function extLink(cls, text, url) {
    var a = el("a", cls, text);
    if (isHttp(url)) { a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    return a;
  }
  // Static icon markup only (no data), so building it from a string is safe.
  var SVGNS = "http://www.w3.org/2000/svg";
  function cameraIcon() {
    var s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 48 48"); s.setAttribute("aria-hidden", "true");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
    s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
    [["path", { d: "M6 16a4 4 0 0 1 4-4h5l3-4h12l3 4h5a4 4 0 0 1 4 4v20a4 4 0 0 1-4 4H10a4 4 0 0 1-4-4z" }],
     ["circle", { cx: "24", cy: "25", r: "8" }],
     ["path", { d: "M24 21.5c-1.2-1.6-4-1-4 1.2 0 2 4 4.3 4 4.3s4-2.3 4-4.3c0-2.2-2.8-2.8-4-1.2z", fill: "currentColor", stroke: "none" }]
    ].forEach(function (x) {
      var n = document.createElementNS(SVGNS, x[0]);
      Object.keys(x[1]).forEach(function (k) { n.setAttribute(k, x[1][k]); });
      s.appendChild(n);
    });
    return s;
  }

  // ---------- derived data ----------
  function agreement(slug) {
    var m = get(slug, PEOPLE[0]).vote, o = get(slug, PEOPLE[1]).vote;
    if (!m || !o) return "";
    return m === o ? m : "mixed";
  }
  function votedCount(p) { return venues.filter(function (v) { return !!get(v.slug, p).vote; }).length; }
  function bothLoveCount() { return venues.filter(function (v) { return agreement(v.slug) === "love"; }).length; }
  var query = "";           // search text; not saved, cleared on reload
  function matches(v) {
    if (!query) return true;
    var hay = [v.name, v.location, v.area === "NYC" ? "NYC New York" : v.area, v.type, v.note].join(" ").toLowerCase();
    return query.toLowerCase().split(/\s+/).every(function (t) { return !t || hay.indexOf(t) >= 0; });
  }
  function show(v, f) {
    f = f || filter;
    if (!matches(v)) return false;
    if (f === "Boston" || f === "NYC") return v.area === f;
    if (f === "both") return agreement(v.slug) === "love";
    if (f === "todo") return !get(v.slug, who).vote;
    return true;
  }
  function areaLabel(a) { return a === "NYC" ? "NYC area" : a === "Boston" ? "Boston area" : a; }

  // ---------- sync ----------
  var SYNC_TEXT = {
    local: ["On this device only", "Sync isn't set up yet, so votes are saved in this browser only."],
    ok: ["Synced", "Votes are synced with the shared Google Sheet."],
    bad: ["Offline – retrying", "Couldn't reach the shared sheet. Your votes are kept on this device and will be retried."]
  };
  var syncKey = "local", syncLabel = null;
  function setSync(ok, label) {
    syncState = ok;
    syncKey = ok === true ? "ok" : ok === false ? "bad" : "local";
    syncLabel = label || null;
    var s = $("sync");
    s.className = "sync " + syncKey;
    s.title = SYNC_TEXT[syncKey][1];
    s.querySelector(".txt").textContent = syncLabel || SYNC_TEXT[syncKey][0];
    syncChip();
  }
  // On phones the sync state is a colored dot on the avatar; full text lives in title/aria-label.
  function syncChip() {
    var chip = $("switchWho"), d = chip.querySelector(".sdot");
    var txt = syncLabel || SYNC_TEXT[syncKey][0];
    if (d) d.className = "sdot " + syncKey;
    if (who) {
      chip.setAttribute("aria-label", "You're " + who + ". Sync: " + txt + ". Switch person");
      chip.title = "Sync: " + txt + " — " + SYNC_TEXT[syncKey][1];
    }
  }
  function remoteOn() { return !!CFG.APPS_SCRIPT_URL; }

  function pull() {
    if (!remoteOn()) { setSync(null); return Promise.resolve(); }
    var u = CFG.APPS_SCRIPT_URL + "?key=" + encodeURIComponent(CFG.SHARED_KEY || "");
    return fetch(u).then(function (r) { return r.json(); }).then(function (rows) {
      if (!Array.isArray(rows)) throw new Error("bad response");
      rows.forEach(function (r) {
        if (PEOPLE.indexOf(r.person) < 0) return;
        votes[r.venue] = votes[r.venue] || {};
        // never overwrite my own unsaved edits with older remote data
        if (r.person === who && votes[r.venue][r.person] && votes[r.venue][r.person].dirty) return;
        votes[r.venue][r.person] = { vote: r.vote || "", note: r.note || "" };
      });
      saveLocal();
      setSync(true);
      retryDirty();
      render();
      return pullRequests();
    }).catch(function () { setSync(false); });
  }

  // Re-send my edits that failed to save earlier (marked dirty), unless a note save is already pending.
  function retryDirty() {
    if (!who) return;
    Object.keys(votes).forEach(function (slug) {
      var mine = votes[slug] && votes[slug][who];
      if (mine && mine.dirty && !noteTimers[slug] && !inflight[slug]) push(slug);
    });
  }

  function pullRequests() {
    var u = CFG.APPS_SCRIPT_URL + "?action=requests&key=" + encodeURIComponent(CFG.SHARED_KEY || "");
    return fetch(u).then(function (r) { return r.json(); }).then(function (rows) {
      if (!Array.isArray(rows)) return;
      requests = rows;
      localStorage.setItem(LS_REQ, JSON.stringify(requests));
      if (view === "requests") renderReqList();
    }).catch(function () {});
  }

  function submitRequest(name, url, note) {
    var item = { id: "local-" + Date.now(), person: who, name: name, url: url, note: note, status: "new", created: new Date().toISOString() };
    requests.push(item);
    localStorage.setItem(LS_REQ, JSON.stringify(requests));
    renderReqList();
    if (!remoteOn()) return Promise.resolve({ ok: false, local: true });
    return fetch(CFG.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" },
      body: JSON.stringify({ key: CFG.SHARED_KEY || "", action: "request", person: who, name: name, url: url, note: note })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) pullRequests();
      return res || { ok: false };
    }).catch(function () { return { ok: false }; });
  }

  var inflight = {};
  // Saves my vote+note for a venue. Resolves to "ok", "local" or "fail".
  function push(slug) {
    votes[slug] = votes[slug] || {};
    var mine = get(slug, who);
    mine.dirty = true; votes[slug][who] = mine; saveLocal();
    if (!remoteOn()) return Promise.resolve("local");
    inflight[slug] = true;
    var sent = { vote: mine.vote || "", note: mine.note || "" };
    return fetch(CFG.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" }, // avoids CORS preflight
      body: JSON.stringify({ key: CFG.SHARED_KEY || "", venue: slug, person: who, vote: sent.vote, note: sent.note })
    }).then(function (r) { return r.json(); }).then(function (res) {
      delete inflight[slug];
      if (res && res.ok) {
        var cur = votes[slug][who];
        // only clear dirty if nothing changed while the request was in flight
        if (cur && (cur.vote || "") === sent.vote && (cur.note || "") === sent.note) delete cur.dirty;
        saveLocal(); setSync(true); return "ok";
      }
      setSync(false); return "fail";
    }).catch(function () { delete inflight[slug]; setSync(false); return "fail"; });
  }

  // ---------- actions ----------
  function setVote(slug, v) {
    votes[slug] = votes[slug] || {};
    var cur = get(slug, who);
    votes[slug][who] = { vote: cur.vote === v ? "" : v, note: cur.note || "" };
    push(slug); render();
  }
  var noteTimers = {};
  function setHint(slug, text, cls) {
    var c = $("v-" + slug), h = c && c.querySelector(".hint");
    if (h) { h.textContent = text; h.className = "hint" + (cls ? " " + cls : ""); }
  }
  function setNote(slug, text) {
    votes[slug] = votes[slug] || {};
    var cur = get(slug, who);
    votes[slug][who] = { vote: cur.vote || "", note: text, dirty: true };
    saveLocal();
    setHint(slug, "Saving…");
    clearTimeout(noteTimers[slug]);
    noteTimers[slug] = setTimeout(function () {
      delete noteTimers[slug];
      push(slug).then(function (res) {
        if (noteTimers[slug]) return; // typing resumed
        if (res === "ok") setHint(slug, "Saved ✓", "ok");
        else if (res === "local") setHint(slug, "Saved on this device ✓", "ok");
        else setHint(slug, "Not synced – will retry", "bad");
      });
    }, 700);
  }

  // ---------- cards ----------
  function carousel(v) {
    var media = el("div", "media");
    if (!v.photos.length) {
      var ph = el("div", "placeholder");
      ph.appendChild(cameraIcon());
      ph.appendChild(el("div", null, "No photos yet — see the venue site"));
      var l0 = v.links && v.links[0];
      if (l0 && isHttp(l0.url)) ph.appendChild(extLink("btnlink", l0.label + " ↗", l0.url));
      media.appendChild(ph);
      var w0 = el("div", "mediawrap"); w0.appendChild(media); w0.appendChild(el("div", "dots"));
      return w0;
    }
    var n = v.photos.length;
    var wrap = el("div", "mediawrap"), dots = el("div", "dots");
    wrap.appendChild(media); wrap.appendChild(dots);
    var car = el("div", "carousel");
    car.setAttribute("aria-label", v.name + " photos");
    v.photos.forEach(function (p, i) {
      var b = el("button", "ph"); b.type = "button";
      b.setAttribute("aria-label", "Open " + v.name + " photo " + (i + 1) + " of " + n);
      var im = el("img"); im.src = p; im.loading = i ? "lazy" : "eager"; im.decoding = "async";
      im.alt = v.name + " photo " + (i + 1) + " of " + n;
      b.appendChild(im);
      b.addEventListener("click", function () { openLb(v, i, b); });
      car.appendChild(b);
    });
    media.appendChild(car);
    if (n < 2) return wrap;
    var count = el("span", "count", "1 / " + n);
    for (var i = 0; i < n; i++) dots.appendChild(el("i", i ? null : "on"));
    var prev = el("button", "arrow prev", "‹"), next = el("button", "arrow next", "›");
    prev.type = next.type = "button";
    prev.setAttribute("aria-label", "Previous photo"); next.setAttribute("aria-label", "Next photo");
    function step() { var f = car.firstChild; return f ? f.getBoundingClientRect().width + 4 : 1; }
    function idx() {
      if (car.scrollLeft >= car.scrollWidth - car.clientWidth - 2) return n - 1;
      return Math.max(0, Math.min(n - 1, Math.round(car.scrollLeft / step())));
    }
    function update() {
      var k = idx();
      count.textContent = (k + 1) + " / " + n;
      Array.prototype.forEach.call(dots.children, function (d, j) { d.className = j === k ? "on" : ""; });
      prev.disabled = k === 0; next.disabled = k === n - 1;
    }
    function go(d) {
      var k = Math.max(0, Math.min(n - 1, idx() + d));
      car.scrollTo({ left: k * step(), behavior: reduceMotion ? "auto" : "smooth" });
    }
    var raf = 0;
    car.addEventListener("scroll", function () { if (!raf) raf = requestAnimationFrame(function () { raf = 0; update(); }); }, { passive: true });
    prev.addEventListener("click", function () { go(-1); });
    next.addEventListener("click", function () { go(1); });
    prev.disabled = true;
    car._update = update;
    media.appendChild(count); media.appendChild(prev); media.appendChild(next);
    return wrap;
  }

  function votePill(v, prefix) {
    var t = v ? VICON[v] + " " + VLABEL[v] : "—";
    return el("span", "vpill " + (v || "none"), prefix ? prefix + t : t);
  }

  var AGREE = { love: "♥ You both love this", mixed: "≈ Mixed feelings", pass: "Both passed", maybe: "~ Both on the fence" };

  function card(v) {
    var mine = get(v.slug, who), theirs = get(v.slug, other()), ag = agreement(v.slug);
    var c = el("article", "card" + (ag ? " ag-" + ag : "") + (ag === "pass" && filter === "all" ? " dim" : ""));
    c.id = "v-" + v.slug;
    c.appendChild(carousel(v));
    var b = el("div", "body");
    var head = el("div", "head");
    head.appendChild(el("h2", null, v.name));
    head.appendChild(el("span", "area " + v.area, areaLabel(v.area)));
    b.appendChild(head);
    b.appendChild(el("p", "loc", v.location + " · " + v.type));
    var f = el("div", "facts");
    [["Site fee", v.siteFee, "fact"], ["Est. total (80 guests)", v.estimate, "fact est"]].forEach(function (kv) {
      var d = el("div", kv[2]); d.appendChild(el("span", "k", kv[0]));
      if (kv[2] === "fact est") {
        var sp = splitEst(kv[1]);
        d.appendChild(el("span", "v", sp[0]));
        if (sp[1]) d.appendChild(el("span", "sub", sp[1]));
      } else d.appendChild(el("span", "v", kv[1]));
      f.appendChild(d);
    });
    b.appendChild(f);
    if (v.note) b.appendChild(el("p", "note", v.note));
    var links = el("div", "links");
    (v.links || []).forEach(function (l) {
      if (!isHttp(l.url)) return;
      var label = l.label === "Official" ? "Official site" : l.label;
      links.appendChild(extLink("linkchip", label + " ↗", l.url));
    });
    if (links.childNodes.length) b.appendChild(links);

    var bot = el("div", "bottom");
    var vw = el("div");
    var lbl = el("div", "lbl vhead"); lbl.appendChild(el("span", null, "Your vote")); vw.appendChild(lbl);
    var seg = el("div", "seg"); seg.setAttribute("role", "group"); seg.setAttribute("aria-label", "Your vote for " + v.name);
    VOTES.forEach(function (x) {
      var btn = el("button", x); btn.type = "button";
      btn.setAttribute("aria-pressed", mine.vote === x ? "true" : "false");
      btn.setAttribute("data-fk", "vote-" + x);
      btn.appendChild(el("span", "ic", VICON[x])).setAttribute("aria-hidden", "true");
      btn.appendChild(el("span", null, VLABEL[x]));
      btn.addEventListener("click", function () { setVote(v.slug, x); });
      seg.appendChild(btn);
    });
    vw.appendChild(seg);
    bot.appendChild(vw);

    var sl = el("div", "status-line");
    if (theirs.vote) sl.appendChild(votePill(theirs.vote, other() + ": "));
    else sl.appendChild(el("span", "waiting", other() + " hasn't voted yet"));
    if (ag) sl.appendChild(el("span", "agree " + ag, AGREE[ag]));
    lbl.appendChild(sl); // partner status + agreement sit on the "Your vote" line

    if (theirs.note) {
      var q = el("div", "theirnote");
      q.appendChild(el("span", "who", other() + "'s note"));
      q.appendChild(document.createTextNode(theirs.note));
      bot.appendChild(q);
    }
    var nw = el("div", "notes");
    var nl = el("label", "lbl"); nl.htmlFor = "note-" + v.slug;
    nl.appendChild(el("span", null, "Your notes")); nl.appendChild(el("span", "hint"));
    nw.appendChild(nl);
    var ta = el("textarea"); ta.id = "note-" + v.slug; ta.rows = 2;
    ta.placeholder = "Vibe, concerns, questions to ask…";
    ta.value = mine.note || "";
    ta.addEventListener("input", function () { autosize(ta); setNote(v.slug, ta.value); });
    ta.addEventListener("blur", function () { if (pendingRender) setTimeout(render, 0); });
    nw.appendChild(ta);
    if (!mine.note) {
      // Collapsed until needed: keeps phone cards short.
      nw.classList.add("hide");
      var add = el("button", "addnote"); add.type = "button"; add.setAttribute("data-fk", "addnote");
      add.appendChild(el("span", "plus", "＋")).setAttribute("aria-hidden", "true");
      add.appendChild(el("span", null, "Add a note"));
      add.setAttribute("aria-expanded", "false"); add.setAttribute("aria-controls", "note-" + v.slug);
      add.addEventListener("click", function () {
        add.classList.add("hide"); nw.classList.remove("hide"); ta.focus();
      });
      bot.appendChild(add);
    } else requestAnimationFrame(function () { if (ta.isConnected) autosize(ta); });
    bot.appendChild(nw);
    b.appendChild(bot);
    c.appendChild(b);
    return c;
  }

  // "$85-90k (up to $125k)" -> ["$85-90k", "(up to $125k)"]
  function splitEst(s) {
    s = String(s || ""); var i = s.indexOf("(");
    return i > 0 ? [s.slice(0, i).trim(), s.slice(i).trim()] : [s, ""];
  }
  function autosize(ta) { ta.style.height = "auto"; ta.style.height = (ta.scrollHeight + 2) + "px"; }

  function cardSig(v) {
    var a = get(v.slug, PEOPLE[0]), b = get(v.slug, PEOPLE[1]);
    return JSON.stringify([who, filter === "all", a.vote, a.note, b.vote, b.note]);
  }

  // ---------- list render (keyed, keeps carousel scroll and focus) ----------
  var listKey = "", sigs = {}, pendingRender = false;
  function typingIn(node) {
    var a = document.activeElement;
    return a && a.tagName === "TEXTAREA" && node.contains(a);
  }
  function swapCard(old, fresh) {
    var a = document.activeElement, fk = old.contains(a) && a.getAttribute("data-fk");
    var car = old.querySelector(".carousel"), sl = car ? car.scrollLeft : 0;
    old.parentNode.replaceChild(fresh, old);
    var nc = fresh.querySelector(".carousel");
    if (nc && sl) { nc.scrollLeft = sl; if (nc._update) nc._update(); }
    if (fk) { var t = fresh.querySelector('[data-fk="' + fk + '"]'); if (t) t.focus(); }
  }
  function renderList(force) {
    var list = $("list");
    var vis = venues.filter(function (v) { return show(v); });
    var key = [who, filter, query].concat(vis.map(function (v) { return v.slug; })).join(",");
    if (key === listKey) {
      vis.forEach(function (v) {
        var s = cardSig(v), old = $("v-" + v.slug);
        if (!old || sigs[v.slug] === s) return;
        if (typingIn(old)) { pendingRender = true; return; }
        swapCard(old, card(v)); sigs[v.slug] = s;
      });
      return;
    }
    if (!force && typingIn(list)) { pendingRender = true; return; }
    var scrolls = {};
    list.querySelectorAll(".card").forEach(function (c) {
      var car = c.querySelector(".carousel"); if (car && car.scrollLeft) scrolls[c.id] = car.scrollLeft;
    });
    listKey = key; sigs = {};
    list.textContent = "";
    vis.forEach(function (v) { list.appendChild(card(v)); sigs[v.slug] = cardSig(v); });
    Object.keys(scrolls).forEach(function (id) {
      var c = $(id), car = c && c.querySelector(".carousel");
      if (car) { car.scrollLeft = scrolls[id]; if (car._update) car._update(); }
    });
    if (!vis.length) list.appendChild(emptyState());
  }

  function emptyState() {
    var d = el("div", "empty");
    if (filter === "both") {
      d.appendChild(el("p", "big", "No mutual loves yet — keep voting!"));
      d.appendChild(navBtn("See venues you haven't voted on", "#todo"));
    } else if (filter === "todo") {
      d.appendChild(el("p", "big", "You've voted on every venue 🎉"));
      d.appendChild(navBtn("Compare your votes", "#compare"));
    } else if (query) {
      d.appendChild(el("p", "big", "No venues match \u201c" + query + "\u201d."));
      var cb = el("button", "btn", "Clear search"); cb.type = "button";
      cb.addEventListener("click", function () { setQuery(""); $("q").focus(); });
      d.appendChild(cb);
    } else {
      d.appendChild(el("p", "big", "Nothing here yet."));
      d.appendChild(navBtn("Show all venues", "#all"));
    }
    return d;
  }
  function navBtn(text, hash) { var a = el("a", "btn", text); a.href = hash; return a; }

  // ---------- header / nav ----------
  function renderHeader() {
    document.body.classList.toggle("nowho", !who);
    var chip = $("switchWho"); chip.textContent = "";
    if (who) {
      var avw = el("span", "avw");
      avw.appendChild(el("span", "av " + (who === PEOPLE[0] ? "rose" : "sage"), who.charAt(0)));
      avw.appendChild(el("span", "sdot " + syncKey));
      chip.appendChild(avw);
      var wl = el("span", "wl");
      var me = el("span"); me.appendChild(document.createTextNode("You're ")); me.appendChild(el("b", null, who));
      me.className = "me";
      wl.appendChild(me); wl.appendChild(el("span", "sep", "·")); wl.appendChild(el("span", "sw", "Switch"));
      chip.appendChild(wl);
      syncChip();
    }
    var pr = $("progress"); pr.textContent = "";
    if (who && venues.length) {
      progressBars().forEach(function (d) { pr.appendChild(d); });
      var nb = bothLoveCount();
      var bl = el("a", "bothlink" + (nb ? "" : " zero"), nb ? "♥ " + nb + " you both love" : "No mutual loves yet"); bl.href = "#both";
      pr.appendChild(bl);
    }
    document.querySelectorAll(".views a").forEach(function (a) {
      if (a.getAttribute("data-view") === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    document.body.classList.toggle("v-venues", view === "venues");
    renderPCard();
  }

  function progressBars() {
    var total = venues.length;
    return [who, other()].map(function (p, i) {
      var n = votedCount(p);
      var d = el("div", "pp" + (i ? " partner" : ""));
      d.title = p + " has voted on " + n + " of " + total + " venues";
      var t = el("span"); t.appendChild(el("b", null, p)); t.appendChild(document.createTextNode(" " + n + "/" + total));
      d.appendChild(t);
      var tr = el("span", "track"), fi = el("span", "fill"); fi.style.width = Math.round(100 * n / total) + "%";
      tr.appendChild(fi); d.appendChild(tr);
      return d;
    });
  }
  function localDismissed() { try { return !!sessionStorage.getItem(SS_BANNER); } catch (e) { return false; } }

  // "Our progress" card: progress on phones (Venues view only) + the local-only notice everywhere.
  function renderPCard() {
    var c = $("pcard"); c.textContent = "";
    if (!who || !venues.length) { c.className = "pcard hide"; return; }
    var showLocal = !remoteOn() && !localDismissed();
    c.className = "pcard" + (showLocal ? "" : " noline");
    var prog = el("div", "prog");
    var bars = el("div", "pbars");
    progressBars().forEach(function (d) { bars.appendChild(d); });
    prog.appendChild(bars);
    var nb = bothLoveCount();
    var bl = el("a", "bothlink2" + (nb ? "" : " zero"), nb ? "♥ " + nb + " you both love →" : "No mutual loves yet"); bl.href = "#both";
    prog.appendChild(bl);
    c.appendChild(prog);
    if (showLocal) {
      var ln = el("div", "localline");
      ln.appendChild(el("span", null, "Saved on this device only — " + other() + " won't see your votes until sync is set up."));
      var x = el("button", "x", "✕"); x.type = "button"; x.setAttribute("aria-label", "Dismiss this notice");
      x.addEventListener("click", function () {
        try { sessionStorage.setItem(SS_BANNER, "1"); } catch (e) {}
        renderPCard();
      });
      ln.appendChild(x);
      c.appendChild(ln);
    }
  }

  var CHIP_LABEL = { all: "All", Boston: "Boston", NYC: "NYC", both: "♥ Both love", todo: "Not voted yet" };
  function renderChips() {
    var box = $("chips"), sl = box.scrollLeft; box.textContent = "";
    FILTERS.forEach(function (f) {
      var a = el("a", null, CHIP_LABEL[f]); a.href = "#" + f;
      a.appendChild(el("span", "n", String(venues.filter(function (v) { return show(v, f); }).length)));
      if (f === filter) a.setAttribute("aria-current", "true");
      box.appendChild(a);
    });
    box.scrollLeft = sl; chipsFade();
  }
  function chipsFade() {
    var box = $("chips");
    box.classList.toggle("atend", box.scrollLeft + box.clientWidth >= box.scrollWidth - 4);
  }
  function setQuery(q) {
    query = q; if ($("q").value !== q) $("q").value = q; $("qclear").classList.toggle("hide", !q);
    renderChips(); renderList(true);
  }
  $("q").addEventListener("input", function () { setQuery(this.value); });
  $("q").addEventListener("keydown", function (e) { if (e.key === "Escape" && query) { setQuery(""); } });
  $("qclear").addEventListener("click", function () { setQuery(""); $("q").focus(); });
  $("chips").addEventListener("scroll", chipsFade, { passive: true });
  window.addEventListener("resize", chipsFade);

  // ---------- compare ----------
  function firstNum(s) { var m = /\d+(\.\d+)?/.exec(String(s || "").replace(/,/g, "")); return m ? parseFloat(m[0]) : Infinity; }
  function loveScore(v) {
    var sc = { love: 3, maybe: 1, pass: -2 }, t = 0;
    PEOPLE.forEach(function (p) { t += sc[get(v.slug, p).vote] || 0; });
    return t;
  }
  function sorted(list) {
    var l = list.slice();
    if (sortMode === "loved") l.sort(function (a, b) { return loveScore(b) - loveScore(a) || venues.indexOf(a) - venues.indexOf(b); });
    if (sortMode === "cost") l.sort(function (a, b) { return firstNum(a.estimate) - firstNum(b.estimate) || venues.indexOf(a) - venues.indexOf(b); });
    return l;
  }
  function thumb(v) {
    if (!v.photos.length) { var d = el("span", "thumb"); d.setAttribute("aria-hidden", "true"); return d; }
    var im = el("img", "thumb"); im.src = v.photos[0]; im.alt = ""; im.loading = "lazy"; return im;
  }
  function cardLink(a, v) {
    a.href = "#v-" + v.slug;
    // same hash twice doesn't fire hashchange, so handle that case directly
    a.addEventListener("click", function (e) { if (location.hash === "#v-" + v.slug) { e.preventDefault(); applyHash(false); } });
    return a;
  }
  function nameLink(v) {
    var a = cardLink(el("a", "vname"), v);
    a.appendChild(thumb(v)); a.appendChild(el("span", null, v.name));
    return a;
  }
  function areas() {
    var out = [];
    venues.forEach(function (v) { if (out.indexOf(v.area) < 0) out.push(v.area); });
    return out;
  }
  function summaryTile(cls, icon, label, list, names) {
    var b = el(cls === "love" ? "a" : "button", "tile " + cls);
    var top = el("span", "tn");
    if (icon) top.appendChild(el("span", "ti", icon)).setAttribute("aria-hidden", "true");
    top.appendChild(document.createTextNode(String(list.length)));
    b.appendChild(top);
    b.appendChild(el("span", "tl", label));
    if (names && list.length) {
      var shown = list.slice(0, 3).map(function (v) { return v.name; }).join(", ");
      b.appendChild(el("span", "tnames", shown + (list.length > 3 ? " +" + (list.length - 3) : "")));
    }
    if (cls === "love") b.href = "#both";
    else {
      b.type = "button";
      if (!list.length) b.disabled = true;
      b.addEventListener("click", function () { scrollToRow(list[0]); });
    }
    return b;
  }
  // Scroll to a venue's row in whichever compare layout (table or phone list) is visible.
  function scrollToRow(v) {
    if (!v) return;
    ["cmp-", "cmpm-"].forEach(function (pre) {
      var r = $(pre + v.slug);
      if (r && r.offsetParent) {
        r.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" });
        r.classList.remove("flash"); void r.offsetWidth; r.classList.add("flash");
      }
    });
  }

  function renderCompare() {
    var box = $("compareView"); box.textContent = "";
    var bothLove = venues.filter(function (v) { return agreement(v.slug) === "love"; });
    var mixed = venues.filter(function (v) { return agreement(v.slug) === "mixed"; });
    var notBoth = venues.filter(function (v) { return !agreement(v.slug); });
    var sum = el("div", "summary");
    // first matching row in the current display order
    var order = [];
    areas().forEach(function (a) { order = order.concat(sorted(venues.filter(function (v) { return v.area === a; }))); });
    function inOrder(list) { return order.filter(function (v) { return list.indexOf(v) >= 0; }); }
    sum.appendChild(summaryTile("love", "♥", "Both love", bothLove, true));
    sum.appendChild(summaryTile("mixed", "≈", "Mixed", inOrder(mixed)));
    sum.appendChild(summaryTile("todo", "", "Not voted by both", inOrder(notBoth)));
    box.appendChild(sum);

    var tools = el("div", "cmptools");
    tools.appendChild(el("h2", null, "Side by side"));
    var sl = el("label", "sortsel"); sl.appendChild(document.createTextNode("Sort by"));
    var sel = el("select"); sel.setAttribute("aria-label", "Sort venues");
    [["default", "Default"], ["loved", "Most loved"], ["cost", "Lowest estimate"]].forEach(function (o) {
      var op = el("option", null, o[1]); op.value = o[0]; if (o[0] === sortMode) op.selected = true; sel.appendChild(op);
    });
    sel.addEventListener("change", function () { sortMode = sel.value; renderCompare(); var s = $("compareView").querySelector("select"); if (s) s.focus(); });
    sl.appendChild(sel); tools.appendChild(sl);
    box.appendChild(tools);

    // desktop table
    var wrap = el("div", "tablewrap"), t = el("table"), th = el("thead"), h = el("tr");
    ["Venue", "Est. total", "Site fee", "You (" + who + ")", other(), "Agree?"].forEach(function (x) {
      var c = el("th", null, x); c.scope = "col"; h.appendChild(c);
    });
    th.appendChild(h); t.appendChild(th);
    var tb = el("tbody");
    // mobile list
    var ml = el("div", "mlist");
    areas().forEach(function (area) {
      var vs = sorted(venues.filter(function (v) { return v.area === area; }));
      var g = el("tr", "grp"), gc = el("th", null, areaLabel(area)); gc.colSpan = 6; gc.scope = "colgroup"; g.appendChild(gc); tb.appendChild(g);
      ml.appendChild(el("h3", "mgrp", areaLabel(area)));
      vs.forEach(function (v) {
        var ag = agreement(v.slug);
        var r = el("tr"), nc = el("th"); r.id = "cmp-" + v.slug; nc.scope = "row"; nc.appendChild(nameLink(v)); r.appendChild(nc);
        r.appendChild(el("td", "est", v.estimate));
        r.appendChild(el("td", "fee", v.siteFee));
        [who, other()].forEach(function (p) { var td = el("td"); td.appendChild(votePill(get(v.slug, p).vote)); r.appendChild(td); });
        var agc = el("td", "ag");
        if (ag === "love" || ag === "mixed" || ag === "pass") {
          var s = el("span", ag, ag === "love" ? "♥" : ag === "mixed" ? "≈" : "✕");
          s.title = ag === "love" ? "You both love it" : ag === "mixed" ? "Mixed feelings" : "Both passed";
          s.setAttribute("aria-hidden", "true");
          agc.appendChild(s); agc.appendChild(el("span", "sr", s.title));
        }
        r.appendChild(agc);
        tb.appendChild(r);

        var row = el("div", "mrow"); row.id = "cmpm-" + v.slug;
        row.appendChild(thumb(v));
        var mm = el("div", "mmain");
        var top = el("div", "mtop");
        top.appendChild(cardLink(el("a", "vname", v.name), v));
        if (ag === "love") top.appendChild(el("span", "agree love", "♥ Both"));
        else if (ag === "mixed") top.appendChild(el("span", "agree mixed", "≈ Mixed"));
        else if (ag === "pass") top.appendChild(el("span", "agree pass", "✕ Both passed"));
        mm.appendChild(top);
        mm.appendChild(el("div", "mest", v.estimate));
        var line = el("div", "mvotes");
        [["You", who], [other(), other()]].forEach(function (x) {
          var cell = el("div", "mv");
          cell.appendChild(el("span", "mwho", x[0]));
          cell.appendChild(votePill(get(v.slug, x[1]).vote));
          line.appendChild(cell);
        });
        mm.appendChild(line);
        mm.appendChild(el("div", "mfee", "Site fee: " + v.siteFee));
        row.appendChild(mm);
        ml.appendChild(row);
      });
    });
    t.appendChild(tb); wrap.appendChild(t);
    box.appendChild(wrap);
    box.appendChild(ml);
  }

  // ---------- venue requests ----------
  function relDate(s) {
    var d = new Date(s); if (!s || isNaN(d)) return "";
    var days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    if (days < 14) return days + " days ago";
    if (days < 60) return Math.floor(days / 7) + " weeks ago";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }
  function renderReqList() {
    var box = $("reqList");
    if (!box) return;
    box.textContent = "";
    if (!requests.length) { box.appendChild(el("p", "reqempty", "No requests yet. Spot a venue you like? Add it above.")); return; }
    requests.slice().reverse().forEach(function (r) {
      var d = el("div", "req");
      var top = el("div", "rtop");
      top.appendChild(el("div", "rname", r.name));
      var st = String(r.status || "new");
      top.appendChild(el("span", "status " + (["new", "researching", "added", "declined"].indexOf(st) >= 0 ? st : ""), st));
      d.appendChild(top);
      if (isHttp(r.url)) d.appendChild(extLink(null, r.url, r.url));
      if (r.note) d.appendChild(el("div", "rnote", r.note));
      var when = relDate(r.created);
      d.appendChild(el("div", "rmeta", "Requested by " + r.person + (when ? " · " + when : "")));
      box.appendChild(d);
    });
  }

  var reqBuilt = false;
  function renderRequests() {
    var box = $("requestsView");
    if (!reqBuilt) {
      reqBuilt = true;
      var lay = el("div", "reqlayout");
      var f = el("form", "reqform"); f.noValidate = true;
      f.appendChild(el("h2", null, "Request a venue"));
      f.appendChild(el("p", "intro", "Add a venue you'd like researched. Claude will look up pricing and photos and add it to the list."));
      var fields = [["Venue name *", "input", "rq-name", "e.g. The Gardens at Ashland"],
                    ["Link (optional)", "input", "rq-url", "https://..."],
                    ["Why / questions (optional)", "textarea", "rq-note", "What caught your eye? Anything to check?"]];
      fields.forEach(function (x) {
        var l = el("label", null, x[0]); l.htmlFor = x[2]; f.appendChild(l);
        var i = el(x[1]); i.id = x[2]; i.placeholder = x[3];
        if (x[1] === "input") { i.maxLength = x[2] === "rq-name" ? 120 : 500; i.type = x[2] === "rq-url" ? "url" : "text"; } else i.maxLength = 1000;
        f.appendChild(i);
      });
      var act = el("div", "actions");
      var btn = el("button", "btn", "Send request"); btn.type = "submit";
      var msg = el("div", "notice hide"); msg.setAttribute("role", "status");
      function say(text, cls) { msg.textContent = text; msg.className = "notice " + cls; }
      f.addEventListener("submit", function (e) {
        e.preventDefault();
        var name = $("rq-name").value.trim();
        var url = $("rq-url").value.trim();
        var note = $("rq-note").value.trim();
        if (!name) { say("Please enter a venue name.", "warn"); $("rq-name").focus(); return; }
        if (url && !isHttp(url)) { say("Link must start with http:// or https://", "warn"); $("rq-url").focus(); return; }
        btn.disabled = true; say("Sending…", "warn");
        submitRequest(name, url, note).then(function (res) {
          btn.disabled = false;
          if (res.ok) say("Sent ✓ It's on the list below.", "ok");
          else say(res.local ? "Saved on this device only (sync is not set up)." : "Could not reach the sheet. Saved on this device; try again later.", "warn");
          ["rq-name", "rq-url", "rq-note"].forEach(function (id) { $(id).value = ""; });
        });
      });
      act.appendChild(btn); act.appendChild(msg);
      f.appendChild(act);
      lay.appendChild(f);
      var col = el("div", "reqcol");
      col.appendChild(el("h2", null, "All requests"));
      var list = el("div"); list.id = "reqList"; col.appendChild(list);
      lay.appendChild(col);
      box.appendChild(lay);
    }
    renderReqList();
  }

  // ---------- render ----------
  function render(force) {
    renderHeader();
    if (!who) return;
    pendingRender = false;
    $("venuesView").classList.toggle("hide", view !== "venues");
    $("compareView").classList.toggle("hide", view !== "compare");
    $("requestsView").classList.toggle("hide", view !== "requests");
    if (view === "compare") renderCompare();
    else if (view === "requests") renderRequests();
    else { renderChips(); renderList(force === true); }
  }

  // ---------- hash routing ----------
  function applyHash(initial) {
    var h = decodeURIComponent(location.hash.replace(/^#/, "")), target = null;
    if (h === "compare" || h === "requests") view = h;
    else if (FILTERS.indexOf(h) >= 0) { view = "venues"; filter = h; }
    else if (/^v-/.test(h) && venues.some(function (v) { return "v-" + v.slug === h; })) {
      view = "venues"; if (!venues.some(function (v) { return "v-" + v.slug === h && show(v); })) filter = "all"; target = h;
    } else if (!h) { view = "venues"; filter = "all"; }
    else return; // unrelated hash (e.g. skip link)
    render(true);
    if (target) {
      var c = $(target);
      if (c) requestAnimationFrame(function () { c.scrollIntoView({ block: "start", behavior: reduceMotion || initial ? "auto" : "smooth" }); });
    } else if (!initial) window.scrollTo(0, 0);
  }
  window.addEventListener("hashchange", function () { applyHash(false); });

  // ---------- lightbox ----------
  var lbSet = [], lbIdx = 0, lbName = "", lbReturn = null;
  function lbShow() {
    var im = $("lbImg");
    im.src = lbSet[lbIdx];
    im.alt = lbName + " photo " + (lbIdx + 1) + " of " + lbSet.length;
    $("lbName").textContent = lbName;
    $("lbCount").textContent = (lbIdx + 1) + " / " + lbSet.length;
    var multi = lbSet.length > 1;
    $("lbPrev").classList.toggle("hide", !multi); $("lbNext").classList.toggle("hide", !multi);
  }
  function openLb(v, i, from) {
    lbSet = v.photos; lbIdx = i; lbName = v.name; lbReturn = from || null; lbShow();
    $("lb").classList.remove("hide"); document.body.classList.add("locked");
    $("lbClose").focus();
  }
  function closeLb() {
    $("lb").classList.add("hide"); document.body.classList.remove("locked");
    if (lbReturn && document.body.contains(lbReturn)) lbReturn.focus();
  }
  function lbStep(d) { lbIdx = (lbIdx + lbSet.length + d) % lbSet.length; lbShow(); }
  $("lbClose").addEventListener("click", closeLb);
  $("lbPrev").addEventListener("click", function () { lbStep(-1); });
  $("lbNext").addEventListener("click", function () { lbStep(1); });
  $("lb").addEventListener("click", function (e) { if (e.target === this || e.target.className === "lbstage") closeLb(); });
  var tx = null, ty = null;
  $("lb").addEventListener("touchstart", function (e) { if (e.touches.length === 1) { tx = e.touches[0].clientX; ty = e.touches[0].clientY; } }, { passive: true });
  $("lb").addEventListener("touchend", function (e) {
    if (tx == null) return;
    var dx = e.changedTouches[0].clientX - tx, dy = e.changedTouches[0].clientY - ty; tx = null;
    if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy) && lbSet.length > 1) lbStep(dx < 0 ? 1 : -1);
  }, { passive: true });

  // ---------- who modal ----------
  var modalReturn = null;
  function openWho() {
    modalReturn = document.activeElement;
    $("who-modal").classList.remove("hide"); document.body.classList.add("locked");
    var first = $("who-modal").querySelector("[data-who]"); if (first) first.focus({ focusVisible: false });
  }
  function closeWho() {
    $("who-modal").classList.add("hide"); document.body.classList.remove("locked");
    if (modalReturn && modalReturn.focus && document.body.contains(modalReturn)) modalReturn.focus();
  }
  function pickWho(w) {
    who = w; localStorage.setItem(LS_WHO, w);
    listKey = "";
    closeWho();
    render(); pull();
  }
  document.querySelectorAll("#who-modal [data-who]").forEach(function (b) {
    b.addEventListener("click", function () { pickWho(b.getAttribute("data-who")); });
  });
  $("who-modal").addEventListener("click", function (e) { if (e.target === this && who) closeWho(); });
  $("switchWho").addEventListener("click", openWho);

  document.addEventListener("keydown", function (e) {
    if (!$("lb").classList.contains("hide")) {
      if (e.key === "Escape") closeLb();
      if (e.key === "ArrowLeft") lbStep(-1);
      if (e.key === "ArrowRight") lbStep(1);
      return;
    }
    if (!$("who-modal").classList.contains("hide") && e.key === "Escape" && who) closeWho();
  });

  // ---------- boot ----------
  setSync(null, remoteOn() ? "Connecting…" : null);
  fetch("data/venues.json").then(function (r) { return r.json(); }).then(function (d) {
    venues = d;
    if (!who) { renderHeader(); openWho(); }
    applyHash(true);
    pull();
    setInterval(pull, 30000);
    window.addEventListener("focus", pull);
  });
})();
