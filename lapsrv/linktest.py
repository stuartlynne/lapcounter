#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import queue
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from tkinter import scrolledtext, ttk
from typing import Any, Optional


PING_YELLOW_SECONDS = 5.0
FETCH_INTERVAL_SECONDS = 5.0
HTTP_TIMEOUT_SECONDS = 8.0
USER_AGENT = "linktest/0.2"


def is_windows_like() -> bool:
    system = platform.system().lower()
    return system.startswith("win") or "cygwin" in system


def debug_log(enabled: bool, message: str) -> None:
    if not enabled:
        return
    now = dt.datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{now}] {message}", file=sys.stderr, flush=True)


@dataclass(slots=True)
class PingState:
    address: str
    last_probe_at: Optional[dt.datetime] = None
    last_ok_at: Optional[dt.datetime] = None
    last_probe_ok: Optional[bool] = None
    last_rtt_ms: Optional[float] = None

    def update(self, ok: bool, rtt_ms: Optional[float]) -> None:
        now = dt.datetime.now().astimezone()
        self.last_probe_at = now
        self.last_probe_ok = ok
        if ok:
            self.last_ok_at = now
            self.last_rtt_ms = rtt_ms

    def visual_state(self) -> str:
        if self.last_probe_at is None:
            return "waiting"
        now = dt.datetime.now().astimezone()
        probe_age = (now - self.last_probe_at).total_seconds()
        if self.last_probe_ok:
            return "ok"
        if probe_age <= PING_YELLOW_SECONDS:
            return "missed"
        return "down"


@dataclass(slots=True)
class PaneSnapshot:
    name: str
    addresses: list[str]
    pings: list[PingState] = field(default_factory=list)
    status_text: str = ""
    bridge_text: str = ""
    fetch_note: str = ""
    summary_text: str = ""
    updated_at: Optional[dt.datetime] = None


@dataclass(slots=True)
class BridgePeer:
    name: str
    ip: str = ""
    mac: str = ""
    online: Optional[bool] = None
    myself: bool = False
    rssi: Optional[str] = None
    tx_rate: Optional[str] = None
    rx_rate: Optional[str] = None


class OmadaSession:
    def __init__(self, base_address: str, login: Optional[tuple[str, str]], debug: bool = False) -> None:
        self.base_address = base_address
        self.login = login
        self.debug = debug
        self.cookie_jar = CookieJar()
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            urllib.request.HTTPSHandler(context=self.ssl_context),
        )
        self.base_url = self._base_url(base_address)
        self.http_fallback_used = False

    def _base_url(self, address: str) -> str:
        if address.startswith("http://") or address.startswith("https://"):
            return address.rstrip("/")
        return f"https://{address}".rstrip("/")

    def _fallback_to_http(self) -> bool:
        if self.base_url.startswith("http://"):
            return False
        parsed = urllib.parse.urlsplit(self.base_url)
        host = parsed.netloc or parsed.path
        if not host:
            return False
        self.base_url = f"http://{host}"
        self.http_fallback_used = True
        debug_log(self.debug, f"http fallback enabled for {self.base_address}: {self.base_url}")
        return True

    def _request(
        self,
        url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        method: Optional[str] = None,
    ) -> tuple[str, str]:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        if headers:
            for key, value in headers.items():
                req.add_header(key, value)
        request_desc = method or ('POST' if data is not None else 'GET')
        debug_log(self.debug, f"http {request_desc} {url}")
        try:
            with self.opener.open(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                body = resp.read().decode(charset, errors="replace")
                debug_log(self.debug, f"http ok {url} -> {resp.geturl()} {len(body)} bytes")
                return body, resp.geturl()
        except urllib.error.URLError as exc:
            reason = getattr(exc, 'reason', None)
            if isinstance(reason, ssl.SSLError) and self._fallback_to_http() and url.startswith('https://'):
                retry_url = 'http://' + url[len('https://'):]
                debug_log(self.debug, f"retry over http {retry_url}")
                return self._request(retry_url, data=data, headers=headers, method=method)
            raise

    def fetch(self, path: str = "/") -> tuple[str, str]:
        url = urllib.parse.urljoin(self.base_url + "/", path)
        return self._request(url)

    def _looks_like_login_page(self, html_text: str) -> bool:
        lowered = html_text.lower()
        return (
            'id="form-login"' in lowered
            or 'id="login-password"' in lowered
            or 'name="password" type="password"' in lowered
            or '<title>login</title>' in lowered
        )

    def login_status(self) -> str:
        body = urllib.parse.urlencode({"operation": "read"}).encode()
        status_text, _ = self._request(
            urllib.parse.urljoin(self.base_url + "/", "data/login.json"),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return status_text

    def _login_json(self) -> bool:
        if not self.login:
            return False
        username, password = self.login
        password_hash = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
        body = urllib.parse.urlencode({"username": username, "password": password_hash}).encode()
        response_text, _ = self._request(
            urllib.parse.urljoin(self.base_url + "/", "/"),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        normalized = response_text.replace(" ", "").lower()
        if normalized == "":
            debug_log(self.debug, f"login success {self.base_address}: empty response body")
            return True
        if '"result":true' in normalized:
            debug_log(self.debug, f"login success {self.base_address}: explicit result=true")
            return True
        debug_log(self.debug, f"login uncertain {self.base_address}: {response_text[:200]!r}")
        return False

    def login_if_needed(self) -> str:
        root_html, _page_url = self.fetch("/")
        if not self._looks_like_login_page(root_html):
            debug_log(self.debug, f"already authenticated {self.base_address}")
            return root_html
        if not self.login:
            debug_log(self.debug, f"login required for {self.base_address} but no credentials provided")
            return root_html
        status_before = ""
        try:
            status_before = self.login_status()
            debug_log(self.debug, f"login status before {self.base_address}: {status_before.strip()}")
        except Exception as exc:
            debug_log(self.debug, f"login status before failed {self.base_address}: {exc}")
        if self._login_json():
            root_html, _ = self.fetch("/")
            if not self._looks_like_login_page(root_html):
                debug_log(self.debug, f"authenticated shell page confirmed {self.base_address}")
                return root_html
        status_after = ""
        try:
            status_after = self.login_status()
            debug_log(self.debug, f"login status after {self.base_address}: {status_after.strip()}")
        except Exception as exc:
            debug_log(self.debug, f"login status after failed {self.base_address}: {exc}")
        if status_after:
            raise RuntimeError(f"login failed: {status_after}")
        if status_before:
            raise RuntimeError(f"login failed: {status_before}")
        raise RuntimeError("login failed")

    def _api_headers(self) -> dict[str, str]:
        return {
            "Referer": self.base_url + "/",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

    def _json_get(self, path: str, operation: str) -> dict[str, Any]:
        url = urllib.parse.urljoin(self.base_url + "/", path)
        sep = "&" if "?" in url else "?"
        full_url = f"{url}{sep}operation={urllib.parse.quote(operation)}"
        body, _ = self._request(full_url, headers=self._api_headers(), method="GET")
        data = json.loads(body)
        debug_log(self.debug, f"json {path} op={operation}: success={data.get('success')} timeout={data.get('timeout')}")
        return data

    def fetch_device_status(self) -> dict[str, Any]:
        return self._json_get("data/status.device.json", "read")

    def fetch_bridge_topology(self) -> dict[str, Any]:
        return self._json_get("data/autopair.json", "load")

    def relevant_pages(self) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
        self.login_if_needed()
        status_payload = self.fetch_device_status()
        bridge_payload = self.fetch_bridge_topology()
        status_text = format_device_status(status_payload)
        bridge_text = format_bridge_topology(bridge_payload)
        note = ""
        if str(status_payload.get("timeout", "")).lower() == "true":
            note = "device status API returned timeout=true"
        elif str(bridge_payload.get("timeout", "")).lower() == "true":
            note = "bridge topology API returned timeout=true"
        return status_payload, bridge_payload, status_text, bridge_text, note


def parse_bridge_peers(payload: dict[str, Any]) -> list[BridgePeer]:
    data = payload.get("data") if isinstance(payload.get("data"), list) else []
    peers: list[BridgePeer] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        peers.append(
            BridgePeer(
                name=str(item.get("name") or ""),
                ip=str(item.get("ip") or ""),
                mac=str(item.get("mac") or ""),
                online=item.get("online") if isinstance(item.get("online"), bool) else None,
                myself=bool(item.get("myself")),
                rssi=str(item.get("rssi")) if item.get("rssi") not in (None, "") else None,
                tx_rate=str(item.get("txRate")) if item.get("txRate") not in (None, "") else None,
                rx_rate=str(item.get("rxRate")) if item.get("rxRate") not in (None, "") else None,
            )
        )
    return peers


def summarize_link_state(name: str, pings: list[PingState], bridge_payload: dict[str, Any], wifi_note: str = "") -> str:
    peer_states = parse_bridge_peers(bridge_payload)
    all_ping_ok = bool(pings) and all(state.visual_state() == "ok" for state in pings)
    self_peer = next((peer for peer in peer_states if peer.myself), None)
    remote_peers = [peer for peer in peer_states if not peer.myself]
    online_remote = any(peer.online is True for peer in remote_peers)
    rssi_values = [peer.rssi for peer in remote_peers if peer.rssi]
    parts: list[str] = []
    if wifi_note:
        parts.append(wifi_note)
    if all_ping_ok and online_remote:
        parts.append(f"{name}: LINK UP")
    elif self_peer or remote_peers:
        parts.append(f"{name}: PARTIAL")
    else:
        parts.append(f"{name}: LINK DOWN")
    if remote_peers:
        parts.append(f"peer={len(remote_peers)}")
    if online_remote:
        parts.append("peer online")
    if rssi_values:
        parts.append(f"RSSI {','.join(rssi_values)} dBm")
    ping_summary = []
    for state in pings:
        visual = state.visual_state()
        ping_summary.append(f"{state.address}:{visual}")
    if ping_summary:
        parts.append("ping " + " ".join(ping_summary))
    return " | ".join(parts)


def format_device_status(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    lan_lines = []
    lan_ports = data.get("lan_port_list") or []
    if isinstance(lan_ports, list):
        for item in lan_ports:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            status = str(item.get("status") or "").strip()
            if name or status:
                lan_lines.append(f"{name}: {status}")
    lines = [
        f"Device Name: {data.get('deviceName', '')}",
        f"Default Role: {role_text(data.get('kitRoleDefault'))}",
        f"Current Role: {role_text(data.get('kitRoleCurrent'))}",
        f"Device Model: {data.get('deviceModel', '')}",
        f"Firmware Version: {data.get('firmwareVersion', '')}",
        f"Hardware Version: {data.get('hardwareVersion', '')}",
        f"MAC Address: {data.get('mac', '')}",
        f"IP Address: {data.get('ip', '')}",
        f"Subnet Mask: {data.get('subnetMask', '')}",
    ]
    lines.extend(lan_lines)
    lines.extend(
        [
            f"System Time: {data.get('time', '')}",
            f"Uptime: {data.get('uptime', '')}",
            f"CPU Utilization: {data.get('cpu', '')}%",
            f"Memory Utilization: {data.get('memory', '')}%",
        ]
    )
    return "\n".join(line for line in lines if line.strip())


def role_text(value: Any) -> str:
    try:
        number = int(value)
    except Exception:
        return str(value or "")
    return {0: "Init", 1: "Main AP", 2: "Client AP"}.get(number, str(number))


def type_text(value: Any) -> str:
    try:
        number = int(value)
    except Exception:
        return str(value or "")
    return {0: "Main AP", 1: "Client AP"}.get(number, str(number))


def format_bridge_topology(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), list) else []
    others = payload.get("others") if isinstance(payload.get("others"), dict) else {}
    lines = []
    child_count = others.get("childDevicesNum")
    max_devices = others.get("maxDevicesNum")
    if child_count is not None or max_devices is not None:
        lines.append(f"Bridge APs List: {child_count}/{max_devices}")
        lines.append("")
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        lines.append(f"{idx}. {name}")
        lines.append(f"   Type: {type_text(item.get('type'))}")
        if item.get("ip") is not None:
            lines.append(f"   IP Address: {item.get('ip')}")
        if item.get("mac") is not None:
            lines.append(f"   MAC Address: {item.get('mac')}")
        if item.get("online") is not None:
            lines.append(f"   Online: {item.get('online')}")
        if item.get("myself"):
            lines.append("   Current AP: yes")
        if item.get("rssi") not in (None, ""):
            lines.append(f"   RSSI: {item.get('rssi')} dBm")
        tx_rate = item.get("txRate")
        rx_rate = item.get("rxRate")
        if tx_rate not in (None, "") or rx_rate not in (None, ""):
            lines.append(f"   TX Rate: {tx_rate or '--'}")
            lines.append(f"   RX Rate: {rx_rate or '--'}")
        lines.append("")
    return "\n".join(lines).strip()


class WiFiManager:
    def __init__(self, target_ssid: Optional[str], debug: bool = False) -> None:
        self.target_ssid = target_ssid
        self.previous_ssid: Optional[str] = None
        self.active = False
        self.debug = debug
        try:
            import pywifi  # type: ignore
            from pywifi import const  # type: ignore
        except Exception:
            pywifi = None
            const = None
        self.pywifi = pywifi
        self.pywifi_const = const

    def current_ssid(self) -> Optional[str]:
        if is_windows_like():
            commands = [["netsh.exe", "wlan", "show", "interfaces"], ["netsh", "wlan", "show", "interfaces"]]
            pattern = re.compile(r"^\s*SSID\s*:\s*(.+?)\s*$", re.M)
        else:
            commands = [["iwgetid", "-r"], ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"]]
            pattern = None
        for cmd in commands:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
            except Exception:
                continue
            output = proc.stdout.strip()
            if not output:
                continue
            if pattern is not None:
                match = pattern.search(output)
                if match:
                    return match.group(1).strip()
            else:
                if cmd[0] == "iwgetid":
                    return output.strip() or None
                for line in output.splitlines():
                    if line.startswith("yes:"):
                        return line.split(":", 1)[1].strip() or None
        return None

    def _connect_with_system_tools(self, ssid: str) -> bool:
        debug_log(self.debug, f"wifi connect attempt system tools ssid={ssid}")
        if is_windows_like():
            for cmd in (["netsh.exe", "wlan", "connect", f"name={ssid}"], ["netsh", "wlan", "connect", f"name={ssid}"]):
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
                except Exception:
                    continue
                if proc.returncode == 0:
                    return True
            return False
        for cmd in (["nmcli", "device", "wifi", "connect", ssid], ["nmcli", "connection", "up", ssid]):
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            except Exception:
                continue
            if proc.returncode == 0:
                return True
        return False

    def _connect_with_pywifi(self, ssid: str) -> bool:
        if self.pywifi is None or self.pywifi_const is None:
            return False
        debug_log(self.debug, f"wifi connect attempt pywifi ssid={ssid}")
        try:
            wifi = self.pywifi.PyWiFi()
            interfaces = wifi.interfaces()
        except Exception:
            return False
        if not interfaces:
            return False
        iface = interfaces[0]
        try:
            iface.disconnect()
            time.sleep(1.0)
            profile = self.pywifi.Profile()
            profile.ssid = ssid
            profile.auth = self.pywifi_const.AUTH_ALG_OPEN
            profile.akm.append(self.pywifi_const.AKM_TYPE_NONE)
            profile.cipher = self.pywifi_const.CIPHER_TYPE_NONE
            iface.remove_all_network_profiles()
            tmp_profile = iface.add_network_profile(profile)
            iface.connect(tmp_profile)
            for _ in range(20):
                time.sleep(0.5)
                if iface.status() == self.pywifi_const.IFACE_CONNECTED:
                    return True
        except Exception:
            return False
        return False

    def connect(self) -> str:
        if not self.target_ssid:
            return ""
        self.previous_ssid = self.current_ssid()
        debug_log(self.debug, f"wifi current ssid={self.previous_ssid!r} target={self.target_ssid!r}")
        if self.previous_ssid == self.target_ssid:
            self.active = True
            return f"already on SSID {self.target_ssid}"
        if self._connect_with_system_tools(self.target_ssid) or self._connect_with_pywifi(self.target_ssid):
            self.active = True
            return f"connected to SSID {self.target_ssid}"
        return f"failed to connect to SSID {self.target_ssid}"

    def restore(self) -> str:
        if not self.active or not self.previous_ssid or self.previous_ssid == self.target_ssid:
            return ""
        debug_log(self.debug, f"wifi restore ssid={self.previous_ssid!r}")
        if self._connect_with_system_tools(self.previous_ssid) or self._connect_with_pywifi(self.previous_ssid):
            return f"restored SSID {self.previous_ssid}"
        return f"failed to restore SSID {self.previous_ssid}"


def ping_once(address: str) -> tuple[bool, Optional[float]]:
    if is_windows_like():
        cmd = ["ping", "-n", "1", "-w", "1000", address]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", address]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
    except Exception:
        return False, None
    ok = proc.returncode == 0
    match = re.search(r"time[=<]([0-9]+(?:\.[0-9]+)?)\s*ms", proc.stdout or "", re.I)
    rtt = float(match.group(1)) if match else None
    return ok, rtt


class PaneMonitor(threading.Thread):
    def __init__(
        self,
        name: str,
        addresses: list[str],
        login: Optional[tuple[str, str]],
        updates: queue.Queue[PaneSnapshot],
        stop_event: threading.Event,
        debug: bool = False,
    ) -> None:
        super().__init__(daemon=True)
        self.name = name
        self.addresses = addresses
        self.http_address = addresses[0] if addresses else ""
        self.login = login
        self.updates = updates
        self.stop_event = stop_event
        self.debug = debug
        self.ping_states = [PingState(address=addr) for addr in addresses]
        self.last_fetch_at = 0.0
        self.last_status_text = ""
        self.last_bridge_text = ""
        self.last_fetch_note = ""
        self.last_bridge_payload: dict[str, Any] = {}
        self.last_summary_text = f"{self.name}: waiting"
        self.client: Optional[OmadaSession] = None

    def _get_client(self) -> OmadaSession:
        if self.client is None:
            debug_log(self.debug, f"{self.name} creating session for {self.http_address}")
            self.client = OmadaSession(self.http_address, self.login, debug=self.debug)
        return self.client

    def run(self) -> None:
        while not self.stop_event.is_set():
            for state in self.ping_states:
                ok, rtt = ping_once(state.address)
                state.update(ok, rtt)
                debug_log(self.debug, f"{self.name} ping {state.address} ok={ok} rtt_ms={rtt}")
            snapshot = PaneSnapshot(
                name=self.name,
                addresses=self.addresses,
                pings=[PingState(address=state.address, last_probe_at=state.last_probe_at, last_ok_at=state.last_ok_at, last_probe_ok=state.last_probe_ok, last_rtt_ms=state.last_rtt_ms) for state in self.ping_states],
                status_text=self.last_status_text,
                bridge_text=self.last_bridge_text,
                fetch_note=self.last_fetch_note,
                summary_text=self.last_summary_text,
            )
            if self.http_address and (time.time() - self.last_fetch_at) >= FETCH_INTERVAL_SECONDS:
                self.last_fetch_at = time.time()
                try:
                    debug_log(self.debug, f"{self.name} fetch start http_address={self.http_address}")
                    client = self._get_client()
                    _status_payload, bridge_payload, status_text, bridge_text, note = client.relevant_pages()
                    self.last_status_text = status_text or "(no status text extracted)"
                    self.last_bridge_text = bridge_text or "(no bridge text extracted)"
                    self.last_bridge_payload = bridge_payload if isinstance(bridge_payload, dict) else {}
                    self.last_fetch_note = note
                    debug_log(self.debug, f"{self.name} fetch success")
                except Exception as exc:
                    self.client = None
                    self.last_status_text = f"fetch failed for {self.http_address}\n{exc}"
                    self.last_bridge_text = self.last_status_text
                    self.last_fetch_note = ""
                    self.last_bridge_payload = {}
                    debug_log(self.debug, f"{self.name} fetch failed; session reset: {exc}")
            snapshot.status_text = self.last_status_text
            snapshot.bridge_text = self.last_bridge_text
            snapshot.fetch_note = self.last_fetch_note
            snapshot.summary_text = summarize_link_state(self.name, snapshot.pings, self.last_bridge_payload)
            self.last_summary_text = snapshot.summary_text
            snapshot.updated_at = dt.datetime.now().astimezone()
            self.updates.put(snapshot)
            self.stop_event.wait(1.0)


class PaneWidgets:
    def __init__(self, parent: tk.Widget, title: str) -> None:
        frame = ttk.LabelFrame(parent, text=title)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.frame = frame

        self.summary_var = tk.StringVar(value="")
        self.summary_label = tk.Label(frame, textvariable=self.summary_var, relief="solid", borderwidth=1, padx=8, pady=8, font=("TkDefaultFont", 12, "bold"), anchor="w", justify="left", bg="#e2e8f0")
        self.summary_label.pack(fill="x", padx=6, pady=(4, 6))

        self.badge_frame = ttk.Frame(frame)
        self.badge_frame.pack(fill="x", padx=6, pady=(0, 6))
        self.badges: dict[str, tk.Label] = {}

        self.note_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.note_var).pack(fill="x", padx=6, pady=(0, 4))

        split = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        split.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        left = ttk.LabelFrame(split, text="Status")
        right = ttk.LabelFrame(split, text="Bridge / Device")
        split.add(left, weight=1)
        split.add(right, weight=1)

        self.status_text = scrolledtext.ScrolledText(left, wrap="word", font=("TkFixedFont", 10))
        self.status_text.pack(fill="both", expand=True)
        self.bridge_text = scrolledtext.ScrolledText(right, wrap="word", font=("TkFixedFont", 10))
        self.bridge_text.pack(fill="both", expand=True)

        for widget in (self.status_text, self.bridge_text):
            widget.configure(state="disabled")

    def _set_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def update(self, snapshot: PaneSnapshot) -> None:
        current = set()
        for state in snapshot.pings:
            current.add(state.address)
            badge = self.badges.get(state.address)
            if badge is None:
                badge = tk.Label(self.badge_frame, relief="solid", borderwidth=1, padx=8, pady=6, font=("TkDefaultFont", 10, "bold"), anchor="w", justify="left")
                badge.pack(side="left", padx=(0, 4))
                self.badges[state.address] = badge
            color = {
                "ok": "#bbf7d0",
                "missed": "#fde68a",
                "down": "#fbcfe8",
                "waiting": "#e2e8f0",
            }.get(state.visual_state(), "#e2e8f0")
            label_text = state.address
            if state.last_rtt_ms is not None and state.visual_state() == "ok":
                label_text = f"{state.address}  {state.last_rtt_ms:.1f} ms"
            badge.configure(text=label_text, bg=color, width=28)
        for address in list(self.badges):
            if address not in current:
                self.badges[address].destroy()
                del self.badges[address]
        summary_lower = (snapshot.summary_text or "").lower()
        summary_bg = "#e2e8f0"
        if "link up" in summary_lower:
            summary_bg = "#bbf7d0"
        elif "partial" in summary_lower:
            summary_bg = "#fde68a"
        elif "link down" in summary_lower:
            summary_bg = "#fbcfe8"
        self.summary_var.set(snapshot.summary_text or "")
        self.summary_label.configure(bg=summary_bg)
        self.note_var.set(snapshot.fetch_note or "")
        if snapshot.status_text:
            self._set_text(self.status_text, snapshot.status_text)
        if snapshot.bridge_text:
            self._set_text(self.bridge_text, snapshot.bridge_text)



class LinkTestApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.updates: queue.Queue[PaneSnapshot] = queue.Queue()
        self.stop_event = threading.Event()
        self.wifi = WiFiManager(args.ssid, debug=args.debug)
        self.root = tk.Tk()
        self.root.title("linktest")
        self.root.geometry("1400x900")

        self.banner_var = tk.StringVar(value="")
        self.banner_label = tk.Label(self.root, textvariable=self.banner_var, relief="solid", borderwidth=1, padx=10, pady=8, font=("TkDefaultFont", 12, "bold"), anchor="w", justify="left", bg="#e2e8f0")
        self.banner_label.pack(fill="x", padx=8, pady=(8, 0))

        vertical = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        vertical.pack(fill="both", expand=True)

        top = ttk.Frame(vertical)
        bottom = ttk.Frame(vertical)
        vertical.add(top, weight=1)
        vertical.add(bottom, weight=1)

        self.main_widgets = PaneWidgets(top, "Main AP")
        self.client_widgets = PaneWidgets(bottom, "Client AP")

        login = tuple(args.login) if args.login else None
        self.monitors = [
            PaneMonitor("Main AP", args.main_ap, login, self.updates, self.stop_event, debug=args.debug),
            PaneMonitor("Client AP", args.client_ap, login, self.updates, self.stop_event, debug=args.debug),
        ]
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda _s, _f: self.close())
            except Exception:
                pass

    def start(self) -> None:
        wifi_note = self.wifi.connect()
        if wifi_note:
            self.banner_var.set(wifi_note)
            self.banner_label.configure(bg="#fde68a" if "failed" in wifi_note.lower() else "#bbf7d0")
        else:
            self.banner_var.set("Ready")
            self.banner_label.configure(bg="#e2e8f0")
        for monitor in self.monitors:
            monitor.start()
        self.root.after(200, self.drain_updates)
        self.root.mainloop()

    def drain_updates(self) -> None:
        try:
            while True:
                snapshot = self.updates.get_nowait()
                if snapshot.name == "Main AP":
                    self.main_widgets.update(snapshot)
                elif snapshot.name == "Client AP":
                    self.client_widgets.update(snapshot)
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(200, self.drain_updates)

    def close(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        restore_note = self.wifi.restore()
        if restore_note:
            self.banner_var.set(restore_note)
            self.banner_label.configure(bg="#fde68a" if "failed" in restore_note.lower() else "#bbf7d0")
            self.root.update_idletasks()
            self.root.after(250, self.root.destroy)
        else:
            self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor a TP-Link Omada bridge setup from the client side.")
    parser.add_argument("--main_ap", nargs="+", required=True, help="Space-separated IP addresses to monitor from the Main AP side.")
    parser.add_argument("--client_ap", nargs="+", required=True, help="Space-separated IP addresses to monitor from the Client AP side.")
    parser.add_argument("--ssid", default=None, help="Optional management SSID to join for testing.")
    parser.add_argument("--login", nargs=2, metavar=("USERNAME", "PASSWORD"), help="Login credentials for the AP web UI.")
    parser.add_argument("--debug", action="store_true", help="Print HTTP/ping/debug progress to stderr.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = LinkTestApp(args)
    app.start()


if __name__ == "__main__":
    main()
