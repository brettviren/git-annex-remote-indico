# SPDX-FileCopyrightText: 2026 Brett Viren <brett.viren@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""git-annex-remote-indico: Indico sites as a git-annex special remote.

``git-annex-remote-indico`` run by git-annex (with no arguments) speaks the
external special remote protocol: it claims the URLs of an Indico site and
downloads them, with the user's token.  Run by a person, it has commands;
``ingest`` walks Indico (a category, an event ...) into a tree of the
repository.  See README.md.

Modules: ``client`` (the HTTP API), ``settings`` (git config and tokens),
``tree`` (where things go in the repository, and the JSON and HTML written
for them), ``repo`` (git and git-annex), ``ingest`` (walking a site into
the tree), ``remote`` (the special remote), ``cli``.
"""

__version__ = "0.1.0"
