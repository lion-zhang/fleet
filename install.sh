#!/bin/sh
# Install fleet: https://github.com/lion-zhang/fleet
#
# Two modes. The center is the one machine that decides who may reach what -- install
# it on the machine you work from. Every other machine is a member.
#
#   curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh
#       center (the default): installs fleet, starts a new fleet here, and teaches
#       every coding agent installed here (Claude Code, Codex, Gemini CLI, ...) to use it.
#
#   curl -LsSf https://raw.githubusercontent.com/lion-zhang/fleet/main/install.sh | sh -s -- --join CODE
#       member: installs fleet and joins the fleet whose center printed CODE
#       (`fleet invite` prints this whole line), instead of starting one here.
#
# Options:  --join CODE   join that fleet as a member (or set FLEET_JOIN=CODE)
#           --no-setup    install the command only; start or join nothing
# Safe to run again: it upgrades fleet and leaves the fleet this machine is in alone.
#
# Everything is inside main, called on the last line, so a download cut short runs
# nothing, and nothing below can read the rest of this script off a piped stdin.

set -eu

say() { printf '%s\n' "$*"; }
die() { printf 'fleet install: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
usage: install.sh [--join CODE] [--no-setup]

Two modes: the center decides who may reach what; every other machine is a member.

  (nothing)     center: install fleet, start a new fleet on this machine, and teach
                every coding agent installed here to use it
  --join CODE   member: install fleet and join the fleet whose center printed CODE
                (`fleet invite`); FLEET_JOIN=CODE does the same
  --no-setup    install the command only; start or join nothing

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

    # 2. fleet. Machines from before the rename carry it as `fleet-broker`; two tools
    #    must not both claim the `fleet` command.
    if "$uv" tool list 2>/dev/null </dev/null | grep -q '^fleet-broker '; then
        "$uv" tool uninstall fleet-broker >/dev/null 2>&1 </dev/null || true
    fi
    source="${FLEET_SOURCE:-agent-fleet}"
    say "installing fleet ..."
    if [ -n "${FLEET_SOURCE:-}" ]; then
        "$uv" tool install --upgrade --quiet "$source" </dev/null || die "could not install $source"
    elif ! "$uv" tool install --upgrade --quiet "$source" </dev/null 2>/dev/null; then
        # Not on PyPI yet, or PyPI is unreachable from here: straight from the repo.
        say "  (not on PyPI from here; installing from GitHub)"
        "$uv" tool install --force --quiet "git+https://github.com/lion-zhang/fleet" </dev/null \
            || die "could not install fleet"
    fi
    bin=$("$uv" tool dir --bin 2>/dev/null </dev/null || printf '%s' "$HOME/.local/bin")
    fleet="$bin/fleet"
    [ -x "$fleet" ] || die "fleet installed, but $fleet is missing"
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
        say "next:  fleet show          this machine, as your agents will see it"
        say "       fleet invite NAME   prints one line to run on another machine to add it"
        say "       fleet add NAME --ssh \"ssh user@host\"   or have this machine reach it"
    elif [ "$setup" = 1 ]; then
        say "next:  fleet ls            the machines in this fleet"
    else
        say "next:  fleet setup         start a fleet here and teach your agents (or: fleet join CODE)"
    fi
}

main "$@"
