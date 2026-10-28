"use strict";
const $ = id => document.getElementById(id);
let key = "", csrf = "", authenticated = false, sessionId = "", generation = 0;
let catalog = [], state = null, working = false, cancelPending = false, timer = null, polling = false;
let recovery = {restart_gateway: false, reboot_host: false, pending: false};
let candidate = null, revision = 0, inspecting = false, saving = false, browseSequence = 0;
let listing = null, roots = [], browserTarget = null, visibleEntries = 100;
const history = {ram: [], gpu: []};
const gib = n => n == null ? "unknown" : (n / 1073741824).toFixed(1) + " GiB";
const notice = text => { $("notice").textContent = text; };
const selectedEngine = () => catalog.find(e => e.id === $("engine").value);
const selectedModel = () => selectedEngine()?.models.find(m => m.id === $("model").value);
const registrationEngine = () => catalog.find(e => e.id === $("register-engine").value);
const registrationProfile = () => registrationEngine()?.profiles?.find(p => p.id === $("register-profile").value);
function option(select, value, label, disabled = false) {
  const item = document.createElement("option");
  item.value = value; item.textContent = label; item.disabled = disabled; select.append(item);
}
function errorText(error) {
  return typeof error === "string" ? error : Array.isArray(error) ? error.map(e => e.msg || JSON.stringify(e)).join("; ") : JSON.stringify(error);
}
async function request(path, body, method = body === undefined ? "GET" : "POST", publicRequest = false) {
  const epoch = generation;
  const headers = {};
  if (!publicRequest && key) headers.Authorization = "Bearer " + key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (!publicRequest && !key && csrf && method !== "GET") headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, {method, headers, credentials: "same-origin", ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  let result;
  try { result = await response.json(); } catch { result = {detail: "Gateway returned an unreadable response. Try again."}; }
  if (!publicRequest && epoch !== generation) throw new Error("Connection changed. Sign in again if needed.");
  if (!response.ok) {
    if (!publicRequest && response.status === 401) disconnect("Session expired or access was revoked. Sign in again.");
    if (!publicRequest && response.status === 403 && !key && /csrf|session/i.test(errorText(result.detail || ""))) {
      disconnect("Session verification expired. Sign in again before retrying the action.");
    }
    throw new Error(errorText(result.detail || "HTTP " + response.status));
  }
  return result;
}
async function api(path, body, method) { return request(path, body, method); }
function disconnect(message = "Signed out. Existing inference continues.") {
  generation++; authenticated = false; key = ""; csrf = ""; sessionId = "";
  clearInterval(timer); timer = null; catalog = []; state = null; working = false; cancelPending = false;
  polling = false; recovery = {restart_gateway: false, reboot_host: false, pending: false};
  history.ram.length = 0; history.gpu.length = 0; invalidateInspection(); browseSequence++;
  for (const id of ["file-dialog", "confirm-dialog"]) if ($(id).open) $(id).close();
  $("connection").textContent = "DISCONNECTED"; $("phase").textContent = "UNKNOWN";
  $("loaded").textContent = "Nothing loaded"; $("active-settings").textContent = "Active settings unavailable";
  for (const id of ["busy", "ram", "gpu", "cpu"]) $(id).textContent = "—";
  $("activity").textContent = "No connection"; $("native").textContent = "Native occupancy unknown";
  $("processes").textContent = "No process data"; $("available").textContent = "Available —";
  $("gpu-info").textContent = "Waiting for counters"; $("gpu-memory").textContent = "Unavailable counters stay unknown.";
  $("swap").textContent = "Swap —"; $("updated").textContent = "No live data";
  for (const id of ["ram-meter", "gpu-meter"]) $(id).value = 0;
  for (const id of ["ram-chart", "gpu-chart", "sessions-list", "artifacts", "inspection-summary", "inspection-warnings", "file-entries"]) $(id).replaceChildren();
  for (const id of ["engine", "model", "register-engine", "register-profile"]) { $(id).replaceChildren(); option($(id), "", "Sign in first"); }
  for (const id of ["key", "password", "register-path", "register-id", "register-context"]) $(id).value = "";
  $("file-path").textContent = ""; $("file-status").textContent = ""; $("file-root").replaceChildren(); listing = null; browserTarget = null;
  for (const id of ["password", "key"]) { $(id).type = "password"; $("show-" + id).textContent = "Show"; $("show-" + id).setAttribute("aria-label", id === "key" ? "Show control key" : "Show password"); }
  $("sessions-panel").hidden = true; $("force").checked = false;
  capabilities(); buttons(); notice(message);
}
function buttons() {
  if (typeof updateButtons === "function") updateButtons();
  $("login-form").hidden = authenticated; $("session-bar").hidden = !authenticated;
  $("session-description").textContent = key ? "Connected with a control key · memory only" : "Signed in to this browser";
  $("sessions-toggle").disabled = !authenticated || Boolean(key);
  $("sessions-refresh").disabled = !authenticated || Boolean(key);
  $("logout").disabled = !authenticated;
  $("disconnect").disabled = !key;
  $("registration-fields").disabled = !authenticated || saving;
  $("refresh-catalog").disabled = !authenticated || working || saving;
  $("restart-gateway").disabled = !authenticated || !recovery.restart_gateway || recovery.pending;
  $("reboot-host").disabled = !authenticated || !recovery.reboot_host || recovery.pending;
  $("recovery-status").textContent = !authenticated ? "Recovery controls disabled until connected." : recovery.pending ? "Recovery scheduled. Waiting for service to return." : "Gateway restart: " + (recovery.restart_gateway ? "enabled" : "disabled") + " · Host reboot: " + (recovery.reboot_host ? "enabled" : "disabled");
  for (const id of ["select-engine", "load", "unload", "engine", "model"]) $(id).disabled = !authenticated || working || cancelPending;
  $("select-engine").disabled ||= !$("engine").value;
  $("load").disabled ||= $("engine").value !== state?.selected_engine || !$("model").value;
  $("unload").disabled = !authenticated || cancelPending || (working && !(state?.phase === "loading" && $("force").checked));
  const model = selectedModel();
  $("custom-context").disabled = !authenticated || working || cancelPending || !model?.context_limits?.adjustable;
  $("context").disabled = !authenticated || working || cancelPending || !model?.context_limits?.adjustable;
  $("reasoning").disabled = !authenticated || working || cancelPending || (model?.reasoning_modes || ["auto"]).length < 2;
  $("inspect-model").disabled = !authenticated || inspecting || saving || !registrationProfile() || !$("register-path").value;
  $("add-model").disabled = !authenticated || !candidate || saving;
  $("browse-model").disabled = !authenticated || saving || !registrationProfile();
}
function capabilities() {
  const model = selectedModel(), limits = model?.context_limits;
  const loadedSelection = model && selectedEngine()?.id === state?.selected_engine && model.id === state?.loaded_model;
  $("custom-context-field").hidden = true; $("context-error").textContent = ""; $("custom-context").removeAttribute("aria-invalid");
  $("context").replaceChildren();
  if (limits?.adjustable) {
    const validContext = value => Number.isSafeInteger(value) && value >= limits.min && value <= limits.max;
    const launchDefaults = loadedSelection ? [state.active_settings?.requested_context, state.active_settings?.context, model.context, limits.min] : [model.context, limits.min];
    const defaultContext = launchDefaults.find(validContext);
    const values = [...new Set([model.context, ...(limits.presets || [])].filter(validContext))].sort((a,b) => a-b);
    for (const value of values) option($("context"), value, value.toLocaleString() + " tokens");
    option($("context"), "custom", "Custom capacity…");
    $("context").value = values.includes(defaultContext) ? String(defaultContext) : "custom";
    $("custom-context").min = limits.min; $("custom-context").max = limits.max; $("custom-context").value = defaultContext;
    $("custom-context-field").hidden = $("context").value !== "custom";
  } else option($("context"), "", model ? `${model.context ?? "Unknown"} tokens · fixed` : "Model default");
  $("reasoning").replaceChildren();
  for (const mode of model?.reasoning_modes || ["auto"]) option($("reasoning"), mode, mode === "auto" ? "Auto" : mode === "on" ? "On" : "Off");
  const defaultReasoning = loadedSelection ? state.active_settings?.reasoning ?? model.reasoning : model?.reasoning;
  if ([...$("reasoning").options].some(item => item.value === defaultReasoning)) $("reasoning").value = defaultReasoning;
  $("settings-help").textContent = model ? `Client settings override default reasoning. ${limits?.adjustable ? `Context capacity is a launch setting (${limits.min.toLocaleString()}–${limits.max.toLocaleString()} tokens), applied when the model loads.` : "Context capacity is fixed by this model/profile."} ${model.reasoning_change === "restart" ? "Changing default reasoning requires a model reload." : model.reasoning_change === "request" ? "Default reasoning is applied to requests after loading." : "Default reasoning is fixed by this model/profile."}` : "Select a model to view its supported settings.";
  buttons();
}
function models(preferred = null) {
  const previous = preferred || $("model").value;
  $("model").replaceChildren(); option($("model"), "", "Choose model");
  for (const model of selectedEngine()?.models || []) option($("model"), model.id, model.id + " · " + (model.context ?? "unknown") + " tokens", !model.enabled);
  const available = selectedEngine()?.models || [];
  if (available.some(m => m.id === previous)) $("model").value = previous;
  else if (selectedEngine()?.id === state?.selected_engine && state?.loaded_model) $("model").value = state.loaded_model;
  capabilities();
}
async function refreshCatalog(preferredEngine = null, preferredModel = null, preserveRegistration = false) {
  catalog = (await api("/api/engines")).engines;
  const engine = preferredEngine || $("engine").value || state?.selected_engine;
  $("engine").replaceChildren(); option($("engine"), "", "Choose engine");
  for (const item of catalog) option($("engine"), item.id, item.label);
  if (catalog.some(e => e.id === engine)) $("engine").value = engine;
  models(preferredModel);
  const registration = $("register-engine").value;
  $("register-engine").replaceChildren();
  for (const item of catalog) option($("register-engine"), item.id, item.label);
  if (catalog.some(e => e.id === registration)) $("register-engine").value = registration;
  if (!preserveRegistration || !registrationProfile()) registrationProfiles();
}
function chart(id, values, value) {
  if (value == null) return;
  values.push(value); if (values.length > 60) values.shift();
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", values.map((v,i) => (i*300/59) + "," + (58-v*.54)).join(" ")); $(id).replaceChildren(line);
}
function describeGeneration(g) {
  const live = g?.live, last = g?.last, num = (v, d = 1) => Number(v).toLocaleString(undefined, {maximumFractionDigits: d});
  if (live) {
    if (live.state === "prefill") return `Prefill · waiting for first token · ${num(live.elapsed_seconds)} s`;
    return `Generating · ${live.tokens_per_second == null ? "measuring" : num(live.tokens_per_second) + " tok/s"} · ${num(live.tokens, 0)} tokens (estimate)`;
  }
  if (!last) return "No generation data since the gateway started";
  const parts = [];
  parts.push(last.generation?.tokens_per_second == null ? "Generation unknown" : `Generation ${num(last.generation.tokens_per_second)} tok/s`);
  parts.push(last.prefill?.tokens_per_second == null ? "Prefill unknown" : `Prefill ${num(last.prefill.tokens_per_second)} tok/s`);
  if (last.time_to_first_token_seconds != null) parts.push(`first token ${num(last.time_to_first_token_seconds)} s`);
  if (last.generation?.tokens != null) parts.push(`${num(last.generation.tokens, 0)} tokens`);
  if (last.draft) parts.push(`${num(last.draft.acceptance * 100, 0)}% of drafts accepted`);
  if (last.source === "estimated") parts.push("estimated");
  if (last.outcome !== "complete") parts.push(last.outcome);
  return "Last: " + parts.join(" · ");
}
let leaseId = null, breaking = false;
function showMaintenance(info) {
  const lease = info?.enabled && info.lease?.held ? info.lease : null;
  leaseId = lease?.id || null;
  $("maintenance").hidden = !lease && !info?.error;
  if (lease) {
    const until = new Date(lease.expires_at * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
    $("maintenance-text").textContent = `Model loads paused by ${lease.holder} until ${until}: ${lease.reason}`;
  } else if (info?.error) $("maintenance-text").textContent = "Maintenance lease unavailable: " + info.error;
  $("maintenance-break").hidden = !lease;
  $("maintenance-break").disabled = !lease || !authenticated || breaking;
}
$("maintenance-break").onclick = async () => {
  if (!authenticated || breaking || !leaseId) return;
  if (!await askConfirmation("Break the maintenance lease?", "The job holding it is told to stop. Model loads resume immediately and the holder cannot take this lease back.", "Break lease")) return;
  const epoch = generation; breaking = true; $("maintenance-break").disabled = true;
  try { await api("/api/maintenance", undefined, "DELETE"); if (epoch === generation) notice("Maintenance lease broken."); }
  catch (error) { if (epoch === generation) notice(error.message); }
  finally { if (epoch === generation) { breaking = false; await poll(); } }
};
async function poll() {
  if (!authenticated || polling) return;
  const epoch = generation; polling = true;
  try {
    const [s,t,r] = await Promise.all([api("/api/status"), api("/api/telemetry"), api("/api/recovery")]);
    if (epoch !== generation) return;
    state = s; recovery = r; $("connection").textContent = "CONNECTED"; $("phase").textContent = s.phase.toUpperCase();
    $("loaded").textContent = s.loaded_model ? `${s.selected_engine} / ${s.loaded_model}` : "No model loaded";
    const active = s.active_settings;
    $("active-settings").textContent = active ? `Active context: ${active.context ?? "unknown"} tokens${active.context_source ? " (" + active.context_source + ")" : ""} · Default reasoning: ${active.reasoning ?? "unknown"}. Native context readback may differ from selected capacity.` : "Active settings unavailable";
    $("busy").textContent = s.busy === true ? "Processing" : s.busy === false ? "Idle" : "Unknown";
    $("activity").textContent = s.active_requests + " active gateway request(s)";
    $("generation").textContent = describeGeneration(s.generation);
    $("native").textContent = "Native occupancy: " + (s.native_busy == null ? "unknown" : s.native_busy ? "busy" : "idle");
    $("processes").textContent = s.native_processes.map(p => "PID " + p.pid + " · start " + p.start_time).join("\n") || "No native engines detected";
    const memory = t.memory, pressure = memory.total_bytes ? memory.pressure_bytes / memory.total_bytes * 100 : 0;
    $("ram").textContent = gib(memory.pressure_bytes); $("available").textContent = gib(memory.available_bytes) + " available / " + gib(memory.total_bytes);
    $("ram-meter").value = pressure; chart("ram-chart", history.ram, pressure);
    const gpu = t.gpus.find(g => g.utilization_percent != null) || t.gpus[0];
    $("gpu").textContent = gpu?.utilization_percent == null ? "Unknown" : gpu.utilization_percent + "%";
    $("gpu-meter").value = gpu?.utilization_percent ?? 0;
    $("gpu-info").textContent = gpu ? gpu.name + " · " + (gpu.temperature_c ?? "?") + " °C" : "No readable GPU counters";
    $("gpu-memory").textContent = gpu ? "VRAM " + gib(gpu.vram_used_bytes) + " / " + gib(gpu.vram_total_bytes) + " · GTT " + gib(gpu.gtt_used_bytes) : "Unavailable";
    chart("gpu-chart", history.gpu, gpu?.utilization_percent);
    $("cpu").textContent = t.cpu_percent == null ? "Unknown" : t.cpu_percent.toFixed(0) + "%";
    $("swap").textContent = "Swap used: " + gib(t.swap_used_bytes); $("updated").textContent = "UPDATED " + new Date().toLocaleTimeString();
    showMaintenance(s.maintenance);
    if (s.error) notice(s.error);
    buttons();
  } catch (error) {
    if (epoch === generation && authenticated) { $("connection").textContent = "UNAVAILABLE"; notice(error.message + " Retrying live data."); }
  } finally { if (epoch === generation) polling = false; }
}
async function connected(session = null) {
  generation++; authenticated = true; csrf = session?.csrf_token || ""; sessionId = session?.session_id || "";
  await poll();
  if (!authenticated) return;
  await refreshCatalog(state?.selected_engine);
  clearInterval(timer); timer = setInterval(poll, 2000); buttons();
  notice(key ? "Connected. Control key stays in memory; refresh forgets it." : "Signed in. Browser access can be revoked from Browser sessions.");
}
let confirmationResolver = null;
function askConfirmation(title, description, actionLabel) {
  if (confirmationResolver) return Promise.resolve(false);
  $("confirm-cancel").disabled = false; $("confirm-accept").disabled = false;
  $("confirm-title").textContent = title; $("confirm-description").textContent = description; $("confirm-accept").textContent = actionLabel;
  return new Promise(resolve => { confirmationResolver = resolve; $("confirm-dialog").showModal(); $("confirm-cancel").focus(); });
}
function closeConfirmation(accepted) {
  const resolve = confirmationResolver; confirmationResolver = null; $("confirm-dialog").close(); resolve?.(accepted);
}
$("confirm-cancel").onclick = () => closeConfirmation(false);
$("confirm-accept").onclick = () => closeConfirmation(true);
$("confirm-dialog").addEventListener("cancel", event => { event.preventDefault(); closeConfirmation(false); });
$("confirm-dialog").addEventListener("close", () => { if (confirmationResolver) { const resolve = confirmationResolver; confirmationResolver = null; resolve(false); } });
async function action(path, body) {
  if (!authenticated) return;
  const epoch = generation, force = $("force").checked;
  const cancelLoad = working && path === "/api/unload" && force && state?.phase === "loading";
  if (cancelPending || (working && !cancelLoad)) return;
  if (force && !await askConfirmation("Interrupt current inference?", "This terminates the current engine and cancels its work. Confirm only when interruption is intended.", "Interrupt inference")) return;
  if (epoch !== generation || !authenticated) return;
  if (cancelLoad) cancelPending = true; else working = true;
  buttons(); notice("Lifecycle action in progress. Native shutdown is verified before loading.");
  try { await api(path, {...body, force}); if (epoch === generation) notice("Lifecycle action completed."); }
  catch (error) { if (epoch === generation) notice(error.message); }
  finally {
    if (epoch === generation) {
      if (cancelLoad) cancelPending = false; else working = false;
      $("force").checked = false; await poll(); buttons();
    }
  }
}
$("login-form").onsubmit = async event => {
  event.preventDefault(); if (event.isComposing || $("sign-in").disabled) return;
  const password = $("password").value; $("password").value = ""; $("auth-error").textContent = "";
  if (!password) { $("auth-error").textContent = "Enter your operator password."; $("password").setAttribute("aria-invalid", "true"); $("password").focus(); return; }
  $("sign-in").disabled = true; $("sign-in").textContent = "Signing in…";
  try {
    const session = await request("/api/auth/login", {password, remember: $("remember").checked}, "POST", true);
    key = ""; $("password").removeAttribute("aria-invalid"); await connected(session);
  } catch (error) { disconnect("Sign in to manage the gateway."); $("auth-error").textContent = error.message; $("password").setAttribute("aria-invalid", "true"); $("password").focus(); }
  finally { $("sign-in").disabled = false; $("sign-in").textContent = "Sign in"; }
};
$("key-form").onsubmit = async event => {
  event.preventDefault(); if ($("connect").disabled) return;
  const value = $("key").value; $("key").value = "";
  if (!value) { notice("Enter a control key."); $("key").focus(); return; }
  disconnect(); key = value; $("connect").disabled = true;
  try { await connected(); } catch (error) { disconnect(error.message); }
  finally { $("connect").disabled = false; }
};
function reveal(id, label) {
  const input = $(id), shown = input.type === "password"; input.type = shown ? "text" : "password";
  $("show-" + id).textContent = shown ? "Hide" : "Show"; $("show-" + id).setAttribute("aria-label", (shown ? "Hide " : "Show ") + label);
}
$("show-password").onclick = () => reveal("password", "password");
$("show-key").onclick = () => reveal("key", "control key");
$("disconnect").onclick = () => disconnect("Key forgotten. Existing inference continues.");
$("logout").onclick = async () => {
  $("logout").disabled = true;
  try { if (!key) await api("/api/auth/logout", {}); disconnect(); }
  catch (error) { notice("Sign out failed: " + error.message + " Retry to end this browser session."); }
  finally { $("logout").disabled = false; }
};
async function loadSessions() {
  $("sessions-panel").hidden = false; $("sessions-list").textContent = "Loading browser sessions…";
  try {
    const result = await api("/api/auth/sessions"); $("sessions-list").replaceChildren();
    if (!result.sessions.length) $("sessions-list").textContent = "No browser sessions. Control-key connections do not create sessions.";
    for (const session of result.sessions) {
      const row = document.createElement("div"), text = document.createElement("span"), button = document.createElement("button");
      row.className = "session-row"; text.textContent = `${session.label || "Browser"}${session.current ? " · This browser" : ""} · Last seen ${new Date(session.last_seen * 1000).toLocaleString()} · Expires ${new Date(session.expires_at * 1000).toLocaleString()}`;
      button.textContent = session.current ? "Revoke this browser" : "Revoke browser"; button.className = "secondary";
      button.onclick = async () => {
        if (!await askConfirmation("Revoke browser access?", `${session.label || "This browser"} will need to sign in again. Existing inference continues.`, "Revoke access")) return;
        button.disabled = true;
        try { await api("/api/auth/sessions/" + encodeURIComponent(session.id), undefined, "DELETE"); if (session.current || session.id === sessionId) disconnect("Browser access revoked. Sign in again to reconnect."); else { await loadSessions(); notice("Browser access revoked."); } }
        catch (error) { button.disabled = false; notice(error.message); }
      };
      row.append(text, button); $("sessions-list").append(row);
    }
  } catch (error) { if (authenticated) $("sessions-list").textContent = error.message + " Use Refresh sessions to retry."; }
}
$("sessions-toggle").onclick = () => $("sessions-panel").hidden ? loadSessions() : $("sessions-panel").hidden = true;
$("sessions-refresh").onclick = loadSessions;
$("refresh-catalog").onclick = async () => {
  const engine = $("engine").value, model = $("model").value;
  const context = $("context").value, custom = $("custom-context").value, reasoning = $("reasoning").value;
  $("refresh-catalog").disabled = true;
  try {
    await refreshCatalog(engine, model, true);
    if ($("model").value === model) {
      if ([...$("context").options].some(o => o.value === context)) $("context").value = context;
      $("custom-context").value = custom; $("custom-context-field").hidden = context !== "custom" || !selectedModel()?.context_limits?.adjustable;
      if ([...$("reasoning").options].some(o => o.value === reasoning)) $("reasoning").value = reasoning;
    }
    notice("Model catalog refreshed. Current model selection and settings are preserved when supported.");
  } catch (error) { notice(error.message); }
  finally { buttons(); }
};
$("engine").onchange = () => { models(); buttons(); };
$("model").onchange = capabilities; $("force").onchange = buttons;
$("context").onchange = () => { $("custom-context-field").hidden = $("context").value !== "custom"; $("context-error").textContent = ""; $("custom-context").removeAttribute("aria-invalid"); if ($("context").value === "custom") $("custom-context").focus(); };
$("custom-context").oninput = () => { $("context-error").textContent = ""; $("custom-context").removeAttribute("aria-invalid"); };
$("select-engine").onclick = () => { if ($("engine").value) action("/api/engine", {engine: $("engine").value}); };
$("load").onclick = () => {
  const model = selectedModel(); if (!model) return;
  const body = {model: model.id, engine: $("engine").value};
  if (model.context_limits?.adjustable) {
    const limits = model.context_limits, value = Number($("context").value === "custom" ? $("custom-context").value : $("context").value);
    if (!Number.isSafeInteger(value) || value < limits.min || value > limits.max) {
      const message = `Enter a whole number from ${limits.min.toLocaleString()} to ${limits.max.toLocaleString()} tokens.`;
      $("context-error").textContent = message; $("custom-context").setAttribute("aria-invalid", "true"); $("custom-context").focus(); notice(message); return;
    }
    body.context = value;
  }
  if ((model.reasoning_modes || ["auto"]).length > 1) body.reasoning = $("reasoning").value;
  action("/api/model", body);
};
$("unload").onclick = () => action("/api/unload", {});
async function recover(actionName) {
  if (!authenticated || recovery.pending) return;
  const epoch = generation, label = actionName === "reboot-host" ? "Reboot inference host" : "Restart gateway";
  if (!await askConfirmation(label + "?", "This interrupts all inference and may terminate engines. The configured default loads when the service returns. A frozen gateway needs its external watchdog.", label)) return;
  if (epoch !== generation) return;
  recovery.pending = true; buttons();
  try { await api("/api/recovery/" + actionName, {confirmation: actionName}); notice("Recovery scheduled. Connection will drop; live data will retry."); }
  catch (error) { if (epoch === generation) { recovery.pending = false; notice(error.message); } }
  buttons();
}
$("restart-gateway").onclick = () => recover("restart-gateway"); $("reboot-host").onclick = () => recover("reboot-host");
function invalidateInspection() {
  revision++; candidate = null; $("inspection").hidden = true; $("add-model").disabled = true;
  $("registration-status").textContent = "Inputs changed. Inspect again before adding.";
}
function registrationProfiles() {
  const previous = $("register-profile").value; $("register-profile").replaceChildren();
  for (const profile of registrationEngine()?.profiles || []) option($("register-profile"), profile.id, profile.label + " · " + profile.format);
  if (registrationEngine()?.profiles?.some(p => p.id === previous)) $("register-profile").value = previous;
  registrationFields();
}
function registrationFields() {
  invalidateInspection(); $("artifacts").replaceChildren(); $("register-path").value = "";
  const profile = registrationProfile();
  $("registration-help").textContent = profile ? `Profile: ${profile.label}. Choose the server model ${profile.format === "gguf" ? "file" : "file or model directory"}. Administrator settings define compatibility.` : "No registration profiles are configured for this engine. Ask the administrator to configure a model profile and approved roots.";
  for (const name of profile?.artifacts || []) {
    const label = document.createElement("label"), field = document.createElement("div"), input = document.createElement("input"), button = document.createElement("button");
    label.textContent = "Companion: " + name; field.className = "path-field"; input.type = "text"; input.readOnly = true; input.dataset.artifact = name;
    button.type = "button"; button.className = "secondary"; button.textContent = "Browse"; button.setAttribute("aria-label", "Browse companion " + name); button.onclick = () => openBrowser(input, true);
    field.append(input, button); label.append(field); $("artifacts").append(label);
  }
  buttons();
}
$("register-engine").onchange = registrationProfiles; $("register-profile").onchange = registrationFields;
for (const id of ["register-id", "register-context", "register-reasoning"]) $(id).addEventListener("input", () => { invalidateInspection(); buttons(); });
$("registration-form").onsubmit = async event => {
  event.preventDefault(); if (!authenticated || inspecting || saving || !registrationProfile()) return;
  const payload = {engine: $("register-engine").value, profile: $("register-profile").value, path: $("register-path").value};
  if (!payload.path) { $("registration-status").textContent = "Choose a model path before inspecting."; $("browse-model").focus(); return; }
  if ($("register-id").value.trim()) payload.model = $("register-id").value.trim();
  const context = $("register-context").value;
  if (context) {
    if (!Number.isSafeInteger(Number(context)) || Number(context) < 1) { $("registration-status").textContent = "Context capacity must be a positive whole number."; $("register-context").setAttribute("aria-invalid", "true"); $("register-context").focus(); return; }
    payload.context = Number(context);
  }
  $("register-context").removeAttribute("aria-invalid");
  if ($("register-reasoning").value) payload.reasoning = $("register-reasoning").value;
  const artifacts = {};
  for (const input of $("artifacts").querySelectorAll("input")) {
    if (!input.value) { $("registration-status").textContent = "Choose all required companion artifacts before inspecting."; input.parentElement.querySelector("button").focus(); return; }
    artifacts[input.dataset.artifact] = input.value;
  }
  if (Object.keys(artifacts).length) payload.artifacts = artifacts;
  invalidateInspection(); const version = revision, epoch = generation; inspecting = true; buttons(); $("registration-status").textContent = "Inspecting model metadata…";
  try {
    const result = await api("/api/models/inspect", payload);
    if (version !== revision || epoch !== generation) return;
    candidate = result.candidate; $("register-id").value = candidate.model;
    $("inspection-summary").textContent = JSON.stringify(result.summary, null, 2); $("inspection-warnings").replaceChildren();
    for (const warning of result.warnings || []) { const item = document.createElement("li"); item.textContent = warning; $("inspection-warnings").append(item); }
    $("inspection").hidden = false; $("registration-status").textContent = "Inspection complete. Review the metadata and warnings before adding.";
  } catch (error) { if (version === revision && epoch === generation) $("registration-status").textContent = error.message + " Correct the selection and inspect again."; }
  finally { inspecting = false; buttons(); }
};
$("add-model").onclick = async () => {
  if (!authenticated || !candidate || saving) return;
  const payload = candidate, epoch = generation; saving = true; buttons(); $("registration-status").textContent = "Adding model…";
  try {
    await api("/api/models", payload);
    if (epoch !== generation) return;
    await refreshCatalog(payload.engine, payload.model); $("inspection").hidden = true; candidate = null;
    $("registration-status").textContent = "Model added and selected. Use Load model when ready."; notice("Model added and selected. No model was loaded."); $("model").focus();
  } catch (error) { if (epoch === generation) $("registration-status").textContent = error.message + " Inspect again before retrying."; candidate = null; }
  finally { saving = false; buttons(); }
};
async function openBrowser(target, companion = false) {
  for (const id of ["file-close", "file-root", "file-more"]) $(id).disabled = false;
  $("file-filter").disabled = true; $("file-clear").disabled = true;
  browserTarget = target; listing = null; $("file-root").replaceChildren(); $("file-entries").replaceChildren(); $("file-path").textContent = ""; $("file-filter").value = ""; $("file-clear").hidden = true;
  $("file-status").textContent = "Loading approved model roots…"; $("choose-directory").hidden = !companion && registrationProfile()?.format === "gguf";
  $("file-up").disabled = true; $("choose-directory").disabled = true; $("file-more").hidden = true; $("file-dialog").showModal();
  const sequence = ++browseSequence;
  try {
    roots = (await api("/api/model-files")).roots;
    if (sequence !== browseSequence || !$("file-dialog").open) return;
    for (const root of roots) option($("file-root"), root.id, root.label);
    if (!roots.length) { $("file-status").textContent = "No approved model roots. Ask the administrator to configure model roots on the server."; return; }
    await browse("");
  } catch (error) { if (sequence === browseSequence) $("file-status").textContent = error.message + " Close and reopen the browser to retry."; }
}
async function browse(path) {
  const sequence = ++browseSequence, root = $("file-root").value;
  listing = null; $("file-filter").disabled = true; $("file-clear").disabled = true; $("file-status").textContent = "Loading folder…"; $("file-entries").replaceChildren(); $("choose-directory").disabled = true; $("file-up").disabled = true; $("file-more").hidden = true;
  $("file-filter").value = ""; $("file-clear").hidden = true;
  try {
    const result = await api("/api/model-files?root=" + encodeURIComponent(root) + "&path=" + encodeURIComponent(path));
    if (sequence !== browseSequence || !$("file-dialog").open) return;
    listing = result; visibleEntries = 100; $("file-path").textContent = result.absolute_path;
    $("file-up").disabled = result.parent == null; $("choose-directory").disabled = false; renderEntries();
  } catch (error) { if (sequence === browseSequence) $("file-status").textContent = error.message + " Choose another root or close and retry."; }
}
function choosePath(absolute) {
  if (!browserTarget || !authenticated) return;
  browserTarget.value = absolute; invalidateInspection(); buttons(); $("file-dialog").close();
  browserTarget.parentElement.querySelector("button")?.focus();
}
function renderEntries() {
  if (!listing) return;
  $("file-filter").disabled = false; $("file-clear").disabled = false;
  const filter = $("file-filter").value.toLocaleLowerCase(), entries = listing.entries.filter(e => e.name.toLocaleLowerCase().includes(filter));
  $("file-clear").hidden = !filter; $("file-entries").replaceChildren();
  for (const entry of entries.slice(0, visibleEntries)) {
    const row = document.createElement("div"), button = document.createElement("button"); row.className = "file-row"; button.className = "secondary";
    const absolute = listing.absolute_path.replace(/\/$/, "") + "/" + entry.name;
    if (entry.kind === "directory") { button.textContent = entry.name + " /"; button.setAttribute("aria-label", "Open folder " + entry.name); button.onclick = () => browse(entry.path); }
    else { button.textContent = entry.name; button.setAttribute("aria-label", "Choose " + entry.name); button.onclick = () => choosePath(absolute); }
    row.append(button); $("file-entries").append(row);
  }
  $("file-status").textContent = entries.length ? `Showing ${Math.min(entries.length, visibleEntries)} of ${entries.length} entries in this folder.` : filter ? "No matching entries in this folder. Clear the filter to see all entries." : "This folder is empty. Go up or choose another root.";
  $("file-more").hidden = entries.length <= visibleEntries;
}
$("browse-model").onclick = () => openBrowser($("register-path"));
$("file-root").onchange = () => browse(""); $("file-up").onclick = () => { if (listing?.parent != null) browse(listing.parent); };
$("file-close").onclick = () => $("file-dialog").close(); $("file-dialog").addEventListener("close", () => { browseSequence++; });
$("choose-directory").onclick = () => { if (listing) choosePath(listing.absolute_path); };
$("file-filter").oninput = () => { visibleEntries = 100; renderEntries(); };
$("file-clear").onclick = () => { $("file-filter").value = ""; visibleEntries = 100; renderEntries(); $("file-filter").focus(); };
$("file-more").onclick = () => { visibleEntries += 100; renderEntries(); };
$("endpoint").textContent = location.origin + "/v1";
async function initialize() {
  const epoch = generation;
  for (const id of ["show-password", "show-key", "connect"]) $(id).disabled = false;
  buttons();
  try {
    const config = await request("/api/auth/config", undefined, "GET", true);
    if (epoch !== generation) return;
    const passwordAvailable = config.enabled && config.configured && (location.protocol === "https:" || config.http_allowed);
    $("sign-in").disabled = !passwordAvailable;
    $("auth-help").textContent = !config.enabled ? "Password access is disabled. Use an administrator control key below." : !config.configured ? "An administrator must set an operator password before password access is available." : !passwordAvailable ? "Password access requires HTTPS or administrator-approved LAN HTTP sessions." : "Use your operator password. Remember only on a trusted browser.";
    if (config.enabled) {
      try { const session = await request("/api/auth/session", undefined, "GET", true); if (epoch === generation) await connected(session); }
      catch (error) { if (authenticated && epoch + 1 === generation) disconnect("Session data could not be restored: " + error.message + " Sign in to retry."); }
    }
  } catch (error) { notice("Gateway access settings unavailable: " + error.message + " Refresh to retry."); }
}
initialize();
