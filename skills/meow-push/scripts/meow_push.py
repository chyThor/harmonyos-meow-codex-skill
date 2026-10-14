"""Authorized checkpoint transport with external private configuration."""
import argparse
import contextlib
import ctypes
import getpass
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]


class SafeError(Exception):
    pass


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise SafeError("invalid_arguments")


def outside_skill(path):
    path = pathlib.Path(path).expanduser().resolve()
    if path.is_relative_to(SKILL_ROOT) or any((parent / "SKILL.md").is_file() for parent in path.parents):
        raise SafeError("private_data_inside_skill")
    return path


def config_path():
    base = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / ".local/share") / "MeowPush"
    return outside_skill(os.environ.get("MEOW_PUSH_CONFIG") or base / "config.dpapi")


def interactive_input():
    if not sys.stdin.isatty():
        return False
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(kernel.GetConsoleMode(kernel.GetStdHandle(wintypes.DWORD(-10)), ctypes.byref(mode)))
    return True


def crypt(data, decrypt=False):
    if os.name != "nt":
        raise SafeError("windows_encryption_required")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = Blob()
    description = ctypes.c_wchar_p()
    api = ctypes.windll.crypt32
    if decrypt:
        function = api.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        second = ctypes.byref(description)
    else:
        function = api.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        second = "Private checkpoint configuration"
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), second, None, None, None, 1, ctypes.byref(destination)):
        raise SafeError("private_config_unavailable")
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        free(ctypes.cast(destination.data, ctypes.c_void_p))
        if description:
            free(ctypes.cast(description, ctypes.c_void_p))


def normalize_config(value):
    if not isinstance(value, dict):
        raise SafeError("invalid_config")
    url = value.get("base_url") or value.get("baseUrl")
    recipient = value.get("recipient") or value.get("meowNickname")
    if not isinstance(url, str) or not isinstance(recipient, str) or not recipient.strip():
        raise SafeError("invalid_config")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SafeError("invalid_config")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SafeError("tls_required")
    timeout = float(value.get("timeout_seconds", value.get("timeoutSeconds", 10)))
    interval = float(value.get("minimum_interval_seconds", value.get("minimumIntervalSeconds", 3)))
    if not 1 <= timeout <= 30 or not 0 <= interval <= 60:
        raise SafeError("invalid_config")
    return dict(base_url=url.rstrip("/"), recipient=recipient.strip(), device_label=str(value.get("device_label") or "电脑"), timeout_seconds=timeout, minimum_interval_seconds=interval)


def load_config(path):
    if not path.is_file():
        raise SafeError("not_configured")
    if path.stat().st_size > 65536:
        raise SafeError("invalid_config")
    raw = path.read_bytes()
    if path.suffix.lower() == ".dpapi":
        if not raw.startswith(b"MEOW\x01"):
            raise SafeError("invalid_config")
        raw = crypt(raw[5:], True)
    elif os.name != "nt" and path.stat().st_mode & 0o077:
        raise SafeError("private_config_permissions")
    return normalize_config(json.loads(raw.decode("utf-8-sig")))


def secure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import subprocess
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=10)
        match = re.search(rb"S-1-5-[0-9-]+", result.stdout)
        if not match:
            raise SafeError("private_storage_permissions")
        sid = match.group().decode()
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True, timeout=10)
        if result.returncode:
            raise SafeError("private_storage_permissions")
    else:
        path.chmod(0o700)


def atomic_write(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as file:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a+b")
    if file.tell() == 0:
        file.write(b"0")
        file.flush()
    deadline = time.monotonic() + 30
    try:
        while True:
            try:
                file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise SafeError("busy")
                time.sleep(.05)
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)
    finally:
        file.close()


def clean(text, limit, cfg):
    text = re.sub(r"https?://[^\s<>]+", "[link omitted]", text, flags=re.I)
    text = re.sub(r"[A-Za-z]:[\\/][^\s<>]+", "[path omitted]", text)
    recipient_pattern = re.escape(cfg["recipient"])
    if re.fullmatch(r'[A-Za-z0-9_]+', cfg['recipient']):
        recipient_pattern = r'(?<![A-Za-z0-9_])' + recipient_pattern + r'(?![A-Za-z0-9_])'
    text = re.sub(recipient_pattern, "[private]", text, flags=re.I)
    for secret in (cfg["base_url"], urllib.parse.urlsplit(cfg["base_url"]).netloc):
        text = re.sub(re.escape(secret), "[private]", text, flags=re.I)
    text = re.sub(r"[\x00-\x1f\x7f\\/]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text.encode("utf-16-le")) // 2 > limit:
        text = text.encode("utf-16-le")[:(limit-1)*2].decode("utf-16-le", errors="ignore").rstrip() + "…"
    if not text:
        raise SafeError("empty_message")
    return text


def payload(path):
    path = outside_skill(path)
    if path.stat().st_size > 16384:
        raise SafeError("payload_too_large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SafeError("invalid_payload")
    for key in ("project", "stage", "message", "event_id"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise SafeError("invalid_payload")
    if len(value["event_id"]) > 200 or re.search(r"[\x00-\x1f]", value["event_id"]):
        raise SafeError("invalid_event_id")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send(value, cfg, path, authorized):
    if not authorized:
        raise SafeError("authorization_required")
    title = clean("GPT(" + cfg["device_label"] + ")·" + value["project"], 60, cfg)
    message = clean(value["stage"] + "：" + value["message"], 500, cfg)
    def make_url():
        return cfg["base_url"] + "/" + "/".join(urllib.parse.quote(s, safe="") for s in (cfg["recipient"], title, message))
    url = make_url()
    while len(url) > 6000 and len(message) > 1:
        message = message[:-1]
        url = make_url()
    if len(url) > 6000:
        raise SafeError("message_too_large")
    event = hashlib.sha256(value["event_id"].encode()).hexdigest()
    digest = hashlib.sha256((title + "|" + message).encode()).hexdigest()
    scope = hashlib.sha256((cfg["base_url"] + "|" + cfg["recipient"]).encode()).hexdigest()
    state_path = path.parent / ("events-" + scope + ".json")
    with lock(path.parent / "events.lock"):
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise SafeError("state_corrupt")
            if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
                raise SafeError("state_corrupt")
        else:
            state = {"events": {}, "last_sent": 0}
        previous = state["events"].get(event)
        if previous:
            if not isinstance(previous, dict) or previous.get('status') not in ('sent','delivery_unknown') or not isinstance(previous.get('digest'),str):
                raise SafeError('state_corrupt')
            if previous["digest"] != digest:
                raise SafeError("event_conflict")
            if previous["status"] == "sent":
                return {"ok": True, "status": "duplicate", "event": event}
            raise SafeError("delivery_unknown")
        delay = cfg["minimum_interval_seconds"] - (time.time() - float(state.get("last_sent", 0)))
        if delay > 0:
            time.sleep(min(delay, cfg["minimum_interval_seconds"]))
        outcome = "delivery_unknown"
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(urllib.request.Request(url, method="GET"), timeout=cfg["timeout_seconds"]) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise SafeError("invalid_response")
                result = json.loads(raw)
            if isinstance(result, dict) and str(result.get("status")) == "200" and (result.get("data") is None or result.get("data") is True):
                outcome = "sent"
            else:
                raise SafeError("api_rejected")
        except SafeError as error:
            if str(error) == 'api_rejected':
                raise
            outcome = 'delivery_unknown'
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            outcome = "delivery_unknown"
        state["events"][event] = {"digest": digest, "status": outcome, "at": int(time.time())}
        if outcome == "sent":
            state["last_sent"] = time.time()
        try:
            atomic_write(state_path, json.dumps(state).encode())
        except OSError:
            raise SafeError("delivery_unknown")
        if outcome != "sent":
            raise SafeError("delivery_unknown")
    return {"ok": True, "status": "sent", "event": event}


def main(argv=None):
    parser = SafeParser(description="Send authorized checkpoint summaries using private local configuration.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    setup = sub.add_parser("setup")
    setup.add_argument("--from-json")
    setup.add_argument("--device-label")
    setup.add_argument("--replace", action="store_true")
    for name in ("send", "dry-run"):
        item = sub.add_parser(name)
        item.add_argument("--payload", required=True)
        if name == "send":
            item.add_argument("--authorized", action="store_true")
    args = parser.parse_args(argv)
    path = config_path()
    if args.command == "setup":
        if os.name != 'nt':
            raise SafeError('windows_encryption_required')
        if path.exists() and not args.replace:
            raise SafeError("config_exists")
        if args.from_json:
            source = outside_skill(args.from_json)
            raw = json.loads(source.read_text(encoding="utf-8-sig"))
        else:
            if not interactive_input():
                raise SafeError('interactive_setup_required')
            raw = {"base_url": getpass.getpass("Service base address (hidden): "), "recipient": getpass.getpass("Recipient (hidden): ")}
        if args.device_label:
            raw["device_label"] = args.device_label
        cfg = normalize_config(raw)
        if path.suffix.lower() != ".dpapi":
            raise SafeError("encrypted_setup_required")
        secure_dir(path.parent)
        atomic_write(path, b"MEOW\x01" + crypt(json.dumps(cfg, ensure_ascii=False).encode()))
        return {"ok": True, "status": "configured", "storage": "current-user-encrypted"}
    if args.command == "status":
        try:
            load_config(path)
        except SafeError as error:
            if str(error) == "not_configured":
                return {"ok": True, "configured": False}
            raise
        return {"ok": True, "configured": True, "storage": "current-user-encrypted" if path.suffix.lower() == ".dpapi" else "external-private-file"}
    value = payload(args.payload)
    cfg = load_config(path)
    if args.command == "dry-run":
        title = clean("GPT(" + cfg["device_label"] + ")·" + value["project"], 60, cfg)
        message = clean(value["stage"] + "：" + value["message"], 500, cfg)
        return {"ok": True, "status": "dry-run", "title_units": len(title.encode("utf-16-le")) // 2, "message_units": len(message.encode("utf-16-le")) // 2}
    return send(value, cfg, path, args.authorized)


if __name__ == "__main__":
    try:
        result = main()
    except SafeError as error:
        result = {"ok": False, "status": str(error)}
    except Exception:
        result = {"ok": False, "status": "operation_failed"}
    except KeyboardInterrupt:
        result = {"ok": False, "status": "cancelled"}
    print(json.dumps(result, ensure_ascii=True))
    sys.exit(0 if result.get("ok") else 1)
