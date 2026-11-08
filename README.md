# B 站视频图文笔记

这个项目把 B 站视频整理成两版图文笔记：先保存带时间点和截图的初版，再由 Codex 根据初版、完整转写和截图写成可独立阅读的详解版。

本仓库包含 skill 指令、CLI、处理脚本和提示模板。外部依赖列在 [requirements.md](requirements.md)，需要使用者自行准备；skill 不携带或自动安装运行环境。运行产物默认保存在技能目录之外。

## 环境与外部服务

- 素材采集需要 Python 3.10 及以上，以及 PATH 中的 FFmpeg、ffprobe、course2md；详见 [requirements.md](requirements.md)。当前仅在 macOS 上验证，其他平台尚未验证。
- 终端独立运行 `run` 或 `finish` 还需要 Codex CLI 和登录；使用当前 Codex 会话整理笔记时不需要额外启动 CLI。Codex 在本机读取文件并调用模型服务。
- 视频信息和文件来自 B 站。音频会上传到 B 站必剪在线 ASR 服务；它是社区发现的非公开接口，可能失败、限流或变化。
- 目前支持 B 站视频及分 P，每个分 P 最长 2 小时。需要登录、会员或无法匿名取得完整视频的内容可能无法处理。

## 在 Codex 中使用

将本仓库内容放入 Codex 技能目录下的 `online-video-notes/` 文件夹，然后直接说：

> 使用 $online-video-notes 把这个 B 站视频整理成笔记：视频链接

Codex 会准备素材，先保存 `note.md`，再生成 `note-expanded.md`。第二版以主题和论证组织内容，截图放在相关段落，时间点作为回看辅助。若已有可用素材或初版，可直接继续，不必重新转写。

## 在终端使用

在 skill 目录下运行：

```sh
./ai-notes doctor
./ai-notes 'https://www.bilibili.com/video/BVxxxxxxxxxx/'
```

`doctor` 检查 Python、PATH 中的工具和 Codex 登录，但不测试必剪或模型推理是否可用；整体 `ok` 包含终端成稿所需的 Codex 检查。`prepare` 和 `check` 不要求 Codex CLI。完整命令会下载视频、转写、抽帧并调用本机 Codex 写**初版**；独立终端命令目前不会自动生成详解版。要生成第二版，可把运行目录交给 Codex 并使用该 skill。

常用分步命令：

```sh
# 采集素材，输出中包含本次 run_dir
./ai-notes prepare 'https://www.bilibili.com/video/BVxxxxxxxxxx/' --json

# 为已有素材生成初版
./ai-notes finish '运行目录'

# 检查并登记初版或详解版
./ai-notes check '运行目录' --complete
./ai-notes check '运行目录' --note note-expanded.md --complete
```

各子命令均支持 `--json`：结果输出到标准输出，进度输出到标准错误。多 P 视频可加 `--page 2`，链接中的 `?p=2` 也会被识别；`--instructions '补充要求'` 可调整初版写作方向。`check` 检查素材、图片引用、来源和时间点等基本条件，不保证转写或笔记内容完全准确。状态中的 `completed` 表示初版完成，详解版记录在 `alternate_notes` 中。

截图检测默认忽略画面底部 15%，以减少烧录字幕造成的误判。可通过 `--roi '0%,0%-100%,100%'` 调整。动画、画中画和短暂画面可能导致重复或漏图，重要页面应抽查。终端自动成稿最多直接附入 24 张沿时间均匀选取的截图，其余相关画面需要 Codex 另行查看。

## 运行数据的位置

运行数据默认保存在 skill 文件夹之外：

- 如果 skill 位于 `<目录>/skills/online-video-notes`，默认使用 `<目录>/runs/`。
- 如果 skill 放在其他位置，默认使用技能目录旁边的 `<技能目录名>-runs/`。
- 设置 `AI_NOTES_RUNS_DIR` 可以指定其他运行目录；处理已有运行目录时，需让该变量指向其所在的运行目录根路径。

如果自行把运行目录设在 skill 文件夹内，复制或打包技能时也会带上其中的数据，应先检查和排除。

每次新运行均创建独立目录，包含 `input/`、`materials/`、`logs/`、`status.json` 和生成的笔记。截图处理会使用系统临时目录，完成后清理临时文件。

中断后可使用 `./ai-notes prepare '链接' --resume '运行目录'` 继续；已有视频必须完整且与 `source.json` 记录的大小一致。提交 ASR 后超时可以尝试恢复同一任务；若服务报告任务失败或任务 ID 无效，需新建运行。错误会记录在状态和日志中，不会把标题或简介充作转写。

## 分享与第三方组件

本仓库提供 `.gitignore`，用于排除运行数据、缓存、日志、常见凭据和系统元数据。忽略规则不适用于已经被 Git 跟踪的文件，也不能阻止手动复制或强制添加；公开前应检查实际拟提交的文件和历史。视频、音频、截图、转写、笔记和日志可能含个人信息或受版权保护的素材，不应默认公开。

本项目自己的开源许可证尚未选定。Bcut 接口实现所参考的社区项目与外部工具来源见 [requirements.md](requirements.md)。
