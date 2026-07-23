# AXIOM — your local AI companion

**🌐 Public, open-source project — free to clone, run, and deploy.**

A **local-first AI operating-system assistant**. Talk to it or type to it; it
reads and edits files, runs commands, inspects repositories, watches system
health, remembers things long-term, schedules tasks, and orchestrates
whatever local models you already have — all on your own machine.

> Renamed and extended from the original open-source project it was
> forked from ("JARVIS"). Same solid core, new name (**AXIOM**), simplified
> setup notes, and new built-in features (quick notes, daily briefing,
> free cloud deploy) — see [What's new](#whats-new-in-this-fork) below.

```
you>    what's eating my RAM?
   >    system_status(detail=True)
axiom>  Chrome, predictably — 41 of your 92 percent. The usual suspects follow.

you>    remind me to call the bank in 10 minutes
   >    schedule_task(when="in 10 minutes", prompt="remind: call the bank")
axiom>  Task #1 scheduled for 14:32.
```

No cloud accounts. No paid API keys. No camera gimmicks. A terminal, your
own models, your own machine.

---

## 100% free, forever — no exceptions

This project is designed so **nothing** in it costs money, from the first
`pip install` to running it every day:

- **The AI itself is a free, local model** you run yourself with
  [Ollama](https://ollama.com) (or LM Studio / any OpenAI-compatible local
  server) — no OpenAI/Anthropic/Google API key, no subscription, no usage
  billing. Your prompts never leave your computer.
- **Every tool** (weather, news, web search, file tools, hardware
  monitor, notes, scheduler...) calls either your local model or a free
  public endpoint (e.g. `wttr.in` for weather, Google News RSS for
  headlines) — nothing paid, nothing needing a key.
- **Every Python package** in `requirements.txt` is free and open-source.
- **"Deploying" AXIOM costs nothing** because it's built to run *on your own
  machine* — see [Running it long-term](#running-it-long-term-optional)
  below for how to keep it running in the background, still for free.

Email automation is the only optional feature that touches an external
account, and it's opt-in and free too (a Gmail **app password**, not a
paid API).

---

## What's new in this fork

Kept everything that made the original good, and added:

1. **Renamed & re-branded** — `AXIOM` throughout (binary, config folder
   `~/.axiom`, window title, persona), so it's clearly your own build.
2. **`notes` plugin** (`plugins/notes.py`) — quick sticky notes / to-do
   list, stored locally in `~/.axiom/notes.json`. Ask "note that I need to
   renew my domain" and later "what are my open notes?".
3. **`daily_briefing` plugin** (`plugins/daily_briefing.py`) — one command
   that stitches together weather, top headlines, machine health, your
   open notes, and scheduled tasks into a single morning summary. Combine
   it with the scheduler: *"every day at 8am give me my daily
   briefing."*
4. **Cleaned-up docs** — this README now has a plain step-by-step install
   section for Windows, macOS and Linux, a full folder tour, and an
   explicit "what's free / what's not" section so there's never any
   surprise.
5. **Free cloud deploy support** (`core/providers.py` + `Procfile` +
   `requirements-cloud.txt`) — added bearer-token auth so AXIOM can talk to
   a hosted, free API (Groq) instead of local Ollama, so it can run on a
   free Render/Railway web service with a permanent link, no local PC
   required. See [Cloud deploy](#cloud-deploy-free--link-stays-live-with-your-pc-off).

Nothing else about the internals changed — the agent loop, memory,
scheduler, and every existing tool work exactly as before, just verified
still passing the project's own test suite after the rename.

---

## Folder tour

```
AXIOM/
├── axiom.py                 entry point — run this file
│                           modes: REPL (default) / --server / --voice / --once / --check
├── requirements.txt        all free, open-source Python packages
├── start.bat                Windows one-click launcher (web UI)
├── .gitignore
│
├── core/                   the actual assistant
│   ├── assistant.py         core service: turn queue + event hub (no UI)
│   ├── cli.py                terminal frontend (the REPL you type into)
│   ├── server.py             web frontend — FastAPI + SSE + built-in chat/IDE UI
│   ├── agent.py               the loop: model → tool calls → results → model...
│   ├── models.py               discovers local models, checks capabilities, routes
│   ├── providers.py             talks to Ollama / any OpenAI-compatible local server
│   ├── memory.py                 SQLite: conversations, long-term facts, tasks, notes
│   ├── scheduler.py               timed / repeating tasks (no polling — timer-driven)
│   ├── watcher.py                  background RAM/CPU/disk/battery alerts
│   ├── voice.py                     optional mic input (VAD) + speech-to-text + text-to-speech
│   ├── persona.py                    AXIOM's system prompt (personality + rules)
│   ├── config.py                      defaults, overridden by ~/.axiom/config.json
│   └── tools/                         the assistant's "hands" — one file per tool group
│       ├── files.py                     read_file / write_file / edit_file / search
│       ├── shell.py                     run_command / open_app / close_app
│       ├── system.py                    system_status / screenshot / clipboard / input
│       ├── hardware.py                  CPU/GPU/RAM/disk/temperature report
│       ├── web.py                       web_search / fetch_url / weather / news_headlines
│       ├── dev.py                       git / github / repo tools
│       ├── memory_sched.py              remember / recall / profile / schedule_task
│       ├── email_.py                    optional Gmail automation (off by default)
│       ├── repo_scanner.py              repository structure scanner (coding agent)
│       └── code_index.py                symbol index (coding agent)
│
├── plugins/                 drop any .py file here to add your own tools — auto-loaded
│   ├── README.md              how to write a plugin (5-line example)
│   ├── coding_agent.py         wires the repo-aware coding tools into the agent
│   ├── notes.py                 NEW — quick notes / to-do list
│   └── daily_briefing.py        NEW — one-command morning summary
│
├── docs/
│   └── CODING_AGENT.md      details on the code-intelligence tools
│
└── tests/
    └── smoke.py             offline tests — no network, no models needed
```

Both frontends (terminal and web) are thin renderers over the same event
stream, so there's exactly one turn pipeline no matter how you talk to it.

---

## Install & run — step by step

### 1. Requirements

- **Python 3.10+** ([python.org](https://www.python.org/downloads/) — free)
- **Ollama** ([ollama.com](https://ollama.com) — free, runs models locally)

### 2. Get the code

```bash
# if you cloned/downloaded a zip, just cd into the extracted folder
cd AXIOM
```

### 3. Install dependencies (free, open-source only)

```bash
pip install -r requirements.txt
```

Text mode only needs `httpx` + `psutil` — if you just want the basic
assistant, `pip install httpx psutil` is enough to get started; the rest
(`fastapi`, `uvicorn`, voice packages, etc.) is only needed for the
features that use them, and everything degrades gracefully if a package
is missing.

### 4. Pull a free local model

```bash
ollama pull qwen3:8b
```

Any model works — AXIOM auto-discovers whatever Ollama (or LM Studio) has
installed and routes chat/code/vision requests to the best one it finds.
Smaller machine? Try `ollama pull qwen2.5:3b` instead.

### 5. Run it

| command | what it does |
|---|---|
| `python axiom.py` | text REPL — starts in well under a second |
| `python axiom.py --server` | web UI at `127.0.0.1:8765` (chat, model switcher, file/IDE panel) |
| `python axiom.py --voice` | also start always-on voice (mic in, speech out) |
| `python axiom.py --resume` | continue the previous conversation |
| `python axiom.py --once "summarize git log"` | one prompt, then exit |
| `python axiom.py --check` | diagnose dependencies, providers, config |

**Windows note:** if `python` opens the Microsoft Store, use `py axiom.py`,
or just double-click `start.bat`.

### 6. Try the new features

```
you>  note that I need to renew my domain on Aug 3
you>  what are my open notes?
you>  every day at 8am give me my daily briefing
```

---

## Putting this on GitHub

1. Go to [github.com](https://github.com) → sign in → click **+** (top
   right) → **New repository**.
2. Name it (e.g. `AXIOM`), set visibility to **Public** (so the free
   deploy step below can pull it), and click **Create repository**.
3. On the next page, click **"uploading an existing file"**.
4. Drag the whole extracted `AXIOM` folder's contents into the browser
   window, then scroll down and click **Commit changes**.

That's it — no `git` command line needed. Your code is now on GitHub and
ready for the deploy step below.

## Cloud deploy (free) — link stays live with your PC off

Everything above runs *on your machine*, so the link only works while your
PC is on. If you want a link that works even with your computer off —
to share with a friend or put in a portfolio — you can deploy AXIOM to a
free cloud host instead. The trade-off: it can no longer use *your*
Ollama, so it uses **Groq** instead — a hosted inference API that is
**free, no credit card**, and fast (it's not a trial, the free tier is
just... free).

### 1. Get a free Groq API key
Go to [console.groq.com/keys](https://console.groq.com/keys), sign up,
create a key. Copy it — you'll paste it once, into your host's
environment variables, never into code.

### 2. Make sure it's on GitHub
If you haven't already, follow [Putting this on GitHub](#putting-this-on-github)
above — Render deploys straight from a GitHub repo.

### 3. Deploy on Render (free tier)
1. [render.com](https://render.com) → sign up (free, no card) → **New → Web Service**
2. Connect your GitHub repo
3. Settings:
   - **Build Command:** `pip install -r requirements-cloud.txt`
   - **Start Command:** `python axiom.py --server --host 0.0.0.0 --no-browser`
   - **Instance type:** Free
4. Under **Environment**, add a variable:
   - `GROQ_API_KEY` = *the key you copied*
5. Deploy. Render gives you a permanent URL like
   `https://axiom-yourname.onrender.com` — share that, no tunnel needed.

(Railway and Fly.io both also have free/low-cost tiers and work the same
way if you'd rather use one of those — same three settings.)

### What's different in the cloud
- **Model:** routes to Groq's hosted Llama models instead of your local
  Ollama — still free, actually faster than most local setups.
- **Screen/voice tools** (`screenshot`, `type_text`, mic input, ...)
  report "unavailable" — a cloud server has no screen or microphone.
  Chat, files, web, notes, briefing, scheduler all work fully.
- **Free-tier limits (Render, as of 2026):** 750 free hours/month, and
  the service **sleeps after 15 minutes of no traffic** — the first
  request after that takes ~30-60s to wake it back up, then it's normal
  speed. Totally fine for sharing a demo link with a friend.
- **On-disk data (notes, memory) resets on redeploy/sleep** unless you
  add a paid persistent disk — worth knowing if you want it to remember
  things long-term rather than just for a demo session.
- The Groq endpoint is already wired into `core/config.py` — nothing to
  edit, it just activates once `GROQ_API_KEY` exists as an environment
  variable.
- Once it's live, pin a solid model in chat: `/model chat llama-3.3-70b-versatile`
  — reliable tool-calling and, on Groq's hardware, noticeably faster
  than any local setup.

## Running it long-term (optional)

AXIOM is a *local* app — there's no cloud "deploy" step, and keeping it
running 24/7 is still completely free:

- **Windows**: Task Scheduler → run `start.bat` at logon.
- **macOS/Linux**: a simple `systemd` user service or `cron @reboot`
  entry running `python axiom.py --server` keeps the web UI up in the
  background. `tmux`/`screen` work fine too for a quick, no-setup option.

None of these need a server, a hosting bill, or an account anywhere —
it's still just your machine, running for free.

---

## Model orchestration — no hardcoded models

At startup AXIOM asks every reachable provider what it serves (Ollama
first, plus any OpenAI-compatible endpoints in config), reads each
model's capabilities (tool calling, vision, thinking, size), and routes
by role:

| role   | picked by                                          |
|--------|----------------------------------------------------|
| chat   | tool-capable general model inside `chat_size_range_b` |
| code   | largest code-tuned model (qwen-coder, deepseek-coder, ...) |
| vision | largest vision-capable model (gemma3, llava, ...)  |

Pin any role manually with `/model code qwen2.5-coder:14b` in the REPL.

## Tools (the actual product)

| group   | tools |
|---------|-------|
| files   | `read_file` `read_pdf` `write_file` `edit_file` `list_dir` `glob_search` `grep_search` |
| shell   | `run_command` `open_app` `close_app` |
| system  | `system_status` `screenshot` `describe_screen` `read_image` `clipboard_get/set` `set_volume` `type_text` `press_keys` |
| hardware| `hardware_report` `gpu_status` |
| web     | `web_search` `fetch_url` `weather` `news_headlines` |
| dev     | `git` `github` `repo_scan` `code_index` `explain_architecture` `find_symbol` `trace_calls` `plan_task` `run_tests` |
| memory  | `remember` `recall_memory` `forget` `profile_set` `profile_forget` `export_chat` |
| tasks   | `schedule_task` `list_tasks` `cancel_task` |
| notes   | `add_note` `list_notes` `done_note` `delete_note` *(new)* |
| daily   | `daily_briefing` *(new)* |
| models  | `list_models` |
| email   | `email_unread` `email_read` `email_search` `email_send` `schedule_email` `email_digest` `email_draft_reply` — only when configured |

The agent chains tool calls until the task is done
(`max_tool_iterations` caps runaway loops).

## Memory — AXIOM actually learns who you are

Two layers, both persistent in SQLite:

- **profile** — key/value facts about *you* (name, preferences,
  projects), injected into every system prompt.
- **facts** — free-form knowledge, auto-recalled into context by
  relevance.

An **auto-memory pass** re-reads your message after every turn
(background thread, `"auto_memory": false` to disable) and saves
anything durable — you'll see `remembered: name = ...` notes as it
learns. Inspect and edit everything in the web UI's **memory** panel or
`/memory` in the terminal.

## Web UI

`--server` (or `start.bat`) serves a local single-file web app: streaming
chat with live thinking blocks, tool-call traces, a stop button, voice
toggle, chat export, and a full **IDE panel** — file tree, editor
(view/edit), inline AI suggestions, live diffs whenever AXIOM edits a file,
and a scoped "coding buddy" chat for the open file. Plus **file
attachments** (PDF, image, text) via `+` or drag-and-drop.

Code-intelligence tools (`repo_scan`, `code_index`,
`explain_architecture`, `find_symbol`, `trace_calls`, `plan_task`,
`run_tests`) are all local, all on-device — see `docs/CODING_AGENT.md`.

## Writing your own plugin

Drop a `.py` file into `plugins/` — it's auto-loaded at startup:

```python
# plugins/dice.py
import random

def register(registry, ctx):
    @registry.register("roll_dice", "Roll an N-sided die",
                       {"?sides": "integer: default 6"})
    def roll_dice(ctx, sides: int = 6):
        return f"Rolled a {random.randint(1, int(sides))} (d{sides})."
```

See `plugins/notes.py` and `plugins/daily_briefing.py` for two more
complete, real examples — including one plugin (`daily_briefing`) that
calls other tools through `registry.execute(...)`.

## Config

Everything lives in `~/.axiom/config.json`, generated on first run from the
defaults in `core/config.py`. Edit the JSON file directly to change
behaviour — missing keys fall back to defaults. Nothing in the defaults
requires a paid service — email automation is the only section that
needs *your own* credentials, and it's off until you fill it in.
