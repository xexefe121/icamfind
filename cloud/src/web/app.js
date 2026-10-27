"use strict";

// iCamFind web app. Plain ES2020, no build step. Same-origin HTTP API (docs/API.md); the browser can
// also be a camera through window.ICamEdge from edge.js (docs/WEB_CAMERA.md).
(() => {
  const POLL_STATE_MS = 5000;
  const POLL_EVENTS_MS = 10000;
  const POLL_JOB_MS = 1000;
  const JOB_TIMEOUT_MS = 120000;
  const URL_REUSE_MS = 45 * 60 * 1000; // presigned URLs last 1 h; reuse to avoid image flicker
  const VIEWS = ["ask", "camera", "things", "routines"];
  const PROC_W = 640; // processing frame width used by the camera engine and zones
  const ESSENTIALS = ["keys", "wallet", "phone", "glasses", "pill", "medication"];

  const TOOL_NAMES = {
    list_objects: "Checked memory",
    get_object_history: "Read history",
    verify_object_live: "Looked with the camera",
    scan_room_for_object: "Scanned the room",
    update_object_status: "Updated memory",
    speak_on_device: "Spoke to you",
    request_caregiver_notification: "Asked the carer's OK",
    record_routine_status: "Noted the result",
    final_answer: "Answer",
  };

  const EVENT_TYPES = {
    placed: { text: "Put down", icon: "＋" },
    removed: { text: "Picked up", icon: "−" },
    moved: { text: "Moved", icon: "→" },
    verified: { text: "Checked", icon: "✓" },
    corrected: { text: "Corrected", icon: "✎" },
  };

  const ROUTINE_STATUS = {
    ok: ["On track", "status-ok"],
    missed: ["Missed", "status-danger"],
    reminded: ["Reminder given", "status-warn"],
    escalated: ["Carer asked", "status-danger"],
  };

  const state = {
    config: null, // {cognito_domain, client_id, redirect_uri, region} from config.json
    auth: null, // {id_token, refresh_token, expires_at}
    me: null,
    households: [],
    household: null, // selected household id (sent as x-icamfind-household)
    householdInfo: null,
    loaded: false, // first /api/state for the current home has arrived
    devices: [],
    device: null,
    objects: [],
    alerts: [],
    approvals: [],
    routines: [],
    events: [],
    filter: "all",
    editingId: null,
    view: "ask",
    job: null,
    jobTarget: "ask", // "ask" (answer card on Ask) or "dialog" (routine check sheet)
    steps: [],
    jobTimer: null,
    jobStarted: 0,
    openSteps: new Set(),
    spokenJobs: new Set(),
    timers: {},
    dialogReturnFocus: null,
    snapForce: false,
    here: null, // "Here it is" picture for the current answer
    drawerHere: null, // "Show me where" picture in the thing's detail sheet
    detailObject: null,
  };

  // Browser camera (this computer watching the room).
  const cam = {
    stream: null,
    engine: null,
    household: null,
    status: { state: "off" },
    paused: false,
    starting: false,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ---------- Small helpers ----------

  const store = {
    get(key) {
      try { return localStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try {
        if (value === null || value === undefined) localStorage.removeItem(key);
        else localStorage.setItem(key, value);
      } catch { /* storage unavailable: settings just won't persist */ }
    },
    json(key) {
      try { return JSON.parse(this.get(key) || "null"); } catch { return null; }
    },
  };

  const session = {
    get(key) {
      try { return sessionStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try {
        if (value === null || value === undefined) sessionStorage.removeItem(key);
        else sessionStorage.setItem(key, value);
        return true;
      } catch { return false; }
    },
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function safeUrl(u) {
    return typeof u === "string" && /^https?:\/\//i.test(u) ? u : "";
  }

  function safeDataImage(u) {
    return typeof u === "string" && /^data:image\/(jpeg|png|webp);base64,/i.test(u) ? u : "";
  }

  // Presigned URLs change every poll; keep the first one per object key while it is still valid.
  const imgCache = new Map();
  function stableUrl(u) {
    u = safeUrl(u);
    if (!u) return "";
    const key = u.split("?")[0];
    const hit = imgCache.get(key);
    const now = Date.now();
    if (hit && now - hit.at < URL_REUSE_MS) return hit.url;
    imgCache.set(key, { url: u, at: now });
    return u;
  }

  // The room snapshot can be replaced in place, so refresh it at least every minute or on demand.
  let snapCache = { key: "", url: "", at: 0 };
  function snapshotUrl(u) {
    u = safeUrl(u);
    if (!u) return "";
    const key = u.split("?")[0];
    const now = Date.now();
    if (key === snapCache.key && !state.snapForce && now - snapCache.at < 60000) return snapCache.url;
    snapCache = { key, url: u, at: now };
    state.snapForce = false;
    return u;
  }

  function relTime(ts) {
    if (!ts) return "never";
    const s = Math.round(Date.now() / 1000 - ts);
    if (s < 45) return "just now";
    if (s < 90) return "1 min ago";
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min ago`;
    const h = Math.round(m / 60);
    if (h < 24) return h === 1 ? "1 hour ago" : `${h} hours ago`;
    const d = Math.round(h / 24);
    return d === 1 ? "yesterday" : `${d} days ago`;
  }

  function absTime(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString([], {
      weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit",
    });
  }

  function clock(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function timeTag(ts) {
    if (!ts) return "<span>never</span>";
    const iso = new Date(ts * 1000).toISOString();
    return `<time datetime="${iso}" title="${esc(absTime(ts))}">${esc(relTime(ts))}</time>`;
  }

  function zoneName(z) {
    return z && z !== "unzoned" ? z : "";
  }

  function whereText(o) {
    const z = zoneName(o.zone);
    if (o.status === "removed") return z ? `Picked up from the ${z}` : "Picked up";
    return z ? `On the ${z}` : "In view of the camera";
  }

  function whereWhen(o) {
    const z = zoneName(o.zone);
    if (o.status === "removed") return `Picked up ${relTime(o.last_seen)}${z ? ` from the ${z}` : ""}`;
    return `${z ? `On the ${z}` : "In view"} · ${relTime(o.last_seen)}`;
  }

  function labelOf(o) {
    const name = (o && (o.label || o.local_label)) || "";
    return name && name.toLowerCase() !== "object" ? name : "Unnamed thing";
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }

  function fkeySel(key) {
    return `[data-fkey="${window.CSS && CSS.escape ? CSS.escape(key) : key}"]`;
  }

  // Replace innerHTML only when it changed, keeping keyboard focus on the same logical control.
  function setHtml(el, html) {
    if (!el || el._html === html) return;
    const active = document.activeElement;
    const fkey = active && el.contains(active) ? active.getAttribute("data-fkey") : null;
    el.innerHTML = html;
    el._html = html;
    if (fkey) {
      const again = el.querySelector(fkeySel(fkey));
      if (again) again.focus();
    }
  }

  let toastTimer = null;
  let lastToast = { msg: "", at: 0 };
  function toast(msg) {
    const now = Date.now();
    if (msg === lastToast.msg && now - lastToast.at < 10000) return; // don't repeat during polling
    lastToast = { msg, at: now };
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
  }

  async function copyText(text, label) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast(`${label} copied.`);
      return;
    } catch { /* fall back below */ }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    toast(ok ? `${label} copied.` : "Couldn't copy automatically. Select the text and copy it.");
  }

  function openDialog(dialog) {
    state.dialogReturnFocus = document.activeElement;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function closeDialog(dialog) {
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else if (dialog.hasAttribute("open")) { dialog.removeAttribute("open"); dialog.dispatchEvent(new Event("close")); }
  }

  function returnFocus() {
    const back = state.dialogReturnFocus;
    state.dialogReturnFocus = null;
    if (back && document.contains(back)) back.focus();
  }

  // ---------- Sign-in: Cognito hosted UI, authorization code + PKCE ----------

  const AUTH_KEY = "icamfind.auth";
  const PKCE_KEY = "icamfind.pkce";
  const HOME_KEY = "icamfind.household";
  const DISMISS_KEY = "icamfind.dismissed";

  function b64url(bytes) {
    let s = "";
    bytes.forEach((b) => { s += String.fromCharCode(b); });
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function randomString(nBytes) {
    const a = new Uint8Array(nBytes);
    crypto.getRandomValues(a);
    return b64url(a);
  }

  async function pkceChallenge(verifier) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    return b64url(new Uint8Array(digest));
  }

  function jwtExpiryMs(jwt) {
    try {
      const part = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const bin = atob(part + "===".slice((part.length + 3) % 4));
      const claims = JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
      return typeof claims.exp === "number" ? claims.exp * 1000 : 0;
    } catch { return 0; }
  }

  function loadAuth() {
    const a = store.json(AUTH_KEY);
    return a && a.id_token ? a : null;
  }

  function saveAuth(a) {
    state.auth = a;
    store.set(AUTH_KEY, a ? JSON.stringify(a) : null);
  }

  function authSet(resp, previous) {
    const fallback = Date.now() + (Number(resp.expires_in) || 3600) * 1000;
    return {
      id_token: resp.id_token,
      refresh_token: resp.refresh_token || (previous && previous.refresh_token) || null,
      expires_at: jwtExpiryMs(resp.id_token) || fallback,
    };
  }

  async function authRequest(params) {
    const res = await fetch(`${state.config.cognito_domain}/oauth2/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(params).toString(),
    });
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON error page */ }
    if (!res.ok || !data || !data.id_token) {
      throw new Error((data && (data.error_description || data.error)) || `Sign-in failed (${res.status})`);
    }
    return data;
  }

  async function beginLogin() {
    if (!state.config) return;
    const verifier = randomString(48); // 64 base64url characters
    const st = randomString(24);
    const saved = JSON.stringify({ verifier, state: st });
    // sessionStorage first; fall back to localStorage so the redirect round trip still works.
    if (!session.set(PKCE_KEY, saved)) store.set(PKCE_KEY, saved);
    const cfg = state.config;
    const q = new URLSearchParams({
      response_type: "code",
      client_id: cfg.client_id,
      redirect_uri: cfg.redirect_uri,
      scope: "openid email profile",
      code_challenge: await pkceChallenge(verifier),
      code_challenge_method: "S256",
      state: st,
    });
    location.assign(`${cfg.cognito_domain}/oauth2/authorize?${q.toString()}`);
  }

  async function handleCallback(params) {
    let saved = null;
    try { saved = JSON.parse(session.get(PKCE_KEY) || store.get(PKCE_KEY) || "null"); } catch { saved = null; }
    session.set(PKCE_KEY, null);
    store.set(PKCE_KEY, null);
    if (!saved || !saved.verifier || saved.state !== params.get("state")) {
      throw new Error("That sign-in link didn't match or has expired. Please sign in again.");
    }
    const data = await authRequest({
      grant_type: "authorization_code",
      client_id: state.config.client_id,
      code: params.get("code"),
      redirect_uri: state.config.redirect_uri,
      code_verifier: saved.verifier,
    });
    saveAuth(authSet(data, null));
  }

  let refreshing = null;
  function refreshAuth() {
    if (refreshing) return refreshing;
    const current = state.auth;
    if (!current || !current.refresh_token || !state.config) return Promise.resolve(false);
    refreshing = authRequest({
      grant_type: "refresh_token",
      client_id: state.config.client_id,
      refresh_token: current.refresh_token,
    })
      .then((data) => { saveAuth(authSet(data, current)); return true; })
      .catch(() => false)
      .finally(() => { refreshing = null; });
    return refreshing;
  }

  async function freshIdToken() {
    if (!state.auth) return null;
    if (state.auth.expires_at - Date.now() < 60000 && !(await refreshAuth())) return null;
    return state.auth ? state.auth.id_token : null;
  }

  // Local session ended (expired or refresh failed): back to the sign-in screen.
  function expireSession(message) {
    saveAuth(null);
    stopJob();
    stopBrowserCamera();
    showLogin(message);
  }

  // User-initiated sign-out also ends the hosted-UI session.
  function signOut() {
    const cfg = state.config;
    saveAuth(null);
    stopJob();
    stopPolling();
    stopBrowserCamera();
    if (cfg && cfg.cognito_domain) {
      const q = new URLSearchParams({ client_id: cfg.client_id, logout_uri: cfg.redirect_uri });
      location.assign(`${cfg.cognito_domain}/logout?${q.toString()}`);
    } else {
      showLogin("");
    }
  }

  // ---------- API ----------

  async function api(path, opts = {}, retried = false) {
    const idToken = await freshIdToken();
    if (!idToken) {
      if ($("#login").hidden) expireSession("Your session has ended. Please sign in again.");
      throw new Error("unauthorized");
    }
    const headers = { authorization: `Bearer ${idToken}` };
    if (state.household && !opts.noHousehold) headers["x-icamfind-household"] = state.household;
    const init = { method: opts.method || "GET", headers };
    if (opts.body !== undefined) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      if (!opts.quiet) toast("Can't reach iCamFind right now. Check the internet connection.");
      throw err;
    }
    if (res.status === 401) {
      if (!retried && (await refreshAuth())) return api(path, opts, true);
      if ($("#login").hidden) expireSession("Your session has ended. Please sign in again.");
      throw new Error("unauthorized");
    }
    let data = null;
    try { data = await res.json(); } catch { /* empty or non-JSON body */ }
    if (!res.ok) {
      const msg = (data && data.error) || `Something went wrong (${res.status})`;
      if (res.status === 403 || !opts.quiet) toast(msg);
      throw new Error(msg);
    }
    return data || {};
  }

  // ---------- Session screens ----------

  function showLogin(message) {
    stopPolling();
    $("#app").hidden = true;
    $("#login").hidden = false;
    $("#login-status").hidden = true;
    const err = $("#login-error");
    err.textContent = message || "";
    err.hidden = !message;
    const btn = $("#login-btn");
    btn.disabled = !state.config;
    btn.hidden = false;
    if (!btn.disabled) btn.focus();
  }

  function showLoginStatus(text) {
    $("#app").hidden = true;
    $("#login").hidden = false;
    $("#login-error").hidden = true;
    $("#login-btn").hidden = true;
    const s = $("#login-status");
    s.textContent = text;
    s.hidden = false;
  }

  async function startApp() {
    $("#login").hidden = true;
    $("#app").hidden = false;
    showView(viewFromHash(), false);
    try {
      await loadMe();
    } catch {
      return; // unauthorized: the sign-in screen is already showing
    }
    renderAll();
    refreshState();
    refreshEvents();
    startPolling();
    resumeBrowserCameraIfSetUp();
  }

  async function loadMe() {
    try {
      const d = await api("api/me", { quiet: true, noHousehold: true });
      state.me = d.user || null;
      state.households = Array.isArray(d.households) ? d.households : [];
      const ids = state.households.map((h) => h.id);
      const pref = store.get(HOME_KEY);
      state.household = ids.includes(pref) ? pref : (ids.includes(d.current) ? d.current : (ids[0] || null));
      store.set(HOME_KEY, state.household);
      renderHouseholdControls();
    } catch (err) {
      if (err && err.message === "unauthorized") throw err;
      toast("Couldn't load your account details. Showing your default home.");
    }
  }

  // ---------- Homes ----------

  function currentHousehold() {
    return state.households.find((h) => h.id === state.household) || null;
  }

  function isDemo() {
    const h = currentHousehold();
    if (h) return !!h.demo;
    return !!(state.householdInfo && state.householdInfo.demo);
  }

  function isOwner() {
    const h = currentHousehold();
    return !!(h && h.role === "owner");
  }

  function ownHome() {
    return state.households.find((h) => !h.demo && h.role === "owner") || state.households.find((h) => !h.demo) || null;
  }

  function demoHome() {
    return state.households.find((h) => h.demo) || null;
  }

  function householdName(id) {
    const h = state.households.find((x) => x.id === id);
    return (h && h.name) || "this home";
  }

  function renderHouseholdControls() {
    const sel = $("#household-select");
    const html = state.households
      .map((h) => `<option value="${esc(h.id)}">${esc(h.name || "Home")}</option>`)
      .join("");
    if (sel._html !== html) {
      sel.innerHTML = html;
      sel._html = html;
    }
    if (state.household && sel.value !== state.household) sel.value = state.household;
    $("#household-wrap").hidden = state.households.length === 0;
    $("#demo-badge").hidden = !isDemo();

    const email = state.me && state.me.email;
    const u = $("#user-email");
    u.textContent = email ? `Signed in as ${email}` : "";
    u.hidden = !email;
  }

  // Everything below the top bar belongs to one home; drop it before switching.
  function resetHouseholdState() {
    stopJob();
    cancelHere(state.here);
    cancelHere(state.drawerHere);
    state.here = null;
    state.drawerHere = null;
    state.job = null;
    state.steps = [];
    state.openSteps = new Set();
    state.householdInfo = null;
    state.loaded = false;
    state.device = null;
    state.devices = [];
    state.objects = [];
    state.alerts = [];
    state.approvals = [];
    state.routines = [];
    state.events = [];
    state.editingId = null;
    imgCache.clear();
    snapCache = { key: "", url: "", at: 0 };
    $("#job-area").hidden = true;
    $("#answer-card").hidden = true;
    $("#answer-tools").hidden = true;
    setAskBusy(false);
    [$("#object-dialog"), $("#job-dialog"), $("#pair-dialog")].forEach((d) => closeDialog(d));
  }

  function switchHousehold(id) {
    if (!id || id === state.household) return;
    // A running camera keeps watching its own home while you look at another one.
    if (cam.engine && cam.household && cam.household !== id && camConfig(id)) stopBrowserCamera();
    state.household = id;
    store.set(HOME_KEY, id);
    resetHouseholdState();
    renderHouseholdControls();
    renderAll();
    refreshState();
    refreshEvents();
    resumeBrowserCameraIfSetUp();
    toast(`Switched to ${householdName(id)}.`);
  }

  function startPolling() {
    stopPolling();
    state.timers.state = setInterval(() => { if (!document.hidden) refreshState(); }, POLL_STATE_MS);
    state.timers.events = setInterval(() => { if (!document.hidden) refreshEvents(); }, POLL_EVENTS_MS);
    state.timers.clock = setInterval(() => { if (!document.hidden) renderRelativeTimes(); }, 30000);
  }

  function stopPolling() {
    Object.values(state.timers).forEach((t) => clearInterval(t));
    state.timers = {};
  }

  async function refreshState() {
    const hh = state.household;
    try {
      const d = await api("api/state", { quiet: true });
      if (state.household !== hh) return; // home switched while this was in flight
      const first = !state.loaded;
      state.loaded = true;
      state.householdInfo = d.household || null;
      state.devices = Array.isArray(d.devices) ? d.devices : [];
      state.device = d.device || null;
      state.objects = Array.isArray(d.objects) ? d.objects : [];
      state.alerts = Array.isArray(d.alerts) ? d.alerts : [];
      state.approvals = Array.isArray(d.approvals) ? d.approvals : [];
      state.routines = Array.isArray(d.routines) ? d.routines : [];
      renderAll();
      if (first) maybeNudgeSetup();
    } catch { /* toast already shown for network errors */ }
  }

  async function refreshEvents() {
    const hh = state.household;
    try {
      const d = await api("api/events?limit=50", { quiet: true });
      if (state.household !== hh) return;
      state.events = Array.isArray(d.events) ? d.events : [];
      renderRecent();
    } catch { /* handled in api() */ }
  }

  let refreshSoonTimer = null;
  function refreshSoon() {
    clearTimeout(refreshSoonTimer);
    refreshSoonTimer = setTimeout(() => { refreshState(); refreshEvents(); }, 1500);
  }

  function renderAll() {
    renderHouseholdControls();
    renderBanners();
    renderRecent();
    renderCamera();
    renderThingsRoom();
    renderObjects();
    renderApprovals();
    renderRoutines();
    renderRoutineOptions();
    renderAlerts();
    renderTabDots();
    if (state.job) renderAnswer();
  }

  function renderRelativeTimes() {
    // Rebuild the lists that show "5 min ago"; setHtml skips unchanged markup.
    renderRecent();
    renderObjects();
    renderCamera();
  }

  // ---------- Views (hash routing) ----------

  function viewFromHash() {
    const h = location.hash.replace(/^#/, "");
    return VIEWS.includes(h) ? h : "ask";
  }

  function showView(name, focus) {
    state.view = name;
    $$(".view").forEach((v) => { v.hidden = v.getAttribute("data-view") !== name; });
    $$(".tab").forEach((t) => {
      if (t.getAttribute("data-view") === name) t.setAttribute("aria-current", "page");
      else t.removeAttribute("aria-current");
    });
    if (name === "camera") renderCamera();
    if (focus) {
      const h = $(`#view-${name} h1`);
      if (h) h.focus({ preventScroll: true });
      const main = $("#main");
      if (main.scrollTo) main.scrollTo(0, 0);
      window.scrollTo(0, 0);
    }
  }

  function go(view) {
    if (location.hash === `#${view}`) showView(view, true);
    else location.hash = `#${view}`;
  }

  function needsSetup() {
    return state.loaded && !isDemo() && isOwner() && !state.devices.length && !camConfig();
  }

  // First visit to your own home with no camera: open the Camera tab once per session.
  function maybeNudgeSetup() {
    if (!needsSetup()) return;
    const key = `icamfind.nudged.${state.household}`;
    if (session.get(key)) return;
    session.set(key, "1");
    if (state.view === "ask") go("camera");
  }

  function renderTabDots() {
    $("#tab-cam-dot").hidden = !(cam.engine && cam.status && cam.status.state === "watching");
    const pending = state.approvals.filter((a) => !a.status || a.status === "pending").length;
    $("#tab-routines-dot").hidden = pending === 0;
  }

  // ---------- Ask view: banners and recent activity ----------

  function dismissed() {
    const list = store.json(DISMISS_KEY);
    return new Set(Array.isArray(list) ? list : []);
  }

  function dismiss(key) {
    const set = dismissed();
    set.add(key);
    store.set(DISMISS_KEY, JSON.stringify(Array.from(set).slice(-200)));
    renderBanners();
  }

  function renderBanners() {
    const off = dismissed();
    const parts = [];
    if (isDemo()) {
      const mine = ownHome();
      parts.push(`<div class="banner"><div class="banner-body">
        <p><strong>You're in the shared Demo home.</strong> Its camera replays a recorded room around the clock, so you can try everything here.</p>
        ${mine ? `<div class="banner-actions"><button class="btn btn-small" type="button" data-action="go-home" data-fkey="b-home">Switch to My home</button></div>` : ""}
      </div></div>`);
    } else if (needsSetup() && !off.has(`setup.${state.household}`)) {
      const demo = demoHome();
      parts.push(`<div class="banner"><div class="banner-body">
        <p><strong>Set up your camera to start remembering things.</strong> It takes about a minute.</p>
        <div class="banner-actions">
          <button class="btn btn-small btn-primary" type="button" data-action="go-camera" data-fkey="b-setup">Set up camera</button>
          ${demo ? `<button class="btn btn-small" type="button" data-action="go-demo" data-fkey="b-demo">Try the Demo home</button>` : ""}
        </div></div>
        <button class="banner-x" type="button" data-action="dismiss" data-key="setup.${esc(state.household)}" aria-label="Dismiss">✕</button></div>`);
    }
    state.approvals.filter((a) => (!a.status || a.status === "pending") && !off.has(`apr.${a.id}`)).forEach((a) => {
      parts.push(`<div class="banner banner-warn"><div class="banner-body">
        <p><strong>iCamFind would like to tell your carer:</strong></p>
        <p>${esc(a.message)}</p>
        ${a.reason ? `<p class="muted small">Why: ${esc(a.reason)}</p>` : ""}
        <div class="banner-actions">
          <button class="btn btn-small btn-primary" type="button" data-action="approve" data-id="${esc(a.id)}" data-fkey="b-ap-${esc(a.id)}">Yes, tell them</button>
          <button class="btn btn-small" type="button" data-action="deny" data-id="${esc(a.id)}" data-fkey="b-dn-${esc(a.id)}">Don't send</button>
        </div></div>
        <button class="banner-x" type="button" data-action="dismiss" data-key="apr.${esc(a.id)}" aria-label="Hide for now">✕</button></div>`);
    });
    const now = Date.now() / 1000;
    state.alerts
      .filter((a) => a.ts && now - a.ts < 86400 && !off.has(`alr.${a.id}`))
      .sort((a, b) => b.ts - a.ts)
      .slice(0, 3)
      .forEach((a) => {
        parts.push(`<div class="banner banner-alert"><div class="banner-body">
          <p><strong>${esc(a.message || "Alert")}</strong></p>
          <p class="ev-time">${timeTag(a.ts)}</p></div>
          <button class="banner-x" type="button" data-action="dismiss" data-key="alr.${esc(a.id)}" aria-label="Dismiss">✕</button></div>`);
      });
    setHtml($("#banners"), parts.join(""));
  }

  function eventSentence(e) {
    const name = e.label || "Something";
    const z = zoneName(e.zone);
    switch (e.type) {
      case "placed": return z ? `${name} on the ${z}` : name;
      case "removed": return z ? `${name} from the ${z}` : name;
      case "moved": {
        const from = zoneName(e.from_zone);
        if (from && z) return `${name} from the ${from} to the ${z}`;
        return z ? `${name} to the ${z}` : name;
      }
      case "verified": return z ? `${name} is still on the ${z}` : `${name} is still there`;
      case "corrected": return `${name}: ${e.note || "memory updated"}`;
      default: return name;
    }
  }

  function renderRecent() {
    const list = $("#recent-strip");
    const items = state.events.slice(0, 5);
    if (!items.length) {
      const msg = state.loaded ? "Nothing yet. Things you put down will show up here." : "Loading…";
      setHtml(list, `<li class="recent-empty">${esc(msg)}</li>`);
      return;
    }
    setHtml(list, items.map((e) => {
      const t = EVENT_TYPES[e.type] || { text: "Seen" };
      const img = stableUrl(e.crop_url);
      return `<li><button class="recent-item" type="button" data-action="open" data-id="${esc(e.object_id || "")}" data-fkey="r-${esc(e.id)}">
        ${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<span class="thumb-empty"></span>`}
        <span class="recent-text"><strong>${esc(t.text)}</strong>${esc(eventSentence(e))}<br><span class="muted">${esc(relTime(e.ts))}</span></span>
      </button></li>`;
    }).join(""));
  }

  // ---------- Camera view ----------

  function camKey(hh) {
    return `icamfind.browserCam.${hh}`;
  }

  function camConfig(hh = state.household) {
    if (!hh) return null;
    const c = store.json(camKey(hh));
    return c && c.deviceId && c.token ? c : null;
  }

  function saveCamConfig(hh, cfg) {
    store.set(camKey(hh), cfg ? JSON.stringify(cfg) : null);
  }

  function renderCamera() {
    const demo = isDemo();
    const cfg = demo ? null : camConfig();
    $("#cam-demo").hidden = !demo;
    $("#cam-live").hidden = demo || !cfg;
    $("#cam-setup").hidden = demo || !!cfg;

    if (demo) {
      renderRoom($("#demo-room"));
      $("#demo-switch-btn").hidden = !ownHome();
    } else if (!cfg) {
      const err = $("#cam-setup-error");
      const btn = $("#cam-setup-btn");
      if (!window.ICamEdge) {
        err.textContent = "The camera feature failed to load. Please reload the page.";
        err.hidden = false;
        btn.disabled = true;
      } else if (state.households.length && !isOwner()) {
        err.textContent = "Only the person who owns this home can add a camera.";
        err.hidden = false;
        btn.disabled = true;
      } else if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        err.textContent = "This browser can't use a camera. Try the latest Chrome, Edge, Safari or Firefox.";
        err.hidden = false;
        btn.disabled = true;
      } else {
        err.hidden = true;
        btn.disabled = false;
      }
    }
    renderCamStatus();
    renderOtherCameras(cfg);
    $("#pair-btn").hidden = demo || !isOwner();
  }

  function renderCamStatus() {
    const el = $("#cam-status");
    const text = $("#cam-status-text");
    const s = cam.status || {};
    let st = s.state || "off";
    if (cam.paused) st = "paused";
    let msg;
    switch (st) {
      case "loading": msg = s.message || "Getting ready…"; break;
      case "learning": msg = "Learning the room. Keep still for a few seconds."; break;
      case "watching": {
        let n = 0;
        if (typeof s.objects === "number") n = s.objects;
        else if (Array.isArray(s.objects)) n = s.objects.filter((o) => o.status !== "removed").length;
        else if (cam.engine && Array.isArray(cam.engine.objects)) n = cam.engine.objects.filter((o) => o.status !== "removed").length;
        msg = `Watching the room · ${n} ${n === 1 ? "thing" : "things"} remembered`;
        break;
      }
      case "paused": msg = "Paused. Press Resume to start watching again."; break;
      case "error": msg = s.message || "The camera stopped. Press Resume to try again."; break;
      default: msg = cam.starting ? "Starting the camera…" : "Camera is off. Press Resume to start.";
    }
    el.setAttribute("data-state", st);
    if (text.textContent !== msg) text.textContent = msg;
    const running = !!cam.engine && !cam.paused;
    $("#cam-pause").textContent = running ? "Pause" : "Resume";
    const cover = $("#cam-paused-cover");
    cover.hidden = running || cam.starting;
    cover.querySelector("p").textContent = cam.paused ? "Camera paused" : "Camera is off";
    $("#cam-places").disabled = !cam.stream;
    $("#cam-reset").disabled = !running;
    renderTabDots();
  }

  function renderOtherCameras(cfg) {
    const list = $("#cameras-list");
    const mine = cfg ? cfg.deviceId : null;
    const others = state.devices
      .filter((d) => d.id !== mine)
      .sort((a, b) => (b.last_heartbeat || 0) - (a.last_heartbeat || 0));
    $("#others-title").textContent = cfg ? "Other cameras in this home" : "Cameras in this home";
    if (!others.length) {
      const msg = !state.loaded ? "Loading…" : (isDemo() ? "The demo camera hasn't checked in yet." : "No other cameras yet.");
      setHtml(list, `<li class="empty">${esc(msg)}</li>`);
      return;
    }
    setHtml(list, others.map((d) => {
      const online = !!d.online;
      const snap = state.device && state.device.id === d.id ? snapshotUrl(state.device.snapshot_url) : "";
      return `<li class="camera-item">
        ${snap ? `<img src="${esc(snap)}" alt="Latest picture from ${esc(d.name || "this camera")}">` : `<span class="thumb-empty" aria-hidden="true"></span>`}
        <div class="cam-meta">
          <p><strong>${esc(d.name || "Camera")}</strong></p>
          <p class="ev-time">${online ? "Watching now" : `Last seen ${esc(relTime(d.last_heartbeat))}`}</p>
        </div>
        <span class="pill ${online ? "pill-online" : "pill-offline"}">${online ? "Online" : "Offline"}</span>
      </li>`;
    }).join(""));
  }

  // Room picture with zones and markers (demo camera, and remote cameras on Things).
  function renderRoom(view) {
    const d = state.device;
    const fs = d && Array.isArray(d.frame_size) && d.frame_size.length === 2 ? d.frame_size : null;
    const snap = d ? snapshotUrl(d.snapshot_url) : "";
    if (!fs && !snap) {
      view.style.aspectRatio = "";
      setHtml(view, `<p class="placeholder">No picture from the camera yet.</p>`);
      return;
    }
    const [w, h] = fs || [640, 480];
    view.style.aspectRatio = `${w} / ${h}`;
    const font = Math.max(14, Math.round(w / 38));
    const doorZone = d && d.door_zone;
    const zones = (d && Array.isArray(d.zones) ? d.zones : []).map((z) => {
      const pts = Array.isArray(z.polygon) ? z.polygon : [];
      if (pts.length < 3) return "";
      const points = pts.map((p) => `${Number(p[0])},${Number(p[1])}`).join(" ");
      const lx = Math.min(...pts.map((p) => Number(p[0]))) + font * 0.4;
      const ly = Math.min(...pts.map((p) => Number(p[1]))) + font * 1.15;
      const cls = z.name === doorZone ? "zone-shape zone-door" : "zone-shape";
      return `<polygon class="${cls}" points="${esc(points)}"><title>${esc(z.name)}</title></polygon>` +
        `<text class="zone-label" x="${lx}" y="${ly}" font-size="${font}" text-anchor="start">${esc(z.name)}</text>`;
    }).join("");
    const markers = state.objects
      .filter((o) => o.status !== "removed" && Array.isArray(o.bbox) && o.bbox.length === 4)
      .map((o) => {
        const [x, y, bw, bh] = o.bbox.map(Number);
        const cx = x + bw / 2;
        const cy = y + bh / 2;
        const r = Math.max(8, Math.round(w / 70));
        const name = labelOf(o);
        const aria = `${name}, ${whereText(o).toLowerCase()}. Open details.`;
        return `<g class="marker${o.essential ? " essential" : ""}" role="button" tabindex="0" data-id="${esc(o.id)}" data-fkey="m-${esc(o.id)}" aria-label="${esc(aria)}">` +
          `<circle cx="${cx}" cy="${cy}" r="${r}"></circle>` +
          `<text x="${cx}" y="${cy - r - 6}" font-size="${Math.round(font * 0.85)}" text-anchor="middle">${esc(name)}</text></g>`;
      }).join("");
    const img = snap ? `<img src="${esc(snap)}" alt="Latest camera picture of the room. People are blurred.">` : "";
    setHtml(view, `${img}<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="group" aria-label="Places and remembered things">${zones}${markers}</svg>`);
  }

  function renderThingsRoom() {
    const wrap = $("#things-room-wrap");
    const cfg = camConfig();
    const d = state.device;
    const remote = d && (!cfg || d.id !== cfg.deviceId) && (d.snapshot_url || d.frame_size);
    wrap.hidden = !remote;
    if (remote) renderRoom($("#things-room"));
  }

  async function requestSnapshot() {
    try {
      await api("api/snapshot", { method: "POST" });
      toast("Asking the camera for a fresh picture…");
      const again = () => { state.snapForce = true; refreshState(); };
      setTimeout(again, 4000);
      setTimeout(again, 9000);
    } catch { /* toast shown */ }
  }

  // ---------- Browser camera ----------

  async function openStream(cameraId) {
    const video = { width: { ideal: 1280 }, height: { ideal: 720 } };
    if (cameraId) video.deviceId = { exact: cameraId };
    try {
      return await navigator.mediaDevices.getUserMedia({ video, audio: false });
    } catch (err) {
      if (cameraId && err && (err.name === "OverconstrainedError" || err.name === "NotFoundError")) {
        delete video.deviceId; // that camera is gone; use the default one
        return navigator.mediaDevices.getUserMedia({ video, audio: false });
      }
      throw err;
    }
  }

  function stopTracks(stream) {
    if (stream) stream.getTracks().forEach((t) => t.stop());
  }

  function cameraErrorText(err) {
    const name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
      return "Camera access is blocked. Click the camera icon in the address bar, choose Allow, then try again.";
    }
    if (name === "NotFoundError" || name === "OverconstrainedError") {
      return "No camera was found. Plug in a webcam or use a laptop with a built-in camera.";
    }
    if (name === "NotReadableError" || name === "AbortError") {
      return "The camera is busy. Close other apps using it (like video calls) and try again.";
    }
    return "The camera couldn't start. Please try again.";
  }

  function waitForVideo(video) {
    return new Promise((resolve) => {
      if (video.readyState >= 1 && video.videoWidth) { resolve(); return; }
      const done = () => { video.removeEventListener("loadedmetadata", done); resolve(); };
      video.addEventListener("loadedmetadata", done);
      setTimeout(done, 4000);
    });
  }

  function setFrameAspect(frame, video) {
    const w = video.videoWidth || 1280;
    const h = video.videoHeight || 720;
    frame.style.setProperty("--ar", String(w / h));
    return { w, h };
  }

  async function attachStream(video, stream, frame) {
    video.srcObject = stream;
    try { await video.play(); } catch { /* autoplay with muted should work; ignore */ }
    await waitForVideo(video);
    if (frame) setFrameAspect(frame, video);
  }

  function setCamStatus(s) {
    cam.status = s;
    renderCamStatus();
  }

  // Start (or resume) this browser as the current home's camera. Reuses `stream` when given.
  async function startBrowserCamera(stream) {
    const hh = state.household;
    const cfg = camConfig(hh);
    if (!cfg || isDemo() || cam.starting) { if (stream && stream !== cam.stream) stopTracks(stream); return; }
    if (!window.ICamEdge) {
      setCamStatus({ state: "error", message: "The camera feature failed to load. Please reload the page." });
      return;
    }
    cam.starting = true;
    cam.paused = false;
    setCamStatus({ state: "loading", message: "Starting the camera…" });
    try {
      if (!stream) stream = await openStream(cfg.cameraId);
      if (state.household !== hh) { stopTracks(stream); return; }
      cam.stream = stream;
      const video = $("#cam-video");
      await attachStream(video, stream, $("#cam-frame"));
      setCamStatus({ state: "loading", message: "Getting ready… (the first time takes a moment)" });
      await window.ICamEdge.load();
      if (state.household !== hh || cam.stream !== stream) return;
      const overlay = $("#cam-overlay");
      cam.engine = window.ICamEdge.create({
        video,
        overlay,
        apiBase: "",
        deviceToken: cfg.token,
        zones: cfg.zones || [],
        doorZone: cfg.doorZone || null,
        essentials: ESSENTIALS,
        onEvent: (ev) => { showEventToast(ev); refreshSoon(); },
        // Claude's name for a new thing arrives a moment after the event: update the toast in place.
        onLabel: (l) => {
          if (eventToastEv && eventToastEv.object_id === l.object_id && !$("#event-toast").hidden) {
            showEventToast({ ...eventToastEv, label: l.label });
          }
        },
        onStatus: (s) => { cam.status = s || {}; if (state.household === hh) renderCamStatus(); else renderTabDots(); },
        onAlert: (a) => { if (a && a.message) toast(a.message); refreshSoon(); },
      });
      cam.household = hh;
      cam.engine.start();
    } catch (err) {
      stopTracks(stream);
      if (cam.stream === stream) cam.stream = null;
      cam.engine = null;
      const msg = err && err.name ? cameraErrorText(err) : "The camera feature couldn't start. Check the internet connection and try again.";
      setCamStatus({ state: "error", message: msg });
    } finally {
      cam.starting = false;
      renderCamStatus();
    }
  }

  function stopBrowserCamera() {
    if (cam.engine) {
      try { cam.engine.stop(); } catch { /* already stopped */ }
    }
    cam.engine = null;
    stopTracks(cam.stream);
    cam.stream = null;
    const v = $("#cam-video");
    if (v) v.srcObject = null;
    cam.household = null;
    cam.status = { state: "off" };
    renderCamStatus();
  }

  function resumeBrowserCameraIfSetUp() {
    if (!isDemo() && camConfig() && !cam.paused && !cam.engine) startBrowserCamera();
  }

  function pauseOrResume() {
    if (cam.engine && !cam.paused) {
      stopBrowserCamera();
      cam.paused = true;
      renderCamStatus();
      toast("Paused. The camera is off.");
    } else {
      cam.paused = false;
      startBrowserCamera();
    }
  }

  function forgetThisCamera() {
    if (!window.confirm("Stop using this computer as a camera for this home? Things already remembered stay in memory.")) return;
    stopBrowserCamera();
    cam.paused = false;
    saveCamConfig(state.household, null);
    renderAll();
    toast("This computer is no longer watching the room.");
  }

  let eventToastTimer = null;
  let eventToastEv = null;
  function showEventToast(ev) {
    if (!ev) return;
    eventToastEv = ev;
    const label = ev.label && ev.label !== "object" ? ev.label : "something";
    const z = zoneName(ev.zone);
    let text;
    if (ev.type === "removed") text = `<strong>Picked up:</strong> ${esc(label)}${z ? ` from the ${esc(z)}` : ""}`;
    else if (ev.type === "moved") text = `<strong>Moved:</strong> ${esc(label)}${z ? ` to the ${esc(z)}` : ""}`;
    else text = `<strong>Remembered:</strong> ${esc(label)}${z ? ` on the ${esc(z)}` : ""}`;
    if (cam.household && cam.household !== state.household) text = `${esc(householdName(cam.household))} · ${text}`;
    const img = safeDataImage(ev.cropDataUrl);
    const el = $("#event-toast");
    el.innerHTML = `${img ? `<img src="${esc(img)}" alt="">` : ""}<span>${text}</span>`;
    el.hidden = false;
    clearTimeout(eventToastTimer);
    eventToastTimer = setTimeout(() => { el.hidden = true; }, 5000);
  }

  // ---------- Setup wizard ----------

  const wiz = {
    mode: "setup", // "setup" (new camera) or "edit" (change places of the running camera)
    step: 1,
    stream: null,
    ownsStream: false,
    cameras: [],
    cameraId: null,
    zones: [],
    doorZone: null,
    pending: null,
    drag: null,
    procH: 360,
    ready: null, // promise from ICamEdge.load()
    loaded: false,
  };

  function openWizard(mode) {
    const dialog = $("#wizard");
    wiz.mode = mode;
    wiz.pending = null;
    wiz.drag = null;
    $("#wiz-cam-error").hidden = true;
    $("#wiz-finish-error").hidden = true;
    if (mode === "edit") {
      const cfg = camConfig() || {};
      wiz.zones = (cfg.zones || []).map((z) => ({ name: z.name, polygon: z.polygon.map((p) => p.slice()) }));
      wiz.doorZone = cfg.doorZone || null;
      wiz.stream = cam.stream;
      wiz.ownsStream = false;
      openDialog(dialog);
      setWizStep(2);
    } else {
      wiz.zones = [];
      wiz.doorZone = null;
      wiz.stream = null;
      wiz.ownsStream = false;
      wiz.cameraId = null;
      $("#wiz-video-placeholder").hidden = false;
      $("#wiz-cam-pick").hidden = true;
      $("#wiz-ready").textContent = "";
      $("#wiz-video").srcObject = null;
      openDialog(dialog);
      setWizStep(1);
    }
  }

  function closeWizard() {
    closeDialog($("#wizard"));
  }

  function onWizardClosed() {
    if (wiz.ownsStream) stopTracks(wiz.stream);
    wiz.stream = null;
    wiz.ownsStream = false;
    $("#wiz-video").srcObject = null;
    $("#draw-video").srcObject = null;
    returnFocus();
  }

  function setWizStep(n) {
    wiz.step = n;
    [1, 2, 3].forEach((i) => { $(`#wiz-step-${i}`).hidden = i !== n; });
    $$(".wiz-steps li").forEach((li) => {
      const s = Number(li.getAttribute("data-step"));
      if (s === n) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      li.classList.toggle("done", s < n);
      li.hidden = wiz.mode === "edit" && s !== 2;
    });
    const next = $("#wiz-next");
    const back = $("#wiz-back");
    back.hidden = wiz.mode === "edit" || n === 1;
    if (n === 1) {
      next.textContent = wiz.stream ? "Next" : "Turn on camera";
      next.disabled = false;
    } else if (n === 2) {
      next.textContent = wiz.mode === "edit" ? "Save places" : "Next";
      enterPlacesStep();
    } else {
      next.textContent = "Start watching";
      next.disabled = false;
    }
    const title = $(`#wiz-step-${n} .wiz-title`);
    if (title) title.focus();
  }

  function kickOffEngineLoad() {
    if (wiz.ready || !window.ICamEdge) return;
    const note = $("#wiz-ready");
    note.innerHTML = `<span class="spinner" aria-hidden="true"></span> Getting ready… (the first time takes a moment)`;
    wiz.ready = window.ICamEdge.load()
      .then(() => { wiz.loaded = true; note.textContent = "Ready."; })
      .catch(() => {
        wiz.ready = null;
        note.textContent = "Couldn't finish getting ready. Check the internet connection. We'll try again when you finish.";
      });
  }

  async function turnOnCamera(cameraId) {
    const err = $("#wiz-cam-error");
    err.hidden = true;
    const next = $("#wiz-next");
    next.disabled = true;
    next.textContent = "Starting camera…";
    try {
      const stream = await openStream(cameraId || null);
      if (wiz.ownsStream) stopTracks(wiz.stream);
      wiz.stream = stream;
      wiz.ownsStream = true;
      const track = stream.getVideoTracks()[0];
      const settings = track && track.getSettings ? track.getSettings() : {};
      wiz.cameraId = settings.deviceId || cameraId || null;
      await attachStream($("#wiz-video"), stream, $("#wiz-video").parentElement);
      $("#wiz-video-placeholder").hidden = true;
      kickOffEngineLoad();
      await listCameras();
      next.textContent = "Next";
    } catch (e) {
      err.textContent = cameraErrorText(e);
      err.hidden = false;
      next.textContent = "Try again";
    } finally {
      next.disabled = false;
    }
  }

  async function listCameras() {
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      wiz.cameras = all.filter((d) => d.kind === "videoinput");
    } catch { wiz.cameras = []; }
    const pick = $("#wiz-cam-pick");
    if (wiz.cameras.length < 2) { pick.hidden = true; return; }
    const sel = $("#wiz-camera-select");
    sel.innerHTML = wiz.cameras
      .map((c, i) => `<option value="${esc(c.deviceId)}">${esc(c.label || `Camera ${i + 1}`)}</option>`)
      .join("");
    if (wiz.cameraId) sel.value = wiz.cameraId;
    pick.hidden = false;
  }

  // --- Places (zones) editor ---

  function enterPlacesStep() {
    const video = $("#draw-video");
    const frame = $("#draw-frame");
    if (wiz.stream && video.srcObject !== wiz.stream) {
      attachStream(video, wiz.stream, frame).then(sizeDrawCanvas);
    } else {
      sizeDrawCanvas();
    }
    hideNamePanel();
    renderPlaces();
  }

  function sizeDrawCanvas() {
    const video = $("#draw-video");
    const { w, h } = setFrameAspect($("#draw-frame"), video);
    wiz.procH = Math.round(PROC_W * h / w);
    const c = $("#draw-canvas");
    c.width = PROC_W;
    c.height = wiz.procH;
    drawPlaces();
  }

  function canvasPoint(e) {
    const c = $("#draw-canvas");
    const r = c.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * c.width;
    const y = ((e.clientY - r.top) / r.height) * c.height;
    return [Math.max(0, Math.min(c.width, x)), Math.max(0, Math.min(c.height, y))];
  }

  function rectPolygon(x0, y0, x1, y1) {
    const [a, b] = [Math.round(Math.min(x0, x1)), Math.round(Math.min(y0, y1))];
    const [c, d] = [Math.round(Math.max(x0, x1)), Math.round(Math.max(y0, y1))];
    return [[a, b], [c, b], [c, d], [a, d]];
  }

  function drawPlaces() {
    const c = $("#draw-canvas");
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, c.width, c.height);
    const font = Math.max(14, Math.round(c.width / 36));
    ctx.font = `700 ${font}px system-ui, sans-serif`;
    const drawPoly = (poly, color, fill, dashed, label) => {
      ctx.beginPath();
      poly.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.setLineDash(dashed ? [8, 6] : []);
      ctx.lineWidth = 3;
      ctx.strokeStyle = color;
      ctx.stroke();
      ctx.setLineDash([]);
      if (label) {
        const x = poly[0][0] + 6;
        const y = poly[0][1] + font + 4;
        ctx.lineWidth = 4;
        ctx.strokeStyle = "rgba(0,0,0,.7)";
        ctx.strokeText(label, x, y);
        ctx.fillStyle = "#fff";
        ctx.fillText(label, x, y);
      }
    };
    wiz.zones.forEach((z) => {
      const door = z.name === wiz.doorZone;
      drawPoly(z.polygon, door ? "#f0a03c" : "#4fc2b9", door ? "rgba(240,160,60,.18)" : "rgba(79,194,185,.18)", door, z.name);
    });
    const p = wiz.drag || wiz.pending;
    if (p) drawPoly(rectPolygon(p.x0, p.y0, p.x1, p.y1), "#ffffff", "rgba(255,255,255,.15)", true, "");
  }

  function renderPlaces() {
    const list = $("#places-list");
    if (!wiz.zones.length) {
      list.innerHTML = `<li class="empty">No places yet. Drag a box over the picture.</li>`;
    } else {
      list.innerHTML = wiz.zones.map((z, i) => {
        const door = z.name === wiz.doorZone;
        return `<li class="${door ? "is-door" : ""}"><span class="swatch" aria-hidden="true"></span>
          <span class="place-name">${esc(z.name)}${door ? " · exit door" : ""}</span>
          <button class="btn btn-ghost btn-icon btn-small" type="button" data-remove="${i}" aria-label="Remove ${esc(z.name)}">✕</button></li>`;
      }).join("");
    }
    if (wiz.step === 2) $("#wiz-next").disabled = !wiz.zones.some((z) => z.name !== wiz.doorZone);
    drawPlaces();
  }

  function showNamePanel() {
    $("#name-panel").hidden = false;
    $("#place-name").value = "";
    const first = $("#name-panel .chip");
    if (first) first.focus();
  }

  function hideNamePanel() {
    $("#name-panel").hidden = true;
    wiz.pending = null;
  }

  function uniquePlaceName(base) {
    const names = new Set(wiz.zones.map((z) => z.name));
    if (!names.has(base)) return base;
    let i = 2;
    while (names.has(`${base} ${i}`)) i += 1;
    return `${base} ${i}`;
  }

  function addPlace(rawName) {
    const p = wiz.pending;
    if (!p) return;
    const clean = String(rawName || "").trim().toLowerCase().replace(/\s+/g, " ").slice(0, 30);
    if (!clean) { $("#place-name").focus(); return; }
    const isDoor = clean === "door";
    const name = uniquePlaceName(clean);
    wiz.zones.push({ name, polygon: rectPolygon(p.x0, p.y0, p.x1, p.y1) });
    if (isDoor) wiz.doorZone = name;
    hideNamePanel();
    renderPlaces();
    $("#draw-canvas").focus();
  }

  function addWholePicture() {
    wiz.pending = { x0: 0, y0: 0, x1: PROC_W, y1: wiz.procH };
    addPlace("room");
  }

  function bindDrawing() {
    const c = $("#draw-canvas");
    c.setAttribute("tabindex", "0");
    c.addEventListener("pointerdown", (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      e.preventDefault();
      const [x, y] = canvasPoint(e);
      wiz.drag = { x0: x, y0: y, x1: x, y1: y };
      wiz.pending = null;
      $("#name-panel").hidden = true;
      try { c.setPointerCapture(e.pointerId); } catch { /* not supported */ }
      drawPlaces();
    });
    c.addEventListener("pointermove", (e) => {
      if (!wiz.drag) return;
      const [x, y] = canvasPoint(e);
      wiz.drag.x1 = x;
      wiz.drag.y1 = y;
      drawPlaces();
    });
    const end = () => {
      if (!wiz.drag) return;
      const d = wiz.drag;
      wiz.drag = null;
      if (Math.abs(d.x1 - d.x0) >= 16 && Math.abs(d.y1 - d.y0) >= 16) {
        wiz.pending = d;
        showNamePanel();
      }
      drawPlaces();
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", () => { wiz.drag = null; drawPlaces(); });

    $("#name-panel").addEventListener("click", (e) => {
      const chip = e.target.closest("[data-place]");
      if (chip) addPlace(chip.getAttribute("data-place"));
    });
    $("#place-custom").addEventListener("submit", (e) => {
      e.preventDefault();
      addPlace($("#place-name").value);
    });
    $("#place-cancel").addEventListener("click", () => { hideNamePanel(); drawPlaces(); });
    $("#places-list").addEventListener("click", (e) => {
      const b = e.target.closest("[data-remove]");
      if (!b) return;
      const i = Number(b.getAttribute("data-remove"));
      const [gone] = wiz.zones.splice(i, 1);
      if (gone && gone.name === wiz.doorZone) wiz.doorZone = null;
      renderPlaces();
    });
    $("#place-whole").addEventListener("click", addWholePicture);
    window.addEventListener("resize", () => { if ($("#wizard").open && wiz.step === 2) drawPlaces(); });
  }

  async function wizNext() {
    if (wiz.step === 1) {
      if (!wiz.stream) { await turnOnCamera(null); return; }
      setWizStep(2);
    } else if (wiz.step === 2) {
      if (!wiz.zones.some((z) => z.name !== wiz.doorZone)) return;
      if (wiz.mode === "edit") { await savePlacesForRunningCamera(); return; }
      setWizStep(3);
    } else {
      await finishSetup();
    }
  }

  async function savePlacesForRunningCamera() {
    const cfg = camConfig();
    if (!cfg) { closeWizard(); return; }
    cfg.zones = wiz.zones;
    cfg.doorZone = wiz.doorZone;
    saveCamConfig(state.household, cfg);
    if (cam.engine && typeof cam.engine.setZones === "function") cam.engine.setZones(cfg.zones, cfg.doorZone);
    try {
      await api(`api/devices/${encodeURIComponent(cfg.deviceId)}/zones`, {
        method: "PUT", body: { zones: cfg.zones, door_zone: cfg.doorZone }, quiet: true,
      });
    } catch { /* places still apply locally; the camera re-sends them with its next check-in */ }
    closeWizard();
    toast("Places saved.");
  }

  async function finishSetup() {
    const next = $("#wiz-next");
    const err = $("#wiz-finish-error");
    err.hidden = true;
    next.disabled = true;
    next.innerHTML = `<span class="spinner" aria-hidden="true"></span> Starting…`;
    const hh = state.household;
    try {
      if (!wiz.loaded) {
        if (!wiz.ready) kickOffEngineLoad();
        if (wiz.ready) await wiz.ready;
        if (!wiz.loaded) await window.ICamEdge.load();
      }
      const d = await api("api/devices", { method: "POST", body: { name: thisComputerName(), kind: "browser" } });
      const id = d.device && d.device.id;
      const secret = d.token;
      if (!id || !secret) throw new Error("setup");
      const cfg = { deviceId: id, token: secret, zones: wiz.zones, doorZone: wiz.doorZone, cameraId: wiz.cameraId };
      saveCamConfig(hh, cfg);
      try {
        await api(`api/devices/${encodeURIComponent(id)}/zones`, {
          method: "PUT", body: { zones: cfg.zones, door_zone: cfg.doorZone }, quiet: true,
        });
      } catch { /* the camera sends its places with each check-in anyway */ }
      const stream = wiz.stream;
      wiz.ownsStream = false; // hand the running camera over to the live view
      closeWizard();
      go("camera");
      renderAll();
      await startBrowserCamera(stream);
      toast("Your camera is on. Put something down to try it.");
    } catch (e) {
      err.textContent = (e && e.message && e.message !== "setup" && e.message !== "unauthorized")
        ? `Couldn't finish setting up: ${e.message}`
        : "Couldn't finish setting up. Please check the internet connection and try again.";
      err.hidden = false;
    } finally {
      next.disabled = false;
      next.textContent = "Start watching";
    }
  }

  function thisComputerName() {
    const ua = navigator.userAgent || "";
    const os = /iPhone|iPad/.test(ua) ? "iPhone or iPad" : /Android/.test(ua) ? "Android"
      : /Mac OS X/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : /CrOS/.test(ua) ? "Chromebook"
        : /Linux/.test(ua) ? "Linux" : "computer";
    const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
      : /Safari\//.test(ua) ? "Safari" : "browser";
    return `${br} on ${os === "computer" ? "this computer" : os}`;
  }

  // ---------- Connect another computer ----------

  async function openPairDialog() {
    const dialog = $("#pair-dialog");
    $("#pair-loading").hidden = false;
    $("#pair-ready").hidden = true;
    $("#pair-error").hidden = true;
    openDialog(dialog);
    $("#pair-close").focus();
    try {
      const d = await api("api/devices/pair-code", { method: "POST", body: { name: "Another camera" }, quiet: true });
      const code = String(d.code || "");
      if (!code) throw new Error("no code");
      $("#pair-code").textContent = code;
      $("#pair-command").textContent = d.command || `python -m icamfind pair ${code}`;
      $("#pair-loading").hidden = true;
      $("#pair-ready").hidden = false;
    } catch (e) {
      $("#pair-loading").hidden = true;
      const err = $("#pair-error");
      err.textContent = "Couldn't make a code right now. Please try again in a moment.";
      err.hidden = false;
    }
  }

  // ---------- Things ----------

  function renderObjects(force) {
    if (state.editingId && !force) return; // don't clobber an inline rename in progress
    const grid = $("#objects-grid");
    let list = state.objects.slice().sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
    if (state.filter === "present") list = list.filter((o) => o.status !== "removed");
    if (state.filter === "removed") list = list.filter((o) => o.status === "removed");
    if (!list.length) {
      let msg = "Loading…";
      if (state.loaded) {
        msg = state.objects.length
          ? "Nothing here with this filter."
          : "Nothing remembered yet. Put something down where the camera can see it.";
      }
      setHtml(grid, `<p class="placeholder">${esc(msg)}</p>`);
      return;
    }
    setHtml(grid, list.map(objectCard).join(""));
    if (force && state.editingId) {
      const input = $(`#rn-${cssId(state.editingId)}`);
      if (input) { input.focus(); input.select(); }
    }
  }

  function cssId(id) {
    return String(id).replace(/[^a-zA-Z0-9_-]/g, "_");
  }

  function objectCard(o) {
    const id = esc(o.id);
    const name = labelOf(o);
    const img = stableUrl(o.crop_url);
    const removed = o.status === "removed";
    const photo = img
      ? `<img class="obj-photo" src="${esc(img)}" alt="Photo of ${esc(name)}" loading="lazy">`
      : `<div class="obj-photo-empty" aria-hidden="true">No photo</div>`;
    const editing = state.editingId === o.id;
    const rename = editing ? `
      <form class="rename-form" data-id="${id}">
        <label class="visually-hidden" for="rn-${cssId(o.id)}">New name for ${esc(name)}</label>
        <input id="rn-${cssId(o.id)}" name="label" type="text" value="${esc(name)}" maxlength="60" required>
        <button class="btn btn-small btn-primary" type="submit">Save</button>
        <button class="btn btn-small" type="button" data-action="cancel-rename">Cancel</button>
      </form>` : "";
    return `
      <article class="obj-card${removed ? " is-removed" : ""}" aria-label="${esc(name)}">
        <button class="obj-open" type="button" data-action="open" data-id="${id}" data-fkey="open-${id}">
          ${photo}
          <div class="obj-info">
            <p class="obj-label">${o.essential ? '<span aria-hidden="true">★ </span>' : ""}${esc(name)}</p>
            <p class="obj-where">${esc(whereWhen(o))}</p>
          </div>
        </button>
        <div class="obj-actions">
          <button class="btn btn-small star-btn" type="button" data-action="star" data-id="${id}" data-fkey="star-${id}"
            aria-pressed="${o.essential ? "true" : "false"}" title="Important things get a reminder if you leave without them">
            <span aria-hidden="true">${o.essential ? "★" : "☆"}</span> Important
          </button>
          <button class="btn btn-small" type="button" data-action="rename" data-id="${id}" data-fkey="rename-${id}">Rename</button>
        </div>
        ${rename}
      </article>`;
  }

  async function updateObject(id, patch) {
    try {
      const d = await api(`api/objects/${encodeURIComponent(id)}`, { method: "POST", body: patch });
      if (d.object) {
        const i = state.objects.findIndex((o) => o.id === id);
        if (i >= 0) state.objects[i] = Object.assign({}, state.objects[i], d.object);
      }
      return true;
    } catch {
      return false;
    }
  }

  async function openObject(id) {
    if (!id) return;
    cancelHere(state.drawerHere);
    state.drawerHere = null;
    const dialog = $("#object-dialog");
    const known = state.objects.find((o) => o.id === id);
    $("#dialog-title").textContent = known ? labelOf(known) : "Thing";
    $("#dialog-body").innerHTML = `<p class="muted">Loading history…</p>`;
    openDialog(dialog);
    $("#dialog-close").focus();
    try {
      const d = await api(`api/objects/${encodeURIComponent(id)}`);
      renderObjectDetail(d.object || known || {}, Array.isArray(d.events) ? d.events : []);
    } catch {
      $("#dialog-body").innerHTML = `<p class="error-text">Couldn't load the history of this thing.</p>`;
    }
  }

  function renderObjectDetail(o, events) {
    const name = labelOf(o);
    state.detailObject = o;
    $("#dialog-title").textContent = name;
    const img = stableUrl(o.crop_url);
    const rows = [
      ["Where", whereText(o)],
      ["Last seen", o.last_seen ? `${relTime(o.last_seen)} (${absTime(o.last_seen)})` : "never"],
      ["First seen", o.first_seen ? absTime(o.first_seen) : "—"],
      ["Looks like", o.description || "—"],
      ["Important", o.essential ? "Yes, you get a reminder at the door" : "No"],
    ];
    const history = events.length
      ? `<ol class="timeline">${events.map(eventRow).join("")}</ol>`
      : `<p class="muted">No history yet.</p>`;
    $("#dialog-body").innerHTML = `
      <div class="detail-top">
        ${img ? `<img src="${esc(img)}" alt="Photo of ${esc(name)}">` : `<div class="obj-photo-empty" aria-hidden="true">No photo</div>`}
        <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      </div>
      ${o.id && o.status !== "removed" ? `<p class="detail-actions"><button class="btn btn-small" type="button" data-action="where" data-id="${esc(o.id)}">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/></svg>
        Show me where</button></p>
      <div class="detail-where" hidden></div>` : ""}
      <h3 class="small-title">History</h3>
      ${history}`;
  }

  function eventRow(e) {
    const t = EVENT_TYPES[e.type] || { text: e.type || "Seen", icon: "•" };
    const img = stableUrl(e.crop_url);
    const thumb = img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<span class="thumb-empty" aria-hidden="true"></span>`;
    return `<li>${thumb}<div>
      <p class="ev-line"><span class="ev-type ev-${esc(e.type)}"><span aria-hidden="true">${t.icon}</span>${esc(t.text)}</span>${esc(eventSentence(e))}</p>
      <p class="ev-time">${timeTag(e.ts)} · ${esc(clock(e.ts))}</p>
    </div></li>`;
  }

  // ---------- Routines, approvals, alerts ----------

  function renderApprovals() {
    const list = $("#approvals-list");
    const pending = state.approvals.filter((a) => !a.status || a.status === "pending");
    if (!pending.length) {
      setHtml(list, `<li class="empty">Nothing waiting for you.</li>`);
      return;
    }
    setHtml(list, pending.map((a) => `
      <li>
        <p><strong>Tell the carer?</strong></p>
        <p>${esc(a.message)}</p>
        ${a.reason ? `<p class="muted small">Why: ${esc(a.reason)}</p>` : ""}
        <p class="ev-time">Asked ${timeTag(a.created)}</p>
        <div class="approval-actions">
          <button class="btn btn-small btn-primary" type="button" data-action="approve" data-id="${esc(a.id)}" data-fkey="ap-${esc(a.id)}">Yes, tell them</button>
          <button class="btn btn-small" type="button" data-action="deny" data-id="${esc(a.id)}" data-fkey="dn-${esc(a.id)}">Don't send</button>
        </div>
      </li>`).join(""));
  }

  function renderRoutines() {
    const list = $("#routines-list");
    if (!state.routines.length) {
      setHtml(list, `<li class="empty">No routines yet. For example: morning pills, picked up between 8 and 9.</li>`);
      return;
    }
    setHtml(list, state.routines.map((r) => {
      const [text, cls] = ROUTINE_STATUS[r.last_status] || ["Not checked yet", "status-muted"];
      const obj = state.objects.find((o) => o.id === r.object_id);
      return `
      <li>
        <div class="routine-head">
          <strong>${esc(r.name || "Routine")}</strong>
          <span class="status-chip ${cls}">${esc(text)}</span>
        </div>
        <p class="muted small">Watching ${esc(obj ? labelOf(obj) : "a thing")} · ${esc(r.start)}–${esc(r.end)}</p>
        <p class="ev-time">Last checked ${timeTag(r.last_checked)}</p>
        <div class="routine-actions">
          <button class="btn btn-small" type="button" data-action="check" data-id="${esc(r.id)}" data-fkey="ck-${esc(r.id)}">Check now</button>
          <button class="btn btn-small btn-danger" type="button" data-action="delete" data-id="${esc(r.id)}" data-fkey="del-${esc(r.id)}">Delete</button>
        </div>
      </li>`;
    }).join(""));
  }

  function renderRoutineOptions() {
    const sel = $("#routine-object");
    const objs = state.objects.slice().sort((a, b) => labelOf(a).localeCompare(labelOf(b)));
    const html = objs.length
      ? objs.map((o) => `<option value="${esc(o.id)}">${esc(labelOf(o))} — ${esc(whereText(o).toLowerCase())}</option>`).join("")
      : `<option value="" disabled selected>Nothing remembered yet</option>`;
    if (sel._html === html) return;
    const prev = sel.value;
    sel.innerHTML = html;
    sel._html = html;
    if (prev && objs.some((o) => o.id === prev)) sel.value = prev;
  }

  function renderAlerts() {
    const list = $("#alerts-list");
    if (!state.alerts.length) {
      setHtml(list, `<li class="empty">No alerts. All calm.</li>`);
      return;
    }
    const sorted = state.alerts.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
    setHtml(list, sorted.map((a) => {
      const kind = a.kind === "door_essentials" ? "Leaving without important things"
        : a.kind === "caregiver_notified" ? "Carer told" : "Alert";
      return `<li><p class="alert-msg">${esc(a.message || kind)}</p><p class="ev-time">${esc(kind)} · ${timeTag(a.ts)}</p></li>`;
    }).join(""));
  }

  async function decideApproval(button) {
    const decision = button.getAttribute("data-action") === "approve" ? "approve" : "deny";
    button.disabled = true;
    try {
      await api(`api/approvals/${encodeURIComponent(button.getAttribute("data-id"))}`, { method: "POST", body: { decision } });
      toast(decision === "approve" ? "Done. The carer has been told." : "Okay, nothing was sent.");
      refreshState();
    } catch {
      button.disabled = false;
    }
  }

  // ---------- Ask + agent job ----------

  function setAskBusy(busy) {
    const btn = $("#ask-btn");
    btn.disabled = busy;
    btn.textContent = busy ? "Looking…" : "Ask";
    $("#ask-input").setAttribute("aria-busy", busy ? "true" : "false");
  }

  async function ask(question) {
    const q = (question || "").trim();
    if (!q) return;
    $("#ask-input").value = q;
    setAskBusy(true);
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      const d = await api("api/ask", { method: "POST", body: { question: q, tz } });
      startJob(d.job_id, q, "ask");
    } catch {
      setAskBusy(false);
    }
  }

  function jobEls() {
    return state.jobTarget === "dialog"
      ? { status: $("#jd-status"), card: $("#jd-answer"), list: $("#jd-trace"), tools: null }
      : { status: $("#job-status"), card: $("#answer-card"), list: $("#trace-list"), tools: $("#answer-tools") };
  }

  function startJob(id, label, target) {
    if (!id) { setAskBusy(false); toast("iCamFind couldn't start looking. Please try again."); return; }
    stopJob();
    cancelHere(state.here);
    state.here = null;
    state.jobTarget = target;
    state.job = { id, status: "running", question: label };
    state.steps = [];
    state.openSteps = new Set();
    state.jobStarted = Date.now();
    const els = jobEls();
    els.card.hidden = true;
    setHtml(els.list, "");
    if (target === "ask") {
      $("#job-area").hidden = false;
      $("#trace").open = false;
      setAskBusy(true);
      scrollIntoViewIfNeeded($("#job-area"));
    } else {
      $("#job-dialog-title").textContent = label;
      openDialog($("#job-dialog"));
      $("#job-dialog-close").focus();
    }
    renderJob();
    pollJob();
  }

  function scrollIntoViewIfNeeded(el) {
    if (!el) return;
    const r = el.getBoundingClientRect();
    if (r.top >= 0 && r.top < window.innerHeight * 0.25) return;
    el.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
  }

  function stopJob() {
    clearTimeout(state.jobTimer);
    state.jobTimer = null;
  }

  async function pollJob() {
    const id = state.job && state.job.id;
    if (!id) return;
    try {
      const d = await api(`api/jobs/${encodeURIComponent(id)}`, { quiet: true });
      if (!state.job || state.job.id !== id) return; // a newer job replaced this one
      state.job = Object.assign({}, state.job, d.job || {});
      state.steps = Array.isArray(d.steps) ? d.steps : [];
      renderJob();
      if (state.job.status === "done" || state.job.status === "error") {
        finishJob();
        return;
      }
    } catch (err) {
      if (err && err.message === "unauthorized") return;
    }
    if (Date.now() - state.jobStarted > JOB_TIMEOUT_MS) {
      state.job.status = "timeout";
      renderJob();
      finishJob();
      return;
    }
    state.jobTimer = setTimeout(pollJob, POLL_JOB_MS);
  }

  function finishJob() {
    stopJob();
    setAskBusy(false);
    const job = state.job;
    const refreshed = refreshState();
    refreshEvents();
    const herePromise = job && job.status === "done" ? startHereForJob(job, refreshed) : Promise.resolve(null);
    if (state.jobTarget === "ask") scrollIntoViewIfNeeded($("#job-area"));
    if (state.jobTarget === "ask" && job && job.status === "done" && job.answer && $("#auto-read").checked && !state.spokenJobs.has(job.id)) {
      state.spokenJobs.add(job.id);
      // Wait for the circled picture (at most 4 s) so the spoken answer can point to it.
      const pictureReady = herePromise.then((v) => (v ? v.ready : false)).catch(() => false);
      Promise.race([pictureReady, new Promise((r) => setTimeout(() => r(false), 4000))])
        .then(() => { if (state.job && state.job.id === job.id) speak(spokenAnswer(job)); });
    }
  }

  function renderJob() {
    const job = state.job;
    if (!job) return;
    const { status } = jobEls();
    const q = esc(job.question || "");
    if (job.status === "running" || !job.status) {
      status.innerHTML = `<span class="spinner" aria-hidden="true"></span> Looking into “${q}”…`;
    } else if (job.status === "done") {
      const secs = job.created ? Math.max(1, Math.round(lastStepTs() - job.created)) : null;
      status.innerHTML = `<span aria-hidden="true">✓</span> Answered${secs ? ` in ${secs} seconds` : ""}`;
    } else if (job.status === "timeout") {
      status.innerHTML = `Still working on it. Check back in a minute.`;
    } else {
      status.innerHTML = `Something went wrong with “${q}”.`;
    }
    renderAnswer();
    renderTrace();
  }

  function lastStepTs() {
    let ts = 0;
    for (const s of state.steps) if (s.ts > ts) ts = s.ts;
    return ts || Date.now() / 1000;
  }

  function renderAnswer() {
    const job = state.job;
    if (!job) return;
    const { card, tools } = jobEls();
    if (job.status === "error") {
      const errStep = state.steps.slice().reverse().find((s) => s.kind === "error");
      const msg = job.answer || (errStep && errStep.text) || "iCamFind couldn't finish. Please try again.";
      card.className = "answer-card no-image is-error";
      setHtml(card, `<div><p class="answer-text">${esc(msg)}</p></div>`);
      card.hidden = false;
      if (tools) tools.hidden = true;
      return;
    }
    if (job.status !== "done" || !job.answer) {
      card.hidden = true;
      if (tools) tools.hidden = true;
      return;
    }
    const obj = job.object_id ? state.objects.find((o) => o.id === job.object_id) : null;
    const img = obj ? stableUrl(obj.crop_url) : "";
    const meta = obj ? `${whereText(obj)} · seen ${relTime(obj.last_seen)} (${clock(obj.last_seen)})` : "";
    const here = hereFor(job);
    const showHere = !!(here && here.status !== "none");
    const text = `<div>
        <p class="answer-text">${esc(job.answer)}</p>
        ${meta ? `<p class="answer-meta">${esc(meta)}</p>` : ""}
        ${obj ? `<p><button class="btn btn-small" type="button" data-action="open" data-id="${esc(obj.id)}" data-fkey="ans-open">See its history</button></p>` : ""}
      </div>`;
    if (showHere) {
      card.className = "answer-card has-here";
      setHtml(card, `
        <div class="here-slot"></div>
        <div class="answer-row${img ? "" : " no-inset"}">
          ${img ? `<img class="answer-inset" src="${esc(img)}" alt="Close photo of ${esc(labelOf(obj))}">` : ""}
          ${text}
        </div>`);
      // The picture lives in its own node (a canvas can't be rebuilt from HTML), so re-attach it after renders.
      const slot = card.querySelector(".here-slot");
      if (slot && here.el.parentNode !== slot) slot.appendChild(here.el);
    } else {
      card.className = `answer-card${img ? "" : " no-image"}`;
      setHtml(card, `
        ${img ? `<img src="${esc(img)}" alt="Photo of ${esc(labelOf(obj))}">` : ""}
        ${text}`);
    }
    card.hidden = false;
    if (tools) tools.hidden = !("speechSynthesis" in window);
  }

  function spokenAnswer(job) {
    if (!job || !job.answer) return "";
    const here = hereFor(job);
    return here && here.status === "ready" ? `${job.answer} I've circled it on the camera picture.` : job.answer;
  }

  // ---------- "Here it is": the camera picture with the thing circled ----------

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function hereFor(job) {
    return job && state.here && state.here.jobId === job.id ? state.here : null;
  }

  function cancelHere(view) {
    if (!view) return;
    view.cancelled = true;
    try { view.stop(); } catch { /* animation already over */ }
  }

  function hereAlt(o) {
    const z = zoneName(o.zone);
    return `Camera picture with the ${labelOf(o)} circled${z ? ` on the ${z}` : ""}`;
  }

  // This browser's camera, if it is watching the current home and remembers the thing.
  function liveEngineFor(o) {
    const e = cam.engine;
    if (!e || cam.paused || !camConfig() || cam.household !== state.household) return null;
    try {
      return Array.isArray(e.objects) && e.objects.some((x) => x.id === o.id) ? e : null;
    } catch { return null; }
  }

  // The camera's own close-up from this answer's live check, when it actually saw the thing.
  function closeUpUrl(steps) {
    const s = (steps || []).slice().reverse().find((x) => x.kind === "tool_result" && x.image_url
      && (x.tool === "verify_object_live" || x.tool === "scan_room_for_object")
      && x.output && (x.output.present === true || x.output.found === true));
    return s ? safeUrl(s.image_url) : "";
  }

  async function startHereForJob(job, refreshed) {
    if (!job || !job.object_id || state.jobTarget !== "ask") return null;
    try { await refreshed; } catch { /* use what we have */ }
    if (!state.job || state.job.id !== job.id) return null;
    const o = state.objects.find((x) => x.id === job.object_id);
    if (!o || o.status === "removed") return null; // picked up: there is nothing to circle
    cancelHere(state.here);
    const view = createHereView(o, { closeUp: closeUpUrl(state.steps) });
    view.jobId = job.id;
    state.here = view;
    renderAnswer();
    view.ready.then(() => { if (state.here === view) renderAnswer(); });
    return view;
  }

  function createHereView(o, opts = {}) {
    const fig = document.createElement("figure");
    fig.className = "here-view";
    fig.innerHTML = `
      <div class="here-pic"><p class="here-wait"><span class="spinner" aria-hidden="true"></span> Getting a fresh camera picture…</p></div>
      <figcaption class="here-caption">
        <span class="here-cap-text"></span>
        <button class="btn btn-small here-show" type="button" hidden>Show on the camera</button>
      </figcaption>
      ${opts.closeUp ? `<div class="here-closeup"><img src="${esc(opts.closeUp)}" alt="Close-up of the ${esc(labelOf(o))} taken by the camera just now"><span>Close-up just now</span></div>` : ""}`;
    const view = { el: fig, objectId: o.id, status: "pending", cancelled: false, stop: () => {} };
    view.ready = fillHereView(view, o).catch(() => false).then((ok) => {
      if (!view.cancelled) view.status = ok ? "ready" : "none";
      return ok;
    });
    return view;
  }

  async function fillHereView(view, o) {
    const pic = view.el.querySelector(".here-pic");
    const cap = view.el.querySelector(".here-cap-text");
    const alt = hereAlt(o);
    const engine = liveEngineFor(o);
    if (engine) {
      let url = "";
      try {
        engine.highlight(o.id, 15000);
        url = safeDataImage(engine.outlinedFrameDataUrl(o.id));
      } catch { url = ""; }
      if (url) {
        pic.innerHTML = `<img src="${esc(url)}" alt="${esc(alt)}">`;
        cap.textContent = "Live from this camera, just now";
        const btn = view.el.querySelector(".here-show");
        btn.hidden = false;
        btn.addEventListener("click", () => {
          try { engine.highlight(o.id, 15000); } catch { /* engine stopped */ }
          closeDialog($("#object-dialog"));
          go("camera");
        });
        return true;
      }
    }
    return remoteHere(view, o, pic, cap, alt);
  }

  // Another camera (or the demo home): ask it for a fresh picture, then circle the thing on it.
  async function remoteHere(view, o, pic, cap, alt) {
    const bbox = Array.isArray(o.bbox) && o.bbox.length === 4 ? o.bbox.map(Number) : null;
    const first = state.device;
    if (!bbox || bbox.some((v) => !Number.isFinite(v)) || !first || (o.device && first.id !== o.device)) return false;
    const hh = state.household;
    const before = Number(first.snapshot_at) || 0;
    let asked = false;
    try {
      await api("api/snapshot", { method: "POST", quiet: true });
      asked = true;
    } catch { /* camera offline: use its last picture */ }
    const deadline = Date.now() + 8000;
    while (asked && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500));
      if (view.cancelled || state.household !== hh) return false;
      state.snapForce = true;
      await refreshState();
      if (state.device && (Number(state.device.snapshot_at) || 0) > before) break;
    }
    if (view.cancelled || state.household !== hh) return false;
    const d = state.device;
    const url = d ? safeUrl(d.snapshot_url) : "";
    const fs = d && Array.isArray(d.frame_size) && d.frame_size.length === 2 ? d.frame_size.map(Number) : null;
    if (!url || !fs || !(fs[0] > 0)) return false;
    // The S3 picture has no CORS headers: drawing it is fine, but this canvas must never be read back.
    const img = new Image();
    img.decoding = "async";
    try {
      await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; img.src = url; });
    } catch { return false; }
    if (view.cancelled) return false;
    const W = img.naturalWidth;
    const H = img.naturalHeight;
    if (!W || !H) return false;
    const s = W / fs[0];
    const box = bbox.map((v) => v * s);
    const px = Math.max(1, s);
    const label = labelOf(o);
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", alt);
    const ctx = canvas.getContext("2d");
    const draw = (pulse) => {
      ctx.drawImage(img, 0, 0, W, H);
      if (window.ICamEdge && typeof window.ICamEdge.drawSpotlight === "function") {
        window.ICamEdge.drawSpotlight(ctx, box, W, H, label, pulse, px);
      } else {
        drawRing(ctx, box, W, H, label, pulse, px);
      }
    };
    draw(1);
    pic.innerHTML = "";
    pic.appendChild(canvas);
    const when = d.snapshot_at ? relTime(d.snapshot_at) : "";
    cap.textContent = when ? `Camera picture from ${when}` : "Latest camera picture";
    if (!reducedMotion()) {
      const until = performance.now() + 12000;
      let raf = 0;
      const tick = (t) => {
        if (view.cancelled || t > until) { draw(1); return; }
        draw(0.5 + 0.5 * Math.sin(t / 180));
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
      view.stop = () => cancelAnimationFrame(raf);
    }
    return true;
  }

  // Fallback when the camera module isn't loaded: a plain yellow ring and a "Here" tag.
  function drawRing(ctx, box, W, H, label, pulse, px) {
    const [x, y, w, h] = box;
    const r = Math.hypot(w, h) / 2 + Math.max(8 * px, 0.25 * Math.max(w, h));
    const cx = x + w / 2;
    const cy = y + h / 2;
    ctx.save();
    ctx.lineWidth = (3 + 3 * pulse) * px;
    ctx.strokeStyle = "#facc15";
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
    const text = label ? `Here: ${label}` : "Here";
    ctx.font = `bold ${Math.round(15 * px)}px system-ui, sans-serif`;
    const tw = ctx.measureText(text).width + 12 * px;
    const tx = Math.min(W - tw, Math.max(0, cx - tw / 2));
    const ty = Math.max(20 * px, cy - r - 10 * px);
    ctx.fillStyle = "#facc15";
    ctx.fillRect(tx, ty - 18 * px, tw, 24 * px);
    ctx.fillStyle = "#111";
    ctx.fillText(text, tx + 6 * px, ty);
    ctx.restore();
  }

  function showWhereInDrawer(button) {
    const id = button.getAttribute("data-id");
    const o = state.objects.find((x) => x.id === id) || (state.detailObject && state.detailObject.id === id ? state.detailObject : null);
    const slot = $("#dialog-body .detail-where");
    if (!o || !slot) return;
    cancelHere(state.drawerHere);
    const view = createHereView(o);
    state.drawerHere = view;
    slot.hidden = false;
    slot.innerHTML = "";
    slot.appendChild(view.el);
    button.disabled = true;
    view.ready.then((ok) => {
      button.disabled = false;
      if (!ok && state.drawerHere === view && view.el.parentNode === slot) {
        slot.innerHTML = `<p class="muted">There's no camera picture of this right now.</p>`;
      }
    });
  }

  // Pair each tool_call with the tool_result that follows it so the trace reads as one step per action.
  function traceItems(steps) {
    const items = [];
    const pending = {};
    for (const s of steps) {
      if (s.kind === "tool_call") {
        const it = { n: s.n, kind: "tool", tool: s.tool, input: s.input, ts: s.ts, image_url: s.image_url };
        items.push(it);
        (pending[s.tool] = pending[s.tool] || []).push(it);
      } else if (s.kind === "tool_result") {
        const queue = pending[s.tool];
        const it = queue && queue.length ? queue.shift() : null;
        if (it) {
          it.output = s.output;
          it.done = true;
          if (s.image_url) it.image_url = s.image_url;
        } else {
          items.push({ n: s.n, kind: "tool", tool: s.tool, output: s.output, done: true, ts: s.ts, image_url: s.image_url });
        }
      } else {
        items.push({ n: s.n, kind: s.kind, text: s.text, ts: s.ts, image_url: s.image_url, output: s.output, tool: s.tool });
      }
    }
    return items;
  }

  function summarize(item) {
    const { tool, output: out, input } = item;
    if (tool === "final_answer") {
      const a = input && input.answer;
      return a ? String(a).slice(0, 140) : "";
    }
    if (!item.done) return "working…";
    if (out === null || out === undefined) return "";
    if (typeof out !== "object") return String(out).slice(0, 140);
    if (out.error) return out.error === "camera offline" ? "The camera is offline" : `Problem: ${out.error}`;
    const pct = (v) => (typeof v === "number" ? ` (${Math.round(v * 100)}% match)` : "");
    switch (tool) {
      case "list_objects": {
        const arr = Array.isArray(out) ? out : (out.objects || []);
        return `${plural(arr.length, "thing")} in memory`;
      }
      case "get_object_history": {
        const arr = Array.isArray(out) ? out : (out.events || []);
        return plural(arr.length, "event");
      }
      case "verify_object_live":
        if (out.present === true) return `Still there${zoneName(out.zone) ? ` on the ${out.zone}` : ""}${pct(out.similarity)}`;
        if (out.present === false) return `Not there any more${pct(out.similarity)}`;
        break;
      case "scan_room_for_object":
        if (out.found === true) return `Found it${zoneName(out.zone) ? ` on the ${out.zone}` : ""}${pct(out.confidence)}`;
        if (out.found === false) return "Not found anywhere in view";
        break;
      case "update_object_status": return "Memory updated";
      case "speak_on_device": return out.spoken === false ? "Couldn't speak" : "Said it out loud";
      case "request_caregiver_notification":
        if (out.status === "sent") return "Carer told";
        return "Waiting for your OK";
      case "record_routine_status": return "Noted";
      default: break;
    }
    return "";
  }

  function jsonBlock(title, value) {
    if (value === undefined) return "";
    let text;
    try { text = JSON.stringify(value, null, 2); } catch { text = String(value); }
    if (text.length > 4000) text = `${text.slice(0, 4000)}\n…`;
    return `<div><p class="small muted">${esc(title)}</p><pre class="json">${esc(text)}</pre></div>`;
  }

  function renderTrace() {
    const { list } = jobEls();
    const items = traceItems(state.steps);
    if (!items.length) {
      setHtml(list, `<li><p class="step-thought-text">Starting…</p></li>`);
      return;
    }
    const html = items.map((it) => {
      const n = esc(it.n);
      const img = stableUrl(it.image_url);
      const imgHtml = img ? `<img src="${esc(img)}" alt="Camera picture used in this step" loading="lazy">` : "";
      const time = it.ts ? `<time class="step-time" datetime="${new Date(it.ts * 1000).toISOString()}">${esc(clock(it.ts))}</time>` : "";
      if (it.kind === "thought") {
        return `<li><p class="step-thought-text">${esc(it.text)}</p>${imgHtml}</li>`;
      }
      if (it.kind === "answer" || it.kind === "error") {
        const name = it.kind === "answer" ? "Answer" : "Problem";
        return `<li><p><span class="step-name">${name}</span> ${time}</p><p>${esc(it.text)}</p>${imgHtml}</li>`;
      }
      const name = TOOL_NAMES[it.tool] || "Step";
      const sum = summarize(it);
      const open = state.openSteps.has(String(it.n)) ? " open" : "";
      return `<li>
        <details data-n="${n}"${open}>
          <summary data-fkey="st-${n}"><span class="step-name">${esc(name)}</span>${sum ? `<span class="step-summary">— ${esc(sum)}</span>` : ""}${time}</summary>
          <div class="step-body">${jsonBlock("Details sent", it.input)}${jsonBlock("Details received", it.output)}</div>
        </details>
        ${imgHtml ? `<div class="step-body">${imgHtml}</div>` : ""}
      </li>`;
    }).join("");
    setHtml(list, html);
  }

  // ---------- Speech ----------

  function speak(text) {
    if (!("speechSynthesis" in window) || !text) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 0.95;
    window.speechSynthesis.speak(u);
  }

  function setupMic() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const mic = $("#mic-btn");
    if (!SR) return;
    mic.hidden = false;
    let rec = null;
    let heard = false;
    mic.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = navigator.language || "en-US";
      rec.interimResults = true;
      rec.maxAlternatives = 1;
      heard = false;
      mic.setAttribute("aria-pressed", "true");
      $("#mic-label").textContent = "Stop listening";
      rec.onresult = (e) => {
        let text = "";
        for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
        $("#ask-input").value = text;
        if (e.results[e.results.length - 1].isFinal) heard = true;
      };
      rec.onerror = (e) => {
        toast(e.error === "not-allowed" ? "Microphone access is blocked for this page." : "I didn't catch that. Please try again.");
      };
      rec.onend = () => {
        rec = null;
        mic.setAttribute("aria-pressed", "false");
        $("#mic-label").textContent = "Speak your question";
        const q = $("#ask-input").value.trim();
        if (heard && q) ask(q);
      };
      try { rec.start(); } catch { rec = null; mic.setAttribute("aria-pressed", "false"); }
    });
  }

  // ---------- Menu and settings ----------

  function applyLargeText(on) {
    document.documentElement.classList.toggle("large", on);
    $("#large-text-toggle").setAttribute("aria-pressed", on ? "true" : "false");
  }

  function setMenu(open) {
    $("#menu-panel").hidden = !open;
    $("#menu-btn").setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      const first = $("#menu-panel .menu-item");
      if (first) first.focus();
    }
  }

  // ---------- Event wiring ----------

  function bindOpenLinks(el) {
    el.addEventListener("click", (e) => {
      const b = e.target.closest('[data-action="open"]');
      if (b) openObject(b.getAttribute("data-id"));
    });
  }

  function bindRoomMarkers(el) {
    el.addEventListener("click", (e) => {
      const m = e.target.closest(".marker");
      if (m) openObject(m.getAttribute("data-id"));
    });
    el.addEventListener("keydown", (e) => {
      const m = e.target.closest && e.target.closest(".marker");
      if (m && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openObject(m.getAttribute("data-id")); }
    });
  }

  function bind() {
    $("#login-btn").addEventListener("click", async () => {
      const btn = $("#login-btn");
      btn.disabled = true;
      try {
        await beginLogin();
      } catch {
        btn.disabled = false;
        showLogin("Couldn't start sign-in in this browser. Please try again.");
      }
    });

    window.addEventListener("hashchange", () => { if (!$("#app").hidden) showView(viewFromHash(), true); });

    // Menu
    $("#menu-btn").addEventListener("click", (e) => { e.stopPropagation(); setMenu($("#menu-panel").hidden); });
    document.addEventListener("click", (e) => {
      if (!$("#menu-panel").hidden && !e.target.closest(".menu")) setMenu(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("#menu-panel").hidden) { setMenu(false); $("#menu-btn").focus(); }
    });
    $("#logout-btn").addEventListener("click", signOut);
    $("#large-text-toggle").addEventListener("click", () => {
      const on = !document.documentElement.classList.contains("large");
      applyLargeText(on);
      store.set("icamfind.large", on ? "1" : "0");
    });
    $("#auto-read").addEventListener("change", (e) => store.set("icamfind.autoread", e.target.checked ? "1" : "0"));

    $("#household-select").addEventListener("change", (e) => switchHousehold(e.target.value));

    // Ask
    $("#ask-form").addEventListener("submit", (e) => {
      e.preventDefault();
      ask($("#ask-input").value);
    });
    $$(".chip[data-q]").forEach((chip) => {
      chip.addEventListener("click", () => ask(chip.getAttribute("data-q")));
    });
    $("#read-aloud-btn").addEventListener("click", () => { if (state.job) speak(spokenAnswer(state.job)); });
    $("#dialog-body").addEventListener("click", (e) => {
      const b = e.target.closest('[data-action="where"]');
      if (b) showWhereInDrawer(b);
    });
    $("#object-dialog").addEventListener("close", () => { cancelHere(state.drawerHere); state.drawerHere = null; });

    // Remember which trace steps were expanded across re-renders (toggle does not bubble).
    [$("#trace-list"), $("#jd-trace")].forEach((list) => {
      list.addEventListener("toggle", (e) => {
        const d = e.target;
        if (!(d instanceof HTMLDetailsElement) || !d.dataset.n) return;
        if (d.open) state.openSteps.add(d.dataset.n); else state.openSteps.delete(d.dataset.n);
        list._html = null; // force the next render to rebuild with the new open state
      }, true);
    });

    $("#banners").addEventListener("click", (e) => {
      const b = e.target.closest("[data-action]");
      if (!b) return;
      const action = b.getAttribute("data-action");
      if (action === "dismiss") dismiss(b.getAttribute("data-key"));
      else if (action === "approve" || action === "deny") decideApproval(b);
      else if (action === "go-camera") go("camera");
      else if (action === "go-home") { const h = ownHome(); if (h) switchHousehold(h.id); }
      else if (action === "go-demo") { const h = demoHome(); if (h) switchHousehold(h.id); }
    });

    [$("#recent-strip"), $("#answer-card"), $("#jd-answer")].forEach(bindOpenLinks);
    [$("#demo-room"), $("#things-room")].forEach(bindRoomMarkers);

    // Camera
    $("#cam-setup-btn").addEventListener("click", () => openWizard("setup"));
    $("#cam-pause").addEventListener("click", pauseOrResume);
    $("#cam-places").addEventListener("click", () => { if (cam.stream) openWizard("edit"); });
    $("#cam-reset").addEventListener("click", () => {
      if (cam.engine && typeof cam.engine.resetBackground === "function") {
        cam.engine.resetBackground();
        toast("Okay. Learning the room again, keep still for a few seconds.");
      }
    });
    $("#cam-forget").addEventListener("click", forgetThisCamera);
    $("#demo-switch-btn").addEventListener("click", () => { const h = ownHome(); if (h) switchHousehold(h.id); });
    $("#demo-refresh-btn").addEventListener("click", requestSnapshot);
    $("#things-refresh-btn").addEventListener("click", requestSnapshot);
    $("#pair-btn").addEventListener("click", openPairDialog);
    $("#pair-close").addEventListener("click", () => closeDialog($("#pair-dialog")));
    $("#pair-copy").addEventListener("click", () => copyText($("#pair-command").textContent, "Command"));

    // Wizard
    $("#wiz-next").addEventListener("click", wizNext);
    $("#wiz-back").addEventListener("click", () => setWizStep(Math.max(1, wiz.step - 1)));
    $("#wiz-close").addEventListener("click", closeWizard);
    $("#wizard").addEventListener("close", onWizardClosed);
    $("#wiz-camera-select").addEventListener("change", (e) => turnOnCamera(e.target.value));
    bindDrawing();

    // Things: filters and cards
    $$(".seg[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.filter = btn.getAttribute("data-filter");
        $$(".seg[data-filter]").forEach((b) => b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
        renderObjects(true);
      });
    });
    const grid = $("#objects-grid");
    grid.addEventListener("click", async (e) => {
      const el = e.target.closest("[data-action]");
      if (!el) return;
      const id = el.getAttribute("data-id") || (el.closest("form") && el.closest("form").getAttribute("data-id"));
      const action = el.getAttribute("data-action");
      if (action === "open") openObject(id);
      else if (action === "star") {
        const o = state.objects.find((x) => x.id === id);
        if (!o) return;
        const ok = await updateObject(id, { essential: !o.essential });
        if (ok) { renderObjects(true); toast(!o.essential ? "Marked as important." : "No longer marked as important."); }
      } else if (action === "rename") {
        state.editingId = id;
        renderObjects(true);
      } else if (action === "cancel-rename") {
        state.editingId = null;
        renderObjects(true);
      }
    });
    grid.addEventListener("submit", async (e) => {
      const form = e.target.closest(".rename-form");
      if (!form) return;
      e.preventDefault();
      const id = form.getAttribute("data-id");
      const label = form.elements.label.value.trim();
      if (!label) return;
      const ok = await updateObject(id, { label });
      if (ok) {
        state.editingId = null;
        renderObjects(true);
        const btn = grid.querySelector(fkeySel(`rename-${id}`));
        if (btn) btn.focus();
        toast(`Renamed to “${label}”.`);
      }
    });
    grid.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && e.target.closest(".rename-form")) {
        state.editingId = null;
        renderObjects(true);
      }
    });

    // Routines
    $("#approvals-list").addEventListener("click", (e) => {
      const b = e.target.closest("[data-action]");
      if (b) decideApproval(b);
    });
    $("#routines-list").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-action]");
      if (!b) return;
      const id = b.getAttribute("data-id");
      const r = state.routines.find((x) => x.id === id);
      if (b.getAttribute("data-action") === "check") {
        try {
          const d = await api(`api/routines/${encodeURIComponent(id)}/check`, { method: "POST" });
          startJob(d.job_id, `Checking: ${r ? r.name : "routine"}`, "dialog");
        } catch { /* toast shown */ }
      } else if (b.getAttribute("data-action") === "delete") {
        if (!window.confirm(`Delete the routine “${r ? r.name : ""}”?`)) return;
        try {
          await api(`api/routines/${encodeURIComponent(id)}`, { method: "DELETE" });
          toast("Routine deleted.");
          refreshState();
        } catch { /* toast shown */ }
      }
    });
    $("#routine-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {
        object_id: $("#routine-object").value,
        name: $("#routine-name").value.trim(),
        start: $("#routine-start").value,
        end: $("#routine-end").value,
        tz: $("#routine-tz").value.trim(),
      };
      if (!body.object_id) { toast("Pick a thing to watch first."); return; }
      try {
        await api("api/routines", { method: "POST", body });
        toast("Routine saved.");
        $("#routine-name").value = "";
        refreshState();
      } catch { /* toast shown */ }
    });

    // Sheets
    $("#dialog-close").addEventListener("click", () => closeDialog($("#object-dialog")));
    $("#job-dialog-close").addEventListener("click", () => closeDialog($("#job-dialog")));
    [$("#object-dialog"), $("#job-dialog"), $("#pair-dialog")].forEach((dialog) => {
      dialog.addEventListener("close", returnFocus);
      dialog.addEventListener("click", (e) => { if (e.target === dialog) closeDialog(dialog); }); // backdrop
      dialog.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && typeof dialog.showModal !== "function") closeDialog(dialog);
      });
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.auth && !$("#app").hidden) { refreshState(); refreshEvents(); }
    });
  }

  async function loadConfig() {
    const res = await fetch("config.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`config ${res.status}`);
    const cfg = await res.json();
    if (!cfg || !cfg.cognito_domain || !cfg.client_id || !cfg.redirect_uri) throw new Error("config incomplete");
    return Object.assign({}, cfg, { cognito_domain: String(cfg.cognito_domain).replace(/\/+$/, "") });
  }

  async function init() {
    let tz = "UTC";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { /* keep UTC */ }
    $("#routine-tz").value = tz;
    $("#auto-read").checked = store.get("icamfind.autoread") === "1";

    bind();
    setupMic();
    applyLargeText(store.get("icamfind.large") === "1");

    state.auth = loadAuth();
    const params = new URLSearchParams(location.search);
    const returning = params.has("code") || params.has("error");
    if (returning) showLoginStatus("Signing you in…");

    try {
      state.config = await loadConfig();
    } catch {
      showLogin("Couldn't load the sign-in settings. Please reload the page.");
      return;
    }

    if (returning) {
      try {
        if (params.has("error")) throw new Error(params.get("error_description") || "Sign-in was cancelled.");
        await handleCallback(params);
      } catch (err) {
        saveAuth(null);
        showLogin((err && err.message) || "Sign-in failed. Please try again.");
        return;
      } finally {
        try { history.replaceState(null, "", location.pathname + location.hash); } catch { /* keep URL */ }
      }
    }

    if (state.auth) startApp(); else showLogin("");
  }

  init();
})();
