#!/usr/bin/env python3
"""
Competitor Hunter server.  python3 server.py  ->  http://127.0.0.1:4317

One click on the page starts one run:
    1. spawn headless Claude Code on Fable as the conductor (engine/conductor.prompt.txt)
    2. Claude runs the engine stages (engine/hunt.py) and tunes the Jev preset between pilots
    3. every engine event and every Claude message lands in runs/<id>/events.jsonl
    4. the page tails that file over Server-Sent Events, then loads runs/<id>/result.json

If Claude Code is missing or stops before a result exists, the remaining stages run directly
(`hunt.py all --resume`) with the base Jev preset, and the page says so.

Binds to 127.0.0.1 only: this process can start local commands. Zero dependencies (stdlib only).
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).resolve().parent
WEB = APP / "web"
RUNS = APP / "runs"
HUNT = APP / "engine" / "hunt.py"
PROMPT = APP / "engine" / "conductor.prompt.txt"
sys.dont_write_bytecode = True
sys.path.insert(0, str(APP / "engine"))
import hunt  # noqa: E402  (roster + env file location, shared with the engine)
import library  # noqa: E402  (bookmarks, folders, saved-post analysis)
import studio  # noqa: E402  (your own channels, topics, board, scripts)

STUDIOPY = APP / "engine" / "studio.py"
studio_jobs = {}   # job key -> {proc, status, msg, started, run_id}

LIBPY = APP / "engine" / "library.py"
ASK_PROMPT = APP / "engine" / "ask.prompt.txt"
CHATS = library.LIB / "chats"
jobs = {}   # post id -> {mode, proc}, while library.py works on that post
asks = {}   # request token -> Claude process, while Ask AI thinks

HOST, PORT = "127.0.0.1", 4317
WINDOW_DAYS = 80
PLATFORMS = ("youtube", "instagram")
CONDUCTOR_MODEL = "fable"
CONDUCTOR_TIMEOUT_S = 30 * 60
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".jpg": "image/jpeg"}

state = {"running": False, "run_id": None, "proc": None}
lock = threading.Lock()
credits_cache = {"at": 0.0, "value": None}


def emit(run, stage, kind, msg, **data):
    hunt.emit(run, stage, kind, msg, **data)


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------
def conductor_command(run, platforms):
    prompt = (PROMPT.read_text(encoding="utf-8")
              .replace("{RUN}", str(run)).replace("{HUNT}", str(HUNT))
              .replace("{DAYS}", str(WINDOW_DAYS)).replace("{PLATFORMS}", ",".join(platforms)))
    return [
        shutil.which("claude"), "-p", prompt,
        "--model", hunt.settings()["model"],
        "--output-format", "stream-json", "--verbose",
        # Project settings only: the conductor needs none of the user-level hooks, plugins, or MCP servers.
        "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands",
        "--no-session-persistence",
        # File edits inside this app are accepted; the only command it may run is the engine.
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Bash(python3 %s:*)" % HUNT, "Read", "Edit", "Write",
    ]


def relay_claude_line(run, line, info):
    """Turn one stream-json line from Claude Code into page events."""
    try:
        msg = json.loads(line)
    except ValueError:
        if line.strip():
            emit(run, "fable", "log", line.strip()[:300])
        return
    kind = msg.get("type")
    if kind == "assistant":
        for block in msg.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                emit(run, "fable", "say", block["text"].strip()[:600])
            elif block.get("type") == "tool_use":
                tool, tool_input = block.get("name"), block.get("input", {})
                if tool == "Bash":
                    stage = re.search(r"hunt\.py\s+(\w+)", tool_input.get("command", ""))
                    emit(run, "fable", "cmd", "hunt.py " + stage.group(1) if stage else tool_input.get("command", "")[:120])
                elif tool in ("Edit", "Write"):
                    emit(run, "fable", "cmd", "rewriting Jev criteria in " + Path(tool_input.get("file_path", "")).name)
    elif kind == "result":
        info.update(cost_usd=msg.get("total_cost_usd"), turns=msg.get("num_turns"),
                    seconds=round((msg.get("duration_ms") or 0) / 1000), failed=bool(msg.get("is_error")))
        if msg.get("is_error"):
            emit(run, "fable", "warn", "Claude Code ended with an error: %s" % str(msg.get("result"))[:300])


def openrouter_key():
    """The shell may export the key; otherwise it is read from the app's env file."""
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    fallback = hunt.ENV_FILE
    if not key and fallback.is_file():
        for line in fallback.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def child_env():
    """Explicit environment for Claude Code and the engine: never whatever the parent happened to inherit."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR") if os.environ.get(k)}
    env["OPENROUTER_API_KEY"] = openrouter_key()
    return env


def run_process(command, on_line=None):
    proc = subprocess.Popen(command, cwd=str(APP), env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    with lock:
        state["proc"] = proc
    timer = threading.Timer(CONDUCTOR_TIMEOUT_S, kill, args=(proc,))
    timer.start()
    try:
        for line in proc.stdout:
            if on_line:
                on_line(line)
        return proc.wait()
    finally:
        timer.cancel()


def kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def conduct(run, platforms):
    info = {"mode": "claude", "model": hunt.settings()["model"]}
    result = run / "result.json"
    try:
        if shutil.which("claude"):
            emit(run, "fable", "phase", "Fable takes the conductor seat in Claude Code")
            run_process(conductor_command(run, platforms), lambda line: relay_claude_line(run, line, info))
        else:
            emit(run, "fable", "warn", "Claude Code is not on PATH")
        if not result.exists() and not state.get("stopped"):
            info["mode"] = "direct"
            emit(run, "fable", "warn", "Claude Code stopped before a result existed. Finishing the remaining stages directly with the base Jev preset.")
            run_process([sys.executable, "-B", str(HUNT), "all", "--run", str(run),
                         "--days", str(WINDOW_DAYS), "--platforms", ",".join(platforms)])
        if result.exists():
            data = hunt.read_json(result)
            data["conductor"] = info
            hunt.write_json(result, data)
            emit(run, "system", "end", "Hunt complete", ok=True, run_id=run.name)
        else:
            emit(run, "system", "end", "Hunt stopped" if state.get("stopped") else "Hunt failed. Read the log above for the stage that broke.",
                 ok=False, run_id=run.name)
    except Exception as e:
        emit(run, "system", "end", "Hunt failed: %s" % str(e)[:300], ok=False, run_id=run.name)
    finally:
        with lock:
            state.update(running=False, proc=None)


def start_run(platforms):
    with lock:
        if state["running"]:
            return None
        run = RUNS / datetime.now().strftime("%Y-%m-%d-%H%M%S")
        run.mkdir(parents=True)
        state.update(running=True, run_id=run.name, stopped=False)
    threading.Thread(target=conduct, args=(run, platforms), daemon=True).start()
    return run.name


# ----------------------------------------------------------------------------
# Saved posts: one library.py job at a time per post, progress read from status.json
# ----------------------------------------------------------------------------
def start_analysis(post_id, mode):
    with lock:
        if post_id in jobs:
            return False
        jobs[post_id] = {"mode": mode, "proc": None}
    folder = library.post_dir(post_id)
    folder.mkdir(parents=True, exist_ok=True)
    library.status(folder, "queued", "Starting %s" % mode)

    def work():
        try:
            proc = subprocess.Popen([sys.executable, "-B", str(LIBPY), "analyze", "--post", post_id, "--mode", mode],
                                    cwd=str(APP), env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            with lock:
                jobs[post_id]["proc"] = proc
            proc.wait(timeout=1800)
            if proc.returncode not in (0, None) and not jobs.get(post_id, {}).get("aborted"):
                status_file = folder / "status.json"
                current = hunt.read_json(status_file) if status_file.exists() else {}
                if not current.get("done"):
                    library.status(folder, "error", "The analysis stopped early (exit %s)" % proc.returncode, done=True, error=True)
        except Exception as e:
            library.status(folder, "error", str(e)[:300], done=True, error=True)
        finally:
            with lock:
                jobs.pop(post_id, None)
    threading.Thread(target=work, daemon=True).start()
    return True


def abort_analysis(post_id):
    with lock:
        job = jobs.get(post_id)
        if not job:
            return False
        job["aborted"] = True
        proc = job.get("proc")
    if proc:
        kill(proc)
    library.status(library.post_dir(post_id), "aborted", "Stopped by you", done=True, error=True)
    return True


def abort_apify(run):
    """A stopped hunt must also stop the paid Instagram scrape running on Apify's side."""
    receipt = run / "apify-run.json"
    if not receipt.exists():
        return
    try:
        run_id = hunt.read_json(receipt)["id"]
        hunt.http_json(hunt.APIFY_API + "actor-runs/%s/abort" % run_id, headers={"Authorization": "Bearer " + hunt.env_value("APIFY_API_TOKEN")}, body={}, timeout=20)
        emit(run, "scrape", "warn", "Apify scrape %s aborted" % run_id)
    except Exception as e:
        emit(run, "scrape", "warn", "Could not abort the Apify scrape: %s" % str(e)[:120])


def library_payload():
    lib = library.load()
    for p in lib["posts"]:
        p["running"] = (jobs.get(p["id"]) or {}).get("mode")
        status_file = library.LIB / p["id"] / "status.json"
        p["status"] = hunt.read_json(status_file) if status_file.exists() else None
    return lib


# ----------------------------------------------------------------------------
# Ask AI: a headless Claude Code chat over a context pack of posts. First turn carries the pack,
# later turns resume the same Claude session, so the pack is never resent.
# ----------------------------------------------------------------------------
def context_pack(piece_ids, include_top, include_reddit, card_ids=()):
    saved = {p["id"]: p for p in library.load()["posts"]}
    latest = latest_result()
    result = hunt.read_json(latest) if latest else None
    items_file = latest.parent / "items.json" if latest else None
    items = {i["id"]: i for i in hunt.read_json(items_file)} if items_file and items_file.exists() else {}
    ids = list(piece_ids) + ([p["id"] for p in result["pieces"][:12]] if include_top and result else [])
    pack, seen = [], set()
    for pid in ids:
        if pid in seen:
            continue
        seen.add(pid)
        post = saved.get(pid)
        piece = post["piece"] if post else next((p for p in (result["pieces"] if result else []) if p["id"] == pid), None)
        if not piece:
            continue
        entry = {k: piece.get(k) for k in ("title", "competitor", "platform", "format", "url", "views", "likes", "comments", "er", "breakout", "score", "age_days", "jev")}
        entry["caption"] = (piece.get("description") or items.get(pid, {}).get("description") or "")[:600]
        if post:
            folder = library.post_dir(pid)
            analysis = post.get("analysis") or {}
            if analysis.get("transcript"):
                entry["spoken_hook"] = analysis["transcript"].get("hook")
            if (folder / "transcript.json").exists():
                entry["transcript"] = hunt.read_json(folder / "transcript.json")["text"][:3000]
            if (folder / "breakdown.md").exists():
                entry["breakdown"] = (folder / "breakdown.md").read_text(encoding="utf-8")[:4000]
        pack.append(entry)
    reddit = (result or {}).get("reddit", {}).get("posts", [])[:20] if include_reddit else []
    cards = []
    for card in studio.board()["cards"]:
        if card["id"] in card_ids:
            entry = {k: card.get(k) for k in ("title", "format", "hook", "angle", "why", "inspiration", "notes", "status")}
            script = studio.CARDS / card["id"] / "script.md"
            if script.exists():
                entry["script"] = script.read_text(encoding="utf-8")[:6000]
            cards.append(entry)
    return {"pieces": pack, "reddit": [{"sub": r["sub"], "title": r["title"], "icp_fit": (r.get("jev") or {}).get("icp_fit")} for r in reddit], "board_cards": cards}


def chat_path(chat_id):
    if not re.fullmatch(r"[0-9a-f]{10}", str(chat_id or "")):
        raise ValueError("unknown chat")
    return CHATS / (chat_id + ".json")


def ask(chat_id, message, piece_ids, include_top, include_reddit, token=None, card_ids=()):
    claude = shutil.which("claude")
    if not claude:
        raise ValueError("Claude Code is not on PATH.")
    CHATS.mkdir(parents=True, exist_ok=True)
    chat = hunt.read_json(chat_path(chat_id)) if chat_id else None
    command = [claude, "-p", "--model", hunt.settings()["model"], "--output-format", "json",
               "--restricted", "--strict-mcp-config", "--disable-slash-commands", "--permission-mode", "dontAsk"]
    if chat:
        command += ["--resume", chat["session_id"]]
        prompt = message
    else:
        pack = context_pack(piece_ids, include_top, include_reddit, card_ids)
        if not pack["pieces"] and not pack["reddit"] and not pack["board_cards"]:
            raise ValueError("Pick at least one post, or include the top pieces from the last hunt.")
        prompt = ASK_PROMPT.read_text(encoding="utf-8").replace("{CREATOR}", hunt.creator_profile()) + json.dumps(pack, ensure_ascii=False) + "\n\nCHARLES: " + message
        chat = {"id": uuid.uuid4().hex[:10], "created": datetime.now().isoformat(timespec="seconds"), "title": message.strip()[:70],
                "pieces": [e["title"] for e in pack["pieces"]] + [c["title"] for c in pack["board_cards"]], "include_top": bool(include_top), "include_reddit": bool(include_reddit),
                "model": hunt.settings()["model"], "messages": []}
    proc = subprocess.Popen(command + [prompt], cwd=str(CHATS), env=child_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    if token:
        asks[token] = proc
    try:
        stdout, stderr = proc.communicate(timeout=600)
    finally:
        aborted = asks.pop(token, None) is None if token else False
    if aborted or proc.returncode == -signal.SIGTERM:
        raise ValueError("Stopped.")
    try:
        data = json.loads(stdout)
    except ValueError:
        raise RuntimeError("Claude Code returned nothing readable: %s" % (stderr or stdout).strip()[-300:])
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result"))[:300])
    chat["session_id"] = data.get("session_id")
    chat["cost_usd"] = round((chat.get("cost_usd") or 0) + (data.get("total_cost_usd") or 0), 4)
    chat["messages"] += [{"role": "you", "text": message}, {"role": "ai", "text": data.get("result") or ""}]
    hunt.write_json(chat_path(chat["id"]), chat)
    return chat


def chat_list():
    if not CHATS.exists():
        return []
    chats = [hunt.read_json(p) for p in CHATS.glob("*.json")]
    chats.sort(key=lambda c: c["created"], reverse=True)
    return [{k: c.get(k) for k in ("id", "title", "created", "pieces", "include_top", "include_reddit")} | {"turns": len(c["messages"]) // 2} for c in chats]


# ----------------------------------------------------------------------------
# My Studio jobs: one subprocess per job key (hunt, topics-<format>, make-<card>-<what>), abortable.
# ----------------------------------------------------------------------------
def start_studio_job(key, command, run=None):
    with lock:
        if key in studio_jobs and studio_jobs[key]["status"] == "running":
            return False
        studio_jobs[key] = {"status": "running", "msg": "Starting", "started": time.time(), "run_id": run.name if run else None, "proc": None, "output": ""}

    def work():
        job = studio_jobs[key]
        try:
            proc = subprocess.Popen(command, cwd=str(APP), env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
            job["proc"] = proc
            tail = []
            for line in proc.stdout:
                tail = (tail + [line.rstrip()])[-6:]
                job["msg"] = tail[-1][:160]
            proc.wait(timeout=1800)
            job["output"] = "\n".join(tail)
            if job.get("aborted"):
                job["status"], job["msg"] = "aborted", "Stopped by you"
            elif proc.returncode == 0:
                job["status"], job["msg"] = "done", "Done"
            else:
                job["status"], job["msg"] = "error", (tail[-1] if tail else "exit %s" % proc.returncode)[:200]
        except Exception as e:
            job["status"], job["msg"] = "error", str(e)[:200]
        finally:
            job["proc"] = None
            if run:
                emit(run, "system", "end", "Done" if job["status"] == "done" else job["msg"], ok=job["status"] == "done", run_id=run.name)
    threading.Thread(target=work, daemon=True).start()
    return True


def abort_studio_job(key):
    job = studio_jobs.get(key)
    if not job or job["status"] != "running":
        return False
    job["aborted"] = True
    if job["proc"]:
        kill(job["proc"])
    return True


def studio_payload():
    mine = studio.own_latest()
    topics = sorted(studio.TOPICS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if studio.TOPICS.exists() else []
    reports = []
    for path in topics[:20]:
        data = hunt.read_json(path)
        reports.append({"file": path.name, "format": data["format"], "created": data["created"], "count": len(data["ideas"]), "model": data.get("model")})
    return {
        "result": hunt.read_json(mine) if mine else None, "run_id": mine.parent.name if mine else None,
        "board": studio.board(), "topics": reports, "statuses": list(studio.STATUSES),
        "jobs": {k: {x: v[x] for x in ("status", "msg", "started", "run_id")} for k, v in studio_jobs.items()},
        "has_youtube": studio.YT_TOKEN.exists(), "has_instagram": studio.IG_ENV.exists(),
    }


# ----------------------------------------------------------------------------
# Read-side helpers
# ----------------------------------------------------------------------------
def latest_result():
    done = [p for p in RUNS.glob("*/result.json") if not p.parent.name.startswith("me-")] if RUNS.exists() else []
    return max(done, key=lambda p: p.stat().st_mtime) if done else None


def env_has(name):
    try:
        return any(line.startswith(name + "=") and len(line) > len(name) + 1
                   for line in hunt.ENV_FILE.read_text(encoding="utf-8").splitlines())
    except OSError:
        return False


def jev_credits():
    key = openrouter_key()
    if not key:
        return None
    if time.time() - credits_cache["at"] > 60:
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": "Bearer " + key})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]
            credits_cache.update(at=time.time(), value=round(float(data["total_credits"]) - float(data["total_usage"]), 2))
        except Exception:
            credits_cache.update(at=time.time(), value=None)
    return credits_cache["value"]


def app_state():
    latest = latest_result()
    return {
        "running": state["running"], "run_id": state["run_id"], "window_days": WINDOW_DAYS,
        "latest_run_id": latest.parent.name if latest else None,
        "roster": hunt.roster(PLATFORMS), "settings": hunt.settings(), "models": hunt.MODELS,
        "health": {
            "claude": bool(shutil.which("claude")), "jev_skill": hunt.JEV_PY.is_file(),
            "jev_key": bool(openrouter_key()), "jev_credits": jev_credits(),
            "youtube_key": env_has("YOUTUBE_DATA_API_KEY"), "apify_key": env_has("APIFY_API_TOKEN"),
        },
    }


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        if not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        wanted = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if wanted and path.suffix == ".mp4":           # the <video> player seeks with Range requests
            start = int(wanted.group(1) or 0)
            end = min(int(wanted.group(2) or size - 1), size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
        with open(path, "rb") as fh:
            fh.seek(start)
            body = fh.read(end - start + 1)
        self.send_response(206 if wanted and path.suffix == ".mp4" else 200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if wanted and path.suffix == ".mp4":
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "max-age=86400" if path.suffix in (".jpg", ".mp4") else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            return self.send_file(WEB / "index.html")
        if url.path in ("/app.js", "/library.js", "/ask.js", "/studio.js", "/styles.css"):
            return self.send_file(WEB / url.path[1:])
        if url.path == "/studio":
            return self.send_file(WEB / "studio.html")
        if url.path == "/api/studio":
            return self.send_json(studio_payload())
        if url.path == "/api/studio/topic":
            name = (query.get("file") or [""])[0]
            if not re.fullmatch(r"[\w-]+\.json", name) or not (studio.TOPICS / name).exists():
                return self.send_json({"error": "unknown report"}, 404)
            return self.send_json(hunt.read_json(studio.TOPICS / name))
        card_file = re.fullmatch(r"/studio/cards/([0-9a-f]{8})/(script|thumbnails)\.md", url.path)
        if card_file:
            return self.send_file(studio.CARDS / card_file.group(1) / (card_file.group(2) + ".md"))
        saved = re.fullmatch(r"/library/([\w-]+)/([\w-]+\.(?:jpg|md|json|mp4))", url.path)
        if saved:
            return self.send_file(library.LIB / saved.group(1) / saved.group(2))
        if url.path == "/api/library":
            return self.send_json(library_payload())
        if url.path == "/api/settings":
            return self.send_json({**hunt.settings(), "models": hunt.MODELS})
        if url.path == "/api/chats":
            return self.send_json({"chats": chat_list()})
        if url.path == "/api/chat":
            try:
                return self.send_json(hunt.read_json(chat_path((query.get("id") or [""])[0])))
            except (ValueError, OSError):
                return self.send_json({"error": "unknown chat"}, 404)
        thumb = re.fullmatch(r"/runs/([\w-]+)/thumbs/([\w-]+\.jpg)", url.path)
        if thumb:
            return self.send_file(RUNS / thumb.group(1) / "thumbs" / thumb.group(2))
        if url.path == "/api/state":
            return self.send_json(app_state())
        if url.path == "/api/result":
            path = latest_result()
            return self.send_json(hunt.read_json(path)) if path else self.send_json({"error": "no hunt yet"}, 404)
        if url.path == "/api/events":
            run_id = (query.get("run") or [""])[0]
            if not re.fullmatch(r"[\w-]+", run_id) or not (RUNS / run_id).is_dir():
                return self.send_json({"error": "unknown run"}, 404)
            return self.stream_events(RUNS / run_id)
        self.send_json({"error": "not found"}, 404)

    def stream_events(self, run):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        events, position, idle = run / "events.jsonl", 0, 0
        try:
            while True:
                chunk = ""
                if events.exists():
                    with open(events, "r", encoding="utf-8") as fh:
                        fh.seek(position)
                        chunk = fh.read()
                # Only forward whole lines; a writer may be mid-append.
                complete = chunk[: chunk.rfind("\n") + 1]
                position += len(complete.encode("utf-8"))
                for line in complete.splitlines():
                    self.wfile.write(("data: %s\n\n" % line).encode("utf-8"))
                if complete:
                    self.wfile.flush()
                    idle = 0
                elif not (state["running"] and state["run_id"] == run.name):
                    self.wfile.write(b"event: closed\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                else:
                    idle += 1
                    if idle % 30 == 0:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        # Same-origin JSON only: a page on another site must not be able to start a run.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in ("%s:%d" % (HOST, PORT), "localhost:%d" % PORT):
            return self.send_json({"error": "cross-origin request refused"}, 403)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_json({"error": "send application/json"}, 415)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)

        if self.path == "/api/run":
            platforms = [p for p in PLATFORMS if p in (body.get("platforms") or [])]
            if not platforms:
                return self.send_json({"error": "pick at least one platform"}, 400)
            run_id = start_run(platforms)
            if not run_id:
                return self.send_json({"error": "a hunt is already running", "run_id": state["run_id"]}, 409)
            return self.send_json({"run_id": run_id})
        if self.path in ("/api/roster", "/api/roster/remove"):
            try:
                if self.path == "/api/roster":
                    added = hunt.add_competitor(str(body.get("input") or ""), body.get("platform"))
                else:
                    added = None
                    hunt.remove_competitor(body.get("platform"), body.get("handle"))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except (Exception, SystemExit) as e:   # env_value() exits when the API key is missing
                return self.send_json({"error": "Could not check that channel with YouTube: %s" % str(e)[:160]}, 502)
            return self.send_json({"roster": hunt.roster(PLATFORMS), "added": added})
        if self.path.startswith("/api/library/"):
            try:
                action = self.path.rsplit("/", 1)[1]
                post_id, folder = str(body.get("id") or body.get("piece_id") or ""), body.get("folder") or None
                extra = {}
                if action == "folder":
                    what = body.get("action")
                    if what == "create":
                        library.add_folder(body.get("name"))
                    elif what == "rename":
                        library.rename_folder(str(body.get("id")), body.get("name"))
                    elif what == "delete":
                        library.delete_folder(str(body.get("id")))
                    else:
                        return self.send_json({"error": "folder action must be create, rename or delete"}, 400)
                elif action == "save":
                    lib = library.save_post(post_id, str(body.get("run_id") or ""), folder)
                    post = next(p for p in lib["posts"] if p["id"] == post_id)
                    if library.is_carousel(post) and not library.slide_files(library.post_dir(post_id)):
                        start_analysis(post_id, "fetch")              # carousels pull their slides on save
                elif action == "move":
                    library.move_post(post_id, folder)
                elif action == "remove":
                    if post_id in jobs:
                        return self.send_json({"error": "wait for the running analysis to finish"}, 409)
                    library.remove_post(post_id)
                elif action == "abort":
                    if not abort_analysis(post_id):
                        return self.send_json({"error": "Nothing is running for that post."}, 409)
                elif action == "analyze":
                    mode = str(body.get("mode") or "")
                    if mode not in library.MODES:
                        return self.send_json({"error": "mode must be one of %s" % ", ".join(library.MODES)}, 400)
                    if not any(p["id"] == post_id for p in library.load()["posts"]):
                        return self.send_json({"error": "Bookmark the post first."}, 400)
                    if not start_analysis(post_id, mode):
                        return self.send_json({"error": "That post is already being analyzed."}, 409)
                elif action == "download":
                    path, count = library.download_slides(post_id)
                    subprocess.Popen(["open", path])                  # show the folder in Finder
                    extra = {"path": path, "count": count}
                else:
                    return self.send_json({"error": "not found"}, 404)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": str(e)[:200]}, 500)
            return self.send_json({**library_payload(), **extra})
        if self.path == "/api/settings":
            try:
                return self.send_json({**hunt.save_settings(model=body.get("model"), theme=body.get("theme"), reddit_subs=body.get("reddit_subs")), "models": hunt.MODELS})
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
        if self.path == "/api/ask":
            message = str(body.get("message") or "").strip()
            if not message:
                return self.send_json({"error": "Type a question first."}, 400)
            try:
                token = str(body.get("token") or "")[:40]
                return self.send_json(ask(body.get("chat_id"), message[:4000], [str(x) for x in (body.get("pieces") or [])][:40],
                                          bool(body.get("include_top")), bool(body.get("include_reddit")), token or None,
                                          [str(x) for x in (body.get("cards") or [])][:10]))
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": str(e)[:300]}, 500)
        if self.path.startswith("/api/studio/"):
            action = self.path.rsplit("/", 1)[1]
            try:
                extra = {}
                if action == "hunt":
                    platforms = [p for p in PLATFORMS if p in (body.get("platforms") or [])] or list(PLATFORMS)
                    run = RUNS / ("me-" + datetime.now().strftime("%Y-%m-%d-%H%M%S"))
                    run.mkdir(parents=True)
                    if not start_studio_job("hunt", [sys.executable, "-B", str(STUDIOPY), "hunt", "--run", str(run), "--days", str(WINDOW_DAYS), "--platforms", ",".join(platforms)], run):
                        return self.send_json({"error": "Your hunt is already running."}, 409)
                    extra = {"run_id": run.name}
                elif action == "topics":
                    fmt = str(body.get("format") or "")
                    if fmt not in studio.FORMATS:
                        return self.send_json({"error": "format must be longform or short"}, 400)
                    if not start_studio_job("topics-" + fmt, [sys.executable, "-B", str(STUDIOPY), "topics", "--format", fmt]):
                        return self.send_json({"error": "That list is already being made."}, 409)
                elif action == "board":
                    what = body.get("action")
                    if what == "add":
                        extra = {"card": studio.add_card(body.get("idea") or {}, body.get("format"), str(body.get("source") or ""))}
                    elif what == "update":
                        studio.update_card(str(body.get("id") or ""), body.get("fields") or {})
                    elif what == "delete":
                        studio.delete_card(str(body.get("id") or ""))
                    else:
                        return self.send_json({"error": "board action must be add, update or delete"}, 400)
                elif action == "make":
                    card_id, what = str(body.get("id") or ""), str(body.get("what") or "")
                    if what not in ("script", "thumbnails") or not re.fullmatch(r"[0-9a-f]{8}", card_id):
                        return self.send_json({"error": "bad request"}, 400)
                    if not start_studio_job("make-%s-%s" % (card_id, what), [sys.executable, "-B", str(STUDIOPY), "make", "--card", card_id, "--what", what]):
                        return self.send_json({"error": "Already being made."}, 409)
                elif action == "download":
                    path, count = studio.download_thumbnails(str(body.get("id") or ""))
                    subprocess.Popen(["open", path])
                    extra = {"path": path, "count": count}
                elif action == "abort":
                    key = str(body.get("job") or "")
                    if not abort_studio_job(key):
                        return self.send_json({"error": "Nothing running under that name."}, 409)
                    if key == "hunt" and studio_jobs[key].get("run_id"):
                        abort_apify(RUNS / studio_jobs[key]["run_id"])
                else:
                    return self.send_json({"error": "not found"}, 404)
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            except Exception as e:
                return self.send_json({"error": str(e)[:200]}, 500)
            return self.send_json({**studio_payload(), **extra})
        if self.path == "/api/ask/abort":
            proc = asks.pop(str(body.get("token") or "")[:40], None)
            if proc:
                kill(proc)
            return self.send_json({"stopped": bool(proc)})
        if self.path == "/api/stop":
            with lock:
                proc = state["proc"]
                run_id = state["run_id"]
                state["stopped"] = True
            if proc:
                kill(proc)
            if run_id:
                abort_apify(RUNS / run_id)
            return self.send_json({"stopped": bool(proc)})
        self.send_json({"error": "not found"}, 404)


def main():
    RUNS.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    url = "http://%s:%d" % (HOST, PORT)
    print("Competitor Hunter  ->  %s" % url, flush=True)
    if "--open" in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
