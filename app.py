
import os
import uuid
import subprocess
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi import Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from fast_engine import router as fast_router
from fast_engine import finish_upload

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
OUTPUTS = BASE / "outputs"

WORK.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

app = FastAPI(title="NeonClipping")
app.include_router(fast_router)

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

# Controlled parallel processing
MAX_PARALLEL_CLIPS = 2


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


def create_clip(source, destination, start, length, job_id, index):
    started = time.perf_counter()

    run_command([
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-threads", "1",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(length),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase,"
        "crop=720:1280",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-threads:v", "1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(destination)
    ], timeout=900)

    elapsed = time.perf_counter() - started

    print(
        f"[{job_id}] Clip {index} completed "
        f"in {elapsed:.2f} seconds"
    )

    return {
        "name": f"Clip {index}",
        "url": f"/outputs/{job_id}/clip_{index}.mp4",
        "index": index
    }


def process_job(job_id, source, count, min_len, max_len):
    global BUSY

    try:
        with LOCK:
            JOBS[job_id]["status"] = "processing"
            JOBS[job_id]["progress"] = 5
            JOBS[job_id]["message"] = "Video check ho rahi hai..."

        started = time.perf_counter()

        duration = get_duration(source)

        if duration <= 0:
            raise RuntimeError("Video duration read nahi ho paayi.")

        if duration <= max_len:
            clip_count = 1
            clip_length = duration
            starts = [0]
        else:
            clip_count = min(
                count,
                max(1, int(duration / min_len))
            )

            clip_length = min(
                max_len,
                max(min_len, duration / clip_count)
            )

            if clip_count == 1:
                starts = [
                    max(0, (duration - clip_length) / 2)
                ]
            else:
                last_start = max(0, duration - clip_length)

                starts = [
                    last_start * i / (clip_count - 1)
                    for i in range(clip_count)
                ]

        job_output = OUTPUTS / job_id
        job_output.mkdir(parents=True, exist_ok=True)

        tasks = {}

        for index, start in enumerate(starts, start=1):
            length = min(clip_length, duration - start)

            destination = job_output / f"clip_{index}.mp4"

            tasks[index] = (start, length, destination)

        completed_clips = []

        with ThreadPoolExecutor(
            max_workers=min(MAX_PARALLEL_CLIPS, len(tasks))
        ) as executor:

            futures = {}

            for index, (start, length, destination) in tasks.items():
                future = executor.submit(
                    create_clip,
                    source,
                    destination,
                    start,
                    length,
                    job_id,
                    index
                )

                futures[future] = index

            completed = 0

            for future in as_completed(futures):
                clip = future.result()

                completed_clips.append(clip)
                completed += 1

                progress = 5 + int(
                    90 * completed / len(tasks)
                )

                with LOCK:
                    JOBS[job_id]["progress"] = progress
                    JOBS[job_id]["message"] = (
                        f"{completed}/{len(tasks)} clips ready ho gayi..."
                    )

        completed_clips.sort(key=lambda clip: clip["index"])

        clips = [
            {
                "name": clip["name"],
                "url": clip["url"]
            }
            for clip in completed_clips
        ]

        elapsed = time.perf_counter() - started

        with LOCK:
            JOBS[job_id] = {
                "status": "completed",
                "progress": 100,
                "message": "Clips ready hain!",
                "clips": clips,
                "error": None
            }

        print(
            f"[{job_id}] All clips completed "
            f"in {elapsed:.2f} seconds"
        )

    except Exception as error:
        print(f"[{job_id}] ERROR: {error}")

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


@app.post("/fast/{upload_id}/generate")
async def generate_fast(
    upload_id: str,
    background_tasks: BackgroundTasks,
    count: int = Query(3, ge=1, le=10),
    min_len: int = Query(20, ge=1),
    max_len: int = Query(60, ge=1)
):
    global BUSY

    if max_len < min_len:
        raise HTTPException(400, "Clip duration settings galat hain.")

    with LOCK:
        if BUSY:
            raise HTTPException(
                429,
                "Ek video pehle se process ho rahi hai. Thoda wait karo."
            )
        BUSY = True

    try:
        result = await finish_upload(upload_id)

        if result.get("status") != "completed":
            raise HTTPException(400, "Upload complete nahi hui.")

        source = Path(result["source"])

        if not source.is_file():
            raise HTTPException(404, "Uploaded video nahi mili.")

        job_id = uuid.uuid4().hex[:12]

        with LOCK:
            JOBS[job_id] = {
                "status": "queued",
                "progress": 0,
                "message": "Upload complete. Processing shuru ho rahi hai...",
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

        return {
            "job_id": job_id,
            "status": "queued"
        }

    except Exception:
        with LOCK:
            BUSY = False
        raise


@app.post("/make")
async def make(
    request: Request,
    background_tasks: BackgroundTasks,
    filename: str = Query(...),
    count: int = Query(3),
    min_len: int = Query(20),
    max_len: int = Query(60)
):
    global BUSY

    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED:
        raise HTTPException(
            400,
            "MP4, MOV, MKV ya WEBM video upload karo."
        )

    if min_len < 1 or max_len < min_len:
        raise HTTPException(400, "Clip duration settings galat hain.")

    count = max(1, min(count, 10))

    content_length = request.headers.get("content-length")

    if content_length:
        if int(content_length) > MAX_SIZE:
            raise HTTPException(413, "Maximum upload size 500 MB hai.")

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
            async for chunk in request.stream():
                if not chunk:
                    continue

                size += len(chunk)

                if size > MAX_SIZE:
                    raise HTTPException(
                        413,
                        "Maximum upload size 500 MB hai."
                    )

                output_file.write(chunk)

        if size == 0:
            raise HTTPException(400, "Empty video upload hui hai.")

    except Exception:
        source.unlink(missing_ok=True)

        try:
            job_folder.rmdir()
        except OSError:
            pass

        with LOCK:
            BUSY = False

        raise

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
        
