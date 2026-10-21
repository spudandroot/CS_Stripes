<p align="center"><img src="assets/icon-256.png" width="128" alt="Stripes icon"></p>

# Stripes

A small Windows app for designing and printing labels on Zebra printers.

- **Text, barcodes, QR codes and pictures.** Each text line has its own size and alignment.
- **Auto layout** stacks items to fill the label; drag items on the preview to place them by hand.
- Barcodes (Code 128, Code 39, EAN-13, UPC-A) and QR codes are made by the printer, so they scan reliably.
- **Label stock**: each printer has a default stock; pick another for a one-off print.
- Prints straight to a printer's IP address (port 9100), or through a printer installed in Windows.
- Shows whether the printer is ready, out of labels, paused or has the head open, and whether its
  settings match the spec IT set for it. **Stripes never changes printer settings**: labels only
  carry their content, so other software printing to the same printers isn't affected.

Download the latest `Stripes.exe` from **[Releases](../../releases/latest)**. It's a single file;
nothing to install.

## Company settings (printers and label stock)

IT keeps two files in a folder on a server share:

| File | What's in it |
|---|---|
| `printers.json` | Each printer: name, IP, model, dpi, default stock, and the settings it should have |
| `stock.json` | Each kind of label roll: size and media type |

Stripes finds the folder through a registry value set by Group Policy (so the server's name never
appears in this repo):

```
HKLM\SOFTWARE\Policies\Stripes   ConfigPath (REG_SZ) = \\SERVER\Share\Stripes\config
```

It reads the folder each time it starts and keeps a copy in the user's profile, so it still works if
the server is down. Users need **read** access to the folder. Without the registry value, Stripes looks
for the files in `C:\ProgramData\Stripes` or next to the exe.

See [`examples/config`](examples/config) for the format.

**stock.json**

| Field | Notes |
|---|---|
| `id` | Short name the printers refer to, e.g. `2x1-gap` |
| `name` | Shown in the app |
| `width_in`, `height_in` | Label size in inches |
| `media` | `gap`, `mark` (black mark) or `continuous` |

**printers.json**

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Shown in the printer list |
| `host` | yes* | Printer IP address |
| `windows_name` | yes* | Print through this installed Windows printer instead (*one of `host`/`windows_name`) |
| `port` | no | Default 9100 |
| `model` | no | e.g. `ZD420`, `ZT410` |
| `dpi` | no | 203, 300 or 600. Zebra's "200 dpi" is 203 |
| `stock` | no | `id` of the default label stock |
| `expected` | no | How the printer should be set up: `print_width` (dots), `media`, `print_mode` (`tear-off`, `peel-off`), `darkness`, `speed`. Compared with what the printer reports; never sent to it |
| `shift_x`, `shift_y` | no | Nudge the print position, in dots (8 dots = 1 mm at 203 dpi) |

Keep your real files on the internal share, **not in this repo**.

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
2. **Company settings location**: *Computer Configuration > Preferences > Windows Settings >
   Registry*, action **Update**: hive `HKEY_LOCAL_MACHINE`, key `SOFTWARE\Policies\Stripes`, value
   `ConfigPath` (REG_SZ) = `\\SERVER\Share\Stripes\config`
3. **Shortcut**: *Computer Configuration > Preferences > Windows Settings > Shortcuts*, **All Users
   Desktop**, target `C:\Program Files\Stripes\Stripes.exe`

Domain Computers need read access to the script; Domain Users need read access to the config folder.

## Releasing a new version

Every push to `main` is tested and built by GitHub Actions on Windows (download the build from
the run's artifacts). To ship to all PCs, publish a release: on GitHub go to
**Actions > Build > Run workflow**, type the version (e.g. `v1.1.0`) and click **Run workflow**.
Pushing a tag (`git tag v1.1.0 && git push origin v1.1.0`) does the same.

That publishes a Release, and PCs install it at their next scheduled check (within a day).

## Development

```
python -m pip install -r requirements.txt
python stripes.py          # run from source
python -m unittest -v      # tests
build.bat                  # local Stripes.exe build (needs Python from python.org)
```
