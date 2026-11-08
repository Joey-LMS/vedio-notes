# 运行要求

本 skill 只提供指令和自身脚本，不包含或自动安装运行环境。使用者自行准备以下依赖；运行 `./ai-notes doctor` 可查看当前环境的检测结果。

## 本地依赖

| 依赖 | 用途与要求 |
| --- | --- |
| Python 3.10 或以上 | 所有命令需要。默认使用 PATH 中的 `python3`，可通过 `AI_NOTES_PYTHON` 指定解释器路径。脚本仅使用标准库；`zoneinfo` 需要系统提供 IANA `Asia/Shanghai` 时区数据，缺少系统时区数据库的平台需自行提供 Python `tzdata` 包。 |
| `ffprobe` | 素材采集时检查视频和音频。可执行文件需在 PATH 中。 |
| `ffmpeg` | 采集时提取音频；需在 PATH 中。恢复已有音频的运行可复用该文件。 |
| `course2md` CLI | 采集时按画面变化提取截图；需在 PATH 中，且能在同一 PATH 中调用 `ffmpeg` 和 `ffprobe`。已验证 2.0.1；其他版本至少需支持 `--transcript-source subtitle`、`--no-llm`、`--no-summarize`、`--sample-interval`、`--cooldown`、`--roi`、`--formats`、`-o` 和 `--json`。来源：[course2md 2.0.1](https://github.com/mizorewww/course2md/releases/tag/v2.0.1)。 |
| Codex CLI | 仅终端独立运行 `run` / `finish` 生成初版时需要可执行文件和已登录账户。默认从 PATH 查找，也可用 `AI_NOTES_CODEX_BIN` 指定路径；当前 Codex 会话执行 `prepare`、`check` 并写笔记时不需要额外启动 CLI。 |

检查已有笔记的 `check` 只需要 Python 和时区数据，不要求安装采集工具。`doctor` 的整体 `ok` 包含 Codex 登录状态，因此当前会话只做素材准备时可分别查看各项结果。`doctor` 不验证外部服务、工具版本兼容性或模型生成效果。

当前工作流已在 macOS 上测试，其他操作系统和架构尚未验证。Python 的时区数据说明：[zoneinfo 文档](https://docs.python.org/3/library/zoneinfo.html)。

## 网络服务

采集需要访问 B 站的视频信息与视频文件。提取的音频会上传至 B 站必剪在线语音识别服务；其接口并非公开稳定 API，可能变化、限流或失败。客户端协议参考 [dsh-bilibili](https://github.com/CZX2244/dsh-bilibili) 与 [bcut-asr](https://github.com/SocialSisterYi/bcut-asr)。
