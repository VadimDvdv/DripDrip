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

1. **Compare like with like.** Optical flow is the least-squares slope of the cumulative drop count, and weight flow is the slope of reservoir mass, both over the **same 30 s window**. A real change in flow moves both estimates the same way, so it doesn't look like a sensor disagreement. A second, 60 s window catches slow, subtle faults with about 2.8x better precision.
2. **The fast sensor alarms, the slow sensor confirms.** When drops stop for 8 s (stretched to 2.5 drop intervals for slow drips, at most 30 s), an alarm is raised immediately. The weight then decides from the **slope of reservoir mass after the stop**, skipping the first 3 s so a hand on the clamp can't bias it: if that flow is clearly below half the previous rate (z < −3), the stop is **confirmed by both sensors**; if clearly above (z > 3), flow is continuing and the **optical sensor is at fault**. The verdict is re-evaluated on every update in both directions, so an early wrong call corrects itself. If the optical receiver sits at the ADC rail (LED off, beam fully dark), the weight alone decides between "optical sensor lost, flow continues" and a weight-only no-flow alarm.
3. **Auto-calibration.** The average drop volume is the slope of mass vs drop count, measured only during clean stretches (both sensors healthy, no disagreement, at least 60 s and 40 drops). It's rejected if it lands more than 25% from the package value, because that more likely means a counting fault than a strange drip set.
4. **Health gating.** Weight is ignored while the reservoir is disturbed and until the disturbance leaves the window. Optical data recorded during an identified optical fault is marked bad afterwards. "Unknown" is a legitimate answer.
5. **Findings separate what was observed from what might explain it.** Two sensors can't uniquely identify every fault, so causes are listed as possibilities.

## Measured on the simulator (not hardware)

These numbers come from simulation, not the real rig. Collect real data and replace them in the write-up.

| Scenario | Result |
|---|---|
| Normal flow, about 6 h simulated, noise 0.3 g and 0.6 g | 0 false alarms in simulation (real-rig numbers pending; at 1.0 g noise expect "weight unstable" flags) |
| Clamp closed at 3 mL/min | Alarm about 7 s after the last drop, confirmed by both sensors about 16 s after the clamp |
| Hand on the clamp shifts the hanging load by −1 to +2 g | Correct verdict in 10/10 runs per step size |
| Beam blocked, flow continuing | "Optical sensor not detecting drops - flow continues" within about 12 s |
| Beam dark (receiver at ADC rail), flow continuing | "Optical sensor lost - flow continues" immediately |
| Partial clamp (3 → 1.5 mL/min) | Low-flow warning within about 7 s |
| Reservoir bumped | Weight marked unstable at once; optical flow carries on |
| Optical misses 25% of drops | Flagged in most runs within about 45 s. Claim a detectable miscount of about 25–30%; a 20% miss is flagged only some of the time |
| Auto-calibration (true 18 drops/mL, nominal 20) | Converges to 17.8–18.0 drops/mL after the first clean minute |

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

## Device screen

In Live mode the dashboard sends its diagnosis to the ESP32 twice a second (`disp ...`), so the OLED on the device shows the same verdict as the laptop: STOPPED, OPT FAULT, BUMPED and so on. These commands aren't written to the log. If the dashboard stops, the device notices within 3 s and switches to standalone mode ("SOLO").

## Don't share the dashboard URL with judges

All browser sessions share one engine, and each new session re-applies the sidebar defaults. A judge opening the network URL on a phone would reset your settings and see the Tare and Stop buttons. Show judges the laptop screen instead.

## Tuning

All thresholds are in `fusion.Settings`, and the expected flow, tolerance, drop factor and low-volume level are in the sidebar. If the real load cell is noisier than in simulation (check "Weight noise over the last 1 s"), increase `window_s` to 45. A longer window is slower but more precise.
