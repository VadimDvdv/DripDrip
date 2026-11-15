# DripGuard dashboard

Live monitoring, sensor fusion and diagnostics for the DripGuard firmware. Water-only test rig; not for clinical use.

## Run it

```
cd dashboard
pip install -r requirements.txt
streamlit run app.py
```

In the sidebar, pick a source:

- **Simulator** (no hardware needed). The `demo` scenario plays a normal run, a partial clamp, a closed clamp, a blocked beam and a bumped reservoir. Use speed 5–10x while developing.
- **Live (USB serial).** Pick COM3 and click Connect. **Close the Arduino Serial Monitor/Plotter first:** only one program can open the port. The dashboard is the only reader, and it logs every line to `logs/live_<time>.jsonl`, so you don't need `dg_log.py` at the same time.
- **Replay log.** Plays back any recorded session through the same pipeline, so you can tune the diagnostics without rebuilding the rig.

## Files

| File | What it does |
|---|---|
| `app.py` | Streamlit UI. A fragment refreshes every 0.5 s; the sidebar holds settings and device commands |
| `fusion.py` | All diagnostics. Pure Python, no UI, testable |
| `sources.py` | Serial / simulator / replay threads, shared store, logging |
| `sim.py` | Physical drip model emitting the exact firmware JSON (`python sim.py demo > logs/demo.jsonl`) |
| `explain.py` | Gemini explanation layer (status, incident note, Q&A) with offline fallback |

## How the diagnosis works (use this for the Devpost write-up)

1. **Compare like with like.** Optical flow is the least-squares slope of the cumulative drop count; weight flow is the slope of reservoir mass. Both use the **same 120 s window**, plus a 240 s window for slow faults. A real change in flow moves both estimates identically.
2. **The scale knows its own noise.** The real load cell has 0.05 g of fast noise, but also wanders slowly by about ±0.5 g over tens of seconds (air currents and temperature acting on microvolt-level signals). Over a 30 s window that wander looks like ±1 mL/min of fake flow; over 120 s it's ±0.25. Those measured numbers (the *noise profile*) are built into every weight uncertainty, so a disagreement or a stop is declared only when it clearly exceeds what the scale does at rest. **Measure scale noise** re-measures the level at the venue in 3 minutes.
3. **The fast sensor alarms, the slow sensor confirms.** After 8 s with no drops (stretched for slow drips), an alarm fires immediately. The scale then decides, using only data from **after** the stop (so a hand on the clamp can't bias it): if mass loss is clearly below half the previous rate, the stop is confirmed by both sensors; if clearly above, the optical sensor is at fault. The verdict waits until the evidence beats the measured noise by 3x.
4. **Optical signal lost** (receiver at the ADC rail) gets the same treatment: "checking weight", then either "flow continues" or a weight-only no-flow alarm.
5. **Auto-calibration** = slope of mass vs drop count over at least 120 s of clean data. It **locks after 3 minutes**, so a later miscount can't leak into it, and it never trains while the sensors disagree. Reset it from the sidebar.
6. **Health gating and honesty.** Weight is ignored while the reservoir is disturbed. Optical data recorded during an identified optical fault is marked bad afterwards. "Unknown" is a legitimate answer.

## Results

**On the real rig (14 Nov):**

| What | Result |
| --- | --- |
| Load-cell calibration | 427.9 counts/g; returns to 0 ± 0.3 g after unloading |
| Fast weight noise | about 0.05 g |
| At-rest weight-flow error | ±1.08 / ±0.63 / ±0.25 / ±0.14 mL/min over 30 / 60 / 120 / 180 s windows |
| 10.6 min at rest replayed through the engine | 0 false alarms (the old 30 s design gave false disagreements 88% of the time) |
| Optical drop pulse | about 600–700 counts against 1–1.5 counts of noise (SNR about 440–830) |

**Simulated with the measured noise** (20 random runs per fault):

| Fault | Correct | Wrong | Typical time after the fault |
| --- | --- | --- | --- |
| Clamp closed, 6 mL/min | 20/20 | 0 | 37 s |
| Clamp closed, 3 mL/min | 19/20 (1 still undecided at 150 s) | 0 | 76 s |
| Optical blocked, flow continuing, 6 mL/min | 20/20 | 0 | 28 s |
| Optical blocked, flow continuing, 3 mL/min | 18/20 (2 still undecided at 150 s) | 0 | 69 s |
| Normal flow, 2 h | no false findings | — | — |

The "drops stopped" alarm itself is always immediate (about 8 s). **Run the demo at about 6 mL/min (about 2 drops/s):** confirmations are about twice as fast as at 3 mL/min.

**Detectable miscount:** about 25% of drops missed, flagged about 2 minutes after onset. Smaller miscounts hide inside the scale's noise.

## Collecting real evidence

1. Connect live. Use the **Mark what you just did** buttons as you close the clamp, block the beam, and so on. The marks go into the log as ground truth.
2. Record each scenario 2–3 times: normal, partial, stopped, beam blocked, bumped.
3. Replay the logs at 10x to check that every scenario produces the right diagnosis. Detection times are in the event log.

## Gemini explanations (MLH "Best Use of Gemini API")

**The rule:** the diagnosis comes from the sensors, in `fusion.py`. Gemini only explains it, and can't change a verdict.

**Setup:**
1. `pip install google-genai`.
2. Create an API key in Google AI Studio.
3. Paste the key in the sidebar, or `set GEMINI_API_KEY=...` (Windows) before `streamlit run app.py`.

**What it does:**
- **Explain current status:** about 70 words on what the device sees, which sensor evidence supports it, and what to check first.
- **Write incident note:** the session's events in order: which sensor noticed first, which confirmed, how long confirmation took, and how it resolved. Your "Mark what you just did" notes appear in it.
- **Ask:** free questions such as "why did it alarm around 03:10?", answered only from the logged data.
- **Auto mode:** each new alarm or sensor fault gets an explanation automatically, at most one every 20 s.

**How it stays honest:**
- Gemini receives a compact JSON of facts: the current diagnosis with its findings, the event log, and a 10-second timeline of both flow estimates and the mass.
- The system prompt forbids inventing numbers or overriding the engine.
- The UI shows "Facts sent to Gemini" under every answer, so judges can check it.
- Temperature is 0.2.

**If Gemini fails** (no key, no network, quota), an offline summary built from the same facts appears instead, labelled as such. Models are tried in order (`gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-2.5-flash`), and the first that answers is remembered. Model IDs change often; type a different one in the sidebar if needed.

**For the Devpost write-up:** describe this as Gemini as the explanation layer over a deterministic, sensor-fusion diagnosis. The AI never decides anything safety-related; it makes the device's reasoning readable.

## Before judging: 3 settings

1. In the Serial Monitor (before starting the dashboard), send `win 120` once. It's saved in flash and makes the board's own weight flow (OLED standalone mode) use 120 s.
2. In the dashboard: clamp closed, reservoir hanging, nobody touching the table → **Measure scale noise (3 min)**. The result is saved to `noise_profile.json`.
3. Let normal flow run for at least 3 minutes so auto-calibration locks before showing faults.

## Device screen

In Live mode the dashboard sends its diagnosis to the ESP32 twice a second (`disp ...`), so the OLED on the device shows the same verdict as the laptop: STOPPED, OPT FAULT, BUMPED and so on. These commands aren't written to the log. If the dashboard stops, the device notices within 3 s and switches to standalone mode ("SOLO").

## Don't share the dashboard URL with judges

All browser sessions share one engine, and each new session re-applies the sidebar defaults. A judge opening the network URL on a phone would reset your settings and see the Tare and Stop buttons. Show judges the laptop screen instead.

## Tuning

All thresholds are in `fusion.Settings`, and the expected flow, tolerance, drop factor and low-volume level are in the sidebar. If the real load cell is noisier than in simulation (check "Weight noise over the last 1 s"), increase `window_s` to 45. A longer window is slower but more precise.
