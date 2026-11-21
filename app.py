
import os
import uuid
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
OUTPUTS = BASE / "outputs"

WORK.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

app = FastAPI(title="NeonClipping")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/outputs", StaticFiles(directory=str(OUTPUTS)), name="outputs")

JOBS = {}
LOCK = threading.Lock()
BUSY = False

ALLOWED = {".mp4", ".mov", ".mkv", ".webm"}
MAX_SIZE = 500 * 1024 * 1024


def run_command(args, timeout=900):
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:] or "FFmpeg error")

    return result.stdout.strip()


def get_duration(path):
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ], timeout=60)

    return float(result)


def process_job(job_id, source, count, min_len, max_len):
    global BUSY

    try:
        with LOCK:
            JOBS[job_id]["status"] = "processing"
            JOBS[job_id]["progress"] = 10
            JOBS[job_id]["message"] = "Video check ho rahi hai..."

        duration = get_duration(source)

        if duration <= 0:
            raise RuntimeError("Video duration read nahi ho paayi.")

        # Short video: make one complete clip.
        if duration <= min_len or duration <= max_len:
            clip_count = 1
            clip_length = duration
            starts = [0]
        else:
            clip_count = min(count, max(1, int(duration / min_len)))
            clip_length = min(max_len, max(min_len, duration / clip_count))
            clip_count = min(clip_count, count)

            if clip_count == 1:
                starts = [max(0, (duration - clip_length) / 2)]
            else:
                last_start = max(0, duration - clip_length)
                starts = [
                    last_start * i / (clip_count - 1)
                    for i in range(clip_count)
                ]

        job_output = OUTPUTS / job_id
        job_output.mkdir(parents=True, exist_ok=True)

        clips = []

        for index, start in enumerate(starts, start=1):
            filename = f"clip_{index}.mp4"
            destination = job_output / filename

            with LOCK:
                JOBS[job_id]["progress"] = 10 + int(
                    80 * (index - 1) / len(starts)
                )
                JOBS[job_id]["message"] = (
                    f"Clip {index}/{len(starts)} ban rahi hai..."
                )

            run_command([
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-i", str(source),
                "-t", str(min(clip_length, duration - start)),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-vf",
                "scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(destination)
            ], timeout=900)

            clips.append({
                "name": f"Clip {index}",
                "url": f"/outputs/{job_id}/{filename}"
            })

        with LOCK:
            JOBS[job_id] = {
                "status": "completed",
                "progress": 100,
                "message": "Clips ready hain!",
                "clips": clips,
                "error": None
            }

    except Exception as error:
        with LOCK:
            JOBS[job_id] = {
                "status": "error",
                "progress": 0,
                "message": "Clip banane mein error aayi.",
                "clips": [],
                "error": str(error)
            }

    finally:
        try:
            source.unlink(missing_ok=True)
        except Exception:
            pass

        with LOCK:
            BUSY = False


@app.get("/")
def home():
    return {"message": "NeonClipping API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/make")
async def make(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    count: int = Form(3),
    min_len: int = Form(20),
    max_len: int = Form(60),
    captions: bool = Form(True)
):
    global BUSY

    suffix = Path(video.filename or "").suffix.lower()

    if suffix not in ALLOWED:
        raise HTTPException(400, "MP4, MOV, MKV ya WEBM video upload karo.")

    if min_len < 1 or max_len < min_len:
        raise HTTPException(400, "Clip duration settings galat hain.")

    count = max(1, min(count, 10))

    with LOCK:
        if BUSY:
            raise HTTPException(
                429,
                "Ek video pehle se process ho rahi hai. Thoda wait karo."
            )
        BUSY = True

    job_id = uuid.uuid4().hex[:12]
    job_folder = WORK / job_id
    job_folder.mkdir(parents=True, exist_ok=True)
    source = job_folder / f"source{suffix}"

    try:
        size = 0

        with open(source, "wb") as output_file:
            while True:
                chunk = await video.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_SIZE:
                    raise HTTPException(413, "Maximum upload size 500 MB hai.")

                output_file.write(chunk)

    except Exception:
        source.unlink(missing_ok=True)
        with LOCK:
            BUSY = False
        raise

    finally:
        await video.close()

    with LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Video upload ho gayi. Processing shuru ho rahi hai...",
            "clips": [],
            "error": None
        }

    background_tasks.add_task(
        process_job,
        job_id,
        source,
        count,
        min_len,
        max_len
    )

    return {"job_id": job_id, "status": "queued"}


@app.get("/make/{job_id}")
def get_job(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)

        if not job:
            raise HTTPException(404, "Job nahi mili.")

        return job
        
