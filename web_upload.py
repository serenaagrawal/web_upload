# web_upload.py  (Railway version)
#
# Environment variables (set them in Railway > Variables):
#   API_ID          Telegram API ID
#   API_HASH        Telegram API hash
#   BOT_TOKEN       bot token from @BotFather
#   CHANNEL_ID      -1001234567890  (or @channelusername)
#   KEY             secret password for the web page (choose a long random text)
#   GEMINI_API_KEY  free key from https://aistudio.google.com/apikey  (AI title/studio/description)
#   NIXPACKS_PKGS   ffmpeg   (installs ffmpeg + ffprobe for thumbnails)
#   PORT            set by Railway automatically
#
# Open:  https://<your-railway-domain>/?key=<KEY>
#
# Big files are sent from the browser in 16 MB pieces, so Railway's per-request
# time limit does not break large uploads.
import asyncio, base64, html, json, os, re, secrets, shutil, sys, urllib.request, uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

TMP_DIR       = Path("/tmp/web_tmp")
DONE_LOG      = Path("/tmp/upload_done.txt")
CAPTIONS_FILE = Path("captions.json")          # optional manual overrides, next to this file
EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
PORT = int(os.environ.get("PORT", "8000"))
DELAY = 3                      # seconds between Telegram uploads
WAIT_FOR_POST = 300            # seconds to wait for a test post if the channel is not known yet
MAX_BYTES = 2000 * 1024 * 1024 # Telegram limit for bots is about 2 GB

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

DEFAULTS = {
    "title": None, "year": None, "ep": None, "language": None,
    "studio": "", "description": "",
    "channels": ["@cinepopx", "@movies_hub"],
}

# ---------------------------------------------------------------- file name parsing
RE_SEASON_EP = re.compile(r"\bS(\d{1,2})\s?E(\d{1,3})\b", re.I)
RE_EP        = re.compile(r"\b(?:EP|E|Episode)\s?(\d{1,3})\b", re.I)
RE_YEAR      = re.compile(r"[\(\[]?\b((?:19|20)\d{2})\b[\)\]]?")
RE_RES       = re.compile(r"\b(2160p|4K|1440p|1080p|720p|480p|360p)\b", re.I)
RE_SOURCE    = re.compile(r"\b(WEB-?DL|WEB-?Rip|Blu-?Ray|BRRip|HDRip|HDTV|DVDRip|HEVC|x26[45]|H\.?26[45]|AAC|DDP?5\.1)\b", re.I)
RE_LANG      = re.compile(r"\b(Hindi|English|Tagalog|Filipino|Tamil|Telugu|Bengali|Korean|Japanese|Dual Audio|Multi Audio)\b", re.I)


def parse_name(stem: str) -> dict:
    s = re.sub(r"[_.]+", " ", stem)
    s = re.sub(r"\s+", " ", s).strip()
    info, cuts = {}, []

    m = RE_SEASON_EP.search(s)
    if m:
        info["ep"] = f"S{int(m.group(1)):02d} • EP {int(m.group(2)):02d}"
        cuts.append(m.start())
    else:
        m = RE_EP.search(s)
        if m:
            info["ep"] = f"EP {int(m.group(1)):02d}"
            cuts.append(m.start())

    for m in RE_YEAR.finditer(s):
        if m.start() > 0:
            info["year"] = m.group(1)
            cuts.append(m.start())
            break

    m = RE_RES.search(s)
    if m:
        cuts.append(m.start())

    m = RE_SOURCE.search(s)
    if m:
        cuts.append(m.start())

    langs = []
    for m in RE_LANG.finditer(s):
        langs.append(m.group(1).title())
        cuts.append(m.start())
    if langs:
        info["language"] = " + ".join(dict.fromkeys(langs))

    title = s[:min(cuts)] if cuts else s
    title = re.sub(r"[\(\[\-–—|]+\s*$", "", title).strip(" -–—|([")
    if title and (title.islower() or title.isupper()):
        title = title.title()
    info["title"] = title or s
    return info


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}".replace(".00", "")
        n /= 1024


def get_duration(p: Path) -> str:
    try:
        from hachoir.parser import createParser
        from hachoir.metadata import extractMetadata
        parser = createParser(str(p))
        if not parser:
            return ""
        with parser:
            md = extractMetadata(parser)
        if md and md.has("duration"):
            secs = int(md.get("duration").total_seconds())
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"
    except Exception:
        pass
    return ""


def build_caption(p: Path, meta: dict, runtime: str = "") -> str:
    detected = parse_name(p.stem)
    d = dict(DEFAULTS)
    d.update({k: v for k, v in detected.items() if v})
    d.update(meta)

    e = html.escape
    title = d.get("title") or p.stem
    year = d.get("year") or ""
    type_ = d.get("ep") or "Full Movie"
    desc = (d.get("description") or "").strip()
    runtime = runtime or get_duration(p)
    try:
        size = human_size(p.stat().st_size)
    except OSError:
        size = ""

    def render(desc_text):
        out = [f"🎬 <b>{e(title)}</b>" + (f" <b>({e(year)})</b>" if year else "")]
        if d.get("studio"):
            out.append(f"🏢 <b>Studio:</b> {e(d['studio'])}")
        if desc_text:
            out.append(f"📝 <b>Description:</b> {e(desc_text)}")
        out.append(f"📁 <b>Type:</b> {e(type_)}")
        if runtime:
            out.append(f"⏱ <b>Runtime:</b> {e(runtime)}")
        if size:
            out.append(f"💾 <b>Size:</b> {e(size)}")
        if d.get("channels"):
            out.append("🔗 <b>Join Us:</b>")
            out.extend(f"📢 {e(c)}" for c in d["channels"])
        return "\n".join(out)

    def plain_len(c):
        return len(re.sub(r"<[^>]+>", "", html.unescape(c)))

    text = render(desc)
    over = plain_len(text) - 1024          # Telegram caption limit
    if over > 0 and desc:
        keep = len(desc) - over - 1
        desc = desc[:keep].rstrip() + "…" if keep > 20 else ""
        text = render(desc)
    return text


# ---------------------------------------------------------------- video info, thumbnail, AI
async def _run(*cmd):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return proc.returncode, out.decode(errors="ignore")
    except FileNotFoundError:
        return 1, ""


async def probe_video(p: Path):
    """returns (duration_seconds, width, height); zeros if ffprobe is missing"""
    rc, out = await _run("ffprobe", "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=width,height:format=duration",
                         "-of", "json", str(p))
    try:
        j = json.loads(out)
        s = j["streams"][0]
        return int(float(j["format"]["duration"])), int(s["width"]), int(s["height"])
    except Exception:
        return 0, 0, 0


async def make_thumb(p: Path, duration: int):
    """grab one frame at about 10% of the video, save as small JPEG"""
    thumb = p.parent / "thumb.jpg"
    ss = max(1, int(duration * 0.10)) if duration else 5
    rc, _ = await _run("ffmpeg", "-y", "-ss", str(ss), "-i", str(p), "-frames:v", "1",
                       "-vf", "scale=320:-2", "-q:v", "5", str(thumb))
    return thumb if rc == 0 and thumb.exists() else None


def _gemini_call(filename: str, thumb) -> dict:
    prompt = (
        "You write info for a Telegram movie channel post.\n"
        f"File name: {filename}\n"
        "A frame from the video is attached (if any).\n"
        "Return JSON with these keys:\n"
        "title: clean movie or series title only (no year, resolution, codec or release tags)\n"
        "year: 4 digits, or '' if unknown\n"
        "studio: production company, studio or platform; '' if you are not sure\n"
        "description: 1 spoiler-free sentence, max 120 characters, in English. "
        "If you do not recognise the film, write a generic teaser from the title and the mood "
        "of the frame. Do not invent actors, characters or plot details.\n"
    )
    parts = [{"text": prompt}]
    if thumb:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(Path(thumb).read_bytes()).decode()}})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    return json.loads(res["candidates"][0]["content"]["parts"][0]["text"])


async def ai_meta(filename: str, thumb) -> dict:
    if not GEMINI_KEY:
        return {}
    for attempt in range(2):                      # one retry, then give up
        try:
            j = await asyncio.to_thread(_gemini_call, filename, thumb)
            out = {}
            for k in ("title", "year", "studio", "description"):
                v = str(j.get(k) or "").strip()
                if v:
                    out[k] = v
            desc = out.get("description", "")
            if len(desc) > 130:                   # keep it short even if the AI writes more
                out["description"] = desc[:127].rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"
            return out
        except Exception as ex:
            print(f"AI caption failed for {filename} (try {attempt + 1}/2): {ex}", flush=True)
            await asyncio.sleep(2)
    return {}      # AI failed: post with file name info only (title, type, runtime, size, channels)


# ---------------------------------------------------------------- web app
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
STATE = {"client": None, "target": None, "token": "", "queue": None, "captions": {}}
JOBS: dict = {}   # job_id -> {name, status, pct, error, path, size, got, next}

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Uploader</title>
<style>
 body{font-family:system-ui,sans-serif;background:#0f1115;color:#e8e8ea;margin:0;padding:20px}
 .wrap{max-width:720px;margin:0 auto}
 h1{font-size:20px;margin:0 0 14px}
 #drop{border:2px dashed #3a3f4b;border-radius:12px;padding:40px 16px;text-align:center;cursor:pointer;background:#161922}
 #drop.over{border-color:#4c8dff;background:#1a2030}
 #drop small{color:#8a8f9c}
 .row{background:#161922;border-radius:10px;padding:10px 12px;margin-top:10px}
 .row.err{outline:1px solid #c0392b}
 .n{font-size:14px;word-break:break-all}
 .bar{height:6px;background:#262a35;border-radius:3px;margin:8px 0 4px;overflow:hidden}
 .bar i{display:block;height:100%;width:0;background:#4c8dff;transition:width .2s}
 .row.done .bar i{background:#2ecc71}
 .s{font-size:12px;color:#8a8f9c}
</style></head><body><div class="wrap">
<h1>Upload to Telegram channel</h1>
<div id="drop">Tap or drop videos here<br><small>mp4, mkv, mov, avi, webm</small></div>
<input id="pick" type="file" accept="video/*,.mkv" multiple hidden>
<div id="list"></div>
</div>
<script>
const KEY="__KEY__", CHUNK=16*1024*1024;
const drop=document.getElementById('drop'), pick=document.getElementById('pick'), list=document.getElementById('list');
const rows={}; let chain=Promise.resolve();

drop.onclick=()=>pick.click();
pick.onchange=()=>{handle(pick.files); pick.value='';};
drop.ondragover=e=>{e.preventDefault(); drop.classList.add('over');};
drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault(); drop.classList.remove('over'); handle(e.dataTransfer.files);};

function addRow(name){
  const d=document.createElement('div'); d.className='row';
  d.innerHTML='<div class="n"></div><div class="bar"><i></i></div><div class="s">Waiting...</div>';
  d.querySelector('.n').textContent=name; list.prepend(d); return d;
}
function setRow(row,pct,text){ row.querySelector('i').style.width=pct+'%'; row.querySelector('.s').textContent=text; }

function handle(files){
  for(const f of files){
    const row=addRow(f.name);
    chain=chain.then(()=>send(f,row));   // one browser upload at a time
  }
}
function xhr(path,body,onprog){
  return new Promise((ok,fail)=>{
    const x=new XMLHttpRequest();
    x.open('POST',path+(path.includes('?')?'&':'?')+'key='+encodeURIComponent(KEY));
    if(onprog) x.upload.onprogress=onprog;
    x.onload=()=>{
      let j={}; try{j=JSON.parse(x.responseText)}catch(e){}
      if(x.status===200) ok(j); else fail(new Error(j.detail||('HTTP '+x.status)));
    };
    x.onerror=()=>fail(new Error('network error'));
    x.send(body);
  });
}
async function retry(fn){
  let last;
  for(let i=0;i<4;i++){
    try{ return await fn(); }catch(e){ last=e; await new Promise(r=>setTimeout(r,2000*(i+1))); }
  }
  throw last;
}
async function send(file,row){
  try{
    const s=await xhr('/upload/start?name='+encodeURIComponent(file.name)+'&size='+file.size);
    rows[s.id]=row;
    let idx=0;
    for(let off=0; off<file.size; off+=CHUNK, idx++){
      const blob=file.slice(off,off+CHUNK), i=idx, o=off;
      await retry(()=>xhr('/upload/chunk?id='+s.id+'&index='+i, blob, e=>{
        const p=(o+e.loaded)/file.size*100; setRow(row,p,'Sending to server '+p.toFixed(0)+'%');
      }));
    }
    await xhr('/upload/finish?id='+s.id);
    setRow(row,0,'Queued for Telegram');
  }catch(e){ setRow(row,0,'Error: '+e.message); row.classList.add('err'); }
}
async function poll(){
  try{
    const r=await fetch('/api/jobs?key='+encodeURIComponent(KEY)); const jobs=await r.json();
    for(const [id,j] of Object.entries(jobs)){
      const row=rows[id]; if(!row) continue;
      if(j.status==='done'){ setRow(row,100,'Done, posted to channel'); row.classList.add('done'); }
      else if(j.status==='error'){ setRow(row,0,'Error: '+j.error); row.classList.add('err'); }
      else if(j.status==='uploading'){ setRow(row,j.pct,'Uploading to Telegram '+j.pct.toFixed(0)+'%'); }
      else if(j.status!=='queued' && j.status!=='receiving'){ setRow(row,j.pct,j.status); }
    }
  }catch(e){}
  setTimeout(poll,2000);
}
poll();
</script></body></html>"""


def check(key: str):
    if not key or not secrets.compare_digest(key, STATE["token"]):
        raise HTTPException(status_code=403, detail="bad key")


def safe_name(name: str) -> str:
    name = Path(name or "video").name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "video.mp4"


@app.get("/", response_class=HTMLResponse)
async def index(key: str = ""):
    check(key)
    return PAGE.replace("__KEY__", key)


@app.get("/api/jobs")
async def api_jobs(key: str = ""):
    check(key)
    return {i: {"name": j["name"], "status": j["status"], "pct": j["pct"], "error": j["error"]}
            for i, j in JOBS.items()}


@app.post("/upload/start")
async def upload_start(key: str = "", name: str = "", size: int = 0):
    check(key)
    name = safe_name(name)
    if Path(name).suffix.lower() not in EXTS:
        raise HTTPException(status_code=400, detail="unsupported file type")
    if size <= 0 or size > MAX_BYTES:
        raise HTTPException(status_code=400, detail="file is empty or over 2 GB")

    job_id = uuid.uuid4().hex[:8]
    folder = TMP_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    JOBS[job_id] = {"name": name, "status": "receiving", "pct": 0.0, "error": "",
                    "path": folder / name, "size": size, "got": 0, "next": 0}
    return {"id": job_id}


@app.post("/upload/chunk")
async def upload_chunk(request: Request, key: str = "", id: str = "", index: int = 0):
    check(key)
    job = JOBS.get(id)
    if not job or job["status"] != "receiving":
        raise HTTPException(status_code=404, detail="unknown upload")
    if index < job["next"]:
        return {"ok": True}                      # chunk already stored (browser retry)
    if index != job["next"]:
        raise HTTPException(status_code=400, detail="chunk out of order")

    data = await request.body()
    if job["got"] + len(data) > job["size"]:
        raise HTTPException(status_code=400, detail="more data than announced")

    def write():
        with job["path"].open("ab") as f:
            f.write(data)
    await asyncio.to_thread(write)
    job["got"] += len(data)
    job["next"] += 1
    return {"ok": True}


@app.post("/upload/finish")
async def upload_finish(key: str = "", id: str = ""):
    check(key)
    job = JOBS.get(id)
    if not job or job["status"] != "receiving":
        raise HTTPException(status_code=404, detail="unknown upload")
    if job["got"] != job["size"]:
        job["status"], job["error"] = "error", "incomplete upload"
        shutil.rmtree(job["path"].parent, ignore_errors=True)
        raise HTTPException(status_code=400, detail="incomplete upload")
    job["status"] = "queued"
    await STATE["queue"].put(id)
    return {"ok": True}


# ---------------------------------------------------------------- telegram worker
async def worker():
    client, target, queue = STATE["client"], STATE["target"], STATE["queue"]
    while True:
        job_id = await queue.get()
        job = JOBS[job_id]
        p: Path = job["path"]
        job["status"] = "uploading"

        def cb(sent, total, job=job):
            job["pct"] = round(sent / total * 100, 1) if total else 0.0

        try:
            dur, w, h = await probe_video(p)
            runtime = ""
            if dur:
                hh, r = divmod(dur, 3600)
                mm, ss = divmod(r, 60)
                runtime = f"{hh}h {mm:02d}m" if hh else f"{mm}m {ss:02d}s"
            thumb = await make_thumb(p, dur)

            job["status"] = "Writing caption (AI)"
            meta = await ai_meta(p.name, thumb)
            if parse_name(p.stem).get("year"):
                meta.pop("year", None)                       # year from the file name wins
            meta.update(STATE["captions"].get(p.name, {}))   # captions.json, if present, wins
            caption = await asyncio.to_thread(build_caption, p, meta, runtime)
            job["status"] = "uploading"
            attrs = [DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)] if dur else None

            for attempt in range(5):
                try:
                    await client.send_file(target, p, caption=caption, parse_mode="html",
                                           thumb=str(thumb) if thumb else None,
                                           attributes=attrs,
                                           supports_streaming=True, progress_callback=cb)
                    job["status"], job["pct"] = "done", 100.0
                    DONE_LOG.parent.mkdir(parents=True, exist_ok=True)
                    with DONE_LOG.open("a", encoding="utf-8") as f:
                        f.write(p.name + "\n")
                    print(f"uploaded {p.name}", flush=True)
                    break
                except FloodWaitError as e:
                    job["status"] = f"Flood wait {e.seconds}s"
                    await asyncio.sleep(e.seconds + 1)
                    job["status"] = "uploading"
                except (ConnectionError, OSError, asyncio.TimeoutError):
                    job["status"] = f"Retry {attempt + 1}/5"
                    await asyncio.sleep(10 * (attempt + 1))
                    job["status"] = "uploading"
            else:
                job["status"], job["error"] = "error", "failed after 5 attempts"
        except Exception as e:
            job["status"], job["error"] = "error", str(e)[:200]
            print(f"FAILED {p.name}: {e}", flush=True)
        finally:
            shutil.rmtree(p.parent, ignore_errors=True)   # free disk space
        await asyncio.sleep(DELAY)


async def resolve_channel(client, channel):
    try:
        return await client.get_input_entity(channel)
    except Exception:
        pass
    if not isinstance(channel, int):
        return None
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    async def on_msg(event):
        if event.chat_id == channel and not fut.done():
            fut.set_result(await event.get_input_chat())

    client.add_event_handler(on_msg, events.NewMessage())
    print("Now post any short message in your channel (e.g. 'start')...", flush=True)
    try:
        return await asyncio.wait_for(fut, timeout=WAIT_FOR_POST)
    except asyncio.TimeoutError:
        return None
    finally:
        client.remove_event_handler(on_msg)


def need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Missing environment variable: {name}")
    return v


async def main():
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if CAPTIONS_FILE.exists():
        STATE["captions"] = json.loads(CAPTIONS_FILE.read_text(encoding="utf-8"))

    api_id   = int(need("API_ID"))
    api_hash = need("API_HASH")
    bot_tok  = need("BOT_TOKEN")
    raw_ch   = need("CHANNEL_ID")
    channel  = int(raw_ch) if re.fullmatch(r"-?\d+", raw_ch) else raw_ch
    key      = os.environ.get("KEY", "").strip() or secrets.token_urlsafe(16)

    client = TelegramClient(StringSession(), api_id, api_hash)   # memory only, no session file
    await client.start(bot_token=bot_tok)

    target = await resolve_channel(client, channel)
    if target is None:
        print("Channel not found. Make the bot an admin of the channel and check the ID (starts with -100).", flush=True)
        await client.disconnect()
        sys.exit(1)
    print("Channel found.", flush=True)

    STATE.update(client=client, target=target, token=key, queue=asyncio.Queue())
    asyncio.create_task(worker())

    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    print("=" * 60, flush=True)
    if domain:
        print(f"Open: https://{domain}/?key={key}", flush=True)
    else:
        print(f"Open: https://<your-railway-domain>/?key={key}", flush=True)
    if not os.environ.get("KEY"):
        print("KEY variable not set, so this key changes at every restart. Set KEY in Railway Variables.", flush=True)
    if not GEMINI_KEY:
        print("GEMINI_API_KEY not set, captions will use the file name only.", flush=True)
    print("=" * 60, flush=True)

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning"))
    try:
        await server.serve()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
