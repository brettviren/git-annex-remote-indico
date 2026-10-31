# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The git and git-annex commands an ingest needs, on one repository."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

#: The ``externaltype`` of this special remote.
EXTERNALTYPE = "indico"

#: Extensions kept in keys (as git-annex's ``*E`` backends do): up to two, of up to four characters.
_EXTENSION = re.compile(r"^[A-Za-z0-9]{1,4}$")


class RepoError(Exception):
    pass


def run(repo: Path, *args: str, input: str | None = None, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=repo, input=input, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        detail = (done.stderr.strip() or done.stdout.strip() or f"exit status {done.returncode}").splitlines()
        raise RepoError(f"git {' '.join(args[:3])}: {detail[-1]}")
    return done.stdout


def toplevel(path: Path) -> Path:
    try:
        top = run(path, "rev-parse", "--show-toplevel").strip()
    except (RepoError, OSError) as exc:
        raise RepoError(f"{path}: not in a git repository") from exc
    top = Path(top)
    if not run(top, "config", "--get", "annex.uuid", check=False).strip():
        raise RepoError(f"{top}: not a git-annex repository (git annex init, or datalad create)")
    return top


@dataclass
class Remote:
    name: str
    uuid: str
    url: str
    enabled: bool


def remotes(repo: Path) -> list[Remote]:
    """The special remotes of this type the repository knows, enabled here or not."""
    log = run(repo, "cat-file", "-p", "git-annex:remote.log", check=False)
    found = []
    for line in log.splitlines():
        uuid, _, rest = line.partition(" ")
        fields = dict(f.split("=", 1) for f in rest.split() if "=" in f)
        if fields.get("externaltype") != EXTERNALTYPE or fields.get("type") != "external":
            continue
        name = fields.get("name", uuid)
        enabled = run(repo, "config", "--get", f"remote.{name}.annex-uuid", check=False).strip() == uuid
        found.append(Remote(name, uuid, fields.get("url", ""), enabled))
    return found


def remote_name(base: str) -> str:
    """A name for the remote of a site: ``indico-bnl`` for https://indico.bnl.gov."""
    host = urlsplit(base).hostname or ""
    labels = [label for label in host.split(".")
              if label not in ("indico", "www", "gov", "org", "com", "edu", "net", "ch", "de", "fr", "it", "uk")]
    return "-".join(["indico", *labels]) if labels else "indico"


def remote_for(repo: Path, base: str, name: str | None = None) -> Remote:
    """The remote of the site at ``base``, enabled here; one is made if there is none."""
    known = [r for r in remotes(repo) if r.url.rstrip("/") == base and (name is None or r.name == name)]
    if known:
        r = known[0]
        if not r.enabled:
            run(repo, "annex", "enableremote", r.name)
            r.enabled = True
        return r
    if name and any(r.name == name for r in remotes(repo)):
        raise RepoError(f"the remote {name} is for another site")
    name = name or remote_name(base)
    run(repo, "annex", "initremote", name, "type=external", f"externaltype={EXTERNALTYPE}",
        "encryption=none", "autoenable=true", f"url={base}")
    return next(r for r in remotes(repo) if r.name == name)


def key_for(size: int, md5: str, filename: str) -> str:
    """The ``MD5E`` key of a file of this size and checksum (git-annex's own form, extension and all)."""
    extensions = []
    stem = filename
    for _ in range(2):
        stem, dot, ext = stem.rpartition(".")
        if not dot or not stem or not _EXTENSION.match(ext):
            break
        extensions.insert(0, "." + ext)
    return f"MD5E-s{size}--{md5.lower()}{''.join(extensions)}"


def md5_of(key: str) -> str | None:
    m = re.match(r"^MD5E?-s\d+(?:-[a-zA-Z]\d+)*--([0-9a-f]{32})", key)
    return m.group(1) if m else None


def lookupkeys(repo: Path, paths: list[str]) -> dict[str, str | None]:
    """The key of each annexed file at these paths, or None."""
    if not paths:
        return {}
    out = run(repo, "annex", "lookupkey", "--batch", "-z", input="\0".join(paths) + "\0", check=False)
    keys = out.split("\0") if "\0" in out else out.split("\n")
    return {p: (k.strip() or None) for p, k in zip(paths, keys)}


def batch(repo: Path, args: list[str], lines: list[str]) -> None:
    if lines:
        run(repo, "annex", *args, "--batch", "-z", input="\0".join(lines) + "\0")


def place(repo: Path, remote: Remote, files: list[tuple[str, str, str, str | None]]) -> None:
    """Point each path at its key, the key's content being at the URL on the remote.

    ``files`` are ``(path, key, url, old_key)``; a path whose old key differs is replaced, and
    the URL is unregistered from the old key: the site has other bytes there now.
    """
    for path, key, url, old in files:
        if old and old != key:
            run(repo, "annex", "unregisterurl", "--remote", remote.name, old, url, check=False)
            if not urls(repo, old, remote.uuid):  # git-annex leaves presence to a remote that claims URLs
                run(repo, "annex", "setpresentkey", old, remote.uuid, "0")
        target = repo / path
        if target.is_symlink() or target.exists():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
    batch(repo, ["registerurl", "--remote", remote.name], [f"{k} {u}" for _, k, u, _ in files])
    batch(repo, ["fromkey", "--force"], [f"{k} {p}" for p, k, _, _ in files])


def urls(repo: Path, key: str, uuid: str) -> list[str]:
    """The URLs recorded for a key on the remote with this UUID."""
    out = run(repo, "annex", "whereis", "--json", "--key", key, check=False)
    try:
        found = json.loads(out.splitlines()[0]) if out.strip() else {}
    except ValueError:
        return []
    return [u for w in found.get("whereis", []) + found.get("untrusted", []) if w.get("uuid") == uuid
            for u in w.get("urls", [])]


def add_small(repo: Path, paths: list[str]) -> None:
    """Add text files to git itself, whatever ``annex.largefiles`` says."""
    if paths:
        run(repo, "annex", "add", "--force-small", "--quiet", "--", *paths)


def commit(repo: Path, message: str) -> bool:
    if not run(repo, "diff", "--cached", "--name-only").strip():
        return False
    run(repo, "commit", "-q", "-m", message)
    return True
