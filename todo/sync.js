/* GitHub Contents + Git Data API sync layer for private org files
 * (agenda.org & inbox.org). Stores the fine-grained PAT only in the
 * browser's localStorage. Zero external dependencies.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.TodoSync = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var STORAGE_KEY = "planner_todo_github_cfg_v1";
  var DEFAULT_CFG = {
    owner: "yyoon62",
    repo: "notes",
    branch: "main",
    token: ""
  };

  function encodeBase64Utf8(str) {
    if (typeof Buffer !== "undefined") {
      return Buffer.from(str, "utf8").toString("base64");
    }
    var bytes = new TextEncoder().encode(str);
    var bin = "";
    var chunk = 0x8000;
    for (var i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(bin);
  }

  function decodeBase64Utf8(b64) {
    var clean = String(b64 || "").replace(/\s+/g, "");
    if (typeof Buffer !== "undefined") {
      return Buffer.from(clean, "base64").toString("utf8");
    }
    var bin = atob(clean);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) {
      bytes[i] = bin.charCodeAt(i);
    }
    return new TextDecoder("utf-8").decode(bytes);
  }

  function loadConfig() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return Object.assign({}, DEFAULT_CFG);
      var parsed = JSON.parse(raw);
      return {
        owner: (parsed.owner || DEFAULT_CFG.owner).trim(),
        repo: (parsed.repo || DEFAULT_CFG.repo).trim(),
        branch: (parsed.branch || DEFAULT_CFG.branch).trim(),
        token: (parsed.token || "").trim()
      };
    } catch (_e) {
      return Object.assign({}, DEFAULT_CFG);
    }
  }

  function saveConfig(cfg) {
    var clean = {
      owner: (cfg.owner || DEFAULT_CFG.owner).trim(),
      repo: (cfg.repo || DEFAULT_CFG.repo).trim(),
      branch: (cfg.branch || DEFAULT_CFG.branch).trim(),
      token: (cfg.token || "").trim()
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(clean));
    return clean;
  }

  function clearConfig() {
    localStorage.removeItem(STORAGE_KEY);
  }

  function makeError(message, code, status) {
    var err = new Error(message);
    err.code = code || "ERROR";
    err.status = status || 0;
    return err;
  }

  function ghFetch(cfg, path, opts, fetchImpl) {
    var fn = fetchImpl || fetch;
    var url = "https://api.github.com/repos/" +
      encodeURIComponent(cfg.owner) + "/" +
      encodeURIComponent(cfg.repo) + path;

    var headers = {
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Authorization": "Bearer " + cfg.token
    };
    var init = {
      method: (opts && opts.method) || "GET",
      headers: headers,
      cache: "no-store"
    };
    if (opts && opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }

    return fn(url, init).then(function (res) {
      return res.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (_e) { data = null; }
        if (res.ok) return data;
        var apiMsg = (data && data.message) || ("HTTP " + res.status);
        if (res.status === 401) {
          throw makeError("GitHub token rejected. Check that it hasn't expired.", "AUTH", 401);
        }
        if (res.status === 403) {
          throw makeError("Permission denied (" + apiMsg + "). Token needs Contents read & write on " + cfg.owner + "/" + cfg.repo + ".", "FORBIDDEN", 403);
        }
        if (res.status === 404) {
          throw makeError("Not found in " + cfg.owner + "/" + cfg.repo + " (" + apiMsg + ").", "NOT_FOUND", 404);
        }
        if (res.status === 409 || res.status === 422) {
          throw makeError("Remote file changed since last sync (" + apiMsg + ").", "CONFLICT", res.status);
        }
        throw makeError("GitHub error: " + apiMsg, "API", res.status);
      });
    });
  }

  function fetchFile(cfg, filePath, fetchImpl) {
    var q = "?ref=" + encodeURIComponent(cfg.branch);
    return ghFetch(cfg, "/contents/" + encodeURIComponent(filePath) + q, null, fetchImpl).then(function (data) {
      return {
        path: filePath,
        sha: data.sha,
        text: decodeBase64Utf8(data.content || "")
      };
    });
  }

  function fetchOrgFiles(cfg, fetchImpl) {
    if (!cfg || !cfg.token) {
      return Promise.reject(makeError("No GitHub token configured.", "NO_TOKEN", 0));
    }
    var refPath = "/git/ref/heads/" + encodeURIComponent(cfg.branch);
    return Promise.all([
      ghFetch(cfg, refPath, null, fetchImpl),
      fetchFile(cfg, "agenda.org", fetchImpl),
      fetchFile(cfg, "inbox.org", fetchImpl)
    ]).then(function (results) {
      return {
        headSha: results[0] && results[0].object ? results[0].object.sha : null,
        agenda: results[1],
        inbox: results[2],
        fetchedAt: new Date()
      };
    });
  }

  function commitSingleFile(cfg, file, message, fetchImpl) {
    return ghFetch(cfg, "/contents/" + encodeURIComponent(file.path), {
      method: "PUT",
      body: {
        message: message,
        content: encodeBase64Utf8(file.text),
        sha: file.sha,
        branch: cfg.branch
      }
    }, fetchImpl).then(function (data) {
      var shas = {};
      shas[file.path] = data.content ? data.content.sha : file.sha;
      return {
        headSha: data.commit ? data.commit.sha : null,
        fileShas: shas
      };
    });
  }

  function commitAtomicMultiFile(cfg, files, expectedHeadSha, message, fetchImpl) {
    var refPath = "/git/ref/heads/" + encodeURIComponent(cfg.branch);
    // 1. Verify branch HEAD hasn't moved since we read it
    return ghFetch(cfg, refPath, null, fetchImpl).then(function (refData) {
      var currentHead = refData && refData.object ? refData.object.sha : null;
      if (expectedHeadSha && currentHead !== expectedHeadSha) {
        throw makeError("Branch advanced remotely before save.", "CONFLICT", 409);
      }
      var parentSha = currentHead || expectedHeadSha;
      // 2. Get base tree from HEAD commit
      return ghFetch(cfg, "/git/commits/" + parentSha, null, fetchImpl).then(function (commitData) {
        var baseTreeSha = commitData.tree.sha;
        // 3. Create new tree with updated files
        var treeEntries = files.map(function (f) {
          return {
            path: f.path,
            mode: "100644",
            type: "blob",
            content: f.text
          };
        });
        return ghFetch(cfg, "/git/trees", {
          method: "POST",
          body: {
            base_tree: baseTreeSha,
            tree: treeEntries
          }
        }, fetchImpl).then(function (treeData) {
          // 4. Create commit pointing to new tree
          return ghFetch(cfg, "/git/commits", {
            method: "POST",
            body: {
              message: message,
              tree: treeData.sha,
              parents: [parentSha]
            }
          }, fetchImpl).then(function (newCommit) {
            // 5. Fast-forward branch ref (force: false guarantees atomicity)
            return ghFetch(cfg, "/git/refs/heads/" + encodeURIComponent(cfg.branch), {
              method: "PATCH",
              body: {
                sha: newCommit.sha,
                force: false
              }
            }, fetchImpl).then(function () {
              var fileShas = {};
              if (treeData.tree && Array.isArray(treeData.tree)) {
                treeData.tree.forEach(function (entry) {
                  files.forEach(function (f) {
                    if (entry.path === f.path) fileShas[f.path] = entry.sha;
                  });
                });
              }
              return {
                headSha: newCommit.sha,
                fileShas: fileShas
              };
            });
          });
        });
      });
    });
  }

  function commitChanges(cfg, files, expectedHeadSha, message, fetchImpl) {
    if (!files || files.length === 0) {
      return Promise.resolve({ headSha: expectedHeadSha, fileShas: {} });
    }
    if (files.length === 1) {
      return commitSingleFile(cfg, files[0], message, fetchImpl);
    }
    return commitAtomicMultiFile(cfg, files, expectedHeadSha, message, fetchImpl);
  }

  return {
    DEFAULT_CFG: DEFAULT_CFG,
    encodeBase64Utf8: encodeBase64Utf8,
    decodeBase64Utf8: decodeBase64Utf8,
    loadConfig: loadConfig,
    saveConfig: saveConfig,
    clearConfig: clearConfig,
    fetchOrgFiles: fetchOrgFiles,
    commitChanges: commitChanges
  };
});
