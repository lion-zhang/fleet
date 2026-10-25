#!/bin/sh
# Install fleet: https://github.com/sinolion/fleet
#
#   curl -LsSf https://raw.githubusercontent.com/sinolion/fleet/main/install.sh | sh
#
# Installs the `fleet` command and teaches every supported agent installed here (Claude
# Code, Codex, Gemini CLI, ...) to use it. Install it where you work, and wherever else
# you run agents; the machines fleet looks at need nothing but SSH.
#
# Options:  --no-setup    install the command only; teach no agents
#           --force-core  install with uv even though another copy of fleet is here
# A fleet already on this machine (uv, pipx, a checkout) is kept, never replaced.
# Safe to run again: it upgrades fleet.
#
# Everything is inside main, called on the last line, so a download cut short runs
# nothing, and nothing below can read the rest of this script off a piped stdin.

set -eu

say() { printf '%s\n' "$*"; }
die() { printf 'fleet install: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
usage: install.sh [--no-setup] [--force-core]

  (nothing)     install fleet and teach every supported agent installed here to use it
  --no-setup    install the command only
  --force-core  install fleet with uv even though another copy is already here

fleet itself (the core) is installed once per machine and shared by every agent. If
it is already here -- from uv, pipx, or your own checkout -- it is kept, and this only
teaches your agents (an older uv-installed copy is upgraded in place).

fleet manages no access: it reaches your machines exactly as your own ssh does.
USAGE
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
    # And over a tool from before the rename: it holds the `fleet` command until it is
    # uninstalled, which happens only once this install has worked.
    force=""; case "$kind" in force|old) force="--force" ;; esac
    if [ -n "${FLEET_SOURCE:-}" ]; then
        "$uv" tool install --upgrade $force --quiet "$source" </dev/null || die "could not install $source"
    else
        errs=$(mktemp)
        if ! "$uv" tool install --upgrade $force --quiet "$source" </dev/null 2>"$errs"; then
            # Only when the package is not there to install. Any other failure -- a
            # download that broke, another program already called `fleet` -- used to
            # become a forced install of the repository's main branch over it.
            if grep -qiE 'not found in the (package )?registry|no solution found|404' "$errs"; then
                say "  (not on PyPI from here; installing from GitHub)"
                "$uv" tool install $force --quiet "git+https://github.com/sinolion/fleet" </dev/null \
                    || { rm -f "$errs"; die "could not install fleet"; }
            else
                cat "$errs" >&2; rm -f "$errs"
                die "could not install fleet"
            fi
        fi
        rm -f "$errs"
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
    setup=1
    while [ $# -gt 0 ]; do
        case "$1" in
            # fleet 0.5 joined a fleet with a code. There is no fleet to join any more:
            # every machine running fleet looks at what its own ssh reaches.
            --join) [ $# -ge 2 ] && shift; shift
                    say "note: --join is no longer needed -- fleet has no center to join; ignoring it" ;;
            --join=*) shift
                    say "note: --join is no longer needed -- fleet has no center to join; ignoring it" ;;
            --no-setup) setup=0; shift ;;
            --force-core) FLEET_FORCE_CORE=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
    done
    if [ -n "${FLEET_JOIN:-}" ]; then
        say "note: FLEET_JOIN is no longer needed -- fleet has no center to join; ignoring it"
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
            # fleet 0.5 ran a listening service on a fleet's center, from the files about
            # to be replaced. 0.6 has neither, so the fleet that installed it takes it
            # off -- the new one has no `fleet service` to do it with.
            if [ -n "$found" ] && [ -x "$found" ]; then
                "$found" service remove </dev/null >/dev/null 2>&1 || true
            fi
            install_core "$uv" "$kind"
            # Machines from before a rename carry it as `fleet-broker` or `agent-fleet`;
            # two tools must not both claim the `fleet` command. Removed only now that
            # the new one is in: uninstalled first, a failed install left the machine
            # with no fleet at all. Removing it takes the `fleet` command with it --
            # uv deletes what it once wrote there, whoever owns it now -- so the new
            # tool's is put back.
            if [ "$kind" = old ]; then
                "$uv" tool uninstall fleet-broker >/dev/null 2>&1 </dev/null || true
                "$uv" tool uninstall agent-fleet >/dev/null 2>&1 </dev/null || true
                [ -x "$("$uv" tool dir --bin 2>/dev/null </dev/null)/fleet" ] || install_core "$uv" old
            fi
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

    # 3. Every agent here taught to use it. Exits non-zero when no agent is installed
    #    yet, which is no failure of the install.
    if [ "$setup" = 1 ]; then
        "$fleet" setup </dev/null || true
    fi

    say ""
    [ "$on_path" = 1 ] || say "open a new terminal (or run: export PATH=\"$bin:\$PATH\") so \`fleet\` is found"
    if [ "$setup" = 1 ]; then
        say "next: add your machines, then ask for what you need. Tell your agent, e.g.:"
        say "        \"add my GPU server: ssh user@host\"     a machine you can SSH into"
        say "        \"find my machines\"                     from your ssh config and tailnet"
        say "        \"what's free right now?\"               every machine, and what is free"
        say "        \"run train.py where a 24 GB card is free\""
        say "      or yourself: fleet add \"ssh user@host\", fleet ls --discover, fleet ls"
        say "agents load fleet's skill when a session starts: start a new one to use it"
    else
        say "next:  fleet setup         teach your agents to use fleet"
    fi
}

main "$@"
