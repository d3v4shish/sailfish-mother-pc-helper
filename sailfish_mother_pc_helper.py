#!/usr/bin/env python3
"""Small Linux-side helper for Sailfish discovery handoff."""

import argparse
import ipaddress
import json
import logging
import os
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import fcntl
except ImportError:  # pragma: no cover - Sailfish/Linux hosts provide fcntl.
    fcntl = None


APP_NAME = "sailfish-mother-pc-helper"
SUPPORTED_DISCOVERY_VERSION = 1
MAX_DISCOVERY_BYTES = 1024 * 1024
GUI_DEFAULT_HOST = "127.0.0.1"
GUI_DEFAULT_PORT = 8766
GUI_TOKEN_MAX_LENGTH = 512


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def isoformat(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None, "announced_at is missing or not a string"

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, f"announced_at is not ISO-8601: {value!r}"

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc), None


def env_path(name):
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def app_paths():
    home = Path.home()
    config_home = env_path("XDG_CONFIG_HOME") or home / ".config"
    state_home = env_path("XDG_STATE_HOME") or home / ".local" / "state"
    cache_home = env_path("XDG_CACHE_HOME") or home / ".cache"

    config_dir = env_path("SAILFISH_HELPER_CONFIG_DIR") or config_home / APP_NAME
    state_dir = env_path("SAILFISH_HELPER_STATE_DIR") or state_home / APP_NAME
    cache_dir = env_path("SAILFISH_HELPER_CACHE_DIR") or cache_home / APP_NAME
    log_dir = env_path("SAILFISH_HELPER_LOG_DIR") or state_dir / "logs"

    return {
        "config_dir": config_dir,
        "state_dir": state_dir,
        "cache_dir": cache_dir,
        "log_dir": log_dir,
        "config_file": config_dir / "config.json",
        "secrets_file": config_dir / "secrets.json",
        "state_file": state_dir / "state.json",
        "log_file": log_dir / "helper.log",
    }


def ensure_dirs(paths):
    for key in ("config_dir", "state_dir", "cache_dir", "log_dir"):
        paths[key].mkdir(parents=True, exist_ok=True)


def default_config():
    return {
        "trusted_device_id": None,
        "stale_after_seconds": 300,
        "listen_host": "0.0.0.0",
        "listen_port": 8765,
        "discovery_port": 45177,
        "discovery_multicast_group": "239.255.77.77",
        "webcam_device": "/dev/video10",
    }


def load_config(paths):
    ensure_dirs(paths)
    config_path = paths["config_file"]
    if not config_path.exists():
        config = default_config()
        write_json(config_path, config)
        return config

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read config {config_path}: {exc}") from exc

    return normalize_config(loaded)


def write_config(paths, config):
    ensure_dirs(paths)
    write_json(paths["config_file"], config)


def secrets_path(paths):
    """Return the private token file, including for older test path dictionaries."""
    return paths.get("secrets_file") or paths["config_dir"] / "secrets.json"


def default_secrets():
    return {"webcam_token": None, "lls_token": None}


def normalize_secret(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > GUI_TOKEN_MAX_LENGTH:
        return None
    return value


def load_secrets(paths):
    path = secrets_path(paths)
    if not path.exists():
        return default_secrets()
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read private token file {path}: {exc}") from exc

    loaded = loaded if isinstance(loaded, dict) else {}
    return {
        "webcam_token": normalize_secret(loaded.get("webcam_token")),
        "lls_token": normalize_secret(loaded.get("lls_token")),
    }


def write_secrets(paths, secrets):
    path = secrets_path(paths)
    write_json(path, {
        "webcam_token": normalize_secret(secrets.get("webcam_token")),
        "lls_token": normalize_secret(secrets.get("lls_token")),
    })
    # Tokens must never inherit an overly broad mode from an old file.
    path.chmod(0o600)


def setup_logging(paths, verbose=False):
    ensure_dirs(paths)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(paths["log_file"], encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if verbose:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        # A unique private temporary file prevents concurrent HTTP and UDP
        # discovery writes from corrupting the state file.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


def load_state(paths):
    state_path = paths["state_file"]
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read state {state_path}: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def deep_get(data, path):
    current = data
    for item in path:
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current


def clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clean_port(value):
    if isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def local_ipv4_addresses():
    """Return non-loopback IPv4 addresses for every local network interface."""
    if fcntl is None:
        return []

    addresses = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as query_socket:
        for _, interface_name in socket.if_nameindex():
            try:
                request = struct.pack("256s", interface_name.encode("utf-8")[:15])
                response = fcntl.ioctl(query_socket.fileno(), 0x8915, request)  # SIOCGIFADDR
                address = socket.inet_ntoa(response[20:24])
            except OSError:
                continue
            if not address.startswith("127."):
                addresses.add(address)
    return sorted(addresses)


def normalize_config(loaded):
    """Return a safe config even when a user-edited JSON file is malformed."""
    config = default_config()
    if not isinstance(loaded, dict):
        return config

    trusted_device_id = clean_text(loaded.get("trusted_device_id"))
    config["trusted_device_id"] = trusted_device_id

    stale_after = loaded.get("stale_after_seconds")
    if not isinstance(stale_after, bool):
        try:
            stale_after = int(stale_after)
        except (TypeError, ValueError):
            stale_after = None
    if isinstance(stale_after, int) and stale_after > 0:
        config["stale_after_seconds"] = stale_after

    for key in ("listen_port", "discovery_port"):
        port = clean_port(loaded.get(key))
        if port:
            config[key] = port

    for key in ("listen_host", "webcam_device"):
        value = clean_text(loaded.get(key))
        if value:
            config[key] = value

    group = clean_text(loaded.get("discovery_multicast_group"))
    if group:
        try:
            if ipaddress.ip_address(group).version == 4 and ipaddress.ip_address(group).is_multicast:
                config["discovery_multicast_group"] = group
        except ValueError:
            pass
    return config


def trust_state(config, device_id):
    trusted_device_id = clean_text(config.get("trusted_device_id"))
    if not trusted_device_id:
        return {
            "configured": False,
            "matched": None,
            "message": "No trusted_device_id is configured. Accepting announcements, but trust is not pinned.",
        }
    return {
        "configured": True,
        "trusted_device_id": trusted_device_id,
        "matched": device_id == trusted_device_id,
    }


def unchanged_state(previous, config, received_at, source, status, issues, extra=None):
    last_ingest = {
        "accepted": False,
        "issues": issues,
        "received_at": isoformat(received_at),
        "source": source,
        "status": status,
    }
    if extra:
        last_ingest.update(extra)
    return {
        "schema_version": 1,
        "updated_at": isoformat(received_at),
        "config": {
            "stale_after_seconds": int(config.get("stale_after_seconds", 300)),
            "trusted_device_id": config.get("trusted_device_id"),
        },
        "current": previous.get("current"),
        "latest_usable": previous.get("latest_usable"),
        "last_ingest": last_ingest,
    }


def normalize_payload(payload, config, previous, source):
    received_at = utc_now()
    if not isinstance(payload, dict):
        return unchanged_state(
            previous,
            config,
            received_at,
            source,
            "invalid_payload",
            ["Discovery payload must be a JSON object."],
        )

    version = payload.get("version")
    if version != SUPPORTED_DISCOVERY_VERSION:
        return unchanged_state(
            previous,
            config,
            received_at,
            source,
            "unsupported_version",
            [f"Unsupported discovery version {version!r}; expected {SUPPORTED_DISCOVERY_VERSION}."],
            {"observed_version": version},
        )

    stale_after = int(config.get("stale_after_seconds", 300))
    issues = []
    missing = []

    device_id = clean_text(deep_get(payload, ("device", "id")))
    device_name = clean_text(deep_get(payload, ("device", "name")))
    announced_raw = deep_get(payload, ("announced_at",))
    announced_at, time_issue = parse_time(announced_raw)
    if time_issue:
        issues.append(time_issue)

    ip = clean_text(deep_get(payload, ("network", "ip")))
    ssh_host = clean_text(deep_get(payload, ("ssh", "host")))
    ssh_port = clean_port(deep_get(payload, ("ssh", "port")))
    ssh_user = clean_text(deep_get(payload, ("ssh", "user")))
    mjpeg_url = clean_text(deep_get(payload, ("webcam", "mjpeg_url")))
    webcam_status_url = clean_text(deep_get(payload, ("webcam", "status_url")))
    lls_control_url = clean_text(deep_get(payload, ("lls", "control_url")))
    lls_status_url = clean_text(deep_get(payload, ("lls", "status_url")))
    token_hint = clean_text(deep_get(payload, ("auth", "token_hint")))

    required_values = {
        "device.id": device_id,
        "device.name": device_name,
        "announced_at": announced_at,
        "network.ip": ip,
        "ssh.host": ssh_host,
        "ssh.port": ssh_port,
        "ssh.user": ssh_user,
        "webcam.mjpeg_url": mjpeg_url,
        "webcam.status_url": webcam_status_url,
        "lls.control_url": lls_control_url,
        "lls.status_url": lls_status_url,
    }
    for name, value in required_values.items():
        if value in (None, ""):
            missing.append(name)

    age_seconds = None
    is_stale = False
    if announced_at:
        age_seconds = int((received_at - announced_at).total_seconds())
        if age_seconds > stale_after:
            is_stale = True
            issues.append(
                f"Announcement is stale: age {age_seconds}s exceeds stale_after_seconds {stale_after}s."
            )
        elif age_seconds < -60:
            issues.append(f"Announcement time is {abs(age_seconds)}s in the future.")

    if missing:
        issues.append("Missing or invalid fields: " + ", ".join(missing) + ".")

    trust = trust_state(config, device_id)
    if trust.get("configured") and not trust.get("matched"):
        return unchanged_state(
            previous,
            config,
            received_at,
            source,
            "untrusted",
            [f"Device id {device_id!r} does not match trusted_device_id {trust['trusted_device_id']!r}."],
            {"observed_device_id": device_id, "trust": trust},
        )

    if missing:
        quality = "partial"
    elif is_stale:
        quality = "stale"
    else:
        quality = "usable"

    record = {
        "version": SUPPORTED_DISCOVERY_VERSION,
        "device": {"id": device_id, "name": device_name},
        "announced_at": isoformat(announced_at) if announced_at else announced_raw,
        "received_at": isoformat(received_at),
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after,
        "is_stale": is_stale,
        "network": {"ip": ip},
        "ssh": {"host": ssh_host, "port": ssh_port, "user": ssh_user},
        "webcam": {"mjpeg_url": mjpeg_url, "status_url": webcam_status_url},
        "lls": {"control_url": lls_control_url, "status_url": lls_status_url},
        "auth": {"token_hint": token_hint},
        "quality": quality,
        "issues": issues,
        "trust": trust,
    }

    latest_usable = previous.get("latest_usable")
    if quality == "usable":
        latest_usable = record

    return {
        "schema_version": 1,
        "updated_at": isoformat(received_at),
        "config": {
            "stale_after_seconds": stale_after,
            "trusted_device_id": config.get("trusted_device_id"),
        },
        "current": record,
        "latest_usable": latest_usable,
        "last_ingest": {
            "accepted": True,
            "issues": issues,
            "received_at": isoformat(received_at),
            "source": source,
            "status": quality,
            "trust": trust,
        },
    }


def ingest_payload(payload, source, paths, config, logger=None):
    previous = load_state(paths)
    state = normalize_payload(payload, config, previous, source)
    write_json(paths["state_file"], state)
    status = state.get("last_ingest", {}).get("status", "unknown")
    issues = state.get("last_ingest", {}).get("issues", [])
    if logger:
        logger.info("ingest source=%s status=%s issues=%s", source, status, "; ".join(issues) or "none")
    return state


def ingest_text(text, source, paths, config, logger=None):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        previous = load_state(paths)
        state = unchanged_state(
            previous,
            config,
            utc_now(),
            source,
            "invalid_json",
            [f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."],
        )
        write_json(paths["state_file"], state)
        if logger:
            logger.warning("ingest source=%s status=invalid_json error=%s", source, exc)
        return state
    return ingest_payload(payload, source, paths, config, logger)


def format_size(size):
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def path_size(path):
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() or child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def generated_paths(paths):
    return [
        ("config", paths["config_file"]),
        ("state", paths["state_file"]),
        ("logs", paths["log_dir"]),
        ("cache", paths["cache_dir"]),
    ]


def print_paths(paths):
    for label, path in generated_paths(paths):
        exists = "yes" if path.exists() else "no"
        print(f"{label:7} {path}  exists={exists}  size={format_size(path_size(path))}")


def endpoint_record(state, endpoint):
    current = state.get("current")
    latest = state.get("latest_usable")
    if has_endpoint(current, endpoint):
        return current, "current"
    if has_endpoint(latest, endpoint):
        return latest, "latest_usable"
    return None, None


def has_endpoint(record, endpoint):
    if not isinstance(record, dict):
        return False
    if endpoint == "ssh":
        ssh = record.get("ssh") or {}
        return bool(ssh.get("host") and ssh.get("port") and ssh.get("user"))
    if endpoint == "webcam":
        webcam = record.get("webcam") or {}
        return bool(webcam.get("mjpeg_url"))
    if endpoint == "lls":
        lls = record.get("lls") or {}
        return bool(lls.get("control_url") or lls.get("status_url"))
    return False


def command_text(command):
    return " ".join(shlex.quote(str(item)) for item in command)


def print_record_warnings(record, source_name, config=None):
    if source_name == "latest_usable":
        print("# Current state does not have this complete endpoint; using latest_usable.")
    quality = record.get("quality")
    if quality and quality != "usable":
        print(f"# Selected discovery quality is {quality}.")
    for issue in record.get("issues") or []:
        print(f"# {issue}")
    trust = (
        trust_state(config, clean_text(deep_get(record, ("device", "id"))))
        if config is not None
        else record.get("trust") or {}
    )
    if trust.get("configured") is False:
        print("# Trust is not pinned. Run the trust command after verifying the phone identity.")


def endpoint_is_trusted(record, config):
    device_id = clean_text(deep_get(record, ("device", "id")))
    trust = trust_state(config, device_id)
    return bool(trust.get("configured") and trust.get("matched"))


def require_trusted_endpoint(record, config, args, endpoint):
    if endpoint_is_trusted(record, config):
        return True
    if getattr(args, "allow_untrusted", False):
        print("# WARNING: using an untrusted discovery endpoint by explicit request.")
        return True
    device_id = clean_text(deep_get(record, ("device", "id"))) or "unknown"
    print(
        f"Refusing untrusted {endpoint} endpoint for device {device_id!r}. "
        f"Verify it, then run `trust {device_id}`; use --allow-untrusted only for diagnostics.",
        file=sys.stderr,
    )
    return False


def cmd_status(args, paths, config, logger):
    state = load_state(paths)
    if args.json:
        print(json.dumps({"config": config, "paths": path_report(paths), "state": state}, indent=2, sort_keys=True))
        return 0

    print("Sailfish Mother-PC Helper")
    last = state.get("last_ingest") or {}
    if not state:
        print("State: no discovery has been ingested")
    else:
        print(f"State: last_ingest={last.get('status', 'unknown')} updated_at={state.get('updated_at')}")

    current = state.get("current") or {}
    if current:
        device = current.get("device") or {}
        print(f"Phone: {device.get('name') or 'unknown'} ({device.get('id') or 'no device.id'})")
        print(f"Freshness: quality={current.get('quality')} age_seconds={current.get('age_seconds')}")
        ssh = current.get("ssh") or {}
        webcam = current.get("webcam") or {}
        lls = current.get("lls") or {}
        print(f"SSH: {ssh.get('user')}@{ssh.get('host')} port {ssh.get('port')}")
        print(f"Webcam: {webcam.get('mjpeg_url')}")
        print(f"LLs control: {lls.get('control_url')}")
    else:
        print("Phone: none")

    trust = trust_state(config, deep_get(current, ("device", "id")))
    if trust.get("configured"):
        print(f"Trust: pinned to {trust.get('trusted_device_id')} matched={trust.get('matched')}")
    else:
        print("Trust: open; use `trust DEVICE_ID` after verifying the phone.")

    issues = last.get("issues") or []
    if issues:
        print("Issues:")
        for issue in issues:
            print(f"  - {issue}")

    print("Generated files:")
    print_paths(paths)
    logger.info("status shown")
    return 0


def path_report(paths):
    report = {}
    for label, path in generated_paths(paths):
        report[label] = {"path": str(path), "exists": path.exists(), "size_bytes": path_size(path)}
    return report


def cmd_ingest(args, paths, config, logger):
    if args.file == "-":
        text = sys.stdin.read()
        source = "stdin"
    else:
        source_path = Path(args.file)
        text = source_path.read_text(encoding="utf-8")
        source = str(source_path)
    state = ingest_text(text, source, paths, config, logger)
    last = state.get("last_ingest", {})
    print(f"ingest status: {last.get('status')}")
    for issue in last.get("issues") or []:
        print(f"- {issue}")
    print(f"state: {paths['state_file']}")
    return 0 if last.get("accepted") else 1


def cmd_ssh_command(args, paths, config, logger):
    state = load_state(paths)
    record, source_name = endpoint_record(state, "ssh")
    if not record:
        print("No SSH endpoint is available. Ingest discovery JSON first.", file=sys.stderr)
        return 1
    if not require_trusted_endpoint(record, config, args, "SSH"):
        return 1
    print_record_warnings(record, source_name, config)
    ssh = record["ssh"]
    print(command_text(["ssh", "-p", ssh["port"], f"{ssh['user']}@{ssh['host']}"]))
    logger.info("ssh command printed source=%s", source_name)
    return 0


def cmd_webcam_preview(args, paths, config, logger):
    state = load_state(paths)
    record, source_name = endpoint_record(state, "webcam")
    if not record:
        print("No webcam MJPEG URL is available. Ingest discovery JSON first.", file=sys.stderr)
        return 1
    if not require_trusted_endpoint(record, config, args, "webcam"):
        return 1
    print_record_warnings(record, source_name, config)
    url = record["webcam"]["mjpeg_url"]
    command = ["ffplay", "-fflags", "nobuffer", "-flags", "low_delay", "-i", url]
    print(command_text(command))
    logger.info("webcam preview command printed source=%s", source_name)
    if args.run:
        return run_external(command)
    return 0


def cmd_webcam_expose(args, paths, config, logger):
    state = load_state(paths)
    record, source_name = endpoint_record(state, "webcam")
    if not record:
        print("No webcam MJPEG URL is available. Ingest discovery JSON first.", file=sys.stderr)
        return 1
    if not require_trusted_endpoint(record, config, args, "webcam"):
        return 1
    print_record_warnings(record, source_name, config)
    url = record["webcam"]["mjpeg_url"]
    device = args.device or config.get("webcam_device") or "/dev/video10"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-re", "-i", url, "-f", "v4l2", device]
    print(command_text(command))
    logger.info("webcam expose command printed source=%s device=%s", source_name, device)
    if args.run:
        return run_external(command)
    return 0


def cmd_lls(args, paths, config, logger):
    state = load_state(paths)
    record, source_name = endpoint_record(state, "lls")
    if not record:
        print("No LLs endpoint is available. Ingest discovery JSON first.", file=sys.stderr)
        return 1
    if not require_trusted_endpoint(record, config, args, "LLs"):
        return 1
    print_record_warnings(record, source_name, config)
    lls = record["lls"]
    if lls.get("control_url"):
        print("# control")
        print(command_text(["curl", "-fsS", lls["control_url"]]))
    if lls.get("status_url"):
        print("# status")
        print(command_text(["curl", "-fsS", lls["status_url"]]))
    logger.info("lls command printed source=%s", source_name)
    return 0


def run_external(command):
    if not shutil.which(command[0]):
        print(f"Required command is missing: {command[0]}", file=sys.stderr)
        return 127
    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        print(f"Failed to run {command[0]}: {exc}", file=sys.stderr)
        return 1
    return completed.returncode


def cmd_doctor(args, paths, config, logger):
    checks = []
    checks.append(("python >= 3.9", sys.version_info >= (3, 9), sys.version.split()[0]))
    for tool in ("ssh", "ffmpeg", "ffplay", "curl"):
        location = shutil.which(tool)
        checks.append((tool, bool(location), location or "missing"))

    systemctl = shutil.which("systemctl")
    checks.append(("systemctl", bool(systemctl), systemctl or "missing; systemd --user service is optional"))

    loopback_loaded = Path("/sys/module/v4l2loopback").exists()
    checks.append(("v4l2loopback module", loopback_loaded, "loaded" if loopback_loaded else "not loaded"))

    webcam_device = Path(str(config.get("webcam_device") or "/dev/video10"))
    checks.append((f"webcam_device {webcam_device}", webcam_device.exists(), "exists" if webcam_device.exists() else "missing"))

    failed_required = False
    for name, ok, detail in checks:
        prefix = "OK" if ok else "WARN"
        print(f"{prefix:4} {name}: {detail}")
        if name in {"python >= 3.9", "ssh", "ffmpeg"} and not ok:
            failed_required = True

    print("Generated files:")
    print_paths(paths)
    logger.info("doctor completed failed_required=%s", failed_required)
    return 1 if failed_required else 0


def cmd_paths(args, paths, config, logger):
    print_paths(paths)
    logger.info("paths shown")
    return 0


def cleanup_targets(paths, target):
    if target == "all":
        selected = ("config", "state", "logs", "cache")
    else:
        selected = (target,)

    paths_to_delete = []
    if "config" in selected:
        paths_to_delete.append(paths["config_file"])
    if "state" in selected:
        paths_to_delete.append(paths["state_file"])
    if "logs" in selected and paths["log_dir"].exists():
        paths_to_delete.extend(sorted(paths["log_dir"].iterdir()))
    if "cache" in selected and paths["cache_dir"].exists():
        paths_to_delete.extend(sorted(paths["cache_dir"].iterdir()))
    return paths_to_delete


def delete_path(path):
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def cmd_cleanup(args, paths, config, logger):
    targets = cleanup_targets(paths, args.target)
    if not targets:
        print("Nothing to delete.")
        return 0
    for path in targets:
        print(f"{path}  size={format_size(path_size(path))}")
    if not args.yes:
        print("Pass --yes to delete these helper-generated files.", file=sys.stderr)
        return 2
    logger.info("cleanup target=%s count=%s", args.target, len(targets))
    for path in targets:
        delete_path(path)
    print("Deleted selected helper-generated files.")
    return 0


def cmd_trust(args, paths, config, logger):
    config["trusted_device_id"] = args.device_id
    write_config(paths, config)
    logger.info("trusted_device_id set device_id=%s", args.device_id)
    print(f"trusted_device_id set to {args.device_id}")
    print(f"config: {paths['config_file']}")
    return 0


def cmd_reset_trust(args, paths, config, logger):
    if not args.yes:
        print("Pass --yes to clear trusted_device_id.", file=sys.stderr)
        return 2
    config["trusted_device_id"] = None
    write_config(paths, config)
    logger.info("trusted_device_id cleared")
    print("trusted_device_id cleared")
    return 0


def cmd_config(args, paths, config, logger):
    if args.config_action == "show":
        print(json.dumps(config, indent=2, sort_keys=True))
        print(f"config: {paths['config_file']}")
        return 0

    key = args.key
    value = args.value
    if key not in {
        "stale_after_seconds",
        "listen_host",
        "listen_port",
        "discovery_port",
        "discovery_multicast_group",
        "webcam_device",
    }:
        print(f"Unsupported config key: {key}", file=sys.stderr)
        return 1
    if key in {"stale_after_seconds", "listen_port", "discovery_port"}:
        try:
            value = int(value)
        except ValueError:
            print(f"{key} must be an integer.", file=sys.stderr)
            return 1
        if key == "stale_after_seconds" and value < 1:
            print("stale_after_seconds must be positive.", file=sys.stderr)
            return 1
        if key in {"listen_port", "discovery_port"} and not (1 <= value <= 65535):
            print(f"{key} must be between 1 and 65535.", file=sys.stderr)
            return 1
    else:
        value = clean_text(value)
        if not value:
            print(f"{key} cannot be empty.", file=sys.stderr)
            return 1
        if key == "discovery_multicast_group":
            try:
                address = ipaddress.ip_address(value)
                if address.version != 4 or not address.is_multicast:
                    raise ValueError
            except ValueError:
                print("discovery_multicast_group must be an IPv4 multicast address.", file=sys.stderr)
                return 1

    config[key] = value
    write_config(paths, config)
    logger.info("config set key=%s", key)
    print(f"{key} set to {value}")
    print(f"config: {paths['config_file']}")
    return 0


def cmd_tutorial(args, paths, config, logger):
    print(
        "\n".join(
            [
                "Sailfish Mother-PC Helper first use:",
                "1. Run `doctor` and install any missing host tools it reports.",
                "2. Start `listen` or enable the systemd --user service template.",
                "3. Sailfish Link UDP discovery on port 45177 is received automatically; HTTP POST /discovery and `ingest FILE` also work.",
                "4. Run `status`, verify the phone identity, then pin it with `trust DEVICE_ID`.",
                "5. Use `ssh-command`, `webcam-preview`, `webcam-expose`, and `lls` for explicit handoff commands.",
                "6. Use `paths` and `cleanup` to inspect or delete helper-generated files.",
            ]
        )
    )
    logger.info("tutorial shown")
    return 0


def make_http_handler(paths, config, logger, state_lock):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code, body):
            encoded = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path == "/status":
                self.send_json(200, load_state(paths))
                return
            if self.path == "/paths":
                self.send_json(200, path_report(paths))
                return
            self.send_json(404, {"error": "not found", "paths": ["/status", "/paths", "/discovery"]})

        def do_POST(self):
            if self.path != "/discovery":
                self.send_json(404, {"error": "not found", "paths": ["/discovery"]})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(411, {"error": "Content-Length must be an integer"})
                return
            if content_length < 1 or content_length > MAX_DISCOVERY_BYTES:
                self.send_json(413, {"error": f"payload must be 1..{MAX_DISCOVERY_BYTES} bytes"})
                return
            text = self.rfile.read(content_length).decode("utf-8", errors="replace")
            with state_lock:
                state = ingest_text(text, f"http:{self.client_address[0]}", paths, config, logger)
            status = state.get("last_ingest", {}).get("status")
            code = 200 if status in {"usable", "stale", "partial"} else 400
            self.send_json(code, {"status": status, "state_file": str(paths["state_file"])})

        def log_message(self, message, *args):
            logger.info("http %s - %s", self.address_string(), message % args)

    return Handler


class UdpDiscoveryListener:
    """Receive Sailfish Link's UDP broadcast/multicast discovery payloads."""

    def __init__(self, port, multicast_group, paths, config, logger, state_lock):
        self.paths = paths
        self.config = config
        self.logger = logger
        self.state_lock = state_lock
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("", port))
        self.port = self.socket.getsockname()[1]

        if multicast_group:
            joined = []
            failures = []
            for interface_address in local_ipv4_addresses() or ["0.0.0.0"]:
                membership = socket.inet_aton(multicast_group) + socket.inet_aton(interface_address)
                try:
                    self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                    joined.append(interface_address)
                except OSError as exc:
                    failures.append(f"{interface_address}: {exc}")
            if joined:
                self.logger.info(
                    "joined discovery multicast group=%s interfaces=%s",
                    multicast_group,
                    ",".join(joined),
                )
            else:
                # Sailfish Link also broadcasts, so an unavailable multicast route
                # should not prevent the helper from receiving LAN broadcasts.
                self.logger.warning(
                    "could not join discovery multicast group=%s: %s; accepting broadcasts only",
                    multicast_group,
                    "; ".join(failures) or "no usable IPv4 interface",
                )
        self.socket.settimeout(0.5)
        self.thread = threading.Thread(
            target=self._serve,
            name="sailfish-discovery-udp",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.socket.close()
        self.thread.join(timeout=2)

    def _serve(self):
        while not self.stop_event.is_set():
            try:
                data, address = self.socket.recvfrom(MAX_DISCOVERY_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                if not self.stop_event.is_set():
                    self.logger.exception("UDP discovery listener stopped unexpectedly")
                return

            source = f"udp:{address[0]}:{address[1]}"
            if len(data) > MAX_DISCOVERY_BYTES:
                self.logger.warning("ignoring oversized discovery datagram source=%s", source)
                continue
            text = data.decode("utf-8", errors="replace")
            with self.state_lock:
                state = ingest_text(text, source, self.paths, self.config, self.logger)
            self.logger.info(
                "udp discovery source=%s status=%s",
                source,
                state.get("last_ingest", {}).get("status", "unknown"),
            )


def make_http_server(host, port, paths, config, logger, state_lock):
    handler = make_http_handler(paths, config, logger, state_lock)
    server = ThreadingHTTPServer((host, port), handler)
    # A slow or abandoned client must not prevent a clean service shutdown.
    server.daemon_threads = True
    return server


GUI_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sailfish Phone Control</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; background: #102128; color: #eef5f4; }
body { margin: 0; } main { max-width: 940px; margin: auto; padding: 24px; }
h1 { margin-top: 0; } h2 { font-size: 1.15rem; margin: 0 0 12px; }
.grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); }
.card { background: #18323b; border: 1px solid #31515a; border-radius: 12px; padding: 16px; }
.wide { grid-column: 1 / -1; } dl { margin: 0; } dt { color: #9eb8bd; margin-top: 8px; }
dd { margin: 2px 0; overflow-wrap: anywhere; } button { margin: 4px 6px 4px 0; padding: 8px 11px; }
input { box-sizing: border-box; width: 100%; margin: 4px 0 8px; padding: 8px; }
.ok { color: #8ee0b5; } .warn { color: #ffd17a; } .error { color: #ff9d9d; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #0c181d; padding: 12px; border-radius: 8px; min-height: 4em; }
.hint { color: #b7cbd0; font-size: .92rem; }
</style>
</head>
<body><main>
<h1>Sailfish Phone Control</h1>
<p id="connection" class="warn">Waiting for Sailfish Link discovery…</p>
<div class="grid">
<section class="card"><h2>Discovered phone</h2><dl id="phone"></dl>
<button id="trust" onclick="trustPhone()" disabled>Trust this phone</button>
<button onclick="forgetTrust()">Forget trusted phone</button></section>
<section class="card"><h2>Private access tokens</h2>
<label>Webcam token <input id="webcamToken" type="password" autocomplete="off" placeholder="Paste only to change it"></label>
<label>LLs Remote token <input id="llsToken" type="password" autocomplete="off" placeholder="Paste only to change it"></label>
<button onclick="saveTokens()">Save entered tokens</button><button onclick="clearTokens()">Clear saved tokens</button>
<p id="tokenState" class="hint"></p></section>
<section class="card"><h2>Phone handoff</h2>
<button onclick="action('ssh-command')">Show SSH command</button>
<button onclick="action('webcam-url')">Show Webcam URL</button>
<button onclick="action('webcam-status')">Check Webcam status</button>
<p class="hint">The Webcam URL includes its saved token. Copy it into ffplay or a browser.</p></section>
<section class="card"><h2>LLs vPlayer</h2>
<button onclick="action('lls-status')">Status</button><button onclick="action('lls-play')">Play</button>
<button onclick="action('lls-pause')">Pause</button><button onclick="action('lls-toggle')">Toggle</button>
<button onclick="action('lls-previous')">Previous</button><button onclick="action('lls-next')">Next</button>
<button onclick="action('lls-seek-back')">−5 seconds</button><button onclick="action('lls-seek-forward')">+5 seconds</button>
<p class="hint">Open LLs vPlayer on the phone before sending controls.</p></section>
<section class="card wide"><h2>Result</h2><pre id="result">No action selected.</pre></section>
</div></main>
<script>
let dashboard = null;
function text(value) { return value === undefined || value === null || value === '' ? '—' : String(value); }
function show(value) { document.getElementById('result').textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({error: 'Invalid response from local dashboard.'}));
  if (!response.ok) throw new Error(data.error || ('Request failed: HTTP ' + response.status));
  return data;
}
function render(data) {
  dashboard = data;
  const current = (data.state || {}).current || {};
  const device = current.device || {};
  const ssh = current.ssh || {};
  const fields = [['Name', device.name], ['Device ID', device.id], ['Address', (current.network || {}).ip], ['SSH', ssh.user && ssh.host ? ssh.user + '@' + ssh.host + ':' + ssh.port : null], ['Last seen', current.received_at], ['Discovery quality', current.quality]];
  const list = document.getElementById('phone'); list.replaceChildren();
  for (const [label, value] of fields) { const dt = document.createElement('dt'); dt.textContent = label; const dd = document.createElement('dd'); dd.textContent = text(value); list.append(dt, dd); }
  const trust = data.trust || {}; const trusted = trust.configured && trust.matched;
  const message = current.device ? (trusted ? 'Trusted phone is ready for local actions.' : 'Verify the phone identity, then trust it before using endpoints.') : 'Waiting for Sailfish Link discovery…';
  const connection = document.getElementById('connection'); connection.textContent = message; connection.className = trusted ? 'ok' : 'warn';
  document.getElementById('trust').disabled = !device.id || trusted;
  document.getElementById('tokenState').textContent = 'Saved: Webcam ' + (data.tokens.webcam_configured ? 'yes' : 'no') + '; LLs ' + (data.tokens.lls_configured ? 'yes' : 'no') + '. Tokens are not displayed.';
}
async function refresh() { try { render(await request('/api/status')); } catch (error) { document.getElementById('connection').textContent = error.message; document.getElementById('connection').className = 'error'; } }
async function trustPhone() { const id = (((dashboard || {}).state || {}).current || {}).device?.id; if (!id) return; try { show(await request('/api/trust', {device_id: id})); await refresh(); } catch (error) { show(error.message); } }
async function forgetTrust() { try { show(await request('/api/trust/reset', {})); await refresh(); } catch (error) { show(error.message); } }
async function saveTokens() { const body = {}; const webcam = document.getElementById('webcamToken'); const lls = document.getElementById('llsToken'); if (webcam.value) body.webcam_token = webcam.value; if (lls.value) body.lls_token = lls.value; if (!Object.keys(body).length) { show('Enter at least one token to change it.'); return; } try { show(await request('/api/tokens', body)); webcam.value = ''; lls.value = ''; await refresh(); } catch (error) { show(error.message); } }
async function clearTokens() { try { show(await request('/api/tokens', {webcam_token: null, lls_token: null})); await refresh(); } catch (error) { show(error.message); } }
async function action(name) { try { show(await request('/api/action', {action: name})); } catch (error) { show(error.message); } }
refresh(); setInterval(refresh, 2000);
</script></body></html>"""


def is_loopback_address(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def with_query_token(url, token):
    """Return an HTTP endpoint with an optional token query parameter added safely."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Discovery supplied an invalid HTTP endpoint.")
    if not token:
        return url
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "token"]
    query.append(("token", token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def gui_endpoint_record(paths, config, endpoint):
    state = load_state(paths)
    record, source_name = endpoint_record(state, endpoint)
    if not record:
        raise LookupError(f"No {endpoint} endpoint has been discovered yet.")
    if not endpoint_is_trusted(record, config):
        raise PermissionError("Verify the discovered phone and click Trust this phone first.")
    return record, source_name


def fetch_endpoint_json(url, token=None, method="GET", body=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read(16 * 1024).decode("utf-8", errors="replace")
            return {"http_status": response.status, "body": decode_json_response(raw)}
    except HTTPError as exc:
        raw = exc.read(16 * 1024).decode("utf-8", errors="replace")
        return {"http_status": exc.code, "body": decode_json_response(raw)}
    except (URLError, OSError) as exc:
        raise ConnectionError(f"Could not reach the phone endpoint: {exc.reason if isinstance(exc, URLError) else exc}") from exc


def decode_json_response(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def gui_status(paths, config, secrets):
    state = load_state(paths)
    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    device_id = clean_text(deep_get(current, ("device", "id")))
    return {
        "state": state,
        "trust": trust_state(config, device_id),
        "tokens": {
            "webcam_configured": bool(secrets.get("webcam_token")),
            "lls_configured": bool(secrets.get("lls_token")),
        },
    }


def run_gui_action(action, paths, config, secrets):
    if action == "ssh-command":
        record, source_name = gui_endpoint_record(paths, config, "ssh")
        ssh = record["ssh"]
        return {"source": source_name, "command": command_text(["ssh", "-p", ssh["port"], f"{ssh['user']}@{ssh['host']}"])}

    if action in {"webcam-url", "webcam-status"}:
        record, source_name = gui_endpoint_record(paths, config, "webcam")
        webcam = record["webcam"]
        token = secrets.get("webcam_token")
        if action == "webcam-url":
            url = with_query_token(webcam["mjpeg_url"], token)
            return {"source": source_name, "url": url, "ffplay_command": command_text(["ffplay", "-fflags", "nobuffer", "-flags", "low_delay", "-i", url])}
        return {"source": source_name, "url": webcam["status_url"], "result": fetch_endpoint_json(webcam["status_url"], token)}

    lls_actions = {
        "lls-status": ("GET", None, None),
        "lls-play": ("POST", "play", None),
        "lls-pause": ("POST", "pause", None),
        "lls-toggle": ("POST", "toggle", None),
        "lls-previous": ("POST", "previous", None),
        "lls-next": ("POST", "next", None),
        "lls-seek-back": ("POST", "seek", {"delta_ms": -5000}),
        "lls-seek-forward": ("POST", "seek", {"delta_ms": 5000}),
    }
    if action not in lls_actions:
        raise ValueError("Unknown dashboard action.")

    record, source_name = gui_endpoint_record(paths, config, "lls")
    token = secrets.get("lls_token")
    if not token:
        raise PermissionError("Save the LLs Remote token in the dashboard before sending player controls.")
    method, suffix, body = lls_actions[action]
    lls = record["lls"]
    url = lls["status_url"] if suffix is None else lls["control_url"].rstrip("/") + "/" + suffix
    return {"source": source_name, "url": url, "result": fetch_endpoint_json(url, token, method, body)}


def make_gui_http_handler(paths, config, secrets, logger, state_lock):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code, body):
            encoded = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_html(self):
            encoded = GUI_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def is_local(self):
            if is_loopback_address(self.client_address[0]):
                return True
            self.send_json(403, {"error": "The dashboard is available only from this Linux computer."})
            return False

        def read_json(self):
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Content-Length must be an integer.") from exc
            if content_length < 2 or content_length > 16 * 1024:
                raise ValueError("Dashboard request must be 2..16384 bytes.")
            try:
                value = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("Dashboard request must be valid JSON.") from exc
            if not isinstance(value, dict):
                raise ValueError("Dashboard request must be a JSON object.")
            return value

        def do_GET(self):
            if not self.is_local():
                return
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                self.send_html()
            elif path == "/api/status":
                with state_lock:
                    self.send_json(200, gui_status(paths, config, secrets))
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self.is_local():
                return
            path = urlsplit(self.path).path
            try:
                body = self.read_json()
                with state_lock:
                    if path == "/api/trust":
                        current = load_state(paths).get("current") or {}
                        expected = clean_text(deep_get(current, ("device", "id")))
                        requested = clean_text(body.get("device_id"))
                        if not expected:
                            raise LookupError("No phone has been discovered yet.")
                        if requested != expected:
                            raise ValueError("Only the currently displayed device ID can be trusted.")
                        config["trusted_device_id"] = expected
                        write_config(paths, config)
                        logger.info("dashboard trusted device_id=%s", expected)
                        self.send_json(200, {"ok": True, "trusted_device_id": expected})
                    elif path == "/api/trust/reset":
                        config["trusted_device_id"] = None
                        write_config(paths, config)
                        logger.info("dashboard cleared trusted device")
                        self.send_json(200, {"ok": True, "trusted_device_id": None})
                    elif path == "/api/tokens":
                        for key in ("webcam_token", "lls_token"):
                            if key not in body:
                                continue
                            value = body[key]
                            if value is not None and not isinstance(value, str):
                                raise ValueError(f"{key} must be a string or null.")
                            if isinstance(value, str) and len(value.strip()) > GUI_TOKEN_MAX_LENGTH:
                                raise ValueError(f"{key} is too long.")
                            secrets[key] = normalize_secret(value)
                        write_secrets(paths, secrets)
                        logger.info("dashboard updated private tokens")
                        self.send_json(200, {"ok": True, "tokens": gui_status(paths, config, secrets)["tokens"]})
                    elif path == "/api/action":
                        action = clean_text(body.get("action"))
                        if not action:
                            raise ValueError("An action is required.")
                        result = run_gui_action(action, paths, config, secrets)
                        logger.info("dashboard action=%s", action)
                        self.send_json(200, {"ok": True, "action": action, "data": result})
                    else:
                        self.send_json(404, {"error": "not found"})
            except PermissionError as exc:
                self.send_json(403, {"error": str(exc)})
            except LookupError as exc:
                self.send_json(404, {"error": str(exc)})
            except (ValueError, ConnectionError) as exc:
                self.send_json(400, {"error": str(exc)})

        def log_message(self, message, *args):
            logger.info("dashboard %s - %s", self.address_string(), message % args)

    return Handler


def make_gui_http_server(host, port, paths, config, secrets, logger, state_lock):
    server = ThreadingHTTPServer((host, port), make_gui_http_handler(paths, config, secrets, logger, state_lock))
    server.daemon_threads = True
    return server


def cmd_gui(args, paths, config, logger):
    state_lock = threading.Lock()
    secrets = load_secrets(paths)
    server = make_gui_http_server(GUI_DEFAULT_HOST, args.port, paths, config, secrets, logger, state_lock)
    udp_listener = None
    if not args.no_udp:
        try:
            udp_listener = UdpDiscoveryListener(
                args.udp_port or int(config.get("discovery_port") or 45177),
                config.get("discovery_multicast_group"),
                paths,
                config,
                logger,
                state_lock,
            )
            udp_listener.start()
        except OSError as exc:
            server.server_close()
            raise SystemExit(f"Cannot listen for Sailfish UDP discovery: {exc}") from exc

    host, port = server.server_address[:2]
    dashboard_url = f"http://{host}:{port}/"
    logger.info("dashboard listening url=%s", dashboard_url)
    print(f"Sailfish Phone Control: {dashboard_url}")
    if udp_listener:
        print(f"Listening for Sailfish Link UDP discovery on port {udp_listener.port}.")
    else:
        print("UDP discovery disabled; displaying the state written by another helper listener.")
    print("The dashboard is restricted to this Linux computer.")
    if not args.no_open:
        webbrowser.open(dashboard_url, new=2)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping dashboard.")
        return 0
    finally:
        server.server_close()
        if udp_listener:
            udp_listener.close()


def cmd_listen(args, paths, config, logger):
    host = args.host or str(config.get("listen_host") or "0.0.0.0")
    port = args.port or int(config.get("listen_port") or 8765)
    udp_port = args.udp_port or int(config.get("discovery_port") or 45177)
    state_lock = threading.Lock()
    server = make_http_server(host, port, paths, config, logger, state_lock)
    udp_listener = None
    if not args.no_udp:
        try:
            udp_listener = UdpDiscoveryListener(
                udp_port,
                config.get("discovery_multicast_group"),
                paths,
                config,
                logger,
                state_lock,
            )
            udp_listener.start()
        except OSError as exc:
            server.server_close()
            raise SystemExit(f"Cannot listen for Sailfish UDP discovery on port {udp_port}: {exc}") from exc

    logger.info("listening host=%s port=%s", host, port)
    print(f"Listening on http://{host}:{port}")
    if udp_listener:
        print(f"Listening for Sailfish Link UDP discovery on port {udp_listener.port}.")
    print("POST discovery JSON to /discovery. GET /status for current state.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping listener.")
        return 0
    finally:
        server.server_close()
        if udp_listener:
            udp_listener.close()


def cmd_self_test(args, paths, config, logger):
    old_env = {key: os.environ.get(key) for key in (
        "SAILFISH_HELPER_CONFIG_DIR",
        "SAILFISH_HELPER_STATE_DIR",
        "SAILFISH_HELPER_CACHE_DIR",
        "SAILFISH_HELPER_LOG_DIR",
    )}
    with tempfile.TemporaryDirectory(prefix=f"{APP_NAME}-") as tmp:
        base = Path(tmp)
        os.environ["SAILFISH_HELPER_CONFIG_DIR"] = str(base / "config")
        os.environ["SAILFISH_HELPER_STATE_DIR"] = str(base / "state")
        os.environ["SAILFISH_HELPER_CACHE_DIR"] = str(base / "cache")
        os.environ["SAILFISH_HELPER_LOG_DIR"] = str(base / "logs")
        try:
            test_paths = app_paths()
            test_config = load_config(test_paths)
            test_config["stale_after_seconds"] = 3600
            payload = sample_payload()
            state = ingest_payload(payload, "self-test", test_paths, test_config)
            assert state["last_ingest"]["status"] == "usable"
            assert state["current"]["ssh"]["host"] == payload["ssh"]["host"]

            partial = sample_payload()
            partial["ssh"].pop("host")
            state = normalize_payload(partial, test_config, state, "self-test-partial")
            assert state["last_ingest"]["status"] == "partial"
            assert state["latest_usable"]["ssh"]["host"] == payload["ssh"]["host"]
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    print("self-test passed")
    logger.info("self-test passed")
    return 0


def sample_payload():
    now = isoformat(utc_now())
    return {
        "version": 1,
        "device": {"id": "sample-phone", "name": "Sample Sailfish Phone"},
        "announced_at": now,
        "network": {"ip": "192.0.2.20"},
        "ssh": {"host": "192.0.2.20", "port": 22, "user": "nemo"},
        "webcam": {
            "mjpeg_url": "http://192.0.2.20:8090/stream.mjpeg",
            "status_url": "http://192.0.2.20:8090/status.json",
        },
        "lls": {
            "control_url": "http://192.0.2.20:8091/control",
            "status_url": "http://192.0.2.20:8091/status",
        },
        "auth": {"token_hint": "pairing-required"},
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="sailfish-mother-pc-helper",
        description="Linux mother-PC helper for Sailfish discovery, SSH, webcam, and LLs handoff.",
    )
    parser.add_argument("--verbose", action="store_true", help="also log to stderr")
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", help="show current phone state and generated file sizes")
    status.add_argument("--json", action="store_true", help="print machine-readable status")
    status.set_defaults(func=cmd_status)

    ingest = subparsers.add_parser("ingest", help="ingest v1 discovery JSON from a file or stdin")
    ingest.add_argument("file", nargs="?", default="-", help="JSON file, or - for stdin")
    ingest.set_defaults(func=cmd_ingest)

    listen = subparsers.add_parser(
        "listen", help="receive discovery JSON over HTTP POST and Sailfish Link UDP"
    )
    listen.add_argument("--host", help="listen host; defaults to config listen_host")
    listen.add_argument("--port", type=int, help="listen port; defaults to config listen_port")
    listen.add_argument("--udp-port", type=int, help="Sailfish Link UDP port; defaults to config discovery_port")
    listen.add_argument("--no-udp", action="store_true", help="disable Sailfish Link UDP discovery for this run")
    listen.set_defaults(func=cmd_listen)

    gui = subparsers.add_parser(
        "gui", help="open the local Sailfish Phone Control dashboard and receive UDP discovery"
    )
    gui.add_argument("--port", type=int, default=GUI_DEFAULT_PORT, help=f"local dashboard port (default: {GUI_DEFAULT_PORT})")
    gui.add_argument("--udp-port", type=int, help="Sailfish Link UDP port; defaults to config discovery_port")
    gui.add_argument("--no-udp", action="store_true", help="show state from another listener instead of receiving UDP discovery")
    gui.add_argument("--no-open", action="store_true", help="do not open the dashboard in the default browser")
    gui.set_defaults(func=cmd_gui)

    ssh_command = subparsers.add_parser("ssh-command", help="print the SSH handoff command")
    ssh_command.add_argument(
        "--allow-untrusted", action="store_true", help="allow an unpinned discovery endpoint for diagnostics"
    )
    ssh_command.set_defaults(func=cmd_ssh_command)

    preview = subparsers.add_parser("webcam-preview", help="print or run the ffplay preview command")
    preview.add_argument("--run", action="store_true", help="run ffplay after printing the command")
    preview.add_argument(
        "--allow-untrusted", action="store_true", help="allow an unpinned discovery endpoint for diagnostics"
    )
    preview.set_defaults(func=cmd_webcam_preview)

    expose = subparsers.add_parser("webcam-expose", help="print or run the ffmpeg v4l2 exposure command")
    expose.add_argument("--device", help="target /dev/video* device; defaults to config webcam_device")
    expose.add_argument("--run", action="store_true", help="run ffmpeg after printing the command")
    expose.add_argument(
        "--allow-untrusted", action="store_true", help="allow an unpinned discovery endpoint for diagnostics"
    )
    expose.set_defaults(func=cmd_webcam_expose)

    lls = subparsers.add_parser("lls", help="print LLs control/status curl commands")
    lls.add_argument(
        "--allow-untrusted", action="store_true", help="allow an unpinned discovery endpoint for diagnostics"
    )
    lls.set_defaults(func=cmd_lls)
    subparsers.add_parser("doctor", help="check host prerequisites and configured device paths").set_defaults(func=cmd_doctor)
    subparsers.add_parser("paths", help="show config/state/log/cache paths and sizes").set_defaults(func=cmd_paths)
    subparsers.add_parser("tutorial", help="show first-use guidance").set_defaults(func=cmd_tutorial)
    subparsers.add_parser("self-test", help="run a small built-in smoke test").set_defaults(func=cmd_self_test)

    cleanup = subparsers.add_parser("cleanup", help="delete helper-generated config, state, logs, or cache")
    cleanup.add_argument("target", choices=("config", "state", "logs", "cache", "all"))
    cleanup.add_argument("--yes", action="store_true", help="confirm deletion")
    cleanup.set_defaults(func=cmd_cleanup)

    trust = subparsers.add_parser("trust", help="pin accepted discovery to one device.id")
    trust.add_argument("device_id")
    trust.set_defaults(func=cmd_trust)

    reset_trust = subparsers.add_parser("reset-trust", help="clear trusted_device_id")
    reset_trust.add_argument("--yes", action="store_true", help="confirm trust reset")
    reset_trust.set_defaults(func=cmd_reset_trust)

    config = subparsers.add_parser("config", help="show or change helper config")
    config_sub = config.add_subparsers(dest="config_action", required=True)
    config_sub.add_parser("show", help="show config JSON").set_defaults(func=cmd_config)
    config_set = config_sub.add_parser("set", help="set one config value")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.set_defaults(func=cmd_config)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2

    paths = app_paths()
    config = load_config(paths)
    logger = setup_logging(paths, args.verbose)
    try:
        return args.func(args, paths, config, logger)
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
