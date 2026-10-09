import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import Cancelled, Engine, HIDDEN, Options, RepairError, filters, find_tool, inspect_container


class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="video-engine-tests-")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "测试 原片's.mp4"
        subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=4",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                        "-c:v", "libx264", "-preset", "ultrafast", "-g", "24",
                        "-c:a", "aac", "-movflags", "+faststart", "-shortest", str(cls.source)],
                       check=True, creationflags=HIDDEN, timeout=30)
        cls.original = cls.source.read_bytes()
        cls.digest = hashlib.sha256(cls.original).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.dest = Path(tempfile.mkdtemp(dir=self.root))

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.digest,
                         "source must remain unchanged")
        self.assertFalse(list(self.dest.glob("video-repair-*")), "working copies must be cleaned")

    def test_healthy_diagnosis(self):
        report = Engine().process(self.source, self.dest, Options(mode="diagnose"))
        self.assertEqual(report["status"], "healthy")
        self.assertTrue(report["diagnosis"]["decode"]["valid"])
        self.assertGreater(report["diagnosis"]["decode"]["decoded_frames"], 0)
        self.assertTrue(Path(report["report"]).exists())
        self.assertFalse(list(self.dest.glob("*.mp4")))

    def test_remux_and_unique_outputs(self):
        first = Engine().process(self.source, self.dest, Options())
        second = Engine().process(self.source, self.dest, Options())
        self.assertEqual(first["method"], "remux")
        self.assertEqual(first["status"], "verified")
        self.assertNotEqual(first["output"], second["output"])
        self.assertTrue(first["validation"]["valid"])
        self.assertEqual(first["input_tracks"], first["output_tracks"])
        self.assertAlmostEqual(first["recovered_duration_seconds"], 4, delta=.1)

    def test_enhance_two_times_with_all_filters(self):
        report = Engine().process(self.source, self.dest, Options(mode="enhance", scale="2 倍",
                                 denoise="强", sharpen=True, brighten=True, deinterlace=True))
        self.assertEqual(report["status"], "verified")
        video = next(s for s in report["output_media"]["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (640, 360))
        self.assertEqual(video["codec_name"], "h264")
        self.assertTrue(report["validation"]["valid"])

    def test_preview(self):
        report = Engine().process(self.source, self.dest, Options(mode="enhance"), preview=True)
        self.assertTrue(report["preview"])
        self.assertLessEqual(report["recovered_duration_seconds"], 5.2)
        self.assertTrue(report["validation"]["valid"])

    def test_missing_index(self):
        broken = self.dest / "missing-moov.mp4"
        data = bytearray(self.original)
        atoms = inspect_container(self.source)["atoms"]
        atom = next(a for a in atoms if a["type"] == "moov")
        data[atom["offset"] + 4:atom["offset"] + 8] = b"free"
        data[atom["offset"] + 8:atom["offset"] + atom["size"]] = b"\x00" * (atom["size"] - 8)
        broken.write_bytes(data)
        with self.assertRaisesRegex(RepairError, "moov"):
            Engine().process(broken, self.dest, Options())
        reports = list(self.dest.glob("*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["diagnosis"]["structure"]["has_moov"])
        self.assertEqual(broken.read_bytes(), data)

    def test_corrupted_index_header(self):
        broken = self.dest / "corrupt-index-header.mp4"
        data = bytearray(self.original)
        atom = next(a for a in inspect_container(self.source)["atoms"] if a["type"] == "moov")
        data[atom["offset"] + 4:atom["offset"] + 8] = b"free"
        broken.write_bytes(data)
        report = Engine().process(broken, self.dest, Options())
        self.assertTrue(report["validation"]["valid"])
        self.assertEqual(report["method"], "remux")
        self.assertFalse(report["diagnosis"]["structure"]["has_moov"])

    def test_truncated_tail(self):
        broken = self.dest / "truncated.mp4"
        broken.write_bytes(self.original[:int(len(self.original) * .70)])
        self.assertTrue(inspect_container(broken)["truncated"])
        report = Engine().process(broken, self.dest, Options())
        self.assertTrue(report["validation"]["valid"])
        self.assertEqual(report["status"], "partial")
        self.assertLess(report["recovered_duration_seconds"], 4)

    def test_local_media_corruption(self):
        result = subprocess.run([find_tool("ffprobe"), "-v", "error", "-select_streams", "v",
                                 "-show_packets", "-of", "json", str(self.source)],
                                capture_output=True, check=True, creationflags=HIDDEN)
        packets = json.loads(result.stdout)["packets"]
        packet = packets[40]
        data = bytearray(self.original)
        pos, size = int(packet["pos"]), int(packet["size"])
        data[pos:pos + size] = b"\xff" * size
        broken = self.dest / "corrupt-media.mp4"
        broken.write_bytes(data)
        diagnosis = Engine().diagnose(broken)
        self.assertFalse(diagnosis["decode"]["valid"])
        report = Engine().process(broken, self.dest, Options())
        self.assertEqual(report["method"], "transcode")
        self.assertFalse(report["remux_validation"]["valid"])
        self.assertTrue(report["validation"]["valid"])

    def test_empty_and_nonvideo(self):
        empty = self.dest / "empty.mp4"
        empty.touch()
        with self.assertRaisesRegex(RepairError, "为空"):
            Engine().process(empty, self.dest, Options())
        nonvideo = self.dest / "example.txt"
        nonvideo.write_text("not a video")
        with self.assertRaisesRegex(RepairError, "无法识别"):
            Engine().process(nonvideo, self.dest, Options())

    def test_copy_read_failure_records_offset(self):
        original_open = Path.open
        source = self.source

        class BrokenRead:
            def __enter__(self):
                self.reads = 0
                return self

            def __exit__(self, *_):
                pass

            def read(self, size):
                self.reads += 1
                if self.reads == 1:
                    return b"a" * 4096
                raise OSError("simulated disk read failure")

        def opened(path, *args, **kwargs):
            if path == source and args and args[0] == "rb":
                return BrokenRead()
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", opened):
            with self.assertRaisesRegex(RepairError, "偏移：4096"):
                Engine().process(source, self.dest, Options())
        report = json.loads(next(self.dest.glob("*.json")).read_text(encoding="utf-8"))
        self.assertIn("4096", report["error"])
        self.assertEqual(report["status"], "failed")
        self.assertFalse(list(self.dest.glob("*.mp4")))

    def test_cancel_active_process(self):
        cancel = threading.Event()

        def emit(kind, value):
            if kind == "status" and str(value).startswith("处理视频"):
                cancel.set()

        with self.assertRaises(Cancelled):
            Engine(cancel, emit).process(self.source, self.dest, Options(mode="enhance"))
        self.assertFalse(list(self.dest.glob("*.mp4")))
        report = json.loads(next(self.dest.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "cancelled")

    def test_unknown_original_duration_no_ratio(self):
        with patch("engine.duration", return_value=None):
            report = Engine().process(self.source, self.dest, Options())
        self.assertNotIn("duration_ratio", report)
        self.assertTrue(report["validation"]["valid"])

    def test_only_remux_rejects_invalid_output(self):
        with patch.object(Engine, "validate", return_value={"valid": False, "errors": "simulated decode error"}):
            with self.assertRaisesRegex(RepairError, "未通过完整解码检查"):
                Engine().process(self.source, self.dest, Options(strategy="remux"))
        self.assertFalse(list(self.dest.glob("*.mp4")))

    def test_1080p_filter_aspect_ratio(self):
        result = subprocess.run([find_tool("ffmpeg"), "-hide_banner", "-v", "error",
                                 "-f", "lavfi", "-i", "testsrc2=size=320x180:duration=0.1",
                                 "-vf", filters(Options(mode="enhance", scale="1080p（仅放大）")),
                                 "-frames:v", "1", "-f", "null", "-"],
                                capture_output=True, creationflags=HIDDEN, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
