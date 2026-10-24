# Install fleet on Windows: https://github.com/lion-zhang/fleet
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"
#
# Installs the `fleet` command and teaches every supported agent installed here (Claude
# Code, Codex, Gemini CLI, ...) to use it. Install it where you work, and wherever else
# you run agents; the machines fleet looks at need nothing but SSH.
#
# FLEET_NO_SETUP=1 installs the command only. Safe to run again: it upgrades fleet. A
# fleet already on this machine (uv, pipx, a checkout) is kept, never replaced;
# FLEET_FORCE_CORE=1 installs with uv anyway.
#
# `irm | iex` passes no arguments, which is why the options are environment variables.
# Everything runs inside a function, so a download cut short runs nothing.

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
    # And over a tool from before the rename, which holds the `fleet` command until it
    # is uninstalled -- only once this install has worked.
    $force = if ($kind -eq 'force' -or $kind -eq 'old') { @('--force') } else { @() }
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
    $setup = -not $env:FLEET_NO_SETUP
    if ($env:FLEET_JOIN) {
        # fleet 0.5 joined a fleet with a code. There is no fleet to join any more: every
        # machine running fleet looks at what its own ssh reaches.
        Write-Host 'note: FLEET_JOIN is no longer needed -- fleet has no center to join; ignoring it'
    }

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
        # fleet 0.5 ran a listener on a fleet's center: pythonw.exe running from the
        # tool's own folder, holding the files uv replaces ("Access is denied" half way).
        # 0.6 has no center, so its task is ended and deleted, and the listener waited for.
        if ($exe -and $fleet -and (Test-Path $fleet)) {
            try { schtasks /end /tn fleet-center 2>&1 | Out-Null } catch { }
            try { schtasks /delete /tn fleet-center /f 2>&1 | Out-Null } catch { }
            $listeners = { Get-CimInstance Win32_Process | Where-Object {
                $_.Name -match '^(fleet|pythonw?)\.exe$' -and $_.CommandLine -like '*center*--listen*' } }
            & $listeners | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            $until = (Get-Date).AddSeconds(15)
            while ((Get-Date) -lt $until -and @(& $listeners).Count -gt 0) { Start-Sleep -Milliseconds 400 }
        }
        Install-FleetCore $uv $kind
        # Machines from before a rename carry it as `fleet-broker` or `agent-fleet`;
        # two tools must not both claim the `fleet` command. Removed only once the
        # new one is in -- uninstalled first, a failed install left no fleet at all --
        # and the `fleet` command it takes with it put back.
        if ($kind -eq 'old') {
            & $uv tool uninstall fleet-broker 2>&1 | Out-Null
            & $uv tool uninstall agent-fleet 2>&1 | Out-Null
            $b = ((& $uv tool dir --bin) | Out-String).Trim()
            if (-not (Test-Path (Join-Path $b "fleet$exe"))) { Install-FleetCore $uv 'old' }
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

    # 3. Every agent here taught to use it. Exits non-zero when no agent is installed
    #    yet, which is no failure of the install.
    if ($setup) { & $fleet setup }

    Write-Host ''
    Write-Host 'open a new terminal so `fleet` is found everywhere'
    if ($setup) {
        Write-Host 'next: add your machines, then ask for what you need. Tell your agent, e.g.:'
        Write-Host '        "add my GPU server: ssh user@host"     a machine you can SSH into'
        Write-Host '        "find my machines"                     from your ssh config and tailnet'
        Write-Host '        "what''s free right now?"               every machine, and what is free'
        Write-Host '        "run train.py where a 24 GB card is free"'
        Write-Host '      or yourself: fleet add "ssh user@host", fleet ls --discover, fleet ls'
        Write-Host "agents load fleet's skill when a session starts: start a new one to use it"
    } else {
        Write-Host 'next:  fleet setup         teach your agents to use fleet'
    }
}

Install-Fleet
