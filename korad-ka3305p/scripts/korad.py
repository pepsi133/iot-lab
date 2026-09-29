#!/usr/bin/env python3
"""korad: control a Korad KA3305P bench supply through one daemon.

The daemon owns the serial port. Every other command is a client. A guard
limits the voltage, the current and the mode for a time window, so a misclick
or a cut input cannot set a dangerous value. See ../DESIGN.md and ../README.md.

Needs Python 3.11+ and pyserial (not for --fake).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
import datetime as dt
import errno
import fcntl
import io
import itertools
import math
import json
import os
import queue
import re
import select
import signal
import socket
import socketserver
import subprocess
import sys
import termios
import threading
import time
import tomllib
from collections import deque
from decimal import Decimal, InvalidOperation
from pathlib import Path

VID, PID = "0416", "5011"
DEFAULT_USB_ID = f"{VID}:{PID}"
USBIPD_DEFAULT_PATH = "/mnt/c/Program Files/usbipd-win/usbipd.exe"
MAX_CV = 3100   # hardware ceiling, 31.00 V, in centivolts (measured)
MAX_MA = 5100   # hardware ceiling, 5.100 A, in milliamps (measured)
WRITE_GAP = 0.03  # measured minimum 20 ms between a write and the next command
MODES = {0: "independent", 1: "series", 2: "parallel"}
MODE_CMD = {"independent": "TRACK0", "series": "TRACK1", "parallel": "TRACK2"}

EXIT = {"E_INTERNAL": 1, "E_USAGE": 2, "E_GUARD": 3, "E_VERIFY": 4,
        "E_DEVICE": 5, "E_DAEMON": 5, "E_CONFIRM": 6, "E_SUPERSEDED": 7}


class KoradError(Exception):
    def __init__(self, code: str, message: str, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self):
        d = {"code": self.code, "message": self.message}
        if self.detail is not None:
            d["detail"] = self.detail
        return d


# ---------------------------------------------------------------- paths

def home_dirs():
    base = os.environ.get("KORAD_HOME")
    if base:
        b = Path(base)
        return b / "config", b / "state"
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "korad"
    st = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "korad"
    return cfg, st


def socket_path():
    return Path(os.environ.get("KORAD_SOCKET") or home_dirs()[1] / "korad.sock")


# ---------------------------------------------------------------- units

_VAL = re.compile(r"^ *([0-9]+(?:\.[0-9]*)?|\.[0-9]+) *([a-zA-Z]*) *\Z", re.ASCII)
_UNITS = {"v": {"": 1, "V": 1, "v": 1, "mV": Decimal("0.001"), "mv": Decimal("0.001")},
          "i": {"": 1, "A": 1, "a": 1, "mA": Decimal("0.001"), "ma": Decimal("0.001")}}


def parse_value(text: str, kind: str, ceiling: int | None = None) -> int:
    """Parse a voltage ('v') to centivolts or a current ('i') to milliamps.

    Refuses anything the supply would ignore in silence: more precision than
    10 mV / 1 mA, a negative value, or a value above the hardware ceiling.
    """
    what = "voltage" if kind == "v" else "current"
    m = _VAL.match(str(text))
    if not m:
        raise KoradError("E_USAGE", f"cannot read {what} {text!r}: use a number with an "
                         f"optional unit, for example 3.3, 3.3V, 3300mV, 0.05A, 50mA")
    num, unit = Decimal(m.group(1)), m.group(2)
    units = _UNITS[kind]
    if unit not in units:
        if unit.lower() in units and unit[0] == "M":
            raise KoradError("E_USAGE", f"unit {unit!r}: M means mega; use "
                             f"{'mV' if kind == 'v' else 'mA'} for milli")
        raise KoradError("E_USAGE", f"unit {unit!r} is not valid for a {what}: "
                         f"use {'V or mV' if kind == 'v' else 'A or mA'}")
    base = num * units[unit]
    scale = 100 if kind == "v" else 1000
    scaled = base * scale
    if scaled != scaled.to_integral_value():
        step = "0.01 V (10 mV)" if kind == "v" else "0.001 A (1 mA)"
        raise KoradError("E_USAGE", f"{what} {text!r} is finer than the supply's step of "
                         f"{step}; the supply ignores such a value, so it is refused, not rounded")
    n = int(scaled)
    ceiling = ceiling or (MAX_CV if kind == "v" else MAX_MA)
    if n > ceiling:
        hint = ""
        if unit == "":
            small = num * Decimal("0.001") * scale
            if small == small.to_integral_value() and small <= ceiling:
                hint = f"; did you mean {m.group(1)}{'mV' if kind == 'v' else 'mA'}?"
        raise KoradError("E_USAGE", f"{what} {text!r} is above the hardware ceiling "
                         f"{fmt_v(ceiling) if kind == 'v' else fmt_i(ceiling)}{hint}")
    return n


def fmt_v(cv):
    return "none" if cv is None else f"{cv / 100:.2f} V"


def fmt_i(ma):
    return "none" if ma is None else f"{ma / 1000:.3f} A"


def parse_duration(text: str) -> float:
    if re.fullmatch(r" *[0-9]+(?:\.[0-9]+)? *", text or ""):
        return float(text)                      # a bare number is seconds
    m = re.fullmatch(r" *(?:([0-9]+)h)? *(?:([0-9]+)m)? *(?:([0-9]+(?:\.[0-9]+)?)s)? *", text or "")
    if not m or not any(m.groups()):
        raise KoradError("E_USAGE", f"cannot read duration {text!r}: use for example 90s, 30m, 4h, 1h30m")
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


MIN_GUARD_S = 60
MAX_GUARD_S = 30 * 86400


def parse_guard_duration(text: str) -> float:
    """--for needs a unit: a cut '4h' must not become a 4-second guard."""
    if re.fullmatch(r" *[0-9]+(?:\.[0-9]+)? *", text or ""):
        raise KoradError("E_USAGE", f"--for {text!r} has no unit: write it with one, "
                         f"for example 90s, 30m, 4h or 1h30m")
    s = parse_duration(text)
    if s > MAX_GUARD_S:
        raise KoradError("E_USAGE", f"--for {text!r} is longer than the maximum of 30 days")
    return s


def validate_guard_window(g: dict, now: dt.datetime | None = None):
    now = now or now_local()
    left = (dt.datetime.fromisoformat(g["expires_utc"]) - now).total_seconds()
    if left < MIN_GUARD_S:
        what = "is already past" if left <= 0 else f"is only {fmt_delta(left)} away"
        raise KoradError("E_USAGE", f"the guard expiry {what} (now: {fmt_local(now)}); a guard "
                         f"must last at least {MIN_GUARD_S} s. Check --for/--until/--today")


# ---------------------------------------------------------------- time

def wall_time() -> float:
    """The wall clock (a separate function, so tests can move it)."""
    return time.time()


CLOCK_JUMP_S = 60   # a wall-clock step larger than this between two polls is reported
# Output state: STATUS? bit 6 or bit 7 means ON (measured). After a mains power cycle
# the supply came up with the output ON and read 0x83 (bit 6 clear, bit 7 set); a front-panel
# ON state read 0x43 (bit 6 set, bit 7 clear). OUT1 gives both bits, OUT0 clears both.
OUTPUT_BITS = 0xC0
MEASURED_ON_V = 0.10     # a measured VOUT above this while STATUS says OFF counts as voltage present
OFF_DECAY_S = 1.5        # the output needs up to ~1.1 s to fall to 0 V after OFF (measured)


def now_local():
    return dt.datetime.now().astimezone()


def fmt_local(t: dt.datetime) -> str:
    t = t.astimezone()
    off = t.strftime("%z")
    return f"{t:%Y-%m-%d %H:%M:%S} {t.tzname()} ({off[:3]}:{off[3:]})"


def fmt_delta(seconds: float) -> str:
    s = int(abs(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


def next_local_time(hhmm: str, now: dt.datetime | None = None) -> dt.datetime:
    m = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})", hhmm or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise KoradError("E_USAGE", f"cannot read time {hhmm!r}: use HH:MM, for example 18:00")
    now = now or now_local()
    # Build the target as a naive local time and resolve its offset on its own day:
    # on a DST change day the offset at the target differs from the offset now.
    naive = now.astimezone().replace(tzinfo=None, hour=int(m.group(1)), minute=int(m.group(2)),
                                     second=0, microsecond=0)
    t = naive.astimezone()
    if t <= now:
        t = (naive + dt.timedelta(days=1)).astimezone()
    return t


# ---------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "poll_hz": 5, "setpoint_every_s": 1.0, "on_live_violation": "off",
    "on_recall_violation": "zero", "stop_action": "off_and_zero",
    "no_guard_warning": True, "today_ends": "10:00", "port": "", "usb_serial": "",
    "usb_id": DEFAULT_USB_ID, "usbipd_reattach": None, "usbipd_path": "",
    "autostart": True, "idle_stop_after_s": 300, "fake": False, "wait_for_device_s": 15,
}
_CHOICES = {"on_live_violation": ("off", "warn"), "on_recall_violation": ("zero", "warn"),
            "stop_action": ("off_and_zero", "off")}


def load_config(path: Path | None = None) -> dict:
    path = path or home_dirs()[0] / "config.toml"
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise KoradError("E_USAGE", f"{path}: {e}")
        unknown = set(data) - set(DEFAULT_CONFIG)
        if unknown:
            raise KoradError("E_USAGE", f"{path}: unknown key(s) {sorted(unknown)}; "
                             f"valid keys: {sorted(DEFAULT_CONFIG)}")
        cfg.update(data)
    for k, allowed in _CHOICES.items():
        if cfg[k] not in allowed:
            raise KoradError("E_USAGE", f"config {k} = {cfg[k]!r}: must be one of {allowed}")
    cfg["poll_hz"] = check_poll(cfg["poll_hz"], f"config poll_hz in {path}")
    next_local_time(cfg["today_ends"])
    cfg["usb_id"] = check_usb_id(cfg["usb_id"], f"config usb_id in {path}")
    cfg["usb_serial"] = check_usb_serial(cfg["usb_serial"], f"config usb_serial in {path}")
    if cfg["usbipd_reattach"] is None:
        cfg["usbipd_reattach"] = "wsl" if (running_in_wsl() and usbipd_exe(cfg["usbipd_path"])) else "off"
    elif cfg["usbipd_reattach"] not in ("off", "wsl"):
        raise KoradError("E_USAGE", f"config usbipd_reattach = {cfg['usbipd_reattach']!r}: "
                         f"must be \"off\" or \"wsl\"")
    if not isinstance(cfg["usbipd_path"], str):
        raise KoradError("E_USAGE", "config usbipd_path must be a string")
    for key in ("autostart", "fake"):
        if not isinstance(cfg[key], bool):
            raise KoradError("E_USAGE", f"config {key} = {cfg[key]!r} in {path}: must be true or false")
    wait = cfg["wait_for_device_s"]
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or wait < 0:
        raise KoradError("E_USAGE", f"config wait_for_device_s = {wait!r} in {path}: a number of "
                         f"seconds, 0 or more (0 = do not wait for the supply)")
    idle = cfg["idle_stop_after_s"]
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not math.isfinite(idle) or idle < 0:
        raise KoradError("E_USAGE", f"config idle_stop_after_s = {idle!r} in {path}: a number of "
                         f"seconds >= 0 (0 = never stop)")
    return cfg


def check_usb_id(value, what="--usb-id") -> str:
    """VID:PID as 4+4 ASCII hex digits, returned in lowercase."""
    s = value.strip().lower() if isinstance(value, str) else value
    if not isinstance(s, str) or not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", s, re.ASCII):
        raise KoradError("E_USAGE", f"{what} = {value!r}: use VID:PID in hex, for example 0416:5011")
    return s


def check_usb_serial(value, what="--usb-serial") -> str:
    """A USB iSerial string: printable ASCII, no spaces, at most 64 characters; empty = any."""
    if not isinstance(value, str) or (value and not re.fullmatch(r"[\x21-\x7e]{1,64}", value, re.ASCII)):
        raise KoradError("E_USAGE", f"{what} = {value!r}: use the USB serial string (printable "
                         f"ASCII, no spaces, at most 64 characters), for example ABC123")
    return value


def running_in_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


def usbipd_exe(configured: str = "") -> str:
    """Path to usbipd.exe: the configured one, else on PATH, else the default install path. '' if none."""
    import shutil
    if configured:
        return configured if (Path(configured).exists() or shutil.which(configured)) else ""
    found = shutil.which("usbipd.exe")
    if found:
        return found
    return USBIPD_DEFAULT_PATH if Path(USBIPD_DEFAULT_PATH).exists() else ""


def check_poll(value, what="--poll"):
    """A poll rate is "max" or a finite number of at least 1 Hz: slower polling
    delays the live guard check, so it is refused."""
    if isinstance(value, str):
        s = value.strip().lower()
        if s == "max":
            return "max"
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", s, re.ASCII):
            raise KoradError("E_USAGE", f"{what} = {value!r}: use a number of Hz (1 or more) or max")
        value = float(s)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 1:
        raise KoradError("E_USAGE", f"{what} = {value!r}: use a number of Hz (1 or more) or max; "
                         f"a slower poll delays the live guard check")
    return value


# ---------------------------------------------------------------- device

def decode_status(b: int) -> dict:
    return {"raw": f"0x{b:02x}", "ch1_cv": bool(b & 1), "ch2_cv": bool(b & 2),
            "mode": MODES.get((b >> 2) & 3, "unknown"), "ovp": bool(b & 0x10),
            "ocp": bool(b & 0x20), "output": bool(b & OUTPUT_BITS),
            "bit6": bool(b & 0x40), "bit7": bool(b & 0x80)}


def scan_acm(usb_id: str = DEFAULT_USB_ID, root: Path = Path("/sys/class/tty")) -> list[tuple[str, str]]:
    """(node, USB serial) for every ttyACM node whose USB parent has this VID:PID."""
    vid_want, pid_want = usb_id.split(":")
    found = []
    for tty in sorted(root.glob("ttyACM*")):
        dev = (tty / "device").resolve().parent
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
            pid = (dev / "idProduct").read_text().strip().lower()
            ser = (dev / "serial").read_text().strip() if (dev / "serial").exists() else ""
        except OSError:
            continue
        if (vid, pid) == (vid_want, pid_want):
            found.append((f"/dev/{tty.name}", ser))
    return found


def find_ports(usb_serial: str = "", usb_id: str = DEFAULT_USB_ID, scan=None) -> list[str]:
    """Every ttyACM node of usb_id (and of usb_serial, if given).

    With no usb_serial set, several devices with DIFFERENT serials are refused: picking one
    could drive the wrong supply. Several nodes of one serial (a stale node next to the live
    one after a USB/IP drop) are returned for find_port to probe.
    """
    nodes = (scan or scan_acm)(usb_id)
    if usb_serial:
        return [n for n, s in nodes if s == usb_serial]
    serials = sorted({s for _, s in nodes})
    if len(serials) > 1:
        listed = ", ".join(f"{s or '(no serial)'} on {', '.join(n for n, x in nodes if x == s)}"
                           for s in serials)
        raise KoradError("E_DEVICE", f"{len(serials)} {usb_id} devices with different USB serials are "
                         f"connected ({listed}); set which one to use: usb_serial in "
                         f"~/.config/korad/config.toml (or 'korad daemon start --usb-serial <serial>')")
    return [n for n, _ in nodes]


def probe_korad(name: str, opener=None, timeout=1.0) -> bool:
    """True if the node answers *IDN? with a KORAD identity within `timeout` seconds."""
    if opener is None:
        import serial
        opener = lambda n: serial.Serial(n, 9600, timeout=timeout, write_timeout=timeout)
    try:
        port = opener(name)
    except Exception:
        return False
    try:
        port.reset_input_buffer()
        port.write(b"*IDN?")
        deadline, buf = time.monotonic() + timeout, b""
        while time.monotonic() < deadline and not buf.startswith(b"KORAD"):
            chunk = port.read(64)
            if not chunk and buf:
                break
            buf += chunk
        return buf.startswith(b"KORAD")
    except Exception:
        return False
    finally:
        try:
            port.close()
        except Exception:
            pass


def find_port(usb_serial: str = "", probe=probe_korad, usb_id: str = DEFAULT_USB_ID) -> tuple[str, list[str]]:
    """The node to use, and the matching nodes skipped because they did not answer.

    After a USB/IP drop and re-attach, a stale node can stay behind next to the live one
    (seen: stale ttyACM0, live ttyACM1), so with several matches each is probed.
    """
    found = find_ports(usb_serial, usb_id=usb_id)
    if len(found) == 1:
        return found[0], []
    skipped = []
    for name in found:
        if probe(name):
            return name, skipped
        skipped.append(name)
    if found:
        raise KoradError("E_DEVICE", f"{len(found)} {usb_id} nodes match but none answers *IDN? "
                         f"within 1 s: {', '.join(found)}; re-attach the supply (usbipd attach)")
    raise KoradError("E_DEVICE", f"no {usb_id} serial device found"
                     + (f" with USB serial {usb_serial}" if usb_serial else "")
                     + "; attach it (usbipd attach) or give --port")


# ---------------------------------------------------------------- usbipd re-attach (WSL)

def _run_cmd(argv, timeout):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _parse_instance_id(iid: str):
    """(vid:pid, serial) from a Windows InstanceId such as USB\\VID_0416&PID_5011\\ABC123.

    A last segment with '&' is a Windows port path, not a device serial: serial ''."""
    m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", iid or "")
    if not m:
        return None, ""
    tail = (iid or "").split("\\")[-1]
    return f"{m.group(1)}:{m.group(2)}".lower(), ("" if "&" in tail else tail)


def usbipd_devices(exe: str, run=_run_cmd) -> list[dict]:
    """Devices on the Windows host bus: busid, usb_id, serial (None if unknown), state, client.

    state is attached | shared | not_shared. `usbipd state` (JSON) is preferred; `usbipd list`
    is the fallback, and it carries no serial."""
    try:
        r = run([exe, "state"], 15)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            out = []
            for d in data.get("Devices", []):
                if not d.get("BusId"):
                    continue                # persisted but not connected
                uid, ser = _parse_instance_id(d.get("InstanceId", ""))
                if not uid:
                    continue
                client = d.get("ClientIPAddress")
                shared = bool(d.get("PersistedGuid")) or bool(d.get("IsForced"))
                out.append({"busid": d["BusId"], "usb_id": uid, "serial": ser, "client": client,
                            "state": "attached" if client else ("shared" if shared else "not_shared")})
            return out
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    r = run([exe, "list"], 15)
    if r.returncode != 0:
        raise KoradError("E_DEVICE", f"usbipd list failed: {(r.stderr or r.stdout).strip()[-200:]}")
    out = []
    section = None
    for line in r.stdout.splitlines():
        if line.strip().endswith(":") and not line.startswith(" "):
            section = line.strip().rstrip(":").lower()
            continue
        m = re.match(r"^(\d+-\d+)\s+([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})\s+.*?\s{2,}(\S.*?)\s*$", line)
        if not m or section not in (None, "connected"):
            continue
        st = m.group(3).lower()
        state = "attached" if st.startswith("attached") else ("not_shared" if st.startswith("not shared")
                                                             else "shared")
        out.append({"busid": m.group(1), "usb_id": m.group(2).lower(), "serial": None,
                    "client": None, "state": state})
    return out


def pick_usbipd_device(devs: list[dict], usb_id: str, usb_serial: str = ""):
    """(device or None, reason). Never guesses between devices with different serials."""
    cands = [d for d in devs if d["usb_id"] == usb_id]
    if usb_serial:
        exact = [d for d in cands if d["serial"] == usb_serial]
        if exact:
            return exact[0], ""
        unknown = [d for d in cands if d["serial"] is None]
        if len(cands) == 1 and unknown:
            return unknown[0], "serial not verifiable from 'usbipd list'"
        return None, f"no {usb_id} device with serial {usb_serial} on the Windows host bus"
    if not cands:
        return None, f"no {usb_id} device on the Windows host bus (unplugged or powered off?)"
    serials = {d["serial"] for d in cands}
    if len(cands) > 1 and (len(serials) > 1 or None in serials):
        return None, (f"{len(cands)} {usb_id} devices on the Windows host bus "
                      f"({', '.join(d['busid'] + ' ' + (d['serial'] or '?') for d in cands)}); "
                      f"set usb_serial to choose one")
    return cands[0], ""


class Korad:
    """The wire protocol. Every write is followed by a read-back in the caller."""

    def __init__(self, port):
        self.port = port
        self._ready = 0.0
        self.last_off_cmd = -1e9

    def _wait(self):
        d = self._ready - time.monotonic()
        if d > 0:
            time.sleep(d)

    def query(self, cmd: str, n: int) -> str:
        for _ in range(2):
            self._wait()
            self.port.reset_input_buffer()
            self.port.write(cmd.encode())
            r = self.port.read(n)
            if len(r) == n:
                return r.decode("latin1")
            time.sleep(0.1)
        raise KoradError("E_DEVICE", f"no {n}-byte reply to {cmd} (got {r!r}) after 2 tries")

    def write(self, cmd: str):
        self._wait()
        self.port.write(cmd.encode())
        self._ready = time.monotonic() + WRITE_GAP
        if cmd == "OUT0" or cmd.startswith(("TRACK", "RCL")):
            self.last_off_cmd = time.monotonic()    # the supply turns the output OFF on these

    def idn(self) -> str:
        self._wait()
        self.port.reset_input_buffer()
        self.port.write(b"*IDN?")
        buf, t0 = b"", time.monotonic()
        while time.monotonic() - t0 < 1.0:
            c = self.port.read(64)
            buf += c
            if buf and not c:
                break
        return buf.decode("latin1")

    def status(self) -> dict:
        return decode_status(ord(self.query("STATUS?", 1)))

    def measured(self) -> dict:
        return {"vout1": float(self.query("VOUT1?", 5)), "iout1": float(self.query("IOUT1?", 5)),
                "vout2": float(self.query("VOUT2?", 5)), "iout2": float(self.query("IOUT2?", 5))}

    def setpoints(self) -> dict:
        """Setpoints as integers: centivolts and milliamps."""
        return {"v1": round(float(self.query("VSET1?", 5)) * 100),
                "i1": round(float(self.query("ISET1?", 5)) * 1000),
                "v2": round(float(self.query("VSET2?", 5)) * 100),
                "i2": round(float(self.query("ISET2?", 5)) * 1000)}

    def set_v(self, ch: int, cv: int) -> int:
        return self._set_verified(f"VSET{ch}:{cv / 100:.2f}", f"VSET{ch}?", 100, cv)

    def set_i(self, ch: int, ma: int) -> int:
        return self._set_verified(f"ISET{ch}:{ma / 1000:.3f}", f"ISET{ch}?", 1000, ma)

    def _set_verified(self, cmd, q, scale, want):
        got = None
        for _ in range(2):
            self.write(cmd)
            got = round(float(self.query(q, 5)) * scale)
            if got == want:
                return got
        raise KoradError("E_VERIFY", f"{cmd} did not take: read-back {q} = {got}, wanted {want}",
                         {"command": cmd, "readback": got, "wanted": want})

    def set_bit(self, cmd_on_off: tuple[str, str], key: str, on: bool) -> dict:
        st = None
        for _ in range(2):
            self.write(cmd_on_off[0] if on else cmd_on_off[1])
            time.sleep(0.05)
            st = self.status()
            if st[key] == on:
                return st
        raise KoradError("E_VERIFY", f"{key} did not change to {'on' if on else 'off'}: "
                         f"status {st['raw']}", st)

    def set_output(self, on: bool) -> dict:
        return self.set_bit(("OUT1", "OUT0"), "output", on)

    def set_mode(self, mode: str) -> dict:
        st = None
        for _ in range(2):
            self.write(MODE_CMD[mode])
            time.sleep(0.3)
            st = self.status()
            if st["mode"] == mode:
                return st
        raise KoradError("E_VERIFY", f"mode did not change to {mode}: status {st['raw']}", st)


class FakeSerial:
    """A simulated KA3305P with the behavior measured on a real unit.

    Silent rejection of bad input, a lost command when it arrives less than
    20 ms after a write, TRACK copying CH2 into CH1, and OUT off on TRACK/RCL.
    """
    _CMD = re.compile(r"^(VSET|ISET)([12]):(\d+(?:\.\d*)?|\.\d+)$|^(VSET|ISET|VOUT|IOUT)([12])\?$"
                      r"|^OUT([01])$|^TRACK([012])$|^(OVP|OCP|BEEP|LOCK)([01])$|^(RCL|SAV)([1-5])$"
                      r"|^STATUS\?$|^\*IDN\?$")

    def __init__(self, load_ohms=1000.0):
        self.v = [0, 3000, 3000]      # index 1, 2: centivolts
        self.i = [0, 10, 10]          # milliamps
        self.out = False
        self.mode = 0
        self.ovp = self.ocp = False
        self.mem = {n: (0, 10, 0, 10) for n in range(1, 6)}
        self.load = load_ohms
        self.buf = b""
        self.last_write = -1.0
        self.lost = []
        self.lock = threading.Lock()
        # Which STATUS? bits the fake reports while the output is ON. 0xC0 is the normal case
        # (OUT1); 0x80 or 0x40 copy the single-bit ON states seen on the real supply; 0 makes the
        # status lie completely (only the measured voltage shows the output is ON).
        self.out_bits = 0xC0
        # hang=True: every read/write blocks until the port is closed, like a dead USB/IP link
        # whose vhci node stayed behind (seen after a Windows sleep).
        self.hang = False
        self._closed = threading.Event()

    def _hung(self):
        if self.hang:
            self._closed.wait(60)
            raise OSError("port closed while the device was hung")

    def reset_input_buffer(self):
        with self.lock:
            self.buf = b""

    def read(self, n):
        self._hung()
        with self.lock:
            r, self.buf = self.buf[:n], self.buf[n:]
            return r

    def close(self):
        self._closed.set()

    def _vout(self, ch):
        if not self.out:
            return 0
        v = self.v[2] if self.mode else self.v[ch]
        i_lim = self.i[2] if self.mode else self.i[ch]
        if self.load and v / 100 / self.load * 1000 > i_lim:
            return round(i_lim / 1000 * self.load * 100)
        return v

    def _iout(self, ch):
        if not self.out or not self.load:
            return 0
        return round(self._vout(ch) / 100 / self.load * 1000)

    def write(self, data: bytes):
        self._hung()
        now = time.monotonic()
        cmd = data.decode("latin1")
        with self.lock:
            if now - self.last_write < 0.02:
                self.lost.append(cmd)
                return len(data)
            m = self._CMD.match(cmd)
            if not m:
                return len(data)
            if cmd == "STATUS?":
                b = ((self._vout(1) == (self.v[2] if self.mode else self.v[1]) or not self.out)
                     | (self._vout(2) == self.v[2] or not self.out) << 1
                     | self.mode << 2 | self.ovp << 4 | self.ocp << 5)
                self.buf += bytes([b | (self.out_bits if self.out else 0)])
            elif cmd == "*IDN?":
                self.buf += b"KORAD KA3305P V7.2 SN:FAKE0000"
            elif m.group(1):
                self.last_write = now
                kind, ch, val = m.group(1), int(m.group(2)), Decimal(m.group(3))
                if self.mode and ch == 1:
                    return len(data)
                if kind == "VSET":
                    if val.as_tuple().exponent < -2 or val > Decimal("31.00"):
                        return len(data)
                    self.v[ch] = int(val * 100)
                    if self.mode:
                        self.v[1] = self.v[2]
                else:
                    if val.as_tuple().exponent < -3 or val > Decimal("5.100"):
                        return len(data)
                    self.i[ch] = int(val * 1000)
                    if self.mode:
                        self.i[1] = self.i[2]
            elif m.group(4):
                kind, ch = m.group(4), int(m.group(5))
                val = {"VSET": f"{self.v[ch] / 100:05.2f}", "ISET": f"{self.i[ch] / 1000:.3f}",
                       "VOUT": f"{self._vout(ch) / 100:05.2f}", "IOUT": f"{self._iout(ch) / 1000:.3f}"}[kind]
                self.buf += val.encode()
            elif m.group(6):
                self.last_write = now
                self.out = m.group(6) == "1"
                if self.out:
                    self.out_bits = 0xC0      # OUT1 sets both bits on the real supply
            elif m.group(7):
                self.last_write = now
                self.mode = int(m.group(7))
                self.out = False
                if self.mode:
                    self.v[1], self.i[1] = self.v[2], self.i[2]
            elif m.group(8):
                self.last_write = now
                if m.group(8) in ("OVP", "OCP"):
                    setattr(self, m.group(8).lower(), m.group(9) == "1")
            elif m.group(10):
                self.last_write = now
                n = int(m.group(11))
                if m.group(10) == "SAV":
                    self.mem[n] = (self.v[1], self.i[1], self.v[2], self.i[2])
                else:
                    self.v[1], self.i[1], self.v[2], self.i[2] = self.mem[n]
                    self.out = False
        return len(data)


# ---------------------------------------------------------------- guard

def guard_is_active(g: dict | None, now: dt.datetime | None = None) -> bool:
    if not g:
        return False
    now = now or now_local()
    return dt.datetime.fromisoformat(g["expires_utc"]) > now


def guard_limit(g: dict, ch: int, key: str):
    """Effective limit for a channel: the channel's own entry, else the global one."""
    for scope in (str(ch), "all"):
        lim = g["limits"].get(scope)
        if lim and lim.get(key) is not None:
            return lim[key]
    return None


def guard_violations(g: dict | None, mode: str, sp: dict) -> list[str]:
    """Check setpoints (cV/mA) and mode against an active guard. [] means safe."""
    if not guard_is_active(g):
        return []
    out = []
    allowed = {"independent", g.get("mode") or "independent"}
    if mode not in allowed:
        out.append(f"mode {mode} is not allowed by the guard (allowed: {', '.join(sorted(allowed))})")
    if mode == "independent":
        for ch in (1, 2):
            mv, mi = guard_limit(g, ch, "max_cv"), guard_limit(g, ch, "max_ma")
            if mv is not None and sp[f"v{ch}"] > mv:
                out.append(f"CH{ch} {fmt_v(sp[f'v{ch}'])} is above the guard's {fmt_v(mv)}")
            if mi is not None and sp[f"i{ch}"] > mi:
                out.append(f"CH{ch} {fmt_i(sp[f'i{ch}'])} is above the guard's {fmt_i(mi)}")
    else:
        mv, mi = guard_limit(g, 2, "max_cv"), guard_limit(g, 2, "max_ma")
        v = sp["v2"] * (2 if mode == "series" else 1)
        i = sp["i2"] * (2 if mode == "parallel" else 1)
        if mv is not None and v > mv:
            out.append(f"{mode} output {fmt_v(v)} is above the guard's {fmt_v(mv)}")
        if mi is not None and i > mi:
            out.append(f"{mode} output {fmt_i(i)} is above the guard's {fmt_i(mi)}")
    return out


MEASURED_MARGIN_CV = 2   # a measured VOUT this far above the limit (0.02 V) counts as a violation


def measured_violations(g: dict | None, mode: str, m: dict) -> list[str]:
    """Measured output voltage against the guard, checked on every sample while the output
    is ON. It catches a front-panel knob before the next setpoint poll. IOUT is not checked:
    turn-on peaks exceed the steady current (58 mA measured at 31 V into 1 kOhm)."""
    if not guard_is_active(g) or not m:
        return []
    out = []
    if mode == "independent":
        for ch in (1, 2):
            lim = guard_limit(g, ch, "max_cv")
            got = round(m[f"vout{ch}"] * 100)
            if lim is not None and got > lim + MEASURED_MARGIN_CV:
                out.append(f"CH{ch} measured {fmt_v(got)} is above the guard's {fmt_v(lim)}")
    else:
        lim = guard_limit(g, 2, "max_cv")
        got = round((m["vout1"] + m["vout2"]) * 100) if mode == "series" else round(m["vout2"] * 100)
        if lim is not None and got > lim + MEASURED_MARGIN_CV:
            out.append(f"{mode} output measured {fmt_v(got)} is above the guard's {fmt_v(lim)}")
    return out


def unguarded_live(g: dict | None, mode: str, sp: dict) -> list[str]:
    """Channels that OUT1 would power with no limit at all under an active guard.

    OUT switches both channels, so a guard on one channel alone would let the
    other one come on at any voltage.
    """
    if not guard_is_active(g) or mode != "independent":
        return []
    out = []
    for ch in (1, 2):
        if sp[f"v{ch}"] > 0 and guard_limit(g, ch, "max_cv") is None and guard_limit(g, ch, "max_ma") is None:
            out.append(f"CH{ch} at {fmt_v(sp[f'v{ch}'])} has no limit in the guard, and OUT "
                       f"switches both channels. Set it to 0 V (korad set {ch} -v 0) or add a limit "
                       f"(korad guard set -c {ch} -v ...)")
    return out


def next_steps(bad: list[str], exclude=()) -> list[str]:
    """For a violation on a channel the caller did not touch, name the command that fixes it."""
    hints = []
    for msg in bad:
        m = re.match(r"CH([12]) (\d+\.\d+) (V|A) is above the guard's (\d+\.\d+) (V|A)", msg)
        if m and int(m.group(1)) not in exclude:
            flag = "-v" if m.group(3) == "V" else "-i"
            hints.append(f"lower CH{m.group(1)} first: korad set {m.group(1)} {flag} {m.group(4)} (or less)")
    return hints


def check_guard_shape(g, where="guard") -> dict:
    """Types and keys of a stored guard. A bad file must fail loudly, never
    reach the poller (where it would look like a device error)."""
    def bad(msg):
        raise KoradError("E_USAGE", f"{where}: {msg}. Fix or delete the file, or set a new guard")
    if not isinstance(g, dict):
        bad("the guard must be a JSON object")
    extra = set(g) - {"version", "limits", "mode", "expires_utc", "note", "set_at_utc"}
    if extra:
        bad(f"unknown key(s) {sorted(extra)}")
    lim = g.get("limits")
    if not isinstance(lim, dict) or not lim:
        bad("'limits' must be a non-empty object")
    for scope, entry in lim.items():
        if scope not in ("all", "1", "2"):
            bad(f"limits key {scope!r} must be all, 1 or 2")
        if not isinstance(entry, dict) or set(entry) - {"max_cv", "max_ma"}:
            bad(f"limits[{scope!r}] must be an object with max_cv and/or max_ma")
        for key, val in entry.items():
            if val is not None and (isinstance(val, bool) or not isinstance(val, int) or val < 0):
                bad(f"limits[{scope!r}][{key!r}] must be a whole number (cV or mA) or null, got {val!r}")
    if g.get("mode") not in (None, "independent", "series", "parallel"):
        bad(f"mode {g.get('mode')!r} must be independent, series, parallel or null")
    if g.get("note") is not None and not isinstance(g["note"], str):
        bad("note must be a string")
    try:
        exp = dt.datetime.fromisoformat(g["expires_utc"])
    except (KeyError, TypeError, ValueError):
        bad("'expires_utc' must be an ISO time")
    if exp.tzinfo is None:
        bad(f"'expires_utc' {g['expires_utc']!r} has no UTC offset")
    return g


def load_guard_file(path: Path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise KoradError("E_USAGE", f"{path} is unreadable ({e})")
    try:
        g = json.loads(text)
    except json.JSONDecodeError as e:
        raise KoradError("E_USAGE", f"{path} is not valid JSON ({e}). Fix or delete the file")
    check_guard_shape(g, str(path))
    validate_guard(g)
    return g


def validate_guard(g: dict):
    if not g["limits"]:
        raise KoradError("E_USAGE", "a guard needs at least one limit: give -v and/or -i")
    mode = g.get("mode") or "independent"
    if mode != "independent":
        if set(g["limits"]) != {"all"}:
            raise KoradError("E_USAGE", f"a {mode} guard needs global limits (no -c): "
                             f"the two channels are linked in {mode} mode")
        lim = g["limits"]["all"]
        if mode == "series" and (lim.get("max_cv") or 0) <= MAX_CV:
            raise KoradError("E_USAGE", f"a series guard with max voltage {fmt_v(lim.get('max_cv'))} "
                             f"makes no sense: one channel gives up to {fmt_v(MAX_CV)} alone. "
                             f"Use series only for a total above that")
        if mode == "parallel" and (lim.get("max_ma") or 0) <= MAX_MA:
            raise KoradError("E_USAGE", f"a parallel guard with max current {fmt_i(lim.get('max_ma'))} "
                             f"makes no sense: one channel gives up to {fmt_i(MAX_MA)} alone. "
                             f"Use parallel only for a total above that")
    for scope, lim in g["limits"].items():
        top_v = MAX_CV * (2 if mode == "series" else 1)
        top_i = MAX_MA * (2 if mode == "parallel" else 1)
        if lim.get("max_cv") is not None and lim["max_cv"] > top_v:
            raise KoradError("E_USAGE", f"max voltage {fmt_v(lim['max_cv'])} is above what "
                             f"{mode} mode can give ({fmt_v(top_v)})")
        if lim.get("max_ma") is not None and lim["max_ma"] > top_i:
            raise KoradError("E_USAGE", f"max current {fmt_i(lim['max_ma'])} is above what "
                             f"{mode} mode can give ({fmt_i(top_i)})")


def guard_loosening(old: dict | None, new: dict | None) -> list[str]:
    """Words the user must retype to confirm. [] means the change is not looser."""
    if not guard_is_active(old):
        return []
    if new is None:
        return ["clear"]
    words = []
    for ch in (1, 2):
        for key, fmt in (("max_cv", lambda x: f"{x / 100:.2f}"), ("max_ma", lambda x: f"{x / 1000:.3f}")):
            o, n = guard_limit(old, ch, key), guard_limit(new, ch, key)
            if o is not None and (n is None or n > o):
                w = "unlimited" if n is None else fmt(n)
                if w not in words:
                    words.append(w)
    if (new.get("mode") or "independent") not in ("independent", old.get("mode") or "independent"):
        words.append(new["mode"])
    if dt.datetime.fromisoformat(new["expires_utc"]) < dt.datetime.fromisoformat(old["expires_utc"]):
        words.append(f"{dt.datetime.fromisoformat(new['expires_utc']).astimezone():%H:%M}")
    return words


def describe_guard(g: dict | None, now: dt.datetime | None = None) -> str:
    now = now or now_local()
    if not g:
        return f"guard: NONE (no limits)   now: {fmt_local(now)}"
    exp = dt.datetime.fromisoformat(g["expires_utc"])
    parts = []
    for scope in ("all", "1", "2"):
        lim = g["limits"].get(scope)
        if lim:
            name = "CH1+CH2" if scope == "all" else f"CH{scope}"
            parts.append(f"{name} <= {fmt_v(lim.get('max_cv'))} / {fmt_i(lim.get('max_ma'))}")
    left = (exp - now).total_seconds()
    state = f"left: {fmt_delta(left)}" if left > 0 else f"EXPIRED {fmt_delta(left)} ago (no limits apply)"
    note = f"   note: {g['note']}" if g.get("note") else ""
    return (f"guard: {'; '.join(parts)}   mode: {g.get('mode') or 'independent'}{note}\n"
            f"expires: {fmt_local(exp)}   now: {fmt_local(now)}   {state}")


def no_guard_warning(g: dict | None) -> str:
    if g and not guard_is_active(g):
        exp = dt.datetime.fromisoformat(g["expires_utc"])
        return (f"guard expired at {fmt_local(exp)} ({fmt_delta((now_local() - exp).total_seconds())} ago): "
                f"no limits apply. Check what is connected, then set one: korad guard set -v ... --today")
    return ("no guard is set: no limits apply. Check what is connected, then set one: "
            "korad guard set -v ... --today")


# ---------------------------------------------------------------- daemon

def set_exclusive(fd: int):
    """TIOCEXCL: a second open() of the tty fails with EBUSY (except for root).
    pyserial's exclusive=True is an advisory flock, which a plain open ignores."""
    fcntl.ioctl(fd, termios.TIOCEXCL)


def peer_closed(conn) -> bool:
    """True when the client at the other end of a Unix socket has gone."""
    try:
        r, _, _ = select.select([conn], [], [], 0)
        if not r:
            return False
        return conn.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


SAFE_M1_PROCEDURE = ("keep M1 as a safe slot at 0 V / 0 A: run 'korad set 12 -v 0 -i 0' and "
                     "'korad preset-save 1' (it saves only when M1 holds anything above 0/0), keep "
                     "working presets in M2-M5, and power-cycle once to check that the supply comes up "
                     "at 0 V")


def preset_save_warning(n: int, sp: dict | None = None) -> str | None:
    """Warning for a save into the power-up slot M1. None for other slots.

    Measured: at power-up the supply always loads M1's setpoints (also when
    another slot was used last) and applies the
    front panel's last On/Off state as of power-off. USB OUT0/OUT1 do not change that
    state and this tool cannot set it, so only 0 V setpoints in M1 make power-up safe.
    """
    if n != 1:
        return None
    base = ("M1 is the power-up slot: at power-up the supply loads M1's setpoints and applies the "
            "front panel's last On/Off state as of power-off (OUT0/OUT1 over USB do not change it; "
            "this tool cannot set it)")
    if sp and (sp.get("v1", 0) > 0 or sp.get("v2", 0) > 0):
        live = ", ".join(f"CH{c} {fmt_v(sp[f'v{c}'])}" for c in (1, 2) if sp.get(f"v{c}", 0) > 0)
        return (f"{base}. You saved {live} into M1: after a power cycle the output can come up ON "
                f"at that voltage. " + SAFE_M1_PROCEDURE[0].upper() + SAFE_M1_PROCEDURE[1:])
    return base + ". M1 now holds 0 V on both channels, so a power cycle puts no voltage on the outputs"


class Daemon:
    """Owns the port. One device thread, a priority queue, one poller."""

    PRIO_OFF, PRIO_CMD = 0, 1
    WATCHDOG_S = 5.0     # one device operation longer than this means the port is hung

    def __init__(self, cfg: dict, port: str | None = None, fake: bool = False, poll_override=None,
                 id_override: dict | None = None):
        self.poll_override = poll_override
        self.id_override = {k: v for k, v in (id_override or {}).items() if v}
        if poll_override is not None:
            cfg = dict(cfg, poll_hz=poll_override)
        cfg = dict(cfg, **self.id_override)
        self._usbipd_run = _run_cmd
        self._reattach_last = 0.0
        self._reattach_busy = threading.Lock()
        self._reattach_sig = None
        self._reattach_kick = False
        self.reattach = {"phase": "waiting", "message": "device not opened yet", "busid": None,
                         "since": time.time(), "final": False}
        self.cfg = cfg
        self.port_name = port
        self.fake = fake
        self.cfg_dir, self.state_dir = home_dirs()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.guard_file = self.state_dir / "guard.json"
        self.audit_file = self.state_dir / "audit.jsonl"
        self.guard = self._load_guard()
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.seq = itertools.count()
        self.dev: Korad | None = None
        self.idn = ""
        self.cache = {"seq": 0, "t": None, "measured": None, "status": None,
                      "setpoints": None, "setpoints_t": None, "device": "absent",
                      "absent_since": time.time(), "opened_at": None}
        self.cache_cv = threading.Condition()
        self.events: deque = deque(maxlen=2000)
        self.event_seq = itertools.count(1)
        self.stopping = threading.Event()
        self._last_sp_poll = 0.0
        self._next_open = 0.0
        self.started = time.time()
        self.absent_since = time.time()
        self._guard_was_active = guard_is_active(self.guard)
        self._out_intent = itertools.count(1)
        self._out_latest = 0
        self._intent_lock = threading.Lock()
        self._tl = threading.local()
        self._lock_fd = None
        self._clock_ref = None          # (wall, monotonic) at the last poll
        self._pending_off = None        # out-intent id of an OFF that could not reach the device
        self._off_verified_at = 0.0     # monotonic time of the last OFF verified by read-back
        self._off_seen_at = -1e9        # monotonic time the poller last saw the output go ON -> OFF
        self._meas_on_count = 0         # consecutive samples with voltage while STATUS says OFF
        self._meas_on_warned = False    # one warning per episode
        self._off_check = None          # (due monotonic, retried) after our OFF: is VOUT really 0?
        self.start_report = None
        self._op_since = None           # monotonic start of the device operation in progress
        self._op_fired = False

    # -- state files
    def _load_guard(self):
        return load_guard_file(self.guard_file)

    def _save_guard(self, g):
        if g is None:
            self.guard_file.unlink(missing_ok=True)
        else:
            tmp = self.guard_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(g, indent=1))
            tmp.replace(self.guard_file)
        self.guard = g
        self._guard_was_active = guard_is_active(g)

    def event(self, kind: str, source: str, message: str, audit: dict | None = None):
        ev = {"seq": next(self.event_seq), "t": time.time(), "kind": kind,
              "source": source, "message": message}
        with self.cache_cv:
            self.events.append(ev)
            self.cache_cv.notify_all()
        rec = dict(ev, pid=os.getpid(), **(audit or {}))
        with open(self.audit_file, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"{dt.datetime.now():%H:%M:%S} {kind} [{source}] {message}", flush=True)

    # -- device thread
    def _phase(self, phase, message, busid=None, final=False):
        """Connection progress for clients: what the daemon is doing to reach the supply."""
        cur = self.reattach
        if (cur["phase"], cur["message"], cur["final"]) != (phase, message, final):
            self.reattach = {"phase": phase, "message": message, "busid": busid,
                             "since": time.time(), "final": final}

    def _reattach_blocker(self) -> str | None:
        """Why the daemon will not re-attach by itself, or None when it will."""
        if self.fake:
            return "the simulated supply (--fake) is absent; restart the daemon"
        fixed = self.port_name or self.cfg["port"]
        if fixed:
            return (f"port {fixed} is not there, and the daemon does not re-attach when a fixed port "
                    f"is set (--port or config port): attach the supply by hand, or remove the fixed port")
        if self.cfg["usbipd_reattach"] != "wsl":
            return (f"no {self.cfg['usb_id']} serial device found, and usbipd re-attach is off (not WSL, "
                    f"or usbipd_reattach = \"off\"): attach the supply by hand (from WSL: usbipd.exe "
                    f"attach --wsl --busid <busid>)")
        return None

    def reattach_view(self) -> dict:
        v = dict(self.reattach)
        now = time.time()
        if self.cache["device"] == "present" or self.fake or self._reattach_blocker():
            v["next_try_in_s"] = None
        else:
            first = 0 if self._reattach_kick else (self.absent_since or now) + 3 - now
            v["next_try_in_s"] = round(max(0.0, first, self._reattach_last + 10 - now), 1)
        return v

    def h_reconnect(self, req):
        """A client waits for the supply: look again now instead of at the next timed try."""
        if self.cache["device"] != "present":
            self._reattach_kick = True
            self._reattach_last = 0.0
            self._reattach_sig = None
            self._next_open = 0.0
            self._phase("waiting", "re-checking the supply now")
        return {"kicked_at": time.time(), "device": self.cache["device"], "reattach": self.reattach_view()}

    def _open(self):
        if self.fake:
            self.dev = Korad(FakeSerial())
        else:
            import serial
            fixed = self.port_name or self.cfg["port"]
            if fixed:
                name, skipped = fixed, []
            else:                          # re-run on every (re)open: node names change
                name, skipped = find_port(self.cfg["usb_serial"], usb_id=self.cfg["usb_id"])
            if skipped:
                self.event("device", "daemon", f"skipped {', '.join(skipped)}: no answer to *IDN? "
                           f"(stale node after a USB/IP drop?); using {name}")
            self._phase("opening", f"opening {name} ...")
            try:
                ser = serial.Serial(name, 9600, timeout=0.5, write_timeout=1.0, exclusive=True)
            except (serial.SerialException, OSError) as e:
                if getattr(e, "errno", None) in (errno.EBUSY, errno.EAGAIN, errno.EWOULDBLOCK) \
                        or "lock" in str(e).lower() or "busy" in str(e).lower():
                    raise KoradError("E_DEVICE", f"port busy (another process has {name} open): {e}")
                raise
            set_exclusive(ser.fileno())
            self.dev = Korad(ser)
        self.idn = self.dev.idn()
        with self.cache_cv:
            self.cache["device"] = "present"
            self.cache["absent_since"] = None
            self.cache["opened_at"] = time.time()
        self._last_sp_poll = 0.0
        self._meas_on_count, self._meas_on_warned = 0, False
        self._reattach_kick = False
        self._phase("idle", "supply reached")
        self.event("device", "daemon", f"opened: {self.idn}")
        try:
            st, sp = self.dev.status(), self.dev.setpoints()
            m = self.dev.measured()
        except Exception as e:
            self.event("warning", "daemon", f"could not read the state at open: {e}")
        else:
            self._refresh(st=st, sp=sp)
            live = max(m["vout1"], m["vout2"]) > MEASURED_ON_V
            if st["output"] or live:
                how = "STATUS says ON" if st["output"] else f"STATUS says OFF but VOUT reads " \
                      f"{m['vout1']:.2f} / {m['vout2']:.2f} V"
                self.event("warning", "daemon", f"output is ON at open (status {st['raw']}, {how}): "
                           f"{self._sp_text(sp)}, {st['mode']}. A power cycle loads M1's setpoints and "
                           f"applies the front panel's last On/Off state; " + SAFE_M1_PROCEDURE)
        pending, self._pending_off = self._pending_off, None
        if pending is not None:
            if self._out_latest == pending:
                st = self._out_off_raw("daemon")
                self.event("out", "daemon", f"pending OFF applied after reconnect (status {st['raw']})")
            else:
                self.event("out", "daemon", "pending OFF discarded after reconnect: a newer output "
                           "request replaced it")

    def _drop(self, err):
        if self.dev:
            try:
                self.dev.port.close()
            except Exception:
                pass
        self.dev = None
        self.absent_since = time.time()
        with self.cache_cv:
            self.cache["device"] = "absent"
            self.cache["absent_since"] = self.absent_since
            self.cache_cv.notify_all()
        self._phase("waiting", f"device lost ({err}); retrying every 2 s")
        self.event("device", "daemon", f"lost: {err}; retrying every 2 s")

    def _ensure(self):
        if self.dev:
            return
        if time.monotonic() < self._next_open:
            raise KoradError("E_DEVICE", "device absent; the daemon retries every 2 s")
        self._next_open = time.monotonic() + 2
        try:
            self._open()
        except Exception as e:
            self.dev = None
            busy = isinstance(e, KoradError) and e.message.startswith("port busy")
            with self.cache_cv:
                self.cache["device"] = "busy" if busy else "absent"
                self.cache["absent_since"] = self.absent_since
            if busy:
                self._phase("blocked", f"{e.message}: close the other program (a serial terminal, "
                            f"a probe script) and retry", final=True)
            else:
                ph, msg = self.reattach["phase"], self.reattach["message"]
                blocker = self._reattach_blocker()
                if blocker:
                    self._phase("blocked", blocker, final=True)
                elif ph in ("idle", "opening") or (ph == "blocked" and msg.startswith("port busy")) or \
                        (ph == "waiting" and msg == "re-checking the supply now"):
                    self._phase("waiting", "device not attached yet; the daemon asks usbipd.exe to "
                                "attach it")
            if not busy:
                self._maybe_reattach()
            raise KoradError("E_DEVICE", e.message if busy else f"device absent: {e}")

    def device_loop(self):
        while True:
            hz = self.cfg["poll_hz"]
            period = 0 if hz == "max" else 1 / hz
            last = self.cache["t"] or 0
            timeout = max(0.0, last + period - time.time()) if self.dev else 0.5
            try:
                prio, _, fn, fut, conn = self.q.get(timeout=timeout)
            except queue.Empty:
                if self.stopping.is_set():
                    return
                if self._idle_stop_due():
                    self._idle_exit()
                    return
                self._op_begin()
                try:
                    self._ensure()
                    self._poll()
                except KoradError:
                    pass
                except Exception as e:
                    self._drop(e)
                finally:
                    self._op_end()
                continue
            if fn is None:          # shutdown marker
                fut.set_result(None)
                self._drain("the daemon is stopping; the request was not run")
                return
            if prio != self.PRIO_OFF and conn is not None and peer_closed(conn):
                # The client gave up (killed, timed out): its request must not run later.
                fut.cancel()
                self.event("dropped", "daemon", "a request from a client that has disconnected "
                           "was dropped without running")
                continue
            if not fut.set_running_or_notify_cancel():
                continue            # cancelled by submit() after its timeout
            self._op_begin()
            try:
                if prio != self.PRIO_OFF and self.dev and time.time() - (self.cache["t"] or 0) > max(2 * period, 0.5):
                    # A queue that never empties must not starve the poll and its guard check.
                    try:
                        self._poll()
                    except KoradError:
                        pass
                    except Exception as e:
                        self._drop(e)
                self._op_begin()
                try:
                    self._ensure()
                    fut.set_result(fn())
                except KoradError as e:
                    fut.set_exception(e)
                except Exception as e:
                    self._drop(e)
                    fut.set_exception(KoradError("E_DEVICE", f"device error: {e}"))
            finally:
                self._op_end()

    def _idle_stop_due(self) -> bool:
        """Device continuously absent for idle_stop_after_s and no active guard."""
        idle = self.cfg.get("idle_stop_after_s", 0)
        if not idle or self.dev is not None or self.stopping.is_set():
            return False
        return time.time() - (self.absent_since or time.time()) >= idle and not guard_is_active(self.guard)

    def _idle_exit(self):
        """Stop without a safe stop: there is no device to talk to, and no guard to enforce."""
        self.event("daemon", "daemon", f"idle stop: device absent for "
                   f"{time.time() - self.absent_since:.0f} s and no active guard; the next korad "
                   f"command starts the daemon again")
        self.stopping.set()
        self._drain("the daemon stopped (idle stop: device absent, no active guard)")
        if getattr(self, "server", None):
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _op_begin(self):
        self._op_since, self._op_fired = time.monotonic(), False

    def _op_end(self):
        self._op_since = None

    def watchdog_loop(self):
        """Close a hung port so the device thread fails out of its read/write and reopens.

        A dead USB/IP link can leave the vhci node behind with reads or writes that never
        return (seen after a Windows sleep): the device thread then hung forever.
        """
        while not self.stopping.wait(0.25):
            since = self._op_since
            if since is None or self._op_fired or time.monotonic() - since < self.WATCHDOG_S:
                continue
            self._op_fired = True
            dev = self.dev
            with self.cache_cv:
                self.absent_since = time.time()
                self.cache["device"] = "absent"
                self.cache["absent_since"] = self.absent_since
                self.cache_cv.notify_all()
            self._phase("waiting", "device hung: port closed; the daemon reopens it")
            self.event("device", "daemon", f"device hung: port closed (one operation ran "
                       f"{time.monotonic() - since:.1f} s); the daemon reopens it")
            if dev is not None:
                try:
                    dev.port.close()
                except Exception:
                    pass

    def submit(self, fn, prio=PRIO_CMD, timeout=15):
        fut = concurrent.futures.Future()
        self.q.put((prio, next(self.seq), fn, fut, getattr(self._tl, "conn", None)))
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            if fut.cancel():
                raise KoradError("E_DEVICE", f"the device thread was busy for {timeout:g} s; the "
                                 f"command was cancelled and NOT run. Retry")
            raise KoradError("E_DEVICE", f"the command is still running after {timeout:g} s and may "
                             f"still complete. Run korad status to see the result")
        except concurrent.futures.CancelledError:
            raise KoradError("E_DAEMON", "the request was dropped without running")

    def _drain(self, why):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            fut = item[3]
            if fut.set_running_or_notify_cancel():
                fut.set_exception(KoradError("E_DAEMON", why))

    def _clock_check(self):
        """A wall-clock step between two polls: (jump_s, old_wall, new_wall) or None."""
        w, m = wall_time(), time.monotonic()
        ref, self._clock_ref = self._clock_ref, (w, m)
        if ref is None:
            return None
        jump = (w - ref[0]) - (m - ref[1])
        return (jump, ref[0] + (m - ref[1]), w) if abs(jump) > CLOCK_JUMP_S else None

    def _poll(self):
        d = self.dev
        jump = self._clock_check()
        m = d.measured()
        st = d.status()
        sp = None
        if time.monotonic() - self._last_sp_poll >= self.cfg["setpoint_every_s"]:
            sp = d.setpoints()
            self._last_sp_poll = time.monotonic()
        with self.cache_cv:
            old_st, old_sp = self.cache["status"], self.cache["setpoints"]
            self.cache.update(seq=self.cache["seq"] + 1, t=time.time(), measured=m, status=st)
            if sp:
                self.cache.update(setpoints=sp, setpoints_t=time.time())
            self.cache_cv.notify_all()
        if old_st and old_st.get("output") and not old_st.get("output_by_measure") and not st["output"]:
            self._off_seen_at = time.monotonic()      # an OFF from the panel also leaves a decay
        self._after_off_check(m)
        st = self._measure_cross_check(st, m)
        if st.get("output_by_measure"):
            with self.cache_cv:
                self.cache["status"] = st
        if old_st and (old_st["output"], old_st["mode"], old_st["ovp"], old_st["ocp"]) != \
                (st["output"], st["mode"], st["ovp"], st["ocp"]) and \
                not (st.get("output_by_measure") or old_st.get("output_by_measure")):
            self.event("panel", "front panel",
                       f"status changed without a command: {old_st['raw']} -> {st['raw']}")
        if sp and old_sp and sp != old_sp:
            self.event("panel", "front panel", f"setpoints changed without a command: "
                       f"{self._sp_text(old_sp)} -> {self._sp_text(sp)}")
        active = guard_is_active(self.guard)
        if jump:
            size, old_w, new_w = jump
            tz = dt.datetime.now().astimezone().tzinfo
            msg = (f"wall clock jumped {'+' if size > 0 else '-'}{fmt_delta(round(size))}: from "
                   f"{fmt_local(dt.datetime.fromtimestamp(old_w, tz))} to "
                   f"{fmt_local(dt.datetime.fromtimestamp(new_w, tz))}")
            if self._guard_was_active and not active:
                msg += "; this jump expired the guard"
            self.event("clock", "daemon", msg)
            self.event("warning", "daemon", "the wall clock jumped; guard expiry follows the wall "
                       "clock. Check the time and the guard: korad guard")
        if self._guard_was_active and not active:
            exp = dt.datetime.fromisoformat(self.guard["expires_utc"])
            self.event("guard", "daemon", f"guard expired at {fmt_local(exp)}: no limits apply now")
            if st["output"]:
                self.event("warning", "daemon", "the output is ON and no guard applies any more. "
                           "Set a new guard: korad guard set ...")
        self._guard_was_active = active
        cur_sp = sp or old_sp
        if st["output"]:
            bad = measured_violations(self.guard, st["mode"], m)
            if cur_sp:
                bad += guard_violations(self.guard, st["mode"], cur_sp) + \
                    unguarded_live(self.guard, st["mode"], cur_sp)
            if bad:
                if self.cfg["on_live_violation"] == "off":
                    self._out_off_raw("daemon")
                    self.event("guard", "daemon", "output turned OFF: live state breaks the guard: "
                               + "; ".join(bad))
                else:
                    self.event("guard", "daemon", "WARNING live state breaks the guard: " + "; ".join(bad))

    def _measure_cross_check(self, st, m):
        """STATUS says OFF but the terminals carry voltage: count the output as ON.

        Needs 2 consecutive samples, and no OFF (ours, TRACK, RCL, or seen from the panel) in the
        last OFF_DECAY_S, because the output decays for up to ~1.1 s after OFF.
        """
        v = max(m["vout1"], m["vout2"])
        recent_off = time.monotonic() - max(self.dev.last_off_cmd, self._off_seen_at,
                                            self._off_verified_at) < OFF_DECAY_S
        if st["output"] or v <= MEASURED_ON_V or recent_off:
            self._meas_on_count = 0
            if st["output"] or v <= MEASURED_ON_V:
                self._meas_on_warned = False
            return st
        self._meas_on_count += 1
        if self._meas_on_count < 2:
            return st
        st = dict(st, output=True, output_by_measure=True)
        if not self._meas_on_warned:
            self._meas_on_warned = True
            self.event("warning", "daemon", f"output is ON by measured voltage although STATUS says OFF "
                       f"(raw {st['raw']}): VOUT {m['vout1']:.2f} / {m['vout2']:.2f} V")
        return st

    @staticmethod
    def _sp_text(sp):
        return (f"CH1 {fmt_v(sp['v1'])} {fmt_i(sp['i1'])}, CH2 {fmt_v(sp['v2'])} {fmt_i(sp['i2'])}")

    def _refresh(self, st=None, sp=None):
        """Put fresh values from a command into the cache, so polls see no false panel change."""
        with self.cache_cv:
            if st is not None:
                self.cache["status"] = st
            if sp is not None:
                self.cache.update(setpoints=sp, setpoints_t=time.time())
            self.cache_cv.notify_all()

    def _out_off_raw(self, source, check=True):
        st = self.dev.set_output(False)       # raises unless the read-back shows OFF (bits 6 and 7 clear)
        self._off_verified_at = time.monotonic()
        if check:
            self._off_check = (time.monotonic() + OFF_DECAY_S, False)
        self._refresh(st=st)
        return st

    def _after_off_check(self, m):
        """After our OFF, VOUT must reach ~0 within OFF_DECAY_S. If not: warn, OUT0 once more."""
        if not self._off_check or time.monotonic() < self._off_check[0]:
            return
        due, retried = self._off_check
        self._off_check = None
        v = max(m["vout1"], m["vout2"])
        if v <= MEASURED_ON_V:
            return
        if not retried:
            self.event("warning", "daemon", f"VOUT still reads {m['vout1']:.2f} / {m['vout2']:.2f} V "
                       f"{OFF_DECAY_S:g} s after a verified OFF: sending OUT0 again")
            self._out_off_raw("daemon", check=False)
            self._off_check = (time.monotonic() + OFF_DECAY_S, True)
        else:
            self.event("warning", "daemon", f"VOUT still reads {m['vout1']:.2f} / {m['vout2']:.2f} V after "
                       f"two OFF commands: the output may be ON. Press OUTPUT on the front panel or cut mains")

    # -- request handlers (called from socket threads)
    def warnings(self) -> list[str]:
        if self.cfg["no_guard_warning"] and not guard_is_active(self.guard):
            return [no_guard_warning(self.guard)]
        return []

    def _startup_check(self):
        """Report the output state found at start. Output ON that breaks the guard -> OFF."""
        st, sp, m = self.dev.status(), self.dev.setpoints(), self.dev.measured()
        self._refresh(st=st, sp=sp)
        by_measure = not st["output"] and max(m["vout1"], m["vout2"]) > MEASURED_ON_V
        rep = {"output_on": st["output"] or by_measure, "by_measure": by_measure, "raw": st["raw"],
               "measured": m, "mode": st["mode"], "setpoints": sp, "violations": [], "turned_off": False}
        if rep["output_on"]:
            bad = guard_violations(self.guard, st["mode"], sp) + unguarded_live(self.guard, st["mode"], sp)
            rep["violations"] = bad
            if bad and self.cfg["on_live_violation"] == "off":
                self._out_off_raw("daemon")
                rep["turned_off"] = True
                self.event("guard", "daemon", f"output was ON at start (status {st['raw']}, "
                           f"{self._sp_text(sp)}) and broke "
                           f"the guard: output turned OFF: " + "; ".join(bad))
            else:
                self.event("startup", "daemon", f"output was ON at start (status {st['raw']}): "
                           f"{self._sp_text(sp)}, {st['mode']}"
                           + (f"; WARNING it breaks the guard: {'; '.join(bad)}" if bad else ""))
        self.start_report = rep
        return rep

    def h_ping(self, req):
        return {"pid": os.getpid(), "idn": self.idn, "fake": self.fake, "device": self.cache["device"],
                "uptime_s": round(time.time() - self.started), "lock": str(self.lock_path()),
                "start_report": self.start_report, "absent_since": self.cache.get("absent_since"),
                "reattach": self.reattach_view()}

    def h_state(self, req):
        with self.cache_cv:
            c = json.loads(json.dumps(self.cache))
            ev = [e for e in self.events if e["seq"] > req.get("events_after", 0)]
        c["guard"] = self.guard
        c["guard_active"] = guard_is_active(self.guard)
        c["guard_text"] = describe_guard(self.guard)
        c["events"] = ev[-200:]
        c["last_event_seq"] = self.events[-1]["seq"] if self.events else 0
        c["poll_hz"] = self.cfg["poll_hz"]
        c["reattach"] = self.reattach_view()
        return c

    def h_wait_sample(self, req):
        after, timeout = req.get("after", 0), min(float(req.get("timeout", 5)), 30)
        with self.cache_cv:
            self.cache_cv.wait_for(lambda: self.cache["seq"] > after or self.stopping.is_set(), timeout)
        return self.h_state(req)

    def h_set(self, req, src):
        ch_list = [int(c) for c in str(req["channel"])]
        v, i = req.get("v"), req.get("i")
        if v is None and i is None:
            raise KoradError("E_USAGE", "nothing to set: give -v and/or -i")
        warn = []

        def run():
            st, sp = self.dev.status(), self.dev.setpoints()
            if st["mode"] != "independent" and 1 in ch_list:
                raise KoradError("E_USAGE", f"the supply is in {st['mode']} mode: CH2 sets both "
                                 f"channels and CH1 is ignored. Use channel 2")
            new = dict(sp)
            for ch in ch_list:
                if v is not None:
                    new[f"v{ch}"] = v
                if i is not None:
                    new[f"i{ch}"] = i
            if st["mode"] != "independent":
                new["v1"], new["i1"] = new["v2"], new["i2"]
            bad = guard_violations(self.guard, st["mode"], new)
            if st["output"]:
                bad += unguarded_live(self.guard, st["mode"], new)
            if bad:
                self.event("refused", src, "set refused by guard: " + "; ".join(bad),
                           {"request": req, "violations": bad})
                hints = next_steps(bad, exclude=ch_list)
                raise KoradError("E_GUARD", "refused by the guard: " + "; ".join(bad) +
                                 "".join(f"\nnext step: {h}" for h in hints) +
                                 "\n" + describe_guard(self.guard), {"violations": bad})
            if not guard_is_active(self.guard) and any(new[k] > sp[k] for k in sp):
                warn.append(no_guard_warning(self.guard))
            # Current limit first, then voltage.
            plan = [("ISET", ch, i) for ch in ch_list if i is not None] + \
                   [("VSET", ch, v) for ch in ch_list if v is not None]
            done = []
            for n, (kind, ch, val) in enumerate(plan):
                label = f"{kind}{ch}={fmt_i(val) if kind == 'ISET' else fmt_v(val)}"
                try:
                    (self.dev.set_i if kind == "ISET" else self.dev.set_v)(ch, val)
                except Exception as e:
                    done.append((label, "sent, not verified"))
                    done += [(f"{k2}{c2}={fmt_i(v2) if k2 == 'ISET' else fmt_v(v2)}", "not sent")
                             for k2, c2, v2 in plan[n + 1:]]
                    self._partial(src, req, sp, done, e)
                done.append((label, "verified"))
            got = self.dev.setpoints()
            self._refresh(sp=got)
            bad = guard_violations(self.guard, st["mode"], got)
            if bad:                       # read-back check (point 2)
                self._out_off_raw(src)
                raise KoradError("E_GUARD", "read-back breaks the guard, output turned OFF: "
                                 + "; ".join(bad))
            self.event("set", src, f"{self._sp_text(sp)} -> {self._sp_text(got)}",
                       {"request": req, "before": sp, "after": got})
            return {"before": sp, "after": got, "mode": st["mode"]}
        return self.submit(run), warn

    def _partial(self, src, req, before, done, err):
        """A set failed after some writes: say exactly what the supply holds now."""
        try:
            got = self.dev.setpoints()
            self._refresh(sp=got)
            now_text = self._sp_text(got)
        except Exception as e2:
            got, now_text = None, f"unknown (read-back failed: {e2})"
        writes = "; ".join(f"{label}: {state}" for label, state in done) or "none"
        self.event("set-partial", src, f"set failed part way: {writes}; now {now_text}; "
                   f"error: {getattr(err, 'message', err)}",
                   {"request": req, "before": before, "after": got, "writes": done})
        code = err.code if isinstance(err, KoradError) else "E_DEVICE"
        raise KoradError(code, f"{getattr(err, 'message', err)}\nthe set stopped part way. Writes: "
                         f"{writes}. Read-back now: {now_text}",
                         {"writes": [{"write": label, "state": state} for label, state in done],
                          "setpoints": got})

    ON_MAX_QUEUE_S = 2.0

    def h_out(self, req, src):
        on = bool(req["on"])
        with self._intent_lock:
            mine = next(self._out_intent)
            self._out_latest = mine
        queued_at = time.monotonic()

        def run():
            if on:
                if self._out_latest != mine:
                    raise KoradError("E_SUPERSEDED", "output ON dropped: a newer output request was "
                                     "made while this one waited. The last request wins; check "
                                     "korad status and retry if you still want ON")
                waited = time.monotonic() - queued_at
                if waited > self.ON_MAX_QUEUE_S:
                    raise KoradError("E_SUPERSEDED", f"output ON dropped: it waited {waited:.1f} s in "
                                     f"the queue (the limit is {self.ON_MAX_QUEUE_S:g} s); queued too "
                                     f"long, retry")
            if not on:
                st = self._out_off_raw(src)
                try:
                    self.event("out", src, "output OFF", {"request": req, "status": st})
                except Exception:
                    pass
                return {"status": st, "verified": True}
            st, sp = self.dev.status(), self.dev.setpoints()
            self._refresh(st=st, sp=sp)
            bad = guard_violations(self.guard, st["mode"], sp) + unguarded_live(self.guard, st["mode"], sp)
            if bad:
                self.event("refused", src, "output ON refused by guard: " + "; ".join(bad),
                           {"request": req, "violations": bad})
                raise KoradError("E_GUARD", "output ON refused by the guard: " + "; ".join(bad)
                                 + "".join(f"\nnext step: {h}" for h in next_steps(bad))
                                 + "\n" + describe_guard(self.guard), {"violations": bad})
            st = self.dev.set_output(True)
            self._refresh(st=st)
            self.event("out", src, f"output ON ({st['mode']}, {self._sp_text(sp)})",
                       {"request": req, "status": st, "setpoints": sp})
            return {"status": st, "setpoints": sp}
        try:
            data = self.submit(run, self.PRIO_OFF if not on else self.PRIO_CMD)
        except KoradError as e:
            if not on and self._off_verified_at >= queued_at:
                # The read-back showed OFF after this request was made: report that, not the later failure.
                with self.cache_cv:
                    st = dict(self.cache["status"] or {})
                return {"status": st, "verified": True}, [
                    f"the OFF was verified by read-back; a later step failed: {e.message}"]
            if not on and e.code == "E_DEVICE":
                with self._intent_lock:
                    if self._out_latest == mine:
                        self._pending_off = mine
                raise KoradError("E_DEVICE", f"{e.message}. The output may still be ON: press OUTPUT "
                                 f"on the front panel or cut mains. The daemon applies this OFF as soon "
                                 f"as the device is back, unless a newer output request replaces it",
                                 e.detail)
            raise
        return data, (self.warnings() if on else [])

    def h_mode(self, req, src):
        mode = req["mode"]
        if mode not in MODE_CMD:
            raise KoradError("E_USAGE", f"mode must be one of {list(MODE_CMD)}")
        warn = []

        def run():
            st, sp = self.dev.status(), self.dev.setpoints()
            after = dict(sp)
            if mode != "independent":
                after["v1"], after["i1"] = sp["v2"], sp["i2"]
            bad = guard_violations(self.guard, mode, after)
            if bad and mode == "independent":
                # Leaving series/parallel is always allowed: the output goes OFF, and the
                # check before output ON still covers the setpoints.
                warn.append(f"after the move to independent, the setpoints break the guard: "
                            + "; ".join(bad) + ". Output ON stays refused until you lower them")
                bad = []
            if bad:
                self.event("refused", src, f"mode {mode} refused by guard: " + "; ".join(bad),
                           {"request": req, "violations": bad})
                raise KoradError("E_GUARD", f"mode {mode} refused by the guard: " + "; ".join(bad)
                                 + "\n" + describe_guard(self.guard), {"violations": bad})
            if not guard_is_active(self.guard) and mode != st["mode"]:
                warn.append(no_guard_warning(self.guard))
            if st["output"]:
                self._out_off_raw(src)
            new_st = self.dev.set_mode(mode)
            got = self.dev.setpoints()
            self._refresh(st=new_st, sp=got)
            self.event("mode", src, f"mode {st['mode']} -> {mode}; output is OFF "
                       f"(the supply does this on every mode change)", {"request": req, "setpoints": got})
            return {"status": new_st, "setpoints": got}
        return self.submit(run), warn

    def h_bit(self, req, src):
        key, on = req["which"], bool(req["on"])
        if key not in ("ovp", "ocp"):
            raise KoradError("E_USAGE", "which must be ovp or ocp")

        def run():
            st = self.dev.set_bit(("OVP1", "OVP0") if key == "ovp" else ("OCP1", "OCP0"), key, on)
            self._refresh(st=st)
            self.event(key, src, f"{key.upper()} {'on' if on else 'off'}", {"request": req})
            return {"status": st}
        return self.submit(run), []

    # -- usbipd re-attach (WSL only; never binds, never elevates)
    def _maybe_reattach(self):
        if self.fake or self.port_name or self.cfg["port"] or self.cfg["usbipd_reattach"] != "wsl":
            return
        now = time.time()
        if (not self._reattach_kick and now - (self.absent_since or now) <= 3) \
                or now - self._reattach_last < 10:
            return
        if not self._reattach_busy.acquire(blocking=False):
            return
        self._reattach_last = now
        self._reattach_kick = False

        def work():
            try:
                self._reattach_once()
            except Exception as e:
                self.event("device", "daemon", f"usbipd re-attach failed: {e}")
            finally:
                self._reattach_busy.release()
        threading.Thread(target=work, daemon=True).start()   # never block the device thread

    def _reattach_once(self) -> str:
        """One re-attach attempt. Returns what happened (also logged when it changed)."""
        exe = usbipd_exe(self.cfg["usbipd_path"])
        if not exe:
            msg = ("usbipd re-attach: usbipd.exe not found; set usbipd_path in the configuration, or "
                   "usbipd_reattach = \"off\" and attach the supply by hand")
            self._phase("blocked", msg, final=True)
            return self._reattach_note(("no-exe",), msg)
        self._phase("looking", f"usbipd re-attach: looking for {self.cfg['usb_id']} on the Windows "
                    f"host bus ...")
        try:
            devs = usbipd_devices(exe, self._usbipd_run)
        except KoradError as e:
            self._phase("failed", f"usbipd re-attach: {e.message}; the daemon tries again in 10 s")
            raise
        d, why = pick_usbipd_device(devs, self.cfg["usb_id"], self.cfg["usb_serial"])
        if d is None:
            if "set usb_serial" in why:
                nxt = "set usb_serial in the configuration to choose one"
            else:
                nxt = "check that the supply is powered on and its USB cable is connected, then retry"
            self._phase("failed", f"usbipd re-attach: {why}", final=True)
            self.reattach["next_step"] = nxt
            return self._reattach_note(("none", why), f"usbipd re-attach: {why}")
        sig = (d["state"], d["busid"], d["client"])
        if d["state"] == "not_shared":
            msg = (f"usbipd re-attach: busid {d['busid']} is not shared. The daemon never binds or "
                   f"elevates: run once, elevated, 'usbipd bind --busid {d['busid']}'. "
                   f"It then re-attaches")
            self._phase("blocked", msg, busid=d["busid"], final=True)
            return self._reattach_note(sig, msg)
        if d["state"] == "attached":
            msg = (f"usbipd re-attach: busid {d['busid']} is attached to client {d['client'] or '?'}; "
                   f"not taking it. If it should come here, detach it there (usbipd detach --busid "
                   f"{d['busid']})")
            self._phase("blocked", msg, busid=d["busid"], final=True)
            return self._reattach_note(sig, msg)
        self._reattach_sig = None
        self._phase("attaching", f"usbipd re-attach: busid {d['busid']} is shared, attaching to WSL ...",
                    busid=d["busid"])
        try:
            r = self._usbipd_run([exe, "attach", "--wsl", "--busid", d["busid"]], 15)
        except subprocess.TimeoutExpired:
            msg = f"usbipd re-attach: busid {d['busid']} attach timed out after 15 s"
            self._phase("failed", msg + "; the daemon tries again in 10 s", busid=d["busid"])
            self.event("device", "daemon", msg)
            return msg
        if r.returncode == 0:
            self._next_open = 0.0
            self.absent_since = time.time()     # a successful attach restarts the idle-stop clock
            msg = f"usbipd re-attach: busid {d['busid']} attached" + (f" ({why})" if why else "")
            self._phase("attached", f"usbipd re-attach: busid {d['busid']} attached; waiting for the "
                        f"serial port ...", busid=d["busid"])
        else:
            msg = (f"usbipd re-attach: busid {d['busid']} attach failed (rc {r.returncode}): "
                   f"{(r.stderr or r.stdout).strip()[-200:]}")
            self._phase("failed", msg + "; the daemon tries again in 10 s", busid=d["busid"])
        self.event("device", "daemon", msg)
        return msg

    def _reattach_note(self, sig, msg) -> str:
        """Log a blocked state once, and stay quiet until it changes."""
        if sig != self._reattach_sig:
            self._reattach_sig = sig
            self.event("device", "daemon", msg)
        return msg

    def h_preset(self, req, src):
        n = int(req["n"])
        if not 1 <= n <= 5:
            raise KoradError("E_USAGE", "preset number must be 1..5")
        save = bool(req.get("save"))

        def run():
            if save and n == 1:
                return self._save_m1(req, src)
            if save:
                self.dev.write(f"SAV{n}")
                sp = self.dev.setpoints()
                self.event("preset-save", src, f"saved M{n}: {self._sp_text(sp)}", {"request": req})
                return {"saved": n, "setpoints": sp}
            before = self.dev.setpoints()
            self.dev.write(f"RCL{n}")
            time.sleep(0.3)
            st, sp = self.dev.status(), self.dev.setpoints()
            if st["output"]:
                st = self._out_off_raw(src)
            self._refresh(st=st, sp=sp)
            bad = guard_violations(self.guard, st["mode"], sp)
            if bad:
                msg = f"preset M{n} ({self._sp_text(sp)}) breaks the guard: " + "; ".join(bad)
                if self.cfg["on_recall_violation"] == "zero":
                    for ch in ((2,) if st["mode"] != "independent" else (1, 2)):
                        self.dev.set_v(ch, 0)
                        self.dev.set_i(ch, 10)
                    sp = self.dev.setpoints()
                    self._refresh(sp=sp)
                    self.event("guard", src, msg + f"; setpoints now {self._sp_text(sp)}", {"request": req})
                    raise KoradError("E_GUARD", msg + f". The setpoints are now {self._sp_text(sp)} "
                                     f"(read back); the output is OFF.\n" + describe_guard(self.guard),
                                     {"setpoints": sp})
                self.event("guard", src, "WARNING " + msg, {"request": req})
            self.event("preset", src, f"recalled M{n}: {self._sp_text(before)} -> {self._sp_text(sp)}; "
                       f"output is OFF (the supply does this on every recall)", {"request": req})
            return {"recalled": n, "status": st, "setpoints": sp}
        warn = self.warnings()
        data = self.submit(run)
        if save and n != 1:
            w = preset_save_warning(n, data.get("setpoints"))
            if w:
                warn = [w] + warn
        return data, warn

    def _save_m1(self, req, src):
        """M1 is the power-up slot (DESIGN.md "Power-up and M1"): it must stay at 0 V / 0 A.

        Refuse any save of non-zero setpoints. With all setpoints at 0, recall M1 to read
        it, restore the setpoints, and save only when M1 holds anything above 0/0.
        Runs in the device thread.
        """
        sp = self.dev.setpoints()
        if any(sp[k] > 0 for k in ("v1", "i1", "v2", "i2")):
            msg = (f"M1 is the power-up slot and must stay at 0 V / 0 A; save working presets in "
                   f"M2-M5 (current setpoints: {self._sp_text(sp)})")
            self.event("refused", src, "preset-save 1 refused: " + msg, {"request": req, "setpoints": sp})
            raise KoradError("E_GUARD", msg, {"setpoints": sp})
        st = self.dev.status()
        if st["output"]:
            raise KoradError("E_USAGE", "checking M1 needs a recall (RCL1), and a recall turns the "
                             "output OFF; turn the output off first: korad out off")
        chans = (1, 2) if st["mode"] == "independent" else (2,)

        def recall_m1():
            self.dev.write("RCL1")
            time.sleep(0.3)
            m = self.dev.setpoints()
            for ch in chans:               # put back the 0/0 setpoints we had before the recall
                self.dev.set_i(ch, sp[f"i{ch}"])
                self.dev.set_v(ch, sp[f"v{ch}"])
            got = self.dev.setpoints()
            self._refresh(sp=got)
            if got != sp:
                raise KoradError("E_VERIFY", f"after reading M1 the setpoints did not return to "
                                 f"{self._sp_text(sp)}: read back {self._sp_text(got)}", {"setpoints": got})
            return m, got

        m1, got = recall_m1()
        if not any(m1[k] > 0 for k in ("v1", "i1", "v2", "i2")):
            msg = "M1 already holds 0/0; not overwritten"
            self.event("preset-save", src, msg, {"request": req, "m1": m1})
            return {"saved": None, "m1": m1, "setpoints": got, "message": msg}
        self.dev.write("SAV1")
        check, got = recall_m1()
        if any(check[k] > 0 for k in ("v1", "i1", "v2", "i2")):
            raise KoradError("E_VERIFY", f"SAV1 did not take: M1 reads {self._sp_text(check)}",
                             {"m1": check})
        msg = f"M1 restored to 0/0 (it held {self._sp_text(m1)})"
        self.event("preset-save", src, msg, {"request": req, "m1_before": m1, "m1_after": check})
        return {"saved": 1, "m1_before": m1, "m1": check, "setpoints": got, "message": msg}

    def h_guard_set(self, req, src):
        new = req["guard"]
        validate_guard(new)
        validate_guard_window(new)
        need = guard_loosening(self.guard, new)
        self._confirm(need, req.get("confirm"), "loosens the guard")
        self._save_guard(new)
        self.event("guard", src, "guard set: " + describe_guard(new).replace("\n", " | "),
                   {"request": req})
        return {"guard": new, "text": describe_guard(new)}, []

    def h_guard_clear(self, req, src):
        need = guard_loosening(self.guard, None)
        self._confirm(need, req.get("confirm"), "clears the guard")
        old = self.guard
        self._save_guard(None)
        self.event("guard", src, "guard cleared", {"request": req, "old": old})
        return {"guard": None, "text": describe_guard(None)}, [no_guard_warning(None)]

    @staticmethod
    def _confirm(need, given, what):
        if not need:
            return
        want = " ".join(need)
        if (given or "").strip() != want:
            raise KoradError("E_CONFIRM", f"this {what}; the current guard stays. To confirm, retype "
                             f"the new value(s): {len(need)} word(s) ({', '.join(_word_kind(w) for w in need)})",
                             {"words": len(need), "kinds": [_word_kind(w) for w in need]})

    def h_reload(self, req, src):
        try:
            cfg = load_config()
            g = self._load_guard()
        except KoradError as e:
            raise KoradError("E_USAGE", f"reload failed, nothing changed (the previous "
                             f"configuration and guard stay): {e.message}")
        if self.poll_override is not None:
            cfg["poll_hz"] = self.poll_override
        cfg.update(self.id_override)
        self.cfg = cfg
        self.guard = g
        self._guard_was_active = guard_is_active(g)
        self.event("daemon", src, "configuration reloaded"
                   + (f" (poll {self.poll_override} Hz kept from --poll)" if self.poll_override else ""))
        return {"config": cfg}, []

    def h_shutdown(self, req, src):
        threading.Thread(target=self.shutdown, args=(src,), daemon=True).start()
        return {"stopping": True}, []

    def safe_stop(self, src):
        """OUT0, both channels to 0 V / 10 mA, independent mode, verified."""
        def run():
            st = self._out_off_raw(src)
            if self.cfg["stop_action"] == "off_and_zero":
                if st["mode"] != "independent":
                    st = self.dev.set_mode("independent")
                for ch in (1, 2):
                    self.dev.set_v(ch, 0)
                    self.dev.set_i(ch, 10)
            sp = self.dev.setpoints()
            self._refresh(st=self.dev.status(), sp=sp)
            self.event("stop", src, f"safe stop: output OFF, {self._sp_text(sp)}, {st['mode']}")
            return sp
        return self.submit(run, self.PRIO_OFF)

    def shutdown(self, src="daemon"):
        if self.stopping.is_set():
            return
        try:
            self.safe_stop(src)
        except Exception as e:
            self.event("stop", src, f"SAFE STOP FAILED: {e}. The supply keeps its last state")
        self.stopping.set()
        fut = concurrent.futures.Future()
        self.q.put((-1, next(self.seq), None, fut, None))
        if getattr(self, "server", None):
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    HANDLERS = {"set": "h_set", "out": "h_out", "mode": "h_mode", "bit": "h_bit",
                "preset": "h_preset", "guard_set": "h_guard_set", "guard_clear": "h_guard_clear",
                "reload": "h_reload", "shutdown": "h_shutdown"}

    def handle(self, req: dict, conn=None) -> dict:
        self._tl.conn = conn
        op = req.get("op")
        src = req.get("client", "unknown")
        try:
            if op in ("ping", "state", "wait_sample", "reconnect"):
                data, warn = getattr(self, f"h_{op}")(req), []
            elif op in self.HANDLERS:
                if self.stopping.is_set():
                    raise KoradError("E_DAEMON", "the daemon is stopping")
                data, warn = getattr(self, self.HANDLERS[op])(req, src)
            else:
                raise KoradError("E_USAGE", f"unknown op {op!r}")
            return {"ok": True, "data": data, "warnings": warn}
        except KoradError as e:
            return {"ok": False, "error": e.to_dict()}
        except concurrent.futures.TimeoutError:
            return {"ok": False, "error": {"code": "E_DEVICE", "message": "device thread timed out; "
                                           "the command may still complete. Run korad status"}}
        except Exception as e:           # never let a bug close the connection without an answer
            return {"ok": False, "error": {"code": "E_INTERNAL",
                                           "message": f"internal error: {type(e).__name__}: {e}"}}

    def lock_path(self) -> Path:
        if self.fake:
            return self.state_dir / "daemon.lock"
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", self.cfg["usb_serial"] or self.cfg["usb_id"].replace(":", "-"))
        return Path(os.environ.get("KORAD_LOCK_DIR", "/tmp")) / f"korad-{key}.lock"

    def _take_lock(self, sock):
        """One daemon per supply: an flock held for the life of the process."""
        path = self.lock_path()
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.pread(fd, 512, 0).decode(errors="replace").strip() or "unknown"
            os.close(fd)
            raise KoradError("E_DAEMON", f"another korad daemon already controls this supply "
                             f"({holder}; lock {path}). Stop it first (korad daemon stop, with its "
                             f"KORAD_HOME/KORAD_SOCKET)")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}, socket {sock}".encode(), 0)
        self._lock_fd = fd

    def serve(self):
        sock = socket_path()
        sock.parent.mkdir(parents=True, exist_ok=True)
        self._take_lock(sock)
        if sock.exists():
            try:
                with socket.socket(socket.AF_UNIX) as s:
                    s.connect(str(sock))
                raise KoradError("E_DAEMON", f"a daemon already listens on {sock}")
            except (ConnectionRefusedError, FileNotFoundError):
                sock.unlink()
        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    try:
                        req = json.loads(line)
                    except json.JSONDecodeError:
                        resp = {"ok": False, "error": {"code": "E_USAGE", "message": "bad JSON"}}
                    else:
                        resp = daemon.handle(req, self.connection)
                    try:
                        self.wfile.write((json.dumps(resp) + "\n").encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        old = os.umask(0o177)
        try:
            self.server = Server(str(sock), Handler)
        finally:
            os.umask(old)
        dev_thread = threading.Thread(target=self.device_loop, daemon=True)
        dev_thread.start()
        threading.Thread(target=self.watchdog_loop, daemon=True).start()
        for sig in ((signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
                    if threading.current_thread() is threading.main_thread() else ()):
            signal.signal(sig, lambda *_: threading.Thread(
                target=self.shutdown, args=("signal",), daemon=True).start())
        try:
            self.submit(self._startup_check, timeout=10)   # opens the device if it is there
        except KoradError as e:
            print(f"warning: {e.message}", flush=True)
        self.event("daemon", "daemon", f"started, pid {os.getpid()}, socket {sock}, "
                   f"poll {self.cfg['poll_hz']} Hz{', FAKE device' if self.fake else ''}")
        for w in self.warnings():
            self.event("warning", "daemon", w)
        # Startup rule: output ON and state breaks the guard -> OFF. The first poll does it.
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.server.server_close()
            sock.unlink(missing_ok=True)
            dev_thread.join(timeout=5)
            if self.dev:
                try:
                    self.dev.port.close()
                except Exception:
                    pass
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None


def _word_kind(w):
    if w in ("clear", "unlimited", "series", "parallel"):
        return f'"{w}"'
    if ":" in w:
        return "the new expiry time as HH:MM (24 h, local)"
    if "." in w:
        if len(w.split(".")[1]) == 2:
            return "the new voltage limit in volts with 2 decimals, in the form N.NN"
        return "the new current limit in amps with 3 decimals, in the form N.NNN"
    return w


# ---------------------------------------------------------------- client

class Client:
    """One connection to the daemon. call(op, **fields) returns data or raises KoradError.

    Ops: ping, state(events_after), wait_sample(after, timeout), reconnect, set(channel, v, i),
    out(on), mode(mode), bit(which, on), preset(n, save), guard_set(guard, confirm),
    guard_clear(confirm), reload, shutdown. Values are integers: centivolts, milliamps.
    """

    def __init__(self, path: Path | None = None, name: str | None = None):
        self.path = path or socket_path()
        self.name = name or f"pid {os.getpid()}: {' '.join(Path(a).name if i == 0 else a for i, a in enumerate(sys.argv))}"
        self.sock = socket.socket(socket.AF_UNIX)
        try:
            self.sock.connect(str(self.path))
        except FileNotFoundError:
            self.sock.close()
            raise KoradError("E_DAEMON", f"daemon not running (no socket at {self.path}): "
                             f"run 'korad daemon start'", {"not_running": True})
        except ConnectionRefusedError:
            self.sock.close()
            raise KoradError("E_DAEMON", f"daemon not running: {self.path} is a stale socket (the "
                             f"daemon was killed without a safe stop, so the supply may still be ON). "
                             f"'korad daemon start' removes the stale socket", {"not_running": True})
        except OSError as e:
            self.sock.close()
            raise KoradError("E_DAEMON", f"cannot connect to the daemon socket {self.path} "
                             f"({len(str(self.path))} bytes): {e.strerror or e}. An AF_UNIX path "
                             f"is limited to 107 bytes; use a shorter KORAD_HOME or KORAD_SOCKET")
        self.f = self.sock.makefile("rwb")
        self.last_warnings: list[str] = []

    TIMEOUT_S = 20

    def call(self, op: str, **fields):
        req = dict(fields, op=op, client=self.name)
        wait = self.TIMEOUT_S + (float(fields.get("timeout", 0)) if op == "wait_sample" else 0)
        self.sock.settimeout(wait)
        unsure = ("the request may or may not have been applied. Run 'korad status' to see the "
                  "supply's state")
        try:
            self.f.write((json.dumps(req) + "\n").encode())
            self.f.flush()
            line = self.f.readline()
        except (TimeoutError, socket.timeout):
            raise KoradError("E_DAEMON", f"no answer from the daemon within {wait:g} s; {unsure}")
        except OSError as e:
            raise KoradError("E_DAEMON", f"lost the connection to the daemon ({e}); {unsure}")
        if not line:
            raise KoradError("E_DAEMON", f"the daemon closed the connection before answering; {unsure}")
        resp = json.loads(line)
        if not resp["ok"]:
            e = resp["error"]
            raise KoradError(e["code"], e["message"], e.get("detail"))
        self.last_warnings = resp.get("warnings", [])
        return resp["data"]

    def close(self):
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- CLI

class Out:
    """Prints one JSON envelope with --json, else short human text."""

    def __init__(self, as_json: bool, command: str, no_wait: bool = False):
        self.json, self.command = as_json, command
        self.warnings: list[str] = []
        self.no_wait = no_wait
        self.first_use = None           # the note when this command started the daemon

    def progress(self, line: str):
        """A progress line while waiting: stderr for people, warnings under --json."""
        if self.json:
            self.warnings.append(line)
        else:
            print(line, file=sys.stderr, flush=True)

    def warn(self, items):
        for w in items or []:
            if w not in self.warnings:
                self.warnings.append(w)
                if not self.json:
                    print(f"warning: {w}", file=sys.stderr, flush=True)

    def done(self, data, text: str | None = None) -> int:
        if self.json:
            print(json.dumps({"ok": True, "command": self.command, "data": data,
                              "warnings": self.warnings}))
        elif text:
            print(text)
        return 0

    def fail(self, e: KoradError) -> int:
        if self.json:
            print(json.dumps({"ok": False, "command": self.command, "error": e.to_dict(),
                              "warnings": self.warnings}))
        else:
            print(f"error: {e.message}", file=sys.stderr)
        return EXIT.get(e.code, 1)


def connect_or_start(name: str | None = None) -> tuple["Client", str | None]:
    """A client of the daemon. When no daemon answers and the config has autostart on (the
    default), start one first. Returns (client, the one-line first-use note or None)."""
    try:
        return Client(name=name), None
    except KoradError as e:
        if not (e.detail or {}).get("not_running"):
            raise
        cfg = load_config()
        if not cfg["autostart"]:
            raise
    info = spawn_daemon(cfg)
    note = "daemon started on first use (pid %s): %s" % (
        info["pid"], start_report_text(info.get("start_report")).strip().replace("\n", "; "))
    return Client(name=name), note


def _connect(out: Out, autostart: bool = True) -> Client:
    if not autostart:
        return Client()
    c, note = connect_or_start()
    if note:
        out.first_use = note
        out.warn([note])
    return c


FINAL_NEXT = {
    "blocked": "waiting will not fix this; follow the step above",
    "failed": "waiting will not fix this; follow the step above",
}


def wait_for_device(c: Client, out: Out, limit: float | None = None, poll: float = 0.25,
                    clock=time.monotonic) -> dict | None:
    """Wait for the daemon to reach the supply, and say what it is doing meanwhile.

    Returns the last state (or None when no wait was needed). Raises E_DEVICE at once on a
    cause that waiting will not fix (unplugged, not shared, attached elsewhere, port busy,
    re-attach off), and after `limit` seconds otherwise."""
    s = c.call("state", events_after=10 ** 12)
    if s["device"] == "present" or out.no_wait:
        return None
    if limit is None:
        limit = load_config()["wait_for_device_s"]
    if not limit:
        return None
    t0 = clock()

    def say(msg):
        out.progress(f"[{clock() - t0:4.1f} s] {msg}")

    if out.first_use:
        say("waiting for the supply: daemon started on first use; device not attached yet")
    else:
        since = s.get("absent_since")
        when = f" since {fmt_local(dt.datetime.fromtimestamp(since).astimezone())}" if since else ""
        say(f"waiting for the supply: device {s['device']}{when}")
    kicked_at = c.call("reconnect")["kicked_at"]
    last = None
    while True:
        s = c.call("state", events_after=10 ** 12)
        if s["device"] == "present":
            say("supply reached")
            return s
        r = s.get("reattach") or {}
        sig = (r.get("phase"), r.get("message"))
        if sig != last and r.get("message") and r.get("message") != "re-checking the supply now":
            say(r["message"])
        last = sig
        if r.get("final") and (r.get("since") or 0) >= kicked_at - 0.5:
            nxt = r.get("next_step") or FINAL_NEXT.get(r.get("phase"), "")
            raise KoradError("E_DEVICE", f"the supply cannot be reached: {r['message']}"
                             + (f". Next step: {nxt}" if nxt else "")
                             + f" (stopped waiting after {clock() - t0:.1f} s: waiting will not fix this)",
                             {"reattach": r, "device": s["device"], "waited_s": round(clock() - t0, 1)})
        if clock() - t0 >= limit:
            raise KoradError("E_DEVICE", f"the supply was not reached within {limit:g} s (last step: "
                             f"{r.get('phase', '?')}: {r.get('message', '?')}). Check that the supply is "
                             f"powered and connected, that 'usbipd.exe list' shows it, and "
                             f"'korad daemon status'. Use --no-wait to answer at once, or raise "
                             f"wait_for_device_s in the configuration",
                             {"reattach": r, "device": s["device"], "waited_s": round(clock() - t0, 1)})
        time.sleep(poll)


def _device_client(out: Out) -> Client:
    """A client for a command that needs the supply: connect (start on first use), then wait."""
    c = _connect(out)
    wait_for_device(c, out)
    return c


def _call(c: Client, out: Out, op, **fields):
    data = c.call(op, **fields)
    out.warn(c.last_warnings)
    return data


def _state(c, out):
    """The cache. Right after daemon start or a reconnect, wait up to 3 s for a sample taken
    after the device was opened, so a stale pre-disconnect sample is never shown as live."""
    s = _call(c, out, "state", events_after=10 ** 12)
    deadline = time.time() + 3
    while s["device"] == "present" and time.time() < deadline and (
            not s["seq"] or (s.get("opened_at") and (s["t"] or 0) < s["opened_at"])):
        s = _call(c, out, "wait_sample", after=s["seq"], timeout=max(0.1, deadline - time.time()),
                  events_after=10 ** 12)
    if s["device"] == "present" and s.get("opened_at") and (s["t"] or 0) < s["opened_at"]:
        out.warn([f"no sample since the device was reopened; the values are "
                  f"{time.time() - (s['t'] or 0):.1f} s old"])
    return s


def _onoff(s):
    if s not in ("on", "off"):
        raise KoradError("E_USAGE", f"expected on or off, got {s!r}")
    return s == "on"


def _preset_n(s):
    if s not in ("1", "2", "3", "4", "5"):
        raise KoradError("E_USAGE", f"preset number must be 1, 2, 3, 4 or 5 (ASCII digits), got {s!r}")
    return int(s)


class _Once(argparse.Action):
    """Refuse a repeated option: '-v 5 -v 3' is a mistake, not 'the last one wins'."""

    def __call__(self, parser, ns, values, option_string=None):
        if getattr(ns, self.dest, None) is not None:
            raise KoradError("E_USAGE", f"{option_string} given twice; give it once")
        setattr(ns, self.dest, values)


def _channel(s):
    if s not in ("1", "2", "12"):
        raise KoradError("E_USAGE", f"channel must be 1, 2 or 12 (both), got {s!r}")
    return s


def _fmt_sp(sp, ch):
    if not sp:
        return "set ?"
    return f"set {fmt_v(sp[f'v{ch}'])} {fmt_i(sp[f'i{ch}'])}"


def status_text(s: dict) -> str:
    st, m, sp = s.get("status"), s.get("measured"), s.get("setpoints")
    age = f"{time.time() - s['t']:.1f} s old" if s.get("t") else "no sample yet"
    if st:
        head = (f"output {'ON ' if st['output'] else 'OFF'}   mode {st['mode']}   "
                f"OVP {'on' if st['ovp'] else 'off'}   OCP {'on' if st['ocp'] else 'off'}   "
                f"sample {age}   device {s['device']}")
    else:
        head = f"device {s['device']}   {age}"
    lines = [head]
    for ch in (1, 2):
        meas = f"out {m[f'vout{ch}']:.2f} V {m[f'iout{ch}']:.3f} A" if m else "out ?"
        cvcc = ("CV" if st[f"ch{ch}_cv"] else "CC") if st else ""
        lines.append(f"CH{ch}  {_fmt_sp(sp, ch):28} {meas:24} {cvcc}")
    lines.append(s["guard_text"])
    return "\n".join(lines)


def _check_fresh(s):
    """Device 'present' but no new sample: the device thread is stuck or the link is dead."""
    if s["device"] != "present" or not s.get("t"):
        return
    hz = s.get("poll_hz")
    limit = max(3.0, 5 / hz) if isinstance(hz, (int, float)) and hz > 0 else 3.0
    age = time.time() - s["t"]
    if age > limit:
        raise KoradError("E_DEVICE", f"no fresh sample for {age:.0f} s: device thread stuck or link "
                         f"dead. The values below are STALE; the output may be ON. The daemon closes a "
                         f"hung port after {Daemon.WATCHDOG_S:g} s and reopens it; if this persists, "
                         f"re-attach the supply and run korad daemon stop\n" + status_text(s),
                         {"stale": True, "sample_age_s": round(age, 1), "last_known": s})


def cmd_status(a, out):
    c = _device_client(out)
    s = _state(c, out)
    s.pop("events", None)
    if not s["guard_active"]:
        out.warn([no_guard_warning(s["guard"])])
    if s["device"] != "present":
        since = s.get("absent_since")
        when = fmt_local(dt.datetime.fromtimestamp(since).astimezone()) if since else "start"
        raise KoradError("E_DEVICE", f"device {s['device']} since {when}: the daemon cannot reach "
                         f"the supply, and the values below are STALE (last known, not live). The "
                         f"output may be ON. The daemon retries every 2 s\n" + status_text(s),
                         {"stale": True, "last_known": s})
    _check_fresh(s)
    return out.done(s, status_text(s))


def cmd_set(a, out):
    ch = _channel(a.channel)
    v = parse_value(a.voltage, "v") if a.voltage is not None else None
    i = parse_value(a.current, "i") if a.current is not None else None
    if v is None and i is None:
        raise KoradError("E_USAGE", "nothing to set: give -v VOLTS and/or -i AMPS")
    c = _device_client(out)
    r = _call(c, out, "set", channel=ch, v=v, i=i)
    got = r["after"]
    chans = (1, 2) if r["mode"] != "independent" else [int(x) for x in ch]
    text = "\n".join(f"CH{n} {_fmt_sp(got, n)} (read back)" for n in chans)
    if r["mode"] == "series":
        text += f"\nseries total: {fmt_v(got['v2'] * 2)}"
    elif r["mode"] == "parallel":
        text += f"\nparallel total: {fmt_i(got['i2'] * 2)}"
    return out.done(r, text)


def cmd_out(a, out):
    on = _onoff(a.state)
    # OFF is tried at once (a failed OFF is kept pending by the daemon); ON waits for the supply.
    r = _call(_device_client(out) if on else _connect(out), out, "out", on=on)
    return out.done(r, f"output {'ON' if r['status']['output'] else 'OFF'} (status {r['status']['raw']})")


def cmd_mode(a, out):
    r = _call(_device_client(out), out, "mode", mode=a.mode)
    sp = r["setpoints"]
    return out.done(r, f"mode {r['status']['mode']}; output OFF (the supply does this on every mode "
                       f"change)\nCH1 {_fmt_sp(sp, 1)}\nCH2 {_fmt_sp(sp, 2)}")


def cmd_preset(a, out, save=False):
    r = _call(_device_client(out), out, "preset", n=a.n, save=save)
    sp = r["setpoints"]
    if save and a.n == 1:
        return out.done(r, r["message"])
    verb = "saved to" if save else "recalled"
    tail = "" if save else "; output OFF (the supply does this on every recall)"
    return out.done(r, f"{verb} M{a.n}{tail}\nCH1 {_fmt_sp(sp, 1)}\nCH2 {_fmt_sp(sp, 2)}")


def cmd_bit(a, out, which):
    on = _onoff(a.state)
    r = _call(_device_client(out), out, "bit", which=which, on=on)
    return out.done(r, f"{which.upper()} {'on' if r['status'][which] else 'off'} (status {r['status']['raw']})")


def _expiry(a, cfg):
    now = now_local()
    if a.for_:
        return now + dt.timedelta(seconds=parse_guard_duration(a.for_))
    if a.until:
        return next_local_time(a.until, now)
    if a.today:
        exp = next_local_time(cfg["today_ends"], now)
        if (exp - now).total_seconds() < 3600:
            out_warn = getattr(a, "_out", None)
            if out_warn:
                out_warn.warn([f"--today ends at {cfg['today_ends']}, only {fmt_delta((exp - now).total_seconds())} "
                               f"from now ({fmt_local(now)}). Use --for or --until for a longer window"])
        return exp
    return None


def _guard_diff(old, new) -> list[str]:
    lines = []
    for ch in (1, 2):
        for key, fmt in (("max_cv", fmt_v), ("max_ma", fmt_i)):
            o = guard_limit(old, ch, key) if guard_is_active(old) else None
            n = guard_limit(new, ch, key) if new else None
            if o != n:
                lines.append(f"  CH{ch} max {'voltage' if key == 'max_cv' else 'current'}: "
                             f"{fmt(o) if o is not None else 'none'} -> {fmt(n) if n is not None else 'none'}")
    om = (old.get("mode") if guard_is_active(old) else None) or "independent"
    nm = (new.get("mode") if new else None) or "independent"
    if om != nm:
        lines.append(f"  mode allowed: {om} -> {nm}")
    if guard_is_active(old) and new and old["expires_utc"] != new["expires_utc"]:
        lines.append(f"  expires: {fmt_local(dt.datetime.fromisoformat(old['expires_utc']))} -> "
                     f"{fmt_local(dt.datetime.fromisoformat(new['expires_utc']))}")
    return lines


def _ask_confirm(old, new, words, a, out, what):
    """Interactive retype. Returns the confirm string to send (None = let the daemon refuse)."""
    if a.confirm is not None or not words:
        return a.confirm
    if not sys.stdin.isatty() or out.json:
        return None
    print(f"This {what}:", file=sys.stderr)
    for line in _guard_diff(old, new) or ["  (guard removed)"]:
        print(line, file=sys.stderr)
    kinds = ", ".join(_word_kind(w) for w in words)
    try:
        ans = input(f"Retype {kinds} to confirm (Enter keeps the current guard): ")
    except EOFError:
        ans = ""
    if ans.strip() != " ".join(words):
        raise KoradError("E_CONFIRM", "not confirmed: the current guard stays")
    return ans.strip()


def cmd_guard(a, out):
    c = _connect(out)
    s = _call(c, out, "state", events_after=10 ** 12)
    cur = s["guard"]
    active = s["guard_active"]
    sub = a.gcmd or "show"
    if sub == "show":
        return out.done({"guard": cur, "active": active, "text": s["guard_text"]}, s["guard_text"])
    if sub == "clear":
        words = guard_loosening(cur, None)
        conf = _ask_confirm(cur, None, words, a, out, "clears the guard")
        r = _call(c, out, "guard_clear", confirm=conf)
        return out.done(r, r["text"])
    # set
    cfg = load_config()
    v = parse_value(a.voltage, "v", 2 * MAX_CV) if a.voltage is not None else None
    i = parse_value(a.current, "i", 2 * MAX_MA) if a.current is not None else None
    if v is None and i is None:
        raise KoradError("E_USAGE", "a guard needs a limit: give -v VOLTS and/or -i AMPS")
    entry = {"max_cv": v, "max_ma": i}
    if a.channel:
        limits = dict(cur["limits"]) if active else {}
        limits[a.channel] = entry
    else:
        limits = {"all": entry}
    if a.mode:
        mode = None if a.mode == "independent" else a.mode
    else:
        mode = (cur.get("mode") if active and a.channel else None)
    a._out = out
    exp = _expiry(a, cfg)
    if exp is None:
        if not active:
            raise KoradError("E_USAGE", "no guard is active, so give a time window: "
                             "--for 4h, --until 18:00 or --today")
        exp_utc = cur["expires_utc"]
    else:
        exp_utc = exp.astimezone(dt.timezone.utc).isoformat()
    note = a.note if a.note is not None else (cur.get("note", "") if active else "")
    new = {"version": 1, "limits": limits, "mode": mode, "expires_utc": exp_utc, "note": note,
           "set_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    validate_guard(new)
    validate_guard_window(new)
    words = guard_loosening(cur, new)
    conf = _ask_confirm(cur, new, words, a, out, "loosens the guard")
    r = _call(c, out, "guard_set", guard=new, confirm=conf)
    return out.done(r, r["text"])


LOG_COLS = ["time_iso", "t_rel_s", "vset1", "iset1", "vout1", "iout1", "vset2", "iset2", "vout2",
            "iout2", "output", "mode", "ch1_cv", "ch2_cv", "ovp", "ocp", "event"]


def _log_row(s, t0, events):
    sp, m, st = s.get("setpoints") or {}, s.get("measured") or {}, s.get("status") or {}
    t = s.get("t") or time.time()
    return {
        "time_iso": dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds"),
        "t_rel_s": f"{t - t0:.3f}",
        "vset1": f"{sp['v1'] / 100:.2f}" if sp else "", "iset1": f"{sp['i1'] / 1000:.3f}" if sp else "",
        "vout1": f"{m['vout1']:.2f}" if m else "", "iout1": f"{m['iout1']:.3f}" if m else "",
        "vset2": f"{sp['v2'] / 100:.2f}" if sp else "", "iset2": f"{sp['i2'] / 1000:.3f}" if sp else "",
        "vout2": f"{m['vout2']:.2f}" if m else "", "iout2": f"{m['iout2']:.3f}" if m else "",
        "output": int(st["output"]) if st else "", "mode": st.get("mode", ""),
        "ch1_cv": int(st["ch1_cv"]) if st else "", "ch2_cv": int(st["ch2_cv"]) if st else "",
        "ovp": int(st["ovp"]) if st else "", "ocp": int(st["ocp"]) if st else "",
        "event": " | ".join(f"{e['source']}: {e['kind']}: {e['message']}" for e in events),
    }


def _is_stdout(path):
    try:
        return os.path.samestat(os.stat(path), os.fstat(sys.stdout.fileno()))
    except (OSError, ValueError, AttributeError):
        return False


def cmd_log(a, out):
    interval = (a.interval or "1s").strip().lower()
    step = None if interval == "max" else parse_duration(interval)
    if step is not None and step <= 0:
        raise KoradError("E_USAGE", "interval must be above 0, or max")
    duration = parse_duration(a.duration) if a.duration else None
    if a.file in ("-", "/dev/stdout", "/dev/fd/1", "/proc/self/fd/1") or \
            (os.path.exists(a.file) and _is_stdout(a.file)):
        raise KoradError("E_USAGE", "log -f needs a file, not standard output (it would mix CSV "
                         "into the command's output); use --echo to also print rows")
    try:
        open(a.file, "a").close()
    except OSError as e:
        raise KoradError("E_USAGE", f"cannot write the log file {a.file!r}: {e.strerror or e}")
    c = _device_client(out)
    s = _call(c, out, "state", events_after=10 ** 12)
    if not s["guard_active"]:
        out.warn([no_guard_warning(s["guard"])])
    last_ev, last_seq = s["last_event_seq"], s["seq"]
    start = time.time()
    t0 = None
    rows = 0
    path = Path(a.file)
    new_file = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        if new_file:
            w.writeheader()
        if a.echo and not out.json:
            print(",".join(LOG_COLS), flush=True)
        try:
            next_t = mono0 = time.monotonic()
            while duration is None or time.time() - start < duration:
                if step is None:
                    left = 5 if duration is None else max(0.1, min(5, duration - (time.time() - start)))
                    s = c.call("wait_sample", after=last_seq, timeout=left, events_after=last_ev)
                    if s["seq"] == last_seq:
                        continue
                else:
                    if rows:
                        next_t += step
                    if duration is not None and next_t - mono0 > duration:
                        break
                    d = next_t - time.monotonic()
                    if d > 0:
                        time.sleep(d)
                    s = c.call("state", events_after=last_ev)
                last_seq = s["seq"]
                if s["last_event_seq"] < last_ev:   # the daemon restarted: seq began again at 1
                    last_ev = 0
                if s["events"]:
                    last_ev = s["events"][-1]["seq"]
                if t0 is None:
                    t0 = s.get("t") or time.time()
                row = _log_row(s, t0, s["events"])
                w.writerow(row)
                f.flush()
                rows += 1
                if a.echo and not out.json:
                    print(",".join(str(row[k]) for k in LOG_COLS), flush=True)
        except KeyboardInterrupt:
            pass
    el = time.time() - start
    data = {"file": str(path), "rows": rows, "seconds": round(el, 2),
            "rows_per_s": round(rows / el, 2) if el else None}
    return out.done(data, f"{rows} rows in {el:.1f} s ({data['rows_per_s']} rows/s) -> {path}")


def _ping():
    try:
        c = Client()
    except KoradError:
        return None
    try:
        return c.call("ping")
    except (KoradError, OSError):
        return None
    finally:
        c.close()


def _wait_lock_free(path: Path, timeout: float) -> bool:
    """True once nobody holds the daemon's flock: the port is closed too."""
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() > deadline:
                return False
        finally:
            os.close(fd)
        time.sleep(0.1)


STOP_WAIT_S = 10.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                   # a zombie child of this process counts as gone
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def _terminate(pid: int) -> str:
    """SIGTERM, then SIGKILL after 5 s. Returns what it took."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            return "SIGTERM"
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "SIGTERM"
    deadline = time.time() + 5
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    return "SIGKILL"


def spawn_daemon(cfg: dict, fake: bool = False, port=None, poll=None, usb_serial=None, usb_id=None) -> dict:
    """Start the daemon in the background and wait (up to 8 s) until a daemon answers.

    Used by `daemon start` and by start-on-first-use. When several clients start one at the same
    moment, the per-supply lock lets one daemon win; the others exit, and every caller gets the
    winner's ping."""
    load_guard_file(home_dirs()[1] / "guard.json")          # refuse to start on a bad guard file
    state_dir = home_dirs()[1]
    state_dir.mkdir(parents=True, exist_ok=True)
    log = state_dir / "daemon.log"
    argv = [sys.executable, str(Path(__file__).resolve()), "daemon", "start", "--foreground"]
    if fake or cfg.get("fake"):
        argv.append("--fake")
    if port:
        argv += ["--port", port]
    if poll is not None:
        argv += ["--poll", str(poll)]
    if usb_serial:
        argv += ["--usb-serial", usb_serial]
    if usb_id:
        argv += ["--usb-id", usb_id]
    with open(log, "a") as lf:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True)
    deadline = time.time() + 8
    exited_at = None
    while time.time() < deadline:
        info = _ping()
        if info:
            return info
        if exited_at is None and proc.poll() is not None:
            exited_at = time.time()
        if exited_at is not None and time.time() - exited_at > 2:
            # It exited and, after 2 s, no other daemon answers either. (Losing the race for the
            # lock to a daemon started at the same moment is normal: that one answers.)
            tail = log.read_text().splitlines()[-5:]
            raise KoradError("E_DAEMON", "the daemon exited at start:\n" + "\n".join(tail))
        time.sleep(0.2)
    raise KoradError("E_DAEMON", f"the daemon did not answer within 8 s; see {log}")


def start_report_text(rep) -> str:
    if not rep:
        return "\noutput at start: unknown (device not read)"
    sp = rep["setpoints"]
    chans = f"CH1 {_fmt_sp(sp, 1)}, CH2 {_fmt_sp(sp, 2)}, {rep['mode']}"
    raw = f"status {rep['raw']}" if rep.get("raw") else "status unknown"
    if rep.get("by_measure"):
        m = rep["measured"]
        raw += f", which says OFF, but VOUT reads {m['vout1']:.2f} / {m['vout2']:.2f} V"
    if not rep["output_on"]:
        return f"\noutput at start: OFF ({chans}; {raw})"
    if rep["turned_off"]:
        return (f"\noutput was ON at start ({raw}): {chans}\n"
                f"the startup rule turned the output OFF: " + "; ".join(rep["violations"]))
    text = f"\noutput was ON at start ({raw}): {chans} (left ON)"
    if rep["violations"]:
        text += "\nWARNING it breaks the guard: " + "; ".join(rep["violations"])
    return text


def cmd_daemon(a, out):
    sub = a.dcmd
    if sub == "start":
        cfg = load_config()
        poll = check_poll(a.poll) if a.poll is not None else None
        load_guard_file(home_dirs()[1] / "guard.json")      # refuse to start on a bad guard file
        if a.foreground:
            ids = {"usb_serial": check_usb_serial(a.usb_serial) if a.usb_serial else None,
                   "usb_id": check_usb_id(a.usb_id) if a.usb_id else None}
            Daemon(cfg, port=a.port, fake=a.fake or cfg["fake"], poll_override=poll, id_override=ids).serve()
            return 0
        info = _ping()
        if info:
            raise KoradError("E_DAEMON", f"a daemon already runs (pid {info['pid']}, {socket_path()})")
        if a.usb_serial:
            check_usb_serial(a.usb_serial)
        if a.usb_id:
            check_usb_id(a.usb_id)
        info = spawn_daemon(cfg, fake=a.fake, port=a.port, poll=a.poll,
                            usb_serial=a.usb_serial, usb_id=a.usb_id)
        log = home_dirs()[1] / "daemon.log"
        c = Client()
        s = _call(c, out, "state", events_after=10 ** 12)
        if not s["guard_active"]:
            out.warn([no_guard_warning(s["guard"])])
        return out.done(dict(info, socket=str(socket_path()), log=str(log)),
                        f"daemon started: pid {info['pid']}, socket {socket_path()}\n"
                        f"device: {info['device']} {info['idn'] or ''}".rstrip()
                        + start_report_text(info.get("start_report"))
                        + f"\n{s['guard_text']}")
    if sub == "stop":
        c = _connect(out, autostart=False)
        info = _call(c, out, "ping")
        pid, lock = info["pid"], info.get("lock")
        _call(c, out, "shutdown")
        c.close()
        sock = socket_path()
        deadline = time.time() + STOP_WAIT_S
        while sock.exists() and time.time() < deadline:
            time.sleep(0.2)
        if sock.exists():
            # The safe stop is stuck (a hung device thread): free the lock and the socket anyway.
            how = _terminate(pid)
            sock.unlink(missing_ok=True)
            raise KoradError("E_DEVICE", f"the safe stop did not finish within {STOP_WAIT_S:g} s: the "
                             f"daemon (pid {pid}) was terminated ({how}). The supply state is UNKNOWN: "
                             f"the output may still be ON. Press OUTPUT on the front panel or cut mains, "
                             f"then re-attach the supply and run korad daemon start",
                             {"pid": pid, "terminated": how})
        if lock and not _wait_lock_free(Path(lock), 10):
            raise KoradError("E_DAEMON", f"the daemon (pid {pid}) closed its socket but still holds "
                             f"{lock} (and so the port) after 10 s")
        stop_ev = None
        audit = home_dirs()[1] / "audit.jsonl"
        try:
            for line in reversed(audit.read_text().splitlines()[-50:]):
                ev = json.loads(line)
                if ev.get("kind") == "stop" and ev.get("pid") == pid:
                    stop_ev = ev
                    break
        except (OSError, json.JSONDecodeError):
            pass
        if not stop_ev:
            raise KoradError("E_DAEMON", f"the daemon (pid {pid}) stopped but left no stop record in "
                             f"{audit}: the safe stop state is unknown. Check the supply: korad is not "
                             f"running, so read the front panel", {"pid": pid})
        msg = stop_ev["message"]
        if "FAILED" in msg:
            raise KoradError("E_DEVICE", f"daemon stopped, but {msg}. The output may still be ON: "
                             f"press OUTPUT on the front panel or cut mains", {"stop_event": stop_ev})
        return out.done({"stopped": True, "stop_event": stop_ev}, f"daemon stopped. {msg}")
    if sub == "status":
        c = _connect(out, autostart=False)
        info = _call(c, out, "ping")
        s = _state(c, out)
        s.pop("events", None)
        _check_fresh(s)
        text = (f"daemon pid {info['pid']}, up {fmt_delta(info['uptime_s'])}, poll {s['poll_hz']} Hz"
                f"{', FAKE device' if info['fake'] else ''}\nidn: {info['idn']}\n" + status_text(s))
        return out.done({"daemon": info, "state": s}, text)
    if sub == "reload":
        r = _call(_connect(out), out, "reload")
        return out.done(r, "configuration reloaded")
    raise KoradError("E_USAGE", "daemon needs start, stop, status or reload")


def cmd_monitor(a, out):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise KoradError("E_USAGE", "monitor needs a terminal (stdin and stdout must be a tty); "
                         "for scripts use korad status or korad log")
    _device_client(out).close()     # start on first use and wait, before curses takes the screen
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import monitor
    return monitor.run(a.csv)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise KoradError("E_USAGE", f"{self.prog}: {message}")


def build_parser():
    p = _Parser(prog="korad", description="Control a Korad KA3305P through the korad daemon. "
                "See README.md and DESIGN.md one directory up.")
    p.add_argument("--json", action="store_true", help="print one JSON line")
    p.add_argument("--debug", action="store_true", help="print tracebacks")
    p.add_argument("--no-wait", action="store_true", help="do not wait for an absent supply; "
                   "answer at once (any position)")
    sp = p.add_subparsers(dest="cmd", parser_class=_Parser)

    sp.add_parser("status", aliases=["s"], help="setpoints, measured values, mode, guard")

    x = sp.add_parser("set", help="set a channel: current limit first, then voltage, with read-back")
    x.add_argument("channel", help="1, 2, or 12 for both")
    x.add_argument("-v", "--voltage", action=_Once, help="volts: 3.3, 3.3V, 3300mV")
    x.add_argument("-i", "--current", action=_Once, help="amps: 0.05, 0.05A, 50mA")

    x = sp.add_parser("out", aliases=["o"], help="output on|off (CH1 and CH2 together)")
    x.add_argument("state", help="on or off")

    x = sp.add_parser("mode", aliases=["m"], help="independent|series|parallel")
    x.add_argument("mode", choices=list(MODE_CMD))

    x = sp.add_parser("preset", aliases=["p"], help="recall memory slot 1-5")
    x.add_argument("n", type=_preset_n, help="1-5")
    x = sp.add_parser("preset-save", aliases=["ps"], help="save the setpoints to slot 1-5")
    x.add_argument("n", type=_preset_n, help="1-5")

    for which in ("ovp", "ocp"):
        x = sp.add_parser(which, help=f"{which.upper()} on|off")
        x.add_argument("state", help="on or off")

    g = sp.add_parser("guard", aliases=["g"], help="show, set or clear the voltage/current/mode guard")
    gs = g.add_subparsers(dest="gcmd", parser_class=_Parser)
    gs.add_parser("show")
    x = gs.add_parser("set", help="set limits; loosening needs a retyped confirmation")
    x.add_argument("-c", "--channel", action=_Once, choices=["1", "2"],
                   help="one channel; omit for both (global)")
    x.add_argument("-v", "--voltage", action=_Once, help="max volts")
    x.add_argument("-i", "--current", action=_Once, help="max amps")
    t = x.add_mutually_exclusive_group()
    t.add_argument("--for", dest="for_", action=_Once, metavar="DURATION", help="e.g. 4h, 90m")
    t.add_argument("--until", action=_Once, metavar="HH:MM", help="next occurrence, local time")
    t.add_argument("--today", action="store_true", help="until the next today_ends (default 10:00)")
    x.add_argument("--mode", action=_Once, choices=["independent", "series", "parallel"])
    x.add_argument("--note", action=_Once)
    x.add_argument("--confirm", action=_Once, help="the retyped word(s) for a looser guard")
    x = gs.add_parser("clear", help="remove the guard; needs the word clear")
    x.add_argument("--confirm", action=_Once)

    x = sp.add_parser("monitor", aliases=["mon"], help="interactive terminal view")
    x.add_argument("--csv", help="also log to this CSV file")

    x = sp.add_parser("log", aliases=["l"], help="log samples to a CSV file")
    x.add_argument("-f", "--file", action=_Once, required=True)
    x.add_argument("--interval", action=_Once, help="e.g. 1s, 0.25s, 2m; or max (every new sample); default 1s")
    x.add_argument("--duration", action=_Once, help="e.g. 10m; default until Ctrl-C")
    x.add_argument("--echo", action="store_true", help="also print rows")

    d = sp.add_parser("daemon", aliases=["d"], help="start|stop|status|reload the daemon")
    d.add_argument("dcmd", choices=["start", "stop", "status", "reload"])
    d.add_argument("--fake", action="store_true", help="simulated supply, no hardware")
    d.add_argument("--foreground", action="store_true")
    d.add_argument("--port", action=_Once, help="serial port; default: find usb_id (0416:5011) in sysfs")
    d.add_argument("--usb-serial", action=_Once, help="USB serial of the supply to use (overrides "
                   "config usb_serial)")
    d.add_argument("--usb-id", action=_Once, help="USB VID:PID of the supply (default 0416:5011)")
    d.add_argument("--poll", action=_Once, help="poll rate in Hz (1 or more), or max")
    return p


ALIASES = {"s": "status", "o": "out", "m": "mode", "p": "preset", "ps": "preset-save",
           "g": "guard", "mon": "monitor", "l": "log", "d": "daemon"}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # --json and --debug are global and may stand before or after the command.
    as_json, debug, no_wait = "--json" in argv, "--debug" in argv, "--no-wait" in argv
    argv = [x for x in argv if x not in ("--json", "--debug", "--no-wait")]
    out = Out(as_json, "korad", no_wait=no_wait)
    try:
        if as_json and ("-h" in argv or "--help" in argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    build_parser().parse_args(argv)
                except SystemExit:
                    pass
            return out.done({"help": buf.getvalue()})
        a = build_parser().parse_args(argv)
        if not a.cmd:
            if as_json:
                raise KoradError("E_USAGE", "no command given; see korad --help")
            build_parser().print_help()
            return EXIT["E_USAGE"]
        cmd = ALIASES.get(a.cmd, a.cmd)
        out.command = cmd
        handlers = {"status": cmd_status, "set": cmd_set, "out": cmd_out, "mode": cmd_mode,
                    "preset": cmd_preset, "preset-save": lambda a, o: cmd_preset(a, o, save=True),
                    "ovp": lambda a, o: cmd_bit(a, o, "ovp"), "ocp": lambda a, o: cmd_bit(a, o, "ocp"),
                    "guard": cmd_guard, "monitor": cmd_monitor, "log": cmd_log, "daemon": cmd_daemon}
        return handlers[cmd](a, out)
    except KoradError as e:
        if debug:
            import traceback
            traceback.print_exc()
        return out.fail(e)
    except KeyboardInterrupt:
        return 130
    except Exception as e:              # no traceback reaches the caller, and --json stays one line
        if debug:
            import traceback
            traceback.print_exc()
        return out.fail(KoradError("E_INTERNAL", f"internal error: {type(e).__name__}: {e} "
                                   f"(run with --debug for the traceback)"))


if __name__ == "__main__":
    sys.exit(main())
