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
import os
import re
import secrets
import shutil
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

log = logging.getLogger("audiobook_sorter")
PER_PAGE = 50
MAX_FORM = 262_144   # the Allow form lists every file of a quarantined item

# Warm, "reading room" palette: parchment and leather-brown rather than neutral grey.
CSS = """
:root{--bg:#f3ece0;--card:#fffdf8;--text:#2b2015;--muted:#7a6a55;--line:#e3d7c3;--accent:#3f6b52;
--accent2:#8a5a35;--warn:#a5620b;--warn-bg:#fbeedb;--bad:#9c3b35;--bad-bg:#f8e6e1;--ok:#3f6b52;
--chip:#ece2cf;--wood:#6b4a33;--shadow:rgba(60,38,18,.18)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1b150f;--card:#241c14;
--text:#eee2cf;--muted:#ab9a82;--line:#3a2e20;--accent:#7fb89a;--accent2:#d19a5f;--warn:#e6a54a;
--warn-bg:#2f2314;--bad:#e2897f;--bad-bg:#30201c;--ok:#7fb89a;--chip:#2c2318;--wood:#3a2a1c;
--shadow:rgba(0,0,0,.45)}}
*{box-sizing:border-box}body{margin:0;background:
radial-gradient(900px 500px at 15% -10%,rgba(138,90,53,.10),transparent),var(--bg);color:var(--text);
font:15px/1.55 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
h1,h2,.brand,.t{font-family:Georgia,"Iowan Old Style","Palatino Linotype","Book Antiqua",serif}
header{background:var(--card);border-bottom:1px solid var(--line);box-shadow:0 1px 0 var(--shadow)}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 24px;padding-top:14px;padding-bottom:14px}
.brand{display:flex;align-items:center;gap:8px;font-weight:650;font-size:19px;letter-spacing:.01em}
.brand svg{flex:none;color:var(--accent2)}
nav{display:flex;gap:4px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;padding:6px 12px;border-radius:8px;font-size:14.5px}
nav a.on{background:var(--chip);color:var(--text);font-weight:600}
nav .n{font-size:12px;margin-left:4px;color:var(--muted)}
.status{display:flex;gap:16px;flex-wrap:wrap;margin-left:auto;font-size:13px;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
main{padding:28px 0 48px}
h1{font-size:23px;margin:0 0 4px;font-weight:600}.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:14px;
box-shadow:0 1px 2px var(--shadow)}
.card.warn{border-left:4px solid var(--warn)}.card.bad{border-left:4px solid var(--bad)}
.card h2{font-size:16px;margin:0 0 6px;word-break:break-word;font-weight:600}
.book-row{display:flex;gap:16px;align-items:flex-start}
.book-row .book-body{flex:1;min-width:0}
.book-row .thumb{flex:none;width:72px}
.reasons{margin:8px 0;padding:0;list-style:none}
.reasons li{background:var(--warn-bg);border-radius:6px;padding:6px 10px;margin-bottom:4px;font-size:14px;
word-break:break-word}
.card.bad .reasons li{background:var(--bad-bg)}
.meta{color:var(--muted);font-size:13px;word-break:break-word}
.meta b{color:var(--text);font-weight:550}
code{font:12.5px ui-monospace,Consolas,monospace;background:var(--chip);padding:1px 5px;border-radius:4px;
word-break:break-all}
.shelf{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:22px 16px;margin-top:4px}
.book-card{display:flex;flex-direction:column}
.book-card .thumb{width:100%;margin-bottom:9px}
.book-card .t{font-size:14.5px;font-weight:600;line-height:1.3;word-break:break-word}
.book-card .s{color:var(--muted);font-size:12.5px;word-break:break-word}
.thumb{position:relative;aspect-ratio:1/1}   /* audiobook covers are square */
.cover{display:block;width:100%;height:100%;border-radius:5px;object-fit:cover;
box-shadow:0 8px 14px -8px var(--shadow),0 1px 0 rgba(255,255,255,.08) inset,
0 0 0 1px rgba(0,0,0,.08);transition:transform .15s ease}
.book-card:hover .cover{transform:translateY(-3px) rotate(-.4deg)}
.badge{position:absolute;left:4px;bottom:4px;font-size:10px;padding:2px 6px;border-radius:99px;
background:rgba(20,14,8,.72);color:#f3ece0;white-space:nowrap}
.badge.seed{background:var(--accent)}
code{font:12.5px ui-monospace,Consolas,monospace;background:var(--chip);padding:1px 5px;border-radius:4px;
word-break:break-all}
.chip{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;background:var(--chip);
color:var(--muted);white-space:nowrap}.chip.seed{color:var(--accent)}
form.search{display:flex;gap:8px;margin-bottom:20px}
form.search input{flex:1;min-width:0;padding:8px 12px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);font:inherit}
form.search button,.pager a{padding:8px 14px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);font:inherit;text-decoration:none;cursor:pointer}
.pager{display:flex;gap:8px;align-items:center;margin-top:20px;color:var(--muted)}
.empty{color:var(--muted);text-align:center;padding:40px 16px}
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start;margin-top:12px;padding-top:12px;
border-top:1px solid var(--line)}
.actions form{margin:0}
.btn,.actions button,.actions summary{display:inline-block;padding:7px 14px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--text);font:inherit;font-size:14px;cursor:pointer;
list-style:none;line-height:1.4}
.actions summary::-webkit-details-marker{display:none}
.btn.primary,.actions button.primary{background:var(--accent);border-color:var(--accent);color:#fff;
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
.files{max-height:320px;overflow:auto;margin:0 0 10px;padding:6px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--card)}
.files label{display:flex;gap:8px;align-items:baseline;padding:3px 0;font-size:13.5px;word-break:break-all}
.files label span{margin-left:auto;padding-left:12px;color:var(--muted);white-space:nowrap}
.files label.blocked{color:var(--bad)}
@media (max-width:700px){.hide-sm{display:none}.status{margin-left:0}
.shelf{grid-template-columns:repeat(auto-fill,minmax(112px,1fr));gap:16px 12px}}
"""

BOOK_ICON = ('<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M3 5.5C3 4.4 3.9 4 5.5 4H11v15.5H5.5C3.9 19.5 3 19 3 18z"/>'
            '<path d="M21 5.5C21 4.4 20.1 4 18.5 4H13v15.5h5.5c1.6 0 2.5-.5 2.5-1.5z"/></svg>')

COVER_NAMES = ("cover", "folder")
IMAGE_EXTS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# Warm, leathery spine colors for books with no cover image yet.
SPINE_COLORS = ("#8a5a35", "#6b4a33", "#9c6b45", "#7a4b3c", "#55684f", "#79536a", "#4f6a74", "#8c6a3f")


def find_cover(path: Path) -> Path | None:
    """A cover/folder image next to (or inside) a book's files, same rule as the sorter's own."""
    folder = path if path.is_dir() else path.parent
    try:
        for f in folder.iterdir():
            if f.is_file() and f.stem.lower() in COVER_NAMES and f.suffix.lower() in IMAGE_EXTS:
                return f
    except OSError:
        return None
    return None


AUDIO_EXTS = (".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac")
_embedded_cache: dict = {}   # audio path -> ((size, mtime), (bytes, mime) | None)


def embedded_cover(path: Path) -> tuple[bytes, str] | None:
    """The cover art stored inside the book's first audio file (most .m4b files have one)."""
    folder = path if path.is_dir() else path.parent
    try:
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            audio = path
        else:
            audio = min((f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTS),
                        key=lambda f: str(f).lower(), default=None)
        if audio is None:
            return None
        st = audio.stat()
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    hit = _embedded_cache.get(audio)
    if hit and hit[0] == key:
        return hit[1]
    found = None
    try:
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4, MP4Cover
        f = mutagen.File(audio)
        tags = getattr(f, "tags", None)
        if isinstance(f, MP4) and tags is not None and tags.get("covr"):
            c = tags["covr"][0]
            found = (bytes(c), "image/png" if c.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg")
        elif isinstance(tags, ID3):
            pics = tags.getall("APIC")
            if pics:
                found = (pics[0].data, pics[0].mime or "image/jpeg")
        elif getattr(f, "pictures", None):
            found = (f.pictures[0].data, f.pictures[0].mime or "image/jpeg")
    except Exception as e:  # unreadable file: just no cover
        log.debug("No embedded cover in %s: %s", audio, e)
    if found and (not found[0] or found[1] not in IMAGE_EXTS.values() or len(found[0]) > 20_000_000):
        found = None
    if len(_embedded_cache) > 2000:
        _embedded_cache.clear()
    _embedded_cache[audio] = (key, found)
    return found


def spine_color(key: str) -> str:
    return SPINE_COLORS[zlib.crc32((key or "").encode("utf-8", "replace")) % len(SPINE_COLORS)]


def book_cover(title: str, cover_url: str | None) -> str:
    """A real cover <img>, or a plain warm 'spine' with the book's initial when there isn't one yet."""
    if cover_url:
        return f'<img class="cover" src="{esc(cover_url)}" alt="" loading="lazy">'
    title = (title or "?").strip()
    letter = esc((title[:1] or "?").upper())
    return (f'<svg class="cover" viewBox="0 0 100 100" role="img" aria-label="{esc(title)}">'
            f'<rect width="100" height="100" rx="5" fill="{spine_color(title)}"/>'
            f'<rect x="7" y="7" width="86" height="86" rx="2" fill="none" '
            f'stroke="rgba(255,255,255,.32)" stroke-width="1.5"/>'
            f'<text x="50" y="55" font-family="Georgia,serif" font-size="44" fill="rgba(255,255,255,.92)" '
            f'text-anchor="middle" dominant-baseline="middle">{letter}</text></svg>')


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
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'")
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
            elif url.path == "/upload":
                msg = self.flash.pop(q.get("m", ""), None)
                self.send(200, self.page("upload", self.upload_page(msg), refresh=False))
            elif url.path == "/cover":
                self.cover_response(q.get("id", ""))
            else:
                self.send(404, "Not found", "text/plain")
        except ValueError:
            self.send(400, "Bad request", "text/plain")
        except Exception:
            log.exception("Web UI error")
            self.send(500, "Something went wrong; see audiobook_sorter.log", "text/plain")

    def can_act(self) -> bool:
        return bool(self.password) and self.actions is not None

    def cover_response(self, id_str: str) -> None:
        """The book's cover image, looked up by history event id (never a raw path from the client)."""
        try:
            event_id = int(id_str)
        except ValueError:
            self.send(404, "Not found", "text/plain")
            return
        r = self.history.event(event_id) if self.history else None
        dest = Path(r["dest"]) if r and r.get("dest") else None
        cover, allowed = None, False
        if dest:
            try:
                allowed = self.cover_allowed(dest)
                if allowed:
                    cover = find_cover(dest)
            except OSError:
                cover = None
        data = ctype = None
        if cover:
            try:
                data, ctype = cover.read_bytes(), IMAGE_EXTS.get(cover.suffix.lower(), "application/octet-stream")
            except OSError:
                data = None
        elif dest and allowed:
            found = embedded_cover(dest)
            if found:
                data, ctype = found
        if not data:
            self.send(404, "Not found", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def cover_allowed(self, dest: Path) -> bool:
        """Covers are only served from the library, review and quarantine folders."""
        resolved = dest.resolve()
        bases = (self.cfg.library_dir, self.cfg.review_dir, self.cfg.quarantine_dir)
        return any(b is not None and b.resolve() in resolved.parents for b in bases) and dest.exists()

    def cover_thumb(self, r: dict) -> str:
        """A book's cover thumbnail for a review/quarantine/history card: real cover if there is one."""
        dest = Path(r["dest"]) if r.get("dest") else None
        try:
            has_cover = bool(dest and self.cover_allowed(dest) and (find_cover(dest) or embedded_cover(dest)))
        except OSError:
            has_cover = False
        url = f"/cover?id={r['id']}" if has_cover else None
        title = r.get("title") or r.get("name") or ""
        return f'<div class="thumb">{book_cover(title, url)}</div>'

    def do_POST(self):
        if not self.authorized():
            self.close_connection = True
            self.send(401, "Login required", "text/plain", {"WWW-Authenticate": 'Basic realm="Audiobook sorter"'})
            return
        if urlparse(self.path).path == "/upload":
            self.handle_upload()
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
        raw = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        form = {k: v[0] for k, v in raw.items()}
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
                ok, text = True, self.actions.allow(event_id, raw.get("file", []))
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

    # ------------------------------------------------------------------ upload
    def upload_page(self, msg=None) -> str:
        flash = f'<div class="flash {"ok" if msg[0] else "err"}">{esc(msg[1])}</div>' if msg else ""
        head = ("<h1>Upload</h1><p class='sub'>Send an audiobook file from this computer to the sorter. "
                "It goes into the inbox and is checked and sorted like any other book: file-type filter "
                "and ClamAV, Audible match, then the library (or Needs review / Quarantine).</p>" + flash)
        if not self.can_act():
            return head + ("<div class='card empty'>Uploading needs a password: set <code>password</code> under "
                           "<code>[web]</code> in config.toml and restart the sorter.</div>")
        exts = ", ".join(self.cfg.audio_extensions)
        return head + f"""<div class="card"><form method="post" action="/upload" enctype="multipart/form-data">
<input type="hidden" name="csrf" value="{esc(self.csrf)}">
<div class="fields"><label>Audiobook file ({esc(exts)})
<input type="file" name="file" required accept="{esc(','.join(self.cfg.audio_extensions))}"></label></div>
<p class="note">Name the file <b>Author - Title</b> (or tag it) if you can; it helps the Audible match.
Big files take a while: keep this tab open until the page reloads. Your browser shows the progress.</p>
<div class="actions"><button class="primary">Upload and sort</button></div></form></div>"""

    def handle_upload(self) -> None:
        """Stream a multipart upload straight to disk (never into memory), then drop it in the inbox."""
        self.close_connection = True  # whatever happens, don't reuse a half-read connection

        def done(ok: bool, text: str) -> None:
            key = secrets.token_urlsafe(8)
            if len(self.flash) > 50:
                self.flash.clear()
            self.flash[key] = (ok, text)
            self.send(303, "", "text/plain", {"Location": f"/upload?m={key}"})

        if not self.can_act():
            self.send(403, "Uploading needs a password: set one under [web] in config.toml.", "text/plain")
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
            self.send(403, "Refused: request came from another site.", "text/plain")
            return
        m = re.search(r'boundary="?([^";]+)"?', self.headers.get("Content-Type", ""))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not m or not 0 < length <= MAX_UPLOAD:
            self.send(400 if length <= MAX_UPLOAD else 413, "Bad or too large upload", "text/plain")
            return
        inbox = self.cfg.source_dir
        try:
            if shutil.disk_usage(inbox).free < length + 512 * 1024 ** 2:
                self.send(507, "Not enough free space on the NAS for this upload.", "text/plain")
                return
        except OSError:
            pass
        tmp = inbox / f".upload-{secrets.token_hex(8)}.part"   # hidden + .part: the sorter ignores it
        try:
            name = self._receive(m.group(1).encode("latin-1"), length, tmp)
        except UploadError as e:
            tmp.unlink(missing_ok=True)
            if e.status:
                self.send(e.status, str(e), "text/plain")
            else:
                done(False, str(e))
            return
        except Exception:
            tmp.unlink(missing_ok=True)
            log.exception("Upload failed")
            done(False, "The upload failed; see audiobook_sorter.log.")
            return
        target = free_name(inbox / name)
        try:
            os.replace(tmp, target)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            done(False, f"Couldn't put the file in the inbox: {e}")
            return
        log.info("UPLOADED %s (%s) on the web page; it will be sorted shortly", target.name,
                 size(target.stat().st_size))
        done(True, f"Uploaded {target.name}. It will be sorted within about "
                   f"{max(1, self.cfg.settle_seconds // 60 + 1)} minute(s): check the Sorted, Needs review "
                   "and Quarantine tabs.")

    def _receive(self, boundary: bytes, length: int, tmp: Path) -> str:
        """Parse the multipart body: the csrf field must come first, then the file (written to tmp)."""
        r = _Reader(self.rfile, length)
        delim = b"--" + boundary
        r.read_until(delim + b"\r\n", 4096)
        csrf_ok = False
        while True:
            head = r.read_until(b"\r\n\r\n", 16384).decode("utf-8", "replace")
            disp = re.search(r"(?im)^content-disposition:.*$", head)
            disp = disp.group(0) if disp else ""
            field_name = re.search(r'\bname="([^"]*)"', disp)
            filename = re.search(r'\bfilename="([^"]*)"', disp)
            if field_name and field_name.group(1) == "csrf" and not filename:
                value = r.read_until(b"\r\n" + delim, 1024).decode("utf-8", "replace")
                if not hmac.compare_digest(value, self.csrf):
                    raise UploadError("Refused: the page was out of date. Reload it and try again.", 403)
                csrf_ok = True
            elif field_name and field_name.group(1) == "file" and filename:
                if not csrf_ok:
                    raise UploadError("Refused: the page was out of date. Reload it and try again.", 403)
                name = clean_filename(filename.group(1))
                if not name:
                    raise UploadError("That file name can't be used. Rename the file and try again.")
                if Path(name).suffix.lower() not in self.cfg.audio_extensions:
                    raise UploadError(f"{name} isn't an audiobook file. Allowed: "
                                      f"{', '.join(self.cfg.audio_extensions)}.")
                with open(tmp, "wb") as f:
                    written = r.stream_until(b"\r\n" + delim, f)
                if written == 0:
                    raise UploadError("The file was empty.")
                return name
            else:
                r.read_until(b"\r\n" + delim, 1024)  # some other small field: skip it
            if r.read_exact(2) == b"--":
                raise UploadError("No file was chosen.")

    # ------------------------------------------------------------------ layout
    def page(self, active: str, body: str, refresh: bool = True) -> str:
        counts = self.history.counts()
        n_review = len(self.history.pending_reviews())
        n_quarantine = sum(1 for r in self.history.quarantined() if self.waiting(r))
        tabs = [("review", "/", "Needs review", n_review), ("history", "/history", "Sorted", counts.get("sorted", 0)),
                ("quarantine", "/quarantine", "Quarantine", n_quarantine)]
        if self.can_act():
            tabs.append(("upload", "/upload", "Upload", ""))
        nav = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{esc(text)}'
                      f'<span class="n">{n}</span></a>' for key, href, text, n in tabs)
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{'<meta http-equiv="refresh" content="60">' if refresh else ''}
<title>Audiobook Sorter</title><style>{CSS}</style></head><body>
<header><div class="wrap top"><div class="brand">{BOOK_ICON}Audiobook Sorter</div><nav>{nav}</nav>
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
            cards.append(f"""<div class="card warn book-row">{self.cover_thumb(r)}<div class="book-body">
<h2>{esc(r['name'])}</h2>
<ul class="reasons">{reasons(r['reason'])}</ul>
<div class="meta">{('Tags: ' + tagged + '<br>') if tagged else ''}In <code>{esc(r['dest'])}</code><br>
{esc(when(r['ts']))} · {r['files']} files · {esc(size(r['size']))}</div>{self.decisions(r)}</div></div>""")
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

    def file_picker(self, r: dict) -> str:
        """Checkboxes for each file of a quarantined item; audio ticked, everything else not."""
        blocked = r["reason"] or ""
        rows = []
        for name, n, audio in self.actions.item_files(r):
            base = Path(name).name
            flag = f"{base} (" in blocked or f"{base}:" in blocked   # named in the block reason
            rows.append(f'<label class="{"blocked" if flag else ""}"><input type="checkbox" name="file" '
                        f'value="{esc(name)}"{" checked" if audio else ""}>{esc(name)}'
                        f'<span>{"blocked · " if flag else ""}{esc(size(n))}</span></label>')
        return f'<div class="files">{"".join(rows)}</div>'

    def history_page(self, query: str, page: int) -> str:
        rows, total = self.history.history(query, page, PER_PAGE)
        head = "<h1>Sorted</h1><p class='sub'>Books on the shelf, newest first.</p>"
        form = (f'<form class="search" method="get" action="/history"><input name="q" value="{esc(query)}" '
                f'placeholder="Search author, series or title" aria-label="Search"><button>Search</button></form>')
        if not rows:
            return head + form + f"<div class='card empty'>{'No matches.' if query else 'Nothing sorted yet.'}</div>"
        cards = "".join(self.book_card(r) for r in rows)
        pages = max(1, -(-total // PER_PAGE))
        qs = f"&q={quote(query)}" if query else ""
        prev = f'<a href="/history?page={page - 1}{qs}">Newer</a>' if page > 1 else ""
        nxt = f'<a href="/history?page={page + 1}{qs}">Older</a>' if page < pages else ""
        pager = f'<div class="pager">{prev}<span>Page {page} of {pages} · {total} books</span>{nxt}</div>'
        return head + form + f'<div class="shelf">{cards}</div>' + pager

    def book_card(self, r: dict) -> str:
        series = f"{esc(r['series'])}{' #' + esc(r['sequence']) if r['sequence'] else ''}" if r["series"] else ""
        if r["torrent_hash"]:
            badge, cls = ("removed", "") if r["removed_ts"] else ("seeding", " seed")
        else:
            badge, cls = "moved", ""
        dest = Path(r["dest"]) if r.get("dest") else None
        try:
            has_cover = bool(dest and self.cover_allowed(dest) and (find_cover(dest) or embedded_cover(dest)))
        except OSError:
            has_cover = False
        url = f"/cover?id={r['id']}" if has_cover else None
        title = f'{esc(r["title"])}{(" — " + series) if series else ""} · from {esc(r["name"])} · {esc(r["dest"])}'
        return f"""<div class="book-card" title="{title}">
<div class="thumb">{book_cover(r['title'], url)}<span class="badge{cls}">{badge}</span></div>
<div class="t">{esc(r['title'])}</div><div class="s">{esc(r['author'])}</div>
<div class="s">{series}</div><div class="s">{esc(when(r['ts']))}</div></div>"""

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
        return f"""<div class="card {'bad' if self.waiting(r) else ''} book-row">{self.cover_thumb(r)}
<div class="book-body"><h2>{esc(r['name'])}</h2>
<ul class="reasons">{reasons(r['reason'])}</ul>
<div class="meta">{where}{esc(when(r['ts']))} · {r['files']} files · {esc(size(r['size']))}</div>{buttons}</div></div>"""

    def quarantine_decisions(self, r: dict) -> str:
        if not self.can_act():
            return ""
        hidden = (f'<input type="hidden" name="csrf" value="{esc(self.csrf)}">'
                  f'<input type="hidden" name="id" value="{r["id"]}">')
        torrent = self.actions.quarantine_state(r) == "torrent"
        virus = "virus found" in (r["reason"] or "")
        if torrent:
            allow_text = ("Links the ticked files into the library (or Needs review if Audible has no match), "
                          "then starts the torrent seeding again. Unticked files stay in the download only.")
            delete_text = (f"Permanently deletes this torrent's {r['files']} files ({esc(size(r['size']))}). "
                           "The torrent stays in qBittorrent's list, stopped. This can't be undone.")
        else:
            allow_text = ("Moves the ticked files into the library (or Needs review if Audible has no match). "
                          "Unticked files are deleted.")
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
                f'<form method="post" action="/review/action">{hidden}{self.file_picker(r)}{confirm}'
                f'<button class="primary" name="do" value="allow">Allow and sort</button></form></div></details>'
                '</div>')


MAX_UPLOAD = 20 * 1024 ** 3   # 20 GB: far beyond any single audiobook file
CHUNK = 1024 * 1024


class UploadError(Exception):
    """A rejected upload. status set = refuse outright; None = show the message on the page."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _Reader:
    """Reads a request body of known length in chunks, with a small look-ahead buffer."""

    def __init__(self, raw, length: int):
        self.raw, self.left, self.buf = raw, length, b""

    def _fill(self) -> bool:
        if self.left <= 0:
            return False
        data = self.raw.read(min(CHUNK, self.left))
        if not data:
            self.left = 0
            return False
        self.left -= len(data)
        self.buf += data
        return True

    def read_until(self, marker: bytes, limit: int) -> bytes:
        """Everything up to marker (marker consumed); limit caps what may come before it."""
        while True:
            i = self.buf.find(marker)
            if i >= 0:
                out, self.buf = self.buf[:i], self.buf[i + len(marker):]
                return out
            if len(self.buf) > limit + len(marker) or not self._fill():
                raise UploadError("The upload was cut off or malformed. Try again.")

    def stream_until(self, marker: bytes, f) -> int:
        """Write everything up to marker to f; returns bytes written. Memory stays at about CHUNK."""
        written = 0
        while True:
            i = self.buf.find(marker)
            if i >= 0:
                f.write(self.buf[:i])
                self.buf = self.buf[i + len(marker):]
                return written + i
            keep = len(marker) - 1   # the marker may straddle two chunks
            if len(self.buf) > keep:
                f.write(self.buf[:-keep])
                written += len(self.buf) - keep
                self.buf = self.buf[-keep:]
            if not self._fill():
                raise UploadError("The upload was cut off before it finished. Try again.")

    def read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            if not self._fill():
                raise UploadError("The upload was cut off or malformed. Try again.")
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def clean_filename(name: str) -> str:
    """A browser's file name made safe for the inbox: no folders, no special characters."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:200] if name and not name.startswith(".") else ""


def free_name(p: Path) -> Path:
    i = 2
    out = p
    while out.exists():
        out = p.with_name(f"{p.stem} ({i}){p.suffix}")
        i += 1
    return out


def start(history, status: dict, cfg, actions=None) -> ThreadingHTTPServer:
    handler = type("SorterHandler", (Handler,), {"history": history, "status": status, "cfg": cfg,
                                                 "password": cfg.web.password, "actions": actions,
                                                 "csrf": secrets.token_urlsafe(32), "flash": {}})
    server = ThreadingHTTPServer((cfg.web.host, cfg.web.port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="webui", daemon=True).start()
    return server
