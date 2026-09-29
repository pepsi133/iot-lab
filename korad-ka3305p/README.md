# Korad KA3305P: the korad tool

`korad` controls a Korad KA3305P bench power supply over USB from Linux. One
daemon owns the serial port, and every command is a client of it. The tool
reads back every write, because the supply ignores a bad command in silence.
A guard limits the voltage, the current and the mode for a time window, so a
misclick or a cut input cannot set a dangerous value.

- `scripts/korad.py`: the daemon and the CLI
- `scripts/monitor.py`: a curses view with keyboard control (`korad monitor`)
- `scripts/test_korad.py`: the test suite, against a simulated supply
- [DESIGN.md](DESIGN.md): how the daemon, the guard and the CLI behave
- [config.example.toml](config.example.toml): every configuration key

The tool runs without an AI agent. An agent skill for it is in
[skill-bazaar](https://github.com/pepsi133/skill-bazaar/tree/main/skills/korad-ka3305p).

## Install

Requirements: Linux, Python 3.11 or later, and pyserial (`python3-serial` on
Debian and Kali). `--fake` runs a simulated supply and needs no pyserial.

```bash
git clone https://github.com/pepsi133/iot-lab.git
ln -s "$PWD/iot-lab/korad-ka3305p/scripts/korad.py" ~/.local/bin/korad
sudo usermod -aG dialout "$USER"      # then log in again
```

The symlink is enough: `korad.py` finds `monitor.py` next to its real path.

Try it without hardware:

```bash
korad daemon start --fake
korad status
korad daemon stop
```

Run the tests (about two minutes):

```bash
cd iot-lab/korad-ka3305p/scripts && python3 -m unittest test_korad
```

## Configuration

Copy [config.example.toml](config.example.toml) to
`~/.config/korad/config.toml`. The two identity keys matter when you have more
than one device with the same USB ID:

```toml
usb_id = "0416:5011"      # VID:PID of the supply
usb_serial = ""           # your supply's USB serial; empty: the only usb_id device
```

Find the serial with `cat /sys/class/tty/ttyACM*/device/../serial`, or with
`usbipd.exe state` on a Windows host. `KORAD_HOME` moves the state and the
configuration under one directory.

**USB/IP under WSL.** When `usbipd.exe` is found, the daemon attaches the
supply again after a power cycle, a replug or a Windows sleep. It finds the
device by `usb_id` and `usb_serial`, because the busid can change. It never
binds and never elevates: run `usbipd bind --busid <busid>` once yourself,
elevated. `usbipd_reattach = "off"` disables this.

**udev (optional).** The supply gets a `ttyACM` number in enumeration order.
A symlink rule gives it a fixed name, and the `ID_MM_DEVICE_IGNORE` flag stops
ModemManager from sending AT commands to it:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="0416", ATTRS{idProduct}=="5011", \
  ATTRS{serial}=="<your serial>", SYMLINK+="ttyPSU", ENV{ID_MM_DEVICE_IGNORE}="1"
```

## Quick start

```bash
korad guard set -v 3.3 -i 0.2 --for 2h --note "3V3 board on CH2"
korad set 2 -i 50mA -v 3.3       # current limit first, then voltage, read back
korad set 1 -v 0                 # park the channel you do not use
korad status
korad out on
korad out off
korad daemon stop                # output OFF, both channels to 0 V, independent
```

The first command starts the daemon. It stops itself after 5 minutes with the
supply absent and no active guard. `korad --help` lists every command, and
[DESIGN.md](DESIGN.md) gives the exit codes, the guard rules and the monitor
keys.

**Keep memory slot M1 at 0.00 V / 0.000 A.** At power-up the supply always
loads M1 and restores the front panel's On/Off state, and the tool cannot
change that state. With M1 at 0 V, a power cycle cannot put a voltage on the
target. `korad preset-save 1` refuses any setpoint above 0.

## The hardware

- **Outputs**: CH1 and CH2 programmable, 0–30 V / 0–5 A each. CH3 is a fixed
  5 V / 3 A rail with no remote control. CH1 and CH2 can run in series (up to
  62 V) or in parallel (up to about 10 A). This is a menu setting, not a wiring
  change.
- **USB**: `0416:5011`, CDC-ACM, so `cdc_acm` and `/dev/ttyACM<n>` on Linux,
  not `/dev/ttyUSB*`. The ID is Nuvoton's stock virtual-COM-port ID (Nuvoton
  inherited the Winbond vendor ID `0416`). Korad did not register its own. So
  the ID names the USB stack, not the supply: bind by serial.
- **Serial link**: 9600 8N1, ASCII. No line terminators, no checksum.
- **Firmware**: Korad publishes no versions and no update path. `*IDN?`
  returns the version, for example `KORAD KA3305P V7.2 SN:<8 digits>`.
- It is a linear supply. It runs hot, so do not box it in.

## Protocol behavior

Measured on one unit, firmware V7.2, over USB/IP from WSL.

### Timing

- The reply latency is 22–25 ms for a short query and 50 ms for `*IDN?`.
- **The minimum gap between two commands is 20 ms.** At 0, 5 and 10 ms the
  second command is lost. A query sent less than 20 ms after a write gets no
  reply.
- Two fragments of one command fuse when the gap is under 20 ms: `VSET2:1`
  and then `.50` gave 1.50 V. So a cut write can land as a different valid
  value. Send one complete command per write.
- Two commands in one write are both ignored. A trailing `\n` or `\r` is
  accepted.
- Queries with fixed-length reads run at about 43/s. A `korad set` with its
  read-backs takes about 425 ms. At `--poll max` a logger gets about 8
  samples/s over USB/IP.

### The supply never reports an error

Every bad input is ignored in silence, and the old setpoint stays. There is no
error reply and no status bit. **Only a read-back shows that a write failed.**

| Sent | Read back | Result |
|---|---|---|
| `VSET2:5`, `5.0`, `05.00`, `7.5` | `05.00`, `07.50` | accepted |
| `VSET2:5.123`, `VSET2:12.345` | old value | ignored: more than 2 decimals |
| `VSET2:31.00` | `31.00` | accepted: above the 30 V rating |
| `VSET2:35`, `99.99`, `-1` | old value | ignored |
| `ISET2:5.100` | `5.100` | accepted: above the 5 A rating |
| `ISET2:6`, `ISET2:0.0015` | old value | ignored (range, 4 decimals) |
| `FOO?`, `VSET3?`, `VOUT3?`, `*RST` | nothing | no reply |

The hardware ceiling is 31.00 V and 5.100 A per channel. Format a voltage with
2 decimals and a current with 3 decimals, or the write is lost.

### Commands

| Command | Effect |
|---|---|
| `VSETn:x`, `ISETn:x`, `n` = 1 or 2 | setpoint |
| `VSETn?`, `ISETn?` | setpoint read-back: `NN.NN`, `N.NNN` |
| `VOUTn?`, `IOUTn?` | measured output |
| `OUT1`, `OUT0` | **one switch for CH1 and CH2 together.** `OUT2:1` does nothing |
| `OCP1`/`OCP0`, `OVP1`/`OVP0` | over-current / over-voltage protection on/off |
| `TRACK0`/`TRACK1`/`TRACK2` | independent / series / parallel |
| `SAVm`, `RCLm`, `m` = 1..5 | save / recall a memory slot |
| `BEEP0`/`BEEP1`, `LOCK0`/`LOCK1` | accepted, no change in `STATUS?` |

### `STATUS?` byte

One raw byte, not ASCII.

| Bit | Meaning (observed) |
|---|---|
| 0 | CH1 mode: 1 = CV, 0 = CC |
| 1 | CH2 mode: 1 = CV, 0 = CC |
| 2–3 | tracking: `00` independent, `01` series, `10` parallel |
| 4 | OVP enabled |
| 5 | OCP enabled |
| 6 | output ON |
| 7 | output ON: set after `OUT1` over USB, and alone after a power-up with the output ON (`0x83`) |

A front-panel ON can read `0x43` (bit 7 clear), and a power-up ON can read
`0x83` (bit 6 clear). Treat either bit as ON. `OUT0` clears both.

### Behavior that a control tool must handle

- **`TRACK1` and `TRACK2` copy the CH2 setpoints into CH1**, and `TRACK0`
  does not restore the old CH1 value. In series and parallel, CH2 is the
  master: `VSET1` is ignored.
- **Any `TRACK` change and any `RCL` turn the output OFF.** `RCL` is a
  setpoint write and can load any stored value, up to 31 V / 5.1 A.
- **The output does not drop to 0 V at once after `OUT0`.** Into 1 kΩ from
  5 V it reached 0.00 V after about 0.8 s. With no load it took about 1.5 s.
- **`ISET 0.000` is not an off switch.** At VSET > 0 about 2.8 mA still flows
  into 1 kΩ. `IOUT` reads about 2–3 mA low at small currents, so a reading
  near 0 does not prove that no current flows.
- **The supply keeps its state when the USB link drops.** An output that was
  ON stays ON.
- **The front panel stays live** while the tool controls the supply. The
  knobs and the On/Off key still work. The daemon turns the output OFF when a
  knob change breaks the guard: about 0.2–0.3 s above the limit was seen.

### Power-up and memory

- At power-up the supply always loads the setpoints in memory slot M1, also
  when another slot was used last.
- It sets the output to the front panel's On/Off state **as it was at
  power-off**. The output state at the time of a save does not matter.
- `OUT0`/`OUT1` over USB switch the output but do not change the panel's
  On/Off state. So the output can come up ON although USB turned it OFF.
- Setpoints set over USB are lost at a power cycle unless they are saved into
  M1.
- A power cycle also drops a USB/IP attach. Until the next attach, no tool can
  see or change the supply.

The fix is to keep M1 at 0 V: save 0.00 V / 0.000 A on both channels into M1
on the front panel, with both outputs OFF. Keep working presets in M2–M5.
Then power-cycle once and make sure that the supply comes up OFF at 0 V.

### Other tools

- libsigrok's `korad-kaxxxxp` driver lists single-channel models only, not
  the KA3305P. `sigrok-cli` does not identify this supply.
- On many units `ISET1?` returns a sixth byte after `*IDN?` was queried (see
  the sigrok wiki). This unit did not show it. Read exactly five bytes.
- The protocol has no checksum, so a corrupted write is a valid write. Set the
  current limit first, read it back, and only then turn the output on.

## Sources

- libsigrok `korad-kaxxxxp` driver: https://github.com/sigrokproject/libsigrok/tree/master/src/hardware/korad-kaxxxxp
- USB ID `0416:5011` (DeviceHunt): https://devicehunt.com/view/type/usb/vendor/0416/device/5011
- List of USB IDs: http://www.linux-usb.org/usb.ids
