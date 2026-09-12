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

$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem

Emit "host.hostname" $env:COMPUTERNAME
Emit "host.uname_s" "Windows"
Emit "host.arch" $env:PROCESSOR_ARCHITECTURE
Emit "host.kernel" $os.Version
Emit "host.os" $os.Caption
# MachineGuid is the closest stable analogue of /etc/machine-id: per-install, survives
# renames and address changes, which is the whole point of preferring it to an address.
Emit "host.machine_id" (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid).MachineGuid
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
    Emit "cpu.load" ("{0:N2} {0:N2} {0:N2}" -f ($busy / 100 * $cs.NumberOfLogicalProcessors))
}

Emit "mem.total_kb" ([int64]$os.TotalVisibleMemorySize)
Emit "mem.avail_kb" ([int64]$os.FreePhysicalMemory)

Write-Output "#DISK mount|total_kb|used_kb|avail_kb|rw"
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    $total = [int64]($_.Size / 1024)
    $free = [int64]($_.FreeSpace / 1024)
    Write-Output ("{0}|{1}|{2}|{3}|1" -f $_.DeviceID, $total, ($total - $free), $free)
}

# nvidia-smi is the same everywhere it exists, so the GPU block is identical to the
# POSIX one and needs no Windows-specific parsing.
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    Emit "gpu.present" 1
    Emit "gpu.driver" (& nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>$null |
                       Select-Object -First 1)
    Write-Output "#GPU idx|uuid|name|total_mib|used_mib|free_mib|util_pct|temp_c|power_w"
    & nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw `
        --format=csv,noheader,nounits 2>$null | ForEach-Object {
        Write-Output (($_ -split ',\s*') -join '|')
    }
} else {
    Emit "gpu.present" 0
}

Write-Output "#END rc=0"
