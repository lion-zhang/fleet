# Install fleet on Windows: https://github.com/lion-zhang/fleet
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"
#       installs fleet, makes this machine the center of a new fleet, and teaches every
#       coding agent installed here (Claude Code, Codex, Gemini CLI, ...) to use it.
#
#   $env:FLEET_JOIN='CODE'; irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex
#       (in PowerShell) installs fleet and joins the fleet whose center printed CODE
#       (`fleet invite`) instead of starting one here.
#
# FLEET_NO_SETUP=1 installs the command only. Safe to run again: it upgrades fleet and
# leaves the fleet this machine is in alone.
#
# `irm | iex` passes no arguments, which is why the options are environment variables.
# Everything runs inside a function, so a download cut short runs nothing.

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

    # 2. fleet. Machines from before the rename carry it as `fleet-broker`; two tools
    #    must not both claim the `fleet` command. A running fleet.exe holds its own
    #    files open, so the background service is stopped first.
    if ((& $uv tool list 2>$null) -match '^fleet-broker ') {
        & $uv tool uninstall fleet-broker 2>&1 | Out-Null
    }
    if ($exe) {
        Get-Process fleet -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }

    $source = if ($env:FLEET_SOURCE) { $env:FLEET_SOURCE } else { 'agent-fleet' }
    Write-Host 'installing fleet ...'
    if ($env:FLEET_SOURCE) {
        & $uv tool install --upgrade --quiet $source
        if ($LASTEXITCODE -ne 0) { throw "could not install $source" }
    } else {
        & $uv tool install --upgrade --quiet $source 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        # Not on PyPI yet, or PyPI is unreachable from here: straight from the repo.
        Write-Host '  (not on PyPI from here; installing from GitHub)'
        & $uv tool install --force --quiet 'git+https://github.com/lion-zhang/fleet'
        if ($LASTEXITCODE -ne 0) { throw 'could not install fleet' }
    }
    & $uv tool update-shell 2>&1 | Out-Null
    $bin = (& $uv tool dir --bin).Trim()
    $fleet = Join-Path $bin "fleet$exe"
    if (-not (Test-Path $fleet)) { throw "fleet installed, but $fleet is missing" }
    $sep = [IO.Path]::PathSeparator
    if (-not (($env:PATH -split $sep) -contains $bin)) { $env:PATH = "$bin$sep$env:PATH" }
    Write-Host "OK $(& $fleet --version)"

    # 3. Into a fleet, and every agent here taught to use it.
    if ($setup) {
        if ($join) {
            $env:FLEET_NO_AUTO_CENTER = '1'
            & $fleet join $join
            $code = $LASTEXITCODE
            Remove-Item Env:FLEET_NO_AUTO_CENTER
            if ($code -ne 0) { throw 'could not join the fleet (see above)' }
        }
        # On a machine in no fleet this also starts one, with this machine as its center.
        # Exits non-zero when no agent is installed yet, which is no failure of the install.
        & $fleet setup
    }

    Write-Host ''
    Write-Host 'open a new terminal so `fleet` is found everywhere'
    # Center or member: by what this machine is now, not by how this run began.
    $isCenter = $setup -and ((& $fleet center --json 2>$null) -join '' -match '"is_center": true')
    if ($isCenter) {
        Write-Host 'next:  fleet show          this machine, as your agents will see it'
        Write-Host '       fleet invite NAME   prints one line to run on another machine to add it'
    } elseif ($setup) {
        Write-Host 'next:  fleet ls            the machines in this fleet'
    } else {
        Write-Host 'next:  fleet setup         start a fleet here and teach your agents (or: fleet join CODE)'
    }
}

Install-Fleet
