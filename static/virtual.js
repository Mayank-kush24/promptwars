/* global Chart */

function qs(id) {
  return document.getElementById(id);
}

function fmtScore(v) {
  var n = Number(v);
  if (!isFinite(n)) return String(v);
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function renderDistChart(bins) {
  var el = qs("distChart");
  if (!el || typeof Chart === "undefined" || !bins || !bins.length) return;
  var fmt = function (n) { return Math.round(Number(n) * 100) / 100; };
  var labels = bins.map(function (b, i) {
    if (b && typeof b.low === "number" && typeof b.high === "number") {
      return fmt(b.low) + "–" + fmt(b.high);
    }
    return "Bin " + (i + 1);
  });
  var counts = bins.map(function (b) { return b.count || 0; });
  // eslint-disable-next-line no-new
  new Chart(el, {
    type: "bar",
    data: {
      labels: labels,
      datasets: [{
        label: "Participants per score bucket",
        data: counts,
        backgroundColor: "rgba(103, 80, 164, 0.45)",
        borderColor: "rgba(79, 55, 138, 1)",
        borderWidth: 1,
        borderRadius: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, ticks: { color: "#494551", precision: 0 }, grid: { color: "rgba(122, 117, 130, 0.15)" } },
        x: { ticks: { color: "#494551" }, grid: { display: false } },
      },
      plugins: { legend: { labels: { color: "#1d1b20" } } },
    },
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
  });
}

function rankCell(rank) {
  if (rank === 1) return '<span class="inline-flex items-center justify-center h-6 w-6 rounded-full bg-yellow-400/30 text-yellow-700 font-bold text-xs">1</span>';
  if (rank === 2) return '<span class="inline-flex items-center justify-center h-6 w-6 rounded-full bg-slate-300/50 text-slate-700 font-bold text-xs">2</span>';
  if (rank === 3) return '<span class="inline-flex items-center justify-center h-6 w-6 rounded-full bg-amber-700/20 text-amber-800 font-bold text-xs">3</span>';
  return '<span class="text-on-surface-variant">' + rank + "</span>";
}

async function refreshLeaderboard(challengeId) {
  var res;
  try {
    res = await fetch("/api/leaderboard?challenge_id=" + encodeURIComponent(String(challengeId)) + "&limit=50&offset=0");
  } catch (_e) { return; }
  if (!res.ok) return;
  var data = await res.json().catch(function () { return null; });
  if (!data) return;

  var tbody = document.querySelector("#lbTable tbody");
  if (!tbody) return;
  var rows = data.rows || [];
  var count = qs("lbCount");
  if (count) count.textContent = rows.length + " players";

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="py-lg text-center font-body-md text-body-md text-on-surface-variant">No participants yet.</td></tr>';
    return;
  }

  var html = "";
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    html +=
      '<tr class="border-b border-outline-variant/15 hover:bg-white/40 transition-colors">' +
      '<td class="py-2 pr-3 tabular-nums">' + rankCell(r.rank) + "</td>" +
      '<td class="py-2 pr-3 truncate max-w-[18rem]">' + escapeHtml(r.display_name) + "</td>" +
      '<td class="py-2 tabular-nums text-right font-medium">' + escapeHtml(fmtScore(r.score)) + "</td>" +
      "</tr>";
  }
  tbody.innerHTML = html;
}

window.addEventListener("DOMContentLoaded", function () {
  var cfg = window.__PW__ || {};
  renderDistChart(cfg.distBins || []);
  var challengeId = cfg.challengeId;
  if (!challengeId) return;
  window.setInterval(function () {
    refreshLeaderboard(challengeId);
  }, 4000);
});
