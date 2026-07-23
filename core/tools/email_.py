"""Email tools — IMAP/SMTP via stdlib. Registered only when configured.

config.json:
    "email": {"imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com",
              "user": "you@gmail.com", "password": "app-password"}
    "email_check_minutes": 5     # >0 = watch the inbox, notify on new mail

The InboxWatcher polls UNSEEN message ids — no model calls, no chat
spam: it only raises a notify event when genuinely new mail arrives.
"""

from __future__ import annotations

import email
import email.header
import imaplib
import smtplib
import threading
from email.message import EmailMessage
from typing import Callable, Optional


def _decode(value: str) -> str:
    parts = email.header.decode_header(value or "")
    out = ""
    for text, enc in parts:
        out += text.decode(enc or "utf-8", errors="replace") if isinstance(text, bytes) else text
    return out


def _imap(cfg) -> imaplib.IMAP4_SSL:
    e = cfg.email
    conn = imaplib.IMAP4_SSL(e["imap_host"], int(e.get("imap_port", 993)))
    conn.login(e["user"], e["password"])
    return conn


def _body_text(msg) -> str:
    """Best-effort plain-text body extraction."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def unread_list(cfg, limit: int = 12) -> list[dict]:
    """Structured unread inbox for the web mail panel. Never raises here —
    the caller decides how to surface connection errors."""
    n = max(1, min(40, int(limit or 12)))
    conn = _imap(cfg)
    try:
        conn.select("INBOX", readonly=True)
        _, data = conn.search(None, "UNSEEN")
        ids = data[0].split()
        out = []
        for uid in reversed(ids[-n:]):
            _, msg_data = conn.fetch(
                uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            msg = email.message_from_bytes(msg_data[0][1])
            out.append({
                "id": uid.decode(),
                "from": _decode(msg["From"] or ""),
                "subject": _decode(msg["Subject"] or "(no subject)"),
                "date": msg["Date"] or "",
            })
        return out
    finally:
        conn.logout()


def classify(cfg, sender: str, subject: str) -> Optional[dict]:
    """Match an incoming message against config `email_rules`.

    A rule: {"name": "...", "from": "substr", "subject": "substr",
             "priority": true, "star": true, "mark_read": false}
    Returns the first matching rule (case-insensitive substring match) or
    None. Empty `from`/`subject` in a rule means "any"."""
    rules = cfg.get("email_rules", []) or []
    s_from, s_subj = sender.lower(), subject.lower()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        fm = (rule.get("from") or "").lower()
        sm = (rule.get("subject") or "").lower()
        if (not fm or fm in s_from) and (not sm or sm in s_subj):
            return rule
    return None


def register(r) -> None:

    @r.register("email_unread", "List unread emails (sender, subject)",
                {"?limit": "integer: default 10"})
    def email_unread(ctx, limit: int = 10) -> str:
        n = max(1, min(25, int(limit or 10)))
        conn = _imap(ctx.cfg)
        try:
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, "UNSEEN")
            ids = data[0].split()
            if not ids:
                return "No unread emails."
            lines = [f"{len(ids)} unread email(s); latest {min(n, len(ids))}:"]
            for uid in reversed(ids[-n:]):
                _, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                msg = email.message_from_bytes(msg_data[0][1])
                lines.append(f"  #{uid.decode()}: {_decode(msg['From'])} — "
                             f"{_decode(msg['Subject'])} ({msg['Date']})")
            return "\n".join(lines)
        finally:
            conn.logout()

    @r.register("email_read", "Read one email body by id (from email_unread)",
                {"id": "string: message id"})
    def email_read(ctx, id: str) -> str:
        conn = _imap(ctx.cfg)
        try:
            conn.select("INBOX", readonly=True)
            _, msg_data = conn.fetch(id.encode(), "(BODY.PEEK[])")
            if not msg_data or msg_data[0] is None:
                return f"No message with id {id}."
            msg = email.message_from_bytes(msg_data[0][1])
            body = _body_text(msg)
            if len(body) > 6000:
                body = body[:6000] + "\n...[truncated]"
            return (f"From: {_decode(msg['From'])}\nSubject: {_decode(msg['Subject'])}\n"
                    f"Date: {msg['Date']}\n\n{body or '(no plain-text body)'}")
        finally:
            conn.logout()

    @r.register("email_send", "Send an email",
                {"to": "string: recipient address",
                 "subject": "string: subject line",
                 "body": "string: message body"},
                needs_confirm=bool(r.ctx.cfg and r.ctx.cfg.confirm_email_send))
    def email_send(ctx, to: str, subject: str, body: str) -> str:
        e = ctx.cfg.email
        msg = EmailMessage()
        msg["From"] = e["user"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        port = int(e.get("smtp_port", 465))
        host = e.get("smtp_host", e["imap_host"].replace("imap", "smtp"))
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(e["user"], e["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(e["user"], e["password"])
                s.send_message(msg)
        return f"Email sent to {to}."

    @r.register("email_search", "Search the inbox by sender or subject text",
                {"query": "string: text to match in From or Subject",
                 "?limit": "integer: default 10"})
    def email_search(ctx, query: str, limit: int = 10) -> str:
        n = max(1, min(25, int(limit or 10)))
        q = query.replace('"', "").strip()
        if not q:
            return "Empty query."
        conn = _imap(ctx.cfg)
        try:
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, f'(OR FROM "{q}" SUBJECT "{q}")')
            ids = data[0].split()
            if not ids:
                return f"No emails matching '{query}'."
            lines = [f"{len(ids)} match(es) for '{query}'; latest {min(n, len(ids))}:"]
            for uid in reversed(ids[-n:]):
                _, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                msg = email.message_from_bytes(msg_data[0][1])
                lines.append(f"  #{uid.decode()}: {_decode(msg['From'])} — "
                             f"{_decode(msg['Subject'])} ({msg['Date']})")
            return "\n".join(lines)
        finally:
            conn.logout()

    @r.register("schedule_email",
                "Schedule an email to be sent later: 'in 2 hours', 'at 09:00'",
                {"to": "string: recipient address",
                 "subject": "string: subject line",
                 "body": "string: message body",
                 "when": "string: time expression"})
    def schedule_email(ctx, to: str, subject: str, body: str, when: str) -> str:
        prompt = (f"Send this email now with email_send and confirm briefly. "
                  f"to: {to}\nsubject: {subject}\nbody:\n{body}")
        return ctx.scheduler.schedule(when, prompt)

    @r.register("email_digest",
                "AI summary of unread mail: what matters, what needs a reply",
                {"?limit": "integer: how many unread to consider, default 12"})
    def email_digest(ctx, limit: int = 12) -> str:
        n = max(1, min(30, int(limit or 12)))
        conn = _imap(ctx.cfg)
        try:
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, "UNSEEN")
            ids = data[0].split()
            if not ids:
                return "Inbox is clear — no unread mail."
            blocks = []
            for uid in reversed(ids[-n:]):
                _, md = conn.fetch(uid, "(BODY.PEEK[])")
                if not md or md[0] is None:
                    continue
                msg = email.message_from_bytes(md[0][1])
                snippet = " ".join(_body_text(msg).split())[:300]
                blocks.append(f"From: {_decode(msg['From'])}\n"
                              f"Subject: {_decode(msg['Subject'])}\n"
                              f"Snippet: {snippet}")
        finally:
            conn.logout()
        prompt = ("Summarize these unread emails for a busy person. Group by "
                  "urgency. Flag anything that needs a reply or has a deadline. "
                  "Be terse.\n\n" + "\n\n---\n\n".join(blocks))
        summary = ctx.complete(
            "You are an executive assistant triaging an inbox. Output short "
            "bullet points only, most urgent first.", prompt)
        return summary or f"{len(ids)} unread; model unavailable for summary."

    @r.register("email_draft_reply",
                "Draft (do NOT send) a reply to an email by id, in the user's voice",
                {"id": "string: message id from email_unread",
                 "intent": "string: what the reply should say / your intent"})
    def email_draft_reply(ctx, id: str, intent: str) -> str:
        conn = _imap(ctx.cfg)
        try:
            conn.select("INBOX", readonly=True)
            _, md = conn.fetch(id.encode(), "(BODY.PEEK[])")
            if not md or md[0] is None:
                return f"No message with id {id}."
            msg = email.message_from_bytes(md[0][1])
            original = _body_text(msg)[:3000]
            sender = _decode(msg["From"] or "")
            subject = _decode(msg["Subject"] or "")
        finally:
            conn.logout()
        prompt = (f"Original email from {sender}, subject '{subject}':\n\n"
                  f"{original}\n\n---\nWrite a reply. Intent: {intent}")
        draft = ctx.complete(
            "You draft email replies. Match a natural, professional tone. "
            "Output only the reply body — no subject line, no preamble, no "
            "explanation. Keep it appropriately brief.", prompt)
        return (f"Draft reply to {sender} (subject: Re: {subject}) — "
                f"review before sending:\n\n{draft}")


class InboxWatcher:
    """Background IMAP poll: notifies when new unseen mail arrives."""

    def __init__(self, cfg, notify, interval_s: float = 300.0,
                 summarize: Optional["Callable[[str], str]"] = None) -> None:
        self.cfg = cfg
        self.notify = notify
        self.interval_s = max(60.0, interval_s)
        self.summarize = summarize     # optional: body -> one-line AI summary
        self._known: set[bytes] = set()
        self._primed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="inbox-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(5 if not self._primed else self.interval_s):
            try:
                self._check()
            except Exception as e:
                # Transient IMAP failures are routine; stay quiet, retry later.
                print(f"[email] inbox check failed: {type(e).__name__}: {e}")

    def _check(self) -> None:
        conn = _imap(self.cfg)
        try:
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, "UNSEEN")
            ids = set(data[0].split())
            new = ids - self._known
            self._known = ids
            if not self._primed:           # first pass: baseline, no spam
                self._primed = True
                return
            if not new:
                return
            previews, priority = [], False
            summary_src = None
            for uid in sorted(new)[-3:]:
                _, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
                if not msg_data or msg_data[0] is None:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                sender = _decode(msg["From"] or "")
                subject = _decode(msg["Subject"] or "")
                rule = classify(self.cfg, sender, subject)
                tag = ""
                if rule:
                    if rule.get("priority"):
                        priority = True
                    if rule.get("name"):
                        tag = f"[{rule['name']}] "
                previews.append(f"{tag}{sender} — {subject}")
                if summary_src is None:
                    summary_src = _body_text(msg)[:2000]
            extra = f" (+{len(new) - 3} more)" if len(new) > 3 else ""
            prefix = "⚡ Priority email" if priority else "New email"
            note = f"{prefix}: " + "; ".join(previews) + extra
            if self.summarize and summary_src:
                try:
                    line = self.summarize(summary_src)
                    if line:
                        note += f"\n  ↳ {line}"
                except Exception:
                    pass
            self.notify(note)
        finally:
            conn.logout()
