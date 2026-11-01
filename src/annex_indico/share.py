# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Datasets shared by the users of one computer.

A *shared dataset* of a site is one repository on the computer, owned by a
Unix group, whose annex holds every file anyone has got.  Each user works in
a *shared clone* of it: a DataLad clone made with ``--reckless ephemeral``,
whose ``.git/annex`` is a symbolic link to the shared dataset's, so a file
got in any clone is stored once, there.

- :func:`share` makes a shared dataset, or makes an existing repository one:
  ``core.sharedRepository=group``, the group on everything, and directories
  that pass their group on (setgid).  Run again, it repairs permissions that
  someone with a narrow umask left.
- :func:`clone` makes a user's shared clone.  The clone's own git config sets
  the permissions of what is got in it, so it gets
  ``core.sharedRepository`` too; without that, files got there would be
  readable by their getter alone.

What one user's token gets into a shared annex, every member of the group can
read.  :func:`exposure` says whether a repository's annex is shared, for the
remote to refuse the files of protected events there (see
``indico.shareprotected``).
"""

from __future__ import annotations

import grp
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import repo as git


class ShareError(Exception):
    pass


#: ``core.sharedRepository`` values meaning the owner alone.
_PRIVATE = ("", "0", "false", "no", "off", "umask")


def shared_value(value: str | None) -> str | None:
    """What ``core.sharedRepository`` says: ``group``, ``all``, an octal mode, or None for private."""
    v = (value or "").strip().lower()
    if v in _PRIVATE:
        return None
    if v in ("1", "true", "yes", "on", "group"):
        return "group"
    if v in ("2", "all", "world", "everybody"):
        return "all"
    try:
        mode = int(v, 8)
    except ValueError:
        return None
    return v if mode & 0o077 else None


def shared_config(repo: Path | None = None) -> str | None:
    """The repository's ``core.sharedRepository``, as :func:`shared_value` says it."""
    done = subprocess.run(["git", "config", "--get", "core.sharedRepository"], cwd=repo,
                          capture_output=True, text=True, check=False)
    return shared_value(done.stdout)


def annex_dir(repo: Path | None = None) -> Path:
    """The repository's ``.git/annex`` (which may be a link to another's)."""
    done = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=repo,
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise ShareError(f"{repo or Path.cwd()}: not in a git repository")
    return Path(done.stdout.strip()) / "annex"


def origin_of(repo: Path | None = None) -> Path | None:
    """The shared dataset whose annex this clone uses, if it is a shared (ephemeral) clone."""
    annex = annex_dir(repo)
    if not annex.is_symlink():
        return None
    return annex.resolve().parent.parent  # .../ORIGIN/.git/annex


def exposure(repo: Path | None = None) -> str | None:
    """Why others may read what is got here, or None if only its getter may."""
    value = shared_config(repo)
    if value:
        return f"core.sharedRepository is {value}"
    annex = annex_dir(repo)
    if annex.is_symlink():
        target = annex.resolve()
        try:
            owner = target.stat().st_uid
        except OSError:
            owner = os.getuid()
        if owner != os.getuid():
            return f"its annex is {target}, of another user"
    return None


@dataclass
class Shared:
    path: Path
    group: str
    created: bool = False
    #: What was done, for people.
    done: list[str] = field(default_factory=list)
    #: Files and directories that could not be changed: owned by other users.
    others: int = 0


def _gid(group: str | None, path: Path) -> tuple[str, int]:
    """The group by name, or that of ``path`` (or of its nearest existing parent)."""
    if group:
        try:
            g = grp.getgrnam(group)
        except KeyError as exc:
            raise ShareError(f"no group {group!r}") from exc
    else:
        where = path
        while not where.exists():
            where = where.parent
        g = grp.getgrgid(where.stat().st_gid)
    if g.gr_gid not in os.getgroups() and g.gr_gid != os.getgid():
        raise ShareError(f"you are not in the group {g.gr_name}: give --group, or ask to be added "
                         f"(then log in again)")
    return g.gr_name, g.gr_gid


def share(path: Path, group: str | None = None) -> Shared:
    """Make ``path`` a shared dataset of ``group`` (by default: the group of its directory).

    A new one is made with DataLad if it is installed, else as a plain git-annex
    repository; an existing repository is kept and made shared.
    """
    path = Path(path).expanduser().absolute()
    name, gid = _gid(group, path)
    found = Shared(path, name)
    if path.exists():
        found.done.extend(_trust(path))
    if not (path / ".git").exists():
        if path.exists() and any(path.iterdir()):
            raise ShareError(f"{path}: not empty, and not a git repository")
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, -1, gid)
        path.chmod(0o2775)
        git.run(path, "init", "-q", "--shared=group")
        if shutil.which("datalad"):
            _run(["datalad", "create", "--force", "-c", "text2git", str(path)])
            found.done.append("made a DataLad dataset")
        else:
            git.run(path, "annex", "init", "-q", f"shared {name}")
            found.done.append("made a git-annex repository (DataLad is not installed)")
        found.created = True
    else:
        if git.run(path, "rev-parse", "--is-bare-repository").strip() == "true":
            raise ShareError(f"{path}: a bare repository; a shared dataset has a work tree")
        git.toplevel(path)  # a git-annex repository
    if shared_config(path) != "group":
        git.run(path, "config", "core.sharedRepository", "group")
        found.done.append("set core.sharedRepository=group")
    changed = _permit(path, gid, found)
    if changed:
        found.done.append(f"gave the group {name} {changed} files and directories")
    if found.others:
        found.done.append(f"left {found.others} files and directories of other users as they are "
                          f"(their owners can run this again)")
    return found


def _permit(path: Path, gid: int, found: Shared) -> int:
    """Give the group what the owner has (read, write, search) on everything of ours below ``path``;
    directories pass the group on.  Symbolic links are left alone."""
    me, changed = os.getuid(), 0
    for top, _, files in os.walk(path):
        for p in [Path(top), *(Path(top) / f for f in files)]:
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                continue
            mode = stat.S_IMODE(st.st_mode)
            want = mode | ((mode & 0o700) >> 3)
            if stat.S_ISDIR(st.st_mode):
                want |= stat.S_ISGID
            if st.st_gid == gid and want == mode:
                continue
            if st.st_uid != me:
                found.others += 1
                continue
            if st.st_gid != gid:
                os.chown(p, -1, gid, follow_symlinks=False)
            p.chmod(want)
            changed += 1
    return changed


@dataclass
class Cloned:
    path: Path
    origin: Path
    shared: str
    done: list[str] = field(default_factory=list)


def clone(source: Path, path: Path | None = None, dataset: Path | None = None) -> Cloned:
    """Make a shared clone of the shared dataset ``source`` at ``path``, a subdataset of ``dataset`` if given."""
    if not shutil.which("datalad"):
        raise ShareError("a shared clone is made with DataLad, which is not installed")
    source = Path(source).expanduser().absolute()
    if not (source / ".git").is_dir():
        raise ShareError(f"{source}: not a shared dataset (a repository with a work tree, on this computer)")
    found = Cloned(Path(path or source.name).expanduser().absolute(), source, "")
    found.done.extend(_trust(source))
    value = shared_config(source)
    if not value:
        raise ShareError(f"{source}: not shared (its owner can make it so: git indico share {source})")
    _gid(grp.getgrgid(source.stat().st_gid).gr_name, source)
    found.shared = value
    command = ["datalad", "clone"]
    if dataset:
        command += ["-d", str(Path(dataset).expanduser().absolute())]
    _run([*command, "--reckless", "ephemeral", str(source), str(found.path),
          "--config", f"core.sharedRepository={value}"])
    # in case DataLad left the option out; what is got here takes its permissions from this
    git.run(found.path, "config", "core.sharedRepository", value)
    found.done.append(f"cloned {source}, sharing its annex, with core.sharedRepository={value}")
    return found


def _trust(source: Path) -> list[str]:
    """Let git use a repository of another user (``safe.directory``), as it must to clone it."""
    if source.stat().st_uid == os.getuid():
        return []
    listed = subprocess.run(["git", "config", "--global", "--get-all", "safe.directory"],
                            capture_output=True, text=True, check=False).stdout.split("\n")
    if str(source) in listed or "*" in listed:
        return []
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(source)], check=True)
    return [f"added {source} to safe.directory in your git config (it belongs to another user)"]


def _run(argv: list[str]) -> None:
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        detail = (done.stderr.strip() or done.stdout.strip() or f"exit status {done.returncode}").splitlines()
        raise ShareError(f"{' '.join(argv[:2])}: {detail[-1]}")
