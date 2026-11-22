(function () {
  "use strict";
  var CFG = window.CONFIG || {};
  var PEOPLE = CFG.PEOPLE || ["Young", "Nanami"];
  var LS_WHO = "wv_who", LS_VOTES = "wv_votes_v2";
  var venues = [];
  var votes = load(LS_VOTES, {}); // votes[slug][person] = {vote, note}
  var who = localStorage.getItem(LS_WHO);
  // A link like ?as=Nanami pre-selects who you are (handy for sending your partner a link).
  var asParam = new URLSearchParams(location.search).get("as");
  if (asParam && PEOPLE.indexOf(asParam) >= 0) { who = asParam; localStorage.setItem(LS_WHO, who); }
  var filter = "all";
  var syncOK = null;

  function load(k, d) { try { return JSON.parse(localStorage.getItem(k)) || d; } catch (e) { return d; } }
  function saveLocal() { localStorage.setItem(LS_VOTES, JSON.stringify(votes)); }
  function other() { return PEOPLE.filter(function (p) { return p !== who; })[0]; }
  function get(slug, p) { return (votes[slug] && votes[slug][p]) || { vote: "", note: "" }; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ---------- sync ----------
  function setSync(ok, msg) {
    syncOK = ok;
    var s = document.getElementById("sync");
    s.className = "sync " + (ok === true ? "ok" : ok === false ? "bad" : "");
    s.textContent = msg;
  }
  function remoteOn() { return !!CFG.APPS_SCRIPT_URL; }

  function pull() {
    if (!remoteOn()) { setSync(null, "local only"); return Promise.resolve(); }
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
      setSync(true, "synced");
      render();
    }).catch(function () { setSync(false, "offline"); });
  }

  function push(slug) {
    var mine = get(slug, who);
    mine.dirty = true; saveLocal();
    if (!remoteOn()) return;
    fetch(CFG.APPS_SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "text/plain;charset=utf-8" }, // avoids CORS preflight
      body: JSON.stringify({ key: CFG.SHARED_KEY || "", venue: slug, person: who, vote: mine.vote || "", note: mine.note || "" })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) { delete votes[slug][who].dirty; saveLocal(); setSync(true, "synced"); }
      else setSync(false, "save failed");
    }).catch(function () { setSync(false, "offline"); });
  }

  // ---------- actions ----------
  function setVote(slug, v) {
    votes[slug] = votes[slug] || {};
    var cur = get(slug, who);
    votes[slug][who] = { vote: cur.vote === v ? "" : v, note: cur.note };
    push(slug); render();
  }
  var noteTimers = {};
  function setNote(slug, text) {
    votes[slug] = votes[slug] || {};
    var cur = get(slug, who);
    votes[slug][who] = { vote: cur.vote, note: text, dirty: true };
    saveLocal();
    clearTimeout(noteTimers[slug]);
    noteTimers[slug] = setTimeout(function () { push(slug); }, 700);
  }

  // ---------- render ----------
  function show(v) {
    var m = get(v.slug, who).vote, o = get(v.slug, other()).vote;
    if (filter === "all") return true;
    if (filter === "Boston" || filter === "NYC") return v.area === filter;
    if (filter === "both") return m === "love" && o === "love";
    if (filter === "todo") return !m;
    return true;
  }

  function voteRow(slug, person, mine) {
    var row = el("div", "vrow " + (mine ? "mine" : "other"));
    row.appendChild(el("span", "name", person));
    var cur = get(slug, person).vote;
    ["love", "maybe", "pass"].forEach(function (v) {
      var b = el("button", v + (cur === v ? " on" : ""), v[0].toUpperCase() + v.slice(1));
      if (mine) b.addEventListener("click", function () { setVote(slug, v); });
      else b.disabled = true;
      row.appendChild(b);
    });
    return row;
  }

  function card(v) {
    var c = el("section", "card");
    c.appendChild(el("h2", null, v.name));
    c.appendChild(el("div", "loc", v.location + " - " + v.type));
    var car = el("div", "carousel");
    if (!v.photos.length) car.appendChild(el("div", "none", "No photos yet. See links below."));
    v.photos.forEach(function (p, i) {
      var im = el("img"); im.src = p; im.loading = "lazy"; im.alt = v.name;
      im.addEventListener("click", function () { openLb(v.photos, i); });
      car.appendChild(im);
    });
    c.appendChild(car);
    var f = el("div", "facts");
    [["Site fee", v.siteFee], ["Full wedding (80 guests)", v.estimate]].forEach(function (kv) {
      var d = el("div"); var b = el("b", null, kv[0] + ": "); d.appendChild(b); d.appendChild(document.createTextNode(kv[1])); f.appendChild(d);
    });
    c.appendChild(f);
    c.appendChild(el("p", "note", v.note));
    var links = el("div", "links");
    v.links.forEach(function (l, i) {
      if (i) links.appendChild(document.createTextNode(" | "));
      var a = el("a", null, l.label); a.href = l.url; a.target = "_blank"; a.rel = "noopener"; links.appendChild(a);
    });
    c.appendChild(links);
    var vt = el("div", "votes");
    vt.appendChild(voteRow(v.slug, who, true));
    vt.appendChild(voteRow(v.slug, other(), false));
    var on = get(v.slug, other()).note;
    if (on) vt.appendChild(el("div", "theirnote", other() + ": " + on));
    var ta = el("textarea"); ta.placeholder = "Your notes (vibe, concerns, questions to ask)";
    ta.value = get(v.slug, who).note || "";
    ta.addEventListener("input", function () { setNote(v.slug, ta.value); });
    vt.appendChild(ta);
    c.appendChild(vt);
    return c;
  }

  function pill(v) { return el("span", "pill " + (v || ""), v || "-"); }

  function renderCompare() {
    var box = document.getElementById("compare"); box.textContent = "";
    var t = el("table"), h = el("tr");
    ["Venue", "Area", "Site fee", "Full wedding", PEOPLE[0], PEOPLE[1]].forEach(function (x) { h.appendChild(el("th", null, x)); });
    t.appendChild(h);
    venues.forEach(function (v) {
      var r = el("tr");
      r.appendChild(el("td", null, v.name)); r.appendChild(el("td", null, v.area));
      r.appendChild(el("td", null, v.siteFee)); r.appendChild(el("td", null, v.estimate));
      PEOPLE.forEach(function (p) { var td = el("td"); td.appendChild(pill(get(v.slug, p).vote)); r.appendChild(td); });
      t.appendChild(r);
    });
    box.appendChild(t);
  }

  function render() {
    if (!who) return;
    document.getElementById("whoLabel").textContent = "You: " + who;
    var list = document.getElementById("list"), cmp = document.getElementById("compare");
    var comparing = filter === "compare";
    list.classList.toggle("hide", comparing); cmp.classList.toggle("hide", !comparing);
    if (comparing) { renderCompare(); return; }
    var active = document.activeElement, keep = active && active.tagName === "TEXTAREA";
    if (keep) return; // don't re-render while typing a note
    list.textContent = "";
    var n = 0;
    venues.forEach(function (v) { if (show(v)) { list.appendChild(card(v)); n++; } });
    if (!n) list.appendChild(el("p", null, "Nothing here yet."));
  }

  // ---------- lightbox ----------
  var lbSet = [], lbIdx = 0;
  function lbShow() { document.getElementById("lbImg").src = lbSet[lbIdx]; }
  function openLb(set, i) { lbSet = set; lbIdx = i; lbShow(); document.getElementById("lb").classList.remove("hide"); }
  function closeLb() { document.getElementById("lb").classList.add("hide"); }
  document.getElementById("lbClose").addEventListener("click", closeLb);
  document.getElementById("lbPrev").addEventListener("click", function () { lbIdx = (lbIdx + lbSet.length - 1) % lbSet.length; lbShow(); });
  document.getElementById("lbNext").addEventListener("click", function () { lbIdx = (lbIdx + 1) % lbSet.length; lbShow(); });
  document.addEventListener("keydown", function (e) {
    if (document.getElementById("lb").classList.contains("hide")) return;
    if (e.key === "Escape") closeLb();
    if (e.key === "ArrowLeft") document.getElementById("lbPrev").click();
    if (e.key === "ArrowRight") document.getElementById("lbNext").click();
  });

  // ---------- who / filters ----------
  function pickWho(w) {
    who = w; localStorage.setItem(LS_WHO, w);
    document.getElementById("who-modal").classList.add("hide");
    render(); pull();
  }
  document.querySelectorAll("#who-modal [data-who]").forEach(function (b) {
    b.addEventListener("click", function () { pickWho(b.dataset.who); });
  });
  document.getElementById("switchWho").addEventListener("click", function () {
    document.getElementById("who-modal").classList.remove("hide");
  });
  document.querySelectorAll("#filters button").forEach(function (b) {
    b.addEventListener("click", function () {
      filter = b.dataset.f;
      document.querySelectorAll("#filters button").forEach(function (x) { x.classList.toggle("on", x === b); });
      render();
    });
  });

  // ---------- boot ----------
  fetch("data/venues.json").then(function (r) { return r.json(); }).then(function (d) {
    venues = d;
    if (!who) document.getElementById("who-modal").classList.remove("hide");
    render(); pull();
    setInterval(pull, 30000);
    window.addEventListener("focus", pull);
  });
})();
