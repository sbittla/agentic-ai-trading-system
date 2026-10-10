// Owner Console: live home (SSE, UI-22), countdowns, 5-s quote refresh while visible,
// keyboard shortcuts and theme (UI-25), confirmations. Served from 'self' (CSP).
(function () {
  "use strict";
  var root = document.documentElement;
  try { var t = localStorage.getItem("theme"); if (t) root.dataset.theme = t; } catch (e) {}

  function money(v) {
    if (v === null || v === undefined || v === "") return "—";
    var n = Number(v);
    return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined,
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function setK(key, text) {
    document.querySelectorAll('[data-k="' + key + '"]').forEach(function (el) {
      el.textContent = text;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var theme = document.getElementById("theme");
    if (theme) theme.addEventListener("click", function () {
      var next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem("theme", next); } catch (e) {}
    });

    document.querySelectorAll("form[data-confirm]").forEach(function (f) {
      f.addEventListener("submit", function (ev) {
        if (!window.confirm(f.dataset.confirm)) ev.preventDefault();
      });
    });

    // countdowns (approvals inbox, UI-10)
    function tick() {
      document.querySelectorAll("[data-expires]").forEach(function (el) {
        var left = Math.round((new Date(el.dataset.expires) - new Date()) / 1000);
        el.textContent = left > 0 ? left + " s" : "expired";
      });
    }
    tick(); setInterval(tick, 1000);

    // live home (SSE, at most 5 s behind the journal)
    if (document.querySelector('[data-live="home"]') && window.EventSource) {
      var es = new EventSource("/events");
      es.onmessage = function (m) {
        var d = JSON.parse(m.data), st = d.status || {};
        setK("equity", money(st.equity));
        setK("day_pnl", money(st.day_pnl));
        setK("trades_today", st.trades_today === undefined ? "—" : String(st.trades_today));
        setK("window", st.window || "none");
        Object.keys(st.health_detail || {}).forEach(function (k) {
          setK("health." + k, st.health_detail[k]);
        });
      };
    }

    // stock page quote refresh every 5 s while visible (UI-22)
    var q = document.querySelector("[data-symbol]");
    if (q) {
      var sym = q.dataset.symbol;
      setInterval(function () {
        if (document.visibilityState !== "visible") return;
        fetch("/api/quote?symbol=" + encodeURIComponent(sym), { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (d) {
            if (!d) return;
            q.querySelectorAll("[data-q]").forEach(function (el) {
              var v = d[el.dataset.q];
              if (v === null || v === undefined) return;
              el.textContent = typeof v === "number" ? v.toFixed(el.dataset.q === "volume" ? 0 : 2) : v;
            });
          }).catch(function () {});
      }, 5000);
    }
  });

  // keyboard: "/" focuses search, "g h" goes home
  var lastG = 0;
  document.addEventListener("keydown", function (ev) {
    var tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (ev.key === "/") { var s = document.getElementById("search"); if (s) { ev.preventDefault(); s.focus(); } }
    else if (ev.key === "g") { lastG = Date.now(); }
    else if (ev.key === "h" && Date.now() - lastG < 1000) { window.location.href = "/"; }
  });
})();
