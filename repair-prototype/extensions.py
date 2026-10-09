"""Real engines: bounded-frame AI pipeline and reference-based Untrunc recovery."""
from contextlib import ExitStack
from dataclasses import replace
from fractions import Fraction
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import zlib

from engine import ROOT, HIDDEN, RepairError, duration, filters, video_streams
from hardware import detect_gpus, select_gpu

MODEL_NAMES = {"general": "realesrgan-x4plus", "anime": "realesr-animevideov3"}
BATCH_SIZE = 6


def extension_tool(kind: str) -> Path:
    name = "realesrgan-ncnn-vulkan" if kind == "ai" else "untrunc"
    folder = ROOT / ".tools" / ("realesrgan" if kind == "ai" else "untrunc")
    matches = list(folder.rglob(name + (".exe" if os.name == "nt" else ""))) if folder.exists() else []
    if not matches:
        raise RepairError("AI 超分辨率引擎未安装。请运行“安装扩展引擎.bat”。" if kind == "ai" else
                          "索引重建引擎未安装。请运行“安装扩展引擎.bat”。")
    return matches[0]


def provenance(kind: str) -> dict:
    folder = "realesrgan" if kind == "ai" else "untrunc"
    path = ROOT / ".tools" / folder / "install.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"note": "本地引擎没有安装来源记录"}


def write_png(path: Path, width: int, height: int, pixels: bytes):
    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
    compressor = zlib.compressobj(level=1)
    compressed = []
    stride = width * 3
    for row in range(height):
        compressed.append(compressor.compress(b"\0" + pixels[row * stride:(row + 1) * stride]))
    compressed.append(compressor.flush())
    with path.open("xb") as output:
        output.write(b"\x89PNG\r\n\x1a\n")
        output.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        output.write(chunk(b"IDAT", b"".join(compressed)))
        output.write(chunk(b"IEND", b""))


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as image:
        header = image.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise RepairError("AI 引擎输出了无效 PNG 图片。")
    return struct.unpack(">II", header[16:24])


class Pipeline:
    """Keep decoder/encoder cancellable even while pipe reads/writes are blocked."""
    def __init__(self, engine):
        self.engine = engine
        self.stack = ExitStack()
        self.items = []
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def __enter__(self):
        def monitor():
            while not self.stop.wait(.1):
                if self.engine.cancel.is_set():
                    self.terminate()
        self.monitor = threading.Thread(target=monitor, daemon=True)
        self.monitor.start()
        return self

    def start(self, args, phase, input_pipe=False, output_pipe=False):
        self.engine.check_cancel()
        errfile = self.stack.enter_context(tempfile.TemporaryFile())
        record = {"phase": phase, "command": args, "exit_code": None, "stderr": ""}
        self.engine.logs.append(record)
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if input_pipe else subprocess.DEVNULL,
                                stdout=subprocess.PIPE if output_pipe else subprocess.DEVNULL,
                                stderr=errfile, creationflags=HIDDEN)
        with self.lock:
            self.items.append((proc, errfile, record))
        return proc

    def terminate(self):
        with self.lock:
            for proc, _, _ in self.items:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass

    def finish(self, proc):
        while True:
            self.engine.check_cancel()
            try:
                return proc.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                continue

    def error(self, proc):
        for child, errfile, record in self.items:
            if child is proc:
                errfile.seek(0, 2)
                record["stderr_bytes"] = errfile.tell()
                errfile.seek(max(0, record["stderr_bytes"] - 65536))
                record["stderr"] = errfile.read().decode("utf-8", errors="replace")
                record["exit_code"] = proc.poll()
                return record["stderr"]
        return ""

    def __exit__(self, *_):
        self.stop.set()
        self.terminate()
        for proc, _, _ in self.items:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self.error(proc)
            for stream in (proc.stdin, proc.stdout):
                if stream and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
        self.monitor.join(timeout=1)
        self.stack.close()


def enhance_ai(engine, source: Path, dest: Path, info: dict, options, preview: bool, report: dict):
    executable = extension_tool("ai")
    model = MODEL_NAMES.get(options.ai_model)
    if model is None or options.ai_scale not in (2, 4):
        raise RepairError("无效的 AI 模型或倍率。")
    models = executable.parent / "models"
    native_scale = 4 if options.ai_model == "general" else options.ai_scale
    model_file = model + (f"-x{native_scale}" if options.ai_model == "anime" else "")
    if not all((models / (model_file + suffix)).is_file() for suffix in (".param", ".bin")):
        raise RepairError("AI 模型文件不完整，请重新运行“安装扩展引擎.bat”。")
    try:
        devices = detect_gpus()
        selected = select_gpu(devices, options.ai_gpu)
    except (OSError, ValueError) as exc:
        raise RepairError(f"无法使用 AI 超分辨率：{exc}") from exc
    if options.ai_tile != 0 and options.ai_tile < 32:
        raise RepairError("AI 分块尺寸必须是自动（0）或至少 32。")
    video = video_streams(info)[0]
    width, height = int(video["width"]), int(video["height"])
    rotation = next((float(s.get("rotation", 0)) for s in video.get("side_data_list", []) if "rotation" in s), 0)
    if rotation % 90:
        raise RepairError("当前 AI 管线支持 90 度整数倍旋转，请先规范化视频旋转角度。")
    rotated = int(rotation) % 180 == 90
    if rotated:
        width, height = height, width
    try:
        rate = Fraction(video.get("avg_frame_rate", "0/0"))
    except (ValueError, ZeroDivisionError):
        rate = Fraction(0)
    if rate <= 0:
        try:
            rate = Fraction(video.get("r_frame_rate", "0/0"))
        except (ValueError, ZeroDivisionError):
            rate = Fraction(0)
    if not 0 < rate <= 240:
        raise RepairError("无法确定有效视频帧率，已停止以避免音画不同步。")
    fps = f"{rate.numerator}/{rate.denominator}"
    # Bound disk usage to a handful of frames, even for multi-hour videos.
    required = width * height * 3 * (native_scale ** 2 + 1) * BATCH_SIZE + 32 * 1024 * 1024
    if shutil.disk_usage(dest.parent).free < required:
        raise RepairError(f"AI 临时帧至少需要约 {required / 1024 ** 3:.2f} GB 可用空间。")
    seconds = duration(info)
    if preview:
        seconds = min(seconds or 5, 5)
    total = max(1, round(seconds * float(rate))) if seconds else None
    details = {"engine": "Real-ESRGAN NCNN Vulkan", "model": model, "native_scale": native_scale,
               "output_scale": options.ai_scale, "gpu_selection": options.ai_gpu, "gpu": selected,
               "available_gpus": devices, "tile_size": options.ai_tile, "batch_size": BATCH_SIZE,
               "output_fps": fps, "timing": "按源时间戳采样到平均帧率，输出恒定帧率",
               "rotation_normalized": rotation,
               "engine_install": provenance("ai"), "processed_frames": 0}
    report["ai"] = details
    engine.emit("log", f"AI 使用 GPU {selected['index']}：{selected['name']}；分块：{'引擎自动' if not options.ai_tile else options.ai_tile}。")
    # Preserve source dimensions before inference; scale/sharpen only after the AI model.
    pre = filters(replace(options, scale="原尺寸", sharpen=False)).split(",")
    pre = [f for f in pre if not f.startswith("pad=")]
    pre += ["setpts=PTS-STARTPTS", "fps=" + fps]
    decoder_args = [engine.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                    "-i", str(source), "-map", "0:V:0", "-vf", ",".join(pre)]
    if preview:
        decoder_args += ["-t", "5"]
    decoder_args += ["-an", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
    final_width, final_height = width * options.ai_scale, height * options.ai_scale
    post = [f"scale={final_width}:{final_height}:flags=lanczos", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    sar = video.get("sample_aspect_ratio", "1:1").replace(":", "/")
    if rotated and sar not in {"N/A", "0/1"}:
        sar = str(1 / Fraction(sar))
    if sar not in {"N/A", "0/1"}:
        post += ["setsar=" + sar]
    if options.sharpen:
        post += ["unsharp=5:5:0.35:5:5:0"]
    video_only = dest.parent / "ai-video.mp4"
    mux_args = [engine.ffmpeg, "-hide_banner", "-nostdin", "-n", "-v", "error", "-f", "image2pipe",
                "-framerate", fps, "-c:v", "png", "-protocol_whitelist", "file,pipe", "-i", "pipe:0",
                "-map", "0:v:0", "-an", "-vf", ",".join(post),
                "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(video_only)]
    frames = queue.Queue(maxsize=2)
    finished = threading.Event()
    reader_errors = []
    with tempfile.TemporaryDirectory(prefix="ai-frames-", dir=dest.parent) as frame_dir, Pipeline(engine) as pipeline:
        decoder = pipeline.start(decoder_args, "AI 视频帧解码", output_pipe=True)
        encoder = pipeline.start(mux_args, "AI 视频帧合成", input_pipe=True)

        def enqueue(value):
            while not finished.is_set() and not engine.cancel.is_set():
                try:
                    frames.put(value, timeout=.1)
                    return
                except queue.Full:
                    pass

        def read_frames():
            try:
                frame_bytes = width * height * 3
                while not finished.is_set() and not engine.cancel.is_set():
                    data = decoder.stdout.read(frame_bytes)
                    if not data:
                        break
                    if len(data) != frame_bytes:
                        raise RepairError("视频帧读取不完整，已停止 AI 处理。")
                    enqueue(data)
            except Exception as exc:
                reader_errors.append(exc)
            finally:
                enqueue(None)

        reader = threading.Thread(target=read_frames, daemon=True)
        reader.start()
        processed, done = 0, False
        try:
            while not done:
                batch = []
                while len(batch) < BATCH_SIZE and not done:
                    engine.check_cancel()
                    try:
                        pixels = frames.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if pixels is None:
                        done = True
                    else:
                        batch.append(pixels)
                if not batch:
                    break
                input_dir, output_dir = Path(frame_dir) / "input", Path(frame_dir) / "output"
                input_dir.mkdir(exist_ok=True)
                output_dir.mkdir(exist_ok=True)
                for index, pixels in enumerate(batch):
                    engine.check_cancel()
                    write_png(input_dir / f"{index:08d}.png", width, height, pixels)
                batch_count = len(batch)
                batch.clear()
                args = [str(executable), "-i", str(input_dir), "-o", str(output_dir), "-m", str(models),
                        "-n", model, "-s", str(native_scale), "-g", str(selected["index"]),
                        "-t", str(options.ai_tile), "-j", "1:1:1", "-f", "png"]
                code, _, errors = engine.command(args, f"AI 超分辨率：第 {processed + 1}–{processed + batch_count} 帧")
                if code:
                    raise RepairError("AI 推理失败。请检查当前显卡 Vulkan 驱动，或把分块调小后重试。\n" + errors[-1600:])
                output_files = sorted(output_dir.glob("*.png"))
                if len(output_files) != batch_count:
                    raise RepairError("AI 输出图片数量不完整，未继续合成视频。")
                for index in range(batch_count):
                    engine.check_cancel()
                    image = output_dir / f"{index:08d}.png"
                    if not image.exists():
                        raise RepairError("AI 输出图片顺序或命名异常。")
                    if png_dimensions(image) != (width * native_scale, height * native_scale):
                        raise RepairError("AI 输出尺寸不符合所选模型，未继续导出。")
                    with image.open("rb") as input_file:
                        while block := input_file.read(1024 * 1024):
                            engine.check_cancel()
                            try:
                                encoder.stdin.write(block)
                            except OSError as exc:
                                engine.check_cancel()
                                raise RepairError("AI 视频合成失败。\n" + pipeline.error(encoder)[-1600:]) from exc
                    image.unlink()
                    (input_dir / f"{index:08d}.png").unlink()
                processed += batch_count
                details["processed_frames"] = processed
                if total:
                    engine.emit("progress", min(99, processed / total * 100))
            if reader_errors:
                raise reader_errors[0]
            if not processed:
                raise RepairError("未解码出可用视频帧，AI 处理已停止。")
            code = pipeline.finish(decoder)
            errors = pipeline.error(decoder)
            if code or errors.strip():
                raise RepairError("源视频解码出现错误，请先进行文件修复。\n" + errors[-1600:])
            encoder.stdin.close()
            code = pipeline.finish(encoder)
            errors = pipeline.error(encoder)
            if code or errors.strip():
                raise RepairError("AI 视频合成未通过检查。\n" + errors[-1600:])
        finally:
            finished.set()
            pipeline.terminate()
            reader.join(timeout=3)
    # Add audio after the exact video length is known. A shorter audio track must
    # not silently truncate correctly processed video frames via -shortest.
    length = details["processed_frames"] / float(rate)
    audio_input = ["-protocol_whitelist", "file,pipe"]
    try:
        offset = max(0.0, float(video.get("start_time", 0)) - float(info.get("format", {}).get("start_time", 0)))
    except (TypeError, ValueError):
        offset = 0.0
    if offset:
        audio_input += ["-ss", str(offset)]
    details["audio_trim_offset_seconds"] = offset
    args = [engine.ffmpeg, "-hide_banner", "-nostdin", "-n", "-v", "error", "-progress", "pipe:1",
            "-protocol_whitelist", "file,pipe", "-i", str(video_only), *audio_input, "-i", str(source),
            "-map", "0:v:0", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{length:.9f}", "-movflags", "+faststart", str(dest)]
    code, _, errors = engine.command(args, "合并 AI 视频和原片音轨", length)
    if code or errors.strip():
        raise RepairError("AI 音轨合成失败，未继续导出。\n" + errors[-1600:])
    video_only.unlink()
    return details


def rebuild_index(engine, copied: Path, source: Path, work: Path, options, report: dict) -> tuple[Path, dict]:
    if not options.reference:
        raise RepairError("缺失可读取的 moov 索引。请在“文件修复”中选择同一设备、相同录制设置的健康参考视频后重试。")
    reference = Path(options.reference).resolve()
    if not reference.is_file() or reference == source:
        raise RepairError("请选择独立、存在的健康参考视频；参考视频不能是损坏文件本身。")
    executable = extension_tool("index")
    details = {"engine": "Untrunc", "reference": str(reference), "engine_install": provenance("index"),
               "skip_unknown": options.index_skip_unknown}
    report["index_rebuild"] = details
    copied_reference = work / ("reference" + reference.suffix)
    details["reference_copied_bytes"] = engine.copy_input(reference, copied_reference)
    reference_info, errors = engine.probe(copied_reference)
    details["reference_media"] = reference_info
    if errors or not video_streams(reference_info):
        raise RepairError("参考视频无法正常读取，请更换健康 MP4/MOV 文件。\n" + errors[-1000:])
    if "mov" not in reference_info.get("format", {}).get("format_name", "").split(","):
        raise RepairError("索引重建的健康参考视频必须是 MP4/MOV 容器。")
    reference_validation = engine.validate(copied_reference, reference_info)
    details["reference_validation"] = reference_validation
    if not reference_validation["valid"]:
        raise RepairError("参考视频未通过完整解码检查，请使用健康参考视频。")
    rebuilt = work / "reconstructed.mp4"
    args = [str(executable), "-n", "-dst", str(rebuilt)]
    if options.index_skip_unknown:
        args.append("-s")
    args += [str(copied_reference), str(copied)]
    code, _, errors = engine.command(args, "使用健康参考视频重建缺失索引")
    if code or not rebuilt.is_file() or not rebuilt.stat().st_size:
        raise RepairError("索引重建未生成有效结果。请检查参考视频是否来自同一设备及相同录制设置。\n" + (errors + engine.logs[-1].get("stdout", ""))[-1600:])
    info, probe_errors = engine.probe(rebuilt)
    details["reconstructed_media"] = info
    details["probe_errors"] = probe_errors
    if not video_streams(info):
        raise RepairError("重建结果仍没有可读取的视频轨道，未导出。")
    details["validation"] = engine.validate(rebuilt, info)
    details["note"] = "原片时长未知时不使用参考片或重建结果计算恢复比例；请核对恢复内容及音画同步。"
    return rebuilt, info
