#!/usr/bin/env python3
"""
My Studio engine: your own channels through the same lens as the competitor hunt, plus topics, board and scripts.

    python3 studio.py hunt   --run <runs/me-...> --days 80 --platforms youtube,instagram
    python3 studio.py topics --format longform|short
    python3 studio.py make   --card <id> --what script|thumbnails

Own data comes from first-party APIs only: YouTube OAuth (yt-token.json) for subscribers gained,
retention and watch time, the YouTube Data API key for the upload list, and the Instagram Graph token for reach,
saves and shares. Jev judges the posts with the competitor preset; rank reuses hunt.py and then re-scores on the
one number competitors cannot show: conversion (subs per 10K views on YouTube, saves + shares per 100 views on Instagram).
"""
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hunt  # noqa: E402
import library  # noqa: E402

APP, RUNS = hunt.APP, hunt.RUNS
STUDIO = APP / "studio"
BOARD = STUDIO / "board.json"
TOPICS = STUDIO / "topics"
CARDS = STUDIO / "cards"
YT_TOKEN = hunt._path("yt_token", APP / "yt-token.json")      # Google OAuth refresh token file (see README)
IG_ENV = hunt._path("instagram_env", hunt.ENV_FILE)          # INSTAGRAM_ACCESS_TOKEN lives in the env file
VOICE = hunt._path("voice", APP / "voice.md")                 # optional voice fingerprint for scripts
TOPICS_PROMPT = APP / "engine" / "topics.prompt.txt"
SCRIPT_PROMPT = APP / "engine" / "script.prompt.txt"
YT_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
IG_API = "https://graph.instagram.com/v21.0/"
STATUSES = ("ideas", "chosen", "scripted", "filmed", "posted")
FORMATS = {"longform": "a longform YouTube video", "short": "a short (YouTube Short or Instagram Reel)"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def own_latest():
    done = sorted(RUNS.glob("me-*/result.json"), key=lambda p: p.stat().st_mtime)
    return done[-1] if done else None


# ----------------------------------------------------------------------------
# Pull: your own channels
# ----------------------------------------------------------------------------
def ig_env():
    out = {}
    for line in IG_ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def yt_access_token():
    t = hunt.read_json(YT_TOKEN)
    body = urllib.parse.urlencode({"client_id": t["client_id"], "client_secret": t["client_secret"],
                                   "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(t["token_uri"], data=body, headers={"User-Agent": hunt.UA})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())["access_token"]


def pull_youtube(run, cutoff, days):
    token = yt_access_token()
    bearer = {"Authorization": "Bearer " + token}
    channel = hunt.http_json(hunt.YT_API + "channels?part=snippet,statistics,contentDetails&mine=true", headers=bearer)["items"][0]
    comp = {"name": channel["snippet"]["title"], "platform": "youtube", "handle": "@me", "source": "me",
            "subs": hunt.count(channel["statistics"].get("subscriberCount")), "url": "https://www.youtube.com/channel/" + channel["id"]}
    pieces = hunt.scrape_youtube_channel({"name": comp["name"], "handle": channel["id"], "platform": "youtube"},
                                         hunt.env_value("YOUTUBE_DATA_API_KEY"), cutoff)
    end, start = date.today(), date.today() - timedelta(days=days)
    query = urllib.parse.urlencode({"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
                                    "metrics": "views,estimatedMinutesWatched,averageViewPercentage,subscribersGained,likes,shares,comments",
                                    "dimensions": "video", "sort": "-views", "maxResults": 200})
    report = hunt.http_json(YT_ANALYTICS + "?" + query, headers=bearer)
    cols = [c["name"] for c in report["columnHeaders"]]
    by_video = {row[0]: dict(zip(cols, row)) for row in report.get("rows", [])}
    for p in pieces:
        p["competitor"], p["handle"] = comp["name"], "@me"
        a = by_video.get(p["id"][3:])
        if a:
            p["own"] = {"subs_gained": a["subscribersGained"], "avg_view_pct": a["averageViewPercentage"], "minutes": a["estimatedMinutesWatched"],
                        "shares": a["shares"], "subs_per_10k": round(a["subscribersGained"] / a["views"] * 10000, 2) if a["views"] else 0}
    traffic = hunt.http_json(YT_ANALYTICS + "?" + urllib.parse.urlencode({"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(),
                                                                          "metrics": "views", "dimensions": "insightTrafficSourceType"}), headers=bearer)
    ad_views = sum(row[1] for row in traffic.get("rows", []) if row[0] == "ADVERTISING")
    hunt.emit(run, "scrape", "target", "%s (you): %d videos, %s subscribers gained" % (comp["name"], len(pieces), sum(p.get("own", {}).get("subs_gained", 0) for p in pieces)),
              name=comp["name"], platform="youtube", pieces=len(pieces))
    return comp, pieces, {"ad_views": ad_views}


def ig_insights(media_id, token):
    try:
        data = hunt.http_json(IG_API + "%s/insights?metric=views,reach,saved,shares&access_token=%s" % (media_id, token))
        return {d["name"]: d["values"][0]["value"] for d in data["data"]}
    except Exception:
        return {}


def pull_instagram(run, cutoff):
    env = ig_env()
    token = env["INSTAGRAM_ACCESS_TOKEN"]
    me = hunt.http_json(IG_API + "me?fields=id,username,followers_count&access_token=" + token)
    comp = {"name": "@" + me["username"], "platform": "instagram", "handle": "@" + me["username"], "source": "me",
            "subs": me.get("followers_count"), "url": "https://www.instagram.com/%s/" % me["username"]}
    url = IG_API + "me/media?fields=id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count,thumbnail_url,media_url&limit=50&access_token=" + token
    pieces = []
    while url:
        page = hunt.http_json(url)
        stop = False
        for m in page.get("data", []):
            stamp = re.sub(r"\+0000$", "+00:00", m["timestamp"])
            if hunt.parse_iso(stamp) < cutoff:
                stop = True
                break
            is_video = m["media_type"] == "VIDEO"
            ins = ig_insights(m["id"], token)
            caption = m.get("caption") or ""
            pieces.append({
                "id": "ig_" + m["id"], "platform": "instagram", "competitor": comp["name"], "handle": comp["handle"],
                "format": "reel" if is_video else ("carousel" if m["media_type"] == "CAROUSEL_ALBUM" else "image"),
                "kind": "video" if is_video else "static", "title": caption.strip().split("\n")[0][:140], "description": caption,
                "url": m.get("permalink"), "thumb": m.get("thumbnail_url") or m.get("media_url"), "published": stamp, "duration_s": 0,
                "views": ins.get("views") if is_video else None, "likes": hunt.count(m.get("like_count")), "comments": hunt.count(m.get("comments_count")),
                "own": {"reach": ins.get("reach"), "saves": ins.get("saved"), "shares": ins.get("shares"), "views_all": ins.get("views")},
            })
        url = None if stop else (page.get("paging") or {}).get("next")
    hunt.emit(run, "scrape", "target", "%s (you): %d posts, %s saves" % (comp["name"], len(pieces), sum(p["own"].get("saves") or 0 for p in pieces)),
              name=comp["name"], platform="instagram", pieces=len(pieces))
    return comp, pieces


def cmd_pull(args):
    run = hunt.run_dir(args)
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    now_dt = datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=args.days)
    hunt.emit(run, "scrape", "phase", "Pulling your own channels, last %d days" % args.days, competitors=[{"name": "You", "platform": p} for p in platforms])
    comps, pieces, meta = [], [], {}
    if "youtube" in platforms:
        comp, got, extra = pull_youtube(run, cutoff, args.days)
        comps.append(comp); pieces += got; meta.update(extra)
    if "instagram" in platforms:
        comp, got = pull_instagram(run, cutoff)
        comps.append(comp); pieces += got
    if not pieces:
        hunt.emit(run, "scrape", "error", "Nothing came back from your channels")
        hunt.die("no pieces")
    for p in pieces:
        p["age_days"] = max(0, (now_dt - hunt.parse_iso(p["published"])).days)
        p.setdefault("images", [])
    hunt.write_json(run / "items.json", pieces)
    hunt.write_json(run / "competitors.json", comps)
    hunt.write_json(run / "meta.json", {"run_id": run.name, "window_days": args.days, "platforms": platforms, "own": True, **meta,
                                        "scraped_at": now_dt.isoformat(timespec="seconds")})
    hunt.write_json(run / "reddit.json", {"posts": [], "blocked": [], "subs": []})
    if not (run / "preset.json").exists():
        shutil.copyfile(hunt.BASE_PRESET, run / "preset.json")
    hunt.emit(run, "scrape", "done", "Pulled %d of your posts" % len(pieces), pieces=len(pieces), competitors=len(comps))


def postprocess(run):
    """Re-score your posts on conversion x demand, and total up the analytics the page shows."""
    result = hunt.read_json(run / "result.json")
    own = {i["id"]: i.get("own") or {} for i in hunt.read_json(run / "items.json")}
    for p in result["pieces"]:
        o = own.get(p["id"], {})
        if p["platform"] == "youtube":
            p["conversion"] = o.get("subs_per_10k")
            p["conversion_label"] = "subs per 10K views"
        else:
            total = (o.get("saves") or 0) + (o.get("shares") or 0)
            views = p.get("views") or o.get("views_all")
            p["conversion"] = round(total / views * 100, 2) if views else None
            p["conversion_label"] = "saves + shares per 100 views"
        p["own"] = o
    for platform in ("youtube", "instagram"):
        group = [p for p in result["pieces"] if p["platform"] == platform and p.get("conversion") is not None]
        ranked = sorted(x["conversion"] for x in group)
        for p in group:
            pct = hunt.percentile(p["conversion"], ranked)
            p["conv_pct"] = round(pct, 3)
            p["hunt_score"] = p["score"]
            p["score"] = round(100 * math.sqrt(max(p["demand"], 0.01) * max(pct, 0.02)), 1)
    result["pieces"].sort(key=lambda p: -p["score"])
    for i, p in enumerate(result["pieces"], 1):
        p["rank"] = i
    items = hunt.read_json(run / "items.json")
    yt = [i for i in items if i["platform"] == "youtube"]
    ig = [i for i in items if i["platform"] == "instagram"]
    result["mine"] = {
        "youtube": {"videos": len(yt), "views": sum(i.get("views") or 0 for i in yt), "subs_gained": sum((i.get("own") or {}).get("subs_gained", 0) for i in yt),
                    "minutes": sum((i.get("own") or {}).get("minutes", 0) for i in yt), "ad_views": hunt.read_json(run / "meta.json").get("ad_views", 0),
                    "subs": next((c.get("subs") for c in result["competitors"] if c["platform"] == "youtube"), None)} if yt else None,
        "instagram": {"posts": len(ig), "views": sum(i.get("views") or 0 for i in ig), "reach": sum((i.get("own") or {}).get("reach") or 0 for i in ig),
                      "saves": sum((i.get("own") or {}).get("saves") or 0 for i in ig), "shares": sum((i.get("own") or {}).get("shares") or 0 for i in ig),
                      "followers": next((c.get("subs") for c in result["competitors"] if c["platform"] == "instagram"), None)} if ig else None,
    }
    hunt.write_json(run / "result.json", result)
    result["report"] = str(hunt.write_report(run, result))
    hunt.write_json(run / "result.json", result)
    hunt.emit(run, "rank", "log", "Report written to %s" % result["report"])


def cmd_hunt(args):
    cmd_pull(args)
    ns = SimpleNamespace(run=args.run, resume=True)
    hunt.cmd_judge(ns)
    hunt.cmd_rank(ns)
    postprocess(Path(args.run))
    hunt.emit(Path(args.run), "rank", "done", "Your posts are scored on conversion and demand", organic=0, paid=0)


# ----------------------------------------------------------------------------
# Topics
# ----------------------------------------------------------------------------
def claude_json(prompt, cwd):
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("Claude Code is not on PATH.")
    out = subprocess.run([claude, "-p", "--model", hunt.settings()["model"], "--output-format", "json", "--restricted", "--strict-mcp-config",
                          "--disable-slash-commands", "--permission-mode", "dontAsk", "--no-session-persistence", prompt],
                         cwd=str(cwd), capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    data = json.loads(out.stdout)
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result"))[:300])
    text = str(data.get("result") or "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]), data.get("total_cost_usd")


def board():
    return hunt.read_json(BOARD) if BOARD.exists() else {"cards": []}


def save_board(data):
    STUDIO.mkdir(exist_ok=True)
    hunt.write_json(BOARD, data)
    return data


def topics_pack(fmt):
    pack = {"format": FORMATS[fmt], "my_posts": [], "saved_competitor_posts": [], "top_competitor_pieces": [], "reddit": [], "already_on_board": []}
    mine = own_latest()
    if mine:
        for p in hunt.read_json(mine)["pieces"][:15]:
            pack["my_posts"].append({k: p.get(k) for k in ("title", "platform", "format", "url", "views", "breakout", "er", "conversion", "conversion_label", "jev")})
    for post in library.load()["posts"]:
        piece, a = post["piece"], post.get("analysis") or {}
        entry = {k: piece.get(k) for k in ("title", "competitor", "platform", "format", "url", "views", "breakout", "er", "jev")}
        if a.get("transcript"):
            entry["spoken_hook"] = a["transcript"].get("hook")
        md = library.post_dir(post["id"]) / "breakdown.md"
        if md.exists():
            entry["breakdown"] = md.read_text(encoding="utf-8")[:700]
        pack["saved_competitor_posts"].append(entry)
    latest = sorted((p for p in RUNS.glob("*/result.json") if not p.parent.name.startswith("me-")), key=lambda p: p.stat().st_mtime)
    if latest:
        result = hunt.read_json(latest[-1])
        pack["top_competitor_pieces"] = [{k: p.get(k) for k in ("title", "competitor", "platform", "format", "url", "views", "breakout", "er", "score", "jev")} for p in result["pieces"][:10]]
        pack["reddit"] = [{"title": r["title"], "url": r["url"]} for r in result.get("reddit", {}).get("posts", [])[:10]]
    pack["already_on_board"] = [c["title"] for c in board()["cards"]]
    # the latest two reports from each side of the hub (viral-discover / viral-audit / app runs), excerpted
    pack["reports"] = []
    for side in ("competitor-data", "my-social-media"):
        for r in [x for x in hunt.list_reports() if x["source"] == side][:2]:
            pack["reports"].append({"name": r["rel"], "excerpt": (hunt.REPORTS / r["rel"]).read_text(encoding="utf-8")[:3500]})
    return pack


def cmd_topics(args):
    TOPICS.mkdir(parents=True, exist_ok=True)
    prompt = TOPICS_PROMPT.read_text(encoding="utf-8").replace("{FORMAT}", FORMATS[args.format]).replace("{CREATOR}", hunt.creator_profile()) + json.dumps(topics_pack(args.format), ensure_ascii=False)
    data, cost = claude_json(prompt, TOPICS)
    ideas = [i for i in data.get("ideas", []) if i.get("title")][:10]
    if not ideas:
        raise RuntimeError("Claude returned no ideas")
    report = {"id": uuid.uuid4().hex[:8], "format": args.format, "created": now(), "model": hunt.settings()["model"], "cost_usd": cost, "ideas": ideas}
    path = TOPICS / ("%s-%s-%s.json" % (datetime.now().strftime("%Y-%m-%d"), args.format, report["id"]))
    hunt.write_json(path, report)
    print(path)


# ----------------------------------------------------------------------------
# Board
# ----------------------------------------------------------------------------
def add_card(idea, fmt, source=""):
    data = board()
    card = {"id": uuid.uuid4().hex[:8], "title": str(idea.get("title", ""))[:140], "format": fmt if fmt in FORMATS else "longform",
            "hook": str(idea.get("hook", ""))[:400], "angle": str(idea.get("angle", ""))[:600], "why": str(idea.get("why", ""))[:600],
            "inspiration": [{"title": str(i.get("title", ""))[:140], "url": str(i.get("url", ""))[:300]} for i in (idea.get("inspiration") or [])][:8],
            "status": "ideas", "done": False, "notes": "", "created": now(), "source": source, "assets": {}}
    data["cards"].insert(0, card)
    save_board(data)
    return card


def update_card(card_id, fields):
    data = board()
    card = next((c for c in data["cards"] if c["id"] == card_id), None)
    if not card:
        raise ValueError("No such card.")
    for key in ("title", "hook", "angle", "why", "notes"):
        if key in fields:
            card[key] = str(fields[key])[:2000]
    if "status" in fields and fields["status"] in STATUSES:
        card["status"] = fields["status"]
    if "done" in fields:
        card["done"] = bool(fields["done"])
    if "format" in fields and fields["format"] in FORMATS:
        card["format"] = fields["format"]
    if "inspiration" in fields:
        card["inspiration"] = [{"title": str(i.get("title", ""))[:140], "url": str(i.get("url", ""))[:300]} for i in fields["inspiration"] if isinstance(i, dict)][:12]
    save_board(data)
    return card


def delete_card(card_id):
    data = board()
    data["cards"] = [c for c in data["cards"] if c["id"] != card_id]
    save_board(data)
    folder = CARDS / card_id
    if re.fullmatch(r"[0-9a-f]{8}", card_id) and folder.exists():
        shutil.rmtree(folder)


# ----------------------------------------------------------------------------
# Make it: script, thumbnail concepts, thumbnail downloads
# ----------------------------------------------------------------------------
def card_dir(card_id):
    if not re.fullmatch(r"[0-9a-f]{8}", card_id):
        raise ValueError("bad card id")
    folder = CARDS / card_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def inspiration_pack(card):
    urls = {i["url"] for i in card.get("inspiration", []) if i.get("url")}
    out = []
    for post in library.load()["posts"]:
        if post["piece"].get("url") not in urls:
            continue
        folder = library.post_dir(post["id"])
        entry = {k: post["piece"].get(k) for k in ("title", "competitor", "platform", "format", "url", "views", "breakout", "er", "jev", "description")}
        if (folder / "transcript.json").exists():
            entry["transcript"] = hunt.read_json(folder / "transcript.json")["text"][:4000]
        if (folder / "breakdown.md").exists():
            entry["breakdown"] = (folder / "breakdown.md").read_text(encoding="utf-8")[:3000]
        out.append(entry)
    return out


def cmd_make(args):
    card = next((c for c in board()["cards"] if c["id"] == args.card), None)
    if not card:
        raise ValueError("No such card.")
    folder = card_dir(card["id"])
    output = "script.md" if args.what == "script" else "thumbnails.md"
    voice = VOICE.read_text(encoding="utf-8")[:6000] if VOICE.exists() else "No fingerprint on file: write plain, direct, warm, like texting a friend who is good at this."
    prompt = (SCRIPT_PROMPT.read_text(encoding="utf-8").replace("{CREATOR}", hunt.creator_profile()) + "\nOUTPUT: %s\n\nVOICE:\n%s\n\nCARD:\n%s\n\nINSPIRATION:\n%s\n"
              % (output, voice, json.dumps(card, ensure_ascii=False), json.dumps(inspiration_pack(card), ensure_ascii=False)))
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("Claude Code is not on PATH.")
    (folder / output).unlink(missing_ok=True)
    out = subprocess.run([claude, "-p", prompt, "--model", hunt.settings()["model"], "--output-format", "json", "--setting-sources", "project",
                          "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--permission-mode", "acceptEdits",
                          "--allowedTools", "Read,Write"], cwd=str(folder), capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
    if not (folder / output).exists():
        raise RuntimeError("Claude did not write %s: %s" % (output, (out.stderr or out.stdout).strip()[-300:]))
    try:
        cost = json.loads(out.stdout).get("total_cost_usd")
    except ValueError:
        cost = None
    data = board()
    live = next(c for c in data["cards"] if c["id"] == card["id"])
    live.setdefault("assets", {})[args.what] = {"file": output, "made": now(), "cost_usd": cost, "model": hunt.settings()["model"]}
    if args.what == "script" and live["status"] in ("ideas", "chosen"):
        live["status"] = "scripted"
    save_board(data)
    print(folder / output)


def download_thumbnails(card_id):
    """Inspiration thumbnails into ~/Downloads/<card title>/: YouTube maxres from the link, Instagram from the saved copy."""
    card = next((c for c in board()["cards"] if c["id"] == card_id), None)
    if not card:
        raise ValueError("No such card.")
    target = library.DOWNLOADS / (library.slug(card["title"]) or card_id)
    target.mkdir(parents=True, exist_ok=True)
    saved = {p["piece"].get("url"): p for p in library.load()["posts"]}
    count = 0
    for i, insp in enumerate(card.get("inspiration", []), 1):
        url = insp.get("url") or ""
        video = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url)
        if video:
            if library.fetch_image("https://i.ytimg.com/vi/%s/maxresdefault.jpg" % video.group(1), target / ("%02d-youtube.jpg" % i)) \
                    or library.fetch_image("https://i.ytimg.com/vi/%s/hqdefault.jpg" % video.group(1), target / ("%02d-youtube.jpg" % i)):
                count += 1
        elif url in saved:
            post = saved[url]
            local = library.post_dir(post["id"])
            first = sorted(local.glob("slide-*.jpg")) + sorted(local.glob("hook.jpg"))
            if first:
                shutil.copyfile(first[0], target / ("%02d-instagram.jpg" % i))
                count += 1
    return str(target), count


def main():
    ap = argparse.ArgumentParser(prog="studio.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("pull", cmd_pull), ("hunt", cmd_hunt)):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        p.add_argument("--days", type=int, default=80)
        p.add_argument("--platforms", default="youtube,instagram")
        p.add_argument("--resume", action="store_true")
        p.set_defaults(fn=fn)
    p = sub.add_parser("topics")
    p.add_argument("--format", required=True, choices=list(FORMATS))
    p.set_defaults(fn=cmd_topics)
    p = sub.add_parser("make")
    p.add_argument("--card", required=True)
    p.add_argument("--what", required=True, choices=("script", "thumbnails"))
    p.set_defaults(fn=cmd_make)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
