/* Submission Analytics: occupation donut + attempt bar charts (Virtual + In-person). */
function pwBuildUsersArenaAnalyticsUrl(params) {
  var routes = window.__PW_ROUTES__ || {};
  var cfg = window.__PW__ || {};
  var mode = (cfg.analyticsMode || "virtual").toLowerCase();
  var base =
    mode === "in_person"
      ? routes.inPersonUsersUrl || routes.mdcUsersUrl || "/in-person/users"
      : routes.mdcUsersUrl || "/virtual/users";
  var q = new URLSearchParams();
  if (mode === "virtual" && cfg.virtualEventId != null && String(cfg.virtualEventId) !== "") {
    q.set("virtualEventId", String(cfg.virtualEventId));
  }
  if (mode === "virtual" && cfg.activeChallengeId != null && String(cfg.activeChallengeId) !== "") {
    q.set("challengeId", String(cfg.activeChallengeId));
  }
  if (mode === "in_person" && cfg.inPersonEventId != null && String(cfg.inPersonEventId) !== "") {
    q.set("inPersonEventId", String(cfg.inPersonEventId));
  }
  if (params) {
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v != null && v !== "") q.set(k, String(v));
    });
  }
  var qs = q.toString();
  return qs ? base + "?" + qs : base;
}

window.pwBuildVirtualUsersArenaUrl = pwBuildUsersArenaAnalyticsUrl;

window.addEventListener("DOMContentLoaded", function () {
  var cfg = window.__PW__ || {};
  var seg = cfg.arenaTeamSegments;
  var host = document.getElementById("pwArenaSegmentHost");
  var canvas = document.getElementById("pwArenaSegmentDonut");
  if (!host || !canvas || typeof Chart === "undefined") return;
  if (!seg) return;

  var mode = (cfg.analyticsMode || "virtual").toLowerCase();
  var isIp = mode === "in_person";

  var labels = ["Student", "Professional", "Other", "Unknown"];
  var data = [
    Number(seg.student) || 0,
    Number(seg.professional) || 0,
    Number(seg.other) || 0,
    Number(seg.unknown) || 0,
  ];
  var sum = data.reduce(function (a, b) {
    return a + b;
  }, 0);
  if (!sum) {
    host.innerHTML =
      '<p class="text-sm text-slate-500 py-12 text-center">No team rows for this challenge yet.</p>';
    return;
  }

  var segmentKeys = ["student", "professional", "other", "unknown"];

  function donutOpts() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      onHover: function (evt, els) {
        if (evt.native && evt.native.target) {
          evt.native.target.style.cursor = els && els.length ? "pointer" : "default";
        }
      },
      plugins: {
        legend: {
          position: "bottom",
          labels: { boxWidth: 10, usePointStyle: true, font: { size: 11 } },
        },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var v = ctx.parsed;
              var n = typeof v === "number" ? v : v != null ? v : 0;
              var pct = sum ? ((100 * n) / sum).toFixed(1) : "0";
              return " " + n.toLocaleString() + " (" + pct + "%)";
            },
            afterBody: function () {
              return isIp
                ? "Click to open In-person · Users with this cohort."
                : "Click to open Virtual · Users with this cohort.";
            },
          },
        },
      },
      cutout: "58%",
      onClick: function (evt, elements) {
        if (!elements || !elements.length) return;
        var idx = elements[0].index;
        var segKey = segmentKeys[idx];
        if (!segKey) return;
        if (isIp) {
          var tok = (cfg.submissionSessionToken || "").trim();
          if (!tok) return;
          window.location.href = pwBuildUsersArenaAnalyticsUrl({
            submission_session: tok,
            arenaTeamSegment: segKey,
          });
        } else {
          if (!cfg.challengeId) return;
          window.location.href = pwBuildUsersArenaAnalyticsUrl({
            arenaChallengeId: String(cfg.challengeId),
            arenaTeamSegment: segKey,
          });
        }
      },
    };
  }

  var colors = isIp
    ? [
        "rgba(22, 163, 74, 0.9)",
        "rgba(13, 148, 136, 0.88)",
        "rgba(148, 163, 184, 0.9)",
        "rgba(251, 191, 36, 0.85)",
      ]
    : [
        "rgba(109, 40, 217, 0.9)",
        "rgba(59, 130, 246, 0.88)",
        "rgba(148, 163, 184, 0.9)",
        "rgba(251, 191, 36, 0.85)",
      ];
  new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: labels,
      datasets: [
        {
          data: data,
          backgroundColor: colors,
          borderColor: "#ffffff",
          borderWidth: 2,
          hoverOffset: 6,
        },
      ],
    },
    options: donutOpts(),
  });
});

window.addEventListener("DOMContentLoaded", function () {
  var stEl = document.getElementById("pwAttemptFunnelStudentsChart");
  var prEl = document.getElementById("pwAttemptFunnelProfessionalsChart");
  if (!stEl && !prEl) return;
  if (typeof Chart === "undefined") return;

  var cfg = window.__PW__ || {};
  var mode = (cfg.analyticsMode || "virtual").toLowerCase();
  var isIp = mode === "in_person";
  var bucketsSt = cfg.attemptBucketsStudent || [];
  var bucketsPr = cfg.attemptBucketsProfessional || [];

  var barFill = isIp ? "rgba(22, 163, 74, 0.75)" : "rgba(109, 40, 217, 0.75)";
  var barBorder = isIp ? "rgba(21, 128, 61, 0.9)" : "rgba(91, 33, 182, 0.9)";

  function barEndValueLabels() {
    return {
      id: "pwBarEndValueLabels",
      afterDatasetsDraw: function (chart) {
        var ctx = chart.ctx;
        var ds0 = chart.data.datasets[0];
        var meta = chart.getDatasetMeta(0);
        if (!meta || !meta.data || !ds0) return;
        ctx.save();
        ctx.font = "600 12px system-ui, -apple-system, Segoe UI, sans-serif";
        ctx.fillStyle = "#0f172a";
        ctx.textBaseline = "middle";
        meta.data.forEach(function (elem, i) {
          var v = Number(ds0.data[i]);
          if (!isFinite(v) || v <= 0) return;
          var x = elem.x;
          var base = elem.base;
          var y = elem.y;
          var xEnd = typeof x === "number" && typeof base === "number" ? Math.max(x, base) : x;
          ctx.textAlign = "left";
          ctx.fillText(v.toLocaleString(), xEnd + 8, y);
        });
        ctx.restore();
      },
    };
  }

  function barOpts(onBarClick) {
    return {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { right: 72 } },
      onHover: function (evt, els) {
        if (evt.native && evt.native.target) {
          evt.native.target.style.cursor = els && els.length ? "pointer" : "default";
        }
      },
      onClick: function (evt, elements) {
        if (typeof onBarClick === "function") onBarClick(evt, elements);
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              return " " + (ctx.parsed.x != null ? ctx.parsed.x : 0).toLocaleString() + " teams";
            },
            afterBody: function () {
              return isIp
                ? "Click to filter In-person · Users by this cohort and attempt count."
                : "Click to filter Virtual · Users by this cohort and attempt count.";
            },
          },
        },
      },
      scales: {
        x: {
          beginAtZero: true,
          ticks: { precision: 0, color: "#64748b" },
          grid: { color: "rgba(148, 163, 184, 0.25)" },
        },
        y: {
          ticks: { color: "#475569", font: { size: 11, weight: "600" } },
          grid: { display: false },
        },
      },
    };
  }

  function sumBuckets(buckets) {
    return (buckets || []).reduce(function (a, b) {
      return a + (Number(b.count) || 0);
    }, 0);
  }

  function mkLabels(buckets) {
    return (buckets || []).map(function (b) {
      var lab = String(b.label);
      if (lab === "0") return "Not reported";
      return lab + (lab === "1" ? " attempt" : " attempts");
    });
  }

  function mkValues(buckets) {
    return (buckets || []).map(function (b) {
      return Number(b.count) || 0;
    });
  }

  function renderBar(canvasId, buckets, hintId, teamSegment) {
    var el = document.getElementById(canvasId);
    if (!el) return;
    var hint = hintId ? document.getElementById(hintId) : null;
    if (sumBuckets(buckets) === 0) {
      if (hint) {
        hint.textContent = isIp
          ? "No attempt counts yet for this segment (import on In-person · Import: Action Center or Challenge attempt counts)."
          : "No attempt counts yet for this segment (import attempts on Virtual · Import).";
        hint.classList.remove("hidden");
      }
      return;
    }
    if (hint) {
      hint.classList.add("hidden");
    }
    var bList = buckets || [];
    function onBarClick(_evt, elements) {
      if (!elements || !elements.length) return;
      var i = elements[0].index;
      var row = bList[i];
      if (!row) return;
      var ac = String(row.label != null ? row.label : "").trim();
      if (!ac) return;
      if (isIp) {
        var tok = (cfg.submissionSessionToken || "").trim();
        if (!tok) return;
        window.location.href = pwBuildUsersArenaAnalyticsUrl({
          submission_session: tok,
          arenaTeamSegment: teamSegment,
          arenaAttemptsCompleted: ac,
        });
      } else {
        if (!cfg.challengeId) return;
        window.location.href = pwBuildUsersArenaAnalyticsUrl({
          arenaChallengeId: String(cfg.challengeId),
          arenaTeamSegment: teamSegment,
          arenaAttemptsCompleted: ac,
        });
      }
    }
    new Chart(el, {
      type: "bar",
      plugins: [barEndValueLabels()],
      data: {
        labels: mkLabels(buckets),
        datasets: [
          {
            label: "Teams",
            data: mkValues(buckets),
            backgroundColor: barFill,
            borderColor: barBorder,
            borderWidth: 1,
            borderRadius: 6,
          },
        ],
      },
      options: barOpts(onBarClick),
    });
  }

  renderBar("pwAttemptFunnelStudentsChart", bucketsSt, "pwAttemptFunnelStudentsHint", "student");
  renderBar("pwAttemptFunnelProfessionalsChart", bucketsPr, "pwAttemptFunnelProfessionalsHint", "professional");
});
