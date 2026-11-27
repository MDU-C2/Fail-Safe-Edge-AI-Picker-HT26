/* One cached GET stream. The observer has no command or control endpoints. */
(() => {
  "use strict";
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value, digits = 1) => finite(value) ? value.toFixed(digits) : "—";
  const label = (value) => ({ primary: "Primary computer", secondary: "Raspberry Pi", tertiary: "Lynx" }[value] || (typeof value === "string" ? value : "Unknown"));
  const title = (value) => typeof value === "string" && value ? value[0].toUpperCase() + value.slice(1) : "Unknown";

  function reading(snapshot, key, elapsed = 0, online = true) {
    const section = snapshot?.sections?.[key];
    const age = finite(section?.age_seconds) ? section.age_seconds + elapsed : null;
    const fresh = Boolean(online && !["offline", "unconfigured"].includes(snapshot?.status)
      && section?.state === "ok" && section.stale === false && age !== null && age <= (snapshot?.stale_after ?? 20));
    let stamp = age === null ? "Not received" : `${Math.floor(age)}s ago`;
    if (section?.state === "unsupported") stamp = "Not supported";
    else if (section?.state === "auth_error") stamp = "Access rejected";
    else if (section?.state === "skipped") stamp = age === null ? "Controller unreachable" : `Last known · ${stamp}`;
    else if (!fresh && age !== null) stamp = `Last known · ${stamp}`;
    else if (section?.state === "unavailable") stamp = "Unavailable";
    return { data: section?.data || {}, fresh, stamp, age, state: section?.state };
  }

  function queueProgress(queue) {
    if (!Number.isInteger(queue.length) || queue.length < 0 || queue.length > 64 || !Array.isArray(queue.steps)
        || queue.steps.length !== queue.length) return null;
    const completed = queue.steps.filter((step) => step?.state === "done").length;
    return { completed, total: queue.length, percent: queue.length ? completed / queue.length * 100 : 0 };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { reading, queueProgress, label, number };
  if (typeof document === "undefined") return;
  const root = document.getElementById("view-observer");
  if (!root) return;
  const el = (id) => document.getElementById(`obs-${id}`);
  const text = (id, value) => { el(id).textContent = String(value ?? "—"); };
  let snapshot = null, receivedAt = 0, online = false, running = false, generation = 0;
  let timer = null, abort = null, lastEvents = "", lastTasks = "";

  function pill(id, value, tone = "") {
    text(id, value);
    el(id).dataset.tone = tone;
  }
  const date = (value) => {
    const parsed = new Date(value);
    return value && Number.isFinite(parsed.valueOf()) ? parsed.toLocaleString() : "—";
  };
  const elapsedTime = (seconds) => finite(seconds) ? `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m` : "—";

  function render() {
    const elapsed = snapshot ? Math.max(0, (performance.now() - receivedAt) / 1000) : 0;
    const get = (key) => reading(snapshot, key, elapsed, online);
    const health = get("health"), control = get("control"), queue = get("queue"), camera = get("camera"), arms = get("arms");
    const state = !online && snapshot ? "offline" : snapshot?.status || "connecting";
    root.dataset.status = state;
    text("connection", ({ live: "Connected", degraded: "Needs attention", offline: "Connection lost", auth_error: "Access rejected", demo: "Simulated demo", unconfigured: "Not configured" })[state] || "Connecting");
    text("message", !online && snapshot ? "Observer connection lost. Displayed readings are last known values." : snapshot?.message || "Waiting for gateway readings.");
    text("source", snapshot?.gateway || "No gateway configured on this dashboard server.");
    text("gateway-state", health.fresh ? title(health.data.status) : snapshot ? "Unavailable" : "Connecting");
    const latency = snapshot?.sections?.health?.elapsed_ms;
    text("gateway-detail", health.fresh ? `${finite(latency) ? Math.round(latency) + " ms · " : ""}${health.stamp}` : health.stamp);
    text("owner", control.fresh ? control.data.active ? label(control.data.client_id) : "Lease free" : "Unknown");
    text("owner-detail", control.fresh ? control.data.active ? `Priority ${control.data.priority ?? "—"}` : "No control lease held" : control.data.active ? `Last: ${label(control.data.client_id)} · ${control.stamp}` : control.stamp);
    const robot = health.data.robot_controller || {}, sensor = health.data.camera || {};
    text("robot-state", health.fresh ? robot.reachable === true ? ({ motoron: "Motors on", motoroff: "Motors off" }[robot.controller_state] || title(robot.controller_state)) : robot.reachable === false ? "Unreachable" : "Unknown" : "Unknown");
    text("robot-detail", health.fresh ? `${robot.operation_mode || "Mode unknown"} · RAPID ${robot.rapid_execution_state || "unknown"}` : health.stamp);
    text("camera-state", health.fresh ? sensor.connected === true ? sensor.ok === false ? "Needs attention" : "Connected" : sensor.connected === false ? "Disconnected" : "Unknown" : "Unknown");
    text("camera-detail", health.fresh ? `${sensor.provider || "Sensor"} · ${finite(sensor.frame_age_seconds) ? number(sensor.frame_age_seconds, 2) + "s at sample" : "frame age unknown"}` : health.stamp);
    for (const role of ["primary", "secondary", "tertiary"]) {
      const active = control.fresh && control.data.active === true && control.data.client_id === role;
      const card = el(`role-${role}`);
      card.dataset.active = String(active);
      card.querySelector("b").textContent = active ? "In control" : control.fresh ? "Not holding control" : "Unknown";
    }
    text("lease-note", control.fresh && control.data.active
      ? `Lease held by ${label(control.data.client_id)}. ${control.data.expires_at ? "Reported expiry: " + date(control.data.expires_at) + ". " : ""}Worker availability is not reported here.`
      : "Reads the current lease. Worker availability is not reported by this endpoint.");

    const q = queue.data;
    pill("queue-badge", queue.fresh ? title(q.state) : queue.state === "unsupported" ? "Not supported" : "Last known / unavailable",
      queue.fresh && q.state === "failed" ? "bad" : queue.fresh && ["done", "running"].includes(q.state) ? "good" : "warn");
    text("queue-owner", q.owner ? label(q.owner) : q.state === "idle" ? "No queue" : "—");
    const progress = queueProgress(q);
    text("queue-count", progress ? `${progress.completed} / ${progress.total}` : "—");
    const activeSteps = Array.isArray(q.steps) ? q.steps.filter((item) => item?.state === "running").map((item) => item.name).filter(Boolean) : [];
    text("queue-step", activeSteps.length ? activeSteps.join(" + ").replace(/_/g, " ") : q.state === "done" ? "Finished" : q.state === "idle" ? "Idle" : "—");
    el("progress-fill").style.width = `${progress?.percent || 0}%`;
    if (progress) el("progress").setAttribute("aria-valuenow", String(Math.round(progress.percent)));
    else el("progress").removeAttribute("aria-valuenow");
    el("progress").setAttribute("aria-valuetext", `${queue.fresh ? "" : "Last known: "}${progress ? `${progress.completed} of ${progress.total} steps complete` : "Progress unavailable"}`);

    for (const arm of ["left", "right"]) {
      const pose = arms.data[arm] || {}, grip = get(`gripper_${arm}`), job = get(`job_${arm}`);
      for (const axis of ["x", "y", "z"]) text(`${arm}-${axis}`, number(pose[axis]));
      text(`${arm}-rotation`, Array.isArray(pose.rotation) ? pose.rotation.map((n) => number(n) + "°").join(" / ") : "—");
      text(`${arm}-gripper`, finite(grip.data.position) ? `${number(grip.data.position)} mm` : "—");
      text(`${arm}-tool`, pose.tool || pose.wobj ? `${pose.tool || "—"} / ${pose.wobj || "—"}` : "—");
      text(`${arm}-config`, ["cf1", "cf4", "cf6", "cfx"].map((k) => finite(pose[k]) ? pose[k] : "—").join(", "));
      pill(`job-${arm}`, job.fresh ? job.data.message || "Unknown" : "Unknown", job.fresh && job.data.message === "DONE" ? "good" : "");
      text(`${arm}-ages`, `Gripper: ${grip.stamp} · Job: ${job.stamp}`);
    }
    text("camera-provider", sensor.provider);
    text("frame-age", finite(sensor.frame_age_seconds) ? `${number(sensor.frame_age_seconds, 2)} s · ${health.stamp}` : "—");
    text("camera-sequence", camera.data.sequence);
    for (const kind of ["rgb", "depth"]) text(kind, camera.data[`${kind}_available`] === true
      ? `${camera.data[`${kind}_width`] ?? "—"} × ${camera.data[`${kind}_height`] ?? "—"} ${camera.data[`${kind}_format`] || ""}`
      : camera.data[`${kind}_available`] === false ? "Unavailable" : "—");
    const intrinsics = get("intrinsics"), calibration = get("calibration");
    text("focal", `${number(intrinsics.data.fx)} / ${number(intrinsics.data.fy)}`);
    text("principal", `${number(intrinsics.data.cx)} / ${number(intrinsics.data.cy)}`);
    text("calibration", calibration.state === "unsupported" ? "None stored" : date(calibration.data.updated_at));
    text("calibration-age", `Intrinsics: ${intrinsics.stamp} · Calibration: ${calibration.stamp}`);
    text("version", health.data.version);
    text("uptime", elapsedTime(health.data.uptime_seconds));
    text("security", health.data.security_mode);
    const legacy = get("robot");
    text("legacy", legacy.fresh ? legacy.data.connected === true ? "Connected" : legacy.data.connected === false ? "Disconnected" : "Unknown" : legacy.stamp);
    root.querySelectorAll("[data-obs-age]").forEach((node) => {
      const r = get(node.dataset.obsAge);
      node.textContent = r.stamp;
      node.dataset.stale = String(!r.fresh);
    });
    const tasks = get("rapid").data.tasks || [];
    const taskKey = JSON.stringify(tasks);
    if (taskKey !== lastTasks) {
      lastTasks = taskKey;
      const rows = tasks.map((task) => {
        const row = document.createElement("tr");
        for (const value of [task.name, task.execution_state, task.state, (task.modules || []).join(", ")]) {
          const cell = document.createElement("td"); cell.textContent = value || "—"; row.append(cell);
        }
        return row;
      });
      if (!rows.length) {
        const row = document.createElement("tr"), cell = document.createElement("td");
        cell.colSpan = 4; cell.textContent = "No task readings available."; row.append(cell); rows.push(row);
      }
      el("tasks").replaceChildren(...rows);
    }
    const events = snapshot?.events || [];
    text("event-count", `${events.length} changes`);
    const eventKey = JSON.stringify(events);
    if (eventKey !== lastEvents) {
      lastEvents = eventKey;
      const rows = events.map((event) => {
        const row = document.createElement("li"), time = document.createElement("time"), source = document.createElement("b"), detail = document.createElement("span");
        const at = new Date(event.at); time.textContent = Number.isFinite(at.valueOf()) ? at.toLocaleTimeString() : "—";
        time.dateTime = event.at; source.textContent = event.source; detail.textContent = event.message;
        row.append(time, source, detail); return row;
      });
      if (!rows.length) {
        const row = document.createElement("li"); row.className = "obs-empty";
        row.textContent = "Changes in connection, ownership and queue status will appear here."; rows.push(row);
      }
      el("events").replaceChildren(...rows);
    }
  }

  async function poll(current) {
    if (!running || current !== generation) return;
    abort = new AbortController();
    const requestAbort = abort;
    const timeout = setTimeout(() => requestAbort.abort(), 4500);
    try {
      const response = await fetch("/api/observer", { cache: "no-store", credentials: "same-origin", signal: requestAbort.signal });
      if (!response.ok || response.redirected) throw new Error("Observer unavailable");
      const data = await response.json();
      if (data?.read_only !== true || !data.sections || !Array.isArray(data.events)) throw new Error("Invalid observer response");
      if (!running || current !== generation) return;
      snapshot = data; receivedAt = performance.now(); online = true;
    } catch (_) {
      if (!running || current !== generation) return;
      online = false;
      if (!snapshot) snapshot = { status: "offline", sections: {}, events: [], message: "Observer unavailable." };
    } finally { clearTimeout(timeout); }
    if (running && current === generation) {
      render();
      timer = setTimeout(() => poll(current), online ? 1000 : 3000);
    }
  }

  function sync() {
    const wanted = !document.hidden && !root.hidden;
    if (wanted === running) return;
    running = wanted; generation += 1;
    clearTimeout(timer); abort?.abort();
    if (running) { render(); poll(generation); }
  }
  new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["hidden"] });
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", () => { running = false; generation += 1; clearTimeout(timer); abort?.abort(); });
  window.addEventListener("pageshow", sync);
  setInterval(() => { if (running) render(); }, 1000);
  sync();
})();
