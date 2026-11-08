"""Web page for the sorter: books under review (with decision buttons), sorted history, and quarantine.

Runs in a background thread inside the sorter (Python's built-in http.server, no framework).
The buttons only work when a password is set; every form carries a secret token so that no
other website can press them for you.
"""
from __future__ import annotations

import base64
import hmac
import html
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

log = logging.getLogger("audiobook_sorter")
PER_PAGE = 50
MAX_FORM = 16_384

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--text:#1d1c1a;--muted:#6b6862;--line:#e3e0da;--accent:#2f6f5e;
--warn:#a5620b;--warn-bg:#fdf3e4;--bad:#b3261e;--bad-bg:#fbeceb;--ok:#2f6f5e;--chip:#efede8}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--text:#ecebe7;--muted:#9c998f;
--line:#33322f;--accent:#6cc2a8;--warn:#e6a54a;--warn-bg:#2d2416;--bad:#f08a80;--bad-bg:#2e1b1a;
--ok:#6cc2a8;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line)}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 24px;padding-top:14px;padding-bottom:14px}
.brand{font-weight:650;font-size:17px}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;padding:6px 12px;border-radius:8px}
nav a.on{background:var(--chip);color:var(--text);font-weight:600}
nav .n{font-size:12px;margin-left:4px;color:var(--muted)}
.status{display:flex;gap:16px;flex-wrap:wrap;margin-left:auto;font-size:13px;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
main{padding:24px 0 48px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px}
.card.warn{border-left:4px solid var(--warn)}.card.bad{border-left:4px solid var(--bad)}
.card h2{font-size:16px;margin:0 0 6px;word-break:break-word}
.reasons{margin:8px 0;padding:0;list-style:none}
.reasons li{background:var(--warn-bg);border-radius:6px;padding:6px 10px;margin-bottom:4px;font-size:14px;
word-break:break-word}
.card.bad .reasons li{background:var(--bad-bg)}
.meta{color:var(--muted);font-size:13px;word-break:break-word}
.meta b{color:var(--text);font-weight:550}
code{font:12.5px ui-monospace,Consolas,monospace;background:var(--chip);padding:1px 5px;border-radius:4px;
word-break:break-all}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;
overflow:hidden}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}
td .t{font-weight:550}td .s{color:var(--muted);font-size:13px}
.chip{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:var(--chip);
color:var(--muted);white-space:nowrap}.chip.seed{color:var(--accent)}
form.search{display:flex;gap:8px;margin-bottom:16px}
form.search input{flex:1;min-width:0;padding:8px 12px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);font:inherit}
form.search button,.pager a{padding:8px 14px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);font:inherit;text-decoration:none;cursor:pointer}
.pager{display:flex;gap:8px;align-items:center;margin-top:16px;color:var(--muted)}
.empty{color:var(--muted);text-align:center;padding:40px 16px}
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start;margin-top:12px;padding-top:12px;
border-top:1px solid var(--line)}
.actions form{margin:0}
.btn,.actions button,.actions summary{display:inline-block;padding:7px 14px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--text);font:inherit;font-size:14px;cursor:pointer;
list-style:none;line-height:1.4}
.actions summary::-webkit-details-marker{display:none}
.btn.primary,.actions button.primary{background:var(--accent);border-color:var(--accent);color:var(--card);
font-weight:600}
.btn.danger,.actions button.danger,.actions summary.danger{color:var(--bad);border-color:var(--bad)}
.actions details{flex-basis:auto}
.actions details[open]{flex-basis:100%}
.actions details[open] summary{background:var(--chip)}
.panel{margin-top:8px;padding:12px;border-radius:8px;background:var(--chip)}
.panel p{margin:0 0 10px;font-size:14px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:10px}
.fields label{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted)}
.fields input{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);
color:var(--text);font:inherit;font-size:14px;min-width:0}
.will{font-size:13px;color:var(--muted);margin-top:10px;word-break:break-word}
.will b{color:var(--text);font-weight:550}
.flash{border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:14px;word-break:break-word}
.flash.ok{background:var(--chip);border-left:4px solid var(--ok)}
.flash.err{background:var(--bad-bg);border-left:4px solid var(--bad)}
.note{font-size:13px;color:var(--muted);margin-top:10px}
@media (max-width:700px){.hide-sm{display:none}.status{margin-left:0}}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def when(ts: float | None) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return time.strftime("%d %b %Y", time.localtime(ts))


def size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def reasons(text: str) -> str:
    return "".join(f"<li>{esc(r)}</li>" for r in (text or "").split("; ") if r)


class Handler(BaseHTTPRequestHandler):
    history = None
    status: dict = {}
    cfg = None
    password = ""
    actions = None          # ReviewActions; None = buttons off (dry run)
    csrf = ""               # secret put in every form; a POST without it is refused
    flash: dict = {}        # message id -> (ok, text), shown once after a decision

    def log_message(self, fmt, *args):  # keep the sorter's log clean
        pass

    # ------------------------------------------------------------------ plumbing
    def authorized(self) -> bool:
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given, self.password)
        return False

    def send(self, code: int, body: str, ctype="text/html; charset=utf-8", extra=None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":  # Docker's health check; reveals nothing
            self.send(200, "ok", "text/plain")
            return
        if not self.authorized():
            self.send(401, "Login required", "text/plain", {"WWW-Authenticate": 'Basic realm="Audiobook sorter"'})
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/review"):
                msg = self.flash.pop(q.get("m", ""), None)
                self.send(200, self.page("review", self.review_page(msg), refresh=not self.can_act()))
            elif url.path == "/history":
                self.send(200, self.page("history", self.history_page(q.get("q", "").strip()[:100],
                                                                      int(q.get("page", "1") or 1))))
            elif url.path == "/quarantine":
                msg = self.flash.pop(q.get("m", ""), None)
                self.send(200, self.page("quarantine", self.quarantine_page(msg), refresh=not self.can_act()))
            else:
                self.send(404, "Not found", "text/plain")
        except ValueError:
            self.send(400, "Bad request", "text/plain")
        except Exception:
            log.exception("Web UI error")
            self.send(500, "Something went wrong; see audiobook_sorter.log", "text/plain")

    def can_act(self) -> bool:
        return bool(self.password) and self.actions is not None

    def do_POST(self):
        if not self.authorized():
            self.send(401, "Login required", "text/plain", {"WWW-Authenticate": 'Basic realm="Audiobook sorter"'})
            return
        if urlparse(self.path).path != "/review/action":
            self.send(404, "Not found", "text/plain")
            return
        if not self.can_act():
            self.send(403, "Decisions need a password: set one under [web] in config.toml.", "text/plain")
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
            self.send(403, "Refused: request came from another site.", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_FORM:
            self.send(400, "Bad request", "text/plain")
            return
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
        if not hmac.compare_digest(form.get("csrf", ""), self.csrf):
            self.send(403, "Refused: the page was out of date. Reload it and try again.", "text/plain")
            return
        try:
            event_id = int(form.get("id", ""))
        except ValueError:
            self.send(400, "Bad request", "text/plain")
            return
        do = form.get("do", "")
        try:
            if do == "accept":
                ok, text = True, self.actions.accept(event_id)
            elif do == "sort":
                ok, text = True, self.actions.sort_as(event_id, form.get("author", ""), form.get("series", ""),
                                                      form.get("sequence", ""), form.get("title", ""))
            elif do == "reject":
                ok, text = True, self.actions.reject(event_id)
            elif do == "allow":
                ok, text = True, self.actions.allow(event_id)
            elif do == "discard":
                ok, text = True, self.actions.discard(event_id)
            else:
                self.send(400, "Bad request", "text/plain")
                return
        except Exception as e:
            if type(e).__name__ != "ActionError":
                log.exception("Review action failed")
                text = "Something went wrong; see audiobook_sorter.log"
            else:
                text = str(e)
            ok = False
        key = secrets.token_urlsafe(8)
        if len(self.flash) > 50:
            self.flash.clear()
        self.flash[key] = (ok, text)
        back = "/quarantine" if do in ("allow", "discard") else "/"
        self.send(303, "", "text/plain", {"Location": f"{back}?m={key}"})

    # ------------------------------------------------------------------ layout
    def page(self, active: str, body: str, refresh: bool = True) -> str:
        counts = self.history.counts()
        n_review = len(self.history.pending_reviews())
        n_quarantine = sum(1 for r in self.history.quarantined() if self.waiting(r))
        tabs = [("review", "/", "Needs review", n_review), ("history", "/history", "Sorted", counts.get("sorted", 0)),
                ("quarantine", "/quarantine", "Quarantine", n_quarantine)]
        nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{esc(text)}'
                      f'<span class="n">{n}</span></a>' for key, href, text, n in tabs)
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{'<meta http-equiv="refresh" content="60">' if refresh else ''}
<title>Audiobook Sorter</title><style>{CSS}</style></head><body>
<header><div class="wrap top"><div class="brand">Audiobook Sorter</div><nav>{nav}</nav>
<div class="status">{self.status_bar()}</div></div></header>
<main class="wrap">{body}</main></body></html>"""

    def status_bar(self) -> str:
        s = self.status
        out = []
        if self.cfg.qbit:
            ok = s.get("qbit_ok")
            out.append(self.dot(ok, "qBittorrent", s.get("qbit_msg", "not checked yet")))
        ok = s.get("clamd_ok")
        msg = s.get("clamd_msg", "not checked yet")
        if ok:  # 'ClamAV 1.4.1/27450/Sat Nov  7 08:24:01 2026' -> signatures from Sat Nov 7
            parts = msg.split("/")
            msg = f"{parts[0]}, signatures {' '.join(parts[2].split()[:3])}" if len(parts) >= 3 else msg
        out.append(self.dot(ok, "ClamAV", msg))
        out.append(f"<span>Last check {esc(when(s.get('last_scan')) or 'never')}</span>")
        return "".join(out)

    @staticmethod
    def dot(ok, name: str, title: str) -> str:
        cls = "ok" if ok else ("bad" if ok is False else "")
        return f'<span title="{esc(title)}"><span class="dot {cls}"></span>{esc(name)}</span>'

    # ------------------------------------------------------------------ pages
    def review_page(self, msg=None) -> str:
        items = self.history.pending_reviews()
        flash = f'<div class="flash {"ok" if msg[0] else "err"}">{esc(msg[1])}</div>' if msg else ""
        if self.can_act():
            sub = "No confident Audible match. Accept the closest match, enter the details yourself, or reject it."
        else:
            sub = ("No confident Audible match. Fix the tags or rename the folder, then move it from the review "
                   "folder into the inbox and it will be sorted again.")
        head = f"<h1>Needs review</h1><p class='sub'>{sub}</p>{flash}"
        if not items:
            return head + "<div class='card empty'>Nothing waiting for review.</div>"
        cards = []
        for r in items:
            tagged = " · ".join(f"{k} <b>{esc(v)}</b>" for k, v in
                                (("author", r["author"]), ("title", r["title"]), ("series", r["series"]),
                                 ("#", r["sequence"])) if v)
            cards.append(f"""<div class="card warn"><h2>{esc(r['name'])}</h2>
<ul class="reasons">{reasons(r['reason'])}</ul>
<div class="meta">{('Tags: ' + tagged + '<br>') if tagged else ''}In <code>{esc(r['dest'])}</code><br>
{esc(when(r['ts']))} · {r['files']} files · {esc(size(r['size']))}</div>{self.decisions(r)}</div>""")
        note = ("" if self.can_act() or self.actions is None else
                "<p class='note'>To decide here instead, set <code>password</code> under <code>[web]</code> "
                "in config.toml and restart the sorter.</p>")
        return head + "".join(cards) + note

    def decisions(self, r: dict) -> str:
        if not self.can_act():
            return ""
        a = self.actions
        hidden = f'<input type="hidden" name="csrf" value="{esc(self.csrf)}"><input type="hidden" name="id" value="{r["id"]}">'
        cand = a.candidate(r)
        out = []
        will = ""
        if a.can_accept(r):
            if cand:
                will = f'<div class="will">Accept match files it as <b>{esc(a.preview(cand))}</b></div>'
            out.append(f'<form method="post" action="/review/action">{hidden}'
                       f'<button class="primary" name="do" value="accept">Accept match</button></form>')
        pre = cand or r
        fields = "".join(
            f'<label>{label}<input name="{name}" value="{esc(pre.get(name, "") if isinstance(pre, dict) else getattr(pre, name))}"'
            f'{extra}></label>'
            for label, name, extra in (("Author", "author", ' required maxlength="200"'),
                                       ("Series (optional)", "series", ' maxlength="200"'),
                                       ("Book number", "sequence", ' inputmode="decimal" maxlength="7" '
                                                                     'pattern="[0-9]{1,4}([.][0-9]{1,2})?"'),
                                       ("Title", "title", ' required maxlength="200"')))
        out.append(f'<details><summary>Edit and sort</summary><div class="panel">'
                   f'<p>Filed as Author / Series / NN - Title, exactly as you type it.</p>'
                   f'<form method="post" action="/review/action">{hidden}<div class="fields">{fields}</div>'
                   f'<button class="primary" name="do" value="sort">Sort it</button></form></div></details>')
        if a.is_torrent_copy(r):
            warn = ("Deletes the copy in the review folder. The torrent keeps seeding until its limit, "
                    "then it's removed together with its download.")
        else:
            warn = f"Permanently deletes these {r['files']} files ({esc(size(r['size']))}). This can't be undone."
        out.append(f'<details><summary class="danger">Reject</summary><div class="panel"><p>{warn}</p>'
                   f'<form method="post" action="/review/action">{hidden}'
                   f'<button class="danger" name="do" value="reject">Delete it</button></form></div></details>')
        return will + '<div class="actions">' + "".join(out) + "</div>"

    def history_page(self, query: str, page: int) -> str:
        rows, total = self.history.history(query, page, PER_PAGE)
        head = "<h1>Sorted</h1><p class='sub'>Books placed in the library, newest first.</p>"
        form = (f'<form class="search" method="get" action="/history"><input name="q" value="{esc(query)}" '
                f'placeholder="Search author, series or title" aria-label="Search"><button>Search</button></form>')
        if not rows:
            return head + form + f"<div class='card empty'>{'No matches.' if query else 'Nothing sorted yet.'}</div>"
        trs = []
        for r in rows:
            series = f"{esc(r['series'])}{' #' + esc(r['sequence']) if r['sequence'] else ''}" if r["series"] else ""
            if r["torrent_hash"]:
                chip = ('<span class="chip">removed</span>' if r["removed_ts"]
                        else '<span class="chip seed">seeding</span>')
            else:
                chip = '<span class="chip">moved</span>'
            trs.append(f"""<tr><td><div class="t">{esc(r['title'])}</div><div class="s">{esc(r['author'])}</div>
<div class="s">{series}</div></td>
<td class="hide-sm"><code>{esc(r['dest'])}</code><div class="s">from {esc(r['name'])}</div></td>
<td>{esc(when(r['ts']))}<div class="s">{esc(size(r['size']))}</div></td><td>{chip}</td></tr>""")
        pages = max(1, -(-total // PER_PAGE))
        qs = f"&q={quote(query)}" if query else ""
        prev = f'<a href="/history?page={page - 1}{qs}">Newer</a>' if page > 1 else ""
        nxt = f'<a href="/history?page={page + 1}{qs}">Older</a>' if page < pages else ""
        pager = f'<div class="pager">{prev}<span>Page {page} of {pages} · {total} books</span>{nxt}</div>'
        return (head + form + "<table><thead><tr><th>Book</th><th class='hide-sm'>Library folder</th>"
                "<th>Sorted</th><th></th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>" + pager)

    @staticmethod
    def waiting(r: dict) -> bool:
        """A quarantined item nobody has decided on, whose files are still there."""
        return not r.get("outcome") and bool(r["dest"]) and Path(r["dest"]).exists()

    def quarantine_page(self, msg=None) -> str:
        items = self.history.quarantined()
        flash = f'<div class="flash {"ok" if msg[0] else "err"}">{esc(msg[1])}</div>' if msg else ""
        if self.can_act():
            sub = ("Blocked by the file-type filter or ClamAV; nothing here has reached the library. Delete the "
                   "files, or allow a book you trust and it's sorted like any other.")
        else:
            sub = ("Blocked by the file-type filter or ClamAV; nothing here has reached the library. Set a "
                   "password under [web] in config.toml to delete or allow them from this page.")
        head = f"<h1>Quarantine</h1><p class='sub'>{sub}</p>{flash}"
        if not items:
            return head + "<div class='card empty'>Nothing has been quarantined.</div>"
        waiting = [r for r in items if self.waiting(r)]
        done = [r for r in items if not self.waiting(r)]
        cards = [self.quarantine_card(r) for r in waiting]
        if not waiting:
            cards.append("<div class='card empty'>Nothing waiting for a decision.</div>")
        if done:
            cards.append("<h1 style='margin-top:28px'>Handled</h1><p class='sub'>Earlier quarantine decisions.</p>")
            cards += [self.quarantine_card(r) for r in done[:50]]
        return head + "".join(cards)

    def quarantine_card(self, r: dict) -> str:
        outcome = r.get("outcome")
        if outcome == "allowed":
            where = "Allowed and sorted<br>"
        elif outcome == "discarded" or not self.waiting(r):
            where = "Deleted<br>"
        elif self.actions is not None and self.actions.quarantine_state(r) == "torrent":
            where = f"Still in qBittorrent (stopped, tagged quarantine) at <code>{esc(r['dest'])}</code><br>"
        else:
            where = f"In <code>{esc(r['dest'])}</code><br>"
        buttons = self.quarantine_decisions(r) if self.waiting(r) else ""
        return f"""<div class="card {'bad' if self.waiting(r) else ''}"><h2>{esc(r['name'])}</h2>
<ul class="reasons">{reasons(r['reason'])}</ul>
<div class="meta">{where}{esc(when(r['ts']))} · {r['files']} files · {esc(size(r['size']))}</div>{buttons}</div>"""

    def quarantine_decisions(self, r: dict) -> str:
        if not self.can_act():
            return ""
        hidden = (f'<input type="hidden" name="csrf" value="{esc(self.csrf)}">'
                  f'<input type="hidden" name="id" value="{r["id"]}">')
        torrent = self.actions.quarantine_state(r) == "torrent"
        virus = "virus found" in (r["reason"] or "")
        if torrent:
            allow_text = ("Links the audio into the library (or Needs review if Audible has no match), "
                          "then starts the torrent seeding again.")
            delete_text = (f"Permanently deletes this torrent's {r['files']} files ({esc(size(r['size']))}). "
                           "The torrent stays in qBittorrent's list, stopped. This can't be undone.")
        else:
            allow_text = ("Moves the audio into the library (or Needs review if Audible has no match). "
                          "Blocked extras left behind are deleted.")
            delete_text = (f"Permanently deletes these {r['files']} files ({esc(size(r['size']))}) from the "
                           "quarantine folder. This can't be undone.")
        confirm = ('<label class="note" style="display:block;margin:0 0 10px">'
                   '<input type="checkbox" required> ClamAV flagged this as a virus. I trust it anyway.</label>'
                   if virus else "")
        return ('<div class="actions">'
                f'<details><summary class="danger">Delete files</summary><div class="panel"><p>{delete_text}</p>'
                f'<form method="post" action="/review/action">{hidden}'
                f'<button class="danger" name="do" value="discard">Delete files</button></form></div></details>'
                f'<details><summary>Allow</summary><div class="panel"><p>{allow_text}</p>'
                f'<form method="post" action="/review/action">{hidden}{confirm}'
                f'<button class="primary" name="do" value="allow">Allow and sort</button></form></div></details>'
                '</div>')


def start(history, status: dict, cfg, actions=None) -> ThreadingHTTPServer:
    handler = type("SorterHandler", (Handler,), {"history": history, "status": status, "cfg": cfg,
                                                 "password": cfg.web.password, "actions": actions,
                                                 "csrf": secrets.token_urlsafe(32), "flash": {}})
    server = ThreadingHTTPServer((cfg.web.host, cfg.web.port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="webui", daemon=True).start()
    return server
