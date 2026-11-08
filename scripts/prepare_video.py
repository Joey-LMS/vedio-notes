#!/usr/bin/env python3
"""Prepare a Bilibili video's audio, timed transcript, and slide screenshots."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS = (ROOT.parent.parent / "runs" if ROOT.parent.name == "skills"
                else ROOT.parent / f"{ROOT.name}-runs")
RUNS = Path(os.environ.get("AI_NOTES_RUNS_DIR") or DEFAULT_RUNS).expanduser().resolve()
STATE = RUNS.parent / ".online-video-notes-state"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
TZ = ZoneInfo("Asia/Shanghai")


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"PATH 中找不到 {name}；请查看 {ROOT / 'requirements.md'} 并自行准备依赖")
    return path


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def api(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as response:
        result = json.load(response)
    if result.get("code") != 0:
        raise RuntimeError(f"Bilibili API: {result.get('code')} {result.get('message')}")
    return result["data"]


def identify(value: str, explicit_page: int | None) -> tuple[str, int]:
    match = re.search(r"BV[0-9A-Za-z]{10}", value)
    if not match and "b23.tv/" in value:
        req = urllib.request.Request(value, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as response:
            value = response.url
        match = re.search(r"BV[0-9A-Za-z]{10}", value)
    if not match:
        raise ValueError("请提供包含 BV 号的 B 站视频链接或 BV 号")
    parameters = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
    page = explicit_page if explicit_page is not None else int(parameters.get("p", ["1"])[0])
    if page < 1:
        raise ValueError("分 P 序号必须大于零")
    return match.group(0), page


def new_run(bvid: str, page: int) -> Path:
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    base = RUNS
    base.mkdir(parents=True, exist_ok=True)
    for index in range(100):
        suffix = "" if index == 0 else f"-{index:02d}"
        path = base / f"{bvid}-P{page:02d}-{stamp}{suffix}"
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError("无法创建唯一运行目录")


def probe(path: Path) -> dict:
    result = subprocess.run(
        [require_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def download_video(bvid: str, page: int, folder: Path) -> dict:
    data = api("https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode({"bvid": bvid}))
    pages = data.get("pages") or []
    if page > len(pages):
        raise ValueError(f"视频共有 {len(pages)} 个分 P，无法选择 P{page}")
    selected = pages[page - 1]
    duration = float(selected["duration"])
    if duration > 7200:
        raise ValueError("当前在线转写流程支持单个分 P 最长 2 小时")
    query = urllib.parse.urlencode({"bvid": bvid, "cid": selected["cid"], "qn": 64, "fnval": 0, "platform": "html5"})
    media = api("https://api.bilibili.com/x/player/playurl?" + query)
    parts = media.get("durl") or []
    if len(parts) != 1 or not parts[0].get("url") or not parts[0].get("size"):
        raise RuntimeError("未获得单段、可校验的视频流；可能需要登录或此视频格式暂不支持")
    item = parts[0]
    expected_size = int(item["size"])
    input_dir = folder / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    source = {
        "source_url": f"https://www.bilibili.com/video/{bvid}/?p={page}", "bvid": bvid,
        "aid": data.get("aid"), "cid": int(selected["cid"]), "page": page,
        "page_count": len(pages), "title": data.get("title"), "part_title": selected.get("part"),
        "author": (data.get("owner") or {}).get("name"), "duration_seconds": duration,
        "published_at": datetime.fromtimestamp(data["pubdate"], TZ).isoformat(),
        "collected_at": now(), "media_quality": media.get("quality"), "media_format": media.get("format"),
        "media_expected_bytes": expected_size, "media_duration_ms": media.get("timelength"),
    }
    save_json(input_dir / "source.json", source)
    part_path = input_dir / "video.mp4.part"
    video = input_dir / "video.mp4"
    print(f"下载视频：{source['title']}，P{page}，约 {expected_size / 1048576:.1f} MiB", flush=True)
    with urllib.request.urlopen(urllib.request.Request(item["url"], headers=HEADERS), timeout=90) as response, part_path.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    size = part_path.stat().st_size
    if size != expected_size:
        raise RuntimeError(f"视频下载不完整：{size}/{expected_size} 字节；保留 .part 文件供排查")
    part_path.rename(video)
    return source


def validate_video(source: dict, video: Path) -> float:
    if not video.is_file() or video.stat().st_size != int(source["media_expected_bytes"]):
        raise RuntimeError("视频文件缺失或大小与来源记录不一致")
    info = probe(video)
    types = {x.get("codec_type") for x in info.get("streams", [])}
    if not {"video", "audio"}.issubset(types):
        raise RuntimeError("视频文件未包含可读取的视频和音频")
    seconds = float(info["format"]["duration"])
    expected = float(source.get("media_duration_ms", 0)) / 1000 or float(source["duration_seconds"])
    if abs(seconds - expected) > max(3, expected * 0.01):
        raise RuntimeError(f"视频时长不一致：{seconds:.1f}s vs {expected:.1f}s")
    return seconds


def srt_time(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds_i, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds_i:02d},{milliseconds:03d}"


def write_transcript_files(materials: Path, transcript: dict) -> None:
    srt, readable = [], []
    for index, segment in enumerate(transcript["segments"], 1):
        start, end = float(segment["start"]), float(segment["end"])
        text = str(segment["text"]).strip().replace("\n", " ")
        srt.append(f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
        readable.append(f"[{int(start // 60):02d}:{int(start % 60):02d}] {text}")
    (materials / "transcript.srt").write_text("\n".join(srt), encoding="utf-8")
    (materials / "transcript.txt").write_text("\n".join(readable) + "\n", encoding="utf-8")


def run_asr(command: list[str], run: Path) -> None:
    logs = run / "logs"
    logs.mkdir(exist_ok=True)
    log_path = logs / "bcut-asr.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n--- {now()} ---\n")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
        code = process.wait()
    if code:
        raise RuntimeError(f"必剪在线转写失败（退出码 {code}），详情见 {log_path}；已提交的任务可尝试用 --resume 继续")


def capture_slides(run: Path, video: Path, transcript: dict, roi: str) -> None:
    materials = run / "materials"
    capture = materials / "capture"
    if capture.exists():
        raise FileExistsError(f"截图结果已存在：{capture}；请创建新运行，避免覆盖")
    logs = run / "logs"
    logs.mkdir(exist_ok=True)
    # course2md uses filesystem operations unsupported by the exFAT volume.
    # Its temporary working files live in the system temporary directory and
    # are removed automatically; persistent results live outside the skill.
    with tempfile.TemporaryDirectory(prefix="ai-notes-work-") as temp:
        stage = Path(temp)
        staged_video = stage / "video.mp4"
        shutil.copyfile(video, staged_video)
        shutil.copyfile(materials / "transcript.srt", stage / "video.srt")
        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = str(STATE / "config")
        env["XDG_CACHE_HOME"] = str(STATE / "cache")
        env["QWEN3_CACHE_DIR"] = str(STATE / "cache")
        command = [require_tool("course2md"), str(staged_video), "--transcript-source", "subtitle",
                   "--no-llm", "--no-summarize", "--sample-interval", "1", "--cooldown", "5",
                   "--roi", roi, "--formats", "md,json", "-o", str(stage / "output"), "--json"]
        print("按画面变化提取课件截图并对齐文字", flush=True)
        completed = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        (logs / "course2md.jsonl").write_text(completed.stdout, encoding="utf-8")
        events = []
        for line in completed.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        done = next((e for e in reversed(events) if e.get("type") == "done"), None)
        if completed.returncode != 0 or not done or done.get("partial"):
            error = next((e.get("message") for e in reversed(events) if e.get("type") == "error"), None)
            raise RuntimeError(f"截图步骤未完整完成：{error or '请检查 logs/course2md.jsonl'}")
        out = Path(done["out_dir"])
        timeline = [json.loads(line) for line in (out / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        frames = [event for event in timeline if event.get("type") == "frame"]
        if not frames or any(not (out / frame["image"]).is_file() for frame in frames):
            raise RuntimeError("截图清单为空或文件缺失")
        capture.mkdir()
        shutil.copytree(out / "frames", capture / "frames", copy_function=shutil.copyfile)
        for name in ("course.md", "document.json", "manifest.json", "timeline.jsonl"):
            shutil.copyfile(out / name, capture / name)
        slide_index = []
        for index, frame in enumerate(frames):
            start = float(frame["t"])
            end = float(frames[index + 1]["t"]) if index + 1 < len(frames) else float(transcript["duration_seconds"])
            matching = [i for i, s in enumerate(transcript["segments"]) if float(s["start"]) < end and float(s["end"]) > start]
            slide_index.append({"start": start, "end": end, "image": frame["image"], "transcript_segment_indexes": matching})
        save_json(capture / "frames.json", {"method": "course2md slide detection", "roi": roi, "slides": slide_index})
        print(f"截图完成：{len(frames)} 张", flush=True)


def prepare_video(video: str, page: int | None = None, resume: Path | None = None,
                  roi: str = "0%,0%-100%,85%") -> Path:
    bvid, page = identify(video, page)
    run = resume.resolve() if resume else new_run(bvid, page)
    if not run.is_relative_to(RUNS) or not run.is_dir():
        raise ValueError(f"运行目录必须位于 {RUNS} 下")
    previous_path = run / "status.json"
    if resume and previous_path.is_file():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if previous.get("bvid") != bvid or int(previous.get("page", 0)) != page:
            raise ValueError("现有运行目录的视频或分 P 与请求不一致")
        if previous.get("state") == "completed" and (run / "note.md").is_file():
            print(f"该运行已完成：{run / 'note.md'}", flush=True)
            return run
    print(f"运行目录：{run}", flush=True)
    status = {"bvid": bvid, "page": page, "started_at": now(), "state": "running"}
    save_json(run / "status.json", status)
    try:
        # Fail before downloading or uploading media if this run needs a missing tool.
        require_tool("ffprobe")
        if not (run / "input" / "audio.m4a").is_file():
            require_tool("ffmpeg")
        if not (run / "materials" / "capture" / "frames.json").is_file():
            require_tool("course2md")
        metadata = run / "input" / "source.json"
        if metadata.exists():
            source = json.loads(metadata.read_text(encoding="utf-8"))
            if source.get("bvid") != bvid or int(source.get("page", 0)) != page:
                raise ValueError("现有运行目录的视频或分 P 与请求不一致")
        else:
            source = download_video(bvid, page, run)
        video = run / "input" / "video.mp4"
        duration = validate_video(source, video)
        if duration > 7200:
            raise ValueError("当前在线转写流程支持单个分 P 最长 2 小时")
        audio = run / "input" / "audio.m4a"
        if not audio.is_file():
            print("提取视频音频", flush=True)
            subprocess.run([require_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-n", "-i", str(video),
                            "-vn", "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", "16000", str(audio)], check=True)
        audio_duration = float(probe(audio)["format"]["duration"])
        if abs(audio_duration - duration) > max(3, duration * 0.01):
            raise RuntimeError("音频时长与视频不一致")
        materials = run / "materials"
        materials.mkdir(exist_ok=True)
        transcript_file = materials / "transcript.json"
        if not transcript_file.is_file():
            command = [sys.executable, str(ROOT / "scripts" / "bcut_asr.py"), "--bvid", bvid,
                       "--cid", str(source["cid"]), "--duration", str(duration), "--audio", str(audio),
                       "--output", str(materials)]
            prior_task = materials / "asr-task-audio-upload.json"
            if prior_task.is_file():
                command.extend(["--resume-task", str(prior_task)])
            run_asr(command, run)
        transcript = json.loads(transcript_file.read_text(encoding="utf-8"))
        if transcript.get("bvid") != bvid or int(transcript.get("cid", 0)) != int(source["cid"]) or not transcript.get("segments"):
            raise RuntimeError("转写结果与当前视频不匹配或为空")
        write_transcript_files(materials, transcript)
        if not (materials / "capture" / "frames.json").is_file():
            capture_slides(run, video, transcript, roi)
        last = float(transcript.get("last_speech_end_seconds", 0))
        warnings = []
        if duration - last > max(20, duration * 0.05):
            warnings.append(f"最后一段语音结束于 {last:.1f}s，距视频末尾 {duration-last:.1f}s；请检查尾部是否遗漏。")
        status.update({"state": "ready_for_codex", "finished_at": now(), "duration_seconds": duration,
                       "transcript_source": transcript["source"], "transcript_segments": len(transcript["segments"]),
                       "last_speech_end_seconds": last, "warnings": warnings})
        save_json(run / "status.json", status)
        print(f"素材已准备好：{run}\n请让 Codex 阅读素材并生成 note.md。", flush=True)
        return run
    except Exception as error:
        status.update({"state": "failed", "failed_at": now(), "error": str(error)})
        save_json(run / "status.json", status)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 B 站视频的转写与课件截图，交给 Codex 写笔记")
    parser.add_argument("video", help="B 站视频链接或 BV 号")
    parser.add_argument("--page", type=int, help="分 P 序号，默认读取链接中的 p，否则为 1")
    parser.add_argument("--resume", type=Path, help="恢复本工作区内已存在的运行目录")
    parser.add_argument("--roi", default="0%,0%-100%,85%", help="用于检测画面变化的区域，默认忽略底部字幕")
    args = parser.parse_args()
    prepare_video(args.video, args.page, args.resume, args.roi)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"准备失败：{exc}", file=sys.stderr)
        sys.exit(1)
