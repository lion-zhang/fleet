"""Who authorizes SSH on a host, decided from the server rather than the address."""

from __future__ import annotations

from fleet.sshauth import classify, server_identity

OPENSSH = """OpenSSH_9.6p1, LibreSSL 3.3.6
debug1: Reading configuration data /etc/ssh/ssh_config
debug1: Connecting to gpu-box.example.ts.net port 22.
debug1: Remote protocol version 2.0, remote software version OpenSSH_9.2p1 Debian-2
debug1: compat_banner: match: OpenSSH_9.2p1 Debian-2 pat OpenSSH* compat 0x04000000
"""

TAILSCALE = """OpenSSH_9.6p1, LibreSSL 3.3.6
debug1: Connecting to gpu-box.example.ts.net port 22.
debug1: Remote protocol version 2.0, remote software version Tailscale
debug1: compat_banner: no match: Tailscale
"""


def test_the_server_names_itself():
    assert server_identity(OPENSSH) == "OpenSSH_9.2p1 Debian-2"
    assert server_identity(TAILSCALE) == "Tailscale"


def test_a_server_that_never_spoke_is_not_an_error():
    """A host that closed the connection early never got to say. Empty is an answer."""
    assert server_identity("") == ""
    assert server_identity("ssh: connect to host 1.2.3.4 port 22: Connection refused") == ""


def test_a_tailnet_address_running_ordinary_sshd_is_not_external():
    """The regression this replaces: `.ts.net` described the route, not the server, so a
    tailnet name in front of a normal sshd was classified as authorizing upstream and
    never got a key."""
    assert server_identity(OPENSSH).startswith("OpenSSH")
    assert classify(server_identity(OPENSSH)) == "keys"


def test_a_server_that_authorizes_upstream_is_external():
    assert classify(server_identity(TAILSCALE)) == "external"
    assert classify("NetBird") == "external"


def test_an_unknown_server_falls_back_to_keys():
    """Wrong in the safe direction: a needless key-install offer, rather than silently
    skipping the install and stranding a host."""
    assert classify("") == "keys"
    assert classify("Dropbear_2022.83") == "keys"
    assert classify("SomeFutureThing_1.0") == "keys"


def test_the_table_is_data_not_logic():
    """Adding a vendor must be one line in sshauth, not a branch somewhere else."""
    from fleet import sshauth

    assert "future-mesh" not in str(sshauth.EXTERNAL_SERVERS)
    original = sshauth.EXTERNAL_SERVERS
    try:
        sshauth.EXTERNAL_SERVERS = original + ("future-mesh",)
        assert classify("Future-Mesh/2.1") == "external"
    finally:
        sshauth.EXTERNAL_SERVERS = original
