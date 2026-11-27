"use strict";

var assert = require("assert");
var TodoSync = require("../todo/sync.js");

function makeResponse(status, body) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status: status,
    text: function () { return Promise.resolve(JSON.stringify(body)); }
  });
}

var cfg = { owner: "demo", repo: "notes", branch: "main", token: "ghp_fake" };

async function run() {
  // 1. UTF-8 Base64 round-trip (emoji, arrows, CJK)
  var unicode = "DONE Book JFK ↔ ICN ✈️ 서울 / 東京";
  var b64 = TodoSync.encodeBase64Utf8(unicode);
  assert.strictEqual(TodoSync.decodeBase64Utf8(b64), unicode);
  console.log("PASS encodeBase64Utf8 / decodeBase64Utf8 handles Unicode");

  // 2. fetchOrgFiles reads ref + agenda.org + inbox.org
  var fetchCalls = [];
  var mockFetch = function (url, init) {
    fetchCalls.push({ url: url, method: init.method });
    if (url.indexOf("/git/ref/heads/main") !== -1) {
      return makeResponse(200, { object: { sha: "head_1" } });
    }
    if (url.indexOf("/contents/agenda.org") !== -1) {
      return makeResponse(200, { sha: "sha_a1", content: TodoSync.encodeBase64Utf8("* Now (NEXT)\n") });
    }
    if (url.indexOf("/contents/inbox.org") !== -1) {
      return makeResponse(200, { sha: "sha_i1", content: TodoSync.encodeBase64Utf8("* Inbox\n") });
    }
    return makeResponse(404, { message: "not found" });
  };
  var fetched = await TodoSync.fetchOrgFiles(cfg, mockFetch);
  assert.strictEqual(fetched.headSha, "head_1");
  assert.strictEqual(fetched.agenda.sha, "sha_a1");
  assert.strictEqual(fetched.inbox.sha, "sha_i1");
  assert.strictEqual(fetched.agenda.text, "* Now (NEXT)\n");
  console.log("PASS fetchOrgFiles fetches headSha, agenda.org, and inbox.org");

  // 3. Single-file commit uses Contents PUT and detects 409 conflict
  var putBody = null;
  var singleFetch = function (url, init) {
    putBody = JSON.parse(init.body);
    return makeResponse(200, {
      content: { sha: "sha_i2" },
      commit: { sha: "head_2" }
    });
  };
  var singleRes = await TodoSync.commitChanges(
    cfg,
    [{ path: "inbox.org", text: "* Inbox\n** TODO Test\n", sha: "sha_i1" }],
    "head_1",
    "inbox: add Test",
    singleFetch
  );
  assert.strictEqual(singleRes.headSha, "head_2");
  assert.strictEqual(singleRes.fileShas["inbox.org"], "sha_i2");
  assert.strictEqual(TodoSync.decodeBase64Utf8(putBody.content), "* Inbox\n** TODO Test\n");
  console.log("PASS commitChanges single file uses Contents PUT");

  var conflictCaught = false;
  try {
    await TodoSync.commitChanges(
      cfg,
      [{ path: "inbox.org", text: "x", sha: "stale" }],
      "head_1",
      "msg",
      function () { return makeResponse(409, { message: "does not match" }); }
    );
  } catch (e) {
    conflictCaught = e.code === "CONFLICT";
  }
  assert.strictEqual(conflictCaught, true);
  console.log("PASS commitChanges surfaces 409 as CONFLICT");

  // 4. Multi-file atomic commit via Git Data API
  var ops = [];
  var multiFetch = function (url, init) {
    ops.push(init.method + " " + url.split("/repos/demo/notes")[1]);
    if (init.method === "GET" && url.indexOf("/git/ref/heads/main") !== -1) {
      return makeResponse(200, { object: { sha: "head_2" } });
    }
    if (init.method === "GET" && url.indexOf("/git/commits/head_2") !== -1) {
      return makeResponse(200, { tree: { sha: "tree_2" } });
    }
    if (init.method === "POST" && url.indexOf("/git/trees") !== -1) {
      return makeResponse(201, {
        sha: "tree_3",
        tree: [
          { path: "agenda.org", sha: "sha_a3" },
          { path: "inbox.org", sha: "sha_i3" }
        ]
      });
    }
    if (init.method === "POST" && url.indexOf("/git/commits") !== -1) {
      return makeResponse(201, { sha: "head_3" });
    }
    if (init.method === "PATCH" && url.indexOf("/git/refs/heads/main") !== -1) {
      return makeResponse(200, { object: { sha: "head_3" } });
    }
    return makeResponse(500, { message: "unexpected " + url });
  };
  var multiRes = await TodoSync.commitChanges(
    cfg,
    [
      { path: "agenda.org", text: "new agenda", sha: "sha_a1" },
      { path: "inbox.org", text: "new inbox", sha: "sha_i2" }
    ],
    "head_2",
    "todo: move inbox item to done",
    multiFetch
  );
  assert.strictEqual(multiRes.headSha, "head_3");
  assert.strictEqual(multiRes.fileShas["agenda.org"], "sha_a3");
  assert.strictEqual(multiRes.fileShas["inbox.org"], "sha_i3");
  assert.deepStrictEqual(ops, [
    "GET /git/ref/heads/main",
    "GET /git/commits/head_2",
    "POST /git/trees",
    "POST /git/commits",
    "PATCH /git/refs/heads/main"
  ]);
  console.log("PASS commitChanges multi-file executes atomic Git Data API commit");

  // 5. Multi-file commit rejects stale HEAD before creating tree/commit
  var staleCaught = false;
  try {
    await TodoSync.commitChanges(
      cfg,
      [
        { path: "agenda.org", text: "a", sha: "s1" },
        { path: "inbox.org", text: "b", sha: "s2" }
      ],
      "head_old",
      "msg",
      function () { return makeResponse(200, { object: { sha: "head_newer" } }); }
    );
  } catch (e) {
    staleCaught = e.code === "CONFLICT";
  }
  assert.strictEqual(staleCaught, true);
  console.log("PASS commitChanges multi-file detects advanced HEAD as CONFLICT");

  console.log("All 6 todo_sync tests passed.");
}

run().catch(function (err) {
  console.error("FAIL", err && err.stack || err);
  process.exit(1);
});
