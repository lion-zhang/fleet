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
device_id=""
for f in "$HOME/.config/fleet/device-id" "$HOME/Library/Application Support/fleet/device-id"; do
  if [ -r "$f" ]; then device_id=$(head -n 1 "$f" 2>/dev/null | clean); break; fi
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
emit host.vast_label "$( [ -r /etc/vast_containerlabel ] && cat /etc/vast_containerlabel 2>/dev/null | clean )"
emit host.runpod_id "$(printf '%s' "${RUNPOD_POD_ID:-}" | clean)"
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
gpu_present=0; gpu_driver=""; gpu_err=""
if have nvidia-smi; then
  gpu_driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | clean)
  if [ -n "$gpu_driver" ]; then gpu_present=1; else
    gpu_present=err
    gpu_err=$(nvidia-smi 2>&1 >/dev/null | head -1 | clean)
  fi
fi
emit gpu.present "$gpu_present"
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
[ -n "$notes_sys" ] && emit notes.system "$(head -c 8192 "$notes_sys" 2>/dev/null | b64)"
[ -n "$notes_user" ] && emit notes.user "$(head -c 8192 "$notes_user" 2>/dev/null | b64)"

# The `paths:` block of those files, as kind|path lines. Reads the shapes YAML allows for
# a list of strings -- `models: /a`, `models: [/a, /b]`, and `models:` then `- /a` -- which
# is everything `fleet edit --path` writes and what people write by hand.
store_paths() {
  for f in "$notes_sys" "$notes_user"; do
    [ -n "$f" ] || continue
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
      }' "$f" 2>/dev/null
  done | while IFS='|' read -r k p; do
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
writable() {
  [ "$1" = "/" ] && { echo 1; return; }
  if [ -w "$1" ]; then echo 1; else echo 0; fi
}

# ---------------------------------------------------------------- disk
echo "#DISK mount|total_kb|used_kb|avail_kb|rw"
if have df; then
  if [ -n "${FLEET_DISK_PATHS:-}" ]; then
    # Configured paths win outright. df is asked about each path directly, so a plain
    # directory works as well as a mount point, and the row is labelled with the path
    # the user asked about rather than whatever filesystem happens to hold it.
    for p in $FLEET_DISK_PATHS; do
      [ -e "$p" ] || continue
      rw=$(writable "$p")
      df -Pk "$p" 2>/dev/null | awk -v p="$p" -v rw="$rw" 'NR>1 && $2+0>0 {
        printf "%s|%s|%s|%s|%s\n", p, $2, $3, $4, rw
      }'
    done | head -20
  else
    # writability is decided in the shell, not awk: a mount you cannot write to cannot
    # fill up, so "100% full" there is never actionable -- a mounted disk image is the
    # usual case, and it alerted forever.
    df -Pk 2>/dev/null | awk 'NR>1 && $2+0>0 {
      m=$6
      if (m=="/" || m ~ /^\/(workspace|data|mnt|home|srv|opt|Volumes|scratch)/)
        printf "%s|%s|%s|%s\n", m, $2, $3, $4
    }' | sort -u | head -20 | while IFS='|' read -r m tot used avail; do
      rw=$(writable "$m")
      printf '%s|%s|%s|%s|%s\n' "$m" "$tot" "$used" "$avail" "$rw"
    done
  fi
fi

if [ "$gpu_present" = "1" ]; then
  echo "#GPU idx|uuid|name|total_mib|used_mib|free_mib|util_pct|temp_c|power_w"
  nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw \
    --format=csv,noheader,nounits 2>/dev/null \
    | sed 's/, */|/g' | sed 's/ *|/|/g; s/| */|/g'
fi

# ---------------------------------------------------------------- processes
# Skipped entirely in shared mode: enumerating other users' work on a machine you do
# not own is both rude and a privacy problem.
if [ "$FLEET_MODE" != "shared" ]; then
  if [ "$gpu_present" = "1" ]; then
    echo "#GPUPROC gpu_uuid|pid|vram_mib|user|etimes|comm|args"
    nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>/dev/null \
      | sed 's/, */|/g' | while IFS='|' read -r guuid gpid gmem; do
          [ -n "$gpid" ] || continue
          # never trust nvidia-smi's process_name: for GUI apps it is ~1KB of flags.
          info=$(ps -o user=,etimes=,comm= -p "$gpid" 2>/dev/null | head -1 \
                 | awk '{u=$1; e=$2; $1=""; $2=""; sub(/^ +/,""); printf "%s|%s|%s", u, e, $0}')
          [ -n "$info" ] || info="||"
          # The command line says what the job is ("python train.py"), where comm only
          # says "python". The parser redacts anything in it that looks like a secret.
          args=$(ps -o args= -p "$gpid" 2>/dev/null | head -1 | clean | cut -c1-200)
          printf '%s|%s|%s|%s|%s\n' "$guuid" "$gpid" "$gmem" "$info" "$args"
        done
  fi

  echo "#CPUPROC pid|user|pcpu|rss_kb|etimes|comm"
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
      | awk -v me="$$" '$1 != me && $2 != me {
              p=$1;u=$3;c=$4;r=$5;e=$6;
              $1="";$2="";$3="";$4="";$5="";$6="";sub(/^ +/,"");
              n=split($0,a,"/"); printf "%s|%s|%s|%s|%s|%s\n",p,u,c,r,e,a[n]}' \
      | sort -t'|' -k3 -rn | head -10
  else
    ps -eo pid=,ppid=,user=,pcpu=,rss=,etimes=,comm= 2>/dev/null \
      | awk -v me="$$" '$1 != me && $2 != me && $4+0>1.0 && $6+0>=1 {
              printf "%s|%s|%s|%s|%s|%s\n",$1,$3,$4,$5,$6,$7}' \
      | sort -t'|' -k3 -rn | head -10
  fi
fi

# ---------------------------------------------------------------- listening services
listen=$( {
if [ "$OS" = "Darwin" ] && have lsof; then
  # macOS: netstat exists but has no -tlnp; lsof is the only reliable source.
  lsof +c 0 -nP -iTCP -sTCP:LISTEN 2>/dev/null | tail -n +2 \
    | awk '{n=split($9,a,":"); printf "LISTEN 0 0 %s:%s x users:((\"%s\",pid=%s,fd=0))\n", (n>1?a[n-1]:"*"), a[n], $1, $2}'
elif have ss; then
  # -p names the program behind each port -- every process we may see, which is ours
  # and, as root, everyone's. Without it a vLLM server was an anonymous port 8000.
  ss -tlnpH 2>/dev/null || ss -tlnH 2>/dev/null || ss -tln 2>/dev/null | tail -n +2
elif have netstat; then
  # The exit status of `netstat`, not of `tail`: piped, the fallback never ran.
  ns=$(netstat -tlnp 2>/dev/null) || ns=$(netstat -tln 2>/dev/null)
  printf '%s\n' "$ns" | tail -n +3
elif have lsof; then
  lsof +c 0 -nP -iTCP -sTCP:LISTEN 2>/dev/null | tail -n +2 \
    | awk '{n=split($9,a,":"); printf "LISTEN 0 0 %s:%s x users:((\"%s\",pid=%s,fd=0))\n", (n>1?a[n-1]:"*"), a[n], $1, $2}'
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
  }' | sort -u -t'|' -k3,3n | head -40 )
echo "#LISTEN proto|addr|port|pid|comm"
[ -n "$listen" ] && printf '%s\n' "$listen"

# ---------------------------------------------------------------- SLURM (shared boxes)
if [ "$FLEET_MODE" = "shared" ] && have squeue; then
  echo "#SLURM key|value"
  printf 'my_jobs|%s\n' "$(squeue -h -u "$(id -un)" 2>/dev/null | grep -c .)"
  printf 'my_running|%s\n' "$(squeue -h -u "$(id -un)" -t RUNNING 2>/dev/null | grep -c .)"
  if have sinfo; then
    sinfo -h -o '%P %a %D %t' 2>/dev/null | head -12 | while read -r line; do
      printf 'partition|%s\n' "$line"
    done
  fi
fi

# Some answers are slow (how big is a folder of models) or rarely change (which torch is
# installed), so they are worked out now and then and kept here, on the machine. Every
# viewer then pays only for reading a file, and `fleet top` never pays at all.
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/fleet"
mkdir -p "$CACHE" 2>/dev/null || CACHE=""
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
  want=$(printf '%s\n' "$paths" | cut -d'|' -f2 | sort -u | tr '\n' ' ')
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
        echo "#v1 $NOW $want" > "$tmp"
        for p in $want; do
          [ -e "$p" ] || continue
          kb=$($T du -sk "$p" 2>/dev/null | tail -1 | cut -f1)
          [ -n "$kb" ] && printf '%s|%s\n' "$p" "$kb" >> "$tmp"
        done
        mv -f "$tmp" "$sizes"
      ) </dev/null >/dev/null 2>&1 &
    fi
  fi
fi
echo "#STORE kind|path|exists|avail_kb|size_kb|sized_at"
if [ -n "$paths" ]; then
  # Read once: the background `du` may replace the file while this loop runs.
  known=$(cat "$sizes" 2>/dev/null)
  sized_at=$(printf '%s\n' "$known" | head -1 | awk '{print $2}')
  printf '%s\n' "$paths" | while IFS='|' read -r k p; do
    if [ -e "$p" ]; then
      ex=1
      av=$(df -Pk "$p" 2>/dev/null | awk 'NR==2{print $4}')
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
    }'
}
env_roots() {
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
  printf '%s\n' "$paths" | awk -F'|' '$1=="envs"{print $2}'
}
software_scan() {
  echo "#SOFTWARE key|value"
  printf 'scanned_at|%s\n' "$NOW"
  if [ "$gpu_present" = "1" ]; then
    printf 'cuda|%s\n' "$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1)"
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
  } | awk '!seen[$0]++' | head -200
}
sw="$CACHE/software"
if [ -n "$CACHE" ] && [ "${FLEET_REFRESH:-}" != "1" ] && age_ok "$sw" 21600 \
     && [ "$(sed -n '1s/^#v1 [0-9]* //p' "$sw")" = "$gpu_driver" ]; then
  tail -n +2 "$sw"
elif [ "${FLEET_LIGHT:-}" = "1" ]; then
  [ -r "$sw" ] && tail -n +2 "$sw"       # stale is better than slow in `fleet top`
elif [ -n "$CACHE" ]; then
  { echo "#v1 $NOW $gpu_driver"; software_scan; } > "$sw.$$" 2>/dev/null && mv -f "$sw.$$" "$sw"
  tail -n +2 "$sw"
else
  software_scan
fi

# ---------------------------------------------------------------- containers
if [ "${FLEET_LIGHT:-}" != "1" ] && have docker; then
  T=; timeout -s KILL 5 true >/dev/null 2>&1 && T="timeout -s KILL 3"
  echo "#CONTAINER image|name|status|ports"
  $T docker ps --format '{{.Image}}|{{.Names}}|{{.Status}}|{{.Ports}}' 2>/dev/null \
    | tr -d '\r' | cut -c1-300 | head -20
fi

# ---------------------------------------------------------------- LLM servers
# A port that looks like a model server is asked what it serves -- here, on the machine,
# so a server that only listens on localhost answers too. Two seconds each, eight at most.
if [ "${FLEET_LIGHT:-}" != "1" ] && { [ -n "$listen" ] || [ -n "$declared" ]; } \
     && { have curl || have wget || have_python; }; then
  body=$(mktemp 2>/dev/null || echo "/tmp/.fleet-llm.$$")
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
  echo "#LLMMODEL port|status|ms|model"
  # Declared ones first, so the eight-server limit never drops what someone asked for.
  # The listing goes in through the environment: a newline in `awk -v` is an error in
  # some awks.
  { printf '%s\n' "$declared" | FLEET_LISTEN="$listen" awk -F'|' '
      BEGIN { n = split(ENVIRON["FLEET_LISTEN"], rows, "\n"); for (i = 1; i <= n; i++) { split(rows[i], f, "|"); at[f[3]] = f[2] } }
      $1 != "" { print (($1 in at) ? at[$1] : "127.0.0.1") "|" $1 "|" $2 }'
    printf '%s\n' "$listen" | awk -F'|' '
      BEGIN { split("8000 8001 8010 8080 11434 1234 5000 30000", h, " "); for (i in h) hint[h[i]] = 1 }
      tolower($5) ~ /vllm|sglang|ollama|mlx|llama|lmstudio|lms|text-gen|omlx|kobold|tabby|aphrodite|litellm|local-ai|exllama/ || ($3 in hint) { print $2 "|" $3 "|" }'
  } | awk -F'|' '$2 != "" && !seen[$2]++' | head -8 | while IFS='|' read -r addr port base; do
    case "$addr" in
      ""|"*"|0.0.0.0|"::"|"[::]"|127.*|"::1"|"[::1]") host=127.0.0.1 ;;
      *:*) host="[${addr#[}"; host="${host%]}]" ;;
      *) host=$addr ;;
    esac
    set -- $(get "http://$host:$port${base:-/v1}/models"); code=$1; secs=$2
    ids=""
    if [ "$code" = "200" ]; then
      ids=$(grep -o '"id"[ ]*:[ ]*"[^"]*"' "$body" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/' | grep -v '^modelperm-')
    fi
    if [ -z "$ids" ] && [ -z "$base" ] && [ "$code" != "401" ] && [ "$code" != "403" ]; then
      set -- $(get "http://$host:$port/api/tags")   # ollama's own list
      if [ "$1" = "200" ]; then
        code=200; secs=$2
        ids=$(grep -o '"name"[ ]*:[ ]*"[^"]*"' "$body" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/')
      fi
    fi
    ms=$(awk -v s="$secs" 'BEGIN{printf "%d", s * 1000}')
    case "$code" in
      200) st=ok ;; 401|403) st=auth ;; 000) st=down ;; *) st=none ;;
    esac
    if [ -n "$ids" ]; then
      printf '%s\n' "$ids" | head -20 | while read -r m; do printf '%s|%s|%s|%s\n' "$port" "$st" "$ms" "$(printf '%s' "$m" | clean)"; done
    else
      printf '%s|%s|%s|\n' "$port" "$st" "$ms"
    fi
  done
  rm -f "$body"
fi
echo "#END rc=0"
