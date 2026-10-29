"""A rental known as one when it is reached by its own address, not its provider's."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from fleet.onboard import rental_provider
from fleet.probe.parse import parse_payload
from test_resources import PAYLOAD, make_home, probe

posix = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"),
                           reason="runs payload.sh")


@posix
def test_a_runpod_pod_is_a_rental_from_its_rp_environment(tmp_path):
    # RunPod's start script puts the pod's RUNPOD_* in /etc/rp_environment for
    # interactive shells only; the probe's shell is not one, so a pod added by its own
    # ip and port read as a permanent machine: no idle-rental alert, no cost burn.
    rp = tmp_path / "rp_environment"
    rp.write_text('export RUNPOD_POD_ID="k3xq9a7b2c"\nexport RUNPOD_GPU_COUNT="1"\n')
    payload = tmp_path / "payload.sh"
    text = PAYLOAD.read_text(encoding="utf-8")
    assert "/etc/rp_environment" in text
    payload.write_text(text.replace("/etc/rp_environment", str(rp)))
    env = {"PATH": os.environ["PATH"], "HOME": str(make_home(tmp_path))}
    out = subprocess.run(["sh", str(payload)], env=env, capture_output=True, text=True,
                         timeout=60).stdout
    snap = parse_payload(out)
    assert snap.runpod_id == "k3xq9a7b2c" and rental_provider(snap) == "runpod"


@posix
def test_a_vast_instance_is_a_rental_from_the_label_in_its_home(tmp_path):
    home = make_home(tmp_path)
    (home / ".vast_containerlabel").write_text("C.12733928\n")
    snap = parse_payload(probe(home))
    assert snap.vast_label == "C.12733928" and rental_provider(snap) == "vastai"


@posix
def test_a_machine_with_neither_is_not_a_rental(tmp_path):
    assert rental_provider(parse_payload(probe(make_home(tmp_path)))) == ""
