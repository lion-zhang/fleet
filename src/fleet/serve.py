"""The center, listening.

Under center-dials-spokes nothing ever reached the center, so a machine learned the fleet
only when the center got round to telling it -- which meant a human typing `fleet sync`,
because the timer that once did it was deleted when the direction flipped and nothing
replaced it.

Only one job in a sweep genuinely needs the center to dial out: removing a key, because a
passive host will not delete a peer's key on its own. Everything else -- the inventory,
the telemetry, even installing a key, which is a machine writing its own authorized_keys
-- is signed data that travels just as well the other way. So the center serves it, and
machines ask when they need it.

**This is a narrower grant than ssh.** Reaching the center over sshd would be a shell on
the machine that is total compromise if taken. Here there is one verb, and every byte in
or out is signed by a key the fleet already pinned.

The wire format is unchanged: the same sealed envelope `fleet sync --serve` has always
read from stdin, over HTTP instead. Nothing new had to be trusted for this to work.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import DEFAULT_PORT
from .ops.sync import record_relayed, telemetry_to_relay

MAX_BODY = 8 * 1024 * 1024                 # an inventory, not a payload to be generous to


class _Handler(BaseHTTPRequestHandler):
    server_version = "fleet"
    sys_version = ""

    def log_message(self, fmt, *args):     # noqa: A003 - BaseHTTPRequestHandler's name
        """Silent by default. A center serving a fleet all day should not narrate it."""

    def _reply(self, code: int, body: str, kind: str = "text/plain") -> None:
        raw = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:              # noqa: N802 - the handler contract
        """Enough to see the thing is up, and nothing about the fleet.

        Deliberately not a status page: an unauthenticated caller learns that a fleet
        center is here, which it could tell from the open port anyway, and no more.
        """
        if self.path.rstrip("/") in ("", "/health"):
            self._reply(200, json.dumps({"service": "fleet"}) + "\n", "application/json")
        else:
            self._reply(404, "not found\n")

    def do_POST(self) -> None:             # noqa: N802 - the handler contract
        if self.path.rstrip("/") != "/sync":
            self._reply(404, "not found\n")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, "bad length\n")
            return
        if length <= 0 or length > MAX_BODY:
            self._reply(413, "body too large\n")
            return
        raw = self.rfile.read(length).decode(errors="replace")

        code, body = self.server.exchange(raw)      # type: ignore[attr-defined]
        self._reply(code, body, "text/yaml" if code == 200 else "text/plain")


def exchange(raw: str) -> tuple[int, str]:
    """One sync, from the center's side. Returns (status, body).

    Pulled out of the handler so it can be tested without a socket, and so the one place
    that decides who is allowed to speak is readable on its own.
    """
    from . import access as acl
    from . import inventory as inv

    try:
        acc = acl.load()
    except acl.AccessError as exc:
        return 503, f"{exc}\n"
    if not acl.is_center(acc):
        # Serving without the signing key would mean handing out a list we cannot sign,
        # which no spoke would accept anyway.
        return 503, "not the center\n"

    signer = acl.claimed_signer(raw)
    if not acl.is_pinned(acc, signer):
        # **Never first contact.** `fleet sync --serve` falls back to trust-on-first-use,
        # which is safe only because the center always spoke first: it pinned whoever it
        # had just dialled. A listener reverses who speaks first, so the same fallback
        # would let the earliest caller pin itself as the center. Enrolment stays the
        # only way in, and it is still the center that dials for it.
        return 403, "not a machine this fleet knows\n"

    try:
        note = acl.unseal(raw, signer)
    except acl.AccessError as exc:
        return 400, f"{exc}\n"

    try:
        incoming = inv.loads(note["inventory"])
    except Exception as exc:
        # A truncated body must never be read as "that machine has no devices".
        return 400, f"unreadable inventory: {exc}\n"

    merged, _ = inv.update(lambda current: inv.merge(current, incoming))
    if note["telemetry"]:
        record_relayed(note["telemetry"])
    return 200, acl.seal(inv.dumps(merged), telemetry=telemetry_to_relay(),
                         center_url=current_url())


_URL = {"value": ""}


def current_url() -> str:
    """The address we are telling machines to come back to."""
    return _URL["value"]


def build(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.exchange = staticmethod(exchange)         # type: ignore[attr-defined]
    return httpd


def serve(host: str = "", port: int = DEFAULT_PORT, *, advertise: str = "") -> None:
    """Run until interrupted. `advertise` is what machines are told to dial."""
    httpd = build(host or "0.0.0.0", port)
    _URL["value"] = advertise or f"http://{host or '0.0.0.0'}:{port}/sync"
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def serve_in_thread(host: str = "127.0.0.1", port: int = 0, *, advertise: str = ""):
    """A running server and its address, for tests and for `--listen` under a supervisor."""
    httpd = build(host, port)
    actual = httpd.server_address[1]
    _URL["value"] = advertise or f"http://{host}:{actual}/sync"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://{host}:{actual}/sync"
