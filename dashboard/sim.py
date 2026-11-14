"""DripGuard simulator.

Produces the exact JSON lines the ESP32 firmware prints ({"type":"s",...} every
500 ms and {"type":"drop",...} per drop), from a simple physical model of a
gravity drip. The optical and weight statistics are a line-by-line port of the
firmware's computeOptical()/computeWeight(), so the dashboard sees realistic
values, nulls and lags.

Standalone use (writes a log you can replay in the dashboard):
    python sim.py demo > logs/sim_demo.jsonl
"""
from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import dataclass


@dataclass
class Segment:
    dur: float               # seconds
    flow: float              # true flow, mL/min
    optical_ok: bool = True  # False = beam blocked / misaligned: drops not detected
    bump: bool = False       # reservoir disturbed at the start of this segment
    detect: float = 1.0      # fraction of drops the optical sensor catches
    rail: bool = False       # optical fault drives the receiver to the ADC rail (o_sat=1)
    label: str = ""


SCENARIOS: dict[str, list[Segment]] = {
    "demo": [
        Segment(60, 3.0, label="normal"),
        Segment(60, 1.5, label="partial clamp"),
        Segment(60, 0.0, label="clamp closed"),
        Segment(45, 3.0, label="normal"),
        Segment(60, 3.0, optical_ok=False, label="optical blocked"),
        Segment(45, 3.0, label="normal"),
        Segment(40, 3.0, bump=True, label="reservoir bumped"),
        Segment(60, 3.0, label="normal"),
    ],
    "normal": [Segment(900, 3.0, label="normal")],
    "partial": [Segment(60, 3.0, label="normal"), Segment(240, 1.2, label="partial clamp")],
    "stopped": [Segment(60, 3.0, label="normal"), Segment(120, 0.0, label="clamp closed"),
                Segment(60, 3.0, label="normal")],
    "optical_block": [Segment(60, 3.0, label="normal"),
                      Segment(90, 3.0, optical_ok=False, label="optical blocked"),
                      Segment(60, 3.0, label="normal")],
    "bump": [Segment(60, 3.0, label="normal"), Segment(90, 3.0, bump=True, label="reservoir bumped")],
    "near_empty": [Segment(600, 3.0, label="normal (small reservoir)")],
    "optical_dark": [Segment(60, 3.0, label="normal"),
                     Segment(60, 3.0, optical_ok=False, rail=True, label="LED off / beam dark, flow continues"),
                     Segment(40, 3.0, label="normal"),
                     Segment(60, 0.0, optical_ok=False, rail=True, label="LED off AND clamp closed")],
    "miscount": [Segment(120, 3.0, label="normal"),
                 Segment(150, 3.0, detect=0.75, label="optical misses 25% of drops")],
}
SCENARIO_START_MASS = {"near_empty": 62.0}


def _r(v, d):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return round(v, d)


class Simulator:
    def __init__(self, segments: list[Segment], m0_g: float = 500.0, v_drop_ml: float = 1 / 18.0,
                 rho: float = 0.998, win_s: float = 30.0, opt_win_s: float = 30.0,
                 drop_factor: float = 20.0, stab_g: float = 1.5, noise_g: float = 0.3, seed: int = 1):
        self.segments = segments
        self.m0, self.v_drop, self.rho = m0_g, v_drop_ml, rho
        self.win, self.owin, self.df, self.stab, self.noise = win_s, opt_win_s, drop_factor, stab_g, noise_g
        self.rng = random.Random(seed)

    def total_duration(self) -> float:
        return sum(s.dur for s in self.segments)

    def segment_at(self, t_rel: float) -> tuple[int, Segment]:
        acc = 0.0
        for i, s in enumerate(self.segments):
            acc += s.dur
            if t_rel < acc:
                return i, s
        return len(self.segments) - 1, self.segments[-1]

    def run(self):
        """Yield (t_seconds, message_dict) in time order."""
        rng = self.rng
        dt = 0.1                   # HX711 at 10 Hz
        t0_ms = 1000               # firmware has been up ~1 s before the first status
        yield 0.0, {"type": "boot", "fw": "dripguard-0.1-sim"}

        water_g = self.m0
        vol_acc, next_v = 0.0, self.v_drop
        drop_t: list[int] = []
        drops_total, amp_avg = 0, 0.0
        long_ev, spikes = 0, 0
        prev_opt_ok = True
        mass: list[tuple[int, float]] = []
        bump_t = None
        seg_start, seg_idx = 0.0, -1
        base = 1800.0
        n_ticks = int(self.total_duration() / dt)

        for i in range(1, n_ticks + 1):
            t = i * dt
            now = t0_ms + int(round(t * 1000))
            idx, seg = self.segment_at(t)

            # segment bookkeeping (bump at segment start, optical transitions)
            if idx != seg_idx:
                seg_idx, seg_start = idx, t
                if seg.bump:
                    bump_t = t
            if seg.optical_ok != prev_opt_ok:
                long_ev += 1           # firmware: long deviation -> re-baseline
                prev_opt_ok = seg.optical_ok
            base_target = 1800.0 if seg.optical_ok else (5.0 if seg.rail else 420.0)
            base += (base_target - base) * 0.5 + rng.gauss(0, 0.3)

            # fluid: true flow leaves the reservoir as discrete drops
            flow = seg.flow if water_g > 0.5 else 0.0
            dv = flow / 60.0 * dt
            water_g = max(0.0, water_g - dv * self.rho)
            vol_acc += dv
            while vol_acc >= next_v:
                vol_acc -= next_v
                frac = 1.0 - vol_acc / dv if dv > 0 else 1.0
                td = now - int((1.0 - frac) * dt * 1000)
                next_v = self.v_drop * (1 + rng.gauss(0, 0.03))
                if seg.optical_ok and rng.random() < seg.detect:
                    amp = -(60 + rng.gauss(0, 8))
                    drop_t.append(td)
                    drops_total += 1
                    amp_avg = abs(amp) if amp_avg == 0 else amp_avg + (abs(amp) - amp_avg) * 0.1
                    yield t, {"type": "drop", "t": td, "amp": round(amp, 1), "w": 13 + rng.randint(0, 5)}
            if rng.random() < 0.0005:
                spikes += 1

            # HX711 sample
            g = water_g + rng.gauss(0, self.noise)
            if bump_t is not None and 0 <= t - bump_t < 8:
                tb = t - bump_t
                g += 25 * math.exp(-tb / 1.5) * math.sin(2 * math.pi * 1.3 * tb)
            mass.append((now, g))
            if len(mass) > 1300:
                mass.pop(0)

            if i % 5 != 0:
                continue

            # ---- port of computeOptical() ----
            win_ms = self.owin * 1000
            n = 0
            first = last = 0
            for td in reversed(drop_t[-256:]):
                if now - td > win_ms:
                    break
                if n == 0:
                    last = td
                first = td
                n += 1
            since = (now - drop_t[-1]) / 1000 if drop_t else None
            if n >= 3 and last > first:
                rate = (n - 1) / ((last - first) / 1000)
            else:
                rate = n / self.owin
            if since is not None and since > 0:
                rate = min(rate, 1.5 / since)
            noise = (2.5 if seg.optical_ok else 1.0) + rng.gauss(0, 0.05)

            # ---- port of computeWeight() ----
            newest_t, newest_g = mass[-1]
            last10 = [m[1] for m in mass[-10:]]
            g_now = sum(last10) / len(last10)
            std1 = math.sqrt(max(0.0, sum((x - g_now) ** 2 for x in last10) / (len(last10) - 1)))
            pts = []
            for mt, mg in reversed(mass):
                if newest_t - mt > self.win * 1000:
                    break
                pts.append((-(newest_t - mt) / 1000.0, mg - newest_g))
            nn = len(pts)
            span = -pts[-1][0] if pts else 0.0
            slope = se = res = None
            if nn >= 3:
                st = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
                stt = sum(p[0] ** 2 for p in pts); sty = sum(p[0] * p[1] for p in pts)
                sxx = stt - st * st / nn
                if sxx > 1e-9:
                    b = (sty - st * sy / nn) / sxx
                    a = (sy - b * st) / nn
                    rss = sum((p[1] - (a + b * p[0])) ** 2 for p in pts)
                    res = math.sqrt(rss / (nn - 2))
                    slope, se = b, res / math.sqrt(sxx)
            q_valid = slope is not None and nn >= 20 and span >= min(10.0, self.win * 0.5)
            stable = std1 < self.stab and (res is None or res < 2 * self.stab)

            yield t, {
                "type": "s", "t": now,
                "o_base": _r(base, 1), "o_noise": _r(noise, 2), "o_sat": 1 if base < 40 else 0,
                "o_drops": drops_total, "o_n": n, "o_rate": _r(rate, 3),
                "o_q": _r(rate * 60 / self.df, 3), "o_since": _r(since, 2),
                "o_amp": _r(amp_avg, 1), "o_snr": _r(amp_avg / noise if noise > 0 else None, 1),
                "o_long": long_ev, "o_spk": spikes,
                "w_ok": 1, "w_cal": 1, "w_raw": int(84000 + 420 * g_now),
                "w_g": _r(g_now, 2), "w_std1": _r(std1, 3),
                "w_q": _r(-slope * 60 / self.rho, 3) if q_valid else None,
                "w_qse": _r(se * 60 / self.rho, 3) if q_valid else None,
                "w_res": _r(res, 3), "w_span": _r(span, 1), "w_n": nn,
                "w_stable": 1 if stable else 0, "w_rem": _r(g_now / self.rho, 1),
                "_sim_label": seg.label,
            }


def make(scenario: str, seed: int = 1) -> Simulator:
    return Simulator(SCENARIOS[scenario], m0_g=SCENARIO_START_MASS.get(scenario, 500.0), seed=seed)


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "demo"
    for _, msg in make(name).run():
        print(json.dumps(msg, separators=(",", ":")))
