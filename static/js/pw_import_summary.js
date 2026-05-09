/**
 * Shared "Import summary" card markup (matches import_summary_macros.html).
 * Used by inline fetch() flows on import pages.
 */
(function (global) {
  function esc(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function infoIcon(title) {
    var t = esc(title || "");
    return (
      '<span class="inline-flex align-middle ml-0.5 text-slate-400" title="' +
      t +
      '">' +
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-3.5 h-3.5" aria-hidden="true">' +
      '<path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a.75.75 0 000 1.5h.253a.25.25 0 01.244.304l-.459 2.066A1.75 1.75 0 0010.747 15H11a.75.75 0 000-1.5h-.253a.25.25 0 01-.244-.304l.458-2.066A1.75 1.75 0 009.253 9H9z" clip-rule="evenodd" />' +
      "</svg></span>"
    );
  }

  function headerIconMarkup(variant) {
    if (variant === "neutral") {
      return (
        '<div class="shrink-0 w-11 h-11 rounded-full bg-slate-200 text-slate-600 flex items-center justify-center text-xl font-bold leading-none" aria-hidden="true">…</div>'
      );
    }
    if (variant === "error") {
      return (
        '<div class="shrink-0 w-11 h-11 rounded-full bg-rose-500 text-white flex items-center justify-center shadow-sm" aria-hidden="true">' +
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-6 h-6"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z" clip-rule="evenodd" /></svg>' +
        "</div>"
      );
    }
    if (variant === "warning") {
      return (
        '<div class="shrink-0 w-11 h-11 rounded-full bg-amber-400 text-white flex items-center justify-center shadow-sm font-bold text-lg leading-none" aria-hidden="true">!</div>'
      );
    }
    return (
      '<div class="shrink-0 w-11 h-11 rounded-full bg-emerald-500 text-white flex items-center justify-center shadow-sm" aria-hidden="true">' +
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-6 h-6"><path fill-rule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-7.5 10.5a.75.75 0 01-1.127.077l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 6.96-9.74a.75.75 0 011.05-.143z" clip-rule="evenodd" /></svg>' +
      "</div>"
    );
  }

  function rowIconMarkup(kind) {
    if (kind === "check") {
      return (
        '<span class="shrink-0 w-6 h-6 rounded-full bg-emerald-500 text-white flex items-center justify-center" aria-hidden="true">' +
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" class="w-3.5 h-3.5"><path fill-rule="evenodd" d="M16.704 4.153a.75.75 0 01.143 1.052l-7.5 10.5a.75.75 0 01-1.127.077l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 6.96-9.74a.75.75 0 011.05-.143z" clip-rule="evenodd" /></svg></span>'
      );
    }
    if (kind === "warn") {
      return (
        '<span class="shrink-0 w-6 h-6 rounded-full bg-amber-400 text-white flex items-center justify-center font-bold text-xs" aria-hidden="true">!</span>'
      );
    }
    return (
      '<span class="shrink-0 w-6 h-6 rounded-full bg-slate-200 text-slate-500 flex items-center justify-center text-xs font-semibold" aria-hidden="true">·</span>'
    );
  }

  function row(iconKind, labelHtml, valueHtml, opts) {
    opts = opts || {};
    var extra = opts.borderTop ? " border-t border-slate-100" : "";
    return (
      '<div class="flex items-start justify-between gap-4 py-3.5' +
      extra +
      '">' +
      '<div class="flex items-center gap-2.5 min-w-0 text-sm text-slate-700">' +
      rowIconMarkup(iconKind || "dot") +
      "<span>" +
      labelHtml +
      "</span></div>" +
      '<span class="tabular-nums text-sm font-medium text-slate-900 shrink-0">' +
      valueHtml +
      "</span></div>"
    );
  }

  function subrows(items) {
    if (!items || !items.length) return "";
    return items
      .map(function (it) {
        var info =
          it && it.infoTitle
            ? infoIcon(it.infoTitle)
            : "";
        return (
          '<div class="flex items-start justify-between gap-4 py-2 pl-9 text-xs text-slate-500">' +
          '<span class="flex items-center gap-1.5 min-w-0"><span class="text-slate-400 shrink-0" aria-hidden="true">↳</span>' +
          (it && it.label ? esc(it.label) : "") +
          info +
          "</span>" +
          '<span class="tabular-nums shrink-0 text-slate-600">' +
          (it && it.value != null ? esc(String(it.value)) : "") +
          "</span></div>"
        );
      })
      .join("");
  }

  function totalRow(labelHtml, valueHtml, infoTitle) {
    var info = infoTitle ? infoIcon(infoTitle) : "";
    return (
      '<div class="flex items-center justify-between gap-4 py-4 mt-1 border-t border-slate-200">' +
      '<span class="text-sm font-medium text-slate-800 flex items-center gap-0.5">' +
      labelHtml +
      info +
      "</span>" +
      '<span class="text-base font-semibold tabular-nums text-slate-900">' +
      valueHtml +
      "</span></div>"
    );
  }

  function card(cfg) {
    cfg = cfg || {};
    var variant = cfg.variant || "success";
    var title = esc(cfg.title || "Import summary");
    var sectionLabel = esc(cfg.sectionLabel || "Rows details");
    var metaLeft = cfg.metaLeft != null ? String(cfg.metaLeft) : "";
    var importId = cfg.importId;
    var idLabel = esc(cfg.importIdLabel || "Import ID");
    var metaRight = "";
    if (importId != null && String(importId).length) {
      metaRight =
        '<span class="text-xs text-slate-400 tabular-nums">' +
        idLabel +
        ': <span class="text-slate-500 font-medium">' +
        esc(String(importId)) +
        "</span></span>";
    }
    var innerRows = cfg.rowsHtml || "";
    var afterRows = cfg.afterRowsHtml || "";
    var footer = cfg.footerHtml || "";

    return (
      '<div class="pw-import-summary-card rounded-2xl border border-slate-200/90 bg-white shadow-sm overflow-hidden max-w-xl mx-auto">' +
      '<div class="px-8 py-8 sm:px-10 sm:py-9">' +
      '<div class="flex gap-4">' +
      headerIconMarkup(variant) +
      '<div class="min-w-0 flex-1">' +
      '<h3 class="text-lg font-semibold text-slate-900 leading-tight tracking-tight">' +
      title +
      "</h3>" +
      '<p class="text-sm font-semibold text-slate-800 mt-5">' +
      sectionLabel +
      "</p>" +
      '<div class="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 mt-1.5">' +
      '<span class="text-sm text-slate-500">' +
      esc(metaLeft) +
      "</span>" +
      metaRight +
      "</div>" +
      "</div>" +
      "</div>" +
      '<div class="mt-6 border-t border-slate-100">' +
      innerRows +
      "</div>" +
      afterRows +
      (footer
        ? '<div class="flex justify-end gap-3 mt-8 pt-1 flex-wrap items-center">' + footer + "</div>"
        : "") +
      "</div></div>"
    );
  }

  global.pwImportSummaryEsc = esc;
  global.pwImportSummaryInfoIcon = infoIcon;
  global.pwImportSummaryCard = card;
  global.pwImportSummaryRow = row;
  global.pwImportSummarySubrows = subrows;
  global.pwImportSummaryTotalRow = totalRow;
})(typeof window !== "undefined" ? window : this);
