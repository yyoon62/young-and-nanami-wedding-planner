/* Lossless Org-mode parser, serializer, and TODO board operations for
 * agenda.org + inbox.org. Pure JS with zero dependencies; works in both
 * browsers (window.OrgTodo) and Node.js (module.exports).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.OrgTodo = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var KEYWORDS = ["TODO", "NEXT", "WAITING", "DONE", "CANCELLED"];
  var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MAX_NEXT = 3;

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function formatOrgDate(date) {
    var d = date instanceof Date ? date : new Date();
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function formatOrgDay(date) {
    var d = date instanceof Date ? date : new Date();
    return DAYS[d.getDay()];
  }

  function formatOrgTimestamp(date) {
    var d = date instanceof Date ? date : new Date();
    return "[" + formatOrgDate(d) + " " + formatOrgDay(d) + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + "]";
  }

  function formatScheduledTimestamp(isoDate) {
    // isoDate: "YYYY-MM-DD"
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || "").trim());
    if (!m) return null;
    var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return "<" + m[1] + "-" + m[2] + "-" + m[3] + " " + formatOrgDay(d) + ">";
  }

  function classifySection(title) {
    var t = String(title || "").trim().toLowerCase();
    if (/^now\b/.test(t) || /\(next\)/.test(t)) return "now";
    if (/^waitlist\b/.test(t) || /^waiting\b/.test(t)) return "waitlist";
    if (/^done\b/.test(t)) return "done";
    if (/^someday\b/.test(t)) return "someday";
    if (/^inbox\b/.test(t)) return "inbox";
    return "other";
  }

  var _idCounter = 0;
  function nextId(prefix) {
    _idCounter += 1;
    return (prefix || "item") + "_" + _idCounter;
  }

  function extractItemMeta(bodyLines) {
    var scheduled = null;
    var deadline = null;
    var closed = null;
    var captured = null;
    var noteLines = [];

    for (var i = 0; i < bodyLines.length; i++) {
      var line = bodyLines[i];
      var trimmed = line.trim();
      var isMeta = false;

      var mSched = /SCHEDULED:\s*<(\d{4}-\d{2}-\d{2})[^>]*>/.exec(trimmed);
      if (mSched) {
        scheduled = mSched[1];
        isMeta = true;
      }
      var mDead = /DEADLINE:\s*<(\d{4}-\d{2}-\d{2})[^>]*>/.exec(trimmed);
      if (mDead) {
        deadline = mDead[1];
        isMeta = true;
      }
      var mClosed = /^CLOSED:\s*\[(\d{4}-\d{2}-\d{2})[^\]]*\]/.exec(trimmed);
      if (mClosed) {
        closed = mClosed[1];
        isMeta = true;
      }
      var mCap = /^captured\s+\[([^\]]+)\]$/.exec(trimmed);
      if (mCap) {
        captured = mCap[1];
        isMeta = true;
      }

      if (!isMeta) {
        noteLines.push(trimmed);
      }
    }

    // Trim leading/trailing empty strings from noteLines for display
    while (noteLines.length && noteLines[0] === "") noteLines.shift();
    while (noteLines.length && noteLines[noteLines.length - 1] === "") noteLines.pop();

    return {
      scheduled: scheduled,
      deadline: deadline,
      closed: closed,
      captured: captured,
      noteLines: noteLines
    };
  }

  function parseHeadingLine(line) {
    // Matches level-2 heading: "** [KEYWORD ]title[   :tag1:tag2:]"
    var m = /^(\*\*)\s+(.*)$/.exec(line);
    if (!m) return null;
    var rest = m[2];
    var keyword = "";
    var kwMatch = /^(TODO|NEXT|WAITING|DONE|CANCELLED)(?:\s+(.*))?$/.exec(rest);
    if (kwMatch) {
      keyword = kwMatch[1];
      rest = kwMatch[2] || "";
    }
    var tags = "";
    var tagMatch = /^(.*?)(\s+:[A-Za-z0-9_@#%:]+:)$/.exec(rest);
    if (tagMatch) {
      rest = tagMatch[1];
      tags = tagMatch[2];
    }
    return {
      stars: m[1],
      keyword: keyword,
      title: rest.trim(),
      tags: tags
    };
  }

  function parseOrgFile(text, fileKind) {
    var normalized = String(text || "").replace(/\r\n/g, "\n");
    var hasTrailingNewline = normalized.length > 0 && normalized.charAt(normalized.length - 1) === "\n";
    var rawLines = normalized.length === 0
      ? []
      : (hasTrailingNewline ? normalized.slice(0, -1).split("\n") : normalized.split("\n"));

    var preamble = [];
    var sections = [];
    var currentSection = null;
    var currentItem = null;

    function finishItem() {
      if (!currentItem) return;
      var meta = extractItemMeta(currentItem.bodyLines);
      currentItem.scheduled = meta.scheduled;
      currentItem.deadline = meta.deadline;
      currentItem.closed = meta.closed;
      currentItem.captured = meta.captured;
      currentItem.noteLines = meta.noteLines;
      currentItem = null;
    }

    function finishSection() {
      finishItem();
      if (!currentSection) return;
      // Separate trailing blank lines at end of section so moving the last
      // item preserves spacing between top-level sections.
      var target = currentSection.items.length > 0
        ? currentSection.items[currentSection.items.length - 1].bodyLines
        : currentSection.intro;
      var blanks = [];
      while (target.length > 0 && target[target.length - 1].trim() === "") {
        blanks.unshift(target.pop());
      }
      currentSection.trailingBlanks = blanks;
      if (currentSection.items.length > 0) {
        var last = currentSection.items[currentSection.items.length - 1];
        var meta = extractItemMeta(last.bodyLines);
        last.scheduled = meta.scheduled;
        last.deadline = meta.deadline;
        last.closed = meta.closed;
        last.captured = meta.captured;
        last.noteLines = meta.noteLines;
      }
      currentSection = null;
    }

    for (var i = 0; i < rawLines.length; i++) {
      var line = rawLines[i];
      if (/^\*\s+/.test(line)) {
        finishSection();
        var secTitle = line.replace(/^\*\s+/, "").trim();
        currentSection = {
          heading: line,
          title: secTitle,
          kind: classifySection(secTitle),
          intro: [],
          items: [],
          trailingBlanks: []
        };
        sections.push(currentSection);
      } else if (currentSection && /^\*\*\s+/.test(line)) {
        finishItem();
        var parsed = parseHeadingLine(line);
        currentItem = {
          id: nextId(fileKind || "org"),
          rawHeading: line,
          stars: parsed.stars,
          keyword: parsed.keyword,
          title: parsed.title,
          tags: parsed.tags,
          bodyLines: [],
          scheduled: null,
          deadline: null,
          closed: null,
          captured: null,
          noteLines: []
        };
        currentSection.items.push(currentItem);
      } else if (currentItem) {
        currentItem.bodyLines.push(line);
      } else if (currentSection) {
        currentSection.intro.push(line);
      } else {
        preamble.push(line);
      }
    }
    finishSection();

    return {
      fileKind: fileKind || "org",
      preamble: preamble,
      sections: sections,
      hasTrailingNewline: hasTrailingNewline
    };
  }

  function serializeItemHeading(item) {
    if (item.rawHeading !== null && item.rawHeading !== undefined) {
      return item.rawHeading;
    }
    var parts = [item.stars || "**"];
    if (item.keyword) parts.push(item.keyword);
    parts.push(item.title);
    return parts.join(" ") + (item.tags || "");
  }

  function serializeOrgFile(doc) {
    var lines = [];
    for (var p = 0; p < doc.preamble.length; p++) {
      lines.push(doc.preamble[p]);
    }
    for (var s = 0; s < doc.sections.length; s++) {
      var sec = doc.sections[s];
      lines.push(sec.heading);
      for (var k = 0; k < sec.intro.length; k++) {
        lines.push(sec.intro[k]);
      }
      for (var j = 0; j < sec.items.length; j++) {
        var it = sec.items[j];
        lines.push(serializeItemHeading(it));
        for (var b = 0; b < it.bodyLines.length; b++) {
          lines.push(it.bodyLines[b]);
        }
      }
      for (var t = 0; t < sec.trailingBlanks.length; t++) {
        lines.push(sec.trailingBlanks[t]);
      }
    }
    if (lines.length === 0) return "";
    return lines.join("\n") + (doc.hasTrailingNewline ? "\n" : "");
  }

  function refreshItemMeta(item) {
    var meta = extractItemMeta(item.bodyLines);
    item.scheduled = meta.scheduled;
    item.deadline = meta.deadline;
    item.closed = meta.closed;
    item.captured = meta.captured;
    item.noteLines = meta.noteLines;
    return item;
  }

  function getOrCreateSection(doc, kind, defaultHeading) {
    for (var i = 0; i < doc.sections.length; i++) {
      if (doc.sections[i].kind === kind) return doc.sections[i];
    }
    // Add a separating blank line on previous section if needed
    if (doc.sections.length > 0) {
      var prev = doc.sections[doc.sections.length - 1];
      if (prev.trailingBlanks.length === 0) {
        prev.trailingBlanks.push("");
      }
    }
    var sec = {
      heading: defaultHeading,
      title: defaultHeading.replace(/^\*\s+/, "").trim(),
      kind: kind,
      intro: [],
      items: [],
      trailingBlanks: []
    };
    doc.sections.push(sec);
    return sec;
  }

  function findItemLocation(state, itemId) {
    var files = [
      { key: "agenda", doc: state.agendaDoc },
      { key: "inbox", doc: state.inboxDoc }
    ];
    for (var f = 0; f < files.length; f++) {
      var doc = files[f].doc;
      if (!doc) continue;
      for (var s = 0; s < doc.sections.length; s++) {
        var sec = doc.sections[s];
        for (var i = 0; i < sec.items.length; i++) {
          if (sec.items[i].id === itemId) {
            return {
              file: files[f].key,
              doc: doc,
              section: sec,
              index: i,
              item: sec.items[i]
            };
          }
        }
      }
    }
    return null;
  }

  function findItemByFingerprint(state, fp) {
    if (!fp) return null;
    var files = [
      { key: "agenda", doc: state.agendaDoc },
      { key: "inbox", doc: state.inboxDoc }
    ];
    // Prefer exact file + section + title match, then title match anywhere
    var fallback = null;
    for (var f = 0; f < files.length; f++) {
      var doc = files[f].doc;
      if (!doc) continue;
      for (var s = 0; s < doc.sections.length; s++) {
        var sec = doc.sections[s];
        for (var i = 0; i < sec.items.length; i++) {
          var it = sec.items[i];
          if (it.title === fp.title) {
            if (files[f].key === fp.file && sec.kind === fp.sectionKind) {
              return it.id;
            }
            if (!fallback) fallback = it.id;
          }
        }
      }
    }
    return fallback;
  }

  function fingerprintItem(loc) {
    return {
      file: loc.file,
      sectionKind: loc.section.kind,
      title: loc.item.title,
      keyword: loc.item.keyword
    };
  }

  function setClosedLine(item, dateStr) {
    // Remove existing CLOSED lines
    var next = [];
    for (var i = 0; i < item.bodyLines.length; i++) {
      if (!/^\s*CLOSED:\s*\[/.test(item.bodyLines[i])) {
        next.push(item.bodyLines[i]);
      }
    }
    if (dateStr) {
      next.unshift("   CLOSED: [" + dateStr + "]");
    }
    item.bodyLines = next;
    refreshItemMeta(item);
  }

  function setScheduledLine(item, isoDate) {
    var next = [];
    var replaced = false;
    var formatted = isoDate ? formatScheduledTimestamp(isoDate) : null;
    for (var i = 0; i < item.bodyLines.length; i++) {
      if (/^\s*SCHEDULED:\s*</.test(item.bodyLines[i])) {
        if (formatted && !replaced) {
          next.push("   SCHEDULED: " + formatted);
          replaced = true;
        }
      } else {
        next.push(item.bodyLines[i]);
      }
    }
    if (formatted && !replaced) {
      // Insert after CLOSED if present, otherwise at top
      var insertAt = next.length > 0 && /^\s*CLOSED:\s*\[/.test(next[0]) ? 1 : 0;
      next.splice(insertAt, 0, "   SCHEDULED: " + formatted);
    }
    item.bodyLines = next;
    refreshItemMeta(item);
  }

  function setNoteText(item, noteText) {
    // Keep metadata lines (CLOSED, SCHEDULED, DEADLINE, captured) and replace free-text note lines.
    var metaLines = [];
    for (var i = 0; i < item.bodyLines.length; i++) {
      var trimmed = item.bodyLines[i].trim();
      if (/^(CLOSED:|SCHEDULED:|DEADLINE:)/.test(trimmed) || /^captured\s+\[[^\]]+\]$/.test(trimmed)) {
        metaLines.push(item.bodyLines[i]);
      }
    }
    var cleaned = String(noteText || "").replace(/\r\n/g, "\n").trim();
    var body = metaLines.slice();
    if (cleaned) {
      var parts = cleaned.split("\n");
      for (var j = 0; j < parts.length; j++) {
        body.push(parts[j].trim() === "" ? "" : "   " + parts[j].trim());
      }
    }
    item.bodyLines = body;
    refreshItemMeta(item);
  }

  function prependReasonIfNeeded(item, reason) {
    var r = String(reason || "").trim();
    if (!r) return;
    // Insert right after metadata lines
    var idx = 0;
    while (idx < item.bodyLines.length) {
      var t = item.bodyLines[idx].trim();
      if (/^(CLOSED:|SCHEDULED:|DEADLINE:)/.test(t) || /^captured\s+\[[^\]]+\]$/.test(t)) {
        idx++;
      } else {
        break;
      }
    }
    item.bodyLines.splice(idx, 0, "   " + r);
    refreshItemMeta(item);
  }

  function addInboxItem(inboxDoc, opts) {
    var title = String((opts && opts.title) || "").replace(/\s+/g, " ").trim();
    if (!title) {
      return { ok: false, error: "EMPTY_TITLE" };
    }
    var now = (opts && opts.now instanceof Date) ? opts.now : new Date();
    var sec = getOrCreateSection(inboxDoc, "inbox", "* Inbox");
    var bodyLines = [];
    if (opts && opts.scheduled) {
      var ts = formatScheduledTimestamp(opts.scheduled);
      if (ts) bodyLines.push("   SCHEDULED: " + ts);
    }
    bodyLines.push("   captured " + formatOrgTimestamp(now));
    var note = String((opts && opts.note) || "").replace(/\r\n/g, "\n").trim();
    if (note) {
      var lines = note.split("\n");
      for (var i = 0; i < lines.length; i++) {
        bodyLines.push(lines[i].trim() === "" ? "" : "   " + lines[i].trim());
      }
    }
    var item = refreshItemMeta({
      id: nextId("inbox"),
      stars: "**",
      keyword: "TODO",
      title: title,
      tags: "",
      bodyLines: bodyLines
    });
    sec.items.push(item);
    inboxDoc.hasTrailingNewline = true;
    return {
      ok: true,
      item: item,
      changedFiles: ["inbox"],
      commitMessage: "inbox: add " + title.slice(0, 50)
    };
  }

  function moveItem(state, itemId, targetColumn, opts) {
    opts = opts || {};
    var loc = findItemLocation(state, itemId);
    if (!loc) return { ok: false, error: "NOT_FOUND" };

    var target = String(targetColumn || "").toLowerCase();
    var validTargets = { inbox: 1, now: 1, waitlist: 1, done: 1, cancelled: 1, someday: 1 };
    if (!validTargets[target]) return { ok: false, error: "INVALID_TARGET" };

    var nowSec = getOrCreateSection(state.agendaDoc, "now", "* Now (NEXT)");
    var isEnteringNow = target === "now" && !(loc.file === "agenda" && loc.section.kind === "now");

    if (isEnteringNow && nowSec.items.length >= MAX_NEXT && !opts.forceMaxNext) {
      return {
        ok: false,
        error: "MAX_NEXT",
        maxNext: MAX_NEXT,
        currentNext: nowSec.items.map(function (it) {
          return { id: it.id, title: it.title };
        })
      };
    }

    var targetDoc = target === "inbox" ? state.inboxDoc : state.agendaDoc;
    var targetFile = target === "inbox" ? "inbox" : "agenda";
    var targetSec;
    var newKeyword;

    if (target === "inbox") {
      targetSec = getOrCreateSection(state.inboxDoc, "inbox", "* Inbox");
      newKeyword = "TODO";
    } else if (target === "now") {
      targetSec = nowSec;
      newKeyword = "NEXT";
    } else if (target === "waitlist") {
      targetSec = getOrCreateSection(state.agendaDoc, "waitlist", "* Waitlist");
      newKeyword = "WAITING";
    } else if (target === "done") {
      targetSec = getOrCreateSection(state.agendaDoc, "done", "* Done");
      newKeyword = "DONE";
    } else if (target === "cancelled") {
      targetSec = getOrCreateSection(state.agendaDoc, "done", "* Done");
      newKeyword = "CANCELLED";
    } else {
      targetSec = getOrCreateSection(state.agendaDoc, "someday", "* Someday / ideas");
      newKeyword = "TODO";
    }

    var item = loc.section.items.splice(loc.index, 1)[0];
    if (item.keyword !== newKeyword) {
      item.keyword = newKeyword;
      item.rawHeading = null;
    }

    var now = opts.now instanceof Date ? opts.now : new Date();
    if (target === "done") {
      setClosedLine(item, formatOrgDate(now));
    } else if (target === "cancelled") {
      setClosedLine(item, null);
      if (opts.reason) prependReasonIfNeeded(item, opts.reason);
    } else {
      // Moving back to an active list removes stale CLOSED line
      if (item.closed) setClosedLine(item, null);
      if (target === "waitlist" && opts.reason) {
        prependReasonIfNeeded(item, opts.reason);
      }
    }

    var insertAt;
    if (typeof opts.insertIndex === "number" && opts.insertIndex >= 0 && opts.insertIndex <= targetSec.items.length) {
      insertAt = opts.insertIndex;
    } else if (target === "done" || target === "cancelled") {
      // Newest completed items sit at the top of * Done
      insertAt = 0;
    } else {
      insertAt = targetSec.items.length;
    }
    targetSec.items.splice(insertAt, 0, item);

    var changedFiles = loc.file === targetFile ? [targetFile] : ["agenda", "inbox"];
    var verb = target === "done" ? "done" : target === "cancelled" ? "cancel" : "move to " + target;
    return {
      ok: true,
      item: item,
      changedFiles: changedFiles,
      commitMessage: "todo: " + verb + " — " + item.title.slice(0, 50)
    };
  }

  function updateItem(state, itemId, fields) {
    var loc = findItemLocation(state, itemId);
    if (!loc) return { ok: false, error: "NOT_FOUND" };
    var item = loc.item;

    if (fields && typeof fields.title === "string") {
      var t = fields.title.replace(/\s+/g, " ").trim();
      if (!t) return { ok: false, error: "EMPTY_TITLE" };
      if (item.title !== t) {
        item.title = t;
        item.rawHeading = null;
      }
    }
    if (fields && typeof fields.note === "string") {
      setNoteText(item, fields.note);
    }
    if (fields && Object.prototype.hasOwnProperty.call(fields, "scheduled")) {
      setScheduledLine(item, fields.scheduled || null);
    }
    return {
      ok: true,
      item: item,
      changedFiles: [loc.file],
      commitMessage: "todo: edit " + item.title.slice(0, 50)
    };
  }

  function parseOrgLinks(text) {
    // Splits text into safe segments: { type: "text" | "file" | "url", text, target? }
    var str = String(text || "");
    var out = [];
    var re = /\[\[([^\]]+)\](?:\[([^\]]+)\])?\]/g;
    var last = 0;
    var m;
    while ((m = re.exec(str)) !== null) {
      if (m.index > last) {
        out.push({ type: "text", text: str.slice(last, m.index) });
      }
      var target = m[1];
      var label = m[2] || target;
      if (/^file:/i.test(target)) {
        out.push({ type: "file", text: label.replace(/^file:/i, ""), target: target.replace(/^file:/i, "") });
      } else if (/^https?:\/\//i.test(target)) {
        out.push({ type: "url", text: label, target: target });
      } else {
        out.push({ type: "text", text: label });
      }
      last = re.lastIndex;
    }
    if (last < str.length) {
      out.push({ type: "text", text: str.slice(last) });
    }
    return out;
  }

  function getBoardSummary(state) {
    function itemsOf(doc, kind) {
      if (!doc) return [];
      for (var i = 0; i < doc.sections.length; i++) {
        if (doc.sections[i].kind === kind) return doc.sections[i].items;
      }
      return [];
    }
    return {
      now: itemsOf(state.agendaDoc, "now"),
      inbox: itemsOf(state.inboxDoc, "inbox"),
      waitlist: itemsOf(state.agendaDoc, "waitlist"),
      done: itemsOf(state.agendaDoc, "done"),
      someday: itemsOf(state.agendaDoc, "someday")
    };
  }

  return {
    KEYWORDS: KEYWORDS,
    MAX_NEXT: MAX_NEXT,
    formatOrgDate: formatOrgDate,
    formatOrgTimestamp: formatOrgTimestamp,
    formatScheduledTimestamp: formatScheduledTimestamp,
    parseOrgFile: parseOrgFile,
    serializeOrgFile: serializeOrgFile,
    addInboxItem: addInboxItem,
    moveItem: moveItem,
    updateItem: updateItem,
    findItemLocation: findItemLocation,
    findItemByFingerprint: findItemByFingerprint,
    fingerprintItem: fingerprintItem,
    parseOrgLinks: parseOrgLinks,
    getBoardSummary: getBoardSummary
  };
});
