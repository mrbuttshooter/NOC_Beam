"use strict";
// NOC_Beam web softphone -- client logic.
//
// Security (brief rule): NEVER assign dynamic data via innerHTML. Every row /
// menu item is built with createElement + textContent so a hostile peer URI
// or account label can't inject markup.

// ---- tiny DOM helper (from the prototype) ---------------------------------
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const $ = (id) => document.getElementById(id);

// ---- bridge handle (populated after the QWebChannel handshake) ------------
let bridge = null;

// ---- app state (last pushed snapshot) -------------------------------------
const state = {
  call: null,
  accounts: { accounts: [], activeId: "", activeLabel: "No account", activeHealth: "muted" },
  suppliers: { visible: false, activeId: "", suppliers: [] },
  recents: [],
};

// ==========================================================================
// Keypad
// ==========================================================================
const KEYS = [["1", ""], ["2", "ABC"], ["3", "DEF"], ["4", "GHI"], ["5", "JKL"],
  ["6", "MNO"], ["7", "PQRS"], ["8", "TUV"], ["9", "WXYZ"], ["*", ""], ["0", "+"], ["#", ""]];

function buildPad() {
  const pad = $("pad");
  const num = $("num");
  for (const [d, c] of KEYS) {
    const k = el("div", "key");
    k.appendChild(el("div", "d", d));
    k.appendChild(el("div", "c", c));
    k.addEventListener("click", () => {
      // Mirror ui/phone_shell.py:_on_digit_pressed -- in-call digits are DTMF,
      // idle digits build the dial string.
      if (state.call) {
        if (bridge) bridge.send_dtmf(d);
      } else {
        num.value += d;
        num.focus();
      }
    });
    pad.appendChild(k);
  }
}

// ==========================================================================
// Dialing
// ==========================================================================
function placeCall() {
  const num = $("num");
  const t = (num.value || "").trim();
  if (!t || !bridge) return;
  bridge.place_call(t);
  num.value = "";
}

// ==========================================================================
// Live call card
// ==========================================================================
let timerHandle = null;

function fmtElapsed(anchorMs) {
  if (!anchorMs) return "";
  let s = Math.max(0, Math.floor((Date.now() - anchorMs) / 1000));
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function renderCall(c) {
  const live = $("live");
  if (timerHandle) { clearInterval(timerHandle); timerHandle = null; }
  if (!c) {
    live.hidden = true;
    return;
  }
  live.hidden = false;
  $("live-num").textContent = c.peer || "";
  $("live-via").textContent = c.via ? ("via " + c.via) : "";
  $("live-via").style.display = c.via ? "" : "none";

  // Chip: label + optional live timer span.
  const chip = $("live-chip");
  chip.className = "chip " + (c.level || "prog");
  chip.textContent = "";
  chip.appendChild(document.createTextNode(c.chip || ""));
  if (c.showTimer && c.anchorMs) {
    const t = el("span", "t", fmtElapsed(c.anchorMs));
    chip.appendChild(t);
    timerHandle = setInterval(() => { t.textContent = fmtElapsed(c.anchorMs); }, 1000);
  }

  // Controls: incoming shows Answer/Reject, otherwise the active row.
  const active = $("controls-active");
  const incoming = $("controls-incoming");
  if (c.incoming) {
    active.hidden = true;
    incoming.hidden = false;
  } else {
    active.hidden = false;
    incoming.hidden = true;
    $("btn-mute").classList.toggle("on", !!c.muted);
    $("btn-hold").classList.toggle("on", !!c.held);
    $("btn-hold").title = c.held ? "Resume" : "Hold";
    // Mute/hold/transfer are only meaningful once media can exist.
    for (const id of ["btn-mute", "btn-hold", "btn-transfer"]) {
      $(id).disabled = !c.canControl;
      $(id).style.opacity = c.canControl ? "" : ".4";
    }
  }
}

// ==========================================================================
// Account pill + switcher
// ==========================================================================
function renderAccounts(a) {
  $("account-label").textContent = a.activeLabel || "No account";
  const dot = $("account-dot");
  dot.className = "dot " + (a.activeHealth || "muted");
}

function openAccountMenu() {
  const menu = $("account-menu");
  menu.textContent = "";
  const a = state.accounts;
  if (!a.accounts.length) {
    menu.appendChild(el("div", "item", "No accounts"));
  } else {
    for (const acc of a.accounts) {
      const item = el("div", "item" + (acc.id === a.activeId ? " active" : ""));
      item.appendChild(el("span", "dot " + (acc.health || "muted")));
      item.appendChild(el("span", "lbl", acc.label));
      item.addEventListener("click", () => {
        if (bridge) bridge.select_account(acc.id);
        closeMenus();
      });
      menu.appendChild(item);
    }
  }
  const sep = el("div", "sep"); menu.appendChild(sep);
  const settings = el("div", "item");
  settings.appendChild(el("span", "lbl", "Account settings…"));
  settings.addEventListener("click", () => { if (bridge) bridge.open_window("accounts"); closeMenus(); });
  menu.appendChild(settings);
  positionMenu(menu, $("account-pill"));
}

// ==========================================================================
// Supplier selector
// ==========================================================================
function renderSuppliers(s) {
  const row = $("supplier-row");
  row.hidden = !s.visible;
  if (!s.visible) return;
  const active = s.suppliers.find((x) => x.id === s.activeId);
  $("supplier-label").textContent = active ? active.label : (s.suppliers[0] ? s.suppliers[0].label : "—");
}

function openSupplierMenu() {
  const menu = $("supplier-menu");
  menu.textContent = "";
  const s = state.suppliers;
  if (!s.suppliers.length) {
    menu.appendChild(el("div", "item", "No suppliers"));
  } else {
    for (const sup of s.suppliers) {
      const item = el("div", "item" + (sup.id === s.activeId ? " active" : ""));
      item.appendChild(el("span", "lbl", sup.label));
      item.addEventListener("click", () => {
        if (bridge) bridge.select_supplier(sup.id);
        closeMenus();
      });
      menu.appendChild(item);
    }
  }
  positionMenu(menu, $("supplier-sel"), true);
}

// ==========================================================================
// Hamburger app menu
// ==========================================================================
const APP_MENU = [
  ["Settings", "settings"],
  ["Accounts", "accounts"],
  ["SIP trace", "trace"],
  ["Test runner", "test-runner"],
  ["sep", null],
  ["History", "history"],
  ["Contacts", "contacts"],
  ["Favorites", "favorites"],
  ["sep", null],
  ["Quit", "quit"],
];

function openAppMenu() {
  const menu = $("app-menu");
  menu.textContent = "";
  for (const [label, target] of APP_MENU) {
    if (label === "sep") { menu.appendChild(el("div", "sep")); continue; }
    const item = el("div", "item");
    item.appendChild(el("span", "lbl", label));
    item.addEventListener("click", () => { if (bridge) bridge.open_window(target); closeMenus(); });
    menu.appendChild(item);
  }
  positionMenu(menu, $("btn-menu"), true);
}

// ==========================================================================
// Menu positioning + dismissal (one open at a time)
// ==========================================================================
function closeMenus() {
  for (const id of ["account-menu", "app-menu", "supplier-menu"]) $(id).hidden = true;
}

function positionMenu(menu, anchor, alignRight) {
  closeMenusExcept(menu);
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  let left = alignRight ? (r.right - mw) : r.left;
  left = Math.max(6, Math.min(left, window.innerWidth - mw - 6));
  menu.style.left = left + "px";
  menu.style.top = (r.bottom + 4) + "px";
}
function closeMenusExcept(keep) {
  for (const id of ["account-menu", "app-menu", "supplier-menu"]) {
    const m = $(id);
    if (m !== keep) m.hidden = true;
  }
}

// ==========================================================================
// Recents
// ==========================================================================
function renderRecents(rows) {
  const box = $("recents");
  box.textContent = "";
  if (!rows.length) {
    box.appendChild(el("div", "empty", "No recent calls yet."));
    return;
  }
  for (const r of rows) {
    const row = el("div", "row");
    const arrowGlyph = r.dir === "in" ? "↙" : "↗";
    row.appendChild(el("span", "arrow " + r.level, arrowGlyph));
    row.appendChild(el("span", "n", r.num));
    row.appendChild(el("span", "badge " + r.level, r.status));
    row.appendChild(el("span", "w", r.time));
    const redial = el("button", "redial", "📞");
    row.appendChild(redial);
    const dial = () => { if (bridge) bridge.redial(r.uri); };
    redial.addEventListener("click", (e) => { e.stopPropagation(); dial(); });
    row.addEventListener("click", dial);
    box.appendChild(row);
  }
}

// ==========================================================================
// State entry point (called from Python via runJavaScript)
// ==========================================================================
window.nb = {
  apply(patch) {
    if (!patch) return;
    if ("call" in patch) { state.call = patch.call; renderCall(state.call); }
    if ("accounts" in patch) { state.accounts = patch.accounts; renderAccounts(state.accounts); }
    if ("suppliers" in patch) { state.suppliers = patch.suppliers; renderSuppliers(state.suppliers); }
    if ("recents" in patch) { state.recents = patch.recents; renderRecents(state.recents); }
  },
};

// ==========================================================================
// Wire static controls
// ==========================================================================
function wireControls() {
  buildPad();

  $("btn-call").addEventListener("click", placeCall);
  $("num").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); placeCall(); }
  });

  $("btn-end").addEventListener("click", () => bridge && bridge.hangup());
  $("btn-mute").addEventListener("click", () => bridge && bridge.toggle_mute());
  $("btn-hold").addEventListener("click", () => bridge && bridge.toggle_hold());
  $("btn-transfer").addEventListener("click", () => {
    const t = window.prompt("Transfer to (number or SIP URI):");
    if (t && t.trim() && bridge) bridge.transfer(t.trim());
  });
  $("btn-answer").addEventListener("click", () => bridge && bridge.answer());
  $("btn-reject").addEventListener("click", () => bridge && bridge.reject());

  $("view-all").addEventListener("click", () => bridge && bridge.open_window("history"));

  // Chrome buttons
  $("btn-min").addEventListener("click", () => bridge && bridge.minimize());
  $("btn-close").addEventListener("click", () => bridge && bridge.close_win());
  $("btn-menu").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("app-menu", openAppMenu); });
  $("account-pill").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("account-menu", openAccountMenu); });
  $("supplier-sel").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu("supplier-menu", openSupplierMenu); });

  // Title-bar drag: mousedown on the chrome (but not on a button) hands the
  // move to the OS via bridge.start_move() (brief §web_shell).
  $("chrome").addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest("button, .account, .menu")) return;
    if (bridge) bridge.start_move();
  });
  // Double-click chrome toggles maximize/restore.
  $("chrome").addEventListener("dblclick", (e) => {
    if (e.target.closest("button, .account, .menu")) return;
    if (bridge) bridge.toggle_max_restore();
  });

  // Dismiss menus on any outside click / Escape.
  document.addEventListener("click", closeMenus);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeMenus(); return; }
    if (e.key === "/") { e.preventDefault(); $("num").focus(); }
    else if (/^[0-9*#+]$/.test(e.key) && document.activeElement !== $("num") && !state.call) {
      $("num").focus(); $("num").value += e.key;
    }
  });
}

function toggleMenu(id, opener) {
  const m = $(id);
  if (!m.hidden) { m.hidden = true; return; }
  opener();
}

// ==========================================================================
// Boot: QWebChannel handshake
// ==========================================================================
function boot() {
  wireControls();
  if (typeof qt === "undefined" || !qt.webChannelTransport) {
    // Running the raw file without Qt (design preview) -- still interactive.
    return;
  }
  new QWebChannel(qt.webChannelTransport, (channel) => {
    bridge = channel.objects.bridge;
    bridge.ready();
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
