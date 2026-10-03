#!/usr/bin/env python3
"""
Competitor Hunter library: bookmarks, folders, and the local analysis pipeline for saved posts.

    python3 library.py analyze --post <piece id> --mode transcribe|watch|both|ffmpeg

Stages (all local, all free, same recipe as the watchvideo skill):
    fetch       yt-dlp pulls the post at <=480p into library/<id>/media.mp4
    transcribe  ffmpeg -> 16 kHz wav -> Whisper (small.en under 10 min, base.en above) -> transcript.json
                spoken hook = everything said in the first 8 seconds
    frames      ffmpeg tiles hook.jpg (first 20 s, 1 fps) and sheet-NN.jpg (whole video, <= 8 sheets)
    breakdown   headless Claude Code on Fable reads post.json, transcript.json and the sheets, writes breakdown.md
                (skipped in ffmpeg mode: that one only downloads and cuts frames)

Progress lands in library/<id>/status.json for the page to poll. Zero dependencies beyond yt-dlp, ffmpeg, openai-whisper.
"""
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
LIB = APP / "library"
INDEX = LIB / "library.json"
RUNS = APP / "runs"
PROMPT = APP / "engine" / "breakdown.prompt.txt"
HOOK_SECONDS = 8
HOOK_SHEET_SECONDS = 20
MAX_SHEETS = 8
MODES = ("fetch", "transcribe", "watch", "both", "ffmpeg")   # fetch = pull the media only (play it in the app)
DOWNLOADS = Path.home() / "Downloads"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hunt  # noqa: E402  (env_value for the Apify token, read_json of run items)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


# ----------------------------------------------------------------------------
# The index: folders and saved posts
# ----------------------------------------------------------------------------
def load():
    return read_json(INDEX) if INDEX.exists() else {"folders": [], "posts": []}


def store(lib):
    LIB.mkdir(exist_ok=True)
    write_json(INDEX, lib)
    return lib


def post_dir(post_id):
    if not re.fullmatch(r"[\w-]+", post_id):
        raise ValueError("bad post id")
    return LIB / post_id


def add_folder(name):
    name = str(name).strip()[:60]
    if not name:
        raise ValueError("A folder needs a name.")
    lib = load()
    if any(f["name"].lower() == name.lower() for f in lib["folders"]):
        raise ValueError("A folder called %s already exists." % name)
    lib["folders"].append({"id": uuid.uuid4().hex[:8], "name": name, "created": now()})
    return store(lib)


def rename_folder(folder_id, name):
    name = str(name).strip()[:60]
    lib = load()
    folder = next((f for f in lib["folders"] if f["id"] == folder_id), None)
    if not folder or not name:
        raise ValueError("No such folder, or empty name.")
    folder["name"] = name
    return store(lib)


def delete_folder(folder_id):
    lib = load()
    lib["folders"] = [f for f in lib["folders"] if f["id"] != folder_id]
    for p in lib["posts"]:
        if p["folder"] == folder_id:
            p["folder"] = None          # the posts stay saved, just unfiled
    return store(lib)


def save_post(piece_id, run_id, folder_id):
    if not re.fullmatch(r"[\w-]+", str(run_id)) or not (RUNS / run_id / "result.json").exists():
        raise ValueError("Unknown hunt run.")
    lib = load()
    if folder_id and not any(f["id"] == folder_id for f in lib["folders"]):
        raise ValueError("No such folder.")
    existing = next((p for p in lib["posts"] if p["id"] == piece_id), None)
    if existing:
        existing["folder"] = folder_id
        return store(lib)
    result = read_json(RUNS / run_id / "result.json")
    piece = next((p for p in result["pieces"] if p["id"] == piece_id), None)
    if not piece:
        raise ValueError("That piece is not in the hunt's ranked list.")
    item = next((i for i in read_json(RUNS / run_id / "items.json") if i["id"] == piece_id), {})
    lib["posts"].append({
        "id": piece_id, "folder": folder_id, "saved_at": now(), "run_id": run_id,
        "piece": {**piece, "description": item.get("description", "")}, "analysis": {},
    })
    return store(lib)


def move_post(post_id, folder_id):
    lib = load()
    post = next((p for p in lib["posts"] if p["id"] == post_id), None)
    if not post:
        raise ValueError("Not saved.")
    if folder_id and not any(f["id"] == folder_id for f in lib["folders"]):
        raise ValueError("No such folder.")
    post["folder"] = folder_id
    return store(lib)


def remove_post(post_id):
    lib = load()
    lib["posts"] = [p for p in lib["posts"] if p["id"] != post_id]
    folder = post_dir(post_id)
    if folder.exists():
        shutil.rmtree(folder)
    return store(lib)


# ----------------------------------------------------------------------------
# Analysis pipeline
# ----------------------------------------------------------------------------
def status(folder, stage, msg, **extra):
    write_json(folder / "status.json", {"stage": stage, "msg": msg, "at": now(), **extra})
    print("[%s] %s" % (stage, msg), flush=True)


def run(cmd, timeout=900):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[-400:] or "%s failed" % cmd[0])
    return out.stdout


def probe(media):
    raw = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-show_entries",
               "format=duration", "-of", "json", str(media)])
    info = json.loads(raw)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    return {"duration": float(info["format"].get("duration") or 0), "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0), "audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def fetch(folder, url):
    media = folder / "media.mp4"
    if media.exists():
        return media
    status(folder, "fetch", "Downloading the post")
    run(["yt-dlp", "-q", "--no-warnings", "--socket-timeout", "30", "--retries", "3",
         "-f", "bv*[height<=480]+ba/b[height<=480]/b", "--merge-output-format", "mp4",
         "-o", str(folder / "media.%(ext)s"), url], timeout=600)
    got = next((p for p in folder.glob("media.*") if p.suffix != ".tmp"), None)
    if not got:
        raise RuntimeError("yt-dlp returned no file")
    if got.suffix != ".mp4":
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(got), "-c", "copy", str(media)])
        got.unlink()
    return media


def is_carousel(post):
    return post["piece"]["kind"] == "static"


def slide_files(folder):
    return sorted(p.name for p in folder.glob("slide-*.jpg"))


def pull_slides(folder, post):
    """Carousel slides. The hunt's items.json holds the signed CDN links; when those have expired, one Apify
    call on the post itself ($0.003) returns fresh ones."""
    have = slide_files(folder)
    if have:
        return have
    status(folder, "fetch", "Pulling the carousel slides")
    items = hunt.read_json(RUNS / post["run_id"] / "items.json")
    urls = next((i.get("images") or [] for i in items if i["id"] == post["id"]), [])
    if not urls or not fetch_image(urls[0], folder / "slide-01.jpg"):
        rows = hunt.http_json(
            hunt.APIFY_API + "acts/%s/run-sync-get-dataset-items?timeout=100" % hunt.IG_ACTOR,
            headers={"Authorization": "Bearer " + hunt.env_value("APIFY_API_TOKEN")},
            body={"directUrls": [post["piece"]["url"]], "resultsType": "posts", "resultsLimit": 1, "addParentData": False},
            timeout=130)
        urls = (rows[0].get("images") if rows else None) or ([rows[0]["displayUrl"]] if rows and rows[0].get("displayUrl") else [])
    if not urls:
        raise RuntimeError("No slides came back for this post")
    for i, url in enumerate(urls, 1):
        target = folder / ("slide-%02d.jpg" % i)
        if not target.exists() and not fetch_image(url, target):
            raise RuntimeError("Slide %d would not download" % i)
    return slide_files(folder)


def fetch_image(url, target):
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        data = urllib.request.urlopen(req, timeout=20).read()
        if len(data) < 2000:
            return False
        target.write_bytes(data)
        return True
    except Exception:
        return False


def slug(text):
    text = re.sub(r"[^\w\s-]", "", str(text)).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)[:60] or "carousel"


def download_slides(post_id):
    """Copy a saved carousel's slides into ~/Downloads/<carousel name>/01.jpg, 02.jpg, ..."""
    lib = load()
    post = next((p for p in lib["posts"] if p["id"] == post_id), None)
    if not post:
        raise ValueError("Not saved.")
    folder = post_dir(post_id)
    slides = slide_files(folder) if folder.exists() else []
    if not slides:
        raise ValueError("No slides pulled yet. Fetch the post first.")
    name = slug(post["piece"]["title"]) or post_id
    target = DOWNLOADS / name
    target.mkdir(parents=True, exist_ok=True)
    for i, slide in enumerate(slides, 1):
        shutil.copyfile(folder / slide, target / ("%02d.jpg" % i))
    return str(target), len(slides)


def transcribe(folder, media, info):
    out = folder / "transcript.json"
    if out.exists():
        return read_json(out)
    if not info["audio"]:
        data = {"text": "", "segments": [], "hook": "", "model": None, "silent": True}
        write_json(out, data)
        return data
    status(folder, "transcribe", "Pulling audio")
    wav = folder / "audio.wav"
    run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
    model_name = "small.en" if info["duration"] <= 600 else "base.en"
    status(folder, "transcribe", "Whisper %s is listening (%d s of audio)" % (model_name, info["duration"]))
    import whisper  # slow import, only when needed
    model = whisper.load_model(model_name)
    result = model.transcribe(str(wav), fp16=False, language="en", verbose=False)
    segments = [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()} for s in result["segments"]]
    hook = " ".join(s["text"] for s in segments if s["start"] < HOOK_SECONDS) or (segments[0]["text"] if segments else "")
    data = {"text": result["text"].strip(), "segments": segments, "hook": hook.strip(), "model": model_name, "silent": not segments}
    write_json(out, data)
    wav.unlink(missing_ok=True)
    return data


def frames(folder, media, info):
    vertical = info["height"] > info["width"]
    scale, hook_tile, tile = ("250", "10x2", "10x3") if vertical else ("304", "5x4", "6x5")
    hook_sheet = folder / "hook.jpg"
    if not hook_sheet.exists():
        status(folder, "frames", "Cutting the hook sheet (first %d s)" % HOOK_SHEET_SECONDS)
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-t", str(HOOK_SHEET_SECONDS), "-i", str(media),
             "-vf", "fps=1,scale=%s:-1,tile=%s" % (scale, hook_tile), "-frames:v", "1", "-q:v", "4", str(hook_sheet)])
    if not list(folder.glob("sheet-*.jpg")):
        interval = max(3, math.ceil(info["duration"] / (MAX_SHEETS * 30)))   # whole video, never more than MAX_SHEETS sheets
        status(folder, "frames", "Cutting the full sheets (1 frame every %d s)" % interval)
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(media),
             "-vf", "fps=1/%d,scale=%s:-1,tile=%s" % (interval, scale, tile), "-q:v", "4", str(folder / "sheet-%02d.jpg")])
    return ["hook.jpg"] + sorted(p.name for p in folder.glob("sheet-*.jpg"))


def breakdown(folder, post, transcript, sheets):
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("Claude Code is not on PATH, so nobody can write the breakdown.")
    status(folder, "breakdown", "Fable is reading the post and writing why it worked")
    write_json(folder / "post.json", {
        "title": post["piece"]["title"], "platform": post["piece"]["platform"], "format": post["piece"]["format"],
        "competitor": post["piece"]["competitor"], "url": post["piece"]["url"], "caption": post["piece"]["description"],
        "numbers": {k: post["piece"].get(k) for k in ("views", "likes", "comments", "er", "breakout", "score", "demand", "fit", "age_days", "duration_s")},
        "jev_verdict": post["piece"].get("jev"), "has_transcript": bool(transcript and not transcript.get("silent")),
        "sheets": sheets, "hook_sheet_covers_seconds": HOOK_SHEET_SECONDS, "slides": slide_files(folder),
    })
    out = subprocess.run([
        claude, "-p", PROMPT.read_text(encoding="utf-8").replace("{CREATOR}", hunt.creator_profile()), "--model", hunt.settings()["model"], "--output-format", "json",
        "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
        "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write",
    ], cwd=str(folder), capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    if out.returncode != 0 or not (folder / "breakdown.md").exists():
        raise RuntimeError("Fable did not deliver breakdown.md: %s" % (out.stderr or out.stdout).strip()[-300:])
    try:
        return json.loads(out.stdout).get("total_cost_usd")
    except ValueError:
        return None


def analyze(post_id, mode):
    if mode not in MODES:
        raise ValueError("mode must be one of %s" % ", ".join(MODES))
    lib = load()
    post = next((p for p in lib["posts"] if p["id"] == post_id), None)
    if not post:
        raise ValueError("That post is not saved. Bookmark it first.")
    folder = post_dir(post_id)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        if is_carousel(post):
            # A carousel has no audio and no frames to cut: pull its slides, then Fable reads them.
            slides = pull_slides(folder, post)
            info = {"duration": 0, "width": 0, "height": 0, "audio": False, "slides": len(slides)}
            transcript, sheets = None, []
            cost = breakdown(folder, post, None, []) if mode not in ("fetch", "ffmpeg") else None
        else:
            media = fetch(folder, post["piece"]["url"])
            info = probe(media)
            transcript = transcribe(folder, media, info) if mode in ("transcribe", "both") else None
            sheets = frames(folder, media, info) if mode in ("watch", "both", "ffmpeg") else []
            cost = breakdown(folder, post, transcript, sheets) if mode not in ("fetch", "ffmpeg") else None
        # Re-read: the server may have moved or renamed things while we worked.
        lib = load()
        post = next(p for p in lib["posts"] if p["id"] == post_id)
        a = post.setdefault("analysis", {})
        a["modes"] = sorted(set(a.get("modes", [])) | {mode})
        a["media"] = {"duration_s": round(info["duration"]), "width": info["width"], "height": info["height"], "audio": info["audio"],
                      "slides": slide_files(folder), "video": (folder / "media.mp4").exists()}
        if transcript is not None:
            a["transcript"] = {"hook": transcript["hook"], "words": len(transcript["text"].split()), "model": transcript["model"], "silent": transcript["silent"]}
        if sheets:
            a["sheets"] = sheets
        if mode not in ("fetch", "ffmpeg"):
            a["breakdown"] = True
            a["breakdown_model"] = hunt.settings()["model"]
            a["fable_cost_usd"] = round((a.get("fable_cost_usd") or 0) + (cost or 0), 4)
        a["updated"] = now()
        store(lib)
        status(folder, "done", "Analysis complete", done=True)
    except Exception as e:
        status(folder, "error", str(e)[:400], done=True, error=True)
        raise


def main():
    ap = argparse.ArgumentParser(prog="library.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("analyze")
    p.add_argument("--post", required=True)
    p.add_argument("--mode", required=True, choices=MODES)
    args = ap.parse_args()
    analyze(args.post, args.mode)


if __name__ == "__main__":
    main()
