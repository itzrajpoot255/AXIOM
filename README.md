# 🧠 AXIOM

**Your own AI assistant, running on your own machine.**

AXIOM is a local-first AI assistant for your desktop. Chat with it in a
terminal or a full web UI, and it can read and edit files, run commands,
browse the web, keep an eye on your system's health, remember things
about you long-term, schedule tasks and reminders, and manage quick
notes: all powered by a free AI model running on your own computer.

![Status](https://img.shields.io/badge/status-active-brightgreen)
![Made with](https://img.shields.io/badge/backend-Python-blue)
![Frontend](https://img.shields.io/badge/frontend-FastAPI%20%2B%20Web%20UI-black)
![AI](https://img.shields.io/badge/AI-Ollama%20%2F%20Groq-orange)
![Cost](https://img.shields.io/badge/cost-100%25%20free-success)

---

## Demo

```
you>   hello
axiom> Hello! It's nice to meet you, Rehan Rajpoot. I'm AXIOM, your
       local AI assistant. What can I help you with today?

you>   suggest me some good places in pakistan for trip
axiom> Pakistan has a rich cultural heritage and diverse landscapes.
       Here are some suggestions:
       1. Hunza Valley — stunning mountain scenery, almond forests
       2. Naran — scenic views, trekking, foot of the Himalayas
       3. Skardu — lakes, glaciers, base for treks near Nanga Parbat
       4. Lahore — Wazir Khan Mosque, Badshahi Mosque, Lahore Fort
       5. Karachi — Clifton Beach, Quaid-e-Azam Museum
```

---

## ✨ Features

| Feature | Description |
|---|---|
| 💬 **AI Chat** | Natural conversation in a terminal or web UI, backed by any local model you run through Ollama. |
| 📂 **File & Code Tools** | Reads, writes, and edits files, searches your filesystem, and runs shell commands on request. |
| 🖥️ **System Monitor** | Reports live CPU, RAM, disk, and hardware status on request, plus background alerts when resources run low. |
| 🌐 **Web Access** | Web search, page fetching, live weather, and news headlines. |
| 🧠 **Long-Term Memory** | Remembers facts about you (name, preferences, projects) across every conversation, stored locally. |
| ⏰ **Task Scheduler** | Set one-off or recurring reminders and scheduled prompts — "every morning at 8, summarize my unread email." |
| 📝 **Notes** | Quick sticky notes / to-do list you can add to and check from chat. |
| ☀️ **Daily Briefing** | One command that pulls together weather, headlines, system health, notes, and tasks into a single summary. |
| 👨‍💻 **Coding Assistant** | Repo-aware tools — scans a codebase, explains its architecture, finds symbols, traces calls, and runs tests. |
| 🔌 **Plugin System** | Drop a Python file into `plugins/` to add new tools — no core code changes needed. |

---

## 🛠️ Tech Stack

- **Language:** Python
- **Web UI:** FastAPI + a built-in single-page interface (streaming chat, file/IDE panel, model switcher)
- **AI models:** [Ollama](https://ollama.com) for free local models, with optional support for [Groq](https://groq.com/)'s free hosted API when deployed to the cloud
- **Storage:** SQLite (conversations, memory, tasks) + local JSON (notes, config)
- **Voice (optional):** Whisper for speech-to-text, Edge/SAPI for text-to-speech

---

## 📂 Project Structure

```
AXIOM/
├── axiom.py                entry point — run this file
├── requirements.txt         full dependency list
├── requirements-cloud.txt   lighter dependency list for cloud hosting
├── Procfile                 start command for cloud deploys
├── start.bat                Windows one-click launcher
│
├── core/
│   ├── assistant.py           core service — turn queue + event hub
│   ├── cli.py                  terminal chat interface
│   ├── server.py                web UI (FastAPI)
│   ├── agent.py                  the reasoning loop: model → tools → results
│   ├── models.py                  discovers models, routes by capability
│   ├── providers.py               talks to Ollama / any OpenAI-compatible API
│   ├── memory.py                   long-term memory (SQLite)
│   ├── scheduler.py                task scheduling
│   ├── persona.py                   AXIOM's personality and system prompt
│   ├── config.py                    settings, stored in ~/.axiom/config.json
│   └── tools/                       every tool the assistant can call
│
├── plugins/
│   ├── notes.py               quick notes / to-do list
│   └── daily_briefing.py      combined morning summary
│
├── docs/                    extra documentation
└── tests/
    └── smoke.py              automated tests
```

---

## 🚀 Getting Started

### 1. Requirements
- [Python 3.10+](https://www.python.org/downloads/)
- [Ollama](https://ollama.com) — runs the AI model locally, free

### 2. Clone and install
```bash
git clone https://github.com/<your-username>/AXIOM.git
cd AXIOM
pip install -r requirements.txt
```

### 3. Pull a model
```bash
ollama pull qwen2.5:3b
```
Any Ollama model works — AXIOM auto-detects what's installed.

### 4. Run it
```bash
python axiom.py              # terminal chat
python axiom.py --server     # web UI at http://127.0.0.1:8765
```
On Windows, `start.bat` launches the web UI with one click.

Pick your model once you're in:
```
/model chat qwen2.5:3b
```

Try it out:
```
note that I need to renew my domain on Aug 3
what are my open notes?
give me my daily briefing
```

---

## ☁️ Cloud Deployment (Free)

AXIOM normally runs on your own machine, which means it's only online
while your PC is on. To get a link that stays live even with your
computer off, you can deploy it to a free cloud host instead, using
[Groq](https://groq.com/)'s free hosted API in place of local Ollama.

1. Get a free API key at [console.groq.com/keys](https://console.groq.com/keys) — no credit card required.
2. Push this repo to GitHub.
3. On [Render](https://render.com), create a **New Web Service** from the repo:
   - **Build Command:** `pip install -r requirements-cloud.txt`
   - **Start Command:** `python axiom.py --server --host 0.0.0.0 --no-browser`
   - **Instance Type:** Free
   - **Environment Variable:** `GROQ_API_KEY` = your key
4. Deploy. You'll get a permanent URL like `https://axiom-yourname.onrender.com`.
5. Once live: `/model chat llama-3.3-70b-versatile`

**Notes on the free tier:** Render's free instance sleeps after 15
minutes of inactivity (next request takes ~30-60s to wake it), and
on-disk data resets on redeploy without a paid persistent disk — fine
for a demo link, worth knowing for anything long-term. Screen and
voice-related tools are unavailable on a headless server; everything
else (chat, files, web, notes, briefing, scheduler) works normally.

---

## 🧩 Adding Your Own Tools

Drop a Python file into `plugins/` and it's loaded automatically:

```python
# plugins/dice.py
import random

def register(registry, ctx):
    @registry.register("roll_dice", "Roll an N-sided die",
                       {"?sides": "integer: default 6"})
    def roll_dice(ctx, sides: int = 6):
        return f"Rolled a {random.randint(1, int(sides))} (d{sides})."
```

`plugins/notes.py` and `plugins/daily_briefing.py` are complete, working
examples to build from.

---

## ⚙️ Configuration

All settings live in `~/.axiom/config.json`, generated automatically on
first run from the defaults in `core/config.py`. Edit the JSON file
directly to change model routing, memory behaviour, watcher thresholds,
and more.

---

## 👤 Author

**Rehan Rajpoot**

---

## 📄 License

This project is open source and free to use, modify, and share.
