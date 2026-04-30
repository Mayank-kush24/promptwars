/* PromptWars shared date range picker.
 *
 * Auto-initializes on `[data-pw-date-range]` elements (idempotent — safe to load multiple times).
 *
 * Reads configuration from `data-*` attributes:
 *   data-pw-drp-from-name    : hidden input name for the "from" ISO date (yyyy-mm-dd / yyyy-mm-ddThh:mm)
 *   data-pw-drp-to-name      : hidden input name for the "to" ISO date
 *   data-pw-drp-min          : optional ISO bound for earliest selectable date
 *   data-pw-drp-max          : optional ISO bound for latest selectable date
 *   data-pw-drp-from         : optional initial "from" value
 *   data-pw-drp-to           : optional initial "to" value
 *   data-pw-drp-accent       : sky | violet | emerald | slate | rose (default sky)
 *   data-pw-drp-label        : trigger label (e.g. "Registration date")
 *   data-pw-drp-placeholder  : trigger placeholder when nothing selected (default "Pick a date range")
 *   data-pw-drp-presets      : "1" to show quick-range presets (Today, Last 7d, Last 30d, This month, All)
 *   data-pw-drp-show-reset   : "1" to show a small "All" reset chip on the trigger
 *   data-pw-drp-mode         : "date" (default) | "datetime"  — datetime adds time inputs
 *   data-pw-drp-size         : "sm" (default) | "md" — visual density of the trigger pill
 *
 * Emits CustomEvents on the host element (bubbling, composed):
 *   pw-date-range:apply  — detail = { from, to, allRange }
 *   pw-date-range:reset  — detail = {} (also fires :apply with allRange=true)
 */
(function () {
  "use strict";

  if (window.__pwDateRangePicker__) return;
  window.__pwDateRangePicker__ = true;

  var ACCENTS = {
    sky:     { fill: "#0ea5e9", soft: "#e0f2fe", strong: "#0369a1", border: "#bae6fd", ring: "rgba(14,165,233,0.45)" },
    violet:  { fill: "#7c3aed", soft: "#ede9fe", strong: "#6d28d9", border: "#ddd6fe", ring: "rgba(124,58,237,0.45)" },
    emerald: { fill: "#059669", soft: "#d1fae5", strong: "#047857", border: "#a7f3d0", ring: "rgba(5,150,105,0.45)" },
    slate:   { fill: "#334155", soft: "#e2e8f0", strong: "#0f172a", border: "#cbd5e1", ring: "rgba(51,65,85,0.45)" },
    rose:    { fill: "#e11d48", soft: "#ffe4e6", strong: "#be123c", border: "#fecdd3", ring: "rgba(225,29,72,0.45)" },
  };

  function injectStyles() {
    if (document.getElementById("pw-drp-styles")) return;
    var css =
      ".pw-drp{position:relative;display:inline-flex;align-items:stretch;font-family:inherit}" +
      ".pw-drp-trigger{display:inline-flex;align-items:center;gap:.5rem;background:#fff;border:1px solid var(--pw-drp-border,#cbd5e1);border-radius:9999px;padding:.45rem .8rem .45rem .65rem;color:#0f172a;font-size:.78rem;font-weight:600;cursor:pointer;line-height:1.1;min-width:0;transition:border-color .15s ease, box-shadow .15s ease}" +
      ".pw-drp-trigger:hover{border-color:var(--pw-drp-fill,#0ea5e9)}" +
      ".pw-drp-trigger:focus-visible{outline:none;box-shadow:0 0 0 3px var(--pw-drp-ring,rgba(14,165,233,0.45));border-color:var(--pw-drp-fill,#0ea5e9)}" +
      ".pw-drp[data-size='md'] .pw-drp-trigger{font-size:.85rem;padding:.55rem 1rem}" +
      ".pw-drp-trigger-icon{font-family:'Material Symbols Outlined';font-size:18px;line-height:1;color:var(--pw-drp-fill,#0ea5e9);flex-shrink:0}" +
      ".pw-drp-trigger-label{display:flex;flex-direction:column;align-items:flex-start;line-height:1.15;min-width:0;flex:1}" +
      ".pw-drp-trigger-label .lbl{font-size:.55rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#64748b;margin-bottom:1px}" +
      ".pw-drp-trigger-label .val{font-size:.78rem;font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:18rem}" +
      ".pw-drp-trigger-label .val.is-empty{color:#94a3b8;font-weight:500}" +
      ".pw-drp-trigger-caret{font-family:'Material Symbols Outlined';font-size:18px;color:#94a3b8;line-height:1;flex-shrink:0}" +
      ".pw-drp-trigger-reset{appearance:none;background:#fff;border:1px solid #e2e8f0;color:#475569;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:.2rem .55rem;border-radius:9999px;margin-left:.45rem;cursor:pointer;line-height:1}" +
      ".pw-drp-trigger-reset:hover{background:#f8fafc;color:#0f172a}" +
      ".pw-drp-trigger-loading{display:none;font-size:10px;font-weight:600;color:var(--pw-drp-strong,#0369a1);margin-left:.4rem}" +
      ".pw-drp.is-loading .pw-drp-trigger-loading{display:inline}" +
      ".pw-drp.pw-drp-is-open{position:relative;z-index:300}" +
      ".pw-drp-popover{position:absolute;top:calc(100% + 8px);right:0;z-index:400;box-sizing:border-box;width:min(920px,calc(100vw - 20px));max-width:calc(100vw - 16px);background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 12px 40px rgba(15,23,42,0.18),0 2px 8px rgba(15,23,42,0.06);overflow:hidden}" +
      ".pw-drp-popover[hidden]{display:none}" +
      ".pw-drp-popover.align-left{right:auto;left:0}" +
      ".pw-drp-popover.align-fixed{position:fixed;top:auto}" +
      ".pw-drp-arrow{position:absolute;top:-7px;right:24px;width:14px;height:14px;background:#fff;border-top:1px solid #e2e8f0;border-left:1px solid #e2e8f0;transform:rotate(45deg)}" +
      ".pw-drp-popover.align-left .pw-drp-arrow{right:auto;left:24px}" +
      ".pw-drp-grid{display:grid;grid-template-columns:minmax(220px,1fr) minmax(220px,1fr) minmax(200px,260px);gap:0}" +
      "@media (max-width:900px) and (min-width:761px){.pw-drp-grid{grid-template-columns:minmax(168px,1fr) minmax(168px,1fr) minmax(188px,240px)}.pw-drp-cell{min-height:34px;height:34px;font-size:.75rem}.pw-drp-days,.pw-drp-dow{gap:3px}}" +
      "@media (max-width:760px){.pw-drp-popover{width:min(400px,calc(100vw - 16px));min-width:min(360px,calc(100vw - 16px));max-width:calc(100vw - 12px)}.pw-drp-grid{grid-template-columns:1fr}}" +
      ".pw-drp-cal{padding:16px 14px 14px;border-right:1px solid #f1f5f9;background:#fff}" +
      ".pw-drp-cal:nth-child(2){border-right:1px solid #f1f5f9}" +
      "@media (max-width:760px){.pw-drp-cal{border-right:none;border-bottom:1px solid #f1f5f9}}" +
      ".pw-drp-cal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;padding:0 4px}" +
      ".pw-drp-cal-title{font-size:.82rem;font-weight:600;color:#0f172a;letter-spacing:0}" +
      ".pw-drp-nav{appearance:none;background:transparent;border:none;width:26px;height:26px;border-radius:9999px;display:inline-flex;align-items:center;justify-content:center;color:#475569;cursor:pointer}" +
      ".pw-drp-nav:hover{background:#f1f5f9;color:#0f172a}" +
      ".pw-drp-nav[disabled]{opacity:.35;cursor:not-allowed}" +
      ".pw-drp-nav .material-symbols-outlined{font-size:18px}" +
      ".pw-drp-dow{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin-bottom:6px}" +
      ".pw-drp-dow span{font-size:11px;font-weight:600;color:#94a3b8;text-align:center;letter-spacing:0.04em;text-transform:uppercase;padding:5px 0}" +
      ".pw-drp-days{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}" +
      ".pw-drp-cell{position:relative;min-height:38px;height:38px;display:flex;align-items:center;justify-content:center;font-size:.8125rem;font-weight:500;color:#0f172a;cursor:pointer;border-radius:9999px;background:transparent;border:none;padding:0;font-family:inherit}" +
      ".pw-drp-cell.is-empty{opacity:0;pointer-events:auto;cursor:default}" +
      ".pw-drp-cell.is-out,.pw-drp-cell[aria-disabled='true']{color:#cbd5e1;cursor:not-allowed;pointer-events:auto}" +
      ".pw-drp-cell:hover:not(.is-empty):not(.is-out):not([aria-disabled='true']){background:#f1f5f9}" +
      ".pw-drp-cell.is-today{font-weight:700;color:var(--pw-drp-strong,#0369a1)}" +
      ".pw-drp-cell.is-in-range{background:var(--pw-drp-soft,#e0f2fe);border-radius:0}" +
      ".pw-drp-cell.is-in-range.is-range-start{border-top-left-radius:9999px;border-bottom-left-radius:9999px}" +
      ".pw-drp-cell.is-in-range.is-range-end{border-top-right-radius:9999px;border-bottom-right-radius:9999px}" +
      ".pw-drp-cell.is-selected,.pw-drp-cell.is-range-start.is-selected,.pw-drp-cell.is-range-end.is-selected{background:var(--pw-drp-fill,#0ea5e9);color:#fff;font-weight:700;border-radius:9999px;z-index:1}" +
      ".pw-drp-cell.is-selected:hover{background:var(--pw-drp-fill,#0ea5e9)}" +
      ".pw-drp-side{padding:16px 16px 14px;display:flex;flex-direction:column;gap:12px;background:#fafbfc;border-left:1px solid #f1f5f9}" +
      "@media (max-width:760px){.pw-drp-side{border-left:none;border-top:1px solid #f1f5f9}}" +
      ".pw-drp-side h4{font-size:.75rem;font-weight:600;color:#475569;margin:0;letter-spacing:.02em}" +
      ".pw-drp-input{width:100%;padding:.5rem .65rem;font-size:.78rem;border:1px solid #cbd5e1;border-radius:8px;background:#fff;color:#0f172a;font-family:inherit}" +
      ".pw-drp-input:focus{outline:none;border-color:var(--pw-drp-fill,#0ea5e9);box-shadow:0 0 0 3px var(--pw-drp-ring,rgba(14,165,233,0.4))}" +
      ".pw-drp-presets{display:flex;flex-wrap:wrap;gap:6px;padding-top:2px;border-top:1px dashed #e2e8f0;margin-top:4px;padding-top:8px}" +
      ".pw-drp-preset{appearance:none;background:#fff;border:1px solid #e2e8f0;color:#334155;font-size:11px;font-weight:600;padding:5px 9px;border-radius:9999px;cursor:pointer;font-family:inherit;line-height:1}" +
      ".pw-drp-preset:hover{border-color:var(--pw-drp-fill,#0ea5e9);color:var(--pw-drp-strong,#0369a1);background:var(--pw-drp-soft,#e0f2fe)}" +
      ".pw-drp-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-top:auto;padding-top:8px;border-top:1px solid #f1f5f9}" +
      ".pw-drp-btn{appearance:none;border:1px solid transparent;border-radius:8px;padding:.45rem .85rem;font-size:.78rem;font-weight:600;cursor:pointer;font-family:inherit;line-height:1.1}" +
      ".pw-drp-btn-cancel{background:#fff;border-color:#e2e8f0;color:#334155}" +
      ".pw-drp-btn-cancel:hover{background:#f8fafc;color:#0f172a}" +
      ".pw-drp-btn-apply{background:var(--pw-drp-fill,#0ea5e9);color:#fff;border-color:var(--pw-drp-fill,#0ea5e9)}" +
      ".pw-drp-btn-apply:hover{filter:brightness(0.95)}" +
      ".pw-drp-btn-apply[disabled]{opacity:.55;cursor:not-allowed}";
    var st = document.createElement("style");
    st.id = "pw-drp-styles";
    st.appendChild(document.createTextNode(css));
    document.head.appendChild(st);
  }

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  function parseISODate(s) {
    if (!s) return null;
    var m = String(s).slice(0, 10).match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return null;
    var y = parseInt(m[1], 10), mo = parseInt(m[2], 10), d = parseInt(m[3], 10);
    if (!y || !mo || !d) return null;
    var dt = new Date(y, mo - 1, d, 0, 0, 0, 0);
    if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null;
    return dt;
  }

  function parseISODateTime(s) {
    if (!s) return null;
    var m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3];
    var hh = m[4] != null ? +m[4] : 0;
    var mm = m[5] != null ? +m[5] : 0;
    if (!y || !mo || !d) return null;
    var dt = new Date(y, mo - 1, d, hh, mm, 0, 0);
    if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null;
    return dt;
  }

  function fmtISODate(d) {
    if (!d) return "";
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function fmtISODateTime(d) {
    if (!d) return "";
    return fmtISODate(d) + "T" + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var WEEKDAYS_SHORT = ["S", "M", "T", "W", "T", "F", "S"];
  var MONTHS_FULL = ["January", "February", "March", "April", "May", "June",
                     "July", "August", "September", "October", "November", "December"];

  function fmtTriggerLabel(d, withTime) {
    if (!d) return "";
    var wd = WEEKDAYS[d.getDay()];
    var dom = d.getDate();
    var mo = MONTHS_FULL[d.getMonth()].slice(0, 3);
    var y = d.getFullYear();
    var base = wd + ", " + dom + " " + mo + " " + y;
    if (!withTime) return base;
    return base + " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function fmtSidePicker(d, withTime) {
    if (!d) return "";
    var wd = WEEKDAYS[d.getDay()];
    var dom = d.getDate();
    var mo = MONTHS_FULL[d.getMonth()].slice(0, 3);
    var y = d.getFullYear();
    var base = wd + ", " + dom + " " + mo + " " + y;
    if (!withTime) return base;
    return base + ", " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1, 0, 0, 0, 0); }
  function endOfMonth(d) { return new Date(d.getFullYear(), d.getMonth() + 1, 0, 23, 59, 59, 999); }
  function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, 1, 0, 0, 0, 0); }
  function ymd(d) { return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate(); }

  function buildPicker(host) {
    if (host.__pwDrpInited) return;
    host.__pwDrpInited = true;

    var fromName = host.dataset.pwDrpFromName || "";
    var toName = host.dataset.pwDrpToName || "";
    var minBound = host.dataset.pwDrpMin || "";
    var maxBound = host.dataset.pwDrpMax || "";
    var initFrom = host.dataset.pwDrpFrom || "";
    var initTo = host.dataset.pwDrpTo || "";
    var accent = host.dataset.pwDrpAccent || "sky";
    var label = host.dataset.pwDrpLabel || "";
    var placeholder = host.dataset.pwDrpPlaceholder || "Pick a date range";
    var withPresets = host.dataset.pwDrpPresets === "1";
    var showReset = host.dataset.pwDrpShowReset === "1";
    var mode = host.dataset.pwDrpMode === "datetime" ? "datetime" : "date";
    var size = host.dataset.pwDrpSize === "md" ? "md" : "sm";

    var theme = ACCENTS[accent] || ACCENTS.sky;
    host.style.setProperty("--pw-drp-fill", theme.fill);
    host.style.setProperty("--pw-drp-soft", theme.soft);
    host.style.setProperty("--pw-drp-strong", theme.strong);
    host.style.setProperty("--pw-drp-border", theme.border);
    host.style.setProperty("--pw-drp-ring", theme.ring);
    host.classList.add("pw-drp");
    host.setAttribute("data-size", size);

    var minDate = mode === "datetime" ? parseISODateTime(minBound) : parseISODate(minBound);
    var maxDate = mode === "datetime" ? parseISODateTime(maxBound) : parseISODate(maxBound);
    var withTime = mode === "datetime";

    var fromDate = (withTime ? parseISODateTime : parseISODate)(initFrom);
    var toDate = (withTime ? parseISODateTime : parseISODate)(initTo);

    function clamp(d) {
      if (!d) return null;
      if (minDate && d < minDate) return new Date(minDate.getTime());
      if (maxDate && d > maxDate) return new Date(maxDate.getTime());
      return d;
    }
    if (fromDate) fromDate = clamp(fromDate);
    if (toDate) toDate = clamp(toDate);
    if (fromDate && toDate && fromDate > toDate) toDate = new Date(fromDate.getTime());

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "pw-drp-trigger";
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "false");
    trigger.innerHTML =
      '<span class="pw-drp-trigger-icon material-symbols-outlined">date_range</span>' +
      '<span class="pw-drp-trigger-label">' +
        (label ? '<span class="lbl"></span>' : "") +
        '<span class="val"></span>' +
      "</span>" +
      '<span class="pw-drp-trigger-loading">…</span>' +
      '<span class="pw-drp-trigger-caret material-symbols-outlined">expand_more</span>';
    if (label) trigger.querySelector(".lbl").textContent = label;
    host.appendChild(trigger);

    var resetBtn = null;
    if (showReset) {
      resetBtn = document.createElement("button");
      resetBtn.type = "button";
      resetBtn.className = "pw-drp-trigger-reset";
      resetBtn.textContent = "All";
      resetBtn.title = "Clear date range";
      host.appendChild(resetBtn);
    }

    var hiddenFrom = document.createElement("input");
    hiddenFrom.type = "hidden";
    if (fromName) hiddenFrom.name = fromName;
    hiddenFrom.value = fromDate ? (withTime ? fmtISODateTime(fromDate) : fmtISODate(fromDate)) : "";
    host.appendChild(hiddenFrom);

    var hiddenTo = document.createElement("input");
    hiddenTo.type = "hidden";
    if (toName) hiddenTo.name = toName;
    hiddenTo.value = toDate ? (withTime ? fmtISODateTime(toDate) : fmtISODate(toDate)) : "";
    host.appendChild(hiddenTo);

    var pop = document.createElement("div");
    pop.className = "pw-drp-popover";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", (label || "Date range") + " picker");
    pop.hidden = true;
    pop.innerHTML =
      '<div class="pw-drp-arrow"></div>' +
      '<div class="pw-drp-grid">' +
        '<div class="pw-drp-cal" data-cal="left">' +
          '<div class="pw-drp-cal-head">' +
            '<button type="button" class="pw-drp-nav" data-nav="prev" aria-label="Previous month"><span class="material-symbols-outlined">chevron_left</span></button>' +
            '<span class="pw-drp-cal-title" data-title="left">—</span>' +
            '<span style="width:26px"></span>' +
          '</div>' +
          '<div class="pw-drp-dow" data-dow></div>' +
          '<div class="pw-drp-days" data-days></div>' +
        '</div>' +
        '<div class="pw-drp-cal" data-cal="right">' +
          '<div class="pw-drp-cal-head">' +
            '<span style="width:26px"></span>' +
            '<span class="pw-drp-cal-title" data-title="right">—</span>' +
            '<button type="button" class="pw-drp-nav" data-nav="next" aria-label="Next month"><span class="material-symbols-outlined">chevron_right</span></button>' +
          '</div>' +
          '<div class="pw-drp-dow" data-dow></div>' +
          '<div class="pw-drp-days" data-days></div>' +
        '</div>' +
        '<div class="pw-drp-side">' +
          '<h4>Date range</h4>' +
          '<input type="text" class="pw-drp-input" data-side="from" placeholder="Start" autocomplete="off" />' +
          '<input type="text" class="pw-drp-input" data-side="to" placeholder="End" autocomplete="off" />' +
          (withPresets ? '<div class="pw-drp-presets" data-presets></div>' : '') +
          '<div class="pw-drp-actions">' +
            '<button type="button" class="pw-drp-btn pw-drp-btn-cancel" data-action="cancel">Cancel</button>' +
            '<button type="button" class="pw-drp-btn pw-drp-btn-apply" data-action="apply">Apply</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    host.appendChild(pop);

    var dowL = pop.querySelector('[data-cal="left"] [data-dow]');
    var dowR = pop.querySelector('[data-cal="right"] [data-dow]');
    [dowL, dowR].forEach(function (host) {
      WEEKDAYS_SHORT.forEach(function (w) {
        var s = document.createElement("span");
        s.textContent = w;
        host.appendChild(s);
      });
    });

    if (withPresets) {
      var presetWrap = pop.querySelector("[data-presets]");
      var presets = [
        { id: "today",  label: "Today" },
        { id: "7d",     label: "Last 7d" },
        { id: "30d",    label: "Last 30d" },
        { id: "month",  label: "This month" },
        { id: "all",    label: "All" },
      ];
      presets.forEach(function (p) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "pw-drp-preset";
        b.dataset.preset = p.id;
        b.textContent = p.label;
        presetWrap.appendChild(b);
      });
    }

    var titleL = pop.querySelector('[data-title="left"]');
    var titleR = pop.querySelector('[data-title="right"]');
    var daysL = pop.querySelector('[data-cal="left"] [data-days]');
    var daysR = pop.querySelector('[data-cal="right"] [data-days]');
    var navPrev = pop.querySelector('[data-nav="prev"]');
    var navNext = pop.querySelector('[data-nav="next"]');
    var sideFromInp = pop.querySelector('[data-side="from"]');
    var sideToInp = pop.querySelector('[data-side="to"]');
    var btnCancel = pop.querySelector('[data-action="cancel"]');
    var btnApply = pop.querySelector('[data-action="apply"]');

    var leftMonth = (function () {
      var seed = fromDate || (maxDate && maxDate < new Date() ? maxDate : new Date());
      return startOfMonth(seed);
    })();

    var pendingFrom = fromDate ? new Date(fromDate.getTime()) : null;
    var pendingTo = toDate ? new Date(toDate.getTime()) : null;
    var awaiting = "from"; // "from" | "to"

    function ensureMonthsFitBounds() {
      if (maxDate) {
        var rightMonth = addMonths(leftMonth, 1);
        if (rightMonth > startOfMonth(maxDate)) {
          leftMonth = startOfMonth(addMonths(maxDate, -1));
        }
      }
      if (minDate && leftMonth < startOfMonth(minDate)) {
        leftMonth = startOfMonth(minDate);
      }
    }

    function clampPending(d) {
      if (!d) return null;
      if (minDate && d < minDate) return new Date(minDate.getTime());
      if (maxDate && d > maxDate) return new Date(maxDate.getTime());
      return d;
    }

    function renderMonth(host, baseDate, sideTitle) {
      sideTitle.textContent = MONTHS_FULL[baseDate.getMonth()] + " " + baseDate.getFullYear();
      host.innerHTML = "";
      var firstDow = baseDate.getDay();
      var nDays = new Date(baseDate.getFullYear(), baseDate.getMonth() + 1, 0).getDate();
      var todayKey = ymd(new Date());
      var aKey = pendingFrom ? ymd(pendingFrom) : null;
      var bKey = pendingTo ? ymd(pendingTo) : null;

      for (var i = 0; i < firstDow; i++) {
        var ph = document.createElement("div");
        ph.className = "pw-drp-cell is-empty";
        ph.setAttribute("aria-hidden", "true");
        host.appendChild(ph);
      }

      for (var d = 1; d <= nDays; d++) {
        var dt = new Date(baseDate.getFullYear(), baseDate.getMonth(), d, 0, 0, 0, 0);
        var dKey = ymd(dt);
        var cell = document.createElement("button");
        cell.type = "button";
        cell.className = "pw-drp-cell";
        cell.textContent = String(d);
        cell.dataset.iso = fmtISODate(dt);
        cell.dataset.day = String(dKey);

        var disabled = false;
        if (minDate && dt < new Date(minDate.getFullYear(), minDate.getMonth(), minDate.getDate())) disabled = true;
        if (maxDate && dt > new Date(maxDate.getFullYear(), maxDate.getMonth(), maxDate.getDate())) disabled = true;
        if (disabled) {
          cell.setAttribute("aria-disabled", "true");
          cell.classList.add("is-out");
          cell.tabIndex = -1;
        }

        if (dKey === todayKey) cell.classList.add("is-today");

        var lo = aKey, hi = bKey;

        if (lo && hi && dKey >= lo && dKey <= hi) {
          cell.classList.add("is-in-range");
          if (dKey === lo) cell.classList.add("is-range-start");
          if (dKey === hi) cell.classList.add("is-range-end");
        }
        if (aKey && dKey === aKey) cell.classList.add("is-selected", "is-range-start");
        if (bKey && dKey === bKey) cell.classList.add("is-selected", "is-range-end");

        host.appendChild(cell);
      }
    }

    function renderAll() {
      ensureMonthsFitBounds();
      var rightMonth = addMonths(leftMonth, 1);
      renderMonth(daysL, leftMonth, titleL);
      renderMonth(daysR, rightMonth, titleR);

      navPrev.disabled = !!(minDate && leftMonth <= startOfMonth(minDate));
      navNext.disabled = !!(maxDate && rightMonth >= startOfMonth(maxDate));

      sideFromInp.value = pendingFrom ? fmtSidePicker(pendingFrom, withTime) : "";
      sideToInp.value = pendingTo ? fmtSidePicker(pendingTo, withTime) : "";
      btnApply.disabled = false;
    }

    function syncTriggerLabel() {
      var val = trigger.querySelector(".val");
      if (fromDate && toDate) {
        var same = ymd(fromDate) === ymd(toDate);
        val.classList.remove("is-empty");
        if (same && !withTime) {
          val.textContent = fmtTriggerLabel(fromDate, withTime);
        } else {
          val.textContent = fmtTriggerLabel(fromDate, withTime) + "  –  " + fmtTriggerLabel(toDate, withTime);
        }
        val.title = val.textContent;
      } else {
        val.classList.add("is-empty");
        val.textContent = placeholder;
        val.title = placeholder;
      }
    }

    function syncHidden() {
      hiddenFrom.value = fromDate ? (withTime ? fmtISODateTime(fromDate) : fmtISODate(fromDate)) : "";
      hiddenTo.value = toDate ? (withTime ? fmtISODateTime(toDate) : fmtISODate(toDate)) : "";
    }

    function openPop() {
      pendingFrom = fromDate ? new Date(fromDate.getTime()) : null;
      pendingTo = toDate ? new Date(toDate.getTime()) : null;
      awaiting = "from";
      var seed = pendingFrom || (maxDate && maxDate < new Date() ? maxDate : new Date());
      leftMonth = startOfMonth(seed);
      ensureMonthsFitBounds();
      pop.hidden = false;
      host.classList.add("pw-drp-is-open");
      trigger.setAttribute("aria-expanded", "true");
      adjustAlignment();
      renderAll();
      sideFromInp.focus({ preventScroll: true });
    }

    function closePop() {
      pop.hidden = true;
      host.classList.remove("pw-drp-is-open");
      trigger.setAttribute("aria-expanded", "false");
    }

    function adjustAlignment() {
      pop.classList.remove("align-left", "align-fixed");
      pop.style.minWidth = "";
      pop.style.width = "";
      try {
        var rect = trigger.getBoundingClientRect();
        var vw = window.innerWidth;
        var desktopTarget = Math.min(920, vw - 20);
        if (rect.right - desktopTarget < 8) {
          pop.classList.add("align-left");
        }
      } catch (_e) {}
    }

    function applyPending() {
      if (pendingFrom && pendingTo && pendingFrom > pendingTo) {
        var t = pendingFrom; pendingFrom = pendingTo; pendingTo = t;
      }
      fromDate = pendingFrom ? new Date(pendingFrom.getTime()) : null;
      toDate = pendingTo ? new Date(pendingTo.getTime()) : null;
      syncHidden();
      syncTriggerLabel();
      closePop();
      var allRange = !fromDate && !toDate;
      host.dispatchEvent(new CustomEvent("pw-date-range:apply", {
        bubbles: true, composed: true,
        detail: {
          from: hiddenFrom.value,
          to: hiddenTo.value,
          allRange: allRange,
        },
      }));
    }

    function resetAll() {
      fromDate = null;
      toDate = null;
      pendingFrom = null;
      pendingTo = null;
      syncHidden();
      syncTriggerLabel();
      closePop();
      host.dispatchEvent(new CustomEvent("pw-date-range:reset", { bubbles: true, composed: true }));
      host.dispatchEvent(new CustomEvent("pw-date-range:apply", {
        bubbles: true, composed: true,
        detail: { from: "", to: "", allRange: true },
      }));
    }

    trigger.addEventListener("click", function (e) {
      e.preventDefault();
      if (pop.hidden) openPop(); else closePop();
    });

    if (resetBtn) {
      resetBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        resetAll();
      });
    }

    navPrev.addEventListener("click", function () {
      leftMonth = addMonths(leftMonth, -1);
      renderAll();
    });
    navNext.addEventListener("click", function () {
      leftMonth = addMonths(leftMonth, 1);
      renderAll();
    });

    pop.addEventListener("click", function (e) {
      var cell = e.target.closest(".pw-drp-cell");
      if (cell && !cell.classList.contains("is-empty") && !cell.classList.contains("is-out")) {
        var iso = cell.dataset.iso;
        var d = parseISODate(iso);
        if (!d) return;
        if (withTime) {
          if (awaiting === "from") d.setHours(0, 0, 0, 0);
          else d.setHours(23, 59, 0, 0);
        }
        if (awaiting === "from" || (pendingFrom && pendingTo)) {
          pendingFrom = d;
          pendingTo = null;
          awaiting = "to";
        } else {
          if (d < pendingFrom) {
            pendingTo = pendingFrom;
            pendingFrom = d;
          } else {
            pendingTo = d;
          }
          awaiting = "from";
        }
        renderAll();
        return;
      }
    });

    function readSideInput(inp) {
      var raw = (inp.value || "").trim();
      if (!raw) return null;
      var iso = raw.match(/(\d{4})-(\d{2})-(\d{2})/);
      if (iso) return (withTime ? parseISODateTime : parseISODate)(raw);
      var dt = new Date(raw);
      if (isNaN(dt.getTime())) return null;
      return dt;
    }

    sideFromInp.addEventListener("change", function () {
      var d = clampPending(readSideInput(sideFromInp));
      if (!d) return;
      pendingFrom = d;
      if (pendingTo && pendingTo < pendingFrom) pendingTo = new Date(pendingFrom.getTime());
      leftMonth = startOfMonth(pendingFrom);
      awaiting = "to";
      renderAll();
    });
    sideToInp.addEventListener("change", function () {
      var d = clampPending(readSideInput(sideToInp));
      if (!d) return;
      pendingTo = d;
      if (pendingFrom && pendingFrom > pendingTo) pendingFrom = new Date(pendingTo.getTime());
      awaiting = "from";
      renderAll();
    });

    if (withPresets) {
      pop.addEventListener("click", function (e) {
        var b = e.target.closest(".pw-drp-preset");
        if (!b) return;
        var kind = b.dataset.preset;
        var now = new Date();
        var todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0, 0);
        var todayEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 0, 0);
        if (kind === "today") {
          pendingFrom = clampPending(todayStart);
          pendingTo = clampPending(todayEnd);
        } else if (kind === "7d") {
          pendingFrom = clampPending(new Date(todayStart.getTime() - 6 * 86400000));
          pendingTo = clampPending(todayEnd);
        } else if (kind === "30d") {
          pendingFrom = clampPending(new Date(todayStart.getTime() - 29 * 86400000));
          pendingTo = clampPending(todayEnd);
        } else if (kind === "month") {
          pendingFrom = clampPending(new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0));
          pendingTo = clampPending(todayEnd);
        } else if (kind === "all") {
          pendingFrom = null;
          pendingTo = null;
          fromDate = null;
          toDate = null;
          syncHidden();
          syncTriggerLabel();
          closePop();
          host.dispatchEvent(new CustomEvent("pw-date-range:reset", { bubbles: true, composed: true }));
          host.dispatchEvent(new CustomEvent("pw-date-range:apply", {
            bubbles: true, composed: true,
            detail: { from: "", to: "", allRange: true },
          }));
          return;
        }
        if (pendingFrom) leftMonth = startOfMonth(pendingFrom);
        awaiting = "from";
        renderAll();
      });
    }

    btnCancel.addEventListener("click", function () { closePop(); });
    btnApply.addEventListener("click", function () { applyPending(); });

    pop.addEventListener("click", function (e) {
      e.stopPropagation();
    });

    document.addEventListener("click", function (e) {
      if (pop.hidden) return;
      if (host.contains(e.target)) return;
      closePop();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !pop.hidden) {
        closePop();
        trigger.focus();
      }
    });

    syncTriggerLabel();

    host.__pwDrp = {
      open: openPop,
      close: closePop,
      reset: resetAll,
      apply: applyPending,
      setLoading: function (on) {
        if (on) host.classList.add("is-loading"); else host.classList.remove("is-loading");
      },
      setRange: function (fromIso, toIso) {
        fromDate = (withTime ? parseISODateTime : parseISODate)(fromIso || "");
        toDate = (withTime ? parseISODateTime : parseISODate)(toIso || "");
        if (fromDate) fromDate = clamp(fromDate);
        if (toDate) toDate = clamp(toDate);
        if (fromDate && toDate && fromDate > toDate) toDate = new Date(fromDate.getTime());
        syncHidden();
        syncTriggerLabel();
      },
    };
  }

  function initAll(root) {
    injectStyles();
    var nodes = (root || document).querySelectorAll("[data-pw-date-range]");
    nodes.forEach(buildPicker);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initAll(document); });
  } else {
    initAll(document);
  }

  window.PwDateRangePicker = {
    init: initAll,
    refresh: function (el) { buildPicker(el); },
  };
})();
