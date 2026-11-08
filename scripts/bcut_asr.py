#!/usr/bin/env python3
"""Transcribe one Bilibili part via the community-documented Bcut endpoint.

Protocol references: CZX2244/dsh-bilibili and SocialSisterYi/bcut-asr (MIT).
The endpoint is not an officially documented public API and can stop working
or rate-limit requests. File mode sends the audio to Bilibili for processing.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://member.bilibili.com/x/bcut/rubick-interface"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


def json_request(url: str, *, body: dict | None = None) -> dict:
    headers = dict(HEADERS)
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise RuntimeError("Bilibili API returned an invalid response")
    if result.get("code") != 0:
        raise RuntimeError(f"Bilibili API error: {result.get('code')} {result.get('message')}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Bilibili API returned no data object")
    return data


def upload_audio(path: Path) -> str:
    size = path.stat().st_size
    if size <= 0 or path.suffix.lower() not in {".m4a", ".wav", ".mp3", ".aac", ".flac"}:
        raise ValueError("Audio must be a non-empty m4a, wav, mp3, aac or flac file")
    info = json_request(API + "/resource/create", body={
        "type": 2, "name": path.name, "size": size,
        "ResourceFileType": path.suffix[1:].lower(), "model_id": "8",
    })
    urls, part_size = info.get("upload_urls"), int(info.get("per_size") or 0)
    if not isinstance(urls, list) or not urls or part_size <= 0:
        raise RuntimeError("Bcut did not return valid upload parts")
    if len(urls) != (size + part_size - 1) // part_size:
        raise RuntimeError("Bcut upload part count does not match audio size")
    if not all(info.get(k) for k in ("in_boss_key", "resource_id", "upload_id")):
        raise RuntimeError("Bcut returned incomplete upload information")
    etags = []
    print(f"Uploading audio to Bilibili: {size / 1048576:.1f} MiB in {len(urls)} part(s)", flush=True)
    with path.open("rb") as source:
        for index, url in enumerate(urls):
            data = source.read(part_size)
            if not data:
                raise RuntimeError("Audio ended before the last upload part")
            request = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/octet-stream"})
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=300) as response:
                        etag = response.headers.get("ETag")
                    if not etag:
                        raise RuntimeError("Bcut upload returned no ETag")
                    etags.append(etag)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code not in {408, 425, 429} and not 500 <= exc.code < 600:
                        raise RuntimeError(f"Audio upload part {index + 1} failed: HTTP {exc.code}") from None
                    if attempt == 2:
                        raise RuntimeError(f"Audio upload part {index + 1} failed after three attempts") from None
                    time.sleep(2 ** (attempt + 1))
                except (urllib.error.URLError, TimeoutError):
                    if attempt == 2:
                        raise RuntimeError(f"Audio upload part {index + 1} failed after three attempts") from None
                    time.sleep(2 ** (attempt + 1))
        if source.read(1):
            raise RuntimeError("Bcut upload parts did not cover the audio file")
    committed = json_request(API + "/resource/create/complete", body={
        "InBossKey": info["in_boss_key"], "ResourceId": info["resource_id"],
        "UploadId": info["upload_id"], "Etags": ",".join(etags), "model_id": "8",
    })
    resource = committed.get("download_url")
    if not resource:
        raise RuntimeError("Bcut upload completion returned no download URL")
    return resource


def main() -> None:
    parser = argparse.ArgumentParser(description="Bilibili online speech transcription")
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--cid", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audio", type=Path, help="Upload this audio file instead of passing a Bilibili stream URL")
    parser.add_argument("--resume-task", type=Path, help="Poll a previously submitted task instead of uploading again")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    task_file = args.resume_task
    if task_file:
        task_record = json.loads(task_file.read_text(encoding="utf-8"))
        if task_record.get("bvid") != args.bvid or int(task_record.get("cid", 0)) != args.cid:
            raise ValueError("The saved ASR task belongs to a different video part")
        if task_record.get("state") == "failed":
            raise RuntimeError("The saved ASR task failed; start a new run to submit another task")
        task_id = task_record["task_id"]
        mode = task_record["mode"]
        print("Resuming previously submitted ASR task", flush=True)
    elif args.audio:
        audio_url = upload_audio(args.audio)
        mode = "audio-upload"
    else:
        query = urllib.parse.urlencode({"bvid": args.bvid, "cid": args.cid, "qn": 32, "fnval": 16})
        info = json_request("https://api.bilibili.com/x/player/playurl?" + query)
        streams = (info.get("dash") or {}).get("audio") or []
        if not streams:
            raise RuntimeError("Bilibili did not provide an accessible audio stream")
        audio_url = streams[0].get("baseUrl") or streams[0].get("base_url")
        if not audio_url:
            raise RuntimeError("Bilibili audio stream has no URL")
        mode = "stream-url"
    if not args.resume_task:
        print("Submitting audio to Bilibili Bcut ASR", flush=True)
        # Do not retry creation after an unknown response: it could create duplicate jobs.
        task = json_request(API + "/task", body={"resource": audio_url, "model_id": "8"})
        task_id = task.get("task_id")
        if not task_id:
            raise RuntimeError("Bcut returned no task ID")
        task_file = args.output / f"asr-task-{mode}.json"
        task_record = {
            "provider": "bilibili-bcut-unofficial", "model_id": "8", "task_id": task_id,
            "mode": mode, "bvid": args.bvid, "cid": args.cid, "state": "running",
            "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        }
        task_file.write_text(json.dumps(task_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        query = urllib.parse.urlencode({"model_id": 7, "task_id": task_id})
        state = json_request(API + "/task/result?" + query)
        code = state.get("state")
        if code == 3:
            task_record["state"] = "failed"
            task_record["remark"] = state.get("remark") or "服务端未返回原因"
            task_file.write_text(json.dumps(task_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise RuntimeError("Bcut transcription failed: " + str(state.get("remark", "unknown error")))
        if code == 4:
            result = state.get("result")
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                raise RuntimeError("Bcut returned an invalid transcription result")
            segments = []
            for row in result.get("utterances", []):
                text = str(row.get("transcript", "")).strip()
                if not text:
                    continue
                start, end = float(row["start_time"]) / 1000, float(row["end_time"]) / 1000
                if start < 0 or end <= start or (args.duration is not None and end > args.duration + 2):
                    raise RuntimeError(f"Invalid ASR timestamps: {start}–{end}")
                segments.append({"start": start, "end": end, "text": text})
            if not segments:
                raise RuntimeError("Bcut completed but returned no speech segments")
            segments.sort(key=lambda row: row["start"])
            raw = args.output / "asr-raw.json"
            raw.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            payload = {
                "source": "bilibili-bcut-unofficial", "language": None,
                "language_note": "识别服务没有返回语言标签", "bvid": args.bvid,
                "cid": args.cid, "duration_seconds": args.duration,
                "last_speech_end_seconds": max(x["end"] for x in segments),
                "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "segments": segments,
            }
            (args.output / "transcript.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            task_record["state"] = "completed"
            task_record["completed_at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            task_file.write_text(json.dumps(task_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"Transcribed {len(segments)} segments; last speech ends at {payload['last_speech_end_seconds']:.1f}s", flush=True)
            return
        print(f"ASR task state: {code}", flush=True)
        time.sleep(5)
    raise TimeoutError(f"Bcut ASR timed out after {args.timeout}s; task ID recorded in asr-task-{mode}.json")


if __name__ == "__main__":
    main()
