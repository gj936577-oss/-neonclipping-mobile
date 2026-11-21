import os, re, json, uuid, shutil, threading, subprocess, time
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

WORK = Path(os.getenv("WORK_DIR", "/tmp/neonclipping"))
OUTPUTS = WORK / "outputs"
WORK.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="NeonClipping API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST"], allow_headers=["*"])
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS)), name="outputs")

jobs, lock = {}, threading.Lock()
executor = ThreadPoolExecutor(max_workers=1)
recent_requests = {}
ALLOWED_HOSTS = {"youtube.com","www.youtube.com","youtu.be","m.youtube.com","twitch.tv","www.twitch.tv","kick.com","www.kick.com"}

class MakeRequest(BaseModel):
    url: str
    count: int = Field(4, ge=1, le=8)
    min_len: int = Field(30, ge=10, le=180)
    max_len: int = Field(60, ge=10, le=240)
    layout: str = "blur"
    mode: str = "fast"
    captions: str = "accurate"

def cmd(args, timeout=900):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Command failed")[-1500:])
    return p.stdout

def validate_url(value):
    p = urlparse(value)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http","https") or not any(host == h or host.endswith("."+h) for h in ALLOWED_HOSTS):
        raise HTTPException(400, "Sirf public YouTube, Twitch ya Kick links supported hain.")
    return value

def update(job, **kwargs):
    with lock:
        jobs[job].update(kwargs)

def select_windows(segments, duration, count, min_len, max_len):
    target = min(max_len, max(min_len, (min_len + max_len)//2))
    step = max(5, target//3)
    candidates, start = [], 0
    while start + min_len <= duration:
        end = min(duration, start + target)
        words = sum(len(s["text"].split()) for s in segments if min(end,s["end"]) > max(start,s["start"]))
        if words:
            candidates.append((words/max(1,end-start), start, end))
        start += step
    candidates.sort(reverse=True)
    chosen = []
    for score, a, b in candidates:
        if all(b <= x or a >= y for _,x,y in chosen):
            chosen.append((score,a,b))
            if len(chosen) >= count: break
    return sorted(chosen, key=lambda x:x[1]) or [(1,0,min(duration,target))]

def timestamp(sec):
    ms = int(max(0,sec)*1000)
    h,ms = divmod(ms,3600000); m,ms = divmod(ms,60000); s,ms = divmod(ms,1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def write_srt(segments, start, end, path):
    rows, n = [], 1
    for s in segments:
        a,b = max(start,s["start"]), min(end,s["end"])
        txt = re.sub(r"\s+"," ",s["text"]).strip()
        if b>a and txt:
            rows.append(f"{n}\n{timestamp(a-start)} --> {timestamp(b-start)}\n{txt}\n"); n+=1
    path.write_text("\n".join(rows), encoding="utf-8")

def process(job, data):
    folder = WORK/job
    folder.mkdir(parents=True, exist_ok=True)
    try:
        update(job,status="downloading",progress=5,message="Video download ho rahi hai…")
        cmd(["yt-dlp","--no-playlist","--no-warnings","--match-filter","duration <= 7200",
             "-f","best[height<=720][ext=mp4]/best[height<=720]/best",
             "--merge-output-format","mp4","-o",str(folder/"source.%(ext)s"),data["url"]], timeout=900)
        files = [p for p in folder.glob("source.*") if p.suffix.lower() in {".mp4",".mkv",".webm",".mov"}]
        if not files: raise RuntimeError("Video download nahi hui. Public link check karo.")
        source = files[0]
        probe = json.loads(cmd(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(source)],30))
        duration = float(probe["format"]["duration"])
        if duration > 7200: raise RuntimeError("2 ghante se lambi video abhi supported nahi hai.")

        update(job,status="transcribing",progress=25,message="Audio transcript ban raha hai…")
        from faster_whisper import WhisperModel
        model = WhisperModel("small" if data.get("captions") in ("accurate","best_hindi") else "base",
                             device="cpu", compute_type="int8")
        it, info = model.transcribe(str(source), beam_size=3, vad_filter=True)
        segments = [{"start":float(s.start),"end":float(s.end),"text":s.text.strip()} for s in it]
        del model
        if not segments: raise RuntimeError("Clear speech transcript nahi mil paya.")

        update(job,status="selecting",progress=55,message="Clip moments select ho rahe hain…")
        windows = select_windows(segments,duration,data["count"],data["min_len"],data["max_len"])
        links=[]
        update(job,status="rendering",progress=65,message="Vertical clips ban rahe hain…")
        for i,(_,start,end) in enumerate(windows,1):
            name=f"clip_{i:02}.mp4"
            outdir=OUTPUTS/job; outdir.mkdir(parents=True,exist_ok=True)
            out=outdir/name
            srt=folder/f"clip_{i:02}.srt"; write_srt(segments,start,end,srt)
            sub=str(srt).replace("\\","/").replace(":","\\:").replace("'","\\'")
            if data.get("layout")=="full":
                vf="scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280"
            else:
                vf=("split[bg][fg];[bg]scale=720:1280:force_original_aspect_ratio=increase,"
                    "crop=720:1280,boxblur=18:8[blur];[fg]scale=720:1280:"
                    "force_original_aspect_ratio=decrease[front];[blur][front]overlay=(W-w)/2:(H-h)/2")
            vf += f",subtitles='{sub}':force_style='FontName=DejaVu Sans,FontSize=18,Outline=2,Shadow=1,Alignment=2,MarginV=90'"
            cmd(["ffmpeg","-y","-ss",f"{start:.2f}","-i",str(source),"-t",f"{end-start:.2f}",
                 "-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","25",
                 "-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)],600)
            links.append({"name":name,"url":f"/outputs/{job}/{name}"})
            update(job,progress=65+int(30*i/max(1,len(windows))),clips=links,message=f"Clip {i}/{len(windows)} ready")
        update(job,status="done",progress=100,clips=links,message="Clips ready",
               language=getattr(info,"language",None))
    except Exception as e:
        update(job,status="error",progress=100,error=str(e)[:1200],message="Processing nahi ho payi.")
    finally:
        shutil.rmtree(folder,ignore_errors=True)

@app.get("/")
def home(): return {"name":"NeonClipping API","status":"online","docs":"/docs"}

@app.get("/health")
def health(): return {"status":"ok"}

@app.post("/make")
def make(data:MakeRequest, request:Request):
    validate_url(data.url)
    if data.max_len < data.min_len: raise HTTPException(400,"Max length min length se kam nahi ho sakti.")
    client=request.client.host if request.client else "unknown"
    now=time.time()
    if now-recent_requests.get(client,0)<20: raise HTTPException(429,"20 second baad try karo.")
    recent_requests[client]=now
    with lock:
        if sum(j["status"] not in ("done","error") for j in jobs.values())>=1:
            raise HTTPException(429,"Server busy hai. Thodi der baad try karo.")
        jid=uuid.uuid4().hex[:12]
        jobs[jid]={"job":jid,"status":"queued","progress":0,"message":"Queue mein hai","clips":[]}
    executor.submit(process,jid,data.model_dump())
    return {"job":jid,"status":"queued"}

@app.get("/make/{job_id}")
def get_job(job_id:str):
    with lock:
        if job_id not in jobs: raise HTTPException(404,"Job nahi mili ya server restart ho gaya.")
        result=dict(jobs[job_id])
    # Return absolute clip URLs so the existing mobile page can open them.
    base = os.getenv("PUBLIC_BASE_URL","").rstrip("/")
    for clip in result.get("clips",[]):
        if base and clip["url"].startswith("/"):
            clip["url"] = base + clip["url"]
    return result
