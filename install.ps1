# Install fleet on Windows: https://github.com/lion-zhang/fleet
#
# Two modes. The center is the one machine that decides who may reach what -- install
# it on the machine you work from. Every other machine is a member.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"
#       center (the default): installs fleet, starts a new fleet here, and teaches every supported
#       agent installed here (Claude Code, Codex, Gemini CLI, ...) to use it.
#
#   $env:FLEET_JOIN='CODE'; irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex
#       member (in PowerShell): installs fleet and joins the fleet whose center printed
#       CODE (`fleet invite` prints this whole line) instead of starting one here.
#
# FLEET_NO_SETUP=1 installs the command only. Safe to run again: it upgrades fleet and
# leaves the fleet this machine is in alone. A fleet already on this machine (uv, pipx,
# a checkout) is kept, never replaced; FLEET_FORCE_CORE=1 installs with uv anyway.
#
# `irm | iex` passes no arguments, which is why the options are environment variables.
# Everything runs inside a function, so a download cut short runs nothing.

# What an installed fleet says this machine is: role (center, member, or empty),
# fleet_id and center. Never starts a fleet to answer. Fleets before `role` existed
# answered with is_center / member, which are read too.
function Get-FleetRole($fleet) {
    # The caller's own setting is put back after: `irm | iex` runs in their session,
    # and removing it outright undid an opt-out they had set themselves.
    $was = $env:FLEET_NO_AUTO_CENTER
    $env:FLEET_NO_AUTO_CENTER = '1'
    try { $j = (& $fleet center --json 2>$null) -join "`n" | ConvertFrom-Json } catch { $j = $null }
    $env:FLEET_NO_AUTO_CENTER = $was
    if (-not $j) { return @{ role = ''; fleet_id = ''; center = '' } }
    $role = if ($j.role) { $j.role } elseif ($j.is_center) { 'center' } elseif ($j.member) { 'member' } else { '' }
    return @{ role = $role; fleet_id = "$($j.fleet_id)"; center = "$($j.center)" }
}

# Which core is here already: @{ kind; path }. kind is none, old (a uv tool under an
# earlier name: fleet-broker, or agent-fleet from git), uv (agents-fleet as a uv tool:
# upgradable in place), source (a uv tool from a checkout), pipx, or other (some other
# `fleet` first on PATH).
function Find-FleetCore($uv, $exe) {
    $tools = (& $uv tool list 2>$null) -join "`n"
    # The package was agent-fleet until PyPI refused that name; it was never released
    # under it, so an agent-fleet tool came from git or a checkout.
    foreach ($name in @('agents-fleet', 'agent-fleet')) {
        if ($tools -notmatch "(?m)^$name ") { continue }
        $bin = (& $uv tool dir --bin).Trim()
        $receipt = Join-Path (& $uv tool dir).Trim() "$name/uv-receipt.toml"
        if ((Test-Path $receipt) -and ((Get-Content -Raw $receipt) -match '(editable|directory) = ')) {
            return @{ kind = 'source'; path = (Join-Path $bin "fleet$exe") }
        }
        if ($name -eq 'agents-fleet') { return @{ kind = 'uv'; path = (Join-Path $bin "fleet$exe") } }
        return @{ kind = 'old'; path = '' }
    }
    if ($tools -match '(?m)^fleet-broker ') { return @{ kind = 'old'; path = '' } }
    $pipx = (Get-Command pipx -ErrorAction SilentlyContinue).Source
    if ($pipx -and (((& $pipx list --short 2>$null) -join "`n") -match '(?m)^agents?-fleet ')) {
        return @{ kind = 'pipx'; path = (Get-Command fleet -ErrorAction SilentlyContinue).Source }
    }
    $other = (Get-Command fleet -ErrorAction SilentlyContinue).Source
    if ($other -and ((& $other --version 2>$null) -match '^fleet ')) { return @{ kind = 'other'; path = $other } }
    return @{ kind = 'none'; path = '' }
}

# Install or upgrade the core with uv. An agents-fleet uv tool is upgraded in place:
# `uv tool upgrade` never downgrades, and fleet's state is never touched.
function Install-FleetCore($uv, $kind) {
    if ($kind -eq 'uv' -and -not $env:FLEET_SOURCE) {
        Write-Host 'core: fleet is installed with uv, upgrading if a newer one exists'
        & $uv tool upgrade --quiet agents-fleet
        if ($LASTEXITCODE -ne 0) { throw 'could not upgrade fleet' }
        return
    }
    $source = if ($env:FLEET_SOURCE) { $env:FLEET_SOURCE } else { 'agents-fleet' }
    # FLEET_FORCE_CORE asked for this copy to take over the `fleet` command from another.
    $force = if ($kind -eq 'force') { @('--force') } else { @() }
    Write-Host 'installing fleet ...'
    if ($env:FLEET_SOURCE) {
        & $uv tool install --upgrade @force --quiet $source
        if ($LASTEXITCODE -ne 0) { throw "could not install $source" }
        return
    }
    $said = (& $uv tool install --upgrade @force --quiet $source 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        # Only when the package is not there to install. Any other failure -- a download
        # that broke, another program already called `fleet` -- used to become a forced
        # install of the repository's main branch over it.
        if ($said -notmatch '(?i)not found in the (package )?registry|no solution found|404') {
            Write-Host $said
            throw 'could not install fleet'
        }
        Write-Host '  (not on PyPI from here; installing from GitHub)'
        & $uv tool install @force --quiet 'git+https://github.com/lion-zhang/fleet'
        if ($LASTEXITCODE -ne 0) { throw 'could not install fleet' }
    }
}

function Install-Fleet {
    # Not 'Stop': Windows PowerShell 5.1 turns any stderr line from a native command into
    # a terminating error under it, and uv reports progress on stderr. Exit codes decide.
    $ErrorActionPreference = 'Continue'
    # Windows is the point; the suffix only lets the same script be tested elsewhere.
    $exe = if ($env:OS -eq 'Windows_NT') { '.exe' } else { '' }
    $join = "$env:FLEET_JOIN"
    $setup = -not $env:FLEET_NO_SETUP
    if ($join -and -not $join.StartsWith('fleet1:')) {
        throw "that is not a fleet invite code -- it starts with fleet1: (run ``fleet invite`` on the center)"
    }

    # Say which mode this run installs, before doing anything. A re-run reads it from the
    # fleet already here, which may have been installed in the other mode.
    $was = ''
    $found = (Get-Command fleet -ErrorAction SilentlyContinue).Source
    if (-not $found) {
        $f = Join-Path $HOME ".local/bin/fleet$exe"
        if (Test-Path $f) { $found = $f }
    }
    if ($found) { $was = (Get-FleetRole $found).role }
    if (-not $setup) { Write-Host 'mode: none -- installing the command only' }
    elseif ($join) { Write-Host 'mode: member -- joining the fleet whose center printed this code' }
    elseif ($was -eq 'center') { Write-Host 'mode: center (already; upgrading)' }
    elseif ($was -eq 'member') { Write-Host 'mode: member (already; upgrading)' }
    else { Write-Host 'mode: center -- starting a new fleet on this machine' }

    # 1. uv, which installs and runs fleet.
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) {
        $local = Join-Path $HOME ".local/bin/uv$exe"
        if (Test-Path $local) { $uv = $local }
    }
    if (-not $uv) {
        Write-Host 'installing uv (https://docs.astral.sh/uv) ...'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null
        $uv = Join-Path $HOME ".local/bin/uv$exe"
        if (-not (Test-Path $uv)) {
            throw 'uv did not install -- see https://docs.astral.sh/uv/getting-started/installation/'
        }
    }

    # 2. The core: the `fleet` command and its state. Installed once per machine and
    #    shared by every agent on it, so installing fleet "for another agent" finds the
    #    one already here and keeps it -- never a second copy beside it, and never a
    #    release over someone's development install.
    $core = Find-FleetCore $uv $exe
    $kind = $core.kind
    if ($env:FLEET_FORCE_CORE -and $kind -in @('source', 'pipx', 'other')) { $kind = 'force' }
    $fleet = $core.path
    if ($kind -eq 'source') {
        Write-Host "core: $(& $fleet --version) -- your source install ($fleet), keeping it"
    } elseif ($kind -eq 'pipx') {
        Write-Host "core: $(& $fleet --version) installed with pipx, keeping it (to upgrade: pipx upgrade agents-fleet)"
    } elseif ($kind -eq 'other') {
        Write-Host "core: $(& $fleet --version) at $fleet, keeping it"
    } else {
        # Machines from before a rename carry it as `fleet-broker` or `agent-fleet`; two
        # tools must not both claim the `fleet` command. A running fleet.exe holds its
        # own files open, so the background service is stopped first.
        if ($kind -eq 'old') {
            & $uv tool uninstall fleet-broker 2>&1 | Out-Null
            & $uv tool uninstall agent-fleet 2>&1 | Out-Null
        }
        # A center's listener is pythonw.exe running from the tool's own folder, not a
        # `fleet` process: stopping only fleet.exe left it holding the files uv replaces,
        # and the upgrade failed half way with "Access is denied". The task is ended, the
        # listener matched by what it runs, and waited for; started again however this
        # ends.
        $restart = $null
        if ($exe -and $fleet -and (Test-Path $fleet)) {
            $restart = $fleet
            try { schtasks /end /tn fleet-center 2>&1 | Out-Null } catch { }
            $listeners = { Get-CimInstance Win32_Process | Where-Object {
                $_.Name -match '^(fleet|pythonw?)\.exe$' -and $_.CommandLine -like '*center*--listen*' } }
            & $listeners | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            $until = (Get-Date).AddSeconds(15)
            while ((Get-Date) -lt $until -and @(& $listeners).Count -gt 0) { Start-Sleep -Milliseconds 400 }
        }
        try {
            Install-FleetCore $uv $kind
        } finally {
            if ($restart) { try { & $restart service start 2>&1 | Out-Null } catch { } }
        }
        & $uv tool update-shell 2>&1 | Out-Null
        $bin = (& $uv tool dir --bin).Trim()
        $fleet = Join-Path $bin "fleet$exe"
        if (-not (Test-Path $fleet)) { throw "fleet installed, but $fleet is missing" }
    }
    $bin = Split-Path $fleet
    $sep = [IO.Path]::PathSeparator
    if (-not (($env:PATH -split $sep) -contains $bin)) { $env:PATH = "$bin$sep$env:PATH" }
    Write-Host "OK $(& $fleet --version)"

    # 3. Into a fleet, and every agent here taught to use it.
    if ($setup) {
        if ($join) {
            $was = $env:FLEET_NO_AUTO_CENTER
            $env:FLEET_NO_AUTO_CENTER = '1'
            & $fleet join $join
            $code = $LASTEXITCODE
            $env:FLEET_NO_AUTO_CENTER = $was
            if ($code -ne 0) { throw 'could not join the fleet (see above)' }
        }
        # On a machine in no fleet this also starts one, with this machine as its center.
        # Exits non-zero when no agent is installed yet, which is no failure of the install.
        & $fleet setup
    }

    Write-Host ''
    Write-Host 'open a new terminal so `fleet` is found everywhere'
    # Center or member: by what this machine is now, not by how this run began.
    $isCenter = $false
    if ($setup) {
        $now = Get-FleetRole $fleet
        $isCenter = $now.role -eq 'center'
        if ($isCenter) { Write-Host "OK this machine is the center of fleet $($now.fleet_id)" }
        elseif ($now.role -eq 'member') {
            Write-Host "OK this machine is a member of fleet $($now.fleet_id); its center is $($now.center)"
        }
    }
    if ($isCenter) {
        Write-Host 'next: add your machines, then ask for what you need. Tell your agent, e.g.:'
        Write-Host '        "add my GPU server: ssh user@host"     a machine you can SSH into'
        Write-Host '        "invite my laptop"                     one line to paste on a machine'
        Write-Host '        "what''s free right now?"               every machine, and what is free'
        Write-Host '        "run train.py where a 24 GB card is free"'
        Write-Host '      or yourself: fleet add "ssh user@host", fleet invite NAME, fleet ls'
    } elseif ($setup) {
        Write-Host 'next:  fleet ls            the machines in this fleet'
    } else {
        Write-Host 'next:  fleet setup         start a fleet here and teach your agents (or: fleet join CODE)'
    }
    if ($setup) { Write-Host "agents load fleet's skill when a session starts: start a new one to use it" }
}

Install-Fleet
