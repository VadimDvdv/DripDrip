#!/usr/bin/env python3
"""DripGuard serial logger.

Prints a compact live line, writes every status line to <name>_status.csv and
every drop to <name>_drops.csv. Type firmware commands (tare, cal 500, df 20, ...)
and press Enter to send them.

    pip install pyserial
    python dg_log.py COM5                 # Windows
    python dg_log.py /dev/ttyUSB0         # Linux
    python dg_log.py /dev/cu.usbserial-0001 --out trial_normal_1   # macOS
"""
import argparse
import csv
import json
import sys
import threading
import time

import serial

ap = argparse.ArgumentParser()
ap.add_argument("port")
ap.add_argument("--baud", type=int, default=921600)
ap.add_argument("--out", default=None, help="file prefix (default dg_<timestamp>)")
args = ap.parse_args()

ser = serial.Serial(args.port, args.baud, timeout=0.2)
prefix = args.out or time.strftime("dg_%Y%m%d_%H%M%S")
status_f = open(prefix + "_status.csv", "w", newline="")
drops_f = open(prefix + "_drops.csv", "w", newline="")
status_w = None
drops_w = csv.writer(drops_f)
drops_w.writerow(["host_time", "t_ms", "amp", "width_ms"])


def send_stdin():
    for line in sys.stdin:
        ser.write((line.strip() + "\n").encode())


threading.Thread(target=send_stdin, daemon=True).start()


def f(v, spec="{:.2f}"):
    return "--" if v is None else spec.format(v)


try:
    while True:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode(errors="replace").strip()
        if not line.startswith("{"):
            if line:
                print(line)
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = m.get("type")
        if kind == "s":
            if status_w is None:
                status_w = csv.DictWriter(status_f, ["host_time"] + list(m.keys()), extrasaction="ignore")
                status_w.writeheader()
            status_w.writerow({"host_time": time.time(), **m})
            status_f.flush()
            print(
                f"OPT {f(m['o_q'])} mL/min  {f(m['o_rate'])} gtt/s  snr {f(m['o_snr'], '{:.0f}')}  "
                f"long {m['o_long']} | WT {f(m['w_q'])} +/- {f(m['w_qse'])} mL/min  "
                f"{f(m['w_g'], '{:.1f}')} g  sd1 {f(m['w_std1'], '{:.2f}')}  "
                f"{'stable' if m['w_stable'] else 'UNSTABLE'}{'' if m['w_ok'] else '  HX711 MISSING'}"
            )
        elif kind == "drop":
            drops_w.writerow([time.time(), m["t"], m["amp"], m["w"]])
            drops_f.flush()
        else:
            print(line)
except KeyboardInterrupt:
    pass
finally:
    status_f.close()
    drops_f.close()
