# Indico in a DataLad dataset

Every command is run from the dataset's top directory.

## 1. Make the dataset, or use one you have

```
datalad create -c text2git talks
cd talks
```

`text2git` isn't required. `git indico` keeps its JSON and HTML in git
whatever the dataset's rules say, and annexes only the attached files.

## 2. Ingest, recorded with `datalad run`

```
datalad run -m "BNL colloquia, 2025" \
  "git indico ingest --since 2025-01-01 https://indico.bnl.gov/category/201/"
```

The first ingest from a site adds the site as a sibling, a read-only
special remote named `indico-bnl`. `datalad run` commits the result, with
the command in the commit message so it can be rerun. A plain
`git indico ingest URL` followed by `datalad save -m ...` works too, but
doesn't record the command.

What a URL takes:

| URL | takes |
|---|---|
| `https://indico.bnl.gov/category/201/` | the category, its events (and those of the categories below it) between `--since` and `--until`, and their files |
| `https://indico.bnl.gov/event/27078/` | the event, its timetable and its files |
| a contribution's or a file's URL | the event it is in |

Useful options:
- `--no-events`: a category's listing only;
- `--type pdf`: only some kinds of file;
- `--max-size 100M`;
- `--materials none`: records and pages only;
- `-n`: a dry run.

## 3. Look

```
datalad siblings                       # here, and indico-bnl [indico]
datalad status
git log --oneline
xdg-open indico.bnl.gov/index.html     # browse; links work for files you have got
```

## 4. Get and drop files

The files are registered, not downloaded:

```
datalad get indico.bnl.gov/event/27078          # one event's files
datalad get -J4 indico.bnl.gov                  # everything, four at a time
datalad drop indico.bnl.gov/event/27078         # free the space; the site still has them
```

Before dropping, `datalad drop` checks that the site still has the same
bytes, by the checksum the site gives now. If someone has since replaced
the file on Indico, that check fails and the drop is refused, so you never
lose the only copy of the old version.

## 5. Refresh

```
datalad rerun                    # the last run again: new events, changed records, re-uploaded files
datalad rerun --since=HEAD~5     # every run in the last five commits
git log -p -- indico.bnl.gov/event/27078/event.json     # what changed on the site, and when it was seen
```

A file uploaded again on Indico keeps its path and gets a new key, so the
rerun's commit shows it as modified. The old version stays in history; get
it from a clone that has it, or keep it before refreshing (`datalad get`).

## 6. Clone

```
datalad clone /path/to/talks talks2      # or a URL
cd talks2
datalad get indico.bnl.gov/event/27078
```

The Indico sibling is enabled in the clone by itself (`autoenable=true`).
The clone needs `git-annex-remote-indico` installed, plus your own token for
protected material (see [tokens](tokens.md)). Tokens are never stored in the
dataset.

## Two sites, one dataset

```
datalad run -m "FNAL PAC" "git indico ingest https://indico.fnal.gov/event/68034/"
```

Each site has its own tree (`indico.fnal.gov/`) and its own sibling
(`indico-fnal`).
