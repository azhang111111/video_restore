# 拾帧 · 视频修复工具

本地 Windows 桌面视频修复工具，支持文件修复、参考视频索引重建和 AI 超分辨率。视频与参考片在本机处理，结果另存，保留原始文件。

## 功能

- 文件修复：重新封装、转码抢救、完整解码验证与 JSON 报告。
- 索引重建：使用 Untrunc 和健康参考片恢复损坏 MP4 / MOV 的索引。
- AI 超分辨率：Real-ESRGAN 实拍 / 动画模型，2 倍 / 4 倍输出，支持短片预览。
- 硬件适配：检测实际 Vulkan 显卡，自动或手动选择 GPU，分块默认按显存预算决定。
- 传统画质修复：降噪、锐化、提亮、去隔行及插值放大。

## 使用

需要 Windows、Python 3.11+（含 Tkinter）；AI 处理还需要支持 Vulkan 的显卡与对应驱动。

1. 首次使用运行 [安装视频引擎.bat](repair-prototype/安装视频引擎.bat)。
2. 需要 AI / 索引重建时运行 [安装扩展引擎.bat](repair-prototype/安装扩展引擎.bat)。
3. 双击 [启动视频修复工具.bat](repair-prototype/启动视频修复工具.bat)。

安装脚本需要联网，引擎与模型保存在本机 `.tools` 子目录；它们未纳入 Git 仓库。后续视频处理不需要联网。

也可以在仓库根目录运行：

```powershell
python repair-prototype/setup_tools.py
python repair-prototype/setup_extensions.py
python repair-prototype/app.py
```

AI 建议先试预览；索引重建需选择同设备、同录制设置的健康参考视频。AI 输出为恒定帧率，索引重建不能恢复已丢失的数据。真实损坏素材的内容完整性与音画同步需要人工确认。

详细操作、限制与引擎来源见 [使用说明](repair-prototype/README.md)。

## 验证

安装基础与扩展引擎后，在仓库根目录运行：

```powershell
python -m unittest discover -s repair-prototype/tests -v
python repair-prototype/tests/ui_smoke.py
```

当前 29 项测试与桌面操作流程验证通过，包含真实 AI 推理和生成样本的索引重建。实际推理已在 Iris Xe 验证；其他型号的选卡逻辑由测试覆盖，仍需对应硬件实测。真实损坏视频由用户后续验证，详见 [验证记录](repair-prototype/VALIDATION.md)。
