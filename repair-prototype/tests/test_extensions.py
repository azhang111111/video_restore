from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Cancelled, Engine, HIDDEN, Options, RepairError, find_tool, inspect_container
from extensions import BATCH_SIZE, extension_tool
from hardware import detect_gpus, select_gpu


class HardwareTests(unittest.TestCase):
    def test_auto_prefers_discrete_regardless_of_brand_name(self):
        devices = [{"index": 0, "name": "Integrated GPU", "device_type": 1, "device_local_bytes": 16 << 30},
                   {"index": 1, "name": "RTX 3080 Ti", "device_type": 2, "device_local_bytes": 12 << 30}]
        self.assertEqual(select_gpu(devices)["index"], 1)
        devices[1]["name"] = "Any vendor, any model"
        self.assertEqual(select_gpu(devices)["index"], 1)
        self.assertEqual(select_gpu(devices, "0")["index"], 0)

    def test_select_and_reject_missing_devices(self):
        with self.assertRaises(ValueError):
            select_gpu([])
        with self.assertRaises(ValueError):
            select_gpu([{"index": 0, "device_type": 1}], "9")

    def test_memory_breaks_same_type_tie(self):
        devices = [{"index": 0, "device_type": 2, "device_local_bytes": 8 << 30},
                   {"index": 1, "device_type": 2, "device_local_bytes": 24 << 30}]
        self.assertEqual(select_gpu(devices)["index"], 1)


class ExtensionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # These are real-engine tests. Missing engines are failures, not silent skips.
        extension_tool("ai")
        extension_tool("index")
        cls.temp = tempfile.TemporaryDirectory(prefix="video-extensions-tests-")
        cls.root = Path(cls.temp.name)
        cls.reference = cls.root / "健康参考's.mp4"
        subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=96x64:rate=12:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
                        "-c:v", "libx264", "-preset", "ultrafast", "-g", "12", "-c:a", "aac",
                        "-movflags", "+faststart", str(cls.reference)], check=True, creationflags=HIDDEN)
        original = cls.reference.read_bytes()
        data = bytearray(original)
        atom = next(a for a in inspect_container(cls.reference)["atoms"] if a["type"] == "moov")
        data[atom["offset"] + 4:atom["offset"] + 8] = b"free"
        data[atom["offset"] + 8:atom["offset"] + atom["size"]] = b"\x00" * (atom["size"] - 8)
        cls.broken = cls.root / "损坏副本.mp4"
        cls.broken.write_bytes(data)
        cls.small = cls.root / "AI小样本.mp4"
        subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=32x24:rate=4:duration=0.5", "-f", "lavfi", "-i", "sine=duration=0.5",
                        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(cls.small)],
                       check=True, creationflags=HIDDEN)
        cls.hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (cls.small, cls.reference, cls.broken)}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.dest = Path(tempfile.mkdtemp(prefix="输出目录-", dir=self.root))

    def tearDown(self):
        for path, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertFalse(list(self.dest.glob("video-repair-*")))

    def test_real_ai_general_two_times_with_audio_and_auto_gpu(self):
        report = Engine().process(self.small, self.dest, Options(mode="enhance", ai=True, denoise="关闭",
                                                               sharpen=False, ai_tile=32))
        self.assertEqual(report["method"], "ai_super_resolution")
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["ai"]["processed_frames"], 2)
        self.assertEqual(report["ai"]["gpu"]["name"], select_gpu(detect_gpus())["name"])
        video = next(s for s in report["output_media"]["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (64, 48))
        self.assertEqual(report["input_tracks"], report["output_tracks"])
        self.assertTrue(report["validation"]["valid"])
        self.assertEqual(report["ai"]["native_scale"], 4)
        decoded = subprocess.run([find_tool("ffmpeg"), "-v", "error", "-i", report["output"],
                                  "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                 capture_output=True, check=True, creationflags=HIDDEN).stdout
        self.assertGreater(max(decoded) - min(decoded), 20, "AI must produce actual non-black image detail")

    def test_real_ai_anime_four_times_and_manual_gpu(self):
        device = select_gpu(detect_gpus())
        options = Options(mode="enhance", ai=True, ai_model="anime", ai_scale=4,
                          ai_gpu=str(device["index"]), ai_tile=32, denoise="关闭", sharpen=False)
        report = Engine().process(self.small, self.dest, options, preview=True)
        self.assertEqual(report["ai"]["gpu_selection"], str(device["index"]))
        video = next(s for s in report["output_media"]["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (128, 96))
        self.assertTrue(report["validation"]["valid"])

    def test_real_index_rebuild_unknown_duration(self):
        report = Engine().process(self.broken, self.dest, Options(reference=str(self.reference)))
        self.assertTrue(report["method"].startswith("untrunc+"))
        self.assertTrue(report["validation"]["valid"])
        self.assertTrue(report["index_rebuild"]["reference_validation"]["valid"])
        self.assertIsNone(report["original_duration_seconds"])
        self.assertNotIn("duration_ratio", report)
        self.assertEqual(report["validation"]["decoded_frames"], 24)

    def test_batched_ai_fractional_frame_rate_and_auto_tile(self):
        source = self.dest / "fractional.mp4"
        subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=32x24:rate=7/2:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
                        "-c:v", "libx264", "-c:a", "aac", str(source)], check=True, creationflags=HIDDEN)
        report = Engine().process(source, self.dest, Options(mode="enhance", ai=True, ai_model="anime",
                                 ai_tile=0, ai_scale=2, denoise="关闭", sharpen=False))
        self.assertGreater(report["ai"]["processed_frames"], BATCH_SIZE)
        self.assertEqual(report["ai"]["processed_frames"], 7)
        self.assertEqual(report["ai"]["output_fps"], "7/2")
        self.assertEqual(report["ai"]["tile_size"], 0)
        self.assertAlmostEqual(report["recovered_duration_seconds"], 2, delta=.1)
        self.assertTrue(report["validation"]["valid"])

    def test_ai_normalizes_phone_rotation(self):
        source = self.dest / "rotated.mp4"
        subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-v", "error", "-display_rotation:v:0", "90", "-i", str(self.small),
                        "-c", "copy", str(source)],
                       check=True, creationflags=HIDDEN)
        report = Engine().process(source, self.dest, Options(mode="enhance", ai=True, ai_model="anime",
                                 ai_scale=2, ai_tile=32, denoise="关闭", sharpen=False))
        video = next(s for s in report["output_media"]["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (48, 64))
        self.assertEqual(abs(report["ai"]["rotation_normalized"]), 90)
        self.assertTrue(report["validation"]["valid"])

    def test_shorter_audio_does_not_cut_ai_video_frames(self):
        source = self.dest / "short-audio.mp4"
        subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=32x24:rate=4:duration=1", "-f", "lavfi", "-i", "sine=duration=0.2",
                        "-c:v", "libx264", "-c:a", "aac", str(source)], check=True, creationflags=HIDDEN)
        report = Engine().process(source, self.dest, Options(mode="enhance", ai=True, ai_model="anime",
                                 ai_tile=32, denoise="关闭", sharpen=False))
        self.assertEqual(report["validation"]["decoded_frames"], 4)
        self.assertEqual(report["ai"]["processed_frames"], 4)
        self.assertAlmostEqual(report["recovered_duration_seconds"], 1, delta=.1)

    def test_forced_index_requires_reference(self):
        with self.assertRaisesRegex(RepairError, "参考"):
            Engine().process(self.broken, self.dest, Options(strategy="index"))

    def test_reference_cannot_be_broken_file_itself(self):
        with self.assertRaisesRegex(RepairError, "不能"):
            Engine().process(self.broken, self.dest, Options(reference=str(self.broken)))

    def test_invalid_reference_is_rejected(self):
        with self.assertRaisesRegex(RepairError, "参考视频"):
            Engine().process(self.broken, self.dest, Options(reference=str(self.broken.parent / "missing.mp4")))

    def test_cancel_real_ai_stage(self):
        cancel = threading.Event()
        def emit(kind, value):
            if kind == "status" and "AI 超分辨率：" in str(value):
                cancel.set()
        with self.assertRaises(Cancelled):
            Engine(cancel, emit).process(self.small, self.dest, Options(mode="enhance", ai=True, ai_tile=32))
        self.assertFalse(list(self.dest.glob("*.mp4")))
        report = json.loads(next(self.dest.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "cancelled")

    def test_ai_does_not_silently_fallback_when_no_gpu(self):
        with patch("extensions.detect_gpus", return_value=[]):
            with self.assertRaisesRegex(RepairError, "Vulkan"):
                Engine().process(self.small, self.dest, Options(mode="enhance", ai=True))
        self.assertFalse(list(self.dest.glob("*.mp4")))

    def test_ai_rejects_incomplete_model_outputs(self):
        real_command = Engine.command
        def command(engine, args, phase, *rest, **kwargs):
            if "AI 超分辨率：" in phase:
                return 0, "", ""
            return real_command(engine, args, phase, *rest, **kwargs)
        with patch.object(Engine, "command", command):
            with self.assertRaisesRegex(RepairError, "数量不完整"):
                Engine().process(self.small, self.dest, Options(mode="enhance", ai=True))
        self.assertFalse(list(self.dest.glob("*.mp4")))


if __name__ == "__main__":
    unittest.main()
