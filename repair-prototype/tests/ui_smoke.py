"""Manual smoke check for the real Tk event loop and background jobs."""
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App
from test_engine import VideoTests
from test_extensions import ExtensionIntegrationTests


def main():
    VideoTests.setUpClass()
    ExtensionIntegrationTests.setUpClass()
    root = tk.Tk()
    app = App(root)
    try:
        root.update()
        for mode in ("repair", "enhance", "diagnose"):
            app._switch_mode(mode)
            root.update()
            assert app.log.winfo_height() > 10, "log panel is clipped"
            assert app.start_button.winfo_rooty() + app.start_button.winfo_height() < root.winfo_rooty() + root.winfo_height()
        app.ai.set(True)
        app._switch_mode("enhance")
        root.update()
        assert app.ai_settings.winfo_height() > 1
        app.canvas.yview_moveto(1)
        root.update()
        assert app.start_button.winfo_rooty() + app.start_button.winfo_height() < root.winfo_rooty() + root.winfo_height()
        app.ai.set(False)
        root.withdraw()
        with tempfile.TemporaryDirectory() as work:
            app.input.set(str(VideoTests.source))
            app.output_dir.set(work)
            for mode, preview, extended in (("repair", False, False), ("enhance", True, False), ("diagnose", False, False),
                                           ("enhance", True, True), ("repair", False, True)):
                app.ai.set(mode == "enhance" and extended)
                app.ai_model.set("动画")
                app.ai_tile.set("32")
                app.input.set(str(ExtensionIntegrationTests.small if mode == "enhance" else ExtensionIntegrationTests.broken) if extended else str(VideoTests.source))
                app.reference.set(str(ExtensionIntegrationTests.reference) if extended and mode == "repair" else "")
                app.strategy.set("参考视频重建索引" if extended and mode == "repair" else "自动：先无损封装，再编码抢救")
                app._switch_mode(mode)
                with patch("app.messagebox.showerror", side_effect=AssertionError), patch("app.messagebox.showinfo", side_effect=AssertionError):
                    app.start(preview=preview)
                    deadline = time.monotonic() + 30
                    while app.busy and time.monotonic() < deadline:
                        root.update()
                        time.sleep(.02)
                    assert not app.busy, "UI worker timed out"
                    assert app.result, "UI did not receive worker result"
                    assert app.last_report and Path(app.last_report).is_file()
                    assert app.start_button.cget("state") == "normal"
                    if extended:
                        assert "ai" in app.result if mode == "enhance" else "index_rebuild" in app.result
                    print(mode, "extended" if extended else "basic", "preview" if preview else "full", app.result["status"], flush=True)
            app.open_report()
            root.update()
            assert any(isinstance(child, tk.Toplevel) for child in root.winfo_children())
        print("UI smoke passed", flush=True)
    finally:
        app.close()
        VideoTests.tearDownClass()
        ExtensionIntegrationTests.tearDownClass()


if __name__ == "__main__":
    main()
