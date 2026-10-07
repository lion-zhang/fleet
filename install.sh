#!/bin/sh
# Install fleet: https://github.com/lion-zhang/fleet
#
# Two modes. The center is the one machine that decides who may reach what -- install
# it on the machine you work from. Every other machine is a member.
#
#   curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh
#       center (the default): installs fleet, starts a new fleet here, and teaches
#       every supported agent installed here (Claude Code, Codex, Gemini CLI, ...) to use it.
#
#   curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh -s -- --join CODE
#       member: installs fleet and joins the fleet whose center printed CODE
#       (`fleet invite` prints this whole line), instead of starting one here.
#
# Options:  --join CODE   join that fleet as a member (or set FLEET_JOIN=CODE)
#           --no-setup    install the command only; start or join nothing
#           --force-core  install with uv even though another copy of fleet is here
# A fleet already on this machine (uv, pipx, a checkout) is kept, never replaced.
# Safe to run again: it upgrades fleet and leaves the fleet this machine is in alone.
#
# Everything is inside main, called on the last line, so a download cut short runs
# nothing, and nothing below can read the rest of this script off a piped stdin.

set -eu

say() { printf '%s\n' "$*"; }
die() { printf 'fleet install: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
usage: install.sh [--join CODE] [--no-setup] [--force-core]

Two modes: the center decides who may reach what; every other machine is a member.

  (nothing)     center: install fleet, start a new fleet on this machine, and teach
                every supported agent installed here to use it
  --join CODE   member: install fleet and join the fleet whose center printed CODE
                (`fleet invite`); FLEET_JOIN=CODE does the same
  --no-setup    install the command only; start or join nothing
  --force-core  install fleet with uv even though another copy is already here

fleet itself (the core) is installed once per machine and shared by every agent. If
it is already here -- from uv, pipx, or your own checkout -- it is kept, and this only
teaches your agents (an older uv-installed copy is upgraded in place).

Safe to run again: it upgrades fleet and leaves the fleet this machine is in alone.
USAGE
}

# What an installed fleet says this machine is: center, member, or nothing yet. Never
# starts a fleet to answer. Reads `role`, and the keys fleets before it had.
role_of() {
    out=$(FLEET_NO_AUTO_CENTER=1 "$1" center --json </dev/null 2>/dev/null || true)
    case "$out" in
        *'"role": "center"'*|*'"is_center": true'*) echo center ;;
        *'"role": "member"'*|*'"member": true'*) echo member ;;
    esac
}

# One string field from `fleet center --json`.
field_of() {
    FLEET_NO_AUTO_CENTER=1 "$1" center --json </dev/null 2>/dev/null \
        | sed -n "s/.*\"$2\": *\"\([^\"]*\)\".*/\1/p" | head -n 1
}

# Which core is here already, as "kind|path". kind is one of:
#   none    no fleet on this machine
#   old     a uv tool under an earlier name (fleet-broker, or agent-fleet from git)
#   uv      agents-fleet as a uv tool, from PyPI, a wheel or git: upgradable in place
#   source  agents-fleet (or agent-fleet) as a uv tool from a checkout
#   pipx    agents-fleet (or agent-fleet) installed with pipx
#   other   some other `fleet` first on PATH (pip, a virtualenv, a package manager)
detect_core() {
    uv=$1
    tools=$("$uv" tool list 2>/dev/null </dev/null || true)
    tooldir=$("$uv" tool dir 2>/dev/null </dev/null || true)
    # The package was agent-fleet until PyPI refused that name; it was never released
    # under it, so an agent-fleet tool came from git or a checkout.
    for name in agents-fleet agent-fleet; do
        printf '%s\n' "$tools" | grep -q "^$name " || continue
        receipt="$tooldir/$name/uv-receipt.toml"
        bin=$("$uv" tool dir --bin 2>/dev/null </dev/null)
        if grep -qE '(editable|directory) = ' "$receipt" 2>/dev/null; then
            echo "source|$bin/fleet"
        elif [ "$name" = agents-fleet ]; then
            echo "uv|$bin/fleet"
        else
            echo "old|"
        fi
        return
    done
    if printf '%s\n' "$tools" | grep -q '^fleet-broker '; then
        echo "old|"
        return
    fi
    if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null </dev/null | grep -qE '^agents?-fleet '; then
        echo "pipx|$(command -v fleet 2>/dev/null || echo "$HOME/.local/bin/fleet")"
        return
    fi
    for f in "$(command -v fleet 2>/dev/null || true)" "$HOME/.local/bin/fleet"; do
        if [ -n "$f" ] && [ -x "$f" ] && "$f" --version </dev/null 2>/dev/null | grep -q '^fleet '; then
            echo "other|$f"
            return
        fi
    done
    echo "none|"
}

# Install or upgrade the core with uv. An agents-fleet uv tool is upgraded in place --
# `uv tool upgrade` never downgrades and leaves fleet's state alone.
install_core() {
    uv=$1; kind=$2
    source="${FLEET_SOURCE:-agents-fleet}"
    if [ "$kind" = uv ] && [ -z "${FLEET_SOURCE:-}" ]; then
        say "core: $("$("$uv" tool dir --bin)/fleet" --version </dev/null 2>/dev/null), upgrading if a newer one exists"
        "$uv" tool upgrade --quiet agents-fleet </dev/null || die "could not upgrade fleet"
        return
    fi
    say "installing fleet ..."
    # --force-core asked for this copy to take over the `fleet` command from another.
    force=""; [ "$kind" = force ] && force="--force"
    if [ -n "${FLEET_SOURCE:-}" ]; then
        "$uv" tool install --upgrade $force --quiet "$source" </dev/null || die "could not install $source"
    elif ! "$uv" tool install --upgrade $force --quiet "$source" </dev/null 2>/dev/null; then
        # Not on PyPI yet, or PyPI is unreachable from here: straight from the repo.
        say "  (not on PyPI from here; installing from GitHub)"
        "$uv" tool install --force --quiet "git+https://github.com/lion-zhang/fleet" </dev/null \
            || die "could not install fleet"
    fi
}

find_uv() {
    for c in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if command -v "$c" >/dev/null 2>&1; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

main() {
    join="${FLEET_JOIN:-}"
    setup=1
    while [ $# -gt 0 ]; do
        case "$1" in
            --join) [ $# -ge 2 ] || die "--join needs the code \`fleet invite\` printed"
                    join="$2"; shift 2 ;;
            --join=*) join="${1#--join=}"; shift ;;
            --no-setup) setup=0; shift ;;
            --force-core) FLEET_FORCE_CORE=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
    done
    case "$join" in
        ""|fleet1:*) ;;
        *) die "that is not a fleet invite code -- it starts with fleet1: (run \`fleet invite\` on the center)" ;;
    esac

    # Say which mode this run installs, before doing anything. A re-run reads it from the
    # fleet already here, which may have been installed in the other mode.
    was=""
    for f in "$(command -v fleet 2>/dev/null || true)" "$HOME/.local/bin/fleet"; do
        if [ -n "$f" ] && [ -x "$f" ]; then was=$(role_of "$f"); break; fi
    done
    if [ "$setup" = 0 ]; then
        say "mode: none -- installing the command only"
    elif [ -n "$join" ]; then
        say "mode: member -- joining the fleet whose center printed this code"
    elif [ "$was" = center ]; then
        say "mode: center (already; upgrading)"
    elif [ "$was" = member ]; then
        say "mode: member (already; upgrading)"
    else
        say "mode: center -- starting a new fleet on this machine"
    fi

    # 1. uv, which installs and runs fleet. Its own installer, unless it is here already.
    if ! uv=$(find_uv); then
        say "installing uv (https://docs.astral.sh/uv) ..."
        command -v curl >/dev/null 2>&1 || die "curl is needed to install uv"
        curl -LsSf https://astral.sh/uv/install.sh </dev/null | sh >/dev/null
        uv=$(find_uv) || die "uv did not install -- see https://docs.astral.sh/uv/getting-started/installation/"
    fi

    # 2. The core: the `fleet` command and its state. Installed once per machine and
    #    shared by every agent on it, so installing fleet "for another agent" must find
    #    the one already here and keep it -- never put a second copy beside it, and
    #    never replace someone's development install with a release.
    core=$(detect_core "$uv")
    kind=${core%%|*}; found=${core#*|}
    case "$kind" in
        none|old|uv) ;;
        *) [ -n "${FLEET_FORCE_CORE:-}" ] && kind=force ;;
    esac
    case "$kind" in
        source)
            say "core: $("$found" --version </dev/null 2>/dev/null) -- your source install ($found), keeping it" ;;
        pipx)
            say "core: $("$found" --version </dev/null 2>/dev/null) installed with pipx, keeping it (to upgrade: pipx upgrade agents-fleet)" ;;
        other)
            say "core: $("$found" --version </dev/null 2>/dev/null) at $found, keeping it" ;;
        *)
            # Machines from before a rename carry it as `fleet-broker` or `agent-fleet`;
            # two tools must not both claim the `fleet` command.
            if [ "$kind" = old ]; then
                "$uv" tool uninstall fleet-broker >/dev/null 2>&1 </dev/null || true
                "$uv" tool uninstall agent-fleet >/dev/null 2>&1 </dev/null || true
            fi
            install_core "$uv" "$kind"
            found="" ;;
    esac
    if [ -n "$found" ]; then
        fleet=$found; bin=$(dirname "$found")
    else
        bin=$("$uv" tool dir --bin 2>/dev/null </dev/null || printf '%s' "$HOME/.local/bin")
        fleet="$bin/fleet"
        [ -x "$fleet" ] || die "fleet installed, but $fleet is missing"
    fi
    # New shells find `fleet` by name. uv asks $SHELL which profile to write, and gives
    # up when it is unset (cron, containers, some ssh sessions): ask the account instead.
    login_shell=$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f7 || true)
    for sh_ in "${SHELL:-}" "$login_shell" /bin/bash; do
        [ -n "$sh_" ] || continue
        if SHELL="$sh_" "$uv" tool update-shell >/dev/null 2>&1 </dev/null; then break; fi
    done
    # And this script's own children: `fleet setup` writes the short name into skills
    # only when it can find it.
    case ":$PATH:" in *":$bin:"*) on_path=1 ;; *) on_path=0; PATH="$bin:$PATH"; export PATH ;; esac
    say "✓ $("$fleet" --version </dev/null)"

    # 3. Into a fleet, and every agent here taught to use it.
    if [ "$setup" = 1 ]; then
        if [ -n "$join" ]; then
            FLEET_NO_AUTO_CENTER=1 "$fleet" join "$join" </dev/null
        fi
        # On a machine in no fleet this also starts one, with this machine as its center.
        # Exits non-zero when no agent is installed yet, which is no failure of the install.
        "$fleet" setup </dev/null || true
    fi

    say ""
    [ "$on_path" = 1 ] || say "open a new terminal (or run: export PATH=\"$bin:\$PATH\") so \`fleet\` is found"
    role=""
    if [ "$setup" = 1 ]; then
        # Center or member: by what this machine is now, not by how this run began.
        role=$(role_of "$fleet")
        id=$(field_of "$fleet" fleet_id)
        if [ "$role" = center ]; then
            say "✓ this machine is the center of fleet $id"
        elif [ "$role" = member ]; then
            say "✓ this machine is a member of fleet $id; its center is $(field_of "$fleet" center)"
        fi
    fi
    if [ "$setup" = 1 ] && [ "$role" = center ]; then
        say "next: add your machines, then ask for what you need. Tell your agent, e.g.:"
        say "        \"add my GPU server: ssh user@host\"     a machine you can SSH into"
        say "        \"invite my laptop\"                     one line to paste on a machine"
        say "        \"what's free right now?\"               every machine, and what is free"
        say "        \"run train.py where a 24 GB card is free\""
        say "      or yourself: fleet add \"ssh user@host\", fleet invite NAME, fleet ls"
    elif [ "$setup" = 1 ]; then
        say "next:  fleet ls            the machines in this fleet"
    else
        say "next:  fleet setup         start a fleet here and teach your agents (or: fleet join CODE)"
    fi
    if [ "$setup" = 1 ]; then
        say "agents load fleet's skill when a session starts: start a new one to use it"
    fi
}

main "$@"
