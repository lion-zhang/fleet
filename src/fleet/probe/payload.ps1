# The Windows twin of payload.sh. Same line protocol, same parser.
#
# A separate file rather than a portable script: cmd.exe and PowerShell share no syntax
# with POSIX sh worth building on, and a lowest-common-denominator probe would collect
# less on both. What must match exactly is the wire format -- `#FLEET v1`, `key=value`,
# section markers, `#END rc=` -- because one parser reads both.
#
# Every block is best-effort. A Windows box that answers at all is worth listing, and a
# missing counter is a missing field rather than a failed probe.
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

function Emit($k, $v) { Write-Output "$k=$v" }

Write-Output "#FLEET v1"
Emit "probe.mode" $(if ($env:FLEET_MODE) { $env:FLEET_MODE } else { "full" })
$light = $env:FLEET_LIGHT -eq '1'
Emit "probe.light" $(if ($light) { 1 } else { 0 })

$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem

Emit "host.hostname" $env:COMPUTERNAME
Emit "host.uname_s" "Windows"
Emit "probe.now" ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
Emit "host.arch" $env:PROCESSOR_ARCHITECTURE
Emit "host.kernel" $os.Version
Emit "host.os" $os.Caption
# MachineGuid is the closest stable analogue of /etc/machine-id: per-install, survives
# renames and address changes, which is the whole point of preferring it to an address.
Emit "host.machine_id" (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid
# The id fleet gave this machine if it is a clone of another (see payload.sh).
$fleetId = Join-Path $env:LOCALAPPDATA 'fleet\device-id'
if (Test-Path -LiteralPath $fleetId) { Emit "host.device_id" ((Get-Content -LiteralPath $fleetId -TotalCount 1) -replace '[\r\n|]', '').Trim() }
if ($os.LastBootUpTime) {
    Emit "host.uptime_s" ([int]((Get-Date) - $os.LastBootUpTime).TotalSeconds)
}
Emit "host.users" (@(quser 2>$null | Select-Object -Skip 1).Count)
Emit "host.is_container" 0
Emit "host.slurm" 0

Emit "cpu.cores" $cs.NumberOfLogicalProcessors
Emit "cpu.model" (Get-CimInstance Win32_Processor | Select-Object -First 1 -Expand Name)
# No load average on Windows. Report instantaneous utilisation as a one-minute stand-in
# rather than omitting it: cpu_pct divides load by cores, so the shape must match.
$busy = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
if ($null -ne $busy) {
    # In the invariant culture: `-f` writes the machine's own, and on a German or French
    # Windows "1,50" is no number to the parser, so its CPU read "?" for ever.
    $load = ($busy / 100 * $cs.NumberOfLogicalProcessors).ToString('F2', [Globalization.CultureInfo]::InvariantCulture)
    Emit "cpu.load" "$load $load $load"
}

Emit "mem.total_kb" ([int64]$os.TotalVisibleMemorySize)
Emit "mem.avail_kb" ([int64]$os.FreePhysicalMemory)

# Before the first section: a `key=value` line after one is read as a row of it, which
# is how gpu.present and gpu.driver used to be lost on every Windows machine.
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    # A broken driver answers on stdout ("NVIDIA-SMI has failed because it couldn't
    # communicate with the NVIDIA driver"), as on Linux: taken for a driver version, the
    # machine read as having no GPU and nothing said why. payload.sh does the same.
    $driver = "" + (& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null |
                    Select-Object -First 1)
    $driver = ($driver -replace '[\r\n|]', ' ').Trim()
    if ($driver -match '^[0-9]') {
        Emit "gpu.present" 1
        Emit "gpu.driver" $driver
    } else {
        if (-not $driver) {
            $driver = ("" + (& nvidia-smi 2>&1 | Where-Object { "$_".Trim() } | Select-Object -First 1)) -replace '[\r\n|]', ' '
        }
        Emit "gpu.present" "err"
        Emit "gpu.error" $driver.Trim()
        $smi = $null
    }
} else {
    Emit "gpu.present" 0
}

# What this machine says about itself (see payload.sh): the machine's file, then the
# user's, base64 so YAML cannot break a line. And the `paths:` block of both, read here
# with the same three shapes payload.sh reads.
$notesFiles = @((Join-Path $env:ProgramData 'fleet\machine.yaml'), (Join-Path $env:LOCALAPPDATA 'fleet\machine.yaml'))
$keys = @('notes.system', 'notes.user')
$storePaths = New-Object System.Collections.Generic.List[object]
$declared = New-Object System.Collections.Generic.List[object]
for ($i = 0; $i -lt 2; $i++) {
    $f = $notesFiles[$i]
    if (-not (Test-Path -LiteralPath $f)) { continue }
    $bytes = [System.IO.File]::ReadAllBytes($f)
    if ($bytes.Length -gt 8193) { $bytes = $bytes[0..8192] }   # one past the limit: see payload.sh
    Emit $keys[$i] ([Convert]::ToBase64String($bytes))
    $inPaths = $false; $inEp = $false; $kind = ''
    foreach ($ln in ([System.Text.Encoding]::UTF8.GetString($bytes) -split "`r?`n")) {
        if ($ln -match '^[^\s#]') {
            $inPaths = $ln -match '^paths:\s*(#.*)?$'; $inEp = $ln -match '^endpoints:\s*(#.*)?$'; $kind = ''; continue
        }
        # Model servers the notes declare: name: PORT or PORT/base/path (see payload.sh).
        if ($inEp -and $ln -match '^\s+[A-Za-z0-9_-]+:\s*["'']?(\d+)(/[A-Za-z0-9._~/-]*)?["'']?\s*(#.*)?$') {
            if (-not ($declared | Where-Object { $_[0] -eq [int]$Matches[1] })) { $declared.Add(@([int]$Matches[1], "$($Matches[2])")) }
            continue
        }
        if (-not $inPaths -or $ln -match '^\s*(#.*)?$') { continue }
        $vals = @()
        if ($ln -match '^\s*-\s+(.*)$') { $vals = @($Matches[1]) }
        elseif ($ln -match '^\s+([A-Za-z0-9_-]+):\s*(.*)$') {
            $kind = $Matches[1]; $v = $Matches[2]
            if ($v -match '^\[(.*)\]') { $vals = $Matches[1] -split ',' } elseif ($v) { $vals = @($v) }
        }
        foreach ($v in $vals) {
            $v = ($v -replace '\s+#.*$', '').Trim().Trim('"', "'")
            if ($v -eq '~') { $v = $HOME } elseif ($v.StartsWith('~/') -or $v.StartsWith('~\')) { $v = Join-Path $HOME $v.Substring(2) }
            if ($kind -and $v) { $storePaths.Add(@($kind, $v, $i)) }
        }
    }
}
# The user's file over the machine's, kind by kind, as the parser lays them (see payload.sh).
$userKinds = @($storePaths | Where-Object { $_[2] -eq 1 } | ForEach-Object { $_[0] })
$storePaths = @($storePaths | Where-Object { $_[2] -eq 1 -or $userKinds -notcontains $_[0] })

# What `fleet wake` needs once this machine is asleep (see payload.sh): each physical
# card's hardware address, IPv4 subnet and whether it wakes on a magic packet, and the
# router's hardware address.
Write-Output "#NIC name|mac|ipv4|prefix|wol"
try {
    foreach ($a in @(Get-NetAdapter -Physical -ErrorAction Stop)) {
        $ip = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
        $pm = Get-NetAdapterPowerManagement -Name $a.Name -ErrorAction SilentlyContinue
        $wol = if (-not $pm) { '' } elseif ("$($pm.WakeOnMagicPacket)" -eq 'Enabled') { 1 } else { 0 }
        Write-Output ("{0}|{1}|{2}|{3}|{4}" -f ($a.Name -replace '[\r\n|]', ''), $a.MacAddress, $ip.IPAddress, $ip.PrefixLength, $wol)
    }
} catch {}
Write-Output "#GATEWAY ip|mac"
try {
    $gw = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | Sort-Object RouteMetric | Select-Object -First 1
    $nb = Get-NetNeighbor -IPAddress $gw.NextHop -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($gw.NextHop) { Write-Output ("{0}|{1}" -f $gw.NextHop, $nb.LinkLayerAddress) }
} catch {}

Write-Output "#DISK mount|total_kb|used_kb|avail_kb|rw"
if ($env:FLEET_DISK_PATHS) {
    # Configured paths win outright, as they do on POSIX: the row is labelled with the
    # path asked about rather than whatever volume happens to hold it, because "819G
    # free on C:" answers a different question from "819G free where my data goes".
    foreach ($path in ($env:FLEET_DISK_PATHS -split '\|' | Where-Object { $_ })) {
        # `~` is this user's home, as in payload.sh; the row keeps the name it was given.
        $q = $path
        if ($q -eq '~') { $q = $HOME } elseif ($q -match '^~[\\/]') { $q = Join-Path $HOME $q.Substring(2) }
        # Not there (mistyped, a drive not mounted): said so, as on POSIX. Dropped, the
        # machine showed no disk at all and nothing said why.
        if (-not (Test-Path -LiteralPath $q)) { Write-Output ("{0}|0|0|0|missing" -f $path); continue }
        $root = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $q).Path)
        $vol = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($root.TrimEnd('\'))'"
        if (-not $vol) { continue }
        $total = [int64]($vol.Size / 1024)
        $free = [int64]($vol.FreeSpace / 1024)
        Write-Output ("{0}|{1}|{2}|{3}|1" -f $path, $total, ($total - $free), $free)
    }
} else {
    Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
        $total = [int64]($_.Size / 1024)
        $free = [int64]($_.FreeSpace / 1024)
        Write-Output ("{0}|{1}|{2}|{3}|1" -f $_.DeviceID, $total, ($total - $free), $free)
    }
}

# nvidia-smi is the same everywhere it exists, so the GPU block is identical to the
# POSIX one and needs no Windows-specific parsing.
if ($smi) {
    Write-Output "#GPU idx|uuid|name|total_mib|used_mib|free_mib|util_pct|temp_c|power_w"
    & nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw `
        --format=csv,noheader,nounits 2>$null | ForEach-Object {
        Write-Output (($_ -split ',\s*') -join '|')
    }
}

if ($smi -and $env:FLEET_MODE -ne 'shared') {
    Write-Output "#GPUPROC gpu_uuid|pid|vram_mib|user|etimes|comm|args"
    & nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>$null | ForEach-Object {
        $f = $_ -split ',\s*'
        if ($f.Count -lt 3 -or -not $f[1]) { return }
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($f[1])"
        $user = ''; $et = ''; $comm = ''; $cmdline = ''
        if ($p) {
            $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner
            if ($o) { $user = $o.User }
            if ($p.CreationDate) { $et = [int]((Get-Date) - $p.CreationDate).TotalSeconds }
            $comm = $p.Name -replace '\.exe$', ''
            $cmdline = ("" + $p.CommandLine) -replace '[\r\n|]', ' '
            if ($cmdline.Length -gt 200) { $cmdline = $cmdline.Substring(0, 200) }
        }
        $mem = $f[2] -replace '[^0-9]', ''
        Write-Output ("{0}|{1}|{2}|{3}|{4}|{5}|{6}" -f $f[0], $f[1], $mem, $user, $et, $comm, $cmdline)
    }
}

# Listening ports, with the program behind each, as payload.sh reports them.
$listen = @()
Get-NetTCPConnection -State Listen | Sort-Object LocalPort -Unique | Select-Object -First 40 | ForEach-Object {
    $comm = (Get-Process -Id $_.OwningProcess).ProcessName
    $listen += ,@($_.LocalAddress, $_.LocalPort, $_.OwningProcess, $comm)
}
Write-Output "#LISTEN proto|addr|port|pid|comm"
foreach ($l in $listen) { Write-Output ("tcp|{0}|{1}|{2}|{3}" -f $l[0], $l[1], $l[2], $l[3]) }

Write-Output "#STORE kind|path|exists|avail_kb|size_kb|sized_at"
foreach ($sp in $storePaths) {
    if (Test-Path -LiteralPath $sp[1]) {
        $root = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $sp[1]).Path).TrimEnd('\')
        $vol = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$root'"
        $av = if ($vol) { [int64]($vol.FreeSpace / 1024) } else { '' }
        Write-Output ("{0}|{1}|1|{2}||" -f $sp[0], $sp[1], $av)
    } else {
        Write-Output ("{0}|{1}|0|||" -f $sp[0], $sp[1])
    }
}

# Versions of the stack, from the names of `.dist-info` folders as on POSIX -- never by
# importing anything.
if (-not $light) {
    Write-Output "#SOFTWARE key|value"
    Write-Output ("scanned_at|" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    if ($smi) {
        $cuda = (& nvidia-smi 2>$null | Select-String 'CUDA Version:\s*([0-9.]+)' | Select-Object -First 1)
        if ($cuda) { Write-Output ("cuda|" + $cuda.Matches[0].Groups[1].Value) }
    }
    if (Get-Command nvcc -ErrorAction SilentlyContinue) {
        $r = (& nvcc --version 2>$null | Select-String 'release ([0-9.]+)' | Select-Object -First 1)
        if ($r) { Write-Output ("nvcc|" + $r.Matches[0].Groups[1].Value) }
    }
    # Not `python`: on a machine without Python that is the Microsoft Store stub.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Write-Output ("python|" + ((& py -3 --version 2>&1) -replace '^Python ', ''))
    }
    if (Get-Command uv -ErrorAction SilentlyContinue) { Write-Output ("uv|" + ((& uv --version 2>$null) -split ' ')[1]) }
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $dv = (& docker --version 2>$null) -replace '^Docker version ([^,]*).*', '$1'
        if ($dv) { Write-Output ("docker|" + $dv) }
    }
    if (Get-Command ollama -ErrorAction SilentlyContinue) { Write-Output ("ollama|" + (Get-Command ollama).Source) }

    $pk = ' torch torchvision torchaudio mlx mlx_lm mlx_vlm omlx vllm sglang transformers accelerate diffusers jax jaxlib tensorflow deepspeed flash_attn xformers triton bitsandbytes peft trl unsloth llama_cpp_python onnxruntime onnxruntime_gpu tensorrt exllamav2 ray '
    $roots = @()
    foreach ($d in @("$HOME\miniconda3", "$HOME\anaconda3", "$HOME\miniforge3", "$HOME\mambaforge", "$env:ProgramData\miniconda3", "$env:ProgramData\anaconda3")) {
        if (Test-Path -LiteralPath $d) { $roots += $d; $roots += @(Get-ChildItem -Directory -Path "$d\envs" | ForEach-Object FullName) }
    }
    $roots += @(Get-ChildItem -Directory -Path "$env:LOCALAPPDATA\Programs\Python\Python3*" | ForEach-Object FullName)
    $roots += @(Get-ChildItem -Directory -Path "$HOME\.venvs\*", "$HOME\venv*", "$HOME\.venv", "$HOME\*\.venv" | ForEach-Object FullName)
    foreach ($sp in $storePaths) { if ($sp[0] -eq 'envs') { $roots += $sp[1] } }
    Write-Output "#PYENV env|python|package|version"
    $n = 0
    foreach ($root in ($roots | Select-Object -Unique -First 40)) {
        $site = Join-Path $root 'Lib\site-packages'
        if (-not (Test-Path -LiteralPath $site)) { continue }
        $py = ''
        $cfg = Join-Path $root 'pyvenv.cfg'
        if (Test-Path -LiteralPath $cfg) {
            $m = Select-String -LiteralPath $cfg -Pattern '^version(_info)?\s*=\s*([0-9]+\.[0-9]+)' | Select-Object -First 1
            if ($m) { $py = $m.Matches[0].Groups[2].Value }
        } elseif ((Split-Path $root -Leaf) -match '^Python(\d)(\d+)$') { $py = "$($Matches[1]).$($Matches[2])" }
        $label = if ($root.StartsWith($HOME)) { '~' + $root.Substring($HOME.Length) } else { $root }
        Get-ChildItem -Directory -LiteralPath $site -Filter '*.dist-info' | ForEach-Object {
            if ($_.Name -match '^(.+)-([^-]+)\.dist-info$') {
                $name = ($Matches[1].ToLower() -replace '[-.]', '_')
                if ($pk.Contains(" $name ") -and $n -lt 200) { $n++; Write-Output ("{0}|{1}|{2}|{3}" -f $label, $py, $name, $Matches[2]) }
            }
        }
    }

    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $job = Start-Job { docker ps --format '{{.Image}}|{{.Names}}|{{.Status}}|{{.Ports}}' 2>$null }
        if (Wait-Job $job -Timeout 3) {
            Write-Output "#CONTAINER image|name|status|ports"
            Receive-Job $job | Select-Object -First 20
        }
        Remove-Job $job -Force
    }

    # What the model servers here serve (see payload.sh): two seconds each, eight at most.
    $hint = @(8000, 8001, 8010, 8080, 11434, 1234, 5000, 30000)
    $asked = 0
    $llm = @()
    # Declared ones first, so the eight-server limit never drops what someone asked for.
    $todo = New-Object System.Collections.Generic.List[object]
    foreach ($d in $declared) {
        $at = ($listen | Where-Object { [int]$_[1] -eq $d[0] } | Select-Object -First 1)
        $todo.Add(@($(if ($at) { $at[0] } else { '127.0.0.1' }), $d[0], 0, '', $d[1]))
    }
    foreach ($l in $listen) {
        if (($todo | Where-Object { $_[1] -eq [int]$l[1] })) { continue }
        if (-not (($hint -contains [int]$l[1]) -or ($l[3] -match 'vllm|sglang|ollama|mlx|llama|lmstudio|lms|text-gen|omlx|kobold|tabby|aphrodite|litellm|local-ai|exllama'))) { continue }
        $todo.Add(@($l[0], $l[1], $l[2], $l[3], ''))
    }
    foreach ($l in $todo) {
        if ($asked -ge 8) { break }
        $asked++
        $hostname = if ($l[0] -in @('0.0.0.0', '::', '127.0.0.1', '::1')) { '127.0.0.1' } elseif ($l[0] -match ':') { "[$($l[0])]" } else { $l[0] }
        $status = 'down'; $ms = 0; $ids = @()
        $paths = if ($l[4]) { @("$($l[4])/models") } else { @('/v1/models', '/api/tags') }
        foreach ($path in $paths) {
            $t = Get-Date
            try {
                $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://${hostname}:$($l[1])$path"
                $ms = [int]((Get-Date) - $t).TotalMilliseconds
                # Bytes when the server sends no Content-Type; text when it does.
                $txt = if ($r.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($r.Content) } else { $r.Content }
                $j = $txt | ConvertFrom-Json
                if ($j.data) { $ids = @($j.data | ForEach-Object { $_.id }) } elseif ($j.models) { $ids = @($j.models | ForEach-Object { $_.name }) }
                $status = if ($ids) { 'ok' } else { 'none' }
                if ($ids) { break }
            } catch {
                $ms = [int]((Get-Date) - $t).TotalMilliseconds
                $code = $_.Exception.Response.StatusCode.value__
                if ($code -in @(401, 403)) { $status = 'auth'; break }
                elseif ($code) { $status = 'none' }
            }
        }
        if ($ids) { foreach ($id in ($ids | Select-Object -First 20)) { $llm += ("{0}|ok|{1}|{2}" -f $l[1], $ms, ($id -replace '[\r\n|]', '')) } }
        else { $llm += ("{0}|{1}|{2}|" -f $l[1], $status, $ms) }
    }
    Write-Output "#LLMMODEL port|status|ms|model"
    $llm | ForEach-Object { Write-Output $_ }
}

Write-Output "#END rc=0"
