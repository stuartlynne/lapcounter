#!/usr/bin/env python3
"""Install or serve the LapCounter HTML outside CrossMgr."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import http.server
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import time
from typing import List, Optional, Tuple


DEFAULT_PORT = 8675
DEFAULT_CROSSMGR_HOST = "127.0.0.1"
DEFAULT_CROSSMGR_HTTP_PORT = 8765
LAPCOUNTER_HTML = Path(__file__).with_name("LapCounter.html")
LAPCOUNTER_RE = re.compile(r"^/LapCounter[1-9A-Z-]*\.html$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Screen:
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def read_lapcounter_html() -> str:
    try:
        return LAPCOUNTER_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"Cannot find {LAPCOUNTER_HTML}") from None


def install_lapcounter(target: Path) -> Path:
    html_text = read_lapcounter_html()
    destination = target if target.suffix.lower() == ".html" else target / "LapCounter.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html_text, encoding="utf-8")
    return destination


def split_host_port(value: str) -> Tuple[str, int]:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("host cannot be empty")

    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise argparse.ArgumentTypeError("invalid IPv6 address")
        host = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            return host, int(rest[1:]) + 2
        if rest:
            raise argparse.ArgumentTypeError("invalid host[:port]")
        return host, DEFAULT_CROSSMGR_HTTP_PORT + 2

    if value.count(":") == 1:
        host, port_text = value.rsplit(":", 1)
        if port_text.isdigit():
            return host, int(port_text) + 2

    return value, DEFAULT_CROSSMGR_HTTP_PORT + 2


def run_output(command: List[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""
    return completed.stdout


def ordered_screens(screens: List[Screen]) -> List[Screen]:
    primaries = [screen for screen in screens if screen.primary]
    primary = primaries[0] if primaries else next((screen for screen in screens if screen.x == 0 and screen.y == 0), None)
    others = [screen for screen in screens if screen is not primary]
    others.sort(key=lambda screen: (screen.y, screen.x, screen.name))
    return ([primary] if primary else []) + others


def parse_xrandr_screens(output: str) -> List[Screen]:
    screens: List[Screen] = []
    pattern = re.compile(
        r"^(?P<name>\S+)\s+connected(?:\s+(?P<primary>primary))?\s+"
        r"(?P<width>\d+)x(?P<height>\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
    )
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        screens.append(
            Screen(
                name=match.group("name"),
                x=int(match.group("x")),
                y=int(match.group("y")),
                width=int(match.group("width")),
                height=int(match.group("height")),
                primary=bool(match.group("primary")),
            )
        )
    return ordered_screens(screens)


def parse_kscreen_screens(output: str) -> List[Screen]:
    output = ANSI_RE.sub("", output)
    screens: List[Screen] = []
    name: Optional[str] = None
    for line in output.splitlines():
        line = line.strip()
        output_match = re.match(r"Output:\s+\d+\s+(.+)$", line)
        if output_match:
            name = output_match.group(1).strip()
            continue
        geometry_match = re.match(r"Geometry:\s+(-?\d+),(-?\d+)\s+(\d+)x(\d+)$", line)
        if geometry_match and name:
            screens.append(
                Screen(
                    name=name,
                    x=int(geometry_match.group(1)),
                    y=int(geometry_match.group(2)),
                    width=int(geometry_match.group(3)),
                    height=int(geometry_match.group(4)),
                    primary=False,
                )
            )
            name = None
    return ordered_screens(screens)


def discover_screens() -> List[Screen]:
    screens = parse_xrandr_screens(run_output(["xrandr", "--query"]))
    if screens:
        return screens
    screens = parse_kscreen_screens(run_output(["kscreen-doctor", "-o"]))
    if screens:
        return screens
    raise RuntimeError("could not determine screen geometry with xrandr or kscreen-doctor")


def find_browser(browser: Optional[str]) -> str:
    if browser:
        return browser
    browser_env = os.environ.get("BROWSER")
    if browser_env:
        return browser_env
    for candidate in ("google-chrome", "chromium-browser", "chromium", "brave-browser", "microsoft-edge", "firefox"):
        path = shutil.which(candidate)
        if path:
            return path
    raise RuntimeError("no browser found; set BROWSER or use --browser")


def browser_args(browser: str, url: str) -> List[str]:
    base = Path(shlex.split(browser)[0]).name.lower()

    if "firefox" in base:
        return ["--new-window", url]
    if any(name in base for name in ("chrome", "chromium", "brave", "edge")):
        return ["--new-window", url, "--start-fullscreen"]
    return [url]


def invoke_kwin_shortcut(shortcut: str) -> bool:
    command = [
        "qdbus6",
        "org.kde.kglobalaccel",
        "/component/kwin",
        "org.kde.kglobalaccel.Component.invokeShortcut",
        shortcut,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return True
    except FileNotFoundError:
        print(f"qdbus6 not found; unable to invoke KWin shortcut {shortcut!r}", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip()
        msg = f"qdbus6 failed while invoking KWin shortcut {shortcut!r}"
        print(f"{msg}: {stderr}" if stderr else msg, file=sys.stderr)
    return False


def focus_kwin_screen(screen_index: int) -> None:
    invoke_kwin_shortcut(f"Switch to Screen {screen_index}")
    time.sleep(0.05)


def launch_browser_on_screen(url: str, screen_index: int, browser: Optional[str], fullscreen_delay: float) -> None:
    screens = discover_screens()
    if screen_index < 0 or screen_index >= len(screens):
        available = ", ".join(f"{idx}:{screen.name}={screen.geometry}" for idx, screen in enumerate(screens))
        raise RuntimeError(f"screen {screen_index} is not available; screens: {available}")

    screen = screens[screen_index]
    browser_command = find_browser(browser)
    command = [*shlex.split(browser_command), *browser_args(browser_command, url)]
    print(f"Launching browser on screen {screen_index} ({screen.name} {screen.geometry})")
    focus_kwin_screen(screen_index)
    print("Browser command:", shlex.join(command))
    subprocess.Popen(command)
    time.sleep(fullscreen_delay)
    invoke_kwin_shortcut("Window Fullscreen")


def print_screens() -> None:
    for idx, screen in enumerate(discover_screens()):
        primary = " primary" if screen.primary or idx == 0 else ""
        print(f"{idx}: {screen.name} {screen.geometry}{primary}")


def local_ip_addresses() -> List[str]:
    addresses = []
    output = run_output(["hostname", "-I"])
    for token in output.split():
        if ":" in token:
            continue
        if token.startswith("127."):
            continue
        if token not in addresses:
            addresses.append(token)
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            address = info[4][0]
            if not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        if not address.startswith("127.") and address not in addresses:
            addresses.append(address)
    except OSError:
        pass
    return addresses


def render_html(crossmgr_host: str, crossmgr_ws_port: int) -> bytes:
    html_text = read_lapcounter_html()
    config = (
        "<script>\n"
        f"window.LAPCOUNTER_CROSSMGR_HOST = {json.dumps(crossmgr_host)};\n"
        f"window.LAPCOUNTER_CROSSMGR_WS_PORT = {int(crossmgr_ws_port)};\n"
        "</script>\n"
    )
    return html_text.replace("<script>", config + "<script>", 1).encode("utf-8")


class ReuseTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_handler(crossmgr_host: str, crossmgr_ws_port: int):
    class CrossMgrWebHandler(http.server.BaseHTTPRequestHandler):
        server_version = "CrossMgrWeb/1.0"

        def do_GET(self) -> None:
            path = posixpath.normpath(self.path.split("?", 1)[0])
            if path in ("", "/", "/LapCounter.html") or LAPCOUNTER_RE.match(path):
                self.send_lapcounter()
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self.send_error(404, "Not found")

        def send_lapcounter(self) -> None:
            body = render_html(crossmgr_host, crossmgr_ws_port)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    return CrossMgrWebHandler


def serve(
    bind: str,
    port: int,
    crossmgr_host: str,
    crossmgr_ws_port: int,
    screen: Optional[int] = None,
    browser: Optional[str] = None,
    fullscreen_delay: float = 1.0,
) -> None:
    handler = make_handler(crossmgr_host, crossmgr_ws_port)
    with ReuseTCPServer((bind, port), handler) as httpd:
        url_host = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
        url = f"http://{url_host}:{port}/LapCounter.html"
        print(f"Serving LapCounter on {url}")
        if bind in ("", "0.0.0.0"):
            for address in local_ip_addresses():
                print(f"LAN URL: http://{address}:{port}/LapCounter.html")
        print(f"Using CrossMgr websocket ws://{crossmgr_host}:{crossmgr_ws_port}/")
        if screen is not None:
            launch_browser_on_screen(url, screen, browser, fullscreen_delay)
        httpd.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install or serve LapCounter.html for CrossMgr lap-counter displays."
    )
    parser.add_argument("--install", metavar="PATH", help="write LapCounter.html to PATH or PATH/LapCounter.html")
    parser.add_argument(
        "--crossmgr",
        default=DEFAULT_CROSSMGR_HOST,
        help="CrossMgr hostname or IP address; host:port is treated as the CrossMgr web port and defaults to 127.0.0.1",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP server port, default {DEFAULT_PORT}")
    parser.add_argument("--bind", default="0.0.0.0", help="HTTP bind address, default 0.0.0.0")
    parser.add_argument("--screen", type=int, help="launch browser on screen N; screen 0 is the primary display")
    parser.add_argument("--list-screens", action="store_true", help="print detected screen numbers and exit")
    parser.add_argument("--browser", help="browser command for --screen; defaults to BROWSER or a detected browser")
    parser.add_argument(
        "--fullscreen-delay",
        type=float,
        default=1.0,
        help="seconds to wait before sending KWin's Window Fullscreen shortcut, default 1.0",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.install:
        destination = install_lapcounter(Path(os.path.expanduser(args.install)))
        print(f"Installed {destination}")
        return 0
    if args.list_screens:
        print_screens()
        return 0

    crossmgr_host, crossmgr_ws_port = split_host_port(args.crossmgr)
    serve(args.bind, args.port, crossmgr_host, crossmgr_ws_port, args.screen, args.browser, args.fullscreen_delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
