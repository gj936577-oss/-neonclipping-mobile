
import os
import uuid
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="NeonClipping API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
OUTPUTS = BASE / "outputs"

WORK.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

app.mount("/outputs", StaticFiles(directory=str(OUTPUTS)), name="outputs")

JOBS = {}
LOCK = threading.Lock()
BUSY = False

MAX_SIZE = 500 * 1024 * 1024
ALLOWED = {".mp4", ".mov", ".mkv", ".webm"}


def update(job, **kwargs):
    JOBS[job].update(kwargs)


def cmd(args, timeout=None):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout
    )

    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "Command failed")[-4000:]
        )

    return result.stdout.strip()


def process(job, video_path, settings):
    global BUSY

    folder = WORK / job
    folder.mkdir(exist_ok=True)

    try:
        update(
            job,
            status="transcribing",
            progress=10,
            message="Audio samjhi ja rahi hai..."
        )

        from faster_whisper import WhisperModel

        model = WhisperModel(
            "tiny",
            device="cpu",
            compute_type="int8"
        )

        segments, info = model.transcribe(
            str(video_path),
            word_timestamps=False
        )

        transcript = []

        for segment in segments:
            transcript.append({
                "start": float(segment.start),
                "end": float(segment.end),
                "text": segment.text.strip()
            })

        if not transcript:
            raise RuntimeError("Video se speech detect nahi hui.")

        update(
            job,
            status="selecting",
            progress=40,
            message="Clip moments select ho rahe hain..."
        )

        duration = max(item["end"] for item in transcript)

        min_len = max(10, int(settings["min_len"]))
        max_len = max(min_len, int(settings["max_len"]))
        count = max(1, min(10, int(settings["count"])))

        windows = []
        step = max(5, min_len // 2)
        start = 0

        while start < duration:
            end = min(duration, start + max_len)

            if end - start >= min_len:
                words = sum(
                    len(item["text"].split())
                    for item in transcript
                    if item["start"] < end and item["end"] > start
                )
                windows.append((words, start, end))

            start += step

        windows.sort(reverse=True)

        selected = []

        for words, start, end in windows:
            if words == 0:
                continue

            overlaps = any(
                not (end <= old_start or start >= old_end)
                for old_start, old_end in selected
            )

            if not overlaps:
                selected.append((start, end))

            if len(selected) >= count:
                break

        if not selected:
            selected = [(0, min(duration, max_len))]

        update(
            job,
            status="rendering",
            progress=55,
            message="Clips ban rahe hain..."
        )

        results = []

        for index, (start, end) in enumerate(selected, 1):
            output_name = f"{job}_{index}.mp4"
            output_path = OUTPUTS / output_name

            video_filter = (
                "scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280"
            )

            cmd([
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-i", str(video_path),
                "-t", str(end - start),
                "-vf", video_filter,
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "26",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output_path)
            ], timeout=600)

            results.append({
                "name": output_name,
                "url": f"/outputs/{output_name}",
                "start": round(start, 2),
                "end": round(end, 2)
            })

            update(
                job,
                progress=55 + int(40 * index / len(selected)),
                message=f"{index}/{len(selected)} clips ready..."
            )

        update(
            job,
            status="done",
            progress=100,
            message="Clips ready!",
            clips=results
        )

    except Exception as exc:
        update(
            job,
            status="error",
            progress=0,
            message="Processing failed",
            error=str(exc)[-3000:]
        )

    finally:
        with LOCK:
            BUSY = False


@app.get("/")
def home():
    return {
        "name": "NeonClipping API",
        "status": "online",
        "docs": "/docs"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/make")
async def make(
    video: UploadFile = File(...),
    count: int = Form(3),
    min_len: int = Form(20),
    max_len: int = Form(60),
    captions: bool = Form(True)
):
    global BUSY

    suffix = Path(video.filename or "").suffix.lower()

    if suffix not in ALLOWED:
        raise HTTPException(
            status_code=400,
            detail="MP4, MOV, MKV ya WEBM video upload karo."
        )

    with LOCK:
        if BUSY:
            raise HTTPException(
                status_code=429,
                detail="Ek video pehle se process ho rahi hai."
            )
        BUSY = True

    job = uuid.uuid4().hex[:12]
    folder = WORK / job
    folder.mkdir(parents=True, exist_ok=True)
    video_path = folder / f"source{suffix}"

    try:
        size = 0

        with open(video_path, "wb") as output:
            while True:
                chunk = await video.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="Video 500 MB se chhoti honi chahiye."
                    )

                output.write(chunk)

        if size == 0:
            raise HTTPException(
                status_code=400,
                detail="Video file khaali hai."
            )

        await video.close()

        JOBS[job] = {
            "status": "queued",
            "progress": 0,
            "message": "Job start ho rahi hai...",
            "clips": [],
            "error": None
        }

        settings = {
            "count": count,
            "min_len": min_len,
            "max_len": max_len,
            "captions": captions
        }

        thread = threading.Thread(
            target=process,
            args=(job, video_path, settings),
            daemon=True
        )
        thread.start()

        return {"job_id": job}

    except Exception:
        with LOCK:
            BUSY = False

        if folder.exists():
            import shutil
            shutil.rmtree(folder, ignore_errors=True)

        raise


@app.get("/make/{job_id}")
def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(
            status_code=404,
            detail="Job nahi mili."
        )

    return JOBS[job_id]
            
