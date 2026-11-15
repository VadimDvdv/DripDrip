"""DripGuard dashboard.

    pip install -r requirements.txt
    streamlit run app.py

Close the Arduino Serial Monitor/Plotter first: only one program can use the port.
"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from fusion import Settings
from sim import SCENARIOS
from sources import Runtime, list_serial_ports

st.set_page_config(page_title="DripGuard", layout="wide")


@st.cache_resource
def get_runtime() -> Runtime:
    return Runtime()


rt = get_runtime()

LEVEL_LABEL = {"alarm": "ALARM", "sensor": "SENSOR ISSUE", "warning": "WARNING",
               "unknown": "UNKNOWN", "info": "INFO", "ok": "NORMAL"}


def fmt(v, spec=".2f", none="--"):
    return none if v is None else format(v, spec)


def fmt_minutes(m):
    if m is None:
        return "--"
    m = int(round(m))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m} min"


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("DripGuard")
    st.caption("Water-only test rig. Not for clinical use.")

    st.subheader("Data source")
    mode = st.radio("Mode", ["Live (USB serial)", "Simulator", "Replay log"], label_visibility="collapsed")
    if mode == "Live (USB serial)":
        ports = list_serial_ports()
        if ports:
            port = st.selectbox("Port", [p for p, _ in ports],
                                format_func=lambda d: dict(ports).get(d, d))
            if st.button("Connect", type="primary", width="stretch"):
                rt.start_serial(port)
        else:
            st.warning("No serial ports found. Check the cable/driver, or install pyserial.")
    elif mode == "Simulator":
        scen = st.selectbox("Scenario", list(SCENARIOS))
        speed = st.select_slider("Speed", options=[1, 2, 5, 10, 20], value=1)
        if st.button("Start simulation", type="primary", width="stretch"):
            rt.start_sim(scen, float(speed))
    else:
        logs = rt.list_logs()
        if logs:
            path = st.selectbox("Log file", logs, format_func=lambda p: p.replace("\\", "/").split("/")[-1])
            speed = st.select_slider("Speed", options=[1, 2, 5, 10, 20], value=5)
            if st.button("Replay", type="primary", width="stretch"):
                rt.start_replay(path, float(speed))
        else:
            st.info("No logs yet. Live sessions are saved to dashboard/logs/.")
    if st.button("Stop", width="stretch"):
        rt.stop()
    st.markdown(rt.status_text())

    st.subheader("Monitoring settings")
    exp = st.number_input("Expected flow (mL/min, 0 = off)", 0.0, 60.0, 3.0, 0.5)
    tol = st.slider("Allowed deviation (%)", 10, 80, 30)
    df_nom = st.number_input("Nominal drop factor (drops/mL)", 5.0, 60.0, 20.0, 1.0,
                             help="From the IV set package.")
    low_v = st.number_input("Low-volume alert (mL)", 0.0, 2000.0, 50.0, 10.0)
    autocal = st.toggle("Use auto-calibrated drop volume", value=True)
    rt.apply_settings(Settings(drop_factor=df_nom, expected_flow=exp or None,
                               tolerance=tol / 100, low_volume_ml=low_v, use_autocal=autocal,
                               noise_profile=dict(rt.noise_profile)))
    if st.button("Reset auto-calibration", width="stretch"):
        rt.reset_calibration()

    st.subheader("Scale noise")
    st.caption(f"Weight-flow uncertainty at rest: \u00b1{rt.noise_profile.get(120.0, float('nan')):.2f} mL/min "
               f"over 120 s ({rt.noise_info}). Measure again at the venue: clamp closed, nobody touching "
               "the table, reservoir hanging.")
    n1, n2 = st.columns(2)
    if n1.button("Measure (3 min)", width="stretch", disabled=rt.source is None):
        rt.start_noise_measure()
    if n2.button("Use default", width="stretch"):
        rt.reset_noise_profile()

    st.subheader("Mark what you just did")
    c1, c2 = st.columns(2)
    for i, label in enumerate(["Clamp closed", "Clamp opened", "Clamp partial",
                               "Beam blocked", "Beam cleared", "Reservoir bumped"]):
        if (c1 if i % 2 == 0 else c2).button(label, width="stretch"):
            rt.note(label)
    note = st.text_input("Custom note")
    if st.button("Add note", width="stretch") and note:
        rt.note(note)

    st.subheader("Gemini explanations")
    key = st.text_input("Gemini API key", type="password",
                        placeholder="or set GEMINI_API_KEY before launching",
                        help="Create one in Google AI Studio. Kept in memory only, never logged.")
    if key:
        rt.explainer.api_key = key.strip()
    rt.explainer.model_pref = st.text_input("Model (optional)", value=rt.explainer.model_pref,
                                            placeholder="auto: gemini-3.5-flash, then fallbacks").strip()
    rt.explainer.auto = st.toggle("Explain new alerts automatically", value=rt.explainer.auto)
    st.caption(rt.explainer.status())

    if rt.mode == "serial":
        st.subheader("Device")
        d1, d2 = st.columns(2)
        if d1.button("Tare", width="stretch"):
            rt.send("tare")
        if d2.button("Set empty", width="stretch"):
            rt.send("empty")
        grams = st.number_input("Known mass (g)", 1.0, 5000.0, 500.0, 10.0)
        if st.button("Calibrate with known mass", width="stretch"):
            rt.send(f"cal {grams:g}")
        if st.button("Reset drop counter", width="stretch"):
            rt.send("reset")
        raw_cmd = st.text_input("Raw command", placeholder="e.g. df 20, win 30, k 5")
        if st.button("Send", width="stretch") and raw_cmd:
            rt.send(raw_cmd)


# ---------------------------------------------------------------- live view
@st.fragment(run_every=0.5)
def live_view():
    snap = rt.store.snapshot()
    a = snap["assessment"]
    src = rt.source

    if src is None and a is None:
        st.info("Choose a data source in the sidebar.")
        return
    if snap["age"] is None:
        err = getattr(src, "error", None)
        nb = getattr(src, "rx_bytes", None)
        if err:
            st.error(f"Can't read the port: {err}")
        elif nb == 0:
            st.info("Port open, but the board has sent nothing yet. If this lasts more than "
                    "2 s: press the EN (reset) button on the ESP32, and make sure the Arduino "
                    "Serial Monitor/Plotter is closed.")
        elif nb:
            st.warning(f"Receiving bytes ({nb}) but no valid data lines. Usually a baud-rate "
                       "mismatch (firmware must use 921600) or the board is in download mode: "
                       "press EN (reset).")
        else:
            st.info("Waiting for data...")
        if snap["messages"]:
            st.caption("Last text from the port:")
            st.code("\n".join(snap["messages"][-5:]))
        return
    job = rt.noise_job_status()
    if job and job["status"] == "running":
        left = max(0, 180 - job.get("elapsed", 0))
        st.info(f"Measuring scale noise: keep the rig still, clamp closed. {int(left // 60)}:{int(left % 60):02d} left.")
        st.progress(min(1.0, job.get("elapsed", 0) / 180))
    elif job and job["status"] == "done" and time.time() - job["start"] < 200:
        st.success("Scale noise measured (mL/min at rest per window): " + job["msg"])
    elif job and job["status"] == "failed" and time.time() - job["start"] < 200:
        st.error("Scale noise measurement failed: " + job["msg"])
    if src is not None and not src.done and snap["age"] > 3:
        st.error(f"No data from the device for {snap['age']:.0f} s. Check the USB connection. "
                 "This is a communication problem, not evidence that flow stopped.")

    # status banner
    banner = {"alarm": st.error, "sensor": st.warning, "warning": st.warning,
              "unknown": st.info, "info": st.info, "ok": st.success}[a.level]
    banner(f"**{LEVEL_LABEL[a.level]}: {a.headline}**")

    # key numbers
    c = st.columns(6)
    c[0].metric("Flow (best estimate)", f"{fmt(a.q_best)} mL/min",
                help=f"Source: {a.q_source or 'none'}")
    c[1].metric("Optical flow", f"{fmt(a.q_o_fast if a.q_o_fast is not None else a.q_o)} mL/min",
                help="Drop count over the last 12 s x calibrated drop volume")
    c[2].metric("Weight flow", f"{fmt(a.q_w)} mL/min",
                delta=None if a.q_w_se is None else f"± {a.q_w_se:.2f}", delta_color="off",
                help="Slope of reservoir mass over the comparison window")
    c[3].metric("Remaining", f"{fmt(a.remaining_ml, '.0f')} mL")
    c[4].metric("Time left", fmt_minutes(a.minutes_left))
    cal_txt = f"{a.cal_df:.1f}" if a.cal_df else "--"
    c[5].metric("Drop factor (auto-cal)", f"{cal_txt} gtt/mL",
                delta=f"nominal {rt.store.engine.s.drop_factor:g}", delta_color="off")

    # findings
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Diagnosis")
        shown = [f for f in a.findings if f.level != "ok"]
        if not shown:
            st.write("All checks passing. Both sensors healthy and consistent.")
        for f in shown:
            with st.container(border=True):
                st.markdown(f"**{LEVEL_LABEL[f.level]} - {f.title}**  \n"
                            f"Observed: {f.observed}")
                if f.causes:
                    st.caption("Possible causes: " + "; ".join(f.causes))
    with right:
        st.subheader("Sensor health")
        last = snap["rows"][-1] if snap["rows"] else {}
        st.markdown(
            f"**Optical:** {a.opt_health}  \n"
            f"SNR {fmt(last.get('o_snr'), '.1f')}, baseline {fmt(last.get('o_base'), '.0f')}, "
            f"drops {last.get('o_drops', '--')}, long events {last.get('o_long', '--')}  \n"
            f"**Weight:** {a.w_health}  \n"
            f"mass {fmt(last.get('w_g'), '.1f')} g, 1-s noise {fmt(last.get('w_std1'), '.2f')} g, "
            f"window {fmt(last.get('w_span'), '.0f')} s")
        if a.stop_verdict:
            st.caption(f"Stop check: {a.stop_verdict}")

    # charts
    rows = snap["rows"]
    if len(rows) > 2:
        df = pd.DataFrame(rows)
        df["min"] = (df["t"] - df["t"].iloc[0]) / 60000.0
        df = df.set_index("min")
        def num(cols):   # all-None columns are object dtype; Streamlit charts need floats
            return df[cols].apply(pd.to_numeric, errors="coerce").astype(float)
        opt = num(["q_o_fast"])["q_o_fast"].fillna(num(["q_o_al"])["q_o_al"])
        g1, g2 = st.columns(2)
        with g1:
            st.caption("Flow, mL/min (x: minutes)")
            st.line_chart(pd.DataFrame({"optical": opt, "weight": num(["q_w_al"])["q_w_al"]}), height=230)
        with g2:
            st.caption("Reservoir mass, g")
            st.line_chart(num(["w_g"]), height=230)
        g3, g4 = st.columns(2)
        with g3:
            st.caption("Optical drop SNR")
            st.line_chart(num(["o_snr"]), height=160)
        with g4:
            st.caption("Weight noise over the last 1 s, g")
            st.line_chart(num(["w_std1"]), height=160)

    # events
    st.subheader("Event log")
    if snap["events"]:
        ev = pd.DataFrame(snap["events"])[::-1]
        ev["time"] = ev["t"].map(lambda s: f"{int(s // 60):02d}:{int(s % 60):02d}")
        st.dataframe(ev[["time", "level", "event"]], hide_index=True, width="stretch", height=240)
    if snap["messages"]:
        with st.expander("Device messages"):
            st.code("\n".join(snap["messages"]))


live_view()

# ---------------------------------------------------------------- Gemini
st.subheader("Ask Gemini about this session")
st.caption("The diagnosis above comes from the sensors. Gemini only explains it, using the logged "
           "facts it is shown, and can't change a verdict.")
b1, b2 = st.columns(2)
if b1.button("Explain current status", width="stretch"):
    rt.explainer.request_async("now", rt.ai_facts)
if b2.button("Write incident note", width="stretch"):
    rt.explainer.request_async("incident", rt.ai_facts)
with st.form("ask", clear_on_submit=True, border=False):
    q = st.text_input("Question", placeholder="e.g. Why did it alarm around 03:10?", label_visibility="collapsed")
    if st.form_submit_button("Ask") and q.strip():
        rt.explainer.request_async("ask", rt.ai_facts, q.strip())


@st.fragment(run_every=1.0)
def ai_feed():
    ex = rt.explainer
    if ex.busy:
        st.caption("Gemini is writing...")
    items = ex.items()
    for i, it in enumerate(items[:5]):
        with st.container(border=True):
            when = pd.Timestamp(it["host_t"], unit="s", tz="UTC").tz_convert("America/New_York").strftime("%H:%M:%S")
            src = f"Gemini ({it['model']})" if it["source"] == "gemini" else "offline summary"
            head = f"**{it['label']}**" + (f" - {it['question']}" if it["question"] else "")
            st.markdown(f"{head}  \n{it['text']}")
            st.caption(f"{when} \u00b7 {src}")
            if i == 0:
                with st.expander("Facts sent to Gemini"):
                    st.json(it["facts"], expanded=False)


ai_feed()
