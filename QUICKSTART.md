# Quickstart: your first hunt in 20 minutes (YouTube only, no paid scraping)

This gets Competitor Hunter running on your machine with two free keys and three competitors. You end with a ranked list of your competitors' best YouTube videos on the map. Instagram, your own-channel analytics (My Studio) and the Claude-driven setup are covered in the full setup guide inside CC Strategic AI+.

## What you need

- Python 3.11 or newer (`python3 --version`)
- Claude Code installed and signed in (`claude --version`)
- A Google account (for a free YouTube Data API key)
- An OpenRouter account with a few dollars on it (Jev, the decision model that judges every post, costs about $0.11 per hunt)

## 1. Download and unpack

Download the zip from GitHub (Code, Download ZIP) or clone it:

```bash
git clone https://github.com/charlesdove977/competitor-hunter.git
cd competitor-hunter
```

## 2. YouTube Data API key (free, 5 minutes)

1. Open https://console.cloud.google.com and create a project (any name).
2. APIs and Services, Library, search "YouTube Data API v3", Enable.
3. APIs and Services, Credentials, Create credentials, API key.
4. Optional but smart: Edit the key, restrict it to the YouTube Data API v3.

Put it in a `.env` file next to `server.py`:

```
YOUTUBE_DATA_API_KEY=AIza...
```

The free quota is 10,000 units a day. One hunt over three channels uses a few hundred.

## 3. OpenRouter key for Jev

1. https://openrouter.ai, Keys, create one.
2. Add credit ($5 is plenty for weeks of hunts).

```
OPENROUTER_API_KEY=sk-or-...
```

Add that line to the same `.env`.

## 3b. The Jev caller (one command)

The app talks to Jev through the claude-x-jev skill's `jev.py`. Install it once:

```bash
npx claude-x-jev install --with-commands
```

That puts `jev.py` at `~/.claude/skills/claude-x-jev/scripts/jev.py`, which is where the app looks. If you keep it somewhere else, point `local.json` at it (`"jev_py": "/path/to/jev.py"`).

## 4. Tell it who you are

```bash
cp brain.example.json brain.json
```

Open `brain.json` and fill in:

- who you are and what you make (one or two sentences)
- your audience (who the content is for)
- three competitors: YouTube channel links or @handles

You can add more competitors later from inside the app.

## 5. Run it

```bash
python3 server.py --open
```

The app opens at http://127.0.0.1:4317. Under "Where to look" pick YouTube only, then press "Find winning content". The first hunt takes 2 to 5 minutes: collect, tune Jev on a pilot batch, judge every post, rank.

## 6. Read the result

- The map: every competitor placed by views (up) and breakout (right). Top right is who to watch.
- Best posts to learn from: ranked by how far each post beat its creator's own median and how well Jev thinks it fits your audience. Paid placements are left out.
- Filters above the cards: date range, breakout floor, topic, hook type, sort.
- Save a post to transcribe it, watch it, or ask Claude about it.

## If something fails

- "YOUTUBE_DATA_API_KEY is missing": the `.env` file is not next to `server.py`, or the line has quotes around the value. Remove the quotes.
- Jev errors: the OpenRouter key has no credit, or the key is wrong. Check https://openrouter.ai/credits.
- A channel came back with 0 pieces: the handle is wrong, or the channel has no uploads in the last 80 days.
- Port already in use: another copy is running. `lsof -ti:4317 | xargs kill` and start again.

## Optional extras (free, install yourself)

- Transcribe and Watch on saved posts need three public tools: `brew install yt-dlp ffmpeg` (or your OS equivalent) and `pip install openai-whisper`. Settings shows which ones are missing.
- Ask AI, "why it worked" breakdowns, Topics and Scripts need Claude Code on your PATH (`claude --version`). Without it the hunt still runs on the base Jev questions, without tuning.

## What the full guide adds (CC Strategic AI+)

Competitor Instagram via Apify, your own YouTube and Instagram analytics in My Studio, the viral-audit and viral-discover skills wired into the same reports folder, a Jev tuning playbook for your niche, and a setup prompt you paste into Claude Code that walks you through all of it one step at a time.
