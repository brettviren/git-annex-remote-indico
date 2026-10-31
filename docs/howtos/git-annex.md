# Plain git-annex

The same as with DataLad, in any git-annex repository:

```
git init talks && cd talks && git annex init
git indico ingest https://indico.bnl.gov/event/27078/ -m "BNL colloquium"   # -m commits; without it, changes are staged
git annex get indico.bnl.gov/event/27078
git annex drop indico.bnl.gov/event/27078
git annex whereis indico.bnl.gov/event/27078       # the URLs on indico-bnl
git annex fsck --from indico-bnl --fast            # does the site still have what we registered?
git indico remotes                                 # the Indico remotes, and their sites
git indico html                                    # make the HTML pages again from the JSON
```

To make a site's remote yourself, with your own name for it:

```
git annex initremote bnl type=external externaltype=indico encryption=none autoenable=true url=https://indico.bnl.gov
git indico ingest --remote bnl https://indico.bnl.gov/event/27078/
```
