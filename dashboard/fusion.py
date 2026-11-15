"""DripGuard sensor fusion and diagnostics.

Input : firmware status messages ({"type":"s", ...}), one at a time.
Output: an Assessment - flow estimates, sensor health, auto-calibrated drop
        volume, and a list of findings, each separating what was OBSERVED
        from what MIGHT explain it.

Design principles
- Compare like with like: both flows are least-squares slopes over the SAME
  time window (cumulative drop count vs time, reservoir mass vs time), so a
  real change in flow moves both estimates identically and doesn't look
  like a disagreement.
- The fast sensor alarms, the slow sensor confirms: when drops stop, an alarm
  is raised at once. The weight then decides between "flow really stopped"
  and "optical sensor stopped seeing drops" by comparing the mass lost since
  the last drop with what the previous flow would have removed.
- Never claim more than the data supports: an estimate is used only while its
  sensor is healthy, and "unknown" is a legitimate answer.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

# At-rest weight-flow error (1 std, mL/min) versus estimation window (s), measured on the real
# rig on 14 Nov (10.6 min, nothing flowing). The scale wanders slowly (about +-0.5 g over tens of
# seconds), so short windows see "flow" that isn't there. Replace with "Measure scale noise".
DEFAULT_NOISE_PROFILE = {30.0: 1.08, 60.0: 0.63, 90.0: 0.38, 120.0: 0.25, 180.0: 0.14}


def noise_floor(profile: dict, window_s: float) -> float:
    """Log-log interpolation of the at-rest flow error for a window length (mL/min)."""
    pts = sorted((float(w), float(v)) for w, v in profile.items() if v and v > 0 and w > 0)
    if not pts:
        return 0.0
    w = max(window_s, 1.0)
    if len(pts) == 1:
        return pts[0][1] * (pts[0][0] / w)
    if w <= pts[0][0]:
        (w0, v0), (w1, v1) = pts[0], pts[1]
    elif w >= pts[-1][0]:
        (w0, v0), (w1, v1) = pts[-2], pts[-1]
    else:
        for (w0, v0), (w1, v1) in zip(pts, pts[1:]):
            if w0 <= w <= w1:
                break
    k = math.log(v1 / v0) / math.log(w1 / w0)
    return v0 * (w / w0) ** k

LEVELS = ["ok", "info", "warning", "sensor", "unknown", "alarm"]
LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


@dataclass
class Settings:
    drop_factor: float = 20.0          # nominal drops/mL from the IV set package
    expected_flow: float | None = 3.0  # mL/min; None disables low/high-flow checks
    tolerance: float = 0.30            # allowed fractional deviation from expected
    low_volume_ml: float = 50.0
    use_autocal: bool = True
    rho: float = 0.998                 # g/mL
    window_s: float = 120.0            # aligned optical-vs-weight comparison window
    long_window_s: float = 240.0       # second, more precise window for slow-developing faults
    fast_window_s: float = 12.0        # responsive optical estimate for display/alerts
    stop_after_s: float = 8.0          # no drops this long => drops stopped (minimum)
    stop_after_max_s: float = 30.0     # ... stretched for slow drips, up to this
    stop_guard_s: float = 3.0          # ignore weight this long after the last drop (hand on the clamp)
    anomaly_hold_s: float = 10.0       # optical anomaly flag duration
    settle_hold_s: float = 5.0         # weight treated as unreliable after a disturbance
    min_snr: float = 6.0
    disagree_hold_s: float = 4.0       # disagreement must persist this long
    stop_max_post_s: float = 150.0     # longest stretch of post-stop weight data used for the verdict
    noise_profile: dict = field(default_factory=lambda: dict(DEFAULT_NOISE_PROFILE))


@dataclass
class Finding:
    level: str
    title: str
    observed: str
    causes: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    t: float
    level: str
    headline: str
    flow_state: str                    # normal | low | high | stopped | unknown
    findings: list[Finding]
    q_o: float | None = None           # optical flow, mL/min (aligned window)
    q_o_fast: float | None = None      # optical flow, mL/min (short window)
    q_o_se: float | None = None
    q_w: float | None = None           # weight flow, mL/min (aligned window)
    q_w_se: float | None = None
    q_best: float | None = None
    q_source: str = ""
    opt_health: str = ""
    w_health: str = ""
    drop_factor_used: float = 20.0
    cal_df: float | None = None        # auto-calibrated drops/mL
    cal_df_se: float | None = None
    remaining_ml: float | None = None
    minutes_left: float | None = None
    stop_verdict: str | None = None    # pending | confirmed | flow_continues | unverifiable


def linfit(xs, ys):
    """Least squares y = a + b x. Returns (b, a, resid_sd, b_se, n) or None."""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    rss = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    sd = math.sqrt(rss / (n - 2)) if n > 2 else 0.0
    return b, a, sd, sd / math.sqrt(sxx), n


def measure_noise_profile(rows: list[dict], windows=(15.0, 30.0, 60.0, 90.0, 120.0, 180.0)) -> dict:
    """At-rest weight-flow error per window from status rows recorded with nothing flowing.

    For each window length W, fit a line to every W-second stretch (stepping 1 s) and take the
    standard deviation of the resulting "flow" in mL/min. Needs at least 1.25 W of data per window.
    """
    pts = [(r["t"] / 1000.0, r["w_g"]) for r in rows
           if r.get("w_cal") and r.get("w_ok") and r.get("w_stable") and r.get("w_g") is not None]
    if len(pts) < 20:
        return {}
    out = {}
    total = pts[-1][0] - pts[0][0]
    for w in windows:
        if total < 1.25 * w:
            continue
        slopes, i, last_start = [], 0, -1e9
        for i, (t0, _) in enumerate(pts):
            if t0 - last_start < 1.0:
                continue
            seg = [p for p in pts[i:] if p[0] - t0 <= w]
            if seg[-1][0] - t0 < 0.95 * w:
                break
            fit = linfit([p[0] for p in seg], [p[1] for p in seg])
            if fit:
                slopes.append(fit[0] * 60.0)
                last_start = t0
        if len(slopes) >= 5:
            m = sum(slopes) / len(slopes)
            out[w] = max(0.02, math.sqrt(sum((x - m) ** 2 for x in slopes) / len(slopes)))
    return out


class Calibrator:
    """Average drop volume = -d(mass)/d(drop count) / rho over clean stretches."""

    MIN_SPAN_S, MIN_DROPS = 120.0, 60   # long enough for the scale's slow wander to average out
    LOCK_SPAN_S = 180.0                 # after this much clean data, lock: later faults can't leak in
    V_MIN, V_MAX = 1 / 60, 1 / 8       # plausible mL/drop

    def __init__(self, rho: float):
        self.rho = rho
        self.done: list[tuple[float, float]] = []   # (V, se) of closed segments
        self.seg: list[tuple[float, float, float]] = []  # (t, drops, grams)
        self.locked: tuple[float, float] | None = None

    def _estimate(self, seg):
        if len(seg) < 10:
            return None
        if seg[-1][0] - seg[0][0] < self.MIN_SPAN_S or seg[-1][1] - seg[0][1] < self.MIN_DROPS:
            return None
        fit = linfit([p[1] for p in seg], [p[2] for p in seg])
        if not fit:
            return None
        b, _, _, b_se, _ = fit
        v = -b / self.rho
        se = b_se * math.sqrt(2) / self.rho   # status samples overlap (1-s means at 2 Hz)
        if not (self.V_MIN <= v <= self.V_MAX):
            return None
        return v, se

    def feed(self, t, drops, grams, clean: bool):
        if self.locked:
            return
        if clean and self.seg and (t - self.seg[-1][0] > 2.0 or drops < self.seg[-1][1]):
            self.close()
        if clean:
            self.seg.append((t, drops, grams))
            if self.seg[-1][0] - self.seg[0][0] >= self.LOCK_SPAN_S:
                est = self._estimate(self.seg)
                if est:
                    self.done.append(est)
                    self.locked = self.result_from(self.done)
                    self.seg = []
        else:
            self.close()

    def close(self):
        est = self._estimate(self.seg)
        if est:
            self.done.append(est)
        self.seg = []

    @staticmethod
    def result_from(ests):
        w = [1 / max(se, 1e-6) ** 2 for _, se in ests]
        v = sum(wi * vi for wi, (vi, _) in zip(w, ests)) / sum(w)
        return v, math.sqrt(1 / sum(w))

    def result(self):
        if self.locked:
            return self.locked
        ests = list(self.done)
        cur = self._estimate(self.seg)
        if cur:
            ests.append(cur)
        if not ests:
            return None
        w = [1 / max(se, 1e-6) ** 2 for _, se in ests]
        v = sum(wi * vi for wi, (vi, _) in zip(w, ests)) / sum(w)
        se = math.sqrt(1 / sum(w))
        return v, se

    def reset(self):
        self.done, self.seg, self.locked = [], [], None


class FusionEngine:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or Settings()
        self.cal = Calibrator(self.s.rho)
        self._reset_stream()

    def set_settings(self, s: Settings):
        self.s = s
        self.cal.rho = s.rho

    def reset_calibration(self):
        self.cal.reset()

    def _reset_stream(self):
        self.hist: deque[dict] = deque()     # per-status rows used for windows
        self.last_t = None
        self.prev_long = None
        self.prev_drops = None
        self.t_opt_anom = -1e9
        self.t_w_unstable = -1e9
        self.t_opt_bad = -1e9
        self.dis_since = None
        self.dis_active = False
        self.dis_clear_since = None
        self.stop = None                     # active "drops stopped" event
        self.lost = None                     # active "optical signal lost" event
        self.restarted = False
        self.low_vol = False

    def _aligned(self, t: float, length: float, v_drop: float):
        """Optical and weight flow over the same window, or None if either sensor
        was unhealthy anywhere in it. Returns (q_o, q_o_se, q_w, q_w_se)."""
        win = [h for h in self.hist if t - h["t"] <= length]
        if not win or win[-1]["t"] - win[0]["t"] < 0.9 * length:
            return None
        if not all(h["opt"] and h["w_good"] and h["g"] is not None for h in win):
            return None
        fo = linfit([h["t"] for h in win], [h["drops"] for h in win])
        fw = linfit([h["t"] for h in win], [h["g"] for h in win])
        if not fo or not fw:
            return None
        span = win[-1]["t"] - win[0]["t"]
        se_w = math.hypot(fw[3] * math.sqrt(2) * 60 / self.s.rho, noise_floor(self.s.noise_profile, span))
        return (max(0.0, fo[0]) * 60 * v_drop, fo[3] * 60 * v_drop, -fw[0] * 60 / self.s.rho, se_w)

    def _post_slope(self, t0: float, t: float):
        """Weight flow from data after t0 (+guard), with the measured-noise floor. (q, se) or None."""
        s = self.s
        post = [h for h in self.hist if h["t"] >= t0 + s.stop_guard_s and t - h["t"] <= s.stop_max_post_s
                and h["w_good"] and h["g"] is not None]
        if len(post) < 8 or post[-1]["t"] - post[0]["t"] < 4.0:
            return None
        fit = linfit([h["t"] for h in post], [h["g"] for h in post])
        if not fit:
            return None
        span = post[-1]["t"] - post[0]["t"]
        se = max(fit[3] * math.sqrt(2) * 60 / s.rho, noise_floor(s.noise_profile, span))
        return -fit[0] * 60 / s.rho, se

    # ------------------------------------------------------------------
    def update(self, m: dict) -> Assessment:
        s = self.s
        t = m["t"] / 1000.0
        if self.last_t is not None and t < self.last_t - 1.0:
            self._reset_stream()             # device rebooted
            self.restarted = True
        self.last_t = t
        findings: list[Finding] = []

        # ---- optical health ----
        o_long = m.get("o_long") or 0
        if self.prev_long is not None and o_long > self.prev_long:
            self.t_opt_anom = t
        self.prev_long = o_long
        o_drops = m.get("o_drops") or 0
        if self.prev_drops is not None and o_drops < self.prev_drops:
            self.hist.clear()                # counter reset on the device
            self.cal.close()
        self.prev_drops = o_drops
        o_sat = bool(m.get("o_sat"))
        o_snr = m.get("o_snr")
        o_since = m.get("o_since")
        # A receiver pinned near the top of the ADC range can still see drops as dips.
        # Only call it "no signal" once drops have also stopped arriving.
        near_sat = o_sat and o_since is not None and o_since < s.stop_after_s
        if o_sat and not near_sat:
            oh = "no_signal"
        elif t - self.t_opt_anom < s.anomaly_hold_s:
            oh = "anomaly"
        elif o_drops >= 5 and o_snr is not None and o_snr < s.min_snr:
            oh = "weak"
        else:
            oh = "ok"
        opt_usable = oh != "no_signal"
        if not opt_usable:
            self.t_opt_bad = t

        # ---- weight health ----
        if not m.get("w_ok"):
            wh = "missing"
        elif not m.get("w_cal"):
            wh = "uncalibrated"
        elif not m.get("w_stable"):
            wh = "disturbed"
            self.t_w_unstable = t
        elif t - self.t_w_unstable < s.settle_hold_s:
            wh = "settling"
        else:
            wh = "ok"
        w_good_now = wh == "ok"
        w_g = m.get("w_g")

        # ---- history and aligned-window estimates ----
        self.hist.append({"t": t, "drops": o_drops, "g": w_g, "w_good": w_good_now,
                          "opt": opt_usable, "std1": m.get("w_std1")})
        while self.hist and t - self.hist[0]["t"] > max(300.0, 1.2 * s.long_window_s, s.stop_max_post_s + 60):
            self.hist.popleft()
        win = [h for h in self.hist if t - h["t"] <= s.window_s]

        cal = self.cal.result()
        cal_ok = False
        if cal:
            cal_df = 1 / cal[0]
            cal_ok = abs(cal_df - s.drop_factor) / s.drop_factor <= 0.25
        v_drop = cal[0] if (s.use_autocal and cal and cal_ok) else 1 / s.drop_factor

        q_o = q_o_se = q_w = q_w_se = None
        span = win[-1]["t"] - win[0]["t"] if win else 0.0
        enough = span >= min(10.0, 0.5 * s.window_s)
        if enough and opt_usable and all(h["opt"] for h in win):
            fit = linfit([h["t"] for h in win], [h["drops"] for h in win])
            if fit:
                q_o = max(0.0, fit[0]) * 60 * v_drop
                q_o_se = fit[3] * 60 * v_drop
        w_pts = [h for h in win if h["w_good"] and h["g"] is not None]
        # The scale's slow wander makes slopes over <30 s meaningless (±3 mL/min at 10 s),
        # so weight flow is only reported once 30 s of data exist.
        w_enough = span >= min(30.0, 0.5 * s.window_s)
        if w_enough and w_good_now and len(w_pts) >= 0.8 * len(win):
            fit = linfit([h["t"] for h in w_pts], [h["g"] for h in w_pts])
            if fit:
                q_w = -fit[0] * 60 / s.rho
                w_span = w_pts[-1]["t"] - w_pts[0]["t"]
                # white-noise error from the fit, plus the measured slow-wander floor for this span
                q_w_se = math.hypot(fit[3] * math.sqrt(2) * 60 / s.rho, noise_floor(s.noise_profile, w_span))
        q_o_fast = None
        fwin = [h for h in win if t - h["t"] <= s.fast_window_s]
        if opt_usable and len(fwin) >= 8 and all(h["opt"] for h in fwin):
            fit = linfit([h["t"] for h in fwin], [h["drops"] for h in fwin])
            if fit:
                q_o_fast = max(0.0, fit[0]) * 60 * v_drop
        self.hist[-1]["q_o"] = q_o
        self.hist[-1]["q_fast"] = q_o_fast

        # ---- calibration feed ----
        # Never calibrate while the sensors disagree: that would absorb a real miscount.
        clean = (oh == "ok" and w_good_now and w_g is not None and self.stop is None
                 and self.dis_since is None and not self.dis_active
                 and (o_since is not None and o_since < 3.0))
        self.cal.feed(t, o_drops, w_g if w_g is not None else 0.0, clean)

        # ---- drops-stopped event with weight confirmation ----
        # Threshold: at least stop_after_s, stretched to 2.5 drop intervals for slow drips
        # (interval taken from the flow estimate as of the last drop, so it cannot grow
        # while drops are absent). o_since is stale right after the optical channel comes
        # back from "no signal", so also require the channel to have been usable that long.
        stop_after = s.stop_after_s
        if o_since is not None:
            at_last = [h for h in self.hist if h["t"] <= t - o_since and h.get("q_fast")]
            if at_last and at_last[-1]["q_fast"] > 0.05:
                stop_after = min(s.stop_after_max_s, max(stop_after, 2.5 * 60 * v_drop / at_last[-1]["q_fast"]))
        drops_stopped = (opt_usable and o_drops > 0 and o_since is not None
                         and o_since > stop_after and t - self.t_opt_bad > stop_after)
        if drops_stopped and self.stop is None:
            t0 = t - o_since
            before = [h for h in self.hist if h["t"] <= t0 and (h.get("q_fast") or h.get("q_o")) is not None]
            q_prev = (before[-1].get("q_fast") or before[-1].get("q_o")) if before else None
            self.stop = {"t0": t0, "q_prev": q_prev,
                         "anomaly": abs(self.t_opt_anom - t0) < 5.0 or oh == "anomaly",
                         "verdict": "pending", "q_after": None, "q_after_se": None}
        elif not drops_stopped and self.stop is not None and o_since is not None and o_since < s.stop_after_s:
            if self.stop["verdict"] == "flow_continues":
                findings.append(Finding("info", "Drop detection resumed",
                                        "Drops are being counted again."))
            self.stop = None

        if self.stop is not None:
            ev = self.stop
            # Evidence = slope of the reservoir mass AFTER the stop, skipping the first few
            # seconds. A hand on the clamp shifts the hanging load by a step; a step before
            # the guard cannot change the slope after it. Re-evaluated on every update, in
            # both directions, so an early wrong call is corrected instead of latched.
            post = [h for h in self.hist if h["t"] >= ev["t0"] + s.stop_guard_s and t - h["t"] <= s.stop_max_post_s
                    and h["w_good"] and h["g"] is not None]
            fit = None
            if len(post) >= 8 and post[-1]["t"] - post[0]["t"] >= 4.0:
                fit = linfit([h["t"] for h in post], [h["g"] for h in post])
            if fit and w_good_now:
                span = post[-1]["t"] - post[0]["t"]
                sd1 = [h["std1"] for h in post if h.get("std1")]
                sd1 = sum(sd1) / len(sd1) if sd1 else 0.3
                # floor: white-noise slope error of 10 Hz samples with the measured 1-s scatter
                se_floor = max(sd1 * math.sqrt(12 / (10 * span ** 3)) * 60 / s.rho,
                               noise_floor(s.noise_profile, span))   # measured slow wander dominates
                q_after = -fit[0] * 60 / s.rho
                se = max(fit[3] * math.sqrt(2) * 60 / s.rho, se_floor)
                ev["q_after"], ev["q_after_se"] = q_after, se
                ref = ev["q_prev"] if (ev["q_prev"] is not None and ev["q_prev"] >= 0.4) else None
                mid = 0.5 * ref if ref else 0.4
                z = (q_after - mid) / se
                if z < -3:
                    ev["verdict"] = "confirmed"
                elif z > 3:
                    ev["verdict"] = "flow_continues"
                elif ev["verdict"] == "unverifiable":
                    ev["verdict"] = "pending"
            elif ev["verdict"] == "pending" and not w_good_now:
                ev["verdict"] = "unverifiable"
            if ev["verdict"] == "flow_continues":
                for h in self.hist:
                    if h["t"] >= ev["t0"]:
                        h["opt"] = False

        # ---- optical signal lost (ADC at the rail): ask the scale, using data after the loss only ----
        if not opt_usable:
            if self.lost is None:
                prev = [h.get("q_fast") for h in list(self.hist)[:-1] if h.get("q_fast") is not None]
                self.lost = {"t0": t, "q_prev": prev[-1] if prev else None, "verdict": "pending",
                             "q_after": None, "q_after_se": None}
            ev = self.lost
            ps = self._post_slope(ev["t0"], t)
            if ps and w_good_now:
                ev["q_after"], ev["q_after_se"] = ps
                ref = ev["q_prev"] if (ev["q_prev"] is not None and ev["q_prev"] >= 0.4) else None
                mid = 0.5 * ref if ref else 0.4
                z = (ps[0] - mid) / ps[1]
                ev["verdict"] = "no_flow" if z < -3 else ("flow_continues" if z > 3 else "pending")
        else:
            self.lost = None

        # ---- choose best flow estimate ----
        optical_fault = self.stop is not None and self.stop["verdict"] == "flow_continues"
        if optical_fault and q_w is not None:
            q_best, src = q_w, "weight"
        elif (q_o_fast is not None or q_o is not None) and not optical_fault:
            q_best, src = (q_o_fast if q_o_fast is not None else q_o), "optical"
        elif q_w is not None:
            q_best, src = q_w, "weight"
        else:
            q_best, src = None, ""

        flow_state = "unknown" if q_best is None else "normal"

        # ---- findings: drops stopped ----
        if self.stop is not None:
            ev, el = self.stop, t - self.stop["t0"]
            v = ev["verdict"]
            if v == "confirmed":
                flow_state = "stopped"
                q_best, src = 0.0, "both"
                findings.append(Finding(
                    "alarm", "Flow stopped - confirmed by both sensors",
                    f"No drops for {el:.0f} s and the reservoir mass is flat: weight flow since the stop "
                    f"{ev['q_after']:.1f} ± {ev['q_after_se']:.1f} mL/min"
                    + (f" (was {ev['q_prev']:.1f})." if ev["q_prev"] else "."),
                    ["Roller clamp closed", "Tubing kinked or occluded", "Reservoir empty"]))
            elif v == "flow_continues":
                rate = f" at about {ev['q_after']:.1f} mL/min" if ev["q_after"] is not None else ""
                findings.append(Finding(
                    "sensor", "Optical sensor not detecting drops - flow continues",
                    f"No drops counted for {el:.0f} s, but the reservoir is still losing mass{rate}.",
                    ["Beam blocked or sensor misaligned", "Drops falling outside the beam",
                     "Optical receiver disconnected"]))
            elif v == "unverifiable":
                flow_state = "stopped"
                findings.append(Finding(
                    "alarm", "Drops stopped - weight cannot confirm",
                    f"No drops for {el:.0f} s; the weight signal is unreliable, so flow loss is unconfirmed.",
                    ["Flow stopped", "Optical sensor problem"]))
            else:
                flow_state = "stopped"
                causes = ["Flow stopped (clamp / occlusion)", "Optical sensor stopped seeing drops"]
                obs = f"No drops for {el:.0f} s. Checking whether the reservoir mass is still decreasing."
                if ev["anomaly"]:
                    obs += " The optical signal changed abruptly at the same moment."
                    causes = ["Optical sensor blocked or moved"] + causes
                findings.append(Finding("alarm", "Drops stopped - confirming with weight", obs, causes))

        # ---- findings: disagreement (both flowing, estimates differ) ----
        if self.stop is None and q_o is not None and q_w is not None and oh in ("ok", "weak"):
            tol_rel = 0.10 if (cal and cal_ok and s.use_autocal) else 0.20
            tests = [(q_o, q_o_se or 0.0, q_w, q_w_se or 0.0)]
            long = self._aligned(t, s.long_window_s, v_drop)
            if long:
                tests.append(long)
            over = False
            for a_o, a_ose, a_w, a_wse in tests:
                lim = 3 * math.hypot(a_wse, a_ose) + tol_rel * max(a_o, a_w) + 0.1
                if abs(a_o - a_w) > lim:
                    over = True
                    q_o, q_w, q_w_se = a_o, a_w, a_wse   # report the window that shows it
            diff = q_o - q_w
            if over:
                self.dis_since = self.dis_since if self.dis_since is not None else t
                self.dis_clear_since = None
            else:
                self.dis_since = None
                self.dis_clear_since = self.dis_clear_since if self.dis_clear_since is not None else t
            if self.dis_since is not None and t - self.dis_since >= s.disagree_hold_s:
                self.dis_active = True
            elif self.dis_active and self.dis_clear_since is not None and t - self.dis_clear_since >= 6.0:
                self.dis_active = False
            if self.dis_active:
                if q_w is not None:      # weight measures volume directly: trust it for the number
                    q_best, src = q_w, "weight"
                if diff < 0:
                    findings.append(Finding(
                        "sensor", "Optical count lower than weight loss",
                        f"Optical {q_o:.2f} vs weight {q_w:.2f} ± {q_w_se:.2f} mL/min.",
                        ["Some drops missed (weak signal / partial misalignment)",
                         "Drop factor setting wrong", "Leak above the drip chamber"]))
                else:
                    findings.append(Finding(
                        "sensor", "Optical count higher than weight loss",
                        f"Optical {q_o:.2f} vs weight {q_w:.2f} ± {q_w_se:.2f} mL/min.",
                        ["Double-counting or electrical noise", "Drop factor setting wrong",
                         "Load cell partly supported / binding"]))
        else:
            self.dis_since = None
            self.dis_active = False

        # ---- findings: sensor health ----
        if oh == "no_signal":
            causes = ["IR LED off or disconnected", "Receiver disconnected",
                      "Beam fully blocked", "Ambient light saturating receiver"]
            ev = self.lost
            lost_for = t - ev["t0"] if ev else 0.0
            if ev and ev["verdict"] == "flow_continues":
                q_best, src = ev["q_after"], "weight"
                findings.append(Finding("sensor", "Optical sensor lost - flow continues",
                                        f"Receiver output has been at the ADC rail for {lost_for:.0f} s, but the "
                                        f"reservoir is still losing mass at {ev['q_after']:.1f} \u00b1 "
                                        f"{ev['q_after_se']:.1f} mL/min.", causes))
            elif ev and ev["verdict"] == "no_flow":
                flow_state = "stopped"
                q_best, src = 0.0, "weight"
                findings.append(Finding("alarm", "No flow on weight - optical sensor lost",
                                        f"Receiver output has been at the ADC rail for {lost_for:.0f} s and the "
                                        f"reservoir mass is flat ({ev['q_after']:.1f} \u00b1 {ev['q_after_se']:.1f} "
                                        "mL/min). Weight is the only evidence.",
                                        ["Flow stopped AND optical fault", "Optical cable or power pulled"]))
            else:
                findings.append(Finding("sensor", "No optical signal - checking weight",
                                        f"Receiver output has been at the ADC rail for {lost_for:.0f} s. Checking "
                                        "whether the reservoir is still losing mass.", causes))
        elif oh == "anomaly" and self.stop is None:
            findings.append(Finding("sensor", "Optical signal disturbance",
                                    "The receiver level jumped for longer than a drop.",
                                    ["Sensor touched or moved", "Beam briefly blocked", "Ambient light change"]))
        if near_sat and oh != "no_signal":
            findings.append(Finding("info", "IR receiver near saturation",
                                    "Receiver level is at the top of the ADC range. Drops are still detected, "
                                    "but a clamp stop will be reported as an optical fault.",
                                    ["Use a smaller photodiode resistor (47k)", "Larger LED resistor",
                                     "Ambient light"]))
        if oh == "weak":
            findings.append(Finding("warning", "Weak drop signal",
                                    f"Drop signal-to-noise ratio is {o_snr:.1f}; some drops may be missed.",
                                    ["Alignment", "Ambient light", "Receiver gain too low"]))
        if wh == "missing":
            findings.append(Finding("sensor", "Load cell not responding",
                                    "No readings from the HX711.", ["Wiring", "HX711 power"]))
        elif wh == "uncalibrated":
            findings.append(Finding("info", "Load cell not calibrated",
                                    "Run tare and cal on the device."))
        elif wh in ("disturbed", "settling"):
            findings.append(Finding("sensor", "Reservoir weight unstable",
                                    "Rapid mass fluctuations; weight-based flow is suspended"
                                    + (" - using drop counting." if q_o is not None else "."),
                                    ["Reservoir bumped or swinging", "Tubing pulled", "Vibration"]))

        # ---- findings: flow level and volume ----
        if s.expected_flow and q_best is not None and flow_state != "stopped":
            lo, hi = s.expected_flow * (1 - s.tolerance), s.expected_flow * (1 + s.tolerance)
            if q_best < lo:
                flow_state = "low"
                findings.append(Finding("warning", "Flow below expected range",
                                        f"{q_best:.2f} mL/min ({src}) vs expected {s.expected_flow:.1f}.",
                                        ["Clamp partly closed", "Partial occlusion", "Reservoir height reduced"]))
            elif q_best > hi:
                flow_state = "high"
                findings.append(Finding("warning", "Flow above expected range",
                                        f"{q_best:.2f} mL/min ({src}) vs expected {s.expected_flow:.1f}.",
                                        ["Clamp opened further"]))
        rem = m.get("w_rem") if m.get("w_ok") and m.get("w_cal") else None
        if rem is not None:
            self.low_vol = rem < s.low_volume_ml if not self.low_vol else rem < s.low_volume_ml + 5
        if rem is not None and self.low_vol:
            findings.append(Finding("warning", "Reservoir nearly empty",
                                    f"About {max(rem, 0):.0f} mL remaining."))
        minutes = None
        if rem is not None and q_best is not None and q_best > 0.1 and w_good_now:
            minutes = max(rem, 0) / q_best

        if q_best is None and flow_state == "unknown":
            findings.append(Finding("unknown", "Unable to determine flow reliably",
                                    "Neither sensor currently provides a trustworthy flow estimate."))
        if self.restarted:
            findings.append(Finding("info", "Device restarted", "History was reset."))
            self.restarted = False
        if cal and not cal_ok:
            findings.append(Finding("warning", "Auto-calibration rejected",
                                    f"Measured {1 / cal[0]:.1f} drops/mL is >25% from nominal "
                                    f"{s.drop_factor:.0f}; possible systematic miscount.",
                                    ["Wrong nominal drop factor", "Optical double/missed counts"]))

        findings.sort(key=lambda f: -LEVEL_RANK[f.level])
        top = findings[0] if findings and LEVEL_RANK[findings[0].level] >= LEVEL_RANK["warning"] else None
        if top:
            level, headline = top.level, top.title
        else:
            level = "ok"
            headline = f"Flow normal - {q_best:.2f} mL/min" if q_best is not None else "Monitoring"

        return Assessment(
            t=t, level=level, headline=headline, flow_state=flow_state, findings=findings,
            q_o=q_o, q_o_se=q_o_se, q_o_fast=q_o_fast, q_w=q_w, q_w_se=q_w_se, q_best=q_best, q_source=src,
            opt_health=oh, w_health=wh, drop_factor_used=1 / v_drop,
            cal_df=(1 / cal[0]) if cal else None,
            cal_df_se=(cal[1] / cal[0] ** 2) if cal else None,
            remaining_ml=rem, minutes_left=minutes,
            stop_verdict=self.stop["verdict"] if self.stop else None,
        )
