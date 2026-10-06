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
    $env:FLEET_NO_AUTO_CENTER = '1'
    try { $j = (& $fleet center --json 2>$null) -join "`n" | ConvertFrom-Json } catch { $j = $null }
    Remove-Item Env:FLEET_NO_AUTO_CENTER -ErrorAction SilentlyContinue
    if (-not $j) { return @{ role = ''; fleet_id = ''; center = '' } }
    $role = if ($j.role) { $j.role } elseif ($j.is_center) { 'center' } elseif ($j.member) { 'member' } else { '' }
    return @{ role = $role; fleet_id = "$($j.fleet_id)"; center = "$($j.center)" }
}

# Which core is here already: @{ kind; path }. kind is none, old (fleet-broker), uv
# (agent-fleet as a uv tool: upgradable in place), source (a uv tool from a checkout),
# pipx, or other (some other `fleet` first on PATH).
function Find-FleetCore($uv, $exe) {
    $tools = (& $uv tool list 2>$null) -join "`n"
    if ($tools -match '(?m)^agent-fleet ') {
        $bin = (& $uv tool dir --bin).Trim()
        $receipt = Join-Path (& $uv tool dir).Trim() 'agent-fleet/uv-receipt.toml'
        $kind = if ((Test-Path $receipt) -and ((Get-Content -Raw $receipt) -match '(editable|directory) = ')) { 'source' } else { 'uv' }
        return @{ kind = $kind; path = (Join-Path $bin "fleet$exe") }
    }
    if ($tools -match '(?m)^fleet-broker ') { return @{ kind = 'old'; path = '' } }
    $pipx = (Get-Command pipx -ErrorAction SilentlyContinue).Source
    if ($pipx -and (((& $pipx list --short 2>$null) -join "`n") -match '(?m)^agent-fleet ')) {
        return @{ kind = 'pipx'; path = (Get-Command fleet -ErrorAction SilentlyContinue).Source }
    }
    $other = (Get-Command fleet -ErrorAction SilentlyContinue).Source
    if ($other -and ((& $other --version 2>$null) -match '^fleet ')) { return @{ kind = 'other'; path = $other } }
    return @{ kind = 'none'; path = '' }
}

# Install or upgrade the core with uv. An agent-fleet uv tool is upgraded in place:
# `uv tool upgrade` never downgrades, and fleet's state is never touched.
function Install-FleetCore($uv, $kind) {
    if ($kind -eq 'uv' -and -not $env:FLEET_SOURCE) {
        Write-Host 'core: fleet is installed with uv, upgrading if a newer one exists'
        & $uv tool upgrade --quiet agent-fleet
        if ($LASTEXITCODE -ne 0) { throw 'could not upgrade fleet' }
        return
    }
    $source = if ($env:FLEET_SOURCE) { $env:FLEET_SOURCE } else { 'agent-fleet' }
    # FLEET_FORCE_CORE asked for this copy to take over the `fleet` command from another.
    $force = if ($kind -eq 'force') { @('--force') } else { @() }
    Write-Host 'installing fleet ...'
    if ($env:FLEET_SOURCE) {
        & $uv tool install --upgrade @force --quiet $source
        if ($LASTEXITCODE -ne 0) { throw "could not install $source" }
        return
    }
    & $uv tool install --upgrade @force --quiet $source 2>$null
    if ($LASTEXITCODE -ne 0) {
        # Not on PyPI yet, or PyPI is unreachable from here: straight from the repo.
        Write-Host '  (not on PyPI from here; installing from GitHub)'
        & $uv tool install --force --quiet 'git+https://github.com/lion-zhang/fleet'
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
        Write-Host "core: $(& $fleet --version) installed with pipx, keeping it (to upgrade: pipx upgrade agent-fleet)"
    } elseif ($kind -eq 'other') {
        Write-Host "core: $(& $fleet --version) at $fleet, keeping it"
    } else {
        # Machines from before the rename carry it as `fleet-broker`; two tools must not
        # both claim the `fleet` command. A running fleet.exe holds its own files open,
        # so the background service is stopped first.
        if ($kind -eq 'old') { & $uv tool uninstall fleet-broker 2>&1 | Out-Null }
        if ($exe) {
            Get-Process fleet -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        }
        Install-FleetCore $uv $kind
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
        Write-Host 'next:  fleet show          this machine, as your agents will see it'
        Write-Host '       fleet invite NAME   prints one line to run on another machine to add it'
    } elseif ($setup) {
        Write-Host 'next:  fleet ls            the machines in this fleet'
    } else {
        Write-Host 'next:  fleet setup         start a fleet here and teach your agents (or: fleet join CODE)'
    }
}

Install-Fleet
