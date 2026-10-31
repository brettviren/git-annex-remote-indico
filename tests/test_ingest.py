import json
from datetime import date
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from annex_indico import ingest, repo as annex, settings, tree
from annex_indico.ingest import Options

from conftest import TOKEN, git


def ingest1(r: Path, url: str, **kw):
    return ingest.ingest(r, [url], Options(**kw))[0]


def material(r: Path, prefix: str, record: dict) -> Path:
    return r / prefix / tree.material_path(record["download_url"].split("/event/")[0], record)


@pytest.fixture
def meeting(site):
    slides = site.attachment(42, 9, 101, "agenda notes.pdf", b"agenda v1")
    talk = site.attachment(42, 31, 102, "noise.pdf", b"noise talk v1", where="contributions/501/")
    contribution = {
        "_type": "Contribution", "id": "1", "db_id": 501, "title": "TPC noise", "material": [],
        "speakers": [{"fullName": "Colleague, A."}], "url": f"{site.base}/event/42/contributions/501/",
        "startDate": {"date": "2026-10-28", "time": "10:10:00", "tz": "UTC"},
        "endDate": {"date": "2026-10-28", "time": "10:30:00", "tz": "UTC"},
        "folders": [{"_type": "folder", "id": 31, "title": None, "attachments": [talk]}],
        "subContributions": [],
    }
    event = site.event(42, "Group meeting", "2026-10-28", files=[slides], contributions=[contribution])
    return event, slides, talk


def test_event(repo, site, meeting):
    event, slides, talk = meeting
    report = ingest1(repo, f"{site.base}/event/42/contributions/501/")
    prefix = tree.site_prefix(site.base)
    assert report.events == 1 and report.files == {"new": 2}
    assert report.remote == annex.remote_name(site.base)

    record = json.loads((repo / prefix / "event/42/event.json").read_text())
    assert record["site"] == site.base and record["event"]["title"] == "Group meeting"
    assert "material" not in json.dumps(record)            # the deprecated repeat is trimmed

    talk_file = material(repo, prefix, talk)
    assert talk_file.is_symlink() and not talk_file.exists()      # registered, not downloaded
    assert talk_file.relative_to(repo / prefix).as_posix() == "event/42/contributions/501/attachments/31/102/noise.pdf"
    key = annex.lookupkeys(repo, [str(talk_file.relative_to(repo))])[str(talk_file.relative_to(repo))]
    assert key == annex.key_for(len(b"noise talk v1"), talk["checksum"], "noise.pdf")

    page = (repo / prefix / "event/42/index.html").read_text()
    assert 'href="contributions/501/attachments/31/102/noise.pdf"' in page
    assert 'href="attachments/9/101/agenda notes.pdf"' in page
    assert 'href="index.html#contribution-501"' in page          # a link in the description, made local
    assert (repo / prefix / "index.html").exists()
    staged = git(repo, "diff", "--cached", "--name-only")
    assert "event/42/event.json" in staged and "noise.pdf" in staged

    git(repo, "commit", "-qm", "ingest")
    git(repo, "annex", "get", str(talk_file.relative_to(repo)))
    assert talk_file.read_bytes() == b"noise talk v1"


def test_again_and_reupload(repo, site, meeting):
    event, slides, talk = meeting
    ingest1(repo, f"{site.base}/event/42/")
    git(repo, "commit", "-qm", "first")
    again = ingest1(repo, f"{site.base}/event/42/")
    assert again.files == {"same": 2} and again.written == []

    prefix = tree.site_prefix(site.base)
    path = str(material(repo, prefix, talk).relative_to(repo))
    old = annex.lookupkeys(repo, [path])[path]
    site.upload(talk, b"noise talk v2, corrected")
    remote = annex.remote_name(site.base)
    # the site has other bytes at the URL now: the old key is not there any more
    assert subprocess.run(["git", "annex", "checkpresentkey", old, remote], cwd=repo).returncode == 1

    later = ingest1(repo, f"{site.base}/event/42/")
    assert later.files == {"same": 1, "changed": 1}
    new = annex.lookupkeys(repo, [path])[path]
    assert new != old and annex.md5_of(new) == talk["checksum"]
    where = subprocess.run(["git", "annex", "whereis", "--key", old], cwd=repo, capture_output=True, text=True)
    assert remote not in where.stdout                       # the URL was unregistered from the old key
    git(repo, "commit", "-qm", "second")
    git(repo, "annex", "get", path)
    assert (repo / path).read_bytes() == b"noise talk v2, corrected"
    # the history keeps both: the first commit's key is the first upload
    assert git(repo, "log", "--format=%s", "--", path).split() == ["second", "first"]


def test_category(repo, site):
    site.categories[7] = {"info": {"id": 7, "title": "Group meetings", "deep_event_count": 3,
                                   "can_manage": False, "parent_path": [{"id": 0, "title": "Home"}]}}
    for n, day in ((1, "2026-01-05"), (2, "2026-03-05"), (3, "2026-05-05")):
        site.event(n, f"Meeting {n}", day, files=[site.attachment(n, 9, 100 + n, "s.pdf", f"s{n}".encode())])
    url = f"{site.base}/category/7/"
    first = ingest1(repo, url, since=date(2026, 1, 1),
                    until=date(2026, 3, 31))
    assert (first.categories, first.events, first.files) == (1, 2, {"new": 2})
    prefix = repo / tree.site_prefix(site.base)
    record = json.loads((prefix / "category/7/category.json").read_text())
    assert [e["id"] for e in record["events"]] == ["1", "2"]
    assert "deep_event_count" not in record["category"] and "can_manage" not in record["category"]

    # another range, the listing only: the events seen before stay listed
    later = ingest1(repo, url, since=date(2026, 4, 1), events=False)
    assert later.events == 0
    record = json.loads((prefix / "category/7/category.json").read_text())
    assert [e["id"] for e in record["events"]] == ["1", "2", "3"]
    page = (prefix / "category/7/index.html").read_text()
    assert 'href="../../event/1/index.html"' in page                 # here
    assert f'href="{site.base}/event/3/"' in page                    # not here: the site's
    assert not (prefix / "event/3").exists()
    assert 'href="../../category/7/index.html"' in (prefix / "event/1/index.html").read_text()   # breadcrumb


def test_filters_and_dry_run(repo, site, meeting):
    event, slides, talk = meeting
    dry = ingest1(repo, f"{site.base}/event/42/", dry_run=True)
    assert dry.files == {"new": 2} and dry.written and not dry.remote
    assert git(repo, "status", "--porcelain") == "" and not annex.remotes(repo)
    small = ingest1(repo, f"{site.base}/event/42/", max_size=10)
    assert small.files == {"new": 1, "skipped (too big)": 1}
    none = ingest1(repo, f"{site.base}/event/42/", materials="none")
    assert none.files == {"skipped (materials none)": 2}


def test_no_checksum(repo, site):
    a = site.attachment(5, 9, 77, "old.pdf", b"from before checksums", checksum=False)
    site.event(5, "Old meeting", "2010-01-01", files=[a])
    report = ingest1(repo, f"{site.base}/event/5/")
    assert report.files == {"new": 1}
    path = str(material(repo, tree.site_prefix(site.base), a).relative_to(repo))
    key = annex.lookupkeys(repo, [path])[path]
    assert key.startswith(f"MD5E-s{len(b'from before checksums')}--")


def test_protected(repo, site, meeting, tmp_path):
    event, slides, talk = meeting
    site.protected.add(42)
    refused = ingest1(repo, f"{site.base}/event/42/")
    assert refused.events == 0 and "42" in refused.denied

    helper = tmp_path / "fake-pass"
    helper.write_text(f"#!/bin/sh\n[ \"$1\" = indico/test ] && printf '%s\\nnotes\\n' {TOKEN}\n")
    helper.chmod(0o755)
    git(repo, "config", f"indico.{site.base}.tokencommand", str(helper))
    git(repo, "config", f"indico.{site.base}.tokenid", "indico/test")
    ok = ingest1(repo, f"{site.base}/event/42/")
    assert ok.events == 1 and ok.files == {"new": 2}
    git(repo, "commit", "-qm", "protected")
    path = str(material(repo, tree.site_prefix(site.base), talk).relative_to(repo))
    done = subprocess.run(["git", "annex", "get", path], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert (repo / path).read_bytes() == b"noise talk v1"
    assert TOKEN not in done.stdout + done.stderr


def test_token_sources(repo, site, tmp_path, monkeypatch):
    assert settings.site(site.base, cwd=repo).token is None
    file = tmp_path / "token"
    file.write_text(TOKEN + "\n")
    file.chmod(0o644)
    git(repo, "config", f"indico.{site.base}.tokenfile", str(file))
    s = settings.site(site.base, cwd=repo)
    assert s.token == TOKEN and s.warnings and "600" in s.warnings[0]
    file.chmod(0o600)
    assert not settings.site(site.base, cwd=repo).warnings
    monkeypatch.setenv(settings.env_name(site.base), "indp_from-env")
    assert settings.site(site.base, cwd=repo).token_source.startswith("$INDICO_TOKEN_127_0_0_1_")
    assert settings.site(site.base, token="indp_given", cwd=repo).token == "indp_given"
    assert TOKEN not in settings.mask(TOKEN) and settings.mask(TOKEN).startswith("indp_")


def test_cli_and_rerender(repo, site, meeting):
    done = subprocess.run(["git", "indico", "ingest", "-m", "ingest", f"{site.base}/event/42/"], cwd=repo,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "committed" in done.stdout
    prefix = repo / tree.site_prefix(site.base)
    (prefix / "event/42/index.html").write_text("spoiled")
    (prefix / "index.html").unlink()
    done = subprocess.run(["git", "indico", "html"], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "Group meeting" in (prefix / "event/42/index.html").read_text()
    assert (prefix / "index.html").exists()
    listed = subprocess.run(["git", "indico", "remotes"], cwd=repo, capture_output=True, text=True).stdout
    assert site.base in listed and "enabled" in listed


def test_clone_autoenables(repo, site, meeting, tmp_path):
    ingest1(repo, f"{site.base}/event/42/")
    git(repo, "commit", "-qm", "ingest")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    git(clone, "annex", "init", "-q", "clone")
    path = str(material(clone, tree.site_prefix(site.base), meeting[2]).relative_to(clone))
    git(clone, "annex", "get", path)
    assert (clone / path).read_bytes() == b"noise talk v1"


@pytest.mark.skipif(shutil.which("datalad") is None, reason="needs DataLad")
def test_datalad_dataset(site, meeting, tmp_path):
    ds = tmp_path / "ds"
    subprocess.run(["datalad", "create", str(ds)], check=True, capture_output=True)
    ingest1(ds, f"{site.base}/event/42/")
    subprocess.run(["datalad", "save", "-m", "ingest"], cwd=ds, check=True, capture_output=True)
    assert git(ds, "status", "--porcelain") == ""
    path = str(material(ds, tree.site_prefix(site.base), meeting[2]).relative_to(ds))
    subprocess.run(["datalad", "get", path], cwd=ds, check=True, capture_output=True)
    assert (ds / path).read_bytes() == b"noise talk v1"
    # the JSON and HTML are in git, not annexed, whatever the dataset's largefiles rule
    record = ds / tree.site_prefix(site.base) / "event/42/event.json"
    assert not record.is_symlink()
    siblings = subprocess.run(["datalad", "siblings"], cwd=ds, capture_output=True, text=True).stdout
    assert annex.remote_name(site.base) in siblings


def test_paths_and_keys():
    base = "https://indico.example.org"
    assert tree.url_path(base, f"{base}/event/1/attachments/2/3/a%20b.pdf") == PurePosixPath(
        "event/1/attachments/2/3/a b.pdf")
    assert tree.url_path(base, "https://elsewhere.org/event/1/") is None
    with pytest.raises(tree.TreeError):
        tree.url_path(base, f"{base}/event/1/attachments/2/3/%2E%2E")
    with pytest.raises(tree.TreeError):
        tree.url_path(base, f"{base}/event/1/attachments/2/3/a%2Fb")
    assert annex.key_for(5, "AB" * 16, "x.tar.gz") == f"MD5E-s5--{'ab' * 16}.tar.gz"
    assert annex.key_for(5, "ab" * 16, "slides.final-version.pdf") == f"MD5E-s5--{'ab' * 16}.pdf"
    assert annex.key_for(5, "ab" * 16, "README") == f"MD5E-s5--{'ab' * 16}"
    assert annex.remote_name("https://indico.bnl.gov") == "indico-bnl"
    assert annex.remote_name("https://indico.cern.ch") == "indico-cern"
    t = ingest.target("https://indico.fnal.gov/event/68034/contributions/308635/attachments/1/2/x.pdf")
    assert (t.base, t.kind, t.number) == ("https://indico.fnal.gov", "event", 68034)
    assert ingest.target("https://example.org/indico/category/5").base == "https://example.org/indico"
