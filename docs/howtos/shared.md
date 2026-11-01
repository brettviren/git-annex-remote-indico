# Share a site's dataset among the users of one computer

Several people on one computer often want the same Indico categories and
the same files. Instead of each keeping a copy, keep **one shared dataset
per site** on the computer, and give each person a **shared clone** of it.
A file anyone gets is downloaded and stored once, in the shared dataset, and
every clone links to it there.

```
/srv/datalad/indico/bnl          the shared dataset of indico.bnl.gov (group indico)
  .git/annex/objects/...         every file anyone has got, once
~alice/indico/bnl                Alice's shared clone: .git/annex -> /srv/datalad/indico/bnl/.git/annex
~bob/work/indico/bnl             Bob's, a subdataset of his dataset ~bob/work
```

It needs DataLad (a shared clone is DataLad's "ephemeral" clone) and the
shared dataset and the clones on one computer.

## Once per computer: a group and a place

An administrator makes a group for the people who share, and a directory
that passes its group on to what is made in it:

```
groupadd indico
usermod -aG indico alice          # and each other user; they log in again to be in it
install -d -g indico -m 2775 /srv/datalad/indico
```

Instead of a directory anyone in the group writes, one account (a `datalad`
user, say) may own the shared datasets; it must be in the group too.

## Once per site: the shared dataset

Whoever first wants a site (any member of the group, or the `datalad`
account) makes its shared dataset and ingests what the group wants:

```
git indico share /srv/datalad/indico/bnl
git -C /srv/datalad/indico/bnl indico ingest -m "BNL colloquia" \
    --since 2025-01-01 https://indico.bnl.gov/category/201/
```

`share` makes a DataLad dataset there (or takes the repository already
there) with `core.sharedRepository=group`, and gives the group what the
owner has on everything in it. `--group` names another group than the
directory's.

Ingest into the shared dataset, not into a clone: an ingest in a clone stays
in that clone (`ingest` warns you). In a shared dataset, ingest leaves what it
writes changeable by the group, whatever your umask. Anyone in the group may
ingest there, and a cron job may keep a few categories current:

```
0 6 * * *  git -C /srv/datalad/indico/bnl indico ingest -m "daily" --since 2025-01-01 https://indico.bnl.gov/category/201/
```

A site used by only some of you (a workshop's, say) is no different: whoever
needs it shares it, under the same directory.

If someone with a narrow umask leaves files the group can't change, the
owners of those files run `git indico share PATH` again: it only repairs.

## Once per person: a shared clone

```
git indico clone /srv/datalad/indico/bnl ~/indico/bnl
```

or, to have it in a dataset of your own:

```
git indico clone -d ~/work /srv/datalad/indico/bnl ~/work/indico/bnl
```

`clone` makes a DataLad clone with `--reckless ephemeral` and
`core.sharedRepository` set in the clone. That setting matters: files are
stored with the permissions of the clone that gets them, and without it
nobody else could read what you get. If the shared dataset belongs to
another user, `clone` adds it to git's `safe.directory` in your git config;
git refuses to work with another user's repository otherwise.

## Day to day

```
cd ~/indico/bnl
datalad update --how merge                         # what others have ingested
datalad get indico.bnl.gov/event/25818             # downloaded once, for everyone
xdg-open indico.bnl.gov/index.html
```

`datalad get` of a file someone already has is immediate: it is there.

## Things to know

- **Don't drop in a shared clone.** `datalad drop` there removes the file
  from the shared dataset, for everyone. Free space in the shared dataset
  itself (`git annex drop`); the site can always serve a file again.
- **What you get, the group can read**, and that includes what your own
  token gets. So a file the site gives only with a token (one of a
  protected event) is refused in a shared dataset or clone:

  ```
  get ... failed: https://indico.bnl.gov/event/.../talk.pdf is protected on the site,
  and what is got here others may read (core.sharedRepository is group); ...
  ```

  Get such files in a dataset of your own. If everyone in the group may see
  a site's protected material, allow it:

  ```
  git config --global indico.https://indico.bnl.gov.shareprotected true
  ```

- **The shared dataset doesn't know what clones got.** Its location
  tracking lists files got through clones only after a check, which a cron
  job can run:

  ```
  git -C /srv/datalad/indico/bnl annex fsck --fast
  ```

- **Each computer has its own.** The shared dataset is on one computer's
  storage. Another computer has its own shared dataset of the same site;
  the two can be siblings of each other (`datalad siblings add`) like any
  datasets.

## Turning an existing dataset into a shared one

If you made the shared dataset and clones by hand, run `git indico share`
on the dataset as its owner (it sets `core.sharedRepository` and fixes
permissions), and in each clone made by hand:

```
git -C ~/indico/bnl config core.sharedRepository group
```
