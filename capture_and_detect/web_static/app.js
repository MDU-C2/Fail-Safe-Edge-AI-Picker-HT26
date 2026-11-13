/* Local dashboard. The server owns proposals, validation, and all robot commands. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const dom = Object.fromEntries(Array.from(document.querySelectorAll("[id]"), (el) => [el.id, el]));
  let state = null;
  let connected = false;
  let posting = false;
  let stopPosting = false;
  let quickPosting = false;
  let quickGeneration = 0;
  let localStopInhibit = false;
  let selectedId = null;
  let selectedCapture = null;
  let imageCapture = null;
  let imageLoaded = false;
  let cardsKey = "";
  let eventsKey = "";
  let inferenceEventsKey = "";
  let placementsKey = "";
  let savedCupsKey = "";
  let saveSelectionKey = "";
  let pollNumber = 0;
  let appliedPoll = 0;
  let proposalDeadline = 0;
  let controlDeadline = 0;
  let speedDirty = false;
  let handoverDirty = false;
  let releaseReview = null;
  let releaseMessage = "";
  let transientError = "";

  const text = (id, value) => { const el = dom[id]; const next = value == null ? "" : String(value); if (el.textContent !== next) el.textContent = next; };
  const hide = (id, hidden) => { dom[id].hidden = hidden; };
  const cups = () => Array.isArray(state?.inventory?.cups) ? state.inventory.cups : [];
  const currentCup = () => selectedCapture === state?.inventory?.capture ? cups().find((cup) => cup.id === selectedId) : null;
  const idle = () => connected && Boolean(state?.ready) && !state?.busy && !posting && !stopPosting;
  const actionable = () => idle() && !localStopInhibit && !state?.stop_requested;
  const controlActionable = () => actionable() && !state?.recovery_required;
  const canStop = () => connected && Boolean(state?.ready) && !stopPosting && !state?.stop_in_flight;
  const isStop = (value) => ["stop", "halt", "stop robot", "stop the robot"].includes(value.trim().toLowerCase().replace(/\s+/g, " "));
  const safeScan = () => Boolean(state?.inventory && state?.scan_usable && !state?.recovery_required && !localStopInhibit && !state?.stop_requested);
  const pickableCups = () => cups().filter((cup) => cup.selectable && cup.pick_available);
  const placementLocations = () => Array.isArray(state?.placement_grid?.locations) ? state.placement_grid.locations : [];
  const placementAvailable = (location) => location.occupied === false && !location.reserved && !location.vision_blocked;
  const placementStatus = (location) => location.reserved ? "reserved" : location.vision_blocked ? "unknown" : location.occupied ? "occupied" : placementAvailable(location) ? "available" : "unknown";
  const availableLocations = () => placementLocations().filter(placementAvailable);
  const gridReady = () => Boolean(state?.placement_grid && !state.placement_grid.error && Array.isArray(state.placement_grid.locations));
  const releaseActionable = () => idle() && !state?.stop_in_flight && gridReady() && placementLocations().length > 0 && typeof state.placement_grid.fingerprint === "string" && state.placement_grid.fingerprint.length > 0;
  const releaseContext = (snapshot) => JSON.stringify([snapshot?.instance_id, snapshot?.placement_grid?.fingerprint, snapshot?.stop_requested, snapshot?.recovery_required, snapshot?.stop]);
  const selectedIds = () => state?.proposal?.action === "goto_all" ? (state.proposal.cup_ids || []) : [state?.proposal?.cup_id ?? selectedId];
  const speedText = (value) => Number.isFinite(value) ? `${value} mm/s` : "unavailable";
  const accelerationText = (value) => Number.isFinite(value) ? `${value}% acceleration` : "acceleration unavailable";
  const handoverAxes = ["x", "y", "z"];
  const coordinatesAvailable = (point) => handoverAxes.every((axis) => Number.isFinite(point?.[axis]));
  const coordinatesText = (point, signed = false) => coordinatesAvailable(point) ? handoverAxes.map((axis) => `${axis.toUpperCase()} ${signed && point[axis] > 0 ? "+" : ""}${point[axis]}`).join(", ") + " mm" : "unavailable";
  const enteredHandoverOffset = () => Object.fromEntries(handoverAxes.map((axis) => [axis, Number(dom[`robot-handover-${axis}`].value)]));
  const clockText = (value) => {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  };
  const asMessage = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
  const humanize = (value) => String(value || "").replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
  const savedImageUrl = (value) => {
    if (typeof value !== "string" || !value) return null;
    try {
      const url = new URL(value, window.location.origin);
      const path = /^\/api\/saved-image\/(?:[0-9a-f]{32}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})$/i;
      return url.origin === window.location.origin && !url.search && !url.hash && path.test(url.pathname) ? url.pathname : null;
    } catch { return null; }
  };

  function setSavedImage(image, fallback, url, name) {
    const path = savedImageUrl(url);
    image.alt = `Saved reference for ${name}`;
    if (image.getAttribute("src") === path) return;
    image.hidden = !path;
    fallback.hidden = Boolean(path);
    if (path) image.setAttribute("src", path);
    else image.removeAttribute("src");
  }

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
    text("motion-summary", `Normal ${speedText(state?.motion?.speed)} · Cup flip ${speedText(state?.motion?.flip_speed)} · Sequence home ${speedText(state?.motion?.home_speed)}`);
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

  // Provider evidence comes only from the controller, never from probes or browser counters.
  const inferenceObject = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : null;
  const inferenceString = (value, fallback = "") => typeof value === "string" && value.trim() ? value : fallback;
  const inferenceCount = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const inferenceProvider = (value) => ({ mac: "MacBook", pi: "Raspberry Pi", local: "Local model" })[value] || "Unknown";
  const inferenceTime = (value) => typeof value === "string" && value.trim() ? clockText(value) : "";

  function renderInference() {
    const snapshot = inferenceObject(state?.inference);
    const demo = state?.mode === "demo";
    const enabled = !demo && snapshot?.enabled === true && snapshot.state !== "local_only";
    const localProvider = ["pi", "local"].includes(snapshot?.local_provider) ? snapshot.local_provider
      : snapshot?.selected_provider === "local" ? "local" : "pi";
    const permittedProviders = enabled ? ["mac", localProvider] : [localProvider];
    const stale = !connected && Boolean(state);
    const knownStates = {
      mac_preferred: ["MAC PREFERRED", "Waiting for a result", "neutral"],
      mac_active: ["MAC ACTIVE", "MacBook supplied the latest accepted result.", "mac"],
      switching_to_pi: ["SWITCHING TO PI", "Waiting for a Raspberry Pi result.", "switching"],
      pi_active: ["PI ACTIVE", "Raspberry Pi supplied the latest accepted result.", "pi"],
      mac_recovering: ["CHECKING MAC", `${inferenceProvider(localProvider)} remains selected while the MacBook is checked.`, "switching"],
      mac_probing: ["CHECKING MAC", "Checking whether the MacBook can accept requests again.", "switching"],
      mac_ready: ["MAC PREFERRED", "Waiting for the first accepted MacBook result.", "neutral"],
      failback_pending: ["MAC RETURN PENDING", "Recovery checks passed. The source will change at a request boundary.", "switching"],
      switching_to_mac: ["SWITCHING TO MAC", "Waiting for a new MacBook result.", "switching"],
      local_ready: ["LOCAL READY", "Waiting for a local inference result.", "neutral"],
      local_active: ["LOCAL ACTIVE", "Local inference is selected.", "neutral"],
      unavailable: ["INFERENCE UNAVAILABLE", "No provider result is available for the current request.", "error"],
      audit_failed: ["RECORDING FAILED", "Inference records could not be saved. Check the controller before continuing.", "error"],
      stopped: ["INFERENCE STOPPED", "Inference processing has stopped.", "neutral"],
    };
    if (localProvider === "local") {
      knownStates.switching_to_pi = ["SWITCHING TO LOCAL", "Waiting for a local inference result.", "switching"];
    }
    const labels = enabled || (!demo && snapshot && ["unavailable", "audit_failed", "stopped"].includes(snapshot.state))
      ? (Object.hasOwn(knownStates, snapshot.state) ? knownStates[snapshot.state] : ["STATUS UNKNOWN", "Waiting for a recognized provider status.", "neutral"])
      : demo ? ["DEMO · NO FALLBACK", "The sample scene does not use MacBook or Raspberry Pi inference.", "neutral"]
      : snapshot ? ["LOCAL ONLY · NO FALLBACK", `${inferenceProvider(localProvider)} supplies inference. MacBook fallback is not enabled in this controller.`, "neutral"]
      : ["STATUS UNAVAILABLE", "This controller has not reported inference-source status. Fallback is not confirmed.", "neutral"];
    dom["inference-panel"].dataset.tone = stale ? "stale" : labels[2];
    text("inference-badge", stale ? "STALE · DISCONNECTED" : !state ? "CONNECTING" : labels[0]);
    hide("inference-stale", !stale);
    text("inference-reason", !state ? "Waiting for controller status." : enabled ? inferenceString(snapshot.reason, labels[1]) : labels[1]);

    const candidate = inferenceObject(snapshot?.last_result);
    const result = !demo && candidate && permittedProviders.includes(candidate.provider) ? candidate : null;
    text("inference-provider", result ? inferenceProvider(result.provider) : demo ? "Sample scene" : "No accepted result");
    const acceptedTime = inferenceTime(result?.accepted_at);
    const duration = Number.isFinite(result?.duration_ms) && result.duration_ms >= 0
      ? result.duration_ms < 1000 ? `${Math.round(result.duration_ms)} ms` : `${(result.duration_ms / 1000).toFixed(2)} s`
      : "duration unavailable";
    text("inference-result-time", result ? `${stale ? "Last known: " : ""}${acceptedTime ? `Accepted at ${acceptedTime}` : "Acceptance time unavailable"} · ${duration}`
      : demo ? "No inference result is produced in demo mode." : "A selected provider is not evidence of a completed result.");
    text("inference-observation", inferenceString(result?.observation_id, "None accepted"));
    text("inference-request-id", inferenceString(result?.request_id, "None accepted"));
    const selected = !demo && permittedProviders.includes(snapshot?.selected_provider) ? inferenceProvider(snapshot.selected_provider)
      : demo ? "Sample scene" : "Unavailable";
    const inFlight = !demo && permittedProviders.includes(snapshot?.in_flight_provider) ? inferenceProvider(snapshot.in_flight_provider) : "None reported";
    text("inference-selected", `${stale ? "Last known: " : ""}${selected}`);
    text("inference-requested", `${stale ? "Last known: " : ""}${inFlight}`);
    const counters = inferenceObject(snapshot?.counters);
    const countLabel = (provider) => inferenceCount(counters?.[provider]) ?? "—";
    const localCount = `${localProvider === "pi" ? "Pi" : "Local"} ${countLabel(localProvider)}`;
    text("inference-counters", demo ? "Not applicable" : enabled ? `Mac ${countLabel("mac")} · ${localCount}`
      : snapshot && snapshot.selected_provider === localProvider ? localCount : "Unavailable");

    const recovery = inferenceObject(snapshot?.recovery);
    const successes = inferenceCount(recovery?.successes);
    const required = inferenceCount(recovery?.required);
    const recovering = enabled && ["pi_active", "local_active", "mac_recovering", "mac_probing", "failback_pending", "switching_to_mac"].includes(snapshot.state) && required > 0 && successes !== null;
    hide("inference-recovery", !recovering);
    // next_probe_at is a coordinator monotonic deadline, not a wall-clock time.
    text("inference-recovery", recovering ? `${stale ? "Last known " : ""}Mac recovery checks: ${successes}/${required}${recovery.ready === true ? " · ready for the next request boundary" : ""}. These checks do not replace the last accepted result.` : "");

    // Render the server's bounded history. Refresh/reconnection never fabricates events.
    const events = !demo && Array.isArray(snapshot?.events) ? snapshot.events.filter((event) => inferenceObject(event) && typeof event.message === "string").slice(-8).reverse() : [];
    const key = JSON.stringify([snapshot?.session_id, enabled, demo, events]);
    text("inference-event-count", events.length);
    text("inference-history-note", demo ? "Demo mode does not exercise inference failover."
      : "Accepted results and this history come from the controller. Recovery checks do not count as accepted results.");
    if (inferenceEventsKey === key) return;
    inferenceEventsKey = key;
    dom["inference-events"].replaceChildren();
    if (!events.length) {
      const empty = document.createElement("li");
      empty.className = "inference-event-empty";
      empty.textContent = demo ? "No inference-source events in demo mode." : "No source events reported.";
      dom["inference-events"].append(empty);
      return;
    }
    events.forEach((event) => {
      const item = document.createElement("li");
      item.className = "inference-event";
      item.dataset.level = ["warning", "error", "info"].includes(event.level) ? event.level : "info";
      const time = document.createElement("time");
      time.textContent = inferenceTime(event.time) || "Time unknown";
      if (inferenceTime(event.time)) time.dateTime = event.time;
      const message = document.createElement("span");
      message.textContent = event.message;
      const evidence = [inferenceString(event.observation_id), inferenceString(event.request_id)].filter(Boolean);
      if (evidence.length) item.title = evidence.join(" · ");
      item.append(time, message);
      dom["inference-events"].append(item);
    });
  }

  function renderStop() {
    const stopping = localStopInhibit || state?.stop_requested;
    hide("stop-panel", !stopping);
    const status = state?.stop?.state;
    text("stop-title", status === "confirmed" ? "Controller reported software stop" : status === "previewed" ? "Dry run: stop latched locally" : status === "unconfirmed" ? "Software stop unconfirmed" : "Software stop requested");
    text("stop-message", state?.stop?.message || "Waiting for confirmation of the stop request. The browser cannot guarantee that the robot has stopped.");
    dom["stop-panel"].classList.toggle("unconfirmed", status === "unconfirmed");
    text("stop-button", stopPosting || state?.stop_in_flight ? "Requesting stop…" : "Stop robot");
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
    const targets = selectedIds();
    document.querySelectorAll(".cup-card").forEach((card) => {
      const selected = targets.includes(Number(card.dataset.cupId));
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
    const targets = selectedIds();
    cups().filter((item) => targets.includes(item.id)).forEach((cup) => {
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
    });
  }

  function placementCupSummary(summary) {
    if (!summary || !Number.isInteger(summary.assigned_count) || summary.assigned_count < 0
        || !Array.isArray(summary.location_ids)
        || !summary.location_ids.every((id) => Number.isInteger(id) && id > 0)
        || new Set(summary.location_ids).size !== summary.location_ids.length
        || summary.location_ids.length > summary.assigned_count
        || (summary.assigned_count > 0 && !summary.location_ids.length)) return "";
    const count = summary.assigned_count;
    const cells = summary.location_ids.length;
    let message = count ? `Last frame: ${count} cup${count === 1 ? "" : "s"} confidently located in ${cells} cell${cells === 1 ? "" : "s"}: ${summary.location_ids.map((id) => `P${id}`).join(", ")}.`
      : "No cups confidently assigned to grid cells in the last frame.";
    if (summary.stable_cell_counts === false) {
      const minimum = summary.min_assigned_count;
      const maximum = summary.max_assigned_count;
      const range = Number.isInteger(minimum) && Number.isInteger(maximum)
        && minimum >= 0 && minimum <= count && count <= maximum && minimum < maximum
        ? ` (${minimum}–${maximum} confidently assigned cups)` : "";
      message += ` Cup assignments changed across the scan${range}. Blocks from all frames are retained.`;
    }
    return message;
  }

  function placementCameraLabel(camera, fallback) {
    const evidence = Array.isArray(camera?.evidence) ? camera.evidence : [];
    const labels = [];
    if (Number.isInteger(camera?.cup_count) && camera.cup_count > 0) {
      labels.push(`${camera.cup_count} cup${camera.cup_count === 1 ? "" : "s"}`);
    } else if (evidence.includes("cup")) labels.push("cup seen earlier");
    if (evidence.includes("detected_object")) labels.push("detected object");
    if (evidence.includes("raised_depth") && !labels.length) labels.push("depth check");
    return labels.length ? labels.join(" + ") : fallback;
  }

  function renderPlacements() {
    const grid = state?.placement_grid;
    const locations = placementLocations();
    const available = availableLocations();
    const reserved = locations.filter((location) => placementStatus(location) === "reserved").length;
    const occupied = locations.filter((location) => placementStatus(location) === "occupied").length;
    const unknown = locations.filter((location) => placementStatus(location) === "unknown").length;
    const observation = state?.inventory?.placement_observation;
    const observations = Array.isArray(observation?.slots) ? observation.slots : [];
    const released = Array.isArray(observation?.released_ids) ? observation.released_ids : [];
    const ready = gridReady();
    text("placement-count", ready ? locations.length : "—");
    text("placement-next", ready ? (available.length ? `Next: location ${available[0].id}` : "No available locations") : "Grid unavailable");
    text("placement-summary", grid?.error ? asMessage(grid.error) : !ready ? "Placement grid unavailable. Picking is blocked until the controller loads it." : `${available.length} available · ${occupied} occupied · ${reserved} reserved · ${unknown} unknown / blocked${!connected ? " · view may be stale" : ""}`);
    dom["placement-summary"].classList.toggle("has-error", !ready);
    const observationMessage = observation ? [placementCupSummary(observation.cup_summary), asMessage(observation.message), released.length ? `${released.length} location${released.length === 1 ? "" : "s"} released: ${released.join(", ")}.` : "", observation.applied === false ? "Saved location status was not updated." : ""].filter(Boolean).join(" ") : state?.mode === "demo" ? "Sample placement status. This demo does not check a camera." : "No placement camera check in this scan.";
    text("placement-observation", observationMessage);
    text("placement-snapshot", state?.inventory ? `Last scan only${clockText(state.inventory.saved_at) ? ` · ${clockText(state.inventory.saved_at)}` : ""}. Scan again after removing a cup; this view does not update continuously.` : "Scan checks the placement area. Remove a cup and scan again to update its location.");
    const key = JSON.stringify([grid, observation, state?.proposal?.placements]);
    if (key === placementsKey) return;
    placementsKey = key;
    dom["placement-grid"].replaceChildren();
    const proposed = Array.isArray(state?.proposal?.placements) ? state.proposal.placements.map((item) => item.id) : [];
    locations.forEach((location) => {
      const status = placementStatus(location);
      const camera = observations.find((item) => item.id === location.id);
      const cameraState = ["clear", "occupied", "unknown"].includes(camera?.state) ? camera.state : "not checked";
      const cameraLabelText = placementCameraLabel(camera, cameraState);
      const cameraDescription = `Camera: ${cameraLabelText}${camera?.reason ? ` (${asMessage(camera.reason)})` : ""}`;
      const slot = document.createElement("div");
      slot.className = `placement-slot ${status}${proposed.includes(location.id) ? " proposed" : ""}`;
      slot.setAttribute("role", "listitem");
      slot.setAttribute("aria-label", `Location ${location.id}: ${status}${proposed.includes(location.id) ? ", in current proposal" : ""}. ${cameraDescription}`);
      const id = document.createElement("strong");
      id.textContent = String(location.id);
      const label = document.createElement("span");
      label.textContent = proposed.includes(location.id) ? "Selected" : status === "unknown" ? "Blocked" : humanize(status);
      slot.title = `Location ${location.id}: ${status}. X ${location.x}, Y ${location.y}, Z ${location.z} mm. ${cameraDescription}.`;
      slot.append(id, label);
      if (camera) {
        const cameraLabel = document.createElement("span");
        cameraLabel.className = "placement-camera-state";
        cameraLabel.textContent = `Camera: ${cameraLabelText}`;
        slot.append(cameraLabel);
      }
      dom["placement-grid"].append(slot);
    });
  }

  function renderProposal() {
    const proposal = state?.proposal;
    hide("proposal-panel", !proposal || localStopInhibit || state?.stop_requested);
    if (!proposal) return;
    const batch = proposal.action === "goto_all";
    const ids = batch && Array.isArray(proposal.cup_ids) ? proposal.cup_ids : [proposal.cup_id];
    const cupCount = `${ids.length} cup${ids.length === 1 ? "" : "s"}`;
    text("proposal-cup", batch ? `Pick ${cupCount}` : `Cup ${proposal.cup_id}`);
    text("proposal-action", proposal.action === "hover" ? "Hover above the cup" : "Pick → handover → place → return home");
    text("proposal-description", proposal.description);
    const saved = proposal.saved_cup;
    hide("proposal-saved-cup", !saved);
    if (saved) {
      text("proposal-saved-name", saved.name);
      text("proposal-saved-reason", `Model match: ${asMessage(saved.match_reason) || "No match explanation provided."}`);
      setSavedImage(dom["proposal-saved-image"], dom["proposal-saved-image-fallback"], saved.thumbnail_url, saved.name);
    }
    const placements = Array.isArray(proposal.placements) ? proposal.placements : [];
    hide("proposal-destinations", !placements.length);
    text("proposal-destinations", placements.map((place, index) => `Cup ${ids[index] ?? "?"} → location ${place.id}`).join(" · "));
    const excluded = Array.isArray(proposal.excluded_cup_ids) ? proposal.excluded_cup_ids : [];
    hide("proposal-excluded", !excluded.length);
    text("proposal-excluded", `Not included because no destination slots remain: ${excluded.map((id) => `cup ${id}`).join(", ")}.`);
    const motion = state?.motion;
    const normal = `Normal: ${speedText(proposal.speed ?? motion?.speed)}, ${accelerationText(motion?.acceleration)}`;
    const flip = `Cup flip: ${speedText(proposal.flip_speed ?? motion?.flip_speed)}, ${accelerationText(motion?.flip_acceleration)}`;
    const home = `Sequence home: ${speedText(proposal.home_speed ?? motion?.home_speed)}`;
    text("proposal-motion", proposal.action === "hover" ? normal : `${normal}. ${flip}. ${home}.`);
    const transfer = ["goto", "goto_all"].includes(proposal.action);
    const handover = proposal.handover;
    hide("proposal-handover", !transfer);
    text("proposal-handover", transfer ? `Reviewed right receiving target: ${coordinatesText(handover?.receive_mm)}. Robot-base offset: ${coordinatesText(handover?.offset_mm, true)}. Receiving approach: ${speedText(handover?.approach_speed)}${handover?.adjusted ? " (adjusted alignment; maximum 25 mm/s)" : ""}.` : "");
    text("proposal-context", `Selected from scan ${proposal.capture_id}. ${batch ? `Cups ${ids.join(", ")} run in this order. Check all highlighted cups and destinations.` : proposal.action === "hover" ? "Check the highlighted cup before continuing." : "Check the highlighted cup and destination before continuing."}`);
    text("confirm-button", `${state.mode === "live" ? "Confirm" : "Preview"} ${batch ? cupCount : `cup ${proposal.cup_id}`}`);
    updateCountdown();
  }

  function renderSavedCups() {
    const library = state?.saved_cups;
    const entries = Array.isArray(library?.cups) ? library.cups.filter((cup) => cup && typeof cup.id === "string" && typeof cup.name === "string") : [];
    const enabled = library?.enabled === true;
    const canManage = controlActionable() && enabled;
    const canFind = canManage && library?.matcher_enabled === true;
    text("saved-cup-count", entries.length);
    const error = asMessage(library?.error);
    const busyMessage = state?.busy && state.operation === "pick_saved" ? "Scanning and comparing the saved reference…" : state?.busy && state.operation === "save_cup" ? "Saving the selected cup reference…" : "";
    text("saved-cups-status", error || busyMessage || (!state ? "Loading saved cups…" : !enabled ? "Saved cup references are unavailable in this session." : !library.matcher_enabled ? "References are saved locally. Visual matching is unavailable in this session." : entries.length ? "Choose a saved reference to find it in a fresh scan." : "No saved cups yet."));
    dom["saved-cups-status"].classList.toggle("has-error", Boolean(error));
    const key = JSON.stringify(entries);
    if (key !== savedCupsKey) {
      savedCupsKey = key;
      dom["saved-cup-list"].replaceChildren();
      if (!entries.length) {
        const empty = document.createElement("p");
        empty.className = "saved-cups-empty";
        empty.textContent = "Scan, select a cup, and save it with a name to find it again later.";
        dom["saved-cup-list"].append(empty);
      }
      entries.forEach((saved) => {
        const card = document.createElement("article");
        card.className = "saved-cup-card";
        card.dataset.savedCupId = saved.id;
        const thumbnail = document.createElement("div");
        thumbnail.className = "saved-cup-thumbnail";
        const image = document.createElement("img");
        image.hidden = true;
        image.loading = "lazy";
        const fallback = document.createElement("span");
        fallback.textContent = "Reference unavailable";
        image.addEventListener("error", () => { image.hidden = true; fallback.hidden = false; });
        thumbnail.append(image, fallback);
        setSavedImage(image, fallback, saved.thumbnail_url, saved.name);
        const details = document.createElement("div");
        details.className = "saved-cup-details";
        const name = document.createElement("h3");
        name.textContent = saved.name;
        const label = document.createElement("p");
        label.textContent = "Saved visual reference";
        details.append(name, label);
        const actions = document.createElement("div");
        actions.className = "saved-cup-actions";
        const find = document.createElement("button");
        find.type = "button";
        find.className = "button button-secondary";
        find.dataset.savedPick = saved.id;
        find.textContent = "Find & preview pick";
        find.setAttribute("aria-label", `Find ${saved.name} and preview pick`);
        find.addEventListener("click", () => { if (!find.disabled) post("/api/pick_saved", { id: saved.id }); });
        const forget = document.createElement("button");
        forget.type = "button";
        forget.className = "cancel-button";
        forget.dataset.savedForget = saved.id;
        forget.textContent = "Forget";
        forget.setAttribute("aria-label", `Forget saved cup ${saved.name}`);
        forget.addEventListener("click", () => { if (!forget.disabled) post("/api/forget_cup", { id: saved.id }); });
        actions.append(find, forget);
        card.append(thumbnail, details, actions);
        dom["saved-cup-list"].append(card);
      });
    }
    dom["saved-cup-list"].querySelectorAll("[data-saved-pick]").forEach((button) => { button.disabled = !canFind; });
    dom["saved-cup-list"].querySelectorAll("[data-saved-forget]").forEach((button) => { button.disabled = !canManage; });
    const cup = currentCup();
    const selection = cup ? `${selectedCapture}:${cup.id}` : "";
    if (selection !== saveSelectionKey) {
      saveSelectionKey = selection;
      dom["save-cup-name"].value = "";
    }
    const name = dom["save-cup-name"].value.trim();
    const canSave = canManage && safeScan() && cup?.can_save === true && !state?.proposal;
    dom["save-cup-name"].disabled = !canSave;
    dom["save-cup-button"].disabled = !canSave || !name || name.length > 48;
    text("save-cup-hint", !enabled ? "Saving references is unavailable in this session." : !safeScan() ? "Scan again and select a cup before saving its reference." : cup?.can_save !== true ? "This detection cannot be saved. Choose a cup with a usable reference image." : "Save a name and reference image locally. Saving sends no movement command.");
  }

  // Only explicit server capabilities authorize hardware previews. The browser
  // never infers support from gateway names, demo mode or an earlier snapshot.
  function robotCapability(action) {
    const capabilities = state?.robot_controls?.capabilities;
    const capability = capabilities && !Array.isArray(capabilities) && typeof capabilities === "object"
      && Object.hasOwn(capabilities, action) ? capabilities[action] : null;
    return capability && !Array.isArray(capability) && typeof capability === "object" ? capability : null;
  }

  const robotActionSupported = (action) => robotCapability(action)?.supported === true;
  const robotStatusAvailable = () => robotCapability("robot_status")?.supported !== false;
  const capabilityText = (action, field, fallback = "") => {
    const value = robotCapability(action)?.[field];
    return typeof value === "string" && value.trim() ? value : fallback;
  };

  function robotRequestSupported(path, payload) {
    if (path === "/api/control_preview") return robotActionSupported(payload.action);
    if (path === "/api/control_confirm") return robotActionSupported(state?.robot_controls?.proposal?.action);
    if (path === "/api/robot_status") return robotStatusAvailable();
    return true; // Stop remains a best-effort request; local settings need no gateway action.
  }

  function renderRobotControls() {
    const controls = state?.robot_controls;
    const snapshot = controls?.status;
    const proposal = controls?.proposal;
    const enabled = controlActionable();
    const currentSpeed = state?.motion?.speed;
    if (!speedDirty && document.activeElement !== dom["robot-speed-input"]) dom["robot-speed-input"].value = Number.isFinite(currentSpeed) ? String(currentSpeed) : "";
    const enteredSpeed = Number(dom["robot-speed-input"].value);
    const validSpeed = dom["robot-speed-input"].value.trim() !== "" && Number.isFinite(enteredSpeed) && enteredSpeed >= 1 && enteredSpeed <= 1500;
    const speedChanged = enteredSpeed !== currentSpeed || Math.min(enteredSpeed, 200) !== state?.motion?.home_speed;
    dom["robot-speed-input"].disabled = !enabled;
    dom["robot-speed-button"].disabled = !enabled || !validSpeed || !speedChanged;
    text("robot-speed-current", `Normal ${speedText(currentSpeed)} · Cup flip ${speedText(state?.motion?.flip_speed)} · Sequence home ${speedText(state?.motion?.home_speed)}.`);
    renderHandoverControls();
    renderReleaseLocations();
    text("robot-home-speed", `Manual home: ${speedText(controls?.home_speed ?? (Number.isFinite(currentSpeed) ? Math.min(currentSpeed, 100) : null))}.`);
    hide("robot-status-button", !robotStatusAvailable());
    dom["robot-status-button"].disabled = !idle() || Boolean(state?.stop_in_flight) || !robotStatusAvailable();
    text("robot-status-button", state?.busy && state.operation === "robot_status" ? "Reading controller…" : "Refresh robot status");
    const actionButtons = Array.from(document.querySelectorAll("[data-robot-action]"));
    actionButtons.forEach((button) => {
      const action = button.dataset.robotAction;
      const supported = robotActionSupported(action);
      button.hidden = !supported;
      button.disabled = !enabled || !supported;
      button.dataset.defaultLabel ||= button.textContent;
      if (!button.dataset.arm) button.textContent = capabilityText(action, "label", button.dataset.defaultLabel);
      button.title = capabilityText(action, "description", capabilityText(action, "reason"));
    });
    hide("robot-home-tool", !robotActionSupported("home"));
    hide("robot-gripper-tool", !robotActionSupported("open") && !robotActionSupported("grip"));
    const capabilityMap = controls?.capabilities;
    const knownCapabilities = capabilityMap && typeof capabilityMap === "object" && !Array.isArray(capabilityMap);
    const supportedActions = actionButtons.some((button) => !button.hidden);
    text("robot-capability-notice", !knownCapabilities
      ? "Gateway controls have not been confirmed. Refresh robot status to check which actions are available. Local settings remain available."
      : !supportedActions ? "No robot actions are available from this gateway. Local settings and app recovery remain available."
      : "Only controls supported by this gateway are shown. Each robot action still needs a reviewed confirmation.");
    dom["robot-local-recovery-button"].disabled = !idle() || !state?.recovery_required;
    text("robot-controls-badge", !connected ? "Disconnected" : state?.recovery_required ? "Inspection needed" : state?.mode === "live" ? "Live controls" : "Preview only");
    dom["robot-controls-panel"].classList.toggle("live", state?.mode === "live");
    text("robot-rapid-state", snapshot?.execution_state ? humanize(snapshot.execution_state) : "Not read");
    text("robot-controller-state", snapshot?.controller_state ? humanize(snapshot.controller_state) : "Not read");
    text("robot-operation-mode", snapshot?.operation_mode ? humanize(snapshot.operation_mode) : "Not read");
    text("robot-collision-state", snapshot?.collision_state != null ? humanize(snapshot.collision_state) : "Unknown");
    text("robot-status-time", snapshot ? `${state?.mode === "demo" ? "Demo snapshot" : state?.mode === "dry_run" ? "Dry-run snapshot" : "Read from controller"}${snapshot.read_at ? ` · ${clockText(snapshot.read_at)}` : ""}. Refresh before recovery; this is not a continuous safety monitor.` : "Refresh to read the robot controller.");
    const statusError = asMessage(snapshot?.collision_error);
    text("robot-status-error", statusError);
    hide("robot-status-error", !statusError);
    const inhibit = !connected ? "Reconnect to the app before using robot controls. Displayed status may be stale." : state?.recovery_required ? "Inspect both arms and the cup, then use Acknowledge app recovery to clear the local block. This does not clear the robot’s collision fault." : localStopInhibit || state?.stop_requested ? "Stop is latched. New robot actions remain blocked until recovery is acknowledged." : state?.busy || posting ? "One action is in progress. Robot controls will be available when it finishes." : state?.mode !== "live" ? "Preview mode: these controls will not send physical robot commands." : "";
    text("robot-control-inhibit", inhibit);
    hide("robot-control-inhibit", !inhibit);
    hide("robot-control-proposal", !proposal || localStopInhibit || state?.stop_requested);
    dom["robot-control-cancel"].disabled = !enabled || !proposal;
    const proposalSupported = Boolean(proposal && robotActionSupported(proposal.action));
    dom["robot-control-ack"].disabled = !enabled || !proposalSupported;
    hide("robot-control-ack-row", !proposalSupported);
    hide("robot-control-confirm", !proposalSupported);
    if (!proposalSupported) dom["robot-control-ack"].checked = false;
    if (proposal) {
      const titles = {home: "Move to home", open: "Open gripper", grip: "Close gripper", collision_ack: "Acknowledge controller collision", rapid_reset: "Reset robot program (leave stopped)", rapid_start: "Initialize robot program"};
      text("robot-control-proposal-heading", `${capabilityText(proposal.action, "label", titles[proposal.action] || humanize(proposal.action))}${proposal.arm ? ` · ${proposal.arm === "both" ? "both arms" : `${proposal.arm} arm`}` : ""}`);
      text("robot-control-description", proposalSupported ? proposal.description
        : capabilityText(proposal.action, "reason", "This action is no longer available from the gateway. Cancel this preview and refresh robot status."));
      text("robot-control-proposal-speed", Number.isFinite(proposal.speed) ? `Reviewed speed: ${speedText(proposal.speed)}.` : "");
      hide("robot-control-proposal-speed", !Number.isFinite(proposal.speed));
      text("robot-control-confirm", state?.mode === "live" ? "Confirm robot action" : "Run preview only");
    }
    updateControlCountdown();
  }

  function cancelReleaseReview(message = "") {
    releaseReview = null;
    releaseMessage = message;
    dom["release-locations-ack"].checked = false;
  }

  function renderReleaseLocations() {
    const enabled = releaseActionable();
    if (releaseReview && (!enabled || releaseReview.context !== releaseContext(state))) {
      cancelReleaseReview("The controller or placement status changed. Review the locations again before clearing them.");
    }
    const locations = placementLocations();
    const occupied = locations.filter((location) => location.occupied).length;
    const reserved = locations.filter((location) => location.reserved).length;
    const blocked = locations.filter((location) => location.vision_blocked).length;
    text("release-locations-status", releaseMessage || (!connected ? "Reconnect to review the placement locations." : !gridReady() || !locations.length ? "Load a valid, nonempty placement grid before clearing locations." : !enabled ? "Wait for the current operation to finish before reviewing locations." : `${locations.length} locations · ${occupied} occupied · ${reserved} reserved · ${blocked} camera-blocked. Counts can overlap.`));
    dom["release-locations-button"].disabled = !enabled || Boolean(releaseReview);
    hide("release-locations-review", !releaseReview);
    dom["release-locations-ack"].disabled = !enabled || !releaseReview;
    dom["release-locations-confirm"].disabled = !enabled || !releaseReview || !dom["release-locations-ack"].checked;
    dom["release-locations-cancel"].disabled = !releaseReview;
    if (releaseReview) text("release-locations-description", `Mark all ${releaseReview.count} placement locations available: ${releaseReview.labels.join(", ")}. This clears their occupied, reserved and camera-blocked records. Their positions stay the same.`);
  }

  function renderHandoverControls() {
    const handover = state?.motion?.handover;
    const applied = handover?.offset_mm;
    const available = coordinatesAvailable(applied) && coordinatesAvailable(handover?.receive_mm);
    const enabled = controlActionable() && available;
    handoverAxes.forEach((axis) => {
      const input = dom[`robot-handover-${axis}`];
      if (!handoverDirty) input.value = Number.isFinite(applied?.[axis]) ? String(applied[axis]) : "";
      input.disabled = !enabled;
    });
    const entered = enteredHandoverOffset();
    const valid = handoverAxes.every((axis) => {
      const input = dom[`robot-handover-${axis}`];
      return input.value.trim() !== "" && Number.isFinite(entered[axis]) && entered[axis] >= -20 && entered[axis] <= 20 && input.validity.valid;
    });
    const changed = handoverDirty && (!valid || handoverAxes.some((axis) => entered[axis] !== applied?.[axis]));
    dom["robot-handover-apply"].disabled = !enabled || !valid || !changed;
    dom["robot-handover-reset"].disabled = !enabled || (!changed && handoverAxes.every((axis) => applied[axis] === 0));
    text("robot-handover-current", `Applied offset: ${coordinatesText(applied, true)}.`);
    text("robot-handover-target", `Right receiving target: ${coordinatesText(handover?.receive_mm)}.`);
    text("robot-handover-speed", `Receiving approach: ${speedText(handover?.approach_speed)}${handover?.adjusted ? " · limited to 25 mm/s while alignment is adjusted" : ""}.`);
    text("robot-handover-status", !available ? "Applied alignment unavailable." : changed ? valid ? "Unsaved edits. Apply alignment to use these offsets for future cup transfers." : "Unsaved edits. Enter each offset from −20 to +20 mm in 0.5 mm steps." : "Applied for this app session. Alignment resets to zero on restart.");
    dom["robot-handover-status"].classList.toggle("unsaved", changed);
  }

  function updateControlCountdown() {
    const proposal = state?.robot_controls?.proposal;
    const remaining = proposal ? Math.max(0, Math.ceil((controlDeadline - performance.now()) / 1000)) : 0;
    text("robot-control-countdown", remaining ? `${remaining}s to confirm` : "Preview expired");
    dom["robot-control-countdown"].classList.toggle("expired", remaining === 0);
    dom["robot-control-confirm"].disabled = !proposal || !robotActionSupported(proposal.action) || !controlActionable() || remaining === 0 || !dom["robot-control-ack"].checked;
  }

  function updateCountdown() {
    if (!state?.proposal) return;
    const remaining = Math.max(0, Math.ceil((proposalDeadline - performance.now()) / 1000));
    text("proposal-countdown", remaining ? `${remaining}s to confirm` : "Request expired");
    dom["proposal-countdown"].classList.toggle("expired", remaining === 0);
    dom["confirm-button"].disabled = !actionable() || remaining === 0 || !safeScan() || state.proposal.capture_id !== state.inventory?.capture;
  }

  function renderActivity() {
    const busy = Boolean(state?.busy || posting || stopPosting);
    const progress = state?.progress;
    text("activity-status", !connected ? "Offline" : busy ? "Working" : state?.recovery_required ? "Check required" : "Idle");
    dom["activity-indicator"].classList.toggle("busy", connected && busy);
    let title = state?.message || "Ready for your next action";
    let description = "Scan the workspace, then select a cup or type a command.";
    if (!state) title = "Waiting for the controller";
    else if (!connected) { title = "Controller disconnected"; description = "This display may be stale. Active motion may continue."; }
    else if (localStopInhibit || state.stop_requested) { title = state.stop?.state === "unconfirmed" ? "Software stop unconfirmed" : "Stop requested · new commands blocked"; description = state.stop?.message || "Waiting for the controller response. Nothing resumes automatically."; }
    else if (busy) { title = progress?.step ? humanize(progress.step) : humanize(state.operation || "Processing request"); description = state.mode === "live" ? "Follow the controller status. Use physical safety controls if needed." : "Processing locally. No physical movement is enabled."; }
    else if (state.recovery_required) { title = "Operator inspection required"; description = "Acknowledge recovery only after checking both arms and the cup."; }
    else if (state.robot_controls?.proposal) { title = "Review the robot control action"; description = "Inspect both arms and the held cup, then confirm the preview in Robot controls. Nothing has been sent to the robot yet."; }
    else if (state.proposal) { title = "Waiting for your confirmation"; description = `Review the action for ${state.proposal.action === "goto_all" ? `cups ${(state.proposal.cup_ids || []).join(", ")}` : `cup ${state.proposal.cup_id}`}. No movement has been requested yet.`; }
    else if (state.error) { title = "Request blocked or interrupted"; description = asMessage(state.error); }
    else if (state.inventory && !state.scan_usable) description = "Capture a fresh scan before selecting another cup.";
    else if (state.inventory) description = "Choose a detected cup to see its available actions.";
    text("operation-title", asMessage(title));
    text("operation-description", description);
    text("operation-symbol", !connected ? "○" : state?.error || state?.recovery_required || localStopInhibit ? "!" : busy ? "↻" : state?.proposal ? "◇" : "✓");
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
    const batch = state?.batch || progress?.batch;
    hide("batch-progress", !batch);
    if (batch) text("batch-progress", `Cup ${batch.current} of ${batch.total}${batch.cup_id != null ? ` · selected ID ${batch.cup_id}` : ""} · ${Array.isArray(batch.completed_cups) ? batch.completed_cups.length : Number(batch.completed_cups) || 0} completed`);
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
    dom["start-button"].disabled = !canAct || pending || Boolean(state?.recovery_required);
    dom["stop-button"].disabled = !canStop();
    dom["status-button"].disabled = !connected || quickPosting;
    dom["help-button"].disabled = !connected || quickPosting;
    dom["scan-button"].disabled = !canAct || Boolean(state?.recovery_required);
    text("scan-button-label", state?.busy && state.operation === "scan" ? "Scanning…" : "Scan workspace");
    const command = dom["command-input"].value.trim();
    const readOnly = ["status", "help"].includes(command.toLowerCase());
    dom["command-input"].disabled = !connected || !state?.ready;
    dom["command-button"].disabled = isStop(command) ? !canStop() : readOnly ? !connected || quickPosting : !canAct || pending || Boolean(state?.recovery_required) || !command;
    document.querySelectorAll("[data-example]").forEach((button) => { button.disabled = !canAct || pending || (!state?.llm_enabled && !button.dataset.direct) || Boolean(state?.recovery_required); });
    dom["hover-button"].disabled = !canAct || pending || !safeScan() || !cup?.selectable;
    dom["pick-button"].disabled = !canAct || pending || !safeScan() || !cup?.pick_available || !gridReady() || !availableLocations().length;
    const pickable = pickableCups();
    const capacity = availableLocations().length;
    hide("batch-tools", !pickable.length);
    text("batch-summary", `${pickable.length} cup${pickable.length === 1 ? "" : "s"} available for picking`);
    text("batch-hint", !safeScan() ? "Scan again before selecting a batch." : !gridReady() ? "Load a valid placement grid before picking." : !capacity ? "No available placement locations remain." : capacity < pickable.length ? `Room for ${capacity} of ${pickable.length} cups. The preview selects the highest confidence cups that fit.` : "Review every cup and destination before confirming.");
    dom["batch-button"].disabled = !canAct || pending || !safeScan() || !pickable.length || !gridReady() || !capacity;
    dom["cancel-button"].disabled = !canAct || !pending;
    dom["recovery-button"].disabled = !idle() || !dom["recovery-ack"].checked || !state?.recovery_required;
    dom["recovery-ack"].disabled = !idle();
    document.querySelectorAll(".cup-card").forEach((card) => { card.disabled = !connected || Boolean(state?.busy) || posting || pending; });
    text("model-label", state?.llm_enabled ? `${state.model || "Local model"} · local` : "Terminal commands enabled");
    text("command-hint", "Start prepares a batch. Stop, status and help also work while the controller is busy. Every movement requires confirmation.");
    dom["command-input"].placeholder = state?.llm_enabled ? "Pick cup two…" : "Try goto 2 or hover 2…";
    updateCountdown();
    renderRobotControls();
    renderSavedCups();
  }

  function render() {
    renderMode();
    renderConnection();
    renderInference();
    renderStop();
    renderInventory();
    renderSelection();
    renderPlacements();
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
      if (releaseReview && (!incoming.ready || incoming.busy || incoming.stop_in_flight || releaseReview.context !== releaseContext(incoming))) {
        cancelReleaseReview("The controller or placement status changed. Review the locations again before clearing them.");
      }
      if (state && incoming.instance_id !== state.instance_id) {
        selectedId = null;
        selectedCapture = null;
        proposalDeadline = 0;
        controlDeadline = 0;
        speedDirty = false;
        handoverDirty = false;
        dom["recovery-ack"].checked = false;
        dom["robot-control-ack"].checked = false;
        transientError = "The controller restarted. Review its current state and scan again before selecting a cup.";
      }
      if (state?.recovery_required !== incoming.recovery_required) dom["recovery-ack"].checked = false;
      if (state?.robot_controls?.proposal?.token !== incoming.robot_controls?.proposal?.token) {
        dom["robot-control-ack"].checked = false;
        if (incoming.robot_controls?.proposal) dom["robot-controls-panel"].open = true;
      }
      if (incoming.stop_requested && !state?.stop_requested) {
        quickGeneration += 1;
        hide("quick-panel", true);
      }
      if (incoming.stop_requested || (state?.stop_requested && !incoming.stop_requested)) localStopInhibit = false;
      state = incoming;
      connected = true;
      proposalDeadline = state.proposal && Number.isFinite(state.proposal.expires_in) ? performance.now() + Math.max(0, state.proposal.expires_in) * 1000 : 0;
      controlDeadline = state.robot_controls?.proposal && Number.isFinite(state.robot_controls.proposal.expires_in) ? performance.now() + Math.max(0, state.robot_controls.proposal.expires_in) * 1000 : 0;
      render();
    } catch (error) {
      if (number < appliedPoll) return;
      appliedPoll = number;
      connected = false;
      if (releaseReview) cancelReleaseReview("Connection lost. Review the current placement locations after reconnecting.");
      if (!state) transientError = "Could not reach the local controller. Keep the web app process running and this page will reconnect.";
      render();
    } finally {
      clearTimeout(timeout);
    }
  }

  async function post(path, payload = {}) {
    const stopping = path === "/api/stop";
    const adminMutation = ["/api/speed", "/api/handover_offset", "/api/control_preview", "/api/control_confirm", "/api/control_cancel", "/api/save_cup", "/api/pick_saved", "/api/forget_cup"].includes(path);
    const permitted = stopping ? canStop() : path === "/api/release_locations" ? releaseActionable() : ["/api/recovery", "/api/robot_status"].includes(path) ? idle() : adminMutation ? controlActionable() : actionable();
    if (!permitted || !state?.csrf_token || !robotRequestSupported(path, payload)) return;
    if (releaseReview) cancelReleaseReview();
    const instance = state.instance_id;
    quickGeneration += 1;
    hide("quick-panel", true);
    if (stopping) {
      stopPosting = true;
      localStopInhibit = true;
      proposalDeadline = 0;
      controlDeadline = 0;
      dom["robot-control-ack"].checked = false;
    } else posting = true;
    transientError = "";
    render();
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
      if (path === "/api/speed") speedDirty = false;
      if (path === "/api/handover_offset") handoverDirty = false;
      await readState();
      if (state?.instance_id !== instance) transientError = "The controller restarted during this request. Check the current state; the request was not retried.";
    } catch (error) {
      transientError = ["AbortError", "UnknownOutcomeError"].includes(error.name) || error instanceof TypeError ? "Request outcome unknown: the connection was interrupted or its response was unreadable. Check the controller before continuing. The request was not retried." : error.message;
      await readState();
    } finally {
      clearTimeout(timeout);
      if (stopping) stopPosting = false;
      else posting = false;
      render();
    }
  }

  async function showQuick(kind) {
    if (!connected || quickPosting) return;
    quickPosting = true;
    const generation = quickGeneration;
    renderControls();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4500);
    try {
      const response = await fetch(`/api/${kind}`, {cache: "no-store", credentials: "same-origin", signal: controller.signal});
      if (!response.ok) throw new Error(`${humanize(kind)} unavailable (${response.status}).`);
      const result = await response.json();
      if (generation !== quickGeneration) return;
      text("quick-title", kind === "help" ? "Available commands" : `Status snapshot · ${clockText(new Date().toISOString())}`);
      const lines = kind === "help" ? result.message : [
        result.source || "Cached application status", `Mode: ${humanize(result.mode)}`,
        `Controller: ${result.busy ? "Busy" : "Idle"}${result.operation ? ` · ${humanize(result.operation)}` : ""}`,
        `Software stop: ${result.stop_requested ? humanize(result.stop?.state || "requested") : "Not requested"}`,
        `Recovery inspection: ${result.recovery_required ? "Required" : "Not required"}`,
        `Scan: ${result.scan_usable ? "Usable" : "Scan required"}`,
        result.progress ? `Sequence: ${result.progress.completed} / ${result.progress.total} steps` : "",
        result.message || "", result.error ? `Attention: ${result.error}` : ""
      ].filter(Boolean).join("\n");
      text("quick-content", lines);
      hide("quick-panel", false);
    } catch (error) {
      transientError = error.name === "AbortError" ? `${humanize(kind)} request timed out.` : error.message;
    } finally {
      clearTimeout(timeout);
      quickPosting = false;
      render();
    }
  }

  dom["start-button"].addEventListener("click", () => post("/api/start"));
  dom["stop-button"].addEventListener("click", () => post("/api/stop"));
  dom["status-button"].addEventListener("click", () => showQuick("status"));
  dom["help-button"].addEventListener("click", () => showQuick("help"));
  dom["quick-close"].addEventListener("click", () => hide("quick-panel", true));
  dom["scan-button"].addEventListener("click", () => post("/api/scan"));
  dom["batch-button"].addEventListener("click", () => {
    if (!dom["batch-button"].disabled) post("/api/batch", { capture_id: state.inventory.capture });
  });
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
    if (isStop(command)) { post("/api/stop"); return; }
    if (["status", "help"].includes(command.toLowerCase())) { showQuick(command.toLowerCase()); return; }
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
  dom["save-cup-name"].addEventListener("input", renderSavedCups);
  dom["save-cup-form"].addEventListener("submit", (event) => {
    event.preventDefault();
    const cup = currentCup();
    if (!cup || dom["save-cup-button"].disabled) return;
    post("/api/save_cup", { capture_id: selectedCapture, cup_id: cup.id, name: dom["save-cup-name"].value.trim() });
  });
  dom["proposal-saved-image"].addEventListener("error", () => {
    dom["proposal-saved-image"].hidden = true;
    dom["proposal-saved-image-fallback"].hidden = false;
  });
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
  dom["robot-speed-input"].addEventListener("input", () => { speedDirty = true; renderRobotControls(); });
  dom["release-locations-button"].addEventListener("click", () => {
    if (dom["release-locations-button"].disabled || !releaseActionable()) return;
    releaseReview = {
      fingerprint: state.placement_grid.fingerprint,
      context: releaseContext(state),
      count: placementLocations().length,
      labels: placementLocations().map((location) => `P${location.id}`),
    };
    releaseMessage = "";
    dom["release-locations-ack"].checked = false;
    renderReleaseLocations();
  });
  dom["release-locations-ack"].addEventListener("change", renderReleaseLocations);
  dom["release-locations-cancel"].addEventListener("click", () => {
    cancelReleaseReview();
    renderReleaseLocations();
  });
  dom["release-locations-confirm"].addEventListener("click", () => {
    if (dom["release-locations-confirm"].disabled || !releaseReview || !releaseActionable() || releaseReview.context !== releaseContext(state)) return;
    post("/api/release_locations", { fingerprint: releaseReview.fingerprint, acknowledged: true });
  });
  dom["robot-speed-form"].addEventListener("submit", (event) => {
    event.preventDefault();
    if (!dom["robot-speed-button"].disabled) post("/api/speed", { speed: Number(dom["robot-speed-input"].value) });
  });
  handoverAxes.forEach((axis) => dom[`robot-handover-${axis}`].addEventListener("input", () => { handoverDirty = true; renderHandoverControls(); }));
  dom["robot-handover-form"].addEventListener("submit", (event) => {
    event.preventDefault();
    if (!dom["robot-handover-apply"].disabled) post("/api/handover_offset", enteredHandoverOffset());
  });
  dom["robot-handover-reset"].addEventListener("click", () => {
    if (!dom["robot-handover-reset"].disabled) post("/api/handover_offset", { x: 0, y: 0, z: 0 });
  });
  dom["robot-status-button"].addEventListener("click", () => { if (!dom["robot-status-button"].disabled) post("/api/robot_status"); });
  document.querySelectorAll("[data-robot-action]").forEach((button) => button.addEventListener("click", () => {
    if (!button.disabled && !button.hidden && robotActionSupported(button.dataset.robotAction)) post("/api/control_preview", { action: button.dataset.robotAction, arm: button.dataset.arm || null });
  }));
  dom["robot-local-recovery-button"].addEventListener("click", () => {
    if (dom["robot-local-recovery-button"].disabled) return;
    dom["recovery-panel"].scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
    dom["recovery-ack"].focus({ preventScroll: true });
  });
  dom["robot-control-ack"].addEventListener("change", updateControlCountdown);
  dom["robot-control-confirm"].addEventListener("click", () => {
    const proposal = state?.robot_controls?.proposal;
    if (!proposal || dom["robot-control-confirm"].disabled) return;
    post("/api/control_confirm", { token: proposal.token, acknowledged: true });
    dom["robot-control-ack"].checked = false;
  });
  dom["robot-control-cancel"].addEventListener("click", () => { if (!dom["robot-control-cancel"].disabled) post("/api/control_cancel"); });
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
  setInterval(updateControlCountdown, 250);
  async function poll() {
    await readState();
    setTimeout(poll, 700);
  }
  poll();
})();
