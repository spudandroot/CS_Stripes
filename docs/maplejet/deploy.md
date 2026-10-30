# MapleJet Hx build spec: deploy

Part 7 of 7. This file specifies how the MapleJet version of Stripes is built, checked and released:
what `pyinstaller.spec` bundles, the printer-password key (`hx_key.py`), the CI changes, the
self-check, the README changes, and the specs of two things delivered after development: the v2
folder script and the test-PC instructions. Names, types and layers come from architecture.md and
are not changed here. §1 lists what this file adds.

Contents: 0 Scope and conventions · 1 Interfaces added here · 2 What goes into the exe ·
3 The printer-password key · 4 CI and local builds · 5 Self-check · 6 Tests · 7 README updates ·
8 The folder script (spec) · 9 Test-PC instructions (spec) · 10 Notes for the other spec files ·
11 OPEN items · 12 Decisions made in this spec

## 0. Scope and conventions

1. Authority: the plan, then architecture.md, then this file. **Owner decision, 29 Oct 2026, which
   overrides the plan: no MSI for now.** The output stays exactly what it is today: one windowed,
   single-file `Stripes.exe`, built by PyInstaller from `pyinstaller.spec` in
   `.github/workflows/build.yml`, signed by the existing Azure Artifact Signing job, and deployed as
   today (Group Policy copies it to `%LocalAppData%\Programs\Stripes` for each user, README
   "Deploying to domain PCs"). There is no MSI, WiX, installer, `--quit` option or detection of old
   copies, and none may be added.
2. This file owns `pyinstaller.spec`, `make_version.py`, `test_make_version.py`,
   `.github/workflows/build.yml`, `build.bat`, `.gitignore`, `README.md`, the generated `hx_key.py`
   and the new `test_deploy.py`. Delivered after development, it also owns
   `tools/New-StripesV2Folders.ps1`, `test_folder_script.py` and `docs/maplejet/test-pc.md` (§8, §9).
   It lists the checks of `hx_ui.self_check(root)` (architecture §6.10: "deploy.md lists the
   checks"; the code in `hx_ui` is written by WP7, README.md "Work packages") and specifies three small
   hooks in `stripes.py` (§1.3), which WP9 writes together with every other `stripes.py` change. In
   build terms, WP9 writes every file listed in this item; this file specifies them.
3. No new runtime or build dependency. `requirements.txt` and the `pip install` line don't change.
4. Public repo: examples use `\\SERVER\Share\Stripes`, 192.0.2.x, UID 1109 with password 123321,
   `EXAMPLE\jsmith`, `time.example.local` and "Example Co". The key is never written into the
   repository, a log, an artifact name or a document.
5. Nothing on a PC needs administrator rights. Stripes creates its own PC folders on first use
   (storage §0.6); only the share folders need the folder script.

## 1. Interfaces added here

### 1.1 make_version.py

```python
KEY_ENV = "STRIPES_HX_KEY"     # environment variable: the key as 64 hex characters
KEY_FILE = "hx_key.py"         # written next to stripes_version.py
KEY_BYTES = 32

def parse_key(text: str) -> bytes
    # text.strip() must be exactly 64 hex characters (either case) -> 32 bytes. Otherwise
    # ValueError("STRIPES_HX_KEY must be 64 hex characters (32 bytes), not {n} characters") or
    # ValueError("STRIPES_HX_KEY must be hex"). A message never contains the text itself.
def key_id(key: bytes) -> str
    # hashlib.sha256(b"stripes-hx-key-id" + key).hexdigest()[:8]; equal to hx_protocol.key_id (tested)
def stamp_key(key: bytes | None, folder: str = ".") -> str
    # key: writes <folder>/hx_key.py (§3.4) and returns key_id(key)
    # None: removes <folder>/hx_key.py if it exists and returns "development"
def main(argv: list[str], environ: Mapping[str, str], folder: str = ".") -> int
    # §3.3; returns the exit code
```

`version_numbers(version)` and `stamp(version, folder=".")` keep their signatures and behaviour. The
file ends with `if __name__ == "__main__": sys.exit(main(sys.argv[1:], os.environ))`. It imports
only the standard library (no `hx_` module), so it runs before anything else exists.

### 1.2 hx_key.py (generated, git-ignored)

```python
"""The printer-password key of this build. Written by make_version.py from the STRIPES_HX_KEY
secret. Generated and git-ignored: never commit it."""
KEY = b'...'
```

`KEY` is the 32 bytes from `parse_key`, written as `repr(key)`. The module imports nothing.
`hx_protocol.release_key()` returns it (protocol.md §3.3). It exists only while a release is being
built (§3.2).

### 1.3 stripes.py hooks (additions to architecture §10)

architecture §10 lists every `stripes.py` change. These three come from the key injection and are
added to that list. They are specified here and written by WP9, the one package that edits
`stripes.py` (README.md "Work packages"). `stripes.py` may import `hx_key` (layer "build", no
imports of its own), as it already imports the generated `stripes_version`.

1. Right after the `VERSION` import block:

   ```python
   try:
       import hx_key  # noqa: F401  written by release builds: the printer-password key
       HX_KEYED = True
   except ImportError:
       HX_KEYED = False
   ```

2. In the "Versions" section, after `parse_version`:

   ```python
   def check_release_key(version, keyed):
       """A release (v1.2.3) must carry the printer-password key: with the public development key
       it couldn't read the printer passwords saved by the other PCs."""
       if parse_version(version) and not keyed:
           raise RuntimeError("this release has no printer-password key (set the STRIPES_HX_KEY secret)")


   def version_text():
       """The version in the header. A build without the release key says so."""
       if HX_KEYED:
           return tr("Version {version}", version=VERSION)
       return tr("Version {version} (development key)", version=VERSION)
   ```

   `App._build` (the `update_label`) and `App._language_changed` use `version_text()` instead of
   `tr("Version {version}", version=VERSION)`. The window title (`f"{APP_NAME} {VERSION}"`) is
   unchanged.
3. `_self_check()` calls `check_release_key(VERSION, HX_KEYED)` as its first line, before creating
   the Tk root, and `hx_ui.self_check(root)` as the last line inside its existing `try`
   (architecture §10.6 item 3). `self_check(out_path)` itself is unchanged: it still writes
   `ok <version>` or `failed: <repr of the exception>`.

### 1.4 hx_ui.self_check(root)

The signature is architecture's: `def self_check(root: tk.Misc) -> None`. ui.md §15.6 defers to
this file and adds no check of its own; §5.2 is the complete, ordered list. It raises
`RuntimeError(f"hx {name}: {e!r}")` from the first failure. Its helpers are private to `hx_ui`
(`_check_modules`, `_check_hashes`, `_check_key`, `_check_http`, `_check_sntp`,
`_check_files`, `_check_windows`, `_check_sound`, `_check_pictures`, `_check_tab`, the constants
`_SELF_CHECK_MODULES` and `_SELF_CHECK_UTC = 1_793_275_200.0`), so no public name is added.

### 1.5 i18n.ES

One new `stripes.py` text, added to `i18n.ES` directly (architecture §10.6 item 4). It is specified
here and written by WP1, the one package that edits `i18n.py` (README.md "Work packages"):

| English | Spanish |
|---|---|
| Version {version} (development key) | Versión {version} (clave de desarrollo) |

### 1.6 The folder script

`tools/New-StripesV2Folders.ps1 -ConfigPath <path> [-AdminGroup <name>] [-SenderGroup <name>]
[-UsersGroup <name>] [-WhatIf]`, specified in §8 (storage §14.3 is the contract it meets).

## 2. What goes into the exe

### 2.1 The hx modules

1. Every `hx_` module is a flat file next to `stripes.py` and is reached by plain `import`
   statements (`stripes` imports `hx_ui` and `hx_share`; `hx_ui` imports the rest down the layers).
   PyInstaller's analysis follows them from `stripes.py`, so **`hiddenimports` doesn't change**.
2. `hx_key` is imported by `stripes.py` (§1.3) and inside `hx_protocol.release_key()`. The analysis
   sees both. When the file is absent (every build except a release) PyInstaller notes a missing
   module and carries on; that is expected.
3. Rule for every builder: app code imports hx modules only with plain `import` / `from … import`
   statements, never `importlib.import_module` or `__import__` with a computed name. The
   self-check's module list (§5.2 check 1) is a check, not a way to bundle: every name on it is
   also imported statically.
4. Imports inside functions (`winsound` in `hx_win`, `wave` in the self-check) are fine: the
   analysis scans function bodies too.

### 2.2 The standard library without OpenSSL and multiprocessing

`pyinstaller.spec` keeps excluding `ssl`, `_ssl`, `_hashlib` and `multiprocessing` (and the rest of
today's `EXCLUDES`). What that means for hx code, checked on Python 3.12 with those modules blocked:

| Feature | In the exe | Notes |
|---|---|---|
| `hashlib.md5`, `hashlib.sha256` (also sha1, sha512, blake2) | yes | CPython's built-in `_md5` and `_sha2` take over when `_hashlib` is missing |
| `hmac.new(key, msg, hashlib.sha256)`, `hmac.digest`, `hmac.compare_digest` | yes | `hmac`'s pure-Python path |
| `secrets`, `random`, `base64`, `struct`, `json`, `re`, `gzip`, `zlib` | yes | |
| `hashlib.pbkdf2_hmac`, `hashlib.scrypt` | **no** | only exist with OpenSSL in 3.12; hx code must not use them |
| `ssl`, HTTPS through `urllib` | **no** | printers are plain HTTP on a raw socket; the Zebra exact preview keeps using `wininet_post` |
| `socket` (TCP and UDP), `threading`, `queue`, `tempfile`, `shutil`, `wave` | yes | |
| `concurrent.futures.ThreadPoolExecutor` | yes | `ProcessPoolExecutor` fails (no `multiprocessing`); hx code never uses processes |
| `zoneinfo` | **no, in practice** | Windows has no system time-zone database and `tzdata` isn't a dependency. Local time comes from `time.localtime` (send.md §9.3) |
| Tcl's `clock format` / `clock scan` | **no** | `_tcl_data/tzdata` and `msgs` are stripped (`TCL_UNUSED`). Tk widgets don't need them; hx UI code must not call Tcl's `clock` |
| `ctypes`, `ctypes.wintypes`, `msvcrt`, `winsound`, `winreg` | yes (Windows) | |
| `http.server`, `socketserver` | not bundled | only `hx_fake` uses them |

### 2.3 pyinstaller.spec changes (complete)

1. The header comment's first command line becomes:

   ```python
   #     python make_version.py v1.2.0      (writes stripes_version.py and version_info.txt, and
   #                                         hx_key.py when STRIPES_HX_KEY is set: see README)
   ```

2. `EXCLUDES` gets one more group, after the OpenSSL line:

   ```python
       # test-only: the fake printer must never ship (app code never imports it)
       "hx_fake",
   ```

   If app code ever imports `hx_fake`, the exe then fails at that import, and the self-check
   (§5.2 check 1) fails the build instead of shipping the fake.
3. Nothing else changes: `datas` already bundles the whole `assets` folder (so `assets/alert.wav`
   is in), the `wanted()` filter touches only Tcl data, `hiddenimports` stays
   `["pystray._win32"]`, and `console=False`, `upx=False`, the icon and `version_info.txt` stay.

### 2.4 Data files

| File | Bundled as | Owner | What the self-check enforces |
|---|---|---|---|
| `assets/alert.wav` | `assets/alert.wav` (through the existing `datas` entry) | ui.md §9.4 (generated by `hx_ui_common.alert_wav_bytes()` and committed, about 132 KB) | `wave` opens it as 22,050 Hz, 1 channel, 2-byte samples, 66,150 frames: exactly ui.md §9.4's format (§5.2 check 8) |
| `assets/icon*.png`, `icon.ico` | unchanged | | |

`testdata/`, `docs/`, `examples/`, `tools/` and the `test_*.py` files are never bundled (nothing in
the app imports or lists them).

### 2.5 Version information

`make_version.VERSION_INFO`'s `FileDescription` changes from `'Stripes - Zebra label printing'` to
`'Stripes - Zebra labels and MapleJet bag printing'` (shown in Task Manager and Properties).
`ProductName`, `InternalName`, `OriginalFilename` and the version fields don't change, so Group
Policy, AppLocker publisher rules and the existing test are unaffected.

## 3. The printer-password key

### 3.1 Where it comes from

1. The key is a **repository secret** named `STRIPES_HX_KEY` (Settings > Secrets and variables >
   Actions > Repository secrets): 64 hex characters, which is 32 random bytes. IT makes it once with
   `python -c "import secrets; print(secrets.token_hex(32))"` and keeps a copy in its password safe,
   because GitHub never shows a secret again.
2. It is a repository secret, not a "release" environment secret, because PyInstaller runs in the
   `build` job, which has no environment. Only the condition in §4.1 decides which builds get it.
3. It reaches `make_version.py` only through the environment of the "Stamp version" step, never on a
   command line, so it can't show in a process list or a log line. GitHub also masks it in logs.

### 3.2 Which builds carry it

| Build | Trigger | Version | Key | Signed | Published |
|---|---|---|---|---|---|
| Pull request | `pull_request` | `dev-<sha7>` | development | no | no |
| Push to main | `push` to main | `dev-<sha7>` | development | no | unsigned artifact |
| Run workflow, version empty | `workflow_dispatch` | `dev-<sha7>` | development | no | unsigned artifact |
| Sign only | `workflow_dispatch`, sign ticked | `dev-<sha7>` | development | yes | signed artifact |
| Release | `workflow_dispatch` with `release: v1.2.0` | `v1.2.0` | **release** | yes | GitHub release |
| Tag | push of a tag `v*` | the tag | **release** | yes | GitHub release |
| `build.bat` | local | `dev` or the argument | development, unless the shell has `STRIPES_HX_KEY` set | no | no |
| Source run | `python stripes.py` | `dev` | development | | |

"Development" means `hx_protocol.DEV_KEY` (protocol.md §1.1), which is public. Every development
build shows "Version … (development key)" in its header. Tokens saved by a development build can't
be read by a release and the other way round; the admin screen's `AuthFailed` text says "A saved
printer password was made by a different Stripes build. Enter it again." (protocol.md §14).

### 3.3 make_version.main(argv, environ, folder=".")

1. `len(argv) != 1`: print `usage: python make_version.py v1.2.0` to stderr and return 1.
2. `stamp(argv[0], folder)` (stripes_version.py and version_info.txt, as today).
3. `raw = environ.get(KEY_ENV, "").strip()`.
4. If `raw`: `key = parse_key(raw)`; on `ValueError` print its message to stderr and return 1
   without writing `hx_key.py` (any old one is removed first, so a failed run never leaves a stale
   key). Otherwise `kid = stamp_key(key, folder)` and print
   `Stamped {version} with printer-password key {kid}`.
5. If not: `stamp_key(None, folder)` (removes a stale `hx_key.py`) and print
   `Stamped {version} with the development key`.
6. Return 0. Nothing printed or raised ever contains the key or the raw text.

### 3.4 Writing hx_key.py

`stamp_key(key, folder)` writes `<folder>/hx_key.py` as UTF-8 with `\n` line ends: the docstring of
§1.2, then `KEY = {key!r}` and a final newline. It writes the whole file at once (`open(..., "w")`),
like `stamp`. `key_id(key)` is printed so a build log shows which key a release carries (8 hex
characters of a hash; it reveals nothing about the key) and matches the `hx1:<key id>:` prefix of
every token that key scrambled (protocol.md §3.2).

`parse_key` accepts exactly 32 bytes. `DEV_KEY` is 29 bytes, so a release can never carry the
development key by accident.

### 3.5 Development key and the version text

1. Source runs, tests and every non-release build have no `hx_key` module: `release_key()` returns
   `DEV_KEY` and the header says "Version dev (development key)" (Spanish "Versión dev (clave de
   desarrollo)").
2. A release-shaped version (`parse_version(VERSION)` is not None) without `hx_key` fails the
   self-check (`check_release_key`), so it can't be published. The "Stamp version" step already
   refuses to build a release when the secret is missing (§4.1); the self-check is the second guard
   and also covers a release-shaped local build if someone runs its self-check.
3. Tests never see the release key: the CI "Test" step runs before "Stamp version", and
   `test_deploy.py` fails if an `hx_key.py` is lying in the checkout (§6.2).

### 3.6 Keeping, losing and changing the key

1. **Never change it** in normal life. Every printer password saved in Stripes is scrambled with it,
   and all PCs must run builds with the same key.
2. If it is lost (deleted from GitHub and not in the password safe), or leaked: set a new
   `STRIPES_HX_KEY`, release, put that release on the share, and once PCs have it an administrator
   enters every printer's password again (MapleJet > Admin > Printers). Until then machines show the
   "different Stripes build" problem and no Send can reach those printers.
3. A new key never needs anything else: there is no key file on the share or on the PCs.

### 3.7 What the key does and doesn't protect

The plan chose obscurity on purpose: every PC can read the scrambled tokens because every PC polls.
The key keeps the tokens on the share from being unscrambled with the public source alone. It is
inside every released `Stripes.exe`, and the GitHub releases of this public repository can be
downloaded by anyone, so someone determined can extract it. The real protections are the share
permissions (only domain users can read printer records), the printers' own network segment that
accepts port 80 only from the floor PCs, and passwords changed before rollout (plan, Security and
permissions). See §11 item 1.

## 4. CI and local builds

### 4.1 build.yml changes (complete)

1. The comment at the top gets one more paragraph, after "…Group Policy copies it to PCs (see
   README).":

   ```yaml
   #
   # Releases carry the printer-password key from the repository secret STRIPES_HX_KEY (see
   # README). Every other build gets the public development key and shows "(development key)"
   # next to its version.
   ```

2. The `build` job gets a time limit, right under `runs-on: windows-latest`:

   ```yaml
       timeout-minutes: 30  # a hung test or self-check fails the build instead of holding a runner
   ```

3. The "Stamp version" step becomes (new lines marked `# new`; the others are today's):

   ```yaml
         - name: Stamp version
           id: stamp
           shell: pwsh
           env:
             RELEASE: ${{ inputs.release }}
             KEYED: ${{ github.ref_type == 'tag' || inputs.release != '' }}                        # new
             STRIPES_HX_KEY: ${{ (github.ref_type == 'tag' || inputs.release != '') && secrets.STRIPES_HX_KEY || '' }}  # new
           run: |
             $v = if ($env:RELEASE) { $env:RELEASE }
                  elseif ($env:GITHUB_REF_TYPE -eq "tag") { $env:GITHUB_REF_NAME }
                  else { "dev-$($env:GITHUB_SHA.Substring(0, 7))" }
             if ($env:RELEASE -and $env:RELEASE -notmatch '^v\d+\.\d+\.\d+$') { throw "Version must look like v1.2.0" }
             if ($env:RELEASE -or $env:GITHUB_REF_TYPE -eq "tag") {
               git merge-base --is-ancestor $env:GITHUB_SHA origin/main
               if ($LASTEXITCODE) { throw "Only main is released. Merge into main first, then release from main." }
             }
             if ($env:KEYED -eq "true" -and -not $env:STRIPES_HX_KEY) { throw "Releases need the STRIPES_HX_KEY secret (see README, Releasing a new version)." }  # new
             python make_version.py $v
             if ($LASTEXITCODE) { throw "make_version.py failed" }
             "version=$v" | Out-File -Append -Encoding utf8 $env:GITHUB_OUTPUT
   ```

   The `# new` markers are for this spec only; the real file has no such comments. `KEYED` is the
   same condition as the "Publish release" step's. On pushes and pull requests `inputs.release` is
   null, which GitHub compares equal to `''`, so both expressions are false and `STRIPES_HX_KEY`
   is empty. Pull requests from forks get no secrets anyway.
4. The comment above "Check the exe starts" becomes:

   ```yaml
         # The exe is windowed, so it can't print errors: have it load everything it needs (Tk,
         # Pillow, the tray, and the MapleJet parts: hashes, the printer-password key, HTTP and
         # time-server replies from fakes on 127.0.0.1, the Windows calls, the alert sound) and
         # write what it is to a file, before anything is published.
   ```

   The step itself is unchanged: it still requires exactly `ok <version>`.
5. Nothing else changes: the Python version, the dependency install, the Test step
   (`python -m unittest -v` on `windows-latest`), the PyInstaller step, both artifacts, the whole
   `sign` job (Azure login, signing `dist\Stripes.exe`, the signature check, the versioned copy) and
   "Publish release".

The unsigned and signed artifacts of a release contain the key, like the published release. They
are no more exposed than the release itself (§3.7).

### 4.2 Linux

CI stays on `windows-latest` only. Developers and the build agents run the same suite on Linux with
`xvfb-run -a /opt/stripes-venv/bin/python -m unittest -v` (architecture §9); that covers the
non-Windows fallbacks, which production never uses, so a Linux CI job would add a variable without
protecting a release.

### 4.3 build.bat

Unchanged except two lines after the `Done:` echo:

```bat
echo Local builds have the development key: printer passwords saved with them don't work in a
echo release. Only put releases from GitHub on the share.
```

`build.bat` already runs `python make_version.py %VERSION%`, which removes any stale `hx_key.py`
unless `STRIPES_HX_KEY` is set in that shell.

### 4.4 .gitignore

Add `hx_key.py` on the line after `version_info.txt`.

## 5. Self-check

### 5.1 How it runs

1. CI runs `dist\Stripes.exe --self-check <file>` (unchanged). `stripes.self_check` calls
   `_self_check()` and writes `ok <version>` or `failed: <repr>`.
2. `_self_check()` order: `check_release_key(VERSION, HX_KEYED)`; the Tk root (withdrawn); today's
   checks (Pillow, picture formats, fonts, ZPL, `wininet_post`, `winreg`, `pystray._win32`,
   `win32print`); then `hx_ui.self_check(root)`; finally `root.destroy()`.
3. Rules for `hx_ui.self_check`:
   1. It talks only to servers it starts itself on `127.0.0.1` with port 0. It never binds
      `0.0.0.0` (which could raise a firewall prompt), never resolves a host name and never reaches
      the network, a share or a printer.
   2. It never imports `hx_fake`.
   3. It plays no sound: CI runners have no audio device, so `PlaySound` of a file would fail there.
      It checks the file and the API instead (check 8).
   4. Files go only into `tempfile.mkdtemp()` folders, removed at the end
      (`shutil.rmtree(..., ignore_errors=True)`).
   5. Every server thread is a daemon with 5 s socket timeouts and is joined with a 5 s limit. The
      whole of `hx_ui.self_check` takes under 15 s on a CI runner.
   6. Environment variables it changes are restored in a `finally`, including removing ones that
      weren't set before.
   7. It is plain code that also runs unfrozen: `test_deploy.py` runs it from source on Linux under
      xvfb and on Windows in CI (§6.2). Checks marked "frozen" run only when
      `getattr(sys, "frozen", False)`; checks marked "Windows" only when `hx_win.is_windows()`.

### 5.2 The checks, in order

| # | Name | What it does | Passes when |
|---|---|---|---|
| 1 | `modules` | `importlib.import_module(n)` for each of `_SELF_CHECK_MODULES` = `("hx_core", "hx_model", "hx_message", "hx_state", "hx_win", "hx_protocol", "hx_time", "hx_share", "hx_send", "hx_poller", "hx_ui_common", "hx_ui_send", "hx_ui_monitor", "hx_ui_admin", "hx_ui")`. Frozen: `importlib.util.find_spec("hx_fake")` | every import works (so no two `add_spanish` calls conflict); frozen: `find_spec` returns None (the fake isn't in the exe) |
| 2 | `hashes` | the known answers of §5.3 | all equal |
| 3 | `key` | `key = hx_protocol.release_key()`; `t = make_token("123321")`; `s = scramble(t, key)`; `keyed` = whether `import hx_key` works | `len(key) >= 16`; `unscramble(s, key) == t`; `s.startswith(f"hx1:{key_id(key)}:")`; `is_development_key(key) == (not keyed)` |
| 4 | `http` | two exchanges with the loopback TCP fake (§5.4) | as §5.4 |
| 5 | `sntp` | one query to the loopback UDP fake (§5.5); `hx_time.local_utc_offset(time.time())` | `abs(utc - _SELF_CHECK_UTC) < 1.0`; the offset is a float between −50,400 and 50,400 |
| 6 | `files` | `hx_share.self_check()` (storage §15); then the swap test of §5.6 | no exception; the swap test as §5.6 |
| 7 | `windows` | `hx_win.self_check()` (architecture §6.3). Windows: `ident = hx_win.current_identity()`, `hx_win.lookup_group_sid(ident.user)`, `hx_win.monitors()` | no exception. Windows: `"\\" in ident.user`; `"S-1-1-0" in ident.sids` (Everyone, enabled in every sign-in token); the looked-up SID fullmatches `S-1-5-21(-\d+){4}`; `monitors()` returns a list (it may be empty on a runner) |
| 8 | `sound` | `path = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(hx_win.__file__))), "assets", "alert.wav")`; `wave.open(path, "rb")`. Windows: `import winsound`; `hx_win.stop_alert()` | the file is there and opens as 22,050 Hz, 1 channel, 2-byte samples, 66,150 frames; Windows: `winsound.SND_FILENAME`, `SND_ASYNC` and `SND_LOOP` exist and `stop_alert()` returns |
| 9 | `pictures` | (a) the BMP of §5.7: `bmp = hx_protocol.decode_preview(hx_protocol.PREVIEW_PREFIX + " " + base64.b64encode(raw).decode())`, `img = hx_ui_common.bmp_to_image(bmp)`, `ImageTk.PhotoImage(img, master=root)`. (b) On a withdrawn `Toplevel`: a `PictureView` given a 1-bit 1024×160 BMP made with Pillow (`Image.new("1", (1024, 160), 1)` saved as BMP); a `BagText` given `"Product of USA and Canada"`. (c) A 64×64 JPEG written to a temp file: `png = hx_ui_common.png_preview(path)`, then `hx_ui_common.photo_from_png(root, png)` | (a) `bmp == raw` and `img.size == (1024, 160)`; (b) `set_bmp` doesn't raise, `view.bmp` is the bytes given, and `BagText.get()` returns the same text; (c) `png` starts with `b"\x89PNG\r\n\x1a\n"` and `photo_from_png` doesn't raise. The `Toplevel` is destroyed afterwards |
| 10 | `tab` | §5.8: `make_services` on temp folders, then `MapleJetTab` | no exception |

### 5.3 Known answers

| Input | Expected |
|---|---|
| `hashlib.md5(b"123321").hexdigest()` | `c8837b23ff8aaa8a2dde915473ce0991` |
| `hx_protocol.make_token("123321")` | `Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE=` |
| `hx_protocol.password_md5(<that token>)` | `c8837b23ff8aaa8a2dde915473ce0991` |
| `hashlib.sha256(b"abc").hexdigest()` | `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad` |
| `hmac.new(b"Jefe", b"what do ya want for nothing?", hashlib.sha256).hexdigest()` (RFC 4231 case 2) | `5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843` |

### 5.4 The loopback HTTP fake

1. `server = socket.create_server(("127.0.0.1", 0))`, `server.settimeout(5.0)`, `port =
   server.getsockname()[1]`. A daemon thread `hx-selfcheck-http` accepts exactly two connections,
   one after the other. For each it reads until `b"\r\n\r\n"`, then as many body bytes as the
   request's `Content-Length` (0 if none), appends the whole request to `received`, sends the
   canned reply with `sendall` and closes the connection. Then it closes the server.
2. Reply 1, Autopilot (the real printer's headers, protocol.md §2.4):

   ```
   b"HTTP/1.1 200 OK\r\nServer: MapJet/v1.1\r\nConnection: close\r\nContent-Length: 4\r\n"
   b"Content-Type: text/plain\r\n\r\n1109"
   ```

3. Reply 2, the web login with a gzipped page: `page = b"<!DOCTYPE html><html><body>Stripes
   self-check</body></html>"`, `gz = gzip.compress(page, mtime=0)`:

   ```
   b"HTTP/1.1 200 OK\r\nServer: MapJet/v1.1\r\nConnection: close\r\nContent-Type: text/html\r\n"
   b"Content-Encoding: gzip\r\nSet-Cookie: Access_token_1109=a1b2\r\n"
   b"Set-Cookie: Auth_token_1109=c3d4\r\nContent-Length: <len(gz)>\r\n\r\n" + gz
   ```

4. Client, exchange 1: `req1 = hx_protocol.autopilot_request(1109, make_token("123321"),
   "<HXUI/>")`; `r1 = hx_protocol.http_exchange("127.0.0.1", port, req1, timeout_s=2.0,
   max_body=hx_protocol.MAX_AUTOPILOT_REPLY, label="self-check")`. Passes when `r1.status == 200`
   and `hx_protocol.check_reply("HXUI", r1.body.decode("ascii").strip()) == 1109`.
5. Client, exchange 2: `req2 = hx_protocol.login_request("127.0.0.1", port,
   password_md5(make_token("123321")))`; `r2 = http_exchange("127.0.0.1", port, req2, timeout_s=2.0,
   max_body=hx_protocol.MAX_LOGIN_REPLY, label="self-check", allow_cut_short=True)`. Passes when
   `r2.status == 200`, `r2.body == page` (gzip undone) and `r2.cookies() == {"Access_token_1109":
   "a1b2", "Auth_token_1109": "c3d4"}`.
6. After joining the thread: `received[0] == req1` (the bytes went out intact) and `received[1]`
   ends with `b"pass=c8837b23ff8aaa8a2dde915473ce0991&remember=on"`.

### 5.5 The loopback SNTP fake

1. `sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)`, `sock.bind(("127.0.0.1", 0))`,
   `sock.settimeout(5.0)`. A daemon thread `hx-selfcheck-sntp` receives one datagram and records
   it.
2. The reply is 48 bytes: byte 0 = `0x24` (leap 0, version 4, mode 4); byte 1 = 2 (stratum);
   bytes 24 to 31 = bytes 40 to 47 of the request (the originate timestamp); bytes 32 to 39 and 40
   to 47 both = `struct.pack("!II", int(_SELF_CHECK_UTC) + hx_time.NTP_EPOCH_OFFSET, 0)`; the rest 0.
3. Client: `utc = hx_time.sntp_query("127.0.0.1", port=port, timeout_s=2.0)`. Passes when
   `abs(utc - _SELF_CHECK_UTC) < 1.0` and the recorded request is 48 bytes with byte 0 = `0x23`
   (send.md §9.1). `_SELF_CHECK_UTC` is 29 Oct 2026 12:00 UTC; the client's result is that plus half
   the round trip.

### 5.6 The swap test

In a `tempfile.mkdtemp()` folder: write `a` = `b"old"`; `h = hx_win.open_for_read(a)`; write `b` =
`b"new"`; `os.replace(b, a)` must succeed while `h` is open; `h.read() == b"old"`; close `h`; a new
read of `a` gives `b"new"`. This is the property every status file relies on (storage §8.2 item 3),
checked with the exe's own ctypes and `msvcrt` calls. Off Windows it passes trivially.

### 5.7 The test picture

```python
raw = (b"BM" + struct.pack("<IHHI", 20542, 0, 0, 62)
       + struct.pack("<IiiHHIIiiII", 40, 1024, 160, 1, 1, 0, 20480, 0, 0, 2, 0)
       + bytes([0, 0, 0, 0, 255, 255, 255, 0]) + b"\xff" * 20480)
```

This is the header protocol.md §11.4 requires (it starts `42 4D 3E 50 00 00 …` and its base64 starts
`Qk0+UAAA`), all white.

### 5.8 The MapleJet tab

1. `tmp = tempfile.mkdtemp()`. Save, then set: `STRIPES_V2_ROAMING = <tmp>/roaming`,
   `STRIPES_V2_LOCAL = <tmp>/local`, `STRIPES_V2_MACHINE = <tmp>/machine`; remove `STRIPES_V2` so
   no share can be found.
2. `services = hx_ui.make_services(root, config_path=None, bg=hx_ui_common.SyncBackground(),
   version="self-check", clock=hx_core.FakeClock(), identity=hx_model.Identity(user="SELFCHECK\\stripes",
   computer="SELFCHECK", sids=frozenset(), pid=os.getpid()), key=hx_protocol.release_key())`.
   Nothing is started (architecture §9.3).
3. `tab = hx_ui.MapleJetTab(root, services)`; `root.update_idletasks()`; `tab.destroy()` (which
   unsubscribes from the bus, ui.md §15.7).
4. `finally`: restore the environment and remove `tmp`. `stop_services` is **not** called.

This proves every UI module, Tk and Pillow path the tab touches is in the exe, without a share or
a printer: the tab shows its "not set up" state.

## 6. Tests

All `unittest`, in the style of the existing files, and part of the normal suite (Linux under xvfb,
Windows in CI).

### 6.1 test_make_version.py (additions)

| Test | Expectation |
|---|---|
| `test_parse_key` | `"00" * 32` gives 32 zero bytes; upper-case hex and surrounding whitespace are accepted; 63 or 65 characters, `"zz" * 32` and `""` raise `ValueError`; no message contains the input |
| `test_stamp_key` | `stamp_key(k, folder)` writes `hx_key.py`; `exec` of it gives `KEY == k`; it returns `key_id(k)`; `stamp_key(None, folder)` removes the file and returns `"development"`; removing when there is no file is fine |
| `test_key_id_matches_protocol` | `make_version.key_id(k) == hx_protocol.key_id(k)` for `hx_protocol.DEV_KEY` and for `secrets.token_bytes(32)` |
| `test_main` | (stdout captured) `main(["v1.2.0"], {}, folder)` returns 0, leaves no `hx_key.py`, prints "development key"; with `{"STRIPES_HX_KEY": h}` (64 hex) it returns 0, writes `hx_key.py` and its output doesn't contain `h`; with a bad key it returns 1, writes no `hx_key.py` (even if one was there before) and stderr doesn't contain the bad text; `main([], {})` returns 1 |
| `test_stamp` (existing) | unchanged; `FileDescription` isn't asserted |

### 6.2 test_deploy.py (new)

| Class | Test | Expectation |
|---|---|---|
| `RepoTests` | `test_key_file_ignored` | `.gitignore` has a line that is exactly `hx_key.py` |
| | `test_no_key_in_checkout` | there is no `hx_key.py` next to the tests (CI tests run before "Stamp version"; `make_version.py` without the secret removes it) |
| | `test_app_never_imports_fake` | `ast` scan of `stripes.py`, `label_design.py`, `zpl_view.py`, `i18n.py` and every `hx_*.py` except `hx_fake.py` and `hx_key.py`: no `Import`/`ImportFrom` of `hx_fake`, and no `import_module(...)` or `__import__(...)` call whose argument is the constant `"hx_fake"` (the self-check's `find_spec("hx_fake")` is allowed) |
| | `test_spec_excludes` | `ast.parse` of `pyinstaller.spec`, the `EXCLUDES` assignment through `ast.literal_eval`: it contains `ssl`, `_ssl`, `_hashlib`, `multiprocessing` and `hx_fake`, and no `hx_` module other than `hx_fake` |
| `BundleTests` | `test_app_runs_without_excluded_modules` | a subprocess (`sys.executable -c`, cwd the repo, 60 s timeout) first sets `sys.modules[n] = None` for `_hashlib`, `ssl`, `_ssl`, `multiprocessing` and `hx_fake`, then imports `stripes` and `hx_ui`, checks the MD5 known answer and a `scramble`/`unscramble` round trip with `release_key()`, and prints `ok`; the test expects return code 0 and `ok` |
| `SelfCheckTests` (skipped if Tk can't start; one withdrawn root per class) | `test_hx_self_check_from_source` | `hx_ui.self_check(root)` returns without raising (non-frozen; the Windows rows run on Windows CI) |
| | `test_self_check_writes_ok` | with `stripes.VERSION` patched to `"dev"` (so a local `stripes_version.py` doesn't matter), `stripes.self_check(path)` writes exactly `"ok dev\n"` |
| | `test_self_check_reports_failure` | with `hx_time.sntp_query` patched to raise `TimeServerError` (the self-check calls it through the module, so the patch applies), `stripes.self_check(path)` writes a line starting `failed: ` that contains `hx sntp` |
| | `test_release_needs_key` | `stripes.check_release_key("v2.0.0", False)` raises `RuntimeError` mentioning `STRIPES_HX_KEY`; `("v2.0.0", True)`, `("dev", False)` and `("dev-abc1234", False)` don't raise |
| | `test_version_text` | with `stripes.VERSION` patched to `"dev"` and `stripes.HX_KEYED` to False: `version_text()` is `"Version dev (development key)"`, and `"Versión dev (clave de desarrollo)"` after `i18n.set_language("es")` (reset in `tearDown`); with `HX_KEYED` True: `"Version dev"` |

### 6.3 test_folder_script.py

Delivered with the script; specified in §8.8.

## 7. README updates

The README stays short and plain, like today. Changes, in README order (exact text for the parts
that matter; the implementer may reflow lines):

1. **First line**: "A small Windows app for printing labels on Zebra printers and bag codes on
   MapleJet Hx printers."
2. **Feature list**: the "Stripes never changes printer settings" bullet starts "For Zebra printers,
   **Stripes never changes printer settings**: …" (rest unchanged), and two bullets are added after
   "Printer monitor":

   ````markdown
   - **MapleJet Hx bag coding** (the MapleJet tab): administrators set up products, films and print
     texts; senders send them to a line's printers. Stripes reads every message back from the
     printer, three times, before the sender confirms it from the printer's own pictures. A monitor
     and a floor board show what every machine is printing, with alerts.
   - On MapleJet Hx printers Stripes writes only its own messages (slots 9 and 10), the printer's
     clock, and printing on or off. It never changes printer-wide settings.
   ````

3. **New section after "Company settings (printers and label stock)"**:

   ````markdown
   ## MapleJet Hx printers (set-up for IT)

   **Network.** Give each printer a fixed IP address on its own network segment that only accepts
   port 80 from the floor PCs. Stripes talks to printers directly, never through a proxy, one
   request at a time. PCs ask the time server over SNTP (UDP 123). Change every printer's password
   before rollout.

   **Groups.** Create two AD groups: `Stripes Administrators` (set up products, films, print texts,
   lines, machines and printers; run test prints) and `Stripes MapleJet Senders` (send, stop,
   resume, set clocks). Someone who does both goes in both. Changes take effect at the person's
   next Windows sign-in.

   **Folders on the share.** The MapleJet version keeps everything in a `v2` folder next to
   today's `config` folder (`\\SERVER\Share\Stripes\v2`). Today's version never reads it, so old and
   new copies can run side by side during a rollout. Create it once with the folder script, as an
   account with Full Control on `\\SERVER\Share\Stripes`, first with `-WhatIf` to see what it
   will do:

       powershell -NoProfile -File New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config -WhatIf
       powershell -NoProfile -File New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config

   It creates the folders and sets their permissions, copies `printers.json` and `stock.json` into
   `v2\config` once, and writes the first company settings with the two groups. Re-running it is
   safe: it puts the permissions back and never deletes anything. Get it from `tools/` in the
   release's source, read it, and `Unblock-File` it if your execution policy asks.

   | Folder under `v2\` | Domain Users | Stripes MapleJet Senders | Stripes Administrators |
   |---|---|---|---|
   | `config\` (Zebra printers and stock, `hx\` settings) | Read | Read | Modify |
   | `templates\` (products, films, print texts) | Read | Read | Modify |
   | `status\machines`, `status\pollers`, `status\deployment` | Modify | Modify | Modify |
   | `status\lines`, `status\locks`, `status\alerts` | Read | Modify | Modify |
   | `logs\hx-sends` | Read | Read and add files | Read |
   | `logs\hx-events` | Read and add files | Read and add files | Read and add files |
   | `logs\hx-admin` (admin changes and test prints) | Read | Read | Read and add files |

   Log files can be added but never changed or deleted, except by the server's administrators.

   **Share settings.** Keep `v2` on one file server (not replicated with DFS-R; a namespace with one
   target is fine), turn offline caching off for the share (`Set-SmbShare -Name <share> -CachingMode
   None`), and use the UNC path in `ConfigPath`, never a mapped drive.

   **Zebra files.** The new version reads `printers.json` and `stock.json` from `v2\config` (and
   from `ConfigPath` until the folder script has copied them). Until every PC runs the new version,
   make each Zebra printer or stock change in both `config\` and `v2\config\`.

   **Printer passwords.** An administrator types each printer's password once, in
   MapleJet > Admin > Printers. Stripes stores only a token made from it, scrambled with a key that
   is built into each release (it isn't in this public repository). Every PC can read the scrambled tokens because
   every PC watches the printers; that's obscurity, chosen on purpose. If Stripes says a password
   "was made by a different Stripes build", enter it again.

   **If a group is replaced.** The company settings keep each group's ID. If the administrators'
   group itself is deleted and re-created, nobody can open the admin screens: edit
   `admin_group_sid` in `v2\config\hx\company.json` (Notepad is fine). Stripes picks it up within
   about 10 minutes, or when it restarts.

   **First rollout, in order:** create the groups and add people; put the printers on their
   segment and change their passwords; make the release (see Releasing); run the folder script;
   try the release on one PC (below); then put it on the share. An administrator then enters the
   company settings (time server, countries, inks), printers, lines, machines, products, films and
   print texts.
   ````

4. **"Deploying to domain PCs"**: add one paragraph after the numbered list:

   ````markdown
   The MapleJet version is the same single `Stripes.exe` with the same Group Policy: nothing else
   to install, and no new registry value (it finds `v2` next to `ConfigPath`). Every running copy
   that can reach the printers, including one waiting in the tray, takes a share of watching them;
   office PCs that can't reach them never do.
   ````

5. **"Deployment log"** is replaced by:

   ````markdown
   ### Deployment log

   Each copy of Stripes keeps one small file (`COMPUTER_user.json`) with its version, when it was
   installed, its last update and a heartbeat every 15 minutes while open. **Manage printers >
   Deployment status...** shows them all in one table. The MapleJet version writes it to
   `v2\status\deployment` (the folder script creates it, with Modify for Domain Users), and the
   window also shows the older copies that still write to `\\SERVER\Share\Stripes\status`.
   Without the folder, nothing is written.
   ````

6. **New subsection at the end of "Deploying to domain PCs"**:

   ````markdown
   ### Trying a new version on one PC first

   1. Download `Stripes.exe` from the new release and **Unblock** it. Keep the old one on the share.
   2. On the test PC, put it in `%LocalAppData%\Programs\Stripes-test\` (not the Group Policy
      folder, which is replaced at every logon) and start it from there. The old copy can keep
      running; they don't share files.
   3. Check the version in the top right corner. It must not say "(development key)".
   4. Work through the checklist in `docs/maplejet/test-pc.md`.
   5. When it passes, put that same `Stripes.exe` on the share. PCs get it at their next logon.
      Delete the `Stripes-test` folder.

   To go back, put the previous `Stripes.exe` on the share. The `v2` folder stays and is ignored by
   the old version; the printers keep printing whatever they were last sent.
   ````

7. **"Releasing a new version"**: add at the end:

   ````markdown
   ### Printer-password key (once)

   Releases carry a key that scrambles the printer passwords saved in Stripes. Set it up once:

   1. Make a key: `python -c "import secrets; print(secrets.token_hex(32))"`.
   2. On GitHub: **Settings > Secrets and variables > Actions > New repository secret**, name
      `STRIPES_HX_KEY`, value the 64 characters.
   3. Keep a copy in IT's password safe. GitHub never shows it again. Never put it in this
      repository or in a file on the share.

   A release without the secret stops at "Stamp version". Other builds use a public development
   key and say "(development key)" next to their version; passwords saved with one don't work in a
   release. **Don't change the key**: every saved printer password depends on it. If it's lost or
   leaks, set a new one, release, and once PCs have that release an administrator enters every
   printer's password again.
   ````

8. **"Development"**: the code block becomes

   ````markdown
   ```
   python -m pip install -r requirements.txt
   python stripes.py          # run from source
   python -m unittest -v      # tests (on Linux: xvfb-run -a python -m unittest -v)
   python hx_fake.py          # fake MapleJet printers and share; prints the variables to set
   build.bat v1.2.0           # local build, dist\Stripes-v1.2.0.exe (needs Python from python.org)
   ```
   ````

   and the paragraph after it gains: "`hx_fake.py` is for tests and trying the MapleJet tab only; it
   is never built into the exe. Source runs and local builds use the development key."

## 8. The folder script (spec; delivered after development)

### 8.1 Delivery

1. File: `tools/New-StripesV2Folders.ps1`, committed to the repository after development, with
   `test_folder_script.py` (§8.8). It isn't attached to releases; IT takes it from the release
   tag's source, so the script and the exe always match.
2. Windows PowerShell 5.1 and PowerShell 7 or later. No modules beyond what Windows has; the
   `SmbShare` module is used only for the optional check in §8.3 step 9.
3. Comment-based help (`.SYNOPSIS`, `.DESCRIPTION`, one `.PARAMETER` per parameter, two
   `.EXAMPLE`s: with `-WhatIf` and without), so `Get-Help .\New-StripesV2Folders.ps1 -Full` explains
   it.
4. It is unsigned. OPEN: sign it if the file server's execution policy is `AllSigned` (§11).

### 8.2 Parameters

`[CmdletBinding(SupportsShouldProcess = $true)]`, so `-WhatIf` and `-Confirm` work.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `-ConfigPath` | string, mandatory | | Today's Zebra config folder, as in the `ConfigPath` policy value: `\\SERVER\Share\Stripes\config`. A local path on the file server also works. `v2` is created as its sibling |
| `-AdminGroup` | string | `Stripes Administrators` | Name of the administrators' group (`Group` or `DOMAIN\Group`) |
| `-SenderGroup` | string | `Stripes MapleJet Senders` | Name of the senders' group |
| `-UsersGroup` | string | `Domain Users` | Everyone who runs Stripes. On a domain that isn't in English, pass its own name for Domain Users |
| `-WhatIf` | switch | | Show every change without making it |

### 8.3 Procedure

`$ErrorActionPreference = 'Stop'`. Every step prints one line per item (`Created`, `Exists`,
`Rights set`, `Rights unchanged`, `Removed entry`, `Copied`, `Kept`, `Wrote`, `Warning`).

1. `ConfigPath` (trailing `\` removed) must be an existing folder (`Test-Path -LiteralPath
   -PathType Container`). `$Stripes = Split-Path -Parent $ConfigPath`; `$V2 = Join-Path $Stripes
   'v2'`. Print both. If `$ConfigPath` is on a mapped drive (`[System.IO.DriveInfo]` of its root
   has `DriveType` `Network`), stop: "Use the UNC path, as in the ConfigPath policy value".
2. Resolve every SID before changing anything: the three groups with
   `([System.Security.Principal.NTAccount]$name).Translate([System.Security.Principal.SecurityIdentifier])`;
   `SYSTEM` = `S-1-5-18`, `BUILTIN\Administrators` = `S-1-5-32-544`, OWNER RIGHTS = `S-1-3-4`
   (well-known SIDs, so they work in any language). A group that doesn't resolve stops the script
   with "Group '<name>' wasn't found. Create it, or pass its name with -<Parameter>." and nothing has
   changed. Print each name with its SID.
3. Create each folder of §8.4 that is missing, top-down (`New-Item -ItemType Directory`, inside
   `ShouldProcess`). Never delete or rename anything.
4. Set the rights of §8.4 on each folder, top-down, `v2` first (§8.5). Each folder gets exactly its
   explicit entries: explicit entries that aren't in its set are removed and printed (`Removed entry:
   <account> <rights> on <folder>`), so a re-run puts the rights back. Inherited entries are left to
   Windows. Only folders whose rights differ are written (compare
   `GetSecurityDescriptorSddlForm('Access')` before and after building the new ACL).
5. The one-time Zebra copy: for `printers.json` and `stock.json`, if `$ConfigPath\<file>` exists and
   `$V2\config\<file>` doesn't, `Copy-Item -LiteralPath` it (a new file inherits `v2\config`'s
   rights). An existing copy is kept (`Kept`). A missing source prints a warning only.
6. If `$V2\config\hx\company.json` is missing, write it (§8.6).
7. If `$V2\config\hx\changes.json` is missing, write it (§8.6).
8. Never touch today's `config\` or `status\` folders apart from reading the two Zebra files.
9. Checks that only warn: if this computer is the file server (the UNC server name is
   `$env:COMPUTERNAME` or starts with it and a dot, or the path is local), find the share holding `$V2` with `Get-SmbShare` and
   warn unless `CachingMode` is `None`; otherwise print "Check on the file server that offline
   caching is off: Get-SmbShare <share> | Select CachingMode". Always print "v2 must stay on this one
   server: don't replicate it with DFS-R."
10. Summary: `icacls "<folder>"` for each folder of §8.4, then the next steps: "Put Stripes.exe on
    the share. Then an administrator opens MapleJet > Admin > Company to set the time server,
    countries and inks."
11. Exit code 0 when done (also with `-WhatIf`). Any error: print the step, the item and the error,
    and exit 1. Steps already done stay done; running again finishes the job.

### 8.4 Folders and their explicit entries

Notation: `icacls` style (storage §14.2). "Modify pair" = `(RX,WD,AD)` on the folder itself plus
`(OI)(CI)(IO)(M)` for everything inside. "Add-files triple" = `(RX,WD,AD)` on the folder itself,
`(CI)(IO)(RX,WD)` for subfolders (the month folders) and `(OI)(IO)(R)` for files. "Users", "Senders"
and "Admins" are the three groups.

| Folder (under `\\SERVER\Share\Stripes\`) | Inheritance from the parent | Explicit entries |
|---|---|---|
| `v2` | removed (protected, inherited entries not copied) | `SYSTEM:(OI)(CI)(F)`, `BUILTIN\Administrators:(OI)(CI)(F)`, `Users:(OI)(CI)(RX)`, `Senders:(OI)(CI)(RX)`, `Admins:(OI)(CI)(RX)` |
| `v2\config` | on | Admins: Modify pair |
| `v2\config\hx`, `…\hx\lines`, `…\hx\machines`, `…\hx\printers`, `…\hx\adjustments`, `…\hx\calibrations` | on | none |
| `v2\templates` | on | Admins: Modify pair |
| `v2\templates\products`, `…\films`, `…\print-texts` | on | none |
| `v2\status` | on | none |
| `v2\status\machines`, `v2\status\pollers`, `v2\status\deployment` | on | Users, Senders, Admins: Modify pair each |
| `v2\status\lines`, `v2\status\locks`, `v2\status\alerts` | on | Senders, Admins: Modify pair each |
| `v2\logs` | on | none |
| `v2\logs\hx-sends` | on | Senders: add-files triple; `*S-1-3-4:(OI)(CI)(IO)(R)` |
| `v2\logs\hx-events` | on | Users, Senders, Admins: add-files triple each; `*S-1-3-4:(OI)(CI)(IO)(R)` |
| `v2\logs\hx-admin` | on | Admins: add-files triple; `*S-1-3-4:(OI)(CI)(IO)(R)` |

That is 23 folders. Every "Read" cell of the README table comes from the `v2` entries, inherited.
Every "Modify" or "Read and add files" cell has its own explicit entry, so the result is right even
when `-UsersGroup` is narrower than Domain Users. There is no CREATOR OWNER entry anywhere in `v2`.

### 8.5 Setting the rights

1. For each folder: `$item = Get-Item -LiteralPath $path`; `$acl =
   $item.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)` (the
   Access section only, so owner and audit settings are never touched, which also works over SMB).
   Keep `$before = $acl.GetSecurityDescriptorSddlForm('Access')` and the list of its explicit
   (non-inherited) rules.
2. `v2`: `$acl.SetAccessRuleProtection($true, $false)`. Every other folder:
   `$acl.SetAccessRuleProtection($false, $true)` (inheritance on, even if someone turned it off).
3. Remove **every** explicit rule (`RemoveAccessRuleSpecific` for each), then add the folder's set
   with `New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $rights, $inheritance,
   $propagation, 'Allow')`, always with `SecurityIdentifier` objects, never names. .NET merges rules
   for the same SID, so two parameters naming the same group still give one canonical result.
4. If `GetSecurityDescriptorSddlForm('Access')` now differs from `$before`,
   `$item.SetAccessControl($acl)` inside `ShouldProcess` and print `Rights set`; otherwise `Rights
   unchanged`. Each explicit rule of the old list whose SID, rights, inheritance and propagation
   aren't in the new list is printed as `Removed entry`. Windows passes inheritable entries down to
   existing files and folders.

The `icacls` notation maps to .NET like this:

| icacls | `FileSystemRights` | `InheritanceFlags` | `PropagationFlags` |
|---|---|---|---|
| `(OI)(CI)(F)` | `FullControl` | `ContainerInherit, ObjectInherit` | `None` |
| `(OI)(CI)(RX)` | `ReadAndExecute` | `ContainerInherit, ObjectInherit` | `None` |
| `(RX,WD,AD)` | `ReadAndExecute, CreateFiles, CreateDirectories` | `None` | `None` |
| `(OI)(CI)(IO)(M)` | `Modify` | `ContainerInherit, ObjectInherit` | `InheritOnly` |
| `(CI)(IO)(RX,WD)` | `ReadAndExecute, CreateFiles` | `ContainerInherit` | `InheritOnly` |
| `(OI)(IO)(R)` | `Read` | `ObjectInherit` | `InheritOnly` |
| `*S-1-3-4:(OI)(CI)(IO)(R)` | `Read` | `ContainerInherit, ObjectInherit` | `InheritOnly` |

### 8.6 The two seed files

Each is written only if missing, with `[System.IO.File]::Open($path, 'CreateNew', 'Write')` (so an
administrator's save that lands at the same moment is never overwritten), as UTF-8 without a BOM
(`New-Object System.Text.UTF8Encoding($false)`), from a text template, not `ConvertTo-Json`. Each
string value is escaped with `ConvertTo-Json -InputObject $value -Compress`. Each time is
`[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0`, written with
`.ToString("0.0##", [Globalization.CultureInfo]::InvariantCulture)`, so it always has a decimal
point and reads as a float. `<account>` is `[System.Security.Principal.WindowsIdentity]::GetCurrent().Name`
(`EXAMPLE\itadmin` style, like `Identity.user`).

`v2\config\hx\company.json` (storage §4.1; the `hx_model.Company` defaults, field order kept):

```
{
 "format_version": 1,
 "file_type": "company",
 "id": "company",
 "revision": 1,
 "updated_by": <account>,
 "updated_at": <now>,
 "time_server": "",
 "time_server_fallback": "",
 "countries": [],
 "inks": [],
 "lot_min_digits": 4,
 "lot_max_digits": 8,
 "free_text_max": 30,
 "admin_group": <AdminGroup as given>,
 "admin_group_sid": <its SID>,
 "sender_group": <SenderGroup as given>,
 "sender_group_sid": <its SID>,
 "standard_fonts": ["CourierN", "Arial", "ArialNarrow", "ArialNarrowB", "TimesNewRom", "LinearBeam"],
 "bag_counter": "PMCNT"
}
```

`v2\config\hx\changes.json` (storage §4.2):

```
{
 "format_version": 1,
 "file_type": "changes",
 "counter": 1,
 "written_at": <now>,
 "written_by": <account>,
 "last": null
}
```

If `hx_model`'s `Company` defaults or `FORMAT` change, the template changes with them; the test in
§8.8 catches a mismatch.

### 8.7 What it never does

It never deletes or renames a file or folder, never changes the share's SMB permissions or settings,
never writes anything outside `v2`, never stores a password, and never touches the registry or
Group Policy.

### 8.8 test_folder_script.py

`skipUnless(sys.platform == "win32")`, and skipped if `tools/New-StripesV2Folders.ps1` or
`powershell.exe` is missing. Each test makes `<tmp>\Stripes\config` holding copies of
`examples/config/printers.json` and `stock.json`, and runs `powershell -NoProfile -ExecutionPolicy
Bypass -File tools\New-StripesV2Folders.ps1 -ConfigPath <tmp>\Stripes\config -AdminGroup
"BUILTIN\Backup Operators" -SenderGroup BUILTIN\Users -UsersGroup "NT AUTHORITY\Authenticated
Users"` (built-in groups with three different SIDs, so it runs on a CI runner, which is in English),
with a 120 s timeout.

| Test | Expectation |
|---|---|
| `test_creates_everything` | exit code 0; all 23 folders exist; `v2\config\printers.json` and `stock.json` are byte-equal to the sources; `company.json` passes `hx_share.decode_json(raw, file_type="company", rel=...)` and `hx_model.from_json(Company, …)` equals `Company(id="company", revision=1, updated_by=<it>, updated_at=<it>, admin_group="BUILTIN\\Backup Operators", admin_group_sid="S-1-5-32-551", sender_group="BUILTIN\\Users", sender_group_sid="S-1-5-32-545")` (so every other field is the `hx_model` default); `updated_at` is a float; `changes.json` has `counter` 1 and `last` null; neither file starts with a BOM |
| `test_rerun_changes_nothing` | a second run exits 0; the two JSON files are byte-identical; every folder prints `Rights unchanged` |
| `test_rights` | `icacls v2` shows no inherited entry; `icacls v2\logs\hx-admin` shows an `OWNER RIGHTS` (or `S-1-3-4`) entry; an extra explicit entry added to `v2\status\locks` before a re-run is gone after it, and the output names it |
| `test_whatif_changes_nothing` | on a fresh temp tree, `-WhatIf` exits 0 and creates no `v2` |
| `test_missing_group_stops` | `-AdminGroup "No Such Group 1109"` exits 1, names the group and creates no `v2` |
| `test_existing_files_kept` | an existing `v2\config\printers.json` and `company.json` are left byte-identical |

## 9. Test-PC instructions (spec; delivered after development)

### 9.1 Delivery

`docs/maplejet/test-pc.md`, written after development against the finished screens, linked from the
README's "Trying a new version on one PC first". Plain steps for IT and an administrator, each with
what they should see. It ends with a test record table (step, result pass or fail, notes, who, date)
for the release being tried. It uses only the public example names.

### 9.2 What it must cover

Preconditions:

1. The two groups exist; the tester is in both and has signed out and in since.
2. The folder script has run; its summary looks like §8.4.
3. A release has been made in GitHub Actions with the `STRIPES_HX_KEY` secret. The share still has
   the old `Stripes.exe`.
4. The test unit is on the printer segment with a new password; the test PC reaches it on port 80
   and the time server on UDP 123.

Steps on the test PC, in this order:

1. **Install side by side**: copy the release's `Stripes.exe` to
   `%LocalAppData%\Programs\Stripes-test\`, Unblock it, check Properties > Digital Signatures shows
   the signer, and start it (if AppLocker or WDAC allows only the Group Policy folder, allow the
   signed publisher). Pass: the header shows `Version vX.Y.Z` with no "(development key)".
2. **Zebra unchanged**: the printer list comes from `v2\config`; the user's Zebra settings came
   across; a test label prints; the Zebra printer monitor works; Ctrl+P prints on the Zebra tab
   only.
3. **Deployment**: Manage printers > Deployment status lists this PC with the new version (its file
   is in `v2\status\deployment`), and still lists the other PCs from the old folder.
4. **Rights, three people**: signed in as a user in neither group, the MapleJet tab shows the
   monitor, and sending and the admin screens are refused; as a sender, Send is offered and admin
   is refused; as an administrator, admin works. In Explorer, a plain user can't change, rename or
   delete a file in `v2\logs\hx-events`, and can't create one in `v2\config`.
5. **Administrator set-up**: company settings (time server and fallback, countries, inks), the test
   unit's printer (address, UID, password), a line, a machine, a product, a film and a print text.
   Then on a second PC (or after restarting Stripes), the printer still logs in: the scrambled token
   works for another copy of the same release.
6. **Test print** on the test unit with empty film, end to end, including the end procedure.
7. **Send**: countdown, the result window with the printer's pictures, "Everything is right"; the
   line goes green; the send log is in `v2\logs\hx-sends`.
8. **Sounds and alerts**: cause an alert (for example Stop printing at the printer's own button):
   the alert sound is heard on the test PC (speakers on, volume up), and acknowledging stops it.
   OPEN item 3 in §11.
9. **Firewall**: no Windows Firewall prompt appeared at the first Send (SNTP) or at any other
   time. OPEN item 4.
10. **Floor board**: opens on the chosen screen and size; with "open at startup", it opens after
    signing out and in.
11. **Stop, Resume, Set clocks** on the test unit.
12. **Share down**: with the share unreachable for a few minutes (unplug, or block it), the header
    says the server is unreachable, a Send from the local copy finishes amber, and its log entry
    uploads when the share is back.
13. **Tray**: closing the window keeps Stripes in the tray, and the MapleJet monitor opens from the
    tray menu.

Optional, with a second PC on the new release: two PCs sending to the test unit at once (the
second is refused while the first holds the lock); closing Stripes on the PC polling the printer
(the other takes over within about 20 s, with no burst of requests).

After the steps:

1. **Sign-off**: the record table is filled in and kept with the release.
2. **Rollout**: put that same `Stripes.exe` on the share; PCs get it at their next logon; watch
   Deployment status. Delete `Stripes-test` on the test PC.
3. **Rollback**: put the previous `Stripes.exe` back on the share. The `v2` folder stays and the old
   version ignores it. MapleJet printers keep printing their last message, and nobody can send,
   stop or resume from Stripes until the new version returns.

The test-unit engineering checks in the plan's "Still to test" list (fonts, sizes, encoder, units,
stress, soak) are separate; this document only accepts the build on a PC.

## 10. Notes for the other spec files

**architecture.md**

- §10 (`stripes.py`): the three hooks of §1.3 and the `hx_key` import (done: architecture §3 and
  §10.6 item 5).
- §9.2 (`test_i18n.py`): `SOURCES` lists every `hx_*.py` except `hx_fake.py` **and `hx_key.py`**
  (generated; it may exist during a release build); `"Version {version} (development key)"` is in
  `i18n.ES` (done: architecture §9.2 and §10.6 item 4, with this file's tests and files in §1 and §9.2).
- §6.3 `hx_win.self_check()`: `lookup_group_sid("Administrators")` returns None on a Windows whose
  display language isn't English; treat None as acceptable there (the self-check's own lookup in
  §5.2 check 7 uses the user's account, which works in any language). `monitors()` may return `[]`
  on a CI runner; that isn't a failure.

**ui.md**

- `hx_ui.self_check(root)` is exactly §5.2 (ui.md §15.6 defers to it). ui.md's builder writes it,
  using the ui.md names `bmp_to_image`, `PictureView.set_bmp`, `BagText.set`/`get`, `png_preview`,
  `photo_from_png`, `SyncBackground` and `MapleJetTab`.
- Check 8 expects `assets/alert.wav` in exactly ui.md §9.4's format; if §9.4 changes, check 8
  changes with it.
- The self-check builds services with `make_services` and a `MapleJetTab`, destroys the tab, and
  never calls `start_services` or `stop_services` (§5.8). `MapleJetTab.destroy()` must leave nothing
  running.
- No Tcl `clock` command anywhere (§2.2).

**storage.md**

- The `STRIPES_V2`, `STRIPES_V2_ROAMING`, `STRIPES_V2_LOCAL` and `STRIPES_V2_MACHINE` overrides must
  work in frozen runs too; the self-check (§5.8) depends on them. (Only the identity overrides are
  unfrozen-only, architecture §6.3.)
- The folder script meets §14.3 as §8 here details it, and writes `updated_at` and `written_at`
  as floats with a decimal point.

**protocol.md**

- `make_version.py` always writes a 32-byte `KEY`; `DEV_KEY` is 29 bytes, so a release can never
  carry it. `key_id` must stay `sha256(b"stripes-hx-key-id" + key).hexdigest()[:8]`, because
  `make_version.key_id` repeats it (tested).
- The self-check calls `autopilot_request`, `login_request`, `http_exchange`, `check_reply`,
  `decode_preview`, `make_token`, `password_md5`, `scramble`, `unscramble`, `key_id`,
  `is_development_key`, `release_key`, `MAX_AUTOPILOT_REPLY`, `MAX_LOGIN_REPLY` and `PREVIEW_PREFIX`
  with the signatures in protocol.md §1.1; `check_reply("HXUI", "1109")` must return 1109.

**send.md**

- The self-check calls `hx_time.sntp_query("127.0.0.1", port=..., timeout_s=2.0)`,
  `hx_time.local_utc_offset` and `hx_time.NTP_EPOCH_OFFSET` as send.md §1.2 and §9.1 define them.

## 11. OPEN items

| # | Item | What to confirm | Safe default built now |
|---|---|---|---|
| 1 | The key ships in public releases | The owner accepts that anyone who downloads a release from this public repository can extract the key (§3.7) | As planned: the key is in the exe; the protection is the share permissions, the printer segment and new passwords. Alternative for later: stop attaching `Stripes.exe` to public releases and give IT the signed artifact |
| 2 | Execution policy on the file server | Whether an unsigned `.ps1` may run there | Unsigned; README tells IT to read it and `Unblock-File` it. If the policy is `AllSigned`, add the script to the `sign` job's files |
| 3 | Alert sound on floor PCs | The sound is heard (PCs have speakers and volume); the self-check can't play it on a runner | The self-check checks the file and the API; the test PC checks the sound (§9.2 step 8) |
| 4 | Firewall prompt | No Windows Firewall prompt for SNTP (UDP) or printer HTTP on the floor-PC image | Stripes only makes outbound connections and binds loopback only; the test PC checks (§9.2 step 9) |
| 5 | Offline caching and DFS | The share's `CachingMode` is `None` and `v2` isn't replicated | The folder script warns; README tells IT (storage §19) |
| 6 | Printer OPEN items (encoder calibration, bag counter, speed units, beacon) | architecture §11 | None of them affects the build or the self-check |

## 12. Decisions made in this spec

1. **The key is a repository secret** `STRIPES_HX_KEY`, 64 hex characters (32 bytes), because the
   `build` job runs PyInstaller and has no environment.
2. **Only release builds carry it** (a `v*` tag or a release version typed in Run workflow), the
   same condition as "Publish release". Pushes, pull requests and "Sign only" builds get the public
   development key. Fewer copies of the key, and a signed test build says what it is.
3. **A release without the secret fails at "Stamp version"**, before PyInstaller.
4. **A release-shaped version without the key fails the self-check** (`check_release_key`), a second
   guard that also covers release-shaped local builds.
5. **The key travels only through an environment variable**; `make_version.py` prints only its key
   id, and no message ever echoes it.
6. **`make_version.py` removes a stale `hx_key.py`** when it runs without a key or with a bad one.
7. **`stripes.py` imports `hx_key` directly** for `HX_KEYED`, like `stripes_version`, and the header
   says "Version … (development key)" without it (an addition to architecture §10).
8. **No `hiddenimports` change**; `hx_fake` is added to `EXCLUDES`, and the frozen self-check
   asserts it is absent.
9. **The self-check uses loopback fakes it starts itself** (127.0.0.1, port 0), plays no sound,
   writes only to temp folders, and builds the MapleJet tab through `make_services` with temp PC
   folders and no share, without starting or stopping any service. It also covers the UI's own
   picture paths (`PictureView`, `BagText`, the PNG photo previews).
10. **The self-check's identity check looks up the user's own account**, not a named built-in group,
    so it works in any display language.
11. **A 30-minute limit on the `build` job**, so a hang can't hold a runner for hours.
12. **CI stays on `windows-latest` only**; Linux under xvfb is the developers' and agents' run.
13. **`FileDescription`** becomes "Stripes - Zebra labels and MapleJet bag printing"; nothing that
    Group Policy or AppLocker rules use changes.
14. **`build.bat` only gains a warning** that local builds have the development key.
15. **Folder script**: `tools/New-StripesV2Folders.ps1`, not attached to releases; rights set
    through the .NET ACL API with well-known SIDs (language-independent), `icacls` only for the
    summary; each managed folder gets exactly its explicit entries, and others are removed and
    printed.
16. **Explicit entries for every Modify and add-files cell**, plus Read for all three groups on `v2`,
    so the table holds even when `-UsersGroup` is narrower than Domain Users.
17. **All SIDs are resolved before any change**, so a missing group leaves nothing half done.
18. **Seed files are create-new only**, UTF-8 without a BOM, from a template, with times written as
    floats with a decimal point.
19. **The test PC runs the published, signed release** from `%LocalAppData%\Programs\Stripes-test`
    (out of the Group Policy folder's way); exactly that file goes on the share after sign-off, and
    rollback is the previous file on the share.
20. **The key is never changed** except after a loss or leak, with the re-entry procedure of §3.6.
21. **`test_deploy.py` simulates the exe's exclusions** by blocking `_hashlib`, `ssl`, `_ssl`,
    `multiprocessing` and `hx_fake` in a subprocess and importing the whole app, so a dependency on
    an excluded module fails on every platform before PyInstaller runs.
