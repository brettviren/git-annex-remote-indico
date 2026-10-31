# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The commands: ``git-annex-remote-indico`` and its other name, ``git indico``.

Run by git-annex, with no arguments and the protocol on its standard input,
``git-annex-remote-indico`` is the special remote.  Otherwise it is a
program with commands, as ``git indico`` always is.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import click

from . import __version__


def main() -> None:
    if len(sys.argv) == 1 and not sys.stdin.isatty():
        from .remote import main as remote
        remote()
        return
    cli(prog_name="git-annex-remote-indico")


def git_indico() -> None:
    cli(prog_name="git indico")


def _size(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKMGT]?)B?\s*", text)
    if not m:
        raise click.BadParameter(f"{text!r}: a size such as 50M or 2G")
    return int(float(m.group(1)) * 1000 ** " kMGT".index(m.group(2).upper() or " "))


def _fail(exc: Exception) -> None:
    raise click.ClickException(str(exc))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
@click.option("-v", "--verbose", count=True, help="Say more (-vv: every request).")
def cli(verbose: int) -> None:
    """Indico sites in a git-annex repository (or DataLad dataset).

    `ingest` brings categories and events of a site into the repository: their
    records as JSON, pages to browse them as HTML, and their files annexed,
    to be got with `git annex get` (or `datalad get`) from the site's special remote.
    """
    logging.basicConfig(level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(verbose, 2)],
                        format="%(levelname)s: %(message)s")


@cli.command()
@click.argument("urls", nargs=-1, required=True)
@click.option("-C", "where", type=click.Path(file_okay=False, path_type=Path), default=".",
              help="The repository (default: the one here).")
@click.option("--prefix", help="Where in the repository the site's tree goes (default: the site's host).")
@click.option("--remote", help="The name of the site's special remote (default: indico-HOST; made if missing).")
@click.option("--since", type=click.DateTime(["%Y-%m-%d"]), help="For a category: events from this day.")
@click.option("--until", type=click.DateTime(["%Y-%m-%d"]), help="For a category: events to this day.")
@click.option("--events/--no-events", default=True, show_default=True,
              help="For a category: also each event listed (else only the listing).")
@click.option("--materials", type=click.Choice(["all", "none"]), default="all", show_default=True,
              help="Which files to register.")
@click.option("--type", "types", multiple=True, help="Only files of this kind: an extension (pdf) or media type. Repeatable.")
@click.option("--max-size", help="No file bigger than this (such as 200M).")
@click.option("-n", "--dry-run", is_flag=True, help="Ask the site, say what would change, change nothing.")
@click.option("-m", "--message", help="Commit with this message (else the changes are only staged).")
@click.option("--token", help="The site's token (better: indico.tokencommand; see `git indico check`).")
def ingest(urls, where, prefix, remote, since, until, events, materials, types, max_size, dry_run, message, token):
    """Bring what Indico URLs name into the repository.

    A URL of a category takes the category and the events listed in it (and
    in the categories below it), between --since and --until; a URL of an
    event, or of anything in one (a contribution, a file), takes the event.
    Files are registered, not downloaded: get them with `git annex get`.

    \b
      git indico ingest https://indico.bnl.gov/event/27078/
      git indico ingest --since 2025-01-01 https://indico.bnl.gov/category/201/ -m "BNL colloquia"
    """
    from .ingest import IngestError, Options, ingest as run
    from .repo import RepoError
    from .settings import SettingsError
    options = Options(prefix=prefix, remote=remote, token=token, since=since and since.date(),
                      until=until and until.date(), events=events, materials=materials,
                      types=tuple(t.lstrip(".") for t in types), max_size=_size(max_size), dry_run=dry_run)
    try:
        reports = run(where, list(urls), options, progress=lambda m: click.echo(m, err=True))
        for r in reports:
            click.echo(("(dry run) " if dry_run else "") + r.summary())
            for w in r.warnings:
                click.echo(f"warning: {w}", err=True)
        if message and not dry_run:
            from . import repo
            if repo.commit(repo.toplevel(where), message):
                click.echo("committed")
        elif not dry_run:
            click.echo("staged; commit with git commit, or datalad save")
    except (IngestError, RepoError, SettingsError) as exc:
        _fail(exc)


@cli.command()
@click.argument("prefixes", nargs=-1)
@click.option("-C", "where", type=click.Path(file_okay=False, path_type=Path), default=".",
              help="The repository (default: the one here).")
def html(prefixes, where):
    """Make the HTML pages of the sites' trees again, from their JSON.

    PREFIXES are the trees (such as indico.bnl.gov); default: every one there is.
    """
    from .ingest import IngestError, rerender
    from .repo import RepoError
    try:
        written = rerender(where, list(prefixes))
    except (IngestError, RepoError) as exc:
        _fail(exc)
    click.echo(f"{len(written)} pages written")


@cli.command()
@click.argument("url")
@click.option("--token", help="A token to try in place of the configured one.")
def check(url, token):
    """Whether a site answers, and who the token says you are.

    \b
    The token is looked for in, first found wins:
      --token;
      $INDICO_TOKEN_<HOST> (such as INDICO_TOKEN_INDICO_BNL_GOV);
      git config indico.<url>.tokencommand (+ tokenid): its first line of output (recommended, with pass);
      git config indico.<url>.tokenfile: its first line (keep it chmod 600).
    For example:
      git config --global indico.https://indico.bnl.gov.tokencommand pass
      git config --global indico.https://indico.bnl.gov.tokenid gov/bnl/indico/api/token
      git config --global indico.https://indico.fnal.gov.useragent curl/8.5.0
    """
    from . import settings
    from .client import IndicoError
    try:
        from .ingest import IngestError, target as parse
        try:
            base = parse(url).base
        except IngestError:
            base = settings.normalize(url)
        site = settings.site(base, token=token)
    except settings.SettingsError as exc:
        _fail(exc)
    click.echo(f"site: {site.url}")
    click.echo(f"token: {settings.mask(site.token)} from {site.token_source}")
    for w in site.warnings:
        click.echo(f"warning: {w}", err=True)
    if site.user_agent:
        click.echo(f"user agent: {site.user_agent}")
    client = site.client(retries=1)
    try:
        client.get("export/categ/0.json", detail="events", limit=1)
        user = client.who() if client.authenticated else None
    except IndicoError as exc:
        _fail(exc)
    if not client.authenticated:
        click.echo("answers; no token, so only what is public")
    elif not user:
        click.echo("answers, but the token identifies no one (expired? wrong site?)")
    else:
        name = user.get("full_name") or " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
        click.echo(f"answers; signed in as {name or '?'} ({user.get('email') or user.get('id')})")


@cli.command()
@click.option("-C", "where", type=click.Path(file_okay=False, path_type=Path), default=".",
              help="The repository (default: the one here).")
def remotes(where):
    """The Indico remotes of the repository, and their sites."""
    from . import repo
    try:
        found = repo.remotes(repo.toplevel(where))
    except repo.RepoError as exc:
        _fail(exc)
    for r in found:
        click.echo(f"{r.name}\t{r.url}\t{'enabled' if r.enabled else 'not enabled here'}\t{r.uuid}")
