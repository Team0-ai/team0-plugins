#!/usr/bin/env python3
"""Pair a local agent host with Team0 without exposing a long-lived key."""

from __future__ import annotations

import hmac
import html
import os
import secrets
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlencode


PLUGIN_ROOT = Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(PLUGIN_ROOT / "runtime"))

from team0_agent_runtime import ApiError, Team0ApiClient  # noqa: E402
from credential_store import load_credential, store_credential  # noqa: E402
from host_profile import current_root, detect_host_id, local_pairing_unavailable_reason, profile  # noqa: E402
from pairing_status import write_status  # noqa: E402


PAIRING_TIMEOUT_SECONDS = 300


def _open_connect_url(connect_url: str) -> bool:
    """Open the pairing page even when Python's browser registry is stale.

    Codex starts this process detached from the hook. On macOS, ``webbrowser``
    can return false (or target a stale browser registration) without raising,
    which previously left pairing running with no visible page. Use the native
    launcher as a second attempt and return whether either launch was accepted.
    """
    try:
        if webbrowser.open(connect_url):
            return True
    except (OSError, webbrowser.Error):
        pass

    if sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["open", connect_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    if os.name == "nt":
        try:
            os.startfile(connect_url)  # type: ignore[attr-defined]
            return True
        except (AttributeError, OSError):
            return False

    # Linux desktop environments conventionally expose the default browser
    # through xdg-open. Keep this as a native fallback for hosts where Python's
    # browser registry is incomplete (or the hook runs outside an interactive
    # shell).
    try:
        subprocess.Popen(
            ["xdg-open", connect_url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _holder_is_running(path: Path) -> bool:
    """Whether the process that wrote this lock still exists."""

    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def _pairing_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A pairing that was abandoned (browser closed, machine slept, process
    # killed) must never block every later attempt: reclaim the lock as soon as
    # its holder is gone, rather than waiting out the full pairing timeout.
    try:
        expired = time.time() - path.stat().st_mtime > PAIRING_TIMEOUT_SECONDS
        if expired or not _holder_is_running(path):
            path.unlink()
    except FileNotFoundError:
        pass
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        yield False
        return
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        yield True
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


_AGENTS_URL = "https://team0.ai/ops/w/living-understanding?view=agents"

# The website's world: warm paper by day, ink by night, one terracotta accent.
_PAGE_STYLE = (
    ":root{--bg:#f3eee6;--card:#fbf8f3;--ink:#1b1714;--muted:#62574c;--line:#d9cfc0;"
    "--clay:#c8553d;--clay-soft:#f0d9d0;color-scheme:light}"
    "@media (prefers-color-scheme:dark){:root{--bg:#1b1714;--card:#241e1a;--ink:#f3eee6;"
    "--muted:#bdb2a5;--line:#3a312a;--clay:#e07a62;--clay-soft:#3a2520;color-scheme:dark}}"
    "*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--bg);"
    "color:var(--ink);font-family:'Schibsted Grotesk',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
    ".card{width:100%;max-width:30rem;padding:40px;border:1px solid var(--line);border-radius:20px;"
    "background:var(--card)}"
    ".brand{font-weight:700;font-size:20px;letter-spacing:-.02em}"
    ".mark{width:56px;height:56px;border-radius:50%;display:grid;place-items:center;margin:32px 0 24px;"
    "background:var(--clay-soft);color:var(--clay)}"
    "h1{margin:0;font-size:34px;line-height:1.05;letter-spacing:-.03em;font-weight:800}"
    "p{margin:14px 0 0;font-size:17px;line-height:1.6;color:var(--muted)}"
    "ul{list-style:none;margin:28px 0 0;padding:20px 0 0;border-top:1px solid var(--line)}"
    "li{display:flex;gap:12px;font-size:15px;line-height:1.55;color:var(--ink);margin-top:12px}"
    "li:first-child{margin-top:0}li span{color:var(--clay);font-weight:700}"
    "a{display:inline-block;margin-top:28px;color:var(--ink);font-size:15px;font-weight:600;"
    "text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:6px}"
    "a:hover{text-decoration-color:var(--clay)}"
    "small{display:block;margin-top:20px;font-size:13px;color:var(--muted)}"
)
_CHECK = (
    "<svg width='26' height='26' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.4' "
    "stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M5 12.5l4.5 4.5L19 7.5'/></svg>"
)
_CROSS = (
    "<svg width='24' height='24' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.4' "
    "stroke-linecap='round' aria-hidden='true'><path d='M7 7l10 10M17 7L7 17'/></svg>"
)


def _page(host_id: str, connected: bool) -> bytes:
    """The browser tab the agent opened for pairing: say what happened, and what to do next."""
    agent = html.escape(profile(host_id).name)
    if connected:
        title = f"{agent} is connected"
        body = (
            f"<div class='mark'>{_CHECK}</div><h1>{agent} is connected.</h1>"
            f"<p>Go back to {agent} and start a new conversation. It now starts from your understanding.</p>"
            "<ul>"
            "<li><span>1</span>It reads your understanding before it answers.</li>"
            "<li><span>2</span>It saves each finished conversation back, under its own name.</li>"
            "<li><span>3</span>You can pause saving or stop its access at any time.</li>"
            "</ul>"
            f"<a href='{_AGENTS_URL}' target='_blank' rel='noopener'>Manage {agent} in Team0 &rarr;</a>"
            "<small>You can close this tab.</small>"
        )
    else:
        title = "Connection didn\u2019t finish"
        body = (
            f"<div class='mark'>{_CROSS}</div><h1>Connection didn\u2019t finish.</h1>"
            f"<p>Team0 could not finish connecting {agent}. Go back to {agent} and connect Team0 again.</p>"
            f"<a href='{_AGENTS_URL}' target='_blank' rel='noopener'>Open Agents in Team0 &rarr;</a>"
        )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title} \u00b7 Team0</title>"
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;600;700;800&display=swap'>"
        f"<style>{_PAGE_STYLE}</style></head><body><main class='card'>"
        f"<div class='brand'>Team0</div>{body}</main></body></html>"
    ).encode("utf-8")


def _handler(
    state: str,
    host_id: str,
    outcome: dict[str, str | bool],
    credential_root: Path | None = None,
):
    class PairingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/complete":
                self.send_error(404)
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 16_384)
                values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                returned_state = values.get("state", [""])[0]
                key = values.get("credential", [""])[0]
                if not hmac.compare_digest(returned_state, state) or not key.startswith("t0_"):
                    raise ValueError("invalid pairing response")
                client = Team0ApiClient(base_url="https://api.team0.ai/v1", api_key=key)
                binding = client.get_agent_runtime_binding()
                store_credential(
                    key,
                    str(binding["contribution_source_id"]),
                    host_id,
                    root=credential_root,
                )
                saved = load_credential(root=credential_root)
                if not saved or saved.get("key") != key or saved.get("host_id") != host_id:
                    raise OSError("credential did not survive secure-store round trip")
                outcome["connected"] = True
                write_status("connected", host_id=host_id, root=credential_root)
                body = _page(host_id, connected=True)
                status = 200
            except (ApiError, KeyError, OSError, subprocess.SubprocessError, ValueError):
                outcome["failed"] = True
                write_status("failed", host_id=host_id, root=credential_root, error_code="callback_rejected")
                body = _page(host_id, connected=False)
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return PairingHandler


def main(argv: list[str] | None = None) -> int:
    # A host's own markers are absent when pairing is run straight from a shell,
    # right after installing the plugin and before any session has loaded it, so
    # the host can be named outright: `pair.py --host claude-code`.
    arguments = list(argv if argv is not None else sys.argv[1:])
    named = arguments[arguments.index("--host") + 1] if "--host" in arguments[:-1] else ""
    host_id = named.strip() or detect_host_id()
    unavailable_reason = local_pairing_unavailable_reason(host_id)
    if unavailable_reason:
        print(unavailable_reason, file=sys.stderr)
        return 1
    credential_root = current_root(host_id)
    with _pairing_lock(credential_root / "pairing.lock") as acquired:
        if not acquired:
            return 0
        server = None
        browser_opened = None
        try:
            state = secrets.token_urlsafe(32)
            outcome: dict[str, str | bool] = {}
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                _handler(state, host_id, outcome, credential_root),
            )
            server.timeout = 1
            callback = f"http://127.0.0.1:{server.server_port}/complete"
            connect_base = os.environ.get(
                "TEAM0_RUNTIME_CONNECT_URL", "https://team0.ai/connect/agent-runtime"
            )
            connect_url = f"{connect_base}?{urlencode({'host': host_id, 'callback': callback, 'state': state})}"
            browser_opened = _open_connect_url(connect_url)
            write_status(
                "waiting",
                host_id=host_id,
                root=credential_root,
                browser_opened=browser_opened,
            )
            deadline = time.monotonic() + PAIRING_TIMEOUT_SECONDS
            while time.monotonic() < deadline and not outcome:
                server.handle_request()
            if not outcome:
                write_status(
                    "expired",
                    host_id=host_id,
                    root=credential_root,
                    browser_opened=browser_opened,
                    error_code="timeout",
                )
            return 0 if outcome.get("connected") else 1
        except (OSError, RuntimeError, ValueError):
            write_status("failed", host_id=host_id, root=credential_root, error_code="local_startup")
            return 1
        finally:
            if server is not None:
                server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
