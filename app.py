#!/usr/bin/env python3
"""TvHeadend Manager：一个无运行时第三方依赖的 Tvheadend 管理面板。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import signal
import secrets
import socket
import ssl
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_VERSION = "1.1.1"
LOG = logging.getLogger("tvheadend-manager")
DATA_DIR = Path(os.getenv("TVHMON_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "tvheadend-manager.db"
LEGACY_DB_PATH = DATA_DIR / "tvh-insight.db"
STATIC_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "static"
POLL_SECONDS = max(5, int(os.getenv("TVHMON_POLL_SECONDS", "10")))
FULL_SYNC_SECONDS = max(60, int(os.getenv("TVHMON_FULL_SYNC_SECONDS", "300")))
WEB_SESSION_SECONDS = max(300, int(os.getenv("TVHMON_WEB_SESSION_SECONDS", "43200")))
WEB_SESSION_COOKIE = "tvhmon_session"

WEB_SESSIONS: dict[str, int] = {}
WEB_SESSION_LOCK = threading.RLock()
LOGIN_FAILURES: dict[str, tuple[int, int]] = {}
LOGIN_FAILURE_LOCK = threading.RLock()
FORWARD_GATEWAY: "GatewayManager | None" = None
INTERNAL_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
))
FORWARD_PATHS = tuple(re.compile(pattern) for pattern in (
    r"^/playlist(?:/(?:auth|ticket))?(?:/(?:m3u|e2|satip))?/channels$",
    r"^/playlist(?:/(?:auth|ticket))?(?:/(?:m3u|e2|satip))?/(?:channelid|channelnumber|channelname|channel)/[^/]+$",
    r"^/xmltv/(?:channels|channelid/[^/]+|channelnumber/[^/]+|channelname/[^/]+|channel/[^/]+)$",
    r"^/stream/(?:channelid|channelnumber|channelname|channel)/[^/]+$",
    r"^/imagecache/[^/]+$",
))
FORWARD_QUERY_KEYS = {"auth", "ticket", "profile", "sort", "lang", "weight", "qsize", "timeshift", "descramble"}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS viewing_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_key TEXT NOT NULL,
  username TEXT NOT NULL, channel_name TEXT NOT NULL, client_ip TEXT NOT NULL DEFAULT '', client TEXT,
  started_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
  ended_at INTEGER, duration_seconds INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_viewing_active ON viewing_sessions(active, subscription_key);
CREATE INDEX IF NOT EXISTS idx_viewing_started ON viewing_sessions(started_at DESC);
"""

OBSOLETE_TABLES = ("channels", "epg_events", "input_samples", "current_inputs", "resource_snapshots", "sync_runs")


def now() -> int:
    return int(time.time())


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.strip().split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            return address.ipv4_mapped
        return address
    except ValueError:
        return None


def address_in_networks(address: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
                        networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    return bool(address and any(address.version == network.version and address in network for network in networks))


def is_internal_address(value: str) -> bool:
    return address_in_networks(parse_ip(value), INTERNAL_NETWORKS)


def trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for value in os.getenv("TVHMON_TRUSTED_PROXIES", "").split(","):
        if not value.strip():
            continue
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            LOG.warning("忽略无效的 TVHMON_TRUSTED_PROXIES 地址段：%s", value.strip())
    return tuple(networks)


def scalar(value: Any, default: Any = "") -> Any:
    """Tvheadend occasionally returns localized values as small dictionaries."""
    if value is None:
        return default
    if isinstance(value, dict):
        return next(iter(value.values()), default)
    return value


def redact_sensitive(value: Any, key: str = "", hide_frequency: bool = True) -> Any:
    """Remove stream addresses and tuning frequencies before data reaches the cache."""
    sensitive_keys = {"url", "iptv_url", "playlist_url", "src", "frequency", "freq", "frequency_min", "frequency_max"}
    if key.lower() in sensitive_keys and (hide_frequency or key.lower() not in {"frequency", "freq", "frequency_min", "frequency_max"}):
        return "[已隐藏]"
    if isinstance(value, dict):
        return {k: redact_sensitive(v, k, hide_frequency) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_sensitive(v, key, hide_frequency) for v in value]
    if isinstance(value, str):
        lowered = value.lower()
        if any(scheme in lowered for scheme in ("http://", "https://", "udp://", "rtsp://", "rtmp://", "srt://")):
            return "[流地址已隐藏]"
        if hide_frequency and any(unit in lowered for unit in ("mhz", "khz", " ghz", " hz")):
            return "[频率已隐藏]"
    return value


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve existing installations after the public project rename.
        if path == DB_PATH and not path.exists() and LEGACY_DB_PATH.exists():
            LEGACY_DB_PATH.replace(path)
            for suffix in ("-wal", "-shm"):
                legacy_sidecar = Path(f"{LEGACY_DB_PATH}{suffix}")
                if legacy_sidecar.exists():
                    legacy_sidecar.replace(Path(f"{path}{suffix}"))
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(viewing_sessions)")}
            if "client_ip" not in columns:
                db.execute("ALTER TABLE viewing_sessions ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''")
            # Versions before 0.2 cached display data in SQLite. Remove those
            # tables during migration so the new data-minimisation promise also
            # applies to existing installations.
            for table in OBSOLETE_TABLES:
                db.execute(f"DROP TABLE IF EXISTS {table}")
            for row in db.execute("SELECT id,subscription_key FROM viewing_sessions").fetchall():
                if "://" in row["subscription_key"]:
                    opaque = hashlib.sha256(row["subscription_key"].encode()).hexdigest()
                    db.execute("UPDATE viewing_sessions SET subscription_key=? WHERE id=?", (opaque, row["id"]))
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def settings(self) -> dict[str, str]:
        with self.connect() as db:
            return {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")}

    def save_settings(self, values: dict[str, str]) -> None:
        ts = now()
        with self.connect() as db:
            db.executemany(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                [(k, v, ts) for k, v in values.items()],
            )

class MemoryCache:
    """Ephemeral Tvheadend state. Nothing in this object is written to disk."""

    def __init__(self):
        self.lock = threading.RLock()
        self.channels: list[dict[str, Any]] = []
        self.epg: list[dict[str, Any]] = []
        self.inputs: list[dict[str, Any]] = []
        self.resources: dict[str, dict[str, Any]] = {}
        self.last_sync: dict[str, Any] | None = None

    def sync_result(self, ok: bool, message: str = "") -> None:
        with self.lock:
            self.last_sync = {"ok": int(ok), "message": message[:1000], "created_at": now()}


CACHE = MemoryCache()


def safe_connection(entry: dict[str, Any], timestamp: int | None = None) -> dict[str, Any] | None:
    """Return only the client-facing connection fields needed by management UI."""
    timestamp = timestamp if timestamp is not None else now()
    try:
        connection_id = int(entry.get("id"))
    except (TypeError, ValueError):
        return None
    peer = str(entry.get("peer") or "").removeprefix("::ffff:")
    try:
        peer = str(ipaddress.ip_address(peer))
    except ValueError:
        peer = "未知地址"
    try:
        started = int(entry.get("started") or timestamp)
    except (TypeError, ValueError):
        started = timestamp
    if started > timestamp or started < timestamp - 365 * 86400:
        started = timestamp
    return {"id": connection_id, "peer": peer, "peer_port": max(0, int(entry.get("peer_port") or 0)),
            "started": started, "streaming": bool(entry.get("streaming")),
            "type": str(redact_sensitive(entry.get("type") or "未知协议"))[:40],
            "user": str(redact_sensitive(entry.get("user") or "匿名"))[:120]}


class TvhClient:
    def __init__(self, url: str, username: str, password: str, timeout: int = 12):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.basic_authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.url, username, password)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPBasicAuthHandler(manager),
        )

    def request(self, endpoint: str, params: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
        method = method.upper()
        if method not in ("GET", "POST"):
            raise ValueError("Tvheadend API 只允许 GET 或 POST")
        encoded = urllib.parse.urlencode(params or {})
        target = f"{self.url}/api/{endpoint}" + (f"?{encoded}" if encoded and method == "GET" else "")
        # Some TVH "plain" configurations return 403 without sending an HTTP
        # authentication challenge. Preemptive Basic is needed for those builds;
        # the opener still handles a Digest challenge when the server sends one.
        headers = {"Accept": "application/json", "User-Agent": f"TvHeadend-Manager/{APP_VERSION}",
                   "Authorization": self.basic_authorization}
        data = encoded.encode() if method == "POST" else None
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(target, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError("认证失败或该用户没有管理员/API 权限") from exc
            raise RuntimeError(f"TvHeadend 返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法连接 TvHeadend：{getattr(exc, 'reason', exc)}") from exc

    def fetch_image(self, source: str) -> tuple[bytes, str]:
        target = urllib.parse.urljoin(self.url + "/", source.lstrip("/"))
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("频道图标地址只允许 HTTP 或 HTTPS")
        if parsed.username or parsed.password:
            raise ValueError("频道图标地址不能包含账号密码")
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii")
            port = parsed.port
        except (UnicodeError, ValueError) as exc:
            raise ValueError("频道图标地址格式无效") from exc
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = hostname + (f":{port}" if port is not None else "")
        # HTTP request lines are ASCII. Keep URL separators and existing percent
        # escapes intact while encoding Unicode filenames and query values.
        target = urllib.parse.urlunsplit((
            parsed.scheme, netloc,
            urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
            urllib.parse.quote(parsed.query, safe="=&;%:+,/?@!$'()*-._~"),
            "",
        ))
        headers = {"User-Agent": f"TvHeadend-Manager/{APP_VERSION}"}
        # External channel logos are allowed, but TVH credentials must only ever
        # be sent back to the configured Tvheadend origin.
        base_origin = urllib.parse.urlsplit(self.url)
        image_origin = urllib.parse.urlsplit(target)
        if (base_origin.scheme, base_origin.netloc) == (image_origin.scheme, image_origin.netloc):
            headers["Authorization"] = self.basic_authorization
        req = urllib.request.Request(target, headers=headers)
        with self.opener.open(req, timeout=self.timeout) as response:
            return response.read(4 * 1024 * 1024), response.headers.get_content_type()


class Collector:
    def __init__(self, store: Store):
        self.store = store
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_full = 0
        self.running_lock = threading.Lock()

    def client(self) -> TvhClient:
        cfg = self.store.settings()
        if not cfg.get("tvh_url"):
            raise RuntimeError("尚未配置 TvHeadend")
        password = os.getenv("TVHMON_TVH_PASSWORD", cfg.get("tvh_password", ""))
        return TvhClient(cfg["tvh_url"], cfg.get("tvh_username", ""), password)

    def start(self) -> None:
        if not self.thread:
            self.thread = threading.Thread(target=self._loop, name="collector", daemon=True)
            self.thread.start()

    def trigger(self) -> None:
        self.last_full = 0
        self.wake_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            if self.store.setting("tvh_url"):
                self.collect_once(force_full=(now() - self.last_full >= FULL_SYNC_SECONDS))
            self.wake_event.wait(self.poll_seconds())
            self.wake_event.clear()

    def poll_seconds(self) -> int:
        try:
            value = int(self.store.setting("poll_seconds", str(POLL_SECONDS)))
        except ValueError:
            value = POLL_SECONDS
        return value if value in (10, 20, 30, 60) else POLL_SECONDS

    def collect_once(self, force_full: bool = False) -> None:
        if not self.running_lock.acquire(blocking=False):
            return
        try:
            client = self.client()
            self._collect_live(client)
            if force_full:
                self._collect_catalog(client)
                self.last_full = now()
            CACHE.sync_result(True)
        except Exception as exc:  # the collector must remain alive after a transient TVH error
            LOG.warning("collection failed: %s", exc)
            CACHE.sync_result(False, str(exc))
        finally:
            self.running_lock.release()

    def _collect_catalog(self, client: TvhClient) -> None:
        channels = client.request("channel/grid", {"limit": 10000, "all": 1}).get("entries", [])
        events = client.request("epg/events/grid", {"limit": 10000}).get("entries", [])
        ts = now()
        safe_channels = []
        for c in channels:
            safe_channels.append({
                "uuid": str(c.get("uuid") or c.get("key") or c.get("id") or c.get("name")),
                "number": str(c.get("number", "")), "name": str(scalar(c.get("name"), "未命名频道")),
                "icon": c.get("icon_public_url") or c.get("icon"), "enabled": int(c.get("enabled", True)),
                "updated_at": ts,
            })
        safe_events = []
        for e in events:
            genre = e.get("genre") or e.get("contentType") or ""
            safe_events.append({
                "event_id": str(e.get("eventId") or e.get("id") or f"{e.get('channelUuid')}:{e.get('start')}"),
                "channel_uuid": e.get("channelUuid"), "channel_name": scalar(e.get("channelName")),
                "title": scalar(e.get("title"), "无标题"), "subtitle": scalar(e.get("subtitle")),
                "summary": scalar(e.get("summary") or e.get("description")), "start": e.get("start"),
                "stop": e.get("stop"), "genre": genre, "dvr_state": e.get("dvrState", ""), "updated_at": ts,
            })
        with CACHE.lock:
            CACHE.channels = safe_channels
            CACHE.epg = [redact_sensitive(e) for e in safe_events if not e.get("stop") or e["stop"] >= ts - 86400]
        # Optional endpoints differ between TVH releases. A failure in one module
        # is stored for the UI, but must not hide data from all other modules.
        optional = {
            "recordings": ("dvr/entry/grid", {"limit": 10000}),
            "server_info": ("serverinfo", {}),
        }
        for resource, (endpoint, params) in optional.items():
            try:
                payload = client.request(endpoint, params)
                entries = payload.get("entries", payload.get("nodes", payload))
                if isinstance(entries, dict):
                    entries = [entries]
                safe_entries = redact_sensitive(entries if isinstance(entries, list) else [])
                self._save_resource(resource, safe_entries, payload.get("totalCount"), "")
            except Exception as exc:
                self._save_resource(resource, [], None, str(exc))

    def _save_resource(self, resource: str, entries: list[Any], total: Any, error: str) -> None:
        with CACHE.lock:
            CACHE.resources[resource] = {"entries": entries, "totalCount": total if total is not None else len(entries),
                                         "error": error[:1000], "updatedAt": now()}

    @staticmethod
    def _input_kind(name: str, stream: str) -> str:
        text = f"{name} {stream}".lower()
        if any(word in text for word in ("iptv", "http://", "https://", "udp://", "rtsp://")):
            return "IPTV"
        if any(word in text for word in ("dvb", "atsc", "isdb", "tuner", "frontend")):
            return "调谐器"
        return "其他"

    def _collect_live(self, client: TvhClient) -> None:
        ts = now()
        subscriptions = client.request("status/subscriptions").get("entries", [])
        inputs = client.request("status/inputs").get("entries", [])
        try:
            raw_connections = client.request("status/connections").get("entries", [])
            connections = [safe for item in raw_connections if (safe := safe_connection(item, ts)) is not None]
            self._save_resource("connections", connections, len(connections), "")
        except Exception as exc:
            self._save_resource("connections", [], None, str(exc))
        safe_inputs = []
        for item in inputs:
            # Frequency shown by status/inputs is live tuner state, not persisted
            # mux configuration. It is safe to display; stream URLs remain hidden.
            name = str(redact_sensitive(item.get("input", "未知输入"), hide_frequency=False))
            stream = str(redact_sensitive(item.get("stream", ""), hide_frequency=False))
            safe_inputs.append({
                "input_uuid": str(item.get("uuid") or item.get("input") or item.get("id")),
                "input_name": name, "stream_name": stream, "kind": self._input_kind(name, stream),
                "signal": item.get("signal"), "signal_scale": item.get("signal_scale", 0),
                "snr": item.get("snr"), "snr_scale": item.get("snr_scale", 0), "bps": item.get("bps", 0),
                "subscribers": item.get("subs", 0),
                "errors": sum(int(item.get(k, 0) or 0) for k in ("cc", "te", "unc", "ec_block")),
                "sampled_at": ts,
            })
        with CACHE.lock:
            CACHE.inputs = safe_inputs
        with self.store.connect() as db:
            seen: set[str] = set()
            for sub in subscriptions:
                username = str(redact_sensitive(sub.get("username") or sub.get("user") or sub.get("client") or "匿名"))
                channel = str(redact_sensitive(sub.get("channel") or sub.get("channelName") or sub.get("title") or sub.get("service") or "未知频道"))
                raw_ip = str(sub.get("hostname") or sub.get("peer") or "").removeprefix("::ffff:")
                try:
                    client_ip = str(ipaddress.ip_address(raw_ip))
                except ValueError:
                    client_ip = ""
                client_name = re.sub(r"[\x00-\x1f\x7f]+", " ", str(redact_sensitive(sub.get("client") or sub.get("title") or "")))
                client_name = " ".join(client_name.split())[:160]
                public_id = sub.get("id") or sub.get("uuid")
                key = str(public_id) if public_id is not None else hashlib.sha256(
                    f"{username}\0{channel}\0{sub.get('start') or sub.get('started') or ''}".encode()).hexdigest()
                seen.add(key)
                row = db.execute("SELECT id,started_at FROM viewing_sessions WHERE active=1 AND subscription_key=?", (key,)).fetchone()
                if row:
                    db.execute("UPDATE viewing_sessions SET username=?,channel_name=?,last_seen_at=?,duration_seconds=?,client_ip=?,client=? WHERE id=?",
                               (username, channel, ts, ts - row["started_at"], client_ip, client_name, row["id"]))
                else:
                    started = int(sub.get("start") or sub.get("started") or ts)
                    if started > ts or started < ts - 30 * 86400:
                        started = ts
                    db.execute("INSERT INTO viewing_sessions(subscription_key,username,channel_name,client_ip,client,started_at,last_seen_at,duration_seconds,active) VALUES(?,?,?,?,?,?,?,?,1)",
                               (key, username, channel, client_ip, client_name, started, ts, max(0, ts - started)))
            active = db.execute("SELECT id,subscription_key,started_at FROM viewing_sessions WHERE active=1").fetchall()
            for session in active:
                if session["subscription_key"] not in seen:
                    db.execute("UPDATE viewing_sessions SET active=0,ended_at=?,duration_seconds=? WHERE id=?",
                               (ts, max(0, ts - session["started_at"]), session["id"]))

STORE = Store(DB_PATH)
COLLECTOR = Collector(STORE)


def rows(db: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query, params).fetchall()]


def recording_is_cancelable(entry: dict[str, Any], timestamp: int | None = None) -> bool:
    """Only future timers may be cancelled from this deliberately narrow UI."""
    timestamp = timestamp if timestamp is not None else now()
    status = str(scalar(entry.get("status") or entry.get("sched_status"))).casefold()
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", str(entry.get("uuid", "")))) and \
        int(entry.get("start") or 0) > timestamp and "scheduled" in status


def recording_is_running(entry: dict[str, Any]) -> bool:
    sched_status = str(scalar(entry.get("sched_status"))).strip().casefold()
    status = str(scalar(entry.get("status"))).strip().casefold()
    return sched_status == "recording" or status == "recording" or status.startswith("recording ")


def recording_enabled_is_editable(entry: dict[str, Any]) -> bool:
    """Only upcoming timers which are not already recording may be enabled or disabled."""
    return valid_uuid(str(entry.get("uuid", ""))) and not recording_is_running(entry)


def safe_recording_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Expose display metadata but never the DVR storage filename or URL."""
    allowed = ("uuid", "enabled", "start", "stop", "channel", "channelname", "channel_icon",
               "title", "disp_title", "subtitle", "disp_subtitle", "status", "sched_status",
               "description", "disp_summary", "owner", "creator", "create", "duration")
    return {key: redact_sensitive(entry.get(key), key, hide_frequency=False)
            for key in allowed if key in entry}


def safe_autorec_entry(entry: dict[str, Any]) -> dict[str, Any]:
    allowed = ("uuid", "enabled", "title", "channel", "tag", "start", "start_window", "weekdays",
               "comment", "owner", "creator", "maxcount", "maxsched", "minduration", "maxduration")
    return {key: redact_sensitive(entry.get(key), key, hide_frequency=False)
            for key in allowed if key in entry}


def safe_timerec_entry(entry: dict[str, Any]) -> dict[str, Any]:
    allowed = ("uuid", "enabled", "name", "title", "channel", "start", "stop", "weekdays",
               "comment", "owner", "creator")
    return {key: redact_sensitive(entry.get(key), key, hide_frequency=False)
            for key in allowed if key in entry}


def sort_dvr_entries(entries: list[dict[str, Any]], section: str) -> None:
    """Sort timers by timestamp and text-based rules without assuming one field type."""
    if section in ("autorecs", "timerecs"):
        entries.sort(key=lambda item: (
            item.get("enabled") is False,
            str(scalar(item.get("name") or item.get("title"), "")).casefold(),
        ))
        return

    def timestamp(item: dict[str, Any]) -> int:
        try:
            return int(item.get("start") or 0)
        except (TypeError, ValueError):
            return 0

    entries.sort(key=timestamp, reverse=section != "upcoming")


def recording_attention_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return upcoming timers which are disabled, unhealthy or overlap another timer."""
    safe_entries = [safe_recording_entry(item) for item in entries]
    result: list[dict[str, Any]] = []
    normal_sched = {"scheduled", "recording"}
    for entry in safe_entries:
        reasons: list[str] = []
        if entry.get("enabled") is False:
            reasons.append("录像已停用")
        sched = str(scalar(entry.get("sched_status"), "")).strip().casefold()
        status = str(scalar(entry.get("status"), "")).strip()
        if sched and sched not in normal_sched:
            reasons.append(status or f"状态异常：{sched}")
        try:
            start, stop = int(entry.get("start") or 0), int(entry.get("stop") or 0)
        except (TypeError, ValueError):
            start = stop = 0
        overlap = 0
        if entry.get("enabled") is not False and start and stop > start:
            for other in safe_entries:
                if other.get("enabled") is False:
                    continue
                try:
                    other_start, other_stop = int(other.get("start") or 0), int(other.get("stop") or 0)
                except (TypeError, ValueError):
                    continue
                if other_start < stop and other_stop > start:
                    overlap += 1
        if overlap > 1:
            reasons.append(f"同一时段共有 {overlap} 项录像，请检查输入容量")
        if reasons:
            entry["conflict_reasons"] = reasons
            entry["overlap_count"] = overlap
            result.append(entry)
    sort_dvr_entries(result, "upcoming")
    return result


def valid_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", value))


class Handler(SimpleHTTPRequestHandler):
    server_version = f"TvHeadend-Manager/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    def _json(self, data: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self) -> str:
        peer = parse_ip(self.client_address[0])
        proxies = trusted_proxy_networks()
        if not address_in_networks(peer, proxies):
            return str(peer) if peer else self.client_address[0]
        forwarded = [parse_ip(item) for item in self.headers.get("X-Forwarded-For", "").split(",")]
        for address in reversed([item for item in forwarded if item]):
            if not address_in_networks(address, proxies):
                return str(address)
        return "0.0.0.0"

    def _request_host(self) -> str:
        peer, proxies = parse_ip(self.client_address[0]), trusted_proxy_networks()
        value = self.headers.get("Host", "")
        if address_in_networks(peer, proxies):
            value = self.headers.get("X-Forwarded-Host", value).split(",")[-1].strip()
        try:
            return (urllib.parse.urlsplit("//" + value).hostname or "").rstrip(".").casefold()
        except ValueError:
            return ""

    def _is_https(self) -> bool:
        if isinstance(self.connection, ssl.SSLSocket):
            return True
        peer = parse_ip(self.client_address[0])
        if address_in_networks(peer, trusted_proxy_networks()):
            return self.headers.get("X-Forwarded-Proto", "").split(",")[-1].strip().lower() == "https"
        return False

    def _access_policy(self) -> tuple[bool, str]:
        if is_internal_address(self._client_ip()):
            return True, ""
        allowed_host = os.getenv("TVHMON_ALLOWED_HOST", STORE.setting("allowed_host", "")).strip().rstrip(".").casefold()
        require_https = os.getenv("TVHMON_REQUIRE_HTTPS", STORE.setting("require_https", "0")).lower() in ("1", "true", "yes", "on")
        if allowed_host and self._request_host() != allowed_host:
            return False, "此域名不允许访问"
        if require_https and not self._is_https():
            return False, "外部访问必须使用 HTTPS"
        return True, ""

    def _require_access_policy(self) -> bool:
        allowed, message = self._access_policy()
        if allowed:
            return True
        self._json({"ok": False, "error": message}, HTTPStatus.FORBIDDEN)
        return False

    def _web_credentials(self) -> tuple[str, str]:
        return os.getenv("TVHMON_WEB_USERNAME", ""), os.getenv("TVHMON_WEB_PASSWORD", "")

    def _session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[WEB_SESSION_COOKIE].value if WEB_SESSION_COOKIE in cookie else ""
        except Exception:
            return ""

    def _session_valid(self) -> bool:
        token, ts = self._session_token(), now()
        if not token:
            return False
        with WEB_SESSION_LOCK:
            for old_token, expires in list(WEB_SESSIONS.items()):
                if expires <= ts:
                    WEB_SESSIONS.pop(old_token, None)
            return WEB_SESSIONS.get(token, 0) > ts

    def _authorized(self) -> bool:
        username, password = self._web_credentials()
        if not username and not password:
            return True
        return is_internal_address(self._client_ip()) or self._session_valid()

    def _require_authorized(self) -> bool:
        if self._authorized():
            return True
        self._json({"ok": False, "error": "需要登录"}, HTTPStatus.UNAUTHORIZED)
        return False

    def _cookie_header(self, token: str, max_age: int) -> str:
        value = f"{WEB_SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        if os.getenv("TVHMON_COOKIE_SECURE", "").lower() in ("1", "true", "yes", "on"):
            value += "; Secure"
        return value

    def _auth_status(self) -> None:
        username, password = self._web_credentials()
        enabled, internal = bool(username or password), is_internal_address(self._client_ip())
        self._json({"required": enabled and not internal,
                    "authenticated": not enabled or internal or self._session_valid(),
                    "internal": internal, "configured": bool(username and password)})

    def _login(self) -> None:
        username, password = self._web_credentials()
        client_ip, ts = self._client_ip(), now()
        if is_internal_address(client_ip) or (not username and not password):
            self._json({"ok": True})
            return
        if not username or not password:
            self._error(ValueError("服务器未完整配置外网登录账号和密码"), 503)
            return
        with LOGIN_FAILURE_LOCK:
            failures, blocked_until = LOGIN_FAILURES.get(client_ip, (0, 0))
        if blocked_until > ts:
            self._error(ValueError(f"登录尝试过多，请在 {blocked_until - ts} 秒后重试"), 429)
            return
        body = self._body()
        supplied_user, supplied_password = str(body.get("username", "")), str(body.get("password", ""))
        valid = secrets.compare_digest(supplied_user, username) and secrets.compare_digest(supplied_password, password)
        if not valid:
            failures += 1
            blocked_until = ts + 60 if failures >= 5 else 0
            with LOGIN_FAILURE_LOCK:
                LOGIN_FAILURES[client_ip] = (failures, blocked_until)
            self._error(ValueError("用户名或密码错误"), HTTPStatus.UNAUTHORIZED)
            return
        token = secrets.token_urlsafe(32)
        with WEB_SESSION_LOCK:
            WEB_SESSIONS[token] = ts + WEB_SESSION_SECONDS
        with LOGIN_FAILURE_LOCK:
            LOGIN_FAILURES.pop(client_ip, None)
        self._json({"ok": True}, headers={"Set-Cookie": self._cookie_header(token, WEB_SESSION_SECONDS)})

    def _logout(self) -> None:
        token = self._session_token()
        with WEB_SESSION_LOCK:
            WEB_SESSIONS.pop(token, None)
        self._json({"ok": True}, headers={"Set-Cookie": self._cookie_header("", 0)})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("请求太大")
        return json.loads(self.rfile.read(length) or b"{}")

    def _error(self, exc: Exception, status: int = 400) -> None:
        self._json({"ok": False, "error": str(exc)}, status)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if not self._require_access_policy():
                return
            if path == "/api/auth/login":
                self._login()
            elif path == "/api/auth/logout":
                self._logout()
            elif not self._require_authorized():
                return
            elif path == "/api/setup":
                body = self._body()
                url = str(body.get("url", "")).strip().rstrip("/")
                if not url.startswith(("http://", "https://")):
                    raise ValueError("地址必须以 http:// 或 https:// 开头")
                parsed_url = urllib.parse.urlsplit(url)
                if not parsed_url.hostname or parsed_url.username or parsed_url.password:
                    raise ValueError("地址里不要写账号密码，请使用下面单独的用户名和密码输入框")
                username, password = str(body.get("username", "")), str(body.get("password", ""))
                client = TvhClient(url, username, password)
                client.request("channel/grid", {"limit": 1})
                client.request("status/subscriptions")
                STORE.save_settings({"tvh_url": url, "tvh_username": username, "tvh_password": password})
                COLLECTOR.collect_once(force_full=True)
                self._json({"ok": True})
            elif path == "/api/sync":
                if not STORE.setting("tvh_url"):
                    raise ValueError("尚未配置 TvHeadend")
                COLLECTOR.trigger()
                self._json({"ok": True})
            elif path == "/api/clients/disconnect":
                raw_id, client = self._body().get("id"), COLLECTOR.client()
                if isinstance(raw_id, bool) or not str(raw_id).isdigit() or int(raw_id) <= 0:
                    raise ValueError("客户端连接 ID 无效")
                connection_id = int(raw_id)
                connections = client.request("status/connections").get("entries", [])
                if not any(str(item.get("id", "")) == str(connection_id) for item in connections):
                    raise ValueError("客户端连接不存在或已经断开")
                client.request("connections/cancel", {"id": connection_id}, method="POST")
                COLLECTOR.trigger()
                self._json({"ok": True, "id": connection_id})
            elif path == "/api/dvr/schedule":
                body = self._body()
                event_id, config_uuid = str(body.get("eventId", "")), str(body.get("configUuid", ""))
                if not event_id.isdigit() or not re.fullmatch(r"[0-9a-fA-F]{32}", config_uuid):
                    raise ValueError("录像节目或配置无效")
                with CACHE.lock:
                    event = next((item for item in CACHE.epg if item.get("event_id") == event_id), None)
                if not event or int(event.get("start") or 0) <= now():
                    raise ValueError("该节目不存在或已经开始")
                client = COLLECTOR.client()
                profiles = client.request("dvr/config/grid", {"limit": 100}).get("entries", [])
                if config_uuid not in {str(item.get("uuid", "")) for item in profiles}:
                    raise ValueError("录像配置不存在或不可用")
                result = client.request("dvr/entry/create_by_event",
                                        {"event_id": event_id, "config_uuid": config_uuid}, method="POST")
                COLLECTOR.trigger()
                self._json({"ok": True, "uuid": result.get("uuid", "")})
            elif path == "/api/dvr/cancel":
                uuid = str(self._body().get("uuid", ""))
                recordings = COLLECTOR.client().request("dvr/entry/grid_upcoming", {"limit": 10000}).get("entries", [])
                entry = next((item for item in recordings if str(item.get("uuid", "")) == uuid), None)
                if not entry or not recording_is_cancelable(entry):
                    raise ValueError("只允许取消尚未开始的定时录像")
                COLLECTOR.client().request("dvr/entry/cancel", {"uuid": uuid}, method="POST")
                COLLECTOR.trigger()
                self._json({"ok": True})
            elif path == "/api/dvr/autorec/create":
                body, client = self._body(), COLLECTOR.client()
                title, name = str(body.get("title", "")).strip(), str(body.get("name", "")).strip()
                channel, config_uuid = str(body.get("channel", "")), str(body.get("configUuid", ""))
                if not title or len(title) > 120 or (channel and not valid_uuid(channel)) or not valid_uuid(config_uuid):
                    raise ValueError("自动录像规则内容无效")
                profiles = client.request("dvr/config/grid", {"limit": 100}).get("entries", [])
                if config_uuid not in {str(item.get("uuid", "")) for item in profiles}:
                    raise ValueError("录像配置不存在")
                with CACHE.lock:
                    channel_ids = {str(item.get("uuid", "")) for item in CACHE.channels}
                if channel and channel not in channel_ids:
                    raise ValueError("频道不存在")
                conf = {"enabled": True, "name": name[:80], "title": title,
                        "fulltext": bool(body.get("fulltext")), "channel": channel,
                        "config_name": config_uuid, "comment": "由 TVH 管理台创建"}
                result = client.request("dvr/autorec/create",
                                        {"conf": json.dumps(conf, ensure_ascii=False), "config_uuid": config_uuid}, method="POST")
                COLLECTOR.trigger()
                self._json({"ok": True, "uuid": result.get("uuid", "")})
            elif path == "/api/dvr/timerec/create":
                body, client = self._body(), COLLECTOR.client()
                name, title = str(body.get("name", "")).strip(), str(body.get("title", "")).strip()
                channel, config_uuid = str(body.get("channel", "")), str(body.get("configUuid", ""))
                start, stop = str(body.get("start", "")), str(body.get("stop", ""))
                weekdays = sorted({int(day) for day in body.get("weekdays", []) if str(day).isdigit()})
                if (not name or len(name) > 80 or len(title) > 120 or not valid_uuid(channel)
                        or not valid_uuid(config_uuid) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start)
                        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", stop) or not weekdays
                        or any(day not in range(1, 8) for day in weekdays)):
                    raise ValueError("手动定时录像内容无效")
                profiles = client.request("dvr/config/grid", {"limit": 100}).get("entries", [])
                if config_uuid not in {str(item.get("uuid", "")) for item in profiles}:
                    raise ValueError("录像配置不存在")
                with CACHE.lock:
                    channel_ids = {str(item.get("uuid", "")) for item in CACHE.channels}
                if channel not in channel_ids:
                    raise ValueError("频道不存在")
                conf = {"enabled": True, "name": name, "title": title or name, "channel": channel,
                        "start": start, "stop": stop, "weekdays": weekdays, "config_name": config_uuid,
                        "comment": "由 TVH 管理台创建"}
                result = client.request("dvr/timerec/create",
                                        {"conf": json.dumps(conf, ensure_ascii=False), "config_uuid": config_uuid}, method="POST")
                COLLECTOR.trigger()
                self._json({"ok": True, "uuid": result.get("uuid", "")})
            elif path == "/api/dvr/action":
                body, client = self._body(), COLLECTOR.client()
                action, uuid = str(body.get("action", "")), str(body.get("uuid", ""))
                if not valid_uuid(uuid):
                    raise ValueError("录像 UUID 无效")
                action_sources = {
                    "stop": ("dvr/entry/grid_upcoming", "dvr/entry/stop"),
                    "cancel": ("dvr/entry/grid_upcoming", "dvr/entry/cancel"),
                    "previously_recorded": ("dvr/entry/grid_upcoming", "dvr/entry/prevrec/toggle"),
                    "remove": ("dvr/entry/grid_finished", "dvr/entry/remove"),
                    "rerecord_finished": ("dvr/entry/grid_finished", "dvr/entry/rerecord/toggle"),
                    "rerecord_failed": ("dvr/entry/grid_failed", "dvr/entry/rerecord/toggle"),
                    "move_finished": ("dvr/entry/grid_failed", "dvr/entry/move/finished"),
                    "move_failed": ("dvr/entry/grid_finished", "dvr/entry/move/failed"),
                    "delete_autorec": ("dvr/autorec/grid", "idnode/delete"),
                    "delete_timerec": ("dvr/timerec/grid", "idnode/delete"),
                }
                if action not in action_sources:
                    raise ValueError("不允许的录像操作")
                source, endpoint = action_sources[action]
                entries = client.request(source, {"limit": 10000}).get("entries", [])
                entry = next((item for item in entries if str(item.get("uuid", "")) == uuid), None)
                if not entry:
                    raise ValueError("录像项目不存在或状态已经改变")
                if action == "cancel" and not recording_is_cancelable(entry):
                    raise ValueError("该录像已不能取消")
                if action == "stop" and not recording_is_running(entry):
                    raise ValueError("该录像当前未在录制")
                client.request(endpoint, {"uuid": uuid}, method="POST")
                COLLECTOR.trigger()
                self._json({"ok": True})
            elif path == "/api/dvr/enabled":
                body, client = self._body(), COLLECTOR.client()
                uuid, enabled = str(body.get("uuid", "")), body.get("enabled")
                if not valid_uuid(uuid) or not isinstance(enabled, bool):
                    raise ValueError("录像开关参数无效")
                entries = client.request("dvr/entry/grid_upcoming", {"limit": 10000}).get("entries", [])
                entry = next((item for item in entries if str(item.get("uuid", "")) == uuid), None)
                if not entry or not recording_enabled_is_editable(entry):
                    raise ValueError("该待录像项目不存在、已开始录制或状态已经改变")
                client.request("idnode/save", {
                    "node": json.dumps({"uuid": uuid, "enabled": enabled}, separators=(",", ":"))
                }, method="POST")
                COLLECTOR.trigger()
                self._json({"ok": True, "enabled": enabled})
            elif path.rstrip("/") == "/api/preferences":
                body = self._body()
                saved: dict[str, str] = {}
                response: dict[str, Any] = {"ok": True}
                if "pollSeconds" in body:
                    interval = int(body["pollSeconds"])
                    if interval not in (10, 20, 30, 60):
                        raise ValueError("采集间隔只能选择 10、20、30 或 60 秒")
                    saved["poll_seconds"] = str(interval)
                    response["pollSeconds"] = interval
                if "allowedHost" in body:
                    allowed_host = str(body["allowedHost"]).strip().rstrip(".").casefold()
                    if allowed_host:
                        parsed_host = urllib.parse.urlsplit("//" + allowed_host)
                        if parsed_host.hostname != allowed_host or parsed_host.port or parsed_host.username:
                            raise ValueError("只填写域名，例如 tv.example.com，不要填写协议、端口或路径")
                        try:
                            allowed_host = allowed_host.encode("idna").decode("ascii")
                        except UnicodeError as exc:
                            raise ValueError("域名格式无效") from exc
                    saved["allowed_host"] = allowed_host
                    response["allowedHost"] = allowed_host
                if "requireHttps" in body:
                    saved["require_https"] = "1" if bool(body["requireHttps"]) else "0"
                    response["requireHttps"] = saved["require_https"] == "1"
                if "forwardPort" in body:
                    forward_port = int(body["forwardPort"] or 0)
                    if forward_port != 0 and not 1024 <= forward_port <= 65535:
                        raise ValueError("转发端口只能留空/填 0 关闭，或填写 1024-65535")
                    if FORWARD_GATEWAY is not None:
                        FORWARD_GATEWAY.configure(forward_port)
                    saved["forward_port"] = str(forward_port)
                    response["forwardPort"] = forward_port
                if not saved:
                    raise ValueError("没有可保存的设置")
                STORE.save_settings(saved)
                if "poll_seconds" in saved:
                    COLLECTOR.wake_event.set()
                self._json(response)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._error(exc)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if not self._require_access_policy():
                return
            if path == "/api/auth/status":
                self._auth_status()
            elif path.startswith("/api/") and not self._require_authorized():
                return
            elif path == "/api/bootstrap":
                cfg = STORE.settings()
                self._json({"configured": bool(cfg.get("tvh_url")), "url": cfg.get("tvh_url", ""),
                            "username": cfg.get("tvh_username", ""), "pollSeconds": COLLECTOR.poll_seconds(),
                            "allowedHost": os.getenv("TVHMON_ALLOWED_HOST", cfg.get("allowed_host", "")),
                            "requireHttps": os.getenv("TVHMON_REQUIRE_HTTPS", cfg.get("require_https", "0")).lower() in ("1", "true", "yes", "on"),
                            "forwardPort": int(os.getenv("TVHMON_FORWARD_PORT", cfg.get("forward_port", "0")) or 0),
                            "version": APP_VERSION, "lastSync": CACHE.last_sync})
            elif path == "/api/dashboard":
                self._dashboard()
            elif path == "/api/channels":
                with CACHE.lock:
                    entries = list(CACHE.channels)
                def channel_sort(item: dict[str, Any]):
                    try:
                        number = float(item.get("number") or 10**12)
                    except (TypeError, ValueError):
                        number = 10**12
                    return number, item.get("name", "")
                self._json({"entries": sorted(entries, key=channel_sort)})
            elif path == "/api/epg":
                search = query.get("q", [""])[0][:100]
                with CACHE.lock:
                    entries = list(CACHE.epg)
                needle = search.casefold()
                entries = [e for e in entries if (e.get("stop") or 0) >= now() - 3600 and
                           (not needle or needle in " ".join(str(e.get(k, "")) for k in ("title", "channel_name", "summary")).casefold())]
                self._json({"entries": sorted(entries, key=lambda e: e.get("start") or 0)[:500]})
            elif path == "/api/dvr/options":
                search = query.get("q", [""])[0][:100].casefold()
                channel = query.get("channel", [""])[0]
                with CACHE.lock:
                    events = [dict(item) for item in CACHE.epg if int(item.get("start") or 0) > now()]
                    channels = [{"uuid": str(item.get("uuid", "")), "name": str(item.get("name", "")),
                                 "number": str(item.get("number", ""))} for item in CACHE.channels if item.get("enabled", 1)]
                channel_ids = {item["uuid"] for item in channels}
                if channel and (not valid_uuid(channel) or channel not in channel_ids):
                    raise ValueError("频道筛选无效")
                if search:
                    events = [item for item in events if search in
                              f"{item.get('title', '')} {item.get('channel_name', '')}".casefold()]
                if channel:
                    events = [item for item in events if str(item.get("channel_uuid", "")) == channel]
                profiles = COLLECTOR.client().request("dvr/config/grid", {"limit": 100}).get("entries", [])
                safe_profiles = [{"uuid": str(item.get("uuid", "")),
                                  "name": str(item.get("name") or "默认录像配置")}
                                 for item in profiles if re.fullmatch(r"[0-9a-fA-F]{32}", str(item.get("uuid", "")))]
                self._json({"profiles": safe_profiles, "channels": channels,
                            "events": sorted(events, key=lambda item: item.get("start") or 0)[:80]})
            elif path == "/api/dvr/library":
                section = query.get("section", ["upcoming"])[0]
                if section == "conflicts":
                    entries = COLLECTOR.client().request("dvr/entry/grid_upcoming", {"limit": 10000}).get("entries", [])
                    safe_entries = recording_attention_entries(entries)
                    self._json({"section": section, "entries": safe_entries, "total": len(safe_entries)})
                    return
                endpoints = {
                    "upcoming": "dvr/entry/grid_upcoming", "finished": "dvr/entry/grid_finished",
                    "failed": "dvr/entry/grid_failed", "removed": "dvr/entry/grid_removed",
                    "autorecs": "dvr/autorec/grid", "timerecs": "dvr/timerec/grid",
                }
                if section not in endpoints:
                    raise ValueError("录像分类无效")
                entries = COLLECTOR.client().request(endpoints[section], {"limit": 10000}).get("entries", [])
                sanitizer = safe_autorec_entry if section == "autorecs" else safe_timerec_entry if section == "timerecs" else safe_recording_entry
                safe_entries = [sanitizer(item) for item in entries]
                if section in ("autorecs", "timerecs"):
                    with CACHE.lock:
                        channel_names = {str(item.get("uuid", "")): str(item.get("name", "")) for item in CACHE.channels}
                    for item in safe_entries:
                        item["channel"] = channel_names.get(str(item.get("channel", "")), item.get("channel", ""))
                sort_dvr_entries(safe_entries, section)
                self._json({"section": section, "entries": safe_entries, "total": len(safe_entries)})
            elif path == "/api/history":
                with STORE.connect() as db:
                    self._json({
                        "entries": rows(db, "SELECT * FROM viewing_sessions ORDER BY active DESC,started_at DESC LIMIT 1000"),
                        "summary": rows(db, "SELECT username,channel_name,COUNT(*) session_count,SUM(duration_seconds) total_seconds "
                                            "FROM viewing_sessions GROUP BY username,channel_name ORDER BY total_seconds DESC"),
                    })
            elif path == "/api/status":
                with CACHE.lock:
                    inputs = sorted(list(CACHE.inputs), key=lambda x: (-int(x.get("subscribers") or 0), x.get("input_name", "")))
                    last = dict(CACHE.last_sync) if CACHE.last_sync else None
                self._json({"inputs": inputs, "syncRuns": [last] if last else []})
            elif path == "/api/clients":
                with CACHE.lock:
                    resource = dict(CACHE.resources.get("connections", {"entries": [], "error": "尚未取得连接信息"}))
                self._json(resource)
            elif path == "/api/resources":
                with CACHE.lock:
                    result = json.loads(json.dumps(CACHE.resources, ensure_ascii=False))
                self._json({"resources": result})
            elif path.startswith("/api/dvr/download/"):
                self._dvr_download(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/channels/") and path.endswith("/icon"):
                self._channel_icon(path.split("/")[3])
            elif path.startswith("/api/"):
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self._static(path)
        except Exception as exc:
            LOG.exception("request failed")
            self._error(exc, 500)

    def _dashboard(self) -> None:
        ts = now()
        with CACHE.lock:
            current_epg = [dict(e) for e in CACHE.epg if (e.get("start") or 0) <= ts < (e.get("stop") or 0)]
            inputs = sorted([dict(i) for i in CACHE.inputs], key=lambda x: (-int(x.get("subscribers") or 0), x.get("input_name", "")))
            channel_count = sum(int(c.get("enabled", 1)) for c in CACHE.channels)
            last = dict(CACHE.last_sync) if CACHE.last_sync else None
        with STORE.connect() as db:
            live = rows(db, "SELECT * FROM viewing_sessions WHERE active=1 ORDER BY started_at")
            stats = dict(db.execute(
                "SELECT (SELECT COUNT(*) FROM viewing_sessions WHERE active=1) viewers,"
                "(SELECT COALESCE(SUM(duration_seconds),0) FROM viewing_sessions WHERE started_at>=?) watch_seconds", (ts - 86400,)).fetchone())
        stats["channels"] = channel_count
        stats["active_inputs"] = sum(1 for item in inputs if int(item.get("subscribers") or 0) > 0)
        self._json({"stats": stats, "live": live, "currentEpg": current_epg, "inputs": inputs,
                    "lastSync": last, "serverTime": ts})

    def _channel_icon(self, uuid: str) -> None:
        wanted = urllib.parse.unquote(uuid)
        with CACHE.lock:
            icon = next((c.get("icon") for c in CACHE.channels if c.get("uuid") == wanted), None)
        if not icon:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            data, content_type = COLLECTOR.client().fetch_image(icon)
        except (urllib.error.URLError, TimeoutError, UnicodeError, ValueError) as exc:
            LOG.warning("channel icon unavailable for %s: %s", wanted, exc)
            self.send_error(HTTPStatus.BAD_GATEWAY, "频道图标暂时不可用")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _dvr_download(self, uuid: str) -> None:
        if not valid_uuid(uuid):
            raise ValueError("录像 UUID 无效")
        client = COLLECTOR.client()
        entries = client.request("dvr/entry/grid_finished", {"limit": 10000}).get("entries", [])
        entry = next((item for item in entries if str(item.get("uuid", "")) == uuid), None)
        if not entry:
            raise ValueError("已完成录像不存在或当前账号无权下载")
        target = urllib.parse.urljoin(client.url + "/", f"dvrfile/{uuid}")
        headers = {"User-Agent": f"TVH-Insight/{APP_VERSION}", "Authorization": client.basic_authorization}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        request = urllib.request.Request(target, headers=headers)
        try:
            response = client.opener.open(request, timeout=max(client.timeout, 60))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise ValueError("Tvheadend 拒绝下载该录像") from exc
            raise RuntimeError(f"Tvheadend 下载返回 HTTP {exc.code}") from exc
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/octet-stream"))
            for name in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            title = str(scalar(entry.get("title") or entry.get("disp_title"), "录像"))
            title = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", "_", title).strip(" .")[:120] or "录像"
            self.send_header("Content-Disposition", "attachment; filename=recording.ts; filename*=UTF-8''" +
                             urllib.parse.quote(title + ".ts"))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, no-store")
            self.end_headers()
            while chunk := response.read(128 * 1024):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            response.close()

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        if self.server_address[0] == "::":
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()


def create_http_server(host: str, port: int, handler: type[SimpleHTTPRequestHandler]) -> ThreadingHTTPServer:
    server_type = IPv6ThreadingHTTPServer if ":" in host else ThreadingHTTPServer
    return server_type((host, port), handler)


def display_endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayHandler(Handler):
    """Read-only Tvheadend gateway with an explicit media allowlist."""

    def _gateway(self, head_only: bool = False) -> None:
        if not self._require_access_policy():
            return
        parsed = urllib.parse.urlsplit(self.path)
        decoded_path = urllib.parse.unquote(parsed.path)
        if (len(self.path) > 4096 or ".." in decoded_path or "%" in decoded_path or "\\" in decoded_path
                or not any(rule.fullmatch(decoded_path) for rule in FORWARD_PATHS)):
            self._json({"ok": False, "error": "此 Tvheadend 路径未开放"}, HTTPStatus.FORBIDDEN)
            return
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if any(key not in FORWARD_QUERY_KEYS for key in query):
            self._json({"ok": False, "error": "请求参数未列入白名单"}, HTTPStatus.FORBIDDEN)
            return
        target_base = STORE.setting("tvh_url", "").rstrip("/")
        if not target_base:
            self._json({"ok": False, "error": "尚未配置 TvHeadend"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        target = target_base + parsed.path + (("?" + parsed.query) if parsed.query else "")
        headers = {"User-Agent": f"TvHeadend-Manager-Gateway/{APP_VERSION}",
                   "Accept": self.headers.get("Accept", "*/*")}
        for name in ("Authorization", "Range"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        forwarded_host = self.headers.get("Host", "")
        peer = parse_ip(self.client_address[0])
        if address_in_networks(peer, trusted_proxy_networks()):
            forwarded_host = self.headers.get("X-Forwarded-Host", forwarded_host).split(",")[-1].strip()
        if forwarded_host:
            headers["Host"] = forwarded_host
        request = urllib.request.Request(target, headers=headers, method="HEAD" if head_only else "GET")
        try:
            response = urllib.request.build_opener(NoRedirectHandler()).open(request, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._json({"ok": False, "error": f"TvHeadend 转发失败：{exc}"}, HTTPStatus.BAD_GATEWAY)
            return
        buffered_body = None
        try:
            if not head_only and decoded_path.startswith("/playlist") and self._is_https() and 200 <= response.status < 300:
                buffered_body = response.read(8 * 1024 * 1024 + 1)
                if len(buffered_body) > 8 * 1024 * 1024:
                    self._json({"ok": False, "error": "播放列表异常大，已停止转发"}, HTTPStatus.BAD_GATEWAY)
                    return
                if forwarded_host:
                    buffered_body = buffered_body.replace(f"http://{forwarded_host}/".encode(),
                                                          f"https://{forwarded_host}/".encode())
            self.send_response(response.status)
            for name in ("Content-Type", "Content-Disposition", "Content-Length", "Accept-Ranges",
                         "Content-Range", "ETag", "Last-Modified", "WWW-Authenticate"):
                if buffered_body is not None and name == "Content-Length":
                    continue
                value = response.headers.get(name)
                if value:
                    self.send_header(name, value)
            if buffered_body is not None:
                self.send_header("Content-Length", str(len(buffered_body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store" if decoded_path.startswith("/playlist") else "private")
            self.end_headers()
            if not head_only:
                if buffered_body is not None:
                    self.wfile.write(buffered_body)
                else:
                    while chunk := response.read(64 * 1024):
                        self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            response.close()

    def do_GET(self) -> None:
        self._gateway()

    def do_HEAD(self) -> None:
        self._gateway(head_only=True)

    def do_POST(self) -> None:
        self._json({"ok": False, "error": "转发端口只允许读取"}, HTTPStatus.METHOD_NOT_ALLOWED)


class GatewayManager:
    def __init__(self, host: str):
        self.host = os.getenv("TVHMON_FORWARD_HOST", host)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.lock = threading.RLock()

    def configure(self, port: int) -> None:
        with self.lock:
            if port == self.port:
                return
            try:
                new_server = create_http_server(self.host, port, GatewayHandler) if port else None
            except OSError as exc:
                raise ValueError(f"无法监听转发端口 {port}：{exc}") from exc
            new_thread = None
            if new_server:
                new_thread = threading.Thread(target=new_server.serve_forever, name="tvh-gateway", daemon=True)
                new_thread.start()
            old_server, old_thread = self.server, self.thread
            self.server, self.thread, self.port = new_server, new_thread, port
            if old_server:
                old_server.shutdown()
                old_server.server_close()
            if old_thread and old_thread is not threading.current_thread():
                old_thread.join(timeout=2)
            if port:
                LOG.info("Tvheadend 安全转发正在监听 http://%s", display_endpoint(self.host, port))

    def close(self) -> None:
        self.configure(0)


def main() -> None:
    global FORWARD_GATEWAY
    parser = argparse.ArgumentParser(description="TvHeadend monitoring dashboard")
    parser.add_argument("--host", default=os.getenv("TVHMON_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("TVHMON_PORT", "8088")))
    parser.add_argument("--version", action="version", version=APP_VERSION)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("TVHMON_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    COLLECTOR.start()
    server = create_http_server(args.host, args.port, Handler)
    FORWARD_GATEWAY = GatewayManager(args.host)
    configured_forward_port = int(os.getenv("TVHMON_FORWARD_PORT", STORE.setting("forward_port", "0")) or 0)
    FORWARD_GATEWAY.configure(configured_forward_port)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    LOG.info("TvHeadend Manager %s listening on http://%s", APP_VERSION, display_endpoint(args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        COLLECTOR.stop_event.set()
        if FORWARD_GATEWAY:
            FORWARD_GATEWAY.close()
        server.server_close()


if __name__ == "__main__":
    main()
