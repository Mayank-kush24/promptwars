/* global Chart, echarts */
window.addEventListener("DOMContentLoaded", function () {
  var dc = window.__PW_MDC__ || {};
  if (dc.error) return;

  var tzNote = "Times use Asia/Kolkata (IST).";

  var pwModule = (document.body && document.body.getAttribute("data-pw-module")) || "";
  var THEME = (function () {
    if (pwModule === "virtual") {
      return {
        line:    { border: "rgb(124, 58, 237)",  bg: "rgba(167, 139, 250, 0.18)" },
        hourly:  { fill: "rgba(167, 139, 250, 0.92)", border: "rgba(109, 40, 217, 0.35)" },
        bar:     { fill: "rgba(139, 92, 246, 0.78)",  border: "rgba(109, 40, 217, 0.45)" },
        usersHint: "Virtual · Users",
      };
    }
    if (pwModule === "in_person") {
      return {
        line:    { border: "rgb(101, 163, 13)",  bg: "rgba(132, 204, 22, 0.18)" },
        hourly:  { fill: "rgba(190, 232, 79, 0.92)", border: "rgba(132, 204, 22, 0.45)" },
        bar:     { fill: "rgba(132, 204, 22, 0.85)", border: "rgba(101, 163, 13, 0.45)" },
        usersHint: "In-person · Users",
      };
    }
    return {
      line:    { border: "rgb(5, 150, 105)",  bg: "rgba(16, 185, 129, 0.12)" },
      hourly:  { fill: "rgba(196, 214, 106, 0.92)", border: "rgba(118, 132, 56, 0.35)" },
      bar:     { fill: "rgba(16, 185, 129, 0.78)",  border: "rgba(5, 150, 105, 0.45)" },
      usersHint: "Users",
    };
  })();

  function mkLine() {
    var el = document.getElementById("mdcTimelineChart");
    if (!el || typeof Chart === "undefined") return;
    var labels = dc.timeline_labels || [];
    var data = dc.timeline_counts || [];
    if (!labels.length) {
      el.parentElement.innerHTML =
        '<p class="text-sm text-slate-500 py-8 text-center">No registration timestamps yet for the timeline.</p>';
      return;
    }
    new Chart(el, {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          label: "Registrations per day",
          data: data,
          borderColor: THEME.line.border,
          backgroundColor: THEME.line.bg,
          fill: true,
          tension: 0.25,
          pointRadius: 2,
          pointHoverRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            ticks: { maxTicksLimit: 12, color: "#64748b" },
            grid: { display: false },
          },
          y: { beginAtZero: true, ticks: { color: "#64748b" }, grid: { color: "rgba(100,116,139,0.12)" } },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (items) {
                var i = items[0] && items[0].dataIndex;
                return i != null ? labels[i] : "";
              },
            },
          },
        },
      },
    });
  }

  function mkHourly() {
    var el = document.getElementById("mdcHourlyChart");
    if (!el || typeof Chart === "undefined") return;
    var hours = [];
    for (var h = 0; h < 24; h++) hours.push(String(h).padStart(2, "0") + ":00");
    var data = dc.hourly_counts || [];
    while (data.length < 24) data.push(0);
    new Chart(el, {
      type: "bar",
      data: {
        labels: hours,
        datasets: [{
          label: "Registrations",
          data: data.slice(0, 24),
          backgroundColor: THEME.hourly.fill,
          borderColor: THEME.hourly.border,
          borderWidth: 1,
          borderRadius: 4,
          borderSkipped: false,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { ticks: { maxRotation: 90, minRotation: 45, font: { size: 9 }, color: "#64748b" }, grid: { display: false } },
          y: { beginAtZero: true, ticks: { stepSize: 1, color: "#64748b" }, grid: { color: "rgba(100,116,139,0.12)" } },
        },
        plugins: {
          legend: { display: false },
          title: { display: true, text: "Registrations by hour of day (IST)", color: "#334155", font: { size: 13 } },
        },
      },
    });
  }

  function mkAttendanceCityBar() {
    var el = document.getElementById("mdcAttendanceCityBar");
    var host = document.getElementById("mdcAttendanceCityChartHost");
    if (!el || !host || typeof Chart === "undefined") return;
    var rows = dc.attendance_cities || [];
    if (!rows.length) {
      host.innerHTML =
        '<p class="text-sm text-slate-500 py-8 text-center">No rows with <code class="rounded bg-slate-100 px-1">attendance_city</code> filled yet.</p>';
      return;
    }
    var n = rows.length;
    var chartH = Math.max(280, Math.min(8000, n * 26 + 100));
    host.style.height = chartH + "px";
    var labels = rows.map(function (r) {
      return String(r.city || "");
    });
    var vals = rows.map(function (r) {
      return Number(r.count) || 0;
    });
    var attendanceCityValueLabels = {
      id: "mdcAttendanceCityValueLabels",
      afterDatasetsDraw: function (chart) {
        var ctx = chart.ctx;
        var meta = chart.getDatasetMeta(0);
        if (!meta || !meta.data || !meta.data.length) return;
        var area = chart.chartArea;
        if (!area) return;
        ctx.save();
        ctx.font = "600 11px system-ui, -apple-system, Segoe UI, sans-serif";
        ctx.fillStyle = "#334155";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        meta.data.forEach(function (bar, i) {
          if (!bar) return;
          var v = vals[i];
          if (v == null) return;
          var text = String(v);
          var pad = 6;
          ctx.textAlign = "left";
          var x = bar.x + pad;
          var y = bar.y;
          if (x + ctx.measureText(text).width > area.right - 2) {
            ctx.textAlign = "right";
            x = bar.x - pad;
            ctx.fillStyle = "#f8fafc";
          } else {
            ctx.fillStyle = "#334155";
          }
          ctx.fillText(text, x, y);
        });
        ctx.restore();
      },
    };
    var barFill = THEME.bar.fill;
    var barBorder = THEME.bar.border;
    var usersHint = THEME.usersHint;
    new Chart(el, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Registrations",
            data: vals,
            backgroundColor: barFill,
            borderColor: barBorder,
            borderWidth: 1,
            borderRadius: 4,
            borderSkipped: false,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        layout: { padding: { right: 36 } },
        scales: {
          x: {
            beginAtZero: true,
            ticks: { color: "#64748b" },
            grid: { color: "rgba(100,116,139,0.12)" },
          },
          y: {
            ticks: { color: "#334155", font: { size: 11 }, autoSkip: false },
            grid: { display: false },
          },
        },
        plugins: {
          legend: { display: false },
          title: {
            display: true,
            text: "By attendance_city (all) — click a bar to open " + usersHint + " with this city",
            color: "#334155",
            font: { size: 13 },
          },
        },
        onHover: function (_evt, elements, chart) {
          if (chart && chart.canvas) {
            chart.canvas.style.cursor = elements && elements.length ? "pointer" : "default";
          }
        },
        onClick: function (_evt, elements) {
          if (!elements || !elements.length) return;
          var i = elements[0].index;
          var city = labels[i];
          if (city === undefined || city === null || city === "") return;
          var base =
            (window.__PW_ROUTES__ && window.__PW_ROUTES__.mdcUsersUrl) ||
            (window.__PW_ROUTES__ && window.__PW_ROUTES__.inPersonUsers) ||
            "/in-person/users";
          try {
            var u = new URL(base, window.location.href);
            u.searchParams.set("page", "1");
            u.searchParams.set("attendance_city", String(city));
            window.location.href = u.pathname + u.search;
          } catch (_e) {
            var sep = base.indexOf("?") >= 0 ? "&" : "?";
            window.location.href =
              base +
              sep +
              "page=1&attendance_city=" +
              encodeURIComponent(String(city));
          }
        },
      },
      plugins: [attendanceCityValueLabels],
    });
  }

  function normState(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/_/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  /** Map normalized export labels → Highcharts / OSM admin-1 `properties.name` spellings */
  var EXPORT_TO_MAP_NAME = {
    orissa: "Odisha",
    "uttaranchal": "Uttarakhand",
    "nct of delhi": "Delhi",
    "pondicherry": "Puducherry",
    "dadra and nagar haveli": "Dadra and Nagar Haveli and Daman and Diu",
    "daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "dadra and nagar haveli and daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "jammu & kashmir": "Jammu and Kashmir",
    "jammu and kashmir": "Jammu and Kashmir",
    "d and n haveli": "Dadra and Nagar Haveli and Daman and Diu",
  };

  function mkIndiaMap() {
    var host = document.getElementById("mdcIndiaMap");
    if (!host || typeof echarts === "undefined") return;
    host.innerHTML = "";
    var stateRows = dc.state_distribution || [];
    if (!stateRows.length) {
      host.innerHTML =
        '<p class="text-sm text-slate-500 p-6 text-center">No <code class="rounded bg-slate-100 px-1">state</code> totals for the heat map. Check the city pivot below or ensure exports include state.</p>';
      return;
    }

    var urls = ["/static/geo/in-all-claimed.geo.json"];

    function loadGeo(url) {
      return fetch(url).then(function (r) {
        if (!r.ok) throw new Error("geo fetch " + r.status);
        return r.json();
      });
    }

    function buildValueByMapName(geo) {
      var features = (geo && geo.features) || [];
      var canonByNorm = {};
      features.forEach(function (f) {
        var n = (f.properties && f.properties.name) || "";
        if (!n) return;
        canonByNorm[normState(n)] = n;
      });

      var sumByCanon = {};
      features.forEach(function (f) {
        var n = (f.properties && f.properties.name) || "";
        if (n) sumByCanon[n] = 0;
      });

      stateRows.forEach(function (row) {
        var raw = String(row.name || "");
        var v = Number(row.value) || 0;
        var nk = normState(raw);
        var mapped = EXPORT_TO_MAP_NAME[nk] || raw;
        var canon = canonByNorm[normState(mapped)] || canonByNorm[nk];
        if (canon) sumByCanon[canon] = (sumByCanon[canon] || 0) + v;
      });

      return features.map(function (f) {
        var nm = (f.properties && f.properties.name) || "";
        return { name: nm, value: sumByCanon[nm] != null ? sumByCanon[nm] : 0 };
      });
    }

    function renderMap(geo) {
      var features = (geo && geo.features) || [];
      if (!features.length) throw new Error("no features");

      var fc = { type: "FeatureCollection", features: features };
      echarts.registerMap("IndiaHC", fc, { nameProperty: "name" });

      var mapData = buildValueByMapName(geo);
      var vals = mapData.map(function (d) { return d.value; });
      var vmax = Math.max.apply(null, vals.concat([1]));

      var chart = echarts.init(host, null, { renderer: "canvas" });
      chart.setOption({
        backgroundColor: "#ffffff",
        tooltip: {
          trigger: "item",
          formatter: function (p) {
            var v = p.value;
            if (p.data && typeof p.data === "object" && p.data.value != null) v = p.data.value;
            return p.name + "<br/>Registrations: " + (Number(v) || 0).toLocaleString();
          },
        },
        visualMap: {
          type: "continuous",
          min: 0,
          max: vmax,
          seriesIndex: 0,
          text: ["High", "Low"],
          calculable: true,
          orient: "horizontal",
          left: "center",
          bottom: 6,
          itemWidth: 10,
          itemHeight: 120,
          textGap: 6,
          textStyle: { color: "#64748b", fontSize: 10 },
          inRange: {
            color: ["#f1f5f9", "#cbd5e1", "#64748b", "#1e293b", "#0f172a"],
          },
        },
        series: [
          {
            name: "Registrations",
            type: "map",
            map: "IndiaHC",
            roam: false,
            layoutCenter: ["50%", "46%"],
            layoutSize: "92%",
            aspectScale: 0.85,
            scaleLimit: { min: 0.85, max: 12 },
            data: mapData,
            itemStyle: {
              borderColor: "#94a3b8",
              borderWidth: 0.55,
            },
            emphasis: {
              label: { show: true, color: "#0f172a", fontSize: 11, fontWeight: 600 },
              itemStyle: {
                borderColor: "#0f172a",
                borderWidth: 1.2,
                shadowBlur: 10,
                shadowColor: "rgba(15,23,42,0.2)",
              },
            },
            select: { disabled: true },
          },
        ],
      });
      window.addEventListener("resize", function () {
        chart.resize();
      });
    }

    loadGeo(urls[0])
      .then(function (geo) {
        renderMap(geo);
      })
      .catch(function () {
        host.innerHTML =
          '<p class="text-xs text-slate-500 p-4 text-center">Could not load <code class="rounded bg-slate-100 px-1">/static/geo/in-all-claimed.geo.json</code>. Run <code class="rounded bg-slate-100 px-1">python scripts/build_india_states_claimed.py</code> to regenerate.</p>';
      });
  }

  function donutOpts() {
    return {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: "bottom",
          labels: {
            color: "#334155",
            boxWidth: 12,
            padding: 8,
            font: { size: 10 },
            maxWidth: 200,
          },
        },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var v = Number(ctx.raw) || 0;
              var arr = ctx.dataset.data || [];
              var total = arr.reduce(function (a, b) {
                return a + (Number(b) || 0);
              }, 0);
              var pct = total ? ((v / total) * 100).toFixed(1) : "0.0";
              return " " + v.toLocaleString() + " (" + pct + "%)";
            },
          },
        },
      },
      cutout: "58%",
    };
  }

  function mkUtmDonut() {
    var el = document.getElementById("mdcUtmDonut");
    var host = document.getElementById("mdcUtmChartHost");
    if (!el || !host || typeof Chart === "undefined") return;
    var rows = dc.utm_sources || [];
    if (!rows.length) {
      host.innerHTML = '<p class="text-sm text-slate-500 py-12 text-center">No UTM source data yet.</p>';
      return;
    }
    var labels = rows.map(function (r) {
      return String(r.source);
    });
    var data = rows.map(function (r) {
      return Number(r.count) || 0;
    });
    var colors = [
      "rgba(14, 165, 233, 0.9)",
      "rgba(245, 158, 11, 0.9)",
      "rgba(20, 184, 166, 0.9)",
      "rgba(99, 102, 241, 0.9)",
      "rgba(236, 72, 153, 0.88)",
      "rgba(34, 197, 94, 0.88)",
      "rgba(148, 163, 184, 0.9)",
      "rgba(251, 146, 60, 0.9)",
    ];
    var bg = labels.map(function (_, i) {
      return colors[i % colors.length];
    });
    new Chart(el, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [
          {
            data: data,
            backgroundColor: bg,
            borderColor: "#ffffff",
            borderWidth: 2,
            hoverOffset: 6,
          },
        ],
      },
      options: donutOpts(),
    });
  }

  function mkGenderDonut() {
    var el = document.getElementById("mdcGenderDonut");
    var host = document.getElementById("mdcGenderChartHost");
    if (!el || !host || typeof Chart === "undefined") return;
    var rows = dc.gender_breakdown || [];
    if (!rows.length) {
      host.innerHTML = '<p class="text-sm text-slate-500 py-12 text-center">No gender field data.</p>';
      return;
    }
    var labels = rows.map(function (r) {
      return String(r.gender);
    });
    var data = rows.map(function (r) {
      return Number(r.count) || 0;
    });
    var colors = [
      "rgba(59, 130, 246, 0.85)",
      "rgba(244, 114, 182, 0.85)",
      "rgba(148, 163, 184, 0.85)",
      "rgba(251, 191, 36, 0.85)",
      "rgba(52, 211, 153, 0.85)",
      "rgba(167, 139, 250, 0.85)",
      "rgba(251, 113, 133, 0.85)",
      "rgba(45, 212, 191, 0.85)",
    ];
    var bg = labels.map(function (_, i) {
      return colors[i % colors.length];
    });
    new Chart(el, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [
          {
            data: data,
            backgroundColor: bg,
            borderColor: "#ffffff",
            borderWidth: 2,
            hoverOffset: 6,
          },
        ],
      },
      options: donutOpts(),
    });
  }

  function mkOccupationDonut() {
    var el = document.getElementById("mdcOccupationDonut");
    var host = document.getElementById("mdcOccupationChartHost");
    if (!el || !host || typeof Chart === "undefined") return;
    var rows = dc.top_occupations || [];
    if (!rows.length) {
      host.innerHTML = '<p class="text-sm text-slate-500 py-12 text-center">No occupation field data.</p>';
      return;
    }
    var labels = rows.map(function (r) {
      return String(r.occupation);
    });
    var data = rows.map(function (r) {
      return Number(r.count) || 0;
    });
    var colors = [
      "rgba(109, 40, 217, 0.88)",
      "rgba(139, 92, 246, 0.88)",
      "rgba(167, 139, 250, 0.88)",
      "rgba(196, 181, 253, 0.92)",
      "rgba(124, 58, 237, 0.88)",
      "rgba(91, 33, 182, 0.88)",
      "rgba(76, 29, 149, 0.88)",
      "rgba(221, 214, 254, 0.95)",
    ];
    var bg = labels.map(function (_, i) {
      return colors[i % colors.length];
    });
    new Chart(el, {
      type: "doughnut",
      data: {
        labels: labels,
        datasets: [
          {
            data: data,
            backgroundColor: bg,
            borderColor: "#ffffff",
            borderWidth: 2,
            hoverOffset: 6,
          },
        ],
      },
      options: donutOpts(),
    });
  }

  mkLine();
  mkHourly();
  if (!dc.skip_attendance_city) {
    mkAttendanceCityBar();
  }
  mkIndiaMap();
  mkUtmDonut();
  mkGenderDonut();
  mkOccupationDonut();

  var note = document.getElementById("mdcTzNote");
  if (note) note.textContent = tzNote;
});
