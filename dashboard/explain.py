"""Gemini explanation layer for DripGuard.

Division of labour (say this in the pitch):
- fusion.py makes every diagnosis from the sensor data, deterministically.
- Gemini only turns that diagnosis plus the logged numbers into plain language:
  a 10-second status for a judge or nurse, an incident note, or an answer to a
  question about the session. It is given the facts as JSON, told to use nothing
  else, and can never change a verdict.
- If Gemini is unavailable (no key, no network, quota), an offline summary is
  built from the same facts, so the panel never goes blank during a demo.

Key: environment variable GEMINI_API_KEY, or paste it in the sidebar.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

# Tried in order; the first that answers is remembered. Model IDs change often,
# so you can also type one in the sidebar.
MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]

SYSTEM = """You are the explanation layer of DripGuard, a water-only bench prototype that monitors \
drip flow with two independent sensors: an infrared drop counter on the drip chamber and a load cell \
weighing the reservoir. A deterministic fusion engine has already made every diagnosis. Your only job \
is to explain it.

Rules:
- Use only the facts in the JSON you are given. Never invent numbers, times, causes or events.
- Never change, soften or override the engine's verdict. If the engine says unknown, say it is unknown.
- If the data does not answer the question, say so plainly.
- Separate what was measured from possible explanations ("one likely cause is...").
- Operator notes in the event log (marked NOTE) are what the team says they did; treat them as context.
- No medical advice. This is not a medical device and is never connected to a person.
- Plain text, no markdown, at most {limit} words. Refer to times as mm:ss from the data."""

TASKS = {
    "now": ("Explain the current status for someone with ten seconds to read it: what the device "
            "sees, which sensor evidence supports it, and what to check first.", 70),
    "incident": ("Write a short incident note for the session's event log: what happened in order, how "
                 "each issue was detected (which sensor noticed first, which confirmed, how long "
                 "confirmation took), and how it resolved. Keep sensor faults separate from flow "
                 "problems.", 150),
    "ask": ("Answer this question using only the data: {q}", 100),
}
TASK_LABEL = {"now": "Status explanation", "incident": "Incident note", "ask": "Question"}


def _r(v, d=2):
    return None if v is None else round(float(v), d)


def build_facts(snap: dict, settings) -> dict:
    """Compact, factual JSON of the session: the only thing Gemini sees."""
    a = snap.get("assessment")
    rows = snap.get("rows") or []
    t0 = rows[0]["t"] / 1000 if rows else 0.0

    def mmss(sec):
        sec = max(0.0, sec - t0)
        return f"{int(sec // 60):02d}:{int(sec % 60):02d}"

    timeline, last = [], -1e9
    for r in rows:
        tt = r["t"] / 1000
        if tt - last >= 10:
            last = tt
            q_opt = r.get("q_o_fast") if r.get("q_o_fast") is not None else r.get("q_o_al")
            timeline.append({"time": mmss(tt), "optical_ml_min": _r(q_opt), "weight_ml_min": _r(r.get("q_w_al")),
                             "reservoir_g": _r(r.get("w_g"), 1), "drops_total": r.get("o_drops"),
                             "engine_status": r.get("headline")})
    current = None
    if a is not None:
        current = {
            "level": a.level, "headline": a.headline, "flow_state": a.flow_state,
            "findings": [{"level": f.level, "title": f.title, "observed": f.observed,
                          "possible_causes": f.causes} for f in a.findings],
            "best_flow_ml_min": _r(a.q_best), "best_flow_source": a.q_source,
            "optical_flow_ml_min": _r(a.q_o_fast if a.q_o_fast is not None else a.q_o),
            "weight_flow_ml_min": _r(a.q_w), "weight_flow_std_error": _r(a.q_w_se),
            "optical_sensor_health": a.opt_health, "weight_sensor_health": a.w_health,
            "auto_calibrated_drops_per_ml": _r(a.cal_df, 1), "stop_check": a.stop_verdict,
            "remaining_ml": _r(a.remaining_ml, 0), "minutes_left": _r(a.minutes_left, 0),
        }
    return {
        "device": "DripGuard water-only bench prototype (not a medical device)",
        "package_drop_factor_drops_per_ml": settings.drop_factor,
        "expected_flow_ml_min": settings.expected_flow,
        "session_length": mmss(rows[-1]["t"] / 1000) if rows else "00:00",
        "current": current,
        "event_log": [{"time": mmss(e["t"]), "level": e["level"], "event": e["event"]}
                      for e in (snap.get("events") or [])][-40:],
        "timeline_every_10s": timeline[-90:],
    }


def offline_text(task: str, facts: dict, question: str = "") -> str:
    """Deterministic fallback built from the same facts."""
    cur = facts.get("current") or {}
    if task == "incident":
        ev = facts.get("event_log") or []
        if not ev:
            return "No events recorded yet."
        return "Events: " + "; ".join(f"{e['time']} {e['event']}" for e in ev[-15:]) + "."
    if not cur:
        return "No data yet."
    parts = [f"{cur['headline']}."]
    tops = [f for f in cur.get("findings", []) if f["level"] not in ("ok", "info")]
    if tops:
        parts.append(tops[0]["observed"])
        if tops[0]["possible_causes"]:
            parts.append("Possible causes: " + ", ".join(tops[0]["possible_causes"][:3]) + ".")
    if cur.get("optical_flow_ml_min") is not None or cur.get("weight_flow_ml_min") is not None:
        parts.append(f"Optical {cur.get('optical_flow_ml_min')} mL/min, weight "
                     f"{cur.get('weight_flow_ml_min')} mL/min.")
    text = " ".join(parts)
    return ("Gemini unavailable, so this question can't be answered. " + text) if task == "ask" else text


class Explainer:
    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.model_pref = ""             # typed in the sidebar (optional)
        self.model_ok = None             # first model that answered
        self.auto = True                 # explain new alerts automatically
        self.feed: deque[dict] = deque(maxlen=20)
        self.lock = threading.Lock()
        self.busy = 0
        self.last_error: str | None = None
        self.last_auto = 0.0
        self.session = 0                 # bumped on every new data session

    # ---------------------------------------------------------------- status
    def sdk_installed(self) -> bool:
        try:
            import google.genai  # noqa: F401
            return True
        except ImportError:
            return False

    def status(self) -> str:
        if not self.sdk_installed():
            return "google-genai not installed (pip install google-genai) - offline summaries only"
        if not self.api_key:
            return "No API key - offline summaries only"
        s = f"Ready ({self.model_ok})" if self.model_ok else "Ready (model chosen on first call)"
        if self.last_error:
            s += f"  \nLast error: {self.last_error}"
        return s

    # ---------------------------------------------------------------- calls
    def _gemini(self, task: str, facts: dict, question: str) -> tuple[str, str]:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key)
        instruction, limit = TASKS[task]
        prompt = instruction.format(q=question) + "\n\nDATA (JSON):\n" + json.dumps(facts, separators=(",", ":"))
        config = types.GenerateContentConfig(system_instruction=SYSTEM.format(limit=limit), temperature=0.2)
        order = [m for m in [self.model_pref, self.model_ok] + MODELS if m]
        order = list(dict.fromkeys(order))
        last_exc = None
        for model in order:
            try:
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
            except Exception as e:  # model missing -> try the next one; anything else -> stop
                last_exc = e
                msg = str(e).lower()
                if "not found" in msg or "404" in msg or "not supported" in msg:
                    continue
                raise
            text = (getattr(resp, "text", None) or "").strip()
            if not text:
                raise RuntimeError("empty response")
            self.model_ok = model
            return text, model
        raise last_exc or RuntimeError("no model available")

    def generate(self, task: str, facts: dict, question: str = "", auto: bool = False,
                 session: int | None = None) -> dict:
        source, model = "offline", None
        if self.api_key and self.sdk_installed():
            try:
                text, model = self._gemini(task, facts, question)
                source = "gemini"
                self.last_error = None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                text = offline_text(task, facts, question)
        else:
            text = offline_text(task, facts, question)
        item = {"host_t": time.time(), "task": task, "label": TASK_LABEL[task] + (" (auto)" if auto else ""),
                "question": question, "text": text, "source": source, "model": model, "facts": facts}
        with self.lock:
            if session is None or session == self.session:   # drop answers from a previous session
                self.feed.appendleft(item)
        return item

    def request_async(self, task: str, facts_fn, question: str = "", auto: bool = False):
        """facts_fn is called inside the worker thread, so the snapshot is current."""
        session = self.session

        def work():
            with self.lock:
                self.busy += 1
            try:
                self.generate(task, facts_fn(), question, auto, session)
            finally:
                with self.lock:
                    self.busy -= 1
        threading.Thread(target=work, daemon=True).start()

    def maybe_auto(self, event: dict, facts_fn):
        """Explain a new alarm/sensor event automatically, at most once per 20 s."""
        if not self.auto or event.get("level") not in ("alarm", "sensor"):
            return
        now = time.time()
        if now - self.last_auto < 20:
            return
        self.last_auto = now
        self.request_async("now", facts_fn, auto=True)

    def items(self) -> list[dict]:
        with self.lock:
            return list(self.feed)

    def clear(self):
        """New session: empty the feed, reset the auto cooldown, orphan in-flight requests."""
        with self.lock:
            self.feed.clear()
            self.last_auto = 0.0
            self.session += 1
