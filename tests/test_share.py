import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from annex_indico import settings, share, tree
from annex_indico.ingest import Options, ingest

from conftest import TOKEN, git
from test_ingest import material, meeting  # noqa: F401 (a fixture)

needs_datalad = pytest.mark.skipif(shutil.which("datalad") is None, reason="needs DataLad")


def mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def gi(cwd: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "indico", *args], cwd=cwd, capture_output=True, text=True)


def test_shared_value():
    assert [share.shared_value(v) for v in ("", "false", "umask", "0", "true", "1", "group", "2", "all")] == \
        [None, None, None, None, "group", "group", "group", "all", "all"]
    assert share.shared_value("0660") == "0660" and share.shared_value("0600") is None


def test_share_an_existing_repository(repo):
    old = os.umask(0o022)
    try:
        (repo / "notes.txt").write_text("hello")
        git(repo, "add", "notes.txt")
        git(repo, "commit", "-qm", "notes")
    finally:
        os.umask(old)
    assert mode(repo / "notes.txt") == 0o644
    found = share.share(repo)
    assert not found.created and "set core.sharedRepository=group" in found.done
    assert share.shared_config(repo) == "group"
    assert mode(repo / "notes.txt") == 0o664
    assert mode(repo / ".git") & stat.S_ISGID and mode(repo / ".git/objects") & 0o070 == 0o070
    assert share.share(repo).done == []  # nothing left to do


@needs_datalad
def test_share_clone_get_and_guard(site, meeting, tmp_path, monkeypatch):  # noqa: F811
    event, slides, talk = meeting
    root = tmp_path / "srv"
    root.mkdir()
    done = gi(tmp_path, "share", str(root / "bnl"))
    assert done.returncode == 0, done.stderr
    shared = root / "bnl"
    assert (shared / ".datalad").is_dir() and share.shared_config(shared) == "group"

    # an ingest there leaves what the group may change, whatever the umask
    old = os.umask(0o022)
    try:
        ingest(shared, [f"{site.base}/event/42/"], Options())
    finally:
        os.umask(old)
    git(shared, "commit", "-qm", "ingest")
    page = shared / tree.site_prefix(site.base) / "event/42/index.html"
    assert mode(page) & 0o060 == 0o060

    done = gi(tmp_path, "clone", str(shared), str(tmp_path / "mine"))
    assert done.returncode == 0, done.stderr
    mine = tmp_path / "mine"
    assert (mine / ".git/annex").is_symlink() and share.origin_of(mine) == shared
    assert share.shared_config(mine) == "group"
    path = str(material(mine, tree.site_prefix(site.base), talk).relative_to(mine))
    subprocess.run(["datalad", "get", path], cwd=mine, check=True, capture_output=True)
    stored = (mine / path).resolve()
    assert stored.is_relative_to(shared / ".git/annex/objects")
    assert mode(stored) & 0o040  # the group may read it

    # the clone's ingests are its own: say so
    report = ingest(mine, [f"{site.base}/event/42/"], Options())[0]
    assert any("shared clone" in w for w in report.warnings)

    # a protected event's files are refused where the group would see them
    site.protected.add(42)
    monkeypatch.setenv(settings.env_name(site.base), TOKEN)
    other = str(material(mine, tree.site_prefix(site.base), slides).relative_to(mine))
    done = subprocess.run(["git", "annex", "get", other], cwd=mine, capture_output=True, text=True)
    assert done.returncode != 0 and "shareprotected" in done.stderr + done.stdout
    assert TOKEN not in done.stderr + done.stdout
    git(mine, "config", "--global", f"indico.{site.base}.shareprotected", "true")
    done = subprocess.run(["git", "annex", "get", other], cwd=mine, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_guard_leaves_private_repositories_alone(repo, site, meeting, monkeypatch):  # noqa: F811
    site.protected.add(42)
    monkeypatch.setenv(settings.env_name(site.base), TOKEN)
    ingest(repo, [f"{site.base}/event/42/"], Options())
    git(repo, "commit", "-qm", "ingest")
    assert share.exposure(repo) is None
    path = str(material(repo, tree.site_prefix(site.base), meeting[2]).relative_to(repo))
    git(repo, "annex", "get", path)


def test_clone_refuses_what_is_not_shared(repo, tmp_path):
    if shutil.which("datalad") is None:
        pytest.skip("needs DataLad")
    done = gi(tmp_path, "clone", str(repo), str(tmp_path / "mine"))
    assert done.returncode != 0 and "git indico share" in done.stderr
