
import os
import json
import math
import uuid
import shutil
import asyncio
import socket
import ipaddress
import urllib.request
import urllib.error

from pathlib import Path
from urllib.parse import urlparse, unquote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "fast_uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)

CHUNK_SIZE = 4 * 1024 * 1024
MAX_SIZE = 500 * 1024 * 1024
ALLOWED = {".mp4", ".mov", ".mkv", ".webm"}

# Render environment variable:
# VIDEO_IMPORT_HOSTS=example.com,cdn.example.com
ALLOWED_IMPORT_HOSTS = {
    host.strip().lower()
    for host in os.getenv("VIDEO_IMPORT_HOSTS", "").split(",")
    if host.strip()
}


class UploadStart(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=MAX_SIZE)


class ImportURL(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


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


def validate_import_url(url):
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise HTTPException(400, "Sirf HTTPS video links allowed hain.")

    if not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "Invalid video URL.")

    host = parsed.hostname.lower()

    if host not in ALLOWED_IMPORT_HOSTS:
        raise HTTPException(
            403,
            "Ye video host approved nahi hai."
        )

    if parsed.port not in (None, 443):
        raise HTTPException(400, "Invalid URL port.")

    try:
        addresses = socket.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM
        )

        if not addresses:
            raise ValueError("No address")

        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])

            if not ip.is_global:
                raise ValueError("Private address")

    except Exception:
        raise HTTPException(400, "Video host safe nahi hai.")

    filename = Path(unquote(parsed.path)).name
    suffix = Path(filename).suffix.lower()

    if suffix not in ALLOWED:
        raise HTTPException(
            400,
            "Direct MP4, MOV, MKV ya WEBM link chahiye."
        )

    return host, filename, suffix


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_video(url, destination):
    opener = urllib.request.build_opener(NoRedirect)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NeonClipping/1.0"}
    )

    total = 0

    try:
        with opener.open(request, timeout=30) as response:

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if not (
                content_type.startswith("video/")
                or "application/octet-stream" in content_type
            ):
                raise ValueError("URL par video file nahi mili.")

            length = response.headers.get("Content-Length")

            if length and int(length) > MAX_SIZE:
                raise ValueError("Maximum video size 500 MB hai.")

            with open(destination, "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)

                    if not chunk:
                        break

                    total += len(chunk)

                    if total > MAX_SIZE:
                        raise ValueError(
                            "Maximum video size 500 MB hai."
                        )

                    output.write(chunk)

        if total == 0:
            raise ValueError("Empty video file.")

        return total

    except urllib.error.HTTPError as error:
        raise ValueError(
            f"Video server error: {error.code}"
        )

    except urllib.error.URLError:
        raise ValueError("Video link open nahi ho paaya.")


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
async def upload_chunk(upload_id: str, index: int, request: Request):
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
        if (folder / f"{index}.part").exists():
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
            "source": str(
                folder / (
                    "source" +
                    Path(meta["filename"]).suffix.lower()
                )
            )
        }

    missing = []

    for index in range(meta["total_chunks"]):
        if not (folder / f"{index}.part").exists():
            missing.append(index)

    if missing:
        raise HTTPException(
            409,
            detail={
                "message": "Chunks missing",
                "missing_chunks": missing
            }
        )

    suffix = Path(meta["filename"]).suffix.lower()
    destination = folder / ("source" + suffix)
    temp = folder / "source.tmp"

    try:
        with open(temp, "wb") as output:
            for index in range(meta["total_chunks"]):
                with open(folder / f"{index}.part", "rb") as source:
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


@router.post("/fast/import-url")
async def import_video_url(data: ImportURL):
    host, filename, suffix = validate_import_url(data.url)

    upload_id = uuid.uuid4().hex
    folder = UPLOADS / upload_id
    folder.mkdir(parents=True)

    destination = folder / ("source" + suffix)

    try:
        size = await asyncio.to_thread(
            download_video,
            data.url,
            destination
        )

        meta = {
            "filename": filename,
            "size": size,
            "chunk_size": CHUNK_SIZE,
            "total_chunks": 0,
            "status": "completed",
            "imported_from": host
        }

        save_meta(folder, meta)

        return {
            "status": "completed",
            "upload_id": upload_id,
            "filename": filename,
            "size": size
        }

    except Exception as error:
        shutil.rmtree(folder, ignore_errors=True)

        if isinstance(error, HTTPException):
            raise

        raise HTTPException(400, str(error))
        
