/**
 * Web worker: polls virtual submission leaderboard JSON off the main thread.
 */
/* eslint-disable no-restricted-globals */
self.onmessage = function (ev) {
  var msg = ev.data || {};
  if (msg.type !== "FETCH_LEADERBOARD") return;

  var baseUrl = msg.baseUrl;
  var challengeId = msg.challengeId;
  var virtualEventId = msg.virtualEventId;
  var requestId = msg.requestId;

  var url =
    baseUrl +
    "?challenge_id=" +
    encodeURIComponent(String(challengeId)) +
    "&virtualEventId=" +
    encodeURIComponent(String(virtualEventId)) +
    "&limit=50&offset=0";

  fetch(url)
    .then(function (res) {
      return res.text().then(function (text) {
        return { ok: res.ok, status: res.status, text: text };
      });
    })
    .then(function (r) {
      var json = null;
      if (r.text) {
        try {
          json = JSON.parse(r.text);
        } catch (_e) {
          json = null;
        }
      }
      self.postMessage({
        type: "LEADERBOARD_RESULT",
        requestId: requestId,
        ok: r.ok,
        status: r.status,
        json: json,
      });
    })
    .catch(function (err) {
      self.postMessage({
        type: "LEADERBOARD_RESULT",
        requestId: requestId,
        ok: false,
        status: 0,
        json: null,
        error: String((err && err.message) || err),
      });
    });
};
