#!/usr/bin/env python3
"""
Competitor Hunter engine.

Stages (each one resumable, each one appends progress to <run>/events.jsonl):
    scrape   competitors from the agent brain -> every piece posted in the window
             YouTube: Data API key (channels -> uploads playlist -> videos)
             Instagram: Apify apify/instagram-scraper (one run for the whole roster)
    pilot    Jev over a fixed sample; prints confidence diagnostics so the preset can be tuned
    lint     validate <run>/preset.json (Jev lint + the keys this engine reads)
    judge    Jev over every piece
    rank     breakout, engagement, paid filter, hunt score, competitor ladders -> result.json
    all      scrape -> pilot -> judge -> rank, skipping stages whose output already exists

Jev is called only through the claude-x-jev skill's jev.py. Zero dependencies (stdlib only).
"""
import argparse
import bisect
import hashlib
import importlib.util
import json
import math
import random
import re
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
# Optional machine-specific paths in local.json (brain, env, instagram_env, jev_py, yt_token, voice). Defaults sit inside the app folder.
LOCAL = APP / "local.json"
_local = json.loads(LOCAL.read_text(encoding="utf-8")) if LOCAL.exists() else {}
def _path(key, default):
    return Path(_local[key]).expanduser() if _local.get(key) else default
RUNS = APP / "runs"
BASE_PRESET = APP / "jev" / "competitor-hunt.json"
PRESET_HISTORY = APP / "jev" / "history"
BRAIN = _path("brain", APP / "brain.json")              # who you are, your audience, your competitors (see brain.example.json)
LOCAL_ROSTER = APP / "roster.json"   # competitors added from the page; merged with the brain's list
SETTINGS = APP / "settings.json"      # page settings: which Claude model conducts hunts and writes breakdowns
MODELS = {"fable": "Fable 5.1", "opus": "Opus 5.5", "sonnet": "Sonnet 5.5"}


DEFAULT_SETTINGS = {"model": "fable", "theme": "light", "reddit_subs": ["ClaudeAI", "ClaudeCode", "AI_Agents", "automation"]}


def settings():
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS.exists():
        data.update(read_json(SETTINGS))
    return data


def save_settings(**changes):
    data = settings()
    if changes.get("model") is not None:
        model = str(changes["model"]).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,60}", model):
            raise ValueError("Model must be fable, opus, sonnet, or a full model id like claude-opus-5-5.")
        data["model"] = model
    if changes.get("theme") is not None:
        if changes["theme"] not in ("light", "dark"):
            raise ValueError("Theme must be light or dark.")
        data["theme"] = changes["theme"]
    if changes.get("reddit_subs") is not None:
        subs = [re.sub(r"^r/", "", str(s).strip()) for s in changes["reddit_subs"]]
        subs = [s for s in subs if s]
        if any(not re.fullmatch(r"[A-Za-z0-9_]{2,30}", s) for s in subs):
            raise ValueError("Subreddit names are letters, numbers and underscores only.")
        data["reddit_subs"] = subs[:12]
    write_json(SETTINGS, data)
    return data


# Reddit: only www.reddit.com/r/<sub>/top/.rss answers (the JSON API and old.reddit are blocked); it carries no vote counts.
def scrape_reddit(run, subs):
    found, blocked = [], []
    for sub in subs:
        try:
            req = urllib.request.Request("https://www.reddit.com/r/%s/top/.rss?t=week" % sub, headers={"User-Agent": UA})
            xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            entries = re.findall(r"<entry>(.*?)</entry>", xml, flags=re.S)
            if not entries:
                raise ValueError("no entries")
            for entry in entries[:25]:
                title = re.search(r"<title>(.*?)</title>", entry, flags=re.S)
                link = re.search(r'<link href="([^"]+)"', entry)
                if title and link:
                    text = re.sub(r"<.*?>", "", title.group(1)).replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").strip()
                    found.append({"id": "rd_%s_%d" % (sub, len(found)), "sub": sub, "title": text, "url": link.group(1)})
        except Exception:
            blocked.append(sub)
    emit(run, "scrape", "log", "Reddit: %d posts from r/%s%s" % (len(found), ", r/".join(s for s in subs if s not in blocked) or "nothing",
                                                                   (", blocked: r/" + ", r/".join(blocked)) if blocked else ""))
    write_json(run / "reddit.json", {"posts": found, "blocked": blocked, "subs": subs})
    return found
IG_NOT_PROFILES = {"p", "reel", "reels", "explore", "stories", "tv", "accounts"}
ENV_FILE = _path("env", APP / ".env")
JEV_PY = _path("jev_py", Path.home() / ".claude" / "skills" / "claude-x-jev" / "scripts" / "jev.py")
REPORTS = _path("reports", APP / "reports")   # the hub every hunt writes to and the app reads from (viral-discover / viral-audit write here too)


def md_link(p):
    return "[%s](%s)" % (str(p.get("title") or "(no caption)").replace("]", ")").replace("[", "(")[:90], p.get("url") or "")


def write_report(run, result):
    """Markdown report in the shared hub, same family as /viral-discover and /viral-audit output."""
    own = bool(result.get("mine"))
    day = datetime.now().strftime("%Y-%m-%d")
    folder = REPORTS / ("my-social-media" if own else "competitor-data") / "app" / ("%s-%s" % (day, run.name))
    folder.mkdir(parents=True, exist_ok=True)
    t = result["totals"]
    lines = ["# %s audit, %s" % ("My content" if own else "Competitor hunt", day), "",
             "Run `%s`, window %d days, platforms %s. %d pieces, %d organic, %d paid placements stripped. Jev: %d decisions, $%.3f."
             % (run.name, result.get("window_days", 0), ", ".join(result.get("platforms", [])), t["pieces"], t["organic"], t["paid"], t["jev_calls"], t["jev_cost"]), ""]
    if own:
        m = result["mine"]
        if m.get("youtube"):
            y = m["youtube"]
            lines += ["## YouTube", "", "%d videos, %s views (%s from ads), %s subscribers gained, %s watch hours." % (y["videos"], f"{y['views']:,}", f"{y.get('ad_views', 0):,}", f"{y['subs_gained']:,}", f"{round(y['minutes'] / 60):,}"), ""]
        if m.get("instagram"):
            g = m["instagram"]
            lines += ["## Instagram", "", "%d posts, %s views, %s reach, %s saves, %s shares." % (g["posts"], f"{g['views']:,}", f"{g['reach']:,}", f"{g['saves']:,}", f"{g['shares']:,}"), ""]
    else:
        lines += ["## The field, least to most views", "", "| Competitor | Platform | Pieces | Views | Median | ER |", "|---|---|---|---|---|---|"]
        for c in sorted(result["competitors"], key=lambda c: c["total_views"]):
            lines.append("| %s | %s | %d | %s | %s | %s |" % (c["name"], c["platform"], c["pieces"], f"{c['total_views']:,}", f"{c['median_views']:,}", "n/a" if c["er"] is None else "%.2f%%" % c["er"]))
        lines.append("")
    lines += ["## Top %s" % ("converters" if own else "pieces"), ""]
    for p in result["pieces"][:25]:
        j = p.get("jev") or {}
        extra = (" · %s %s" % (p.get("conversion"), p.get("conversion_label"))) if own and p.get("conversion") is not None else ""
        lines.append("%d. **%.1f** %s by %s (%s %s): %s views, breakout %s, ER %s%s. Jev: %s, hook %s, proof %s/3, replicable %s%%."
                     % (p["rank"], p["score"], md_link(p), p["competitor"], p["platform"], p["format"], "n/a" if p.get("views") is None else f"{p['views']:,}",
                        "n/a" if p.get("breakout") is None else "%.1fx" % p["breakout"], "n/a" if p.get("er") is None else "%.2f%%" % p["er"], extra,
                        j.get("topic_lane"), j.get("hook_type"), round(j.get("proof") or 0), round((j.get("replicable") or 0) * 100)))
    if result.get("paid"):
        lines += ["", "## Paid placements left out", ""] + ["- %s by %s: %s views, %s" % (md_link(p), p["competitor"], "n/a" if p.get("views") is None else f"{p['views']:,}", p["reason"]) for p in result["paid"][:10]]
    (folder / ("audit-%s.md" % day)).write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not own:
        ideas = ["# Topic ideas, %s" % day, "", "Seeds from the top pieces of run `%s` and this week's Reddit." % run.name, "", "## From the field", ""]
        ideas += ["- %s (%s, %s): %s lane, %s hook, breakout %s" % (md_link(p), p["competitor"], p["format"], (p.get("jev") or {}).get("topic_lane"), (p.get("jev") or {}).get("hook_type"),
                   "n/a" if p.get("breakout") is None else "%.1fx" % p["breakout"]) for p in result["pieces"][:15]]
        reddit = (result.get("reddit") or {}).get("posts", [])
        if reddit:
            ideas += ["", "## Reddit this week", ""] + ["- [%s](%s) (r/%s)" % (r["title"].replace("[", "(").replace("]", ")"), r["url"], r["sub"]) for r in reddit[:20]]
        (folder / ("topic-ideas-%s.md" % day)).write_text("\n".join(ideas) + "\n", encoding="utf-8")
    shutil.copyfile(run / "result.json", folder / "result.json")
    return folder


def list_reports(limit=200):
    if not REPORTS.exists():
        return []
    files = sorted(REPORTS.rglob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"rel": str(p.relative_to(REPORTS)), "source": p.relative_to(REPORTS).parts[0], "name": p.stem,
             "date": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"), "size": p.stat().st_size} for p in files]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
YT_API = "https://www.googleapis.com/youtube/v3/"
APIFY_API = "https://api.apify.com/v2/"
IG_ACTOR = "apify~instagram-scraper"
IG_RESULTS_PER_PROFILE = 100
SHORT_MAX_SECONDS = 180
PILOT_SIZE = 40
JEV_CONCURRENCY = 8
MAX_JEV_ERROR_SHARE = 0.05

# Question keys and types the rank stage reads. Tuning may rewrite wording, never these.
REQUIRED_QUESTIONS = {
    "topic_lane": "choice", "icp_fit": "score", "hook_type": "choice", "proof": "score",
    "replicable": "noul", "sponsored": "noul", "newsjack": "noul",
}
# Paid-placement filter, from /viral:discover Step 3.0.
PAID_ER_RELATIVE = 0.6      # ER% under this AND views over 3x the channel median
PAID_REACH_MULTIPLE = 3
PAID_ER_FLOOR = 0.2         # ER% under this at any view count
PAID_JEV_SPONSORED = 0.8    # Jev says the whole piece is a paid promotion


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def die(msg, code=1):
    sys.stderr.write("[hunt] " + msg + "\n")
    sys.exit(code)


def emit(run, stage, kind, msg, **data):
    """Append one progress event for the UI and echo it for whoever runs the stage."""
    event = {"ts": round(time.time(), 3), "stage": stage, "kind": kind, "msg": msg}
    if data:
        event["data"] = data
    with open(run / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    print("[%s] %s" % (stage, msg), flush=True)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


def env_value(name):
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    die("%s is not set in %s" % (name, ENV_FILE))


def http_json(url, headers=None, body=None, timeout=60):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"User-Agent": UA, "Content-Type": "application/json", **(headers or {})},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_jev():
    """Import the skill's jev.py as a module: the one Decisions API caller on this machine."""
    if not JEV_PY.is_file():
        die("claude-x-jev is not installed at %s. Run: npx claude-x-jev install --with-commands" % JEV_PY)
    sys.dont_write_bytecode = True  # never leave a __pycache__ inside the skill
    spec = importlib.util.spec_from_file_location("jev", JEV_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_dir(args):
    run = Path(args.run).resolve()
    if RUNS.resolve() not in run.parents:
        die("--run must be a folder inside %s" % RUNS)
    run.mkdir(parents=True, exist_ok=True)
    return run


def parse_iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso_duration_seconds(text):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def count(value):
    """Platform counters arrive as strings, None, or -1 when hidden."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


# ----------------------------------------------------------------------------
# Scrape
# ----------------------------------------------------------------------------
def local_roster():
    return read_json(LOCAL_ROSTER) if LOCAL_ROSTER.exists() else []


def roster(platforms):
    """Competitors from the agent brain, plus the ones added in the app (roster.json)."""
    out = []
    for source, entries in (("brain", read_json(BRAIN).get("competitors", [])), ("app", local_roster())):
        for c in entries:
            platform = c.get("platform", "").lower()
            if platform in platforms:
                out.append({"name": c["name"], "platform": platform, "handle": c["handle"], "source": source})
    return out


def parse_competitor(text, platform):
    """A pasted link or handle -> (platform, handle). A link names its own platform; a bare handle needs one."""
    text = text.strip()
    link = re.search(r"youtube\.com/(@[\w.\-]+|channel/(UC[\w-]{22}))", text)
    if link:
        return "youtube", link.group(2) or link.group(1)
    link = re.search(r"instagram\.com/([A-Za-z0-9._]{1,30})", text)
    if link and link.group(1).lower() not in IG_NOT_PROFILES:
        return "instagram", "@" + link.group(1)
    if "/" in text or not re.fullmatch(r"@?[\w.\-]{1,60}", text) or platform not in ("youtube", "instagram"):
        raise ValueError("Paste a YouTube channel link, an Instagram profile link, or an @handle.")
    if platform == "youtube" and re.fullmatch(r"UC[\w-]{22}", text):
        return "youtube", text
    return platform, "@" + text.lstrip("@")


def add_competitor(text, platform):
    """Add one competitor to roster.json. YouTube channels are confirmed (and named) through the Data API."""
    platform, handle = parse_competitor(text, platform)
    entry = {"name": handle.lstrip("@"), "platform": platform, "handle": handle}
    same = {handle.lower()}
    if platform == "youtube":
        lookup = {"id": handle} if handle.startswith("UC") else {"forHandle": handle}
        found = yt_get("channels", env_value("YOUTUBE_DATA_API_KEY"), part="snippet", **lookup).get("items") or []
        if not found:
            raise ValueError("No YouTube channel found for %s." % handle)
        channel = found[0]
        entry.update(name=channel["snippet"]["title"], handle=channel["snippet"].get("customUrl") or channel["id"])
        same |= {channel["id"].lower(), entry["handle"].lower()}   # the brain lists some channels by id, some by handle
    elif not re.fullmatch(r"@[A-Za-z0-9._]{1,30}", handle):
        raise ValueError("That is not a valid Instagram handle.")
    for c in roster((platform,)):
        if c["handle"].lower() in same:
            raise ValueError("%s is already on the roster." % c["name"])
    write_json(LOCAL_ROSTER, local_roster() + [{**entry, "added": datetime.now(timezone.utc).date().isoformat()}])
    return entry


def remove_competitor(platform, handle):
    """Only app-added competitors can be removed here; the agent brain is never written."""
    current = local_roster()
    kept = [c for c in current if not (c["platform"] == platform and c["handle"].lower() == str(handle).lower())]
    if len(kept) == len(current):
        raise ValueError("Only competitors added in the app can be removed here.")
    write_json(LOCAL_ROSTER, kept)


def creator_profile():
    brain = read_json(BRAIN)
    ident, icp = brain.get("identity", {}), brain.get("icp", {})
    pillars = "; ".join("%s (%s)" % (p["name"], p["description"]) for p in brain.get("pillars", []))
    return (
        "%s (%s) makes content about %s. Audience: %s. Audience pains: %s. Content pillars: %s."
        % (ident.get("name"), ident.get("brand"), ident.get("niche"), ", ".join(icp.get("segments", [])),
           "; ".join(icp.get("pain_points", [])), pillars)
    )


def yt_get(resource, key, **params):
    return http_json(YT_API + resource + "?" + urllib.parse.urlencode(params), headers={"X-goog-api-key": key})


def scrape_youtube_channel(comp, key, cutoff):
    handle = comp["handle"]
    lookup = {"id": handle} if handle.startswith("UC") else {"forHandle": handle}
    found = yt_get("channels", key, part="snippet,statistics,contentDetails", **lookup).get("items") or []
    if not found:
        raise RuntimeError("channel not found")
    channel = found[0]
    comp["subs"] = count(channel["statistics"].get("subscriberCount"))
    comp["url"] = "https://www.youtube.com/channel/" + channel["id"]
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]

    # Uploads playlist, newest first. search.list is never used: it returns stale, truncated results.
    video_ids, page = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = yt_get("playlistItems", key, **params)
        in_window = [
            it["contentDetails"]["videoId"] for it in data.get("items", [])
            if parse_iso(it["contentDetails"].get("videoPublishedAt", "1970-01-01T00:00:00Z")) >= cutoff
        ]
        video_ids += in_window
        page = data.get("nextPageToken")
        if not page or len(in_window) < len(data.get("items", [])):
            break

    pieces = []
    for i in range(0, len(video_ids), 50):
        data = yt_get("videos", key, part="snippet,statistics,contentDetails", id=",".join(video_ids[i:i + 50]))
        for v in data.get("items", []):
            snippet, stats = v["snippet"], v.get("statistics", {})
            if snippet.get("liveBroadcastContent") == "upcoming":
                continue
            seconds = iso_duration_seconds(v["contentDetails"].get("duration"))
            pieces.append({
                "id": "yt_" + v["id"], "platform": "youtube", "competitor": comp["name"], "handle": handle,
                "format": "short" if seconds <= SHORT_MAX_SECONDS else "longform", "kind": "video",
                "title": snippet.get("title", ""), "description": snippet.get("description", ""),
                "url": "https://www.youtube.com/watch?v=" + v["id"],
                "thumb": "https://i.ytimg.com/vi/%s/mqdefault.jpg" % v["id"],
                "published": snippet.get("publishedAt"), "duration_s": seconds,
                "views": count(stats.get("viewCount")), "likes": count(stats.get("likeCount")),
                "comments": count(stats.get("commentCount")),
            })
    return pieces


def scrape_youtube(run, comps, key, cutoff):
    pieces = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(scrape_youtube_channel, c, key, cutoff): c for c in comps}
        for fut in as_completed(futures):
            comp = futures[fut]
            try:
                got = fut.result()
                pieces += got
                emit(run, "scrape", "target", "%s: %d pieces" % (comp["name"], len(got)),
                     name=comp["name"], platform="youtube", pieces=len(got))
            except Exception as e:
                comp["error"] = str(e)[:200]
                emit(run, "scrape", "warn", "%s: %s" % (comp["name"], comp["error"]), name=comp["name"])
    return pieces


def scrape_instagram(run, comps, token, cutoff, days):
    auth = {"Authorization": "Bearer " + token}
    by_handle = {c["handle"].lstrip("@").lower(): c for c in comps}
    for c in comps:
        c["url"] = "https://www.instagram.com/%s/" % c["handle"].lstrip("@")
    # The Apify run is paid per result. A scrape stage that gets retried rejoins the run it already started.
    receipt = run / "apify-run.json"
    if receipt.exists():
        started = read_json(receipt)
        emit(run, "scrape", "log", "Instagram: rejoining Apify run %s" % started["id"])
    else:
        started = http_json(APIFY_API + "acts/%s/runs" % IG_ACTOR, headers=auth, body={
            "directUrls": [c["url"] for c in comps], "resultsType": "posts",
            "resultsLimit": IG_RESULTS_PER_PROFILE, "onlyPostsNewerThan": "%d days" % days, "addParentData": False,
        })["data"]
        write_json(receipt, {k: started[k] for k in ("id", "status", "defaultDatasetId")})
        emit(run, "scrape", "log", "Instagram: Apify run %s started for %d profiles" % (started["id"], len(comps)))
    status, waited = started["status"], 0
    while status in ("READY", "RUNNING"):
        time.sleep(6)
        waited += 6
        status = http_json(APIFY_API + "actor-runs/" + started["id"], headers=auth)["data"]["status"]
        if waited % 30 == 0:
            emit(run, "scrape", "log", "Instagram: Apify still scraping (%ds)" % waited)
        if waited > 900:
            raise RuntimeError("Apify run %s did not finish in 15 minutes" % started["id"])
    if status != "SUCCEEDED":
        raise RuntimeError("Apify run %s ended %s" % (started["id"], status))
    rows = http_json(APIFY_API + "datasets/%s/items?clean=true" % started["defaultDatasetId"], headers=auth, timeout=120)

    pieces, per_comp = [], {}
    for row in rows:
        comp = by_handle.get((row.get("ownerUsername") or "").lower())
        stamp = row.get("timestamp")
        # onlyPostsNewerThan is not reliably honoured (pinned posts leak through): enforce the window here.
        if not comp or not stamp or parse_iso(stamp) < cutoff:
            continue
        is_video = row.get("type") == "Video"
        caption = row.get("caption") or ""
        pieces.append({
            "id": "ig_" + row["shortCode"], "platform": "instagram", "competitor": comp["name"], "handle": comp["handle"],
            "format": "reel" if is_video else ("carousel" if row.get("type") == "Sidecar" else "image"),
            "kind": "video" if is_video else "static",
            "title": caption.strip().split("\n")[0][:140], "description": caption,
            "url": row.get("url"), "thumb": row.get("displayUrl"),
            "published": stamp, "duration_s": int(row.get("videoDuration") or 0),
            "views": count(row.get("videoPlayCount") or row.get("videoViewCount")) if is_video else None,
            "likes": count(row.get("likesCount")), "comments": count(row.get("commentsCount")),
            "images": list(row.get("images") or []) if row.get("type") == "Sidecar" else [],   # carousel slides, signed CDN links
        })
        per_comp[comp["name"]] = per_comp.get(comp["name"], 0) + 1
    for c in comps:
        n = per_comp.get(c["name"], 0)
        if not n:
            c["error"] = "no posts returned in the window"
        emit(run, "scrape", "target" if n else "warn", "%s: %d pieces" % (c["name"], n),
             name=c["name"], platform="instagram", pieces=n)
    return pieces


def cmd_scrape(args):
    run = run_dir(args)
    if args.resume and (run / "items.json").exists():
        return print("[scrape] items.json exists, skipping")
    platforms = [p.strip().lower() for p in args.platforms.split(",") if p.strip()]
    comps = roster(platforms)
    if not comps:
        die("no competitors in the agent brain for platforms: %s" % args.platforms)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=args.days)
    emit(run, "scrape", "phase", "Scouting %d competitors, last %d days" % (len(comps), args.days),
         competitors=[{"name": c["name"], "platform": c["platform"]} for c in comps])

    pieces = []
    yt = [c for c in comps if c["platform"] == "youtube"]
    ig = [c for c in comps if c["platform"] == "instagram"]
    if yt:
        pieces += scrape_youtube(run, yt, env_value("YOUTUBE_DATA_API_KEY"), cutoff)
    if ig:
        try:
            pieces += scrape_instagram(run, ig, env_value("APIFY_API_TOKEN"), cutoff, args.days)
        except Exception as e:
            for c in ig:
                c["error"] = str(e)[:200]
            emit(run, "scrape", "warn", "Instagram scrape failed: %s" % str(e)[:200])
    if not pieces:
        emit(run, "scrape", "error", "No pieces scraped")
        die("no pieces scraped")
    scrape_reddit(run, settings().get("reddit_subs") or [])
    for p in pieces:
        p["age_days"] = max(0, (now - parse_iso(p["published"])).days)

    write_json(run / "items.json", pieces)
    write_json(run / "competitors.json", comps)
    write_json(run / "meta.json", {"run_id": run.name, "window_days": args.days, "platforms": platforms,
                                    "scraped_at": now.isoformat(timespec="seconds")})
    if not (run / "preset.json").exists():
        shutil.copyfile(BASE_PRESET, run / "preset.json")
    emit(run, "scrape", "done", "Scouted %d pieces from %d competitors" % (len(pieces), len(comps)),
         pieces=len(pieces), competitors=len(comps))


# ----------------------------------------------------------------------------
# Jev: pilot, lint, judge
# ----------------------------------------------------------------------------
def jev_state(piece, profile, fields):
    state = {
        "creator_profile": profile, "platform": piece["platform"], "format": piece["format"],
        "duration_min": round(piece["duration_s"] / 60, 1), "title": piece["title"][:300],
        "description": piece["description"][:700],
    }
    return {k: v for k, v in state.items() if k in fields}


def check_preset(jev, preset):
    errors, warns = jev.lint_preset(preset)
    for key, kind in REQUIRED_QUESTIONS.items():
        got = preset["questions"].get(key, {}).get("type")
        if got != kind:
            errors.append("%s: must exist with type %s (found %s). The rank stage reads it." % (key, kind, got))
    if "off_niche" not in (preset["questions"].get("topic_lane", {}).get("criteria") or {}):
        errors.append("topic_lane: the 'off_niche' label must stay. The rank stage reads its probability.")
    for key in ("icp_fit", "proof"):
        if len(preset["questions"].get(key, {}).get("criteria") or []) != 4:
            errors.append("%s: must keep exactly 4 ordered levels. The rank stage divides the score by 3." % key)
    return errors, warns


def run_jev(run, stage, pieces, preset, jev):
    key = jev.api_key()
    profile = creator_profile()
    fields = preset.get("state_fields") or list(REQUIRED_QUESTIONS)
    thresholds = preset.get("thresholds", {})

    def one(piece):
        row = {"_id": piece["id"]}
        try:
            out = jev.decide(jev_state(piece, profile, fields), preset["questions"], preset["model"], key)
            flat = jev.flatten_answers(out.get("answers", {}))
            row["_rules"] = jev.apply_rules(flat, preset.get("rules", []))
            row.update(flat)
            row["_sure"] = jev.is_sure(flat, preset["questions"], thresholds, 0.7)
            row["_cost"] = out.get("usage", {}).get("cost") or 0
        except Exception as e:
            row["_error"] = str(e)[:300]
        return row

    rows, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=JEV_CONCURRENCY) as pool:
        for i, row in enumerate(pool.map(one, pieces), 1):
            rows.append(row)
            if i % 25 == 0 or i == len(pieces):
                emit(run, stage, "progress", "Jev judged %d / %d" % (i, len(pieces)), done=i, total=len(pieces),
                     cost=round(sum(r.get("_cost", 0) for r in rows), 5))
    errors = [r for r in rows if "_error" in r]
    summary = {"items": len(rows), "errors": len(errors), "seconds": round(time.time() - t0, 1),
               "cost": round(sum(r.get("_cost", 0) for r in rows), 5),
               "unsure": sum(1 for r in rows if "_error" not in r and not r["_sure"])}
    if errors and len(errors) / len(rows) > MAX_JEV_ERROR_SHARE:
        emit(run, stage, "error", "Jev failed on %d of %d items: %s" % (len(errors), len(rows), errors[0]["_error"]))
        die("jev errors on %d / %d items. First: %s" % (len(errors), len(rows), errors[0]["_error"]), 2)
    return rows, summary


def pilot_sample(run, pieces):
    """Same sample every round of a run, spread across competitors, so rounds compare."""
    seed = int(hashlib.sha256(run.name.encode()).hexdigest()[:8], 16)
    shuffled = sorted(pieces, key=lambda p: p["id"])
    random.Random(seed).shuffle(shuffled)
    by_comp, sample = {}, []
    for p in shuffled:
        by_comp.setdefault(p["competitor"], []).append(p)
    while len(sample) < min(PILOT_SIZE, len(pieces)):
        for group in by_comp.values():
            if group and len(sample) < PILOT_SIZE:
                sample.append(group.pop())
    return sample


def diagnose(rows, preset):
    """Per-question confidence health. health = mean of the per-question health numbers (0..1)."""
    ok = [r for r in rows if "_error" not in r]
    questions, flags, healths = {}, [], []
    for q, spec in preset["questions"].items():
        kind = spec["type"]
        if kind == "noul":
            vals = [r[q] for r in ok if isinstance(r.get(q), (int, float))]
            unknown = sum(1 for v in vals if 0.35 < v < 0.65) / max(1, len(vals))
            questions[q] = {"type": kind, "mean": round(statistics.fmean(vals), 3) if vals else None,
                            "unknown_share": round(unknown, 3)}
            healths.append(1 - unknown)
            if unknown > 0.3:
                flags.append("%s: %d%% of answers sit in the unknown band (0.35 to 0.65). The statement is not checkable from the fields; name the cue." % (q, unknown * 100))
        else:
            confs = [r[q + "_conf"] for r in ok if isinstance(r.get(q + "_conf"), (int, float))]
            mean_conf = statistics.fmean(confs) if confs else 0
            labels = {}
            for r in ok:
                label = str(round(r[q])) if kind == "score" and isinstance(r.get(q), (int, float)) else str(r.get(q))
                labels[label] = labels.get(label, 0) + 1
            shares = {k: round(v / max(1, len(ok)), 3) for k, v in sorted(labels.items(), key=lambda kv: -kv[1])}
            questions[q] = {"type": kind, "mean_conf": round(mean_conf, 3), "shares": shares}
            healths.append(mean_conf)
            if mean_conf < 0.6:
                flags.append("%s: mean confidence %.2f is under 0.60. Labels overlap or lack an observable cue." % (q, mean_conf))
            top_label, top_share = next(iter(shares.items()), ("", 0))
            if top_share > 0.65:
                flags.append("%s: '%s' holds %d%% of items. Split it or tighten its neighbours." % (q, top_label, top_share * 100))
            if kind == "choice" and shares.get("other", 0) > 0.3:
                flags.append("%s: catch-all 'other' holds %d%% of items. A real label is missing." % (q, shares["other"] * 100))
    return {"health": round(statistics.fmean(healths), 4) if healths else 0, "questions": questions, "flags": flags}


def cmd_pilot(args):
    run = run_dir(args)
    pilots_path = run / "pilots.json"
    pilots = read_json(pilots_path) if pilots_path.exists() else []
    if args.resume and pilots:
        return print("[pilot] pilots.json exists, skipping")
    jev = load_jev()
    preset = jev.load_preset(str(run / "preset.json"))
    errors, _ = check_preset(jev, preset)
    if errors:
        die("preset has errors, fix them and run lint:\n  " + "\n  ".join(errors))
    pieces = read_json(run / "items.json")
    sample = pilot_sample(run, pieces)
    round_no = len(pilots) + 1
    emit(run, "pilot", "phase", "Calibrating Jev: pilot round %d on %d pieces (preset v%s)" % (round_no, len(sample), preset.get("version")))
    rows, summary = run_jev(run, "pilot", sample, preset, jev)
    diag = diagnose(rows, preset)
    pilots.append({"round": round_no, "preset_version": preset.get("version"), "n": len(sample), **summary, **diag})
    write_json(pilots_path, pilots)
    emit(run, "pilot", "done", "Pilot round %d: health %.3f, %d flags" % (round_no, diag["health"], len(diag["flags"])),
         round=round_no, health=diag["health"], flags=diag["flags"])

    print(json.dumps({"round": round_no, **diag}, indent=1))
    print("\nSample answers (read these for wrong calls at high confidence):")
    titles = {p["id"]: p for p in sample}
    for r in rows[:14]:
        if "_error" in r:
            continue
        p = titles[r["_id"]]
        print("- [%s/%s] %s" % (p["platform"], p["format"], p["title"][:90]))
        print("    lane=%s(%.2f) icp=%.1f(%.2f) hook=%s(%.2f) proof=%.1f replicable=%.2f sponsored=%.2f newsjack=%.2f"
              % (r["topic_lane"], r["topic_lane_conf"], r["icp_fit"], r.get("icp_fit_conf") or 0, r["hook_type"],
                 r["hook_type_conf"], r["proof"], r["replicable"], r["sponsored"], r["newsjack"]))


def cmd_lint(args):
    run = run_dir(args)
    jev = load_jev()
    errors, warns = check_preset(jev, jev.load_preset(str(run / "preset.json")))
    for e in errors:
        print("  ERROR  " + e)
    for w in warns:
        print("  warn   " + w)
    print("  clean" if not errors and not warns else "")
    sys.exit(1 if errors else 0)


def cmd_judge(args):
    run = run_dir(args)
    if args.resume and (run / "verdicts.jsonl").exists():
        return print("[judge] verdicts.jsonl exists, skipping")
    jev = load_jev()
    preset = jev.load_preset(str(run / "preset.json"))
    errors, _ = check_preset(jev, preset)
    if errors:
        emit(run, "judge", "warn", "Tuned preset failed lint, falling back to the base preset: %s" % errors[0])
        shutil.copyfile(BASE_PRESET, run / "preset.json")
        preset = jev.load_preset(str(run / "preset.json"))
    pieces = read_json(run / "items.json")
    emit(run, "judge", "phase", "Jev judging %d pieces (preset v%s)" % (len(pieces), preset.get("version")), total=len(pieces))
    rows, summary = run_jev(run, "judge", pieces, preset, jev)
    with open(run / "verdicts.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_json(run / "judge.json", summary)
    emit(run, "judge", "done", "Jev judged %d pieces in %.0fs for $%.4f (%d unsure, %d errors)"
         % (summary["items"], summary["seconds"], summary["cost"], summary["unsure"], summary["errors"]), **summary)
    # Reddit titles get the same Jev questions, so the ideas list is ranked on audience fit, not vote counts we cannot see.
    reddit_path = run / "reddit.json"
    if reddit_path.exists():
        reddit = read_json(reddit_path)
        if reddit["posts"]:
            fake = [{"id": r["id"], "platform": "reddit", "format": "post", "duration_s": 0, "title": r["title"], "description": ""} for r in reddit["posts"]]
            rows, _ = run_jev(run, "judge", fake, preset, jev)
            by_id = {r["_id"]: r for r in rows}
            for r in reddit["posts"]:
                v = by_id.get(r["id"], {})
                r["jev"] = {k: v.get(k) for k in ("topic_lane", "icp_fit", "hook_type", "replicable")} if "_error" not in v else None
            write_json(reddit_path, reddit)


# ----------------------------------------------------------------------------
# Rank
# ----------------------------------------------------------------------------
def percentile(value, sorted_values):
    return bisect.bisect_right(sorted_values, value) / len(sorted_values) if sorted_values else 0.0


def engagement_of(p):
    if p["likes"] is None and p["comments"] is None:
        return None
    return (p["likes"] or 0) + (p["comments"] or 0)


def adopt_if_better(run, pilots):
    """Keep a tuned preset as the new base only when the pilot says it judges with more confidence."""
    tuned = read_json(run / "preset.json")
    base = read_json(BASE_PRESET)
    if len(pilots) < 2 or tuned["questions"] == base["questions"] or pilots[-1]["health"] <= pilots[0]["health"] + 0.01:
        return False
    PRESET_HISTORY.mkdir(exist_ok=True)
    shutil.copyfile(BASE_PRESET, PRESET_HISTORY / ("competitor-hunt.v%s.json" % base.get("version")))
    tuned["version"] = int(base.get("version", 1)) + 1
    write_json(BASE_PRESET, tuned)
    write_json(run / "preset.json", tuned)
    return True


def cmd_rank(args):
    run = run_dir(args)
    pieces = read_json(run / "items.json")
    comps = read_json(run / "competitors.json")
    meta = read_json(run / "meta.json")
    pilots = read_json(run / "pilots.json") if (run / "pilots.json").exists() else []
    verdicts = {}
    for line in (run / "verdicts.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        verdicts[row["_id"]] = row
    weights = read_json(BRAIN).get("learning_weights", {})
    emit(run, "rank", "phase", "Ranking %d pieces" % len(pieces))

    # 1. Engagement rate and each channel's own baseline, per format bucket.
    buckets = {}
    for p in pieces:
        p["engagement"] = engagement_of(p)
        p["reach"] = p["views"] if p["kind"] == "video" else p["engagement"]
        p["er"] = round(p["engagement"] / p["views"] * 100, 3) if p["views"] and p["engagement"] is not None else None
        if p["reach"] is not None:
            buckets.setdefault((p["handle"], p["platform"], p["format"] if p["kind"] == "video" else "static"), []).append(p["reach"])
    medians = {k: statistics.median(v) for k, v in buckets.items() if len(v) >= 3}

    # 2. Paid-placement filter, before any ranking.
    for p in pieces:
        v = verdicts.get(p["id"], {})
        median = medians.get((p["handle"], p["platform"], p["format"] if p["kind"] == "video" else "static"))
        p["breakout"] = round(p["reach"] / median, 2) if median and p["reach"] is not None else None
        p["paid_reason"] = None
        if p["er"] is not None and p["er"] < PAID_ER_FLOOR:
            p["paid_reason"] = "ER %.2f%% is under the %.1f%% floor" % (p["er"], PAID_ER_FLOOR)
        elif p["er"] is not None and median and p["er"] < PAID_ER_RELATIVE and p["views"] > PAID_REACH_MULTIPLE * median:
            p["paid_reason"] = "ER %.2f%% with %.1fx the channel median reach" % (p["er"], p["views"] / median)
        elif isinstance(v.get("sponsored"), (int, float)) and v["sponsored"] >= PAID_JEV_SPONSORED:
            p["paid_reason"] = "Jev: dedicated sponsor piece (%.2f)" % v["sponsored"]
    organic = [p for p in pieces if not p["paid_reason"] and p["id"] in verdicts and "_error" not in verdicts[p["id"]]]

    # 3. Demand (what the numbers say) x fit (what Jev says) = hunt score.
    groups = {}
    for p in organic:
        groups.setdefault((p["platform"], p["kind"]), []).append(p)
    w_icp, w_proof = weights.get("icp_relevance", 1.0), weights.get("proof_potential", 1.0)
    w_fresh, w_rep = weights.get("timeliness", 1.0), weights.get("content_gap", 1.0)
    for group in groups.values():
        reach_sorted = sorted(p["reach"] for p in group if p["reach"] is not None)
        er_sorted = sorted(p["er"] for p in group if p["er"] is not None)
        for p in group:
            v = verdicts[p["id"]]
            lift = min(1.0, math.log2(1 + (p["breakout"] or 1.0)) / math.log2(9))  # 8x the channel median = full marks
            reach_pct = percentile(p["reach"], reach_sorted) if p["reach"] is not None else 0.0
            er_pct = percentile(p["er"], er_sorted) if p["er"] is not None else reach_pct
            demand = 0.5 * lift + 0.25 * er_pct + 0.25 * reach_pct
            # A news-pegged piece loses value as it ages; an evergreen one does not.
            fresh = 1 - 0.7 * v["newsjack"] * min(1.0, max(0.0, (p["age_days"] - 14) / 45))
            fit = (w_icp * v["icp_fit"] / 3 + w_proof * v["proof"] / 3 + w_rep * v["replicable"] + w_fresh * fresh) \
                / (w_icp + w_proof + w_rep + w_fresh)
            fit *= 1 - (v.get("topic_lane_probs") or {}).get("off_niche", 0)
            confs = [v[k] for k in ("topic_lane_conf", "icp_fit_conf") if isinstance(v.get(k), (int, float))]
            p.update({
                "demand": round(demand, 3), "fit": round(fit, 3), "score": round(100 * math.sqrt(demand * fit), 1),
                "jev": {"topic_lane": v["topic_lane"], "hook_type": v["hook_type"], "icp_fit": round(v["icp_fit"], 2),
                        "proof": round(v["proof"], 2), "replicable": round(v["replicable"], 2),
                        "sponsored": round(v["sponsored"], 2), "newsjack": round(v["newsjack"], 2),
                        "conf": round(statistics.fmean(confs), 2) if confs else None, "sure": v["_sure"]},
            })
    organic.sort(key=lambda p: -p["score"])
    for i, p in enumerate(organic, 1):
        p["rank"] = i

    # 4. Instagram CDN thumbnails expire and refuse hotlinking: keep a local copy of every one (filters can surface any rank).
    instagram = [p for p in organic if p["platform"] == "instagram"]
    local = keep_thumbs(run, instagram)
    for p in instagram:
        p["thumb"] = local.get(p["id"])

    # 5. Competitor ladders, organic pieces only.
    ladder = []
    for c in comps:
        mine = [p for p in organic if p["handle"] == c["handle"] and p["platform"] == c["platform"]]
        viewed = [p for p in mine if p["views"]]
        rated = [p for p in viewed if p["engagement"] is not None]
        ladder.append({
            "name": c["name"], "platform": c["platform"], "handle": c["handle"], "url": c.get("url"),
            "subs": c.get("subs"), "error": c.get("error"), "pieces": len(mine),
            "total_views": sum(p["views"] for p in viewed),
            "median_views": int(statistics.median([p["views"] for p in viewed])) if viewed else 0,
            "er": round(sum(p["engagement"] for p in rated) / sum(p["views"] for p in rated) * 100, 2) if rated else None,
            "best_score": mine[0]["score"] if mine else None, "top_piece": mine[0]["id"] if mine else None,
        })

    adopted = adopt_if_better(run, pilots)
    preset = read_json(run / "preset.json")
    judge = read_json(run / "judge.json") if (run / "judge.json").exists() else {}
    keep = ("id", "rank", "platform", "competitor", "handle", "format", "kind", "title", "url", "thumb", "published",
            "age_days", "duration_s", "views", "likes", "comments", "er", "breakout", "demand", "fit", "score", "jev")
    paid = sorted((p for p in pieces if p["paid_reason"]), key=lambda p: -(p["views"] or 0))
    reddit = read_json(run / "reddit.json") if (run / "reddit.json").exists() else {"posts": [], "blocked": [], "subs": []}
    reddit["posts"] = sorted(reddit["posts"], key=lambda r: -((r.get("jev") or {}).get("icp_fit") or 0))[:40]
    result = {
        "reddit": reddit,
        **meta, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {"competitors": len(comps), "pieces": len(pieces), "organic": len(organic), "paid": len(paid),
                   "jev_calls": judge.get("items", 0) + sum(p["items"] for p in pilots),
                   "jev_cost": round(judge.get("cost", 0) + sum(p["cost"] for p in pilots), 5),
                   "jev_seconds": judge.get("seconds"), "jev_unsure": judge.get("unsure"), "jev_errors": judge.get("errors")},
        "competitors": ladder,
        "pieces": [{k: p.get(k) for k in keep} for p in organic],
        "paid": [{**{k: p.get(k) for k in ("id", "platform", "competitor", "title", "url", "views", "er")}, "reason": p["paid_reason"]} for p in paid],
        "jev": {"model": preset.get("model"), "version": preset.get("version"), "adopted": adopted,
                "questions": preset["questions"], "thresholds": preset.get("thresholds", {}),
                "tuning_notes": preset.get("tuning_notes", []),
                "pilots": [{k: p[k] for k in ("round", "preset_version", "n", "health", "flags", "questions")} for p in pilots]},
    }
    if (run / "result.json").exists():   # a re-rank keeps the conductor record the server attached
        result["conductor"] = read_json(run / "result.json").get("conductor")
    write_json(run / "result.json", result)
    if not meta.get("own"):
        result["report"] = str(write_report(run, result))
        write_json(run / "result.json", result)
        emit(run, "rank", "log", "Report written to %s" % result["report"])
    emit(run, "rank", "done", "Ranked %d organic pieces, %d paid placements stripped. Top: %s"
         % (len(organic), len(paid), organic[0]["title"][:80] if organic else "none"), organic=len(organic), paid=len(paid))


def keep_thumbs(run, pieces, workers=16):
    """Download each piece's CDN thumbnail into runs/<id>/thumbs/. Returns {id: local url or None}."""
    thumbs = run / "thumbs"
    thumbs.mkdir(exist_ok=True)

    def keep(p):
        target = thumbs / (p["id"] + ".jpg")
        try:
            if not target.exists():
                req = urllib.request.Request(p["thumb"], headers={"User-Agent": UA})
                target.write_bytes(urllib.request.urlopen(req, timeout=10).read())
            return "/runs/%s/thumbs/%s.jpg" % (run.name, p["id"])
        except Exception:
            return None

    todo = [p for p in pieces if str(p.get("thumb") or "").startswith("http")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip((p["id"] for p in todo), pool.map(keep, todo)))


def cmd_thumbs(args):
    """Backfill missing Instagram thumbnails on a finished run from the CDN urls still in items.json."""
    run = Path(args.run)
    result = read_json(run / "result.json")
    src = {p["id"]: p.get("thumb") for p in read_json(run / "items.json")}
    missing = [p for p in result["pieces"] if p["platform"] == "instagram"
               and not (p.get("thumb") and (APP / p["thumb"].lstrip("/")).exists())]
    local = keep_thumbs(run, [{"id": p["id"], "thumb": src.get(p["id"])} for p in missing])
    got = 0
    for p in missing:
        if local.get(p["id"]):
            p["thumb"] = local[p["id"]]
            got += 1
    write_json(run / "result.json", result)
    emit(run, "rank", "log", "Thumbnails: %d of %d missing Instagram thumbnails recovered" % (got, len(missing)))
    print("thumbs: %d missing, %d recovered, %d gone from the CDN" % (len(missing), got, len(missing) - got))


def cmd_all(args):
    args.resume = True
    cmd_scrape(args)
    cmd_pilot(args)
    cmd_judge(args)
    cmd_rank(args)


def main():
    ap = argparse.ArgumentParser(prog="hunt.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("scrape", cmd_scrape), ("pilot", cmd_pilot), ("lint", cmd_lint), ("judge", cmd_judge),
                     ("rank", cmd_rank), ("thumbs", cmd_thumbs), ("all", cmd_all)):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True, help="run folder inside apps/competitor-hunter/runs/")
        p.add_argument("--resume", action="store_true", help="skip the stage when its output already exists")
        if name in ("scrape", "all"):
            p.add_argument("--days", type=int, default=80)
            p.add_argument("--platforms", default="youtube,instagram")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
