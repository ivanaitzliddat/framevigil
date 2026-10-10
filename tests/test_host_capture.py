import tempfile
import threading
import unittest
import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from host_capture import DEMO_DEVICE, HostCaptureManager, capture_frame, is_dark_frame, parse_avfoundation_devices, parse_dshow_devices
from server import TesseractOCR


MAC_LIST = """[AVFoundation indev @ 0x123] AVFoundation video devices:
[AVFoundation indev @ 0x123] [0] FaceTime HD Camera
[AVFoundation indev @ 0x123] [1] USB HDMI Capture
[AVFoundation indev @ 0x123] AVFoundation audio devices:
[AVFoundation indev @ 0x123] [0] MacBook Microphone
"""

WINDOWS_LIST = """[dshow @ 0x123] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0x123]  "USB Video"
[dshow @ 0x123]     Alternative name "@device_pnp_\\\\?\\usb#vid_1234"
[dshow @ 0x123] DirectShow audio devices
[dshow @ 0x123]  "Microphone"
"""


class DeviceDiscoveryTests(unittest.TestCase):
    def test_demo_frame_can_be_read_by_real_ocr(self):
        image = capture_frame(DEMO_DEVICE)
        self.assertTrue(image.startswith(b"\xff\xd8"))
        self.assertIn("SYSTEM ERROR", TesseractOCR().extract(image))

    def test_missing_signal_skips_black_frames(self):
        stream = io.BytesIO()
        Image.new("RGB", (64, 64), "black").save(stream, "JPEG")
        self.assertTrue(is_dark_frame(stream.getvalue()))
        self.assertFalse(is_dark_frame(capture_frame(DEMO_DEVICE)))

    def test_mac_video_devices_exclude_audio(self):
        devices = parse_avfoundation_devices(MAC_LIST)
        self.assertEqual([device["name"] for device in devices], ["FaceTime HD Camera", "USB HDMI Capture"])
        self.assertEqual(devices[1]["locator"], "1")

    def test_windows_uses_stable_alternative_name(self):
        devices = parse_dshow_devices(WINDOWS_LIST)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "USB Video")
        self.assertTrue(devices[0]["id"].startswith("dshow:@device_pnp_"))

    def test_mac_capture_uses_supported_framerate(self):
        class Result:
            def __init__(self, code, out, err):
                self.returncode = code
                self.stdout = out
                self.stderr = err

        rejected = Result(1, b"", b"Selected framerate (29.970030) is not supported. Supported modes: 1280x720@[15.000000 30.000000]fps")
        captured = Result(0, b"\xff\xd8frame", b"")
        device = {"id": "avfoundation:Test Camera:0", "locator": "0", "name": "Test Camera"}
        with patch("host_capture.subprocess.run", side_effect=[rejected, captured]) as run:
            self.assertEqual(capture_frame(device, system="darwin"), b"\xff\xd8frame")
        self.assertIn("30.000000", run.call_args.args[0])


class FakeState:
    def __init__(self):
        self.scanned = threading.Event()
        self.values = []

    def scan(self, image, **kwargs):
        self.values.append((image, kwargs))
        self.scanned.set()


class CaptureManagerTests(unittest.TestCase):
    def test_disconnected_source_is_visible_even_when_monitoring_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            available = [DEMO_DEVICE]
            manager = HostCaptureManager(FakeState(), Path(directory) / "sources.json", discover=lambda: available)
            source = manager.add(DEMO_DEVICE["id"])
            self.assertTrue(manager.list_sources()[0]["connected"])
            available.clear()
            disconnected = manager.list_sources()[0]
            self.assertFalse(disconnected["connected"])
            self.assertEqual(disconnected["status"], "disconnected")
            self.assertIn("unplugged", disconnected["error"])
            manager.shutdown()

    def test_active_source_resumes_after_server_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            capture = lambda _: b"\xff\xd8frame"
            manager = HostCaptureManager(FakeState(), path, discover=lambda: [DEMO_DEVICE], capture=capture)
            source = manager.add(DEMO_DEVICE["id"], interval=2)
            manager.start(source["id"])
            manager.shutdown()

            state = FakeState()
            restarted = HostCaptureManager(state, path, discover=lambda: [DEMO_DEVICE], capture=capture)
            self.assertTrue(state.scanned.wait(2))
            self.assertEqual(restarted.list_sources()[0]["status"], "running")
            restarted.stop(source["id"])
            restarted.shutdown()
            self.assertFalse(HostCaptureManager(FakeState(), path, discover=lambda: [DEMO_DEVICE], capture=capture).list_sources()[0]["enabled"])

    def test_source_captures_in_memory_and_persists_only_config(self):
        with tempfile.TemporaryDirectory() as directory:
            state = FakeState()
            path = Path(directory) / "sources.json"
            device = {"id": "avfoundation:USB HDMI Capture:0", "name": "USB HDMI Capture", "locator": "1"}
            manager = HostCaptureManager(state, path, discover=lambda: [device], capture=lambda _: b"\xff\xd8demo")
            source = manager.add(device["id"], "Training display", 2)
            with self.assertRaises(ValueError):
                manager.add(device["id"])
            manager.update_rules(source["id"], ["warning"])
            manager.rename(source["id"], "Renamed display")
            manager.update_interval(source["id"], 3)
            manager.start(source["id"])
            self.assertTrue(state.scanned.wait(2))
            self.assertEqual(manager.frame(source["id"]), b"\xff\xd8demo")
            self.assertEqual(state.values[0][1]["source_name"], "Renamed display")
            manager.stop(source["id"])
            self.assertIsNone(manager.frame(source["id"]))
            manager.shutdown()
            self.assertNotIn("demo", path.read_text())
            loaded = HostCaptureManager(FakeState(), path, discover=lambda: [device])
            self.assertEqual(loaded.list_sources()[0]["rules"], ["warning"])
            self.assertEqual(loaded.list_sources()[0]["interval"], 3)
            loaded.shutdown()


if __name__ == "__main__":
    unittest.main()
