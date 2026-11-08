#!/usr/bin/env python3
"""Collect a Bilibili video and produce screenshot-supported notes with Codex."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from prepare_video import ROOT, RUNS, prepare_video


def clock() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def resolve_run(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_relative_to(RUNS) or not candidate.is_dir():
        raise ValueError(f"运行目录必须位于 {RUNS} 下：{candidate}")
    return candidate


def resolve_note(run: Path, value: str | Path) -> Path:
    candidate = (run / Path(value).expanduser()).resolve()
    if not candidate.is_relative_to(run) or candidate == run:
        raise ValueError("笔记必须位于指定运行目录内")
    return candidate


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"缺少文件：{path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 格式不正确：{path}")
    return value


def inspect_materials(run: Path) -> dict:
    source = load_json(run / "input/source.json")
    transcript = load_json(run / "materials/transcript.json")
    frame_index = load_json(run / "materials/capture/frames.json")
    segments, slides = transcript.get("segments"), frame_index.get("slides")
    if not segments or not slides:
        raise ValueError("转写或截图清单为空")
    if source.get("bvid") != transcript.get("bvid") or source.get("cid") != transcript.get("cid"):
        raise ValueError("转写和视频标识不一致")
    duration = float(transcript.get("duration_seconds") or source["duration_seconds"])
    previous_start = -1.0
    covered_until = 0.0
    longest_gap = 0.0
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        if start < 0 or end <= start or end > duration + 2 or start < previous_start or not str(segment.get("text", "")).strip():
            raise ValueError("转写分段的时间或内容不合法")
        previous_start = start
        longest_gap = max(longest_gap, start - covered_until)
        covered_until = max(covered_until, end)
    images = set()
    for slide in slides:
        image = (run / "materials/capture" / slide["image"]).resolve()
        if not image.is_relative_to(run) or not image.is_file():
            raise ValueError(f"截图文件缺失或路径无效：{slide['image']}")
        images.add(image)
    last_speech = max(float(segment["end"]) for segment in segments)
    warnings = []
    if longest_gap > max(20.0, duration * 0.05):
        warnings.append(f"转写开头或段间有 {longest_gap:.1f}s 无文字，可能是静音或转写遗漏，请核对。")
    if duration - last_speech > max(20.0, duration * 0.05):
        warnings.append(f"末段语音结束于 {last_speech:.1f}s，距离视频结束 {duration-last_speech:.1f}s，请核对尾部。")
    return {"source": source, "duration_seconds": duration, "transcript_segments": len(segments),
            "screenshots": len(slides), "last_speech_end_seconds": last_speech,
            "longest_untranscribed_gap_seconds": round(longest_gap, 3),
            "warnings": warnings, "images": images}


def inspect_note(run: Path, note: Path, materials: dict) -> dict:
    if not note.is_file():
        raise FileNotFoundError(f"未生成笔记：{note}")
    content = note.read_text(encoding="utf-8")
    if len(content.strip()) < 100:
        raise ValueError("笔记正文过短，未通过完整性检查")
    refs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", content)
    matching, missing = 0, []
    for ref in refs:
        location = ref.strip().split(" ", 1)[0].strip("<>")
        if urllib.parse.urlparse(location).scheme:
            continue
        path = (note.parent / urllib.parse.unquote(location)).resolve()
        if not path.is_relative_to(run) or not path.is_file():
            missing.append(location)
        elif path in materials["images"]:
            matching += 1
    if missing:
        raise ValueError("笔记图片不存在或超出运行目录：" + ", ".join(missing))
    if not matching:
        raise ValueError("笔记未嵌入本次采集的截图")
    if str(materials["source"].get("bvid", "")) not in content:
        raise ValueError("笔记未包含原视频 BV 号或链接")
    if not re.search(r"(?:\b\d{1,2}:\d{2}\b|[?&]t=\d+)", content):
        raise ValueError("笔记未包含视频时间点")
    return {"note_characters": len(content), "note_screenshots": matching}


def report_for(run: Path, note: Path | None = None) -> dict:
    materials = inspect_materials(run)
    result = {key: value for key, value in materials.items() if key not in {"images", "source"}}
    result.update({"run_dir": str(run), "title": materials["source"].get("title"),
                   "source_url": materials["source"].get("source_url")})
    if note:
        result.update(inspect_note(run, note, materials))
        result["note"] = str(note)
    return result


def complete(run: Path, note: Path, attachment_info: dict | None = None) -> dict:
    result = report_for(run, note)
    if attachment_info:
        result.update(attachment_info)
        if attachment_info["attached_screenshots"] < result["screenshots"]:
            result["warnings"].append("仅部分截图自动附给 Codex；其余截图需要另行查看，具体核对范围以笔记说明为准。")
    status_path = run / "status.json"
    status = load_json(status_path)
    quality = status.get("quality_check", {})
    if not isinstance(quality, dict):
        raise ValueError("status.json 中的 quality_check 格式不正确")
    quality.update({key: result[key] for key in
                    ("transcript_segments", "screenshots", "note_screenshots", "warnings")})
    status.update({"state": "completed", "note": str(note.relative_to(run)), "completed_at": clock(),
                   "quality_check": quality})
    if attachment_info:
        status["quality_check"].update(attachment_info)
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def complete_alternate(run: Path, note: Path) -> dict:
    result = report_for(run, note)
    status_path = run / "status.json"
    status = load_json(status_path)
    if status.get("state") != "completed" or status.get("note") != "note.md":
        raise ValueError("请先检查初版并使用 check --complete 标记完成")
    report_for(run, run / "note.md")
    alternates = status.setdefault("alternate_notes", [])
    if not isinstance(alternates, list) or any(not isinstance(item, dict) for item in alternates):
        raise ValueError("status.json 中的 alternate_notes 格式不正确")
    relative = str(note.relative_to(run))
    entry = next((item for item in alternates if item.get("path") == relative), None)
    if entry is None:
        entry = {"path": relative, "recorded_at": clock()}
        alternates.append(entry)
    entry.update({"checked_at": clock(), "quality_check": {"note_screenshots": result["note_screenshots"],
                                                              "warnings": result["warnings"]}})
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def codex_path() -> str:
    override = os.environ.get("AI_NOTES_CODEX_BIN")
    candidate = override or shutil.which("codex")
    if not candidate:
        raise FileNotFoundError("PATH 中找不到 Codex CLI；请自行准备或在 AI_NOTES_CODEX_BIN 中指定其路径")
    if not os.access(candidate, os.X_OK):
        raise PermissionError(f"Codex CLI 不可执行：{candidate}")
    return candidate


def finish(run: Path, instructions: str | None = None) -> dict:
    final = run / "note.md"
    if final.exists():
        status = load_json(run / "status.json")
        if status.get("state") == "completed":
            print(f"笔记已存在：{final}", flush=True)
            return report_for(run, final)
        raise FileExistsError("note.md 已存在但未标记完成；请先检查，确认后运行 check --complete")
    materials = inspect_materials(run)
    slides = load_json(run / "materials/capture/frames.json")["slides"]
    # Attach original frames so the model receives actual visual evidence, not
    # merely filenames. For large videos sample the attachments across the
    # whole timeline and require separate inspection of the remainder.
    attachment_limit = 24
    if len(slides) <= attachment_limit:
        indexes = list(range(len(slides)))
    else:
        indexes = [round(i * (len(slides) - 1) / (attachment_limit - 1)) for i in range(attachment_limit)]
    attachments = [(run / "materials/capture" / slides[i]["image"]).resolve() for i in indexes]
    executable = codex_path()
    attempt = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    draft = run / f"note-draft-{attempt}.md"
    logs = run / "logs"
    logs.mkdir(exist_ok=True)
    log_path, last_message = logs / f"codex-{attempt}.log", logs / f"codex-last-{attempt}.txt"
    prompt = (ROOT / "templates/note-prompt.md").read_text(encoding="utf-8")
    prompt += f"\n草稿文件：{draft.name}\n"
    if materials["warnings"]:
        prompt += "\n素材自动检查发现的待核查问题：\n- " + "\n- ".join(materials["warnings"]) + "\n"
    prompt += "\n本次作为图片附件提供的截图，按附件顺序排列（秒数为画面起点）：\n"
    prompt += "\n".join(f"{number}. {slides[i]['start']} 秒：materials/capture/{slides[i]['image']}"
                        for number, i in enumerate(indexes, 1)) + "\n"
    if len(attachments) < len(slides):
        prompt += (f"本次仅附上 {len(attachments)}/{len(slides)} 张截图，按时间均匀选取。"
                   "请使用图像查看工具核查其余与内容相关的截图；不能查看的部分，"
                   "应在笔记中明确说明，不要声称已经看过。\n")
    else:
        prompt += "以上附件覆盖全部截图；请根据实际画面检查，不要把字幕推测当成画面内容。\n"
    if instructions:
        prompt += f"\n用户对笔记的补充要求：\n{instructions}\n"
    command = [executable, "-a", "never", "exec", "--sandbox", "workspace-write",
               "--skip-git-repo-check", "--ephemeral", "--cd", str(run),
               "--output-last-message", str(last_message)]
    for image in attachments:
        command.extend(["--image", str(image)])
    command.extend(["--", "-"])
    print(f"正在调用本机 Codex 阅读转写，附带 {len(attachments)}/{len(slides)} 张截图并生成笔记……", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(prompt)
        process.stdin.close()
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(line, end="", file=sys.stderr, flush=True)
        code = process.wait()
    if code != 0:
        raise RuntimeError(f"Codex 成稿失败（退出码 {code}），详情见 {log_path}；素材已保留")
    report_for(run, draft)
    if final.exists():
        raise FileExistsError("生成期间 note.md 被创建，草稿已保留，未覆盖该文件")
    draft.rename(final)
    return complete(run, final, {"attached_screenshots": len(attachments), "total_screenshots": len(slides)})


def doctor() -> dict:
    checks = {"python": {"path": sys.executable, "version": sys.version.split()[0],
                         "ok": sys.version_info >= (3, 10)}}
    for name in ("ffmpeg", "ffprobe", "course2md"):
        path = shutil.which(name)
        checks[name] = {"path": path, "ok": bool(path)}
    try:
        codex = codex_path()
        login = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=15)
        checks["codex"] = {"path": codex, "ok": login.returncode == 0, "login_status": (login.stdout + login.stderr).strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        checks["codex"] = {"ok": False, "error": str(error)}
    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks,
            "requirements": str(ROOT / "requirements.md"),
            "codex_scope": "仅终端 run / finish 需要 Codex CLI；当前 Codex 会话的 prepare / check 不需要",
            "asr": "B站必剪在线服务；真正转写时上传音频，服务可用性未由 doctor 测试"}


def add_prepare_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("video", help="B 站链接或 BV 号")
    parser.add_argument("--page", type=int, help="分 P；默认使用链接中的 p，否则为 1")
    parser.add_argument("--resume", type=Path, help="恢复 runs/ 下已有运行目录")
    parser.add_argument("--roi", default="0%,0%-100%,85%", help="截图变化检测区域")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"run", "prepare", "finish", "check", "doctor"}
    if argv and argv[0] not in commands and not argv[0].startswith("-"):
        argv.insert(0, "run")
    parser = argparse.ArgumentParser(prog="ai-notes", description="在线 B 站视频 → 语音转写、课件截图和 Codex 图文笔记")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="完整处理视频并由 Codex 生成笔记")
    add_prepare_options(run_parser)
    run_parser.add_argument("--instructions", help="对最终笔记的补充要求")
    prep_parser = sub.add_parser("prepare", help="只准备转写和截图，交给当前 Codex 会话整理")
    add_prepare_options(prep_parser)
    finish_parser = sub.add_parser("finish", help="用 Codex 为已有素材生成笔记")
    finish_parser.add_argument("run_dir", help="runs/ 下的运行目录")
    finish_parser.add_argument("--instructions", help="对最终笔记的补充要求")
    check_parser = sub.add_parser("check", help="检查素材与笔记；--complete 同时记录结果")
    check_parser.add_argument("run_dir", help="runs/ 下的运行目录")
    check_parser.add_argument("--note", default="note.md", help="要检查的笔记，路径相对于运行目录；默认 note.md")
    check_parser.add_argument("--complete", action="store_true")
    sub.add_parser("doctor", help="检查本机工具和 Codex 登录")
    for item in (run_parser, prep_parser, finish_parser, check_parser, sub.choices["doctor"]):
        item.add_argument("--json", action="store_true", help="标准输出仅返回 JSON；进度写入标准错误")
    args = parser.parse_args(argv)
    try:
        with contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext():
            if args.command == "doctor":
                result = doctor()
            elif args.command in {"run", "prepare"}:
                if args.command == "run":
                    health = doctor()
                    if not health["ok"]:
                        failed = ", ".join(name for name, check in health["checks"].items() if not check["ok"])
                        raise RuntimeError(f"本机依赖或 Codex 登录检查未通过：{failed}；请运行 doctor 查看详情")
                run = prepare_video(args.video, args.page, args.resume, args.roi)
                result = finish(run, args.instructions) if args.command == "run" else report_for(run)
            elif args.command == "finish":
                result = finish(resolve_run(args.run_dir), args.instructions)
            else:
                run = resolve_run(args.run_dir)
                note = resolve_note(run, args.note)
                if args.complete:
                    result = complete(run, note) if note == run / "note.md" else complete_alternate(run, note)
                else:
                    result = report_for(run, note)
        if args.json:
            print(json.dumps({"ok": bool(result.get("ok", True)), "command": args.command, **result}, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok", True) else 1
    except (Exception, KeyboardInterrupt) as error:
        if args.json:
            print(json.dumps({"ok": False, "command": args.command, "error": str(error)}, ensure_ascii=False))
        else:
            print(f"处理失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
