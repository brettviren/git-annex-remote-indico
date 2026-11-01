# git-annex-remote-indico

Indico sites (indico.bnl.gov, indico.fnal.gov, ...) as a git-annex special
remote, so that a git-annex repository or a DataLad dataset can hold
categories, events and their presentation materials, and track them as
they change.

For now it only *ingests*: it brings things from Indico into the
repository. It doesn't publish to Indico.

```
git indico ingest https://indico.bnl.gov/event/27078/
git indico ingest --since 2025-01-01 https://indico.bnl.gov/category/201/ -m "BNL colloquia"
git annex get indico.bnl.gov/event/27078          # or: datalad get ...
```

How-tos: [docs/howtos](docs/howtos/README.md), including
[Indico in a DataLad dataset](docs/howtos/datalad.md),
[sharing a site among the users of a computer](docs/howtos/shared.md) and
[tokens, and what to do about a 403](docs/howtos/tokens.md).

## Install

```
uv tool install git+https://github.com/brettviren/git-annex-remote-indico   # or: pipx install git+https://...
```

This installs two programs, which must be on `PATH`:

- `git-annex-remote-indico`, the special remote that git-annex runs;
- `git-indico`, its commands, which are also there as `git indico`.

It needs git-annex (and DataLad, if you use datasets).

## What goes where

One site's tree, below its host name, follows the paths of the site's own
URLs, so a URL and a path name the same thing:

```
indico.bnl.gov/
  index.html                                     the categories and events here
  category/201/category.json                     the category, and the events listed in it
  category/201/index.html
  event/27078/event.json                         the event, its timetable and its files, as the API gives them
  event/27078/index.html
  event/27078/attachments/60575/103996/talk.pdf  annexed
  event/27078/contributions/103544/attachments/60072/103186/slides.pdf
```

**Materials** are the files attached to an event, a session, a contribution
or a subcontribution. They are annexed, and **registered rather than
downloaded**. The site gives each file's size and MD5, which is all
git-annex needs to make its key (`MD5E-s…--….pdf`), and the URL is
recorded as where the site's remote can get it. `git annex get` downloads
it, and git-annex checks it against the key.

**Taxa** are categories and events. Each is kept as two text files in git
(never annexed, whatever `annex.largefiles` says):

- the JSON record the API gave (`event.json`, `category.json`);
- an `index.html` made from that record, for people to read.

The HTML links to what is in the repository by relative paths, and to the
site for anything that isn't. That includes links inside descriptions. Open
`indico.bnl.gov/index.html` in a browser, or serve the tree. Files not yet
got show as broken links until you `git annex get` them.
`git indico html` remakes every page from the JSON.

**Changes are commits.** A file uploaded to the site again keeps its URL,
so it keeps its path in the repository but gets a new key. Ingesting again
records that as a change of the annexed file. Every ingest also updates
`event.json` (and so the page), so `git log` shows what changed on the site
and when it was seen. When a file is replaced:

- the old key's URL is unregistered;
- the old key is marked as no longer on the site;
- older commits still name the old key, but nothing will try to get it from
  the site.

A category is listed a range of days at a time (`--since`, `--until`).
Events listed by earlier ingests stay listed. `--no-events` takes only the
listing, without the events themselves.

Nothing is committed unless you give `-m`. Otherwise the changes are staged
for `git commit` or `datalad save`. In a dataset, the following also records
how the files came to be:

```
datalad run -m "BNL colloquia" git indico ingest https://indico.bnl.gov/category/201/
```

## The remote

`ingest` makes one special remote per site if there isn't one yet. It's
named `indico-<host>`, for example `indico-bnl` or `indico-fnal`, and is
created like this:

```
git annex initremote indico-bnl type=external externaltype=indico \
    encryption=none autoenable=true url=https://indico.bnl.gov
```

It is read-only. It claims its site's URLs and downloads them with your
token. Because of `autoenable=true`, a clone of the repository (or
`datalad clone` of the dataset) enables it by itself, so `get` works there
too, provided `git-annex-remote-indico` is installed. `datalad siblings`
lists it.

When asked whether the site still has a key (`git annex fsck --from
indico-bnl`, `checkpresentkey`), the remote compares the key's MD5 with the
checksum the site gives for that URL now. A URL that still answers after
the file was replaced is not mistaken for the old content.

## Shared datasets

On a computer with several users, one dataset of a site can serve them all,
so each file is downloaded and stored once
([how-to](docs/howtos/shared.md)):

```
git indico share /srv/datalad/indico/bnl                 # a dataset shared by a group (core.sharedRepository=group)
git indico clone /srv/datalad/indico/bnl ~/indico/bnl    # yours: an ephemeral DataLad clone using its annex
```

What is got in a shared dataset or clone, the group can read. So the remote
refuses there a file the site gives only with a token, unless
`indico.shareprotected` is true for the site.

## Tokens and settings

Settings are per user, in git config, and can be given per site, the way
git's own `http.<url>.*` settings are:

```
git config --global indico.https://indico.bnl.gov.tokencommand pass
git config --global indico.https://indico.bnl.gov.tokenid gov/bnl/indico/api/token
git config --global indico.https://indico.fnal.gov.tokencommand pass
git config --global indico.https://indico.fnal.gov.tokenid gov/fnal/indico/api/token
git config --global indico.https://indico.fnal.gov.useragent curl/8.5.0
git indico check https://indico.fnal.gov                # does it answer, and who am I
```

The token, first found wins:

1. `--token` on the command line;
2. `$INDICO_TOKEN_<HOST>`, for example `INDICO_TOKEN_INDICO_BNL_GOV`;
3. `indico.tokencommand` (plus `indico.tokenid` as its argument): the first
   line of its output. **Recommended**, with `pass`: the token is never
   written down in the clear;
4. `indico.tokenfile`: its first line. You're warned if the file is more
   open than mode 600.

**Scopes.** Make a token at `https://SITE/user/tokens/`. Reading the
records of protected events needs the scope `read:legacy_api`. Downloading
the files of protected events, and category pages, go through the site's
web pages, so they need `read:everything`. A token that lacks the scope is
refused even for public pages, so such requests are retried without it.
Public events need no token at all. A token is
never sent to another host (not even on a redirect), and never printed.

Other settings:

- `indico.useragent`: FNAL's site sits behind Cloudflare, which refuses
  user agents it doesn't know (`curl/8.5.0` gets through);
- `indico.pause`: seconds between requests;
- `indico.shareprotected`: get the files of protected events in shared
  datasets too (see above).

## Not (yet) done

- **Publishing to Indico.** git-annex's import/export interface for external
  remotes is still a draft, and Indico has no documented API for uploading.
- **The legacy API key with a signature.** Use a personal token instead.
- **A download cache across shared datasets.** Shared clones already store
  each file once per computer (see above).
- **Notes (minutes) as separate files.** They are in `event.json` and shown
  on the page.

## Development

```
uv sync
uv run pytest          # a fake Indico site on localhost, real git-annex repositories (and DataLad, if installed)
```

Parts of the client are ported from orgface's Indico slurper.
