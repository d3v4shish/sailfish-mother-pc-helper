# Sailfish Mother-PC Helper

A dependency-free Linux companion for Sailfish Link, Sailfish Webcam, and LLs
vPlayer Remote Control. It receives phone discovery, keeps one trusted local
state record, and provides a local browser dashboard for SSH, Webcam, and
player handoffs.

## Quick start

Install for the current Linux user:

```sh
./install.sh
```

Then open **Sailfish Phone Control** from the desktop menu, or run:

```sh
sailfish-mother-pc-helper gui
```

The dashboard opens at `http://127.0.0.1:8766/` and listens for Sailfish Link
announcements on UDP port `45177`.

## Typical workflow

1. Connect the phone and Linux computer to the same trusted LAN.
2. Enable discovery in the Sailfish Link app.
3. Wait for the phone to appear in the dashboard.
4. Verify its device ID, then click **Trust this phone**.
5. Paste the optional Webcam and LLs Remote access tokens into the dashboard.
6. Use the displayed SSH command, Webcam URL/ffplay command, or LLs transport
   controls.

The helper will not use an SSH, Webcam, or LLs endpoint until the device ID has
been explicitly trusted.

## Security model

The dashboard binds only to `127.0.0.1`. Webcam and LLs tokens are stored in
`~/.config/sailfish-mother-pc-helper/secrets.json` with mode `0600`; status
responses never include them. Sailfish Link announcements are useful discovery
metadata, not cryptographic proof of identity, so always verify the device ID
before trusting it.

## CLI and service mode

Useful commands:

```sh
sailfish-mother-pc-helper doctor
sailfish-mother-pc-helper status
sailfish-mother-pc-helper listen
sailfish-mother-pc-helper gui --no-udp
```

Use `gui --no-udp` when the optional always-on `listen` user service is already
receiving discovery and maintaining the shared state file. Install that service
with `./install.sh --enable-listener`.

## Development

The project uses only Python's standard library. Run validation with:

```sh
python3 -m unittest discover -s tests -v
python3 sailfish_mother_pc_helper.py self-test
```

## License

No source license has been selected yet. The code is publicly visible, but
reuse and redistribution are not granted until a license is added.
