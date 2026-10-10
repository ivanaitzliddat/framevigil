import io
import time
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from server import EVIDENCE_TTL_SECONDS, MonitorState, TesseractOCR, matches_background, validate_conditions


def image_with_text(text, background="white"):
    image = Image.new("RGB", (1000, 200), background)
    font = ImageFont.load_default(size=72)
    ImageDraw.Draw(image).text((35, 55), text, font=font, fill="black")
    stream = io.BytesIO()
    image.save(stream, "JPEG")
    return stream.getvalue()


class FakeLinesOCR:
    available = True

    def extract_lines(self, image):
        return [{"text": "SYSTEM ERROR detected", "box": [35, 55, 750, 125]}]


class ConditionTests(unittest.TestCase):
    def test_all_keywords_color_and_custom_message(self):
        image = image_with_text("SYSTEM ERROR")
        conditions = validate_conditions([{"name": "Critical warning", "keywords": ["SYSTEM", "error"], "color": "#ffffff", "message": "Check the display"}])
        state = MonitorState(FakeLinesOCR())
        result = state.scan(image, now=100, source_id="server1", source_name="USB capture", rules=[], conditions=conditions)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["message"], "Check the display")
        self.assertEqual(result["events"][0]["source"], "USB capture")
        self.assertEqual(result["events"][0]["line"], "SYSTEM ERROR detected")
        self.assertEqual(result["events"][0]["boxes"], [[35, 55, 750, 125]])

    def test_color_only_condition_and_rearm_after_text_disappears(self):
        class ChangingOCR:
            available = True
            lines = [{"text": "SYSTEM ERROR detected", "box": [35, 55, 750, 125]}]

            def extract_lines(self, image):
                return self.lines

        ocr = ChangingOCR()
        state = MonitorState(ocr)
        image = image_with_text("SYSTEM ERROR")
        conditions = validate_conditions([{"name": "Light background", "keywords": [], "color": "#ffffff"}])
        self.assertEqual(len(state.scan(image, now=100, rules=[], conditions=conditions)["events"]), 1)
        self.assertEqual(state.scan(image, now=105, rules=[], conditions=conditions)["events"], [])
        ocr.lines = []
        state.scan(image, now=110, rules=[], conditions=conditions)
        ocr.lines = [{"text": "SYSTEM ERROR detected", "box": [35, 55, 750, 125]}]
        self.assertEqual(len(state.scan(image, now=115, rules=[], conditions=conditions)["events"]), 1)

    def test_same_detected_text_marks_every_location(self):
        class RepeatedOCR:
            available = True

            def extract_lines(self, image):
                return [
                    {"text": "ERROR", "box": [30, 30, 130, 80], "words": [{"text": "ERROR", "box": [30, 30, 130, 80]}]},
                    {"text": "ERROR", "box": [400, 30, 500, 80], "words": [{"text": "ERROR", "box": [400, 30, 500, 80]}]},
                ]

        state = MonitorState(RepeatedOCR())
        state.set_rules(["ERROR"])
        result = state.scan(image_with_text("ERROR"), now=100)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["boxes"], [[30, 30, 130, 80], [400, 30, 500, 80]])

    def test_wrong_background_does_not_alert(self):
        image = image_with_text("SYSTEM ERROR")
        conditions = validate_conditions([{"name": "Red screen", "keywords": ["SYSTEM", "ERROR"], "color": "#ff0000"}])
        state = MonitorState(FakeLinesOCR())
        result = state.scan(image, now=100, rules=[], conditions=conditions)
        self.assertEqual(result["events"], [])
        self.assertTrue(matches_background(image, [35, 55, 750, 125], "#ffffff"))

    def test_rejects_bad_color(self):
        with self.assertRaises(ValueError):
            validate_conditions([{"name": "Warning", "keywords": ["ERROR"], "color": "red"}])

    @unittest.skipUnless(TesseractOCR().available, "Tesseract not installed")
    def test_real_tesseract_returns_bounded_lines(self):
        lines = TesseractOCR().extract_lines(image_with_text("SYSTEM ERROR"))
        self.assertTrue(any("SYSTEM ERROR" in line["text"].upper() for line in lines))
        self.assertTrue(all(len(line["box"]) == 4 for line in lines))
        self.assertTrue(all(line["words"] for line in lines))

    @unittest.skipUnless(TesseractOCR().available, "Tesseract not installed")
    def test_alert_retains_frame_and_precise_word_rectangle(self):
        image = image_with_text("SYSTEM ERROR")
        state = MonitorState(TesseractOCR())
        state.set_rules(["ERROR"])
        event = state.scan(image)["events"][0]
        line = TesseractOCR().extract_lines(image)[0]
        self.assertEqual(state.evidence_for(event["id"]), image)
        self.assertEqual(len(event["boxes"]), 1)
        self.assertLess(event["boxes"][0][2] - event["boxes"][0][0], line["box"][2] - line["box"][0])
        with patch("server.time.time", return_value=time.time() + EVIDENCE_TTL_SECONDS + 1):
            self.assertIsNone(state.evidence_for(event["id"]))


if __name__ == "__main__":
    unittest.main()
