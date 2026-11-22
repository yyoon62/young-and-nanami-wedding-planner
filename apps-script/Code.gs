// Google Apps Script: stores votes and venue requests in the spreadsheet this script
// is attached to. Setup steps are in README.md ("Set up syncing").

var SHARED_KEY = "change-me";            // must match SHARED_KEY in config.js
var VOTES_SHEET = "Votes";
var REQUESTS_SHEET = "Requests";
var PEOPLE = ["Young", "Nanami"];
var VOTES = ["", "love", "maybe", "pass"];
var STATUSES = ["new", "researching", "added", "declined"];

function sheet_(name, header) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var s = ss.getSheetByName(name);
  if (!s) {
    s = ss.insertSheet(name);
    s.appendRow(header);
  }
  return s;
}
function votes_() { return sheet_(VOTES_SHEET, ["venue", "person", "vote", "note", "updated"]); }
function requests_() { return sheet_(REQUESTS_SHEET, ["id", "person", "name", "url", "note", "status", "created"]); }

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  if ((e.parameter.key || "") !== SHARED_KEY) return json_({ ok: false, error: "bad key" });
  if (e.parameter.action === "requests") {
    var reqs = requests_().getDataRange().getValues().slice(1).map(function (r) {
      return { id: r[0], person: r[1], name: r[2], url: r[3], note: r[4], status: r[5], created: r[6] };
    });
    return json_(reqs);
  }
  var rows = votes_().getDataRange().getValues().slice(1).map(function (r) {
    return { venue: r[0], person: r[1], vote: r[2], note: r[3], updated: r[4] };
  });
  return json_(rows);
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var d = JSON.parse(e.postData.contents);
    if (d.key !== SHARED_KEY) return json_({ ok: false, error: "bad key" });
    if (d.action === "request") return addRequest_(d);
    if (d.action === "setStatus") return setStatus_(d);
    return saveVote_(d);
  } finally {
    lock.releaseLock();
  }
}

function saveVote_(d) {
  if (!/^[a-z0-9_-]{1,40}$/.test(d.venue || "")) return json_({ ok: false, error: "bad venue" });
  if (PEOPLE.indexOf(d.person) < 0) return json_({ ok: false, error: "bad person" });
  if (VOTES.indexOf(d.vote || "") < 0) return json_({ ok: false, error: "bad vote" });
  var note = String(d.note || "").slice(0, 1000);
  var s = votes_();
  var data = s.getDataRange().getValues();
  var row = -1;
  for (var i = 1; i < data.length; i++) {
    if (data[i][0] === d.venue && data[i][1] === d.person) { row = i + 1; break; }
  }
  var vals = [d.venue, d.person, d.vote || "", note, new Date().toISOString()];
  if (row > 0) s.getRange(row, 1, 1, 5).setValues([vals]);
  else s.appendRow(vals);
  return json_({ ok: true });
}

function addRequest_(d) {
  if (PEOPLE.indexOf(d.person) < 0) return json_({ ok: false, error: "bad person" });
  var name = String(d.name || "").trim().slice(0, 120);
  if (!name) return json_({ ok: false, error: "name required" });
  var url = String(d.url || "").trim().slice(0, 500);
  if (url && !/^https?:\/\//i.test(url)) return json_({ ok: false, error: "bad url" });
  var note = String(d.note || "").slice(0, 1000);
  // Guard against spreadsheet formula injection: text that starts with = + - @ becomes plain text.
  [name, url, note] = [name, url, note].map(function (x) { return /^[=+\-@]/.test(x) ? "'" + x : x; });
  requests_().appendRow([Utilities.getUuid(), d.person, name, url, note, "new", new Date().toISOString()]);
  return json_({ ok: true });
}

function setStatus_(d) {
  if (STATUSES.indexOf(d.status) < 0) return json_({ ok: false, error: "bad status" });
  var s = requests_();
  var data = s.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (data[i][0] === d.id) { s.getRange(i + 1, 6).setValue(d.status); return json_({ ok: true }); }
  }
  return json_({ ok: false, error: "not found" });
}
