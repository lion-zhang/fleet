#!/bin/sh
# fleet probe payload -- POSIX sh, no bashisms, no external deps beyond coreutils/busybox.
# Emits line-oriented key=value with #SECTION markers and a mandatory "#END rc=" sentinel.
# Absence of #END means the probe was truncated or killed -- never a successful parse.
#
# Env in:  FLEET_MODE=full|shared   (shared = polite: no process enumeration)
#          FLEET_LIGHT=1   a quick re-read for `fleet top`: no software scan, no HTTP calls
#          FLEET_REFRESH=1 rebuild the cached software scan and folder sizes now
# Never exits non-zero for "thing not present"; that is normal and reported as empty.

FLEET_MODE=${FLEET_MODE:-full}
OS=$(uname -s 2>/dev/null || echo unknown)
# Numbers as the parser reads them. ssh forwards LANG and LC_*, and under de_DE ps prints
# CPU use as "12,5" -- read as 12, and under 1% a busy process was dropped as idle. Only
# the numeric part changes: names keep the machine's own character set.
[ -n "${LC_ALL:-}" ] && { LANG=$LC_ALL; export LANG; unset LC_ALL; }
LC_NUMERIC=C; export LC_NUMERIC

emit() { printf '%s=%s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }
# A python3 that is really there. On a Mac without the developer tools /usr/bin/python3
# is a stub, and running it puts an "install the command line tools" dialog in front of
# whoever is at that Mac -- a probe must never do that.
have_python() {
  have python3 || return 1
  [ "$OS" = "Darwin" ] && [ "$(command -v python3)" = /usr/bin/python3 ] \
    && ! xcode-select -p >/dev/null 2>&1 && return 1
  return 0
}
# strip newlines/pipes so a value can never break the line protocol
clean() { tr -d '\n\r|' 2>/dev/null | cut -c1-300; }

echo "#FLEET v1"
emit probe.mode "$FLEET_MODE"
emit probe.light "${FLEET_LIGHT:-0}"
emit probe.now "$(date +%s 2>/dev/null)"

# ---------------------------------------------------------------- host identity
hostname_v=$( (hostname 2>/dev/null || uname -n 2>/dev/null || cat /etc/hostname 2>/dev/null) | clean )
emit host.hostname "$hostname_v"
emit host.uname_s "$OS"
emit host.arch "$(uname -m 2>/dev/null | clean)"
emit host.kernel "$(uname -r 2>/dev/null | clean)"

# The same order identity.py reads them in, and the same rule: an empty file is no id.
# Without that, a container whose /etc/machine-id exists but is empty reported no id here
# while fleet on it fell back to dbus's -- and the two never matched.
machine_id=""
if [ -r /etc/machine-id ]; then machine_id=$(cat /etc/machine-id 2>/dev/null | clean); fi
if [ -z "$machine_id" ] && [ -r /var/lib/dbus/machine-id ]; then
  machine_id=$(cat /var/lib/dbus/machine-id 2>/dev/null | clean)
fi
if [ -z "$machine_id" ] && [ "$OS" = "Darwin" ] && have ioreg; then
  machine_id=$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null \
    | awk -F'"' '/IOPlatformUUID/{print $4; exit}' | clean)
fi
emit host.machine_id "$machine_id"
# The id fleet gave this machine when it found it shares its machine-id with another --
# a clone of the same image. Written by the center (or by `fleet join`) into fleet's
# config folder here, and read by fleet on this machine too, so all agree which it is.
# `device-id.HOST` first: clones that share a home share the folder, one file each.
# Where there are such files and none is this host's, the plain one is a copy of one of
# theirs, kept for an older fleet.
device_id=""
for d in "$HOME/.config/fleet" "$HOME/Library/Application Support/fleet"; do
  if [ -r "$d/device-id.$hostname_v" ]; then
    device_id=$(head -n 1 "$d/device-id.$hostname_v" 2>/dev/null | clean); break
  fi
  for f in "$d"/device-id.*; do [ -e "$f" ] && break 2; done
  if [ -r "$d/device-id" ]; then device_id=$(head -n 1 "$d/device-id" 2>/dev/null | clean); break; fi
done
emit host.device_id "$device_id"

os_pretty=""
if [ -r /etc/os-release ]; then
  os_pretty=$(. /etc/os-release 2>/dev/null; printf '%s' "$PRETTY_NAME" | clean)
elif [ "$OS" = "Darwin" ] && have sw_vers; then
  os_pretty="$(sw_vers -productName 2>/dev/null | clean) $(sw_vers -productVersion 2>/dev/null | clean)"
fi
[ -n "$os_pretty" ] || os_pretty="$OS"
emit host.os "$os_pretty"

# uptime in seconds
up=""
if [ -r /proc/uptime ]; then up=$(cut -d. -f1 /proc/uptime 2>/dev/null)
elif have sysctl; then
  bt=$(sysctl -n kern.boottime 2>/dev/null | sed -n 's/^{ *sec *= *\([0-9][0-9]*\).*/\1/p')
  [ -n "$bt" ] && up=$(( $(date +%s) - bt ))
fi
emit host.uptime_s "$up"

# distinct logged-in users -- feeds the "is this a shared box" heuristic
users_n=""
have who && users_n=$(who 2>/dev/null | awk '{print $1}' | sort -u | grep -c . )
emit host.users "$users_n"

# containerisation / rental provider fingerprints
is_container=0
[ -f /.dockerenv ] && is_container=1
[ -f /run/.containerenv ] && is_container=1
if [ "$is_container" = "0" ] && [ -r /proc/1/cgroup ]; then
  grep -qE '(docker|lxc|kubepods|containerd)' /proc/1/cgroup 2>/dev/null && is_container=1
fi
emit host.is_container "$is_container"
# vast.ai's own .bashrc takes the label from ~/.vast_containerlabel, and the instance's
# VAST_CONTAINERLABEL is in its first process's environment, not in an ssh session's.
vl=${VAST_CONTAINERLABEL:-}
for f in /etc/vast_containerlabel "$HOME/.vast_containerlabel"; do
  [ -z "$vl" ] && [ -r "$f" ] && vl=$(head -1 "$f" 2>/dev/null)
done
emit host.vast_label "$(printf '%s' "$vl" | clean)"
# A pod's RUNPOD_* reach only its first process. RunPod's start script writes them to
# /etc/rp_environment for interactive shells; the probe's shell is not one, so a pod
# reached by its own ip and port was never known as a rental.
rp_id=${RUNPOD_POD_ID:-}
[ -z "$rp_id" ] && [ -r /etc/rp_environment ] && rp_id=$(sed -n \
  's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}RUNPOD_POD_ID=["'"'"']\{0,1\}\([^"'"'"']*\).*/\2/p' \
  /etc/rp_environment 2>/dev/null | head -1)
emit host.runpod_id "$(printf '%s' "$rp_id" | clean)"
emit host.autodl "$( [ -d /root/autodl-tmp ] && echo 1 || echo 0 )"
emit host.slurm "$( have sinfo && echo 1 || echo 0 )"

# ---------------------------------------------------------------- cpu
cores=""
if have nproc; then cores=$(nproc 2>/dev/null)
elif have getconf; then cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null)
fi
if [ -z "$cores" ] && [ "$OS" = "Darwin" ]; then cores=$(sysctl -n hw.ncpu 2>/dev/null); fi
if [ -z "$cores" ] && [ -r /proc/cpuinfo ]; then cores=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null); fi
emit cpu.cores "$cores"

cpu_model=""
if [ -r /proc/cpuinfo ]; then
  cpu_model=$(awk -F: '/model name/{print $2; exit}' /proc/cpuinfo 2>/dev/null | sed 's/^ *//' | clean)
  [ -n "$cpu_model" ] || cpu_model=$(awk -F: '/^Model/{print $2; exit}' /proc/cpuinfo 2>/dev/null | sed 's/^ *//' | clean)
elif [ "$OS" = "Darwin" ]; then
  cpu_model=$(sysctl -n machdep.cpu.brand_string 2>/dev/null | clean)
fi
emit cpu.model "$cpu_model"

load=""
if [ -r /proc/loadavg ]; then load=$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)
elif have sysctl; then load=$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | sed 's/^ *//;s/ *$//')
fi
emit cpu.load "$load"

# ---------------------------------------------------------------- memory (kB)
mem_total=""; mem_avail=""
if [ -r /proc/meminfo ]; then
  mem_total=$(awk '/^MemTotal:/{print $2; exit}' /proc/meminfo 2>/dev/null)
  mem_avail=$(awk '/^MemAvailable:/{print $2; exit}' /proc/meminfo 2>/dev/null)
  if [ -z "$mem_avail" ]; then
    mem_avail=$(awk '/^MemFree:/{f=$2} /^Cached:/{c=$2} END{if(f!="")print f+c}' /proc/meminfo 2>/dev/null)
  fi
elif [ "$OS" = "Darwin" ]; then
  b=$(sysctl -n hw.memsize 2>/dev/null); [ -n "$b" ] && mem_total=$(( b / 1024 ))
  # vm_stat pages are 4096B on arm64 and x86_64 macOS; free+inactive ~= available
  if have vm_stat; then
    mem_avail=$(vm_stat 2>/dev/null | awk '
      /page size of/ {gsub(/[^0-9]/,"",$0); ps=$0}
      /Pages free/ {gsub(/[^0-9]/,"",$3); f=$3}
      /Pages inactive/ {gsub(/[^0-9]/,"",$3); i=$3}
      END {if(ps=="")ps=4096; if(f!="") print int((f+i)*ps/1024)}')
  fi
fi
emit mem.total_kb "$mem_total"
emit mem.avail_kb "$mem_avail"

# ---------------------------------------------------------------- gpu
gpu_present=0; gpu_driver=""; gpu_err=""; gpu_vendor=""; amd_out=""
# nvidia-smi with a time limit. A card that fell off the bus leaves it hanging rather
# than failing, and the probe ran out its time on it: the machine read as off, "nothing
# answered at that address". Waited for in the background, not under `timeout`, which
# cannot return while the process is stuck in the driver. 124 when it did not finish.
# The 8s is kept by a timer, not by counting turns of the loop: each turn starts a
# `sleep`, and on a loaded machine 80 turns of 0.1s took 12 to 17s -- close to the 20s
# fleet gives a machine, on exactly the machines that are slow to answer.
if sleep 0.1 2>/dev/null; then NV_STEP=0.1; else NV_STEP=1; fi
# NV_ALL=1: its errors too, which a broken driver writes on stderr.
nvsmi() {
  nv_out=$(mktemp "${TMPDIR:-/tmp}/fleet-nvsmi.XXXXXX" 2>/dev/null)
  if [ -z "$nv_out" ]; then nvidia-smi "$@" 2>/dev/null; return; fi
  if [ -n "${NV_ALL:-}" ]; then nvidia-smi "$@" >"$nv_out" 2>&1 </dev/null &
  else nvidia-smi "$@" >"$nv_out" 2>/dev/null </dev/null &
  fi
  nv_pid=$!
  ( sleep 8; : >"$nv_out.late" ) >/dev/null 2>&1 </dev/null &
  nv_timer=$!
  while kill -0 "$nv_pid" 2>/dev/null; do
    if [ -e "$nv_out.late" ]; then
      kill -9 "$nv_pid" 2>/dev/null; rm -f "$nv_out" "$nv_out.late"; return 124
    fi
    sleep "$NV_STEP"
  done
  kill "$nv_timer" 2>/dev/null; rm -f "$nv_out.late"
  wait "$nv_pid"; nv_rc=$?
  cat "$nv_out"; rm -f "$nv_out"
  return "$nv_rc"
}
if have nvidia-smi; then
  gpu_vendor=nvidia
  gpu_driver=$(nvsmi --query-gpu=driver_version --format=csv,noheader)
  [ $? -eq 124 ] && gpu_driver="nvidia-smi did not answer within 8s"
  gpu_driver=$(printf '%s\n' "$gpu_driver" | head -1 | clean)
  # A broken driver answers on stdout -- "Failed to initialize NVML: Driver/library
  # version mismatch" -- so anything that is not a version number is the error, not a
  # driver: read as one, the GPU table was made of that sentence.
  case $gpu_driver in
    [0-9]*) gpu_present=1 ;;
    *) gpu_present=err
       gpu_err=${gpu_driver:-$(NV_ALL=1 nvsmi | grep . | head -1 | clean)}
       gpu_driver="" ;;
  esac
fi

# AMD: amd-smi (ROCm 6.1 and later) or rocm-smi before it. Both are Python programs, so
# python3 is there whenever they are, and reading their --json beats scraping a table
# whose columns move between releases. Prints the same rows as the nvidia-smi queries
# below: GPU rows, then `--`, then gpu_uuid|pid|vram_mib for each process on a card.
amd_smi() {
  python3 - "$1" "$FLEET_MODE" <<'PY' 2>/dev/null
import json, re, subprocess, sys
tool, mode = sys.argv[1], sys.argv[2]
def run(*args):
    try:
        out = subprocess.run([tool, *args], capture_output=True, text=True, timeout=8).stdout
        return json.loads(out[out.find(next(c for c in out if c in "[{")):]) if out.strip() else None
    except Exception:
        return None
def num(v):
    # 64 | "64" | "64.0 W" | {"value": 64, "unit": "MB"} | "N/A"
    unit = ""
    if isinstance(v, dict):
        v, unit = v.get("value"), str(v.get("unit", ""))
    m = re.search(r"-?\d+(\.\d+)?", str(v)) if v is not None else None
    if not m:
        return None, ""
    return float(m.group()), unit or str(v)[m.end():].strip()
def mib(v, default_unit="B"):
    n, unit = num(v)
    if n is None:
        return None
    unit = (unit or default_unit).upper()
    scale = {"B": 1 / 2**20, "KB": 1 / 1024, "KIB": 1 / 1024, "MB": 1, "MIB": 1,
             "GB": 1024, "GIB": 1024}.get(unit, 1 / 2**20)
    return int(n * scale)
def find(d, *names):
    # the first value whose key contains one of names (case-insensitive), searched deep
    if isinstance(d, dict):
        for name in names:
            for k, v in d.items():
                leaf = not isinstance(v, (dict, list)) or (isinstance(v, dict) and "value" in v)
                if leaf and name in str(k).lower():
                    return v
        for v in d.values():
            r = find(v, *names)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = find(v, *names)
            if r is not None:
                return r
    return None
def first_num(d, *names):
    # the first of names that holds a number: an MI300 reports its edge sensor as "N/A"
    # and the junction one as a number, and reading only the first name found gave none
    for name in names:
        n = num(find(d, name))[0]
        if n is not None:
            return n
    return None
def ident(v):
    # "N/A", or a serial of zeros, names no card: every card of a box got the same "N/A"
    v = str(v or "").strip()
    return None if not v or v.upper() in ("N/A", "NA", "NONE", "UNKNOWN") \
        or not v.strip("0xX") else v
def unique(gpus):
    # two cards reporting one serial stay two cards, each with its own jobs
    seen = set()
    for g in gpus:
        if g[1] in seen:
            g[1] = f"{g[1]}-{g[0]}"
        seen.add(g[1])
gpus, apps = [], []
def cell(x):
    return "" if x is None else (str(int(x)) if isinstance(x, float) and x.is_integer() else str(x))
if tool.endswith("amd-smi"):
    static, metric = run("static", "--json") or [], run("metric", "--json") or []
    static = static.get("gpu_data", static) if isinstance(static, dict) else static
    metric = metric.get("gpu_data", metric) if isinstance(metric, dict) else metric
    by = {}
    for s in static if isinstance(static, list) else []:
        by.setdefault(s.get("gpu"), {})["s"] = s
    for m in metric if isinstance(metric, list) else []:
        by.setdefault(m.get("gpu"), {})["m"] = m
    for idx, d in sorted(by.items(), key=lambda kv: (kv[0] is None, kv[0])):
        s, m = d.get("s", {}), d.get("m", {})
        uuid = ident(find(s, "asic_serial")) or ident(find(s, "serial")) \
            or ident(find(s, "uuid")) or f"amd-{idx}"
        name = str(find(s, "market_name", "product_name", "name") or "AMD GPU")
        total = mib(find(m.get("mem_usage", m), "total_vram"), "MB")
        used = mib(find(m.get("mem_usage", m), "used_vram"), "MB")
        free = mib(find(m.get("mem_usage", m), "free_vram"), "MB")
        if free is None and total is not None and used is not None:
            free = total - used
        util = num(find(m.get("usage", m), "gfx_activity", "gfx_usage"))[0]
        temp = first_num(m.get("temperature", m), "edge", "hotspot", "junction")
        power = first_num(m.get("power", m), "socket_power", "average_socket_power")
        gpus.append([idx, uuid, name, total, used, free, util, temp, power])
    unique(gpus)
    if mode != "shared":
        uuid_of = {g[0]: g[1] for g in gpus}
        procs = run("process", "--json") or []
        procs = procs.get("gpu_data", procs) if isinstance(procs, dict) else procs
        for entry in procs if isinstance(procs, list) else []:
            for p in entry.get("process_list", []) or []:
                info = p.get("process_info", p) if isinstance(p, dict) else {}
                pid = num(info.get("pid"))[0] if isinstance(info, dict) else None
                if pid:
                    apps.append([uuid_of.get(entry.get("gpu"), ""), int(pid),
                                 mib(find(info, "vram_mem", "vram"), "B") or 0])
else:
    data = run("--showuniqueid", "--showproductname", "--showmeminfo", "vram", "--showuse",
               "--showtemp", "--showpower", "--json") or {}
    cards = sorted((k for k in data if re.fullmatch(r"card\d+", k)), key=lambda k: int(k[4:]))
    for k in cards:
        c, idx = data[k], int(k[4:])
        uuid = ident(find(c, "unique id")) or f"amd-{idx}"
        name = str(find(c, "card series", "card model", "card sku") or "AMD GPU")
        total, used = mib(find(c, "vram total memory")), mib(find(c, "vram total used memory"))
        free = total - used if total is not None and used is not None else None
        util = num(find(c, "gpu use"))[0]
        temp = first_num(c, "temperature (sensor edge)", "temperature (sensor junction)",
                         "temperature")
        power = first_num(c, "average graphics package power", "current socket graphics",
                          "power")
        gpus.append([idx, uuid, name, total, used, free, util, temp, power])
    unique(gpus)
    if mode != "shared":
        pids = (run("--showpids", "--json") or {}).get("system", {})
        where = (run("--showpidgpus", "--json") or {}).get("system", {})
        for key, val in pids.items():
            if not key.upper().startswith("PID"):
                continue
            # "name, number of GPUs, VRAM bytes, SDMA bytes, CU occupancy"
            parts = [p.strip() for p in str(val).split(",")]
            vram = mib(parts[2]) if len(parts) > 2 else 0
            on = [int(x) for x in re.findall(r"\d+", str(where.get(key, "")))]
            uuid = (gpus[0][1] if len(gpus) == 1 else
                    next((g[1] for g in gpus if on and g[0] == on[0]), ""))
            apps.append([uuid, int(key[3:]), vram or 0])
for g in gpus:
    print("|".join(cell(x) for x in g))
print("--")
for a in apps:
    print("|".join(cell(x) for x in a))
PY
}
if [ "$gpu_present" = "0" ] && have python3; then
  for tool in amd-smi rocm-smi; do
    have "$tool" || continue
    gpu_vendor=amd
    amd_out=$(amd_smi "$(command -v "$tool")")
    if [ -n "$(printf '%s\n' "$amd_out" | sed '/^--$/,$d')" ]; then
      gpu_present=1
      gpu_driver=$(head -1 /opt/rocm/.info/version 2>/dev/null | clean)
      break
    fi
    gpu_present=err
    gpu_err="$tool is installed but read no GPU -- is the amdgpu driver loaded?"
  done
fi
emit gpu.present "$gpu_present"
emit gpu.vendor "$gpu_vendor"
emit gpu.driver "$gpu_driver"
emit gpu.error "$gpu_err"

# ---------------------------------------------------------------- what this machine says about itself
# machine.yaml is the machine's own description: cost, tags, notes, and where models,
# datasets and media live. It is kept here, on the machine, so that whoever probes it --
# from any machine, as any number of people -- reads the same thing with nothing to sync.
# /etc/fleet is for the whole machine, the user's file for whoever logs in; the parser
# lays the second over the first. Sent base64 so a YAML `#` or `|` cannot break a line.
NOW=$(date +%s 2>/dev/null)
b64() { if have base64; then base64 | tr -d '\n\r'; elif have openssl; then openssl base64 | tr -d '\n\r'; fi; }
notes_sys=""; notes_user=""
[ -r /etc/fleet/machine.yaml ] && notes_sys=/etc/fleet/machine.yaml
for f in "$HOME/.config/fleet/machine.yaml" "$HOME/Library/Application Support/fleet/machine.yaml"; do
  if [ -r "$f" ]; then notes_user=$f; break; fi
done
# One byte past the 8 KB limit, so the parser knows a file is over it rather than cut.
[ -n "$notes_sys" ] && emit notes.system "$(head -c 8193 "$notes_sys" 2>/dev/null | b64)"
[ -n "$notes_user" ] && emit notes.user "$(head -c 8193 "$notes_user" 2>/dev/null | b64)"

# The `paths:` block of those files, as kind|path lines. Reads the shapes YAML allows for
# a list of strings -- `models: /a`, `models: [/a, /b]`, and `models:` then `- /a` -- which
# is everything `fleet edit --path` writes and what people write by hand.
paths_in() {
  awk '
      function out(k, v) { gsub(/^[ \t"\047]+|[ \t"\047]+$/, "", v); if (k != "" && v != "") print k "|" v }
      /^[^ \t#]/ { inp = ($0 ~ /^paths:[ \t]*(#.*)?$/); k = ""; next }
      !inp || /^[ \t]*(#.*)?$/ { next }
      /^[ \t]*-[ \t]/ { v = $0; sub(/^[ \t]*-[ \t]*/, "", v); sub(/[ \t]+#.*/, "", v); out(k, v); next }
      /^[ \t]+[A-Za-z0-9_-]+:/ {
        k = $0; sub(/^[ \t]+/, "", k); sub(/:.*/, "", k)
        v = $0; sub(/^[^:]*:[ \t]*/, "", v); sub(/[ \t]+#.*/, "", v)
        if (v ~ /^\[/) { gsub(/[][]/, "", v); n = split(v, a, ","); for (i = 1; i <= n; i++) out(k, a[i]) }
        else out(k, v)
      }' "$1" 2>/dev/null
}
store_paths() {
  # The user's file over the machine's, kind by kind, as the parser lays them: a kind the
  # user's file names is that file's alone. Both were read whole, and the machine's
  # folder was sized, listed and alerted on beside the one that replaced it.
  mine=""; [ -n "$notes_user" ] && mine=$(paths_in "$notes_user")
  kinds=" $(printf '%s\n' "$mine" | cut -d'|' -f1 | tr '\n' ' ')"
  { [ -n "$notes_sys" ] && paths_in "$notes_sys" | while IFS='|' read -r k p; do
      case "$kinds" in *" $k "*) ;; *) printf '%s|%s\n' "$k" "$p" ;; esac
    done
    [ -n "$mine" ] && printf '%s\n' "$mine"
  } | while IFS='|' read -r k p; do
    case "$p" in "~") p=$HOME ;; "~/"*) p="$HOME/${p#\~/}" ;; esac
    printf '%s|%s\n' "$k" "$p"
  done | awk '!seen[$0]++' | head -40
}
paths=$(store_paths)

# The model servers the notes declare (`endpoints:` -- name: PORT or PORT/base/path), as
# port|path lines: ones the probe would not recognise by their port or program name.
declared_endpoints() {
  for f in "$notes_sys" "$notes_user"; do
    [ -n "$f" ] || continue
    awk '
      /^[^ \t#]/ { ine = ($0 ~ /^endpoints:[ \t]*(#.*)?$/); next }
      !ine || /^[ \t]*(#.*)?$/ { next }
      /^[ \t]+[A-Za-z0-9_-]+:/ {
        v = $0; sub(/^[^:]*:[ \t]*/, "", v); sub(/[ \t]+#.*/, "", v); gsub(/["\047 ]/, "", v)
        if (v ~ /^[0-9]+(\/[A-Za-z0-9._~\/-]*)?$/) { n = index(v, "/"); if (n) print substr(v, 1, n - 1) "|" substr(v, n); else print v "|" }
      }' "$f" 2>/dev/null
  done | awk -F'|' '!seen[$1]++' | head -8
}
declared=$(declared_endpoints)

# Can this mount actually fill up? A mounted disk image is read-only and 100% full by
# definition, and alerting on it forever is noise. "/" is the exception and is always
# treated as writable: on macOS it is the read-only signed system volume, yet the free
# space df reports for it belongs to the shared container that user data fills, so
# trusting -w there would silence the one alert that matters most.
#
# Read from the mount's own flags, not from whether we can write there: a user's -w is
# false on /home, /mnt/data and every other disk owned by root, and a full one of those
# never alerted. -w is the fallback where the flags cannot be read.
# A network share that stopped answering (a stale NFS mount) makes df wait for ever: the
# probe ran out its time and the machine read as unreachable. Five seconds, then local
# disks alone (`-l`). Where there is no `timeout` (macOS), as before.
TD=; timeout -s KILL 5 true >/dev/null 2>&1 && TD="timeout -s KILL 5"
dfk() { $TD df "$@"; }
writable() {
  [ "$1" = "/" ] && { echo 1; return; }
  # The mount holding the path, as df finds it. Matching the path's text against mount
  # points goes wrong on a Mac, where /Users and /private/var reach the data volume by
  # firmlinks: the longest text match was the sealed, read-only system volume at "/",
  # so every folder there read as read-only and never alerted.
  mp=$(dfk -P "$1" 2>/dev/null | awk 'NR == 2 { m = $0; sub(/^.* [0-9]+% +/, "", m); print m }')
  [ "$mp" = "/" ] && { echo 1; return; }
  set -- "${mp:-$1}"
  ro=""
  if [ -r /proc/mounts ]; then
    # The longest mount point holding the path; spaces in /proc/mounts are \040.
    ro=$(awk -v p="$1" '{ m = $2; gsub(/\\040/, " ", m)
           if ((p == m || index(p, (m == "/" ? "/" : m "/")) == 1) && length(m) >= best) {
             best = length(m); r = ($4 ~ /(^|,)ro(,|$)/) } }
         END { if (best != "") print r }' /proc/mounts 2>/dev/null)
  elif have mount; then
    ro=$(mount 2>/dev/null | awk -v p="$1" '{ i = index($0, " on "); j = index($0, " (")
           if (!i || j <= i) next; m = substr($0, i + 4, j - i - 4)
           if ((p == m || index(p, (m == "/" ? "/" : m "/")) == 1) && length(m) >= best) {
             best = length(m); r = (substr($0, j) ~ /read-only/) } }
         END { if (best != "") print r }')
  fi
  case $ro in
    1) echo 0 ;; 0) echo 1 ;;
    *) if [ -w "$1" ]; then echo 1; else echo 0; fi ;;
  esac
}

# ---------------------------------------------------------------- network
# What `fleet wake` needs once this machine is asleep: each card's hardware address and
# IPv4 subnet, whether it is armed to wake on a magic packet, and the router's hardware
# address -- which tells one LAN from another on the same subnet. All read without root;
# a card or a command that is not there is left out.
echo "#NIC name|mac|ipv4|prefix|wol"
gw_ip=""; gw_mac=""
if [ "$OS" = "Linux" ] && [ -d /sys/class/net ]; then
  for d in /sys/class/net/*; do
    [ -e "$d/device" ] || continue       # docker, veth, bridges, tailscale: no hardware
    nic=${d##*/}
    mac=$(clean < "$d/address" 2>/dev/null)
    # The kernel's own switch for the card waking the machine, which the driver sets from
    # its Wake-on-LAN setting; ethtool says it too, but only to root.
    case $(cat "$d/device/power/wakeup" 2>/dev/null) in
      enabled) wol=1 ;; disabled) wol=0 ;; *) wol= ;;
    esac
    # A cloud VM's card is software: nothing listens for a packet while it is stopped.
    case $(basename "$(readlink "$d/device/driver" 2>/dev/null)" 2>/dev/null) in
      virtio_net|virtio-pci|vmxnet3|hv_netvsc|vif|xen-netfront|ena|gve) wol=0 ;;
    esac
    if [ -z "$wol" ] && have ethtool; then
      case $(ethtool "$nic" 2>/dev/null | awk '$1 == "Wake-on:" { print $2; exit }') in
        *g*) wol=1 ;; d) wol=0 ;;
      esac
    fi
    ip4=""; have ip && ip4=$(ip -4 -o addr show dev "$nic" 2>/dev/null | awk '{ print $4; exit }')
    pfx=""; [ -n "$ip4" ] && pfx=${ip4#*/}
    printf '%s|%s|%s|%s|%s\n' "$nic" "$mac" "${ip4%/*}" "$pfx" "$wol"
  done
  if have ip; then
    gw_ip=$(ip -4 route show default 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }')
    [ -n "$gw_ip" ] && gw_mac=$(ip neigh show "$gw_ip" 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "lladdr") { print $(i + 1); exit } }')
  fi
elif [ "$OS" = "Darwin" ]; then
  # "Wake for network access" is one setting for the whole Mac.
  womp=$(pmset -g 2>/dev/null | awk '$1 == "womp" { print $2; exit }')
  ifconfig 2>/dev/null | awk -v womp="$womp" '
    function bits(mask,   i, n) { n = 0; mask = tolower(substr(mask, 3))
      for (i = 1; i <= length(mask); i++) n += index("08cef", substr(mask, i, 1)) - 1
      return n }
    function flush() { if (n ~ /^en[0-9]+$/ && m != "") printf "%s|%s|%s|%s|%s\n", n, m, a, p, w
      n = ""; m = ""; a = ""; p = "" }
    BEGIN { w = (womp == "1") ? 1 : (womp == "0") ? 0 : "" }
    /^[^ \t]/ { flush(); n = $1; sub(/:$/, "", n); next }
    $1 == "ether" { m = $2 }
    $1 == "inet" && a == "" { a = $2; for (i = 3; i < NF; i++) if ($i == "netmask") p = bits($(i + 1)) }
    END { flush() }'
  gw_ip=$(route -n get default 2>/dev/null | awk '$1 == "gateway:" { print $2; exit }')
  # "? (192.168.0.1) at 0:11:22:33:44:55 on en0 ifscope [ethernet]"
  [ -n "$gw_ip" ] && gw_mac=$(arp -n "$gw_ip" 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "at") { print $(i + 1); exit } }')
fi
echo "#GATEWAY ip|mac"
[ -n "$gw_ip" ] && printf '%s|%s\n' "$gw_ip" "$(printf '%s' "$gw_mac" | clean)"

# ---------------------------------------------------------------- disk
# On a Mac every APFS volume of a disk reports the whole disk's size and free space, and
# only its own use: "/" said 11G used and 30G free on a 1T disk that its data volume had
# filled to 97% -- 27% "used", and the disk-full alert could never come. What is used of
# the disk is what is not free.
MAC_DF=; [ "$OS" = "Darwin" ] && MAC_DF=1
echo "#DISK mount|total_kb|used_kb|avail_kb|rw"
if have df; then
  if [ -n "${FLEET_DISK_PATHS:-}" ]; then
    # Configured paths win outright. df is asked about each path directly, so a plain
    # directory works as well as a mount point, and the row is labelled with the path
    # the user asked about rather than whatever filesystem happens to hold it. They
    # come separated by "|", not spaces, which cut "/mnt/My Data" in two.
    printf '%s\n' "$FLEET_DISK_PATHS" | tr '|' '\n' | while IFS= read -r p; do
      [ -n "$p" ] || continue
      # `~/data` is this login's home, as it is for a folder: looked up as written, it
      # was "not there". The row keeps the path as asked.
      q=$p
      case "$q" in "~") q=$HOME ;; "~/"*) q="$HOME/${q#\~/}" ;; esac
      # A path that is not there (mistyped, or not mounted yet) is said so: dropped,
      # the machine showed no disk at all and nothing said why.
      [ -e "$q" ] || { printf '%s|0|0|0|missing\n' "$p"; continue; }
      rw=$(writable "$q")
      dfk -Pk "$q" 2>/dev/null | awk -v p="$p" -v rw="$rw" -v mac="$MAC_DF" 'NR>1 && $2+0>0 {
        printf "%s|%s|%s|%s|%s\n", p, $2, (mac ? $2 - $4 : $3), $4, rw
      }'
    done | head -20
  else
    # writability is decided in the shell, not awk: a mount you cannot write to cannot
    # fill up, so "100% full" there is never actionable -- a mounted disk image is the
    # usual case, and it alerted forever.
    # The mount point is everything after the capacity column: $6 alone cut
    # "/Volumes/My Passport" to "/Volumes/My", which df then could not find again.
    # Twenty are kept: / and then the biggest. Cut by name, twenty shares under /mnt
    # left out /workspace -- the 8 TB volume a rental is for, and its storage facts.
    { all=$(dfk -Pk 2>/dev/null); [ $? -ge 124 ] && all=$(dfk -Pkl 2>/dev/null); printf '%s\n' "$all"; } \
      | awk -v mac="$MAC_DF" 'NR>1 && $2+0>0 {
      m=$0; sub(/^.* [0-9]+% +/, "", m)
      if (m=="/" || m ~ /^\/(workspace|data|mnt|home|srv|opt|Volumes|scratch)/)
        printf "%s|%s|%s|%s\n", m, $2, (mac ? $2 - $4 : $3), $4
    }' | sort -u \
      | awk -F'|' '{ print ($1 == "/" ? 0 : 1) "|" $0 }' | sort -t'|' -k1,1n -k3,3nr \
      | head -20 | cut -d'|' -f2- | sort | while IFS='|' read -r m tot used avail; do
      rw=$(writable "$m")
      printf '%s|%s|%s|%s|%s\n' "$m" "$tot" "$used" "$avail" "$rw"
    done
  fi
fi

if [ "$gpu_present" = "1" ]; then
  echo "#GPU idx|uuid|name|total_mib|used_mib|free_mib|util_pct|temp_c|power_w"
  if [ "$gpu_vendor" = "amd" ]; then
    printf '%s\n' "$amd_out" | sed '/^--$/,$d'
  else
    nvsmi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw \
      --format=csv,noheader,nounits \
      | sed 's/, */|/g' | sed 's/ *|/|/g; s/| */|/g'
  fi
fi
# gpu_uuid|pid|vram_mib for each process on a GPU, from whichever vendor's tool it is.
gpu_apps() {
  if [ "$gpu_vendor" = "amd" ]; then
    printf '%s\n' "$amd_out" | sed '1,/^--$/d'
  else
    nvsmi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits \
      | sed 's/, */|/g'
  fi
}

# ---------------------------------------------------------------- processes
# Skipped entirely in shared mode: enumerating other users' work on a machine you do
# not own is both rude and a privacy problem.
if [ "$FLEET_MODE" != "shared" ]; then
  if [ "$gpu_present" = "1" ]; then
    echo "#GPUPROC gpu_uuid|pid|vram_mib|user|etimes|comm|args"
    gpu_apps | while IFS='|' read -r guuid gpid gmem; do
          [ -n "$gpid" ] || continue
          # never trust nvidia-smi's process_name: for GUI apps it is ~1KB of flags.
          # user:64, or procps cuts a name past 8 characters to "research+" whenever
          # another column follows it: the one thing that says whose job it is. The
          # plain form is for a ps that takes no width.
          info=$({ ps -o user:64=,etimes=,comm= -p "$gpid" 2>/dev/null \
                   || ps -o user=,etimes=,comm= -p "$gpid" 2>/dev/null; } | head -1 \
                 | awk '{u=$1; e=$2; $1=""; $2=""; sub(/^ +/,""); printf "%s|%s|%s", u, e, $0}')
          [ -n "$info" ] || info="||"
          # The command line says what the job is ("python train.py"), where comm only
          # says "python". The parser redacts anything in it that looks like a secret.
          args=$(ps -o args= -p "$gpid" 2>/dev/null | head -1 | clean | cut -c1-200)
          printf '%s|%s|%s|%s|%s\n' "$guuid" "$gpid" "$gmem" "$info" "$args"
        done
  fi

  echo "#CPUPROC pid|user|pcpu|rss_kb|etimes|comm"
  # Only processes using over 1% of a core: on a Mac every idle one was listed too, and
  # `fleet show` named launchd and a shell at 0% as what kept the machine busy.
  # A process alive for under a second is also dropped (Linux only -- Darwin's etime
  # cannot express sub-second). pcpu is cputime/elapsed, so at elapsed near zero it is
  # meaningless and inflated, which is how a tailscaled fork servicing our own inbound
  # connection kept outranking processes actually using the machine.
  # ppid is asked for so we can drop our own processes, then dropped again before
  # output -- the probe runs ps, so ps and this shell see themselves, and a
  # just-started process reports a huge pcpu that lands it straight at the top.
  # Filtering happens before sort|head, or our own rows would displace real ones.
  if [ "$OS" = "Darwin" ]; then
    ps -axo pid=,ppid=,user=,pcpu=,rss=,etime=,comm= 2>/dev/null \
      | awk -v me="$$" '$1 != me && $2 != me && $4+0>1.0 {
              p=$1;u=$3;c=$4;r=$5;e=$6;
              $1="";$2="";$3="";$4="";$5="";$6="";sub(/^ +/,"");
              n=split($0,a,"/"); printf "%s|%s|%s|%s|%s|%s\n",p,u,c,r,e,a[n]}' \
      | sort -t'|' -k3 -rn | head -10
  else
    { ps -eo pid=,ppid=,user:64=,pcpu=,rss=,etimes=,comm= 2>/dev/null \
        || ps -eo pid=,ppid=,user=,pcpu=,rss=,etimes=,comm= 2>/dev/null; } \
      | awk -v me="$$" '$1 != me && $2 != me && $4+0>1.0 && $6+0>=1 {
              c=$7; for (i = 8; i <= NF; i++) c = c " " $i     # "Web Content", not "Web"
              printf "%s|%s|%s|%s|%s|%s\n",$1,$3,$4,$5,$6,c}' \
      | sort -t'|' -k3 -rn | head -10
  fi
fi

# ---------------------------------------------------------------- listening services
# What a model server is called, and the ports one usually takes.
LLM_NAMES='vllm|sglang|ollama|mlx|llama|lmstudio|lms|text-gen|omlx|kobold|tabby|aphrodite|litellm|local-ai|exllama'
LLM_PORTS='8000 8001 8010 8080 11434 1234 5000 30000'
listen=$( {
if [ "$OS" = "Darwin" ] && have lsof; then
  # macOS: netstat exists but has no -tlnp; lsof is the only reliable source.
  lsof +c 0 -nP -iTCP -sTCP:LISTEN 2>/dev/null | tail -n +2 \
    | awk '{la=$9; port=la; sub(/.*:/, "", port); addr=substr(la, 1, length(la)-length(port)-1)
            printf "LISTEN 0 0 %s:%s x users:((\"%s\",pid=%s,fd=0))\n", (addr==""?"*":addr), port, $1, $2}'
elif have ss; then
  # -p names the program behind each port -- every process we may see, which is ours
  # and, as root, everyone's. Without it a vLLM server was an anonymous port 8000.
  ss -tlnpH 2>/dev/null || ss -tlnH 2>/dev/null || ss -tln 2>/dev/null | tail -n +2
elif have netstat; then
  # The exit status of `netstat`, not of `tail`: piped, the fallback never ran.
  ns=$(netstat -tlnp 2>/dev/null) || ns=$(netstat -tln 2>/dev/null)
  # netstat names the program as "1234/python3", not as ss does: read as it came, every
  # port it found was a nameless one, and a model server on a box without ss was unknown.
  printf '%s\n' "$ns" | tail -n +3 | awk '$7 ~ /^[0-9]+\// {
      c = substr($0, index($0, $7)); pid = c; sub(/\/.*/, "", pid)
      sub(/^[0-9]+\//, "", c); sub(/[ \t]+$/, "", c)
      printf "%s %s %s %s %s %s users:((\"%s\",pid=%s,fd=0))\n", $1, $2, $3, $4, $5, $6, c, pid
      next }
    { print }'
elif [ "$OS" = "Linux" ] && [ -r /proc/net/tcp ]; then
  # A slim image (a rented container) may have none of ss, netstat or lsof, and lsof run
  # as a user lists only that user's ports: there `fleet models` found no server at all.
  # The kernel's own table lists every port; the processes we may see name theirs.
  # Which process holds each socket first, then the table, in one stream: no file to write.
  { ls -l /proc/[0-9]*/fd 2>/dev/null | awk '
      /^\/proc\/[0-9]+\/fd:$/ { pid = $0; sub(/^\/proc\//, "", pid); sub(/\/.*/, "", pid); next }
      /socket:\[/ { i = $NF; gsub(/[^0-9]/, "", i); print "S", i, pid }'
    cat /proc/net/tcp /proc/net/tcp6 2>/dev/null; } \
    | awk '
      function hex(s,  i, n) { n = 0; s = toupper(s)
        for (i = 1; i <= length(s); i++) n = n * 16 + index("0123456789ABCDEF", substr(s, i, 1)) - 1
        return n }
      function v4(a) { return hex(substr(a,7,2)) "." hex(substr(a,5,2)) "." hex(substr(a,3,2)) "." hex(substr(a,1,2)) }
      function v6(a,  w, i, out, g) {
        if (a ~ /^0+$/) return "[::]"
        if (a == "00000000000000000000000001000000") return "[::1]"
        if (substr(a, 1, 16) == "0000000000000000" && substr(a, 17, 8) == "FFFF0000") return "[::ffff:" v4(substr(a, 25, 8)) "]"
        out = ""
        for (w = 0; w < 4; w++) { g = substr(a, w*8+1, 8)
          g = substr(g,7,2) substr(g,5,2) substr(g,3,2) substr(g,1,2)
          out = out (w ? ":" : "") sprintf("%x:%x", hex(substr(g,1,4)), hex(substr(g,5,4))) }
        return "[" out "]" }
      $1 == "S" { owner[$2] = $3; next }
      $4 == "0A" { split($2, la, ":"); a = la[1]
        addr = (length(a) == 8) ? v4(a) : v6(toupper(a)); port = hex(la[2]); pid = owner[$10]
        if (pid == "") { printf "LISTEN 0 0 %s:%s x\n", addr, port; next }
        comm = ""; cf = "/proc/" pid "/comm"; if ((getline comm < cf) <= 0) comm = "?"; close(cf)
        printf "LISTEN 0 0 %s:%s x users:((\"%s\",pid=%s,fd=0))\n", addr, port, comm, pid }'
elif have lsof; then
  lsof +c 0 -nP -iTCP -sTCP:LISTEN 2>/dev/null | tail -n +2 \
    | awk '{la=$9; port=la; sub(/.*:/, "", port); addr=substr(la, 1, length(la)-length(port)-1)
            printf "LISTEN 0 0 %s:%s x users:((\"%s\",pid=%s,fd=0))\n", (addr==""?"*":addr), port, $1, $2}'
fi; } 2>/dev/null | awk '
  {
    la=""
    for (i=1;i<=NF;i++) if ($i ~ /:[0-9]+$/) { la=$i; break }
    if (la=="") next
    n=split(la, parts, ":"); port=parts[n]
    addr=substr(la, 1, length(la)-length(port)-1)
    if (addr=="") addr="*"
    pid=""; comm=""
    if (match($0, /pid=[0-9]+/)) pid=substr($0, RSTART+4, RLENGTH-4)
    if (match($0, /\(\("[^"]+"/)) comm=substr($0, RSTART+3, RLENGTH-4)
    printf "tcp|%s|%s|%s|%s\n", addr, port, pid, comm
  }' | sort -u -t'|' -k3,3n \
  | awk -F'|' -v re="$LLM_NAMES" -v ports="$LLM_PORTS" '
      # What looks like a model server first, then by port, then cut: by port alone, a
      # node with 40 services below 8000 lost its vLLM server from `fleet models`.
      BEGIN { n = split(ports, h, " "); for (i = 1; i <= n; i++) hint[h[i]] = 1 }
      { print ((tolower($5) ~ re || ($3 in hint)) ? 0 : 1) "|" $0 }' \
  | sort -t'|' -k1,1n -k4,4n | head -40 | cut -d'|' -f2- | sort -t'|' -k3,3n )
echo "#LISTEN proto|addr|port|pid|comm"
[ -n "$listen" ] && printf '%s\n' "$listen"

# ---------------------------------------------------------------- SLURM (shared boxes)
if [ "$FLEET_MODE" = "shared" ] && have squeue; then
  echo "#SLURM key|value"
  # One call, with a few seconds for it. A controller that is down or swamped answers
  # "Unable to contact slurm controller", which was counted as no jobs -- "0 job(s) of
  # yours" while yours ran -- or answers nothing for a minute, and the probe ran out its
  # time on it: the login node read as down. Unanswered, the count is unknown, and says so.
  TQ=; [ -n "$TD" ] && TQ="timeout -s KILL 4"
  if sq=$($TQ squeue -h -u "$(id -un)" -o '%T' 2>/dev/null); then
    printf 'my_jobs|%s\n' "$(printf '%s' "$sq" | grep -c .)"
    printf 'my_running|%s\n' "$(printf '%s\n' "$sq" | grep -c '^RUNNING$')"
  else
    printf 'my_jobs|\nmy_running|\n'
  fi
  if have sinfo; then
    $TQ sinfo -h -o '%P %a %D %t' 2>/dev/null | head -12 | while read -r line; do
      printf 'partition|%s\n' "$line"
    done
  fi
fi

# Some answers are slow (how big is a folder of models) or rarely change (which torch is
# installed), so they are worked out now and then and kept here, on the machine. Every
# viewer then pays only for reading a file, and `fleet top` never pays at all.
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/fleet"
# A folder that is there and may not be written -- made by a `sudo` run, on a read-only
# home -- is no place to keep them: every write failed, and the software list came back
# empty on every probe, cuda and python and torch with it.
{ mkdir -p "$CACHE" && [ -w "$CACHE" ]; } 2>/dev/null || CACHE=""
# One folder for each machine, by its id: a home shared by several -- the nodes of a
# cluster -- shares this folder too, and one node reported another's CUDA, Python and
# Docker, and its folder sizes, for six hours.
if [ -n "$CACHE" ]; then
  mine=$(printf '%s' "${machine_id:-$hostname_v}" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-64)
  CACHE="$CACHE/${mine:-this}"
  { mkdir -p "$CACHE" && [ -w "$CACHE" ]; } 2>/dev/null || CACHE=""
fi
age_ok() {  # file max_age_s: does the file exist and say it was written recently enough?
  [ -n "$CACHE" ] && [ -r "$1" ] || return 1
  t=$(head -1 "$1" 2>/dev/null | awk '{print $2}')
  [ -n "$t" ] && [ -n "$NOW" ] && [ $((NOW - t)) -lt "$2" ]
}

# ---------------------------------------------------------------- storage
# Folder sizes come from `du`, which on a disk of model weights can take minutes. It runs
# in the background, detached from this probe, and the next probe reports what it found.
sizes="$CACHE/sizes"
if [ -n "$paths" ] && [ -n "$CACHE" ] && [ "${FLEET_LIGHT:-}" != "1" ]; then
  # "|"-separated, as the paths may hold spaces; the header line records the same list.
  want=$(printf '%s\n' "$paths" | cut -d'|' -f2 | sort -u | tr '\n' '|')
  have_for=$(sed -n '1s/^#v1 [0-9]* //p' "$sizes" 2>/dev/null)
  if [ "${FLEET_REFRESH:-}" = "1" ] || [ "$want" != "$have_for" ] || ! age_ok "$sizes" 21600; then
    # One at a time: a lock older than an hour is from a run that died.
    lock="$CACHE/du.lock"
    if [ -d "$lock" ] && [ -n "$(find "$lock" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then rmdir "$lock" 2>/dev/null; fi
    if mkdir "$lock" 2>/dev/null; then
      T=; timeout -s KILL 5 true >/dev/null 2>&1 && T="timeout -s KILL 1800"
      (
        trap 'rmdir "$lock" 2>/dev/null' EXIT
        tmp="$sizes.$$"
        got=$(printf '%s' "$want" | tr '|' '\n' | while IFS= read -r p; do
          [ -n "$p" ] && [ -e "$p" ] || continue
          # -H: a folder that is a link (~/models -> /mnt/nvme/models) measured as 0,
          # the size of the link. Links inside it are still not followed.
          kb=$({ $T du -H -sk "$p" 2>/dev/null || $T du -sk "$p" 2>/dev/null; } | tail -1 | cut -f1)
          [ -n "$kb" ] && printf '%s|%s\n' "$p" "$kb"
        done)
        # Kept only when all of it was written: on a disk that filled up, the sizes it
        # had were swapped for an empty file -- gone just when you look for what filled it.
        if { echo "#v1 $NOW $want"; [ -n "$got" ] && printf '%s\n' "$got"; } | cat > "$tmp" \
             && mv -f "$tmp" "$sizes"; then :; else rm -f "$tmp"; fi
      ) </dev/null >/dev/null 2>&1 &
    fi
  fi
fi
echo "#STORE kind|path|exists|avail_kb|size_kb|sized_at"
if [ -n "$paths" ]; then
  # Read once: the background `du` may replace the file while this loop runs.
  known=; [ -n "$CACHE" ] && known=$(cat "$sizes" 2>/dev/null)
  sized_at=$(printf '%s\n' "$known" | head -1 | awk '{print $2}')
  printf '%s\n' "$paths" | while IFS='|' read -r k p; do
    if [ -e "$p" ]; then
      ex=1
      av=$(dfk -Pk "$p" 2>/dev/null | awk 'NR==2{print $4}')
      kb=$(printf '%s\n' "$known" | awk -F'|' -v p="$p" 'NR>1 && $1==p {print $2; exit}')
    else ex=0; av=""; kb=""
    fi
    printf '%s|%s|%s|%s|%s|%s\n' "$k" "$p" "$ex" "$av" "$kb" "${kb:+$sized_at}"
  done
fi

# ---------------------------------------------------------------- software
# Versions of the stack people build on. Python packages are read from the names of their
# `.dist-info` folders, never by importing them: `import torch` costs seconds and can
# start a CUDA context on a GPU somebody else is using.
PKGS=" torch torchvision torchaudio mlx mlx_lm mlx_vlm omlx vllm sglang transformers accelerate diffusers jax jaxlib tensorflow deepspeed flash_attn xformers triton bitsandbytes peft trl unsloth llama_cpp_python onnxruntime onnxruntime_gpu tensorrt exllamav2 ray "
short() { case "$1" in "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac; }
scan_site() {  # env-label python-version site-packages-dir
  ls "$3" 2>/dev/null | awk -v pk="$PKGS" -v env="$1" -v py="$2" '
    /\.dist-info$/ {
      s = $0; sub(/\.dist-info$/, "", s)
      if (!match(s, /-[^-]*$/)) next
      name = tolower(substr(s, 1, RSTART - 1)); ver = substr(s, RSTART + 1)
      gsub(/[-.]/, "_", name)
      if (index(pk, " " name " ")) printf "%s|%s|%s|%s\n", env, py, name, ver
    }
    # What `setup.py install` leaves -- the pytorch conda channel, flash-attn or apex
    # built from source: torch-2.1.0-py3.10.egg-info, or an .egg. Its name never holds a
    # "-", so the second part is the version.
    /\.egg(-info)?$/ && split($0, f, "-") >= 2 {
      name = tolower(f[1]); ver = f[2]; sub(/\.egg(-info)?$/, "", ver)
      gsub(/[-.]/, "_", name)
      if (index(pk, " " name " ")) printf "%s|%s|%s|%s\n", env, py, name, ver
    }'
}
env_roots() {
  # Named ones first: the list is cut at 40, and those came last, so on a machine with
  # that many of its own the very environment you named to be found never was.
  printf '%s\n' "$paths" | awk -F'|' '$1=="envs"{print $2}'
  for d in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" "$HOME/mambaforge" \
           "$HOME/micromamba" "$HOME/.conda" /opt/conda /opt/miniconda3; do
    [ -d "$d" ] || continue
    echo "$d"
    for e in "$d"/envs/*; do [ -d "$e" ] && echo "$e"; done
  done
  for d in "$HOME"/.venvs/* "$HOME"/venv* "$HOME"/.venv "$HOME"/*/.venv "$HOME"/*/venv \
           "$HOME"/.pyenv/versions/* /workspace/.venv /workspace/*/.venv; do
    [ -d "$d/lib" ] && echo "$d"
  done
}
software_scan() {
  echo "#SOFTWARE key|value"
  printf 'scanned_at|%s\n' "$NOW"
  if [ "$gpu_present" = "1" ]; then
    printf 'cuda|%s\n' "$(nvsmi | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1)"
  fi
  nvcc_bin=$(command -v nvcc 2>/dev/null); [ -n "$nvcc_bin" ] || { [ -x /usr/local/cuda/bin/nvcc ] && nvcc_bin=/usr/local/cuda/bin/nvcc; }
  [ -n "$nvcc_bin" ] && printf 'nvcc|%s\n' "$("$nvcc_bin" --version 2>/dev/null | sed -n 's/.*release \([0-9.]*\).*/\1/p' | head -1)"
  for h in /usr/include/cudnn_version.h /usr/local/cuda/include/cudnn_version.h; do
    [ -r "$h" ] || continue
    printf 'cudnn|%s\n' "$(awk '/#define CUDNN_(MAJOR|MINOR|PATCHLEVEL) /{v = v (v == "" ? "" : ".") $3} END{print v}' "$h")"
    break
  done
  [ -r /opt/rocm/.info/version ] && printf 'rocm|%s\n' "$(head -1 /opt/rocm/.info/version | clean)"
  [ "$OS" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] && printf 'metal|1\n'
  have_python && printf 'python|%s\n' "$(python3 --version 2>&1 | awk '{print $2}' | clean)"
  have uv && printf 'uv|%s\n' "$(uv --version 2>/dev/null | awk '{print $2}' | clean)"
  have conda && printf 'conda|%s\n' "$(short "$(command -v conda)")"
  have docker && printf 'docker|%s\n' "$(docker --version 2>/dev/null | sed -n 's/^Docker version \([^,]*\).*/\1/p' | clean)"
  have ollama && printf 'ollama|%s\n' "$(short "$(command -v ollama)")"

  echo "#PYENV env|python|package|version"
  {
    if have_python; then
      python3 -c 'import site,sysconfig;print(sysconfig.get_paths()["purelib"]);print(site.getusersitepackages())' 2>/dev/null \
        | while read -r sp; do
            [ -d "$sp" ] && scan_site "$(short "$sp")" "$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" "$sp"
          done
    fi
    for sp in /usr/lib/python3/dist-packages /usr/local/lib/python3*/dist-packages; do
      [ -d "$sp" ] && scan_site "$(short "$sp")" "" "$sp"
    done
    env_roots | awk '!seen[$0]++' | head -40 | while read -r root; do
      for sp in "$root"/lib/python3*/site-packages; do
        [ -d "$sp" ] || continue
        py=${sp%/site-packages}; py=${py##*/python}
        scan_site "$(short "$root")" "$py" "$sp"
      done
    done
  } | awk '!seen[$0]++' | head -1300   # 40 environments and the system's, ~30 names each:
}                                      # at 200 the last of the 40 were never listed
sw="$CACHE/software"
if [ -n "$CACHE" ] && [ "${FLEET_REFRESH:-}" != "1" ] && age_ok "$sw" 21600 \
     && [ "$(sed -n '1s/^#v1 [0-9]* //p' "$sw")" = "$gpu_driver" ]; then
  tail -n +2 "$sw"
elif [ "${FLEET_LIGHT:-}" = "1" ]; then
  [ -n "$CACHE" ] && [ -r "$sw" ] && tail -n +2 "$sw"   # stale beats slow in `fleet top`
else
  scan=$(software_scan)
  [ -n "$scan" ] && printf '%s\n' "$scan"
  # Kept only when all of it was written (`cat` fails on a full disk where the scan does
  # not), so a copy cut short is never read back as all the machine has.
  if [ -n "$CACHE" ]; then
    if { echo "#v1 $NOW $gpu_driver"; [ -n "$scan" ] && printf '%s\n' "$scan"; } \
         | cat 2>/dev/null > "$sw.$$" && mv -f "$sw.$$" "$sw" 2>/dev/null; then :
    else rm -f "$sw.$$" 2>/dev/null; fi
  fi
fi

# ---------------------------------------------------------------- containers
if [ "${FLEET_LIGHT:-}" != "1" ] && have docker; then
  T=; timeout -s KILL 5 true >/dev/null 2>&1 && T="timeout -s KILL 3"
  running=$($T docker ps --format '{{.Image}}|{{.Names}}|{{.Status}}|{{.Ports}}' 2>/dev/null \
    | tr -d '\r' | cut -c1-300)
  echo "#CONTAINER image|name|status|ports"
  [ -n "$running" ] && printf '%s\n' "$running" | head -20
  # Twenty are listed; how many there are, so the rest are not taken for none.
  echo "#COUNT what|total"
  printf 'containers|%s\n' "$(printf '%s' "$running" | grep -c .)"
fi

# ---------------------------------------------------------------- LLM servers
# A port that looks like a model server is asked what it serves -- here, on the machine,
# so a server that only listens on localhost answers too. Two seconds each, eight at most,
# ten seconds in all.
if [ "${FLEET_LIGHT:-}" != "1" ] && { [ -n "$listen" ] || [ -n "$declared" ]; } \
     && { have curl || have wget || have_python; }; then
  # Without mktemp, beside the script -- the probe's own folder -- not at a name in /tmp
  # anyone could make first.
  case "$0" in */*) here=${0%/*} ;; *) here=. ;; esac
  body=$(mktemp 2>/dev/null || echo "$here/.fleet-llm.$$")
  get() {  # url -> "code seconds"; body in $body
    if have curl; then
      curl -s -m 2 -o "$body" -w '%{http_code} %{time_total}' "$1" 2>/dev/null || echo "000 0"
    elif have wget; then
      wget -S -q -T 2 -t 1 -O "$body" "$1" 2>&1 | awk '/^ *HTTP\//{c=$2} END{print (c ? c : "000") " 0"}'
    else
      # Minimal images -- most rentals -- have neither, but a model server needs Python.
      python3 - "$1" "$body" <<'PY' 2>/dev/null || echo "000 0"
import sys, time, urllib.request, urllib.error
t = time.time()
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        open(sys.argv[2], "wb").write(r.read(1 << 20)); code = r.status
except urllib.error.HTTPError as e:
    code = e.code
except Exception:
    code = 0
print("%03d %.3f" % (code, time.time() - t))
PY
    fi
  }
  is_list() {  # key: is the body a JSON object with this list in it?
    [ "$(tr -d ' \t\r\n' < "$body" 2>/dev/null | head -c 1)" = "{" ] \
      && grep -q "\"$1\"[ ]*:[ ]*\\[" "$body" 2>/dev/null
  }
  echo "#LLMMODEL port|status|ms|model"
  # Declared ones first, so the eight-server limit never drops what someone asked for.
  # The listing goes in through the environment: a newline in `awk -v` is an error in
  # some awks.
  { printf '%s\n' "$declared" | FLEET_LISTEN="$listen" awk -F'|' '
      BEGIN { n = split(ENVIRON["FLEET_LISTEN"], rows, "\n"); for (i = 1; i <= n; i++) { split(rows[i], f, "|"); at[f[3]] = f[2] } }
      $1 != "" { print (($1 in at) ? at[$1] : "127.0.0.1") "|" $1 "|" $2 }'
    printf '%s\n' "$listen" | awk -F'|' -v re="$LLM_NAMES" -v ports="$LLM_PORTS" '
      BEGIN { n = split(ports, h, " "); for (i = 1; i <= n; i++) hint[h[i]] = 1 }
      tolower($5) ~ /^(vllm|sglang)::/ && tolower($5) !~ /::(api|http)/ { next }   # engine workers
      tolower($5) ~ /^(controlce|ctrlcenter|airplay)/ { next }                 # macOS AirPlay on :5000
      tolower($5) ~ re || ($3 in hint) { print $2 "|" $3 "|" }'
  } | awk -F'|' '$2 != "" && !seen[$2]++' | head -8 | while IFS='|' read -r addr port base; do
    # Ten seconds for all of them: eight servers that took the connection and never
    # answered held the probe past its own limit, and the machine read as unreachable.
    now=$(date +%s 2>/dev/null) && { [ -n "${t0:-}" ] || t0=$now; [ $((now - t0)) -lt 10 ] || break; }
    case "$addr" in
      ""|"*"|0.0.0.0|"::"|"[::]"|127.*) host=127.0.0.1 ;;
      "::1"|"[::1]") host="[::1]" ;;      # IPv6 loopback only: 127.0.0.1 finds nothing
      *:*) host="[${addr#[}"; host="${host%]}]" ;;
      *) host=$addr ;;
    esac
    set -- $(get "http://$host:$port${base:-/v1}/models"); code=$1; secs=$2
    ids=""
    # A 200 is a model server only when the answer is a model list: a web app on
    # :8080 answers every path with its HTML page, and any JSON API has an "id" in it.
    if [ "$code" = "200" ]; then
      if is_list data; then
        ids=$(grep -o '"id"[ ]*:[ ]*"[^"]*"' "$body" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' | grep -v '^modelperm-')
      else
        code=other
      fi
    fi
    # Not again when it did not answer at all: that doubled the wait on a hung server.
    if [ -z "$ids" ] && [ -z "$base" ] && [ "$code" != "401" ] && [ "$code" != "403" ] \
       && [ "$code" != "000" ]; then
      set -- $(get "http://$host:$port/api/tags")   # ollama's own list
      if [ "$1" = "200" ] && is_list models; then
        code=200; secs=$2
        ids=$(grep -o '"name"[ ]*:[ ]*"[^"]*"' "$body" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/')
      fi
    fi
    ms=$(awk -v s="$secs" 'BEGIN{printf "%d", s * 1000}')
    case "$code" in
      200) st=ok ;; 401|403) st=auth ;; 000) st=down ;; *) st=none ;;
    esac
    if [ -n "$ids" ]; then
      # Twenty names at most, and how many there were: a server with 300 showed twenty
      # and said nothing of the rest.
      n=$(printf '%s\n' "$ids" | grep -c .)
      printf '%s\n' "$ids" | head -20 | while read -r m; do printf '%s|%s|%s|%s|%s\n' "$port" "$st" "$ms" "$(printf '%s' "$m" | clean)" "$n"; done
    else
      printf '%s|%s|%s|\n' "$port" "$st" "$ms"
    fi
  done
  rm -f "$body"
fi
echo "#END rc=0"
