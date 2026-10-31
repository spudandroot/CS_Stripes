"""Tests for the v2 folder script, tools/New-StripesV2Folders.ps1 (deploy.md §8.8).

ScriptTextTests read the script as text, on every platform: its folders are the ones hx_share needs,
its rights are the README's table, its two seed files are hx_model's Company and the change counter
that Stripes writes, it names only well-known SIDs and example names, and it uses nothing that
Windows PowerShell 5.1 lacks.

PowerShellTests need a PowerShell (Windows PowerShell or PowerShell 7, so CI runs them in both): the
parser finds no error, the script's own functions build the right entries and seed files, and the
script runs every step but the permissions, with stand-ins for the Windows account calls.

WindowsTests run the whole script for real, permissions included, on Windows only (CI runs them on
windows-latest), with built-in groups, so no domain is needed.

Run: python -m unittest test_folder_script -v
"""

import dataclasses
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import hx_model
import hx_share
import stripes
from hx_model import Company

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tools", "New-StripesV2Folders.ps1")
EXAMPLES = os.path.join(HERE, "examples", "config")
RUN_TIMEOUT_S = 120
WELL_KNOWN_SIDS = {"S-1-5-18", "S-1-5-32-544", "S-1-3-4"}    # SYSTEM, BUILTIN\Administrators, OWNER RIGHTS
GROUPS = ("Users", "Senders", "Admins")
# The values the script fills in; every other field of the seed files is a fixed default
COMPANY_FILLED = {"updated_by": "ACCOUNT", "updated_at": "NOW", "admin_group": "ADMIN_GROUP",
                  "admin_group_sid": "ADMIN_SID", "sender_group": "SENDER_GROUP", "sender_group_sid": "SENDER_SID"}
CHANGES_FILLED = {"written_at": "NOW", "written_by": "ACCOUNT"}
# icacls notation -> FileSystemRights, InheritanceFlags, PropagationFlags (deploy.md §8.5)
ENTRY_PARTS = {
    "(OI)(CI)(F)": ("FullControl", "ContainerInherit, ObjectInherit", "None"),
    "(OI)(CI)(RX)": ("ReadAndExecute", "ContainerInherit, ObjectInherit", "None"),
    "(RX,WD,AD)": ("ReadAndExecute, CreateFiles, CreateDirectories", "None", "None"),
    "(OI)(CI)(IO)(M)": ("Modify", "ContainerInherit, ObjectInherit", "InheritOnly"),
    "(CI)(IO)(RX,WD)": ("ReadAndExecute, CreateFiles", "ContainerInherit", "InheritOnly"),
    "(OI)(IO)(R)": ("Read", "ObjectInherit", "InheritOnly"),
    "(OI)(CI)(IO)(R)": ("Read", "ContainerInherit, ObjectInherit", "InheritOnly"),
}
# icacls rights as access masks, and .NET's InheritanceFlags and PropagationFlags values
ICACLS_RIGHTS = {"F": 0x1F01FF, "M": 0x1301BF, "RX": 0x1200A9, "R": 0x120089, "WD": 0x2, "AD": 0x4}
SYNCHRONIZE = 0x100000                  # .NET adds it to every Allow rule, as icacls does
CONTAINER_INHERIT, OBJECT_INHERIT, INHERIT_ONLY = 1, 2, 2
MODIFY_PAIR = ("(RX,WD,AD)", "(OI)(CI)(IO)(M)")
ADD_FILES_TRIPLE = ("(RX,WD,AD)", "(CI)(IO)(RX,WD)", "(OI)(IO)(R)")
OWNER_RIGHTS_READ = ("S-1-3-4", "(OI)(CI)(IO)(R)")
# Example SIDs (storage.md §4.1) for the stand-ins
EXAMPLE_SIDS = {"Admins": "S-1-5-21-1000000001-1000000002-1000000003-2101",
                "Senders": "S-1-5-21-1000000001-1000000002-1000000003-2102",
                "Users": "S-1-5-21-1000000001-1000000002-1000000003-513"}


# --------------------------------------------------------------------------
# Reading the script
# --------------------------------------------------------------------------

def script_text():
    with open(SCRIPT, "rb") as f:
        return f.read().decode("utf-8").replace("\r\n", "\n")


def function_text(text, name):
    """The text of one top-level function, up to the next one."""
    m = re.search(rf"^function {re.escape(name)}\b.*?(?=^function |^# -{{20,}}|\Z)", text, re.S | re.M)
    if not m:
        raise AssertionError(f"the script has no function {name}")
    return m.group(0)


def here_string(text):
    """The first single-quoted here-string in text."""
    m = re.search(r"@'\n(.*?)\n'@", text, re.S)
    if not m:
        raise AssertionError("no here-string")
    return m.group(1)


def folder_table(text):
    """The script's folder table: [(folder with /, Modify groups, AddFiles groups)], in its order."""
    rows = []
    for line in here_string(function_text(text, "Get-FolderPlan")).splitlines():
        if line.strip():
            rel, modify, add = (cell.strip() for cell in line.split("|"))
            rows.append((rel.replace("\\", "/"), tuple(modify.split()), tuple(add.split())))
    return rows


def fill_template(template, values):
    """A seed-file template with its @NAME@ placeholders replaced by JSON text."""
    def one(m):
        return values[m.group(1)]
    return re.sub(r"@([A-Z_]+)@", one, template)


def ordered_json(raw):
    """[(key, value)] of a JSON object, in file order; a key twice fails."""
    def pairs(items):
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise AssertionError(f"a key appears twice: {keys}")
        return items
    return json.loads(raw, object_pairs_hook=pairs)


def code_only(text):
    """The script without its comments and with every string emptied to '', so syntax checks only
    look at code."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("<#", i):
            end = text.find("#>", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
        elif c == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c == "@" and text[i + 1:i + 2] in ("'", '"'):
            m = re.compile("\n" + re.escape(text[i + 1]) + "@").search(text, i + 2)
            i = n if not m else m.end()
            out.append("''")
        elif c == "'":
            j = i + 1
            while j < n and not (text[j] == "'" and text[j + 1:j + 2] != "'"):
                j += 2 if text[j] == "'" else 1
            i = j + 1
            out.append("''")
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "`" else 1
            i = j + 1
            out.append("''")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def table_rights(path, header_start):
    """{folder under v2 with /: {group: right}} from a markdown rights table (README, storage.md §14.1).
    "" is v2 itself. Only the folders before any "(" in the first cell count."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(header_start)), None)
    if start is None:
        raise AssertionError(f"no rights table in {os.path.basename(path)}")
    header = [cell.strip() for cell in lines[start].strip("|").split("|")]
    columns = {"Domain Users": "Users", "Stripes MapleJet Senders": "Senders", "Stripes Administrators": "Admins"}
    groups = [columns.get(h) for h in header]
    rights = {}
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        for name in re.findall(r"`([^`]+)`", cells[0].split("(")[0]):
            rel = name.replace("\\", "/").strip("/")
            rel = "" if rel == "v2" else rel.removeprefix("v2/")
            rights[rel] = {g: cells[i] for i, g in enumerate(groups) if g}
    return rights


def expected_plan(rights):
    """{folder: (Modify groups, AddFiles groups)} for each folder a rights table names."""
    plan = {}
    for rel, cells in rights.items():
        if rel == "":
            continue
        unknown = {right for right in cells.values()} - {"Read", "Modify", "Read and add files"}
        if unknown:
            raise AssertionError(f"{rel}: unknown right {unknown}")
        plan[rel] = (tuple(g for g in GROUPS if cells[g] == "Modify"),
                     tuple(g for g in GROUPS if cells[g] == "Read and add files"))
    return plan


def expected_entries(rel, modify, add, sids):
    """The explicit entries a folder must get, as sorted (sid, icacls) pairs (deploy.md §8.4)."""
    entries = []
    if rel == "":
        entries += [("S-1-5-18", "(OI)(CI)(F)"), ("S-1-5-32-544", "(OI)(CI)(F)")]
        entries += [(sids[g], "(OI)(CI)(RX)") for g in GROUPS]
    entries += [(sids[g], e) for g in modify for e in MODIFY_PAIR]
    entries += [(sids[g], e) for g in add for e in ADD_FILES_TRIPLE]
    if add:
        entries.append(OWNER_RIGHTS_READ)
    return sorted(entries)


def icacls_meaning(entry):
    """(access mask, InheritanceFlags, PropagationFlags) of an icacls entry like (OI)(CI)(IO)(M)."""
    parts = re.findall(r"\(([^)]*)\)", entry)
    rights = 0
    for name in parts[-1].split(","):
        rights |= ICACLS_RIGHTS[name]
    flags = set(parts[:-1])
    inheritance = (CONTAINER_INHERIT if "CI" in flags else 0) | (OBJECT_INHERIT if "OI" in flags else 0)
    return rights | SYNCHRONIZE, inheritance, INHERIT_ONLY if "IO" in flags else 0


def shown(rel):
    """How the script names a folder in its output: v2, v2\\config\\hx."""
    return "v2" + "".join("\\" + part for part in rel.split("/") if part)


def seed_record(raw, file_type):
    """A seed file's dict, read the way Stripes reads it."""
    return hx_share.decode_json(raw, file_type=file_type, rel=f"config/hx/{file_type}.json")


def changes_written():
    """The changes.json dict that DataService writes on a save (storage.md §4.2), envelope first."""
    written = {}

    def write_json(rel, data):
        written.update(json.loads(hx_share.encode_json(data["file_type"], data)))
    stub = types.SimpleNamespace(
        store=types.SimpleNamespace(read_bytes=lambda rel: None, write_json=write_json),
        clock=types.SimpleNamespace(time=lambda: 1_793_275_200.0),
        identity=types.SimpleNamespace(user="EXAMPLE\\jsmith"))
    hx_share.DataService._bump_changes(stub, "film", "fm_3f9a1c22b0", 4)
    return written


def changes_counter(raw):
    """What DataService makes of a changes.json: its counter, or None."""
    stub = types.SimpleNamespace(store=types.SimpleNamespace(read_bytes=lambda rel: raw))
    return hx_share.DataService._changes_counter(stub)


# --------------------------------------------------------------------------
# Running PowerShell
# --------------------------------------------------------------------------

def powershells():
    """[(name, path)] of the PowerShells here: Windows PowerShell (powershell) and PowerShell 7 (pwsh)."""
    found = []
    for name in ("powershell", "pwsh"):
        path = shutil.which(name)
        if path:
            found.append((name, path))
    return found


def run_powershell(exe, script, *args, timeout=RUN_TIMEOUT_S):
    """Runs a .ps1 file; (exit code, stdout and stderr together, without colour codes)."""
    env = dict(os.environ, POWERSHELL_TELEMETRY_OPTOUT="1", POWERSHELL_UPDATECHECK="Off", NO_COLOR="1")
    try:
        done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script,
                               *args], capture_output=True, timeout=timeout, env=env, cwd=HERE)
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or b"").decode("utf-8", "replace") + (e.stderr or b"").decode("utf-8", "replace")
        raise AssertionError(f"{os.path.basename(script)} didn't finish in {timeout} s:\n{output}") from None
    output = done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")
    return done.returncode, re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output).replace("\r\n", "\n")


def remove_tree(path):
    """Removes a test tree, after giving its folders their inherited rights back (v2 inherits none)."""
    if sys.platform == "win32" and os.path.isdir(path):
        try:
            subprocess.run(["icacls", path, "/reset", "/T", "/C", "/Q"], capture_output=True, timeout=RUN_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            pass
    shutil.rmtree(path, ignore_errors=True)


def inheriting_temp_folder(prefix):
    """A new folder in the temp folder whose rights are inherited the usual way, marked (I), as a server's
    Stripes folder's are. tempfile.mkdtemp's folder won't do on Windows: Python 3.12.4 and later make it
    (mode 0o700) with a protected list of its own, SYSTEM, Administrators and OWNER RIGHTS full control,
    not marked as inheriting automatically, and Windows gives every folder made inside it copies of those
    entries as entries of its own: the CI runner showed v2, made by os.makedirs, with no (I) entry."""
    base = tempfile.gettempdir()
    while True:
        path = os.path.join(base, f"{prefix}{secrets.token_hex(6)}")
        try:
            os.mkdir(path)      # the default mode: no list of its own, only what the temp folder passes down
        except FileExistsError:
            continue
        break
    if sys.platform == "win32":
        # The temp folder may pass nothing down: on the CI runner a folder made in it got the account's
        # default list (SYSTEM, Administrators and the user, full control) as entries of its own, not (I).
        # So give this folder inheritable entries, as a server's Stripes folder has, and check that a folder
        # made inside really inherits them, or every (I) check below would test the runner, not the script
        user = f"{os.environ.get('USERDOMAIN')}\\{os.environ.get('USERNAME')}" if os.environ.get("USERDOMAIN") \
            else os.environ.get("USERNAME", "")
        done = subprocess.run(["icacls", path, "/grant", "*S-1-5-18:(OI)(CI)(F)", "*S-1-5-32-544:(OI)(CI)(F)",
                               f"{user}:(OI)(CI)(F)"], capture_output=True, timeout=RUN_TIMEOUT_S)
        probe = os.path.join(path, "probe")
        os.mkdir(probe)
        listing = subprocess.run(["icacls", probe], capture_output=True, timeout=RUN_TIMEOUT_S)
        os.rmdir(probe)
        if done.returncode != 0 or b"(I)" not in listing.stdout:
            raise AssertionError(f"The test folder doesn't pass its rights down: {done.stdout!r} {done.stderr!r}"
                                 f" / {listing.stdout!r}")
    return path


def make_tree(test):
    """<tmp>/Stripes/config with copies of the example Zebra files; returns (config, v2). Removed after. The
    folders inherit their rights the usual way (inheriting_temp_folder)."""
    tmp = inheriting_temp_folder("stripes-v2-")
    test.addCleanup(remove_tree, tmp)
    config = os.path.join(tmp, "Stripes", "config")
    os.makedirs(config)
    for name in ("printers.json", "stock.json"):
        shutil.copyfile(os.path.join(EXAMPLES, name), os.path.join(config, name))
    return config, os.path.join(tmp, "Stripes", "v2")


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def all_folders():
    return [""] + list(hx_share._SHARE_FOLDERS)


def folder_path(v2, rel):
    return os.path.join(v2, *rel.split("/")) if rel else v2


# Test stand-ins put into a copy of the script just before its main block: no Windows accounts and no
# permissions, so every other step runs on any platform
STAND_INS = r"""
# Test stand-ins (test_folder_script.py): example groups, an example account, no permissions
function Find-AccountSid([string]$Name) {
    $known = @{
        'Stripes Administrators'   = 'S-1-5-21-1000000001-1000000002-1000000003-2101'
        'Stripes MapleJet Senders' = 'S-1-5-21-1000000001-1000000002-1000000003-2102'
        'Domain Users'             = 'S-1-5-21-1000000001-1000000002-1000000003-513'
        'Stripes Admins 2'         = 'S-1-5-21-1000000001-1000000002-1000000003-2201'
        'Stripes Senders 2'        = 'S-1-5-21-1000000001-1000000002-1000000003-2202'
    }
    if ($known.ContainsKey($Name)) { return $known[$Name] }
    $null
}
function Get-CurrentAccount { 'EXAMPLE\itadmin' }
function Set-FolderRights {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Path, [string]$Shown, [bool]$Protect, [object[]]$Entries)
    if ($PSCmdlet.ShouldProcess($Shown, 'Set rights')) {
        Write-Host "Rights left alone (test): $Shown"
        if ($Protect) { $script:V2Protected = $true }      # as the real one notes it
    }
}

"""
# The other example groups the stand-ins know, for a re-run with other groups
OTHER_GROUPS = {"Admins": ("Stripes Admins 2", "S-1-5-21-1000000001-1000000002-1000000003-2201"),
                "Senders": ("Stripes Senders 2", "S-1-5-21-1000000001-1000000002-1000000003-2202")}
NOT_PROTECTED = "v2 isn't protected yet: it still has the Stripes folder's rights"
RUN_AGAIN = "What is done stays done. Fix the problem and run the script again to finish."

# Loads the script's functions without running it, and writes what they give into <Out>\out.txt
FUNCTIONS_HARNESS = r"""
param([string]$ScriptPath, [string]$Out)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$tokens, [ref]$errors)
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add(("version`t{0}" -f $PSVersionTable.PSVersion))
foreach ($e in $errors) {
    $lines.Add(("error`t{0}`t{1}" -f $e.Extent.StartLineNumber, $e.Message))
}
# PowerShell 7 operators (Windows PowerShell's parser already fails on them)
$newer = 'QuestionQuestion', 'QuestionQuestionEquals', 'QuestionDot', 'QuestionLBracket', 'AndAnd', 'OrOr',
         'QuestionMark'
foreach ($t in $tokens) {
    if ($newer -contains [string]$t.Kind) {
        $lines.Add(("token`t{0}`t{1}" -f $t.Kind, $t.Extent.StartLineNumber))
    }
}
if (@($errors).Count -eq 0) {
    $found = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
    foreach ($f in $found) {
        . ([scriptblock]::Create($f.Extent.Text))
    }
    $sids = @{
        Admins  = 'S-1-5-21-1000000001-1000000002-1000000003-2101'
        Senders = 'S-1-5-21-1000000001-1000000002-1000000003-2102'
        Users   = 'S-1-5-21-1000000001-1000000002-1000000003-513'
    }
    foreach ($folder in @(Get-V2Folders 'V2ROOT' $sids)) {
        $rules = @($folder.Entries | ForEach-Object { $_.Sid + ' ' + $_.Entry }) -join '|'
        $lines.Add(("folder`t{0}`t{1}`t{2}" -f $folder.Rel, $folder.Protect, $rules))
    }
    foreach ($entry in '(OI)(CI)(F)', '(OI)(CI)(RX)', '(RX,WD,AD)', '(OI)(CI)(IO)(M)', '(CI)(IO)(RX,WD)', '(OI)(IO)(R)',
                       '(OI)(CI)(IO)(R)') {
        $p = Get-EntryParts $entry
        $lines.Add(("parts`t{0}`t{1}`t{2}`t{3}" -f $entry, $p.Rights, $p.Inheritance, $p.Propagation))
        $rights = [int]([System.Security.AccessControl.FileSystemRights]$p.Rights)
        $inheritance = [int]([System.Security.AccessControl.InheritanceFlags]$p.Inheritance)
        $propagation = [int]([System.Security.AccessControl.PropagationFlags]$p.Propagation)
        $lines.Add(("mask`t{0}`t{1}`t{2}`t{3}" -f $entry, $rights, $inheritance, $propagation))
    }
    # A decimal comma must not reach the files
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    try {
        [System.Threading.Thread]::CurrentThread.CurrentCulture = New-Object System.Globalization.CultureInfo 'de-DE'
    } catch { }
    foreach ($s in 1793275200.0, 1793275200.5, 1793275200.123, 0.25) {
        $lines.Add(("time`t{0}`t{1}" -f $s.ToString('R', $invariant), (Format-JsonTime $s)))
    }
    $lines.Add(("now`t{0}" -f (Format-JsonTime (Get-UnixTime))))
    $admin = 'EXAMPLE\Administradores de Stripes ' + [char]0x00F1 + ' ' + [char]0x20AC + " <&'>"
    $sender = 'Stripes "MapleJet" Senders' + [char]9 + 'tab'
    $company = Get-CompanyJson -Account 'EXAMPLE\itadmin' -Now 1793275200 -AdminGroup $admin -AdminSid $sids['Admins'] `
                               -SenderGroup $sender -SenderSid $sids['Senders']
    $changes = Get-ChangesJson 'EXAMPLE\itadmin' 1793275200.5
    $lines.Add(("written`tcompany.json`t{0}" -f (Write-NewFile (Join-Path $Out 'company.json') $company)))
    $lines.Add(("written`tchanges.json`t{0}" -f (Write-NewFile (Join-Path $Out 'changes.json') $changes)))
    $lines.Add(("written`tcompany.json again`t{0}" -f (Write-NewFile (Join-Path $Out 'company.json') 'other')))
}
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines((Join-Path $Out 'out.txt'), $lines.ToArray(), $utf8)
"""
HARNESS_ADMIN_GROUP = "EXAMPLE\\Administradores de Stripes \u00f1 \u20ac <&'>"
HARNESS_SENDER_GROUP = 'Stripes "MapleJet" Senders\ttab'


def stubbed_script(folder, extra="", name="stubbed.ps1"):
    """A copy of the script with the test stand-ins, then the extra ones (which win), in folder."""
    text = script_text()
    m = re.search(r"^# Main\n# -+\n", text, re.M)
    if not m:
        raise AssertionError("the script has no main block")
    path = os.path.join(folder, name)
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(text[:m.end()] + STAND_INS + extra + text[m.end():])
    return path


def warnings_in(output):
    """The script's Warning lines about company.json (not the share checks', which depend on the PC)."""
    return [line for line in output.splitlines() if line.startswith("Warning: ") and "company.json" in line]


# --------------------------------------------------------------------------
# The script as text (every platform)
# --------------------------------------------------------------------------

class ScriptTextTests(unittest.TestCase):
    def setUp(self):
        self.text = script_text()
        self.code = code_only(self.text)

    def test_folders_are_the_ones_stripes_needs(self):
        rels = [rel for rel, _, _ in folder_table(self.text)]
        self.assertEqual(len(rels), len(set(rels)), "a folder is listed twice")
        self.assertEqual(set(rels), set(hx_share._SHARE_FOLDERS))
        seen = {""}
        for rel in rels:                    # top-down: a folder after its parent
            self.assertIn(rel.rpartition("/")[0], seen, rel)
            seen.add(rel)
        m = re.search(r"Creates the (\d+) folders", self.text)
        self.assertIsNotNone(m, "the help says how many folders it creates")
        self.assertEqual(int(m.group(1)), len(all_folders()))

    def test_code_uses_only_these_folders(self):
        # Every share folder the code reads or writes is one the script makes (Stripes makes only the
        # film folders and the log month folders, below these)
        folders = set(hx_share._SHARE_FOLDERS)
        ids = {hx_model.Line: "ln_1a2b3c4d5e", hx_model.Machine: "mc_5d1e2f3a4b", hx_model.Printer: "pr_9c8b7a6d5e",
               hx_model.Calibration: "mc_5d1e2f3a4b", hx_model.Product: "pd_7e6f5a4b3c", hx_model.Film: "fm_3f9a1c22b0",
               hx_model.PrintText: "pt_0b1c2d3e4f", hx_model.MachineFilmAdjustment: "mc_5d1e2f3a4b__fm_3f9a1c22b0",
               Company: "company"}
        self.assertEqual(set(ids), set(hx_share.FILE_TYPES))
        for cls, record_id in ids.items():
            rel = hx_share.record_rel_for(cls, record_id)
            folder = rel.rsplit("/", 1)[0]
            if cls is hx_model.Film:
                folder = folder.rsplit("/", 1)[0]        # Stripes makes templates/films/<id> itself
            self.assertIn(folder, folders, rel)
        for kind in hx_model.LOG_KINDS:
            self.assertIn(f"logs/{kind}", folders)
        # Share paths written as text anywhere in the app code
        sources = ["stripes.py"] + sorted(n for n in os.listdir(HERE) if n.startswith("hx_") and n.endswith(".py")
                                          and n not in ("hx_fake.py", "hx_key.py"))
        found = set()
        for name in sources:
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                source = f.read()
            for m in re.finditer(r"""(?<![\w/\\.])f?["']((?:config|templates|status|logs)/[^"'{}]*)""", source):
                # An example id (a film's own folder, in a docstring) is below the folder that holds it
                folder = re.split(r"/(?:ln|mc|pr|pd|fm|pt)_[0-9a-f]{10}\b", m.group(1).rsplit("/", 1)[0])[0]
                found.add((name, folder))
        self.assertTrue(any(folder.startswith("status/") for _, folder in found))
        self.assertEqual(sorted((n, f) for n, f in found if f not in folders), [])
        # The Zebra deployment log (stripes.status_folder) and v2 itself, next to ConfigPath
        tmp = tempfile.mkdtemp(prefix="stripes-v2-")
        self.addCleanup(shutil.rmtree, tmp, True)
        config = os.path.join(tmp, "Stripes", "config")
        env = {k: v for k, v in os.environ.items() if k not in ("STRIPES_V2", "STRIPES_STATUS")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(os.environ, {"STRIPES_CONFIG": config}):
            v2 = os.path.join(tmp, "Stripes", "v2")
            self.assertEqual(hx_share.v2_share_root(config), v2)
            self.assertEqual(hx_share.v2_share_root(config + os.sep), v2)
            deployment = os.path.relpath(stripes.status_folder(), v2).replace(os.sep, "/")
        self.assertIn(deployment, folders)
        self.assertIn("$stripes = [System.IO.Path]::GetDirectoryName($config)", self.text)
        self.assertIn("$v2 = Join-Path $stripes 'v2'", self.text)

    def test_rights_are_the_readme_table(self):
        table = {rel: (modify, add) for rel, modify, add in folder_table(self.text)}
        for path, header in ((os.path.join(HERE, "README.md"), "| Folder under `v2\\` |"),
                             (os.path.join(HERE, "docs", "maplejet", "storage.md"), "| Folder under `v2\\` |")):
            rights = table_rights(path, header)
            plan = expected_plan(rights)
            self.assertTrue(plan, path)
            for rel in table:
                # A folder the table doesn't name has only what it inherits
                self.assertEqual(table[rel], plan.get(rel, ((), ())), f"{rel} in {os.path.basename(path)}")
            for rel in plan:
                self.assertIn(rel, table)
            if "" in rights:                            # v2 itself: Read for everyone
                self.assertEqual(set(rights[""].values()), {"Read"})

    def test_company_template_is_the_company_record(self):
        template = here_string(function_text(self.text, "Get-CompanyJson"))
        lines = template.splitlines()
        self.assertTrue(all(len(re.findall(r"@[A-Z_]+@", line)) <= 1 for line in lines))
        for key, name in COMPANY_FILLED.items():
            self.assertIn(f' "{key}": @{name}@,', lines)
        self.assertEqual(set(re.findall(r"@([A-Z_]+)@", template)), set(COMPANY_FILLED.values()))
        values = {name: json.dumps(f"x-{name}") for name in COMPANY_FILLED.values()}
        values["NOW"] = "1793275200.0"
        raw = fill_template(template, values)
        pairs = ordered_json(raw)
        fields = [f.name for f in dataclasses.fields(Company)]
        self.assertEqual([k for k, _ in pairs], ["format_version", "file_type"] + fields)
        defaults = hx_model.to_json(Company(id="company"))
        got = dict(pairs)
        self.assertEqual(got["format_version"], hx_model.FORMAT)
        self.assertEqual(got["file_type"], hx_share.FILE_TYPES[Company])
        self.assertEqual((got["id"], got["revision"]), ("company", 1))
        for name in fields:
            if name not in COMPANY_FILLED and name not in ("id", "revision"):
                self.assertEqual(got[name], defaults[name], name)
        record = hx_model.from_json(Company, seed_record(raw.encode("utf-8"), "company"))
        self.assertEqual(record, Company(id="company", revision=1, updated_by="x-ACCOUNT", updated_at=1793275200.0,
                                         admin_group="x-ADMIN_GROUP", admin_group_sid="x-ADMIN_SID",
                                         sender_group="x-SENDER_GROUP", sender_group_sid="x-SENDER_SID"))
        # Times always with a decimal point
        self.assertIn("ToString('0.0##', [System.Globalization.CultureInfo]::InvariantCulture)",
                      function_text(self.text, "Format-JsonTime"))
        self.assertIn("ConvertTo-Json -InputObject ([string]$Value) -Compress",
                      function_text(self.text, "Format-JsonString"))

    def test_group_defaults_are_the_company_defaults(self):
        defaults = dict(re.findall(r"\[string\]\$(\w+) = '([^']*)'", self.text.split("#Requires")[0]))
        self.assertEqual(defaults["AdminGroup"], Company(id="company").admin_group)
        self.assertEqual(defaults["SenderGroup"], Company(id="company").sender_group)
        self.assertEqual(defaults["UsersGroup"], "Domain Users")
        for name in ("ConfigPath", "AdminGroup", "SenderGroup", "UsersGroup", "WhatIf"):
            self.assertRegex(self.text, rf"\n\.PARAMETER {name}\n")
        for section in (".SYNOPSIS", ".DESCRIPTION"):
            self.assertIn(f"\n{section}\n", self.text)
        self.assertEqual(len(re.findall(r"\n\.EXAMPLE\n", self.text)), 2)
        self.assertIn("-WhatIf", self.text.split("\n.EXAMPLE\n")[1])

    def test_changes_template_is_what_stripes_writes(self):
        template = here_string(function_text(self.text, "Get-ChangesJson"))
        self.assertEqual(set(re.findall(r"@([A-Z_]+)@", template)), set(CHANGES_FILLED.values()))
        for key, name in CHANGES_FILLED.items():
            self.assertRegex(template, rf'\n "{key}": @{name}@,?\n')
        raw = fill_template(template, {"NOW": "1793275200.0", "ACCOUNT": json.dumps("EXAMPLE\\itadmin")})
        pairs = ordered_json(raw)
        self.assertEqual([k for k, _ in pairs], list(changes_written()))
        d = seed_record(raw.encode("utf-8"), "changes")
        self.assertEqual((d["counter"], d["last"], d["written_at"]), (1, None, 1793275200.0))
        self.assertEqual(changes_counter(raw.encode("utf-8")), 1)

    def test_only_well_known_sids_and_example_names(self):
        self.assertEqual(set(re.findall(r"S-1-\d+(?:-\d+)+", self.text)), WELL_KNOWN_SIDS)
        self.assertEqual(set(re.findall(r"\\\\([A-Za-z0-9._-]+)\\", self.text)), {"SERVER"})
        self.assertLessEqual(set(re.findall(r"\b([A-Z][A-Z0-9-]{1,14})\\[A-Za-z]", self.text)),
                             {"SERVER", "EXAMPLE", "DOMAIN", "BUILTIN", "AUTHORITY"})
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", self.text):
            self.assertTrue(ip.startswith(("192.0.2.", "127.")), ip)
        for host in re.findall(r"\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|local|lan|corp|internal|io)\b", self.text):
            self.assertRegex(host.lower(), r"(^|\.)example\.", host)
        self.assertEqual(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", self.text), [])

    def test_runs_in_windows_powershell(self):
        raw = read_bytes(SCRIPT)
        # Windows PowerShell 5.1 reads a file without a BOM in the ANSI code page: ASCII only
        self.assertTrue(all(b < 128 for b in raw), "the script must be ASCII only")
        self.assertIn("Set-StrictMode -Version 2.0", self.text)
        self.assertIn("[CmdletBinding(SupportsShouldProcess = $true)]\nparam(", self.text)
        self.assertIn("$ErrorActionPreference = 'Stop'", self.text)
        # Nothing that only PowerShell 6 or 7 has
        newer = [r"\?", r"&&", r"\|\|", r"-Parallel\b", r"-AsHashtable\b", r"-AdditionalChildPath\b",
                 r"-AsByteStream\b", r"-EscapeHandling\b", r"\butf8NoBOM\b", r"\butf8BOM\b",
                 r"\$Is(?:Windows|Linux|MacOS|CoreCLR)\b", r"\bJoin-String\b", r"\bGet-Error\b", r"\$PSStyle\b",
                 r"\bTest-Json\b", r"\bclean\s*\{", r"-replace\s+[^,\n]+,\s*\{", r"ConvertFrom-Json\s+.*-Depth",
                 r"-NoEnumerate\b", r"Split-Path\s+.*-(?:LeafBase|Extension)\b"]
        for pattern in newer:
            self.assertEqual([m.group(0) for m in re.finditer(pattern, self.code)], [], pattern)
        joins = re.findall(r"Join-Path((?:[ \t]+[^\s)|;]+)+)", self.code)
        self.assertTrue(joins)
        for args in joins:
            parts = args.split()
            self.assertTrue(len(parts) == 2 and not any(p.startswith("-") for p in parts), "Join-Path" + args)
        # It never deletes, renames or moves anything, and never changes the share, the registry or a policy
        for pattern in (r"\bRemove-Item\b", r"\bRename-Item\b", r"\bMove-Item\b", r"\bSet-Acl\b", r"\bSet-SmbShare\b",
                        r"\bSet-ItemProperty\b", r"\bNew-ItemProperty\b", r"\bHKLM:", r"Registry::", r"\.Delete\(",
                        r"::Delete\(", r"::Move\(", r"\.MoveTo\(",
                        r"\bicacls(?:\.exe)?\b[^\n]*/(?:grant|remove|reset)"):
            self.assertEqual(re.findall(pattern, self.code, re.I), [], pattern)

    def test_stand_ins_replace_real_functions(self):
        names = set(re.findall(r"^function ([\w-]+)", self.text, re.M))
        for name in re.findall(r"^function ([\w-]+)", STAND_INS, re.M):
            self.assertIn(name, names)
        for name in ("Get-V2Folders", "Get-EntryParts", "Format-JsonTime", "Get-UnixTime", "Get-CompanyJson",
                     "Get-ChangesJson", "Write-NewFile"):
            self.assertIn(name, names)

    def test_other_groups_only_warn(self):
        # A company.json that is already there keeps its groups: the script reads it and warns, and
        # never writes it (final review B-1)
        check = function_text(self.text, "Test-CompanyGroups")
        code = code_only(check)
        for pattern in (r"\bWrite-NewFile\b", r"\bWrite-SeedFile\b", r"WriteAll", r"\bSet-Content\b", r"\bOut-File\b",
                        r"::Open\(", r"::Create\(", r"\bConvertTo-Json\b", r"\bCopy\("):
            self.assertEqual(re.findall(pattern, code), [], pattern)
        self.assertIn("ConvertFrom-Json -InputObject $text", check)
        # The fields it compares are Company's
        fields = {f.name for f in dataclasses.fields(Company)}
        setup = function_text(self.text, "Invoke-FolderSetup")
        found = re.findall(r"Field = '(\w+)'; Parameter = '(\w+)'", setup)
        self.assertEqual(found, [("admin_group", "AdminGroup"), ("sender_group", "SenderGroup")])
        for field, _ in found:
            self.assertIn(field, fields)
            self.assertIn(field + "_sid", fields)
        self.assertIn("($group.Field + '_sid')", check)
        # Its advice points at what exists: the Company tab and the README paragraph
        joined = re.sub(r"'\s*\+\s*'", "", check)          # 'a ' + 'b' as one string
        self.assertIn("MapleJet > Admin > Company, presses Look up and saves", joined)
        self.assertIn('as the README says under "If a group is replaced"', joined)
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("**If a group is replaced.**", readme)
        self.assertIn("`admin_group_sid`", readme)
        # Checked right after company.json's step, before the rights
        order = [setup.index(s) for s in ("Write-SeedFile -Path $companyPath", "Test-CompanyGroups -Path $companyPath",
                                          "$script:SetupStep = 'setting the rights'")]
        self.assertEqual(order, sorted(order))
        # And reminded once more at the end, where it isn't lost above the icacls listing
        summary = function_text(self.text, "Show-Summary")
        self.assertLess(summary.index("$script:GroupsDiffer"), summary.index("'Done. Put Stripes.exe"))

    def test_failure_says_whether_v2_is_protected(self):
        # A failure before the rights step leaves v2 with the Stripes folder's rights: the error says
        # so (final review B-2). Set-FolderRights notes when v2 has its own
        error = function_text(self.text, "Write-SetupError")
        self.assertIn("v2 isn't protected yet: it still has the Stripes folder's rights", error)
        self.assertIn(RUN_AGAIN, error)
        self.assertLess(error.index("Test-V2Unprotected"), error.index(RUN_AGAIN))
        rights = function_text(self.text, "Set-FolderRights")
        self.assertEqual(rights.count("if ($Protect) { $script:V2Protected = $true }"), 2)
        self.assertLess(rights.index("Set-FolderAccess $Path $wanted"),
                        rights.rindex("if ($Protect) { $script:V2Protected = $true }"))
        check = function_text(self.text, "Test-V2Unprotected")
        self.assertIn("(Get-RuleProtection $v2) -ne $true", check)
        main = self.text[self.text.index("\n# Main\n"):]
        for name in ("SetupV2", "V2Protected", "GroupsDiffer"):
            self.assertRegex(main, rf"\n\$script:{name} = ", name)

    def test_code_only_strips_strings_and_comments(self):
        code = code_only("a # c ?\n<# ? #>b 'x''?' \"y`\"?\" @'\n?\n'@ c\n")
        self.assertNotIn("?", code)
        self.assertEqual(code.split(), ["a", "b", "''", "''", "''", "c"])


# --------------------------------------------------------------------------
# The script in PowerShell, without the Windows-only parts
# --------------------------------------------------------------------------

@unittest.skipUnless(os.path.isfile(SCRIPT), "no folder script")
@unittest.skipUnless(powershells(), "no PowerShell here (powershell or pwsh)")
class PowerShellTests(unittest.TestCase):
    results = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="stripes-ps-")
        harness = os.path.join(cls.tmp, "functions.ps1")
        with open(harness, "w", encoding="ascii", newline="\n") as f:
            f.write(FUNCTIONS_HARNESS)
        cls.results = {}
        for name, exe in powershells():
            out = os.path.join(cls.tmp, name)
            os.makedirs(out)
            code, output = run_powershell(exe, harness, "-ScriptPath", SCRIPT, "-Out", out)
            lines = []
            if os.path.exists(os.path.join(out, "out.txt")):
                with open(os.path.join(out, "out.txt"), encoding="utf-8") as f:
                    lines = [line.split("\t") for line in f.read().splitlines()]
            cls.results[name] = types.SimpleNamespace(code=code, output=output, lines=lines, out=out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def each(self, check):
        """check(result, rows) for what each PowerShell gave; rows(kind) are its lines of that kind."""
        for name, result in self.results.items():
            with self.subTest(powershell=name):
                self.assertEqual(result.code, 0, result.output)
                check(result, lambda kind: [row[1:] for row in result.lines if row[0] == kind])

    def test_no_parse_errors(self):
        def check(result, rows):
            self.assertTrue(rows("version"), result.output)
            self.assertEqual(rows("error"), [])
            self.assertEqual(rows("token"), [])
        self.each(check)

    def test_entries_of_every_folder(self):
        plan = {rel: (modify, add) for rel, modify, add in folder_table(script_text())}

        def check(result, rows):
            got = {row[0].replace("\\", "/"): (row[1], sorted(tuple(e.split(" ", 1)) for e in row[2].split("|") if e))
                   for row in rows("folder")}
            self.assertEqual(list(got), [""] + list(plan))      # v2 first, then the table, top-down
            self.assertEqual(set(got), set(all_folders()))
            for rel in all_folders():
                modify, add = plan.get(rel, ((), ()))
                self.assertEqual(got[rel], ("True" if rel == "" else "False",
                                            expected_entries(rel, modify, add, EXAMPLE_SIDS)), rel)
        self.each(check)

    def test_entry_notation(self):
        def check(result, rows):
            self.assertEqual({row[0]: tuple(row[1:]) for row in rows("parts")}, ENTRY_PARTS)
            # The .NET values are what icacls means by the notation
            masks = {row[0]: tuple(int(v) for v in row[1:]) for row in rows("mask")}
            self.assertEqual(set(masks), set(ENTRY_PARTS))
            for entry, (rights, inheritance, propagation) in masks.items():
                self.assertEqual((rights | SYNCHRONIZE, inheritance, propagation), icacls_meaning(entry), entry)
        self.each(check)

    def test_times_are_floats(self):
        def check(result, rows):
            got = dict(rows("time"))
            self.assertEqual(got, {"1793275200": "1793275200.0", "1793275200.5": "1793275200.5",
                                   "1793275200.123": "1793275200.123", "0.25": "0.25"})
            (now,), = rows("now")
            self.assertRegex(now, r"^\d+\.\d{1,3}$")
            self.assertLess(abs(float(now) - time.time()), 600)
        self.each(check)

    def test_seed_files(self):
        def check(result, rows):
            self.assertEqual(rows("written"), [["company.json", "True"], ["changes.json", "True"],
                                               ["company.json again", "False"]])
            raw = read_bytes(os.path.join(result.out, "company.json"))
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(raw.endswith(b"}\n"))
            self.assertNotIn(b"\r", raw)
            d = seed_record(raw, "company")
            self.assertIsInstance(d["updated_at"], float)
            self.assertEqual(hx_model.from_json(Company, d), Company(
                id="company", revision=1, updated_by="EXAMPLE\\itadmin", updated_at=1793275200.0,
                admin_group=HARNESS_ADMIN_GROUP, admin_group_sid=EXAMPLE_SIDS["Admins"],
                sender_group=HARNESS_SENDER_GROUP, sender_group_sid=EXAMPLE_SIDS["Senders"]))
            raw = read_bytes(os.path.join(result.out, "changes.json"))
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            d = seed_record(raw, "changes")
            self.assertEqual((d["counter"], d["last"], d["written_by"], d["written_at"]),
                             (1, None, "EXAMPLE\\itadmin", 1793275200.5))
            self.assertIsInstance(d["written_at"], float)
            self.assertEqual(changes_counter(raw), 1)
        self.each(check)


@unittest.skipUnless(os.path.isfile(SCRIPT), "no folder script")
@unittest.skipUnless(powershells(), "no PowerShell here (powershell or pwsh)")
class StubbedRunTests(unittest.TestCase):
    """The whole script with stand-ins for the groups, the account and the permissions."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stripes-ps-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = stubbed_script(self.tmp)

    def run_script(self, exe, config, *args):
        return run_powershell(exe, self.script, "-ConfigPath", config, *args)

    def test_whatif_changes_nothing(self):
        for name, exe in powershells():
            with self.subTest(powershell=name):
                config, v2 = make_tree(self)
                code, output = self.run_script(exe, config + os.sep, "-WhatIf")
                self.assertEqual(code, 0, output)
                self.assertFalse(os.path.exists(v2), output)
                self.assertIn('"Create folder" on target "v2\\logs\\hx-admin"', output)
                self.assertIn('on target "v2\\config\\hx\\company.json"', output)
                self.assertIn("Nothing was changed", output)

    def test_run_and_run_again(self):
        for name, exe in powershells():
            with self.subTest(powershell=name):
                config, v2 = make_tree(self)
                code, output = self.run_script(exe, config)
                self.assertEqual(code, 0, output)
                for rel in all_folders():
                    self.assertTrue(os.path.isdir(folder_path(v2, rel)), rel)
                    self.assertIn(f"Created: {shown(rel)}\n", output)
                for zebra in ("printers.json", "stock.json"):
                    self.assertEqual(read_bytes(os.path.join(v2, "config", zebra)),
                                     read_bytes(os.path.join(config, zebra)))
                company_raw = read_bytes(os.path.join(v2, "config", "hx", "company.json"))
                changes_raw = read_bytes(os.path.join(v2, "config", "hx", "changes.json"))
                company = hx_model.from_json(Company, seed_record(company_raw, "company"))
                self.assertEqual(company, Company(
                    id="company", revision=1, updated_by="EXAMPLE\\itadmin", updated_at=company.updated_at,
                    admin_group_sid=EXAMPLE_SIDS["Admins"], sender_group_sid=EXAMPLE_SIDS["Senders"]))
                self.assertLess(abs(company.updated_at - time.time()), 600)
                self.assertEqual(changes_counter(changes_raw), 1)
                self.assertIn("Wrote: v2\\config\\hx\\company.json", output)
                self.assertIn("v2 must stay on this one server", output)

                code, output = self.run_script(exe, config)
                self.assertEqual(code, 0, output)
                for rel in all_folders():
                    self.assertIn(f"Exists: {shown(rel)}\n", output)
                self.assertEqual(output.count("Kept: "), 4, output)
                self.assertEqual(read_bytes(os.path.join(v2, "config", "hx", "company.json")), company_raw)
                self.assertEqual(read_bytes(os.path.join(v2, "config", "hx", "changes.json")), changes_raw)

    def test_missing_group_stops(self):
        for name, exe in powershells():
            with self.subTest(powershell=name):
                config, v2 = make_tree(self)
                code, output = self.run_script(exe, config, "-SenderGroup", "No Such Group 1109")
                self.assertEqual(code, 1, output)
                self.assertIn("Group 'No Such Group 1109' wasn't found", output)
                self.assertIn("-SenderGroup", output)
                self.assertFalse(os.path.exists(v2), output)

    def test_missing_config_folder_stops(self):
        for name, exe in powershells():
            with self.subTest(powershell=name):
                config, v2 = make_tree(self)
                code, output = self.run_script(exe, os.path.join(config, "nothing-here"))
                self.assertEqual(code, 1, output)
                self.assertIn("isn't a folder that exists", output)
                self.assertFalse(os.path.exists(v2), output)

    def test_other_groups_warn(self):
        # A re-run with other groups gives the folder rights to them but keeps company.json, which
        # Stripes goes by: it says so (final review B-1)
        old = {"Admins": ("administrators' group", "Stripes Administrators", "AdminGroup"),
               "Senders": ("senders' group", "Stripes MapleJet Senders", "SenderGroup")}

        def expected(who):
            label, old_name, parameter = old[who]
            new_name, new_sid = OTHER_GROUPS[who]
            return (f"Warning: v2\\config\\hx\\company.json has the {label} {old_name} ({EXAMPLE_SIDS[who]}), not "
                    f"{new_name} ({new_sid}) from -{parameter}. Stripes still uses {old_name}, but this script gives "
                    f"the folder rights to {new_name}.")
        admin, sender = OTHER_GROUPS["Admins"][0], OTHER_GROUPS["Senders"][0]
        for name, exe in powershells():
            with self.subTest(powershell=name):
                config, v2 = make_tree(self)
                code, output = self.run_script(exe, config)
                self.assertEqual(code, 0, output)
                self.assertEqual(warnings_in(output), [])
                path = os.path.join(v2, "config", "hx", "company.json")
                before = read_bytes(path)
                code, output = self.run_script(exe, config)          # the same groups: nothing to say
                self.assertEqual(code, 0, output)
                self.assertEqual(warnings_in(output), [])
                for args, changed in ((("-AdminGroup", admin), ["Admins"]),
                                      (("-AdminGroup", admin, "-SenderGroup", sender), ["Admins", "Senders"]),
                                      (("-SenderGroup", sender, "-WhatIf"), ["Senders"])):
                    code, output = self.run_script(exe, config, *args)
                    self.assertEqual(code, 0, output)
                    self.assertEqual(read_bytes(path), before)
                    self.assertIn("Kept: v2\\config\\hx\\company.json (already there)", output)
                    warnings = warnings_in(output)
                    self.assertEqual(warnings[:len(changed)], [expected(who) for who in changed], output)
                    advice, reminder = warnings[len(changed):]
                    self.assertTrue(advice.startswith("Warning: this script never changes company.json. "), advice)
                    for part in ("MapleJet > Admin > Company, presses Look up and saves", "admin_group_sid",
                                 'as the README says under "If a group is replaced"'):
                        self.assertIn(part, advice)
                    self.assertEqual(reminder, "Warning: Stripes still uses the groups in company.json, not the "
                                               "ones given to this run (see above).")
                    # The reminder comes last, below the rights, just before the closing line
                    whatif = "-WhatIf" in args
                    end = "Nothing was changed (-WhatIf)" if whatif else "Done. Put Stripes.exe"
                    rights = '"Set rights" on target' if whatif else "Rights left alone (test)"
                    self.assertLess(output.rindex(rights), output.index(reminder))
                    self.assertLess(output.index(reminder), output.index(end))

    def test_unreadable_company_json_warns(self):
        for name, exe in powershells():
            with self.subTest(powershell=name):
                config, v2 = make_tree(self)
                os.makedirs(os.path.join(v2, "config", "hx"))
                path = os.path.join(v2, "config", "hx", "company.json")
                with open(path, "wb") as f:
                    f.write(b"{not json\n")
                code, output = self.run_script(exe, config)
                self.assertEqual(code, 0, output)
                self.assertEqual(read_bytes(path), b"{not json\n")
                self.assertEqual(warnings_in(output),
                                 ["Warning: couldn't read v2\\config\\hx\\company.json to check its groups."])

    def run_failing(self, exe, config, extra):
        script = self.script
        if extra:
            script = stubbed_script(self.tmp, extra, name="failing.ps1")
        return run_powershell(exe, script, "-ConfigPath", config)

    def test_failure_before_the_rights_says_v2_isnt_protected(self):
        # Until the rights step, v2 has the Stripes folder's rights: a failure says so (final review B-2)
        def file_at(*parts):
            def make(v2):
                os.makedirs(os.path.join(v2, *parts[:-1]), exist_ok=True)
                with open(os.path.join(v2, *parts), "wb"):
                    pass                                # a file where a folder must go
            return make
        cases = [
            (file_at("config", "hx"), "", "Failed while creating the folders (v2\\config\\hx): "),
            (None, "function Get-CurrentAccount { throw 'no account (test)' }\n",
             "Failed while writing the first settings (company.json): no account (test)"),
        ]
        for name, exe in powershells():
            for make, extra, failed in cases:
                with self.subTest(powershell=name, failed=failed):
                    config, v2 = make_tree(self)
                    if make:
                        make(v2)
                    code, output = self.run_failing(exe, config, extra)
                    self.assertEqual(code, 1, output)
                    self.assertIn(failed, output)
                    self.assertTrue(os.path.isdir(v2), output)
                    self.assertNotIn("Rights left alone", output)
                    self.assertIn(NOT_PROTECTED, output)
                    self.assertIn(RUN_AGAIN, output)
                    self.assertLess(output.index(failed), output.index(NOT_PROTECTED))
                    self.assertLess(output.index(NOT_PROTECTED), output.index(RUN_AGAIN))

    def test_failure_with_no_v2_or_after_the_rights_doesnt(self):
        def v2_is_a_file(v2):
            with open(v2, "wb"):
                pass
        cases = [
            (v2_is_a_file, "", "Failed while creating the folders (v2): "),
            (None, "function Test-ShareSettings([string]$V2) { throw 'share check (test)' }\n",
             "Failed while checking the share ("),
        ]
        for name, exe in powershells():
            for make, extra, failed in cases:
                with self.subTest(powershell=name, failed=failed):
                    config, v2 = make_tree(self)
                    if make:
                        make(v2)
                    code, output = self.run_failing(exe, config, extra)
                    self.assertEqual(code, 1, output)
                    self.assertIn(failed, output)
                    self.assertNotIn("isn't protected", output)
                    self.assertIn(RUN_AGAIN, output)


# --------------------------------------------------------------------------
# The real script on Windows (deploy.md §8.8)
# --------------------------------------------------------------------------

def windows_powershell():
    return shutil.which("powershell")


@unittest.skipUnless(sys.platform == "win32", "Windows folder permissions")
@unittest.skipUnless(os.path.isfile(SCRIPT), "no folder script")
@unittest.skipUnless(windows_powershell(), "no powershell.exe")
class WindowsTests(unittest.TestCase):
    # Built-in groups with three different SIDs, so it runs on a CI runner (which is in English)
    GROUP_ARGS = ("-AdminGroup", "BUILTIN\\Backup Operators", "-SenderGroup", "BUILTIN\\Users",
                  "-UsersGroup", "NT AUTHORITY\\Authenticated Users")

    def run_script(self, config, *args, exe=None):
        return run_powershell(exe or windows_powershell(), SCRIPT, "-ConfigPath", config, *(args or self.GROUP_ARGS))

    def icacls(self, path):
        done = subprocess.run(["icacls", path], capture_output=True, timeout=RUN_TIMEOUT_S)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.decode("utf-8", "replace")

    def unchanged(self, output):
        """The folders a run reported as "Rights unchanged"."""
        return set(re.findall(r"^Rights unchanged: (\S+)\s*$", output, re.M))

    def test_creates_everything(self):
        config, v2 = make_tree(self)
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        explicit = {""} | {rel for rel, modify, add in folder_table(script_text()) if modify or add}
        for rel in all_folders():
            self.assertTrue(os.path.isdir(folder_path(v2, rel)), rel)
            if rel in explicit:
                self.assertIn(f"Rights set: {shown(rel)}\n", output)
            else:       # a new folder with no entries of its own already has what it inherits
                self.assertRegex(output, rf"Rights (set|unchanged): {re.escape(shown(rel))}\n")
        for zebra in ("printers.json", "stock.json"):
            self.assertEqual(read_bytes(os.path.join(v2, "config", zebra)), read_bytes(os.path.join(config, zebra)))
        raw = read_bytes(os.path.join(v2, "config", "hx", "company.json"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        d = seed_record(raw, "company")
        self.assertIsInstance(d["updated_at"], float)
        self.assertIn("\\", d["updated_by"])
        company = hx_model.from_json(Company, d)
        self.assertEqual(company, Company(id="company", revision=1, updated_by=d["updated_by"],
                                          updated_at=d["updated_at"], admin_group="BUILTIN\\Backup Operators",
                                          admin_group_sid="S-1-5-32-551", sender_group="BUILTIN\\Users",
                                          sender_group_sid="S-1-5-32-545"))
        raw = read_bytes(os.path.join(v2, "config", "hx", "changes.json"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        d = seed_record(raw, "changes")
        self.assertEqual((d["counter"], d["last"]), (1, None))
        self.assertIsInstance(d["written_at"], float)

    def test_rerun_changes_nothing(self):
        config, v2 = make_tree(self)
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        seeds = [os.path.join(v2, "config", "hx", name) for name in ("company.json", "changes.json")]
        before = [read_bytes(path) for path in seeds]
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        self.assertEqual([read_bytes(path) for path in seeds], before)
        self.assertEqual(self.unchanged(output), {shown(rel) for rel in all_folders()}, output)
        self.assertNotIn("Rights set:", output)
        self.assertNotIn("Removed entry", output)

    def test_rights(self):
        config, v2 = make_tree(self)
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        self.assertNotIn("(I)", self.icacls(v2))                 # v2 inherits nothing
        listing = self.icacls(os.path.join(v2, "logs", "hx-admin"))
        self.assertTrue("OWNER RIGHTS" in listing or "S-1-3-4" in listing, listing)
        self.assertNotIn("CREATOR OWNER", self.icacls(os.path.join(v2, "status", "locks")))
        # An entry added by hand is taken out again, and the run says so
        locks = os.path.join(v2, "status", "locks")
        done = subprocess.run(["icacls", locks, "/grant", "*S-1-5-32-546:(OI)(CI)(M)"], capture_output=True,
                              timeout=RUN_TIMEOUT_S)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Guests", self.icacls(locks))
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        removed = [line for line in output.splitlines() if line.startswith("Removed entry:")]
        self.assertEqual(len(removed), 1, output)
        self.assertTrue("Guests" in removed[0] or "S-1-5-32-546" in removed[0], removed)
        self.assertTrue(removed[0].endswith(" on v2\\status\\locks"), removed)
        self.assertIn("Rights set: v2\\status\\locks", output)
        self.assertNotIn("Guests", self.icacls(locks))
        self.assertEqual(self.unchanged(output), {shown(rel) for rel in all_folders()} - {"v2\\status\\locks"})

    def test_whatif_changes_nothing(self):
        config, v2 = make_tree(self)
        code, output = self.run_script(config, *self.GROUP_ARGS, "-WhatIf")
        self.assertEqual(code, 0, output)
        self.assertFalse(os.path.exists(v2), output)
        self.assertIn("What if:", output)

    def test_missing_group_stops(self):
        config, v2 = make_tree(self)
        code, output = self.run_script(config, "-AdminGroup", "No Such Group 1109", *self.GROUP_ARGS[2:])
        self.assertEqual(code, 1, output)
        self.assertIn("No Such Group 1109", output)
        self.assertIn("-AdminGroup", output)
        self.assertFalse(os.path.exists(v2), output)

    def test_existing_files_kept(self):
        config, v2 = make_tree(self)
        os.makedirs(os.path.join(v2, "config", "hx"))
        kept = {os.path.join(v2, "config", "printers.json"): b'{"printers": []}\n',
                os.path.join(v2, "config", "hx", "company.json"): b'{"format_version": 1, "file_type": "company"}\n'}
        for path, data in kept.items():
            with open(path, "wb") as f:
                f.write(data)
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        for path, data in kept.items():
            self.assertEqual(read_bytes(path), data)
        self.assertIn("Kept: v2\\config\\printers.json", output)
        self.assertIn("Kept: v2\\config\\hx\\company.json", output)
        self.assertEqual(read_bytes(os.path.join(v2, "config", "stock.json")),
                         read_bytes(os.path.join(config, "stock.json")))
        self.assertTrue(os.path.isfile(os.path.join(v2, "config", "hx", "changes.json")))

    def test_other_groups_warn(self):
        # A re-run with another administrators' group moves the rights and keeps company.json (B-1)
        config, v2 = make_tree(self)
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        path = os.path.join(v2, "config", "hx", "company.json")
        before = read_bytes(path)
        code, output = self.run_script(config, "-AdminGroup", "BUILTIN\\Guests", *self.GROUP_ARGS[2:])
        self.assertEqual(code, 0, output)
        self.assertEqual(read_bytes(path), before)
        warnings = warnings_in(output)
        self.assertEqual(len(warnings), 3, output)
        self.assertIn("has the administrators' group BUILTIN\\Backup Operators (S-1-5-32-551), not BUILTIN\\Guests "
                      "(S-1-5-32-546) from -AdminGroup.", warnings[0])
        self.assertIn("Rights set: v2\\config\n", output)
        self.assertIn("Guests", self.icacls(os.path.join(v2, "config")))

    def test_failure_says_whether_v2_is_protected(self):
        # Before the rights step v2 still inherits, and the error says so; once it has its own
        # rights, a failure doesn't (B-2)
        config, v2 = make_tree(self)
        os.makedirs(os.path.join(v2, "config"))
        blocker = os.path.join(v2, "config", "hx")
        with open(blocker, "wb"):
            pass
        code, output = self.run_script(config)
        self.assertEqual(code, 1, output)
        self.assertIn("Failed while creating the folders (v2\\config\\hx): ", output)
        self.assertIn(NOT_PROTECTED, output)
        self.assertIn("(I)", self.icacls(v2))
        os.remove(blocker)
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        admin_logs = os.path.join(v2, "logs", "hx-admin")
        os.rmdir(admin_logs)
        with open(admin_logs, "wb"):
            pass
        code, output = self.run_script(config)
        self.assertEqual(code, 1, output)
        self.assertIn("Failed while creating the folders (v2\\logs\\hx-admin): ", output)
        self.assertNotIn("isn't protected", output)
        self.assertIn(RUN_AGAIN, output)

    @unittest.skipUnless(shutil.which("pwsh"), "no PowerShell 7")
    def test_powershell_7_gives_the_same_rights(self):
        config, v2 = make_tree(self)
        code, output = self.run_script(config, *self.GROUP_ARGS, exe=shutil.which("pwsh"))
        self.assertEqual(code, 0, output)
        self.assertIn("Rights set: v2\n", output)
        code, output = self.run_script(config, *self.GROUP_ARGS, exe=shutil.which("pwsh"))
        self.assertEqual(code, 0, output)
        self.assertEqual(self.unchanged(output), {shown(rel) for rel in all_folders()}, output)
        # Windows PowerShell finds nothing to change either
        code, output = self.run_script(config)
        self.assertEqual(code, 0, output)
        self.assertEqual(self.unchanged(output), {shown(rel) for rel in all_folders()}, output)


if __name__ == "__main__":
    unittest.main()
