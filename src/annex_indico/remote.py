# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The special remote: an Indico site, read only.

Made with::

    git annex initremote indico-bnl type=external externaltype=indico \\
        encryption=none autoenable=true url=https://indico.bnl.gov

(``git indico ingest`` does this for a site that has none).  It claims the
URLs of its site, so ``git annex registerurl`` and ``addurl`` record them
as on this remote, and it downloads them with the user's token (see
:mod:`.settings`).  It can store and remove nothing: what is on the site is
put there by people, through the site.

Whether the site still has a key is answered from the event's record: a
file uploaded again keeps its URL, so the URL answering is not enough; the
checksum the site gives for it must be the key's.
"""

from __future__ import annotations

from pathlib import Path

from annexremote import Master, RemoteError, SpecialRemote

from . import settings, tree
from .client import IndicoError
from .repo import md5_of


class IndicoRemote(SpecialRemote):

    def __init__(self, annex):
        super().__init__(annex)
        self.base: str | None = None
        self._client = None
        self._events: dict[str, dict | None] = {}
        self.configs = {"url": "the Indico site, such as https://indico.bnl.gov"}

    # -- set up

    def initremote(self):
        url = self.annex.getconfig("url")
        if not url:
            raise RemoteError("give url=, the Indico site (such as url=https://indico.bnl.gov)")
        try:
            self.annex.setconfig("url", settings.normalize(url))
        except settings.SettingsError as exc:
            raise RemoteError(str(exc)) from exc

    def prepare(self):
        url = self.annex.getconfig("url")
        if not url:
            raise RemoteError("this remote has no url= (the Indico site)")
        self.base = settings.normalize(url)

    def client(self):
        """The client, with the token: looked up only when something must be fetched."""
        if self._client is None:
            try:
                # git-annex runs this in the work tree, with GIT_DIR set (relative to here)
                site = settings.site(self.base)
            except settings.SettingsError as exc:
                raise RemoteError(str(exc)) from exc
            for w in site.warnings:
                self._say(w)
            self.annex.debug(f"{self.base}: token from {site.token_source}, user agent {site.user_agent or 'default'}")
            self._client = site.client()
        return self._client

    def _say(self, message: str) -> None:
        try:
            self.annex.info(message)
        except Exception:  # an old git-annex without INFO
            self.annex.debug(message)

    # -- what it holds

    def claimurl(self, url):
        return self.base is not None and tree.url_path(self.base, url) is not None

    def checkurl(self, url):
        try:
            return self.client().head(url)
        except IndicoError as exc:
            raise RemoteError(str(exc)) from exc

    def urls(self, key) -> list[str]:
        return self.annex.geturls(key, self.base + "/") if self.base else []

    def getavailability(self):
        return "global"

    def checkpresent(self, key):
        urls = self.urls(key)
        md5 = md5_of(key)
        try:
            for url in urls:
                if md5:
                    found = self._checksum(url)
                    if found is not None:
                        if found == md5:
                            return True
                        continue
                if self.client().head(url):
                    return True
        except IndicoError as exc:
            raise RemoteError(str(exc)) from exc
        return False

    def _checksum(self, url: str) -> str | None:
        """The MD5 the site gives now for the file at ``url``; None if it cannot say."""
        try:
            path = tree.url_path(self.base, url)
        except tree.TreeError:
            return None
        if not path or len(path.parts) < 2 or path.parts[0] != "event":
            return None
        number = path.parts[1]
        if number not in self._events:
            self._events[number] = self.client().event(number)
        event = self._events[number]
        if event is None:
            return None
        for a in tree.attachments(event):
            if a.get("download_url") and tree.material_path(self.base, a) == path:
                return (a.get("checksum") or "").lower() or None
        return ""  # the event no longer has this file: not present

    # -- moving content

    def transfer_retrieve(self, key, filename):
        urls = self.urls(key)
        if not urls:
            raise RemoteError(f"no URL of {self.base} is recorded for {key}")
        errors = []
        for url in urls:
            try:
                self.client().download(url, Path(filename), progress=self.annex.progress)
                return
            except IndicoError as exc:
                errors.append(str(exc))
        raise RemoteError("; ".join(errors))

    def transfer_store(self, key, filename):
        raise RemoteError("an Indico site is read only here: upload through the site")

    def remove(self, key):
        raise RemoteError("an Indico site is read only here: remove through the site")


def main() -> None:
    master = Master()
    master.LinkRemote(IndicoRemote(master))
    master.Listen()
