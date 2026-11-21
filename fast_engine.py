
import os
import json
import math
import uuid
import shutil
import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "fast_uploads"

UPLOADS.mkdir(parents=True, exist_ok=True)

CHUNK_SIZE = 4 * 1024 * 1024
MAX_SIZE = 500 * 1024 * 1024

ALLOWED = {".mp4", ".mov", ".mkv", ".webm"}


class UploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=MAX_SIZE)


def session_dir(upload_id):
    if not upload_id.isalnum() or len(upload_id) != 32:
        raise HTTPException(400, "Invalid upload ID")

    folder = UPLOADS / upload_id

    if not folder.is_dir():
        raise HTTPException(404, "Upload session nahi mili")

    return folder


def read_meta(folder):
    path = folder / "meta.json"

    if not path.exists():
        raise HTTPException(404, "Upload session nahi mili")

    try:
        return json.loads(path.read_text())
    except Exception:
        raise HTTPException(500, "Upload metadata error")


def save_meta(folder, data):
    temp = folder / "meta.tmp"
    temp.write_text(json.dumps(data))
    os.replace(temp, folder / "meta.json")


@router.post("/fast/start")
async def start_upload(data: UploadStart):
    suffix = Path(data.filename).suffix.lower()

    if suffix not in ALLOWED:
        raise HTTPException(400, "Unsupported video format")

    upload_id = uuid.uuid4().hex
    folder = UPLOADS / upload_id
    folder.mkdir(parents=True)

    total_chunks = math.ceil(data.size / CHUNK_SIZE)

    meta = {
        "filename": Path(data.filename).name,
        "size": data.size,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total_chunks,
        "status": "uploading"
    }

    save_meta(folder, meta)

    return {
        "upload_id": upload_id,
        "chunk_size": CHUNK_SIZE,
        "total_chunks": total_chunks,
        "status": "uploading"
    }


@router.put("/fast/{upload_id}/chunks/{index}")
async def upload_chunk(
    upload_id: str,
    index: int,
    request: Request
):
    folder = session_dir(upload_id)
    meta = read_meta(folder)

    if meta["status"] != "uploading":
        raise HTTPException(409, "Upload already finalized")

    if index < 0 or index >= meta["total_chunks"]:
        raise HTTPException(400, "Invalid chunk number")

    expected = min(
        meta["chunk_size"],
        meta["size"] - index * meta["chunk_size"]
    )

    content_length = request.headers.get("content-length")

    if content_length and int(content_length) != expected:
        raise HTTPException(400, "Incorrect chunk size")

    temp = folder / f"{index}.tmp"
    destination = folder / f"{index}.part"

    received = 0

    try:
        with open(temp, "wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue

                received += len(chunk)

                if received > expected:
                    raise HTTPException(413, "Chunk too large")

                await asyncio.to_thread(output.write, chunk)

        if received != expected:
            raise HTTPException(400, "Incomplete chunk")

        os.replace(temp, destination)

    except Exception:
        temp.unlink(missing_ok=True)
        raise

    return {
        "chunk": index,
        "received": received,
        "status": "saved"
    }


@router.get("/fast/{upload_id}/status")
async def upload_status(upload_id: str):
    folder = session_dir(upload_id)
    meta = read_meta(folder)

    received = []

    for index in range(meta["total_chunks"]):
        part = folder / f"{index}.part"
        if part.exists():
            received.append(index)

    missing = [
        i for i in range(meta["total_chunks"])
        if i not in received
    ]

    uploaded_bytes = sum(
        min(
            meta["chunk_size"],
            meta["size"] - i * meta["chunk_size"]
        )
        for i in received
    )

    return {
        "status": meta["status"],
        "uploaded_bytes": uploaded_bytes,
        "total_bytes": meta["size"],
        "received_chunks": received,
        "missing_chunks": missing,
        "total_chunks": meta["total_chunks"]
    }


@router.post("/fast/{upload_id}/finish")
async def finish_upload(upload_id: str):
    folder = session_dir(upload_id)
    meta = read_meta(folder)

    if meta["status"] == "completed":
        return {
            "status": "completed",
            "source": str(folder / ("source" + Path(meta["filename"]).suffix.lower()))
        }

    missing = []

    for index in range(meta["total_chunks"]):
        if not (folder / f"{index}.part").exists():
            missing.append(index)

    if missing:
        raise HTTPException(
            409,
            detail={"message": "Chunks missing", "missing_chunks": missing}
        )

    suffix = Path(meta["filename"]).suffix.lower()
    destination = folder / ("source" + suffix)
    temp = folder / "source.tmp"

    try:
        with open(temp, "wb") as output:
            for index in range(meta["total_chunks"]):
                part = folder / f"{index}.part"

                with open(part, "rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)

        if temp.stat().st_size != meta["size"]:
            raise RuntimeError("Final file size mismatch")

        os.replace(temp, destination)

        meta["status"] = "completed"
        save_meta(folder, meta)

        for index in range(meta["total_chunks"]):
            (folder / f"{index}.part").unlink(missing_ok=True)

        return {
            "status": "completed",
            "filename": meta["filename"],
            "size": meta["size"],
            "source": str(destination)
        }

    except Exception as error:
        temp.unlink(missing_ok=True)
        raise HTTPException(500, str(error))
