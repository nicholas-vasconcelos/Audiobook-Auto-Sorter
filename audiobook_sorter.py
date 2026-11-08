"""
Audiobook sorter: watches a folder for new audiobooks and moves them into an
Audiobookshelf-style library:  <library>/<Author>/<Series>/<N - Title>/<files>

Metadata comes from the files' tags (falling back to the file/folder name) and is
verified against the Audible catalog. Anything uncertain is moved to a review folder.

Usage:
    python audiobook_sorter.py                 # watch continuously
    python audiobook_sorter.py --once          # process what's there now and exit
    python audiobook_sorter.py --once --dry-run
    python audiobook_sorter.py --config other.toml
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import mutagen
import requests
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from rapidfuzz import fuzz, utils
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import scanner
from history import History
from qbit import Qbit, QbitError

SCRIPT_DIR = Path(__file__).resolve().parent
# Where the log and history database live (/config in Docker).
DATA_DIR = Path(os.environ.get("SORTER_DATA_DIR") or SCRIPT_DIR)

# Fuzzy matching that ignores case and punctuation ('Double-Blind:' == 'double blind')
_tsr = partial(fuzz.token_set_ratio, processor=utils.default_process)
_ratio = partial(fuzz.ratio, processor=utils.default_process)
log = logging.getLogger("audiobook_sorter")

PARTIAL_SUFFIXES = {".part", ".crdownload", ".tmp", ".!qb", ".partial", ".download"}
POLL_SECONDS = 5
RETRY_SECONDS = 300          # wait before retrying after a network / NAS error
SCANNER_RETRY_SECONDS = 60   # clamd still loading its signatures, etc.
TORRENT_POLL_SECONDS = 30    # how often qBittorrent is asked for finished torrents
SEED_CHECK_SECONDS = 300     # how often finished-seeding torrents are looked for

TAG_SORTED, TAG_REVIEW = "sorted", "review"
DONE_STATES = {"uploading", "stalledUP", "queuedUP", "forcedUP", "pausedUP", "stoppedUP"}
STOPPED_STATES = {"pausedUP", "stoppedUP"}


# --------------------------------------------------------------------------- config

@dataclass
class QbitSettings:
    url: str
    username: str = ""
    password: str = ""
    category: str = "audiobooks"
    delete_after_seeding: bool = True
    path_map: list[tuple[str, str]] = field(default_factory=list)  # qBittorrent path -> path here


@dataclass
class ScanSettings:
    allowed_extensions: tuple[str, ...] = scanner.DEFAULT_ALLOWED  # plus the audio extensions
    clamd_host: str = ""     # empty = no ClamAV (file-type filter only)
    clamd_port: int = 3310


@dataclass
class MetadataSettings:
    write_opf: bool = True          # metadata.opf next to each book (Audiobookshelf prefers it to the tags)
    download_cover: bool = True     # Audible's cover as cover.jpg, unless the folder already has a cover
    asin_in_folder: bool = False    # "Project Hail Mary [B08G9PRS1K]": lets Audiobookshelf match by ASIN


@dataclass
class WebSettings:
    host: str = "0.0.0.0"
    port: int = 8090
    password: str = ""


@dataclass
class Config:
    source_dir: Path
    library_dir: Path
    review_dir: Path
    quarantine_dir: Path | None = None
    audible_region: str = "com"
    match_threshold: int = 85
    settle_seconds: int = 60
    audio_extensions: tuple[str, ...] = (".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac")
    dry_run: bool = False
    qbit: QbitSettings | None = None
    scan: ScanSettings = field(default_factory=ScanSettings)
    web: WebSettings | None = None
    metadata: MetadataSettings = field(default_factory=MetadataSettings)


def load_config(path: Path) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))  # tolerate a BOM from Notepad/PowerShell
    source = Path(raw["source_dir"]).expanduser()
    library = Path(raw["library_dir"]).expanduser()
    review = Path(raw["review_dir"]).expanduser() if raw.get("review_dir") else source / "_needs_review"
    quarantine = (Path(raw["quarantine_dir"]).expanduser() if raw.get("quarantine_dir")
                  else source / "_quarantine")
    if not source.is_dir():
        sys.exit(f"source_dir does not exist: {source}  (edit {path})")
    cfg = Config(
        source_dir=source,
        library_dir=library,
        review_dir=review,
        quarantine_dir=quarantine,
        audible_region=raw.get("audible_region", "com"),
        match_threshold=int(raw.get("match_threshold", 85)),
        settle_seconds=int(raw.get("settle_seconds", 60)),
        audio_extensions=tuple(e.lower() for e in raw.get("audio_extensions", Config.audio_extensions)),
        dry_run=bool(raw.get("dry_run", False)),
    )
    if q := raw.get("qbittorrent"):
        cfg.qbit = QbitSettings(
            url=q["url"],
            username=q.get("username", ""),
            password=os.environ.get("QBIT_PASSWORD") or q.get("password", ""),
            category=q.get("category", "audiobooks"),
            delete_after_seeding=bool(q.get("delete_after_seeding", True)),
            path_map=[(k, v) for k, v in (q.get("path_map") or {}).items()],
        )
    s = raw.get("scan", {})
    cfg.scan = ScanSettings(
        allowed_extensions=tuple(e.lower() for e in s.get("allowed_extensions", scanner.DEFAULT_ALLOWED)),
        clamd_host=s.get("clamd_host", ""),
        clamd_port=int(s.get("clamd_port", 3310)),
    )
    md = raw.get("metadata", {})
    cfg.metadata = MetadataSettings(write_opf=bool(md.get("write_opf", True)),
                                    download_cover=bool(md.get("download_cover", True)),
                                    asin_in_folder=bool(md.get("asin_in_folder", False)))
    if (w := raw.get("web")) is not None and w.get("enabled", True):
        cfg.web = WebSettings(host=w.get("host", "0.0.0.0"), port=int(w.get("port", 8090)),
                              password=os.environ.get("SORTER_UI_PASSWORD") or w.get("password", ""))
    return cfg


# --------------------------------------------------------------------------- finding books

@dataclass
class Book:
    root: Path                      # a folder (whole folder is the book) or its first audio file
    audio: list[Path]               # audio files, naturally sorted
    files: list[Path] = field(default_factory=list)  # everything to move (audio + extras)
    album_shared: bool = False      # split out of a folder whose files all share one album tag
    label: str = ""                 # how it's named in logs (set for books from torrents)

    @property
    def name(self) -> str:
        return self.root.stem if self.root.is_file() else self.root.name


def natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))]


# Subfolders like "CD1", "Disc 2", "Part 3" are pieces of one book, not separate books.
DISC_RE = re.compile(r"^\s*(?:cd|disc|disk|part|pt|vol(?:ume)?)[\s._-]*\d+\b", re.I)
# Title tags like "Part 2", "Chapter 3", "Track 7" mean the files are pieces of one book.
PIECE_RE = re.compile(r"\b(?:part|pt|disc|disk|cd|chapter|ch|track)\b\.?\s*\d", re.I)
LONG_FILE_SECONDS = 2.5 * 3600  # chapter files are shorter than this; whole books are longer


def is_audio(cfg: Config, p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in cfg.audio_extensions


def special_dirs(cfg: Config) -> set[Path]:
    """Folders that may sit inside source_dir but are never books."""
    return {d.resolve() for d in (cfg.review_dir, cfg.quarantine_dir) if d is not None}


def find_books(cfg: Config) -> list[Book]:
    books = []
    skip = special_dirs(cfg)
    for entry in sorted(cfg.source_dir.iterdir(), key=natural_key):
        if entry.name.startswith(".") or entry.resolve() in skip:
            continue
        if is_audio(cfg, entry):
            books.append(Book(entry, [entry], [entry]))
        elif entry.is_dir():
            try:
                if any(p.suffix.lower() in PARTIAL_SUFFIXES for p in entry.rglob("*")):
                    continue  # still downloading somewhere in here
                books.extend(books_in_folder(cfg, entry))
            except OSError as e:
                log.debug("Skipping %s: %s", entry, e)
    return books


def books_in_folder(cfg: Config, folder: Path) -> list[Book]:
    """One folder may be a single book, or a collection of books (subfolders or loose files)."""
    files = sorted((p for p in folder.rglob("*") if p.is_file()), key=natural_key)
    audio = [p for p in files if p.suffix.lower() in cfg.audio_extensions]
    if not audio:
        return []
    subdirs = [d for d in sorted(folder.iterdir(), key=natural_key)
               if d.is_dir() and any(is_audio(cfg, p) for p in d.rglob("*"))]
    direct = sorted((p for p in folder.iterdir() if is_audio(cfg, p)), key=natural_key)

    if subdirs and not all(DISC_RE.match(d.name) for d in subdirs):
        # e.g. "Monarch/1 Monarch A Prince Out of Time/", "Monarch/2 Monarch II .../"
        out = []
        for d in subdirs:
            out.extend(books_in_folder(cfg, d))
        if direct:
            out.extend(split_loose_files(cfg, folder, direct) or [loose_group(folder, direct, False)])
        return out

    split = split_loose_files(cfg, folder, direct) if not subdirs else None
    return split or [Book(folder, audio, files)]


def loose_group(folder: Path, audio: list[Path], album_shared: bool) -> Book:
    """A book made of some files in a shared folder; extras with the same name go along (X.jpg, X.cue)."""
    stems = {p.stem.lower() for p in audio}
    extras = [p for p in folder.iterdir() if p.is_file() and p not in audio and p.stem.lower() in stems]
    return Book(audio[0], audio, audio + extras, album_shared=album_shared)


def split_loose_files(cfg: Config, folder: Path, direct: list[Path]) -> list[Book] | None:
    """Return one Book per book when a folder's audio files are several books, else None."""
    if len(direct) < 2:
        return None
    info = {p: probe(p) for p in direct}
    albums = {p: norm_text(info[p][0].get("album", "")) for p in direct}

    # Different album tags -> each album is a book.
    if all(albums.values()) and len(set(albums.values())) > 1:
        groups: dict[str, list[Path]] = {}
        for p in direct:
            groups.setdefault(albums[p], []).append(p)
        return [loose_group(folder, g, False) for g in groups.values()]

    # Same (or no) album, but every file is book-length with its own distinct title -> one book per file.
    titles = [norm_text(info[p][0].get("title", "")) for p in direct]
    long_enough = all(info[p][1] >= LONG_FILE_SECONDS for p in direct)
    distinct = all(titles) and len(set(titles)) == len(titles) and not any(PIECE_RE.search(t) for t in titles)
    if long_enough and (distinct or not any(titles)):
        shared = bool(all(albums.values()) and len(set(albums.values())) == 1)
        return [loose_group(folder, [p], shared) for p in direct]
    return None


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def signature(book: Book) -> tuple | None:
    """Size/mtime fingerprint; None if the book is obviously still being written."""
    try:
        if book.root.is_dir():
            book.files = sorted((p for p in book.root.rglob("*") if p.is_file()), key=natural_key)
        if any(p.suffix.lower() in PARTIAL_SUFFIXES for p in book.files):
            return None
        stats = [p.stat() for p in book.files]
    except OSError:
        return None
    return (len(stats), sum(s.st_size for s in stats), max(s.st_mtime for s in stats))


def files_unlocked(book: Book) -> bool:
    for p in book.files:
        try:
            with open(p, "r+b"):
                pass
        except OSError:
            return False
    return True


# --------------------------------------------------------------------------- tags

@dataclass
class Meta:
    author: str = ""
    title: str = ""
    series: str = ""
    sequence: str = ""
    narrator: str = ""
    source: str = "tags"
    title_variants: list[str] = field(default_factory=list)  # alternative titles to search with
    author_candidates: list[str] = field(default_factory=list)  # tag author, then filename/folder author
    seq_hint: str = ""   # a leading number like "1 Double-Blind": only used to break ties
    asin: str = ""       # Audible product id, for a match / closest candidate
    product: dict | None = field(default=None, repr=False)  # the full Audible record (for metadata.opf)

    def __str__(self):
        s = f"author={self.author!r} title={self.title!r}"
        if self.series:
            s += f" series={self.series!r} #{self.sequence or '?'}"
        return s


def _first(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, tuple):  # MP4 track tuples
        v = v[0]
    return str(v).strip()


_probe_cache: dict[Path, tuple[tuple, tuple[dict[str, str], float]]] = {}


def probe(path: Path) -> tuple[dict[str, str], float]:
    """(tags, length in seconds), cached until the file changes."""
    try:
        st = path.stat()
        key = (st.st_size, st.st_mtime)
    except OSError:
        return {}, 0.0
    hit = _probe_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        f = mutagen.File(path)
    except Exception as e:  # corrupt / unsupported
        log.debug("Could not read tags from %s: %s", path, e)
        f = None
    length = float(getattr(getattr(f, "info", None), "length", 0) or 0)
    result = (_extract_tags(f), length)
    _probe_cache[path] = (key, result)
    return result


def read_tags(path: Path) -> dict[str, str]:
    return probe(path)[0]


def _extract_tags(f) -> dict[str, str]:
    """Return a normalized dict: artist, albumartist, album, title, composer, series, part, grouping."""
    out: dict[str, str] = {}
    if f is None or f.tags is None:
        return out
    t = f.tags

    if isinstance(f, MP4):
        def mp4(key):
            return _first(t.get(key))
        out = {
            "artist": mp4("©ART"), "albumartist": mp4("aART"), "album": mp4("©alb"),
            "title": mp4("©nam"), "composer": mp4("©wrt"), "grouping": mp4("©grp"),
            "series": mp4("----:com.apple.iTunes:SERIES") or mp4("----:com.apple.iTunes:series")
            or mp4("©mvn"),
            "part": mp4("----:com.apple.iTunes:SERIES-PART") or mp4("----:com.apple.iTunes:series-part")
            or mp4("©mvi"),
        }
    elif isinstance(t, ID3):
        def id3(key):
            fr = t.get(key)
            return _first(fr.text) if fr is not None and hasattr(fr, "text") else ""
        txxx = {fr.desc.lower(): _first(fr.text) for fr in t.getall("TXXX")}
        out = {
            "artist": id3("TPE1"), "albumartist": id3("TPE2"), "album": id3("TALB"),
            "title": id3("TIT2"), "composer": id3("TCOM"), "grouping": id3("TIT1"),
            "series": txxx.get("series") or id3("MVNM"),
            "part": txxx.get("series-part") or txxx.get("series_part") or id3("MVIN"),
        }
    else:  # Vorbis comments (FLAC / Ogg / Opus)
        def vc(key):
            return _first(t.get(key))
        out = {
            "artist": vc("artist"), "albumartist": vc("albumartist"), "album": vc("album"),
            "title": vc("title"), "composer": vc("composer"), "grouping": vc("grouping"),
            "series": vc("series") or vc("movementname"),
            "part": vc("series-part") or vc("movement"),
        }
    return {k: v for k, v in out.items() if v}


NAME_PATTERNS = [
    # Series - 01 - Title  (e.g. "Destroyermen - 01 - Into the Storm")
    re.compile(r"^(?P<series>[^-]+?)\s+-\s+(?P<seq>\d+(?:\.\d+)?)\s+-\s+(?P<title>.+)$"),
    # Author - Series 01 - Title   /  Author - Series Book 1 - Title
    re.compile(r"^(?P<author>[^-]+?)\s+-\s+(?P<series>.+?)\s+(?:Book\s*|#|Vol\.?\s*)?(?P<seq>\d+(?:\.\d+)?)\s+-\s+(?P<title>.+)$", re.I),
    re.compile(r"^(?P<author>[^-]+?)\s+-\s+(?P<title>.+)$"),
]


def parse_name(name: str) -> dict[str, str]:
    for pat in NAME_PATTERNS:
        m = pat.match(name.strip())
        if m:
            return {k: v.strip() for k, v in m.groupdict().items() if v}
    return {"title": name.strip()}


def first_author(s: str) -> str:
    return re.split(r"\s*(?:[,;/&]|\band\b|\bfeat\.?)\s*", s, maxsplit=1)[0].strip()


def book_meta(book: Book) -> Meta:
    tags = read_tags(book.audio[0])
    parsed = parse_name(book.name)
    folder = parse_name(book.root.parent.name if book.root.is_file() else book.root.name)
    single = len(book.audio) == 1
    album, title_tag = tags.get("album", ""), tags.get("title", "")
    m = Meta()
    # The artist tag is sometimes the narrator, so the filename/folder author is kept as a backup.
    authors = [first_author(a) for a in (tags.get("albumartist"), tags.get("artist"),
                                         parsed.get("author"), folder.get("author")) if a]
    m.author_candidates = list(dict.fromkeys(a for a in authors if a))
    m.author = m.author_candidates[0] if m.author_candidates else ""
    if book.album_shared:
        # e.g. ten books all tagged album="Destroyermen": the album is really the series
        m.title = title_tag or parsed.get("title", "")
        m.series = tags.get("series") or album
    else:
        # For multi-file books the per-file title is a chapter name, so only trust 'album' there.
        m.title = album or (title_tag if single else "") or parsed.get("title", "")
        m.series = tags.get("series") or parsed.get("series", "")
    m.series = clean_series(m.series)
    m.sequence = tags.get("part") or parsed.get("seq", "")
    m.narrator = tags.get("composer", "")
    if not tags:
        m.source = "filename"
    # "Book 3" style info in the title, e.g. "Mistborn, Book 1" / "Title (Series #2)"
    if not m.sequence:
        mm = re.search(r"(?:Book|#|Vol\.?)\s*(\d+(?:\.\d+)?)", f"{m.title} {album} {title_tag}", re.I)
        if mm:
            m.sequence = mm.group(1)
    m.sequence = clean_sequence(m.sequence)
    lead = re.match(r"^\s*(\d{1,3})[\s_.-]+\D", m.title)
    m.seq_hint = m.sequence or (clean_sequence(lead.group(1)) if lead else "")

    candidates = [m.title, album, title_tag if (single or book.album_shared) else "", parsed.get("title", "")]
    m.title_variants = title_variants(candidates)
    return m


def strip_leading_number(s: str) -> str:
    """'1 Double-Blind' -> 'Double-Blind', '14 Pass of Fire' -> 'Pass of Fire' (keeps '1984')."""
    return re.sub(r"^\s*\d{1,3}(?:\.\d+)?[\s_.-]+(?=\D)", "", s).strip()


def title_variants(candidates: list[str]) -> list[str]:
    """Search terms to try in order, e.g. 'Anderson, T: 14 Pass of Fire' -> ..., 'Pass of Fire'."""
    out: list[str] = []
    for c in candidates:
        base = clean_title(c or "")
        if not base:
            continue
        forms = [base, strip_leading_number(base)]
        if ":" in base:
            tail = clean_title(base.rsplit(":", 1)[1])
            forms += [tail, strip_leading_number(tail)]
        for f in forms:
            if len(f) >= 3 and f.lower() not in (o.lower() for o in out):
                out.append(f)
    return out


def clean_series(s: str) -> str:
    """'Double-Blind,' -> 'Double-Blind';  'Destroyermen, Book 12' -> 'Destroyermen'."""
    s = re.sub(r"[,;:\s-]*(?:\(?\s*(?:Book|#|Vol\.?)\s*\d+(?:\.\d+)?\s*\)?)\s*$", "", s or "", flags=re.I)
    return s.strip(" ,;:-_")


def clean_sequence(s: str) -> str:
    s = (s or "").split("/")[0].strip()       # "1/3" -> "1"
    mm = re.search(r"\d+(?:\.\d+)?", s)
    if not mm:
        return ""
    whole, _, frac = mm.group(0).partition(".")
    whole = whole.lstrip("0") or "0"
    return f"{whole}.{frac}" if frac else whole


JUNK = re.compile(
    r"\((?:un)?abridged\)|\[(?:un)?abridged\]|\[[^\]]*\]|\((?:mp3|m4b|aac|audiobook)[^)]*\)"
    r"|\b(?:un)?abridged\b|\baudiobook\b|:\s*a novel\b",
    re.I,
)


def clean_title(s: str) -> str:
    s = JUNK.sub(" ", s)
    s = re.sub(r"^\s*(?:Book|Vol\.?)\s*\d+(?:\.\d+)?\s*[-:.]\s*", "", s, flags=re.I)  # "Book 1 - X"
    s = re.sub(r"^\s*\d+(?:\.\d+)?\s*[-.]\s+", "", s)                                 # "01 - X"
    s = re.sub(r"[,(]\s*(?:Book|#|Vol\.?)\s*\d+(?:\.\d+)?\s*\)?\s*$", "", s, flags=re.I)  # "X, Book 1"
    return re.sub(r"\s+", " ", s).strip(" -_:")


# --------------------------------------------------------------------------- Audible

class NetworkError(Exception):
    pass


@dataclass
class Verdict:
    ok: bool
    meta: Meta | None = None
    reason: str = ""
    final: bool = False  # don't bother trying other title variants
    candidate: Meta | None = None  # closest Audible match when it failed (offered on the review page)


# Title, authors, narrators, series, description, publisher, date, language, genres and cover.
AUDIBLE_GROUPS = ("contributors,series,product_desc,product_attrs,product_extended_attrs,media,"
                  "category_ladders")


def audible_search(cfg: Config, params: dict) -> list[dict]:
    url = f"https://api.audible.{cfg.audible_region}/1.0/catalog/products"
    params = {"num_results": 10, "products_sort_by": "Relevance",
              "response_groups": AUDIBLE_GROUPS, "image_sizes": "500,1024", **params}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json().get("products", [])
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                raise NetworkError(f"Audible lookup failed: {e}") from e
            time.sleep(2 ** attempt * 2)
    return []


def verify_with_audible(cfg: Config, tag: Meta) -> Verdict:
    if not tag.title:
        return Verdict(False, reason="no title in tags or filename")
    if not tag.author:
        return Verdict(False, reason="no author in tags or filename")

    variants = tag.title_variants or [clean_title(tag.title)]
    authors = tag.author_candidates or [tag.author]
    reason = "no Audible results"
    candidate = None
    for author in authors[:3]:
        for i, title_q in enumerate(variants[:4]):
            products = audible_search(cfg, {"title": title_q, "author": author})
            if not products and i == 0:
                products = audible_search(cfg, {"keywords": f"{title_q} {author}"})
            if not products:
                continue
            verdict = judge(cfg, tag, author, title_q, products)
            if verdict.ok or verdict.final:
                return verdict
            reason = verdict.reason
            candidate = verdict.candidate or candidate
    return Verdict(False, reason=reason, candidate=candidate)


def dedupe_name(name: str) -> str:
    """Audible sometimes lists 'J. McCoy J. McCoy'."""
    words = name.split()
    half = len(words) // 2
    if len(words) % 2 == 0 and half and words[:half] == words[half:]:
        return " ".join(words[:half])
    return name.strip()


def judge(cfg: Config, tag: Meta, author: str, title_q: str, products: list[dict]) -> Verdict:
    th = cfg.match_threshold
    best = None
    for p in products:
        a_title = p.get("title") or ""
        t_score = max(_tsr(title_q.lower(), clean_title(a_title).lower()),
                      _tsr(title_q.lower(), f"{a_title} {p.get('subtitle') or ''}".lower()))
        names = [dedupe_name(a.get("name", "")) for a in p.get("authors") or []]
        scored = [(_tsr(author.lower(), n.lower()), n) for n in names] or [(0, "")]
        a_score, a_name = max(scored)
        # A short title like "Double-Blind" fits every book in the series, so among the good
        # matches the book number we know (tags/filename) decides, then the closest title.
        seqs = {clean_sequence(s.get("sequence", "")) for s in p.get("series") or []} - {""}
        seq_match = 1 if tag.seq_hint and tag.seq_hint in seqs else 0
        passes = t_score >= th and a_score >= th
        rank = (passes, seq_match, t_score + a_score, _ratio(title_q.lower(), clean_title(a_title).lower()))
        if best is None or rank > best[0]:
            best = (rank, t_score, a_score, a_name, p, seqs)

    _, t_score, a_score, a_name, p, seqs = best
    closest = candidate_meta(p, a_name, tag)
    cand = f"best Audible match: {p.get('title')!r} by {a_name!r} (title {t_score:.0f}, author {a_score:.0f}, asin {p.get('asin')})"
    if t_score < th or a_score < th:
        return Verdict(False, reason=f"low match score for {title_q!r}; {cand}", candidate=closest)
    if tag.seq_hint and seqs and tag.seq_hint not in seqs:
        return Verdict(False, reason=f"book number mismatch: expected #{tag.seq_hint}, Audible has "
                                     f"#{', #'.join(sorted(seqs))}; {cand}", candidate=closest)

    series_list = [(s.get("title", ""), s.get("sequence", "")) for s in p.get("series") or []]
    series, seq = "", ""
    if tag.series:
        if series_list:
            score, (series, seq) = max(
                (_tsr(clean_title(tag.series).lower(), s[0].lower()), s) for s in series_list)
            if score < th:
                return Verdict(False, final=True,
                               reason=f"series conflict: tags say {tag.series!r}, Audible says "
                                      f"{[s[0] for s in series_list]}; {cand}", candidate=closest)
        else:
            series = tag.series  # Audible has no series info; keep what the tags say
        seq = clean_sequence(seq) or tag.sequence
    elif series_list:
        # Prefer a series that has a position (e.g. "Stormlight Archive #1" over "The Cosmere")
        with_seq = [s for s in series_list if s[1]]
        series, seq = (with_seq or series_list)[0]
        seq = clean_sequence(seq)

    meta = Meta(author=a_name, title=clean_title(p.get("title") or tag.title) or tag.title,
                series=series, sequence=seq, narrator=tag.narrator, source="audible", asin=p.get("asin", ""),
                product=p)
    return Verdict(True, meta=meta, reason=cand)


def candidate_meta(p: dict, author: str = "", tag: Meta | None = None) -> Meta:
    """How an Audible product would be filed if you accept it on the review page."""
    series_list = [(s.get("title", ""), s.get("sequence", "")) for s in p.get("series") or []]
    with_seq = [s for s in series_list if s[1]]
    series, seq = (with_seq or series_list or [("", "")])[0]
    if not author:
        names = [dedupe_name(a.get("name", "")) for a in p.get("authors") or []]
        author = next((n for n in names if n), "")
    title = p.get("title") or ""
    return Meta(author=author, title=clean_title(title) or title, series=series, sequence=clean_sequence(seq),
                narrator=tag.narrator if tag else "", source="audible", asin=p.get("asin", ""), product=p)


ASIN_RE = re.compile(r"\basin ([A-Z0-9]{10})\b")


def audible_product(cfg: Config, asin: str) -> dict | None:
    """Look up one Audible product by its ASIN."""
    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
        return None
    url = f"https://api.audible.{cfg.audible_region}/1.0/catalog/products/{asin}"
    try:
        r = requests.get(url, params={"response_groups": AUDIBLE_GROUPS, "image_sizes": "500,1024"},
                         timeout=20)
        r.raise_for_status()
        return r.json().get("product") or None
    except (requests.RequestException, ValueError) as e:
        raise NetworkError(f"Audible lookup failed: {e}") from e


# --------------------------------------------------------------------------- moving

def safe_part(s: str, limit: int = 120) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s.replace(":", " -"))
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:limit].rstrip(". ") or "Unknown"


FILLER_WORD = re.compile(r"^(?:[ivxlc]+|\d+(?:\.\d+)?|book|vol|volume|part|the|a|an)$", re.I)


def strip_series_prefix(title: str, series: str) -> str:
    """'Monarch III: Whitefall' in series 'RE: Monarch' -> 'Whitefall'."""
    if not series or ":" not in title:
        return title
    head, tail = title.split(":", 1)
    head_words = {w for w in re.findall(r"[\w'’-]+", head.lower()) if not FILLER_WORD.match(w)}
    series_words = set(re.findall(r"[\w'’-]+", series.lower()))
    tail = tail.strip(" -:")
    if head_words and head_words <= series_words and tail:
        return tail
    return title


def format_sequence(seq: str) -> str:
    """'1' -> '01', '1.5' -> '01.5' (matches the library's existing '01 - Title' folders)."""
    whole, _, frac = seq.partition(".")
    return whole.zfill(2) + (f".{frac}" if frac else "")


def destination(cfg: Config, m: Meta) -> Path:
    parts = [safe_part(m.author)]
    title = m.title
    if m.series:
        parts.append(safe_part(m.series))
        title = strip_series_prefix(title, m.series)
        if m.sequence:
            title = f"{format_sequence(m.sequence)} - {title}"
    folder = safe_part(title)
    if cfg.metadata.asin_in_folder and re.fullmatch(r"[A-Z0-9]{10}", m.asin or ""):
        folder += f" [{m.asin}]"
    parts.append(folder)
    return cfg.library_dir.joinpath(*parts)


# --------------------------------------------------------------------------- metadata for Audiobookshelf

COVER_NAMES = {"cover", "folder"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_COVER_BYTES = 15_000_000


def html_to_text(s: str) -> str:
    """Audible's HTML description -> plain paragraphs."""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", s or "")
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = [" ".join(line.split()) for line in s.splitlines()]
    return "\n".join(line for line in lines if line)


def genres(p: dict) -> list[str]:
    out = []
    for ladder in p.get("category_ladders") or []:
        for step in ladder.get("ladder") or []:
            name = (step.get("name") or "").strip()
            if name and name not in out:
                out.append(name)
    return out[:10]


def opf_xml(m: Meta, p: dict | None) -> str:
    """An OPF file in the layout Audiobookshelf's docs show (Calibre style)."""
    p = p or {}
    authors = [m.author] + [n for a in p.get("authors") or []
                            if (n := dedupe_name(a.get("name", ""))) and n != m.author]
    narrators = [n for x in p.get("narrators") or [] if (n := (x.get("name") or "").strip())]
    if not narrators and m.narrator:
        narrators = [m.narrator]
    rows = [f"<dc:title>{escape(m.title)}</dc:title>"]
    if p.get("subtitle"):
        rows.append(f"<dc:subtitle>{escape(p['subtitle'])}</dc:subtitle>")
    rows += [f'<dc:creator opf:role="aut">{escape(a)}</dc:creator>' for a in authors if a]
    rows += [f'<dc:creator opf:role="nrt">{escape(n)}</dc:creator>' for n in narrators]
    if p.get("publisher_name"):
        rows.append(f"<dc:publisher>{escape(p['publisher_name'])}</dc:publisher>")
    if date := (p.get("release_date") or p.get("issue_date") or ""):
        rows.append(f"<dc:date>{escape(date)}</dc:date>")
    if p.get("language"):
        rows.append(f"<dc:language>{escape(p['language'])}</dc:language>")
    if desc := html_to_text(p.get("publisher_summary") or p.get("merchandising_summary") or ""):
        rows.append(f"<dc:description>{escape(desc)}</dc:description>")
    rows += [f"<dc:subject>{escape(g)}</dc:subject>" for g in genres(p)]
    if asin := (m.asin or p.get("asin") or ""):
        rows.append(f'<dc:identifier opf:scheme="ASIN">{escape(asin)}</dc:identifier>')
    if m.series:
        rows.append(f'<meta name="calibre:series" content={quoteattr(m.series)}/>')
        if m.sequence:
            rows.append(f'<meta name="calibre:series_index" content={quoteattr(m.sequence)}/>')
    body = "\n    ".join(rows)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n'
            f"    {body}\n  </metadata>\n</package>\n")


def has_cover(folder: Path) -> bool:
    return any(f.is_file() and f.stem.lower() in COVER_NAMES and f.suffix.lower() in IMAGE_EXTS
               for f in folder.iterdir())


def download_cover(p: dict, target: Path) -> bool:
    images = p.get("product_images") or {}
    url = images.get("1024") or images.get("500") or next(iter(images.values()), "")
    if not url.startswith("https://"):
        return False
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.content
    except requests.RequestException as e:
        log.warning("Couldn't download the cover from Audible: %s", e)
        return False
    if len(data) > MAX_COVER_BYTES or not data.startswith(b"\xff\xd8"):  # must be a JPEG
        log.warning("Audible's cover for %s isn't a usable JPEG; skipped", p.get("asin"))
        return False
    tmp = target.with_name(target.name + ".sorting")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return True


def write_book_metadata(cfg: Config, dest: Path, m: Meta) -> None:
    """Put metadata.opf (and Audible's cover) in the book folder, so Audiobookshelf shows the Audible
    details instead of whatever the download's tags say. The audio files themselves are never changed:
    for a torrent they're hard links to the files being seeded."""
    md = cfg.metadata
    if cfg.dry_run or not (md.write_opf or md.download_cover) or not dest.is_dir():
        return
    p = m.product
    if p is None and m.asin:
        try:
            p = audible_product(cfg, m.asin)
        except NetworkError as e:
            log.warning("No Audible details for %s (%s); writing what's known", dest.name, e)
    try:
        if md.write_opf:
            tmp = dest / "metadata.opf.sorting"
            tmp.write_text(opf_xml(m, p), encoding="utf-8")
            os.replace(tmp, dest / "metadata.opf")
        if md.download_cover and p and not has_cover(dest):
            download_cover(p, dest / "cover.jpg")
    except OSError as e:
        log.warning("Couldn't write metadata for %s: %s", dest, e)


def backfill_metadata(cfg: Config) -> None:
    """--write-metadata: add metadata.opf / cover.jpg to books this sorter filed before it wrote them."""
    if _history is None:
        sys.exit("--write-metadata needs the history database (a [web] or [qbittorrent] section).")
    done = skipped = 0
    for r in _history.sorted_books():
        dest = Path(r["dest"])
        if not dest.is_dir() or (dest / "metadata.opf").exists():
            skipped += 1
            continue
        match = ASIN_RE.search(r.get("reason") or "")
        m = Meta(author=r["author"], title=r["title"], series=r["series"], sequence=r["sequence"],
                 asin=match.group(1) if match else "")
        write_book_metadata(cfg, dest, m)
        log.info("METADATA %s", dest)
        done += 1
    log.info("Wrote metadata for %d book(s); %d skipped (already done or folder gone)", done, skipped)


def label(cfg: Config, book: Book) -> str:
    """How a book is named in the log, e.g. 'Destroyermen 01 - 10/Destroyermen - 01 - Into the Storm.mp3'."""
    if book.label:
        return book.label
    try:
        return book.root.relative_to(cfg.source_dir).as_posix()
    except ValueError:
        return book.root.name


_dry_gone: set[Path] = set()      # dry run: files that would have been moved out
_dry_cleaned: set[Path] = set()   # dry run: leftover folders already reported


def top_level_folder(cfg: Config, path: Path) -> Path | None:
    """The folder directly inside source_dir that contains `path` (never source_dir or review_dir)."""
    try:
        rel = path.relative_to(cfg.source_dir)
    except ValueError:
        return None
    if not rel.parts:
        return None
    top = cfg.source_dir / rel.parts[0]
    if not top.is_dir():
        return None
    for special in special_dirs(cfg):
        if top.resolve() == special or top.resolve() in special.parents:
            return None
    return top


def cleanup_leftovers(cfg: Config, path: Path) -> None:
    """Delete the source folder that held `path` once it has no audiobook files left.

    Covers, .txt, .nfo and other extras that no book claimed are deleted with it. Nothing is
    deleted while any audio file or part-downloaded file remains anywhere inside the folder.
    """
    top = top_level_folder(cfg, path)
    if top is None or top in _dry_cleaned:
        return
    try:
        files = [p for p in top.rglob("*") if p.is_file() and p not in _dry_gone]
    except OSError:
        return
    if any(p.suffix.lower() in cfg.audio_extensions or p.suffix.lower() in PARTIAL_SUFFIXES for p in files):
        return
    names = [p.relative_to(top).as_posix() for p in files]
    shown = ", ".join(names[:8]) + (f", +{len(names) - 8} more" if len(names) > 8 else "") or "empty"
    if cfg.dry_run:
        _dry_cleaned.add(top)
        log.info("DRY-RUN would delete leftover folder %s (%s)", top.name, shown)
        _report.append(Decision("cleanup", top.name, reason=shown, files=len(names)))
        return

    def force(func, p, _exc):  # clear read-only flags (common on torrent extras) and retry
        os.chmod(p, 0o666)
        func(p)

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(top, onexc=force)
        else:
            shutil.rmtree(top, onerror=force)
        log.info("CLEANED %s (deleted leftovers: %s)", top.name, shown)
    except OSError as e:
        log.warning("Could not delete leftover folder %s: %s", top, e)


LEFTOVER_MIN_AGE = 600  # a folder with only a cover in it might be a download just starting


def leftover_folders(cfg: Config) -> list[Path]:
    """Folders in source_dir with no audio that nobody has touched for a while (e.g. from an older run).

    Folders this script empties itself are cleaned straight away; this is only for the others.
    """
    now = time.time()
    min_age = max(cfg.settle_seconds, LEFTOVER_MIN_AGE)
    out = []
    for entry in sorted(cfg.source_dir.iterdir(), key=natural_key):
        if entry.name.startswith(".") or top_level_folder(cfg, entry) is None:
            continue
        try:
            items = list(entry.rglob("*"))
            if any(p.is_file() and (p.suffix.lower() in cfg.audio_extensions
                                    or p.suffix.lower() in PARTIAL_SUFFIXES) for p in items):
                continue
            newest = max([entry.stat().st_mtime] + [p.stat().st_mtime for p in items])
        except OSError:
            continue
        if now - newest >= min_age:
            out.append(entry)
    return out


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}" if p.suffix else f"{p.name} ({i})")
        if not cand.exists():
            return cand
        i += 1


def _same_device(a: Path, b: Path) -> bool:
    """True when a rename from a to b will work (same disk/volume)."""
    while not b.exists() and b.parent != b:
        b = b.parent
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


_link_warned = False


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hard-link (no extra space; the torrent keeps seeding), or copy if the folders are on different volumes."""
    global _link_warned
    try:
        os.link(src, dst)
    except OSError as e:
        if not _link_warned:
            log.warning("Can't hard-link %s (%s); copying instead. Put downloads and the library on the "
                        "same volume, inside one container mount, to save space.", src, e)
            _link_warned = True
        shutil.copy2(src, dst)


def file_plan(book: Book, dest: Path) -> list[tuple[Path, Path]]:
    out = []
    for src in book.files:
        rel = src.name if book.root.is_file() else src.relative_to(book.root)
        out.append((src, dest / rel))
    return out


def already_placed(book: Book, dest: Path) -> bool:
    """A torrent re-checked after a restart: its files are already linked/copied to dest."""
    try:
        return all(dst.is_file() and (os.path.samefile(src, dst) or src.stat().st_size == dst.stat().st_size)
                   for src, dst in file_plan(book, dest))
    except OSError:
        return False


def place_book(cfg: Config, book: Book, dest: Path, mode: str = "move") -> list[tuple[Path, Path]]:
    """Put the book's files in dest.

    mode "move": rename when on the same volume, else copy + size check + delete the source.
    mode "link": hard-link (or copy) and leave the source alone, so a torrent can keep seeding.
    """
    plan = file_plan(book, dest)
    rename = mode == "move" and _same_device(book.files[0], dest)
    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rename:
            os.replace(src, dst)
            continue
        tmp = dst.with_name(dst.name + ".sorting")
        tmp.unlink(missing_ok=True)
        if mode == "link":
            _link_or_copy(src, tmp)
        else:
            shutil.copy2(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            tmp.unlink(missing_ok=True)
            raise OSError(f"size mismatch after copying {src}")
        os.replace(tmp, dst)
    if mode == "move":
        if not rename:
            for src, _ in plan:
                src.unlink()
        if book.root.is_dir():
            for d in sorted((p for p in book.root.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            try:
                book.root.rmdir()
            except OSError:
                pass
    return plan


def move_aside(cfg: Config, book: Book, base: Path) -> Path:
    """Move a book out of the way into base/ (review or quarantine), keeping its files together."""
    base.mkdir(parents=True, exist_ok=True)
    if book.root.is_dir() or book.root.parent == cfg.source_dir:
        target = unique_path(base / book.root.name)
        shutil.move(str(book.root), str(target))
    else:  # one book out of a shared folder: keep its files (and extras) together
        target = unique_path(base / book.root.stem)
        target.mkdir(parents=True)
        for p in book.files:
            shutil.move(str(p), str(target / p.name))
    return target


def remember(kind: str, name: str, **fields) -> None:
    """Add an event to the history shown in the web UI (no-op without a database or in a dry run)."""
    if _history is not None:
        try:
            _history.record(kind, name, **fields)
        except Exception as e:  # the UI's database must never stop the sorting
            log.warning("Could not write history: %s", e)


_history: History | None = None


def send_to_review(cfg: Config, book: Book, reason: str, tag: Meta | None,
                   mode: str = "move", torrent_hash: str = "", candidate: Meta | None = None) -> str:
    log.warning("REVIEW %s (%s)", label(cfg, book), reason)
    if cfg.dry_run:
        _report.append(Decision("review", label(cfg, book), reason=reason))
        _dry_gone.update(book.files)
        cleanup_leftovers(cfg, book.root)
        return "review"
    size = book_size(book)
    if mode == "link":  # torrent still seeding: link into review, leave the download alone
        target = cfg.review_dir / safe_part(book.name, 150)
        if target.exists() and not already_placed(book, target):
            target = unique_path(target)
        pairs = place_book(cfg, book, target, "link")
        if _history is not None:
            _history.add_placements(torrent_hash, pairs)
    else:
        target = move_aside(cfg, book, cfg.review_dir)
    with open(cfg.review_dir / "review.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{target.name}\t{reason}\ttags: {tag}\n")
    remember("review", label(cfg, book), torrent_hash=torrent_hash, dest=str(target), reason=reason,
             tags=str(tag or ""), author=tag.author if tag else "", series=tag.series if tag else "",
             sequence=tag.sequence if tag else "", title=tag.title if tag else "",
             files=len(book.files), size=size, candidate=meta_json(candidate))
    if mode == "move":
        cleanup_leftovers(cfg, book.root)
    return "review"


def meta_json(m: Meta | None) -> str:
    if m is None or not (m.author and m.title):
        return ""
    return json.dumps({"author": m.author, "series": m.series, "sequence": m.sequence, "title": m.title,
                       "asin": m.asin})


def quarantine(cfg: Config, book: Book, reason: str, torrent_hash: str = "") -> None:
    (log.warning if cfg.dry_run else log.error)("QUARANTINE %s (%s)", label(cfg, book), reason)
    if cfg.dry_run:
        _report.append(Decision("quarantine", label(cfg, book), reason=reason))
        _dry_gone.update(book.files)
        return
    size = book_size(book)
    target = move_aside(cfg, book, cfg.quarantine_dir)
    with open(cfg.quarantine_dir / "quarantine.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{target.name}\t{reason}\n")
    remember("quarantined", label(cfg, book), torrent_hash=torrent_hash, dest=str(target), reason=reason,
             files=len(book.files), size=size)
    cleanup_leftovers(cfg, book.root)


def security_check(cfg: Config, files: list[Path], scan_paths: list[Path]) -> str | None:
    """Return why these files must be quarantined, or None if they're safe.

    Raises RetryLater when ClamAV is configured but can't scan right now: nothing unscanned moves.
    """
    allowed = tuple(cfg.audio_extensions) + tuple(cfg.scan.allowed_extensions)
    bad = scanner.check_file_types(files, allowed)
    if bad:
        return "blocked files: " + "; ".join(bad[:10]) + (f"; +{len(bad) - 10} more" if len(bad) > 10 else "")
    if not cfg.scan.clamd_host:
        return None
    infected = []
    try:
        for p in scan_paths:
            infected += scanner.clamd_scan(cfg.scan.clamd_host, cfg.scan.clamd_port, p.as_posix())
    except scanner.ScannerDown as e:
        raise RetryLater(f"virus scanner unavailable: {e}", SCANNER_RETRY_SECONDS)
    if infected:
        return "virus found: " + "; ".join(infected[:10])
    return None


# --------------------------------------------------------------------------- processing

class RetryLater(Exception):
    def __init__(self, message: str, seconds: int = RETRY_SECONDS):
        super().__init__(message)
        self.seconds = seconds


def process(cfg: Config, book: Book, mode: str = "move", torrent_hash: str = "") -> str:
    """Sort one book. Returns 'sorted' or 'review'; raises RetryLater to try again later."""
    tag = book_meta(book)
    log.info("Found %s  [%s: %s]", label(cfg, book), tag.source, tag)
    try:
        verdict = verify_with_audible(cfg, tag)
    except NetworkError as e:
        raise RetryLater(str(e))

    if not verdict.ok:
        return send_to_review(cfg, book, verdict.reason, tag, mode, torrent_hash, verdict.candidate)

    m = verdict.meta
    dest = destination(cfg, m)
    if not cfg.library_dir.exists():
        raise RetryLater(f"library_dir not reachable: {cfg.library_dir}")
    if mode == "link" and already_placed(book, dest):
        log.info("Already in the library: %s -> %s", label(cfg, book), dest)
        if not (dest / "metadata.opf").exists():
            write_book_metadata(cfg, dest, m)
        return "sorted"
    if dest.exists() and any(p.suffix.lower() in cfg.audio_extensions for p in dest.rglob("*")):
        return send_to_review(cfg, book, f"duplicate: {dest} already has audio files", tag, mode, torrent_hash)

    if cfg.dry_run:
        if dest in _dry_run_dests:
            return send_to_review(cfg, book, f"duplicate: another book in this run also maps to {dest}", tag)
        _dry_run_dests.add(dest)
        log.info("DRY-RUN would %s %s -> %s", "link" if mode == "link" else "move", label(cfg, book), dest)
        _report.append(Decision("move", label(cfg, book), dest=dest,
                                files=len(book.files), size=book_size(book)))
        _dry_gone.update(book.files)
        cleanup_leftovers(cfg, book.root)
        return "sorted"
    size = book_size(book)
    try:
        pairs = place_book(cfg, book, dest, mode)
    except OSError as e:
        raise RetryLater(f"{mode} failed: {e}")
    if _history is not None and mode == "link":
        _history.add_placements(torrent_hash, pairs)
    write_book_metadata(cfg, dest, m)
    log.info("%s %s -> %s", "LINKED" if mode == "link" else "MOVED", label(cfg, book), dest)
    remember("sorted", label(cfg, book), torrent_hash=torrent_hash, author=m.author, series=m.series,
             sequence=m.sequence, title=m.title, dest=str(dest), reason=verdict.reason, tags=str(tag),
             files=len(book.files), size=size)
    if mode == "move":
        cleanup_leftovers(cfg, book.root)
    return "sorted"


_dry_run_dests: set[Path] = set()

# The sorting loop and the web page's buttons both move files; only one of them works at a time.
WORK_LOCK = threading.Lock()


# --------------------------------------------------------------------------- review page decisions

class ActionError(Exception):
    """A decision on the review page couldn't be carried out; the message is shown to you."""


def review_book(cfg: Config, path: Path) -> Book:
    """The book sitting in a review folder (or a single file there)."""
    if path.is_file():
        return Book(root=path, audio=[path], files=[path])
    files = sorted((p for p in path.rglob("*") if p.is_file()), key=natural_key)
    return Book(root=path, audio=[p for p in files if p.suffix.lower() in cfg.audio_extensions], files=files)


class ReviewActions:
    """What the buttons on the review page do. Called from the web UI thread."""

    MAX_FIELD = 200

    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ---------------------------------------------------------------- shown on the page
    def candidate(self, r: dict) -> Meta | None:
        try:
            c = json.loads(r.get("candidate") or "null")
        except ValueError:
            return None
        if not c:
            return None
        return Meta(author=c.get("author", ""), title=c.get("title", ""), series=c.get("series", ""),
                    sequence=c.get("sequence", ""), asin=c.get("asin", ""), source="audible")

    def can_accept(self, r: dict) -> bool:
        return self.candidate(r) is not None or bool(ASIN_RE.search(r.get("reason") or ""))

    def preview(self, m: Meta) -> str:
        """'Author/Series/03 - Title' as it would appear in the library."""
        return destination(self.cfg, m).relative_to(self.cfg.library_dir).as_posix()

    def is_torrent_copy(self, r: dict) -> bool:
        return bool(r.get("torrent_hash"))

    # ---------------------------------------------------------------- the buttons
    def accept(self, event_id: int) -> str:
        r, path = self._item(event_id)
        m = self.candidate(r)
        if m is None:
            match = ASIN_RE.search(r.get("reason") or "")
            if not match:
                raise ActionError("There's no Audible match to accept for this book; use Edit and sort.")
            try:
                p = audible_product(self.cfg, match.group(1))
            except NetworkError as e:
                raise ActionError(f"Couldn't reach Audible ({e}). Try again in a minute.")
            m = candidate_meta(p) if p else None
            if not m or not (m.author and m.title):
                raise ActionError("Audible no longer lists that book; use Edit and sort.")
        return self._sort(r, path, m, f"accepted on the review page: Audible match {m.title!r} by {m.author!r}"
                                      + (f" (asin {m.asin})" if m.asin else ""))

    def sort_as(self, event_id: int, author: str, series: str, sequence: str, title: str) -> str:
        author, series, sequence, title = (" ".join((v or "").split()) for v in (author, series, sequence, title))
        if not author or not title:
            raise ActionError("Author and title are both needed.")
        if any(len(v) > self.MAX_FIELD for v in (author, series, title)):
            raise ActionError(f"Keep each field under {self.MAX_FIELD} characters.")
        if sequence and not re.fullmatch(r"\d{1,4}(?:\.\d{1,2})?", sequence):
            raise ActionError("Book number must be a number, like 3 or 2.5.")
        if sequence and not series:
            raise ActionError("A book number needs a series name.")
        r, path = self._item(event_id)
        m = Meta(author=author, series=series, sequence=sequence, title=title, source="you")
        return self._sort(r, path, m, "filed on the review page with the details you entered")

    def reject(self, event_id: int) -> str:
        r, path = self._item(event_id)
        self._acquire()
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as e:
            raise ActionError(f"Deleting the files failed: {e}")
        finally:
            WORK_LOCK.release()
        log.info("REJECTED %s on the review page; deleted %s", r["name"], path)
        remember("rejected", r["name"], torrent_hash=r.get("torrent_hash", ""), dest=str(path),
                 reason="rejected on the review page", files=r.get("files", 0), size=r.get("size", 0))
        if r.get("torrent_hash"):
            return (f"Rejected {r['name']}. The review copy is deleted; the torrent seeds until its limit "
                    f"and is then removed with its download.")
        return f"Rejected {r['name']} and deleted its files."

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _acquire() -> None:
        if not WORK_LOCK.acquire(timeout=120):
            raise ActionError("The sorter is busy (scanning or sorting). Try again in a minute.")

    def _item(self, event_id: int) -> tuple[dict, Path]:
        if self.cfg.dry_run or _history is None:
            raise ActionError("Decisions are off during a dry run.")
        r = _history.event(event_id)
        if not r or r["kind"] != "review":
            raise ActionError("That review item doesn't exist.")
        path = Path(r["dest"])
        try:
            path.resolve().relative_to(self.cfg.review_dir.resolve())
        except ValueError:
            raise ActionError("That item isn't in the review folder.")
        if path.resolve() == self.cfg.review_dir.resolve() or not path.exists():
            raise ActionError("Its files are no longer in the review folder (already handled?).")
        return r, path

    def _sort(self, r: dict, path: Path, m: Meta, reason: str) -> str:
        cfg = self.cfg
        self._acquire()
        try:
            if not path.exists():
                raise ActionError("Its files are no longer in the review folder (already handled?).")
            book = review_book(cfg, path)
            if not book.audio:
                raise ActionError("There are no audio files left in it.")
            dest = destination(cfg, m)
            if not cfg.library_dir.exists():
                raise ActionError(f"The library folder isn't reachable: {cfg.library_dir}")
            if dest.exists() and any(p.suffix.lower() in cfg.audio_extensions for p in dest.rglob("*")):
                raise ActionError(f"{dest.relative_to(cfg.library_dir).as_posix()} already has audio files. "
                                  "Pick a different title or book number, or reject this copy.")
            size = book_size(book)
            try:
                pairs = place_book(cfg, book, dest, "move")
            except OSError as e:
                raise ActionError(f"Moving the files failed: {e}")
            if r.get("torrent_hash") and _history is not None:
                _history.move_placements(r["torrent_hash"], pairs)
        finally:
            WORK_LOCK.release()
        write_book_metadata(cfg, dest, m)
        log.info("SORTED from review %s -> %s (%s)", r["name"], dest, reason)
        remember("sorted", r["name"], torrent_hash=r.get("torrent_hash", ""), author=m.author, series=m.series,
                 sequence=m.sequence, title=m.title, dest=str(dest), reason=reason, tags=r.get("tags", ""),
                 files=len(book.files), size=size)
        return f"Filed {r['name']} as {dest.relative_to(cfg.library_dir).as_posix()}"


# --------------------------------------------------------------------------- dry-run report

@dataclass
class Decision:
    kind: str                 # "move" | "review" | "error" | "cleanup" | "quarantine" | "remove"
    label: str                # source, relative to source_dir
    dest: Path | None = None
    reason: str = ""
    files: int = 0
    size: int = 0


_report: list[Decision] = []


def book_size(book: Book) -> int:
    total = 0
    for p in book.files:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t): return self("1", t)
    def dim(self, t): return self("2", t)
    def green(self, t): return self("32", t)
    def yellow(self, t): return self("33", t)
    def red(self, t): return self("31", t)
    def cyan(self, t): return self("36", t)


def use_color() -> bool:
    if sys.stdout is None or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if os.name == "nt":
        os.system("")  # turns on ANSI colour support in the Windows console
    return True


def print_report(cfg: Config) -> None:
    s = Style(use_color())
    moves = [d for d in _report if d.kind == "move"]
    reviews = [d for d in _report if d.kind == "review"]
    errors = [d for d in _report if d.kind == "error"]
    cleanups = [d for d in _report if d.kind == "cleanup"]
    quarantines = [d for d in _report if d.kind == "quarantine"]
    removals = [d for d in _report if d.kind == "remove"]
    out = []
    w = out.append

    w("")
    w(s.bold("DRY RUN") + s.dim(" - nothing was moved"))
    w(f"  {s.dim('From')}  {cfg.source_dir}")
    if cfg.qbit:
        w(f"  {s.dim('    ')}  qBittorrent {cfg.qbit.url}, category {cfg.qbit.category!r}")
    w(f"  {s.dim('To  ')}  {cfg.library_dir}")

    if not any(d.kind not in ("cleanup", "remove") for d in _report):
        w("")
        w("  No audiobooks found in the source folder.")
    if moves:
        total = human_size(sum(d.size for d in moves))
        w("")
        w(s.green(s.bold(f"WOULD MOVE  {len(moves)} book{'s' * (len(moves) != 1)}")) + s.dim(f"  ({total})"))
        tree: dict[str, dict[str, list[tuple[str, Decision]]]] = {}
        for d in moves:
            parts = d.dest.relative_to(cfg.library_dir).parts
            author, series, title = (parts[0], "", parts[1]) if len(parts) == 2 else parts[:3]
            tree.setdefault(author, {}).setdefault(series, []).append((title, d))
        for author in sorted(tree, key=str.lower):
            w("")
            w("  " + s.bold(s.cyan(author)))
            for series in sorted(tree[author], key=lambda x: (x != "", x.lower())):
                indent = "    "
                if series:
                    w("    " + s.bold(series))
                    indent = "      "
                books = sorted(tree[author][series], key=lambda x: natural_key(Path(x[0])))
                col = min(max(len(t) for t, _ in books), 48)
                for title, d in books:
                    info = f"{d.files:>3} file{'s' if d.files != 1 else ' '}  {human_size(d.size):>9}"
                    w(f"{indent}{title:<{col}}  {s.dim(info)}")
                    w(f"{indent}  {s.dim('from ' + d.label)}")
    if quarantines:
        w("")
        w(s.red(s.bold(f"QUARANTINE  {len(quarantines)}")) + s.dim(f"  (would go to {cfg.quarantine_dir})"))
        for d in quarantines:
            w("")
            w("  " + s.red("x ") + d.label)
            for line in d.reason.split("; "):
                w("      " + s.dim(line))
    if reviews:
        w("")
        w(s.yellow(s.bold(f"NEEDS REVIEW  {len(reviews)}")) + s.dim(f"  (would go to {cfg.review_dir})"))
        for d in reviews:
            w("")
            w("  " + s.yellow("! ") + d.label)
            for line in d.reason.split("; "):
                w("      " + s.dim(line))
    if errors:
        w("")
        w(s.red(s.bold(f"COULD NOT CHECK  {len(errors)}")) + s.dim("  (would be retried later)"))
        for d in errors:
            w("  " + s.red("x ") + d.label)
            w("      " + s.dim(d.reason))
    if cleanups:
        w("")
        w(s.bold(f"LEFTOVER FOLDERS TO DELETE  {len(cleanups)}") + s.dim("  (no audio left in them)"))
        for d in cleanups:
            w("  - " + d.label)
            w("      " + s.dim(d.reason))
    if removals:
        w("")
        w(s.bold(f"WOULD REMOVE  {len(removals)}") + s.dim("  (seed limit reached, library copy checked)"))
        for d in removals:
            w("  - " + d.label)
            w("      " + s.dim(d.reason))
    w("")
    if moves:
        w(s.dim("Run without --dry-run to move them. Full details are in audiobook_sorter.log."))
    print("\n".join(out))


STATUS: dict = {}  # shown in the web UI header: qBittorrent / ClamAV reachability, last scan


def _tags(t: dict) -> set[str]:
    return {x.strip() for x in (t.get("tags") or "").split(",") if x.strip()}


def copy_problem(src: str, dst: str) -> str:
    """Why the library/review copy of one downloaded file can't be confirmed ('' = it's fine)."""
    try:
        if Path(dst).stat().st_size == Path(src).stat().st_size:
            return ""
        return f"{dst} has the wrong size"
    except FileNotFoundError:
        pass
    except OSError as e:
        return str(e)
    # Not where it was put. If the download still has another hard link, the copy was moved
    # (e.g. from review into the library by hand), so deleting the download loses nothing.
    try:
        if Path(src).stat().st_nlink > 1:
            return ""
        return f"missing {dst}"
    except OSError as e:
        return str(e)


def limit_reached(t: dict, prefs: dict) -> bool:
    """Has this torrent hit its share limit (its own, or qBittorrent's global one)?"""
    ratio_limit = t.get("ratio_limit", -2)
    if ratio_limit == -2:
        ratio_limit = prefs.get("max_ratio", -1) if prefs.get("max_ratio_enabled") else -1
    time_limit = t.get("seeding_time_limit", -2)          # minutes
    if time_limit == -2:
        time_limit = prefs.get("max_seeding_time", -1) if prefs.get("max_seeding_time_enabled") else -1
    by_ratio = ratio_limit is not None and ratio_limit >= 0 and t.get("ratio", 0) >= ratio_limit - 0.001
    by_time = time_limit is not None and time_limit >= 0 and t.get("seeding_time", 0) >= time_limit * 60
    return by_ratio or by_time


class Sorter:
    def __init__(self, cfg: Config, once: bool, progress: bool = False):
        self.cfg = cfg
        self.once = once
        self.progress = progress and sys.stdout is not None and sys.stdout.isatty()
        self.seen: dict[Path, tuple] = {}         # root -> (signature, stable_since)
        self.retry_at: dict = {}                  # book root or torrent hash -> time
        self.dry_done: set = set()
        self.wake = threading.Event()
        self.qbit = Qbit(cfg.qbit.url, cfg.qbit.username, cfg.qbit.password) if cfg.qbit else None
        self.next_torrent_poll = 0.0
        self.next_seed_check = 0.0

    def scan(self) -> None:
        with WORK_LOCK:
            self._scan()

    def _scan(self) -> None:
        now = time.time()
        STATUS["last_scan"] = now
        if self.qbit and now >= self.next_torrent_poll:
            self.next_torrent_poll = now + TORRENT_POLL_SECONDS
            self.scan_torrents()
            if self.cfg.qbit.delete_after_seeding and time.time() >= self.next_seed_check:
                self.next_seed_check = time.time() + SEED_CHECK_SECONDS
                self.cleanup_seeded()
        self.scan_inbox()

    # ------------------------------------------------------------------ source_dir (manual drops)
    def scan_inbox(self) -> None:
        now = time.time()
        books = find_books(self.cfg)
        current = {b.root for b in books}
        for gone in set(self.seen) - current:
            self.seen.pop(gone, None)
            self.retry_at.pop(gone, None)

        for i, book in enumerate(books, 1):
            if self.progress:
                text = f"Checking {i}/{len(books)}: {label(self.cfg, book)}"
                width = shutil.get_terminal_size((100, 20)).columns - 1
                print("\r\033[K" + text[:width], end="", flush=True)
            sig = signature(book)
            if sig is None:
                self.seen.pop(book.root, None)
                continue
            if self.retry_at.get(book.root, 0) > now:
                continue
            if not self.once:
                prev = self.seen.get(book.root)
                if prev is None or prev[0] != sig:
                    self.seen[book.root] = (sig, now)
                    continue
                if now - prev[1] < self.cfg.settle_seconds:
                    continue
            if not files_unlocked(book):
                continue
            if self.cfg.dry_run and (book.root, sig) in self.dry_done:
                continue
            try:
                scan_paths = [book.root] if book.root.is_dir() else list(book.files)
                reason = security_check(self.cfg, book.files, scan_paths)
                if reason:
                    quarantine(self.cfg, book, reason)
                else:
                    process(self.cfg, book)
                self.retry_at.pop(book.root, None)
            except RetryLater as e:
                self.retry(book.root, label(self.cfg, book), e)
            except Exception as e:
                log.exception("Unexpected error with %s; will retry", book.root.name)
                self.retry(book.root, label(self.cfg, book), RetryLater(f"unexpected error: {e}"))
            if self.cfg.dry_run:
                self.dry_done.add((book.root, sig))
            self.seen.pop(book.root, None)
        if self.progress:
            print("\r\033[K", end="", flush=True)

        # Folders with no audio left (e.g. from an older run, or a review book moved out by hand)
        for folder in leftover_folders(self.cfg):
            cleanup_leftovers(self.cfg, folder)

    def retry(self, key, name: str, e: RetryLater) -> None:
        log.error("Will retry %s in %ds: %s", name, e.seconds, e)
        self.retry_at[key] = time.time() + e.seconds
        if self.cfg.dry_run:
            _report.append(Decision("error", name, reason=str(e)))

    # ------------------------------------------------------------------ qBittorrent
    def local_path(self, p: str) -> Path:
        """Translate a path as qBittorrent sees it into a path the sorter can open."""
        for remote, local in self.cfg.qbit.path_map:
            r = remote.rstrip("/\\")
            if p == r or p.startswith(r + "/") or p.startswith(r + "\\"):
                return Path(local + p[len(r):])
        return Path(p)

    def fetch_torrents(self) -> list[dict] | None:
        try:
            torrents = self.qbit.torrents(self.cfg.qbit.category)
        except QbitError as e:
            if STATUS.get("qbit_ok") is not False:
                log.error("qBittorrent: %s", e)
            STATUS.update(qbit_ok=False, qbit_msg=str(e))
            return None
        STATUS.update(qbit_ok=True, qbit_msg=f"{len(torrents)} torrent(s) in {self.cfg.qbit.category!r}")
        return torrents

    def update_clamd_status(self) -> None:
        if not self.cfg.scan.clamd_host:
            STATUS.update(clamd_ok=None, clamd_msg="not configured")
            return
        try:
            STATUS.update(clamd_ok=True, clamd_msg=scanner.clamd_version(self.cfg.scan.clamd_host,
                                                                         self.cfg.scan.clamd_port))
        except scanner.ScannerDown as e:
            STATUS.update(clamd_ok=False, clamd_msg=str(e))

    def scan_torrents(self) -> None:
        self.update_clamd_status()
        torrents = self.fetch_torrents()
        if torrents is None:
            return
        now = time.time()
        for t in torrents:
            h = t.get("hash", "")
            if _tags(t) & {TAG_SORTED, TAG_REVIEW}:
                continue
            if t.get("progress", 0) < 1 or t.get("state") not in DONE_STATES:
                continue  # still downloading, checking or being moved out of the incomplete folder
            if self.retry_at.get(h, 0) > now or (self.cfg.dry_run and h in self.dry_done):
                continue
            try:
                self.handle_torrent(t)
                self.retry_at.pop(h, None)
            except RetryLater as e:
                self.retry(h, t.get("name", h), e)
            except Exception as e:
                log.exception("Unexpected error with torrent %s; will retry", t.get("name"))
                self.retry(h, t.get("name", h), RetryLater(f"unexpected error: {e}"))
            if self.cfg.dry_run:
                self.dry_done.add(h)

    def handle_torrent(self, t: dict) -> None:
        cfg, h, name = self.cfg, t["hash"], t.get("name") or t["hash"]
        root = self.local_path(t.get("content_path") or str(Path(t["save_path"]) / name))
        if not root.exists():
            raise RetryLater(f"can't see the download at {root} (check [qbittorrent] path_map and mounts)")
        files = [root] if root.is_file() else sorted((p for p in root.rglob("*") if p.is_file()), key=natural_key)
        whole = Book(root, [p for p in files if p.suffix.lower() in cfg.audio_extensions], files, label=name)

        reason = security_check(cfg, files, [root])
        if reason:
            if not cfg.dry_run:
                try:  # stop seeding it and let go of the files before moving them
                    self.qbit.stop(h)
                    self.qbit.delete(h, delete_files=False)
                except QbitError as e:
                    raise RetryLater(f"couldn't remove the torrent before quarantining it: {e}")
            quarantine(cfg, whole, reason, torrent_hash=h)
            return

        if root.is_file():
            books = [Book(root, [root], [root])] if root.suffix.lower() in cfg.audio_extensions else []
        else:
            books = books_in_folder(cfg, root)
        if not books:
            log.warning("REVIEW %s (no audio files in this torrent)", name)
            if not cfg.dry_run:
                remember("review", name, torrent_hash=h, dest=str(root), reason="no audio files in this torrent")
                self.qbit.add_tags(h, TAG_REVIEW)
            else:
                _report.append(Decision("review", name, reason="no audio files in this torrent"))
            return
        for b in books:
            if b.root != root:
                b.label = f"{name}/{b.root.relative_to(root).as_posix()}"
            else:
                b.label = name
        outcomes = [process(cfg, b, mode="link", torrent_hash=h) for b in books]
        if not cfg.dry_run:
            tag = TAG_SORTED if all(o == "sorted" for o in outcomes) else TAG_REVIEW
            try:
                self.qbit.add_tags(h, tag)
            except QbitError as e:  # harmless: next time it's found to be already in place
                log.warning("Couldn't tag %s as %r: %s", name, tag, e)

    def cleanup_seeded(self) -> None:
        """Delete torrents that reached their seed limit, once their library copy is confirmed."""
        torrents = self.fetch_torrents()
        if not torrents:
            return
        try:
            prefs = self.qbit.preferences()
        except QbitError as e:
            log.error("qBittorrent: %s", e)
            return
        for t in torrents:
            if not (_tags(t) & {TAG_SORTED, TAG_REVIEW}) or t.get("state") not in STOPPED_STATES:
                continue
            if not limit_reached(t, prefs):
                continue  # stopped by hand, not by the share limit: leave it
            h, name = t["hash"], t.get("name", t["hash"])
            pairs = _history.placements(h) if _history is not None else []
            rejected = _history is not None and _history.rejected(h)
            if not pairs and not rejected:
                log.debug("Not removing %s: no record of where its files were placed", name)
                continue
            problems = [] if rejected else [p for src, dst in pairs if (p := copy_problem(src, dst))]
            if problems:
                log.warning("Not removing %s: library copy not confirmed (%s)", name, "; ".join(problems[:3]))
                continue
            days = t.get("seeding_time", 0) / 86400
            info = f"ratio {t.get('ratio', 0):.2f}, seeded {days:.1f} days"
            if self.cfg.dry_run:
                _report.append(Decision("remove", name, reason=info))
                continue
            try:
                self.qbit.delete(h, delete_files=True)
            except QbitError as e:
                log.error("Couldn't remove %s: %s", name, e)
                continue
            log.info("REMOVED %s from qBittorrent and deleted its download (%s); library copy kept", name, info)
            remember("removed", name, torrent_hash=h, reason=info)

    # ------------------------------------------------------------------ main loop
    def run(self) -> None:
        self.scan()
        if self.once:
            return

        sorter = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                sorter.wake.set()

        observer = Observer()
        observer.schedule(Handler(), str(self.cfg.source_dir), recursive=True)
        observer.start()
        log.info("Watching %s (settle %ds)%s. Ctrl+C to stop.", self.cfg.source_dir, self.cfg.settle_seconds,
                 f" and qBittorrent category {self.cfg.qbit.category!r}" if self.cfg.qbit else "")
        try:
            while True:
                self.wake.wait(POLL_SECONDS)
                self.wake.clear()
                try:
                    self.scan()
                except OSError as e:
                    log.error("Scan failed: %s", e)
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            observer.join()


def setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(DATA_DIR / "audiobook_sorter.log",
                                                  maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except PermissionError:
        uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "this user"
        sys.exit(f"ERROR: can't write to {DATA_DIR}. The sorter runs as {uid}, which has no write "
                 f"permission on that folder. On the NAS, make that user the owner of the folder "
                 f"mounted at {DATA_DIR} (README: Run on QNAP -> Fix 'Permission denied').")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout is not None:  # pythonw has no console
        try:  # don't crash on characters the console can't show (e.g. curly quotes when piped)
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.set_name("console")
        log.addHandler(sh)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main() -> None:
    global _history
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=SCRIPT_DIR / "config.toml")
    ap.add_argument("--once", action="store_true", help="process current files (no settle wait) and exit")
    ap.add_argument("--dry-run", action="store_true", help="log decisions without moving anything")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--write-metadata", action="store_true",
                    help="add metadata.opf / cover.jpg to books sorted earlier, then exit")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    if args.dry_run:
        cfg.dry_run = True
    # A one-off dry run prints a tidy summary at the end instead of log lines
    # (the full log still goes to audiobook_sorter.log). Use -v to see the log lines too.
    report = cfg.dry_run and args.once
    if report and not args.verbose:
        for h in log.handlers:
            if h.get_name() == "console":
                h.setLevel(logging.ERROR)
    if cfg.web or cfg.qbit:
        _history = History(DATA_DIR / "sorter.db", readonly=cfg.dry_run)
    log.info("Source %s -> library %s%s", cfg.source_dir, cfg.library_dir, "  [DRY RUN]" if cfg.dry_run else "")
    if args.write_metadata:  # one-off job next to the running sorter: no web page, no sorting
        backfill_metadata(cfg)
        return
    if cfg.web and not args.once:
        import webui
        webui.start(_history, STATUS, cfg, None if cfg.dry_run else ReviewActions(cfg))
        log.info("Web UI on http://%s:%d", "localhost" if cfg.web.host in ("0.0.0.0", "") else cfg.web.host,
                 cfg.web.port)
    Sorter(cfg, once=args.once, progress=report).run()
    if report:
        print_report(cfg)


if __name__ == "__main__":
    main()
