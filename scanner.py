"""Safety checks run before anything reaches the library: a file-type filter and ClamAV (clamd)."""
from __future__ import annotations

import socket
from pathlib import Path

DEFAULT_ALLOWED = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",   # covers
    ".cue", ".nfo", ".txt", ".pdf", ".m3u", ".m3u8",     # notes, playlists, companion PDFs
    ".sfv", ".md5", ".log",
)
# Junk that operating systems leave behind; harmless and simply ignored.
IGNORED_NAMES = {"thumbs.db", ".ds_store", "desktop.ini"}

# The first bytes of programs and archives. A real audio file or picture never starts like this,
# so a "Chapter 1.mp3" that does is a disguised program.
EXEC_MAGIC = {
    b"MZ": "Windows program",
    b"\x7fELF": "Linux program",
    b"\xcf\xfa\xed\xfe": "macOS program",
    b"\xce\xfa\xed\xfe": "macOS program",
    b"\xca\xfe\xba\xbe": "macOS program",
    b"PK\x03\x04": "zip archive",
    b"Rar!": "rar archive",
    b"7z\xbc\xaf\x27\x1c": "7z archive",
    b"MSCF": "cab archive",
}


def check_file_types(files: list[Path], allowed: tuple[str, ...]) -> list[str]:
    """Return a description of every file that shouldn't be in an audiobook (empty list = all fine)."""
    bad = []
    for p in files:
        name = p.name.lower()
        if name in IGNORED_NAMES or name.startswith("._"):
            continue
        ext = p.suffix.lower()
        if ext not in allowed:
            bad.append(f"{p.name} (file type {ext or 'with no extension'} not allowed)")
            continue
        try:
            with open(p, "rb") as f:
                head = f.read(8)
        except OSError as e:
            bad.append(f"{p.name} (can't be read: {e.strerror})")
            continue
        for magic, what in EXEC_MAGIC.items():
            if head.startswith(magic):
                bad.append(f"{p.name} (named {ext} but is really a {what})")
                break
    return bad


class ScannerDown(Exception):
    """clamd couldn't be reached or couldn't scan; the book must wait, never be moved unscanned."""


def clamd_command(host: str, port: int, command: str, timeout: float = 900) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"z" + command.encode("utf-8") + b"\0")
            chunks = []
            while True:
                data = s.recv(65536)
                if not data:
                    break
                chunks.append(data)
    except OSError as e:
        raise ScannerDown(f"can't reach clamd at {host}:{port}: {e}") from e
    return b"".join(chunks).decode("utf-8", "replace").strip("\0\r\n ")


def clamd_version(host: str, port: int) -> str:
    """e.g. 'ClamAV 1.4.1/27450/Sat Nov  7 08:24:01 2026' (engine/signature version/signature date)."""
    return clamd_command(host, port, "VERSION", timeout=10)


def clamd_scan(host: str, port: int, path: str) -> list[str]:
    """Scan a file or folder (as clamd sees the path). Returns infected files as 'file: Signature'."""
    reply = clamd_command(host, port, f"CONTSCAN {path}")
    infected, errors = [], []
    for line in filter(None, (l.strip() for l in reply.replace("\0", "\n").splitlines())):
        if line.endswith(" FOUND"):
            file, _, sig = line[:-6].rpartition(": ")
            infected.append(f"{Path(file).name}: {sig}")
        elif line.endswith(" ERROR"):
            errors.append(line[:-6])
    if infected:
        return infected
    if errors or not reply.endswith("OK"):
        raise ScannerDown("clamd could not scan: " + ("; ".join(errors) or reply or "empty reply"))
    return []
