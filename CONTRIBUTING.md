# Contributing

Contributions are welcome: bug reports, fixes, new device tools, and
measurements from your own hardware.

## Report a bug or a device quirk

Open an issue. Include:

- the device model and firmware version, if known
- the command that you ran
- the output that you got and the output that you expected

## Send a change

1. Fork the repository and make a branch.
2. Keep each tool in its own directory, with its own `README.md`.
3. Run the tests for the tool that you changed, for example
   `python -m pytest korad-ka3305p/scripts/test_korad.py`.
4. Open a pull request. Tell what the change does and why.

## Add a new device

Make a new directory with the device name. Add a `README.md` with the install
steps, the requirements and the measured behavior of the device. Add a row to
the table in the top-level `README.md`.

## License of contributions

This project uses the Apache License 2.0. When you send a contribution, you
agree that it is licensed under the same license (see section 5 of
`LICENSE`). Keep the `NOTICE` file in every copy that you distribute.
