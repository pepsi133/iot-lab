# iot-lab

Command-line tools for bench instruments in a hardware lab. Each tool runs on
its own, from a shell or a script. Agent skills that drive these tools are in
[skill-bazaar](https://github.com/pepsi133/skill-bazaar).

| Directory | Device | Tool |
|---|---|---|
| [korad-ka3305p/](korad-ka3305p/README.md) | Korad KA3305P programmable DC power supply | `korad`: daemon, CLI, curses monitor, CSV log, voltage/current guard |

Each directory has its own README with the install steps, the requirements and
the measured behavior of the device.

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Copyright 2026 Robert Żegleń. Licensed under the
[Apache License 2.0](LICENSE). If you distribute this code or a work based on
it, keep the [NOTICE](NOTICE) file, which names the author.
