"""Data sources and shared state for the DripGuard dashboard.

Exactly one source runs at a time (serial, simulator or replay). It parses
firmware lines, runs the fusion engine on every status message and stores
the results in a thread-safe Store. The Streamlit UI only reads snapshots.

The serial source is the ONLY thing that opens the COM port, and it logs every
received line to logs/live_*.jsonl, so no separate logger is needed. Those
logs replay in the dashboard exactly as they were recorded.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path

from explain import Explainer, build_facts
from fusion import Assessment, FusionEngine, Settings

LOG_DIR = Path(__file__).resolve().parent / "logs"


def list_serial_ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(p.device, f"{p.device} - {p.description}") for p in list_ports.comports()]


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.engine = FusionEngine()
        self.on_event = None          # callback(event) for new events, called outside the lock
        self.clear()

    def clear(self):
        self.rows: deque[dict] = deque(maxlen=7200)        # 1 h at 2 Hz
        self.events: deque[dict] = deque(maxlen=200)
        self.messages: deque[str] = deque(maxlen=50)
        self.assessment: Assessment | None = None
        self.last_rx = None
        self.drop_count = 0
        self._prev_key = None

    def reset(self):
        """New session: fresh history AND fresh calibration (don't carry sim results into live)."""
        with self.lock:
            self.clear()
            self.engine = FusionEngine(self.engine.s)

    def ingest(self, line: str):
        line = line.strip()
        if not line:
            return
        now = time.time()
        if not line.startswith("{"):
            with self.lock:
                self.messages.append(line)
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        kind = msg.get("type")
        new_ev = None
        with self.lock:
            self.last_rx = now
            if kind == "s":
                a = self.engine.update(msg)
                self.assessment = a
                row = {k: v for k, v in msg.items() if k != "type"}
                row.update({"host_t": now, "level": a.level, "headline": a.headline,
                            "q_o_al": a.q_o, "q_o_fast": a.q_o_fast, "q_w_al": a.q_w,
                            "q_w_se_al": a.q_w_se, "q_best": a.q_best, "df_used": a.drop_factor_used})
                self.rows.append(row)
                key = (a.level, a.headline.split(" - ")[0])
                if key != self._prev_key:
                    self._prev_key = key
                    new_ev = {"t": msg["t"] / 1000, "host_t": now, "level": a.level, "event": a.headline}
                    self.events.append(new_ev)
            elif kind == "drop":
                self.drop_count += 1
            elif kind == "note":
                self.events.append({"t": self.rows[-1]["t"] / 1000 if self.rows else 0,
                                    "host_t": now, "level": "info", "event": "NOTE: " + msg.get("text", "")})
            else:
                self.messages.append(line)
        if new_ev is not None and self.on_event:
            self.on_event(new_ev)

    def snapshot(self, max_rows: int = 1200) -> dict:
        with self.lock:
            rows = list(self.rows)[-max_rows:]
            return {
                "assessment": self.assessment,
                "rows": rows,
                "events": list(self.events)[-30:],
                "messages": list(self.messages)[-10:],
                "age": (time.time() - self.last_rx) if self.last_rx else None,
                "drop_count": self.drop_count,
            }


# Headline prefix -> (big word, second line) for the device's OLED (big word <= 10 chars).
_OLED_MAP = [
    ("Flow stopped - confirmed", "STOPPED", "both sensors agree"),
    ("Drops stopped - confirming", "NO DROPS", "checking weight..."),
    ("Drops stopped - weight cannot", "NO DROPS", "weight can't confirm"),
    ("No flow on weight", "STOPPED?", "weight only, IR lost"),
    ("Optical sensor not detecting", "OPT FAULT", "flow continues"),
    ("Optical sensor lost", "OPT FAULT", "flow continues"),
    ("No optical signal", "OPT FAULT", "no IR signal"),
    ("Optical signal disturbance", "IR JUMP", "beam moved/blocked?"),
    ("Optical count", "MISMATCH", "optical vs weight"),
    ("Load cell not responding", "WT FAULT", "no HX711 data"),
    ("Reservoir weight unstable", "BUMPED", "weight paused"),
    ("Flow below expected", "LOW FLOW", None),
    ("Flow above expected", "HIGH FLOW", None),
    ("Reservoir nearly empty", "LOW BAG", None),
    ("Unable to determine", "UNKNOWN", "no reliable sensor"),
    ("Weak drop signal", "WEAK IR", "drops may be missed"),
    ("Auto-calibration rejected", "CAL ?", "check drop factor"),
]


def oled_line(a: Assessment) -> str:
    """Build the firmware `disp` command for an assessment (ASCII, no '|' in fields)."""
    def num(v, d=2):
        return "-" if v is None else f"{v:.{d}f}"
    flow_txt = f"{a.q_best:.2f} mL/min" if a.q_best is not None else ""
    big, line2 = "FLOW OK", flow_txt
    for prefix, b, l2 in _OLED_MAP:
        if a.headline.startswith(prefix):
            big = b
            line2 = l2 if l2 is not None else flow_txt
            if prefix == "Reservoir nearly empty":
                line2 = f"{num(a.remaining_ml, 0)} mL left"
            break
    if big == "FLOW OK" and a.q_best is None:
        big, line2 = "WAIT", "collecting data"
    optical_fault = a.stop_verdict == "flow_continues" or a.headline.startswith("Optical sensor")
    opt = "o" if a.opt_health in ("ok", "weak") and not optical_fault else "x"
    wt = "o" if a.w_health == "ok" else ("~" if a.w_health in ("disturbed", "settling") else "x")
    q_opt = a.q_o_fast if a.q_o_fast is not None else a.q_o
    fields = [a.level, big, line2[:21], num(a.q_best), num(q_opt), num(a.q_w),
              num(a.remaining_ml, 0), num(a.cal_df, 1), opt, wt]
    clean = [str(f).replace("|", "/").encode("ascii", "replace").decode() for f in fields]
    return "disp " + "|".join(clean)


class _Source(threading.Thread):
    def __init__(self, store: Store):
        super().__init__(daemon=True)
        self.store = store
        self.stop_evt = threading.Event()
        self.error: str | None = None
        self.done = False

    def stop(self):
        self.stop_evt.set()

    def send(self, cmd: str):
        pass


class SerialSource(_Source):
    def __init__(self, store: Store, port: str, baud: int = 921600):
        super().__init__(store)
        self.port, self.baud = port, baud
        self.ser = None
        self.wlock = threading.Lock()
        LOG_DIR.mkdir(exist_ok=True)
        self.log_path = LOG_DIR / time.strftime("live_%Y%m%d_%H%M%S.jsonl")
        self.log = open(self.log_path, "a", encoding="utf-8")

    def label(self):
        return f"Live: {self.port} (logging to {self.log_path.name})"

    def _log(self, text: str):
        self.log.write(text + "\n")
        self.log.flush()

    def run(self):
        try:
            import serial
        except ImportError:
            self.error = "pyserial is not installed (pip install pyserial)"
            self.done = True
            return
        while not self.stop_evt.is_set():
            try:
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = self.port, self.baud, 0.2
                ser.dtr = False      # don't hold the ESP32 in reset / bootloader
                ser.rts = False
                ser.open()
                self.ser, self.error = ser, None
                buf = b""
                while not self.stop_evt.is_set():
                    chunk = ser.read(ser.in_waiting or 1)
                    if not chunk:
                        continue
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("utf-8", errors="replace").strip()
                        if line:
                            self._log(line)
                            self.store.ingest(line)
                            if line.startswith('{"type":"s"'):
                                self._push_display()
            except Exception as e:        # unplugged, port busy, ...
                self.error = f"{type(e).__name__}: {e}"
                if "denied" in str(e).lower() or "busy" in str(e).lower():
                    self.error += " - close the Arduino Serial Monitor/Plotter and any other program using the port"
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self.stop_evt.wait(1.0)   # retry: survives a USB unplug/replug
        try:
            if self.ser:
                self.ser.close()
        finally:
            self.log.close()
            self.done = True

    def _push_display(self):
        """Mirror the diagnosis on the device's OLED, at most twice a second (not logged)."""
        now = time.time()
        if now - getattr(self, "_last_disp", 0) < 0.45:
            return
        with self.store.lock:
            a = self.store.assessment
        if a is None:
            return
        self._last_disp = now
        with self.wlock:
            if self.ser:
                self.ser.write((oled_line(a) + "\n").encode("ascii", "replace"))

    def send(self, cmd: str):
        cmd = cmd.strip()
        if not cmd:
            return
        self._log(json.dumps({"type": "host_cmd", "cmd": cmd, "host_t": time.time()}))
        with self.wlock:
            if self.ser:
                self.ser.write((cmd + "\n").encode())

    def note(self, text: str):
        line = json.dumps({"type": "note", "text": text, "host_t": time.time()})
        self._log(line)
        self.store.ingest(line)


class SimSource(_Source):
    def __init__(self, store: Store, scenario: str, speed: float = 1.0, seed: int = 1):
        super().__init__(store)
        self.scenario, self.speed, self.seed = scenario, speed, seed

    def label(self):
        return f"Simulator: {self.scenario} at {self.speed:g}x"

    def run(self):
        import sim
        start = time.time()
        for t, msg in sim.make(self.scenario, self.seed).run():
            if self.stop_evt.is_set():
                break
            delay = start + t / self.speed - time.time()
            if delay > 0 and self.stop_evt.wait(delay):
                break
            self.store.ingest(json.dumps(msg))
        self.done = True


class ReplaySource(_Source):
    def __init__(self, store: Store, path: str, speed: float = 1.0):
        super().__init__(store)
        self.path, self.speed = path, speed

    def label(self):
        return f"Replay: {os.path.basename(self.path)} at {self.speed:g}x"

    def run(self):
        last_dev_t = None
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    if self.stop_evt.is_set():
                        break
                    line = line.strip()
                    try:
                        msg = json.loads(line) if line.startswith("{") else None
                    except json.JSONDecodeError:
                        msg = None
                    if msg and msg.get("type") in ("s", "drop") and "t" in msg:
                        dev_t = msg["t"] / 1000
                        if last_dev_t is not None and dev_t >= last_dev_t:
                            wait = min((dev_t - last_dev_t) / self.speed, 2.0)
                            if wait > 0 and self.stop_evt.wait(wait):
                                break
                        last_dev_t = dev_t
                    if msg and msg.get("type") == "host_cmd":
                        msg = {"type": "note", "text": "command: " + msg.get("cmd", "")}
                        line = json.dumps(msg)
                    self.store.ingest(line)
        except OSError as e:
            self.error = str(e)
        self.done = True


class Runtime:
    """Owns the store and the single active source. Cached once by Streamlit."""

    def __init__(self):
        self.store = Store()
        self.source: _Source | None = None
        self.mode = None
        self.explainer = Explainer()
        self.store.on_event = lambda ev: self.explainer.maybe_auto(ev, self.ai_facts)

    def ai_facts(self) -> dict:
        return build_facts(self.store.snapshot(max_rows=7200), self.store.engine.s)

    def _start(self, src: _Source, mode: str):
        self.stop()
        self.store.reset()
        self.explainer.clear()
        self.source, self.mode = src, mode
        src.start()

    def start_serial(self, port: str):
        self._start(SerialSource(self.store, port), "serial")

    def start_sim(self, scenario: str, speed: float):
        self._start(SimSource(self.store, scenario, speed), "sim")

    def start_replay(self, path: str, speed: float):
        self._start(ReplaySource(self.store, path, speed), "replay")

    def stop(self):
        if self.source:
            self.source.stop()
            self.source.join(timeout=2)
        self.source, self.mode = None, None

    def send(self, cmd: str):
        if self.source:
            self.source.send(cmd)

    def note(self, text: str):
        if isinstance(self.source, SerialSource):
            self.source.note(text)
        else:
            self.store.ingest(json.dumps({"type": "note", "text": text}))

    def apply_settings(self, s: Settings):
        with self.store.lock:
            self.store.engine.set_settings(s)

    def reset_calibration(self):
        with self.store.lock:
            self.store.engine.reset_calibration()

    def status_text(self) -> str:
        if not self.source:
            return "Stopped"
        txt = self.source.label()
        if self.source.error:
            txt += f"  \n**Error:** {self.source.error}"
        elif self.source.done:
            txt += "  \nFinished."
        return txt

    @staticmethod
    def list_logs() -> list[str]:
        if not LOG_DIR.exists():
            return []
        return sorted((str(p) for p in LOG_DIR.glob("*.jsonl")), reverse=True)


def assessment_dict(a: Assessment | None) -> dict | None:
    return asdict(a) if a else None
