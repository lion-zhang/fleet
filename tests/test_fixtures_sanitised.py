"""Guard against committing real host identifiers.

Fixtures are captured from live machines with `fleet probe --raw`, so the natural
workflow drops real data into the repo. These checks are deliberately pattern-based
rather than a denylist of actual values -- hardcoding the real identifiers here would
put them in the repo, which is the thing we are preventing.
"""

from __future__ import annotations

import pathlib
import re

import pytest

FIX = pathlib.Path(__file__).parent / "fixtures" / "probe"
FIXTURES = sorted(FIX.glob("*.txt"))

SYNTHETIC_MACHINE_IDS = {
    "11111111111111111111111111111111",
    "22222222222222222222222222222222",
    "33333333333333333333333333333333",
    "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
}


def test_there_are_fixtures():
    assert FIXTURES, "no probe fixtures found"


@pytest.mark.parametrize("f", FIXTURES, ids=lambda p: p.name)
def test_machine_id_is_synthetic(f):
    """/etc/machine-id is documented as confidential: it uniquely and stably identifies
    a host. A real one must never reach the repo."""
    for line in f.read_text().splitlines():
        if line.startswith("host.machine_id="):
            value = line.split("=", 1)[1].strip()
            assert value in SYNTHETIC_MACHINE_IDS, (
                f"{f.name} carries a real machine-id. Sanitise before committing.")


@pytest.mark.parametrize("f", FIXTURES, ids=lambda p: p.name)
def test_no_real_tailnet_name(f):
    """Tailscale names look like <host>.tailXXXXXX.ts.net; only 'example.ts.net' is ours."""
    bad = [m for m in re.findall(r"[\w.-]*\.ts\.net", f.read_text())
           if not m.endswith("example.ts.net")]
    assert not bad, f"{f.name} leaks a real tailnet name: {sorted(set(bad))}"


@pytest.mark.parametrize("f", FIXTURES, ids=lambda p: p.name)
def test_tailscale_ips_are_from_the_documentation_slice(f):
    """Tailnet v4 addresses live in 100.64.0.0/10; keep fixtures inside 100.64.0.x."""
    bad = [ip for ip in re.findall(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", f.read_text())
           if not ip.startswith("100.64.0.")]
    assert not bad, f"{f.name} leaks real tailnet IPs: {sorted(set(bad))}"


@pytest.mark.parametrize("f", FIXTURES, ids=lambda p: p.name)
def test_no_home_directory_paths(f):
    text = f.read_text()
    for pattern in (r"/Users/(?!u\b)[a-z]", r"/home/(?!u\b|user\b)[a-z]"):
        assert not re.search(pattern, text), f"{f.name} leaks a real home directory"


@pytest.mark.parametrize("f", FIXTURES, ids=lambda p: p.name)
def test_no_credential_material(f):
    text = f.read_text()
    for pattern in (r"BEGIN [A-Z ]*PRIVATE KEY", r"ssh-(rsa|ed25519) AAAA",
                    r"\bsk-[A-Za-z0-9]{20}", r"Bearer [A-Za-z0-9]"):
        assert not re.search(pattern, text), f"{f.name} contains credential material"


def test_fixtures_still_carry_the_numbers_the_parser_tests_rely_on():
    """Sanitising must not hollow out the fixtures: the display-vs-compute case is the
    whole reason the GPU fixture exists."""
    gpu = (FIX / "gpu-box.txt").read_text()
    assert "24564|853|23197" in gpu          # total|used|free
    assert gpu.count("#GPUPROC") == 1
    assert "msedge" in gpu and "code" in gpu  # display-class processes must survive


# ------------------------------------------------- and the source, not just the fixtures

SRC = pathlib.Path(__file__).parent.parent
# RFC 5737 / RFC 3849 documentation ranges, loopback, and the private blocks. Everything
# else is somebody's real machine -- ours or, worse, a stranger's.
_ALLOWED = re.compile(
    r"^(1\.2\.3\.4|5\.6\.7\.8|0\.0\.0\.0|255\.|127\.|10\.|192\.168\.|100\.64\.0\.|"
    r"172\.(1[6-9]|2[0-9]|3[01])\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _python_files():
    for d in ("src", "tests"):
        yield from sorted((SRC / d).rglob("*.py"))


@pytest.mark.parametrize("f", list(_python_files()), ids=lambda p: p.name)
def test_no_real_address_in_the_source(f):
    """`test_help.py` forbids real addresses in --help output. Nothing applied the same
    rule to the source, which is exactly where one sat: a rental's public IP and port,
    committed in the first commit and carried ever since.

    A public resolver counts too. It is not ours to leak, but a test that ever really
    dialled it would reach a stranger's service.
    """
    bad = sorted({ip for ip in _IP.findall(f.read_text()) if not _ALLOWED.match(ip)})
    assert not bad, f"{f.name} carries a real address: {bad}. Use 1.2.3.4 or 5.6.7.8."


@pytest.mark.parametrize("f", list(_python_files()), ids=lambda p: p.name)
def test_no_real_tailnet_name_in_the_source(f):
    """Fixtures were checked for these and the source was not -- so one arrived in a
    code comment, written while explaining a bug the same name had caused.

    Real tailnets are <host>.tailXXXXXX.ts.net with a hex suffix. `example.ts.net` and a
    literal `tailXXXXXX` are the placeholders.
    """
    # A real one is <host>.<tailnet>.ts.net -- four labels. The bare ".ts.net" suffix
    # appears in source as a literal, and short fakes like "oracle.ts.net" name no real
    # tailnet; matching either would make the guard noise, and a noisy guard gets muted.
    bad = sorted({m for m in re.findall(r"\b[\w-]+\.[\w-]+\.ts\.net\b", f.read_text())
                  if not m.endswith("example.ts.net") and "tailXXXXXX" not in m})
    assert not bad, f"{f.name} leaks a real tailnet name: {bad}"
