<p align="center"><img src="assets/icon-256.png" width="128" alt="Stripes icon"></p>

# Stripes

A small Windows app for designing and printing labels on Zebra printers.

- **Text, barcodes, QR codes and pictures.** Each text line has its own size and alignment.
- **Auto layout** stacks items to fill the label; drag items on the preview to place them by hand.
- Barcodes (Code 128, Code 39, EAN-13, UPC-A) and QR codes are made by the printer, so they scan reliably.
- **Label stock**: each printer has a default stock; pick another for a one-off print.
- Prints straight to a printer's IP address (port 9100), or through a printer installed in Windows.
- Shows whether the printer is ready, out of labels, paused or has the head open, and whether the
  labels loaded are the size of the label stock. **Stripes never changes printer settings**: labels
  only carry their content, so other software printing to the same printers isn't affected.
- **Printer monitor** shows every printer at once: problems and what to do about them first, then
  which printer it is, with everything else behind **More info**.

Download the latest `Stripes.exe` from **[Releases](../../releases/latest)**. It's a single file;
nothing to install.

## Company settings (printers and label stock)

IT keeps two files in a folder on a server share:

| File | What's in it |
|---|---|
| `printers.json` | Each printer: name, IP, model, dpi and default label stock |
| `stock.json` | Each kind of label roll: size and media type |
| `beta.json` | Optional: computers and users who get beta versions (see [Beta versions](#beta-versions)) |

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
| `shift_x`, `shift_y` | no | Nudge the print position, in dots (8 dots = 1 mm at 203 dpi) |

Keep your real files on the internal share, **not in this repo**.

## Deploying to domain PCs (Group Policy)

Each user runs their own copy from `%LocalAppData%\Programs\Stripes`, and **Stripes keeps itself up
to date**: at start (and every few hours while open) it checks the latest GitHub release, downloads
it, verifies its SHA-256 checksum and offers **Restart to update**. Users own that folder, so no admin
rights or install scripts are needed. (A copy in a folder it can't write to, like Program Files, just
says a new version is available.)

One GPO, linked to an OU that contains both the users and their PCs:

1. Put `Stripes.exe` from [Releases](../../releases/latest) on a share Domain Users can read
   (Properties > **Unblock** it first). It's only the starting copy; it never needs updating.
2. **Settings location**: *Computer Configuration > Preferences > Windows Settings > Registry*,
   action **Update**: hive `HKEY_LOCAL_MACHINE`, key `SOFTWARE\Policies\Stripes`, value `ConfigPath`
   (REG_SZ) = `\\SERVER\Share\Stripes\config`
3. **Each user's copy**: *User Configuration > Preferences > Windows Settings > Files*, action
   **Create** (copies only if missing, so it never undoes a self-update): source
   `\\SERVER\Share\Stripes\Stripes.exe`, destination `%LocalAppData%\Programs\Stripes\Stripes.exe`
4. **Shortcuts**: *User Configuration > Preferences > Windows Settings > Shortcuts*, location
   **Desktop** and **Start Menu**, target `%LocalAppData%\Programs\Stripes\Stripes.exe`

### Deployment log

Create a `status` folder next to `config` (e.g. `\\SERVER\Share\Stripes\status`) and give Domain
Users **Modify** on it. Each copy of Stripes then keeps one small file there (`COMPUTER_user.json`)
with its version, when it was installed, its last update, a heartbeat every 15 minutes while open,
pending updates and update errors. **Manage printers > Deployment status...** shows them all in one
table. Without the folder, nothing is written.

## Releasing a new version

Every push to `main` is tested and built by GitHub Actions on Windows (download the build from
the run's artifacts). To ship to all PCs, publish a release: on GitHub go to
**Actions > Build > Run workflow**, type the version (e.g. `v1.1.0`) and click **Run workflow**.
Pushing a tag (`git tag v1.1.0 && git push origin v1.1.0`) does the same.

That publishes a Release; each copy of Stripes picks it up the next time it starts (or within a few hours).

### Beta versions

To try a version on a few PCs first, list them in `beta.json` next to `printers.json`:

```json
{"beta": ["ARROW01", "jsmith"]}
```

Each entry is a computer name (anyone on that PC) or a Windows username (that person on any PC).
Then run the workflow with a beta version, e.g. `v1.4.0-beta.1`. It can be run from any branch.
It's published as a pre-release, which ordinary copies of Stripes ignore. Copies on listed PCs move
to it the next time they start (or within a few hours), and show "Beta" next to the version.

- Release the same code to everyone with `v1.4.0`; beta PCs move to it too.
- Beta PCs always run the newest of the latest release and the current beta, even if that's
  older than what they have, so publishing a fixed beta (or re-pointing `beta`) rolls them back.
- Take a PC off the list and it goes back to the latest release.
- **Deployment status** shows who is on beta.

A copy older than v1.4.0 doesn't know about betas: to start one on beta, download the beta's
`Stripes.exe` from Releases into `%LocalAppData%\Programs\Stripes` once (with Stripes closed).

## Development

```
python -m pip install -r requirements.txt
python stripes.py          # run from source
python -m unittest -v      # tests
build.bat                  # local Stripes.exe build (needs Python from python.org)
```
