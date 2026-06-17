(function () {
  "use strict";

  const COGNITO_KEY      = "fd_cognito_session_v1";
  const PKCE_KEY         = "fd_cognito_pkce_v1";
  const AUTO_REFRESH     = 60_000;
  const TOKEN_REFRESH_PAD = 60_000;

  // ── State ──────────────────────────────────────────────────────────────────
  let refreshTimer = null;
  let tokenRefreshTimer = null;
  let authState = {
    mode: "cognito",
    config: null,
    tokens: null,
    me: null,
    canManageInvites: false,
    canChangePassword: false,
    isActive: false,
    deniedReason: "",
  };

  // Global filters — shared across all tabs
  let gf = { user_id: "", country: "", channel: "", from: "", to: "" };

  // Per-tab state (not affected by global filters)
  let txState    = { offset: 0, limit: 20, is_fraud: null, sortBy: "processed_at", sortDir: "desc" };
  let usersState = { offset: 0, limit: 20, sortBy: "fraud_count", sortDir: "desc" };

  // ── DOM helpers ────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);
  const qsa = sel => document.querySelectorAll(sel);

  // ── Tooltip ────────────────────────────────────────────────────────────────
  const TIP = {
    el: null,
    show(e, html) {
      if (!this.el) this.el = $("tip");
      this.el.innerHTML = html;
      this.el.hidden = false;
      this._pos(e);
    },
    move(e) { if (this.el && !this.el.hidden) this._pos(e); },
    hide()   { if (this.el) this.el.hidden = true; },
    _pos(e) {
      const el = this.el;
      const W = window.innerWidth;
      // Temporarily make visible to measure
      el.style.visibility = "hidden"; el.hidden = false;
      const w = el.offsetWidth, h = el.offsetHeight;
      el.style.visibility = "";
      let x = e.clientX - w / 2;
      let y = e.clientY - h - 10;
      if (y < 6)      y = e.clientY + 18; // flip below cursor
      if (x < 6)      x = 6;
      if (x + w > W)  x = W - w - 6;
      el.style.left = x + "px";
      el.style.top  = y + "px";
    },
  };

  // ── Sortable table headers ─────────────────────────────────────────────────
  function initSortHeaders(tableId, state, loadFn) {
    document.querySelectorAll(`#${tableId} th[data-sort]`).forEach(th => {
      th.addEventListener("click", () => {
        const col = th.dataset.sort;
        if (state.sortBy === col) {
          state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
        } else {
          state.sortBy  = col;
          state.sortDir = "desc";
        }
        state.offset = 0;
        updateSortHeaders(tableId, state);
        loadFn();
      });
    });
    updateSortHeaders(tableId, state);
  }

  function updateSortHeaders(tableId, state) {
    document.querySelectorAll(`#${tableId} th[data-sort]`).forEach(th => {
      const col  = th.dataset.sort;
      const icon = th.querySelector(".sort-icon");
      const active = col === state.sortBy;
      th.classList.toggle("sort-active", active);
      if (icon) icon.textContent = active
        ? (state.sortDir === "asc" ? " ↑" : " ↓")
        : " ⇅";
    });
  }

  function attachSvgTooltips(svgEl) {
    if (!svgEl) return;
    svgEl.addEventListener("mousemove", e => {
      const t = e.target.closest("[data-tip]");
      if (t) TIP.show(e, t.dataset.tip);
      else   TIP.hide();
    });
    svgEl.addEventListener("mouseleave", () => TIP.hide());
  }

  function esc(s) {
    return String(s == null ? "—" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmt(n)  { return n != null ? Number(n).toLocaleString("es-AR") : "—"; }
  function txFraudScore(tx) {
    if (!tx) return null;
    const raw = tx.fraud_score ?? tx.fraudScore ?? tx.score;
    if (raw == null || raw === "") return null;
    const n = Number(raw);
    return Number.isNaN(n) ? null : n;
  }

  /** Fraud scores from the processor are 0–1; values already on 0–100 are shown as-is. */
  function fmtFraudScore(value) {
    if (value == null) return "—";
    const n = Number(value);
    if (Number.isNaN(n)) return "—";
    const pct = n <= 1 ? n * 100 : n;
    return pct.toFixed(1) + "%";
  }

  function fmtPct(n) {
    return fmtFraudScore(n);
  }
  function fmtDate(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString("es-AR", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
    catch (_) { return iso; }
  }
  function fmtAmount(n, cur) {
    if (n == null) return "—";
    return new Intl.NumberFormat("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n) + (cur ? " " + cur : "");
  }
  function decisionPill(d) {
    const key = String(d || "").toLowerCase();
    const map = { allow: ["allow", "Permitida"], block: ["block", "Bloqueada"], challenge: ["challenge", "Challenge"] };
    const [cls, label] = map[key] || ["", d || "—"];
    return `<span class="pill ${cls}">${esc(label)}</span>`;
  }
  function traceCell(tx) {
    const traceId = textOrEmpty(tx?.trace_id);
    if (!traceId) return '<span class="mono">—</span>';
    return `<button type="button" class="trace-copy mono" data-trace="${esc(traceId)}" title="Copiar trace ID">${esc(traceId)}</button>`;
  }
  async function copyTraceId(traceId) {
    if (!traceId || !navigator.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(traceId);
    } catch (_) {
      // Clipboard failures are non-critical; the visible trace id remains selectable.
    }
  }
  function kpiCard(value, label, variant) {
    return `<div class="kpi-card${variant ? " " + variant : ""}"><div class="kpi-value">${esc(String(value))}</div><div class="kpi-label">${esc(label)}</div></div>`;
  }

  function textOrEmpty(value) {
    return value == null ? "" : String(value).trim();
  }

  function nowlessUrl() {
    const url = new URL(window.location.href);
    url.search = "";
    url.hash = "";
    return url.toString();
  }

  function base64UrlFromBytes(bytes) {
    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function randomUrlSafeString(byteLength = 64) {
    const bytes = new Uint8Array(byteLength);
    globalThis.crypto.getRandomValues(bytes);
    return base64UrlFromBytes(bytes);
  }

  async function sha256UrlSafe(value) {
    if (!hasPkceCrypto()) {
      throw new Error("Esta URL no permite iniciar sesión con Cognito. Abrí el dashboard desde la URL HTTPS.");
    }
    const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return base64UrlFromBytes(new Uint8Array(digest));
  }

  function storageGetJson(key) {
    try {
      const raw = sessionStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function storageSetJson(key, value) {
    sessionStorage.setItem(key, JSON.stringify(value));
  }

  function clearAuthStorage() {
    sessionStorage.removeItem(COGNITO_KEY);
    sessionStorage.removeItem(PKCE_KEY);
  }

  function currentRedirectUri() {
    const explicit = textOrEmpty(window.COGNITO_REDIRECT_URI || window.DASHBOARD_REDIRECT_URI);
    return explicit || nowlessUrl();
  }

  function hasPkceCrypto() {
    const webCrypto = globalThis.crypto;
    return Boolean(
      webCrypto?.getRandomValues &&
      webCrypto?.subtle?.digest &&
      typeof TextEncoder === "function"
    );
  }

  function configuredSecureRedirectUri() {
    const redirectUri = textOrEmpty(authState.config?.redirectUri);
    if (!redirectUri) return "";
    try {
      const target = new URL(redirectUri);
      if (target.protocol !== "https:") return "";
      if (target.href === nowlessUrl()) return "";
      return target.href;
    } catch (_) {
      return "";
    }
  }

  function ensureCognitoSecureContext() {
    if (!useCognito()) return true;
    const target = configuredSecureRedirectUri();
    if (target) {
      showLoadingScreen("Redirigiendo al dashboard HTTPS…");
      window.location.replace(target);
      return false;
    }
    if (hasPkceCrypto()) return true;
    showLoginScreen("Esta URL no permite iniciar sesión con Cognito. Abrí el dashboard desde la URL HTTPS.");
    return false;
  }

  function resolveCognitoConfig() {
    const source = [
      window.COGNITO_CONFIG,
      window.DASHBOARD_AUTH_CONFIG,
      window.AUTH_CONFIG,
    ].find(v => v && typeof v === "object") || {};
    const read = (...keys) => {
      for (const key of keys) {
        const camel = key.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
        for (const candidate of [key, key.toUpperCase(), camel]) {
          const fromObj = source[candidate];
          if (typeof fromObj === "string" && fromObj.trim()) return fromObj.trim();
          const fromWindow = window[candidate];
          if (typeof fromWindow === "string" && fromWindow.trim()) return fromWindow.trim();
        }
      }
      return "";
    };
    const domain = read("domain", "cognito_domain", "hosted_ui_domain", "user_pool_domain");
    const clientId = read("client_id", "clientId", "cognito_client_id");
    const logoutUri = read("logout_uri", "logoutUri", "cognito_logout_uri") || currentRedirectUri();
    const forgotPasswordUrl = read("forgot_password_url", "forgotPasswordUrl", "cognito_forgot_password_url");
    const scope = read("scope") || "openid email profile";
    const redirectUri = read("redirect_uri", "redirectUri", "cognito_redirect_uri") || currentRedirectUri();
    return {
      domain,
      clientId,
      logoutUri,
      forgotPasswordUrl,
      scope,
      redirectUri,
    };
  }

  function useCognito() {
    return Boolean(authState.config?.domain && authState.config?.clientId);
  }

  function cognitoBaseUrl() {
    const base = textOrEmpty(authState.config?.domain);
    if (!base) return "";
    if (/^https?:\/\//i.test(base)) return base.replace(/\/$/, "");
    return `https://${base.replace(/\/$/, "")}`;
  }

  function buildCognitoUrl(path, params = {}) {
    const base = cognitoBaseUrl();
    if (!base) return "";
    const url = new URL(path, base);
    Object.entries(params).forEach(([key, value]) => {
      if (value != null && String(value).length > 0) {
        url.searchParams.set(key, String(value));
      }
    });
    return url.toString();
  }

  function buildAuthHeaders(extra = {}) {
    const headers = { ...extra };
    if (authState.mode === "cognito" && authState.tokens?.id_token) {
      headers.Authorization = `Bearer ${authState.tokens.id_token}`;
    }
    if (authState.mode === "cognito" && authState.tokens?.access_token) {
      headers["X-Cognito-Access-Token"] = authState.tokens.access_token;
    }
    return headers;
  }

  async function readResponseBody(res) {
    const contentType = res.headers.get("content-type") || "";
    if (res.status === 204) return null;
    if (contentType.includes("application/json")) return res.json();
    const text = await res.text();
    try {
      return text ? JSON.parse(text) : null;
    } catch (_) {
      return text || null;
    }
  }

  const apiBase = () => (window.API_BASE || "").replace(/\/$/, "");

  async function apiFetch(path, options = {}) {
    const {
      method = "GET",
      body = null,
      headers = {},
      auth = true,
    } = options;
    const requestHeaders = {
      Accept: "application/json",
      ...headers,
    };
    if (!requestHeaders["X-Trace-Id"] && !requestHeaders["x-trace-id"]) {
      requestHeaders["X-Trace-Id"] = newTraceId();
    }
    if (auth) Object.assign(requestHeaders, buildAuthHeaders());
    const init = { method, headers: requestHeaders };
    if (body != null) {
      if (body instanceof FormData) {
        init.body = body;
      } else if (typeof body === "string") {
        init.body = body;
      } else {
        init.body = JSON.stringify(body);
        init.headers["Content-Type"] = "application/json";
      }
    }
    const res = await fetch(apiBase() + path, init);
    const payload = await readResponseBody(res).catch(() => null);
    if (!res.ok) {
      const message =
        payload?.error?.message ||
        payload?.message ||
        payload?.detail ||
        `HTTP ${res.status}`;
      const err = new Error(message);
      err.status = res.status;
      err.payload = payload;
      throw err;
    }
    return payload;
  }

  function newTraceId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    try {
      return randomUrlSafeString(16);
    } catch (_) {
      return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    }
  }

  function normalizeEnvelope(payload) {
    if (!payload) return null;
    return payload.data != null ? payload.data : payload;
  }

  function normalizeArrayPayload(payload) {
    const data = normalizeEnvelope(payload);
    if (Array.isArray(data)) return data;
    if (Array.isArray(payload?.items)) return payload.items;
    if (Array.isArray(payload?.invites)) return payload.invites;
    return [];
  }

  function isSessionExpiredError(err) {
    return Number(err?.status) === 401;
  }

  function readStoredTokens() {
    return storageGetJson(COGNITO_KEY);
  }

  function saveStoredTokens(tokens) {
    storageSetJson(COGNITO_KEY, tokens);
  }

  function clearTimers() {
    clearInterval(refreshTimer);
    clearTimeout(tokenRefreshTimer);
    refreshTimer = null;
    tokenRefreshTimer = null;
  }

  function clearAuthState() {
    clearTimers();
    authState = {
      mode: "cognito",
      config: authState.config,
      tokens: null,
      me: null,
      canManageInvites: false,
      canChangePassword: false,
      isActive: false,
      deniedReason: "",
    };
  }

  function hideAllViews() {
    $("view-login").hidden = true;
    $("view-app").hidden = true;
    const denied = $("view-denied");
    if (denied) denied.hidden = true;
  }

  function setLoginMode(mode) {
    const loading = $("auth-loading");
    const cognito = $("auth-cognito");
    if (loading) loading.hidden = mode !== "loading";
    if (cognito) cognito.hidden = mode !== "cognito";
  }

  function showLoginScreen(message = "") {
    hideAllViews();
    $("view-login").hidden = false;
    setLoginMode("cognito");
    const status = $("auth-status");
    if (status) status.hidden = true;
    setStatus("offline");
    $("login-error").textContent = message;
    $("login-error").hidden = !message;
  }

  function showLoadingScreen(message = "Verificando sesión…") {
    hideAllViews();
    $("view-login").hidden = false;
    setLoginMode("loading");
    const status = $("auth-status");
    if (status) {
      status.hidden = false;
      status.textContent = message;
    }
    $("login-error").hidden = true;
  }

  function showDeniedScreen(reason) {
    hideAllViews();
    const denied = $("view-denied");
    if (denied) denied.hidden = false;
    const status = $("auth-status");
    if (status) status.hidden = true;
    const msg = $("denied-message");
    if (msg) msg.textContent = reason || "Tu cuenta no tiene acceso al panel.";
    const details = $("denied-details");
    if (details) {
      const email = textOrEmpty(authState.me?.email || authState.me?.user_email);
      const role = textOrEmpty(authState.me?.role || authState.me?.user_role);
      details.textContent = [email && `Email: ${email}`, role && `Rol: ${role}`].filter(Boolean).join(" · ");
    }
  }

  function showAppShell() {
    hideAllViews();
    $("view-app").hidden = false;
  }

  function updatePermissionedUi() {
    const inviteButton = $("tab-btn-invites");
    const invitePane = $("tab-invites");
    const settingsButton = $("btn-settings");
    if (inviteButton) inviteButton.hidden = !authState.canManageInvites;
    if (invitePane) invitePane.hidden = !authState.canManageInvites;
    if (settingsButton) settingsButton.hidden = !(authState.isActive && authState.me);
    const current = document.querySelector(".tab-btn.active");
    if (current?.hidden) setTab("overview");
  }

  function scheduleTokenRefresh() {
    clearTimeout(tokenRefreshTimer);
    if (authState.mode !== "cognito" || !authState.tokens?.id_token) return;
    const expiresAt = Number(authState.tokens.expires_at || 0);
    if (!expiresAt) return;
    const delay = Math.max(10_000, expiresAt - Date.now() - TOKEN_REFRESH_PAD);
    tokenRefreshTimer = setTimeout(() => {
      clearAuthStorage();
      startCognitoLogin();
    }, delay);
  }

  function setAccountModalContent() {
    const modal = $("account-modal");
    if (!modal || !authState.me) return;
    $("account-email").textContent = textOrEmpty(authState.me.email || authState.me.user_email) || "—";
    $("account-role").textContent = textOrEmpty(authState.me.role || authState.me.user_role) || "—";
    $("account-status").textContent = authState.deniedReason ? "Acceso restringido" : "Activo";
    const passwordBox = $("account-password-box");
    const fallbackActions = $("account-passwordless-actions");
    if (passwordBox) passwordBox.hidden = !authState.canChangePassword;
    if (fallbackActions) fallbackActions.hidden = authState.canChangePassword;
    $("account-password-form").reset();
    $("account-password-message").textContent = "";
    if (authState.canChangePassword) {
      $("account-password-hint").textContent = "El cambio se valida contra Cognito usando tu token actual.";
    }
    modal.showModal();
  }

  function buildDeniedReason(me, fallback = "Tu cuenta no puede acceder al panel.") {
    const status = textOrEmpty(me?.status || me?.account_status || me?.access_status).toLowerCase();
    if (me?.email_verified === false || me?.is_email_verified === false) return "Debes verificar tu correo antes de ingresar.";
    if (me?.disabled === true || me?.enabled === false || status === "disabled") return "Tu acceso está deshabilitado.";
    if (me?.invited === false || me?.is_invited === false || status === "pending") return "Tu cuenta todavía no fue invitada o no fue aprobada.";
    if (me?.can_access_dashboard === false || me?.access_granted === false || me?.authorized === false || status === "unauthorized") return "No tenés permisos para ver este panel.";
    return fallback;
  }

  async function exchangeCodeForTokens(code, verifier) {
    const tokenUrl = buildCognitoUrl("/oauth2/token");
    const body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: authState.config.clientId,
      code,
      code_verifier: verifier,
      redirect_uri: authState.config.redirectUri,
    });
    const res = await fetch(tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body,
    });
    const payload = await readResponseBody(res).catch(() => null);
    if (!res.ok) {
      throw new Error(payload?.error_description || payload?.error || payload?.message || `HTTP ${res.status}`);
    }
    return {
      access_token: payload.access_token,
      id_token: payload.id_token,
      refresh_token: payload.refresh_token,
      token_type: payload.token_type,
      expires_at: Date.now() + (Number(payload.expires_in || 0) * 1000),
    };
  }

  async function loadDashboardMe() {
    const response = await apiFetch("/dashboard/me");
    return normalizeEnvelope(response) || {};
  }

  async function resumeCognitoSession() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("error")) {
      const msg = params.get("error_description") || params.get("error");
      window.history.replaceState({}, document.title, nowlessUrl());
      throw new Error(msg || "No se pudo completar el ingreso con Cognito.");
    }

    const code = params.get("code");
    if (code) {
      const storedPkce = storageGetJson(PKCE_KEY);
      if (!storedPkce?.verifier) throw new Error("Faltan datos de inicio de sesión para completar el intercambio de tokens.");
      if (params.get("state") && storedPkce.state && params.get("state") !== storedPkce.state) {
        throw new Error("El estado de autenticación no coincide.");
      }
      const tokens = await exchangeCodeForTokens(code, storedPkce.verifier);
      saveStoredTokens(tokens);
      sessionStorage.removeItem(PKCE_KEY);
      window.history.replaceState({}, document.title, nowlessUrl());
      authState.tokens = tokens;
      return tokens;
    }

    const stored = readStoredTokens();
    if (!stored?.id_token) return null;
    authState.tokens = stored;
    if (stored.expires_at && Date.now() > stored.expires_at - TOKEN_REFRESH_PAD) {
      clearAuthStorage();
      return null;
    }
    return stored;
  }

  async function verifyCognitoAccess(tokens) {
    authState.tokens = tokens;
    const me = await loadDashboardMe();
    authState.me = me;
    authState.canManageInvites = Boolean(me?.can_manage_invites);
    authState.canChangePassword = Boolean(me?.can_change_password);
    authState.isActive = !isDeniedProfile(me);
    authState.deniedReason = authState.isActive ? "" : buildDeniedReason(me);
    return { me, tokens };
  }

  function isDeniedProfile(me) {
    return Boolean(
      me?.email_verified === false ||
      me?.is_email_verified === false ||
      me?.disabled === true ||
      me?.enabled === false ||
      me?.invited === false ||
      me?.is_invited === false ||
      me?.can_access_dashboard === false ||
      me?.access_granted === false ||
      me?.authorized === false ||
      String(me?.status || me?.account_status || me?.access_status || "").toLowerCase() === "disabled" ||
      String(me?.status || me?.account_status || me?.access_status || "").toLowerCase() === "pending" ||
      String(me?.status || me?.account_status || me?.access_status || "").toLowerCase() === "unauthorized"
    );
  }

  async function bootstrapCognito() {
    authState.mode = "cognito";
    try {
      const tokens = await resumeCognitoSession();
      if (!tokens?.access_token) {
        startCognitoLogin();
        return;
      }
      const access = await verifyCognitoAccess(tokens);
      if (!authState.isActive) {
        showDeniedScreen(authState.deniedReason);
        return;
      }
      clearTimers();
      showAppShell();
      updatePermissionedUi();
      await loadFiltersDropdowns();
      setTab("overview");
      scheduleTokenRefresh();
      return access;
    } catch (err) {
      clearAuthStorage();
      if (Number(err?.status) === 403) {
        authState.deniedReason = err.message || "Tu cuenta no tiene acceso al panel.";
        showDeniedScreen(authState.deniedReason);
        return;
      }
      showLoginScreen(err.message || "No se pudo iniciar sesión.");
    }
  }

  function startCognitoLogin() {
    if (!useCognito()) {
      showLoginScreen("La configuración de Cognito no está disponible.");
      return;
    }
    if (!ensureCognitoSecureContext()) return;
    const verifier = randomUrlSafeString(64);
    const state = randomUrlSafeString(16);
    storageSetJson(PKCE_KEY, { verifier, state, created_at: Date.now() });
    sha256UrlSafe(verifier).then(challenge => {
      const url = buildCognitoUrl("/oauth2/authorize", {
        response_type: "code",
        client_id: authState.config.clientId,
        redirect_uri: authState.config.redirectUri,
        scope: authState.config.scope,
        state,
        code_challenge_method: "S256",
        code_challenge: challenge,
      });
      window.location.assign(url);
    }).catch(err => {
      showLoginScreen(err.message || "No se pudo iniciar el inicio de sesión.");
    });
  }

  function buildForgotPasswordUrl() {
    if (authState.config?.forgotPasswordUrl) return authState.config.forgotPasswordUrl;
    return buildCognitoUrl("/forgotPassword", {
      client_id: authState.config?.clientId,
      redirect_uri: authState.config?.redirectUri,
      response_type: "code",
      scope: authState.config?.scope,
    });
  }

  function buildLogoutUrl() {
    const url = buildCognitoUrl("/logout", {
      client_id: authState.config?.clientId,
      logout_uri: authState.config?.logoutUri,
    });
    return url;
  }

  function doLogout() {
    clearAuthStorage();
    clearTimers();
    authState.tokens = null;
    authState.me = null;
    authState.isActive = false;
    authState.canManageInvites = false;
    authState.canChangePassword = false;
    authState.deniedReason = "";
    if (useCognito()) {
      window.location.assign(buildLogoutUrl());
      return;
    }
    showLoginScreen("La configuración de Cognito no está disponible.");
  }

  function gfParams(extra) {
    const p = new URLSearchParams();
    if (gf.user_id) p.set("user_id", gf.user_id);
    if (gf.country) p.set("country", gf.country);
    if (gf.channel) p.set("channel", gf.channel);
    if (gf.from)    p.set("from", gf.from);
    if (gf.to)      p.set("to", gf.to);
    if (extra) Object.entries(extra).forEach(([k, v]) => v != null && p.set(k, v));
    return p;
  }

  function hasActiveFilters() {
    return Object.values(gf).some(v => v !== "");
  }

  // ── Views ──────────────────────────────────────────────────────────────────
  function showView(name) {
    hideAllViews();
    if (name === "login") {
      $("view-login").hidden = false;
      setLoginMode("cognito");
    } else if (name === "app") {
      $("view-app").hidden = false;
    } else if (name === "denied") {
      const denied = $("view-denied");
      if (denied) denied.hidden = false;
    }
  }

  function setTab(tab) {
    const allowedTab = tab === "invites" && !authState.canManageInvites ? "overview" : tab;
    qsa(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === allowedTab));
    qsa(".tab-pane").forEach(p => p.classList.toggle("active", p.id === "tab-" + allowedTab));
    clearInterval(refreshTimer);

    if (allowedTab === "overview") {
      loadOverview();
      refreshTimer = setInterval(loadOverview, AUTO_REFRESH);
    } else if (allowedTab === "transactions") {
      loadTransactions();
    } else if (allowedTab === "users") {
      loadUsers();
    } else if (allowedTab === "invites") {
      loadInvites();
    }
  }

  function setStatus(s) {
    const dot = $("status-dot");
    const text = $("status-text");
    if (dot) dot.className = "dot " + s;
    if (text) text.textContent = { online: "En línea", offline: "Sin conexión", loading: "Verificando…" }[s] || s;
  }

  function showErr(tab, msg) { const e = $("error-" + tab); if (e) { e.textContent = msg; e.hidden = false; } }
  function clearErr(tab)     { const e = $("error-" + tab); if (e) { e.textContent = ""; e.hidden = true; } }

  // ── Global filters ─────────────────────────────────────────────────────────
  async function loadFiltersDropdowns() {
    try {
      const res = await apiFetch("/filters");
      const { countries = [], channels = [] } = normalizeEnvelope(res) || {};
      populateSelect("gf-country", countries, "País: todos");
      populateSelect("gf-channel", channels,  "Canal: todos");
    } catch (_) { /* non-fatal */ }
  }

  function populateSelect(id, values, placeholder) {
    const sel = $(id);
    const current = sel.value;
    sel.innerHTML = `<option value="">${esc(placeholder)}</option>` +
      values.map(v => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(v)}</option>`).join("");
  }

  function applyGlobalFilters() {
    gf = {
      user_id: $("gf-user").value.trim(),
      country: $("gf-country").value,
      channel: $("gf-channel").value,
      from:    $("gf-from").value,
      to:      $("gf-to").value,
    };
    $("active-badge").hidden = !hasActiveFilters();
    txState.offset    = 0;
    usersState.offset = 0;
    const active = document.querySelector(".tab-btn.active");
    if (active) setTab(active.dataset.tab);
  }

  function clearGlobalFilters() {
    gf = { user_id: "", country: "", channel: "", from: "", to: "" };
    $("gf-user").value    = "";
    $("gf-country").value = "";
    $("gf-channel").value = "";
    $("gf-from").value    = "";
    $("gf-to").value      = "";
    $("active-badge").hidden = true;
    txState.offset    = 0;
    usersState.offset = 0;
    const active = document.querySelector(".tab-btn.active");
    if (active) setTab(active.dataset.tab);
  }

  // ── Overview ───────────────────────────────────────────────────────────────
  const CHART_SPECS = {
    hourly: {
      wrapId: "chart-wrap",
      buildQuery(p) {
        const q = new URLSearchParams(p);
        q.set("granularity", "hour");
        q.set("days", "1");
        return q;
      },
      render: renderHourlyChart,
    },
    weekly: {
      wrapId: "chart-weekly-wrap",
      buildQuery(p) {
        const q = new URLSearchParams(p);
        q.set("granularity", "day");
        q.set("days", "7");
        return q;
      },
      render: renderWeeklyChart,
    },
    minute: {
      wrapId: "chart-minute-wrap",
      buildQuery(p) {
        const q = new URLSearchParams(p);
        q.set("granularity", "minute");
        q.set("minutes", "60");
        return q;
      },
      render: renderMinuteChart,
    },
    second: {
      wrapId: "chart-second-wrap",
      buildQuery(p) {
        const q = new URLSearchParams(p);
        q.set("granularity", "second");
        q.set("seconds", "60");
        return q;
      },
      render: renderSecondChart,
    },
  };

  function setChartLoading(chartKey) {
    const wrap = $(CHART_SPECS[chartKey]?.wrapId);
    if (wrap) wrap.innerHTML = '<p class="empty-msg">Cargando…</p>';
  }

  function setChartError(chartKey, message) {
    const wrap = $(CHART_SPECS[chartKey]?.wrapId);
    if (wrap) wrap.innerHTML = `<p class="empty-msg">${esc(message)}</p>`;
  }

  async function reloadActivityChart(chartKey, triggerBtn) {
    const spec = CHART_SPECS[chartKey];
    if (!spec) return;

    if (triggerBtn) {
      triggerBtn.disabled = true;
      triggerBtn.classList.add("spinning");
    }
    setChartLoading(chartKey);
    clearErr("overview");

    try {
      const res = await apiFetch("/stats/timeseries?" + spec.buildQuery(gfParams()));
      spec.render(normalizeArrayPayload(res));
    } catch (e) {
      setChartError(chartKey, e.message || "No se pudo actualizar el gráfico.");
    } finally {
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.classList.remove("spinning");
      }
    }
  }

  async function loadOverview({ checkHealth = true } = {}) {
    if (checkHealth) {
      setStatus("loading");
      clearErr("overview");
      try {
        const h = normalizeEnvelope(await apiFetch("/health"));
        setStatus(h?.status === "ok" ? "online" : "offline");
      } catch (e) {
        setStatus("offline");
        showErr("overview", "No se pudo contactar la API: " + e.message);
        return;
      }
    }

    const p = gfParams();
    Object.keys(CHART_SPECS).forEach(setChartLoading);

    let stats = null;
    try {
      stats = normalizeEnvelope(await apiFetch("/stats?" + p));
      renderKpis(stats);
      renderPie(stats);
    } catch (_) {
      // Preserve existing behavior: stats failure is silent and leaves the section unchanged.
    }

    try {
      renderHourlyChart(normalizeArrayPayload(await apiFetch("/stats/timeseries?" + CHART_SPECS.hourly.buildQuery(p))));
    } catch (e) {
      setChartError("hourly", e.message || "Error al cargar.");
    }

    try {
      renderWeeklyChart(normalizeArrayPayload(await apiFetch("/stats/timeseries?" + CHART_SPECS.weekly.buildQuery(p))));
    } catch (e) {
      setChartError("weekly", e.message || "Error al cargar.");
    }

    try {
      renderMinuteChart(normalizeArrayPayload(await apiFetch("/stats/timeseries?" + CHART_SPECS.minute.buildQuery(p))));
    } catch (e) {
      setChartError("minute", e.message || "Error al cargar.");
    }

    try {
      renderSecondChart(normalizeArrayPayload(await apiFetch("/stats/timeseries?" + CHART_SPECS.second.buildQuery(p))));
    } catch (e) {
      setChartError("second", e.message || "Error al cargar.");
    }

    try {
      renderRecentFraud(normalizeArrayPayload(await apiFetch("/transactions?" + gfParams({
        is_fraud: "true",
        limit: 10,
        sort_by: "fraud_score",
        sort_order: "desc",
      }))));
    } catch (_) {
      // Preserve existing behavior: recent fraud is best-effort and silent on failure.
    }
  }

  function renderKpis(stats) {
    if (!stats) return;
    const rate = stats.fraud_rate != null ? fmtFraudScore(stats.fraud_rate) : "—";
    const avg  = fmtFraudScore(stats.avg_fraud_score);
    $("kpi-grid").innerHTML = [
      kpiCard(fmt(stats.total),   "Procesadas",     ""),
      kpiCard(fmt(stats.fraud),   "Fraudes",        stats.fraud > 0 ? "danger" : ""),
      kpiCard(rate,               "Tasa fraude",    stats.fraud_rate > 0.05 ? "danger" : ""),
      kpiCard(avg,                "Score prom. (todas)", ""),
      kpiCard(fmt(stats.allowed), "Permitidas",     "success"),
      kpiCard(fmt(stats.blocked), "Bloqueadas",     ""),
    ].join("");
  }

  // ── Pie / donut chart ──────────────────────────────────────────────────────
  function polarToCart(cx, cy, r, deg) {
    const rad = deg * Math.PI / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
  }

  function donutArc(cx, cy, outerR, innerR, startDeg, endDeg) {
    const span = endDeg - startDeg;
    if (span >= 359.9) {
      const mid = startDeg + 180;
      const a = polarToCart(cx, cy, outerR, startDeg), b = polarToCart(cx, cy, outerR, mid),
            c = polarToCart(cx, cy, outerR, endDeg),
            d = polarToCart(cx, cy, innerR, startDeg), e = polarToCart(cx, cy, innerR, mid),
            f = polarToCart(cx, cy, innerR, endDeg);
      return `M${a.x} ${a.y} A${outerR} ${outerR} 0 0 1 ${b.x} ${b.y} A${outerR} ${outerR} 0 0 1 ${c.x} ${c.y} L${f.x} ${f.y} A${innerR} ${innerR} 0 0 0 ${e.x} ${e.y} A${innerR} ${innerR} 0 0 0 ${d.x} ${d.y}Z`;
    }
    const large = span > 180 ? 1 : 0;
    const s1 = polarToCart(cx, cy, outerR, startDeg), e1 = polarToCart(cx, cy, outerR, endDeg),
          s2 = polarToCart(cx, cy, innerR, endDeg),   e2 = polarToCart(cx, cy, innerR, startDeg);
    return `M${s1.x.toFixed(2)} ${s1.y.toFixed(2)} A${outerR} ${outerR} 0 ${large} 1 ${e1.x.toFixed(2)} ${e1.y.toFixed(2)} L${s2.x.toFixed(2)} ${s2.y.toFixed(2)} A${innerR} ${innerR} 0 ${large} 0 ${e2.x.toFixed(2)} ${e2.y.toFixed(2)}Z`;
  }

  function renderPie(stats) {
    const wrap = $("pie-wrap");
    if (!stats || stats.total === 0) { wrap.innerHTML = '<p class="empty-msg">Sin datos.</p>'; return; }

    const segments = [
      { label: "Permitidas", count: stats.allowed    || 0, color: "#16a34a" },
      { label: "Bloqueadas", count: stats.blocked    || 0, color: "#dc2626" },
      { label: "Challenge",  count: stats.challenged || 0, color: "#d97706" },
    ].filter(s => s.count > 0);

    const total = segments.reduce((acc, s) => acc + s.count, 0);
    if (total === 0) { wrap.innerHTML = '<p class="empty-msg">Sin datos.</p>'; return; }

    const cx = 80, cy = 80, outerR = 68, innerR = 40;
    let paths = "", start = -90;

    segments.forEach(seg => {
      const span = (seg.count / total) * 360;
      const end  = start + span;
      const pct  = ((seg.count / total) * 100).toFixed(1);
      const tip  = `<b>${seg.label}</b><br>${fmt(seg.count)} transacciones · ${pct}%`;
      paths += `<path class="pie-seg" d="${donutArc(cx, cy, outerR, innerR, start, end)}" fill="${seg.color}" stroke="#fff" stroke-width="1.5" data-tip="${esc(tip)}"></path>`;
      start = end;
    });

    const legend = segments.map(s => {
      const pct = ((s.count / total) * 100).toFixed(1);
      return `<div class="pie-legend-item">
        <span class="pie-swatch" style="background:${s.color}"></span>
        <span class="pie-legend-label">${esc(s.label)}</span>
        <span class="pie-legend-count">${fmt(s.count)}</span>
        <span class="pie-legend-pct">${pct}%</span>
      </div>`;
    }).join("");

    wrap.innerHTML = `
      <svg id="pie-svg" viewBox="0 0 160 160" class="pie-svg">
        ${paths}
        <text x="${cx}" y="${cy - 7}" text-anchor="middle" fill="#6b7280" font-size="10" font-family="inherit">Total</text>
        <text x="${cx}" y="${cy + 11}" text-anchor="middle" fill="#111827" font-size="17" font-weight="700" font-family="inherit">${fmt(total)}</text>
      </svg>
      <div class="pie-legend">${legend}</div>`;

    attachSvgTooltips($("pie-svg"));
  }

  // ── Bar chart helpers ──────────────────────────────────────────────────────
  function _buildBarSvg(slots, labelFn, tickEvery) {
    const n = slots.length;
    const maxVal = Math.max(...slots.map(s => s.total), 1);
    const W = 560, H = 180, PL = 36, PR = 6, PT = 10, PB = 34;
    const cW = W - PL - PR, cH = H - PT - PB;
    const bW = Math.floor(cW / n), gap = Math.max(1, Math.floor(bW * 0.12));

    let grid = "", bars = "", labels = "";
    for (let i = 0; i <= 4; i++) {
      const y = PT + cH - (i / 4) * cH, val = Math.round(maxVal * i / 4);
      grid += `<line x1="${PL}" y1="${y.toFixed(1)}" x2="${W - PR}" y2="${y.toFixed(1)}" stroke="#e5e7eb" stroke-width="1"/>`;
      grid += `<text x="${PL - 4}" y="${(y + 4).toFixed(1)}" fill="#9ca3af" font-size="9" text-anchor="end" font-family="inherit">${val}</text>`;
    }
    slots.forEach((s, i) => {
      const x = PL + i * bW, by = PT + cH;
      const tH = s.total > 0 ? Math.max((s.total / maxVal) * cH, 2) : 0;
      const fH = s.fraud > 0 ? Math.max((s.fraud / maxVal) * cH, 2) : 0;
      const tip = `<b>${esc(labelFn(s))}</b><br>Total: ${s.total} · Fraudes: ${s.fraud}`;
      if (tH > 0) bars += `<rect class="bar-rect" x="${x + gap}" y="${(by - tH).toFixed(1)}" width="${bW - gap * 2}" height="${tH.toFixed(1)}" fill="#bfdbfe" rx="2" data-tip="${esc(tip)}"></rect>`;
      if (fH > 0) bars += `<rect class="bar-rect" x="${x + gap}" y="${(by - fH).toFixed(1)}" width="${bW - gap * 2}" height="${fH.toFixed(1)}" fill="#ef4444" opacity=".85" rx="2" data-tip="${esc(tip)}"></rect>`;
      if (i % tickEvery === 0) labels += `<text x="${(x + bW / 2).toFixed(1)}" y="${H - 5}" fill="#9ca3af" font-size="9" text-anchor="middle" font-family="inherit">${esc(labelFn(s, true))}</text>`;
    });

    return `${grid}${bars}${labels}`;
  }

  function _chartLegend() {
    return `<div class="chart-legend">
      <span class="legend-item"><i class="legend-swatch total"></i>Total</span>
      <span class="legend-item"><i class="legend-swatch fraud"></i>Fraudes</span>
    </div>`;
  }

  // ── Timeseries 24h ─────────────────────────────────────────────────────────
  function renderHourlyChart(data) {
    const wrap = $("chart-wrap");
    if (!data) { wrap.innerHTML = '<p class="empty-msg">Sin datos.</p>'; return; }

    const byHour = {};
    data.forEach(d => { try { byHour[new Date(d.hour).toISOString().slice(0, 13)] = d; } catch (_) {} });

    const slots = [];
    const base = new Date(); base.setMinutes(0, 0, 0);
    for (let i = 23; i >= 0; i--) {
      const h = new Date(base); h.setHours(h.getHours() - i);
      const d = byHour[h.toISOString().slice(0, 13)] || {};
      slots.push({ h, total: Number(d.total) || 0, fraud: Number(d.fraud) || 0 });
    }

    const labelFn = (s, short) => short
      ? String(s.h.getHours()).padStart(2, "0") + "h"
      : String(s.h.getHours()).padStart(2, "0") + ":00 UTC";

    const W = 560, H = 180;
    wrap.innerHTML = `
      <svg id="hourly-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">
        ${_buildBarSvg(slots, labelFn, 4)}
      </svg>${_chartLegend()}`;
    attachSvgTooltips($("hourly-svg"));
  }

  // ── Timeseries 7 días ──────────────────────────────────────────────────────
  function renderWeeklyChart(data) {
    const wrap = $("chart-weekly-wrap");
    if (!data) { wrap.innerHTML = '<p class="empty-msg">Sin datos.</p>'; return; }

    const byDay = {};
    data.forEach(d => { try { byDay[new Date(d.hour).toISOString().slice(0, 10)] = d; } catch (_) {} });

    const DAY_NAMES = ["Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"];
    const slots = [];
    const base = new Date(); base.setHours(0, 0, 0, 0);
    for (let i = 6; i >= 0; i--) {
      const d = new Date(base); d.setDate(d.getDate() - i);
      const key = d.toISOString().slice(0, 10);
      const row = byDay[key] || {};
      slots.push({ d, key, total: Number(row.total) || 0, fraud: Number(row.fraud) || 0 });
    }

    const labelFn = (s, short) => short
      ? DAY_NAMES[s.d.getDay()] + " " + s.d.getDate()
      : DAY_NAMES[s.d.getDay()] + " " + s.d.getDate() + "/" + (s.d.getMonth() + 1);

    const W = 560, H = 180;
    wrap.innerHTML = `
      <svg id="weekly-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">
        ${_buildBarSvg(slots, labelFn, 1)}
      </svg>${_chartLegend()}`;
    attachSvgTooltips($("weekly-svg"));
  }

  function renderMinuteChart(data) {
    const wrap = $("chart-minute-wrap");
    if (!wrap) return;
    if (!data) { wrap.innerHTML = '<p class="empty-msg">Sin datos.</p>'; return; }

    const byMinute = {};
    data.forEach(d => {
      try { byMinute[new Date(d.hour).toISOString().slice(0, 16)] = d; } catch (_) {}
    });

    const slots = [];
    const base = new Date();
    base.setSeconds(0, 0);
    for (let i = 59; i >= 0; i--) {
      const m = new Date(base);
      m.setMinutes(m.getMinutes() - i);
      const row = byMinute[m.toISOString().slice(0, 16)] || {};
      slots.push({ m, total: Number(row.total) || 0, fraud: Number(row.fraud) || 0 });
    }

    const labelFn = (s, short) => short
      ? String(s.m.getHours()).padStart(2, "0") + ":" + String(s.m.getMinutes()).padStart(2, "0")
      : String(s.m.getHours()).padStart(2, "0") + ":" + String(s.m.getMinutes()).padStart(2, "0");

    const W = 560, H = 180;
    wrap.innerHTML = `
      <svg id="minute-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">
        ${_buildBarSvg(slots, labelFn, 10)}
      </svg>${_chartLegend()}`;
    attachSvgTooltips($("minute-svg"));
  }

  function renderSecondChart(data) {
    const wrap = $("chart-second-wrap");
    if (!wrap) return;
    if (!data) { wrap.innerHTML = '<p class="empty-msg">Sin datos.</p>'; return; }

    const bySecond = {};
    data.forEach(d => {
      try { bySecond[new Date(d.hour).toISOString().slice(0, 19)] = d; } catch (_) {}
    });

    const slots = [];
    const base = new Date();
    for (let i = 59; i >= 0; i--) {
      const s = new Date(base);
      s.setSeconds(s.getSeconds() - i, 0);
      const row = bySecond[s.toISOString().slice(0, 19)] || {};
      slots.push({ s, total: Number(row.total) || 0, fraud: Number(row.fraud) || 0 });
    }

    const labelFn = (slot, short) => short
      ? String(slot.s.getSeconds()).padStart(2, "0") + "s"
      : String(slot.s.getHours()).padStart(2, "0") + ":" +
        String(slot.s.getMinutes()).padStart(2, "0") + ":" +
        String(slot.s.getSeconds()).padStart(2, "0");

    const W = 560, H = 180;
    wrap.innerHTML = `
      <svg id="second-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="chart-svg">
        ${_buildBarSvg(slots, labelFn, 10)}
      </svg>${_chartLegend()}`;
    attachSvgTooltips($("second-svg"));
  }

  function renderRecentFraud(rows) {
    const tbody = document.querySelector("#recent-fraud-table tbody");
    if (!tbody) return;
    if (!rows || rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty-row">Sin transacciones fraudulentas recientes.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(tx => `<tr>
      <td class="mono">${esc(tx.transaction_id)}</td>
      <td>${traceCell(tx)}</td>
      <td>${esc(tx.user_id)}</td>
      <td>${esc(fmtAmount(tx.amount, tx.currency))}</td>
      <td>${esc(tx.country)}</td>
      <td>${esc(tx.channel)}</td>
      <td>${esc(fmtFraudScore(txFraudScore(tx)))}</td>
      <td>${decisionPill(tx.decision || (tx.is_fraud ? "block" : "allow"))}</td>
      <td>${esc(fmtDate(tx.processed_at))}</td>
    </tr>`).join("");
  }

  // ── Transactions ───────────────────────────────────────────────────────────
  async function loadTransactions() {
    clearErr("transactions");
    $("tx-tbody").innerHTML = '<tr><td colspan="9" class="loading-row">Cargando…</td></tr>';
    const extra = {
      limit:      txState.limit,
      offset:     txState.offset,
      sort_by:    txState.sortBy,
      sort_order: txState.sortDir,
    };
    if (txState.is_fraud === true)  extra.is_fraud = "true";
    if (txState.is_fraud === false) extra.is_fraud = "false";

    try {
      const res = await apiFetch("/transactions?" + gfParams(extra));
      renderTxTable(normalizeArrayPayload(res), res?.meta || res?.pagination || {});
      updateSortHeaders("tx-table", txState);
    } catch (e) {
      showErr("transactions", e.message);
      $("tx-tbody").innerHTML = "";
    }
  }

  function renderTxTable(rows, meta) {
    const tbody = $("tx-tbody");
    if (!rows || rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty-row">Sin resultados para los filtros aplicados.</td></tr>';
    } else {
      tbody.innerHTML = rows.map(tx => `<tr>
        <td class="mono">${esc(tx.transaction_id)}</td>
        <td>${traceCell(tx)}</td>
        <td>${esc(tx.user_id)}</td>
        <td>${esc(fmtAmount(tx.amount, tx.currency))}</td>
        <td>${esc(tx.country)}</td>
        <td>${esc(tx.channel)}</td>
        <td>${esc(fmtFraudScore(txFraudScore(tx)))}</td>
        <td>${decisionPill(tx.decision)}</td>
        <td>${esc(fmtDate(tx.processed_at))}</td>
      </tr>`).join("");
    }
    if (meta) {
      const page = Math.floor(meta.offset / meta.limit) + 1, pages = Math.ceil(meta.total / meta.limit) || 1;
      $("tx-page-info").textContent = `Página ${page} de ${pages} · ${fmt(meta.total)} resultados`;
      $("tx-prev").disabled = meta.offset === 0;
      $("tx-next").disabled = meta.offset + meta.limit >= meta.total;
    }
  }

  // ── Users ──────────────────────────────────────────────────────────────────
  async function loadUsers() {
    clearErr("users");
    $("users-tbody").innerHTML = '<tr><td colspan="6" class="loading-row">Cargando…</td></tr>';
    try {
      const res = await apiFetch("/users?" + gfParams({
        limit:      usersState.limit,
        offset:     usersState.offset,
        sort_by:    usersState.sortBy,
        sort_order: usersState.sortDir,
      }));
      renderUsersTable(normalizeArrayPayload(res), res?.meta || res?.pagination || {});
      updateSortHeaders("users-table", usersState);
    } catch (e) {
      showErr("users", e.message);
      $("users-tbody").innerHTML = "";
    }
  }

  function renderUsersTable(rows, meta) {
    const tbody = $("users-tbody");
    if (!rows || rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-row">Sin usuarios registrados.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(u => {
      const rate = u.total_transactions > 0 ? ((u.fraud_count / u.total_transactions) * 100).toFixed(1) + "%" : "0%";
      return `<tr class="clickable" data-userid="${esc(u.user_id)}">
        <td class="mono">${esc(u.user_id)}</td>
        <td>${fmt(u.total_transactions)}</td>
        <td>${fmt(u.fraud_count)}</td>
        <td>${esc(rate)}</td>
        <td>${esc(fmtFraudScore(u.avg_fraud_score))}</td>
        <td>${esc(fmtDate(u.last_seen))}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll("tr.clickable").forEach(tr => {
      tr.addEventListener("click", () => openUserModal(tr.dataset.userid));
    });
    if (meta) {
      const page = Math.floor(meta.offset / meta.limit) + 1, pages = Math.ceil(meta.total / meta.limit) || 1;
      $("users-page-info").textContent = `Página ${page} de ${pages}`;
      $("users-prev").disabled = meta.offset === 0;
      $("users-next").disabled = meta.offset + meta.limit >= meta.total;
    }
  }

  async function loadUserBehaviorProfile(userId) {
    const res = await apiFetch("/users/" + encodeURIComponent(userId) + "/behavior");
    return normalizeEnvelope(res) || {};
  }

  function behaviorSummaryItem(label, value) {
    return `<div class="summary-item"><span class="summary-label">${esc(label)}</span><span class="summary-value">${esc(value)}</span></div>`;
  }

  function behaviorTags(values, limit = 8) {
    if (!Array.isArray(values) || values.length === 0) {
      return '<p class="behavior-empty">Sin datos.</p>';
    }
    const shown = values.slice(0, limit);
    const extra = values.length - shown.length;
    const tags = shown.map(value => `<span class="behavior-tag">${esc(value)}</span>`);
    if (extra > 0) tags.push(`<span class="behavior-tag">+${extra} más</span>`);
    return `<div class="behavior-tags">${tags.join("")}</div>`;
  }

  function renderBehaviorPanel(profile, errorMessage = "") {
    if (errorMessage) {
      return `
        <div class="behavior-panel">
          <h4>Perfil de comportamiento actual</h4>
          <p class="error-text">${esc(errorMessage)}</p>
        </div>`;
    }
    if (!profile || profile.has_profile === false) {
      return `
        <div class="behavior-panel">
          <h4>Perfil de comportamiento actual</h4>
          <p class="behavior-empty">Este usuario todavía no tiene un perfil histórico en DynamoDB.</p>
        </div>`;
    }
    return `
      <div class="behavior-panel">
        <h4>Perfil de comportamiento actual</h4>
        <div class="summary-grid">
          ${behaviorSummaryItem("Monto prom.", fmtAmount(profile.avg_amount))}
          ${behaviorSummaryItem("Desvío monto", fmtAmount(profile.std_dev_amount))}
          ${behaviorSummaryItem("Transacciones", fmt(profile.tx_count))}
          ${behaviorSummaryItem("Última hora", fmt(profile.tx_last_hour))}
          ${behaviorSummaryItem("Últimos 10 min", fmt(profile.tx_last_10min))}
          ${behaviorSummaryItem("Último país", profile.last_country || "—")}
          ${behaviorSummaryItem("Última actividad", fmtDate(profile.last_timestamp))}
        </div>
        <h4>Países típicos</h4>
        ${behaviorTags(profile.typical_countries)}
        <h4>Canales típicos</h4>
        ${behaviorTags(profile.typical_channels)}
        <h4>Destinos conocidos</h4>
        ${behaviorTags(profile.known_destinations, 10)}
      </div>`;
  }

  async function openUserModal(userId) {
    $("user-modal-title").textContent = userId;
    $("user-modal-body").innerHTML = '<p class="loading-row">Cargando…</p>';
    $("user-modal").showModal();
    try {
      const encodedUserId = encodeURIComponent(userId);
      const [res, behaviorResult] = await Promise.all([
        apiFetch("/users/" + encodedUserId),
        loadUserBehaviorProfile(userId)
          .then(profile => ({ profile }))
          .catch(error => ({ error })),
      ]);
      const u = normalizeEnvelope(res) || {};
      const behaviorError = behaviorResult.error?.message || "";
      const behaviorPanel = renderBehaviorPanel(behaviorResult.profile, behaviorError);
      const rate = u.total_transactions > 0 ? ((u.fraud_count / u.total_transactions) * 100).toFixed(1) + "%" : "0%";
      const txRows = (u.recent_transactions || []).map(tx => `<tr>
        <td class="mono">${esc(tx.transaction_id)}</td>
        <td>${traceCell(tx)}</td>
        <td>${esc(fmtAmount(tx.amount, tx.currency))}</td>
        <td>${esc(tx.country)}</td>
        <td>${esc(tx.channel)}</td>
        <td>${esc(fmtFraudScore(txFraudScore(tx)))}</td>
        <td>${decisionPill(tx.decision)}</td>
        <td>${esc(fmtDate(tx.processed_at))}</td>
      </tr>`).join("") || '<tr><td colspan="8" class="empty-row">Sin transacciones.</td></tr>';

      $("user-modal-body").innerHTML = `
        <div class="user-kpis">
          ${kpiCard(fmt(u.total_transactions), "Transacciones", "")}
          ${kpiCard(fmt(u.fraud_count), "Fraudes", u.fraud_count > 0 ? "danger" : "success")}
          ${kpiCard(rate, "Tasa fraude", u.fraud_count > 0 ? "danger" : "")}
          ${kpiCard(fmtFraudScore(u.avg_fraud_score), "Score prom.", "")}
        </div>
        ${behaviorPanel}
        <h4>Últimas 10 transacciones</h4>
        <div class="table-wrap">
          <table>
            <thead><tr><th>ID</th><th>Trace</th><th>Monto</th><th>País</th><th>Canal</th><th>Score</th><th>Decisión</th><th>Procesada</th></tr></thead>
            <tbody>${txRows}</tbody>
          </table>
        </div>`;
    } catch (e) {
      $("user-modal-body").innerHTML = `<p class="error-text">${esc(e.message)}</p>`;
    }
  }

  // ── Account / invitations ─────────────────────────────────────────────────
  function openAccountModal() {
    if (!authState.me) return;
    setAccountModalContent();
  }

  function closeAccountModal() {
    const modal = $("account-modal");
    if (modal?.open) modal.close();
  }

  async function submitPasswordChange(e) {
    e.preventDefault();
    const message = $("account-password-message");
    message.textContent = "";
    const current_password = $("current-password").value;
    const new_password = $("new-password").value;
    const confirmation = $("confirm-password").value;
    if (!current_password || !new_password || !confirmation) {
      message.textContent = "Completá todos los campos.";
      return;
    }
    if (new_password !== confirmation) {
      message.textContent = "La confirmación no coincide.";
      return;
    }
    try {
      await apiFetch("/dashboard/me/password", {
        method: "PUT",
        body: {
          current_password,
          new_password,
          new_password_confirmation: confirmation,
        },
      });
      $("account-password-form").reset();
      message.textContent = "Contraseña actualizada.";
    } catch (err) {
      message.textContent = err.message;
    }
  }

  async function loadInvites() {
    if (!authState.canManageInvites) return;
    clearErr("invites");
    const tbody = $("invites-tbody");
    if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="loading-row">Cargando…</td></tr>';
    try {
      const res = await apiFetch("/dashboard/invites");
      renderInvitesTable(normalizeArrayPayload(res));
    } catch (err) {
      showErr("invites", err.message);
      if (tbody) tbody.innerHTML = "";
    }
  }

  function isInviteDisabled(row) {
    return Boolean(
      row.disabled ||
      row.is_disabled ||
      row.revoked ||
      row.archived ||
      String(row.status || "").toLowerCase() === "disabled"
    );
  }

  function setInviteListMessage(text, kind = "") {
    const el = $("invite-list-message");
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("success", "error");
    if (kind) el.classList.add(kind);
    el.hidden = !text;
  }

  function renderInviteActionCell(row) {
    const id = textOrEmpty(row.id);
    const email = textOrEmpty(row.email || row.user_email || row.invited_email);
    if (row.is_bootstrap_admin) {
      return '<span class="invite-action-note">Protegida</span>';
    }
    if (isInviteDisabled(row)) {
      return '<span class="invite-action-note">Ya deshabilitada</span>';
    }
    if (!id) {
      return '<span class="invite-action-note">—</span>';
    }
    return `<button type="button" class="btn-ghost btn-mini invite-disable" data-id="${esc(id)}" data-email="${esc(email)}">Deshabilitar</button>`;
  }

  function renderInvitesTable(rows) {
    const tbody = $("invites-tbody");
    if (!tbody) return;
    if (!rows || rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-row">No hay invitaciones.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(row => {
      const email = textOrEmpty(row.email || row.user_email || row.invited_email);
      const displayName = textOrEmpty(row.display_name || row.name);
      const isBootstrapAdmin = Boolean(row.is_bootstrap_admin);
      const isDisabled = isInviteDisabled(row);
      const rawStatus = String(row.status || "").toLowerCase();
      const status = isBootstrapAdmin
        ? "Bootstrap admin"
        : isDisabled
          ? "Deshabilitada"
          : rawStatus === "pending"
            ? "Pendiente"
            : rawStatus === "active"
              ? "Activa"
              : (row.status || "Activa");
      return `<tr>
        <td>${esc(email)}</td>
        <td>${esc(displayName || "—")}</td>
        <td>${esc(status)}</td>
        <td style="text-align:right">${renderInviteActionCell(row)}</td>
      </tr>`;
    }).join("");
    tbody.querySelectorAll(".invite-disable").forEach(btn => {
      btn.addEventListener("click", () => disableInvite(btn.dataset.id, btn.dataset.email));
    });
  }

  async function createInvite(e) {
    e.preventDefault();
    const message = $("invite-message");
    message.textContent = "";
    const email = $("invite-email").value.trim();
    const display_name = $("invite-display-name").value.trim();
    if (!email) {
      message.textContent = "Ingresá un email.";
      return;
    }
    try {
      await apiFetch("/dashboard/invites", {
        method: "POST",
        body: {
          email,
          display_name: display_name || null,
        },
      });
      $("invite-form").reset();
      message.textContent = "Invitación creada.";
      await loadInvites();
    } catch (err) {
      message.textContent = err.message;
    }
  }

  async function disableInvite(id, email) {
    if (!id) {
      showErr("invites", "No se encontró el id de la invitación. Recargá la página.");
      return;
    }
    if (!window.confirm(`Deshabilitar invitación para ${email || id}?`)) return;
    clearErr("invites");
    setInviteListMessage("");
    try {
      await apiFetch(`/dashboard/invites/${encodeURIComponent(id)}`, { method: "DELETE" });
      setInviteListMessage(
        `La invitación de ${email || "ese usuario"} fue deshabilitada.`,
        "success",
      );
      await loadInvites();
    } catch (err) {
      showErr("invites", err.message || "No se pudo deshabilitar la invitación.");
    }
  }

  // ── Bootstrap ──────────────────────────────────────────────────────────────
  function init() {
    authState.config = resolveCognitoConfig();
    authState.mode = "cognito";
    if (useCognito() && !ensureCognitoSecureContext()) return;
    setLoginMode("cognito");

    const cognitoLoginButton = $("btn-cognito-login");
    if (cognitoLoginButton) cognitoLoginButton.addEventListener("click", startCognitoLogin);
    const forgotPasswordLink = $("forgot-password-link");
    if (forgotPasswordLink) {
      forgotPasswordLink.addEventListener("click", e => {
        if (!useCognito()) return;
        e.preventDefault();
        window.location.assign(buildForgotPasswordUrl());
      });
    }

    $("btn-logout").addEventListener("click", doLogout);
    const deniedLogout = $("btn-denied-logout");
    if (deniedLogout) deniedLogout.addEventListener("click", doLogout);
    const accountLogout = $("account-logout");
    if (accountLogout) accountLogout.addEventListener("click", doLogout);
    const accountLogoutFallback = $("account-logout-fallback");
    if (accountLogoutFallback) accountLogoutFallback.addEventListener("click", doLogout);
    const settingsButton = $("btn-settings");
    if (settingsButton) settingsButton.addEventListener("click", openAccountModal);
    document.addEventListener("click", e => {
      const button = e.target.closest(".trace-copy");
      if (!button) return;
      e.preventDefault();
      e.stopPropagation();
      copyTraceId(button.dataset.trace || "");
    });
    $("btn-refresh").addEventListener("click", () => {
      const active = document.querySelector(".tab-btn.active");
      if (active) setTab(active.dataset.tab);
    });

    qsa(".chart-reload").forEach(btn => {
      btn.addEventListener("click", () => reloadActivityChart(btn.dataset.chart, btn));
    });

    // Tabs
    qsa(".tab-btn").forEach(b => b.addEventListener("click", () => setTab(b.dataset.tab)));

    // Sortable headers (initialized once; re-fired on each load via updateSortHeaders)
    initSortHeaders("tx-table",    txState,    loadTransactions);
    initSortHeaders("users-table", usersState, loadUsers);

    // Global filters
    $("gf-apply").addEventListener("click", applyGlobalFilters);
    $("gf-clear").addEventListener("click", clearGlobalFilters);
    [$("gf-user"), $("gf-from"), $("gf-to")].forEach(el => {
      el.addEventListener("keydown", e => { if (e.key === "Enter") applyGlobalFilters(); });
    });

    // Transactions per-tab filters
    const txFilters = { "tx-filter-all": null, "tx-filter-fraud": true, "tx-filter-ok": false };
    Object.entries(txFilters).forEach(([id, val]) => {
      $(id).addEventListener("click", () => {
        txState.is_fraud = val; txState.offset = 0;
        Object.keys(txFilters).forEach(bid => $(bid).classList.toggle("active", bid === id));
        loadTransactions();
      });
    });
    $("tx-limit").addEventListener("change", e => { txState.limit = parseInt(e.target.value, 10); txState.offset = 0; loadTransactions(); });
    $("tx-prev").addEventListener("click",  () => { txState.offset = Math.max(0, txState.offset - txState.limit); loadTransactions(); });
    $("tx-next").addEventListener("click",  () => { txState.offset += txState.limit; loadTransactions(); });

    // Users pagination
    $("users-prev").addEventListener("click", () => { usersState.offset = Math.max(0, usersState.offset - usersState.limit); loadUsers(); });
    $("users-next").addEventListener("click", () => { usersState.offset += usersState.limit; loadUsers(); });

    // Account modal
    $("account-modal-close").addEventListener("click", closeAccountModal);
    $("account-modal").addEventListener("click", e => { if (e.target === e.currentTarget) closeAccountModal(); });
    $("account-password-form").addEventListener("submit", submitPasswordChange);

    // Invites
    $("invite-form").addEventListener("submit", createInvite);

    // User modal
    $("user-modal-close").addEventListener("click", () => $("user-modal").close());
    $("user-modal").addEventListener("click", e => { if (e.target === e.currentTarget) e.currentTarget.close(); });

    if (useCognito()) {
      showLoadingScreen();
      bootstrapCognito();
      return;
    }

    showLoginScreen("La configuración de Cognito no está disponible.");
  }

  document.addEventListener("DOMContentLoaded", init);
})();
