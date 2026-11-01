# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Walking an Indico site into a tree of a git-annex repository.

``ingest`` takes URLs of a site (a category, an event, or anything in an
event: a contribution, a file) and brings what they name into the tree
(see :mod:`.tree`):

- a category: ``category.json`` with the events listed in it (and in the
  categories below it) between two days, and each of those events unless
  told not to;
- an event: ``event.json`` with its timetable, and its files.

Files are *registered*, not downloaded: the site gives the size and MD5 of
every file, which is enough for git-annex to make the key, and the URL is
recorded as where the remote can get it.  ``git annex get`` downloads them.
A file the site gives no checksum for is downloaded once, to make its key.

Nothing is committed unless a message is given; the changes are staged.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import repo as git
from . import settings, share, tree
from .client import IndicoError

log = logging.getLogger(__name__)

#: How much of each file kind to take: all, or none.
MATERIALS = ("all", "none")

_TARGET = re.compile(r"^(?P<base>.*?)/(?P<kind>event|category)/(?P<number>\d+)(?:/|$)")


class IngestError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    base: str
    kind: str  # "event" or "category"
    number: int


def target(url: str) -> Target:
    """What a URL of a site names: the event it is in, or the category."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise IngestError(f"not a URL: {url!r}")
    m = _TARGET.match(parts.path)
    if not m:
        raise IngestError(f"{url}: not the URL of a category or of an event (or of something in one)")
    base = settings.normalize(f"{parts.scheme}://{parts.netloc}{m.group('base')}")
    return Target(base, m.group("kind"), int(m.group("number")))


@dataclass
class Options:
    prefix: str | None = None
    remote: str | None = None
    token: str | None = None
    since: date | None = None
    until: date | None = None
    #: For a category: also each event listed in it (else only the listing).
    events: bool = True
    materials: str = "all"
    #: Only files of these kinds: extensions (``pdf``) or media types.
    types: tuple[str, ...] = ()
    #: No file bigger than this many bytes.
    max_size: int | None = None
    dry_run: bool = False


@dataclass
class Report:
    site: str = ""
    prefix: str = ""
    remote: str = ""
    categories: int = 0
    events: int = 0
    #: Files by what became of them: new, changed, same, skipped (why).
    files: dict[str, int] = field(default_factory=dict)
    written: list[str] = field(default_factory=list)
    denied: dict[str, str] = field(default_factory=dict)
    requests: int = 0
    warnings: list[str] = field(default_factory=list)

    def count(self, what: str) -> None:
        self.files[what] = self.files.get(what, 0) + 1

    def summary(self) -> str:
        files = ", ".join(f"{n} {k}" for k, n in sorted(self.files.items())) or "none"
        line = (f"{self.site} → {self.prefix}/ (remote {self.remote or '-'}): {self.categories} categories, "
                f"{self.events} events; files: {files}; {len(self.written)} text files written; "
                f"{self.requests} requests")
        if self.denied:
            line += f"; {len(self.denied)} events refused: {', '.join(sorted(self.denied))}"
        return line


@dataclass
class _Material:
    path: str  # relative to the repository
    url: str
    record: dict
    key: str | None = None


def wanted(record: dict, options: Options) -> str | None:
    """Why a file is not taken, or None if it is."""
    if options.materials == "none":
        return "materials none"
    if options.types:
        extension = Path(str(record.get("filename", ""))).suffix.lstrip(".").lower()
        kinds = {t.lower() for t in options.types}
        if extension not in kinds and str(record.get("content_type", "")).lower() not in kinds:
            return "other kind"
    size = record.get("size")
    if options.max_size is not None and isinstance(size, int) and size > options.max_size:
        return "too big"
    return None


def ingest(
    where: Path,
    urls: list[str],
    options: Options | None = None,
    progress: Callable[[str], None] = lambda message: None,
) -> list[Report]:
    """Bring what the URLs name into the repository at (or above) ``where``; one report per site."""
    options = options or Options()
    if options.materials not in MATERIALS:
        raise IngestError(f"materials: one of {', '.join(MATERIALS)}")
    top = git.toplevel(where)
    by_site: dict[str, list[Target]] = {}
    for url in urls:
        t = target(url)
        by_site.setdefault(t.base, []).append(t)
    if options.prefix and len(by_site) > 1:
        raise IngestError("--prefix is for one site at a time")
    origin = share.origin_of(top)
    # in a shared dataset, what is written must be the group's to change too
    umask = os.umask(0)
    os.umask(umask & ~0o070 if share.shared_config(top) and not origin else umask)
    try:
        reports = [_site(top, base, targets, options, progress) for base, targets in by_site.items()]
    finally:
        os.umask(umask)
    if origin:
        for r in reports:
            r.warnings.append(f"this is a shared clone of {origin}: what is ingested here stays here; "
                              f"to share it, ingest in {origin}")
    return reports


def _site(top: Path, base: str, targets: list[Target], options: Options, progress) -> Report:
    site = settings.site(base, token=options.token, cwd=top)
    report = Report(site=base, prefix=options.prefix or tree.site_prefix(base))
    report.warnings.extend(site.warnings)
    for w in site.warnings:
        log.warning("%s", w)
    progress(f"{base}: token from {site.token_source}")
    client = site.client()
    root = top / report.prefix
    remote = None
    if not options.dry_run:
        remote = git.remote_for(top, base, options.remote)
        report.remote = remote.name
    texts: dict[PurePosixPath, str] = {}
    events: dict[int, dict] = {}
    paths: dict[str, list[dict]] = {}
    try:
        for t in targets:
            if t.kind == "category":
                progress(f"category {t.number}")
                try:
                    info = client.category(t.number)
                except IndicoError as exc:
                    if exc.status is None:
                        raise
                    info = None
                    report.warnings.append(f"category {t.number}: {exc}; described from its events instead")
                listed = {}
                for e in client.events(t.number, options.since, options.until, "events", paths=paths):
                    listed[str(e["id"])] = e
                file = tree.category_dir(t.number) / tree.CATEGORY_JSON
                old = _read_json(root / file)
                if info is None:
                    info = _category_from(t.number, paths, old)
                texts[file] = tree.dumps(tree.category_record(base, info, listed.values(), old))
                report.categories += 1
                progress(f"category {t.number}: {len(listed)} events")
                if options.events:
                    for number in listed:
                        events.setdefault(int(number), None)
            else:
                events.setdefault(t.number, None)
        for count, number in enumerate(events, 1):
            progress(f"event {number} ({count} of {len(events)})")
            record = client.event(number)
            if record is None:
                continue
            events[number] = record
            category = str(record.get("categoryId"))
            if category not in paths and category.isdigit():
                try:
                    found = client.category(category)["category"].get("path") or []
                    paths[category] = [{"id": step.get("id"), "title": step.get("title")} for step in found]
                except IndicoError as exc:  # the event may be seen though its category is not
                    log.info("category %s: %s", category, exc)
                    paths[category] = []
            file = tree.event_dir(number) / tree.EVENT_JSON
            texts[file] = tree.dumps(tree.event_record(base, record, paths))
            report.events += 1
        materials = _materials(top, report, base, [e for e in events.values() if e], options, client)
    except IndicoError as exc:
        raise IngestError(str(exc)) from exc
    finally:
        report.denied = dict(client.denied)
        report.requests = client.requests

    if options.dry_run:
        for file, text in texts.items():
            if _read_text(root / file) != text:
                report.written.append(str(PurePosixPath(report.prefix) / file))
        return report

    # the text files first: the pages are made from them
    for file, text in texts.items():
        _write(root / file, text, report, top)
    extra = {PurePosixPath(m.path).relative_to(report.prefix) for m in materials}
    for page, text in tree.render(root, base, extra).items():
        _write(root / page, text, report, top)
    git.add_small(top, report.written)
    changed = [m for m in materials if m.key]
    olds = git.lookupkeys(top, [m.path for m in changed])
    git.place(top, remote, [(m.path, m.key, m.url, olds.get(m.path)) for m in changed])
    return report


def _materials(top: Path, report: Report, base: str, events: list[dict], options: Options, client) -> list[_Material]:
    """The files of the events to place, with their keys; those already in place have no key."""
    found: list[_Material] = []
    for event in events:
        for record in tree.attachments(event):
            if record.get("type") != "file":
                continue
            why = wanted(record, options)
            if why:
                report.count(f"skipped ({why})")
                continue
            try:
                path = tree.material_path(base, record)
            except tree.TreeError as exc:
                report.warnings.append(str(exc))
                report.count("skipped (odd URL)")
                continue
            found.append(_Material(str(PurePosixPath(report.prefix) / path), record["download_url"], record))
    keys = git.lookupkeys(top, [m.path for m in found])
    kept = []
    for m in found:
        size, md5 = m.record.get("size"), m.record.get("checksum")
        if not (isinstance(size, int) and md5):
            if keys.get(m.path) and options.dry_run:
                report.count("unknown (no checksum)")
                continue
            with tempfile.TemporaryDirectory() as scratch:
                size, md5 = client.download(m.url, Path(scratch) / "file")
        key = git.key_for(size, md5, PurePosixPath(m.path).name)
        have = keys.get(m.path)
        # in place only if it is in the work tree too (datalad rerun removes a run's files first)
        if have and git.md5_of(have) == md5.lower() and os.path.lexists(top / m.path):
            report.count("same")
            if not options.dry_run:
                kept.append(m)  # in the tree already; its page still links to it
            continue
        m.key = key
        report.count("changed" if have and git.md5_of(have) != md5.lower() else "new")
        kept.append(m)
    return kept


def _category_from(number: int, paths: dict[str, list[dict]], old: dict | None) -> dict:
    """What can be said of a category whose own page cannot be had: what an earlier ingest kept,
    else its place in the tree, from the paths of the categories of the events listed in it."""
    if old and old.get("category"):
        return {"category": old["category"], "subcategories": old.get("subcategories") or []}
    for path in paths.values():
        ids = [step.get("id") for step in path]
        if number in ids:
            path = path[:ids.index(number) + 1]
            return {"category": {"id": number, "title": path[-1].get("title"), "path": path,
                                 "parent_path": path[:-1]}}
    return {"category": {"id": number, "title": f"category {number}"}}


def _read_text(path: Path) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as f:  # as written: descriptions hold \r\n
            return f.read()
    except OSError:
        return None


def _read_json(path: Path) -> dict | None:
    import json
    text = _read_text(path)
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _write(path: Path, text: str, report: Report, top: Path) -> None:
    if _read_text(path) == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    report.written.append(str(path.relative_to(top)))


def rerender(where: Path, prefixes: list[str] | None = None) -> list[str]:
    """Make the pages of the sites' trees again; the trees are those named, or every one found."""
    top = git.toplevel(where)
    if not prefixes:
        prefixes = sorted({p.parent.parent.parent.name for p in top.glob("*/*/*/*.json")
                           if p.name in (tree.CATEGORY_JSON, tree.EVENT_JSON)})
    report = Report()
    for prefix in prefixes:
        root = top / prefix
        record = next((_read_json(p) for p in root.glob("*/*/*.json")), None)
        if not record or "site" not in record:
            raise IngestError(f"{prefix}: no category.json or event.json saying which site it is")
        for page, text in tree.render(root, record["site"]).items():
            _write(root / page, text, report, top)
    git.add_small(top, report.written)
    return report.written
