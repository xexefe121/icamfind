"use strict";

// iCamFind web UI. Plain ES2020, no build step. Talks to the same-origin HTTP API (see docs/API.md).
(() => {
  const POLL_STATE_MS = 5000;
  const POLL_EVENTS_MS = 10000;
  const POLL_JOB_MS = 1000;
  const JOB_TIMEOUT_MS = 120000;
  const URL_REUSE_MS = 45 * 60 * 1000; // presigned URLs last 1 h; reuse to avoid image flicker

  const TOOL_NAMES = {
    list_objects: "Checked memory",
    get_object_history: "Read history",
    verify_object_live: "Looked with the camera",
    scan_room_for_object: "Scanned the room",
    update_object_status: "Updated memory",
    speak_on_device: "Spoke to you",
    request_caregiver_notification: "Asked caregiver approval",
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
    escalated: ["Caregiver notified", "status-danger"],
  };

  const state = {
    config: null, // {cognito_domain, client_id, redirect_uri, region} from config.json
    auth: null, // {id_token, refresh_token, expires_at}
    me: null,
    households: [],
    household: null, // selected household id (sent as x-icamfind-household)
    householdInfo: null, // {id, name, demo} from /api/state
    devices: [],
    device: null,
    objects: [],
    alerts: [],
    approvals: [],
    routines: [],
    events: [],
    filter: "all",
    editingId: null,
    job: null,
    steps: [],
    jobTimer: null,
    jobStarted: 0,
    openSteps: new Set(),
    spokenJobs: new Set(),
    timers: {},
    dialogReturnFocus: null,
    dialogReturnKey: null,
    snapForce: false,
  };

  const $ = (sel, root = document) => root.querySelector(sel);

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
  };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function safeUrl(u) {
    return typeof u === "string" && /^https?:\/\//i.test(u) ? u : "";
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

  // The room snapshot may be overwritten in place, so refresh it at least every minute or on demand.
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
    if (o.status === "removed") return z ? `Taken from the ${z}` : "Taken";
    return z ? `On the ${z}` : "In view of the camera";
  }

  function labelOf(o) {
    return (o && (o.label || o.local_label)) || "Unnamed object";
  }

  function plural(n, word) {
    return `${n} ${word}${n === 1 ? "" : "s"}`;
  }

  // Replace innerHTML only when it changed, keeping keyboard focus on the same logical control.
  function setHtml(el, html) {
    if (el._html === html) return;
    const active = document.activeElement;
    const fkey = active && el.contains(active) ? active.getAttribute("data-fkey") : null;
    el.innerHTML = html;
    el._html = html;
    if (fkey) {
      const sel = `[data-fkey="${window.CSS && CSS.escape ? CSS.escape(fkey) : fkey}"]`;
      const again = el.querySelector(sel);
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

  // ---------- Sign-in: Cognito hosted UI, authorization code + PKCE ----------

  const AUTH_KEY = "icamfind.auth";
  const PKCE_KEY = "icamfind.pkce";
  const HOME_KEY = "icamfind.household";

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

  function jwtExpiryMs(token) {
    try {
      const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      const bin = atob(part + "===".slice((part.length + 3) % 4));
      const claims = JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
      return typeof claims.exp === "number" ? claims.exp * 1000 : 0;
    } catch { return 0; }
  }

  function loadAuth() {
    try {
      const a = JSON.parse(store.get(AUTH_KEY) || "null");
      return a && a.id_token ? a : null;
    } catch { return null; }
  }

  function saveAuth(a) {
    state.auth = a;
    store.set(AUTH_KEY, a ? JSON.stringify(a) : null);
  }

  function tokenSet(resp, previous) {
    const fallback = Date.now() + (Number(resp.expires_in) || 3600) * 1000;
    return {
      id_token: resp.id_token,
      refresh_token: resp.refresh_token || (previous && previous.refresh_token) || null,
      expires_at: jwtExpiryMs(resp.id_token) || fallback,
    };
  }

  async function tokenRequest(params) {
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
    const data = await tokenRequest({
      grant_type: "authorization_code",
      client_id: state.config.client_id,
      code: params.get("code"),
      redirect_uri: state.config.redirect_uri,
      code_verifier: saved.verifier,
    });
    saveAuth(tokenSet(data, null));
  }

  let refreshing = null;
  function refreshTokens() {
    if (refreshing) return refreshing;
    const current = state.auth;
    if (!current || !current.refresh_token || !state.config) return Promise.resolve(false);
    refreshing = tokenRequest({
      grant_type: "refresh_token",
      client_id: state.config.client_id,
      refresh_token: current.refresh_token,
    })
      .then((data) => { saveAuth(tokenSet(data, current)); return true; })
      .catch(() => false)
      .finally(() => { refreshing = null; });
    return refreshing;
  }

  async function freshIdToken() {
    if (!state.auth) return null;
    if (state.auth.expires_at - Date.now() < 60000 && !(await refreshTokens())) return null;
    return state.auth ? state.auth.id_token : null;
  }

  // Local session ended (expired or refresh failed): back to the sign-in screen.
  function expireSession(message) {
    saveAuth(null);
    stopJob();
    showLogin(message);
  }

  // User-initiated sign-out also ends the hosted-UI session.
  function signOut() {
    const cfg = state.config;
    saveAuth(null);
    stopJob();
    stopPolling();
    if (cfg && cfg.cognito_domain) {
      const q = new URLSearchParams({ client_id: cfg.client_id, logout_uri: cfg.redirect_uri });
      location.assign(`${cfg.cognito_domain}/logout?${q.toString()}`);
    } else {
      showLogin("");
    }
  }

  // ---------- API ----------

  async function api(path, opts = {}, retried = false) {
    const token = await freshIdToken();
    if (!token) {
      if ($("#login").hidden) expireSession("Your session has ended. Please sign in again.");
      throw new Error("unauthorized");
    }
    const headers = { authorization: `Bearer ${token}` };
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
      toast("Can't reach iCamFind right now. Check the internet connection.");
      throw err;
    }
    if (res.status === 401) {
      if (!retried && (await refreshTokens())) return api(path, opts, true);
      if ($("#login").hidden) expireSession("Your session has ended. Please sign in again.");
      throw new Error("unauthorized");
    }
    let data = null;
    try { data = await res.json(); } catch { /* empty or non-JSON body */ }
    if (!res.ok) {
      const msg = (data && data.error) || `Request failed (${res.status})`;
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
    try {
      await loadMe();
    } catch {
      return; // unauthorized: the sign-in screen is already showing
    }
    refreshState();
    refreshEvents();
    startPolling();
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

  // ---------- Households ----------

  function currentHousehold() {
    return state.households.find((h) => h.id === state.household) || null;
  }

  function isDemo() {
    const h = currentHousehold();
    if (h) return !!h.demo;
    return !!(state.householdInfo && state.householdInfo.demo);
  }

  function householdName(id) {
    const h = state.households.find((x) => x.id === id);
    return (h && h.name) || "this home";
  }

  function renderHouseholdControls() {
    const sel = $("#household-select");
    const html = state.households
      .map((h) => `<option value="${esc(h.id)}">${esc(h.name || "Home")}${h.demo ? " (Demo)" : ""}</option>`)
      .join("");
    if (sel._html !== html) {
      sel.innerHTML = html;
      sel._html = html;
    }
    if (state.household && sel.value !== state.household) sel.value = state.household;
    $("#household-wrap").hidden = state.households.length === 0;

    const demo = isDemo();
    $("#demo-badge").hidden = !demo;
    $("#demo-banner").hidden = !demo;

    const email = state.me && state.me.email;
    const u = $("#user-email");
    u.textContent = email || "";
    u.title = email ? `Signed in as ${email}` : "";
    u.hidden = !email;
  }

  // Everything shown below the header belongs to one household; drop it before switching.
  function resetHouseholdState() {
    stopJob();
    state.job = null;
    state.steps = [];
    state.openSteps = new Set();
    state.householdInfo = null;
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
    hideCameraToken();
    $("#job-area").hidden = true;
    $("#answer-card").hidden = true;
    $("#answer-tools").hidden = true;
    setAskBusy(false);
    const dialog = $("#object-dialog");
    if (dialog.open || dialog.hasAttribute("open")) closeDialog();
  }

  function switchHousehold(id) {
    if (!id || id === state.household) return;
    state.household = id;
    store.set(HOME_KEY, id);
    resetHouseholdState();
    renderAll();
    renderEvents();
    refreshState();
    refreshEvents();
    toast(`Switched to ${householdName(id)}.`);
  }

  function startPolling() {
    stopPolling();
    state.timers.state = setInterval(() => { if (!document.hidden) refreshState(); }, POLL_STATE_MS);
    state.timers.events = setInterval(() => { if (!document.hidden) refreshEvents(); }, POLL_EVENTS_MS);
  }

  function stopPolling() {
    clearInterval(state.timers.state);
    clearInterval(state.timers.events);
    state.timers = {};
  }

  async function refreshState() {
    const hh = state.household;
    try {
      const d = await api("api/state", { quiet: true });
      if (state.household !== hh) return; // household switched while this was in flight
      state.householdInfo = d.household || null;
      state.devices = Array.isArray(d.devices) ? d.devices : [];
      state.device = d.device || null;
      state.objects = Array.isArray(d.objects) ? d.objects : [];
      state.alerts = Array.isArray(d.alerts) ? d.alerts : [];
      state.approvals = Array.isArray(d.approvals) ? d.approvals : [];
      state.routines = Array.isArray(d.routines) ? d.routines : [];
      renderAll();
    } catch { /* toast already shown for network errors */ }
  }

  async function refreshEvents() {
    const hh = state.household;
    try {
      const d = await api("api/events?limit=50", { quiet: true });
      if (state.household !== hh) return;
      state.events = Array.isArray(d.events) ? d.events : [];
      renderEvents();
    } catch { /* handled in api() */ }
  }

  function renderAll() {
    renderHouseholdControls();
    renderHeader();
    renderCameras();
    renderRoom();
    renderObjects();
    renderAlerts();
    renderApprovals();
    renderRoutines();
    renderRoutineOptions();
    if (state.job) renderAnswer();
  }

  // ---------- Header ----------

  function renderHeader() {
    const d = state.device;
    const pill = $("#device-pill");
    const name = d ? (d.name || d.id || "") : "";
    const named = name ? ` · ${name}` : "";
    if (!d) {
      pill.className = "pill pill-muted";
      pill.textContent = "Camera not connected yet";
    } else if (d.online) {
      pill.className = "pill pill-online";
      pill.textContent = `Camera online${named} · seen ${relTime(d.last_heartbeat)}`;
    } else {
      pill.className = "pill pill-offline";
      pill.textContent = d.last_heartbeat
        ? `Camera offline${named} · last seen ${relTime(d.last_heartbeat)}`
        : `Camera offline${named}`;
    }

    const now = Date.now() / 1000;
    const today = state.alerts.filter((a) => a.ts && now - a.ts < 86400).length;
    const badge = $("#alerts-badge");
    badge.hidden = today === 0;
    $("#alerts-badge-text").textContent = `${plural(today, "alert")} today`;
  }

  // ---------- Cameras ----------

  function renderCameras() {
    const list = $("#cameras-list");
    const demo = isDemo();
    const devices = state.devices.slice().sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));
    if (!devices.length) {
      const msg = demo ? "The replay camera hasn't checked in yet." : "No cameras connected yet.";
      setHtml(list, `<li class="empty">${esc(msg)}</li>`);
    } else {
      setHtml(list, devices.map((d) => {
        const online = !!d.online;
        return `<li class="camera-item">
          <div class="camera-head">
            <strong>${esc(d.name || d.id)}</strong>
            <span class="pill ${online ? "pill-online" : "pill-offline"}">${online ? "Online" : "Offline"}</span>
          </div>
          <p class="ev-time">Last heartbeat ${timeTag(d.last_heartbeat)}</p>
        </li>`;
      }).join(""));
    }
    const h = currentHousehold();
    const owner = !!(h && h.role === "owner");
    $("#camera-demo-note").hidden = !demo;
    $("#camera-add").hidden = demo || !owner;
    $("#camera-member-note").hidden = demo || owner || !h;
  }

  function showCameraToken(d) {
    const panel = $("#camera-token-panel");
    $("#camera-token-name").textContent = (d.device && (d.device.name || d.device.id)) || "camera";
    $("#camera-token").value = d.token || "";
    $("#camera-command").textContent = d.run_command || "";
    panel.hidden = false;
    $("#camera-token-title").focus();
  }

  function hideCameraToken() {
    $("#camera-token").value = "";
    $("#camera-command").textContent = "";
    $("#camera-token-panel").hidden = true;
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

  // ---------- Room view ----------

  function renderRoom() {
    const view = $("#room-view");
    const d = state.device;
    const fs = d && Array.isArray(d.frame_size) && d.frame_size.length === 2 ? d.frame_size : null;
    const snap = d ? snapshotUrl(d.snapshot_url) : "";
    if (!fs && !snap) {
      view.style.aspectRatio = "";
      setHtml(view, `<p class="placeholder">No camera view yet. Press “Refresh camera view” once the camera is online.</p>`);
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
      const cx = pts.reduce((s, p) => s + Number(p[0]), 0) / pts.length;
      const cy = pts.reduce((s, p) => s + Number(p[1]), 0) / pts.length;
      const cls = z.name === doorZone ? "zone-shape zone-door" : "zone-shape";
      const label = z.name === doorZone ? `${z.name} (door)` : z.name;
      return `<polygon class="${cls}" points="${esc(points)}"><title>${esc(label)}</title></polygon>` +
        `<text class="zone-label" x="${cx}" y="${cy}" font-size="${font}" text-anchor="middle">${esc(label)}</text>`;
    }).join("");

    const markers = state.objects
      .filter((o) => o.status !== "removed" && Array.isArray(o.bbox) && o.bbox.length === 4)
      .map((o) => {
        const [x, y, bw, bh] = o.bbox.map(Number);
        const cx = x + bw / 2;
        const cy = y + bh / 2;
        const r = Math.max(8, Math.round(w / 70));
        const name = labelOf(o);
        const aria = `${name}, ${whereText(o).toLowerCase()}, seen ${relTime(o.last_seen)}. Open details.`;
        return `<g class="marker${o.essential ? " essential" : ""}" role="button" tabindex="0" data-id="${esc(o.id)}" data-fkey="m-${esc(o.id)}" aria-label="${esc(aria)}">` +
          `<circle cx="${cx}" cy="${cy}" r="${r}"></circle>` +
          `<text x="${cx}" y="${cy - r - 6}" font-size="${Math.round(font * 0.85)}" text-anchor="middle">${esc(name)}</text></g>`;
      }).join("");

    const noImg = snap ? "" :
      `<text x="${w / 2}" y="${h / 2}" font-size="${font}" text-anchor="middle" class="zone-label">No picture yet — press “Refresh camera view”</text>`;

    const img = snap ? `<img src="${esc(snap)}" alt="Latest camera view of the room. People are blurred.">` : "";
    setHtml(view, `${img}<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="group" aria-label="Zones and remembered objects">${zones}${noImg}${markers}</svg>`);
  }

  // ---------- Objects ----------

  function renderObjects(force) {
    if (state.editingId && !force) return; // don't clobber an inline rename in progress
    const grid = $("#objects-grid");
    let list = state.objects.slice().sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
    if (state.filter === "present") list = list.filter((o) => o.status !== "removed");
    if (state.filter === "removed") list = list.filter((o) => o.status === "removed");
    if (!list.length) {
      const msg = state.objects.length
        ? "Nothing matches this filter."
        : "Nothing remembered yet. Put something down where the camera can see it.";
      setHtml(grid, `<p class="placeholder">${msg}</p>`);
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
    const chip = removed
      ? `<span class="status-chip status-muted">Taken</span>`
      : `<span class="status-chip status-ok">On a surface</span>`;
    const editing = state.editingId === o.id;
    const rename = editing ? `
      <form class="rename-form" data-id="${id}">
        <label class="visually-hidden" for="rn-${cssId(o.id)}">New name for ${esc(name)}</label>
        <input id="rn-${cssId(o.id)}" name="label" value="${esc(name)}" maxlength="60" required>
        <button class="btn btn-small btn-primary" type="submit">Save</button>
        <button class="btn btn-small" type="button" data-action="cancel-rename">Cancel</button>
      </form>` : "";
    return `
      <article class="obj-card${removed ? " is-removed" : ""}" aria-label="${esc(name)}">
        <button class="obj-open" type="button" data-action="open" data-id="${id}" data-fkey="open-${id}">
          ${photo}
          <div class="obj-info">
            <p class="obj-label">${o.essential ? '<span aria-hidden="true">★ </span>' : ""}${esc(name)}</p>
            <p class="obj-where">${esc(whereText(o))} · ${timeTag(o.last_seen)}</p>
          </div>
        </button>
        <div class="obj-actions">
          ${chip}
          <button class="btn btn-small star-btn" type="button" data-action="star" data-id="${id}" data-fkey="star-${id}"
            aria-pressed="${o.essential ? "true" : "false"}" title="Essential items trigger the leaving-the-house alert">
            <span aria-hidden="true">${o.essential ? "★" : "☆"}</span> Essential
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

  // ---------- Object detail dialog ----------

  async function openObject(id) {
    const dialog = $("#object-dialog");
    state.dialogReturnFocus = document.activeElement;
    state.dialogReturnKey = document.activeElement ? document.activeElement.getAttribute("data-fkey") : null;
    const known = state.objects.find((o) => o.id === id);
    $("#dialog-title").textContent = known ? labelOf(known) : "Object";
    $("#dialog-body").innerHTML = `<p class="muted">Loading history…</p>`;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    $("#dialog-close").focus();
    try {
      const d = await api(`api/objects/${encodeURIComponent(id)}`);
      renderObjectDetail(d.object || known || {}, Array.isArray(d.events) ? d.events : []);
    } catch {
      $("#dialog-body").innerHTML = `<p class="error-text">Couldn't load this object's history.</p>`;
    }
  }

  function renderObjectDetail(o, events) {
    const name = labelOf(o);
    $("#dialog-title").textContent = name;
    const img = stableUrl(o.crop_url);
    const rows = [
      ["Where", whereText(o)],
      ["Last seen", o.last_seen ? `${relTime(o.last_seen)} (${absTime(o.last_seen)})` : "never"],
      ["First seen", o.first_seen ? absTime(o.first_seen) : "—"],
      ["Camera saw", o.local_label || "object"],
      ["Description", o.description || "—"],
      ["Essential", o.essential ? "Yes — reminds you at the door" : "No"],
    ];
    const history = events.length
      ? `<ol class="timeline">${events.map((e) => eventRow(e, false)).join("")}</ol>`
      : `<p class="muted">No history yet.</p>`;
    $("#dialog-body").innerHTML = `
      <div class="detail-top">
        ${img ? `<img src="${esc(img)}" alt="Photo of ${esc(name)}">` : `<div class="obj-photo-empty" aria-hidden="true">No photo</div>`}
        <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      </div>
      <h3>History</h3>
      ${history}`;
  }

  function closeDialog() {
    const dialog = $("#object-dialog");
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else { dialog.removeAttribute("open"); onDialogClosed(); }
  }

  function onDialogClosed() {
    // The opener may have been re-rendered while the dialog was open; find it again by its focus key.
    let back = state.dialogReturnFocus;
    const key = state.dialogReturnKey;
    state.dialogReturnFocus = null;
    state.dialogReturnKey = null;
    if ((!back || !document.contains(back)) && key) {
      back = document.querySelector(`[data-fkey="${window.CSS && CSS.escape ? CSS.escape(key) : key}"]`);
    }
    if (back && document.contains(back)) back.focus();
  }

  // ---------- Timeline ----------

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

  function eventRow(e, linkObject) {
    const t = EVENT_TYPES[e.type] || { text: e.type || "Event", icon: "•" };
    const img = stableUrl(e.crop_url);
    const thumb = img
      ? `<img src="${esc(img)}" alt="" loading="lazy">`
      : `<span class="thumb-empty" aria-hidden="true"></span>`;
    const sentence = esc(eventSentence(e));
    const body = linkObject && e.object_id
      ? `<button class="linklike" type="button" data-action="open" data-id="${esc(e.object_id)}" data-fkey="ev-${esc(e.id)}">${sentence}</button>`
      : sentence;
    const conf = typeof e.confidence === "number" ? ` · ${Math.round(e.confidence * 100)}% sure` : "";
    return `<li>${thumb}<div>
      <p class="ev-line"><span class="ev-type ev-${esc(e.type)}"><span aria-hidden="true">${t.icon}</span>${esc(t.text)}</span>${body}</p>
      <p class="ev-time">${timeTag(e.ts)} · ${esc(clock(e.ts))}${esc(conf)}${e.note && e.type !== "corrected" ? ` · ${esc(e.note)}` : ""}</p>
    </div></li>`;
  }

  function renderEvents() {
    const list = $("#events-list");
    if (!state.events.length) {
      setHtml(list, `<li class="empty-row"><p class="muted">No activity yet.</p></li>`);
      return;
    }
    setHtml(list, state.events.map((e) => eventRow(e, true)).join(""));
  }

  // ---------- Alerts ----------

  function renderAlerts() {
    const list = $("#alerts-list");
    if (!state.alerts.length) {
      setHtml(list, `<li class="empty">No alerts. All calm.</li>`);
      return;
    }
    const sorted = state.alerts.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
    setHtml(list, sorted.map((a) => {
      const kind = a.kind === "door_essentials" ? "Leaving without essentials" : (a.kind || "Alert");
      return `<li class="alert-item"><p class="alert-msg">${esc(a.message || kind)}</p>
        <p class="ev-time">${esc(kind)} · ${timeTag(a.ts)}</p></li>`;
    }).join(""));
  }

  // ---------- Caregiver: approvals and routines ----------

  function renderApprovals() {
    const list = $("#approvals-list");
    const pending = state.approvals.filter((a) => !a.status || a.status === "pending");
    if (!pending.length) {
      setHtml(list, `<li class="empty">Nothing waiting.</li>`);
      return;
    }
    setHtml(list, pending.map((a) => `
      <li>
        <p><strong>Notify caregiver?</strong></p>
        <p>${esc(a.message)}</p>
        ${a.reason ? `<p class="muted small">Why: ${esc(a.reason)}</p>` : ""}
        <p class="ev-time">Asked ${timeTag(a.created)}</p>
        <div class="approval-actions">
          <button class="btn btn-small btn-primary" type="button" data-action="approve" data-id="${esc(a.id)}" data-fkey="ap-${esc(a.id)}">Approve and send</button>
          <button class="btn btn-small" type="button" data-action="deny" data-id="${esc(a.id)}" data-fkey="dn-${esc(a.id)}">Don't send</button>
        </div>
      </li>`).join(""));
  }

  function renderRoutines() {
    const list = $("#routines-list");
    if (!state.routines.length) {
      setHtml(list, `<li class="empty">No routines yet. Add one below, for example morning pills.</li>`);
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
        <p class="muted small">Watching ${esc(obj ? labelOf(obj) : "an object")} · ${esc(r.start)}–${esc(r.end)} (${esc(r.tz)})</p>
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
      : `<option value="" disabled selected>No objects remembered yet</option>`;
    if (sel._html === html) return;
    const prev = sel.value;
    sel.innerHTML = html;
    sel._html = html;
    if (prev && objs.some((o) => o.id === prev)) sel.value = prev;
  }

  // ---------- Ask + agent job ----------

  function setAskBusy(busy) {
    const btn = $("#ask-btn");
    btn.disabled = busy;
    btn.textContent = busy ? "Asking…" : "Ask";
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
      startJob(d.job_id, q);
    } catch {
      setAskBusy(false);
    }
  }

  function startJob(id, label) {
    if (!id) { setAskBusy(false); toast("The assistant didn't start. Please try again."); return; }
    stopJob();
    state.job = { id, status: "running", question: label };
    state.steps = [];
    state.openSteps = new Set();
    state.jobStarted = Date.now();
    $("#job-area").hidden = false;
    $("#trace").open = true;
    setAskBusy(true);
    renderJob();
    pollJob();
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
    if (job && job.status === "done" && job.answer && $("#auto-read").checked && !state.spokenJobs.has(job.id)) {
      state.spokenJobs.add(job.id);
      speak(job.answer);
    }
    refreshState();
    refreshEvents();
  }

  function renderJob() {
    const job = state.job;
    if (!job) return;
    const status = $("#job-status");
    const q = esc(job.question || "");
    if (job.status === "running" || !job.status) {
      status.innerHTML = `<span class="spinner" aria-hidden="true"></span> Working on “${q}”…`;
    } else if (job.status === "done") {
      const secs = job.created ? Math.max(1, Math.round(lastStepTs() - job.created)) : null;
      status.innerHTML = `<span aria-hidden="true">✓</span> Answered “${q}”${secs ? ` in ${secs} s` : ""}`;
    } else if (job.status === "timeout") {
      status.innerHTML = `<span aria-hidden="true">…</span> Still working on “${q}”. I stopped waiting after 2 minutes; check Recent activity shortly.`;
    } else {
      status.innerHTML = `<span aria-hidden="true">!</span> Something went wrong with “${q}”.`;
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
    const card = $("#answer-card");
    const tools = $("#answer-tools");
    if (!job) return;
    if (job.status === "error") {
      const errStep = state.steps.slice().reverse().find((s) => s.kind === "error");
      const msg = job.answer || (errStep && errStep.text) || "The assistant couldn't finish. Please try again.";
      card.className = "answer-card no-image is-error";
      setHtml(card, `<div><p class="answer-text">${esc(msg)}</p></div>`);
      card.hidden = false;
      tools.hidden = true;
      return;
    }
    if (job.status !== "done" || !job.answer) {
      card.hidden = true;
      tools.hidden = true;
      return;
    }
    const obj = job.object_id ? state.objects.find((o) => o.id === job.object_id) : null;
    const img = obj ? stableUrl(obj.crop_url) : "";
    const meta = obj ? `${whereText(obj)} · last seen ${relTime(obj.last_seen)} (${clock(obj.last_seen)})` : "";
    card.className = `answer-card${img ? "" : " no-image"}`;
    setHtml(card, `
      ${img ? `<img src="${esc(img)}" alt="Photo of ${esc(labelOf(obj))}">` : ""}
      <div>
        <p class="answer-text">${esc(job.answer)}</p>
        ${meta ? `<p class="answer-meta">${esc(meta)}</p>` : ""}
        ${obj ? `<p><button class="btn btn-small" type="button" data-action="open" data-id="${esc(obj.id)}" data-fkey="ans-open">See its history</button></p>` : ""}
      </div>`);
    card.hidden = false;
    tools.hidden = !("speechSynthesis" in window);
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
    if (out.error) return `Problem: ${out.error}`;
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
      case "update_object_status":
        return "Memory updated";
      case "speak_on_device":
        return out.spoken === false ? "Couldn't speak on the device" : "Message spoken on the device";
      case "request_caregiver_notification":
        if (out.status === "sent") return "Caregiver notified";
        return out.status ? `Approval ${out.status}` : "Approval requested";
      default:
        break;
    }
    if (typeof out.similarity === "number") return pct(out.similarity).trim();
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
    const list = $("#trace-list");
    const items = traceItems(state.steps);
    if (!items.length) {
      setHtml(list, `<li class="step-thought"><p class="step-thought-text">Starting…</p></li>`);
      return;
    }
    const html = items.map((it) => {
      const n = esc(it.n);
      const img = stableUrl(it.image_url);
      const imgHtml = img ? `<img src="${esc(img)}" alt="Camera picture used in this step" loading="lazy">` : "";
      const time = it.ts ? `<time class="step-time" datetime="${new Date(it.ts * 1000).toISOString()}">${esc(clock(it.ts))}</time>` : "";
      if (it.kind === "thought") {
        return `<li class="step-thought"><p class="step-thought-text">${esc(it.text)}</p>${imgHtml}</li>`;
      }
      if (it.kind === "answer" || it.kind === "error") {
        const name = it.kind === "answer" ? "Answer" : "Problem";
        return `<li class="step-${it.kind}"><p><span class="step-name">${name}</span> ${time}</p><p>${esc(it.text)}</p>${imgHtml}</li>`;
      }
      const name = TOOL_NAMES[it.tool] || it.tool || "Step";
      const sum = summarize(it);
      const open = state.openSteps.has(String(it.n)) ? " open" : "";
      return `<li class="step-tool">
        <details data-n="${n}"${open}>
          <summary data-fkey="st-${n}"><span class="step-name">${esc(name)}</span>${sum ? `<span class="step-summary">— ${esc(sum)}</span>` : ""}${time}</summary>
          <div class="step-body">${imgHtml}${jsonBlock("What I sent", it.input)}${jsonBlock("What came back", it.output)}</div>
        </details>
        ${imgHtml && !open ? `<div class="step-body">${imgHtml}</div>` : ""}
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
        toast(e.error === "not-allowed" ? "Microphone permission is blocked for this page." : "I didn't catch that. Please try again.");
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

  // ---------- Settings ----------

  function applyLargeText(on) {
    document.documentElement.classList.toggle("large", on);
    $("#large-text-toggle").setAttribute("aria-pressed", on ? "true" : "false");
  }

  // ---------- Event wiring ----------

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

    $("#logout-btn").addEventListener("click", signOut);

    $("#household-select").addEventListener("change", (e) => switchHousehold(e.target.value));

    $("#camera-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = $("#camera-name").value.trim();
      if (!name) return;
      const btn = e.target.querySelector('button[type="submit"]');
      btn.disabled = true;
      const hh = state.household;
      try {
        const d = await api("api/devices", { method: "POST", body: { name } });
        if (state.household === hh) {
          $("#camera-name").value = "";
          showCameraToken(d);
          refreshState();
        }
      } catch { /* toast shown */ } finally {
        btn.disabled = false;
      }
    });
    $("#copy-token-btn").addEventListener("click", () => copyText($("#camera-token").value, "Token"));
    $("#copy-command-btn").addEventListener("click", () => copyText($("#camera-command").textContent, "Command"));
    $("#camera-token").addEventListener("focus", (e) => e.target.select());
    $("#token-done-btn").addEventListener("click", () => {
      hideCameraToken();
      $("#camera-name").focus();
    });

    $("#large-text-toggle").addEventListener("click", () => {
      const on = !document.documentElement.classList.contains("large");
      applyLargeText(on);
      store.set("icamfind.large", on ? "1" : "0");
    });

    $("#ask-form").addEventListener("submit", (e) => {
      e.preventDefault();
      ask($("#ask-input").value);
    });

    document.querySelectorAll(".chip[data-q]").forEach((chip) => {
      chip.addEventListener("click", () => ask(chip.getAttribute("data-q")));
    });

    $("#auto-read").addEventListener("change", (e) => store.set("icamfind.autoread", e.target.checked ? "1" : "0"));
    $("#read-aloud-btn").addEventListener("click", () => { if (state.job) speak(state.job.answer); });

    // Remember which trace steps the user expanded across re-renders (toggle does not bubble).
    $("#trace-list").addEventListener("toggle", (e) => {
      const d = e.target;
      if (!(d instanceof HTMLDetailsElement) || !d.dataset.n) return;
      if (d.open) state.openSteps.add(d.dataset.n); else state.openSteps.delete(d.dataset.n);
      $("#trace-list")._html = null; // force next render to rebuild with the new open state
    }, true);

    $("#snapshot-btn").addEventListener("click", async () => {
      try {
        await api("api/snapshot", { method: "POST" });
        toast("Asking the camera for a fresh picture…");
        const again = () => { state.snapForce = true; refreshState(); };
        setTimeout(again, 4000);
        setTimeout(again, 9000);
      } catch { /* toast shown */ }
    });

    // Room markers
    const room = $("#room-view");
    room.addEventListener("click", (e) => {
      const m = e.target.closest(".marker");
      if (m) openObject(m.getAttribute("data-id"));
    });
    room.addEventListener("keydown", (e) => {
      const m = e.target.closest && e.target.closest(".marker");
      if (m && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openObject(m.getAttribute("data-id")); }
    });

    // Filters
    document.querySelectorAll(".seg[data-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.filter = btn.getAttribute("data-filter");
        document.querySelectorAll(".seg[data-filter]").forEach((b) => b.setAttribute("aria-pressed", b === btn ? "true" : "false"));
        renderObjects(true);
      });
    });

    // Objects grid actions
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
        if (ok) { renderObjects(true); toast(!o.essential ? "Marked as essential." : "No longer essential."); }
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
        const btn = grid.querySelector(`[data-fkey="rename-${window.CSS && CSS.escape ? CSS.escape(id) : id}"]`);
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

    // Anything else with data-action="open" (timeline, answer card)
    [$("#events-list"), $("#answer-card")].forEach((el) => {
      el.addEventListener("click", (e) => {
        const b = e.target.closest('[data-action="open"]');
        if (b) openObject(b.getAttribute("data-id"));
      });
    });

    // Approvals
    $("#approvals-list").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-action]");
      if (!b) return;
      const decision = b.getAttribute("data-action") === "approve" ? "approve" : "deny";
      b.disabled = true;
      try {
        await api(`api/approvals/${encodeURIComponent(b.getAttribute("data-id"))}`, { method: "POST", body: { decision } });
        toast(decision === "approve" ? "Caregiver notified." : "Okay, nothing was sent.");
        refreshState();
      } catch {
        b.disabled = false;
      }
    });

    // Routines
    $("#routines-list").addEventListener("click", async (e) => {
      const b = e.target.closest("[data-action]");
      if (!b) return;
      const id = b.getAttribute("data-id");
      const r = state.routines.find((x) => x.id === id);
      if (b.getAttribute("data-action") === "check") {
        try {
          const d = await api(`api/routines/${encodeURIComponent(id)}/check`, { method: "POST" });
          startJob(d.job_id, `Routine check: ${r ? r.name : "routine"}`);
          $("#ask").scrollIntoView({ block: "start" });
          $("#job-status").setAttribute("tabindex", "-1");
          $("#job-status").focus();
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
      if (!body.object_id) { toast("Pick an object for the routine first."); return; }
      try {
        await api("api/routines", { method: "POST", body });
        toast("Routine saved.");
        $("#routine-name").value = "";
        refreshState();
      } catch { /* toast shown */ }
    });

    // Dialog
    const dialog = $("#object-dialog");
    $("#dialog-close").addEventListener("click", closeDialog);
    dialog.addEventListener("close", onDialogClosed);
    dialog.addEventListener("click", (e) => { if (e.target === dialog) closeDialog(); }); // backdrop click
    dialog.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && typeof dialog.showModal !== "function") closeDialog();
    });

    $("#alerts-badge").addEventListener("click", () => setTimeout(() => $("#alerts").focus(), 0));

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
    if (!("speechSynthesis" in window)) $("#answer-tools").hidden = true;

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
