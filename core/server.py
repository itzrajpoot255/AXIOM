"""Web frontend -- FastAPI server + embedded chat UI.

One process serves everything: the agent, voice, and a local web page.
The page is a thin renderer over GET /api/events (SSE) -- the same
event stream the terminal frontend consumes, so there is exactly one
turn pipeline regardless of frontend.

Endpoints:
    GET  /                  chat UI (self-contained HTML, no CDN)
    GET  /api/events        SSE event stream (tokens, thinking, tools, ...)
    POST /api/chat          {"text": ...} -> {"turn_id": ...}
    POST /api/stop          cancel the in-flight turn
    GET  /api/history       current session transcript (for page reloads)
    GET  /api/changes       recent agent file edits (diffs) for the IDE view
    GET  /api/export        download the session transcript as markdown
    GET  /api/memory        profile + facts AXIOM has learned
    POST /api/memory/profile  {"key", "value"}  (empty value deletes)
    POST /api/memory/fact     {"content", "topic"?}
    POST /api/memory/forget   {"id"}
    GET  /api/status        models/routes/voice/session snapshot
    POST /api/voice         {"on": true|false}
    POST /api/upload        {"name", "data_b64"} -> saved path (pdf/image/...)
    GET  /api/sessions      past conversations (id, title, date, count)
    POST /api/session/load  {"id"} -> continue an earlier conversation
    POST /api/session/new   fresh conversation
    GET  /api/models        discovery summary (?refresh=1 to re-scan)
    POST /api/model         {"role": "chat", "name": "qwen3:8b"|"auto"}
    GET  /api/tools         list of registered tool names
    GET  /api/models/list   structured model list for the switcher UI
    GET  /api/workspace     directory listing  (?path=...)
    GET  /api/workspace/file   read a file     (?path=...)
    POST /api/workspace/file   save a file     {"path", "content"}
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import socket
import threading
import time
import webbrowser

from .assistant import Assistant
from .config import DATA_DIR, config

_MAX_EDIT_BYTES = 2_000_000


def _strip_code_fence(text: str) -> str:
    """Remove a single wrapping ```lang ... ``` fence if the model added one."""
    t = (text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.rstrip("\n") + "\n" if t.strip() else ""


def create_app(assistant: Assistant):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, Response, StreamingResponse

    app = FastAPI(title="AXIOM", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE

    @app.get("/api/events")
    def events():
        def stream():
            q = assistant.subscribe()
            try:
                yield f"data: {json.dumps({'type': 'hello'})}\n\n"
                while True:
                    try:
                        ev = q.get(timeout=15)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                assistant.unsubscribe(q)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})

    @app.post("/api/chat")
    def chat(body: dict):
        text = (body.get("text") or "").strip()
        if not text:
            return {"error": "empty"}
        return {"turn_id": assistant.submit(text, source="text")}

    @app.post("/api/stop")
    def stop():
        assistant.agent.cancel.set()
        return {"ok": True}

    @app.get("/api/history")
    def history(limit: int = 60):
        msgs = [{"role": m["role"], "content": m["content"]}
                for m in assistant.memory.recent_messages(max(1, min(200, limit)))
                if m["content"].strip()]
        return {"messages": msgs, "session": assistant.memory.session_id}

    @app.get("/api/changes")
    def changes():
        return {"changes": assistant.file_changes}

    @app.get("/api/export")
    def export():
        name, text = assistant.memory.export_markdown()
        return Response(content=text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{name}"'})

    # ── Memory: what AXIOM knows about the user ─────────────────

    @app.get("/api/memory")
    def memory():
        return {"profile": assistant.memory.profile_all(),
                "facts": [{"id": fid, "content": content, "topic": topic}
                          for fid, content, topic in assistant.memory.all_facts()]}

    @app.post("/api/memory/profile")
    def memory_profile(body: dict):
        key = (body.get("key") or "").strip()
        value = (body.get("value") or "").strip()
        if not key:
            return {"error": "No key."}
        if value:
            assistant.memory.profile_set(key, value)
        else:
            assistant.memory.profile_delete(key)
        return {"ok": True}

    @app.post("/api/memory/fact")
    def memory_fact(body: dict):
        content = (body.get("content") or "").strip()
        if not content:
            return {"error": "Nothing to remember."}
        fid = assistant.memory.remember(content, body.get("topic") or "")
        return {"ok": True, "id": fid}

    @app.post("/api/memory/forget")
    def memory_forget(body: dict):
        return {"ok": assistant.memory.forget(int(body.get("id") or 0))}

    @app.get("/api/status")
    def status():
        return assistant.status()

    @app.post("/api/voice")
    def voice(body: dict):
        msg = assistant.voice_on() if body.get("on") else assistant.voice_off()
        return {"message": msg, "active": assistant.voice_active}

    @app.post("/api/session/new")
    def new_session():
        return {"session": assistant.new_session()}

    @app.get("/api/sessions")
    def sessions():
        return {"sessions": assistant.memory.sessions(),
                "current": assistant.memory.session_id}

    @app.post("/api/session/load")
    def session_load(body: dict):
        ok = assistant.memory.switch_session(int(body.get("id") or 0))
        return {"ok": ok, "session": assistant.memory.session_id}

    @app.post("/api/upload")
    def upload(body: dict):
        data = body.get("data_b64") or ""
        if len(data) > 34_000_000:                 # ~25 MB decoded
            return {"error": "File too large (25 MB max)."}
        try:
            raw = base64.b64decode(data)
        except Exception:
            return {"error": "Bad upload data."}
        if not raw:
            return {"error": "Empty file."}
        name = os.path.basename((body.get("name") or "").strip()) \
            or f"upload_{int(time.time())}"
        updir = os.path.join(DATA_DIR, "uploads")
        os.makedirs(updir, exist_ok=True)
        path = os.path.join(updir, name)
        if os.path.exists(path):
            stem, ext = os.path.splitext(name)
            path = os.path.join(updir, f"{stem}_{int(time.time())}{ext}")
        with open(path, "wb") as f:
            f.write(raw)
        return {"ok": True, "path": path, "size": len(raw)}

    @app.get("/api/models")
    def models(refresh: int = 0):
        if refresh:
            assistant.models.discover()
        assistant.models.ensure_ready()
        return {"summary": assistant.models.summary()}

    @app.post("/api/model")
    def set_model(body: dict):
        role = body.get("role", "chat")
        name = body.get("name", "auto")
        return {"message": assistant.set_model_override(role, name)}

    @app.get("/api/tools")
    def tools():
        return {"tools": assistant.registry.names()}

    # ── Workspace: browse and edit the codebase from the UI ──────

    @app.get("/api/workspace")
    def workspace(path: str = ""):
        root = os.path.abspath(os.path.expanduser(path) or os.getcwd())
        if not os.path.isdir(root):
            return {"error": f"Not a directory: {root}"}
        dirs, files = [], []
        try:
            for name in sorted(os.listdir(root), key=str.lower):
                full = os.path.join(root, name)
                if os.path.isdir(full):
                    dirs.append(name)
                else:
                    try:
                        files.append({"name": name, "size": os.path.getsize(full)})
                    except OSError:
                        files.append({"name": name, "size": 0})
        except PermissionError:
            return {"error": f"Permission denied: {root}"}
        parent = os.path.dirname(root)
        return {"path": root, "parent": parent if parent != root else None,
                "dirs": dirs[:300], "files": files[:300]}

    @app.get("/api/workspace/file")
    def workspace_read(path: str):
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(full):
            return {"error": f"Not a file: {full}"}
        if os.path.getsize(full) > _MAX_EDIT_BYTES:
            return {"error": "File too large to edit in the browser (>2 MB)."}
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                return {"path": full, "content": f.read()}
        except OSError as e:
            return {"error": str(e)}

    @app.post("/api/workspace/file")
    def workspace_write(body: dict):
        path = (body.get("path") or "").strip()
        if not path:
            return {"error": "No path."}
        full = os.path.abspath(os.path.expanduser(path))
        try:
            parent = os.path.dirname(full)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(body.get("content") or "")
            return {"ok": True, "path": full}
        except OSError as e:
            return {"error": str(e)}

    # ── AI IDE: code-aware analysis + inline suggestions ─────────

    @app.post("/api/ide/analyze")
    def ide_analyze(body: dict):
        """Coding-buddy chat: answer a question about the open file."""
        question = (body.get("question") or "").strip()
        path = (body.get("path") or "").strip()
        code = body.get("code") or ""
        if not question:
            return {"error": "empty question"}
        # Trim code so we never blow the model context.
        code = code[:12000]
        lang = os.path.splitext(path)[1].lstrip(".") or "text"
        system = ("You are a coding buddy embedded in an IDE — like AXIOM pair-programming. "
                  "Answer the user's question about the open file directly and concisely. "
                  "Reference function/line names when useful. Prefer short, practical answers; "
                  "show small code snippets only when they help. No filler.")
        user = (f"File: {path or '(unsaved)'} [{lang}]\n\n"
                f"```{lang}\n{code}\n```\n\nQuestion: {question}")
        answer = assistant.quick_complete(system, user, role="code",
                                          num_predict=500, temperature=0.3)
        return {"answer": answer}

    @app.post("/api/ide/apply")
    def ide_apply(body: dict):
        """Direct edit: ask the model to rewrite the file per an instruction,
        write it to disk, and emit a diff event (shows in the changes strip)."""
        path = (body.get("path") or "").strip()
        instruction = (body.get("instruction") or "").strip()
        code = body.get("code") or ""
        if not path:
            return {"error": "no file open"}
        if not instruction:
            return {"error": "describe the change you want"}
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(full):
            return {"error": f"not a file: {full}"}
        if len(code) > 14000:
            return {"error": "file too large for direct edit — ask AXIOM in the "
                             "main chat to use edit_file for surgical changes."}
        lang = os.path.splitext(path)[1].lstrip(".") or "text"
        system = ("You are a precise code editor. Apply the requested change to the file "
                  "and return the COMPLETE updated file content only. No explanation, no "
                  "commentary. You may wrap it in a single ``` fence. Preserve unrelated "
                  "code, style, and indentation exactly.")
        user = (f"Change requested: {instruction}\n\n"
                f"Current file `{path}` [{lang}]:\n```{lang}\n{code}\n```\n\n"
                f"Return the full updated file.")
        out = assistant.quick_complete(system, user, role="code",
                                       num_predict=4096, temperature=0.1)
        new_code = _strip_code_fence(out)
        if not new_code.strip():
            return {"error": "model returned nothing"}
        if new_code == code:
            return {"ok": True, "unchanged": True, "content": code,
                    "added": 0, "removed": 0}
        try:
            with open(full, "w", encoding="utf-8") as f:
                f.write(new_code)
        except OSError as e:
            return {"error": str(e)}
        # Reuse the assistant's diff plumbing so the edit shows up in the
        # IDE changes strip and chat exactly like an agent edit.
        try:
            assistant._on_file_change(full, code, new_code)
        except Exception:
            pass
        import difflib
        diff = list(difflib.unified_diff(code.splitlines(), new_code.splitlines()))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        return {"ok": True, "content": new_code, "added": added, "removed": removed}

    @app.post("/api/ide/suggest")
    def ide_suggest(body: dict):
        """Real inline suggestions: a few concrete improvements with line refs."""
        path = (body.get("path") or "").strip()
        code = (body.get("code") or "")[:12000]
        if not code.strip():
            return {"suggestions": []}
        lang = os.path.splitext(path)[1].lstrip(".") or "text"
        system = ("You are a senior code reviewer. Return up to 4 of the most useful, "
                  "specific suggestions for this file: bugs, risks, or clear improvements. "
                  "Each suggestion MUST be one line in the exact form:\n"
                  "LINE <n>: <short actionable suggestion>\n"
                  "Use the real line number. If the file looks fine, return 'OK'. "
                  "No preamble, no markdown.")
        user = f"File: {path} [{lang}]\n\n```{lang}\n{code}\n```"
        raw = assistant.quick_complete(system, user, role="code",
                                       num_predict=350, temperature=0.2)
        suggestions = []
        for line in (raw or "").splitlines():
            line = line.strip()
            m = re.match(r"(?:LINE|Line)\s*(\d+)\s*[:\-]\s*(.+)", line)
            if m:
                suggestions.append({"line": int(m.group(1)), "text": m.group(2).strip()})
        return {"suggestions": suggestions[:4], "raw": raw}

    # ── Hardware inspection ──────────────────────────────────────

    @app.get("/api/hardware")
    def hardware():
        from .tools import hardware as hw
        try:
            return hw.snapshot()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    @app.post("/api/hardware/recommend")
    def hardware_recommend(body: dict = None):
        from .tools import hardware as hw
        try:
            snap = body.get("snapshot") if body and body.get("snapshot") else hw.snapshot()
            report = hw.report_text(snap)
        except Exception as e:
            return {"error": f"could not read hardware: {e}"}
        advice = assistant.quick_complete(hw.RECOMMEND_SYSTEM, report,
                                          num_predict=350, temperature=0.3)
        return {"advice": advice, "report": report}

    @app.get("/api/roast")
    def roast():
        from .tools import hardware as hw
        try:
            report = hw.report_text()
        except Exception as e:
            return {"error": str(e)}
        text = assistant.quick_complete(
            "You are AXIOM in a playful mood. Roast the user's PC based on "
            "this hardware snapshot — witty, dry, affectionate, 2-3 sentences. "
            "Reference real numbers. Keep it light, never mean.",
            report, num_predict=160, temperature=0.9)
        return {"roast": text}

    # ── Mail panel (only meaningful when email is configured) ────

    @app.get("/api/mail")
    def mail(limit: int = 12):
        e = (config.email or {})
        if not (e.get("user") and e.get("imap_host")):
            return {"configured": False, "messages": []}
        try:
            from .tools.email_ import unread_list
            return {"configured": True, "user": e.get("user"),
                    "messages": unread_list(config, limit)}
        except Exception as ex:
            return {"configured": True, "error": f"{type(ex).__name__}: {ex}",
                    "messages": []}

    @app.post("/api/mail/digest")
    def mail_digest():
        if "email_digest" not in assistant.registry.names():
            return {"error": "Email is not configured."}
        return {"digest": assistant.registry.execute("email_digest", {})}

    # ── Automations: scheduled tasks (list / cancel) ─────────────

    @app.get("/api/tasks")
    def tasks_list():
        import time as _t
        out = []
        for tid, due_ts, repeat_s, prompt in assistant.memory.enabled_tasks():
            out.append({"id": tid, "due_ts": due_ts, "repeat_s": repeat_s,
                        "prompt": prompt,
                        "due": _t.strftime("%a %H:%M", _t.localtime(due_ts))})
        out.sort(key=lambda x: x["due_ts"])
        return {"tasks": out}

    @app.post("/api/tasks/cancel")
    def tasks_cancel(body: dict):
        tid = int(body.get("id") or 0)
        return {"message": assistant.scheduler.cancel(tid)}

    @app.post("/api/tasks/add")
    def tasks_add(body: dict):
        when = (body.get("when") or "").strip()
        prompt = (body.get("prompt") or "").strip()
        if not when or not prompt:
            return {"error": "Need both 'when' and 'prompt'."}
        return {"message": assistant.scheduler.schedule(when, prompt)}

    @app.get("/api/models/list")
    def models_list():
        assistant.models.ensure_ready()
        items = []
        for m in sorted(assistant.models.models, key=lambda x: -x.size_b):
            items.append({
                "name": m.name,
                "size": f"{m.size_b:g}B" if m.size_b else "?",
                "caps": sorted(m.capabilities) or ["chat"],
                "provider": m.provider.name,
            })
        routes = {}
        for role in ("chat", "code", "vision"):
            picked = assistant.models.pick(role)
            routes[role] = picked.name if picked else None
        return {"models": items, "routes": routes}

    return app


def _free_port(host: str, start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start


def run(assistant: Assistant, host: str = "127.0.0.1",
        port: int = 0, open_browser: bool = True) -> None:
    import uvicorn

    port = _free_port(host, port or int(config.get("server_port", 8765)))
    url = f"http://{host}:{port}"
    app = create_app(assistant)

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"  AXIOM is up at {url}  (Ctrl+C to stop)")

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        assistant.shutdown()


# ---------------------------------------------------------------------------
# The UI
# ---------------------------------------------------------------------------

PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AXIOM</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:#0c0c0c;color:#c8c8c8;
  font-family:'Jetbrains Mono','IBM Plex Mono','Fira Code','Source Code Pro',Consolas,'Courier New',monospace;
  font-size:13px;line-height:1.6;
  display:flex;flex-direction:column;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:3px}

/* top bar */
.topbar{
  display:flex;align-items:center;gap:10px;
  padding:7px 16px;
  background:#111;border-bottom:1px solid #1e1e1e;
  flex-shrink:0;
}
.topbar .name{color:#e0e0e0;font-weight:bold;font-size:13px;letter-spacing:2px}
.topbar .sep{color:#333;margin:0 1px}
.topbar .status{color:#555;font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topbar .status.ok{color:#5a5}
.topbar .status.err{color:#c44}
.topbar button{
  background:none;color:#666;border:1px solid #222;
  border-radius:4px;padding:3px 10px;cursor:pointer;
  font:inherit;font-size:11px;transition:all .15s;
}
.topbar button:hover{color:#ccc;border-color:#444}
.topbar button.on{color:#5bf;border-color:#5bf}
.topbar button.voice-listening{color:#4c4;border-color:#4c4;animation:pulse 1.5s infinite}
.topbar button.voice-speaking{color:#db4;border-color:#db4}
.topbar button.voice-loading{color:#888;border-color:#555}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

/* drawers */
.drawer{
  background:#111;border-bottom:1px solid #1e1e1e;
  overflow:hidden;max-height:0;transition:max-height .3s ease;
  font-size:12px;color:#666;
}
.drawer.open{max-height:500px}
.drawer-inner{padding:10px 16px;display:flex;gap:24px;flex-wrap:wrap}
.drawer-inner .col{min-width:120px}
.drawer-inner .lbl{color:#555;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
.drawer-inner .val{color:#888}
.drawer-inner .val b{color:#aaa;font-weight:normal}

/* model switcher */
.model-panel{
  background:#111;border-bottom:1px solid #1e1e1e;
  overflow:hidden;max-height:0;transition:max-height .3s ease;
  font-size:12px;
}
.model-panel.open{max-height:600px}
.model-panel-inner{padding:12px 16px;max-height:400px;overflow-y:auto}
.model-panel-inner h3{color:#888;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin:0 0 8px;font-weight:normal}
.model-row{
  display:flex;align-items:center;gap:8px;
  padding:5px 8px;border-radius:3px;cursor:pointer;
  transition:background .1s;
}
.model-row:hover{background:#1a1a1a}
.model-row .mname{color:#aaa;flex:1}
.model-row .msize{color:#555;font-size:11px;min-width:40px;text-align:right}
.model-row .mcaps{color:#444;font-size:10px}
.model-row .mtag{
  font-size:9px;padding:1px 5px;border-radius:2px;
  background:#1a2a1a;color:#5a5;text-transform:uppercase;letter-spacing:.5px;
}
.role-btns{display:flex;gap:4px;opacity:0;transition:opacity .15s}
.model-row:hover .role-btns{opacity:1}
.role-btns button{
  font-size:9px;padding:1px 6px;background:none;
  border:1px solid #333;color:#666;border-radius:2px;
  cursor:pointer;font:inherit;transition:all .1s;
}
.role-btns button:hover{color:#5bf;border-color:#5bf}
.role-btns button.active{color:#5a5;border-color:#5a5}

/* memory panel */
.mem-note{color:#555;font-size:11px;margin:0 0 10px}
.mem-row{display:flex;align-items:center;gap:10px;padding:4px 8px;border-radius:3px}
.mem-row:hover{background:#1a1a1a}
.mem-row .mk{color:#7ab;min-width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mem-row .mv{color:#aaa;flex:1;word-break:break-word}
.mem-x{
  font-size:10px;padding:1px 7px;background:none;border:1px solid #333;
  color:#666;border-radius:2px;cursor:pointer;font:inherit;flex-shrink:0;
}
.mem-x:hover{color:#c55;border-color:#c55}
.mem-add{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
.mem-add input{
  background:#0c0c0c;border:1px solid #222;color:#c8c8c8;
  border-radius:3px;padding:4px 8px;font:inherit;font-size:11px;outline:none;
}
.mem-add input:focus{border-color:#333}
.mem-add button{
  background:none;border:1px solid #222;color:#666;border-radius:3px;
  padding:4px 10px;cursor:pointer;font:inherit;font-size:11px;
}
.mem-add button:hover{color:#ccc;border-color:#444}

/* sessions panel */
.sess-row{display:flex;align-items:center;gap:10px;padding:5px 8px;border-radius:3px;cursor:pointer}
.sess-row:hover{background:#1a1a1a}
.sess-row.current{background:#16202c}
.sess-row .st{color:#aaa;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sess-row .sd{color:#555;font-size:10px;flex-shrink:0}
.sess-row .sc{color:#444;font-size:10px;min-width:44px;text-align:right;flex-shrink:0}

/* attachments */
.attach-row{max-width:680px;margin:0 auto 6px;display:flex;gap:6px;flex-wrap:wrap}
.attach-chip{
  display:flex;align-items:center;gap:7px;font-size:11px;color:#9ab;
  background:#141a20;border:1px solid #223;border-radius:3px;padding:2px 9px;
}
.attach-chip .ax{cursor:pointer;color:#666;font-size:12px}
.attach-chip .ax:hover{color:#c55}
#attachbtn{
  background:#1a1a1a;color:#666;border:none;border-radius:4px;
  padding:8px 12px;cursor:pointer;font:inherit;font-size:13px;
  transition:all .15s;flex-shrink:0;
}
#attachbtn:hover{background:#222;color:#ccc}
.composer.drag{outline:1px dashed #5bf;outline-offset:-4px}

/* chat area */
#chat{flex:1;overflow-y:auto;padding:20px 0}
.inner{max-width:680px;margin:0 auto;padding:0 20px}

/* welcome */
.welcome{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;text-align:center;
  min-height:50vh;gap:16px;padding-top:8vh;
}
.welcome .mark{font-size:20px;letter-spacing:6px;color:#e0e0e0;font-weight:bold}
.welcome .sub{color:#555;font-size:13px;max-width:440px;line-height:1.7}
.welcome .caps{
  display:grid;grid-template-columns:1fr 1fr;gap:6px;
  max-width:420px;width:100%;margin-top:4px;
}
.welcome .cap{
  text-align:left;padding:10px 12px;
  border:1px solid #1a1a1a;border-radius:4px;
  cursor:pointer;transition:all .15s;
}
.welcome .cap:hover{border-color:#333;background:#141414}
.welcome .cap .cap-title{color:#999;font-size:12px;margin-bottom:2px}
.welcome .cap .cap-desc{color:#555;font-size:11px;line-height:1.4}

/* ai orb */
.orb-wrap{
  position:relative;width:100px;height:100px;margin-bottom:8px;
  animation:orb-float 4s ease-in-out infinite;
}
@keyframes orb-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}

.orb-svg{width:100px;height:100px;display:block}

/* rotating rings */
.ring-outer{animation:spin-cw 12s linear infinite;transform-origin:50px 50px}
.ring-mid{animation:spin-ccw 8s linear infinite;transform-origin:50px 50px}
.ring-inner{animation:spin-cw 5s linear infinite;transform-origin:50px 50px}
@keyframes spin-cw{to{transform:rotate(360deg)}}
@keyframes spin-ccw{to{transform:rotate(-360deg)}}

/* core pulse */
.core-glow{animation:core-pulse 2.5s ease-in-out infinite}
@keyframes core-pulse{
  0%,100%{opacity:.6;r:6}
  50%{opacity:1;r:8}
}
.core-flare{animation:flare 2.5s ease-in-out infinite}
@keyframes flare{
  0%,100%{opacity:.15;r:18}
  50%{opacity:.3;r:22}
}

/* particles orbiting */
.particle{animation:orbit 3s linear infinite;transform-origin:50px 50px}
.particle:nth-child(2){animation-delay:-1s;animation-duration:4s}
.particle:nth-child(3){animation-delay:-2s;animation-duration:3.5s}
@keyframes orbit{to{transform:rotate(360deg)}}

/* ground glow */
.orb-glow{
  position:absolute;bottom:-6px;left:50%;transform:translateX(-50%);
  width:60px;height:8px;border-radius:50%;
  background:radial-gradient(ellipse, rgba(80,180,255,.12) 0%, transparent 70%);
  animation:orb-float 4s ease-in-out infinite;
}

/* messages */
.msg{margin:0 0 16px}
.msg .tag{font-size:10px;color:#555;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}
.msg.user .bubble{
  background:#161616;border:1px solid #222;
  border-radius:6px;padding:8px 12px;
  display:inline-block;max-width:90%;color:#ccc;
}
.msg.bot .bubble{color:#c8c8c8}

/* markdown in bot */
.msg.bot .bubble pre{
  background:#0a0a0a;border:1px solid #1e1e1e;
  border-radius:4px;padding:10px 12px;overflow-x:auto;
  font-size:13px;margin:8px 0;line-height:1.5;
}
.msg.bot .bubble code{background:#151515;padding:1px 4px;border-radius:2px;font-size:13px;color:#8be}
.msg.bot .bubble pre code{background:none;padding:0;color:#c8c8c8}
.msg.bot .bubble b,.msg.bot .bubble strong{color:#e8e8e8;font-weight:bold}
.msg.bot .bubble a{color:#5bf;text-decoration:none}
.msg.bot .bubble a:hover{text-decoration:underline}
.msg.bot .bubble ul,.msg.bot .bubble ol{padding-left:18px;margin:4px 0}
.msg.bot .bubble li{margin:2px 0}
.msg.bot .bubble blockquote{border-left:2px solid #333;padding-left:10px;color:#888;margin:6px 0}
.msg.bot .bubble hr{border:none;border-top:1px solid #1e1e1e;margin:10px 0}
.msg.bot .bubble h1,.msg.bot .bubble h2,.msg.bot .bubble h3{color:#ddd;margin:8px 0 4px;font-size:15px}
.msg.bot .bubble table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
.msg.bot .bubble th{text-align:left;padding:6px 10px;border:1px solid #222;background:#151515;color:#aaa}
.msg.bot .bubble td{padding:6px 10px;border:1px solid #1e1e1e}

/* thinking block */
.think-block{
  margin:6px 0;border:1px solid #1a1a1a;border-radius:4px;
  background:#0a0a0a;font-size:12px;
}
.think-block summary{
  cursor:pointer;padding:5px 10px;color:#555;
  list-style:none;user-select:none;display:flex;align-items:center;gap:6px;
}
.think-block summary::-webkit-details-marker{display:none}
.think-block summary::before{content:"\25b6";font-size:8px;color:#444;transition:transform .15s}
.think-block[open] summary::before{transform:rotate(90deg)}
.think-block .think-body{
  padding:8px 12px;color:#666;border-top:1px solid #1a1a1a;
  line-height:1.5;white-space:pre-wrap;word-break:break-word;
}

/* tools */
.tool{
  font-size:12px;color:#555;
  border-left:2px solid #1e1e1e;
  margin:3px 0 3px 4px;padding-left:10px;
  white-space:pre-wrap;word-break:break-all;
}
.tool .fn{color:#5a8}
.tool .res{color:#555}

.meta{font-size:10px;color:#444;margin-top:4px}
.note{font-size:12px;color:#b90;margin:6px 0;padding:4px 0}
.err-banner{font-size:12px;color:#c44;margin:6px 0;padding:4px 0}

.typing::after{content:"_";animation:blink .8s step-start infinite;color:#5bf}
@keyframes blink{50%{opacity:0}}

.dots span{display:inline-block;width:4px;height:4px;background:#555;border-radius:50%;margin:0 2px;animation:dot 1s ease infinite}
.dots span:nth-child(2){animation-delay:.15s}
.dots span:nth-child(3){animation-delay:.3s}
@keyframes dot{0%,80%,100%{opacity:.2}40%{opacity:1}}

/* composer */
.composer{
  border-top:1px solid #1e1e1e;background:#111;
  padding:10px 16px;flex-shrink:0;
}
.compose-row{
  max-width:680px;margin:0 auto;
  display:flex;align-items:flex-end;gap:8px;
}
#box{
  flex:1;resize:none;
  background:#0c0c0c;color:#c8c8c8;
  border:1px solid #222;border-radius:4px;
  padding:8px 12px;font:inherit;outline:none;
  max-height:140px;line-height:1.5;
}
#box:focus{border-color:#333}
#box::placeholder{color:#444}
#sendbtn{
  background:#222;color:#888;border:none;
  border-radius:4px;padding:8px 14px;
  cursor:pointer;font:inherit;font-size:12px;
  transition:all .15s;flex-shrink:0;
}
#sendbtn:hover{background:#2a2a2a;color:#ccc}
#sendbtn:disabled{opacity:.3;cursor:default}
#sendbtn.stop{background:#3a1818;color:#c66}
#sendbtn.stop:hover{background:#4a1d1d;color:#e88}
.compose-hint{
  max-width:680px;margin:4px auto 0;
  font-size:10px;color:#333;text-align:center;
}

/* IDE panel */
.ws{
  position:fixed;top:0;right:0;bottom:0;width:min(1360px,100%);
  background:#101010;border-left:1px solid #222;z-index:20;
  display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform .25s ease;
}
.ws.open{transform:translateX(0)}
.ws-head{
  display:flex;align-items:center;gap:8px;padding:8px 12px;
  border-bottom:1px solid #1e1e1e;background:#111;flex-shrink:0;
}
.ws-head .ws-title{color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px}
#wspath{
  flex:1;background:#0c0c0c;color:#9a9;border:1px solid #222;
  border-radius:3px;padding:4px 8px;font:inherit;font-size:11px;outline:none;
}
#wspath:focus{border-color:#333}
.ws button{
  background:none;color:#666;border:1px solid #222;border-radius:3px;
  padding:3px 9px;cursor:pointer;font:inherit;font-size:11px;transition:all .15s;
}
.ws button:hover{color:#ccc;border-color:#444}
.ws button.on{color:#5bf;border-color:#5bf}
.ws-body{flex:1;display:flex;overflow:hidden}
.ws-tree{
  width:230px;min-width:150px;flex-shrink:0;overflow-y:auto;
  border-right:1px solid #1e1e1e;padding:6px 0;
}
.ws-row{
  padding:3px 14px;cursor:pointer;font-size:12px;color:#888;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.ws-row:hover{background:#181818;color:#ccc}
.ws-row.dir{color:#7ab}
.ws-row.active{background:#1a2230;color:#9cf}
.ws-row .fsize{color:#444;font-size:10px;margin-left:8px}
.ws-err{color:#c44;font-size:12px;padding:10px 14px}
.ws-main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.ws-file-bar{
  display:flex;align-items:center;gap:6px;padding:6px 12px;
  border-bottom:1px solid #1e1e1e;flex-shrink:0;
}
.ws-file-bar #wsfile{
  flex:1;color:#9a9;font-size:11px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.ws-file-bar .dirty{color:#db4}
.ws-split{flex:1;display:flex;overflow:hidden}
#wstext{
  flex:1;min-width:0;background:#0c0c0c;color:#c8c8c8;border:none;outline:none;
  padding:10px 14px;font:inherit;font-size:13px;line-height:1.5;
  resize:none;white-space:pre;tab-size:4;
}
/* diff pane: shown beside the code when a change is selected */
.ws-diff{
  flex:1;min-width:0;display:none;overflow:auto;
  border-left:1px solid #1e1e1e;background:#0c0c0c;
  font-size:12px;line-height:1.5;padding:8px 0;
}
.ws-diff.open{display:block}
.diff-line{white-space:pre;padding:0 12px}
.diff-add{background:rgba(60,170,90,.13);color:#8d9}
.diff-del{background:rgba(200,70,70,.11);color:#d88}
.diff-hunk{color:#69c;background:#0f1722;margin:4px 0;padding:1px 12px}
.diff-file{color:#777}
/* changes strip: every file the agent touched this session */
.ws-changes{
  border-top:1px solid #1e1e1e;max-height:150px;overflow-y:auto;
  flex-shrink:0;background:#0e0e0e;
}
.ws-changes .chg-head{
  font-size:10px;color:#555;text-transform:uppercase;letter-spacing:1px;
  padding:5px 12px 2px;
}
.chg-row{
  display:flex;align-items:center;gap:8px;padding:3px 12px;
  font-size:11px;color:#888;cursor:pointer;
}
.chg-row:hover{background:#181818;color:#ccc}
.chg-row.active{background:#1a2230}
.chg-row .chg-path{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chg-row .chg-time{color:#444;font-size:10px}
.chg-add{color:#5a5}
.chg-del{color:#c55}
.badge{
  background:#5bf;color:#000;border-radius:8px;
  font-size:9px;padding:0 5px;margin-left:5px;vertical-align:1px;
}
/* file-edit line in chat */
.tool.edit{cursor:pointer;border-left-color:#2a4a3a}
.tool.edit:hover{background:#121a14}

/* ── shared panel polish: search + actions row ─────────────── */
.panel-tools{display:flex;gap:8px;align-items:center;margin:0 0 12px;flex-wrap:wrap}
.panel-tools input.search{
  flex:1;min-width:140px;background:#0c0c0c;border:1px solid #222;color:#c8c8c8;
  border-radius:4px;padding:5px 10px;font:inherit;font-size:12px;outline:none;
}
.panel-tools input.search:focus{border-color:#3a5}
.panel-tools .pbtn{
  background:none;border:1px solid #222;color:#777;border-radius:4px;
  padding:5px 11px;cursor:pointer;font:inherit;font-size:11px;transition:all .15s;
}
.panel-tools .pbtn:hover{color:#ccc;border-color:#444}
.panel-tools .pbtn.accent{color:#5bf;border-color:#244}
.panel-tools .pbtn.accent:hover{border-color:#5bf}

/* ── app shell: left icon rail + main column ───────────────── */
.shell{flex:1;display:flex;min-height:0}
.main{flex:1;display:flex;flex-direction:column;min-width:0;position:relative}

.sidebar{
  width:58px;flex-shrink:0;background:#070707;
  border-right:1px solid #161616;z-index:30;
  display:flex;flex-direction:column;align-items:center;
  padding:11px 0 12px;gap:3px;
}
.side-brand{width:30px;height:30px;margin-bottom:10px;opacity:.95}
.side-brand svg{width:30px;height:30px;display:block}
.side-spacer{flex:1}
.side-sep{width:24px;height:1px;background:#1c1c1c;margin:5px 0}

.navicon{
  position:relative;width:40px;height:40px;border:none;background:none;
  color:#585858;border-radius:10px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:color .18s,background .18s,box-shadow .2s;
}
.navicon svg{width:19px;height:19px;stroke:currentColor;fill:none;
  stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.navicon:hover{color:#e2e2e2;background:#151515}
.navicon:active{transform:translateY(1px)}
.navicon.active{
  color:#7cc4ff;background:linear-gradient(180deg,#15222f,#0e1620);
  box-shadow:inset 0 0 0 1px #1d3247;
}
.navicon.active::before{
  content:"";position:absolute;left:-11px;top:10px;bottom:10px;width:2px;
  border-radius:2px;background:#5bf;box-shadow:0 0 9px #5bf;
}
.navicon::after{
  content:attr(data-label);position:absolute;left:50px;top:50%;
  transform:translateY(-50%) translateX(-5px) scale(.96);transform-origin:left center;
  background:#1c1c1c;color:#d2d2d2;font-size:11px;letter-spacing:.4px;
  padding:5px 10px;border-radius:7px;white-space:nowrap;
  border:1px solid #2b2b2b;box-shadow:0 8px 22px -10px #000;
  opacity:0;pointer-events:none;transition:opacity .15s,transform .15s;z-index:60;
}
.navicon:hover::after{opacity:1;transform:translateY(-50%) translateX(0) scale(1)}
.navicon .badge{position:absolute;top:3px;right:3px;margin:0}
/* voice states on the rail icon */
.navicon.v-on{color:#5bf}
.navicon.v-listening{color:#5c5;animation:pulse 1.5s infinite}
.navicon.v-speaking{color:#db4}
.navicon.v-loading{color:#888}

/* ── floating windows: draggable + resizable, glassy ───────── */
.fwin{
  position:fixed;z-index:42;display:none;flex-direction:column;
  min-width:320px;min-height:220px;width:480px;height:540px;
  background:rgba(13,13,14,.94);backdrop-filter:blur(16px) saturate(120%);
  -webkit-backdrop-filter:blur(16px) saturate(120%);
  border:1px solid #2a2a2a;border-radius:12px;overflow:hidden;resize:both;
  box-shadow:0 28px 70px -20px rgba(0,0,0,.9),0 0 0 1px rgba(120,180,255,.05),
             inset 0 1px 0 rgba(255,255,255,.03);
}
.fwin.open{display:flex;animation:fwin-in .19s cubic-bezier(.2,.8,.3,1)}
@keyframes fwin-in{from{opacity:0;transform:scale(.97) translateY(8px)}to{opacity:1;transform:none}}
.fwin-head{
  display:flex;align-items:center;gap:9px;padding:9px 11px 9px 13px;cursor:move;
  border-bottom:1px solid #1b1b1b;flex-shrink:0;user-select:none;
  background:linear-gradient(180deg,#151515,#101010);
}
.fwin-head .fw-dot{width:8px;height:8px;border-radius:50%;
  background:#5bf;box-shadow:0 0 9px #5bf;flex-shrink:0}
.fwin-head .fw-title{flex:1;font-size:11px;text-transform:uppercase;
  letter-spacing:1.6px;color:#a8a8a8}
.fwin-head .fw-x{
  width:24px;height:24px;border:none;background:none;color:#666;cursor:pointer;
  border-radius:6px;font-size:15px;line-height:1;
  display:flex;align-items:center;justify-content:center;transition:all .15s;
}
.fwin-head .fw-x:hover{background:#2a1717;color:#ec8}
.fwin-body{flex:1;overflow-y:auto;padding:14px 16px;min-height:0}
.fwin::after{
  content:"";position:absolute;right:3px;bottom:3px;width:9px;height:9px;
  border-right:2px solid #3a3a3a;border-bottom:2px solid #3a3a3a;
  border-radius:0 0 3px 0;pointer-events:none;opacity:.7;
}

/* ── hardware dashboard ────────────────────────────────────── */
.hw-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.hw-card{
  border:1px solid #1c1c1c;border-radius:6px;padding:11px 13px;background:#0d0d0d;
}
.hw-card .hw-top{display:flex;justify-content:space-between;align-items:baseline}
.hw-card .hw-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#666}
.hw-card .hw-sub{font-size:10px;color:#555}
.hw-card .hw-val{font-size:23px;color:#dcdcdc;margin:3px 0 7px;font-weight:bold;letter-spacing:.5px}
.hw-card .hw-val small{font-size:12px;color:#777;font-weight:normal}
.hw-bar{height:6px;background:#1a1a1a;border-radius:3px;overflow:hidden}
.hw-bar > i{display:block;height:100%;border-radius:3px;transition:width .4s ease,background .4s}
.hw-bar.cool > i{background:#3a8}
.hw-bar.warm > i{background:#db4}
.hw-bar.hot  > i{background:#c55}
.hw-spark{width:100%;height:34px;display:block;margin-top:8px}

.hw-section-h{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#555;margin:16px 0 8px}
.hw-cores{display:grid;grid-template-columns:repeat(auto-fill,minmax(46px,1fr));gap:5px}
.hw-core{font-size:9px;color:#666;text-align:center}
.hw-core .cbar{height:30px;width:100%;background:#161616;border-radius:2px;position:relative;overflow:hidden;margin-bottom:2px}
.hw-core .cbar > i{position:absolute;bottom:0;left:0;right:0;background:#3a8;transition:height .4s,background .4s}

.hw-proc{width:100%;font-size:12px;border-collapse:collapse;margin-top:4px}
.hw-proc td{padding:3px 8px;border-bottom:1px solid #161616;color:#9a9a9a}
.hw-proc td.n{color:#bbb}
.hw-proc td.r{text-align:right;color:#888;font-variant-numeric:tabular-nums}

.hw-advice{
  margin-top:14px;border:1px solid #1d2a1d;border-radius:6px;background:#0c110c;
  padding:12px 14px;font-size:13px;color:#bcd;line-height:1.6;
}
.hw-advice .ha-head{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#5a5;margin-bottom:6px}
.hw-advice ul{margin:0;padding-left:18px}
.hw-advice li{margin:3px 0}

/* ── mail panel ────────────────────────────────────────────── */
.mail-row{display:flex;gap:10px;align-items:baseline;padding:6px 8px;border-radius:4px;border-bottom:1px solid #161616}
.mail-row:hover{background:#161616}
.mail-row .mfrom{color:#9bd;min-width:160px;max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px}
.mail-row .msubj{flex:1;color:#bbb;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mail-row .macts{display:flex;gap:4px;opacity:0;transition:opacity .15s}
.mail-row:hover .macts{opacity:1}
.mail-row .macts button{font-size:9px;padding:1px 7px;background:none;border:1px solid #2a3340;color:#789;border-radius:2px;cursor:pointer;font:inherit}
.mail-row .macts button:hover{color:#9cf;border-color:#5bf}

/* ── automations / tasks ───────────────────────────────────── */
.task-row{display:flex;gap:10px;align-items:center;padding:6px 8px;border-radius:4px;border-bottom:1px solid #161616}
.task-row:hover{background:#161616}
.task-row .tdue{color:#7ab;font-size:11px;min-width:92px;font-variant-numeric:tabular-nums}
.task-row .trepeat{color:#b90;font-size:9px}
.task-row .tprompt{flex:1;color:#bbb;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

@media(max-width:760px){
  .ws-tree{display:none}
  .ws-split{flex-direction:column}
  .ws-diff{border-left:none;border-top:1px solid #1e1e1e}
}
@media(max-width:600px){
  .welcome .caps{grid-template-columns:1fr}
  .topbar{gap:6px;padding:6px 10px}
  .topbar .status{display:none}
  .ws{width:100%}
}

/* ── IDE: syntax highlighting, view pane, coding buddy ───────── */
.code-editor{
  flex:1;min-width:0;overflow:auto;tab-size:4;
  font-family:'Jetbrains Mono','Fira Code',Consolas,monospace;
  font-size:13px;line-height:1.5;background:#0c0c0c;
}
.code-line{display:flex;background:#0c0c0c;border-bottom:1px solid #0f0f0f;color:#c8c8c8}
.code-line:hover{background:#131313}
.code-line-num{
  flex-shrink:0;width:48px;padding:0 10px;text-align:right;color:#555;
  background:#0a0a0a;user-select:none;font-size:11px;border-right:1px solid #1a1a1a;
}
.code-line-content{flex:1;padding:0 12px;white-space:pre-wrap;word-break:break-word;position:relative}

/* syntax tokens, scoped under the editor so global .fn/.cls (tool trace) are untouched */
.code-line-content .kw{color:#d68fd8;font-weight:600}
.code-line-content .str{color:#8cbf7a}
.code-line-content .cmnt{color:#5a5a5a;font-style:italic}
.code-line-content .num{color:#b5a8ff}
.code-line-content .cls{color:#7eb3d8;font-weight:600}
.code-line-content .fn{color:#5fb3f0}

/* inline AI suggestion rows injected beneath a source line */
.code-suggestion-row{display:flex;background:rgba(100,170,255,.06);border-top:1px dashed #1e3346}
.code-suggestion-row .code-line-num{color:#5bf;background:#0b1622}
.code-suggestion-row .sg{flex:1;padding:3px 12px;color:#8dd;font-size:11px;line-height:1.4}
.code-suggestion-row .sg b{color:#bdf;font-weight:600}

/* coding buddy column inside the IDE */
.ws-buddy{
  width:320px;min-width:240px;flex-shrink:0;display:flex;flex-direction:column;
  overflow:hidden;background:#080808;border-left:1px solid #1e1e1e;
}
.ws-buddy.hidden{display:none}
.ws-buddy-head{
  display:flex;align-items:center;gap:8px;padding:9px 12px;flex-shrink:0;
  border-bottom:1px solid #1a1a1a;background:#0d0d0d;
  font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#8aa;font-weight:600;
}
.chat-buddy-icon{width:7px;height:7px;border-radius:50%;background:#5a8;flex-shrink:0;box-shadow:0 0 8px #5a8}

.ide-chat-messages{flex:1;overflow-y:auto;overflow-x:hidden;padding:10px 12px;display:flex;flex-direction:column;gap:8px;min-width:0}
.ide-msg{display:flex;flex-direction:column;gap:3px;font-size:11px;line-height:1.4;max-width:100%}
.ide-msg.user{align-items:flex-end}
.ide-msg.assistant{align-items:flex-start}
.ide-msg-bubble{max-width:92%;padding:6px 9px;border-radius:4px;word-break:break-word;overflow-wrap:anywhere}
.ide-msg.user .ide-msg-bubble{background:#1a2a3a;color:#9cf}
.ide-msg.assistant .ide-msg-bubble{background:#141a16;color:#bcd;line-height:1.5}
.ide-msg-bubble pre{background:#0a0a0a;border:1px solid #1e1e1e;border-radius:4px;padding:6px 8px;margin:5px 0;overflow-x:auto;max-width:100%;font-size:11px;line-height:1.45;white-space:pre}
.ide-msg-bubble code{background:#10181f;padding:1px 4px;border-radius:2px;font-size:11px;color:#8be}
.ide-msg-bubble pre code{background:none;padding:0;color:#cdd}
.ide-msg-bubble p{margin:4px 0}
.ide-msg-bubble ul,.ide-msg-bubble ol{padding-left:16px;margin:4px 0}
.ide-msg-bubble li{margin:1px 0}
.ide-msg-bubble h1,.ide-msg-bubble h2,.ide-msg-bubble h3{font-size:12px;color:#dfe;margin:6px 0 3px}
.ide-msg-bubble a{color:#5bf;text-decoration:none}
.ide-msg-bubble b,.ide-msg-bubble strong{color:#dff}
.ide-msg-time{font-size:9px;color:#555;padding:0 2px}
.ide-chat-input{display:flex;gap:6px;padding:8px 10px;border-top:1px solid #1a1a1a;flex-shrink:0;background:#0a0a0a}
.ide-chat-box{flex:1;background:#0c0c0c;border:1px solid #1a1a1a;border-radius:3px;padding:5px 8px;font:inherit;font-size:11px;color:#c8c8c8;outline:none;resize:none;max-height:60px}
.ide-chat-box:focus{border-color:#2a4a4a}
.ide-chat-send{background:none;border:1px solid #2a4a4a;color:#888;border-radius:3px;padding:5px 10px;cursor:pointer;font:inherit;font-size:10px;transition:all .15s;flex-shrink:0}
.ide-chat-send:hover{color:#9cf;border-color:#5bf}
.ide-apply-btn{color:#7d8;border-color:#264}
.ide-apply-btn:hover{color:#9f8;border-color:#5a8}
.ide-apply-btn:disabled{opacity:.4;cursor:default}
.status-dot{width:5px;height:5px;border-radius:50%}
.status-dot.ok{background:#5a8;box-shadow:0 0 6px #5a8}
.status-dot.error{background:#c55;box-shadow:0 0 6px #c55}
.status-dot.warning{background:#db4}
@media(max-width:900px){.ws-buddy{display:none}}
</style>
</head>
<body>

<div class="shell">
<nav class="sidebar">
  <div class="side-brand" title="AXIOM">
    <svg viewBox="0 0 30 30">
      <circle cx="15" cy="15" r="12" fill="none" stroke="#214257" stroke-width="1"/>
      <circle cx="15" cy="15" r="7" fill="none" stroke="#2f6486" stroke-width="1" stroke-dasharray="3 4"/>
      <circle cx="15" cy="15" r="4" fill="#5bf" opacity=".4"/>
      <circle cx="15" cy="15" r="2" fill="#cfe8ff"/>
    </svg>
  </div>
  <button class="navicon" id="wsbtn" data-label="ide · code + ai buddy">
    <svg viewBox="0 0 24 24"><polyline points="9 8 5 12 9 16"/><polyline points="15 8 19 12 15 16"/></svg>
    <span class="badge" id="wsbadge" style="display:none">0</span></button>
  <button class="navicon" id="sessbtn" data-label="past conversations">
    <svg viewBox="0 0 24 24"><path d="M4 5h16v11H8l-4 4z"/></svg></button>
  <button class="navicon" id="membtn" data-label="what axiom remembers">
    <svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/></svg></button>
  <button class="navicon" id="modelsbtn" data-label="switch models">
    <svg viewBox="0 0 24 24"><polygon points="12 3 21 8 12 13 3 8 12 3"/><polyline points="3 13 12 18 21 13"/></svg></button>
  <div class="side-sep"></div>
  <button class="navicon" id="hwbtn" data-label="hardware dashboard">
    <svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M10 2v3M14 2v3M10 19v3M14 19v3M2 10h3M2 14h3M19 10h3M19 14h3"/></svg></button>
  <button class="navicon" id="mailbtn" data-label="gmail · automation">
    <svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg></button>
  <button class="navicon" id="tasksbtn" data-label="scheduled automations">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg></button>
  <button class="navicon" id="infobtn" data-label="system info">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg></button>
  <div class="side-spacer"></div>
  <button class="navicon" id="micbtn" data-label="voice">
    <svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4"/></svg></button>
  <button class="navicon" id="exportbtn" data-label="download chat">
    <svg viewBox="0 0 24 24"><path d="M12 4v10M8 11l4 4 4-4M5 19h14"/></svg></button>
  <button class="navicon" id="newbtn" data-label="new session">
    <svg viewBox="0 0 24 24"><path d="M12 6v12M6 12h12"/></svg></button>
</nav>
<div class="main">
<div class="topbar">
  <span class="name">AXIOM</span>
  <span class="sep">/</span>
  <span class="status" id="chip">starting&hellip;</span>
</div>

<div class="ws" id="ws">
  <div class="ws-head">
    <span class="ws-title">ide</span>
    <input id="wspath" spellcheck="false" placeholder="path...">
    <button id="wsbuddybtn" class="on" title="toggle the coding buddy panel">buddy</button>
    <button id="wsclose">close</button>
  </div>
  <div class="ws-body">
    <div class="ws-tree" id="wstree"></div>
    <div class="ws-main">
      <div class="ws-file-bar">
        <span id="wsfile">select a file from the tree</span>
        <button id="wsmode" style="display:none" title="switch between highlighted view and editing">edit</button>
        <button id="wssuggest" style="display:none" title="get inline AI suggestions">&#9889; suggest</button>
        <button id="wsdiffbtn" style="display:none" title="toggle the diff pane">diff</button>
        <button id="wsask" title="reference this file in the main chat">ask axiom</button>
        <button id="wssave" style="display:none">save</button>
      </div>
      <div class="ws-split">
        <div class="ws-view code-editor" id="wsview"></div>
        <textarea id="wstext" spellcheck="false" placeholder="" style="display:none"></textarea>
        <div class="ws-diff" id="wsdiff"></div>
      </div>
      <div class="ws-changes" id="wschanges" style="display:none">
        <div class="chg-head">changes by axiom</div>
        <div id="wschglist"></div>
      </div>
    </div>
    <div class="ws-buddy" id="wsbuddy">
      <div class="ws-buddy-head"><span class="chat-buddy-icon"></span>coding buddy</div>
      <div class="ide-chat-messages" id="ide-messages"></div>
      <div class="ide-chat-input">
        <textarea class="ide-chat-box" id="ide-chat-input" rows="1" placeholder="ask, or describe an edit…"></textarea>
        <button class="ide-chat-send" id="ide-chat-send" title="ask about the open file (read-only)">ask</button>
        <button class="ide-chat-send ide-apply-btn" id="ide-chat-apply" title="apply your message as a direct edit (Ctrl+Enter)">apply</button>
      </div>
    </div>
  </div>
</div>

<div class="model-panel" id="modelpanel"><div class="model-panel-inner" id="modellist">loading...</div></div>
<div class="model-panel" id="mempanel"><div class="model-panel-inner" id="memlist">loading...</div></div>
<div class="model-panel" id="sesspanel"><div class="model-panel-inner" id="sesslist">loading...</div></div>
<div class="drawer" id="drawer"><div class="drawer-inner" id="dinfo"></div></div>

<div id="chat">
  <div class="inner" id="log">
    <div class="welcome" id="welcome">
      <div class="orb-wrap">
        <svg class="orb-svg" viewBox="0 0 100 100">
          <defs>
            <radialGradient id="cg" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stop-color="#5bf" stop-opacity=".4"/>
              <stop offset="100%" stop-color="#5bf" stop-opacity="0"/>
            </radialGradient>
          </defs>
          <!-- ambient flare -->
          <circle class="core-flare" cx="50" cy="50" r="20" fill="url(#cg)"/>
          <!-- outer ring -->
          <circle class="ring-outer" cx="50" cy="50" r="42" fill="none" stroke="rgba(80,180,255,.12)" stroke-width=".6" stroke-dasharray="8 12"/>
          <!-- mid ring -->
          <circle class="ring-mid" cx="50" cy="50" r="33" fill="none" stroke="rgba(80,180,255,.2)" stroke-width=".7" stroke-dasharray="5 10 2 10"/>
          <!-- inner ring -->
          <circle class="ring-inner" cx="50" cy="50" r="22" fill="none" stroke="rgba(100,200,255,.25)" stroke-width=".8" stroke-dasharray="3 7"/>
          <!-- orbiting particles -->
          <g class="particle"><circle cx="50" cy="8" r="1.2" fill="rgba(120,200,255,.5)"/></g>
          <g class="particle"><circle cx="92" cy="50" r="1" fill="rgba(120,200,255,.35)"/></g>
          <g class="particle"><circle cx="20" cy="80" r=".8" fill="rgba(120,200,255,.3)"/></g>
          <!-- core -->
          <circle class="core-glow" cx="50" cy="50" r="6" fill="rgba(140,220,255,.7)"/>
          <circle cx="50" cy="50" r="3" fill="#fff" opacity=".8"/>
        </svg>
        <div class="orb-glow"></div>
      </div>
      <div class="mark">AXIOM</div>
      <div class="sub">your local AI assistant. controls files, shell, git, github, system, web, memory, and schedules.</div>
      <div class="caps">
        <div class="cap" onclick="q('what processes are eating my RAM?')">
          <div class="cap-title">system</div>
          <div class="cap-desc">monitor RAM, CPU, battery, running processes</div>
        </div>
        <div class="cap" onclick="q('summarize recent git commits')">
          <div class="cap-title">git &amp; github</div>
          <div class="cap-desc">commit, push, pull, PRs, list repos</div>
        </div>
        <div class="cap" onclick="q('what\'s the weather?')">
          <div class="cap-title">web &amp; search</div>
          <div class="cap-desc">weather, news, web search, fetch URLs</div>
        </div>
        <div class="cap" onclick="q('list files in this directory')">
          <div class="cap-title">files &amp; code</div>
          <div class="cap-desc">read, write, edit files, run commands</div>
        </div>
        <div class="cap" onclick="document.getElementById('hwbtn').click()">
          <div class="cap-title">hardware</div>
          <div class="cap-desc">live CPU/GPU/RAM dashboard + AI tuning advice</div>
        </div>
        <div class="cap" onclick="q('give me an AI digest of my unread email')">
          <div class="cap-title">gmail</div>
          <div class="cap-desc">triage, digest, draft replies, scheduled sends</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="composer" id="composer">
  <div class="attach-row" id="attachrow" style="display:none"></div>
  <div class="compose-row">
    <button id="attachbtn" title="attach a pdf, image, or text file">+</button>
    <textarea id="box" rows="1" placeholder="message axiom&hellip;"></textarea>
    <button id="sendbtn">send</button>
  </div>
  <div class="compose-hint">enter to send &middot; shift+enter for newline &middot; + or drop a file to attach</div>
  <input type="file" id="fileinput" multiple style="display:none">
</div>
</div><!-- /.main -->
</div><!-- /.shell -->

<!-- floating, draggable + resizable windows -->
<div class="fwin" id="hwwin" style="top:64px;right:30px">
  <div class="fwin-head" data-win="hwwin"><span class="fw-dot"></span>
    <span class="fw-title">hardware</span><button class="fw-x" data-win="hwwin">&times;</button></div>
  <div class="fwin-body"><div id="hwbody">loading...</div></div>
</div>
<div class="fwin" id="mailwin" style="top:96px;right:70px;width:560px">
  <div class="fwin-head" data-win="mailwin"><span class="fw-dot"></span>
    <span class="fw-title">mail</span><button class="fw-x" data-win="mailwin">&times;</button></div>
  <div class="fwin-body"><div id="mailbody">loading...</div></div>
</div>
<div class="fwin" id="taskswin" style="top:128px;right:110px;width:520px;height:440px">
  <div class="fwin-head" data-win="taskswin"><span class="fw-dot"></span>
    <span class="fw-title">automations</span><button class="fw-x" data-win="taskswin">&times;</button></div>
  <div class="fwin-body"><div id="tasksbody">loading...</div></div>
</div>

<script>
var log=document.getElementById("log"),
    chat=document.getElementById("chat"),
    box=document.getElementById("box"),
    chip=document.getElementById("chip"),
    sendbtn=document.getElementById("sendbtn"),
    micbtn=document.getElementById("micbtn"),
    newbtn=document.getElementById("newbtn"),
    infobtn=document.getElementById("infobtn"),
    modelsbtn=document.getElementById("modelsbtn"),
    membtn=document.getElementById("membtn"),
    sessbtn=document.getElementById("sessbtn"),
    drawer=document.getElementById("drawer"),
    modelpanel=document.getElementById("modelpanel"),
    mempanel=document.getElementById("mempanel"),
    memlist=document.getElementById("memlist"),
    sesspanel=document.getElementById("sesspanel"),
    sesslist=document.getElementById("sesslist"),
    composer=document.getElementById("composer"),
    attachbtn=document.getElementById("attachbtn"),
    attachrow=document.getElementById("attachrow"),
    fileinput=document.getElementById("fileinput"),
    attachments=[],
    modellist=document.getElementById("modellist"),
    dinfo=document.getElementById("dinfo"),
    wel=document.getElementById("welcome"),
    cur=null,busy=false;

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}

function md(s){
  var h=esc(s);
  /* code blocks */
  h=h.replace(/```(\w*)\n?([\s\S]*?)```/g,function(_,l,c){return'<pre><code>'+c+'</code></pre>'});
  h=h.replace(/`([^`\n]+)`/g,'<code>$1</code>');
  /* headings */
  h=h.replace(/^#### (.+)$/gm,'<h4>$1</h4>');
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  /* bold/italic */
  h=h.replace(/\*\*\*(.+?)\*\*\*/g,'<b><em>$1</em></b>');
  h=h.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>');
  h=h.replace(/(?<![\w*])\*([^*\n]+)\*(?![\w*])/g,'<em>$1</em>');
  /* blockquote */
  h=h.replace(/^&gt; (.+)$/gm,'<blockquote>$1</blockquote>');
  /* hr */
  h=h.replace(/^---$/gm,'<hr>');
  /* numbered list */
  h=h.replace(/(^\d+\. .+$(?:\n\d+\. .+$)*)/gm,function(block){
    var items=block.split('\n').map(function(l){return '<li>'+l.replace(/^\d+\.\s/,'')+'</li>'}).join('');
    return '<ol>'+items+'</ol>';
  });
  /* bullet list */
  h=h.replace(/(^[-*] .+$(?:\n[-*] .+$)*)/gm,function(block){
    var items=block.split('\n').map(function(l){return '<li>'+l.replace(/^[-*]\s/,'')+'</li>'}).join('');
    return '<ul>'+items+'</ul>';
  });
  /* tables */
  h=h.replace(/(^\|.+\|$(?:\n\|.+\|$)+)/gm,function(block){
    var rows=block.split('\n').filter(function(r){return r.trim()});
    if(rows.length<2)return block;
    var html='<table>';
    var hdr=rows[0].split('|').filter(function(c){return c.trim()!==''}).map(function(c){return c.trim()});
    html+='<tr>'+hdr.map(function(c){return '<th>'+c+'</th>'}).join('')+'</tr>';
    for(var i=2;i<rows.length;i++){
      var cells=rows[i].split('|').filter(function(c){return c.trim()!==''}).map(function(c){return c.trim()});
      html+='<tr>'+cells.map(function(c){return '<td>'+c+'</td>'}).join('')+'</tr>';
    }
    html+='</table>';return html;
  });
  /* links */
  h=h.replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank">$1</a>');
  /* paragraphs: convert double newlines */
  h=h.replace(/\n{2,}/g,'</p><p>');
  if(h.indexOf('</p><p>')!==-1)h='<p>'+h+'</p>';
  return h;
}

/* thinking blocks — streamed from the model via "thinking" events */
function thinkTok(t){
  if(!cur)return;
  if(!cur.thinkEl){
    var d=document.createElement("details");
    d.className="think-block";d.open=true;
    d.innerHTML='<summary>thinking...</summary><div class="think-body"></div>';
    cur.el.insertBefore(d,cur.body);
    cur.thinkEl=d;
  }
  cur.thinkEl.querySelector(".think-body").textContent+=t;
  scroll();
}

function closeThink(final_){
  if(cur&&cur.thinkEl){
    cur.thinkEl.open=false;
    cur.thinkEl.querySelector("summary").textContent="thought process";
    if(!final_)cur.thinkEl=null; /* next thinking phase gets a fresh block */
  }
}

function scroll(){requestAnimationFrame(function(){chat.scrollTop=chat.scrollHeight})}
function hide_wel(){if(wel&&wel.parentNode){wel.remove()}}

function addUser(text,source){
  hide_wel();
  var d=document.createElement("div");d.className="msg user";
  var tag=source==="voice"?"you (voice)":source==="schedule"?"scheduled":"you";
  d.innerHTML='<div class="tag">'+tag+'</div><div class="bubble"></div>';
  d.querySelector(".bubble").textContent=text;
  log.appendChild(d);scroll();
}

function startBot(tid){
  hide_wel();
  var d=document.createElement("div");d.className="msg bot";
  d.innerHTML='<div class="tag">axiom</div><div class="bubble"><span class="dots"><span></span><span></span><span></span></span></div>';
  log.appendChild(d);
  var b=d.querySelector(".bubble");
  cur={el:d,body:b,tid:tid,txt:"",wait:true,thinkEl:null};
  scroll();
}

function need(tid){if(!cur||cur.tid!==tid)startBot(tid);return cur}

function tok(t){
  closeThink(true);
  if(cur.wait){cur.body.innerHTML="";cur.wait=false}
  cur.txt+=t;
  cur.body.innerHTML=md(cur.txt);
  cur.body.classList.add("typing");
  scroll();
}

function addTool(html,cls){
  var d=document.createElement("div");d.className="tool"+(cls?" "+cls:"");d.innerHTML=html;
  (cur?cur.el:log).appendChild(d);scroll();
  return d;
}

function banner(cls,icon,txt){
  hide_wel();
  var d=document.createElement("div");d.className=cls;d.textContent=icon+" "+txt;
  (cur?cur.el:log).appendChild(d);scroll();
}

/* SSE */
var es=new EventSource("/api/events");
es.onmessage=function(m){
  var ev;try{ev=JSON.parse(m.data)}catch(e){return}
  switch(ev.type){
    case"hello":refreshStatus();break;
    case"user":
      addUser(ev.text,ev.source);startBot(ev.turn_id);setBusy(true);break;
    case"token":
      need(ev.turn_id);tok(ev.text);break;
    case"thinking":
      need(ev.turn_id);thinkTok(ev.text);break;
    case"tool_start":
      need(ev.turn_id);closeThink(false);
      addTool('<span class="fn">'+esc(ev.name)+'</span> '+esc(String(ev.args)));break;
    case"tool_result":
      addTool('<span class="res">  '+esc(String(ev.preview||""))+'</span>');break;
    case"file_edit":
      onFileEdit(ev,true);break;
    case"done":
      if(cur&&cur.tid===ev.turn_id){
        closeThink(true);
        cur.body.classList.remove("typing");
        if(cur.wait){cur.body.innerHTML=ev.text?md(ev.text):"";cur.wait=false}
        else if(!cur.txt&&ev.text){cur.body.innerHTML=md(ev.text)}
        var mt=document.createElement("div");mt.className="meta";
        var p=[];
        if(ev.model)p.push(ev.model);
        if(ev.tools_used)p.push(ev.tools_used+" tool"+(ev.tools_used>1?"s":""));
        mt.textContent=p.join(" / ");
        cur.el.appendChild(mt);cur=null;
      }
      setBusy(false);refreshStatus();break;
    case"error":
      if(cur){closeThink(true);cur.body.classList.remove("typing");cur=null}
      banner("err-banner","!",ev.text);setBusy(false);break;
    case"notify":banner("note","*",ev.text);break;
    case"memory":
      banner("note","*","remembered: "+ev.text);
      if(mempanel.classList.contains("open"))loadMemory();
      break;
    case"voice":voiceUI(ev.state);break;
  }
};
es.onerror=function(){chip.textContent="reconnecting...";chip.className="status err"};

/* status */
function refreshStatus(){
  fetch("/api/status").then(function(r){return r.json()}).then(function(s){
    var r=s.routes||{};
    if(s.models===null){chip.textContent="discovering models...";chip.className="status";return}
    if(s.errors&&s.errors.length&&!s.models){chip.textContent="! "+s.errors[0];chip.className="status err";return}
    chip.textContent=s.models+" models / "+s.tools+" tools / session #"+s.session;
    chip.className="status ok";
    dinfo.innerHTML=
      '<div class="col"><div class="lbl">routes</div>'+
        '<div class="val">chat <b>'+(r.chat||"-")+'</b></div>'+
        '<div class="val">code <b>'+(r.code||"-")+'</b></div>'+
        '<div class="val">vision <b>'+(r.vision||"-")+'</b></div></div>'+
      '<div class="col"><div class="lbl">stats</div>'+
        '<div class="val"><b>'+s.models+'</b> models</div>'+
        '<div class="val"><b>'+s.tools+'</b> tools</div>'+
        '<div class="val"><b>'+s.facts+'</b> facts / <b>'+(s.profile||0)+'</b> profile</div></div>'+
      '<div class="col"><div class="lbl">voice</div>'+
        '<div class="val">'+(s.voice?"on":"off")+'</div>'+
        '<div class="lbl" style="margin-top:6px">speak</div>'+
        '<div class="val">'+s.speak+'</div></div>';
    if(!micbtn._t)voiceUI(s.voice?"on":"off");
  }).catch(function(){chip.textContent="unreachable";chip.className="status err"});
}
refreshStatus();setInterval(refreshStatus,15000);

/* voice UI */
function voiceUI(st){
  var on=st&&st!=="off"&&st!=="unavailable";
  micbtn.className="navicon";          /* keep the rail icon, swap state class + tooltip */
  if(st==="listening"){micbtn.classList.add("v-listening");micbtn.setAttribute("data-label","listening…")}
  else if(st==="speaking"){micbtn.classList.add("v-speaking");micbtn.setAttribute("data-label","speaking…")}
  else if(st==="loading"){micbtn.classList.add("v-loading");micbtn.setAttribute("data-label","loading…")}
  else if(st==="transcribing"){micbtn.classList.add("v-loading");micbtn.setAttribute("data-label","thinking…")}
  else if(on){micbtn.classList.add("v-on","active");micbtn.setAttribute("data-label","voice on")}
  else{micbtn.setAttribute("data-label","voice")}
}

/* model switcher — with search + refresh */
var _modelData=null,_modelFilter="";
function loadModels(refresh){
  modellist.innerHTML="loading...";
  fetch("/api/models/list"+(refresh?"?":"")).then(function(r){return r.json()}).then(function(data){
    _modelData=data;renderModels();
    if(refresh){fetch("/api/models?refresh=1").then(function(){return fetch("/api/models/list")})
      .then(function(r){return r.json()}).then(function(d){_modelData=d;renderModels()}).catch(function(){})}
  }).catch(function(){modellist.innerHTML="<span style='color:#c44'>failed to load</span>"});
}
function renderModels(){
  if(!_modelData){return}
  var models=_modelData.models||[],routes=_modelData.routes||{};
  var tools=document.createElement("div");tools.className="panel-tools";
  var search=document.createElement("input");search.className="search";
  search.placeholder="filter models...";search.value=_modelFilter;
  search.oninput=function(){_modelFilter=search.value;renderRows();search.focus()};
  var rb=document.createElement("button");rb.className="pbtn accent";rb.textContent="rescan";
  rb.onclick=function(){loadModels(true)};
  tools.appendChild(search);tools.appendChild(rb);
  var routeInfo=document.createElement("div");routeInfo.className="hw-sub";
  routeInfo.style.cssText="font-size:11px;color:#666;margin-bottom:8px";
  routeInfo.innerHTML="active: chat <b style='color:#9cf'>"+esc(routes.chat||"-")+
    "</b> &middot; code <b style='color:#9cf'>"+esc(routes.code||"-")+
    "</b> &middot; vision <b style='color:#9cf'>"+esc(routes.vision||"-")+"</b>";
  var h=document.createElement("h3");h.textContent="click a role button to pin a model";
  var rows=document.createElement("div");rows.id="modelrows";
  modellist.innerHTML="";
  modellist.appendChild(tools);modellist.appendChild(routeInfo);
  modellist.appendChild(h);modellist.appendChild(rows);
  renderRows();
  function renderRows(){
    var rowsEl=document.getElementById("modelrows");
    var f=_modelFilter.toLowerCase();
    var shown=models.filter(function(m){
      return !f||m.name.toLowerCase().indexOf(f)>=0||m.caps.join(" ").indexOf(f)>=0;
    });
    if(!shown.length){rowsEl.innerHTML="<span style='color:#555'>no match</span>";return}
    var html="";
    shown.forEach(function(m){
      var tags=[];
      ["chat","code","vision"].forEach(function(role){
        if(routes[role]===m.name)tags.push('<span class="mtag">'+role+'</span>');
      });
      html+='<div class="model-row"><span class="mname">'+esc(m.name)+'</span>';
      html+=tags.join(" ");
      html+='<span class="mcaps">'+m.caps.join(", ")+'</span>';
      html+='<span class="msize">'+esc(m.size)+'</span><span class="role-btns">';
      ["chat","code","vision"].forEach(function(role){
        var cls=routes[role]===m.name?" active":"";
        html+='<button class="'+cls+'" onclick="setRole(\''+role+'\',\''+m.name.replace(/'/g,"\\'")+'\')">'+role+'</button>';
      });
      html+='</span></div>';
    });
    rowsEl.innerHTML=html;
  }
}

function setRole(role,name){
  fetch("/api/model",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({role:role,name:name})})
    .then(function(r){return r.json()}).then(function(r){
      banner("note","*",r.message||"model updated");
      loadModels();refreshStatus();
    }).catch(function(){});
}

/* ── hardware dashboard ────────────────────────────────────── */
var _hwBuilt=false,_hwHist={cpu:[],ram:[],gpu:[]};
function barClass(pct,warm,hot){
  warm=warm||70;hot=hot||88;
  return pct>=hot?"hot":pct>=warm?"warm":"cool";
}
function hwCard(label,sub,valueHtml,pct,warm,hot){
  var w=Math.max(0,Math.min(100,pct||0));
  return '<div class="hw-card"><div class="hw-top">'+
    '<span class="hw-label">'+label+'</span>'+
    '<span class="hw-sub">'+(sub||"")+'</span></div>'+
    '<div class="hw-val">'+valueHtml+'</div>'+
    '<div class="hw-bar '+barClass(w,warm,hot)+'"><i style="width:'+w+'%"></i></div></div>';
}
function buildHwSkeleton(){
  hwbody.innerHTML=
    '<div class="panel-tools">'+
      '<button class="pbtn accent" id="hwadvbtn">✨ AI recommendations</button>'+
      '<button class="pbtn" id="hwroastbtn">🔥 roast my PC</button>'+
      '<span class="hw-sub" id="hwts" style="margin-left:auto"></span>'+
    '</div>'+
    '<div class="hw-grid" id="hwgauges"></div>'+
    '<div class="hw-section-h">cpu cores</div><div class="hw-cores" id="hwcores"></div>'+
    '<div class="hw-grid" style="margin-top:14px">'+
      '<div class="hw-card"><span class="hw-label">cpu history</span><canvas class="hw-spark" id="sparkcpu"></canvas></div>'+
      '<div class="hw-card"><span class="hw-label">ram history</span><canvas class="hw-spark" id="sparkram"></canvas></div>'+
    '</div>'+
    '<div class="hw-section-h">top processes by ram</div><table class="hw-proc" id="hwproc"></table>'+
    '<div id="hwadvice"></div>';
  document.getElementById("hwadvbtn").onclick=hwRecommend;
  document.getElementById("hwroastbtn").onclick=hwRoast;
  _hwBuilt=true;
}
function loadHardware(){
  if(!_hwBuilt)buildHwSkeleton();
  fetch("/api/hardware").then(function(r){return r.json()}).then(function(s){
    if(s.error){hwbody.innerHTML='<span style="color:#c44">'+esc(s.error)+'</span>';_hwBuilt=false;return}
    hwUpdate(s);
  }).catch(function(){});
}
function pushHist(key,v){var a=_hwHist[key];a.push(v);if(a.length>80)a.shift()}
function drawSpark(id,data,warm,hot){
  var c=document.getElementById(id);if(!c)return;
  var w=c.offsetWidth||300,h=34;c.width=w;c.height=h;
  var ctx=c.getContext("2d");ctx.clearRect(0,0,w,h);
  if(data.length<2)return;
  var last=data[data.length-1];
  var col=last>=(hot||88)?"#c55":last>=(warm||70)?"#db4":"#3a8";
  var step=w/(data.length-1);
  ctx.beginPath();
  data.forEach(function(v,i){var y=h-(v/100)*h;i?ctx.lineTo(i*step,y):ctx.moveTo(0,y)});
  /* fill under the line */
  ctx.lineTo((data.length-1)*step,h);ctx.lineTo(0,h);ctx.closePath();
  ctx.globalAlpha=0.12;ctx.fillStyle=col;ctx.fill();ctx.globalAlpha=1;
  /* stroke the line on top */
  ctx.beginPath();
  data.forEach(function(v,i){var y=h-(v/100)*h;i?ctx.lineTo(i*step,y):ctx.moveTo(0,y)});
  ctx.strokeStyle=col;ctx.lineWidth=1.5;ctx.stroke();
}
function hwUpdate(s){
  var g="",cpu=s.cpu||{},mem=s.memory||{};
  /* CPU */
  var cpuSub=(cpu.freq_mhz?Math.round(cpu.freq_mhz)+" MHz":"")+
    (cpu.temp_c?" · "+Math.round(cpu.temp_c)+"°C":"");
  g+=hwCard("cpu",cpuSub,Math.round(cpu.percent||0)+'<small>%</small>',cpu.percent,75,92);
  /* RAM */
  g+=hwCard("memory",(mem.used_gb||0)+" / "+(mem.total_gb||0)+" GB",
    Math.round(mem.percent||0)+'<small>%</small>',mem.percent,75,90);
  /* GPU(s) */
  (s.gpus||[]).forEach(function(gp){
    var sub=[];if(gp.temp!=null)sub.push(Math.round(gp.temp)+"°C");
    if(gp.power_w!=null)sub.push(Math.round(gp.power_w)+"W");
    var vram=(gp.mem_used_mb&&gp.mem_total_mb)?
      ' <small>'+(gp.mem_used_mb/1024).toFixed(1)+'/'+(gp.mem_total_mb/1024).toFixed(1)+'GB</small>':'';
    g+=hwCard("gpu · "+esc(gp.name.slice(0,22)),sub.join(" · "),
      Math.round(gp.util||0)+'<small>%</small>'+vram,gp.util,75,92);
  });
  /* primary disk */
  if(s.disks&&s.disks.length){
    var d=s.disks[0];
    g+=hwCard("disk "+esc(d.device),d.free_gb+" GB free",
      Math.round(d.percent)+'<small>%</small>',d.percent,80,92);
  }
  /* temp card if we have a CPU temp */
  if(cpu.temp_c){
    g+=hwCard("cpu temp","thermal",Math.round(cpu.temp_c)+'<small>°C</small>',
      cpu.temp_c,70,85);
  }
  /* battery */
  if(s.battery){
    g+=hwCard("battery",s.battery.plugged?"charging":"on battery",
      Math.round(s.battery.percent)+'<small>%</small>',s.battery.percent,100,101);
  }
  document.getElementById("hwgauges").innerHTML=g;
  /* per-core */
  var cores="";
  (cpu.per_core||[]).forEach(function(v,i){
    var cls=v>=92?"#c55":v>=75?"#db4":"#3a8";
    cores+='<div class="hw-core"><div class="cbar"><i style="height:'+v+'%;background:'+cls+'"></i></div>'+
      Math.round(v)+'</div>';
  });
  document.getElementById("hwcores").innerHTML=cores;
  /* sparklines */
  pushHist("cpu",cpu.percent||0);pushHist("ram",mem.percent||0);
  drawSpark("sparkcpu",_hwHist.cpu,75,92);drawSpark("sparkram",_hwHist.ram,75,90);
  /* processes */
  var pr="";
  (s.top_ram||[]).forEach(function(p){
    pr+='<tr><td class="n">'+esc(p.name)+'</td><td class="r">'+
      (p.ram_mb>=1024?(p.ram_mb/1024).toFixed(1)+" GB":Math.round(p.ram_mb)+" MB")+
      '</td><td class="r">'+Math.round(p.cpu)+'%</td></tr>';
  });
  document.getElementById("hwproc").innerHTML=pr;
  var ts=document.getElementById("hwts");
  if(ts)ts.textContent="up "+(s.uptime_h||0)+"h · live";
}
function hwRecommend(){
  var box=document.getElementById("hwadvice");
  box.innerHTML='<div class="hw-advice"><div class="ha-head">analysing...</div>'+
    '<span class="dots"><span></span><span></span><span></span></span></div>';
  fetch("/api/hardware/recommend",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})
    .then(function(r){return r.json()}).then(function(d){
      if(d.error){box.innerHTML='<div class="hw-advice">'+esc(d.error)+'</div>';return}
      box.innerHTML='<div class="hw-advice"><div class="ha-head">✨ AI recommendations</div>'+md(d.advice||"")+'</div>';
    }).catch(function(){box.innerHTML='<div class="hw-advice">failed to get recommendations</div>'});
}
function hwRoast(){
  var box=document.getElementById("hwadvice");
  box.innerHTML='<div class="hw-advice"><div class="ha-head">🔥 warming up...</div></div>';
  fetch("/api/roast").then(function(r){return r.json()}).then(function(d){
    box.innerHTML='<div class="hw-advice"><div class="ha-head">🔥 the roast</div>'+
      esc(d.roast||d.error||"")+'</div>';
  }).catch(function(){box.innerHTML='<div class="hw-advice">no roast today</div>'});
}

/* ── mail panel ────────────────────────────────────────────── */
function loadMail(){
  mailbody.innerHTML="loading...";
  fetch("/api/mail?limit=15").then(function(r){return r.json()}).then(function(d){
    mailbody.innerHTML="";
    var tools=document.createElement("div");tools.className="panel-tools";
    if(!d.configured){
      mailbody.innerHTML='<div class="mem-note">Gmail not configured. Add an '+
        '<b>email</b> block to <code>~/.axiom/config.json</code> '+
        '(imap_host, smtp_host, user, app-password) and restart.</div>';
      return;
    }
    var dg=document.createElement("button");dg.className="pbtn accent";dg.textContent="✨ AI digest";
    dg.onclick=mailDigest;
    var rf=document.createElement("button");rf.className="pbtn";rf.textContent="refresh";
    rf.onclick=loadMail;
    var who=document.createElement("span");who.className="hw-sub";who.style.marginLeft="auto";
    who.textContent=d.user||"";
    tools.appendChild(dg);tools.appendChild(rf);tools.appendChild(who);
    mailbody.appendChild(tools);
    var dbox=document.createElement("div");dbox.id="maildigest";mailbody.appendChild(dbox);
    if(d.error){
      var e=document.createElement("div");e.className="mem-note";e.style.color="#c44";
      e.textContent=d.error;mailbody.appendChild(e);
    }
    var msgs=d.messages||[];
    if(!msgs.length){
      var z=document.createElement("div");z.className="mem-note";z.textContent="inbox is clear — no unread mail.";
      mailbody.appendChild(z);return;
    }
    var h=document.createElement("div");h.className="hw-section-h";h.textContent=msgs.length+" unread";
    mailbody.appendChild(h);
    msgs.forEach(function(m){
      var row=document.createElement("div");row.className="mail-row";
      row.innerHTML='<span class="mfrom">'+esc(m.from)+'</span>'+
        '<span class="msubj">'+esc(m.subject)+'</span>'+
        '<span class="macts"><button data-a="read">read</button>'+
        '<button data-a="reply">reply</button></span>';
      row.querySelectorAll(".macts button").forEach(function(b){
        b.onclick=function(){
          var a=b.getAttribute("data-a");
          if(a==="read"){q("Read email #"+m.id+" and summarize it.")}
          else{box.value="Draft a reply to email #"+m.id+": ";box.focus()}
          closeWin("mailwin");
        };
      });
      mailbody.appendChild(row);
    });
  }).catch(function(){mailbody.innerHTML="<span style='color:#c44'>failed to load mail</span>"});
}
function mailDigest(){
  var box=document.getElementById("maildigest");
  box.innerHTML='<div class="hw-advice"><div class="ha-head">reading inbox...</div>'+
    '<span class="dots"><span></span><span></span><span></span></span></div>';
  fetch("/api/mail/digest",{method:"POST"}).then(function(r){return r.json()}).then(function(d){
    box.innerHTML='<div class="hw-advice"><div class="ha-head">✨ inbox digest</div>'+
      md(d.digest||d.error||"")+'</div>';
  }).catch(function(){box.innerHTML='<div class="hw-advice">digest failed</div>'});
}

/* ── automations / scheduled tasks ─────────────────────────── */
function loadTasks(){
  tasksbody.innerHTML="loading...";
  fetch("/api/tasks").then(function(r){return r.json()}).then(function(d){
    tasksbody.innerHTML="";
    var note=document.createElement("div");note.className="mem-note";
    note.textContent="scheduled prompts AXIOM runs on its own. add one below or just ask in chat (\"every morning at 8, summarize my unread email\").";
    tasksbody.appendChild(note);
    var add=document.createElement("div");add.className="mem-add";
    var iw=document.createElement("input");iw.placeholder="when (e.g. at 09:00 / every 2 hours)";iw.style.width="220px";
    var ip=document.createElement("input");ip.placeholder="what to do";ip.style.flex="1";ip.style.minWidth="180px";
    var b=document.createElement("button");b.textContent="schedule";
    b.onclick=function(){
      if(!iw.value.trim()||!ip.value.trim())return;
      fetch("/api/tasks/add",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({when:iw.value.trim(),prompt:ip.value.trim()})})
        .then(function(r){return r.json()}).then(function(r){
          banner("note","*",r.message||r.error||"scheduled");loadTasks();
        }).catch(function(){});
    };
    add.appendChild(iw);add.appendChild(ip);add.appendChild(b);
    tasksbody.appendChild(add);
    var ts=d.tasks||[];
    var h=document.createElement("div");h.className="hw-section-h";
    h.textContent=ts.length?ts.length+" active":"no active automations";
    tasksbody.appendChild(h);
    ts.forEach(function(t){
      var row=document.createElement("div");row.className="task-row";
      var rep=t.repeat_s?'<span class="trepeat">↻ every '+Math.round(t.repeat_s)+'s</span>':'';
      row.innerHTML='<span class="tdue">'+esc(t.due)+'</span>'+rep+
        '<span class="tprompt">'+esc(t.prompt)+'</span>'+
        '<button class="mem-x">cancel</button>';
      row.querySelector(".mem-x").onclick=function(){
        fetch("/api/tasks/cancel",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({id:t.id})}).then(function(){loadTasks()}).catch(function(){});
      };
      tasksbody.appendChild(row);
    });
  }).catch(function(){tasksbody.innerHTML="<span style='color:#c44'>failed to load</span>"});
}

/* memory panel — what axiom knows about the user */
function memPost(url,payload){
  return fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)}).then(function(r){return r.json()})
    .then(function(){loadMemory();refreshStatus()}).catch(function(){});
}
function memRow(label,value,onDelete){
  var d=document.createElement("div");d.className="mem-row";
  var k=document.createElement("span");k.className="mk";k.textContent=label;
  var v=document.createElement("span");v.className="mv";v.textContent=value;
  var x=document.createElement("button");x.className="mem-x";x.textContent="forget";
  x.onclick=onDelete;
  d.appendChild(k);d.appendChild(v);d.appendChild(x);
  return d;
}
function loadMemory(){
  fetch("/api/memory").then(function(r){return r.json()}).then(function(d){
    memlist.innerHTML="";
    var note=document.createElement("div");note.className="mem-note";
    note.textContent="learned automatically as you talk; injected into every reply. add or forget entries freely.";
    memlist.appendChild(note);
    var h=document.createElement("h3");h.textContent="profile";
    memlist.appendChild(h);
    var keys=Object.keys(d.profile||{});
    if(!keys.length){
      var e=document.createElement("div");e.className="mem-note";
      e.textContent="(empty — tell axiom about yourself)";
      memlist.appendChild(e);
    }
    keys.forEach(function(k){
      memlist.appendChild(memRow(k,d.profile[k],function(){
        memPost("/api/memory/profile",{key:k,value:""});
      }));
    });
    var h2=document.createElement("h3");h2.textContent="facts";h2.style.marginTop="12px";
    memlist.appendChild(h2);
    var facts=d.facts||[];
    if(!facts.length){
      var e2=document.createElement("div");e2.className="mem-note";
      e2.textContent="(none yet)";
      memlist.appendChild(e2);
    }
    facts.forEach(function(f){
      memlist.appendChild(memRow(f.topic||"#"+f.id,f.content,function(){
        memPost("/api/memory/forget",{id:f.id});
      }));
    });
    var add=document.createElement("div");add.className="mem-add";
    var ik=document.createElement("input");ik.placeholder="key (e.g. name)";ik.style.width="140px";
    var iv=document.createElement("input");iv.placeholder="value";iv.style.flex="1";iv.style.minWidth="180px";
    var b=document.createElement("button");b.textContent="add to profile";
    b.onclick=function(){
      if(ik.value.trim()&&iv.value.trim()){
        memPost("/api/memory/profile",{key:ik.value.trim(),value:iv.value.trim()});
      }
    };
    add.appendChild(ik);add.appendChild(iv);add.appendChild(b);
    memlist.appendChild(add);
  }).catch(function(){memlist.innerHTML="<span style='color:#c44'>failed to load</span>"});
}

/* attachments: upload to the server, reference in the message */
function renderAttach(){
  attachrow.style.display=attachments.length?"":"none";
  attachrow.innerHTML="";
  attachments.forEach(function(p,i){
    var chip=document.createElement("span");chip.className="attach-chip";
    var nm=document.createElement("span");nm.textContent=p.split(/[\\\/]/).pop();
    var x=document.createElement("span");x.className="ax";x.textContent="x";
    x.title="remove";
    x.onclick=function(){attachments.splice(i,1);renderAttach()};
    chip.appendChild(nm);chip.appendChild(x);
    attachrow.appendChild(chip);
  });
}
function uploadFile(file){
  if(file.size>25*1024*1024){banner("err-banner","!",file.name+" is too large (25 MB max)");return}
  var chip=document.createElement("span");chip.className="attach-chip";
  chip.textContent="uploading "+file.name+"...";
  attachrow.style.display="";attachrow.appendChild(chip);
  var rd=new FileReader();
  rd.onload=function(){
    var b64=String(rd.result).split(",")[1]||"";
    fetch("/api/upload",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:file.name,data_b64:b64})})
      .then(function(r){return r.json()}).then(function(d){
        if(d.error){banner("err-banner","!",d.error);renderAttach();return}
        attachments.push(d.path);renderAttach();
      }).catch(function(){renderAttach()});
  };
  rd.readAsDataURL(file);
}
attachbtn.onclick=function(){fileinput.click()};
fileinput.onchange=function(){
  for(var i=0;i<fileinput.files.length;i++)uploadFile(fileinput.files[i]);
  fileinput.value="";
};
["dragover","dragenter"].forEach(function(evn){
  composer.addEventListener(evn,function(e){e.preventDefault();composer.classList.add("drag")});
});
["dragleave","dragend"].forEach(function(evn){
  composer.addEventListener(evn,function(){composer.classList.remove("drag")});
});
composer.addEventListener("drop",function(e){
  e.preventDefault();composer.classList.remove("drag");
  var fs=e.dataTransfer&&e.dataTransfer.files;
  if(fs)for(var i=0;i<fs.length;i++)uploadFile(fs[i]);
});

/* send / stop */
function setBusy(b){
  busy=b;
  sendbtn.textContent=b?"stop":"send";
  sendbtn.className=b?"stop":"";
}
function send(){
  if(busy)return;
  var t=box.value.trim();
  if(!t&&!attachments.length)return;
  if(attachments.length){
    var pre=attachments.map(function(p){return "[Attached file: "+p+"]"}).join("\n");
    t=pre+"\n"+(t||"Read the attached file(s) and summarize the contents.");
    attachments=[];renderAttach();
  }
  box.value="";box.style.height="auto";setBusy(true);
  fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text:t})})
    .catch(function(){setBusy(false)});
}
sendbtn.onclick=function(){
  if(busy){fetch("/api/stop",{method:"POST"}).catch(function(){})}
  else send();
};
box.addEventListener("keydown",function(e){if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}});
box.addEventListener("input",function(){box.style.height="auto";box.style.height=Math.min(box.scrollHeight,140)+"px"});

/* quick send */
function q(t){box.value=t;send()}

/* voice toggle */
micbtn.onclick=function(){
  micbtn._t=true;var on=!/v-(on|listening|speaking)/.test(micbtn.className);
  fetch("/api/voice",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({on:on})})
    .then(function(r){return r.json()}).then(function(r){voiceUI(r.active?"on":"off");if(r.message)banner("note","*",r.message)})
    .catch(function(){});
};

/* export chat */
document.getElementById("exportbtn").onclick=function(){window.location="/api/export"};

/* new session */
newbtn.onclick=function(){
  fetch("/api/session/new",{method:"POST"}).then(function(){
    log.innerHTML="";cur=null;refreshStatus();
  }).catch(function(){});
};

/* ── slide-down drawers: models / memory / chats / info ────── */
function togglePanel(panel,onOpen){
  [drawer,modelpanel,mempanel,sesspanel].forEach(function(p){if(p!==panel)p.classList.remove("open")});
  var open=panel.classList.toggle("open");
  syncNav();
  if(open&&onOpen)onOpen();
}
infobtn.onclick=function(){togglePanel(drawer)};
modelsbtn.onclick=function(){togglePanel(modelpanel,loadModels)};
membtn.onclick=function(){togglePanel(mempanel,loadMemory)};
sessbtn.onclick=function(){togglePanel(sesspanel,loadSessions)};

/* ── floating windows: hardware / mail / automations ───────── */
var hwbody=document.getElementById("hwbody"),
    mailbody=document.getElementById("mailbody"),
    tasksbody=document.getElementById("tasksbody"),
    hwbtn=document.getElementById("hwbtn"),
    mailbtn=document.getElementById("mailbtn"),
    tasksbtn=document.getElementById("tasksbtn"),
    hwTimer=null,_zTop=42;
var WINS={
  hwwin:{btn:hwbtn,onOpen:loadHardware},
  mailwin:{btn:mailbtn,onOpen:loadMail},
  taskswin:{btn:tasksbtn,onOpen:loadTasks}
};
function winEl(id){return document.getElementById(id)}
function focusWin(id){var w=winEl(id);if(w)w.style.zIndex=(++_zTop)}
function openWin(id){
  var w=winEl(id);if(!w)return;
  w.classList.add("open");focusWin(id);
  if(id==="hwwin"){if(hwTimer)clearInterval(hwTimer);hwTimer=setInterval(loadHardware,2000)}
  if(WINS[id]&&WINS[id].onOpen)WINS[id].onOpen();
  syncNav();
}
function closeWin(id){
  var w=winEl(id);if(!w)return;
  w.classList.remove("open");
  if(id==="hwwin"&&hwTimer){clearInterval(hwTimer);hwTimer=null}
  syncNav();
}
function toggleWin(id){
  var w=winEl(id);if(!w)return;
  w.classList.contains("open")?closeWin(id):openWin(id);
}
hwbtn.onclick=function(){toggleWin("hwwin")};
mailbtn.onclick=function(){toggleWin("mailwin")};
tasksbtn.onclick=function(){toggleWin("taskswin")};

/* nav active-state sync */
function syncNav(){
  modelsbtn.classList.toggle("active",modelpanel.classList.contains("open"));
  membtn.classList.toggle("active",mempanel.classList.contains("open"));
  sessbtn.classList.toggle("active",sesspanel.classList.contains("open"));
  infobtn.classList.toggle("active",drawer.classList.contains("open"));
  Object.keys(WINS).forEach(function(id){
    WINS[id].btn.classList.toggle("active",winEl(id).classList.contains("open"));
  });
}

/* make every floating window draggable by its header + click-to-front */
(function(){
  var fwins=document.querySelectorAll(".fwin");
  for(var i=0;i<fwins.length;i++)(function(win){
    win.addEventListener("mousedown",function(){focusWin(win.id)},true);
    var head=win.querySelector(".fwin-head"),dragging=false,ox=0,oy=0;
    head.addEventListener("mousedown",function(e){
      if(e.target.classList.contains("fw-x"))return;
      dragging=true;
      var r=win.getBoundingClientRect();
      ox=e.clientX-r.left;oy=e.clientY-r.top;
      win.style.right="auto";win.style.bottom="auto";
      win.style.left=r.left+"px";win.style.top=r.top+"px";
      document.body.style.userSelect="none";e.preventDefault();
    });
    window.addEventListener("mousemove",function(e){
      if(!dragging)return;
      var x=Math.max(0,Math.min(window.innerWidth-60,e.clientX-ox));
      var y=Math.max(0,Math.min(window.innerHeight-28,e.clientY-oy));
      win.style.left=x+"px";win.style.top=y+"px";
    });
    window.addEventListener("mouseup",function(){
      if(dragging){dragging=false;document.body.style.userSelect=""}
    });
  })(fwins[i]);
  var xs=document.querySelectorAll(".fwin-head .fw-x");
  for(var j=0;j<xs.length;j++)xs[j].onclick=function(){closeWin(this.getAttribute("data-win"))};
})();

/* chat history: render a session transcript into the log */
function loadHistory(){
  fetch("/api/history?limit=200").then(function(r){return r.json()}).then(function(d){
    var ms=d.messages||[];
    if(!ms.length)return;
    hide_wel();
    ms.forEach(function(m){
      if(m.role==="user"){addUser(m.content,"text")}
      else{
        var div=document.createElement("div");div.className="msg bot";
        div.innerHTML='<div class="tag">axiom</div><div class="bubble"></div>';
        div.querySelector(".bubble").innerHTML=md(m.content);
        log.appendChild(div);
      }
    });
    scroll();
  }).catch(function(){});
}
loadHistory();

/* past conversations */
function loadSessions(){
  fetch("/api/sessions").then(function(r){return r.json()}).then(function(d){
    sesslist.innerHTML="";
    var h=document.createElement("h3");h.textContent="conversations — click to continue one";
    sesslist.appendChild(h);
    var ss=d.sessions||[];
    if(!ss.length){
      var e=document.createElement("div");e.className="mem-note";
      e.textContent="(no conversations yet)";
      sesslist.appendChild(e);return;
    }
    ss.forEach(function(s){
      var row=document.createElement("div");
      row.className="sess-row"+(s.id===d.current?" current":"");
      var t=document.createElement("span");t.className="st";
      t.textContent=(s.id===d.current?"> ":"")+s.title;
      var dt=document.createElement("span");dt.className="sd";
      var when=new Date(s.ts*1000);
      dt.textContent=when.toLocaleDateString()+" "+
        ("0"+when.getHours()).slice(-2)+":"+("0"+when.getMinutes()).slice(-2);
      var c=document.createElement("span");c.className="sc";
      c.textContent=s.messages+" msg";
      row.appendChild(t);row.appendChild(dt);row.appendChild(c);
      row.onclick=function(){loadSession(s.id)};
      sesslist.appendChild(row);
    });
  }).catch(function(){sesslist.innerHTML="<span style='color:#c44'>failed to load</span>"});
}
function loadSession(id){
  fetch("/api/session/load",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:id})})
    .then(function(r){return r.json()}).then(function(d){
      if(!d.ok){banner("err-banner","!","could not load session "+id);return}
      sesspanel.classList.remove("open");
      log.innerHTML="";cur=null;
      loadHistory();refreshStatus();
      banner("note","*","continuing conversation #"+d.session);
    }).catch(function(){});
}

/* ── IDE: file tree + editor + live agent diffs ─────────────── */
var ws=document.getElementById("ws"),
    wsbtn=document.getElementById("wsbtn"),
    wsbadge=document.getElementById("wsbadge"),
    wsclose=document.getElementById("wsclose"),
    wspath=document.getElementById("wspath"),
    wstree=document.getElementById("wstree"),
    wsfile=document.getElementById("wsfile"),
    wstext=document.getElementById("wstext"),
    wsdiff=document.getElementById("wsdiff"),
    wsdiffbtn=document.getElementById("wsdiffbtn"),
    wssave=document.getElementById("wssave"),
    wsask=document.getElementById("wsask"),
    wschanges=document.getElementById("wschanges"),
    wschglist=document.getElementById("wschglist"),
    wsLoaded=false,wsDirty=false,wsCurFile="",
    chgs=[],chgUnseen=0,chgActive=null;

function fsize(n){return n>1048576?(n/1048576).toFixed(1)+"M":n>1024?Math.round(n/1024)+"K":n+"B"}

function wsRow(label,cls,onclick,size){
  var d=document.createElement("div");d.className="ws-row "+cls;
  d.textContent=label;
  if(size!==undefined){
    var s=document.createElement("span");s.className="fsize";s.textContent=fsize(size);
    d.appendChild(s);
  }
  d.onclick=onclick;
  return d;
}

function wsLoad(p){
  fetch("/api/workspace?path="+encodeURIComponent(p||"")).then(function(r){return r.json()}).then(function(d){
    if(d.error){wstree.innerHTML='<div class="ws-err"></div>';wstree.firstChild.textContent=d.error;return}
    wspath.value=d.path;
    var sep=d.path.indexOf("\\")>=0||d.path.indexOf(":")===1?"\\":"/";
    wstree.innerHTML="";
    if(d.parent)wstree.appendChild(wsRow("..","dir",function(){wsLoad(d.parent)}));
    d.dirs.forEach(function(n){
      wstree.appendChild(wsRow(n+"/","dir",function(){wsLoad(d.path+sep+n)}));
    });
    d.files.forEach(function(f){
      var full=d.path+sep+f.name;
      var row=wsRow(f.name,"file"+(full===wsCurFile?" active":""),function(){wsOpen(full)},f.size);
      wstree.appendChild(row);
    });
  }).catch(function(){});
}

function wsOpen(p,then){
  if(wsDirty&&!confirm("Discard unsaved changes to "+wsCurFile+"?"))return;
  fetch("/api/workspace/file?path="+encodeURIComponent(p)).then(function(r){return r.json()}).then(function(d){
    if(d.error){banner("err-banner","!",d.error);return}
    wsCurFile=d.path;wsDirty=false;
    wsfile.textContent=d.path;wsfile.className="";
    wstext.value=d.content;
    wsRenderView();wsSetMode("view");
    wsmode.style.display="";wssuggest.style.display="";
    var rows=wstree.querySelectorAll(".ws-row.file");
    for(var i=0;i<rows.length;i++)rows[i].classList.remove("active");
    var c=latestChange(d.path);
    wsdiffbtn.style.display=c?"":"none";
    if(!then)hideDiff();
    buddyOnOpen(d);
    if(then)then(d);
  }).catch(function(){});
}

/* diff pane */
function renderDiff(diffText){
  wsdiff.innerHTML="";
  diffText.split("\n").forEach(function(l){
    var d=document.createElement("div");d.className="diff-line";
    if(l.indexOf("+++")===0||l.indexOf("---")===0)d.className+=" diff-file";
    else if(l.indexOf("@@")===0)d.className+=" diff-hunk";
    else if(l.indexOf("+")===0)d.className+=" diff-add";
    else if(l.indexOf("-")===0)d.className+=" diff-del";
    d.textContent=l||" ";
    wsdiff.appendChild(d);
  });
}
function showDiff(c){
  renderDiff(c.diff||"(no diff)");
  wsdiff.classList.add("open");wsdiffbtn.classList.add("on");
}
function hideDiff(){wsdiff.classList.remove("open");wsdiffbtn.classList.remove("on")}
wsdiffbtn.onclick=function(){
  if(wsdiff.classList.contains("open")){hideDiff();return}
  var c=latestChange(wsCurFile);
  if(c)showDiff(c);
};

/* changes made by the agent */
function latestChange(path){
  for(var i=chgs.length-1;i>=0;i--)if(chgs[i].path===path)return chgs[i];
  return null;
}
function fmtTime(ts){
  var d=new Date((ts||0)*1000);
  return ("0"+d.getHours()).slice(-2)+":"+("0"+d.getMinutes()).slice(-2);
}
function openChange(c){
  ws.classList.add("open");
  if(!wsLoaded){wsLoaded=true;wsLoad("")}
  chgActive=c;
  wsOpen(c.path,function(){showDiff(c);renderChanges()});
}
function renderChanges(){
  if(!chgs.length){wschanges.style.display="none";return}
  wschanges.style.display="";
  wschglist.innerHTML="";
  chgs.slice().reverse().forEach(function(c){
    var d=document.createElement("div");
    d.className="chg-row"+(c===chgActive?" active":"");
    var p=document.createElement("span");p.className="chg-path";p.textContent=c.path;
    var a=document.createElement("span");a.className="chg-add";a.textContent="+"+(c.added||0);
    var r=document.createElement("span");r.className="chg-del";r.textContent="-"+(c.removed||0);
    var t=document.createElement("span");t.className="chg-time";t.textContent=fmtTime(c.ts);
    d.appendChild(p);d.appendChild(a);d.appendChild(r);d.appendChild(t);
    d.onclick=function(){openChange(c)};
    wschglist.appendChild(d);
  });
}
function updateBadge(){
  if(chgUnseen>0&&!ws.classList.contains("open")){
    wsbadge.style.display="";wsbadge.textContent=chgUnseen;
  }else{wsbadge.style.display="none";chgUnseen=0}
}
function onFileEdit(ev,live){
  chgs.push(ev);if(chgs.length>50)chgs.shift();
  renderChanges();
  if(live){
    if(!ws.classList.contains("open"))chgUnseen++;
    updateBadge();
    var name=ev.path.split(/[\\\/]/).pop();
    var d=addTool('<span class="fn">edit '+esc(name)+'</span> <span class="chg-add">+'+
      (ev.added||0)+'</span> <span class="chg-del">-'+(ev.removed||0)+
      '</span> <span class="res">'+esc(ev.path)+'</span>',"edit");
    d.title="click to view the diff in the IDE";
    d.onclick=function(){openChange(ev)};
    if(ws.classList.contains("open")&&ev.path===wsCurFile&&!wsDirty){
      wsOpen(ev.path,function(){showDiff(ev)});
    }
  }
}
/* restore agent edits from before this page load */
fetch("/api/changes").then(function(r){return r.json()}).then(function(d){
  (d.changes||[]).forEach(function(c){onFileEdit(c,false)});
}).catch(function(){});

wstext.addEventListener("input",function(){
  if(!wsDirty){wsDirty=true;wsfile.className="dirty"}
});

wssave.onclick=function(){
  if(!wsCurFile){banner("note","*","no file open");return}
  fetch("/api/workspace/file",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:wsCurFile,content:wstext.value})})
    .then(function(r){return r.json()}).then(function(d){
      if(d.error){banner("err-banner","!",d.error);return}
      wsDirty=false;wsfile.className="";
      banner("note","*","saved "+wsCurFile);
    }).catch(function(){});
};

wsask.onclick=function(){
  if(!wsCurFile)return;
  box.value="Regarding the file "+wsCurFile+": "+box.value;
  ws.classList.remove("open");
  box.focus();
  box.setSelectionRange(box.value.length,box.value.length);
};

wspath.addEventListener("keydown",function(e){
  if(e.key==="Enter")wsLoad(wspath.value.trim());
});

wsbtn.onclick=function(){
  ws.classList.toggle("open");
  if(ws.classList.contains("open")){
    if(!wsLoaded){wsLoaded=true;wsLoad("")}
    chgUnseen=0;updateBadge();
  }
};
wsclose.onclick=function(){ws.classList.remove("open")};

box.focus();

/* ── coding buddy + syntax-highlight view (inside the one IDE) ─ */
var ideMessages=document.getElementById("ide-messages"),
    ideChatInput=document.getElementById("ide-chat-input"),
    wsview=document.getElementById("wsview"),
    wsmode=document.getElementById("wsmode"),
    wssuggest=document.getElementById("wssuggest"),
    wsbuddy=document.getElementById("wsbuddy"),
    wsbuddybtn=document.getElementById("wsbuddybtn"),
    ideBusy=false;

/* language from the file extension — drives the highlighter */
function ideLangOf(path){
  var m=(path||"").toLowerCase().match(/\.([a-z0-9]+)$/);
  var e=m?m[1]:"";
  if(e==="py"||e==="pyi")return"python";
  if(e==="js"||e==="jsx"||e==="mjs"||e==="cjs")return"javascript";
  if(e==="ts"||e==="tsx")return"typescript";
  if(e==="go")return"go";if(e==="rs")return"rust";
  if(e==="java")return"java";if(e==="cs")return"csharp";
  if(e==="c"||e==="h"||e==="cpp"||e==="cc"||e==="hpp")return"c++";
  return e||"text";
}

var IDE_KW={
  python:"def class return if elif else for while try except finally with as import from pass break continue lambda yield global nonlocal raise assert del in is not and or None True False async await self",
  javascript:"function return if else for while try catch finally const let var new class extends import export from default async await yield typeof instanceof in of this null undefined true false switch case break continue throw delete void",
  typescript:"function return if else for while try catch finally const let var new class extends implements interface type enum import export from default async await yield typeof instanceof in of this null undefined true false switch case break continue throw public private protected readonly string number boolean any void never",
  go:"func return if else for range var const type struct interface map chan go defer package import nil true false switch case break continue select",
  rust:"fn return if else for while loop match let mut const struct enum impl trait use pub mod self Some None Ok Err true false as ref move async await",
  java:"public private protected class interface extends implements return if else for while try catch finally new import package static final void int String boolean this null true false switch case break continue throw",
  "c++":"int char float double void return if else for while struct class public private template typename const new delete this nullptr true false switch case break continue include using namespace"
};

function ideEsc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}

/* tokenise one line into highlighted HTML (escaped throughout) */
function ideHi(line,lang){
  var kws=(IDE_KW[lang]||"").split(" ");
  var kwset={};for(var i=0;i<kws.length;i++)kwset[kws[i]]=1;
  var cmt=(lang==="python"||lang==="shell"||lang==="ruby")?"#":(lang==="sql")?"--":"//";
  var rx=/("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\/\/[^\n]*|#[^\n]*|--[^\n]*)|(\b\d[\d_.eExXa-fA-F]*\b)|([A-Za-z_$][\w$]*)|(\s+|[^\w\s])/g;
  var out="",m;
  while((m=rx.exec(line))!==null){
    if(m[1]){out+='<span class="str">'+ideEsc(m[1])+'</span>'}
    else if(m[2]){
      var c=m[2];
      var isCmt=(cmt==="#"&&c[0]==="#")||(cmt==="//"&&c.slice(0,2)==="//")||(cmt==="--"&&c.slice(0,2)==="--");
      out+=isCmt?'<span class="cmnt">'+ideEsc(c)+'</span>':ideEsc(c);
    }
    else if(m[3]){out+='<span class="num">'+ideEsc(m[3])+'</span>'}
    else if(m[4]){
      var w=m[4];
      if(kwset[w])out+='<span class="kw">'+ideEsc(w)+'</span>';
      else if(/^[A-Z]/.test(w))out+='<span class="cls">'+ideEsc(w)+'</span>';
      else out+=ideEsc(w);
    }
    else{out+=ideEsc(m[5]||"")}
  }
  return out||"&nbsp;";
}

/* render the highlighted read view from the textarea's current content */
function wsRenderView(){
  var lang=ideLangOf(wsCurFile);
  var lines=(wstext.value||"").split("\n");
  var html="";
  for(var i=0;i<lines.length;i++){
    html+='<div class="code-line" data-line="'+(i+1)+'">'+
      '<div class="code-line-num">'+(i+1)+'</div>'+
      '<div class="code-line-content">'+ideHi(lines[i],lang)+'</div></div>';
  }
  wsview.innerHTML=html||'<div class="code-line"><div class="code-line-num">1</div><div class="code-line-content">&nbsp;</div></div>';
}

/* toggle highlighted view vs editable textarea */
function wsSetMode(mode){
  if(mode==="edit"){
    wsview.style.display="none";wstext.style.display="";
    wsmode.textContent="view";wssave.style.display="";
    wstext.focus();
  }else{
    wsRenderView();
    wstext.style.display="none";wsview.style.display="";
    wsmode.textContent="edit";wssave.style.display=wsDirty?"":"none";
  }
}
wsmode.onclick=function(){wsSetMode(wstext.style.display==="none"?"edit":"view")};

/* coding buddy — operates on the file open in the IDE (wsCurFile / wstext.value) */
function addIDEMsg(role,text){
  var d=document.createElement("div");d.className="ide-msg "+role;
  var bubble=document.createElement("div");bubble.className="ide-msg-bubble";
  if(role==="assistant")bubble.innerHTML=md(text);else bubble.textContent=text;
  d.appendChild(bubble);
  var t=document.createElement("div");t.className="ide-msg-time";
  var now=new Date();t.textContent=now.getHours()+":"+("0"+now.getMinutes()).slice(-2);
  d.appendChild(t);
  ideMessages.appendChild(d);ideMessages.scrollTop=ideMessages.scrollHeight;
}
function buddyOnOpen(d){
  addIDEMsg("assistant","Opened **"+d.path.split(/[\\/]/).pop()+"** ("+
    (wstext.value||"").split("\n").length+" lines). Ask about it, ⚡ suggest, or describe an edit and hit apply.");
}
function buddyWait(label){
  var w=document.createElement("div");w.className="ide-msg assistant";
  w.innerHTML='<div class="ide-msg-bubble">'+(label||"")+'<span class="dots"><span></span><span></span><span></span></span></div>';
  ideMessages.appendChild(w);ideMessages.scrollTop=ideMessages.scrollHeight;return w;
}
function buddyBusy(on){
  ideBusy=on;
  document.getElementById("ide-chat-send").disabled=on;
  document.getElementById("ide-chat-apply").disabled=on;
}

function ideAsk(){
  var q=(ideChatInput.value||"").trim();
  if(!q||ideBusy)return;
  if(!wsCurFile){addIDEMsg("assistant","Open a file from the tree first.");return}
  addIDEMsg("user",q);ideChatInput.value="";ideChatInput.style.height="auto";
  buddyBusy(true);var wait=buddyWait("");
  fetch("/api/ide/analyze",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({question:q,path:wsCurFile,code:wstext.value})})
    .then(function(r){return r.json()}).then(function(d){
      wait.remove();addIDEMsg("assistant",d.answer||d.error||"(no answer)");buddyBusy(false);
    }).catch(function(e){wait.remove();addIDEMsg("assistant","Error: "+e);buddyBusy(false)});
}

/* direct edit: the message is the instruction; the file is rewritten + saved */
function ideApply(){
  var instr=(ideChatInput.value||"").trim();
  if(!wsCurFile){addIDEMsg("assistant","Open a file from the tree first, then describe the edit.");return}
  if(!instr){ideChatInput.focus();return}
  if(ideBusy)return;
  addIDEMsg("user","✦ edit: "+instr);ideChatInput.value="";ideChatInput.style.height="auto";
  buddyBusy(true);var wait=buddyWait("✦ rewriting ");
  fetch("/api/ide/apply",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:wsCurFile,instruction:instr,code:wstext.value})})
    .then(function(r){return r.json()}).then(function(d){
      wait.remove();buddyBusy(false);
      if(d.error){addIDEMsg("assistant","⚠️ "+d.error);return}
      if(d.unchanged){addIDEMsg("assistant","No change needed — the file already satisfies that.");return}
      wstext.value=d.content;wsDirty=false;wsfile.className="";
      wsRenderView();wsSetMode("view");
      addIDEMsg("assistant","✅ Applied & saved to **"+wsCurFile.split(/[\\/]/).pop()+
        "** (+"+d.added+" −"+d.removed+"). See the diff in the changes strip below.");
    }).catch(function(e){wait.remove();buddyBusy(false);addIDEMsg("assistant","Error: "+e)});
}

/* inline AI suggestions injected beneath the referenced source line (view mode) */
function ideSuggest(){
  if(!wsCurFile){addIDEMsg("assistant","Open a file from the tree first.");return}
  if(ideBusy)return;
  wsSetMode("view");
  var old=wsview.querySelectorAll(".code-suggestion-row");
  for(var i=0;i<old.length;i++)old[i].remove();
  buddyBusy(true);var wait=buddyWait("⚡ reviewing ");
  fetch("/api/ide/suggest",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:wsCurFile,code:wstext.value})})
    .then(function(r){return r.json()}).then(function(d){
      wait.remove();buddyBusy(false);
      var sg=d.suggestions||[];
      if(!sg.length){addIDEMsg("assistant","Looks solid — no pressing suggestions.");return}
      sg.forEach(function(s){
        var anchor=wsview.querySelector('.code-line[data-line="'+s.line+'"]');
        var row=document.createElement("div");row.className="code-suggestion-row";
        row.innerHTML='<div class="code-line-num">&#9889;</div>'+
          '<div class="sg"><b>line '+s.line+':</b> '+esc(s.text)+'</div>';
        if(anchor&&anchor.nextSibling)wsview.insertBefore(row,anchor.nextSibling);
        else if(anchor)wsview.appendChild(row);else wsview.insertBefore(row,wsview.firstChild);
      });
      addIDEMsg("assistant","Added "+sg.length+" inline suggestion(s) in the view. ⚡ Switch to **edit** to act on them, or tell me to apply one.");
    }).catch(function(){wait.remove();buddyBusy(false);addIDEMsg("assistant","Review failed.")});
}

/* wire buddy + view controls */
document.getElementById("ide-chat-send").onclick=ideAsk;
document.getElementById("ide-chat-apply").onclick=ideApply;
wssuggest.onclick=ideSuggest;
wsbuddybtn.onclick=function(){
  wsbuddy.classList.toggle("hidden");
  wsbuddybtn.classList.toggle("on",!wsbuddy.classList.contains("hidden"));
};
ideChatInput.addEventListener("keydown",function(e){
  /* Enter = ask · Ctrl/Cmd+Enter = apply edit */
  if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)){e.preventDefault();ideApply();return}
  if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();ideAsk()}
});
ideChatInput.addEventListener("input",function(){
  this.style.height="auto";this.style.height=Math.min(60,this.scrollHeight)+"px";
});

addIDEMsg("assistant","👋 I'm your coding buddy. Open a file from the tree — it shows with syntax colours. Then **ask** about it, **⚡ suggest** improvements, or type an instruction and hit **apply** to edit the file directly (Ctrl+Enter). Use **edit** in the top bar to hand-edit and **save**.");
</script>
</body>
</html>
"""
