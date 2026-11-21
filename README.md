# NeonClipping backend (early MVP)

## Files
- `app.py`: FastAPI API used by the mobile website
- `requirements.txt`: Python packages
- `Dockerfile`: Docker Space deployment recipe

## API
- `POST /make` accepts `url`, `count`, `min_len`, `max_len`, `layout`, `mode`, `captions`
- `GET /make/{job_id}` returns status and clip URLs
- `GET /health` health check
- `GET /outputs/{job_id}/{clip}.mp4` generated MP4

## What this MVP does
1. Downloads public YouTube/Twitch/Kick videos (up to 2 hours, 720p source cap).
2. Transcribes speech with faster-whisper on CPU.
3. Selects non-overlapping speech-dense windows as candidate clips.
4. Renders 9:16 MP4 clips with subtitles and a blurred background (or full-screen crop).

This is not the full original NeonClipping desktop engine. The clip selector is a simple speech-density heuristic, not a trained viral-moment AI. Face/gameplay layouts, MP4 upload, persistent storage, authentication, and multi-user queueing are not implemented. It is intended for testing with videos the user has permission to process.

## Deploy
Create a **new Docker Space** on Hugging Face, then upload these three files to the root of that Space. Wait for the build to finish. Open the Space URL and confirm `/health` returns `{"status":"ok"}`. Use the Space URL in the website's Server connection box.

CPU processing can be slow and may run out of memory. Free hosted services can sleep or restart; job state and generated clips are temporary and may disappear after a restart. Do not assume unlimited free processing.
