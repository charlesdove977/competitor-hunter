# Competitor Hunter

A local web app for creators. One click collects every post your competitors made in the last 80 days, has **Jev** (TypeSafe's decision model) judge each one, and shows you the ones most likely to work for your audience. **Claude Code** runs the hunt headlessly and tunes Jev's questions on a pilot batch. Save posts, transcribe them, watch them frame by frame, ask AI about them, and plan your next piece from your own analytics on the **My Studio** page.

No framework, no build step, no database. Python 3.10+ standard library, plain HTML/JS, your own API keys. Everything stays on your machine except the scrapes and the model calls.

![Light and dark, Apple-style, one blue accent](docs/screenshot-placeholder.txt)

## What you get

| Page | What it does |
|---|---|
| **Home** | Run a hunt. Live map of every competitor (x = breakout, y = views, size = engagement), ladders least to most, the top posts ranked by hunt score, Reddit ideas, Saved library, Ask AI, Settings |
| **Saved** | Bookmark any post into folders. Transcribe (local Whisper), Watch (ffmpeg frame sheets), Both, FFmpeg only. In-app player, carousel viewer with slide download. Claude writes "why it worked" |
| **Ask AI** | A chat over the posts you pick (saved posts, top 12 from the hunt, Reddit ideas, board cards). Later turns resume the same Claude session |
| **My Studio** (`/studio`) | Your own YouTube and Instagram numbers, your posts ranked on conversion x demand, AI top 10 topics per format, a Kanban board, scripts and thumbnail concepts in your voice, thumbnail downloads |

## How a hunt works

```
Run button
   |
server.py  -- spawns -->  claude -p --model <your choice>   (headless Claude Code, the conductor)
                             |
                             |  1. hunt.py scrape   YouTube Data API  +  Apify instagram-scraper  +  Reddit RSS
                             |  2. hunt.py pilot    Jev on a 40-piece sample -> confidence health + flags
                             |     Claude rewrites weak Jev criteria in runs/<id>/preset.json, lints, re-pilots (max 2 rounds)
                             |  3. hunt.py judge    Jev rules on every post (7 questions each)
                             |  4. hunt.py rank     paid filter, breakout, hunt score, ladders -> result.json
                             v
                    runs/<id>/events.jsonl  -- Server-Sent Events -->  the page
```

Jev makes every content judgment (topic lane, audience fit, hook type, proof, replicable, sponsored, news-pegged). Claude never judges a post; it only tunes the questions. If Claude Code is missing or stops early, the server finishes the remaining stages directly with the base preset and says so.

## Requirements

- **Python 3.10+** (no packages needed for the hunt itself)
- **Claude Code** CLI on PATH, logged in (`claude --version`)
- **claude-x-jev** skill: `npx claude-x-jev install --with-commands` (installs `~/.claude/skills/claude-x-jev/scripts/jev.py`, the one Jev caller)
- **OpenRouter** key with a few dollars of credit (Jev costs about $0.10 per hunt)
- **YouTube Data API v3** key (free)
- **Apify** token, only if you track Instagram competitors
- For Saved-post analysis: `yt-dlp`, `ffmpeg`, and `pip install openai-whisper`
- For My Studio: a Google OAuth refresh-token file for your channel and, optionally, an Instagram Login token (see below)

## Setup

```bash
git clone https://github.com/charlesdove977/competitor-hunter.git
cd competitor-hunter
cp .env.example .env            # fill in the keys
cp brain.example.json brain.json # who you are, your audience, your competitors
export OPENROUTER_API_KEY=sk-or-...   # or put it in .env
python3 server.py --open
```

Opens http://127.0.0.1:4317. Pick YouTube and/or Instagram, press **Find winning content**. You can add competitors from the page (paste a channel link, a profile link, or an @handle); they are stored in `roster.json` next to the brain.

Files that live elsewhere on your machine (an existing brain, env file, token) can be pointed at with `local.json` (copy `local.example.json`).

### YouTube OAuth token for My Studio

Create an OAuth client (Desktop) in Google Cloud with the YouTube Analytics API and YouTube Data API enabled, authorize the scopes `yt-analytics.readonly` and `youtube.readonly`, and save the result as `yt-token.json`:

```json
{ "client_id": "...", "client_secret": "...", "refresh_token": "...", "token_uri": "https://oauth2.googleapis.com/token" }
```

The app refreshes it on every pull and reads only your own channel. Subscribers gained per video is the number competitors cannot show you, and it is what My Studio ranks on.

### Instagram token for My Studio

An Instagram Login flavor access token for your own account (`graph.instagram.com`). Put it in `.env` as `INSTAGRAM_ACCESS_TOKEN`. Competitor Instagram data never uses this token; that goes through Apify.

## Scoring

- **Breakout** = a post's reach divided by that creator's own median for the same format in the window.
- **Paid filter**, applied before any ranking: engagement rate under 0.2% at any view count, or under 0.6% with over 3x the channel median, or Jev says the whole post is a dedicated sponsor promotion. These posts are listed, never ranked.
- **Demand** = 0.5 breakout (8x = full marks) + 0.25 engagement-rate percentile + 0.25 reach percentile, within the same platform.
- **Fit** = Jev's audience fit, proof and replicable scores plus freshness (news-pegged posts decay with age), weighted by `learning_weights` in the brain, scaled down by Jev's off-niche probability.
- **Hunt score** = 100 x sqrt(demand x fit). A post needs both.
- **Studio score** (your own posts) = 100 x sqrt(demand x conversion percentile), conversion being subs per 10K views on YouTube or saves + shares per 100 views on Instagram.

## Layout

| Path | What |
|---|---|
| `server.py` | HTTP server on 127.0.0.1:4317 only. Starts the conductor, streams events, serves the pages |
| `engine/hunt.py` | Scrape, pilot, judge, rank. Each stage runs alone and resumes |
| `engine/library.py` | Saved posts: yt-dlp, ffmpeg sheets, Whisper, carousel slides, Claude breakdowns |
| `engine/studio.py` | Your channels, topics, board, scripts, thumbnail concepts |
| `engine/*.prompt.txt` | The prompts Claude runs under |
| `jev/competitor-hunt.json` | The Jev question set. A tuned preset replaces it only when a pilot shows higher confidence |
| `web/` | The pages. No build step |
| `runs/`, `library/`, `studio/` | Your data, created on first use, ignored by git |

## Costs per full hunt

Jev about $0.10 (1,800 calls). Apify Instagram about $0.0023 per post returned. YouTube free. Claude Code conductor: a few turns on your subscription or API key. Saved-post breakdowns, topics and scripts: roughly $0.20 to $0.40 each API-equivalent on Opus-class models.

## Credits

Built by Charles J Dove (Charlie Automates). Jev is TypeSafe's decision model, served through OpenRouter; see [claude-x-jev](https://github.com/charlesdove977/claude-x-jev). MIT licensed.

## Reports hub

Every hunt ends by writing a markdown report, so the app and the terminal keep one shared record:

- competitor hunt → `<reports>/competitor-data/app/<date>-<run>/audit-<date>.md` + `topic-ideas-<date>.md` + `result.json`
- own-content analysis (My Studio) → `<reports>/my-social-media/app/<date>-<run>/audit-<date>.md` + `result.json`

`<reports>` defaults to `reports/` inside the app and is overridable with the `reports` key in `local.json`, so you can point it at a folder other tools already write to. The home page lists every `.md` in that folder newest first (any subfolder, any writer), opens each one in a drawer, and Ask AI can pull the latest three into a chat with the "Latest 3 reports" option. My Studio's topic generator reads the two newest reports from each side too.
