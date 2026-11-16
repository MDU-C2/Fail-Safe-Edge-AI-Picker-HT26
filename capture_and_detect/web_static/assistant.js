/* Prepare real controller previews automatically; only an explicit confirmation can execute them. */
(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  if (!byId("assistant-form")) return;
  const ids = ["state", "context-label", "source", "context-note", "error", "messages", "empty", "form", "input", "send", "settings", "settings-form", "api-key", "remember", "remember-option", "tier", "reasoning", "save", "test", "forget", "settings-status", "settings-access", "key-state", "phone-note", "phone-setup", "phone-link", "phone-code-row", "phone-code", "phone-unavailable"];
  const ui = Object.fromEntries(ids.map((id) => [id, byId(`assistant-${id}`)]));
  let status = null, busy = "", connected = false, statusSequence = 0, settingsDirty = false;
  let active = null, controllerState = null, controllerSeenAt = 0, controllerOnline = false, watchTimer = null, localStop = false, sawStop = false, askGeneration = 0, capabilityContext = null;
  const retiredRequests = new Set();
  const previewButtons = new Set();
  const string = (value) => typeof value === "string" ? value : "";
  const message = (value, fallback) => string(value?.detail) || string(value?.error) || string(value?.message) || fallback;
  const setText = (node, value) => { node.textContent = String(value ?? ""); };
  const cleanError = (error) => (string(error?.message) || "The assistant request could not finish.")
    .replace(/sk-[A-Za-z0-9_-]+/g, "[key hidden]");
  const showError = (value = "") => { setText(ui.error, value); ui.error.hidden = !value; };
  const deviceAccess = (state) => !state?.devices || (state.assistant_available === true && state.devices.active_id === "mac" && !state.devices.switching && state.ready === true && typeof state.device_epoch === "string" && Boolean(state.device_epoch));
  const localDeviceLabel = (state) => {
    const entry = Array.isArray(state?.devices?.entries) ? state.devices.entries.find((item) => item?.id === "mac") : null;
    return string(entry?.label).trim() || "the local computer";
  };
  const assistantAvailable = () => controllerState?.devices ? deviceAccess(controllerState) && controllerOnline : status?.available !== false;
  const canAsk = () => connected && assistantAvailable() && !busy && !active && !controllerState?.busy;
  const rememberSupported = () => status?.remember_supported !== false;
  const canEditSettings = () => connected && status?.settings_editable === true && !busy && !active;

  function invalidateDrafts() {
    for (const button of previewButtons) {
      if (button.dataset.used === "true") continue;
      button.dataset.used = "true";
      setText(button, "Ask again");
      button.title = "This suggestion is no longer active. Send a new request.";
    }
  }

  function renderPhoneSetup() {
    const phone = status?.phone;
    let origin = "";
    try {
      const candidate = new URL(string(phone?.origin));
      if (["https:", "http:"].includes(candidate.protocol) && !candidate.username && !candidate.password
        && candidate.pathname === "/" && !candidate.search && !candidate.hash) origin = candidate.origin;
    } catch (_) { /* Missing or invalid phone origins do not become links. */ }
    ui["phone-setup"].hidden = !connected || !origin;
    setText(ui["phone-link"], origin);
    if (origin) ui["phone-link"].setAttribute("href", origin);
    else ui["phone-link"].removeAttribute("href");
    const code = connected && origin ? string(phone?.pairing_code).trim().slice(0, 80) : "";
    setText(ui["phone-code"], code);
    ui["phone-code-row"].hidden = !code;
    ui["phone-unavailable"].hidden = connected && Boolean(origin);

  }

  async function request(path, { method = "GET", payload, timeout = 12000, verifyState, bindOwner = false } = {}) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), timeout);
    try {
      const headers = {};
      if (method === "POST") {
        const stateResponse = await fetch("/api/state", { cache: "no-store", credentials: "same-origin", signal: abort.signal });
        if (!stateResponse.ok) throw new Error("Cannot verify the dashboard connection. Reconnect before trying again.");
        const state = await stateResponse.json();
        if (typeof state?.csrf_token !== "string" || !state.csrf_token) throw new Error("Dashboard session unavailable. Refresh this page.");
        if (verifyState) verifyState(state);
        if (bindOwner && state.devices) {
          if (!deviceAccess(state)) throw new Error("Activate the local computer controller before using the Assistant.");
          payload = { ...payload, _device_id: state.devices.active_id, _device_epoch: state.device_epoch };
        }
        headers["X-CSRF-Token"] = state.csrf_token;
        headers["Content-Type"] = "application/json";
      }
      const response = await fetch(path, {
        method, headers, cache: "no-store", credentials: "same-origin", signal: abort.signal,
        ...(method === "POST" ? { body: JSON.stringify(payload || {}) } : {}),
      });
      let result;
      try { result = await response.json(); } catch (_) { throw new Error("The dashboard returned an unreadable response."); }
      if (!response.ok) throw new Error(message(result, `The assistant request failed (${response.status}).`));
      return result;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("This request timed out. It was not retried; check the current workspace before trying again.");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function render() {
    const configured = status?.configured === true;
    const scriptedDemo = status?.demo === true && !configured;
    const unavailable = !assistantAvailable();
    const scanning = busy === "ask" && controllerState?.busy && ["scan", "assistant_scan"].includes(controllerState.operation);
    setText(ui.state, !connected ? "Unavailable" : unavailable ? "Local computer only" : scanning ? "Taking photo…" : busy === "ask" ? "Thinking…" : scriptedDemo ? "Offline demo" : status?.demo ? "Sample scene" : configured ? "Ready" : "Local commands");
    ui.state.dataset.tone = !connected ? "error" : status?.demo ? "demo" : "ready";
    setText(ui["key-state"], !connected ? "Service unavailable" : configured ? "Key saved" : "No key saved");
    setText(ui.source, scriptedDemo ? "Local demo · no OpenAI request" : !configured ? "Saved cup commands · local" : `GPT-6 Astra · OpenAI · ${status?.service_tier === "flex" ? "Flex" : "Standard"}${status?.demo ? " · sample scene" : ""}`);
    setText(ui["context-label"], string(status?.context_label) || (status?.demo ? "Sample workspace · no robot connected" : "Current workspace snapshot"));
    setText(ui["context-note"], unavailable ? controllerState?.devices ? `Select ${localDeviceLabel(controllerState)} and use that device to enable the Assistant. Picking controls remain available on the selected device.` : string(status?.unavailable_reason) || "Assistant features run on the local computer controller only. Open its dashboard to use them."
      : scanning ? string(controllerState.message) || "Taking a fresh workspace photo automatically…"
      : busy === "ask" ? "Checking the workspace and preparing your answer…"
      : status?.stale ? "The next scene request will take a fresh photo automatically. Cup numbers may change."
      : status?.demo ? "Explore a sample scene. Previews stay in demo mode."
      : !configured ? "Save and list cup references without a key. Add a key in Settings to find a saved cup or use other requests."
      : "Takes a fresh photo when needed. Review the real pick here, then confirm once.");
    ui.input.disabled = !connected || unavailable || Boolean(busy) || Boolean(active) || Boolean(controllerState?.busy);
    ui.send.disabled = !canAsk() || !ui.input.value.trim();
    const editable = canEditSettings();
    for (const id of ["api-key", "remember", "tier", "reasoning", "save"]) ui[id].disabled = !editable;
    ui["remember-option"].hidden = !rememberSupported();
    ui.remember.disabled = !editable || !rememberSupported();
    if (!rememberSupported()) ui.remember.checked = false;
    ui.test.disabled = !editable || !configured;
    ui.forget.disabled = !editable || !configured;
    ui["settings-access"].hidden = !connected || status?.settings_editable === true;
    for (const button of previewButtons) button.disabled = !canAsk() || button.dataset.used === "true" || status?.stale === true;
    if (active?.phase === "review") {
      active.confirm.disabled = !connected || !controllerOnline || Boolean(busy) || unavailable || localStop || Date.now() >= active.deadline;
      active.cancel.disabled = !connected || !controllerOnline || Boolean(busy);
    }
    renderPhoneSetup();
  }

  const contextKey = (state) => JSON.stringify([state?.instance_id, state?.device_epoch ?? null, state?.mode, state?.devices?.active_id ?? null]);
  const proposalKey = (proposal) => JSON.stringify(Object.fromEntries(Object.entries(proposal || {}).filter(([key]) => key !== "expires_in")));
  const reviewKey = (state) => JSON.stringify([proposalKey(state.proposal), state.motion ?? null]);
  const positiveId = (value) => Number.isSafeInteger(value) && value > 0;
  const scrollChat = () => { ui.messages.scrollTop = ui.messages.scrollHeight; };
  function requestId() {
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  function stateProblem(state, requireScan = false) {
    if (!state || typeof state.instance_id !== "string" || !["live", "dry-run", "dry_run", "demo"].includes(state.mode)) return "Controller status is unavailable. Reconnect before continuing.";
    if (!deviceAccess(state) || (!state.devices && status?.available === false)) return "Assistant actions are available on the owned local computer controller only.";
    if (localStop || state.stop_requested || state.stop_in_flight) return "Stop requested. This pick will not be confirmed. Inspect the robot before continuing.";
    if (state.recovery_required) return "Recovery inspection is required. This pick will not be confirmed.";
    if (!state.ready) return "The controller is not ready. Check its status before continuing.";
    if (requireScan && (!state.scan_usable || !state.inventory?.capture)) return "The scan is no longer usable. Scan again and ask for a new pick.";
    return "";
  }
  function proposalProblem(state, record) {
    const issue = stateProblem(state, true);
    if (issue) return issue;
    const proposal = state.proposal;
    if (!proposal || proposal.assistant_request_id !== record.id) return "This preview was cancelled or replaced. Ask again using the current scene.";
    if (contextKey(state) !== record.context) return "The controller or device changed. Ask again using the current workspace.";
    if (proposal.capture_id !== state.inventory.capture || typeof proposal.token !== "string" || !proposal.token) return "The preview no longer matches the current scan. Ask again.";
    if (!["goto", "goto_all"].includes(proposal.action)) return "This action is not a supported cup-picking preview.";
    const ids = proposal.action === "goto_all" ? proposal.cup_ids : [proposal.cup_id];
    if (!Array.isArray(ids) || !ids.length || !ids.every(positiveId) || new Set(ids).size !== ids.length) return "The controller preview has no valid cup selection.";
    if (!Array.isArray(proposal.placements) || proposal.placements.length !== ids.length || !proposal.placements.every((place) => positiveId(place?.id))) return "The controller preview has no matching placement destinations.";
    if (!Number.isFinite(proposal.expires_in) || proposal.expires_in <= 0 || (record.deadline && Date.now() >= record.deadline)) return "This preview expired. Ask again to prepare a fresh one.";
    if (record.fingerprint && reviewKey(state) !== record.fingerprint) return "The proposed cups, destination or movement settings changed. Ask again.";
    return "";
  }
  function notifyController() { window.dispatchEvent(new CustomEvent("assistant-preview-ready")); }
  function makeOperation(article, phase = "preparing") {
    const wrapper = document.createElement("section"); wrapper.className = "assistant-pick";
    const progress = document.createElement("p"); progress.className = "assistant-pick-status"; progress.setAttribute("role", "status");
    const review = document.createElement("div"); review.className = "assistant-pick-review"; review.hidden = true;
    const details = document.createElement("div"); details.className = "assistant-pick-details";
    const expiry = document.createElement("p"); expiry.className = "assistant-helper";
    const actions = document.createElement("div"); actions.className = "assistant-pick-actions";
    const confirm = document.createElement("button"); confirm.type = "button"; confirm.className = "button button-primary"; confirm.disabled = true; confirm.hidden = true;
    const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "button button-secondary"; cancel.disabled = true; setText(cancel, "Cancel");
    actions.append(confirm, cancel); review.append(details, expiry, actions); wrapper.append(progress, review); article.append(wrapper);
    const record = { article, wrapper, progress, review, details, expiry, confirm, cancel, phase, id: null, context: null, fingerprint: null, proposal: null, deadline: 0, listening: false, started: Date.now() };
    confirm.addEventListener("click", () => confirmOperation(record));
    cancel.addEventListener("click", () => cancelOperation(record));
    active = record; render(); return record;
  }
  function finishOperation(record, text, failed = false) {
    if (active !== record) return;
    record.phase = "finished"; record.confirm.disabled = true; record.cancel.disabled = true;
    record.actionsDone = true; setText(record.progress, text); record.wrapper.dataset.tone = failed ? "error" : "done";
    setText(record.expiry, "");
    if (record.id) retiredRequests.add(record.id);
    active = null; clearTimeout(watchTimer); watchTimer = null; render(); scrollChat();
  }
  function blockOperation(record, text) {
    if (active !== record) return;
    record.phase = "blocked"; record.confirm.disabled = true; record.cancel.disabled = true;
    setText(record.progress, text); setText(record.expiry, "No further confirmation will be sent from this review."); record.wrapper.dataset.tone = "error";
    render(); scheduleWatch();
  }
  function showRealPreview(record, state) {
    const proposal = state.proposal;
    record.proposal = proposal; record.fingerprint = reviewKey(state); record.deadline = Date.now() + proposal.expires_in * 1000;
    const ids = proposal.action === "goto_all" ? proposal.cup_ids : [proposal.cup_id];
    const heading = document.createElement("h4"); setText(heading, ids.length === 1 ? `Pick Cup ${ids[0]}` : `Pick ${ids.length} cups`);
    const mode = document.createElement("p"); mode.className = "assistant-pick-mode";
    setText(mode, state.mode === "live" ? "LIVE · confirmation will move the robot" : state.mode === "demo" ? "DEMO · no robot movement" : "DRY RUN · no robot movement");
    const description = document.createElement("p"); setText(description, string(proposal.description) || "Pick, hand over, place and return home.");
    const destinations = document.createElement("p"); destinations.className = "assistant-pick-destinations";
    setText(destinations, proposal.placements.map((place, index) => `Cup ${ids[index]} → P${place.id}`).join(" · "));
    record.details.append(heading, mode, description, destinations);
    const saved = proposal.saved_cup;
    if (saved) {
      const reference = document.createElement("div"); reference.className = "assistant-pick-reference";
      const url = string(saved.thumbnail_url);
      if (/^\/api\/saved-image\/[A-Za-z0-9_-]+$/.test(url)) {
        const image = document.createElement("img"); image.setAttribute("src", url); image.setAttribute("alt", `Saved reference: ${string(saved.name)}`);
        image.addEventListener("error", () => { image.hidden = true; }); reference.append(image);
      }
      const name = document.createElement("strong"); setText(name, `Saved reference: ${string(saved.name) || "Named cup"}`);
      const reason = document.createElement("p"); setText(reason, string(saved.match_reason) || "No match explanation was provided.");
      const hint = document.createElement("p"); hint.className = "assistant-helper"; setText(hint, "Compare this reference with the current cup before confirming.");
      reference.append(name, reason, hint); record.details.append(reference);
    }
    const imageUrl = string(state.inventory?.image_url);
    if (/^\/api\/image\/[A-Za-z0-9_-]+$/.test(imageUrl)) {
      const scan = document.createElement("details"); scan.className = "assistant-pick-scan"; scan.open = Boolean(saved);
      const summary = document.createElement("summary"); setText(summary, `Current scan · ${proposal.capture_id}`);
      const image = document.createElement("img"); image.setAttribute("src", imageUrl); image.setAttribute("alt", `Current scan. Review cup${ids.length === 1 ? "" : "s"} ${ids.join(", ")} before confirming.`);
      const note = document.createElement("p"); note.className = "assistant-helper"; setText(note, `Selected: cup${ids.length === 1 ? "" : "s"} ${ids.join(", ")}. Check the scene before confirming.`);
      image.addEventListener("error", () => { image.hidden = true; setText(note, "The current scan could not be displayed. Check it in Pick cups before confirming."); });
      scan.append(summary, image, note); record.details.append(scan);
    }
    const movement = document.createElement("details"); movement.className = "assistant-pick-motion";
    const summary = document.createElement("summary"); setText(summary, "Movement settings");
    const speeds = document.createElement("p"); const speed = (value) => Number.isFinite(value) ? `${value} mm/s` : "unavailable";
    setText(speeds, `Normal: ${speed(proposal.speed)} · Cup flip: ${speed(proposal.flip_speed)} · Return home: ${speed(proposal.home_speed)}.`);
    movement.append(summary, speeds);
    const phases = proposal.phase_speeds;
    if (phases && ["pick_lift", "handover", "flip", "placement"].every((key) => Number.isFinite(phases[key]))) {
      const stages = document.createElement("p");
      setText(stages, `Pick & lift: ${speed(phases.pick_lift)} · Right handover: ${speed(phases.handover)} · Cup flip: ${speed(phases.flip)} · Place & clear: ${speed(phases.placement)}.`);
      movement.append(stages);
    }
    if (Number.isFinite(state.motion?.acceleration) || Number.isFinite(state.motion?.flip_acceleration)) {
      const acceleration = document.createElement("p"); setText(acceleration, `Acceleration: ${state.motion.acceleration ?? "unavailable"}% · Cup flip: ${state.motion.flip_acceleration ?? "unavailable"}%.`); movement.append(acceleration);
    }
    if (proposal.handover) {
      const handover = document.createElement("p"); const coordinates = (point) => ["x", "y", "z"].every((axis) => Number.isFinite(point?.[axis])) ? ["x", "y", "z"].map((axis) => `${axis.toUpperCase()} ${point[axis]}`).join(", ") + " mm" : "unavailable";
      setText(handover, `Right receiving target: ${coordinates(proposal.handover.receive_mm)}. Offset: ${coordinates(proposal.handover.offset_mm)}. Approach: ${speed(proposal.handover.approach_speed)}.`); movement.append(handover);
    }
    record.details.append(movement);
    if (Array.isArray(proposal.excluded_cup_ids) && proposal.excluded_cup_ids.length) {
      const excluded = document.createElement("p"); excluded.className = "assistant-helper";
      setText(excluded, `Not included: cups ${proposal.excluded_cup_ids.filter(positiveId).join(", ")}.`); record.details.append(excluded);
    }
    setText(record.confirm, ids.length === 1 ? "Confirm pick" : `Confirm ${ids.length} cups`);
    record.confirm.hidden = false; record.review.hidden = false; scrollChat();
  }
  function updateOperation(state, { fresh = false, cached = false } = {}) {
    if (!state || typeof state.instance_id !== "string") return;
    if (contextKey(controllerState) === contextKey(state) && Number.isFinite(controllerState?.revision) && Number.isFinite(state.revision) && state.revision < controllerState.revision) {
      // A newer app poll can arrive while the post-acceptance read is in flight.
      // Process that newer snapshot without treating it as a new receipt.
      if (fresh) updateOperation(controllerState, { fresh: true, cached: true });
      return;
    }
    controllerState = state;
    if (!cached) { controllerSeenAt = Date.now(); controllerOnline = true; }
    const nextCapability = JSON.stringify([contextKey(state), state.assistant_available ?? null, state.devices?.switching ?? false, state.ready]);
    if (nextCapability !== capabilityContext) {
      if (capabilityContext !== null) { askGeneration += 1; invalidateDrafts(); }
      capabilityContext = nextCapability;
      refreshStatus();
    }
    render();
    if (state.stop_requested) sawStop = true;
    if (localStop && sawStop && !state.stop_requested && !state.recovery_required) { localStop = false; sawStop = false; }
    if (!active && !busy && !state.busy && state.proposal?.assistant_request_id && !retiredRequests.has(state.proposal.assistant_request_id)) {
      const article = appendMessage("assistant", "A cup-picking preview is waiting for your review.");
      const record = makeOperation(article); record.id = state.proposal.assistant_request_id; record.context = contextKey(state); record.listening = true;
    }
    const record = active;
    if (!record || (!record.listening && !fresh)) return;
    if (fresh) record.listening = true;
    if (!record.id) return;
    if (contextKey(state) !== record.context || !deviceAccess(state)) { finishOperation(record, "Controller or device changed. This review is no longer active.", true); return; }
    const matching = state.proposal?.assistant_request_id === record.id;
    if (record.phase === "blocked") {
      if (matching && !record.proposal && !proposalProblem(state, record)) showRealPreview(record, state);
      if (matching && !record.proposal && /^[a-f0-9]{8}$/.test(string(state.proposal.token))) { record.proposal = state.proposal; record.review.hidden = false; }
      record.cancel.disabled = !controllerOnline || state.busy || !matching || !record.proposal || state.proposal.token !== record.proposal.token;
      if (!matching && !state.busy) finishOperation(record, record.progress.textContent, true);
      scheduleWatch(); return;
    }
    if (["running", "cancelling"].includes(record.phase)) {
      if (state.busy) {
        const progress = state.progress;
        setText(record.progress, [string(state.message) || "Controller is working…", progress && Number.isFinite(progress.completed) && Number.isFinite(progress.total) ? `${progress.completed} / ${progress.total} steps` : ""].filter(Boolean).join(" · "));
      } else finishOperation(record, string(state.error) || string(state.message) || (record.phase === "cancelling" ? "Preview cancelled. No movement requested." : "Controller finished. Check the result in the workspace."), Boolean(state.error || state.recovery_required || state.stop_requested));
      scheduleWatch(); return;
    }
    const issue = stateProblem(state, record.phase === "review");
    if (issue) { blockOperation(record, issue); return; }
    if (record.phase === "review") {
      const problem = proposalProblem(state, record);
      if (problem || state.busy || state.error) { blockOperation(record, problem || string(state.error) || "The controller started another action. This review is no longer available."); return; }
    } else if (record.phase === "preparing") {
      if (state.busy) { setText(record.progress, string(state.message) || "Preparing the cup-picking preview…"); scheduleWatch(); return; }
      if (!matching) { finishOperation(record, string(state.error) || "No matching pick preview was produced. Check the current scene and ask again.", true); return; }
      const problem = proposalProblem(state, record);
      if (problem) { blockOperation(record, problem); return; }
      showRealPreview(record, state); record.phase = "review";
    }
    if (record.phase === "review") {
      record.deadline = Math.min(record.deadline || Infinity, Date.now() + state.proposal.expires_in * 1000);
      setText(record.progress, "Review the cups and destinations, then confirm once to continue.");
      const ids = record.proposal.action === "goto_all" ? record.proposal.cup_ids : [record.proposal.cup_id];
      const destinations = record.proposal.placements.map((place, index) => `Cup ${ids[index]} → P${place.id}`).join(" · ");
      const mode = state.mode === "live" ? "LIVE · moves the robot" : state.mode === "demo" ? "DEMO · no movement" : "DRY RUN · no movement";
      setText(record.expiry, `${destinations}. ${mode}. Preview expires in ${Math.max(0, Math.ceil((record.deadline - Date.now()) / 1000))}s.`);
      record.confirm.disabled = !connected || !controllerOnline || Boolean(busy) || !assistantAvailable(); record.cancel.disabled = !connected || !controllerOnline;
    }
    render(); scheduleWatch();
  }
  async function readOperation(record) {
    try { const state = await request("/api/state", { timeout: 4500 }); if (active === record) updateOperation(state, { fresh: true }); }
    catch (error) { if (active === record) blockOperation(record, `${cleanError(error)} Check the controller before trying another action.`); }
  }
  function scheduleWatch() {
    clearTimeout(watchTimer);
    if (!active) return;
    watchTimer = setTimeout(async () => {
      const record = active; if (!record) return;
      if (record.phase === "review" && Date.now() >= record.deadline) blockOperation(record, "This preview expired. Cancel it and ask for a fresh one.");
      if (record.phase === "preparing" && Date.now() - record.started > 180000) blockOperation(record, "Preparing the preview took too long. Check the controller before trying again.");
      if (Date.now() - controllerSeenAt > 1200) await readOperation(record);
      else if (active === record && record.listening) updateOperation(controllerState, { cached: true });
      scheduleWatch();
    }, 700);
  }
  async function preparePick(answer, article, expectedContext) {
    const record = makeOperation(article);
    setText(record.progress, answer.action === "pick_saved" ? "Taking a fresh scan and comparing the saved cup reference…" : "Checking the selected cups and placement destinations…");
    try {
      const response = await request("/api/assistant/prepare", { method: "POST", payload: { proposal_id: answer.proposal_id }, bindOwner: true, timeout: 30000, verifyState: (state) => {
        if (active !== record) throw new Error("This request is no longer active.");
        if (contextKey(state) !== expectedContext) throw new Error("The controller or device changed while answering. Ask again using the current workspace.");
        const issue = stateProblem(state, answer.action !== "pick_saved");
        if (issue || state.busy || state.proposal) throw new Error(issue || "Another controller action or preview is active. Finish or cancel it first.");
        record.context = contextKey(state);
      } });
      if (active !== record) return;
      if (response.accepted !== true || typeof response.assistant_request_id !== "string" || !response.assistant_request_id) throw new Error("The controller did not identify this preview request. Check its status before trying again.");
      record.id = response.assistant_request_id; notifyController(); await readOperation(record);
    } catch (error) { if (active === record) finishOperation(record, cleanError(error), true); }
    render(); scrollChat();
  }
  async function confirmOperation(record) {
    if (active !== record || record.phase !== "review" || record.confirm.disabled || !record.proposal) return;
    record.phase = "submitting"; record.listening = false; record.confirm.disabled = true; record.cancel.disabled = true;
    setText(record.progress, "Submitting your confirmation…");
    const proposal = record.proposal;
    try {
      const result = await request("/api/confirm", { method: "POST", payload: { capture_id: proposal.capture_id, cup_id: proposal.cup_id, action: proposal.action, token: proposal.token, request_id: requestId() }, bindOwner: true, verifyState: (state) => {
        const issue = proposalProblem(state, record);
        if (active !== record || issue || state.busy || state.error) throw new Error(issue || string(state.error) || "This preview is no longer ready to confirm.");
      } });
      if (active !== record) return;
      if (result.accepted !== true) throw new Error("Confirmation was not accepted. Check the controller; it was not retried.");
      record.phase = "running"; setText(record.progress, "Confirmation accepted. Waiting for controller progress…"); notifyController(); await readOperation(record);
    } catch (error) { if (active === record) { record.listening = true; blockOperation(record, `${cleanError(error)} Confirmation was not retried.`); } }
  }
  async function cancelOperation(record) {
    if (active !== record || !["review", "blocked"].includes(record.phase) || record.cancel.disabled || !record.proposal) return;
    record.phase = "cancelling"; record.listening = false; record.confirm.disabled = true; record.cancel.disabled = true;
    setText(record.progress, "Cancelling this preview…");
    try {
      const result = await request("/api/cancel", { method: "POST", payload: { request_id: requestId(), token: record.proposal.token }, bindOwner: true, verifyState: (state) => {
        if (active !== record || contextKey(state) !== record.context || state.busy || state.proposal?.assistant_request_id !== record.id || (record.proposal && state.proposal.token !== record.proposal.token)) throw new Error("This preview changed. Nothing was cancelled; refresh the current controller state.");
      } });
      if (active !== record) return;
      if (result.accepted !== true) throw new Error("Cancellation was not accepted.");
      notifyController(); await readOperation(record);
    } catch (error) { if (active === record) { record.listening = true; blockOperation(record, cleanError(error)); } }
  }

  async function refreshStatus() {
    const sequence = ++statusSequence;
    try {
      const next = await request("/api/assistant/status");
      if (sequence !== statusSequence) return;
      status = next; connected = true;
      if (next.settings_editable !== true) ui["api-key"].value = "";
      if (!settingsDirty) {
        ui.tier.value = next.service_tier === "flex" ? "flex" : "default";
        ui.reasoning.value = ["low", "medium", "high"].includes(next.reasoning) ? next.reasoning : "low";
        ui.remember.checked = next.remember_supported !== false && (next.remembered === true || next.remember === true);
      }
      if (next.error) showError(cleanError({ message: message(next, "The assistant service needs attention.") }));
      else showError();
    } catch (error) {
      if (sequence !== statusSequence) return;
      connected = false;
      showError(cleanError(error));
    }
    render();
  }

  function appendMessage(role, value) {
    ui.empty.hidden = true;
    const article = document.createElement("article");
    article.className = `assistant-message assistant-message-${role}`;
    const heading = document.createElement("h3");
    setText(heading, role === "user" ? "You" : "Assistant");
    const body = document.createElement("p"); setText(body, value);
    article.append(heading, body); ui.messages.append(article);
    article.scrollIntoView?.({ block: "nearest", behavior: "instant" });
    return article;
  }


  async function showAnswer(answer, expectedContext) {
    const responseText = string(answer.message) || "No answer was returned. Try a more specific question.";
    const article = appendMessage("assistant", responseText);
    if (answer.context_label || answer.mode || answer.camera_refreshed) {
      const evidence = document.createElement("p"); evidence.className = "assistant-message-context";
      setText(evidence, [answer.camera_refreshed ? `Fresh photo${answer.capture_id ? ` · ${string(answer.capture_id)}` : ""}` : "", string(answer.context_label), answer.mode === "scripted_demo" ? "Local demo response" : answer.mode === "saved_cup_command" ? "Saved cup command" : answer.mode === "camera_command" ? "Camera result · no AI request" : answer.mode === "gpt" ? "GPT-6 Astra response" : ""].filter(Boolean).join(" · "));
      article.append(evidence);
    }
    if (answer.action === "answer" && (answer.mode === "camera_command" || answer.camera_refreshed)) {
      const inventory = controllerState?.inventory;
      const imageUrl = string(inventory?.image_url);
      if (contextKey(controllerState) === expectedContext && string(answer.capture_id) && inventory?.capture === answer.capture_id && /^\/api\/image\/[A-Za-z0-9_-]+$/.test(imageUrl)) {
        const photo = document.createElement("details"); photo.className = "assistant-camera-result assistant-pick-scan"; photo.open = true;
        const summary = document.createElement("summary"); setText(summary, `Workspace photo · ${answer.capture_id}`);
        const image = document.createElement("img"); image.setAttribute("src", imageUrl); image.setAttribute("alt", "Fresh workspace photo with cup numbers. Review the numbers before selecting a cup.");
        const note = document.createElement("p"); note.className = "assistant-helper"; setText(note, "Cup numbers belong to this photo. Check them before asking for a numbered cup.");
        image.addEventListener("error", () => { image.hidden = true; setText(note, "The photo could not be displayed. Open Pick cups to check the current workspace."); });
        photo.append(summary, image, note); article.append(photo);
      }
    }
    if (Array.isArray(answer.groups) && answer.groups.length) {
      const list = document.createElement("ul"); list.className = "assistant-groups";
      for (const group of answer.groups) {
        const line = document.createElement("li");
        const cups = Array.isArray(group.cup_ids) ? group.cup_ids.filter((id) => Number.isInteger(id)).join(", ") : "";
        setText(line, `${string(group.label) || string(group.name) || "Group"}${cups ? `: cups ${cups}` : ""}`); list.append(line);
      }
      article.append(list);
    }
    if (typeof answer.proposal_id === "string" && answer.proposal_id && ["select", "batch", "pick_saved"].includes(answer.action)) {
      await preparePick(answer, article, expectedContext);
    } else if (typeof answer.proposal_id === "string" && answer.proposal_id && answer.action === "save_cup") {
      const button = document.createElement("button"); button.type = "button"; button.className = "button button-secondary";
      setText(button, "Save cup reference");
      previewButtons.add(button); article.append(button);
      const hint = document.createElement("p"); hint.className = "assistant-helper";
      const cupId = Array.isArray(answer.cup_ids) && answer.cup_ids.length === 1 && Number.isSafeInteger(answer.cup_ids[0]) && answer.cup_ids[0] > 0 ? answer.cup_ids[0] : null;
      const cupName = string(answer.cup_name).trim();
      setText(hint, `${cupId && cupName ? `Saves Cup ${cupId} as “${cupName}” from this scan.` : "Saves a named visual reference from this scan."} No robot movement.`);
      article.append(hint);
      button.addEventListener("click", async () => {
        if (button.disabled || !canAsk() || button.dataset.used === "true") return;
        button.dataset.used = "true"; // An uncertain submission must not be replayed.
        busy = "prepare"; showError(); render();
        try {
          await request("/api/assistant/prepare", { method: "POST", payload: { proposal_id: answer.proposal_id }, bindOwner: true, timeout: 30000, verifyState: (state) => {
            if (contextKey(state) !== expectedContext || !deviceAccess(state)) throw new Error("The controller or device changed. Ask again using the current workspace.");
          } });
          setText(button, "Save requested");
          window.dispatchEvent(new CustomEvent("assistant-preview-ready"));
        } catch (error) { setText(button, "Ask again"); showError(cleanError(error)); }
        finally { busy = ""; render(); }
      });
    }
    render();
    // Action controls are appended after the answer text; keep the complete reply in view.
    ui.messages.scrollTop = ui.messages.scrollHeight;
  }

  ui.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = ui.input.value.trim();
    if (!value || !canAsk()) return;
    const generation = ++askGeneration;
    invalidateDrafts();
    busy = "ask"; showError(); appendMessage("user", value); render();
    try {
      let expectedContext;
      const answer = await request("/api/assistant/ask", { method: "POST", payload: { text: value }, bindOwner: true, timeout: 180000, verifyState: (state) => {
        if (!deviceAccess(state) || state.busy) throw new Error("The Assistant needs an idle local computer controller. Check the selected device and try again.");
        expectedContext = contextKey(state);
      } });
      const current = await request("/api/state");
      updateOperation(current);
      if (contextKey(current) !== expectedContext || !deviceAccess(current)) askGeneration += 1;
      if (generation !== askGeneration) { appendMessage("assistant", "The workspace changed while answering. No new pick was prepared."); return; }
      ui.input.value = ""; await showAnswer(answer, expectedContext);
    } catch (error) { showError(cleanError(error)); }
    finally { busy = ""; render(); ui.input.focus({ preventScroll: true }); }
  });
  ui.input.addEventListener("input", render);
  ui.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); ui.form.requestSubmit(); }
  });
  document.querySelectorAll("[data-assistant-example]").forEach((button) => button.addEventListener("click", () => {
    if (ui.input.disabled) return;
    ui.input.value = button.dataset.assistantExample; ui.input.focus(); render();
  }));
  for (const id of ["api-key", "remember", "tier", "reasoning"]) ui[id].addEventListener("input", () => { settingsDirty = true; });
  ui["settings-form"].addEventListener("submit", async (event) => {
    event.preventDefault(); if (!canEditSettings()) return;
    invalidateDrafts();
    busy = "settings"; showError(); setText(ui["settings-status"], "Saving settings…"); render();
    const key = ui["api-key"].value.trim();
    const payload = { remember: rememberSupported() && ui.remember.checked, service_tier: ui.tier.value, reasoning: ui.reasoning.value };
    if (key) payload.api_key = key;
    try {
      await request("/api/assistant/settings", { method: "POST", payload });
      settingsDirty = false;
      setText(ui["settings-status"], ui.remember.checked ? "Settings saved on the dashboard computer." : "Settings saved for this service session.");
      await refreshStatus();
    } catch (_) { showError("Settings could not be saved. Check the dashboard connection and API key, then try again."); setText(ui["settings-status"], "Settings could not be saved. Check the connection and API key, then try again."); }
    finally { ui["api-key"].value = ""; busy = ""; render(); }
  });
  ui.test.addEventListener("click", async () => {
    if (ui.test.disabled || !canEditSettings()) return;
    busy = "test"; showError(); setText(ui["settings-status"], "Testing GPT-6 Astra access…"); render();
    try {
      const result = await request("/api/assistant/test", { method: "POST", timeout: 60000 });
      setText(ui["settings-status"], string(result.message) || "Connection test completed.");
    } catch (error) { showError(cleanError(error)); setText(ui["settings-status"], cleanError(error)); }
    finally { busy = ""; render(); }
  });
  ui.forget.addEventListener("click", async () => {
    if (ui.forget.disabled || !canEditSettings()) return;
    invalidateDrafts();
    busy = "forget"; showError(); render();
    try {
      await request("/api/assistant/forget", { method: "POST" });
      ui["api-key"].value = ""; ui.remember.checked = false; settingsDirty = false;
      setText(ui["settings-status"], "API key forgotten."); await refreshStatus();
    } catch (error) { showError(cleanError(error)); setText(ui["settings-status"], cleanError(error)); }
    finally { busy = ""; render(); }
  });
  for (const id of ["nav-assistant", "nav-settings"]) byId(id)?.addEventListener("click", () => { if (!busy) refreshStatus(); });
  window.addEventListener("assistant-refresh", () => { if (!busy) refreshStatus(); });
  window.addEventListener("assistant-state", (event) => updateOperation(event.detail));
  window.addEventListener("assistant-disconnected", () => {
    askGeneration += 1; controllerOnline = false;
    if (active) blockOperation(active, "Controller connection lost. Confirmation is blocked; check the controller after reconnecting.");
    render();
  });
  window.addEventListener("assistant-local-stop", () => {
    localStop = true; askGeneration += 1; invalidateDrafts();
    if (active) blockOperation(active, "Stop requested. No further confirmation will be sent from this review.");
  });
  render(); refreshStatus();
})();
