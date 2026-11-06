/* Local dashboard. The server owns proposals, validation, and all robot commands. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const dom = Object.fromEntries(Array.from(document.querySelectorAll("[id]"), (el) => [el.id, el]));
  let state = null;
  let connected = false;
  let posting = false;
  let selectedId = null;
  let selectedCapture = null;
  let imageCapture = null;
  let imageLoaded = false;
  let cardsKey = "";
  let eventsKey = "";
  let pollNumber = 0;
  let appliedPoll = 0;
  let proposalDeadline = 0;
  let transientError = "";

  const text = (id, value) => { const el = dom[id]; const next = value == null ? "" : String(value); if (el.textContent !== next) el.textContent = next; };
  const hide = (id, hidden) => { dom[id].hidden = hidden; };
  const cups = () => Array.isArray(state?.inventory?.cups) ? state.inventory.cups : [];
  const currentCup = () => selectedCapture === state?.inventory?.capture ? cups().find((cup) => cup.id === selectedId) : null;
  const actionable = () => connected && Boolean(state?.ready) && !state?.busy && !posting;
  const safeScan = () => Boolean(state?.inventory && state?.scan_usable && !state?.recovery_required);
  const clockText = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  };
  const asMessage = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
  const humanize = (value) => String(value || "").replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());

  function renderMode() {
    const mode = state?.mode;
    const labels = {
      demo: ["DEMO MODE", "Explore without motion.", "A sample scene and preview controller. No camera or robot connection.", "◇"],
      dry_run: ["DRY RUN", "Camera on. Motion off.", "Real detections. Preview the sequence without sending robot commands.", "◉"],
      live: ["PHYSICAL ROBOT", "Physical movement enabled.", "Confirmed actions can move both arms. Check the workspace before confirming.", "↗"],
    };
    const values = labels[mode];
    if (values) {
      ["mode-badge", "mode-title", "mode-description", "mode-icon"].forEach((id, i) => text(id, values[i]));
    }
    dom["mode-panel"].classList.toggle("live", mode === "live");
    text("mode-footer", mode === "demo" ? "Local demo · no physical movement" : "One controller · one action at a time");
  }

  function renderConnection() {
    dom.connection.classList.toggle("connected", connected);
    dom.connection.classList.toggle("disconnected", !connected && Boolean(state));
    text("connection-text", connected ? "Controller connected" : state ? "Disconnected" : "Connecting");
    hide("connection-alert", connected || !state);
    const error = transientError || asMessage(state?.error);
    text("error-banner", error);
    hide("error-banner", !error);
  }

  function chooseCup(id) {
    if (!state?.inventory || state.proposal || posting || state.busy) return;
    if (!cups().some((cup) => cup.id === id)) return;
    selectedId = id;
    selectedCapture = state.inventory.capture;
    renderSelection();
    renderControls();
  }

  function renderCups() {
    const inventory = state?.inventory;
    const list = cups();
    const key = JSON.stringify([inventory?.capture, list]);
    if (cardsKey === key) return;
    cardsKey = key;
    dom["cup-list"].replaceChildren();
    text("cup-count", list.length);
    if (!list.length) {
      const empty = document.createElement("p");
      empty.className = "empty-list";
      empty.textContent = inventory ? "No cup candidates found. Adjust the scene and scan again." : "Your detected cups will appear here.";
      dom["cup-list"].append(empty);
      return;
    }
    list.forEach((cup) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = `cup-card${cup.selectable ? "" : " blocked"}`;
      card.dataset.cupId = String(cup.id);
      card.setAttribute("aria-pressed", "false");
      const topline = document.createElement("span");
      topline.className = "cup-topline";
      const number = document.createElement("span");
      number.className = "cup-number";
      // This SVG is a fixed decorative icon, never model or detection output.
      number.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4h10l-1.5 12h-7zM14 5h2a3 3 0 0 1-.5 6h-2M4 18h10"/></svg>';
      number.append(document.createTextNode(`Cup ${cup.id}`));
      const check = document.createElement("span");
      check.className = "cup-check";
      check.textContent = "✓";
      check.setAttribute("aria-hidden", "true");
      topline.append(number, check);
      const confidence = document.createElement("span");
      confidence.className = "confidence";
      confidence.textContent = Number.isFinite(cup.confidence) ? `${Math.round(cup.confidence * 100)}% detection confidence` : "Confidence unavailable";
      const status = document.createElement("span");
      status.className = "cup-status";
      const dot = document.createElement("span");
      dot.className = "status-dot";
      const label = document.createElement("span");
      label.className = "cup-status-label";
      label.textContent = cup.selectable ? (cup.pick_available ? "Available" : "Hover only") : "Blocked";
      status.append(dot, label);
      card.append(topline, confidence, status);
      card.title = cup.blocked_reason || `Select cup ${cup.id}`;
      card.setAttribute("aria-label", `Cup ${cup.id}, ${label.textContent}${cup.blocked_reason ? `: ${cup.blocked_reason}` : ""}`);
      card.addEventListener("click", () => chooseCup(cup.id));
      dom["cup-list"].append(card);
    });
  }

  function renderInventory() {
    const inventory = state?.inventory;
    if (selectedCapture !== inventory?.capture) {
      selectedId = null;
      selectedCapture = inventory?.capture || null;
    }
    const capture = inventory?.capture || null;
    if (capture !== imageCapture) {
      imageCapture = capture;
      imageLoaded = false;
      dom["selection-overlay"].replaceChildren();
      hide("image-stage", true);
      hide("empty-camera", false);
      dom["empty-camera"].querySelector("h3").textContent = capture ? "Loading this scan…" : "A fresh view starts here.";
      dom["empty-camera"].querySelector("p").textContent = capture ? "Your numbered workspace image will appear here." : "Scan your workspace to find and number the cups.";
      if (inventory?.image_url) {
        let imageURL = null;
        try { imageURL = new URL(inventory.image_url, window.location.origin); } catch { /* Invalid image URLs are never loaded. */ }
        if (imageURL?.origin === window.location.origin && imageURL.pathname.startsWith("/api/image/")) {
          dom["camera-image"].src = imageURL.href;
          dom["camera-image"].alt = `Latest workspace scan with ${cups().length} numbered cup candidates`;
        } else {
          dom["camera-image"].removeAttribute("src");
        }
      } else {
        dom["camera-image"].removeAttribute("src");
      }
    }
    text("image-time", inventory ? clockText(inventory.saved_at) : "");
    text("scan-summary", inventory ? `${cups().length} candidate${cups().length === 1 ? "" : "s"} · ${cups().filter((cup) => cup.selectable).length} selectable` : "No scan captured yet");
    const fresh = Boolean(inventory && state.scan_usable && connected);
    text("scan-state", !inventory ? "Awaiting scan" : !connected ? "View may be stale" : fresh ? "Scan ready" : "Scan again to move");
    dom["scan-state"].classList.toggle("fresh", fresh);
    dom["scan-state"].classList.toggle("stale", Boolean(inventory && !fresh));
    text("capture-id", capture || "");
    hide("capture-details", !inventory);
    renderCups();
  }

  function renderSelection() {
    const target = state?.proposal?.cup_id ?? selectedId;
    document.querySelectorAll(".cup-card").forEach((card) => {
      const selected = Number(card.dataset.cupId) === target;
      card.classList.toggle("selected", selected);
      card.setAttribute("aria-pressed", String(selected));
    });
    const cup = currentCup();
    hide("selection-tools", !cup || Boolean(state?.proposal));
    if (cup) {
      text("selected-label", `Cup ${cup.id} selected`);
      text("selected-status", !state.scan_usable ? "New scan required" : cup.selectable ? "Review an action" : "Movement blocked");
      const reason = cup.blocked_reason || (!cup.pick_available && cup.selectable ? "Pick is unavailable for this detection. Hover may still be available." : "");
      text("selected-reason", reason);
      hide("selected-reason", !reason);
    }
    renderOverlay();
  }

  function renderOverlay() {
    const overlay = dom["selection-overlay"];
    overlay.replaceChildren();
    if (!imageLoaded) return;
    const image = dom["camera-image"];
    overlay.setAttribute("viewBox", `0 0 ${image.naturalWidth} ${image.naturalHeight}`);
    overlay.setAttribute("preserveAspectRatio", "xMidYMid meet");
    const target = state?.proposal?.cup_id ?? selectedId;
    const cup = cups().find((item) => item.id === target);
    if (!cup) return;
    const box = cup.bbox_xyxy;
    if (Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) && box[2] > box[0] && box[3] > box[1]) {
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("x", box[0] - 8);
      rect.setAttribute("y", box[1] - 8);
      rect.setAttribute("width", box[2] - box[0] + 16);
      rect.setAttribute("height", box[3] - box[1] + 16);
      rect.setAttribute("rx", "10");
      rect.classList.add("selection-box");
      overlay.append(rect);
    } else if (Number.isFinite(cup.pixel?.u) && Number.isFinite(cup.pixel?.v)) {
      const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      circle.setAttribute("cx", cup.pixel.u);
      circle.setAttribute("cy", cup.pixel.v);
      circle.setAttribute("r", "18");
      circle.classList.add("selection-dot");
      overlay.append(circle);
    }
  }

  function renderProposal() {
    const proposal = state?.proposal;
    hide("proposal-panel", !proposal);
    if (!proposal) return;
    text("proposal-cup", proposal.cup_id);
    text("proposal-action", proposal.action === "hover" ? "Hover above the cup" : "Pick → handover → place → return home");
    text("proposal-description", proposal.description);
    text("proposal-context", `Selected from scan ${proposal.capture_id}. Check the highlighted cup before continuing.`);
    text("confirm-button", `${state.mode === "live" ? "Confirm" : "Preview"} cup ${proposal.cup_id}`);
    updateCountdown();
  }

  function updateCountdown() {
    if (!state?.proposal) return;
    const remaining = Math.max(0, Math.ceil((proposalDeadline - performance.now()) / 1000));
    text("proposal-countdown", remaining ? `${remaining}s to confirm` : "Request expired");
    dom["proposal-countdown"].classList.toggle("expired", remaining === 0);
    dom["confirm-button"].disabled = !actionable() || remaining === 0 || !safeScan() || state.proposal.capture_id !== state.inventory?.capture;
  }

  function renderActivity() {
    const busy = Boolean(state?.busy || posting);
    const progress = state?.progress;
    text("activity-status", !connected ? "Offline" : busy ? "Working" : state?.recovery_required ? "Check required" : "Idle");
    dom["activity-indicator"].classList.toggle("busy", connected && busy);
    let title = state?.message || "Ready for your next action";
    let description = "Scan the workspace, then select a cup or type a command.";
    if (!state) title = "Waiting for the controller";
    else if (!connected) { title = "Controller disconnected"; description = "This display may be stale. Active motion may continue."; }
    else if (busy) { title = progress?.step ? humanize(progress.step) : humanize(state.operation || "Processing request"); description = state.mode === "live" ? "Follow the controller status. Use physical safety controls if needed." : "Processing locally. No physical movement is enabled."; }
    else if (state.recovery_required) { title = "Operator inspection required"; description = "Acknowledge recovery only after checking both arms and the cup."; }
    else if (state.proposal) { title = "Waiting for your confirmation"; description = `Review the action for cup ${state.proposal.cup_id}. No movement has been requested yet.`; }
    else if (state.error) { title = "Request blocked or interrupted"; description = asMessage(state.error); }
    else if (state.inventory && !state.scan_usable) description = "Capture a fresh scan before selecting another cup.";
    else if (state.inventory) description = "Choose a detected cup to see its available actions.";
    text("operation-title", asMessage(title));
    text("operation-description", description);
    text("operation-symbol", !connected ? "○" : state?.error || state?.recovery_required ? "!" : busy ? "↻" : state?.proposal ? "◇" : "✓");
    const showProgress = progress && Number.isFinite(progress.total) && progress.total > 0;
    hide("progress-section", !showProgress);
    if (showProgress) {
      const completed = Math.max(0, Math.min(progress.total, Number(progress.completed) || 0));
      const percentage = Math.round(100 * completed / progress.total);
      dom["progress-fill"].style.width = `${percentage}%`;
      dom["progress-bar"].setAttribute("aria-valuenow", String(percentage));
      text("progress-step", humanize(progress.step || progress.state || "Sequence"));
      text("progress-count", `${completed} / ${progress.total}`);
    }
    const events = Array.isArray(state?.events) ? state.events.slice(-7).reverse() : [];
    const key = JSON.stringify(events);
    if (key === eventsKey) return;
    eventsKey = key;
    dom["event-list"].replaceChildren();
    if (!events.length) {
      const empty = document.createElement("li");
      empty.className = "event-empty";
      empty.textContent = "Your action history will appear here.";
      dom["event-list"].append(empty);
    }
    events.forEach((event) => {
      const row = document.createElement("li");
      if (["error", "warning"].includes(event.level)) row.className = "error";
      else if (event.level === "success") row.className = "success";
      const time = document.createElement("time");
      time.className = "event-time";
      time.textContent = clockText(event.time);
      const message = document.createElement("span");
      message.className = "event-message";
      message.textContent = asMessage(event.message);
      row.append(time, message);
      dom["event-list"].append(row);
    });
  }

  function renderControls() {
    const canAct = actionable();
    const pending = Boolean(state?.proposal);
    const cup = currentCup();
    dom["scan-button"].disabled = !canAct || Boolean(state?.recovery_required);
    text("scan-button-label", state?.busy && state.operation === "scan" ? "Scanning…" : "Scan workspace");
    dom["command-input"].disabled = !canAct || pending || Boolean(state?.recovery_required);
    dom["command-button"].disabled = !canAct || pending || Boolean(state?.recovery_required) || !dom["command-input"].value.trim();
    document.querySelectorAll("[data-example]").forEach((button) => { button.disabled = !canAct || pending || !state?.llm_enabled || Boolean(state?.recovery_required); });
    dom["hover-button"].disabled = !canAct || pending || !safeScan() || !cup?.selectable;
    dom["pick-button"].disabled = !canAct || pending || !safeScan() || !cup?.pick_available;
    dom["cancel-button"].disabled = !canAct || !pending;
    dom["recovery-button"].disabled = !canAct || !dom["recovery-ack"].checked || !state?.recovery_required;
    dom["recovery-ack"].disabled = !canAct;
    document.querySelectorAll(".cup-card").forEach((card) => { card.disabled = !connected || Boolean(state?.busy) || posting || pending; });
    text("model-label", state?.llm_enabled ? `${state.model || "Local model"} · local` : "Terminal commands enabled");
    text("command-hint", state?.llm_enabled ? "Every movement starts with an action preview." : "Use scan, list, goto 2, or hover 2. All movement requires confirmation.");
    dom["command-input"].placeholder = state?.llm_enabled ? "Pick cup two…" : "Try goto 2 or hover 2…";
    updateCountdown();
  }

  function render() {
    renderMode();
    renderConnection();
    renderInventory();
    renderSelection();
    renderProposal();
    hide("recovery-panel", !state?.recovery_required || Boolean(state?.busy));
    renderActivity();
    renderControls();
  }

  async function readState() {
    const number = ++pollNumber;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4500);
    try {
      const response = await fetch("/api/state", { cache: "no-store", credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`Controller status unavailable (${response.status}).`);
      const incoming = await response.json();
      if (!incoming || typeof incoming.instance_id !== "string" || typeof incoming.csrf_token !== "string") throw new Error("Invalid controller status.");
      if (number < appliedPoll) return;
      appliedPoll = number;
      if (!state) transientError = "";
      if (state && incoming.instance_id !== state.instance_id) {
        selectedId = null;
        selectedCapture = null;
        proposalDeadline = 0;
        dom["recovery-ack"].checked = false;
        transientError = "The controller restarted. Review its current state and scan again before selecting a cup.";
      }
      if (state?.recovery_required !== incoming.recovery_required) dom["recovery-ack"].checked = false;
      state = incoming;
      connected = true;
      proposalDeadline = state.proposal && Number.isFinite(state.proposal.expires_in) ? performance.now() + Math.max(0, state.proposal.expires_in) * 1000 : 0;
      render();
    } catch (error) {
      if (number < appliedPoll) return;
      appliedPoll = number;
      connected = false;
      if (!state) transientError = "Could not reach the local controller. Keep the web app process running and this page will reconnect.";
      render();
    } finally {
      clearTimeout(timeout);
    }
  }

  async function post(path, payload = {}) {
    if (!actionable() || !state?.csrf_token) return;
    const instance = state.instance_id;
    posting = true;
    transientError = "";
    renderControls();
    renderActivity();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf_token },
        body: JSON.stringify({ ...payload, request_id: crypto.randomUUID() }),
        signal: controller.signal,
      });
      let result;
      try { result = await response.json(); } catch {
        const unknown = new Error("The controller returned an unreadable response.");
        unknown.name = "UnknownOutcomeError";
        throw unknown;
      }
      if (!response.ok || !result.accepted) throw new Error(asMessage(result.detail || result.error) || `Request rejected (${response.status}).`);
      await readState();
      if (state?.instance_id !== instance) transientError = "The controller restarted during this request. Check the current state; the request was not retried.";
    } catch (error) {
      transientError = ["AbortError", "UnknownOutcomeError"].includes(error.name) || error instanceof TypeError ? "Request outcome unknown: the connection was interrupted or its response was unreadable. Check the controller before continuing. The request was not retried." : error.message;
      await readState();
    } finally {
      clearTimeout(timeout);
      posting = false;
      render();
    }
  }

  dom["scan-button"].addEventListener("click", () => post("/api/scan"));
  dom["command-input"].addEventListener("input", renderControls);
  dom["command-input"].addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!dom["command-button"].disabled) dom["command-form"].requestSubmit();
    }
  });
  dom["command-form"].addEventListener("submit", (event) => {
    event.preventDefault();
    const command = dom["command-input"].value.trim();
    if (!command || dom["command-button"].disabled) return;
    post("/api/command", { text: command });
  });
  document.querySelectorAll("[data-example]").forEach((button) => button.addEventListener("click", () => {
    dom["command-input"].value = button.dataset.example;
    dom["command-input"].focus();
    renderControls();
  }));
  ["hover", "pick"].forEach((action) => dom[`${action}-button`].addEventListener("click", () => {
    const cup = currentCup();
    if (!cup || dom[`${action}-button`].disabled) return;
    post("/api/select", { capture_id: selectedCapture, cup_id: cup.id, action: action === "pick" ? "goto" : "hover" });
  }));
  dom["confirm-button"].addEventListener("click", () => {
    const proposal = state?.proposal;
    if (!proposal || dom["confirm-button"].disabled) return;
    post("/api/confirm", { token: proposal.token, capture_id: proposal.capture_id, cup_id: proposal.cup_id, action: proposal.action });
  });
  dom["cancel-button"].addEventListener("click", () => { if (!dom["cancel-button"].disabled) post("/api/cancel"); });
  dom["recovery-ack"].addEventListener("change", renderControls);
  dom["recovery-button"].addEventListener("click", () => {
    if (!dom["recovery-button"].disabled) post("/api/recovery", { acknowledged: true });
  });
  dom["camera-image"].addEventListener("load", () => {
    imageLoaded = true;
    hide("image-stage", false);
    hide("empty-camera", true);
    renderOverlay();
  });
  dom["camera-image"].addEventListener("error", () => {
    imageLoaded = false;
    hide("image-stage", true);
    hide("empty-camera", false);
    dom["empty-camera"].querySelector("h3").textContent = "Scan image unavailable.";
    dom["empty-camera"].querySelector("p").textContent = "Check the controller and scan again to refresh the view.";
  });
  dom["camera-image"].addEventListener("click", (event) => {
    if (!imageLoaded || !connected) return;
    const image = dom["camera-image"];
    const rect = image.getBoundingClientRect();
    const scale = Math.min(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
    const x = (event.clientX - rect.left - (rect.width - image.naturalWidth * scale) / 2) / scale;
    const y = (event.clientY - rect.top - (rect.height - image.naturalHeight * scale) / 2) / scale;
    const hits = cups().filter((cup) => Array.isArray(cup.bbox_xyxy) && x >= cup.bbox_xyxy[0] && x <= cup.bbox_xyxy[2] && y >= cup.bbox_xyxy[1] && y <= cup.bbox_xyxy[3]);
    if (hits.length === 1) chooseCup(hits[0].id);
  });
  setInterval(updateCountdown, 250);
  async function poll() {
    await readState();
    setTimeout(poll, 700);
  }
  poll();
})();
