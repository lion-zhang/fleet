#!/bin/sh
# fleet probe payload -- POSIX sh, no bashisms, no external deps beyond coreutils/busybox.
# Emits line-oriented key=value with #SECTION markers and a mandatory "#END rc=" sentinel.
# Absence of #END means the probe was truncated or killed -- never a successful parse.
#
# Env in:  FLEET_MODE=full|shared   (shared = polite: no process enumeration)
# Never exits non-zero for "thing not present"; that is normal and reported as empty.

FLEET_MODE=${FLEET_MODE:-full}
OS=$(uname -s 2>/dev/null || echo unknown)

emit() { printf '%s=%s\n' "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }
# strip newlines/pipes so a value can never break the line protocol
clean() { tr -d '\n\r|' 2>/dev/null | cut -c1-300; }

echo "#FLEET v1"
emit probe.mode "$FLEET_MODE"

# ---------------------------------------------------------------- host identity
hostname_v=$( (hostname 2>/dev/null || uname -n 2>/dev/null || cat /etc/hostname 2>/dev/null) | clean )
emit host.hostname "$hostname_v"
emit host.uname_s "$OS"
emit host.arch "$(uname -m 2>/dev/null | clean)"
emit host.kernel "$(uname -r 2>/dev/null | clean)"

machine_id=""
if [ -r /etc/machine-id ]; then machine_id=$(cat /etc/machine-id 2>/dev/null | clean)
elif [ -r /var/lib/dbus/machine-id ]; then machine_id=$(cat /var/lib/dbus/machine-id 2>/dev/null | clean)
elif [ "$OS" = "Darwin" ] && have ioreg; then
  machine_id=$(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null \
    | awk -F'"' '/IOPlatformUUID/{print $4; exit}' | clean)
fi
emit host.machine_id "$machine_id"

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

# ---------------------------------------------------------------- disk
echo "#DISK mount|total_kb|used_kb|avail_kb"
if have df; then
  df -Pk 2>/dev/null | awk 'NR>1 && $2+0>0 {
    m=$6
    if (m=="/" || m ~ /^\/(workspace|data|mnt|home|srv|opt|Volumes|scratch)/)
      printf "%s|%s|%s|%s\n", m, $2, $3, $4
  }' | sort -u | head -20
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
    echo "#GPUPROC gpu_uuid|pid|vram_mib|user|etimes|comm"
    nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits 2>/dev/null \
      | sed 's/, */|/g' | while IFS='|' read -r guuid gpid gmem; do
          [ -n "$gpid" ] || continue
          # never trust nvidia-smi's process_name: for GUI apps it is ~1KB of flags.
          info=$(ps -o user=,etimes=,comm= -p "$gpid" 2>/dev/null | head -1 \
                 | awk '{u=$1; e=$2; $1=""; $2=""; sub(/^ +/,""); printf "%s|%s|%s", u, e, $0}')
          [ -n "$info" ] || info="||"
          printf '%s|%s|%s|%s\n' "$guuid" "$gpid" "$gmem" "$info"
        done
  fi

  echo "#CPUPROC pid|user|pcpu|rss_kb|etimes|comm"
  if [ "$OS" = "Darwin" ]; then
    ps -axo pid=,user=,pcpu=,rss=,etime=,comm= 2>/dev/null | sort -k3 -rn | head -10 \
      | awk '{p=$1;u=$2;c=$3;r=$4;e=$5;$1="";$2="";$3="";$4="";$5="";sub(/^ +/,"");
              n=split($0,a,"/"); printf "%s|%s|%s|%s|%s|%s\n",p,u,c,r,e,a[n]}'
  else
    ps -eo pid=,user=,pcpu=,rss=,etimes=,comm= 2>/dev/null | sort -k3 -rn | head -10 \
      | awk '$3+0>1.0 {printf "%s|%s|%s|%s|%s|%s\n",$1,$2,$3,$4,$5,$6}'
  fi
fi

# ---------------------------------------------------------------- listening services
echo "#LISTEN proto|addr|port|pid|comm"
if [ "$OS" = "Darwin" ] && have lsof; then
  # macOS: netstat exists but has no -tlnp; lsof is the only reliable source.
  lsof +c 0 -nP -iTCP -sTCP:LISTEN 2>/dev/null | tail -n +2 \
    | awk '{n=split($9,a,":"); printf "LISTEN 0 0 %s:%s x users:((\"%s\",pid=%s,fd=0))\n", (n>1?a[n-1]:"*"), a[n], $1, $2}'
elif have ss; then
  ss -tlnH 2>/dev/null || ss -tln 2>/dev/null | tail -n +2
elif have netstat; then
  netstat -tlnp 2>/dev/null | tail -n +3 || netstat -tln 2>/dev/null | tail -n +3
elif have lsof; then
  lsof +c 0 -nP -iTCP -sTCP:LISTEN 2>/dev/null | tail -n +2 \
    | awk '{n=split($9,a,":"); printf "LISTEN 0 0 %s:%s x users:((\"%s\",pid=%s,fd=0))\n", (n>1?a[n-1]:"*"), a[n], $1, $2}'
fi 2>/dev/null | awk '
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
  }' | sort -u -t'|' -k3,3n | head -40

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

echo "#END rc=0"
