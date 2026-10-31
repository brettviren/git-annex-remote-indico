# Tokens and site settings

## Set up

1. On the site, make a personal token at `https://SITE/user/tokens/`, for
   example https://indico.bnl.gov/user/tokens/. Give it the scopes
   **`read:legacy_api`** (records of protected events) and
   **`read:everything`** (files of protected events, and category pages).
2. Keep the token in `pass`, and tell git where it is:

   ```
   pass insert gov/bnl/indico/api/token
   git config --global indico.https://indico.bnl.gov.tokencommand pass
   git config --global indico.https://indico.bnl.gov.tokenid gov/bnl/indico/api/token
   ```

3. Check it:

   ```
   git indico check https://indico.bnl.gov
   ```

   It prints where the token came from (never the token itself), and who
   the site says you are.

For FNAL, whose site is behind Cloudflare, also set:

```
git config --global indico.https://indico.fnal.gov.useragent curl/8.5.0
```

Other places a token can come from are `--token`,
`$INDICO_TOKEN_INDICO_BNL_GOV` and `indico.tokenfile`. Keep any token file
chmod 600; you're warned if it is more open. See the README.

## When something is refused

| message | what it means | what to do |
|---|---|---|
| `403 ... requires higher privileges than provided by the access token` | the token lacks a scope for this request. Public pages and files are then fetched without the token, so this shows only for protected material | make a token with `read:everything` too |
| `403 ... not in the site's own words: from cloudflare Just a moment...` | a gateway refuses this program's user agent | set `indico.useragent` as above |
| `events refused: 1234` in the summary | the event is protected, and the token can't see it, or there is no token | `git indico check`; ask the event's managers for access |
| `indico.tokencommand ...: Error: ... is not in the password store` | `pass` couldn't find the entry | check the `tokenid`; run `pass show ID` yourself (gpg may need your passphrase first) |
| `answers, but the token identifies no one` (from `check`) | the token has expired or been revoked, lacks `read:user`, or is for another site | make a new one |

`git annex get` runs the remote, which reads the same git config, so these
apply to downloads too. Add `--debug` to `git annex get` to see the remote's
messages.
