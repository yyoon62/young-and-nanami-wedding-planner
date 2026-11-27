"use strict";

var assert = require("assert");
var OrgTodo = require("../todo/org.js");

var SAMPLE_AGENDA = [
  "#+TITLE: Agenda: Now / Waitlist",
  "#+STARTUP: overview",
  "",
  "Cross-project priorities. Detail lives in project files.",
  "Rule of thumb: at most 3 NEXT items at once.",
  "",
  "* Now (NEXT)",
  "** NEXT Pick weekend hiking trail :outdoors:",
  "   Compare distance and trailhead parking. See [[file:hiking_notes.org][hiking_notes.org]].",
  "",
  "* Waitlist",
  "** WAITING Order replacement coffee grinder burr",
  "   Waiting on part number confirmation from manufacturer.",
  "** WAITING Paint guest hallway",
  "   Blocked on picking paint swatch.",
  "",
  "* Done",
  "** DONE Renew library card",
  "   CLOSED: [2026-11-20]",
  "   Done online.",
  "** CANCELLED Print paper train timetable",
  "   Not needed: mobile ticket app works offline.",
  "",
  "* Someday / ideas",
  ""
].join("\n");

var SAMPLE_INBOX = [
  "#+TITLE: Inbox",
  "#+STARTUP: overview",
  "",
  "Quick capture lands here (C-c o c). Triage into NEXT / WAITING / a project file.",
  "",
  "* Inbox",
  "** TODO Replace kitchen water filter",
  "  captured [2026-11-25 Wed 15:19]",
  "** TODO Check museum timed-entry tickets",
  "   SCHEDULED: <2026-12-18 Fri>",
  "   Opens one week before visit; check [[https://example.org/museum][museum portal]].",
  ""
].join("\n");

var tests = [];
function test(name, fn) {
  tests.push({ name: name, fn: fn });
}

test("byte-for-byte round-trip on agenda.org and inbox.org", function () {
  var agenda = OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda");
  var inbox = OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox");
  assert.strictEqual(OrgTodo.serializeOrgFile(agenda), SAMPLE_AGENDA);
  assert.strictEqual(OrgTodo.serializeOrgFile(inbox), SAMPLE_INBOX);

  var summary = OrgTodo.getBoardSummary({ agendaDoc: agenda, inboxDoc: inbox });
  assert.strictEqual(summary.now.length, 1);
  assert.strictEqual(summary.now[0].tags, " :outdoors:");
  assert.strictEqual(summary.waitlist.length, 2);
  assert.strictEqual(summary.done.length, 2);
  assert.strictEqual(summary.done[0].closed, "2026-11-20");
  assert.strictEqual(summary.done[1].keyword, "CANCELLED");
  assert.strictEqual(summary.inbox.length, 2);
  assert.strictEqual(summary.inbox[0].captured, "2026-11-25 Wed 15:19");
  assert.strictEqual(summary.inbox[1].scheduled, "2026-12-18");
});

test("addInboxItem appends TODO with captured timestamp, optional scheduled date and note", function () {
  var inbox = OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox");
  var now = new Date(2026, 10, 27, 9, 5); // 2026-11-27 Fri 09:05
  var res = OrgTodo.addInboxItem(inbox, {
    title: "Buy winter windshield fluid",
    scheduled: "2026-12-01",
    note: "Check trunk for spare bottle first.",
    now: now
  });
  assert.strictEqual(res.ok, true);
  assert.deepStrictEqual(res.changedFiles, ["inbox"]);

  var out = OrgTodo.serializeOrgFile(inbox);
  assert(out.indexOf("** TODO Buy winter windshield fluid\n") !== -1);
  assert(out.indexOf("   SCHEDULED: <2026-12-01 Tue>\n") !== -1);
  assert(out.indexOf("   captured [2026-11-27 Fri 09:05]\n") !== -1);
  assert(out.indexOf("   Check trunk for spare bottle first.\n") !== -1);
  // Existing items untouched
  assert(out.indexOf("  captured [2026-11-25 Wed 15:19]\n") !== -1);
});

test("moveItem from Now to Done marks DONE, adds CLOSED date, preserves blank lines", function () {
  var state = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  var item = OrgTodo.getBoardSummary(state).now[0];
  var res = OrgTodo.moveItem(state, item.id, "done", {
    now: new Date(2026, 10, 27, 14, 0)
  });
  assert.strictEqual(res.ok, true);
  assert.deepStrictEqual(res.changedFiles, ["agenda"]);

  var out = OrgTodo.serializeOrgFile(state.agendaDoc);
  assert(out.indexOf("* Now (NEXT)\n\n* Waitlist\n") !== -1, "blank line after empty Now preserved");
  assert(out.indexOf("* Done\n** DONE Pick weekend hiking trail :outdoors:\n   CLOSED: [2026-11-27]\n") !== -1);
  assert(out.indexOf("Compare distance and trailhead parking") !== -1);
});

test("cross-file move from Inbox to Done updates both files cleanly", function () {
  var state = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  var inboxItem = OrgTodo.getBoardSummary(state).inbox[0];
  var res = OrgTodo.moveItem(state, inboxItem.id, "done", {
    now: new Date(2026, 10, 27, 14, 0)
  });
  assert.strictEqual(res.ok, true);
  assert.deepStrictEqual(res.changedFiles.sort(), ["agenda", "inbox"]);
  assert.strictEqual(OrgTodo.getBoardSummary(state).inbox.length, 1);
  assert.strictEqual(OrgTodo.getBoardSummary(state).done[0].title, "Replace kitchen water filter");
  assert.strictEqual(OrgTodo.getBoardSummary(state).done[0].closed, "2026-11-27");
});

test("enforces max 3 NEXT items when moving into Now, allows reordering within Now", function () {
  var state = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  // Promote both waitlist items into Now -> total 3
  var w = OrgTodo.getBoardSummary(state).waitlist.slice();
  assert.strictEqual(OrgTodo.moveItem(state, w[0].id, "now").ok, true);
  assert.strictEqual(OrgTodo.moveItem(state, w[1].id, "now").ok, true);
  assert.strictEqual(OrgTodo.getBoardSummary(state).now.length, 3);

  // Attempting to promote a 4th from Inbox must fail with MAX_NEXT
  var inboxItem = OrgTodo.getBoardSummary(state).inbox[0];
  var blocked = OrgTodo.moveItem(state, inboxItem.id, "now");
  assert.strictEqual(blocked.ok, false);
  assert.strictEqual(blocked.error, "MAX_NEXT");
  assert.strictEqual(blocked.currentNext.length, 3);

  // Reordering within Now is allowed even at 3 items
  var thirdNow = OrgTodo.getBoardSummary(state).now[2];
  var reorder = OrgTodo.moveItem(state, thirdNow.id, "now", { insertIndex: 0 });
  assert.strictEqual(reorder.ok, true);
  assert.strictEqual(OrgTodo.getBoardSummary(state).now[0].id, thirdNow.id);
});

test("moving to Waitlist attaches reason and moving out of Done strips CLOSED", function () {
  var state = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  var doneItem = OrgTodo.getBoardSummary(state).done[0];
  var res = OrgTodo.moveItem(state, doneItem.id, "waitlist", {
    reason: "Waiting for confirmation email."
  });
  assert.strictEqual(res.ok, true);
  assert.strictEqual(doneItem.keyword, "WAITING");
  assert.strictEqual(doneItem.closed, null);
  assert(doneItem.noteLines[0].indexOf("Waiting for confirmation email.") !== -1);
});

test("cancelling moves item to Done with CANCELLED keyword and reason", function () {
  var state = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  var inboxItem = OrgTodo.getBoardSummary(state).inbox[0];
  var res = OrgTodo.moveItem(state, inboxItem.id, "cancelled", {
    reason: "Already replaced last month."
  });
  assert.strictEqual(res.ok, true);
  var topDone = OrgTodo.getBoardSummary(state).done[0];
  assert.strictEqual(topDone.keyword, "CANCELLED");
  assert.strictEqual(topDone.closed, null);
  assert(topDone.noteLines.join("\n").indexOf("Already replaced last month.") !== -1);
});

test("updateItem edits title, note, and scheduled date while keeping captured metadata", function () {
  var state = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  var inboxItem = OrgTodo.getBoardSummary(state).inbox[0];
  var res = OrgTodo.updateItem(state, inboxItem.id, {
    title: "Replace kitchen filter cartridge",
    note: "Model XF-200 under sink.",
    scheduled: "2026-12-05"
  });
  assert.strictEqual(res.ok, true);
  assert.strictEqual(inboxItem.captured, "2026-11-25 Wed 15:19");
  assert.strictEqual(inboxItem.scheduled, "2026-12-05");
  assert.deepStrictEqual(inboxItem.noteLines, ["Model XF-200 under sink."]);
});

test("parseOrgLinks handles file links, https links, and ignores unsafe schemes", function () {
  var segs = OrgTodo.parseOrgLinks(
    "See [[file:notes.org][notes.org]] and [[https://example.com][site]] and [[javascript:alert(1)][bad]]."
  );
  assert.strictEqual(segs.filter(function (s) { return s.type === "file"; }).length, 1);
  assert.strictEqual(segs.filter(function (s) { return s.type === "url"; }).length, 1);
  // javascript: must be degraded to plain text, never a clickable url
  var jsSegs = segs.filter(function (s) { return s.text === "bad"; });
  assert.strictEqual(jsSegs[0].type, "text");
});

test("findItemByFingerprint locates an item across fresh re-parse (conflict replay)", function () {
  var state1 = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  var loc = OrgTodo.findItemLocation(state1, OrgTodo.getBoardSummary(state1).inbox[1].id);
  var fp = OrgTodo.fingerprintItem(loc);

  var state2 = {
    agendaDoc: OrgTodo.parseOrgFile(SAMPLE_AGENDA, "agenda"),
    inboxDoc: OrgTodo.parseOrgFile(SAMPLE_INBOX, "inbox")
  };
  var foundId = OrgTodo.findItemByFingerprint(state2, fp);
  assert(foundId);
  assert.strictEqual(OrgTodo.findItemLocation(state2, foundId).item.title, "Check museum timed-entry tickets");
});

var failed = 0;
tests.forEach(function (t) {
  try {
    t.fn();
    console.log("PASS", t.name);
  } catch (err) {
    failed += 1;
    console.error("FAIL", t.name, "\n", err && err.stack || err);
  }
});

if (failed > 0) {
  process.exit(1);
}
console.log("All " + tests.length + " org_todo tests passed.");
