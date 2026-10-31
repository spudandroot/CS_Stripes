<#
.SYNOPSIS
Creates the v2 folder of Stripes on the file share, with its subfolders and permissions.

.DESCRIPTION
The MapleJet version of Stripes keeps its shared data in a v2 folder next to today's Zebra config
folder: \\SERVER\Share\Stripes\config gives \\SERVER\Share\Stripes\v2. Stripes finds it there by
itself, so no new policy value is needed.

The script:
 1. Checks ConfigPath and finds the three groups. A group that isn't found stops it before any change.
 2. Creates the 23 folders that are missing (v2 and everything in it).
 3. Copies printers.json and stock.json from ConfigPath into v2\config, once. A copy that is already
    there is kept.
 4. Writes the first company settings (v2\config\hx\company.json, with the two groups and their
    IDs) and the change counter (v2\config\hx\changes.json), if they are missing.
 5. Sets the permissions of every folder. v2 doesn't inherit from the Stripes folder. Each folder
    gets exactly its own entries; any other entry added by hand is removed and listed.
 6. Reminds you about offline caching and DFS, and lists the permissions with icacls.

    Folder under v2                                  Users          Senders              Administrators
    config, templates                                Read           Read                 Modify
    status\machines, status\pollers, status\deployment  Modify      Modify               Modify
    status\lines, status\locks, status\alerts        Read           Modify               Modify
    logs\hx-sends                                    Read           Read and add files   Read
    logs\hx-events                                   Read and add files (all three)
    logs\hx-admin                                    Read           Read                 Read and add files

Log files can be added but never changed or deleted. The server's SYSTEM and Administrators have
Full Control everywhere.

Running it again is safe: it never deletes, renames or overwrites anything, and it puts the
permissions back. It never touches today's config or status folders (it only reads the two Zebra
files), the share's own settings, the registry or Group Policy.

Run it as an account with Full Control on the Stripes folder, best a member of the file server's
Administrators group. On the file server itself, run it as administrator.

.PARAMETER ConfigPath
Today's Zebra config folder, exactly as in the ConfigPath policy value, for example
\\SERVER\Share\Stripes\config. A local path on the file server also works. v2 is created next to
it. Never a mapped drive.

.PARAMETER AdminGroup
The administrators' group, as Group or DOMAIN\Group. Default: Stripes Administrators.

.PARAMETER SenderGroup
The senders' group, as Group or DOMAIN\Group. Default: Stripes MapleJet Senders.

.PARAMETER UsersGroup
Everyone who runs Stripes. Default: Domain Users. On a domain that isn't in English, give its own
name for Domain Users.

.PARAMETER WhatIf
Shows every change without making anything, not even the v2 folder.

.EXAMPLE
powershell -NoProfile -File New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config -WhatIf

Shows what the script would create and change, and changes nothing.

.EXAMPLE
powershell -NoProfile -File New-StripesV2Folders.ps1 -ConfigPath \\SERVER\Share\Stripes\config

Creates \\SERVER\Share\Stripes\v2 and sets its permissions, for the groups Stripes Administrators,
Stripes MapleJet Senders and Domain Users.

.NOTES
Works in Windows PowerShell 5.1 and PowerShell 7. Spec: docs/maplejet/deploy.md section 8.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ConfigPath,

    [ValidateNotNullOrEmpty()]
    [string]$AdminGroup = 'Stripes Administrators',

    [ValidateNotNullOrEmpty()]
    [string]$SenderGroup = 'Stripes MapleJet Senders',

    [ValidateNotNullOrEmpty()]
    [string]$UsersGroup = 'Domain Users'
)

#Requires -Version 5.1

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'


# ---------------------------------------------------------------------------------------------
# The folders and their rights (deploy.md 8.4, storage.md 14)
# ---------------------------------------------------------------------------------------------

function Get-FolderPlan {
    # Every folder under v2, top-down. Users, Senders and Admins are the three groups.
    # Modify:   the "Modify pair" for each group named: list, create files and folders here,
    #           and Modify on everything inside.
    # AddFiles: the "add-files triple" for each group named: list and create files and month
    #           folders here, create files in the month folders, read the files. Plus one
    #           OWNER RIGHTS read entry, so nobody can change a log file, not even its creator.
    # A folder with neither has only what it inherits from v2: Read for the three groups.
    #
    # Folder under v2        | Modify               | AddFiles
    $table = @'
config                   | Admins               |
config\hx                |                      |
config\hx\lines          |                      |
config\hx\machines       |                      |
config\hx\printers       |                      |
config\hx\adjustments    |                      |
config\hx\calibrations   |                      |
templates                | Admins               |
templates\products       |                      |
templates\films          |                      |
templates\print-texts    |                      |
status                   |                      |
status\deployment        | Users Senders Admins |
status\machines          | Users Senders Admins |
status\pollers           | Users Senders Admins |
status\lines             | Senders Admins       |
status\locks             | Senders Admins       |
status\alerts            | Senders Admins       |
logs                     |                      |
logs\hx-sends            |                      | Senders
logs\hx-events           |                      | Users Senders Admins
logs\hx-admin            |                      | Admins
'@
    foreach ($line in ($table -split '\r?\n')) {
        if ($line.Trim() -eq '') { continue }
        $cells = $line.Split('|')
        [pscustomobject]@{
            Rel      = $cells[0].Trim()
            Modify   = @($cells[1].Trim() -split '\s+' | Where-Object { $_ -ne '' })
            AddFiles = @($cells[2].Trim() -split '\s+' | Where-Object { $_ -ne '' })
        }
    }
}

function New-Entry([string]$Sid, [string]$Entry) {
    [pscustomobject]@{ Sid = $Sid; Entry = $Entry }
}

function Get-FolderRules($Folder, [hashtable]$Sids) {
    # The explicit entries of one folder, in icacls notation (storage.md 14.2)
    if ($Folder.Rel -eq '') {
        New-Entry 'S-1-5-18' '(OI)(CI)(F)'          # SYSTEM
        New-Entry 'S-1-5-32-544' '(OI)(CI)(F)'      # BUILTIN\Administrators (the server's)
        foreach ($who in 'Users', 'Senders', 'Admins') {
            New-Entry $Sids[$who] '(OI)(CI)(RX)'
        }
    }
    foreach ($who in $Folder.Modify) {
        if (-not $Sids.ContainsKey($who)) { throw "Unknown group '$who' in the folder table" }
        New-Entry $Sids[$who] '(RX,WD,AD)'
        New-Entry $Sids[$who] '(OI)(CI)(IO)(M)'
    }
    foreach ($who in $Folder.AddFiles) {
        if (-not $Sids.ContainsKey($who)) { throw "Unknown group '$who' in the folder table" }
        New-Entry $Sids[$who] '(RX,WD,AD)'
        New-Entry $Sids[$who] '(CI)(IO)(RX,WD)'
        New-Entry $Sids[$who] '(OI)(IO)(R)'
    }
    if (@($Folder.AddFiles).Count -gt 0) {
        New-Entry 'S-1-3-4' '(OI)(CI)(IO)(R)'       # OWNER RIGHTS
    }
}

function Get-EntryParts([string]$Entry) {
    # icacls notation -> FileSystemRights, InheritanceFlags, PropagationFlags (deploy.md 8.5)
    $map = @{
        '(OI)(CI)(F)'     = 'FullControl', 'ContainerInherit, ObjectInherit', 'None'
        '(OI)(CI)(RX)'    = 'ReadAndExecute', 'ContainerInherit, ObjectInherit', 'None'
        '(RX,WD,AD)'      = 'ReadAndExecute, CreateFiles, CreateDirectories', 'None', 'None'
        '(OI)(CI)(IO)(M)' = 'Modify', 'ContainerInherit, ObjectInherit', 'InheritOnly'
        '(CI)(IO)(RX,WD)' = 'ReadAndExecute, CreateFiles', 'ContainerInherit', 'InheritOnly'
        '(OI)(IO)(R)'     = 'Read', 'ObjectInherit', 'InheritOnly'
        '(OI)(CI)(IO)(R)' = 'Read', 'ContainerInherit, ObjectInherit', 'InheritOnly'
    }
    if (-not $map.ContainsKey($Entry)) { throw "Unknown entry $Entry" }
    $parts = $map[$Entry]
    [pscustomobject]@{ Rights = $parts[0]; Inheritance = $parts[1]; Propagation = $parts[2] }
}

function Get-V2Folders([string]$V2, [hashtable]$Sids) {
    $top = [pscustomobject]@{ Rel = ''; Modify = @(); AddFiles = @() }
    foreach ($folder in @($top) + @(Get-FolderPlan)) {
        $path = $V2
        $shown = 'v2'
        if ($folder.Rel -ne '') {
            foreach ($part in $folder.Rel.Split('\')) {
                $path = Join-Path $path $part
                $shown = $shown + '\' + $part
            }
        }
        [pscustomobject]@{
            Rel     = $folder.Rel
            Path    = $path
            Shown   = $shown
            Protect = ($folder.Rel -eq '')
            Entries = @(Get-FolderRules $folder $Sids)
        }
    }
}


# ---------------------------------------------------------------------------------------------
# The two seed files (deploy.md 8.6, storage.md 4.1 and 4.2)
# ---------------------------------------------------------------------------------------------

function Get-CompanyJson([string]$Account, [double]$Now, [string]$AdminGroup, [string]$AdminSid,
                         [string]$SenderGroup, [string]$SenderSid) {
    # The fields of hx_model.Company, in field order, with its defaults
    $template = @'
{
 "format_version": 1,
 "file_type": "company",
 "id": "company",
 "revision": 1,
 "updated_by": @ACCOUNT@,
 "updated_at": @NOW@,
 "time_server": "",
 "time_server_fallback": "",
 "countries": [],
 "inks": [],
 "lot_min_digits": 4,
 "lot_max_digits": 8,
 "free_text_max": 30,
 "admin_group": @ADMIN_GROUP@,
 "admin_group_sid": @ADMIN_SID@,
 "sender_group": @SENDER_GROUP@,
 "sender_group_sid": @SENDER_SID@,
 "standard_fonts": ["CourierN", "Arial", "ArialNarrow", "ArialNarrowB", "TimesNewRom", "LinearBeam"],
 "bag_counter": "PMCNT"
}
'@
    Expand-Template $template @{
        ACCOUNT      = Format-JsonString $Account
        NOW          = Format-JsonTime $Now
        ADMIN_GROUP  = Format-JsonString $AdminGroup
        ADMIN_SID    = Format-JsonString $AdminSid
        SENDER_GROUP = Format-JsonString $SenderGroup
        SENDER_SID   = Format-JsonString $SenderSid
    }
}

function Get-ChangesJson([string]$Account, [double]$Now) {
    # The change counter Stripes bumps on every admin save
    $template = @'
{
 "format_version": 1,
 "file_type": "changes",
 "counter": 1,
 "written_at": @NOW@,
 "written_by": @ACCOUNT@,
 "last": null
}
'@
    Expand-Template $template @{
        ACCOUNT = Format-JsonString $Account
        NOW     = Format-JsonTime $Now
    }
}

function Expand-Template([string]$Template, [hashtable]$Values) {
    # Fills in the one @NAME@ of each line. Always "\n" line ends, and a "\n" at the end
    $lines = foreach ($line in ($Template -split '\r?\n')) {
        $m = [regex]::Match($line, '@([A-Z_]+)@')
        if ($m.Success) {
            $name = $m.Groups[1].Value
            if (-not $Values.ContainsKey($name)) { throw "No value for @$name@" }
            $line.Substring(0, $m.Index) + $Values[$name] + $line.Substring($m.Index + $m.Length)
        } else {
            $line
        }
    }
    ($lines -join "`n") + "`n"
}

function Format-JsonString([string]$Value) {
    # A JSON string. Windows PowerShell escapes more characters than PowerShell 7; both are valid
    ConvertTo-Json -InputObject ([string]$Value) -Compress
}

function Format-JsonTime([double]$Seconds) {
    # Always with a decimal point, so Stripes reads it as a float
    $Seconds.ToString('0.0##', [System.Globalization.CultureInfo]::InvariantCulture)
}

function Get-UnixTime {
    # Now, in seconds since 1970 (UTC), to the millisecond
    $epoch = New-Object System.DateTime -ArgumentList 1970, 1, 1, 0, 0, 0, ([System.DateTimeKind]::Utc)
    [Math]::Floor(([System.DateTime]::UtcNow - $epoch).TotalMilliseconds) / 1000.0
}

function Write-NewFile([string]$Path, [string]$Text) {
    # Writes a new file as UTF-8 without a BOM. Never overwrites: False if the file is already there
    $bytes = (New-Object System.Text.UTF8Encoding -ArgumentList $false).GetBytes($Text)
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
                                         [System.IO.FileShare]::None)
    } catch {
        if (Test-Path -LiteralPath $Path) { return $false }
        throw
    }
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
    } finally {
        $stream.Dispose()
    }
    $true
}


# ---------------------------------------------------------------------------------------------
# Windows accounts and permissions
# ---------------------------------------------------------------------------------------------

function Find-AccountSid([string]$Name) {
    # The SID of an account name, or $null if there is no such account
    $account = New-Object System.Security.Principal.NTAccount -ArgumentList $Name
    try {
        $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        $e = $_.Exception
        while ($null -ne $e) {
            if ($e -is [System.Security.Principal.IdentityNotMappedException]) { return $null }
            $e = $e.InnerException
        }
        throw
    }
}

function Get-AccountName([string]$Sid) {
    try {
        (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $Sid).Translate(
            [System.Security.Principal.NTAccount]).Value
    } catch {
        $Sid
    }
}

function Get-CurrentAccount {
    # EXAMPLE\itadmin, as Stripes writes Identity.user
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function ConvertTo-AccessRule($Rule) {
    $parts = Get-EntryParts $Rule.Entry
    New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
        (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $Rule.Sid),
        [System.Security.AccessControl.FileSystemRights]$parts.Rights,
        [System.Security.AccessControl.InheritanceFlags]$parts.Inheritance,
        [System.Security.AccessControl.PropagationFlags]$parts.Propagation,
        [System.Security.AccessControl.AccessControlType]::Allow)
}

function Get-ExplicitRules($Security) {
    foreach ($rule in $Security.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier])) {
        [pscustomobject]@{
            Key  = '{0}|{1}|{2}|{3}|{4}' -f $rule.IdentityReference.Value, [int]$rule.FileSystemRights,
                   [int]$rule.InheritanceFlags, [int]$rule.PropagationFlags, [int]$rule.AccessControlType
            Rule = $rule
        }
    }
}

function Join-RuleKeys([object[]]$Rules) {
    (@($Rules | ForEach-Object { $_.Key }) | Sort-Object) -join ';'
}

function Format-Rule($Rule) {
    $text = '{0} {1} ({2}; {3})' -f (Get-AccountName $Rule.IdentityReference.Value), $Rule.FileSystemRights,
            $Rule.InheritanceFlags, $Rule.PropagationFlags
    if ([string]$Rule.AccessControlType -ne 'Allow') { $text = 'deny ' + $text }
    $text
}

function Set-FolderAccess([string]$Path, $Security) {
    # Writes the permissions only (never the owner or auditing). Windows passes inheritable
    # entries down to everything inside
    $folder = New-Object System.IO.DirectoryInfo -ArgumentList $Path
    if ($null -ne $folder.PSObject.Methods['SetAccessControl']) {
        $folder.SetAccessControl($Security)                                  # Windows PowerShell 5.1
    } else {
        [System.IO.FileSystemAclExtensions]::SetAccessControl($folder, $Security)   # PowerShell 7
    }
}

function Set-FolderRights {
    # Gives one folder exactly its explicit entries. Inherited entries are left to Windows
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Path, [string]$Shown, [bool]$Protect, [object[]]$Entries)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        if ($WhatIfPreference) {
            [void]$PSCmdlet.ShouldProcess($Shown, 'Set rights')
        } else {
            Write-Host "Skipped: $Shown (not created)"
        }
        return
    }
    $sections = [System.Security.AccessControl.AccessControlSections]::Access
    $current = New-Object System.Security.AccessControl.DirectorySecurity -ArgumentList $Path, $sections
    $wanted = New-Object System.Security.AccessControl.DirectorySecurity
    # v2: no inheritance from the Stripes folder, and its inherited entries aren't kept.
    # Every other folder: inheritance on, even if someone turned it off
    $wanted.SetAccessRuleProtection($Protect, (-not $Protect))
    foreach ($entry in $Entries) {
        $wanted.AddAccessRule((ConvertTo-AccessRule $entry))
    }
    $before = @(Get-ExplicitRules $current)
    $after = @(Get-ExplicitRules $wanted)
    if ($current.AreAccessRulesProtected -eq $Protect -and (Join-RuleKeys $before) -eq (Join-RuleKeys $after)) {
        Write-Host "Rights unchanged: $Shown"
        return
    }
    $afterKeys = @($after | ForEach-Object { $_.Key })
    $removed = @($before | Where-Object { $afterKeys -notcontains $_.Key })
    $action = 'Set rights'
    if ($removed.Count -gt 0) {
        $action = $action + ', removing: ' + ((@($removed | ForEach-Object { Format-Rule $_.Rule })) -join ', ')
    }
    if ($PSCmdlet.ShouldProcess($Shown, $action)) {
        Set-FolderAccess $Path $wanted
        Write-Host "Rights set: $Shown"
        foreach ($old in $removed) {
            Write-Host ('Removed entry: {0} on {1}' -f (Format-Rule $old.Rule), $Shown)
        }
    }
}


# ---------------------------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------------------------

function New-SetupStop([string]$Message) {
    # An expected stop: only its message is printed
    $e = New-Object System.Exception -ArgumentList $Message
    $e.Data['StripesStop'] = $true
    $e
}

function Resolve-ConfigPath([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw (New-SetupStop "ConfigPath '$Path' isn't a folder that exists.")
    }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath.TrimEnd('\', '/')
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($root -match '^[A-Za-z]:\\?$') {
        $drive = New-Object System.IO.DriveInfo -ArgumentList $root
        if ($drive.DriveType -eq [System.IO.DriveType]::Network) {
            throw (New-SetupStop "$Path is on a mapped drive. Use the UNC path, as in the ConfigPath policy value.")
        }
    }
    $parent = $null
    if ($full -ne '') { $parent = [System.IO.Path]::GetDirectoryName($full) }
    if ([string]::IsNullOrEmpty($parent)) {
        throw (New-SetupStop "ConfigPath must be a folder inside the share, like \\SERVER\Share\Stripes\config.")
    }
    $full
}

function Resolve-GroupSid([string]$Name, [string]$Parameter) {
    $sid = Find-AccountSid $Name
    if (-not $sid) {
        throw (New-SetupStop "Group '$Name' wasn't found. Create it, or pass its name with -$Parameter.")
    }
    Write-Host ('Group {0}: {1}' -f $Name, $sid)
    $sid
}

function New-V2Folder {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Path, [string]$Shown)

    if (Test-Path -LiteralPath $Path -PathType Container) {
        Write-Host "Exists: $Shown"
    } elseif ($PSCmdlet.ShouldProcess($Shown, 'Create folder')) {
        [void][System.IO.Directory]::CreateDirectory($Path)
        Write-Host "Created: $Shown"
    }
}

function Copy-ZebraFile {
    # The one-time copy of a Zebra file. A copy that is already there is never replaced
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Source, [string]$Target, [string]$Shown)

    if (Test-Path -LiteralPath $Target) {
        Write-Host "Kept: $Shown (already there)"
    } elseif (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        Write-Host "Warning: there is no $Source to copy to $Shown." -ForegroundColor Yellow
    } elseif ($PSCmdlet.ShouldProcess($Shown, "Copy $Source")) {
        [System.IO.File]::Copy($Source, $Target, $false)
        Write-Host "Copied: $Source to $Shown"
    }
}

function Write-SeedFile {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$Path, [string]$Shown, [string]$Text)

    if (Test-Path -LiteralPath $Path) {
        Write-Host "Kept: $Shown (already there)"
    } elseif ($PSCmdlet.ShouldProcess($Shown, 'Write')) {
        if (Write-NewFile -Path $Path -Text $Text) {
            Write-Host "Wrote: $Shown"
        } else {
            Write-Host "Kept: $Shown (written by someone else just now)"
        }
    }
}

function Test-ShareSettings([string]$V2) {
    # Checks that only warn: offline caching and DFS-R
    $server = ''
    $share = '<share>'
    if ($V2.StartsWith('\\')) {
        $parts = $V2.Substring(2).Split('\')
        $server = $parts[0]
        if ($parts.Count -gt 1) { $share = $parts[1] }
    }
    $here = [string]$env:COMPUTERNAME
    $onServer = ($server -eq '') -or ($here -ne '' -and ($server -eq $here -or $server -like "$here.*")) -or
                ($server -eq 'localhost')
    $check = "Get-SmbShare $share | Select CachingMode"
    $smb = Get-Command -Name Get-SmbShare -ErrorAction SilentlyContinue
    if (-not $onServer -or $null -eq $smb) {
        Write-Host "Check on the file server that offline caching is off: $check"
    } else {
        try {
            if ($server -eq '') {
                $shares = @(Get-SmbShare | Where-Object {
                    $_.Name -notlike '*$' -and $_.Path -and (
                        $V2 -eq $_.Path.TrimEnd('\') -or $V2.StartsWith($_.Path.TrimEnd('\') + '\', 'OrdinalIgnoreCase'))
                })
            } else {
                $shares = @(Get-SmbShare -Name $share)
            }
            if ($shares.Count -eq 0) {
                Write-Host "No share on this computer holds v2. Check on the file server that offline caching is off: $check"
            }
            foreach ($s in $shares) {
                if ([string]$s.CachingMode -eq 'None') {
                    Write-Host "Offline caching is off for share $($s.Name)."
                } else {
                    Write-Host ("Warning: offline caching is {0} for share {1}. Turn it off: Set-SmbShare -Name {1} -CachingMode None" -f
                                $s.CachingMode, $s.Name) -ForegroundColor Yellow
                }
            }
        } catch {
            Write-Host ("Warning: couldn't check offline caching ({0}). Check it: {1}" -f $_.Exception.Message, $check) -ForegroundColor Yellow
        }
    }
    Write-Host "v2 must stay on this one server: don't replicate it with DFS-R."
}

function Show-Summary([object[]]$Folders) {
    # The rights as icacls shows them, then the next steps. Only informs: never stops the script
    $ErrorActionPreference = 'Continue'
    $icacls = Get-Command -Name icacls.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $icacls) {
        Write-Host ''
        Write-Host 'The rights now (icacls):'
        foreach ($folder in $Folders) {
            if (-not (Test-Path -LiteralPath $folder.Path -PathType Container)) { continue }
            try {
                $lines = @(& icacls.exe $folder.Path 2>&1 | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() -ne '' })
                if ($LASTEXITCODE -eq 0 -and $lines.Count -gt 1) {
                    $lines = $lines[0..($lines.Count - 2)]      # without "Successfully processed 1 files"
                }
                foreach ($line in $lines) { Write-Host $line }
            } catch {
                Write-Host "Warning: icacls failed on $($folder.Shown): $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
    }
    Write-Host ''
    if ($WhatIfPreference) {
        Write-Host 'Nothing was changed (-WhatIf). Run it again without -WhatIf to make these changes.'
    } else {
        Write-Host 'Done. Put Stripes.exe on the share. Then an administrator opens MapleJet > Admin > Company to set the time server, countries and inks.'
    }
}

function Invoke-FolderSetup {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$ConfigPath, [string]$AdminGroup, [string]$SenderGroup, [string]$UsersGroup)

    # 1. The folders
    $script:SetupStep = 'checking ConfigPath'
    $script:SetupItem = $ConfigPath
    $config = Resolve-ConfigPath $ConfigPath
    $stripes = [System.IO.Path]::GetDirectoryName($config)
    $v2 = Join-Path $stripes 'v2'
    Write-Host "Stripes folder: $stripes"
    Write-Host "v2 folder:      $v2"

    # 2. Every SID before any change, so a missing group leaves nothing half done
    $script:SetupStep = 'finding the groups'
    $sids = @{}
    foreach ($group in @(('Admins', $AdminGroup, 'AdminGroup'), ('Senders', $SenderGroup, 'SenderGroup'),
                         ('Users', $UsersGroup, 'UsersGroup'))) {
        $script:SetupItem = $group[1]
        $sids[$group[0]] = Resolve-GroupSid $group[1] $group[2]
    }
    $folders = @(Get-V2Folders $v2 $sids)

    # 3. Create what is missing, top-down
    $script:SetupStep = 'creating the folders'
    foreach ($folder in $folders) {
        $script:SetupItem = $folder.Shown
        New-V2Folder -Path $folder.Path -Shown $folder.Shown
    }

    # 4. The one-time Zebra copy
    $script:SetupStep = 'copying the Zebra files'
    $v2config = Join-Path $v2 'config'
    foreach ($name in 'printers.json', 'stock.json') {
        $script:SetupItem = $name
        Copy-ZebraFile -Source (Join-Path $config $name) -Target (Join-Path $v2config $name) -Shown "v2\config\$name"
    }

    # 5. The first company settings and the change counter
    $script:SetupStep = 'writing the first settings'
    $hx = Join-Path $v2config 'hx'
    $account = Get-CurrentAccount
    $now = Get-UnixTime
    $company = @{
        Account     = $account
        Now         = $now
        AdminGroup  = $AdminGroup
        AdminSid    = $sids['Admins']
        SenderGroup = $SenderGroup
        SenderSid   = $sids['Senders']
    }
    $script:SetupItem = 'company.json'
    Write-SeedFile -Path (Join-Path $hx 'company.json') -Shown 'v2\config\hx\company.json' -Text (Get-CompanyJson @company)
    $script:SetupItem = 'changes.json'
    Write-SeedFile -Path (Join-Path $hx 'changes.json') -Shown 'v2\config\hx\changes.json' -Text (Get-ChangesJson $account $now)

    # 6. The rights, top-down, v2 first. Last, so the files above inherit them too
    $script:SetupStep = 'setting the rights'
    foreach ($folder in $folders) {
        $script:SetupItem = $folder.Shown
        Set-FolderRights -Path $folder.Path -Shown $folder.Shown -Protect $folder.Protect -Entries $folder.Entries
    }

    # 7. Checks that only warn, and the summary
    $script:SetupStep = 'checking the share'
    $script:SetupItem = $v2
    Test-ShareSettings $v2
    $script:SetupStep = 'listing the rights'
    Show-Summary $folders
}

function Write-SetupError($ErrorRecord) {
    $e = $ErrorRecord.Exception
    if ($e.Data.Contains('StripesStop')) {
        $Host.UI.WriteErrorLine($e.Message)
        $Host.UI.WriteErrorLine('Nothing was changed.')
    } else {
        $Host.UI.WriteErrorLine(('Failed while {0} ({1}): {2}' -f $script:SetupStep, $script:SetupItem, $e.Message))
        $Host.UI.WriteErrorLine('What is done stays done. Fix the problem and run the script again to finish.')
    }
}


# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------

$script:SetupStep = 'starting'
$script:SetupItem = ''
try {
    Invoke-FolderSetup -ConfigPath $ConfigPath -AdminGroup $AdminGroup -SenderGroup $SenderGroup -UsersGroup $UsersGroup
} catch {
    Write-SetupError $_
    exit 1
}
exit 0
