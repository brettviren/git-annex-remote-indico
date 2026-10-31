"""A fake Indico site on localhost, and real git-annex repositories.

The site serves the few endpoints the client uses, from a dict that a test
may change (to upload a file again, say), and refuses protected events and
files to requests without the token.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import pytest

TOKEN = "indp_secret-test-token"


def git(root: Path, *args, input=None) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
                          input=input).stdout


class Site:
    """What the fake site holds: events by number, files by path, categories by number."""

    def __init__(self, base: str):
        self.base = base
        self.events: dict[int, dict] = {}
        self.files: dict[str, bytes] = {}
        self.categories: dict[int, dict] = {}
        self.protected: set[int] = set()
        self.log: list[str] = []

    def attachment(self, event: int, folder: int, number: int, name: str, data: bytes, where: str = "",
                   checksum=True) -> dict:
        path = f"/event/{event}/{where}attachments/{folder}/{number}/{quote(name)}"
        self.files[path] = data
        record = {"_type": "attachment", "id": number, "type": "file", "title": name, "filename": name,
                  "download_url": self.base + path, "content_type": "application/pdf", "size": len(data),
                  "modified_dt": "2026-10-01T10:00:00+00:00", "is_protected": event in self.protected}
        if checksum:
            record["checksum"] = hashlib.md5(data).hexdigest()
        return record

    def upload(self, record: dict, data: bytes) -> None:
        """Upload a file again: same URL, other bytes."""
        self.files[urlsplit(record["download_url"]).path] = data
        record.update(size=len(data), checksum=hashlib.md5(data).hexdigest())

    def event(self, number: int, title: str, day: str, category: int = 7, files=(), contributions=()) -> dict:
        record = {
            "_type": "Conference", "id": str(number), "title": title, "categoryId": category,
            "category": "Group meetings", "url": f"{self.base}/event/{number}/",
            "startDate": {"date": day, "time": "10:00:00", "tz": "UTC"},
            "endDate": {"date": day, "time": "11:00:00", "tz": "UTC"},
            "description": f'<p>See <a href="{self.base}/event/{number}/contributions/501/">the talk</a>.</p>',
            "chairs": [{"fullName": "Viren, Brett"}], "material": [{"repeat": True}],
            "folders": [{"_type": "folder", "id": 9, "title": None, "attachments": list(files)}],
            "contributions": list(contributions), "sessions": [],
        }
        self.events[number] = record
        return record


class Handler(BaseHTTPRequestHandler):
    site: Site

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, kind="application/json", head=False):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data).encode())

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        parts = urlsplit(self.path)
        path, query = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.site.log.append(path)
        if path.startswith("/export/event/"):
            number = int(path.split("/")[-1].removesuffix(".json"))
            if number in self.site.protected and not self._authorized():
                return self._json({"message": "Access denied"}, 403)
            found = [self.site.events[number]] if number in self.site.events else []
            return self._json({"count": len(found), "results": found})
        if path.startswith("/export/categ/"):
            number = int(path.split("/")[-1].removesuffix(".json"))
            first, last = query.get("from", "0000"), query.get("to", "9999")
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 1000))
            listed = [{k: v for k, v in e.items() if k not in ("contributions", "sessions")}
                      for e in self.site.events.values()
                      if e["categoryId"] in self.site.categories.get(number, {}).get("below", {number})
                      and first <= e["startDate"]["date"] <= last and int(e["id"]) not in self.site.protected]
            page = listed[offset:offset + limit]
            info = {"eventCategories": [{"categoryId": number, "path": [
                {"id": 0, "name": "Home"}, {"id": number, "name": "Group meetings"}]}]}
            return self._json({"count": len(page), "results": page, "additionalInfo": info})
        if path.startswith("/category/") and path.endswith("/info"):
            number = int(path.split("/")[2])
            c = self.site.categories.get(number)
            if not c:
                return self._json({"message": "not found"}, 404)
            return self._json({"category": c["info"], "subcategories": [], "success": True})
        if path == "/api/user/":
            return self._json({"full_name": "Brett Viren", "email": "bv@example.org"} if self._authorized() else None)
        if path in self.site.files:
            number = int(path.split("/")[2])
            if number in self.site.protected and not self._authorized():
                return self._send(403, b"<html><title>Forbidden</title></html>", "text/html", head)
            return self._send(200, self.site.files[path], "application/octet-stream", head)
        if unquote(path) != path and quote(unquote(path)) in self.site.files:
            return self._send(200, self.site.files[quote(unquote(path))], "application/octet-stream", head)
        return self._json({"message": "not found"}, 404)


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    s = Site(base)
    server.RequestHandlerClass = type("H", (Handler,), {"site": s})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield s
    server.shutdown()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path_factory):
    """No user git config, a fixed author, and this package's programs on PATH."""
    home = tmp_path_factory.mktemp("home")
    (home / "gitconfig").touch()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.org"),
                 ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.org")):
        monkeypatch.setenv(k, v)
    for k in [k for k in os.environ if k.startswith("INDICO_TOKEN")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "annex", "init", "-q", "test")
    return r
