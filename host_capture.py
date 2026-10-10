"""Portable FFmpeg video-device discovery and session-only host capture."""

from __future__ import annotations

import json
import io
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable


MAX_SOURCES = 8
INTERVAL_MIN = 2
INTERVAL_MAX = 30
_AVF_RATE_CACHE: dict[str, str] = {}
DEMO_DEVICE = {"id": "demo:status-board", "name": "Demo status board (synthetic)", "locator": "demo"}


def parse_avfoundation_devices(output: str) -> list[dict[str, str]]:
    devices = []
    in_video = False
    seen: dict[str, int] = {}
    for line in output.splitlines():
        if "AVFoundation video devices:" in line:
            in_video = True
            continue
        if "AVFoundation audio devices:" in line:
            break
        if not in_video:
            continue
        match = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if match:
            index, label = match.groups()
            ordinal = seen.get(label, 0)
            seen[label] = ordinal + 1
            devices.append({"id": f"avfoundation:{label}:{ordinal}", "name": label, "locator": index})
    return devices


def parse_dshow_devices(output: str) -> list[dict[str, str]]:
    devices = []
    in_video = False
    for line in output.splitlines():
        if "DirectShow video devices" in line:
            in_video = True
            continue
        if "DirectShow audio devices" in line:
            break
        if not in_video:
            continue
        alternate = re.search(r'Alternative name "([^"]+)"', line)
        if alternate and devices:
            devices[-1]["id"] = f"dshow:{alternate.group(1)}"
            devices[-1]["locator"] = alternate.group(1)
            continue
        label = re.search(r'"([^"]+)"', line)
        if label:
            name = label.group(1)
            devices.append({"id": f"dshow:{name}", "name": name, "locator": name})
    return devices


def discover_devices(system: str | None = None) -> list[dict[str, str]]:
    platform = system or sys.platform
    if platform == "darwin":
        command = ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
        parser = parse_avfoundation_devices
    elif platform == "win32":
        command = ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
        parser = parse_dshow_devices
    elif platform.startswith("linux"):
        stable = sorted(Path("/dev/v4l/by-id").glob("*-video-index0"))
        paths = stable or sorted(Path("/dev").glob("video[0-9]*"))
        return [{"id": f"v4l2:{path}", "name": path.name, "locator": str(path)} for path in paths] + [DEMO_DEVICE.copy()]
    else:
        return [DEMO_DEVICE.copy()]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [DEMO_DEVICE.copy()]
    return parser(result.stderr) + [DEMO_DEVICE.copy()]


def demo_frame() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (960, 540), "#f4f7f5")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((35, 34, 925, 505), radius=20, fill="white", outline="#c8d8d0", width=3)
    draw.text((70, 78), "SYNTHETIC DEMO INPUT", font=ImageFont.load_default(size=28), fill="#45705a")
    draw.text((70, 175), "SYSTEM ERROR", font=ImageFont.load_default(size=72), fill="#171d1a")
    draw.text((70, 325), "Add an alert rule to test notifications.", font=ImageFont.load_default(size=32), fill="#40534a")
    stream = io.BytesIO()
    image.save(stream, "JPEG", quality=88)
    return stream.getvalue()


def is_dark_frame(image: bytes) -> bool:
    """Treat an almost entirely black capture as a missing video signal."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(image)) as source:
            sample = source.convert("L").resize((64, 64))
            return sum(pixel < 10 for pixel in sample.get_flattened_data()) > 0.95 * 64 * 64
    except (OSError, ValueError):
        return False


def capture_frame(device: dict[str, str], system: str | None = None) -> bytes:
    if device["id"] == DEMO_DEVICE["id"]:
        return demo_frame()
    platform = system or sys.platform
    rates: list[str | None] = [_AVF_RATE_CACHE.get(device["id"])] if platform == "darwin" and device["id"] in _AVF_RATE_CACHE else [None]
    attempted: set[str | None] = set()
    while rates:
        rate = rates.pop(0)
        if rate in attempted:
            continue
        attempted.add(rate)
        start = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if platform == "darwin":
            source = ["-f", "avfoundation"] + (["-framerate", rate] if rate else []) + ["-i", f"{device['locator']}:none"]
        elif platform == "win32":
            source = ["-f", "dshow", "-i", f"video={device['locator']}"]
        elif platform.startswith("linux"):
            source = ["-f", "v4l2", "-i", device["locator"]]
        else:
            raise RuntimeError("This operating system has no video capture adapter.")
        command = start + source + ["-frames:v", "1", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=12, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("FFmpeg is not installed on the host computer.") from exc
        except subprocess.TimeoutExpired as exc:
            hint = " Check macOS Camera permission for the app running this server." if platform == "darwin" else " Check that the device is available."
            raise RuntimeError("Video capture timed out." + hint) from exc
        if not result.returncode and result.stdout.startswith(b"\xff\xd8"):
            if platform == "darwin" and rate:
                _AVF_RATE_CACHE[device["id"]] = rate
            return result.stdout
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if platform == "darwin" and "Selected framerate" in detail:
            supported = set(re.findall(r"\[([\d.]+)\s+([\d.]+)\]fps", detail))
            choices = {candidate for pair in supported for candidate in pair}
            rates.extend(sorted(choices, key=lambda value: abs(float(value) - 30)))
            continue
        raise RuntimeError(f"Could not capture a frame: {detail[:160] or 'no image returned'}")
    raise RuntimeError("Could not find a supported video framerate for this device.")


class HostCaptureManager:
    def __init__(self, state: object, config_path: Path, discover: Callable = discover_devices, capture: Callable = capture_frame):
        self.state = state
        self.config_path = config_path
        self.discover = discover
        self.capture = capture
        self.lock = threading.RLock()
        self.sources: dict[str, dict] = {}
        self.runtime: dict[str, dict] = {}
        self.workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.latest_frames: dict[str, tuple[float, bytes]] = {}
        self._load()
        for identifier, entry in list(self.sources.items()):
            if entry.get("enabled", False):
                self.start(identifier)

    def _load(self) -> None:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                        self.sources[entry["id"]] = entry
        except (FileNotFoundError, ValueError, OSError):
            return

    def _save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.config_path.with_suffix(".tmp")
        temp.write_text(json.dumps(list(self.sources.values()), indent=2) + "\n", encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.config_path)

    def list_sources(self, devices: list[dict] | None = None) -> list[dict]:
        connected = {device["id"] for device in (self.discover() if devices is None else devices)}
        with self.lock:
            result = []
            for key, entry in self.sources.items():
                present = entry["device_id"] in connected
                runtime = self.runtime.get(key, {"status": "stopped", "error": ""})
                if not present:
                    runtime = {"status": "disconnected", "error": "Device unplugged or unavailable. Monitoring will resume when it reconnects."}
                result.append({**entry, **runtime, "connected": present})
            return result

    def add(self, device_id: str, name: str | None = None, interval: int = 5) -> dict:
        devices = {entry["id"]: entry for entry in self.discover()}
        if device_id not in devices:
            raise ValueError("Select a currently connected video device.")
        if type(interval) is not int or not INTERVAL_MIN <= interval <= INTERVAL_MAX:
            raise ValueError("Scan interval must be between 2 and 30 seconds.")
        if name is not None and not isinstance(name, str):
            raise ValueError("Source name must be text.")
        label = (name or devices[device_id]["name"]).strip()
        if not 1 <= len(label) <= 60:
            raise ValueError("Source name must contain 1–60 characters.")
        with self.lock:
            if len(self.sources) >= MAX_SOURCES:
                raise ValueError(f"Only {MAX_SOURCES} host sources can be configured.")
            if any(source["device_id"] == device_id for source in self.sources.values()):
                raise ValueError("This device is already configured as a source.")
            identifier = uuid.uuid4().hex[:12]
            entry = {"id": identifier, "device_id": device_id, "name": label, "interval": interval, "rules": [], "conditions": [], "enabled": False}
            self.sources[identifier] = entry
            self._save()
        return entry.copy()

    def update_rules(self, identifier: str, rules: list[str]) -> dict:
        with self.lock:
            if identifier not in self.sources:
                raise KeyError(identifier)
            self.sources[identifier]["rules"] = rules
            self._save()
            if hasattr(self.state, "reset_rule_matches"):
                self.state.reset_rule_matches(identifier)
            return self.sources[identifier].copy()

    def rename(self, identifier: str, name: object) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("Source name must contain 1–60 characters.")
        with self.lock:
            if identifier not in self.sources:
                raise KeyError(identifier)
            self.sources[identifier]["name"] = name.strip()
            self._save()
            return self.sources[identifier].copy()

    def update_interval(self, identifier: str, interval: object) -> dict:
        if type(interval) is not int or not INTERVAL_MIN <= interval <= INTERVAL_MAX:
            raise ValueError("Scan interval must be between 2 and 30 seconds.")
        with self.lock:
            if identifier not in self.sources:
                raise KeyError(identifier)
            self.sources[identifier]["interval"] = interval
            self._save()
            return self.sources[identifier].copy()

    def update_conditions(self, identifier: str, conditions: list[dict]) -> dict:
        with self.lock:
            if identifier not in self.sources:
                raise KeyError(identifier)
            self.sources[identifier]["conditions"] = conditions
            self._save()
            if hasattr(self.state, "reset_condition_matches"):
                self.state.reset_condition_matches(identifier)
            return self.sources[identifier].copy()

    def start(self, identifier: str) -> None:
        with self.lock:
            if identifier not in self.sources:
                raise KeyError(identifier)
            previous = self.workers.get(identifier)
            if previous and previous[0].is_alive() and not previous[1].is_set():
                return
            self.sources[identifier]["enabled"] = True
            self._save()
            stop = threading.Event()
            worker = threading.Thread(target=self._run, args=(identifier, stop), daemon=True, name=f"capture-{identifier}")
            self.workers[identifier] = (worker, stop)
            self.runtime[identifier] = {"status": "starting", "error": ""}
            worker.start()

    def stop(self, identifier: str) -> None:
        with self.lock:
            if identifier not in self.sources:
                raise KeyError(identifier)
            self.sources[identifier]["enabled"] = False
            self._save()
            worker = self.workers.get(identifier)
            if worker:
                worker[1].set()
            self.runtime[identifier] = {"status": "stopped", "error": ""}
            self.latest_frames.pop(identifier, None)

    def remove(self, identifier: str) -> None:
        self.stop(identifier)
        with self.lock:
            self.sources.pop(identifier, None)
            self.runtime.pop(identifier, None)
            self._save()

    def frame(self, identifier: str) -> bytes | None:
        with self.lock:
            latest = self.latest_frames.get(identifier)
            return latest[1] if latest and time.time() - latest[0] < 30 else None

    def _run(self, identifier: str, stop: threading.Event) -> None:
        while not stop.is_set():
            with self.lock:
                entry = self.sources.get(identifier)
                if not entry:
                    break
                entry = entry.copy()
            try:
                devices = {device["id"]: device for device in self.discover()}
                device = devices.get(entry["device_id"])
                if not device:
                    raise RuntimeError("Device disconnected; waiting for it to return.")
                image = self.capture(device)
                no_signal = is_dark_frame(image)
                with self.lock:
                    if stop.is_set():
                        break
                    self.latest_frames[identifier] = (time.time(), image)
                    self.runtime[identifier] = {"status": "no_signal" if no_signal else "running", "error": ""}
                if not no_signal:
                    self.state.scan(image, source_id=identifier, source_name=entry["name"], rules=entry.get("rules", []), conditions=entry.get("conditions", []))
            except Exception as exc:
                with self.lock:
                    if not stop.is_set():
                        self.runtime[identifier] = {"status": "waiting", "error": str(exc)[:160]}
            stop.wait(entry["interval"])
        with self.lock:
            if identifier in self.sources and self.workers.get(identifier, (None, None))[1] is stop:
                self.runtime[identifier] = {"status": "stopped", "error": ""}

    def shutdown(self) -> None:
        with self.lock:
            workers = list(self.workers.values())
        for _, stop in workers:
            stop.set()
        for worker, _ in workers:
            worker.join(timeout=1)
