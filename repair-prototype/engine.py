"""Local video processing. Originals are read only; every export is verified."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from typing import Callable
import uuid

ROOT = Path(__file__).resolve().parent
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class Cancelled(Exception):
    pass


class RepairError(Exception):
    pass


@dataclass(frozen=True)
class Options:
    mode: str = "repair"
    strategy: str = "auto"
    denoise: str = "中"
    sharpen: bool = True
    brighten: bool = False
    deinterlace: bool = False
    scale: str = "原尺寸"
    ai: bool = False
    ai_model: str = "general"
    ai_scale: int = 2
    ai_gpu: str = "auto"
    ai_tile: int = 0
    reference: str = ""
    index_skip_unknown: bool = True


def find_tool(name: str) -> str:
    local = ROOT / ".tools" / (name + (".exe" if os.name == "nt" else ""))
    if local.is_file():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise RepairError("找不到 " + name + "。请运行“安装视频引擎.bat”。")


def duration(info: dict) -> float | None:
    values = [info.get("format", {}).get("duration")]
    values += [s.get("duration") for s in info.get("streams", [])]
    for value in values:
        try:
            number = float(value)
            if number > 0:
                return number
        except (ValueError, TypeError):
            pass
    return None


def video_streams(info: dict) -> list[dict]:
    return [s for s in info.get("streams", [])
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]


def describe(info: dict) -> str:
    videos = video_streams(info)
    if not videos:
        return "未检测到可识别的视频轨道"
    v = videos[0]
    seconds = duration(info)
    length = f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}" if seconds else "时长未知"
    audio = sum(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return f"{v.get('width', '?')} × {v.get('height', '?')}  ·  {v.get('codec_name', '?').upper()}  ·  {length}  ·  {audio} 条音轨"


def inspect_container(path: Path) -> dict:
    """Inspect top-level ISO BMFF headers, without scanning arbitrary media bytes."""
    if path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".3gp"}:
        return {"kind": "other", "note": "由媒体引擎识别文件格式"}
    size = path.stat().st_size
    atoms, offset, truncated = [], 0, False
    with path.open("rb") as source:
        for _ in range(10000):
            if offset >= size:
                break
            source.seek(offset)
            head = source.read(8)
            if len(head) < 8:
                truncated = True
                break
            length, tag = struct.unpack(">I4s", head)
            header = 8
            if length == 1:
                extra = source.read(8)
                if len(extra) != 8:
                    truncated = True
                    break
                length = struct.unpack(">Q", extra)[0]
                header = 16
            elif length == 0:
                length = size - offset
            tag_text = tag.decode("ascii", errors="replace")
            atoms.append({"type": tag_text, "offset": offset, "size": length})
            if length < header or offset + length > size:
                truncated = True
                break
            offset += length
        else:
            return {"kind": "mp4/mov", "atoms": atoms, "note": "结构过于复杂，交由媒体引擎诊断"}
    tags = {a["type"] for a in atoms}
    return {"kind": "mp4/mov", "atoms": atoms, "truncated": truncated,
            "has_moov": "moov" in tags, "has_mdat": "mdat" in tags,
            "note": "容器可能被截断" if truncated else "已读取顶层容器结构"}


def filters(options: Options) -> str:
    chain = []
    if options.deinterlace:
        chain.append("bwdif=mode=send_frame:parity=auto:deint=all")
    noise = {"轻": "1.5:1.5:3:3", "中": "3:3:6:6", "强": "5:5:9:9"}
    if options.denoise in noise:
        chain.append("hqdn3d=" + noise[options.denoise])
    if options.brighten:
        chain.append("eq=brightness=0.035:contrast=1.06:saturation=1.03")
    if options.scale == "2 倍":
        chain.append("scale=trunc(iw*2/2)*2:trunc(ih*2/2)*2:flags=lanczos")
    elif options.scale == "1080p（仅放大）":
        chain.append("scale=w='if(gte(iw,ih),max(iw,1920),max(iw,1080))':h='if(gte(iw,ih),max(ih,1080),max(ih,1920))':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos")
    else:
        chain.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
    if options.sharpen:
        chain.append("unsharp=5:5:0.55:5:5:0")
    return ",".join(chain)


class Engine:
    def __init__(self, cancel: threading.Event | None = None,
                 emit: Callable[[str, object], None] | None = None):
        self.cancel = cancel if cancel is not None else threading.Event()
        self.emit = emit or (lambda kind, value: None)
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")
        self.logs: list[dict] = []

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("任务已取消")

    def command(self, args: list[str], phase: str, seconds: float | None = None,
                timeout: float | None = None) -> tuple[int, str, str]:
        self.check_cancel()
        self.emit("status", phase)
        record = {"phase": phase, "command": args, "exit_code": None, "stderr": ""}
        self.logs.append(record)
        # Disk-backed stderr avoids deadlocks and bounds memory for damaged files.
        with tempfile.TemporaryFile() as errfile:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=errfile, creationflags=HIDDEN)
            lines: queue.Queue[str | None] = queue.Queue()

            def read_stdout():
                try:
                    for line in iter(proc.stdout.readline, b""):
                        lines.put(line.decode("utf-8", errors="replace"))
                finally:
                    proc.stdout.close()
                    lines.put(None)

            reader = threading.Thread(target=read_stdout, daemon=True)
            reader.start()
            started, output, frames = time.monotonic(), ([] if args[0] == self.ffprobe else deque(maxlen=512)), 0
            try:
                ended = False
                while not ended:
                    self.check_cancel()
                    if timeout and time.monotonic() - started > timeout:
                        raise RepairError(phase + "超时，请检查文件或磁盘是否正常。")
                    try:
                        line = lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        ended = True
                        continue
                    output.append(line)
                    key, _, value = line.strip().partition("=")
                    if key == "frame":
                        try:
                            frames = int(value)
                        except ValueError:
                            pass
                    if key == "out_time_us" and seconds:
                        try:
                            self.emit("progress", min(99.0, max(0.0, float(value) / 1e6 / seconds * 100)))
                        except ValueError:
                            pass
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                reader.join(timeout=3)
                record["exit_code"] = proc.returncode
                errfile.seek(0, 2)
                record["stderr_bytes"] = errfile.tell()
                errfile.seek(max(0, record["stderr_bytes"] - 65536))
                record["stderr"] = errfile.read().decode("utf-8", errors="replace")
                record["decoded_frames"] = frames
                record["stdout"] = "".join(output)[-65536:]
            self.check_cancel()
            return proc.returncode, "".join(output), record["stderr"]

    def probe(self, path: Path) -> tuple[dict, str]:
        code, out, err = self.command([self.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_format", "-show_streams",
                                      "-of", "json", str(path)], "读取媒体信息", timeout=60)
        try:
            info = json.loads(out)
        except json.JSONDecodeError:
            info = {}
        if code or not video_streams(info):
            return info, err or "没有可读取的视频轨道"
        return info, err

    def validate(self, path: Path, info: dict) -> dict:
        code, _, errors = self.command([self.ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                                        "-progress", "pipe:1", "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:V?",
                                        "-map", "0:a?", "-f", "null", "-"],
                                       "完整解码检查（当前阶段进度）", duration(info))
        frames = self.logs[-1]["decoded_frames"]
        valid = code == 0 and not errors.strip() and frames > 0 and bool(video_streams(info))
        return {"valid": valid, "exit_code": code, "errors": errors,
                "decoded_frames": frames, "duration_seconds": duration(info)}

    def copy_input(self, source: Path, dest: Path) -> int:
        self.emit("status", "创建只读输入的工作副本")
        copied, size = 0, source.stat().st_size
        try:
            with source.open("rb") as src, dest.open("xb") as out:
                while True:
                    self.check_cancel()
                    try:
                        block = src.read(4 * 1024 * 1024)
                    except OSError as exc:
                        raise RepairError(f"源文件读取失败，失败块起始字节偏移：{copied}。已停止，未进行零填充。{exc}") from exc
                    if not block:
                        break
                    out.write(block)
                    copied += len(block)
                    self.emit("progress", copied / max(size, 1) * 100)
        except OSError as exc:
            raise RepairError(f"工作副本创建失败（已复制 {copied} 字节）：{exc}") from exc
        if copied != size or source.stat().st_size != size:
            raise RepairError("复制期间源文件大小发生变化，已停止处理。")
        if not copied:
            raise RepairError("输入文件为空，无法修复。")
        return copied

    def diagnose(self, path: Path, deep: bool = True) -> dict:
        structure = inspect_container(path)
        info, errors = self.probe(path)
        result = {"structure": structure, "media": info, "probe_errors": errors}
        if video_streams(info) and deep:
            result["decode"] = self.validate(path, info)
        return result

    def encode_args(self, source: Path, dest: Path, options: Options,
                    preview: bool = False) -> list[str]:
        args = [self.ffmpeg, "-hide_banner", "-nostdin", "-n", "-v", "warning",
                "-progress", "pipe:1"]
        if options.mode == "repair":
            args += ["-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err"]
        args += ["-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:V:0", "-map", "0:a?"]
        if preview:
            args += ["-t", "5"]
        if options.mode == "enhance":
            args += ["-vf", filters(options)]
        else:
            args += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
        args += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(dest)]
        return args

    def process(self, source: Path, directory: Path, options: Options,
                preview: bool = False) -> dict:
        source, directory = source.resolve(), directory.resolve()
        if not source.is_file():
            raise RepairError("请选择一个存在的视频文件。")
        if not directory.is_dir():
            raise RepairError("输出目录不存在，请重新选择。")
        if source.stat().st_size == 0:
            raise RepairError("输入文件为空，无法修复。")
        if shutil.disk_usage(directory).free < source.stat().st_size * 2 + 32 * 1024 * 1024:
            raise RepairError("输出磁盘剩余空间不足：至少需要工作副本、结果文件和额外空间。")
        report = {"source": str(source), "started_at": datetime.now().astimezone().isoformat(),
                  "options": asdict(options), "preview": preview, "status": "failed",
                  "logs": self.logs}
        label = "preview" if preview else {"diagnose": "diagnosis", "enhance": "enhanced"}.get(options.mode, "repaired")
        unique = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        stem = source.stem[:80] + "_" + label + "_" + unique
        report_path = directory / (stem + ".json")
        final = directory / (stem + ".mp4")
        try:
            with tempfile.TemporaryDirectory(prefix="video-repair-", dir=directory) as workdir:
                work = Path(workdir)
                copied = work / ("input" + source.suffix)
                report["copied_bytes"] = self.copy_input(source, copied)
                diagnosis = self.diagnose(copied, deep=options.mode == "diagnose")
                report["diagnosis"] = diagnosis
                info = diagnosis["media"]
                original_info = info
                if options.mode == "diagnose":
                    report["status"] = "healthy" if diagnosis.get("decode", {}).get("valid") and not diagnosis["probe_errors"] else "damaged"
                    return report
                if options.mode == "repair" and (options.strategy == "index" or not video_streams(info)) and (options.reference or options.strategy == "index"):
                    from extensions import rebuild_index
                    copied, info = rebuild_index(self, copied, source, work, options, report)
                if not video_streams(info):
                    if diagnosis["structure"].get("has_moov") is False:
                        raise RepairError("缺失可读取的 moov 索引。请选择同一设备、相同录制设置的健康参考视频后重试，工具将使用 Untrunc 重建索引。")
                    raise RepairError("无法识别视频轨道，普通重新封装与转码无法处理。\n" + diagnosis["probe_errors"][-1500:])
                candidate = work / "candidate.mp4"
                validation = None
                output_info = {}
                if options.mode == "repair" and options.strategy in {"auto", "remux", "index"} and not preview:
                    code, _, _ = self.command([self.ffmpeg, "-hide_banner", "-nostdin", "-n", "-v", "warning",
                                               "-progress", "pipe:1", "-fflags", "+genpts", "-protocol_whitelist", "file,pipe", "-i", str(copied),
                                               "-map", "0:V?", "-map", "0:a?", "-c", "copy",
                                               "-movflags", "+faststart", str(candidate)],
                                              "无损重新封装（当前阶段进度）", duration(info))
                    if code == 0 and candidate.exists():
                        output_info, probe_error = self.probe(candidate)
                        validation = self.validate(candidate, output_info) if video_streams(output_info) else {"valid": False, "errors": probe_error}
                    report["remux_validation"] = validation
                    if validation and validation["valid"]:
                        report["method"] = "remux"
                    elif options.strategy == "remux":
                        raise RepairError("无损重新封装未通过完整解码检查。可以选择“自动”或“重新编码抢救”再试。")
                    else:
                        candidate.unlink(missing_ok=True)
                        self.emit("log", "重新封装未通过检查，尝试重新编码抢救。")
                if not validation or not validation["valid"]:
                    seconds = min(duration(info) or 5, 5) if preview else duration(info)
                    if options.mode == "enhance" and options.ai:
                        from extensions import enhance_ai
                        enhance_ai(self, copied, candidate, info, options, preview, report)
                    else:
                        code, _, errors = self.command(self.encode_args(copied, candidate, options, preview),
                                                       "生成 5 秒预览" if preview else "处理视频（当前阶段进度）", seconds)
                        if code or not candidate.exists():
                            raise RepairError("处理失败。\n" + errors[-1800:])
                    output_info, probe_error = self.probe(candidate)
                    validation = self.validate(candidate, output_info) if video_streams(output_info) else {"valid": False, "errors": probe_error}
                    report["method"] = ("ai_super_resolution" if options.ai else "enhance") if options.mode == "enhance" else "transcode"
                if "index_rebuild" in report:
                    report["method"] = "untrunc+" + report["method"]
                report["output_media"] = output_info
                report["validation"] = validation
                if not validation["valid"]:
                    raise RepairError("输出未通过完整解码检查，未作为修复成功导出。\n" + validation.get("errors", "")[-1500:])
                if report.get("ai") and validation.get("decoded_frames") != report["ai"]["processed_frames"]:
                    raise RepairError("AI 合成后的视频帧数与已处理帧数不一致，已停止导出；请检查源视频音轨和时长。")
                before, after = duration(original_info), duration(output_info)
                report["recovered_duration_seconds"] = after
                report["original_duration_seconds"] = before
                if before and after and not preview:
                    report["duration_ratio"] = after / before
                input_tracks = [s.get("codec_type") for s in original_info.get("streams", []) if s.get("codec_type") in {"audio", "video"}]
                output_tracks = [s.get("codec_type") for s in output_info.get("streams", []) if s.get("codec_type") in {"audio", "video"}]
                report["input_tracks"] = input_tracks
                report["output_tracks"] = output_tracks
                partial = not preview and ((bool(before and after) and after < before * 0.98) or (bool(input_tracks) and sorted(input_tracks) != sorted(output_tracks)))
                report["status"] = "partial" if partial else "verified"
                report["note"] = "解码检查通过；时长或轨道减少，请人工确认。" if partial else "输出完整解码检查通过；视觉效果仍需人工确认。"
                if "index_rebuild" in report and not before:
                    report["note"] += " 原片时长未知；索引重建的恢复范围、音画同步需人工核对，未计算恢复比例。"
                self.check_cancel()
                # On Windows rename is atomic and refuses to overwrite existing files.
                if final.exists():
                    raise RepairError("目标文件已存在，已停止以避免覆盖。")
                candidate.rename(final)
                report["output"] = str(final)
                self.emit("progress", 100.0)
                return report
        except Cancelled:
            report["status"] = "cancelled"
            raise
        except Exception as exc:
            report["error"] = str(exc)
            raise
        finally:
            report["finished_at"] = datetime.now().astimezone().isoformat()
            report["report"] = str(report_path)
            try:
                with report_path.open("x", encoding="utf-8") as output:
                    json.dump(report, output, ensure_ascii=False, indent=2)
                self.emit("report", str(report_path))
            except OSError as exc:
                self.emit("log", f"报告保存失败：{exc}")
