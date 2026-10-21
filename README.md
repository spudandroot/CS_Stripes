<p align="center"><img src="assets/icon-256.png" width="128" alt="Stripes icon"></p>

# Stripes

A small Windows app for printing text labels on Zebra printers. Type the text, pick the printer,
set copies, click **Print** (or Ctrl+P).

- Prints straight to a printer's IP address (port 9100), or through a printer installed in Windows
- Text is sized automatically to fill the label, or set a fixed point size
- Live preview, "Bigger first line", left/center/right alignment, recent labels
- Shows whether the printer is ready, out of labels, paused or has the head open
- A company printer list (`printers.json`) that IT manages for every PC

Download the latest `Stripes.exe` from **[Releases](../../releases/latest)**. It's a single file;
nothing to install.

## Company printer list

Stripes reads `C:\ProgramData\Stripes\printers.json` (or a `printers.json` next to the exe).
Those printers appear for every user, marked "company", and can't be edited in the app.
See [`examples/printers.example.json`](examples/printers.example.json):

```json
{"printers": [
  {"name": "Packing ZD420", "host": "192.0.2.10", "model": "ZD420", "width_in": 2, "height_in": 1, "dpi": 203}
]}
```

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Shown in the printer list |
| `host` | yes* | Printer IP address |
| `windows_name` | yes* | Print through this installed Windows printer instead (*one of `host`/`windows_name`) |
| `port` | no | Default 9100 |
| `model` | no | `ZD420`, `ZT410` or `Other` |
| `width_in`, `height_in` | no | Label size in inches |
| `dpi` | no | 203, 300 or 600. Zebra's "200 dpi" is 203 |
| `shift_x`, `shift_y` | no | Nudge the print position, in dots (8 dots = 1 mm at 203 dpi) |

Keep your real `printers.json` on an internal share, **not in this repo** (it's git-ignored).

## Deploying to domain PCs (Group Policy)

One computer GPO linked to the OU with the label-printing PCs:

1. **Install and update the app**: *Computer Configuration > Preferences > Control Panel Settings >
   Scheduled Tasks > New > Scheduled Task (At least Windows 7)*
   - General: run as `NT AUTHORITY\SYSTEM`, **Run whether user is logged on or not**,
     **Run with highest privileges**
   - Triggers: **At startup** (delay 2 minutes) and **Daily** (random delay 1 hour)
   - Action: Start a program `powershell.exe` with arguments
     `-NoProfile -ExecutionPolicy Bypass -File "\\SERVER\Share\Stripes\Update-Stripes.ps1"`

   [`deploy/Update-Stripes.ps1`](deploy/Update-Stripes.ps1) downloads the latest release into
   `C:\Program Files\Stripes`, checks its SHA-256 checksum, and does nothing if it's already
   up to date. If Stripes is open, it tries again next run. Log: `C:\ProgramData\Stripes\update.log`.
2. **Printer list**: *Computer Configuration > Preferences > Windows Settings > Files*, action
   **Update**: `\\SERVER\Share\Stripes\printers.json` to `C:\ProgramData\Stripes\printers.json`
3. **Shortcut**: *Computer Configuration > Preferences > Windows Settings > Shortcuts*, **All Users
   Desktop**, target `C:\Program Files\Stripes\Stripes.exe`

Domain Computers need read access to the share.

## Releasing a new version

Every push to `main` is tested and built by GitHub Actions on Windows (download the build from
the run's artifacts). To ship to all PCs, tag a version:

```
git tag v1.1.0
git push origin v1.1.0
```

That publishes a Release, and PCs install it at their next scheduled check (within a day).

## Development

```
python -m pip install -r requirements.txt
python stripes.py          # run from source
python -m unittest -v      # tests
build.bat                  # local Stripes.exe build (needs Python from python.org)
```
