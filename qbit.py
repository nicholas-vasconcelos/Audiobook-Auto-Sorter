"""Minimal qBittorrent Web API client (v4.1+ and v5)."""
from __future__ import annotations

import requests


class QbitError(Exception):
    pass


class Qbit:
    def __init__(self, url: str, username: str = "", password: str = "", timeout: float = 15):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.s = requests.Session()
        # qBittorrent rejects requests whose Referer/Origin doesn't match its own address
        self.s.headers["Referer"] = self.url

    # ------------------------------------------------------------------ plumbing
    def _req(self, method: str, path: str, **kw) -> requests.Response:
        url = f"{self.url}/api/v2/{path}"
        for attempt in (0, 1):
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                raise QbitError(f"can't reach qBittorrent at {self.url}: {e}") from e
            if r.status_code in (401, 403) and attempt == 0:
                self.login()
                continue
            return r
        return r

    def _ok(self, r: requests.Response, what: str) -> requests.Response:
        if not r.ok:
            raise QbitError(f"{what} failed: HTTP {r.status_code} {r.text[:200]}")
        return r

    def login(self) -> None:
        if not self.username:
            raise QbitError("qBittorrent asked for a login but no username is set in config.toml "
                            "(or allow the sorter's subnet to bypass authentication)")
        try:
            r = self.s.post(f"{self.url}/api/v2/auth/login", timeout=self.timeout,
                            data={"username": self.username, "password": self.password})
        except requests.RequestException as e:
            raise QbitError(f"can't reach qBittorrent at {self.url}: {e}") from e
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise QbitError("qBittorrent login failed (check username/password)")

    # ------------------------------------------------------------------ API
    def version(self) -> str:
        return self._ok(self._req("GET", "app/version"), "version").text.strip()

    def preferences(self) -> dict:
        return self._ok(self._req("GET", "app/preferences"), "preferences").json()

    def torrents(self, category: str) -> list[dict]:
        r = self._ok(self._req("GET", "torrents/info", params={"category": category}), "torrent list")
        return r.json()

    def torrent(self, h: str) -> dict | None:
        """One torrent by hash, whatever its category (None if it's not in qBittorrent)."""
        r = self._ok(self._req("GET", "torrents/info", params={"hashes": h}), "torrent info")
        found = [t for t in r.json() if t.get("hash", "").lower() == h.lower()]
        return found[0] if found else None

    def files(self, h: str) -> list[str]:
        """The torrent's file names, relative to its save path."""
        r = self._ok(self._req("GET", "torrents/files", params={"hash": h}), "torrent files")
        return [f["name"] for f in r.json()]

    def add_tags(self, h: str, tags: str) -> None:
        self._ok(self._req("POST", "torrents/addTags", data={"hashes": h, "tags": tags}), "add tags")

    def remove_tags(self, h: str, tags: str) -> None:
        self._ok(self._req("POST", "torrents/removeTags", data={"hashes": h, "tags": tags}), "remove tags")

    def stop(self, h: str) -> None:
        r = self._req("POST", "torrents/stop", data={"hashes": h})       # v5
        if r.status_code == 404:
            r = self._req("POST", "torrents/pause", data={"hashes": h})  # v4
        self._ok(r, "stop torrent")

    def start(self, h: str) -> None:
        r = self._req("POST", "torrents/start", data={"hashes": h})      # v5
        if r.status_code == 404:
            r = self._req("POST", "torrents/resume", data={"hashes": h})  # v4
        self._ok(r, "start torrent")

    def delete(self, h: str, delete_files: bool) -> None:
        data = {"hashes": h, "deleteFiles": "true" if delete_files else "false"}
        self._ok(self._req("POST", "torrents/delete", data=data), "delete torrent")
