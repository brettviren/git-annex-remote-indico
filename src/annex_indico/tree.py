# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Where an Indico site's things go in the repository, and what is written for them.

The tree of one site, below its *prefix* (the site's host name unless
another is given), mirrors the paths of the site's own URLs, so that a URL
and a path name one thing::

    indico.bnl.gov/
      index.html                                the categories and events here
      category/201/category.json                the category, and the events listed in it
      category/201/index.html
      event/27078/event.json                    the event as the API gives it, with its timetable
      event/27078/index.html
      event/27078/attachments/60575/103996/slides.pdf
      event/27078/contributions/103544/attachments/60072/103186/notes.pdf

**Materials** (files attached to an event, a session, a contribution) are
annexed: their content is on the site and is got with ``git annex get``.
They sit where the site's download URL puts them, so a file that is
uploaded again keeps its path and gets a new key: a commit says what
changed and when it was seen.

**Taxa** (categories and events) are small text kept in git: the JSON the
API gave (``category.json``, ``event.json``), which is the record, and an
``index.html`` made from it, for people.  The HTML links to what is in the
tree by relative paths, and to the site for what is not, so it can be
browsed from the working tree (or a web server over it).  The HTML is made
from the JSON alone and the files present, so it can be made again at
any time (``git indico html``).
"""

from __future__ import annotations

import html
import json
import os
import posixpath
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

#: Keys of the API's records that only repeat what is elsewhere in them (``material`` is the
#: deprecated form of ``folders``, and each session slot carries the whole event).
REPEATS = ("material", "conference")

#: Keys of a category that change with every event added to the site, or depend on who asks.
VOLATILE = re.compile(r"^(deep_.*_count|can_.*)$")

CATEGORY_JSON, EVENT_JSON, INDEX = "category.json", "event.json", "index.html"


class TreeError(Exception):
    pass


# -- paths ---------------------------------------------------------------------


def site_prefix(url: str) -> str:
    """The default prefix of a site: its host (and port, if it has one)."""
    return urlsplit(url).netloc.replace(":", "_")


def url_path(base: str, url: str) -> PurePosixPath | None:
    """The path of a URL of the site, relative to the site, or None if it is not one of the site's.

    Each segment is decoded; one that would climb out of the tree is refused.
    """
    base_parts, parts = urlsplit(base), urlsplit(urljoin(base + "/", url))
    if (parts.scheme, parts.netloc) != (base_parts.scheme, base_parts.netloc):
        return None
    root = base_parts.path.rstrip("/")
    if root and not (parts.path + "/").startswith(root + "/"):
        return None
    segments = [unquote(s) for s in parts.path[len(root):].split("/") if s]
    for s in segments:
        if s in (".", "..") or "/" in s or "\0" in s:
            raise TreeError(f"{url}: a path segment {s!r} cannot be a file name")
    return PurePosixPath(*segments) if segments else PurePosixPath(".")


def category_dir(number) -> PurePosixPath:
    return PurePosixPath("category", str(number))


def event_dir(number) -> PurePosixPath:
    return PurePosixPath("event", str(number))


def material_path(base: str, attachment: dict) -> PurePosixPath | None:
    """Where an attached file goes: the path of its download URL, below ``event/``."""
    url = attachment.get("download_url")
    if attachment.get("type") != "file" or not url:
        return None
    path = url_path(base, url)
    if path is None or len(path.parts) < 3 or path.parts[0] != "event":
        raise TreeError(f"{url}: not where an event's files are")
    return path


# -- records -------------------------------------------------------------------


def trim(record):
    """A record without what repeats what it holds elsewhere."""
    if isinstance(record, dict):
        return {k: trim(v) for k, v in record.items() if k not in REPEATS}
    if isinstance(record, list):
        return [trim(v) for v in record]
    return record


def attachments(record) -> Iterator[dict]:
    """Every attachment in a record (an event with its timetable), each once."""
    seen: set = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("_type") == "attachment":
                if node.get("id") not in seen:
                    seen.add(node.get("id"))
                    yield node
                return
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    yield from walk(record)


def contributions(event: dict) -> list[dict]:
    """Every contribution of an event, wherever it is in the timetable, in time order."""
    found: dict = {}
    for holder in (event, *(event.get("sessions") or ())):
        for c in holder.get("contributions") or ():
            found.setdefault(c.get("db_id") or c.get("id"), c)
    return sorted(found.values(), key=lambda c: (_when(c.get("startDate")), str(c.get("title"))))


def dumps(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def category_record(base: str, info: dict, events: Iterable[dict], old: dict | None = None) -> dict:
    """What ``category.json`` holds: the category, its subcategories, and the events listed in it.

    Events listed before are kept: a category is listed a range of days at a time.
    """
    def steady(d: dict) -> dict:
        return {k: v for k, v in d.items() if not VOLATILE.match(k)}

    listed = {str(e["id"]): e for e in (old or {}).get("events", ())}
    for e in events:
        listed[str(e["id"])] = {
            k: e.get(k) for k in ("id", "title", "type", "startDate", "endDate", "categoryId", "category", "url")
        }
    return {
        "site": base,
        "category": steady(info.get("category") or {}),
        "subcategories": [steady(s) for s in info.get("subcategories") or ()],
        "events": sorted(listed.values(), key=lambda e: (_when(e.get("startDate")), int(e["id"]))),
    }


def event_record(base: str, event: dict, paths: dict[str, list[dict]] | None = None) -> dict:
    """What ``event.json`` holds: the event as the API gave it, trimmed, with where its category is."""
    record = trim(event)
    path = (paths or {}).get(str(event.get("categoryId")))
    return {"site": base, **({"categoryPath": path} if path else {}), "event": record}


# -- HTML ----------------------------------------------------------------------

STYLE = """
body { font-family: system-ui, sans-serif; max-width: 60rem; margin: 2rem auto; padding: 0 1rem;
       line-height: 1.45; color: #222; background: #fff; }
nav, .meta { color: #555; font-size: 0.92em; }
h1 { margin-bottom: 0.2rem; }
ul.files { list-style: none; padding-left: 0.5rem; }
ul.files li::before { content: "📄 "; }
ul.files li.link::before { content: "🔗 "; }
.remote { color: #888; font-size: 0.85em; }
.item { border-top: 1px solid #ddd; padding: 0.6rem 0; }
.when { color: #555; font-variant-numeric: tabular-nums; }
.description { margin: 0.4rem 0 0.4rem 1rem; }
table { border-collapse: collapse; } td { padding: 0.15rem 0.6rem 0.15rem 0; vertical-align: top; }
"""


class Linker:
    """Makes the links of one page: to what is in the tree by a relative path, else to the site.

    ``have(path)`` says whether a path (relative to the site's prefix) is in the tree.
    """

    def __init__(self, base: str, page: PurePosixPath, have: Callable[[PurePosixPath], bool]) -> None:
        self.base = base
        self.page = page
        self.have = have

    def rel(self, target: PurePosixPath) -> str:
        return posixpath.relpath(str(target), str(self.page.parent))

    def local(self, url: str) -> str | None:
        """The relative link that stands for a URL of the site, if what it names is here."""
        try:
            path = url_path(self.base, url)
        except TreeError:
            return None
        if path is None:
            return None
        fragment = urlsplit(url).fragment
        parts = path.parts
        if len(parts) >= 2 and parts[0] in ("event", "category"):
            page = PurePosixPath(parts[0], parts[1], INDEX)
            if "attachments" in parts:
                return self.rel(path) if self.have(path) else None
            if not self.have(page):
                return None
            anchor = ""
            if parts[0] == "event" and len(parts) >= 4 and parts[2] == "contributions":
                anchor = f"#contribution-{parts[3]}"
            elif fragment:
                anchor = f"#{fragment}"
            return self.rel(page) + anchor
        if parts == () or path == PurePosixPath("."):
            index = PurePosixPath(INDEX)
            return self.rel(index) if self.have(index) else None
        return None

    def href(self, url: str | None) -> str:
        if not url:
            return ""
        return self.local(url) or url

    def rewrite(self, fragment: str) -> str:
        """HTML from the site (a description) with its links to things here made local."""
        def one(m: re.Match) -> str:
            url = html.unescape(m.group(3))
            return f"{m.group(1)}={m.group(2)}{html.escape(self.href(url))}{m.group(2)}"
        return re.sub(r"""\b(href|src)=(["'])(.*?)\2""", one, fragment, flags=re.IGNORECASE)


def _when(d) -> str:
    if not isinstance(d, dict):
        return ""
    return f"{d.get('date', '')} {(d.get('time') or '')[:5]}".strip()


def _span(start, end) -> str:
    a, b = _when(start), _when(end)
    if not b or a == b:
        return a
    if a[:10] == b[:10]:
        return f"{a}–{b[11:]}"
    return f"{a} – {b}"


def _people(people) -> str:
    names = [p.get("fullName") or " ".join(filter(None, (p.get("first_name"), p.get("last_name"))))
             for p in people or () if isinstance(p, dict)]
    names = [n for n in names if n]
    return ", ".join(html.escape(n) for n in names)


def _size(n) -> str:
    if not isinstance(n, int):
        return ""
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return ""


def _page(title: str, nav: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{html.escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n"
        f"<nav>{nav}</nav>\n{body}\n</body>\n</html>\n"
    )


def _files(link: Linker, folders) -> str:
    rows = []
    for folder in folders or ():
        title = folder.get("title")
        items = []
        for a in folder.get("attachments") or ():
            label = html.escape(a.get("title") or a.get("filename") or "file")
            lock = " (protected)" if a.get("is_protected") else ""
            if a.get("type") == "link":
                url = html.escape(a.get("link_url") or "")
                items.append(f"<li class=\"link\"><a href=\"{url}\">{label}</a>{lock}</li>")
                continue
            url = a.get("download_url") or ""
            here = link.local(url)
            extra = [s for s in (_size(a.get("size")), a.get("modified_dt", "")[:10]) if s]
            remote = "" if here else " <span class=\"remote\">(on the site)</span>"
            items.append(f"<li><a href=\"{html.escape(here or url)}\">{label}</a>{lock}"
                         f" <span class=\"meta\">{html.escape(', '.join(extra))}</span>{remote}</li>")
        if items:
            head = f"<div class=\"meta\">{html.escape(title)}</div>" if title else ""
            rows.append(head + "<ul class=\"files\">" + "".join(items) + "</ul>")
    return "".join(rows)


def _note(link: Linker, record: dict) -> str:
    note = record.get("note")
    text = note.get("html") if isinstance(note, dict) else None
    return f"<details><summary>Minutes</summary>{link.rewrite(text)}</details>" if text else ""


def _breadcrumb(link: Linker, path: list[dict] | Iterable[dict]) -> str:
    steps = []
    for step in path:
        number, title = step.get("id"), step.get("title") or step.get("name") or ""
        url = f"{link.base}/category/{number}/" if number else f"{link.base}/"
        steps.append(f"<a href=\"{html.escape(link.href(url))}\">{html.escape(str(title))}</a>")
    return " › ".join(steps)


def _site_nav(link: Linker) -> str:
    index = link.local(link.base + "/")
    return f"<a href=\"{html.escape(index)}\">{html.escape(link.base)}</a>" if index else html.escape(link.base)


def event_html(record: dict, link: Linker) -> str:
    e = record["event"]
    base = link.base
    crumbs = _breadcrumb(link, record.get("categoryPath") or [])
    nav = _site_nav(link) + (f" · {crumbs}" if crumbs else "")
    where = ", ".join(x for x in (e.get("location"), e.get("roomFullname") or e.get("room")) if x)
    meta = " · ".join(html.escape(x) for x in (_span(e.get("startDate"), e.get("endDate")), where) if x)
    body = [f"<h1>{html.escape(e.get('title') or '')}</h1>", f"<p class=\"meta\">{meta}</p>"]
    origin = e.get("url") or f"{base}/event/{e.get('id')}/"
    body.append(f"<p class=\"meta\">On the site: <a href=\"{html.escape(origin)}\">{html.escape(origin)}</a>"
                f" · record: <a href=\"{EVENT_JSON}\">{EVENT_JSON}</a></p>")
    if e.get("chairs"):
        body.append(f"<p>Chairs: {_people(e['chairs'])}</p>")
    if e.get("description"):
        body.append(f"<div class=\"description\">{link.rewrite(e['description'])}</div>")
    body.append(_files(link, e.get("folders")))
    body.append(_note(link, e))
    sessions = {}
    for slot in e.get("sessions") or ():
        s = slot.get("session") or {}
        if s.get("folders"):
            sessions.setdefault(s.get("id"), s)
    if sessions:
        body.append("<h2>Sessions</h2>")
        for s in sessions.values():
            body.append(f"<div class=\"item\"><strong>{html.escape(s.get('title') or '')}</strong>"
                        f"{_files(link, s.get('folders'))}</div>")
    items = contributions(e)
    if items:
        body.append("<h2>Timetable</h2>")
    for c in items:
        anchor = c.get("db_id") or c.get("id")
        who = _people(c.get("speakers")) or _people(c.get("primaryauthors"))
        session = c.get("session")
        parts = [f"<div class=\"item\" id=\"contribution-{html.escape(str(anchor))}\">",
                 f"<span class=\"when\">{html.escape(_span(c.get('startDate'), c.get('endDate')))}</span> ",
                 f"<strong>{html.escape(c.get('title') or '')}</strong>"]
        if who:
            parts.append(f" — {who}")
        if session:
            parts.append(f" <span class=\"meta\">({html.escape(session)})</span>")
        if c.get("description"):
            parts.append(f"<div class=\"description\">{link.rewrite(c['description'])}</div>")
        parts.append(_files(link, c.get("folders")))
        parts.append(_note(link, c))
        for sub in c.get("subContributions") or ():
            parts.append(f"<div class=\"description\"><em>{html.escape(sub.get('title') or '')}</em>"
                         f"{(' — ' + _people(sub.get('speakers'))) if sub.get('speakers') else ''}"
                         f"{_files(link, sub.get('folders'))}{_note(link, sub)}</div>")
        parts.append("</div>")
        body.append("".join(parts))
    return _page(e.get("title") or f"event {e.get('id')}", nav, "\n".join(b for b in body if b))


def category_html(record: dict, link: Linker) -> str:
    c = record["category"]
    nav = _site_nav(link) + " · " + _breadcrumb(link, (c.get("parent_path") or []))
    body = [f"<h1>{html.escape(c.get('title') or '')}</h1>"]
    origin = f"{link.base}/category/{c.get('id')}/"
    body.append(f"<p class=\"meta\">On the site: <a href=\"{html.escape(origin)}\">{html.escape(origin)}</a>"
                f" · record: <a href=\"{CATEGORY_JSON}\">{CATEGORY_JSON}</a></p>")
    subs = record.get("subcategories") or []
    if subs:
        body.append("<h2>Subcategories</h2><ul>" + "".join(
            f"<li><a href=\"{html.escape(link.href(f'{link.base}/category/{s.get('id')}/'))}\">"
            f"{html.escape(s.get('title') or str(s.get('id')))}</a></li>" for s in subs) + "</ul>")
    events = record.get("events") or []
    if events:
        rows = []
        for e in events:
            url = e.get("url") or f"{link.base}/event/{e.get('id')}/"
            here = link.local(url)
            remote = "" if here else " <span class=\"remote\">(on the site)</span>"
            rows.append(f"<tr><td class=\"when\">{html.escape(_when(e.get('startDate'))[:10])}</td>"
                        f"<td><a href=\"{html.escape(here or url)}\">{html.escape(e.get('title') or '')}</a>"
                        f"{remote}</td></tr>")
        body.append(f"<h2>Events ({len(events)})</h2><table>" + "".join(rows) + "</table>")
    return _page(c.get("title") or f"category {c.get('id')}", nav, "\n".join(body))


def site_html(base: str, categories: list[dict], events: list[dict], link: Linker) -> str:
    body = [f"<h1>{html.escape(base)}</h1>"]
    if categories:
        body.append("<h2>Categories</h2><ul>" + "".join(
            f"<li><a href=\"{link.rel(category_dir(c.get('id')) / INDEX)}\">"
            f"{html.escape(c.get('title') or str(c.get('id')))}</a></li>"
            for c in sorted(categories, key=lambda c: str(c.get("title")))) + "</ul>")
    if events:
        body.append("<h2>Events</h2><table>" + "".join(
            f"<tr><td class=\"when\">{html.escape(_when(e.get('startDate'))[:10])}</td>"
            f"<td><a href=\"{link.rel(event_dir(e.get('id')) / INDEX)}\">{html.escape(e.get('title') or '')}"
            f"</a></td></tr>"
            for e in sorted(events, key=lambda e: _when(e.get("startDate")), reverse=True)) + "</table>")
    return _page(base, f"<a href=\"{base}/\">{html.escape(base)}</a>", "\n".join(body))


# -- the pages of a whole tree ---------------------------------------------------


def render(root: Path, base: str, extra: set[PurePosixPath] = frozenset()) -> dict[PurePosixPath, str]:
    """Every ``index.html`` of the site's tree at ``root``, made from the JSON there.

    ``extra`` are paths that will be in the tree though they are not yet (files about to be added).
    """
    def have(path: PurePosixPath) -> bool:
        return path in pages or path in extra or os.path.lexists(root / path)

    records: dict[PurePosixPath, tuple[str, dict]] = {}
    for kind, name in (("category", CATEGORY_JSON), ("event", EVENT_JSON)):
        for file in sorted((root / kind).glob(f"*/{name}")) if (root / kind).is_dir() else ():
            try:
                records[PurePosixPath(kind, file.parent.name, INDEX)] = (kind, json.loads(file.read_text("utf-8")))
            except (OSError, ValueError) as exc:
                raise TreeError(f"{file}: {exc}") from exc
    pages: set[PurePosixPath] = set(records) | {PurePosixPath(INDEX)}
    out: dict[PurePosixPath, str] = {}
    for page, (kind, record) in records.items():
        link = Linker(base, page, have)
        out[page] = event_html(record, link) if kind == "event" else category_html(record, link)
    cats = [r["category"] for k, r in records.values() if k == "category"]
    evs = [r["event"] for k, r in records.values() if k == "event"]
    out[PurePosixPath(INDEX)] = site_html(base, cats, evs, Linker(base, PurePosixPath(INDEX), have))
    return out
