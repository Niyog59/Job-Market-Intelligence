// Progressive enhancement only: every page works without this script (both tabs and all
// evidence are then simply shown).
(function () {
  "use strict";

  function store(action) {
    try { return action(window.sessionStorage); } catch (e) { return null; }
  }

  // Tabs (ARIA tab pattern): click or Left/Right/Home/End; the hash keeps the open tab.
  function setupTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      });
      if (focus) tab.focus();
      if (history.replaceState) history.replaceState(null, "", "#" + tab.getAttribute("aria-controls"));
    }
    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function (event) { event.preventDefault(); select(tab, false); });
      tab.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
        else if (event.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (event.key === "Home") next = tabs[0];
        else if (event.key === "End") next = tabs[tabs.length - 1];
        if (next) { event.preventDefault(); select(next, true); }
      });
    });
    var wanted = tabs.filter(function (t) { return "#" + t.getAttribute("aria-controls") === location.hash; })[0];
    select(wanted || tabs[0], false);
    if (!wanted && history.replaceState) history.replaceState(null, "", location.pathname + location.search);
  }

  // "View source evidence" buttons open and close the quotes below their row.
  function setupEvidence() {
    document.querySelectorAll("[data-evidence]").forEach(function (button) {
      var panel = document.getElementById(button.getAttribute("aria-controls"));
      if (!panel) return;
      panel.hidden = true;
      button.setAttribute("aria-expanded", "false");
      button.addEventListener("click", function () {
        var open = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", open ? "false" : "true");
        button.textContent = open ? "View source evidence" : "Hide source evidence";
        panel.hidden = open;
      });
    });
  }

  // Back to results restores the scroll position the reader left the results at. (The
  // browser's own Back button restores it by itself.)
  function setupScroll() {
    var key = "scroll:" + location.search;
    document.querySelectorAll("[data-detail-link]").forEach(function (link) {
      link.addEventListener("click", function () {
        store(function (s) { s.setItem(key, String(window.scrollY)); });
      });
    });
    var back = document.querySelector("[data-back]");
    if (back) back.addEventListener("click", function () {
      store(function (s) { s.setItem("restore-scroll", "1"); });
    });
    if (document.body.classList.contains("results-page")) {
      var restore = store(function (s) { return s.getItem("restore-scroll"); });
      var y = store(function (s) { return s.getItem(key); });
      store(function (s) { s.removeItem("restore-scroll"); });
      if (restore && y !== null) window.scrollTo(0, parseInt(y, 10) || 0);
    }
  }

  function setupForm() {
    var form = document.getElementById("search");
    if (!form) return;
    form.querySelectorAll("[data-autosubmit]").forEach(function (control) {
      control.addEventListener("change", function () {
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      });
    });
    form.addEventListener("submit", function () {
      var loading = form.querySelector("[data-loading]");
      if (loading) loading.hidden = false;
    });
    // On a phone the filters start folded so the results come first.
    var filters = document.getElementById("filters");
    if (filters && window.matchMedia && window.matchMedia("(max-width: 820px)").matches) {
      filters.open = false;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupTabs();
    setupEvidence();
    setupScroll();
    setupForm();
  });
})();
