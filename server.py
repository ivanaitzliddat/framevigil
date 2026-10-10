"""Local, browser-based OCR monitor. Frames are processed in memory only."""

from __future__ import annotations

import argparse
import csv
import hmac
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from collections import Counter
from collections import OrderedDict

from host_capture import HostCaptureManager, discover_devices


ROOT = Path(__file__).resolve().parent
MAX_IMAGE_BYTES = 2_000_000
MAX_RULES = 20
EVENT_LIMIT = 50
COOLDOWN_SECONDS = 15
DEFAULT_ACK_SECONDS = 3600
MIN_ACK_SECONDS = 60
MAX_ACK_SECONDS = 7 * 24 * 3600
EVIDENCE_TTL_SECONDS = 60 * 60
MAX_EVIDENCE_BYTES = 40_000_000


def clean_rule(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Every alert term must be text.")
    rule = " ".join(value.split())
    if not 2 <= len(rule) <= 80 or any(ord(char) < 32 for char in rule):
        raise ValueError("Alert terms must contain 2–80 printable characters.")
    return rule


def clean_keyword(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Every keyword must be text.")
    keyword = " ".join(value.split())
    if not 1 <= len(keyword) <= 80 or any(ord(char) < 32 for char in keyword):
        raise ValueError("Keywords must contain 1–80 printable characters.")
    return keyword


class TesseractOCR:
    @property
    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def extract_lines(self, image: bytes) -> list[dict]:
        if not self.available:
            raise RuntimeError("Tesseract is unavailable. Install it before scanning.")
        try:
            result = subprocess.run(
                ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "11", "tsv"],
                input=image,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("OCR timed out after 15 seconds.") from exc
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"OCR failed: {detail[:160]}")
        lines: dict[tuple[str, ...], dict] = {}
        reader = csv.DictReader(io.StringIO(result.stdout.decode("utf-8", errors="replace")), delimiter="\t")
        for row in reader:
            if row.get("level") != "5" or not row.get("text", "").strip():
                continue
            try:
                left, top = int(row["left"]), int(row["top"])
                right, bottom = left + int(row["width"]), top + int(row["height"])
            except (KeyError, ValueError):
                continue
            key = tuple(row.get(part, "") for part in ("page_num", "block_num", "par_num", "line_num"))
            group = lines.setdefault(key, {"words": [], "box": [left, top, right, bottom]})
            group["words"].append({"text": row["text"].strip(), "box": [left, top, right, bottom]})
            group["box"] = [min(group["box"][0], left), min(group["box"][1], top), max(group["box"][2], right), max(group["box"][3], bottom)]
        return [{"text": " ".join(word["text"] for word in group["words"]), "box": group["box"], "words": group["words"]} for group in lines.values()]

    def extract(self, image: bytes) -> str:
        return "\n".join(line["text"] for line in self.extract_lines(image))


def validate_conditions(values: object) -> list[dict]:
    if not isinstance(values, list) or len(values) > MAX_RULES:
        raise ValueError(f"Provide up to {MAX_RULES} conditions.")
    conditions = []
    names = set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Each condition must be an object.")
        name = clean_rule(value.get("name"))
        if name.casefold() in names:
            raise ValueError("Condition names must be unique for each source.")
        names.add(name.casefold())
        keywords = value.get("keywords")
        if not isinstance(keywords, list) or len(keywords) > 8:
            raise ValueError("A condition supports up to 8 keywords.")
        keywords = [clean_keyword(word) for word in keywords]
        color = value.get("color") or None
        if color is not None and (not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
            raise ValueError("Background color must be a six-digit hex value.")
        if not keywords and not color:
            raise ValueError("A condition needs at least one keyword or a background color.")
        message = value.get("message", "")
        if not isinstance(message, str) or len(message) > 160 or any(ord(char) < 32 for char in message):
            raise ValueError("Spoken message must be at most 160 printable characters.")
        identifier = value.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{12}", identifier):
            identifier = secrets.token_hex(6)
        conditions.append({"id": identifier, "name": name, "keywords": keywords, "color": color, "message": message.strip(), "enabled": value.get("enabled", True) is not False})
    return conditions


def matches_background(image: bytes, box: list[int], color: str, tolerance: int = 25) -> bool:
    from PIL import Image

    with Image.open(io.BytesIO(image)) as source:
        picture = source.convert("RGB")
        left, top, right, bottom = box
        crop = picture.crop((max(0, left), max(0, top), min(picture.width, right), min(picture.height, bottom)))
        if not crop.width or not crop.height:
            return False
        common = Counter(crop.get_flattened_data()).most_common(1)[0][0]
    target = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    return all(abs(actual - expected) <= tolerance for actual, expected in zip(common, target))


def boxes_for_phrases(line: dict, phrases: list[str]) -> list[list[int]]:
    """Return OCR word rectangles that overlap the requested phrases in one line."""
    words = line.get("words") or []
    if not words:
        return [line["box"]] if line.get("box") else []
    positions = []
    offset = 0
    for word in words:
        end = offset + len(word["text"])
        positions.append((offset, end, word["box"]))
        offset = end + 1
    text = line["text"].casefold()
    boxes = []
    for phrase in phrases:
        query = phrase.casefold()
        cursor = 0
        while (start := text.find(query, cursor)) >= 0:
            end = start + len(query)
            boxes.extend(box for left, right, box in positions if left < end and right > start)
            cursor = end
    return [list(box) for box in dict.fromkeys(tuple(box) for box in boxes)]


def unique_boxes(boxes: list[list[int]]) -> list[list[int]]:
    return [list(box) for box in dict.fromkeys(tuple(box) for box in boxes)]


class MonitorState:
    def __init__(self, ocr: TesseractOCR | object):
        self.ocr = ocr
        self.lock = threading.Lock()
        self.rules: list[str] = []
        self.events: list[dict[str, object]] = []
        self.last_fired: dict[str, float] = {}
        self.acknowledged: dict[str, float] = {}
        self.ack_duration_seconds = DEFAULT_ACK_SECONDS
        self.evidence: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.evidence_bytes = 0
        self.scan_count = 0
        self.last_scan: str | None = None

    @staticmethod
    def validate_rules(values: object) -> list[str]:
        if not isinstance(values, list) or len(values) > MAX_RULES:
            raise ValueError(f"Provide a list of up to {MAX_RULES} alert terms.")
        rules = []
        seen = set()
        for value in values:
            rule = clean_rule(value)
            key = rule.casefold()
            if key not in seen:
                rules.append(rule)
                seen.add(key)
        return rules

    def set_rules(self, values: object) -> list[str]:
        rules = self.validate_rules(values)
        with self.lock:
            self.rules = rules
            self.last_fired = {key: stamp for key, stamp in self.last_fired.items() if not key.startswith("browser:")}
        return rules

    def reset_rule_matches(self, source_id: str) -> None:
        with self.lock:
            self.last_fired = {key: stamp for key, stamp in self.last_fired.items() if not key.startswith(f"{source_id}:")}

    def reset_condition_matches(self, source_id: str) -> None:
        with self.lock:
            self.last_fired = {key: stamp for key, stamp in self.last_fired.items() if not key.startswith(f"{source_id}:")}

    def status(self) -> dict[str, object]:
        with self.lock:
            return {
                "ocr_available": bool(self.ocr.available),
                "rules": list(self.rules),
                "scan_count": self.scan_count,
                "last_scan": self.last_scan,
                "event_count": len(self.events),
                "ack_duration_seconds": self.ack_duration_seconds,
            }

    def recent_events(self) -> list[dict[str, object]]:
        with self.lock:
            now = time.time()
            self.acknowledged = {key: expiry for key, expiry in self.acknowledged.items() if expiry > now}
            return [{**event, "acknowledged_until": self.acknowledged.get(event["fingerprint"])} for event in reversed(self.events)]

    def set_ack_duration(self, seconds: object) -> int:
        if type(seconds) is not int or not MIN_ACK_SECONDS <= seconds <= MAX_ACK_SECONDS:
            raise ValueError("Acknowledgement duration must be between 1 minute and 7 days.")
        with self.lock:
            self.ack_duration_seconds = seconds
        return seconds

    def load_acknowledgements(self, entries: object) -> None:
        if not isinstance(entries, dict):
            return
        now = time.time()
        with self.lock:
            self.acknowledged = {
                key: expiry for key, expiry in entries.items()
                if isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key)
                and type(expiry) in (int, float) and now < expiry <= now + MAX_ACK_SECONDS
            }

    def settings_snapshot(self) -> dict:
        with self.lock:
            return {"ack_duration_seconds": self.ack_duration_seconds,
                    "acknowledged": {key: expiry for key, expiry in self.acknowledged.items() if expiry > time.time()}}

    def clear_acknowledgements(self) -> None:
        with self.lock:
            self.acknowledged.clear()

    def acknowledge(self, event_id: str, now: float | None = None) -> dict:
        with self.lock:
            event = next((item for item in self.events if item["id"] == event_id), None)
            if event is None:
                raise KeyError(event_id)
            stamp = time.time() if now is None else now
            expiry = stamp + self.ack_duration_seconds
            self.acknowledged[event["fingerprint"]] = expiry
            return {**event, "acknowledged_until": expiry}

    def unacknowledge(self, event_id: str) -> dict:
        with self.lock:
            event = next((item for item in self.events if item["id"] == event_id), None)
            if event is None:
                raise KeyError(event_id)
            self.acknowledged.pop(event["fingerprint"], None)
            return {**event, "acknowledged_until": None}

    def evidence_for(self, event_id: str) -> bytes | None:
        with self.lock:
            self._prune_evidence(time.time())
            if not any(event["id"] == event_id for event in self.events):
                return None
            record = self.evidence.get(event_id)
            return record[1] if record else None

    def _prune_evidence(self, now: float) -> None:
        for identifier, (stamp, image) in list(self.evidence.items()):
            if now - stamp > EVIDENCE_TTL_SECONDS:
                self.evidence.pop(identifier)
                self.evidence_bytes -= len(image)
        while self.evidence_bytes > MAX_EVIDENCE_BYTES and self.evidence:
            _, (_, image) = self.evidence.popitem(last=False)
            self.evidence_bytes -= len(image)

    def scan(
        self,
        image: bytes,
        now: float | None = None,
        source_id: str = "browser",
        source_name: str = "Browser feed",
        rules: list[str] | None = None,
        conditions: list[dict] | None = None,
    ) -> dict[str, object]:
        if hasattr(self.ocr, "extract_lines"):
            lines = self.ocr.extract_lines(image)
            recognized = "\n".join(line["text"] for line in lines)
        else:
            recognized = self.ocr.extract(image)
            lines = [{"text": line, "box": None} for line in recognized.splitlines()]
        normalized = " ".join(recognized.split()).casefold()
        stamp = time.time() if now is None else now
        iso_time = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        new_events = []
        with self.lock:
            self.scan_count += 1
            self.last_scan = iso_time
            self.last_fired = {key: value for key, value in self.last_fired.items() if stamp - value <= EVIDENCE_TTL_SECONDS}
            self.acknowledged = {key: expiry for key, expiry in self.acknowledged.items() if expiry > stamp}
            active_rules = self.rules if rules is None else rules

            def fingerprint(trigger: str, line: str) -> str:
                identity = json.dumps([source_id, trigger, " ".join(line.split()).casefold()], ensure_ascii=False)
                return hashlib.sha256(identity.encode("utf-8")).hexdigest()

            def add_event(term: str, message: str, line: str, boxes: list[list[int]], identity: str) -> None:
                event = {
                    "term": term, "message": message, "line": line[:240], "boxes": boxes,
                    "time": iso_time, "id": secrets.token_hex(12), "source": source_name, "source_id": source_id,
                    "fingerprint": identity,
                }
                self.events.append(event)
                new_events.append(event)
                self.evidence[event["id"]] = (stamp, image)
                self.evidence_bytes += len(image)

            for rule in active_rules:
                matching = [line for line in lines if rule.casefold() in line["text"].casefold()]
                if not matching and rule.casefold() in normalized:
                    matching = [{"text": rule, "box": None}]
                grouped: dict[str, list[dict]] = {}
                for line in matching:
                    grouped.setdefault(" ".join(line["text"].split()).casefold(), []).append(line)
                for line_key, group in list(grouped.items())[:10]:
                    identity = fingerprint(f"rule:{rule.casefold()}", line_key)
                    last_key = f"{source_id}:{identity}"
                    if identity in self.acknowledged or stamp - self.last_fired.get(last_key, float("-inf")) < COOLDOWN_SECONDS:
                        continue
                    boxes = unique_boxes([box for line in group for box in boxes_for_phrases(line, [rule])])
                    add_event(rule, rule, group[0]["text"], boxes, identity)
                    self.last_fired[last_key] = stamp
            for condition in conditions or []:
                if not condition.get("enabled", True):
                    continue
                grouped: dict[str, list[dict]] = {}
                for line in lines:
                    line_text = " ".join(line["text"].split()).casefold()
                    if not all(word.casefold() in line_text for word in condition["keywords"]):
                        continue
                    if condition.get("color") and (not line["box"] or not matches_background(image, line["box"], condition["color"])):
                        continue
                    grouped.setdefault(line_text, []).append(line)
                for line_text, group in list(grouped.items())[:10]:
                    identity = fingerprint(f"condition:{condition['id']}", line_text)
                    last_key = f"{source_id}:{identity}"
                    if identity in self.acknowledged or stamp - self.last_fired.get(last_key, float("-inf")) < COOLDOWN_SECONDS:
                        continue
                    boxes = unique_boxes([box for line in group for box in (boxes_for_phrases(line, condition["keywords"]) if condition["keywords"] else [line["box"]])])
                    add_event(condition["name"], condition.get("message") or group[0]["text"], group[0]["text"], boxes, identity)
                    self.last_fired[last_key] = stamp
            self.events = self.events[-EVENT_LIMIT:]
            for identifier in set(self.evidence) - {event["id"] for event in self.events}:
                _, saved = self.evidence.pop(identifier)
                self.evidence_bytes -= len(saved)
            self._prune_evidence(stamp)
        return {"events": new_events, "recognized_characters": len(recognized), "scanned_at": iso_time}


class Handler(BaseHTTPRequestHandler):
    server: "MonitorServer"

    def log_message(self, format: str, *args: object) -> None:
        # Access logs can disclose alert terms in other implementations; keep local logs quiet.
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value).encode("utf-8"), "application/json; charset=utf-8")

    def _authorized_host(self) -> bool:
        return self.headers.get("Host") in self.server.allowed_hosts

    def _authorized_post(self) -> bool:
        return self._authorized_host() and self.headers.get("Origin") in self.server.allowed_origins

    def _authorized_api(self) -> bool:
        if not self.server.access_token:
            return True
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header, f"Bearer {self.server.access_token}")

    def do_GET(self) -> None:
        if not self._authorized_host():
            self._json(403, {"error": "Invalid host."})
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/") and not self._authorized_api():
            self._json(401, {"error": "Enter the access code to continue."})
            return
        if path == "/api/status":
            self._json(200, self.server.state.status())
        elif path == "/api/settings":
            self._json(200, {"ack_duration_seconds": self.server.state.status()["ack_duration_seconds"]})
        elif path == "/api/events":
            self._json(200, {"events": self.server.state.recent_events()})
        elif path == "/api/devices":
            self._json(200, {"devices": discover_devices()})
        elif path == "/api/hardware":
            devices = discover_devices()
            self._json(200, {"devices": devices, "sources": self.server.host_capture.list_sources(devices)})
        elif path == "/api/sources":
            self._json(200, {"sources": self.server.host_capture.list_sources()})
        elif path.startswith("/api/evidence/"):
            identifier = path.removeprefix("/api/evidence/")
            frame = self.server.state.evidence_for(identifier) if re.fullmatch(r"[a-f0-9]{24}", identifier) else None
            if frame:
                self._send(200, frame, "image/jpeg")
            else:
                self._json(404, {"error": "This alert image has expired."})
        elif path.startswith("/api/frames/"):
            identifier = path.removeprefix("/api/frames/")
            frame = self.server.host_capture.frame(identifier)
            if frame:
                self._send(200, frame, "image/jpeg")
            else:
                self._json(404, {"error": "No recent frame for this source."})
        elif path in ("/", "/app.js", "/style.css"):
            filename = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
            kind = "text/html" if filename.endswith(".html") else "text/javascript" if filename.endswith(".js") else "text/css"
            self._send(200, (ROOT / "web" / filename).read_bytes(), f"{kind}; charset=utf-8")
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._authorized_post():
            self._json(403, {"error": "Requests must come from the local app."})
            return
        if not self._authorized_api():
            self._json(401, {"error": "Enter the access code to continue."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Invalid request length."})
            return
        path = urlsplit(self.path).path
        limit = MAX_IMAGE_BYTES if path == "/api/scan" else 4096
        action_path = path == "/api/shutdown" or (path.startswith("/api/sources/") and path.rsplit("/", 1)[-1] in ("start", "stop")) or path.rsplit("/", 1)[-1] in ("ack", "unack", "clear")
        if length < 0 or length > limit or (length == 0 and not action_path):
            self._json(413, {"error": "Request is empty or too large."})
            return
        body = self.rfile.read(length)
        try:
            if path == "/api/shutdown":
                self._json(200, {"message": "FrameVigil is shutting down. Restart the Python service on the host to use it again."})
                threading.Thread(target=self.server.shutdown, daemon=True, name="framevigil-shutdown").start()
            elif path == "/api/rules":
                if self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected JSON.")
                value = json.loads(body)
                rules = self.server.state.set_rules(value.get("rules") if isinstance(value, dict) else None)
                self._json(200, {"rules": rules})
            elif path == "/api/settings":
                if self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected JSON.")
                value = json.loads(body)
                seconds = self.server.state.set_ack_duration(value.get("ack_duration_seconds") if isinstance(value, dict) else None)
                self.server.save_settings()
                self._json(200, {"ack_duration_seconds": seconds})
            elif path == "/api/acknowledgements/clear":
                self.server.state.clear_acknowledgements()
                self.server.save_settings()
                self._json(200, {"events": self.server.state.recent_events()})
            elif path.startswith("/api/events/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4 or parts[:2] != ["api", "events"] or not re.fullmatch(r"[a-f0-9]{24}", parts[2]) or parts[3] not in ("ack", "unack"):
                    self._json(404, {"error": "Not found."})
                    return
                event = self.server.state.acknowledge(parts[2]) if parts[3] == "ack" else self.server.state.unacknowledge(parts[2])
                self.server.save_settings()
                self._json(200, {"event": event})
            elif path == "/api/scan":
                if self.headers.get("Content-Type") != "image/jpeg" or not body.startswith(b"\xff\xd8"):
                    raise ValueError("Expected a JPEG frame.")
                source_id = self.headers.get("X-FrameVigil-Source", "browser")
                if source_id != "browser" and not re.fullmatch(r"browser:[a-f0-9]{32}:[a-f0-9]{16}", source_id):
                    raise ValueError("Invalid browser source identifier.")
                source_name = self.headers.get("X-FrameVigil-Name", "Browser feed")
                if not 1 <= len(source_name) <= 60 or any(ord(char) < 32 or ord(char) > 126 for char in source_name):
                    raise ValueError("Invalid browser source name.")
                self._json(200, self.server.state.scan(body, source_id=source_id, source_name=source_name))
            elif path == "/api/sources":
                if self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Expected JSON.")
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("Expected a source object.")
                source = self.server.host_capture.add(value.get("device_id"), value.get("name"), value.get("interval", 5))
                self._json(201, {"source": source})
            elif path.startswith("/api/sources/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    self._json(404, {"error": "Not found."})
                    return
                identifier, action = parts[2], parts[3]
                if action == "start":
                    self.server.host_capture.start(identifier)
                elif action == "stop":
                    self.server.host_capture.stop(identifier)
                elif action == "name":
                    if self.headers.get("Content-Type") != "application/json":
                        raise ValueError("Expected JSON.")
                    value = json.loads(body)
                    self.server.host_capture.rename(identifier, value.get("name") if isinstance(value, dict) else None)
                elif action == "interval":
                    if self.headers.get("Content-Type") != "application/json":
                        raise ValueError("Expected JSON.")
                    value = json.loads(body)
                    self.server.host_capture.update_interval(identifier, value.get("interval") if isinstance(value, dict) else None)
                elif action == "rules":
                    if self.headers.get("Content-Type") != "application/json":
                        raise ValueError("Expected JSON.")
                    value = json.loads(body)
                    rules = self.server.state.validate_rules(value.get("rules") if isinstance(value, dict) else None)
                    self.server.host_capture.update_rules(identifier, rules)
                elif action == "conditions":
                    if self.headers.get("Content-Type") != "application/json":
                        raise ValueError("Expected JSON.")
                    value = json.loads(body)
                    conditions = validate_conditions(value.get("conditions") if isinstance(value, dict) else None)
                    self.server.host_capture.update_conditions(identifier, conditions)
                else:
                    self._json(404, {"error": "Not found."})
                    return
                self._json(200, {"sources": self.server.host_capture.list_sources()})
            else:
                self._json(404, {"error": "Not found."})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except KeyError:
            self._json(404, {"error": "Source or alert not found."})
        except RuntimeError as exc:
            self._json(503, {"error": str(exc)})

    def do_DELETE(self) -> None:
        if not self._authorized_post() or not self._authorized_api():
            self._json(403, {"error": "Request denied."})
            return
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "sources"]:
            self._json(404, {"error": "Not found."})
            return
        try:
            self.server.host_capture.remove(parts[2])
        except KeyError:
            self._json(404, {"error": "Source not found."})
            return
        self._json(200, {"sources": self.server.host_capture.list_sources()})


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        port: int,
        ocr: TesseractOCR | object | None = None,
        host: str = "127.0.0.1",
        allowed_origins: list[str] | None = None,
        access_token: str | None = None,
        source_config_path: Path | None = None,
        settings_path: Path | None = None,
    ):
        if host != "127.0.0.1" and not access_token:
            raise ValueError("A non-loopback listener requires an access token.")
        super().__init__((host, port), Handler)
        self.state = MonitorState(ocr or TesseractOCR())
        self.settings_lock = threading.Lock()
        source_config_path = source_config_path or Path.home() / ".config" / "framevigil" / "sources.json"
        self.settings_path = settings_path or source_config_path.parent / "settings.json"
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.state.set_ack_duration(saved.get("ack_duration_seconds", DEFAULT_ACK_SECONDS))
                self.state.load_acknowledgements(saved.get("acknowledged"))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass
        self.host_capture = HostCaptureManager(
            self.state,
            source_config_path,
        )
        self.access_token = access_token
        self.allowed_origins = {f"http://127.0.0.1:{self.server_port}"}
        self.allowed_origins.update(allowed_origins or [])
        self.allowed_hosts = {urlsplit(origin).netloc for origin in self.allowed_origins}
        if host != "127.0.0.1":
            self.allowed_origins.add(f"http://{host}:{self.server_port}")
            self.allowed_hosts.add(f"{host}:{self.server_port}")

    def server_close(self) -> None:
        self.host_capture.shutdown()
        super().server_close()

    def save_settings(self) -> None:
        with self.settings_lock:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.state.settings_snapshot(), indent=2) + "\n", encoding="utf-8")
            temp.chmod(0o600)
            temp.replace(self.settings_path)


def load_or_create_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(24)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token + "\n")
    if len(token) < 24:
        raise ValueError("Access token must be at least 24 characters.")
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description="FrameVigil private OCR monitor")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; 0.0.0.0 requires an access token")
    parser.add_argument("--allowed-origin", action="append", default=[], help="Additional browser origin, e.g. a Tailscale HTTPS URL")
    parser.add_argument("--access-token-file", type=Path, help="Private file containing a bearer access code")
    args = parser.parse_args()
    token = load_or_create_token(args.access_token_file) if args.access_token_file else None
    with MonitorServer(args.port, host=args.host, allowed_origins=args.allowed_origin, access_token=token) as server:
        print(f"Listening on {args.host}:{server.server_port}. Open http://127.0.0.1:{server.server_port}/ locally.", flush=True)
        if args.access_token_file:
            print(f"Access code file: {args.access_token_file}", flush=True)
        if not server.state.ocr.available:
            print("OCR needs Tesseract; install it before scanning frames.", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
