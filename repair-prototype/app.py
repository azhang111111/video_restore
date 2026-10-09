"""Chinese Windows desktop UI; all video jobs run outside Tk's UI thread."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from engine import Cancelled, Engine, HIDDEN, Options, RepairError, describe, find_tool
from extensions import extension_tool
from hardware import detect_gpus

BG = "#10151f"
PANEL = "#192130"
FIELD = "#111927"
TEXT = "#edf2fa"
MUTED = "#9cabc1"
ACCENT = "#65ddbc"
MODES = {"repair": ("文件修复", "诊断损坏 · 重新封装 · 抢救可播放内容"),
         "enhance": ("画质修复", "降噪 · 锐化 · 调整亮度 · 放大画面"),
         "diagnose": ("视频诊断", "检查容器、媒体轨道与完整解码错误")}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("拾帧 · 视频修复工具 v0.2")
        self.root.geometry("1120x850")
        self.root.minsize(1000, 790)
        self.root.configure(bg=BG)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.root.option_add("*insertBackground", TEXT)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.closing = False
        self.task = None
        self.players = []
        self.previews = []
        self.result = None
        self.last_report = None
        self.mutable = []
        self.mode = tk.StringVar(value="repair")
        self.input = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.strategy = tk.StringVar(value="自动：先无损封装，再编码抢救")
        self.denoise = tk.StringVar(value="中")
        self.scale = tk.StringVar(value="原尺寸")
        self.sharpen = tk.BooleanVar(value=True)
        self.brighten = tk.BooleanVar(value=False)
        self.deinterlace = tk.BooleanVar(value=False)
        self.reference = tk.StringVar()
        self.index_skip_unknown = tk.BooleanVar(value=True)
        self.ai = tk.BooleanVar(value=False)
        self.ai_model = tk.StringVar(value="实拍通用")
        self.ai_scale = tk.StringVar(value="2 倍")
        self.ai_gpu = tk.StringVar(value="自动（优先独立显卡）")
        self.ai_tile = tk.StringVar(value="自动")
        self.gpu_choices = {"自动（优先独立显卡）": "auto"}
        self.status = tk.StringVar(value="选择视频，开始修复")
        self.media = tk.StringVar(value="支持 MP4、MOV、MKV、AVI、TS 等常见视频格式")
        self.progress_value = tk.DoubleVar(value=0)
        self._style()
        self._build()
        self._switch_mode()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._poll)
        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
            self.engine_status.configure(text="●  本地视频引擎已就绪", fg=ACCENT)
            try:
                extension_tool("ai")
                extension_tool("index")
                self.engine_status.configure(text="●  基础 / AI / 索引引擎已就绪")
            except RepairError:
                self._log("AI / 索引引擎可通过“安装扩展引擎.bat”安装。")
        except RepairError:
            self.engine_status.configure(text="●  视频引擎尚未安装", fg="#ffce87")
            self._log("请先双击“安装视频引擎.bat”，安装后重新打开程序。")
        self.refresh_gpus()

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=FIELD, background=PANEL,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor="#35435a", padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", TEXT), ("disabled", MUTED)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                        bordercolor=FIELD, lightcolor=ACCENT, darkcolor=ACCENT)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    def label(self, parent, text="", **kwargs):
        bg = kwargs.pop("bg", parent.cget("bg"))
        return tk.Label(parent, text=text, bg=bg, fg=kwargs.pop("fg", TEXT),
                        anchor="w", **kwargs)

    def button(self, parent, text, command, primary=False, mutable=True, **kwargs):
        btn = tk.Button(parent, text=text, command=command, bg=ACCENT if primary else "#29364b",
                        fg=BG if primary else TEXT, activebackground="#89ebcf" if primary else "#374a65",
                        activeforeground=BG if primary else TEXT, disabledforeground="#768399",
                        relief="flat", bd=0, cursor="hand2", padx=kwargs.pop("padx", 16), pady=kwargs.pop("pady", 9), **kwargs)
        if mutable:
            self.mutable.append(btn)
        return btn

    def card(self, parent):
        return tk.Frame(parent, bg=PANEL, padx=20, pady=15)

    def _build(self):
        sidebar = tk.Frame(self.root, bg="#0c111b", width=212, padx=20, pady=30)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self.label(sidebar, "拾帧", font=("Microsoft YaHei UI", 28, "bold")).pack(fill="x")
        self.label(sidebar, "VIDEO RESTORE", fg=ACCENT, font=("Segoe UI", 9, "bold")).pack(fill="x", pady=(2, 36))
        self.nav = {}
        for mode, (name, _) in MODES.items():
            btn = self.button(sidebar, name, lambda m=mode: self._switch_mode(m), anchor="w")
            btn.pack(fill="x", pady=5)
            self.nav[mode] = btn
        footer = tk.Frame(sidebar, bg=sidebar.cget("bg"))
        footer.pack(side="bottom", fill="x")
        self.label(footer, "LOCAL FIRST", fg=ACCENT, font=("Segoe UI", 9, "bold")).pack(fill="x")
        self.label(footer, "视频在本机处理\n结果另存，保留原片", fg=MUTED, justify="left").pack(fill="x", pady=(8, 0))
        content = tk.Frame(self.root, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(content, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(content, orient="vertical", command=self.canvas.yview)
        scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        main = tk.Frame(self.canvas, bg=BG, padx=28, pady=16)
        window = self.canvas.create_window(0, 0, window=main, anchor="nw")

        def resize_content(event=None):
            self.canvas.itemconfigure(window, width=self.canvas.winfo_width(),
                                      height=max(main.winfo_reqheight(), self.canvas.winfo_height()))
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        main.bind("<Configure>", resize_content)
        self.canvas.bind("<Configure>", resize_content)
        self.root.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units")
                       if event.widget.winfo_class() not in {"Text", "TCombobox"} else None)
        top = tk.Frame(main, bg=BG)
        top.pack(fill="x", pady=(0, 12))
        self.title = self.label(top, font=("Microsoft YaHei UI", 24, "bold"))
        self.title.pack(side="left")
        self.engine_status = self.label(top, fg=ACCENT, font=("Microsoft YaHei UI", 9))
        self.engine_status.pack(side="right", pady=(8, 0))
        self.subtitle = self.label(main, fg=MUTED)
        self.subtitle.pack(fill="x", pady=(0, 16))

        files = self.card(main)
        files.pack(fill="x", pady=(0, 12))
        self.label(files, "01   选择视频", font=("Microsoft YaHei UI", 11, "bold")).pack(fill="x", pady=(0, 10))
        row = tk.Frame(files, bg=PANEL)
        row.pack(fill="x")
        tk.Entry(row, textvariable=self.input, readonlybackground=FIELD, fg=TEXT,
                 relief="flat", state="readonly", borderwidth=8).pack(side="left", fill="x", expand=True, ipady=3)
        self.button(row, "浏览文件", self.choose_input).pack(side="left", padx=(10, 0))
        self.label(files, textvariable=self.media, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(fill="x", pady=(8, 12))
        self.label(files, "输出目录", fg=MUTED).pack(fill="x", pady=(0, 6))
        row = tk.Frame(files, bg=PANEL)
        row.pack(fill="x")
        tk.Entry(row, textvariable=self.output_dir, readonlybackground=FIELD, fg=TEXT,
                 relief="flat", state="readonly", borderwidth=8).pack(side="left", fill="x", expand=True, ipady=3)
        self.button(row, "选择目录", self.choose_output).pack(side="left", padx=(10, 0))

        self.settings = self.card(main)
        self.settings.pack(fill="x", pady=(0, 12))
        self.label(self.settings, "02   处理设置", font=("Microsoft YaHei UI", 11, "bold")).pack(fill="x", pady=(0, 10))
        self.repair_settings = tk.Frame(self.settings, bg=PANEL)
        row = tk.Frame(self.repair_settings, bg=PANEL)
        row.pack(fill="x")
        self.label(row, "修复策略", fg=MUTED).pack(side="left", padx=(0, 14))
        combo = ttk.Combobox(row, textvariable=self.strategy, state="readonly", width=40,
                             values=["自动：先无损封装，再编码抢救", "仅无损重新封装", "重新编码抢救", "参考视频重建索引"])
        combo.pack(side="left", fill="x", expand=True)
        self.mutable.append(combo)
        row = tk.Frame(self.repair_settings, bg=PANEL)
        row.pack(fill="x", pady=(10, 0))
        self.label(row, "健康参考片", fg=MUTED).pack(side="left", padx=(0, 8))
        tk.Entry(row, textvariable=self.reference, readonlybackground=FIELD, fg=TEXT,
                 relief="flat", state="readonly", borderwidth=5).pack(side="left", fill="x", expand=True)
        self.button(row, "选择", self.choose_reference, padx=10).pack(side="left", padx=(6, 0))
        self.button(row, "清除", lambda: self.reference.set(""), padx=10).pack(side="left", padx=(6, 0))
        check = tk.Checkbutton(self.repair_settings, text="重建时跳过无法识别的损坏数据", variable=self.index_skip_unknown,
                               bg=PANEL, fg=TEXT, selectcolor=FIELD, activebackground=PANEL, activeforeground=TEXT)
        check.pack(anchor="w", pady=(6, 0))
        self.mutable.append(check)
        self.enhance_settings = tk.Frame(self.settings, bg=PANEL)
        check = tk.Checkbutton(self.enhance_settings, text="启用 AI 超分辨率（Real-ESRGAN）", variable=self.ai,
                               command=self.toggle_ai, bg=PANEL, fg=ACCENT, selectcolor=FIELD,
                               activebackground=PANEL, activeforeground=ACCENT)
        check.pack(anchor="w", pady=(0, 8))
        self.mutable.append(check)
        self.ai_settings = tk.Frame(self.enhance_settings, bg=PANEL)
        row = tk.Frame(self.ai_settings, bg=PANEL)
        row.pack(fill="x", pady=(0, 8))
        self.label(row, "AI 模型", fg=MUTED).pack(side="left", padx=(0, 10))
        model = ttk.Combobox(row, textvariable=self.ai_model, values=["实拍通用", "动画"], width=14, state="readonly")
        model.pack(side="left", padx=(0, 20))
        self.label(row, "AI 倍率", fg=MUTED).pack(side="left", padx=(0, 10))
        ai_scale = ttk.Combobox(row, textvariable=self.ai_scale, values=["2 倍", "4 倍"], width=8, state="readonly")
        ai_scale.pack(side="left")
        row = tk.Frame(self.ai_settings, bg=PANEL)
        row.pack(fill="x", pady=(0, 10))
        self.label(row, "推理设备", fg=MUTED).pack(side="left", padx=(0, 10))
        self.gpu_combo = ttk.Combobox(row, textvariable=self.ai_gpu, values=list(self.gpu_choices), width=31, state="readonly")
        self.gpu_combo.pack(side="left", fill="x", expand=True)
        self.button(row, "检测", self.refresh_gpus, padx=8).pack(side="left", padx=6)
        self.label(row, "分块", fg=MUTED).pack(side="left", padx=(5, 8))
        tile = ttk.Combobox(row, textvariable=self.ai_tile, values=["自动", "32", "64", "128", "256"], width=6, state="readonly")
        tile.pack(side="left")
        self.mutable.extend([model, ai_scale, self.gpu_combo, tile])
        row = tk.Frame(self.enhance_settings, bg=PANEL)
        row.pack(fill="x", pady=(0, 10))
        self.label(row, "降噪强度", fg=MUTED).pack(side="left", padx=(0, 10))
        denoise = ttk.Combobox(row, textvariable=self.denoise, state="readonly", width=7, values=["关闭", "轻", "中", "强"])
        denoise.pack(side="left", padx=(0, 25))
        self.label(row, "输出尺寸", fg=MUTED).pack(side="left", padx=(0, 10))
        scale = ttk.Combobox(row, textvariable=self.scale, state="readonly", width=22,
                             values=["原尺寸", "2 倍", "1080p（仅放大）"])
        scale.pack(side="left")
        self.traditional_scale = scale
        self.mutable.extend([denoise, scale])
        row = tk.Frame(self.enhance_settings, bg=PANEL)
        row.pack(fill="x")
        for name, variable in [("细节锐化", self.sharpen), ("轻微提亮", self.brighten), ("去隔行（老录像）", self.deinterlace)]:
            check = tk.Checkbutton(row, text=name, variable=variable, bg=PANEL, fg=TEXT,
                                   selectcolor=FIELD, activebackground=PANEL, activeforeground=TEXT,
                                   relief="flat", cursor="hand2")
            check.pack(side="left", padx=(0, 16))
            self.mutable.append(check)
        self.diagnose_settings = self.label(self.settings, "读取容器结构与轨道信息，并逐帧解码检查；导出 JSON 诊断报告。", fg=MUTED)
        self.hint = self.label(self.settings, fg=MUTED, wraplength=730, justify="left", font=("Microsoft YaHei UI", 9))
        self.settings.bind("<Configure>", lambda event: self.hint.configure(wraplength=max(200, event.width - 40)))

        action = tk.Frame(main, bg=BG)
        action.pack(fill="x", pady=(0, 14))
        self.start_button = self.button(action, "开始修复", self.start, primary=True)
        self.start_button.pack(side="left")
        self.preview_button = self.button(action, "生成 5 秒预览", lambda: self.start(preview=True))
        self.preview_button.pack(side="left", padx=10)
        self.cancel_button = self.button(action, "取消任务", self.cancel_task, mutable=False, state="disabled")
        self.cancel_button.pack(side="right")

        progress = self.card(main)
        progress.pack(fill="x", pady=(0, 12))
        self.status_label = self.label(progress, textvariable=self.status, wraplength=735, justify="left")
        progress.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(200, event.width - 40)))
        self.status_label.pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(progress, variable=self.progress_value, maximum=100)
        self.progress.pack(fill="x")
        self.label(progress, "进度按当前阶段显示；未知时长时显示活动指示。", fg=MUTED,
                   font=("Microsoft YaHei UI", 8)).pack(fill="x", pady=(6, 0))
        results = tk.Frame(main, bg=BG)
        results.pack(fill="x", pady=(0, 10))
        self.play_original = self.button(results, "播放原片", lambda: self.play(self.input.get()), mutable=False)
        self.play_original.pack(side="left")
        self.play_result = self.button(results, "播放结果", self.play_output, mutable=False, state="disabled")
        self.play_result.pack(side="left", padx=8)
        self.open_output = self.button(results, "打开输出目录", self.open_directory, mutable=False)
        self.open_output.pack(side="left")
        self.report_button = self.button(results, "查看报告", self.open_report, mutable=False, state="disabled")
        self.report_button.pack(side="right")
        self.log = tk.Text(main, bg=FIELD, fg=MUTED, relief="flat", height=5, padx=12, pady=10,
                           font=("Microsoft YaHei UI", 9), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True)
        self._log("就绪。请在可正常读写、空间充足的磁盘选择输出目录。")

    def _switch_mode(self, mode=None):
        if self.busy:
            return
        if mode:
            self.mode.set(mode)
        mode = self.mode.get()
        name, subtitle = MODES[mode]
        self.title.configure(text=name)
        self.subtitle.configure(text=subtitle)
        for key, button in self.nav.items():
            button.configure(bg="#203b3c" if key == mode else "#0c111b", fg=ACCENT if key == mode else MUTED)
        for panel in (self.repair_settings, self.enhance_settings, self.diagnose_settings, self.hint):
            panel.pack_forget()
        {"repair": self.repair_settings, "enhance": self.enhance_settings, "diagnose": self.diagnose_settings}[mode].pack(fill="x")
        hints = {"repair": "缺失索引时需同一设备、相同录制设置的健康参考片。自动策略仅在轨道无法读取时尝试索引重建。",
                 "enhance": "AI 按当前 Vulkan 设备处理并另存；建议先试 5 秒预览。AI 输出为平均帧率对应的恒定帧率，逐帧处理可能出现闪烁。" if self.ai.get() else "传统滤镜修复；勾选 AI 超分辨率可选择实拍/动画模型和 2 倍/4 倍放大。",
                 "diagnose": "损坏盘读取失败会停止并记录失败块的起始偏移；请优先使用健康磁盘上的副本。"}
        self.hint.configure(text=hints[mode])
        self.hint.pack(fill="x", pady=(10, 0))
        self.start_button.configure(text={"repair": "开始修复", "enhance": "开始画质修复", "diagnose": "开始完整诊断"}[mode])
        self.preview_button.configure(state="normal" if mode == "enhance" else "disabled")
        self.toggle_ai(update_hint=False)

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def choose_input(self):
        path = filedialog.askopenfilename(title="选择待修复视频", filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.ts *.m4v *.mts *.m2ts *.webm *.wmv *.3gp"), ("所有文件", "*.*")])
        if not path:
            return
        self.input.set(path)
        if not self.output_dir.get():
            self.output_dir.set(str(Path(path).parent))
        self.media.set("已选择视频，正在读取媒体信息…")
        self.result = None
        self.play_result.configure(state="disabled")

        def read_info():
            try:
                engine = Engine()
                info, errors = engine.probe(Path(path))
                self.events.put(("media", (path, describe(info) if not errors else "媒体信息异常，可使用“视频诊断”查看详情")))
            except Exception as exc:
                self.events.put(("media", (path, str(exc))))
        threading.Thread(target=read_info, daemon=True).start()

    def choose_output(self):
        directory = filedialog.askdirectory(title="选择输出目录（建议健康磁盘）")
        if directory:
            self.output_dir.set(directory)

    def choose_reference(self):
        path = filedialog.askopenfilename(title="选择同设备、同录制设置的健康参考视频", filetypes=[("MP4 / MOV", "*.mp4 *.mov *.m4v *.3gp"), ("所有文件", "*.*")])
        if path:
            self.reference.set(path)

    def toggle_ai(self, update_hint=True):
        self.ai_settings.pack_forget()
        if self.ai.get():
            children = self.enhance_settings.winfo_children()
            self.ai_settings.pack(fill="x", before=children[2])
        self.traditional_scale.configure(state="disabled" if self.ai.get() or self.busy else "readonly")
        if update_hint:
            self._switch_mode()

    def refresh_gpus(self):
        if self.busy:
            return
        def detect():
            try:
                self.events.put(("gpus", (detect_gpus(), "")))
            except Exception as exc:
                self.events.put(("gpus", ([], str(exc))))
        threading.Thread(target=detect, daemon=True).start()

    def options(self):
        strategies = {"自动：先无损封装，再编码抢救": "auto", "仅无损重新封装": "remux", "重新编码抢救": "transcode", "参考视频重建索引": "index"}
        return Options(mode=self.mode.get(), strategy=strategies[self.strategy.get()],
                       denoise=self.denoise.get(), sharpen=self.sharpen.get(),
                       brighten=self.brighten.get(), deinterlace=self.deinterlace.get(), scale=self.scale.get(),
                       ai=self.ai.get(), ai_model="anime" if self.ai_model.get() == "动画" else "general",
                       ai_scale=4 if self.ai_scale.get() == "4 倍" else 2,
                       ai_gpu=self.gpu_choices.get(self.ai_gpu.get(), "auto"),
                       ai_tile=0 if self.ai_tile.get() == "自动" else int(self.ai_tile.get()),
                       reference=self.reference.get(), index_skip_unknown=self.index_skip_unknown.get())

    def _set_busy(self, busy):
        self.busy = busy
        for widget in self.mutable:
            widget.configure(state="disabled" if busy else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if not busy:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._switch_mode()

    def start(self, preview=False):
        if self.busy:
            return
        source_text, output_text = self.input.get(), self.output_dir.get()
        if not source_text or not Path(source_text).is_file():
            messagebox.showinfo("选择视频", "请先选择一个存在的视频文件。")
            return
        if not output_text or not Path(output_text).is_dir():
            messagebox.showinfo("选择目录", "请先选择输出目录。")
            return
        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
        except RepairError as exc:
            messagebox.showerror("缺少视频引擎", str(exc))
            return
        source, directory, options = Path(source_text), Path(output_text), self.options()
        if options.mode == "repair" and options.strategy == "index" and not options.reference:
            messagebox.showinfo("选择参考视频", "索引重建需要同一设备、相同录制设置的健康参考视频。")
            return
        if options.mode == "enhance" and options.ai:
            try:
                extension_tool("ai")
            except RepairError as exc:
                messagebox.showerror("缺少 AI 引擎", str(exc))
                return
        if preview:
            temporary = tempfile.TemporaryDirectory(prefix="shizhen-preview-")
            self.previews.append(temporary)
            directory = Path(temporary.name)
        self.result = None
        self.last_report = None
        self.play_result.configure(state="disabled")
        self.report_button.configure(state="disabled")
        self.progress_value.set(0)
        self.cancel.clear()
        self._set_busy(True)
        self._log(("预览" if preview else MODES[options.mode][0]) + "：" + str(source))

        def work():
            try:
                engine = Engine(self.cancel, lambda kind, value: self.events.put((kind, value)))
                result = engine.process(source, directory, options, preview)
                self.events.put(("done", result))
            except Cancelled:
                self.events.put(("cancelled", "任务已取消，工作副本已清理。"))
            except Exception as exc:
                self.events.put(("failed", str(exc)))
        self.task = threading.Thread(target=work, daemon=True)
        self.task.start()

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status":
                    self.status.set(value)
                    self.progress_value.set(0)
                    self.progress.stop()
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(15)
                    self._log(value)
                elif kind == "progress":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress_value.set(value)
                elif kind == "media":
                    path, text = value
                    if self.input.get() == path:
                        self.media.set(text)
                elif kind == "gpus":
                    devices, error = value
                    self.gpu_choices = {"自动（优先独立显卡）": "auto"}
                    self.gpu_choices.update({f"GPU {d['index']} · {d['name']}": str(d["index"]) for d in devices})
                    self.gpu_combo.configure(values=list(self.gpu_choices))
                    if self.ai_gpu.get() not in self.gpu_choices:
                        self.ai_gpu.set("自动（优先独立显卡）")
                    self._log("检测到 Vulkan 设备：" + "；".join(d["name"] for d in devices) if devices else "未检测到 Vulkan GPU：" + error)
                elif kind == "log":
                    self._log(value)
                elif kind == "report":
                    self.last_report = value
                    self.report_button.configure(state="normal")
                    self._log("报告：" + value)
                elif kind == "done":
                    self.result = value
                    self._set_busy(False)
                    self.progress_value.set(100)
                    status = value["status"]
                    if value.get("preview"):
                        text = "5 秒预览已生成并通过解码检查，可播放原片和结果比较。"
                    else:
                        text = {"verified": "处理完成，输出已通过完整解码检查。",
                                "partial": "已抢救部分内容；时长或轨道减少，请查看报告。",
                                "healthy": "诊断完成：容器可读取，完整解码检查通过。",
                                "damaged": "诊断完成：检测到媒体信息或解码异常，请查看报告。"}[status]
                    self.status.set(text)
                    self._log(text)
                    if value.get("index_rebuild"):
                        self.status.set("索引重建已抢救部分内容，输出解码检查通过；请查看报告。" if status == "partial" else
                                        "索引重建完成，输出已通过解码检查；请核对恢复内容和音画同步。")
                        self._log(value["note"])
                    if value.get("ai"):
                        self._log(f"AI 已处理 {value['ai']['processed_frames']} 帧；设备：{value['ai']['gpu']['name']}。")
                    if value.get("output"):
                        self.play_result.configure(state="normal")
                        self._log("结果：" + value["output"])
                    self.media.set(describe(value.get("diagnosis", {}).get("media", {})))
                elif kind in {"failed", "cancelled"}:
                    self._set_busy(False)
                    self.progress_value.set(0)
                    self.status.set("任务已取消" if kind == "cancelled" else "处理失败，请查看日志或报告")
                    self._log(value)
                    if kind == "failed" and not self.closing:
                        messagebox.showerror("处理失败", value[:1600])
        except queue.Empty:
            pass
        if self.closing and not self.busy:
            self._destroy()
            return
        self.root.after(100, self._poll)

    def cancel_task(self):
        if self.busy:
            self.cancel.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("正在停止任务并清理工作文件…")

    def play(self, path):
        if not path or not Path(path).is_file():
            messagebox.showinfo("播放视频", "请先选择视频或完成处理。")
            return
        try:
            player = find_tool("ffplay")
            proc = subprocess.Popen([player, "-hide_banner", "-loglevel", "error", "-autoexit",
                                     "-window_title", "拾帧 · " + Path(path).name,
                                     "-x", "800", "-y", "450", "-protocol_whitelist", "file,pipe", str(Path(path).resolve())],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=HIDDEN)
            self.players.append(proc)
        except (RepairError, OSError) as exc:
            messagebox.showerror("无法播放", str(exc))

    def play_output(self):
        if self.result:
            self.play(self.result.get("output"))

    def open_directory(self):
        path = self.output_dir.get()
        if path and Path(path).is_dir():
            try:
                os.startfile(path)
            except OSError as exc:
                messagebox.showerror("无法打开目录", str(exc))

    def open_report(self):
        if not self.last_report or not Path(self.last_report).is_file():
            return
        report_window = tk.Toplevel(self.root)
        report_window.title("拾帧 · 处理报告")
        report_window.geometry("900x650")
        report_window.configure(bg=BG)
        self.label(report_window, self.last_report, fg=MUTED, wraplength=850).pack(fill="x", padx=16, pady=12)
        body = tk.Text(report_window, bg=FIELD, fg=TEXT, wrap="word", font=("Consolas", 10), padx=15, pady=15)
        scrollbar = tk.Scrollbar(report_window, command=body.yview)
        scrollbar.pack(side="right", fill="y")
        body.configure(yscrollcommand=scrollbar.set)
        body.pack(fill="both", expand=True)
        body.insert("1.0", Path(self.last_report).read_text(encoding="utf-8"))
        body.configure(state="disabled")

    def close(self):
        self.closing = True
        if self.busy:
            self.cancel_task()
        else:
            self._destroy()

    def _destroy(self):
        for proc in self.players:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        for temporary in self.previews:
            try:
                temporary.cleanup()
            except OSError:
                pass
        self.root.destroy()


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
