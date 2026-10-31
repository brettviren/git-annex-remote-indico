# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The settings of one Indico site, from git config, and its token.

Settings are per user, never per repository's shared state: a token must
not go into the git-annex branch, where every clone would see it.  They are
read with ``git config --get-urlmatch``, so they can be given for one site,
as git's ``http.<url>.*`` settings are::

    [indico "https://indico.fnal.gov"]
        tokencommand = pass
        tokenid = gov/fnal/indico/api/token
        useragent = curl/8.5.0

The token, first found wins:

1. ``--token`` on the command line (where a command has it);
2. the environment variable ``INDICO_TOKEN_<HOST>``, the host in upper case
   with every other character ``_`` (``INDICO_TOKEN_INDICO_BNL_GOV``);
3. ``indico.tokencommand``: a program whose first line of output is the
   token, given ``indico.tokenid`` as its argument when that is set.  This
   is the recommended way, with ``pass``: the token is never written down
   in the clear;
4. ``indico.tokenfile``: a file whose first line is the token.  A warning is
   given if anyone but its owner may read it (permissions more open than 600).

Without a token only what is public can be had.  A resolved token is
never printed or logged; :func:`mask` is for showing one.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class SettingsError(Exception):
    """A setting is wrong, or a token could not be had."""


@dataclass
class Site:
    url: str
    token: str | None = None
    #: Where the token came from, for people: never the token itself.
    token_source: str = "none"
    user_agent: str | None = None
    pause: float | None = None
    warnings: tuple[str, ...] = ()

    def client(self, **options):
        from .client import Client
        extra = {k: v for k, v in (("user_agent", self.user_agent), ("pause", self.pause)) if v is not None}
        return Client(self.url, token=self.token, **extra, **options)


def normalize(url: str) -> str:
    """The site's base URL: scheme, host and any path prefix, without a trailing slash."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SettingsError(f"not the URL of a site: {url!r}")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def env_name(url: str) -> str:
    return "INDICO_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", urlsplit(url).netloc.upper())


def git_config(key: str, url: str, cwd: Path | None = None) -> str | None:
    """``indico.KEY`` as it applies to ``url``, from every git config there is."""
    done = subprocess.run(
        ["git", "config", "--get-urlmatch", f"indico.{key}", url],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    value = done.stdout.strip()
    return value or None


def site(url: str, token: str | None = None, cwd: Path | None = None, need_token: bool = True) -> Site:
    """The settings of the site at ``url``; the token too, unless ``need_token`` is false."""
    url = normalize(url)
    found = Site(url, user_agent=git_config("useragent", url, cwd))
    pause = git_config("pause", url, cwd)
    if pause is not None:
        try:
            found.pause = float(pause)
        except ValueError as exc:
            raise SettingsError(f"indico.pause for {url}: not a number of seconds: {pause!r}") from exc
    if need_token:
        found.token, found.token_source, found.warnings = _token(url, token, cwd)
    return found


def _token(url: str, given: str | None, cwd: Path | None) -> tuple[str | None, str, tuple[str, ...]]:
    if given:
        return given, "the command line", ()
    name = env_name(url)
    if os.environ.get(name):
        return os.environ[name], f"${name}", ()
    command = git_config("tokencommand", url, cwd)
    if command:
        argv = shlex.split(command)
        ident = git_config("tokenid", url, cwd)
        if ident:
            argv.append(ident)
        shown = " ".join(argv)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise SettingsError(f"indico.tokencommand for {url}: cannot run {shown}: {exc}") from exc
        if done.returncode != 0:
            detail = done.stderr.strip().splitlines()[-1:] or [f"exit status {done.returncode}"]
            raise SettingsError(f"indico.tokencommand for {url}: {shown}: {detail[0]}")
        value = done.stdout.partition("\n")[0].strip()
        if not value:
            raise SettingsError(f"indico.tokencommand for {url}: {shown}: the first line is empty")
        return value, f"the command {shown}", ()
    path = git_config("tokenfile", url, cwd)
    if path:
        file = Path(path).expanduser()
        try:
            value = file.read_text(encoding="utf-8").partition("\n")[0].strip()
            mode = stat.S_IMODE(file.stat().st_mode)
        except OSError as exc:
            raise SettingsError(f"indico.tokenfile for {url}: {exc}") from exc
        if not value:
            raise SettingsError(f"indico.tokenfile for {url}: {file}: the first line is empty")
        warnings = ()
        if mode & 0o077:
            warnings = (f"{file} may be read by others (mode {mode:03o}); chmod 600 it, "
                        "or better keep the token in pass and set indico.tokencommand",)
        return value, f"the file {file}", warnings
    return None, "none", ()


def mask(secret: str | None) -> str:
    """A token as it may be shown: its kind and length, not its value."""
    if not secret:
        return "(none)"
    kind = secret.split("_", 1)[0] + "_" if secret.startswith("indp_") else ""
    return f"{kind}…({len(secret)} characters)"
