# iot-lab

Command-line tools for bench instruments in a hardware lab. Each tool runs on
its own, from a shell or a script. Agent skills that drive these tools are in
[skill-bazaar](https://github.com/pepsi133/skill-bazaar).

| Directory | Device | Tool |
|---|---|---|
| [korad-ka3305p/](korad-ka3305p/README.md) | Korad KA3305P programmable DC power supply | `korad`: daemon, CLI, curses monitor, CSV log, voltage/current guard |

Each directory has its own README with the install steps, the requirements and
the measured behavior of the device.
