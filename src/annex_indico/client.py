# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""A barebones client of the HTTP API of one Indico site.

Ported from orgface (``orgface.httpclient`` and
``orgface.sources.indico.client``).  Only the standard library is used.

What the site offers is the "legacy" export API:

``export/categ/ID.json``
    The events in a category *and all below it*.  ``detail`` says how much
    of each; ``from``/``to`` bound the days, ``limit``/``offset`` page.
``export/event/ID.json``
    One event; with ``detail=sessions`` also its timetable: sessions with
    their contributions, contributions with their sub-contributions, and
    the folders of attachments of each.
``category/ID/info``
    A category: its title and where it is in the tree of categories.

**A page may be short and a later one not.**  The site applies ``limit``
before it removes the events the caller may not see, so only a few empty
pages in a row mean the end.  See :meth:`Client.events`.

**Credentials** are a personal access token, ``indp_...``, sent as a
bearer token.  ``read:legacy_api`` is the scope the export API needs; files
of protected events are web pages, which need ``read:everything``.  The
token goes to this site only: a redirect to another host drops it.

Some sites (FNAL's) put a gateway before Indico that refuses the user
agents of programs it does not know, so the user agent is a setting.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__

log = logging.getLogger(__name__)

USER_AGENT = f"git-annex-remote-indico/{__version__}"

#: HTTP statuses worth another try: too many requests, and a server having a bad moment.
RETRY = frozenset({429, 500, 502, 503, 504})

#: The most events a page can ask for, by ``detail``; the site refuses more.
MAX_PAGE = {"events": 1000, "contributions": 500, "subcontributions": 500, "sessions": 100}

#: Pages in a row with nothing in them that mean there is nothing more.
EMPTY_PAGES = 3


class IndicoError(Exception):
    """The site could not be asked, or did not answer in a way that makes sense.

    ``status`` is the HTTP status it answered with, or None if it did not answer.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


#: Request headers that must not follow a redirect to another host.
_CREDENTIALS = ("authorization", "cookie")


class _SafeRedirect(HTTPRedirectHandler):
    """Follow redirects, but keep credentials from going to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(newurl).netloc != urlsplit(req.full_url).netloc:
            for name in list(redirected.headers):
                if name.lower() in _CREDENTIALS:
                    del redirected.headers[name]
        return redirected


class Client:
    """Asks one Indico site for JSON, and for the files attached to things.

    ``pause`` is kept before every request, to stay inside a rate limit.
    ``sleep`` is the clock to wait with, so a test need not wait.
    """

    def __init__(
        self,
        base: str,
        *,
        token: str | None = None,
        timeout: float = 60.0,
        retries: int = 4,
        pause: float = 0.2,
        user_agent: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.user_agent = user_agent or USER_AGENT
        self._sleep = sleep
        self._opener = build_opener(_SafeRedirect)
        self.requests = 0
        #: Events that could not be had, by number, and why.
        self.denied: dict[str, str] = {}

    @property
    def authenticated(self) -> bool:
        return bool(self.headers)

    # -- one request

    def url(self, path: str, **params: Any) -> str:
        """The URL of ``path`` below the site, with ``params`` encoded (None is left out)."""
        wanted = {k: v for k, v in params.items() if v is not None}
        url = f"{self.base}/{path.strip('/')}"
        if path.endswith("/"):  # `api/user/` is not `api/user` to the site
            url += "/"
        if wanted:
            url += "?" + urlencode(wanted, quote_via=quote, safe=",")
        return url

    def owns(self, url: str) -> bool:
        """Whether ``url`` is on this site (and so may be sent the credentials)."""
        mine, theirs = urlsplit(self.base), urlsplit(url)
        return (theirs.scheme, theirs.netloc) == (mine.scheme, mine.netloc) and (
            theirs.path + "/").startswith(mine.path.rstrip("/") + "/")

    def get(self, path: str, retries: int | None = None, **params: Any) -> Any:
        """The JSON at ``path`` below the site."""
        url = self.url(path, **params)

        def decode(response: Any) -> Any:
            try:
                return json.load(response)
            except json.JSONDecodeError as exc:
                raise IndicoError(f"{url}: the answer is not JSON: {exc}") from exc

        return self._request(url, decode, "application/json", retries)

    def head(self, url: str) -> bool:
        """Whether the site would give ``url`` now."""
        try:
            return self._request(url, lambda r: True, "*/*", 0, method="HEAD")
        except IndicoError as exc:
            if exc.status in (401, 403, 404, 410):
                return False
            raise

    def download(
        self, url: str, target: Path, progress: Callable[[int], None] | None = None
    ) -> tuple[int, str]:
        """GET a file of this site into ``target``; return its size and MD5.

        The file appears all at once, so there is never a half-written one.
        A file of any other site is refused: the credentials go with the request.
        """
        if not self.owns(url):
            raise IndicoError(f"{url}: not a file of {self.base}")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)

        def save(response: Any) -> tuple[int, str]:
            digest, size = hashlib.md5(usedforsecurity=False), 0
            handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    while chunk := response.read(1 << 20):
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if progress:
                            progress(size)
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
            return size, digest.hexdigest()

        return self._request(url, save, "*/*")

    def _request(
        self,
        url: str,
        consume: Callable[[Any], Any],
        accept: str,
        retries: int | None = None,
        method: str = "GET",
    ) -> Any:
        retries = self.retries if retries is None else retries
        headers = {"Accept": accept, "User-Agent": self.user_agent}
        if self.owns(url):
            headers.update(self.headers)
        for attempt in range(retries + 1):
            if self.requests:
                self._sleep(self.pause)
            self.requests += 1
            log.debug("%s %s", method, url)
            request = Request(url, headers=headers, method=method)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return consume(response)
            except HTTPError as exc:
                said, own_words = _said(exc)
                # a gateway in front of a site may refuse for a while, and says so in HTML;
                # the site itself says why it refuses in JSON, and that is for good
                again = exc.code in RETRY or (exc.code == 403 and not own_words and method == "GET")
                if not again or attempt == retries:
                    hint = ""
                    if exc.code == 403 and not own_words:
                        hint = (" (a gateway may be refusing this program's user agent; try: "
                                f"git config --global indico.{self.base}.useragent curl/8.5.0)")
                    raise IndicoError(f"{url}: {exc.code} {exc.reason}{said}{hint}", exc.code) from exc
                wait = _retry_after(exc, attempt)
                log.info("%s answered %d%s; waiting %.1fs", url, exc.code, said, wait)
            except (URLError, TimeoutError, ConnectionError) as exc:
                if attempt == retries:
                    raise IndicoError(f"{url}: {exc}") from exc
                wait = 2.0**attempt
                log.info("%s: %s; waiting %.1fs", url, exc, wait)
            self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- the things the site offers

    def who(self) -> dict | None:
        """Who the credentials are for, or None when there are none that work."""
        answer = self.get("api/user/")
        return answer if isinstance(answer, dict) else None

    def category(self, number: int | str) -> dict:
        """A category's own information: ``{"category": {...}, "subcategories": [...], ...}``."""
        answer = self.get(f"category/{number}/info")
        if not isinstance(answer, dict) or "category" not in answer:
            raise IndicoError(f"{self.base}: category {number}: not what was expected")
        return answer

    def event(self, number: int | str, detail: str = "sessions") -> dict | None:
        """One event as the site gives it, or None if there is none to be seen."""
        try:
            answer = self.get(f"export/event/{number}.json", detail=detail)
        except IndicoError as exc:
            if exc.status is None:
                raise
            self.denied[str(number)] = str(exc)
            return None
        results = answer.get("results") or []
        return results[0] if results else None

    def events(
        self,
        category: int | str,
        since: date | None,
        until: date | None,
        detail: str = "events",
        *,
        window: int = 366,
        paths: dict[str, list[dict]] | None = None,
    ) -> Iterator[dict]:
        """The events of a category, and the categories below it, between two days.

        An event is included if any of its days is in the range, so an event
        can come from two windows; the caller keeps one.  Where in the
        category tree each event's category is goes into ``paths``.
        """
        page = MAX_PAGE[detail]
        for first, last in windows(since, until, window):
            offset = empty = 0
            while empty < EMPTY_PAGES:
                answer = self.get(
                    f"export/categ/{category}.json", detail=detail, limit=page, offset=offset,
                    order="asc", **{"from": first.isoformat(), "to": last.isoformat()},
                )
                results = answer.get("results") or []
                empty = 0 if results else empty + 1
                if paths is not None:
                    for found in (answer.get("additionalInfo") or {}).get("eventCategories", ()):
                        paths[str(found["categoryId"])] = [
                            {"id": step["id"], "title": step["name"]}
                            for step in found.get("path", ())
                            if step.get("id") is not None
                        ]
                yield from results
                offset += page


def windows(since: date | None, until: date | None, days: int) -> Iterator[tuple[date, date]]:
    """Cover the days from ``since`` to ``until`` with ranges of about ``days``.

    A missing ``since`` is the beginning of Indico's world: its first events
    are from the 1980s and few, so everything before 2000 is one range.  A
    missing ``until`` is a year from now.  A last range shorter than a
    quarter of ``days`` is joined to the one before it.
    """
    last = until or date.today() + timedelta(days=366)
    first = since
    if first is None:
        end = min(last, date(1999, 12, 31))
        yield date(1970, 1, 1), end
        first = end + timedelta(days=1)
    pieces: list[tuple[date, date]] = []
    while first <= last:
        end = min(last, first + timedelta(days=days - 1))
        pieces.append((first, end))
        first = end + timedelta(days=1)
    if len(pieces) > 1 and (pieces[-1][1] - pieces[-1][0]).days + 1 < days // 4:
        previous = pieces.pop(-2)
        pieces[-1] = (previous[0], pieces[-1][1])
    yield from pieces


def _said(exc: HTTPError) -> tuple[str, bool]:
    """What the site said about its refusal, and whether it said it in its own words (JSON)."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except OSError:
        raw = ""
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    message = (
        next((body[k] for k in ("message", "error_description", "error") if body.get(k)), None)
        if isinstance(body, dict)
        else None
    )
    if message:
        return f" ({message})", True
    sender = (exc.headers.get("Server") if exc.headers else None) or ""
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
    text = (title.group(1) if title else re.sub(r"<[^>]+>", " ", raw)).split()
    summary = " ".join([*([f"from {sender}"] if sender else []), *text[:12]])
    return (f" (not in the site's own words: {summary})" if summary else ""), False


def _retry_after(exc: HTTPError, attempt: int) -> float:
    """Seconds to wait: what the server asked for, else a growing wait."""
    asked = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return max(0.0, float(asked)) if asked is not None else 2.0**attempt
    except ValueError:
        return 2.0**attempt
