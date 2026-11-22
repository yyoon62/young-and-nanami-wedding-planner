// Google Apps Script: stores votes in the spreadsheet this script is attached to.
// Setup steps are in README.md ("Set up syncing").

var SHARED_KEY = "change-me";            // must match SHARED_KEY in config.js
var SHEET_NAME = "Votes";
var PEOPLE = ["Young", "Nanami"];
var VOTES = ["", "love", "maybe", "pass"];

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var s = ss.getSheetByName(SHEET_NAME);
  if (!s) {
    s = ss.insertSheet(SHEET_NAME);
    s.appendRow(["venue", "person", "vote", "note", "updated"]);
  }
  return s;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  if ((e.parameter.key || "") !== SHARED_KEY) return json_({ ok: false, error: "bad key" });
  var rows = sheet_().getDataRange().getValues().slice(1).map(function (r) {
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
    if (!/^[a-z0-9_-]{1,40}$/.test(d.venue || "")) return json_({ ok: false, error: "bad venue" });
    if (PEOPLE.indexOf(d.person) < 0) return json_({ ok: false, error: "bad person" });
    if (VOTES.indexOf(d.vote || "") < 0) return json_({ ok: false, error: "bad vote" });
    var note = String(d.note || "").slice(0, 1000);
    var s = sheet_();
    var data = s.getDataRange().getValues();
    var row = -1;
    for (var i = 1; i < data.length; i++) {
      if (data[i][0] === d.venue && data[i][1] === d.person) { row = i + 1; break; }
    }
    var vals = [d.venue, d.person, d.vote || "", note, new Date().toISOString()];
    if (row > 0) s.getRange(row, 1, 1, 5).setValues([vals]);
    else s.appendRow(vals);
    return json_({ ok: true });
  } finally {
    lock.releaseLock();
  }
}
