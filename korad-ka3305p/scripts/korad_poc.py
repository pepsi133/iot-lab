#!/usr/bin/env python3
"""Wide protocol probe for the Korad KA3305P over USB CDC-ACM.

Run it only with NOTHING connected to the outputs. It drives both channels up
to 31 V, toggles the output, switches series/parallel tracking and recalls the
five memory slots. It ends with both channels at 0 V / 10 mA, output OFF.

Findings from the first run (2026-09-28) are in ../README.md, section
"Protocol behavior".

Usage: korad_poc.py [/dev/ttyACM0] > poc-log.jsonl
"""
import json
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
GAP = 0.05  # 20 ms is the measured minimum between commands; keep margin
s = serial.Serial(PORT, 9600, timeout=0)


def emit(**rec):
    print(json.dumps(rec), flush=True)


def read_reply(first=1.0, idle=0.06):
    """The protocol has no terminator: a reply ends when the line goes idle."""
    t0 = time.monotonic()
    buf, last = b"", None
    while True:
        chunk = s.read(64)
        now = time.monotonic()
        if chunk:
            buf, last = buf + chunk, now
        elif last and now - last > idle:
            break
        elif not last and now - t0 > first:
            break
        time.sleep(0.003)
    return buf, (round((last - t0) * 1000) if last else None)


def q(cmd):
    s.reset_input_buffer()
    s.write(cmd.encode())
    b, lat = read_reply()
    time.sleep(GAP)
    return b.decode("latin1"), lat


def w(cmd, settle=0.15):
    s.reset_input_buffer()
    s.write(cmd.encode())
    time.sleep(settle)
    stray, _ = read_reply(0.1)
    emit(section=SEC, op="write", cmd=cmd, stray=stray.decode("latin1"))


def status():
    r, _ = q("STATUS?")
    return ord(r[0]) if r else None


def snap(tag):
    rec = {c: q(c)[0] for c in ("VSET1?", "ISET1?", "VSET2?", "ISET2?",
                                "VOUT1?", "IOUT1?", "VOUT2?", "IOUT2?")}
    st = status()
    emit(section=SEC, op="snap", tag=tag, status=f"0x{st:02x}" if st is not None else None, **rec)


SEC = "identity"
for c in ("*IDN?", "STATUS?"):
    r, lat = q(c)
    emit(section=SEC, op="query", cmd=c, resp=r, lat_ms=lat)

SEC = "start-state"
snap("as-found")
w("OUT0")

SEC = "vset-format"
for v in ("5", "5.0", "05.00", "5.123", "7.5", "12.345", "0"):
    w(f"VSET2:{v}")
    emit(section=SEC, op="readback", sent=v, got=q("VSET2?")[0])

SEC = "range"
for v in ("30.00", "31.00", "35", "99.99", "-1"):
    w(f"VSET2:{v}")
    emit(section=SEC, op="readback", sent=v, got=q("VSET2?")[0])
for v in ("0.5", "5.000", "5.100", "6", "0.0015"):
    w(f"ISET2:{v}")
    emit(section=SEC, op="readback", sent=v, got=q("ISET2?")[0])

SEC = "unknown-commands"
for c in ("FOO?", "VSET3?", "VOUT3?", "*RST", "OUT2:1"):
    r, lat = q(c)
    emit(section=SEC, op="query", cmd=c, resp=r, lat_ms=lat)
w("OUT0")

SEC = "status-bits"
for c in ("OUT1", "OUT0", "OCP1", "OVP1", "OCP0", "OVP0", "BEEP0", "BEEP1", "LOCK1", "LOCK0"):
    w(c)
    emit(section=SEC, op="status", after=c, status=f"0x{status():02x}")

SEC = "command-gap"
for gap in (0, 0.005, 0.01, 0.02, 0.03, 0.05):
    ok = 0
    for i in range(5):
        a, b = f"{1 + i * 0.5:.2f}", f"{10 + i:.2f}"
        s.write(f"VSET1:{a}".encode())
        time.sleep(gap)
        s.write(f"VSET2:{b}".encode())
        time.sleep(0.15)
        ok += q("VSET1?")[0] == f"{float(a):05.2f}" and q("VSET2?")[0] == f"{float(b):05.2f}"
    emit(section=SEC, op="gap", gap_ms=gap * 1000, ok=ok, of=5)

SEC = "memories"
for m in range(1, 6):
    w(f"RCL{m}", 0.5)
    snap(f"M{m}")
w("OUT1", 0.3)
w("RCL1", 0.5)
snap("RCL-while-on")
w("OUT0")

SEC = "tracking"
w("VSET1:2.00"); w("VSET2:6.00"); w("ISET1:0.010"); w("ISET2:0.010")
snap("pre-track")
w("TRACK1", 0.5); snap("series")
w("VSET1:3.00"); snap("series-set-ch1")
w("VSET2:4.00"); snap("series-set-ch2")
w("OUT1", 1.0); snap("series-on")
w("TRACK2", 1.0); snap("parallel-after-switch-while-on")
w("OUT1", 1.0); snap("parallel-on")
w("TRACK0", 1.0); snap("independent-after-switch-while-on")
w("OUT0")

SEC = "rate"
n, t = 0, time.monotonic()
while time.monotonic() - t < 10:
    q("VOUT1?")
    n += 1
emit(section=SEC, op="rate", hz=round(n / 10, 1))

SEC = "park"
for c in ("OUT0", "TRACK0", "VSET1:0.00", "VSET2:0.00", "ISET1:0.010", "ISET2:0.010"):
    w(c)
snap("parked")
