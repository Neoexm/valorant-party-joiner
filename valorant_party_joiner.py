import base64
import csv
import http.client
import json
import math
import os
import queue
import re
import ssl
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote


ROUTES = {
    "Europe": ("eu", "eu"),
    "North America": ("na", "na"),
    "Latin America": ("latam", "na"),
    "Brazil": ("br", "na"),
    "Asia Pacific": ("ap", "ap"),
    "Korea": ("kr", "kr"),
    "PBE": ("na", "pbe"),
}
ALLOWED_ROUTES = set(ROUTES.values())
HOST_PATTERN = re.compile(r"glz-(na|latam|br|eu|ap|kr)-1\.(na|pbe|eu|ap|kr)\.a\.pvp\.net")
VERSION_PATTERN = re.compile(r"\b(?:release|pbe)-[A-Za-z0-9_.-]+-shipping-[A-Za-z0-9_.-]+", re.I)
PLATFORM = base64.b64encode(json.dumps({
    "platformType": "PC",
    "platformOS": "Windows",
    "platformOSVersion": "10.0.19042.1.256.64bit",
    "platformChipset": "Unknown",
}, separators=(",", ":")).encode()).decode()


class StopReason(Exception):
    pass


class ConnectionProblem(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status, code="", retry_after=None, local=False):
        self.status = status
        self.code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else ""
        self.retry_after = retry_after
        self.local = local
        super().__init__(f"HTTP {status}" + (f" ({self.code})" if self.code else ""))


@dataclass(frozen=True)
class Lockfile:
    pid: int
    port: int
    password: str = field(repr=False)


@dataclass(frozen=True)
class Party:
    id: str
    invite_code: str = field(repr=False)
    member: bool


@dataclass(frozen=True)
class Result:
    status: str
    message: str
    attempts: int


class RetryDelay:
    def __init__(self, seconds=5):
        self.lock = threading.Lock()
        self.set(seconds)

    def set(self, seconds):
        if not math.isfinite(seconds) or not 0.1 <= seconds <= 300:
            raise ValueError("Use a retry delay between 0.1 and 300 seconds.")
        with self.lock:
            self.seconds = seconds

    def get(self):
        with self.lock:
            return self.seconds


def valid_id(value):
    if not isinstance(value, str):
        return False
    try:
        return uuid.UUID(value).int != 0
    except ValueError:
        return False


def validate_code(value):
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Paste the party code only: letters, numbers, hyphens or underscores.")
    return value


def retry_delay(value, fallback, now=None):
    if value is None:
        return fallback
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            seconds = (target - current).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    if not math.isfinite(seconds) or seconds < 0:
        return fallback
    return max(fallback, seconds)


def read_lockfile(path):
    try:
        parts = path.read_text(encoding="utf-8").strip().split(":")
    except OSError:
        raise StopReason("Riot Client was not found. Open VALORANT and sign in, then press Start.") from None
    try:
        if len(parts) != 5 or parts[4].lower() != "https":
            raise ValueError
        pid, port = int(parts[1]), int(parts[2])
        if pid < 1 or not 1 <= port <= 65535 or not parts[3]:
            raise ValueError
        return Lockfile(pid, port, parts[3])
    except ValueError:
        raise StopReason("The Riot Client lockfile is not ready. Wait for the lobby, then try again.") from None


def game_is_running():
    if os.name != "nt":
        raise StopReason("This tool runs on the Windows PC where VALORANT is open.")
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq VALORANT-Win64-Shipping.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise StopReason("Windows could not check whether VALORANT is running.") from None
    if result.returncode:
        raise StopReason("Windows could not check whether VALORANT is running.")
    return any(row and row[0].lower() == "valorant-win64-shipping.exe" for row in csv.reader(result.stdout.splitlines()))


def read_game_log(path):
    try:
        with path.open("rb") as stream:
            start = stream.read(2 * 1024 * 1024)
            size = stream.seek(0, 2)
            if size > len(start):
                stream.seek(max(len(start), size - 2 * 1024 * 1024))
                start += b"\n" + stream.read(2 * 1024 * 1024)
        return start.decode("utf-8", errors="replace")
    except OSError:
        return ""


def select_session(data):
    if not isinstance(data, dict):
        raise StopReason("Riot Client returned an unexpected session response.")
    matches = []
    for key, item in data.items():
        if not isinstance(item, dict):
            continue
        config = item.get("launchConfiguration") or {}
        executable = str(config.get("executable", "")) if isinstance(config, dict) else ""
        if str(item.get("productId", "")).lower() == "valorant" or "valorant-win64-shipping.exe" in executable.lower():
            matches.append((key, item))
    if not matches:
        raise StopReason("VALORANT's session was not found. Wait for the main menu, then press Start.")
    if len(matches) != 1:
        raise StopReason("More than one VALORANT session was found. Close the extra client, then try again.")
    return matches[0]


def session_details(session, log, region="Auto"):
    version = session.get("version", "")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        versions = VERSION_PATTERN.findall(log)
        version = versions[-1] if versions else ""
    if not version:
        raise StopReason("Could not detect the game version. Restart VALORANT to refresh its session and log.")
    if region != "Auto":
        if region not in ROUTES:
            raise StopReason("Choose a region from the list.")
        return ROUTES[region], version
    if str(session.get("patchlineId", "")).lower() == "pbe":
        return ROUTES["PBE"], version
    config = session.get("launchConfiguration") or {}
    arguments = config.get("arguments", []) if isinstance(config, dict) else []
    if isinstance(arguments, list):
        for argument in arguments:
            match = re.fullmatch(r"-ares-deployment=(na|latam|br|eu|ap|kr)", str(argument), re.I)
            if match:
                value = match.group(1).lower()
                return (value, "na" if value in {"na", "latam", "br"} else value), version
    matches = list(HOST_PATTERN.finditer(log))
    for match in reversed(matches):
        route = match.groups()
        if route in ALLOWED_ROUTES:
            return route, version
    raise StopReason("Could not detect your account region. Select it above, then press Start.")


class Transport:
    def __init__(self):
        self.remote_context = ssl.create_default_context()
        self.local_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.local_context.check_hostname = False
        self.local_context.verify_mode = ssl.CERT_NONE
        self.connections = {}

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()

    def request(self, host, port, path, headers, method="GET", local=False):
        if local:
            if host != "127.0.0.1":
                raise StopReason("The local connection must stay on this PC.")
        else:
            match = HOST_PATTERN.fullmatch(host)
            if not match or match.groups() not in ALLOWED_ROUTES or port != 443:
                raise StopReason("The server address is not a supported Riot endpoint.")
        key = (host, port, local)
        connection = self.connections.get(key)
        if connection is None:
            connection = http.client.HTTPSConnection(host, port, timeout=8, context=self.local_context if local else self.remote_context)
            self.connections[key] = connection
        try:
            connection.request(method, path, body=b"" if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            status = response.status
            retry_after = response.getheader("Retry-After")
            raw = response.read(1024 * 1024 + 1)
        except ssl.SSLCertVerificationError:
            self.connections.pop(key, None)
            connection.close()
            raise StopReason("Riot's HTTPS certificate could not be verified. Check your PC's clock and network.") from None
        except (OSError, http.client.HTTPException):
            self.connections.pop(key, None)
            connection.close()
            location = "Riot Client" if local else "Riot's servers"
            raise ConnectionProblem(f"No response from {location}.") from None
        if response.will_close or len(raw) > 1024 * 1024:
            self.connections.pop(key, None)
            connection.close()
        if len(raw) > 1024 * 1024:
            raise StopReason("The server returned an unexpectedly large response.")
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            data = None
        if not 200 <= status < 300:
            code = data.get("errorCode", "") if isinstance(data, dict) else ""
            raise ApiError(status, code, retry_after, local)
        if not isinstance(data, dict):
            raise StopReason("The client interface returned an unexpected response. Check VALORANT before restarting retries.")
        if data.get("errorCode"):
            raise StopReason("Riot returned an error inside a success response. Check VALORANT before restarting retries.")
        return data


class RiotClient:
    def __init__(self, region="Auto", root=None, transport=None, running=None):
        self.root = Path(root or os.environ.get("LOCALAPPDATA", ""))
        self.lock_path = self.root / "Riot Games" / "Riot Client" / "Config" / "lockfile"
        self.log_path = self.root / "VALORANT" / "Saved" / "Logs" / "ShooterGame.log"
        self.region = region
        self.transport = transport or Transport()
        self.running = running or game_is_running
        self.lock = None
        self.session_key = None
        self.subject = None
        self.route = None
        self.version = None
        self.headers = {}
        self.allow_cache = False
        self.prepared_at = None
        self.party_at = None
        self.cached_party = None

    def local(self, path):
        basic = base64.b64encode(("riot:" + self.lock.password).encode()).decode()
        return self.transport.request("127.0.0.1", self.lock.port, path, {"Authorization": "Basic " + basic}, local=True)

    def prepare(self):
        lock = read_lockfile(self.lock_path)
        if self.lock is not None and lock != self.lock:
            raise StopReason("Riot Client restarted. Press Start again when you are back in the lobby.")
        self.lock = lock
        if self.allow_cache and self.headers and self.prepared_at is not None and time.monotonic() - self.prepared_at < 2:
            return
        if not self.running():
            raise StopReason("VALORANT is closed. Retries stopped.")
        key, session = select_session(self.local("/product-session/v1/external-sessions"))
        if self.session_key is not None and key != self.session_key:
            raise StopReason("The game session changed. Retries stopped.")
        self.session_key = key
        if self.route is None:
            self.route, self.version = session_details(session, read_game_log(self.log_path), self.region)
        self.refresh_auth()
        self.prepared_at = time.monotonic()

    def refresh_auth(self):
        data = self.local("/entitlements/v1/token")
        subject = data.get("subject")
        if not valid_id(subject) or not isinstance(data.get("accessToken"), str) or not isinstance(data.get("token"), str):
            raise StopReason("Your Riot session is not ready. Wait until you are signed in and in the lobby.")
        if not data["accessToken"] or not data["token"]:
            raise StopReason("Riot Client has not supplied session tokens yet. Wait for the main menu.")
        if self.subject is not None and subject != self.subject:
            raise StopReason("The signed-in Riot account changed. Retries stopped.")
        self.subject = subject
        self.headers = {
            "Authorization": "Bearer " + data["accessToken"],
            "X-Riot-Entitlements-JWT": data["token"],
            "X-Riot-ClientPlatform": PLATFORM,
            "X-Riot-ClientVersion": self.version,
            "Accept": "application/json",
        }

    def invalidate_auth(self):
        self.allow_cache = False
        self.headers.clear()

    def remote(self, path, method="GET"):
        region, shard = self.route
        return self.transport.request(f"glz-{region}-1.{shard}.a.pvp.net", 443, path, self.headers, method)

    def snapshot(self):
        if self.allow_cache and self.cached_party is not None and time.monotonic() - self.party_at < 2:
            return self.cached_party
        data = self.remote("/parties/v1/players/" + self.subject)
        if data.get("Subject") != self.subject:
            raise StopReason("The party response did not match your signed-in account.")
        party_id = data.get("CurrentPartyID")
        if not valid_id(party_id):
            raise ConnectionProblem("Waiting for VALORANT to create your lobby.")
        party = self.remote("/parties/v1/parties/" + party_id)
        if party.get("ID") != party_id or not isinstance(party.get("Members"), list):
            raise ConnectionProblem("The party changed while it was being checked.")
        member = any(isinstance(item, dict) and item.get("Subject") == self.subject for item in party["Members"])
        if not member:
            raise ConnectionProblem("Waiting for your party membership to settle.")
        code = party.get("InviteCode", "")
        self.cached_party = Party(party_id, code if isinstance(code, str) else "", member)
        self.party_at = time.monotonic()
        return self.cached_party

    def join(self, code):
        self.allow_cache = False
        try:
            data = self.remote("/parties/v1/players/joinbycode/" + quote(code, safe=""), "POST")
        except ApiError as error:
            self.allow_cache = error.status == 403 and error.code == "PARTY_FULL" and not error.local
            raise
        if data.get("Subject") == self.subject and valid_id(data.get("CurrentPartyID")):
            return data["CurrentPartyID"]
        return None

    def clear(self):
        self.transport.close()
        self.headers.clear()
        self.lock = None
        self.subject = None


class JoinWorker:
    def __init__(self, client, stop, emit, wait=None):
        self.client = client
        self.stop = stop
        self.emit = emit
        self.wait = wait

    def _pause(self, timing, minimum=0, fixed=None, started=None):
        now = time.monotonic()
        anchor = now if started is None else started

        def duration():
            return fixed if fixed is not None else max(anchor + timing.get() - now, minimum, 0)

        if self.wait is not None:
            return self.wait(duration())
        while not self.stop.is_set():
            remaining = duration() - (time.monotonic() - now)
            if remaining <= 0:
                return False
            if self.stop.wait(min(0.2, remaining)):
                return True
        return True

    def run(self, code, interval=5):
        attempts = 0
        baseline = None
        expected = None
        accepted = False
        confirmations = 0
        failures = 0
        auth_failures = 0
        connected = False
        previous_attempt = None
        try:
            code = validate_code(code)
            timing = interval if isinstance(interval, RetryDelay) else RetryDelay(interval)
            while not self.stop.is_set():
                interval = timing.get()
                minimum = 0
                fixed = None
                started = None
                stage = "check"
                try:
                    self.client.prepare()
                    if self.stop.is_set():
                        break
                    if not connected:
                        self.emit("log", "Connected to the running VALORANT session.")
                        connected = True
                    party = self.client.snapshot()
                    if self.stop.is_set():
                        break
                    if party.member and party.invite_code == code:
                        return Result("joined", "You are in the party. Retries stopped.", attempts)
                    if baseline is None:
                        baseline = party.id
                    if accepted:
                        if expected and expected != baseline and party.id == expected and party.member:
                            return Result("joined", "Joined the party and confirmed your membership.", attempts)
                        confirmations += 1
                        if party.id != baseline or confirmations >= 3:
                            return Result("check", "The join was accepted, but the target could not be confirmed. Check VALORANT; retries stopped.", attempts)
                        self.emit("status", "Join accepted. Waiting for membership confirmation...")
                        fixed = 3
                    else:
                        if party.id != baseline:
                            return Result("check", "Your party changed. Check VALORANT; retries stopped.", attempts)
                        if self.stop.is_set():
                            break
                        self.emit("status", f"Trying to join - attempt {attempts + 1}")
                        if self.stop.is_set():
                            break
                        attempts += 1
                        stage = "join"
                        started = time.monotonic()
                        try:
                            expected = self.client.join(code)
                        finally:
                            self.emit("attempt", str(attempts))
                            elapsed = time.monotonic() - started
                            gap = "first attempt" if previous_attempt is None else f"actual gap {started - previous_attempt:.3f}s"
                            self.emit("timing", f"{gap} | response {elapsed:.3f}s")
                            previous_attempt = started
                        accepted = True
                        self.emit("log", f"Attempt {attempts}: request accepted; checking membership.")
                        fixed = 3
                    failures = 0
                    auth_failures = 0
                except ApiError as error:
                    if self.stop.is_set():
                        break
                    label = "Client check" if stage == "check" else f"Attempt {attempts}"
                    if error.status == 401:
                        auth_failures += 1
                        if auth_failures >= 2:
                            return Result("error", "Riot still rejected the refreshed session. Restart the game and try again.", attempts)
                        self.client.invalidate_auth()
                        self.emit("log", "Refreshing authentication from Riot Client.")
                    elif error.status == 403 and error.code == "PARTY_FULL" and stage == "join" and not error.local:
                        delay = retry_delay(error.retry_after, interval)
                        minimum = delay if error.retry_after is not None else 0
                        self.emit("log", f"{label}: party is full (HTTP 403, PARTY_FULL). Target interval {interval:g}s.")
                    elif error.status == 403:
                        return Result("error", f"{label}: {error}. Riot denied this operation; retries stopped.", attempts)
                    elif error.status == 429 or 500 <= error.status <= 599 or error.status == 408:
                        failures += 1
                        fallback = min(120, max(interval, 15 if error.status == 429 else 5) * 2 ** min(failures - 1, 5))
                        delay = retry_delay(error.retry_after, fallback)
                        minimum = delay
                        self.emit("log", f"{label}: {error}. Waiting {delay:g} seconds.")
                    elif error.status in {400, 404, 409, 422} and not error.local:
                        delay = retry_delay(error.retry_after, interval)
                        minimum = delay if error.retry_after is not None else 0
                        self.emit("log", f"{label}: {error}. Retrying in {delay:g} seconds.")
                    else:
                        return Result("error", f"{label}: {error}. Retries stopped.", attempts)
                    self.emit("status", f"Server cooldown: {minimum:g}s" if minimum else f"Retrying with a target interval of {interval:g}s")
                except ConnectionProblem as error:
                    if self.stop.is_set():
                        break
                    failures += 1
                    delay = min(60, max(interval, 5) * 2 ** min(failures - 1, 4))
                    minimum = delay
                    self.emit("log", f"{error} Rechecking your party in {delay:g} seconds.")
                    self.emit("status", "Waiting to recheck the connection and your party")
                if self.stop.is_set() or self._pause(timing, minimum, fixed, started):
                    break
            return Result("stopped", "Stopped. No further join attempts will be sent.", attempts)
        except (StopReason, ValueError) as error:
            return Result("error", str(error), attempts)
        except Exception as error:
            return Result("error", f"Unexpected {type(error).__name__}. Retries stopped.", attempts)
        finally:
            self.client.clear()


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.closing = False
        self.started = None
        self.delay = RetryDelay(5)
        root.title("VALORANT Party Joiner")
        root.geometry("720x610")
        root.minsize(700, 570)
        root.configure(bg="#111923")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#111923")
        style.configure("TLabel", background="#111923", foreground="#edf0f4", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("Muted.TLabel", foreground="#a8b6c9")
        style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(14, 9))
        style.configure("Start.TButton", background="#ff4655", foreground="#ffffff")
        style.map("Start.TButton", background=[("active", "#e63b4b"), ("disabled", "#384350")])
        style.configure("TCheckbutton", background="#111923", foreground="#edf0f4", font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", "#111923")])
        style.configure("TEntry", padding=7, font=("Segoe UI", 11))
        style.configure("TCombobox", padding=6)
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(8, weight=1)
        ttk.Label(frame, text="Party joiner", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Keep trying one code while VALORANT is open.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 20))
        ttk.Label(frame, text="PARTY CODE").grid(row=2, column=0, sticky="w")
        code_frame = ttk.Frame(frame)
        code_frame.grid(row=3, column=0, sticky="ew", pady=(6, 15))
        code_frame.columnconfigure(0, weight=1)
        self.code = tk.StringVar()
        self.code_entry = ttk.Entry(code_frame, textvariable=self.code, show="*", font=("Consolas", 13))
        self.code_entry.grid(row=0, column=0, sticky="ew")
        self.show_code = tk.BooleanVar()
        ttk.Checkbutton(code_frame, text="Show", variable=self.show_code, command=lambda: self.code_entry.configure(show="" if self.show_code.get() else "*")).grid(row=0, column=1, padx=(12, 0))
        options = ttk.Frame(frame)
        options.grid(row=4, column=0, sticky="ew")
        ttk.Label(options, text="Target interval").grid(row=0, column=0, sticky="w")
        self.interval = tk.StringVar(value="5")
        self.interval_entry = ttk.Spinbox(options, textvariable=self.interval, from_=0.1, to=300, increment=0.1, width=6)
        self.interval_entry.grid(row=0, column=1, padx=8)
        ttk.Label(options, text="seconds", style="Muted.TLabel").grid(row=0, column=2)
        self.apply_button = ttk.Button(options, text="Apply", command=self.apply_delay, padding=(10, 5))
        self.apply_button.grid(row=0, column=3, padx=(8, 0))
        self.interval_entry.bind("<Return>", lambda event: self.apply_delay())
        ttk.Label(options, text="Region").grid(row=0, column=4, padx=(20, 8))
        self.region = tk.StringVar(value="Auto")
        self.region_box = ttk.Combobox(options, textvariable=self.region, values=["Auto", *ROUTES], state="readonly", width=17)
        self.region_box.grid(row=0, column=5, sticky="e")
        controls = ttk.Frame(frame)
        controls.grid(row=5, column=0, sticky="ew", pady=(19, 15))
        self.start_button = ttk.Button(controls, text="Start retrying", style="Start.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.attempts = tk.StringVar(value="0 attempts")
        ttk.Label(controls, textvariable=self.attempts, style="Muted.TLabel").pack(side="right")
        self.status = tk.StringVar(value="Open the game, paste a code, then press Start.")
        self.status_label = ttk.Label(frame, textvariable=self.status, wraplength=615)
        self.status_label.grid(row=6, column=0, sticky="ew", pady=(0, 6))
        self.elapsed = tk.StringVar(value="")
        timing_frame = ttk.Frame(frame)
        timing_frame.grid(row=7, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(timing_frame, textvariable=self.elapsed, style="Muted.TLabel").pack(side="left")
        self.actual_timing = tk.StringVar(value="")
        ttk.Label(timing_frame, textvariable=self.actual_timing, style="Muted.TLabel").pack(side="right")
        self.log = ScrolledText(frame, height=7, wrap="word", bg="#0a1018", fg="#aebdd0", insertbackground="white", relief="flat", font=("Consolas", 9), padx=10, pady=10, state="disabled")
        self.log.grid(row=8, column=0, sticky="nsew")
        ttk.Label(frame, text="Change the interval and press Apply, even while running.\nSlower responses and server cooldowns increase the actual interval.\nUnofficial client interface; not approved or guaranteed safe by Riot.", style="Muted.TLabel", wraplength=650).grid(row=9, column=0, sticky="w", pady=(14, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda event: self.stop())
        self.code_entry.focus_set()
        root.after(100, self.poll)

    def add_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", datetime.now().strftime("%H:%M:%S.%f")[:-3] + " " + message + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "51.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        from tkinter import messagebox

        if self.thread is not None:
            return
        try:
            code = validate_code(self.code.get())
            self.delay.set(float(self.interval.get()))
        except ValueError as error:
            messagebox.showerror("Check the settings", str(error), parent=self.root)
            return
        self.stop_event = threading.Event()
        self.started = time.monotonic()
        self.attempts.set("0 attempts")
        self.actual_timing.set("")
        self.status.set("Connecting to VALORANT...")
        self.status_label.configure(foreground="#edf0f4")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.code_entry.configure(state="disabled")
        self.region_box.configure(state="disabled")
        self.add_log("Started.")
        region = self.region.get()
        self.thread = threading.Thread(target=self.work, args=(code, self.delay, region), daemon=True)
        self.thread.start()

    def apply_delay(self):
        from tkinter import messagebox

        try:
            value = float(self.interval.get())
            self.delay.set(value)
        except ValueError:
            messagebox.showerror("Check the delay", "Enter a number between 0.1 and 300 seconds, such as 0.1, 1.5 or 5.", parent=self.root)
            return
        self.interval.set(f"{value:g}")
        self.add_log(f"Target interval set to {value:g}s.")
        if self.thread is not None and not self.stop_event.is_set():
            self.status.set(f"Target interval set to {value:g}s")

    def work(self, code, interval, region):
        try:
            client = RiotClient(region)
            worker = JoinWorker(client, self.stop_event, lambda kind, message: self.events.put((kind, message)))
            result = worker.run(code, interval)
        except Exception as error:
            result = Result("error", f"Could not start: {type(error).__name__}.", 0)
        self.events.put(("done", result))

    def stop(self):
        if self.thread is None:
            return
        self.stop_event.set()
        self.status.set("Stopping. An in-flight request may still finish...")
        self.stop_button.configure(state="disabled")

    def close(self):
        if self.thread is None:
            self.root.destroy()
        else:
            self.closing = True
            self.stop()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.add_log(value)
                elif kind == "status" and not self.stop_event.is_set():
                    self.status.set(value)
                elif kind == "attempt":
                    self.attempts.set(value + (" attempt" if value == "1" else " attempts"))
                elif kind == "timing":
                    self.actual_timing.set(value)
                elif kind == "done":
                    self.thread = None
                    self.status.set(value.message)
                    self.add_log(value.message)
                    self.status_label.configure(foreground="#61d7a9" if value.status == "joined" else "#edf0f4")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.code_entry.configure(state="normal")
                    self.region_box.configure(state="readonly")
                    if self.closing:
                        self.root.destroy()
                        return
                    if value.status == "joined":
                        self.root.bell()
        except queue.Empty:
            pass
        if self.thread is not None and self.started is not None:
            elapsed = int(time.monotonic() - self.started)
            self.elapsed.set(f"Running for {elapsed // 60}:{elapsed % 60:02d}")
        self.root.after(100, self.poll)


def main():
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    if os.name != "nt":
        root.withdraw()
        messagebox.showerror("Windows required", "Run this tool on your Windows PC with VALORANT open.")
        root.destroy()
        return
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
