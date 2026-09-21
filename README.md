# codebase-oracle

**The timeline is the truth.** Current state is a fold over events, not a stored
snapshot — so "what did we believe on 2026-09-18" becomes answerable, not just "what is
true now."

Git keeps *decisions* and throws away *deliberation*. A commit reading
`fix progress counter` records what won; it does not record that the counter was
counting imported rather than scanned files, that it therefore reported a 17-hour ETA
for a 250-second run, or which four other theories were ruled out first. This indexes
both halves into one log.

> Status: **v1, the indexer.** It reads. The session join that answers *why* is v2 —
> see [Roadmap](#roadmap).

## Per codebase, not per machine

You run `cbo` *inside* a codebase and it indexes that codebase. There is no global
index, no central root, no configuration naming which repositories exist. Every
codebase can carry its own oracle.

The store lives in `<codebase>/.codebase-oracle/` and registers itself in that repo's
`.git/info/exclude` — machine-local, never a foreign repo's committed `.gitignore`.
ripgrep honours the same file, so the store is invisible to both `git status` and search.

## Units: a codebase is not one history

`git log` in a superproject contains **no** submodule commits. A commit that moves a
submodule pointer stores a 40-byte gitlink, not the upstream work. So every git history
is a `unit` — `.` for the superproject, its path for each submodule — and the gitlink
change becomes its own event kind:

```
submodule-bump   vendor/libfoo   a1b2c3d4…  →  e5f6a7b8…
```

That is the join between two timelines: *which of our commits pulled in which upstream
change*. An uninitialised submodule has no local objects, so it reports
`indexed: false` rather than zero events — "cannot be read" and "is empty" are
different answers and only one of them is true.

Vendored libraries and monorepo packages are not units. They have no separate history,
so they are just paths.

## Author is three fields, because one of them lies

`git log` reports the committer's configured identity for every commit, including the
ones an agent wrote. The model appears only in the `Co-Authored-By` trailer. So events
carry `author_human`, `author_agent`, and `author_model`, and a trailer naming a person
rather than a known agent is left as a human co-author instead of being promoted to a
model.

## Install

```bash
uv sync
uv run cbo --help
```

## Use

```bash
cd ~/some/codebase

cbo index                 # walk every unit, fill from the watermark
cbo index --no-gh         # git only, no GitHub calls
cbo index --full          # ignore watermarks and re-read

cbo status                # units, counts per kind, and staleness
cbo search "progress counter"
cbo timeline --since 2026-09-16 --until 2026-09-19
cbo show git:.:a1b2c3d4…
```

`status` reports staleness on purpose. A stale hit that looks exactly like a fresh one
is a real bug in a real indexer, filed as `agents-relic#43`; not having the field is how
you get it.

Re-running `index` is safe and cheap: every id is content-addressed
(`git:<unit>:<sha>:<path>`), so a second pass rewrites identical rows rather than
duplicating them, and a lost or corrupt watermark costs time instead of correctness.

## Search is lexical, and that is a measurement

No embeddings. On 200 queries over 3,000 documents, FTS with ICU tokenisation scored
MRR **0.890** against **0.600** for `multilingual-e5-small`; on Thai, lexical beat every
vector model by **2.5x**; RRF fusion made results *worse* (0.890 → 0.822); and embedding
the query consumed 36.7s of a 37.4s search. Commit subjects and issue titles are shorter
and more known-item than prose, so the gap only widens here.

ICU rather than the default tokenizer because Thai has no spaces — `simple` turns a
whole Thai sentence into one token, and a measured sample found 1 of 12 substrings
against ICU's 11. Stemming is off because this is a code corpus and the English stemmer
mangles identifiers (`structured_output_mode` → `..._mod`).

## Roadmap

| | |
|---|---|
| **v1** | `index` `status` `search` `timeline` `show` — this |
| **v2** | `blame` (line → commit → the session that reasoned about it), `gap` (decided but never built) |
| **v3** | `ask` — retrieval over a time *window*, not top-k, with mandatory event-id citations |

v2 is held back deliberately. It reads a second index, and if the git half has a
timestamp or id bug you cannot tell whether the join is lying or the index is.

## Licence

MIT
