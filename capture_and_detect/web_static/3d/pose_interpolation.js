/* Smooth playback of timestamped joint readings (no three.js, unit-tested).
 *
 * The dashboard gets a reading per arm a few times a second. Instead of
 * chasing the newest one (which moves in steps), the view plays the readings
 * back a little behind real time and interpolates between them, as games do
 * with network positions. Interpolation is a monotone cubic (Fritsch-Carlson),
 * so the arm moves smoothly and never overshoots a reading, e.g. when it stops.
 * Across a gap longer than maxGap (missed readings) it holds the last reading
 * and then jumps, instead of inventing motion that was never measured. After
 * the newest reading it holds still: it never extrapolates.
 */

export class JointTrack {
  constructor({ maxGap = 1.0, keep = 32 } = {}) {
    this.maxGap = maxGap;
    this.keep = keep;
    this.samples = [];   // [{ t, q: number[] }], t ascending (server seconds)
  }

  push(t, q) {
    const last = this.samples.at(-1);
    if (!Number.isFinite(t) || !Array.isArray(q) || !q.length || !q.every(Number.isFinite)
        || (last && (t <= last.t || q.length !== last.q.length))) return false;
    this.samples.push({ t, q: q.slice() });
    if (this.samples.length > this.keep) this.samples.shift();
    return true;
  }

  get latest() { return this.samples.at(-1) || null; }

  /** Median time between recent readings, or null with fewer than 3. */
  interval() {
    const s = this.samples.slice(-9);
    const gaps = [];
    for (let i = 1; i < s.length; i++) {
      const gap = s[i].t - s[i - 1].t;
      if (gap <= this.maxGap) gaps.push(gap);
    }
    if (gaps.length < 2) return null;
    gaps.sort((a, b) => a - b);
    return gaps[Math.floor(gaps.length / 2)];
  }

  /** Joint values at server time t, or null before the first reading. */
  sample(t) {
    const s = this.samples;
    if (!s.length) return null;
    if (t <= s[0].t) return s[0].q.slice();
    const last = s.length - 1;
    if (t >= s[last].t) return s[last].q.slice();
    let i = last - 1;
    while (i > 0 && s[i].t > t) i--;
    const a = s[i], b = s[i + 1];
    const h = b.t - a.t;
    if (h > this.maxGap) return a.q.slice();
    const before = i > 0 && a.t - s[i - 1].t <= this.maxGap ? s[i - 1] : null;
    const after = i + 2 <= last && s[i + 2].t - b.t <= this.maxGap ? s[i + 2] : null;
    const u = (t - a.t) / h, u2 = u * u, u3 = u2 * u;
    const h00 = 2 * u3 - 3 * u2 + 1, h10 = u3 - 2 * u2 + u, h01 = -2 * u3 + 3 * u2, h11 = u3 - u2;
    return a.q.map((p1, j) => {
      const p2 = b.q[j];
      const d = (p2 - p1) / h;
      const slope = (prev, next, dt) => (next - prev) / dt;
      let m1 = before ? slope(before.q[j], p2, b.t - before.t) : d;
      let m2 = after ? slope(p1, after.q[j], after.t - a.t) : d;
      // Fritsch-Carlson: flat segment stays flat; tangents never point
      // against the segment or exceed 3x its slope, so no overshoot.
      if (d === 0) { m1 = 0; m2 = 0; } else {
        if (Math.sign(m1) !== Math.sign(d)) m1 = 0;
        if (Math.sign(m2) !== Math.sign(d)) m2 = 0;
        const limit = 3 * Math.abs(d);
        m1 = Math.sign(m1) * Math.min(Math.abs(m1), limit);
        m2 = Math.sign(m2) * Math.min(Math.abs(m2), limit);
      }
      return h00 * p1 + h10 * h * m1 + h01 * p2 + h11 * h * m2;
    });
  }
}

/**
 * Maps the browser clock to the dashboard's monotonic clock. The offset with
 * the smallest network delay over the recent window is the best estimate.
 */
export class ClockSync {
  constructor(window = 40) {
    this.window = window;
    this.offsets = [];
  }

  observe(clientSeconds, serverNow) {
    if (!Number.isFinite(serverNow)) return;
    this.offsets.push(clientSeconds - serverNow);
    if (this.offsets.length > this.window) this.offsets.shift();
  }

  get ready() { return this.offsets.length > 0; }

  serverTime(clientSeconds) {
    return clientSeconds - Math.min(...this.offsets);
  }
}

// Each worker has an unrelated monotonic clock. Pair only scene and pose
// snapshots from the same dashboard selection and controller instance.
export class PoseSource {
  constructor() { this.identity = null; }

  scene(state) {
    const clock = state?.pose_clock_id ?? state?.instance_id;
    const identity = typeof clock === "string" && clock
      ? JSON.stringify([state?.devices?.active_id ?? null, state?.device_epoch ?? null, clock]) : null;
    const changed = identity !== this.identity;
    this.identity = identity;
    return changed;
  }

  accepts(pose) {
    return this.identity !== null && typeof pose?.clock_id === "string"
      && JSON.stringify([pose?.device_id ?? null, pose?.device_epoch ?? null, pose.clock_id]) === this.identity;
  }
}
