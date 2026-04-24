function qs(id) {
  return document.getElementById(id);
}

function fmtScore(v) {
  var n = Number(v);
  if (!isFinite(n)) return String(v);
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
  });
}

function initialsTeam(name) {
  var s = String(name || "").trim();
  if (!s.length) return "?";
  if (s.length === 1) return s.toUpperCase();
  var parts = s.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return s.slice(0, 2).toUpperCase();
}

function podiumSlot(entry, rank, champion) {
  if (!entry) {
    return '<div class="min-h-[128px] sm:min-h-[142px]" aria-hidden="true"></div>';
  }
  var wm = rank === 1 ? "text-amber-200" : rank === 2 ? "text-sky-200" : "text-orange-200";
  var box = champion
    ? "border-violet-300 bg-gradient-to-b from-violet-50/90 to-white shadow-lg shadow-violet-500/15 py-7 sm:py-9 min-h-[158px] sm:min-h-[178px] -mt-0.5 sm:-mt-2 z-[1]"
    : "border-slate-100/80 bg-gradient-to-b from-slate-50/40 to-white py-5 sm:py-6 min-h-[128px] sm:min-h-[142px]";
  var ini = escapeHtml(initialsTeam(entry.team_name));
  var team = escapeHtml(entry.team_name || "");
  var leader = escapeHtml(entry.leader_name || "—");
  var scoreBlock =
    entry.total_score == null || entry.total_score === ""
      ? "—"
      : escapeHtml(fmtScore(entry.total_score)) +
        ' <span class="text-xs font-semibold text-violet-600">pts</span>';
  return (
    '<div class="pw-lb-podium-slot relative flex flex-col items-center text-center rounded-2xl overflow-hidden border ' +
    box +
    '">' +
    '<span class="pointer-events-none absolute left-1/2 top-0 -translate-x-1/2 text-[4rem] sm:text-[5rem] font-black leading-none tracking-tighter select-none ' +
    wm +
    '" aria-hidden="true">' +
    rank +
    "</span>" +
    '<div class="relative mt-5 sm:mt-6 flex h-12 w-12 sm:h-14 sm:w-14 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-violet-500 to-indigo-600 text-sm sm:text-base font-bold text-white shadow-md ring-2 ring-white/80">' +
    ini +
    "</div>" +
    '<p class="relative mt-2.5 px-1 text-sm font-semibold text-slate-900 truncate max-w-full w-full">' +
    team +
    "</p>" +
    '<p class="relative text-[11px] text-slate-500 truncate max-w-full w-full px-1">' +
    leader +
    "</p>" +
    '<p class="relative mt-1 text-base sm:text-lg font-bold tabular-nums tracking-tight text-slate-900">' +
    scoreBlock +
    "</p>" +
    "</div>"
  );
}

function buildPodiumHtml(rows) {
  var p2 = rows.length > 1 ? rows[1] : null;
  var p1 = rows[0];
  var p3 = rows.length > 2 ? rows[2] : null;
  return (
    '<div class="grid grid-cols-3 gap-2 sm:gap-3 items-end mb-8 max-w-md sm:max-w-lg mx-auto" id="slbPodium" aria-label="Top three teams">' +
    podiumSlot(p2, 2, false) +
    podiumSlot(p1, 1, true) +
    podiumSlot(p3, 3, false) +
    "</div>"
  );
}

function buildListRowHtml(r) {
  var sub = r.leader_email
    ? '<span class="font-mono">' + escapeHtml(r.leader_email) + "</span>"
    : r.leader_name
      ? "<span>" + escapeHtml(r.leader_name) + "</span>"
      : "—";
  var sc =
    r.total_score == null || r.total_score === "" ? "—" : escapeHtml(fmtScore(r.total_score));
  return (
    '<div class="pw-lb-list-row flex items-center gap-3 sm:gap-4 py-3.5 sm:py-4 px-3 sm:px-4">' +
    '<span class="w-7 sm:w-8 shrink-0 text-center text-sm font-semibold text-slate-400 tabular-nums">' +
    r.rank +
    "</span>" +
    '<div class="flex h-10 w-10 sm:h-11 sm:w-11 shrink-0 items-center justify-center rounded-full bg-violet-100 text-xs sm:text-sm font-bold text-violet-700 ring-1 ring-violet-200/60">' +
    escapeHtml(initialsTeam(r.team_name)) +
    "</div>" +
    '<div class="flex-1 min-w-0">' +
    '<p class="font-semibold text-slate-900 truncate text-sm sm:text-base">' +
    escapeHtml(r.team_name || "") +
    "</p>" +
    '<p class="text-xs text-slate-500 truncate">' +
    sub +
    "</p>" +
    "</div>" +
    '<div class="shrink-0 text-right">' +
    '<p class="text-sm sm:text-base font-bold tabular-nums text-slate-900">' +
    sc +
    "</p>" +
    '<p class="text-[10px] sm:text-[11px] font-medium text-violet-600">pts</p>' +
    "</div>" +
    "</div>"
  );
}

function buildLeaderboardDom(rows) {
  if (!rows.length) {
    return '<div class="py-14 text-center text-sm text-slate-500">No submission rows yet for this challenge.</div>';
  }
  var html = buildPodiumHtml(rows);
  var rest = rows.slice(3);
  if (rest.length) {
    html += '<div id="slbList" class="rounded-xl border border-slate-100 divide-y divide-slate-100 overflow-hidden bg-white">';
    for (var i = 0; i < rest.length; i++) {
      html += buildListRowHtml(rest[i]);
    }
    html += "</div>";
  }
  return html;
}

function showSubmissionLbError(challengeId, status, detail) {
  var board = document.getElementById("slbBoard");
  var count = qs("slbCount");
  if (count) {
    count.textContent = status === 404 ? "—" : "—";
  }
  if (!board) return;
  var msg =
    status === 404
      ? "No virtual challenge with id <strong>#" +
        escapeHtml(String(challengeId)) +
        "</strong> for this event. Choose another challenge from <strong>Challenge eligibility</strong> below or open <strong>Manage challenges</strong>."
      : escapeHtml(detail || "Could not load submission leaderboard.");
  board.innerHTML =
    '<div class="rounded-xl border border-rose-200 bg-rose-50/80 text-rose-800 px-4 py-3 text-sm leading-relaxed">' +
    msg +
    "</div>";
}

/**
 * Apply API JSON to the live leaderboard DOM (main thread only).
 * @returns {boolean}
 */
function applyLeaderboardPayload(data) {
  if (!data) return false;
  var board = document.getElementById("slbBoard");
  if (!board) return false;
  var rows = data.rows || [];
  var count = qs("slbCount");
  var total = typeof data.total === "number" ? data.total : rows.length;
  if (count) count.textContent = total + " team(s)";
  board.innerHTML = buildLeaderboardDom(rows);
  return true;
}

/**
 * Fetch leaderboard on the main thread (fallback).
 * @returns {Promise<boolean>}
 */
async function refreshSubmissionLeaderboardFetch(challengeId, virtualEventId, baseUrl) {
  var url =
    baseUrl +
    "?challenge_id=" +
    encodeURIComponent(String(challengeId)) +
    "&virtualEventId=" +
    encodeURIComponent(String(virtualEventId)) +
    "&limit=50&offset=0";
  var res;
  try {
    res = await fetch(url);
  } catch (_e) {
    return false;
  }
  if (!res.ok) {
    var errText = "";
    try {
      var ej = await res.json();
      if (ej && ej.error) errText = String(ej.error);
    } catch (_e2) {
      /* ignore */
    }
    showSubmissionLbError(challengeId, res.status, errText || res.statusText);
    return false;
  }
  var data = await res.json().catch(function () {
    return null;
  });
  if (!data) return false;
  return applyLeaderboardPayload(data);
}

var __pwLbWorker = null;
var __pwLbWorkerPending = Object.create(null);
var __pwLbWorkerRid = 0;
var __pwLbWorkerBroken = false;

function initLeaderboardWorker(workerUrl) {
  if (__pwLbWorkerBroken || !workerUrl || typeof Worker === "undefined") return null;
  if (__pwLbWorker) return __pwLbWorker;
  try {
    __pwLbWorker = new Worker(workerUrl, { type: "classic" });
    __pwLbWorker.onmessage = function (ev) {
      var d = ev.data || {};
      if (d.type !== "LEADERBOARD_RESULT") return;
      var cb = __pwLbWorkerPending[d.requestId];
      delete __pwLbWorkerPending[d.requestId];
      if (typeof cb === "function") cb(d);
    };
    __pwLbWorker.onerror = function () {
      __pwLbWorkerBroken = true;
      try {
        __pwLbWorker.terminate();
      } catch (_e) {
        /* ignore */
      }
      __pwLbWorker = null;
    };
    return __pwLbWorker;
  } catch (_e) {
    __pwLbWorkerBroken = true;
    return null;
  }
}

/**
 * Ask the leaderboard worker to fetch; resolves with same shape as fetch path.
 * @returns {Promise<{ ok: boolean, status: number, data: object|null }>}
 */
function fetchLeaderboardViaWorker(challengeId, virtualEventId, baseUrl, workerUrl, timeoutMs) {
  var w = initLeaderboardWorker(workerUrl);
  if (!w) {
    return Promise.resolve({ ok: false, status: 0, data: null });
  }
  var id = ++__pwLbWorkerRid;
  return new Promise(function (resolve) {
    var t = window.setTimeout(function () {
      delete __pwLbWorkerPending[id];
      resolve({ ok: false, status: 0, data: null, timedOut: true });
    }, timeoutMs || 12000);
    __pwLbWorkerPending[id] = function (d) {
      window.clearTimeout(t);
      if (!d.ok) {
        var err = (d.json && d.json.error) || d.error || "";
        resolve({ ok: false, status: d.status || 0, data: null, errText: err });
        return;
      }
      resolve({ ok: true, status: d.status, data: d.json });
    };
    w.postMessage({
      type: "FETCH_LEADERBOARD",
      requestId: id,
      baseUrl: baseUrl,
      challengeId: challengeId,
      virtualEventId: virtualEventId,
    });
  });
}

/**
 * @returns {Promise<boolean>}
 */
async function refreshSubmissionLeaderboard(challengeId, virtualEventId, baseUrl, workerUrl, preferWorker) {
  if (preferWorker && workerUrl && !__pwLbWorkerBroken) {
    var wr = await fetchLeaderboardViaWorker(challengeId, virtualEventId, baseUrl, workerUrl, 12000);
    if (wr.ok && wr.data) {
      return applyLeaderboardPayload(wr.data);
    }
    if (!wr.timedOut && wr.status === 404) {
      var et404 = (wr.data && wr.data.error) || wr.errText || "";
      showSubmissionLbError(challengeId, 404, et404);
      return false;
    }
    if (!wr.timedOut && !wr.ok && wr.status) {
      var et = wr.errText || "";
      showSubmissionLbError(challengeId, wr.status, et);
      return false;
    }
    /* timeout or worker failure → fall through to fetch */
  }
  return refreshSubmissionLeaderboardFetch(challengeId, virtualEventId, baseUrl);
}

window.addEventListener("DOMContentLoaded", function () {
  var cfg = window.__PW__ || {};
  var challengeId = cfg.challengeId;
  var virtualEventId = cfg.virtualEventId;
  var baseUrl = cfg.submissionLeaderboardUrl || "/api/virtual/submission-leaderboard";
  var workerUrl = cfg.leaderboardWorkerUrl || "";
  var useWorker = cfg.useLeaderboardWorker !== false;
  if (!challengeId || virtualEventId == null) return;

  var pollMs = 4000;
  var timer = null;

  function stopPoll() {
    if (timer != null) {
      clearInterval(timer);
      timer = null;
    }
  }

  /* First paint: main-thread fetch is simplest; polls use worker when enabled. */
  refreshSubmissionLeaderboardFetch(challengeId, virtualEventId, baseUrl).then(function (ok) {
    if (!ok) return;
    timer = window.setInterval(function () {
      refreshSubmissionLeaderboard(challengeId, virtualEventId, baseUrl, workerUrl, useWorker).then(function (stillOk) {
        if (!stillOk) stopPoll();
      });
    }, pollMs);
  });
});
