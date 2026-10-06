"""Socket client for the herdr server, plus the one snapshot this plugin takes.

Shape follows herdr-sort's client. The snapshot exists only for what
`herdr-sessions --json` does not carry -- terminal_id and the workspace list --
never to re-derive a column that tool already owns.
"""

import json
import os
import socket
import subprocess

DEFAULT_SOCKET = "~/.config/herdr/herdr.sock"


class HerdrError(Exception):
    pass


class Herdr:
    def __init__(self, source="herdr-dash"):
        self.source = source
        self.path = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
            DEFAULT_SOCKET)

    def _connect(self, timeout):
        if not os.path.exists(self.path):
            raise HerdrError(f"no herdr server socket at {self.path}")
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(self.path)
        except OSError as err:
            conn.close()
            raise HerdrError(f"cannot reach {self.path}: {err}")
        return conn

    def call(self, method, params=None, timeout=10):
        conn = self._connect(timeout)
        try:
            payload = {"id": self.source, "method": method, "params": params or {}}
            conn.sendall(json.dumps(payload).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except OSError as err:
            raise HerdrError(f"{method} failed: {err}")
        finally:
            conn.close()
        try:
            response = json.loads(buf.split(b"\n", 1)[0])
        except ValueError:
            raise HerdrError(f"unreadable response to {method}: {buf!r}")
        if "error" in response:
            err = response["error"]
            raise HerdrError(f"{method}: {err.get('code')}: {err.get('message')}")
        return response.get("result", {})

    def events(self, types):
        """Yield raw event lines. Caller treats them as a wake-up, not as data.

        One subscription was measured never firing, so an event is only ever
        allowed to shorten the refresh interval -- never to be the sole trigger.
        """
        conn = self._connect(None)
        try:
            payload = {
                "id": self.source,
                "method": "events.subscribe",
                "params": {"subscriptions": [{"type": name} for name in types]},
            }
            conn.sendall(json.dumps(payload).encode() + b"\n")
            buf = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        yield line
        finally:
            conn.close()


def snapshot(herdr=None):
    """Panes, tabs and workspaces, via the CLI so a stale socket path still works."""
    proc = subprocess.run(["herdr", "api", "snapshot"], capture_output=True,
                          text=True)
    if 0 != proc.returncode:
        raise HerdrError(
            f"herdr api snapshot failed: {proc.stderr.strip() or proc.returncode}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        raise HerdrError("herdr api snapshot returned unreadable JSON")
    if "error" in payload:
        raise HerdrError(f"herdr api snapshot error: {payload['error']}")
    return payload["result"]["snapshot"]


def pane_index(snap):
    """pane_id -> pane record, and pane_id -> terminal_id."""
    panes = {pane["pane_id"]: pane for pane in snap.get("panes", [])}
    terminals = {pid: pane.get("terminal_id") for pid, pane in panes.items()}
    return panes, terminals


def workspace_labels(snap):
    out = {}
    for workspace in snap.get("workspaces", []):
        key = workspace.get("workspace_id")
        out[key] = workspace.get("label") or key
    return out
