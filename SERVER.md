# mdc as a service on the commons box

A plan, not yet executed (written 2026-10-24). It moves all of mdc's
functionality onto the server and leaves the Mac holding only the files Rodrigo
edits. It sits inside the site plan, `~/src/rvanegas-site/SITE.md`: mdc becomes
one more service on the `commons` box behind Caddy, at `mdc.rvanegas.co`.

The goal: one API that every client uses.

- **Rodrigo** keeps editing plain markdown in their editor and typing `mdc …` in
  the terminal. The local `mdc` becomes a client of the API: it syncs their
  files to their libraries on the server, runs the command there, streams the output, and
  writes changed files back. No model API key and no model call stays on the
  Mac.
- **Naive users** never touch a terminal or a local file. A richer client
  (later) drives the same API against their server-side libraries.

The server is canonical. The Mac's tree is a working copy for editing. There
is no separate sync tool (Mutagen and rsync were considered); syncing is part
of the API, since clients that don't edit locally need file endpoints anyway.

---

## Principle: one entry point per service

Every client reaches a service only through its HTTP API: mdc (its own CLI
included), Roxana, Noesis until it is retired, and scheduled jobs. When a client
needs something, expand the API; never bypass it. No shelling out to another
project's CLI, no importing its modules, no `docker exec` jobs. Operator tasks
(migrations, smoke tests, backups, debugging) and a service's own internals are
exempt.

Dianoia's CLI stays. mdc stops using it, and the streamlining that follows
Noesis's retirement may return to it.

## What exists (found 2026-10-24)

| Kind | Where |
|---|---|
| Documents | `~/Documents/Writing/Chats` (`library_path`): transcripts, `.document/.chat/.argument/.analysis/.review.md` companions, numbered revisions, assets linked by relative path, `MANIFEST.md`, `INDEX.md`, `KEYS.md`, `SYSTEM_PROMPT.md` |
| Config | `~/.config/mdc/`: `config.toml` (API keys inline), `system.md`, `review/` prompts |
| State | `~/.local/state/mdc/`: `library-manifest.json` (stores the absolute `library_path`; entries are relative), `library-index.json`, `library-relations.json`, stale `review-*.json~/.bak` |
| Cache | `~/.cache/mdc/anthropic-asset-cache.json` (remote file IDs; carries over) |
| Secrets | Anthropic, OpenAI, Roxana AppSync key, in `config.toml` or env |
| External tools | `dianoia` CLI as a subprocess (`dianoia_client.py`); `pandoc` + `xelatex`; `diff`; Ollama at `localhost:11434` |
| Local-only UX | `$MDC_EDITOR`/`$EDITOR` (`cli.py`), `open` on PDFs (`cmd_pdf.py`), the Emacs and Sublime plugins shelling out to `mdc` and reverting the buffer |

**Correction to SITE.md:** mdc does not call dianoia over HTTP today. It runs
the `dianoia` CLI as a subprocess for `extract`, `audit` and `evaluate`. Phase 0
below makes SITE.md's description true.

## Order, alongside SITE.md

One phase at a time, nothing in parallel (decided 2026-10-25; SITE.md,
*Alongside mdc's `SERVER.md`*, holds the authoritative list):

mdc Phase 0 → SITE Phase 1 → SITE Phase 2 (`commons` + Caddy) → SITE Phase 3
→ SITE Phase 5 → SITE Phase 4 (without Roxana's Gen 2) → mdc Phase 1 → mdc
Phase 2 → mdc Phase 3.

mdc Phase 0 depends on no box and is assumed by SITE Phase 4. mdc Phases 1–3
wait for the SITE work to finish; mdc still moves only once, and SITE Phase
4's mdc items (its own AppSync key, the fixture test of `dianoia_client.py`)
land in the CLI first, so its server `.env` starts with them.

---

## Phase 0 — mdc calls dianoia over HTTPS — done 2026-10-24

**Landed:** dianoia `405a5a2` (deployed on `Ubuntu-3` with `MDC_TOKEN` in
`.env`) and mdc `ab1f935`. **Verified** in production: without a token
`/argument/extract` returns 401, and with mdc's token and an empty body it
returns 422. A live `mdc argue` and `mdc analyze` on a throwaway document
extracted the argument, ran all six agents on proposition 4 alone, and wrote
the analysis.

- **dianoia:** two new routes in `src/api/argument.py`, alongside the existing
  `POST /argument/audit`:
  - `POST /argument/extract` `{text, max_props}` → `extract_argument`.
  - `POST /argument/evaluate` `{argument}` → starts the agents
    (`coordinator.react_to_user_argument_change`, as `cli/argue.py:cmd_evaluate`
    does) and returns `{conversation_id, snapshot_id}`. The client polls the
    existing `GET /agents/results`, whose `tasks_complete` comes from the same
    `coordinator.are_conversation_tasks_complete` the CLI checks.
- **A service token now, not in SITE Phase 4.** The new routes spend model
  tokens, so they require `Authorization: Bearer <token>`, checked by a FastAPI
  dependency against `MDC_TOKEN` in dianoia's `.env`. SITE Phase 4 extends the
  same dependency to every route and adds Cognito for Roxana.
- **mdc:** `dianoia_client.py` changes from subprocess to HTTP, and
  `extract`/`audit`/`evaluate` keep their signatures so `cmd_argue`,
  `cmd_analyze` and `cmd_audit` don't change. `--step` filtering moves client
  side (keep the step and its justifiers). It prints "<agent> done" as results
  arrive, as it does today. New config keys `dianoia_api_url`
  (`https://dianoia.rvanegas.com/api` until `commons`, then
  `https://dianoia.rvanegas.co/api`) and `dianoia_token`, with the env fallbacks
  `DIANOIA_API_URL` and `DIANOIA_TOKEN`. The "install dianoia" hint goes away.
- **Caveat:** dianoia keeps agent state in memory. A dianoia restart during an
  evaluation loses that run.

## Where things live (decided 2026-10-26)

Several users, each with several libraries. A library's id is
`<user>/<library>`, the first being `rodvandur/philosophy`
(`~/Documents/Writing/Chats` on the Mac). Nothing is copied per user: what
looked per user turned out to be system-wide, per library, or the client's to
say.

| Scope | What | Where |
|---|---|---|
| System | keys | the box's `~/mdc/.env` |
| System | model, `index_model`, `llm_names`, `wrap_width`, service URLs, base system prompt, review prompts | `etc/config.toml`, baked into the image at `/etc/mdc` (`MDC_CONFIG_DIR`); code defaults |
| System | asset cache (remote file IDs belong to the API key) | `/data/cache` (`MDC_CACHE_DIR`) |
| User | bearer token | `/data/users.toml` (Phase 2) |
| Run | the user's names as transcript headings, beside `Prompt`, which is everyone's | sent by the client: the CLI from its `user_names`, The Floor from the user's display name; `MDC_USER_NAMES` |
| Run | which library | `--lib` (sets `MDC_LIBRARY`) |
| Library | `SYSTEM_PROMPT.md` (absorbs the old per-user `system.md`), `KEYS.md`, the documents | its tree, edited and synced |
| Library | index state: `manifest.json`, `index.json`, `relations.json` | its `.mdc/`, server-owned, never synced |

```
/data/                                 /srv/mdc on the box
  users.toml                           Phase 2
  cache/
  libraries/rodvandur/philosophy/      the id is the path
    SYSTEM_PROMPT.md  KEYS.md  2023/ … 2026/  REVISIONS/  ASSESSMENTS/
    .mdc/
```

`user_names` cannot be derived from the documents: tried against the 1,290
`.md` files of `rodvandur/philosophy`, "every non-LLM heading is the user"
stopped 10 transcripts with no AI turn from being transcripts, and labeled
third parties (Goff, Isabella, John, Nannick) and `Context` sections as the
user in 34 more.

Made on mdc branch `library-layout`: `config.py` reads `MDC_CONFIG_DIR`,
`MDC_CACHE_DIR`, `MDC_LIBRARY`, `MDC_USER_NAMES` and `ROXANA_WEB_URL`; state
moves into `<library>/.mdc/`; `--lib` reaches every command (it used to miss
`reply`'s `REVISIONS/` and title lookup and the library's `SYSTEM_PROMPT.md`);
`system.md`, `system_prompt_file` and the state directory go; the finished
`review-*.json` migration goes with it. Data migration:
`scripts/migrate_library.py`.

## Library layout (decided 2026-10-26)

Names are unique across a library (checked: no basename repeats outside
`REVISIONS/`), so a directory carries nothing a name does not.

- **A dated file lives in the year directory its name gives:**
  `2026/2026-06-29-the-eleatic-critique.md`, its companions, review and PDF
  beside it. `REVISIONS/`, `ASSESSMENTS/` and `.mdc/` are kept apart by what
  they are. `check` reports a misplaced file; `fix` moves it with everything
  sharing its stem and carries its index state (`library.move_documents`), so
  a move is never reindexed.
- **Links name assets bare** and resolve across the library, beside the
  transcript first (`assets._AssetScope`); an asset must stay within the
  library. A link with a directory resolves from the transcript as written.
- **Every dated file names its role:** `<date>-<slug>.<role>.<ext>`, the role
  one of `chat` (a transcript), `document` (the user's prose), `source`
  (third-party material, any format), or mdc's `argument`, `analysis`,
  `review`. Another format keeps the role (`x.chat.pdf`). A bare dated file
  is an error; the indexer selects and labels voice by role, skipping a
  writing session's chat for its document.
- **Sources are dated by the earliest document linking them:**
  `parfit2011.pdf` → `2026-10-19-parfit2011.source.pdf`. An upload nothing
  links to takes the date it was added.
- **The asset cache is keyed by content** (`mdc/asset_cache.py`): a copy,
  sync, rename or move is the same uploaded file. Seeding copies the cache
  as it is.
- **Folders that grouped documents** become Related entries. A Related title
  must precede its document (`validate`), so the `_phil184` seminar documents
  each relate the members before them, and the `mmaa` folder (sources for
  *Money and Marriage by Authoritative Assertion*, which does not reference
  them) becomes a hub document dated by its last source. `_private` and
  `_published` are dropped: their files move up to their years.
- **Anthropic alone:** OpenAI and Ollama are retired (2026-10-26). `## GPT`
  turns of older transcripts are still read as the model's voice.

`scripts/migrate_layout.py` does this to `rodvandur/philosophy` (plan by
default, `--apply` to make it). Rehearsed on a copy: 5 editor backups to the
Trash, 6 sources named, 602 bare transcripts → `.chat.md`, 23 → `.document.md`,
35 PDFs → `.chat.pdf`; 924 files renamed or moved, and 37 `REVISIONS/`
backups renamed with their documents so `mdc diff` still finds them. Every
reference to a moved or renamed file is rewritten in place: 18,850 paths in
the generated INDEX/RELATED/REFERENCES/MANIFEST (some escaped, `2023/\_private/…`),
and the links and `[asset: …]` lines of documents and backups; a scan of the
result finds no old name or path left. All 631 index entries carried, and the
following `mdc index` summarizes 11 documents, the ones the migration writes.
A second run finds nothing to do. Before it, 2026-10-26: the
tension drafts, `_phil184/conversations.zip` and the unlinked
`2026-07-28-civilizational-conflict.pdf` went to the Trash, and
`condensed-systematicity.{wav,pdf}` out to `~/Documents/Writing`.

## Phase 1 — mdc on `commons` — done 2026-10-26

**Deployed** master `0254d78` to `commons`, answering at
`https://mdc.rvanegas.co/healthz`: the image (`Dockerfile`,
`docker-compose.yml`, `etc/config.toml`), `bin/deploy`, `mdc serve` with
`GET /healthz`, and the Caddy block (rvanegas-site master, installed
2026-10-26; the old Caddyfile kept as `/etc/caddy/Caddyfile.bak-2026-10-26`).
The `mdc` A record went in 2026-10-25 (DNS.md). The box's `~/mdc/.env`
holds `ANTHROPIC_API_KEY`, `ROXANA_API_KEY` and `DIANOIA_TOKEN` (the token
matches dianoia's `MDC_TOKEN`); `OPENAI_API_KEY` left it with OpenAI.

**Seeded** `rodvandur/philosophy` at `/srv/mdc/libraries/rodvandur/philosophy`
from the migrated Mac library (*Library layout*), mirrored with
`rsync --delete` (1,339 files, no difference from the Mac), and the
content-keyed asset cache at `/srv/mdc/cache`.

**Verified** on the box, each command run as the API will run it (in the
library, `MDC_USER_NAMES=Rodrigo`): `config`, `new`, `check`, `index` (632
documents, all cached), `reply` with an asset, with companions and with `-l`,
`argue`, `analyze` (all six dianoia agents), `audit`, `export --dry-run`,
`pdf`; `pytest` in the image (220 passed); a source's bytes hit the seeded
cache. The smoke test found three defects, fixed before the last deploy: the
image lacked `scripts/` (its tests could not load them), `pdf` opened its
output with xdg-utils' `open`, and the cache keyed by path and time missed
on every seeded asset, which led to *Library layout*.

Decided while building:

- **`/healthz` needs a server, so `mdc serve` starts here** with that one
  route (FastAPI + uvicorn), and is the image's command from the start. Phase
  2 adds routes to it. `serve` skips writing a default config.
- **The pytest extra is in the image**, so `pytest tests/` runs there.
- **`pdf` opens its output only on a desktop** (`darwin`, `win32`): Debian's
  TeX packages bring xdg-utils' `open`, so "an `open` exists" no longer
  meant a Mac.
- **The Caddyfile goes in before the deploy**, since `bin/deploy` ends by
  checking `https://mdc.rvanegas.co/healthz`. Until the container is up,
  Caddy answers 502 there, which is harmless.

**Migrating the Mac** comes first, because `.mdc/` is what seeding sends, and
goes right after landing: the Mac's `mdc` is an editable install of the
`~/src/mdc` checkout, so landing switches it to `.mdc/` at once, and an
`mdc index` before the migration finds no state and reindexes everything.
That happened on 2026-10-26 while the checkout sat on this branch (7
documents indexed, `MANIFEST.md` overwritten, then rebuilt from the intact
state). Branch work stays in a worktree, under `~/src/mdc/.claude/worktrees/<branch>`.

```bash
python3 scripts/migrate_library.py ~/Documents/Writing/Chats
```

It moves the three state files into `Chats/.mdc/`, puts `system.md` at the
top of `Chats/SYSTEM_PROMPT.md`, and drops `system_prompt_file`. The stale
`review-*.json~/.bak` stay in `~/.local/state/mdc` for you to delete.

**Seeding `rodvandur/philosophy`** (operator task). The library goes without
`.DS_Store`, editor backups, and the five `.pages` drafts (each has its PDF
beside it); the 48 MB `condensed-systematicity.wav` already moved out to
`~/Documents/Writing` (2026-10-26).

```bash
BOX=ubuntu@32.190.173.222
SSH="ssh -i ~/.ssh/lightsail-ubuntu"
$SSH $BOX 'mkdir -p /srv/mdc/libraries/rodvandur/philosophy /srv/mdc/cache'
rsync -a -e "$SSH" --exclude .DS_Store --exclude '*~' --exclude '*.pages' \
    ~/Documents/Writing/Chats/ $BOX:/srv/mdc/libraries/rodvandur/philosophy/
rsync -e "$SSH" ~/.cache/mdc/anthropic-asset-cache.json $BOX:/srv/mdc/cache/
```

This copy is disposable until Phase 3: edits on the Mac do not reach it, and
the smoke test writes to it. Phase 3's first sync re-seeds it (below).

**Smoke test** (operator task): each command runs as the API will run it, in
the library with the user's names.

```bash
cd ~/mdc && docker compose run --rm -e MDC_USER_NAMES=Rodrigo \
  -w /data/libraries/rodvandur/philosophy mdc mdc --lib . <cmd>
docker compose run --rm -w /app mdc pytest -p no:cacheprovider tests/
```

for `config`, `check`, `reply` on a `.chat.md` with companions and assets,
`reply -l`, `index` (expect 0 changed), `argue`, `analyze`, `audit`,
`export`, `pdf`.

The plan as written before building (its workspace layout is superseded by
*Where things live*):

- **Image:** `Dockerfile` from `python:3.13-slim`, like dianoia's: `pip install
  .`, `pandoc`, `texlive-xetex`, `lmodern`, `texlive-fonts-recommended`,
  `diffutils`. No dianoia in the image. TeX adds about 1 GB of disk and is used
  rarely. A `pdf` run takes a few hundred MB for seconds, so it's no concern
  on the 2 GB box.
- **Compose:** `docker-compose.yml`, published on loopback `127.0.0.1:8001`
  (dianoia has :8000, imitation :8080; recorded in `PROJECTS.md`). `.env`
  holds `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ROXANA_API_KEY` (mdc's own
  AppSync key, SITE Phase 4) and `DIANOIA_TOKEN`. The host directory `/srv/mdc`
  is bind-mounted at `/data`, and the container runs as the box's deploy user.
- **Caddy:** in `rvanegas-site`, the `Caddyfile` gets `mdc.rvanegas.co` →
  `127.0.0.1:8001`, `DNS.md` gets an `mdc` A record, and `PROJECTS.md` gets
  mdc's row. Caddy streams `text/event-stream` without extra configuration.
- **`GET /healthz`** reports the deployed sha (thefloor's
  `server/deployed.json` pattern). `bin/deploy` ends in a health check against
  `https://mdc.rvanegas.co/healthz`.

## Phase 2 — the API (`mdc serve`) — done 2026-10-27

FastAPI + uvicorn in `mdc/server.py`, the same stack as dianoia, grown from
Phase 1's `/healthz`.

**Built** on mdc branch `api`, as planned below except where noted here:

- **Preconditions are HTTP's own:** `PUT` creates with `If-None-Match: *` and
  replaces with `If-Match: "<sha256>"` (`If-Match: *` means the opposite in
  HTTP); a stale one gets 412 Precondition Failed, a missing one 428. 409 is
  for a library busy with a run, which also refuses writes. Writes are atomic.
  Every dotfile is hidden, not only `.mdc/`.
- **`/run` takes names, not paths:** mdc runs from the library root, and its
  CLI now resolves a name or an abbreviation across the library (the
  migration had broken abbreviations: every file has a role now). `mdc new`
  writes into this year's directory. Output streams as `output` events of
  `{text}` chunks, not lines (reply prints tokens without newlines), then a
  `done` event, or `error` if the run could not start. The run belongs to
  the server: a client that goes away does not cut it short. Allowed:
  `check validate fix new reply index argue analyze audit export pdf diff
  review`; `--lib` and `reply --watch` are refused.
- **Tokens:** `scripts/add_token.py <user>` prints a new token once and
  appends its SHA-256 to `users.toml` (mode 600); `"*"` is a service. The file
  is read on every request, so a token works without a restart.
- **Nightly:** `bin/nightly-index`, for cron on the box, lists the libraries
  with a service token from `~/.config/mdc/nightly-token` (outside `~/mdc`,
  which deploys mirror with `--delete`) and runs `index` in each through
  `/run`. There is no `relate`: relations are part of `index`.

**Deployed** master `f046b0b` (2026-10-27). Tokens, minted by Rodrigo with
`scripts/add_token.py` (minting credentials is theirs to run): `rodvandur`,
written into the Mac's `config.toml` as `server_url`/`server_token` for
Phase 3, and `*` in the box's `~/.config/mdc/nightly-token`; `users.toml`,
`config.toml` and the token file are mode 600.

**Verified** through Caddy: no token or a wrong one gets 401; Rodrigo's token
lists `rodvandur/philosophy`. A throwaway chat created with `PUT`
(`If-None-Match: *`), replied to with `run` (`reply api-smoke-test`, found by
abbreviation): Claude's reply arrived as 25 `output` events between 3.8 s and
5.0 s, and `done` named the chat and its new backup as changed (and the 18
backups `reply` pruned for age as deleted); both were then removed with
`DELETE` and their `If-Match` hashes, and a stale `If-Match` got 412.
`bin/nightly-index` run by hand on the box indexed the library through the
API with the service token (exit 0, nothing to summarize).

**Cron:** installed by Rodrigo 2026-10-27 (a recurring job is theirs to add):
`30 9 * * * $HOME/mdc/bin/nightly-index >> $HOME/.local/state/mdc-nightly.log 2>&1`
in the box's crontab, half an hour before the daily snapshot.

The plan as written before building:

- **Auth:** a bearer token per user, hashed in `/data/users.toml` (token hash
  → user), required on every route except `/healthz`. A user reaches only
  `libraries/<user>/…`. Naive users could later sign in with Roxana's Cognito
  pool instead (SITE Phase 4 verifies those tokens for dianoia already).
- **Libraries:** `GET /libraries` → the caller's library ids. Creating one
  (`POST /libraries/{user}/{library}`) comes with the first client that needs
  it.
- **Files**, for every kind of client, under `/libraries/{user}/{library}`:
  - `GET …/files` → `{rel_path: sha256}`.
  - `GET …/files/{rel_path}` → content and hash.
  - `PUT …/files/{rel_path}` with `If-Match: <base hash>` (`*` for a new file)
    → 409 on mismatch.
  - `DELETE …/files/{rel_path}` with `If-Match`.
  - Paths are confined to the library, reusing the containment check in
    `assets.py`. Symlinks are refused. `.mdc/` is neither listed nor
    writable.
- **Commands:** `POST /libraries/{user}/{library}/run` `{argv, user_names}` →
  a server-sent-event stream of output lines, ending with `{exit_code,
  changed: {rel_path: sha256}, deleted: […]}`.
  - The first version runs `mdc --lib <library> <argv>` as a subprocess with
    `cwd` in the library and `MDC_USER_NAMES` from the request. This is
    internal to the service, not a second entry point. It avoids reworking the
    ~215 `print` calls in `cmd_*` before the API is useful.
  - Changed and deleted files come from hashing the library (without `.mdc/`)
    before and after the run.
  - The allowed subcommands exclude `edit`, `config`, `files` and
    `reply --watch` (client-side concerns). `pdf` never runs `open` on the
    server.
  - One run at a time per library; a second request gets 409.
- **Scheduled:** a nightly `index` + `relate` per library goes through `/run`
  with a service token, from cron `curl` on the box, not `docker exec`.
- **Later:** structured endpoints for the naive client (create a session, list
  documents, reply with a JSON result) by refactoring `cmd_*` to return results
  and stream through the clients' existing `on_delta` callbacks. That
  retires the subprocess.

## Phase 3 — the local CLI becomes a client — done 2026-10-27

**Built** on mdc branch `client` (`mdc/remote.py`), as planned below except:

- **What runs remotely:** `reply`, `index`, `argue`, `analyze`, `audit`,
  `export`, `pdf`, `review` and `files` (it lists what the server's key
  uploaded), whenever the config has `server_url`, `server_token` and a
  `[libraries."<id>"] local_root` holding the working directory (or `--lib`).
  `mdc sync` syncs alone. The server's own runs set `MDC_RUN_LOCAL`, so they
  never turn client.
- **Sync** is three-way per file: here (L), on the server (R), and as both
  last agreed (B, in the local library's `.mdc/sync.json`). Only L changed:
  push (`PUT`/`DELETE` with `If-Match`). Only R changed: pull. Both: a
  conflict, the server's version saved as `<name>.server` (never synced),
  nothing merged; deleting that copy keeps the local version, which the next
  sync pushes. A conflict before a run stops it; one after (an edit made
  during the run) is left for the user. The first sync, with no sync.json,
  takes the server's state as B, so the Mac re-seeds the server.
- **`reply --watch`** watches the local file and replies remotely.
- **The editor plugins** make the buffer read-only while `reply` runs and
  writable again when it ends, reverting on success.
- **What the Mac stops holding** is its configuration, not code: the model
  key leaves `config.toml` (the server runs the same `config.py`, reading its
  key from `.env`). The Mac's `.mdc/` index state stays: `validate` still
  reads it to resolve Related titles, so it ages until `validate` asks the
  server instead.

**Deployed** master `3720636` (2026-10-27). The Mac's config mirrors
`rodvandur/philosophy` from `~/Documents/Writing/Chats`; the first `mdc sync`
re-seeded the server's copy (19 sent, 0 received: the 18 backups a test
`reply` had pruned there, and `MANIFEST.md`), and agrees on 1,336 files. The
Mac's config no longer holds `anthropic_api_key`, `dianoia_token` or
`roxana_api_key` (none is in its environment either): those commands run on
the server.

**Verified** with no key on the Mac: `mdc index` ran on the server (632
documents, all cached) and synced back the `MANIFEST.md` it rewrote; `mdc
files ls` listed what the server's key uploaded; `mdc check` stayed local; a
second `mdc sync` had nothing to do.

**Checked by hand** in Emacs on a test pair
(`2026-10-27-remote-reply-test.{chat,document}.md`): `C-c C-r` synced the
prompt, Claude on the server edited the `.document.md` with `edit_file`, and
the chat, the document and their backups came back (Mac and server
byte-identical; `mdc diff` shows the edit); `reply -l` looked up a term and
answered with a Related section. The first try showed that `reply` loaded a
chat's companions only with `-e`, which no editor command passed; since
`4c07778` it loads them whenever they exist, and the sync leaves editor
backups and autosaves alone.

Two older copies of the Mac's config still hold keys:
`~/.config/mdc/config.toml.bak-2026-10-25` and `config.toml~`.

The plan as written before building:

- `~/.config/mdc/config.toml`: `server_url`, `server_token`, `user_names`
  (sent with every run), and the libraries it mirrors:
  `[libraries."rodvandur/philosophy"] local_root = "~/Documents/Writing/Chats"`.
  No model keys.
- `check`, `validate`, `fix`, `new`, `edit` and `diff` stay local: text work on
  local files with no model calls, fast in the editor loop. Everything else
  runs remotely through `mdc/remote.py`, against the library whose
  `local_root` holds the file:
  1. Hash the local tree and `GET …/files`. `PUT` each differing file with
     `If-Match` = its hash at the last successful sync (kept in the local
     library's `.mdc/sync.json`, which is never synced). A 409 aborts before
     running.
  2. Translate path arguments to be relative to `local_root`, call `…/run`,
     and print the stream as it arrives.
  3. For each changed file, write the server's version if the local file still
     matches what was pushed. Otherwise save it as `<name>.server.md` and
     report a conflict. Never merge automatically.
  4. Update `sync.json`. Deletions propagate in both directions through it.
- **The first sync re-seeds** the server's copy from the Mac, which is
  canonical until then: Phase 1's seed has gone stale, and without a
  `sync.json` every edit since would look like a conflict.
- The editor plugins (`editors/emacs/mdc.el`, `editors/sublime/mdc.py`) keep
  shelling out to the local `mdc` and reverting the buffer. They make the
  buffer read-only while `reply` runs.
- Then remove what the Mac no longer uses: the model key in `config.py`, the
  Mac's local `.mdc/` index state, and `cmd_diff.py`'s key
  lookup. The server runs the same package, so the model clients themselves
  stay.

## Phase 4 — `mdc import` — built 2026-10-27, not yet landed

Conversations held elsewhere become library files. Local, with no server
change; independent of Phase 5, and first because it is small and settles
how outside transcripts enter a library.

`mdc import <file>…` writes role-named files into the current library's
year directories, like `mdc new`, and the next sync carries them up:

- **claude.ai's data export** (Settings → Privacy → Export data: a zip, or
  its `conversations.json`): each conversation becomes `<date>-<slug>.chat.md`
  from its own messages, exactly: title from the conversation's name, date
  from its creation, `## Prompt` and `## Claude` turns, `##` headings inside
  replies promoted to `###` (as `reply` does). `--only <name or uuid>`
  imports one conversation; the default is all, skipping one whose file
  already exists (`--replace` to overwrite it).
- **saveai.net exports** (Markdown, the "AI Chat Exporter" format `mdc fix`
  already converts: `# You asked` / `# Claude response` blocks): converted by
  `form.fix_exported_chat` into a `.chat.md`. To check against a real
  saveai.net Claude export before relying on it.
- **Anything else**, or a chat export that does not parse: imported as a
  `.document.md`, its text as it is, under a title and date taken from its
  first heading and the file's date (or today).

Every imported file passes `check` (a chat) or `validate` (a document)
before it is written; names stay unique across the library.

**Verification**

- tests on a recorded claude.ai export and a saveai.net file: exact
  turns, promoted headings, unique names, the document fallback; by hand,
  importing one real claude.ai conversation and replying to it with
  `mdc reply`.

## Phase 5 — claude.ai reaches the library — 5a deployed 2026-10-27; 5b built

Claude on claude.ai (web, phone, Desktop) gets the tools `mdc reply` gives
the model, on Rodrigo's libraries, through a custom connector: a remote MCP
server. This is the first "client other than the CLI" of TODO.md, with
claude.ai as that client. It supersedes the plan of the session "MCP access
to mdc reply" (2026-10-23), which put a stdio server on the Mac and needed a
tunnel for claude.ai: `mdc.rvanegas.co` is already the public HTTPS
endpoint, and the library and keys are already on `commons`. Phase 4's
`mdc import` is what repairs or replaces a transcript `append_turn` kept
badly.

### 5a — the tools, in-process, behind `/mcp`

**Built** on mdc branch `mcp` (`mdc/mcp_server.py`, `mdc/service.py`), with
the official SDK `mcp` 2.3 (its `MCPServer`, formerly FastMCP). As planned
below, and:

- **Auth already goes through the SDK's OAuth resource-server machinery:**
  one `TokenVerifier` (5a: `users.toml` tokens; 5b adds Cognito's), and the
  protected-resource metadata at `/.well-known/oauth-protected-resource/mcp`
  already names Roxana's pool as the authorization server. Without a token,
  `/mcp` answers 401.
- **`mdc/service.py`** holds what `/run` and `/mcp` share: token lookup,
  library resolution, the per-library locks.
- **A fresh MCP server per app lifespan**, behind a small mount: a
  streamable-HTTP session manager runs only once. Stateless HTTP; allowed
  hosts `mdc.rvanegas.co` and loopback, origins claude.ai and claude.com.
- **Tools report anticipated failures as `ToolError`**, whose message the
  model sees (2.x hides other exceptions' text).
- **Versions:** `reply` kept the text before an edit only when re-wrapping
  had changed it. `_write_version`, shared by `reply` and `edit_file`, now
  records a file's current text before an automated write unless it already
  is the latest version, so every write can be undone from `REVISIONS/`.
- **`reply`'s library instructions** are `library.LIBRARY_TOOLS_PROMPT`,
  shared with the MCP server's instructions.
- **claude.ai does not show the model a server's instructions** (found
  2026-10-27: asked, Claude saw only the tool descriptions). So the guidance
  is also a tool, `guide`, which the other descriptions point to, and the
  rules that matter most are in the descriptions themselves: the naming
  scheme, citing only what was read, never renumbering propositions, what
  `append_turn`'s speakers mean.

Connect Claude Code with a token from `users.toml` (for instance the Mac's
`server_token`): `claude mcp add --transport http mdc
https://mdc.rvanegas.co/mcp --header "Authorization: Bearer <token>"`.

**Deployed** master `969eb0e` (2026-10-27): through Caddy, `/mcp` answers
401 without a token, and with the Mac's token the server lists its six tools
and `lookup_term` answers from the library. Claude Code holds an entry for it
(local scope, `~/src/mdc`).

The plan:


- **Lift the tools out of `reply`.** Their executors are closures built in
  `cmd_reply.py` (`_reply_anthropic`); they move to one module, called by
  `reply` and by the MCP server alike. On the server they run in-process,
  not as a `/run` subprocess: the start of the "API for clients other than
  the CLI" in TODO.md.
- **The MCP server:** the official `mcp` Python SDK, streamable HTTP,
  mounted at `/mcp` in `mdc serve`'s FastAPI app (behind the Caddy block that
  exists). Tools, each taking a library id (default: the caller's only one):
  - `list_documents(query?)`: documents by title or term, with their roles
    and companions.
  - `lookup_term(term)`, `read_document(path)`: as in `reply -l`.
  - `read_file(name)`: a chat, document, argument or source by name.
  - `edit_file(name, old_str, new_str)`: exact replacement in a `.document.md`
    or `.argument.md` (never a source), with `reply`'s numbered backup,
    paragraph wrapping, and the returned diff. **Withdrawn 2026-10-27**:
    claude.ai users edit with claude.ai's own documents and import them into
    the library later, so `edit_file` is `mdc reply`'s alone.
  - `append_turn(name, speaker, text)`: adds a turn to a `.chat.md`, created
    when missing (`speaker`: the user's heading or `Claude`), so a claude.ai
    conversation can be kept as it goes. Unreliable by nature: Claude may
    forget to call it, and `mdc import` repairs or replaces the transcript
    afterwards. **Withdrawn 2026-10-27**, for that reason: import alone is
    the way a claude.ai conversation reaches the library, whole, its
    Related and References gathered at the end. The server no longer writes.
  - Later: `analyze`, `audit` (through dianoia), `new_session` (a
    `.chat.md` + `.document.md` pair).
  The rules `reply` puts in its system prompt (the editing instructions, the
  argument format, never renumbering propositions) go into the server's MCP
  instructions and the tools' descriptions.
- **Writes** take the library's lock like `/run` (409 while a run is in
  progress) and land in the server's copy, which is canonical; the Mac gets
  them on its next sync, and a file also edited locally becomes a
  `.server` conflict, never a silent overwrite.
- **Tested first from Claude Code**, which can send a bearer header to a
  remote MCP server: a token from `users.toml` works there before any OAuth
  exists.

### 5b — mdc's own OAuth server, signed into with an mdc token (2026-10-27)

**Decided:** real authentication, per person, with a sign-in that need not be
easy yet. mdc is its own OAuth authorization server for claude.ai's connector
(`mdc/oauth.py`, the MCP SDK's provider interface; the SDK serves discovery,
`/register`, `/authorize`, `/token`, `/revoke` and checks PKCE):

- **Sign-in:** claude.ai registers itself (dynamic client registration: the
  connector needs only `https://mdc.rvanegas.co/mcp`) and opens mdc's
  `/oauth/login`, which names the client and its redirect host; the person
  pastes **their mdc token**, minted for them by the operator with
  `scripts/add_token.py <user>`, and is sent back with a code.
- **Tokens:** an access token for an hour, a refresh token for 90 days,
  rotated on use, revocable; stored as hashes in `/data/oauth.json` (mode
  600), so a deploy signs nobody out. Each records the mdc token that signed
  in, and stops working when that token leaves `users.toml`: removing a
  person's token removes their claude.ai access too.
- **Redirects** are limited at registration to claude.ai's and claude.com's
  callbacks and to localhost, since registration is open to anyone.
- **A minted mdc token still works as a bearer token** (Claude Code, the
  CLI's API calls), through the same provider.
- **A second person:** a `users.toml` token of their own, and a library at
  `libraries/<user>/<library>` (made by hand until there is an API for it).

**Why not Cognito, nor The Floor's sign-in:** The Floor, which mdc is to
integrate with, signs people in with emailed one-time codes, no passwords;
Roxana's Cognito pool would have been a second identity to map, and borrowing
The Floor's codes would have tied this to an API The Floor does not have yet.
Pasting a token is plain, not friendly, and is the one part a real identity
plan (TODO.md) replaces; the OAuth server around it stays. A Cognito verifier
built first (branch `oauth`, `5e7ab3b`, not landed) is kept only in that
commit; a secret connector URL was built and dropped before landing.

The plan as it stood before (superseded):

claude.ai's custom connectors sign in with OAuth. The MCP server is a
resource server; Roxana's user pool is the authorization server.

- **Identity is Cognito's, not claude.ai's.** Adding the connector opens
  Cognito's sign-in page; claude.ai never sends its own account's email to
  the server. Cognito's username need not be an email (in Roxana's pool it
  is not: `usernameAttributes` is empty, email a required attribute). mdc
  maps the token's `sub`, which never changes, to an mdc user:
  `[[cognito]] sub = "…" user = "rodvandur"` in `users.toml`.
- **The pool needs, as AWS changes on their own go-ahead** (and recorded in
  rvanegas-site's IAM/SITE notes): a Cognito domain for its hosted sign-in,
  and an app client `mdc-claude` with a secret, the authorization-code grant
  and refresh tokens, scopes `openid email`, and callback URLs
  `https://claude.ai/api/mcp/auth_callback` and
  `https://claude.com/api/mcp/auth_callback`. Roxana's Gen 2 migration
  (before May 2027) must carry them.
- **Cognito has no dynamic client registration**, so the connector is added
  in claude.ai with that client's ID and secret under *Advanced settings*.
- **mdc serves** `/.well-known/oauth-protected-resource` naming the pool as
  its authorization server, and checks each request's access token against
  the pool's JWKS (issuer, `client_id` = `mdc-claude`, `token_use` =
  `access`, expiry), as dianoia checks Roxana's ID tokens since SITE Phase 4.
- **To settle first, with a stub:** whether claude.ai finds Cognito's
  endpoints from its OpenID discovery document (Cognito does not serve the
  OAuth metadata path, RFC 8414). If not, mdc serves
  `/.well-known/oauth-authorization-server` describing Cognito's endpoints.

### Verification

- **5a:** MCP tests with the SDK's in-process client: each tool, the lock,
  no write to a source or outside the library; from Claude Code with a
  bearer token, a `lookup_term`, an `edit_file` whose backup and diff appear,
  and the edit reaching the Mac with `mdc sync`.
- **5b:** an expired, foreign (another pool or client) or unmapped token
  gets 401; then, in claude.ai, adding the connector, signing in through
  Cognito, and asking Claude to look up a term and edit a document.

---

## Files touched

- **mdc:** `SERVER.md`, `mdc/dianoia_client.py`, `mdc/config.py`,
  `mdc/cli.py`, `mdc/cmd_pdf.py`, new `mdc/server.py` and `mdc/remote.py`,
  `mdc/library.py`, `mdc/review.py`, `mdc/cmd_*.py`, `etc/config.toml`,
  `Dockerfile`, `docker-compose.yml`, `bin/deploy`,
  `scripts/migrate_library.py`, `scripts/migrate_layout.py`, `mdc/asset_cache.py`, `tests/`,
  `pyproject.toml` (fastapi, uvicorn),
  `CLAUDE.md`, `mdc/README.md`
- **dianoia:** `src/api/argument.py` (`extract`, `evaluate`, token dependency),
  `src/tests/`, `.env-sample`
- **rvanegas-site:** `PROJECTS.md`, `Caddyfile`, `DNS.md`, `SITE.md`

## Verification

- **Phase 0:** a recorded-response fixture test of `dianoia_client.py` (SITE
  Phase 4's drift guard). Dianoia route tests: a missing or wrong token gets
  401. Against the live service: `mdc argue`, `mdc analyze`, `mdc audit` on a
  sample document with no `dianoia` on the Mac's `PATH`.
- **Phase 1:** `tests/test_server.py` (`/healthz` says `dev` in a checkout and
  the stamped commit otherwise), `tests/test_config.py` (library, names and
  system prompt per run), `tests/test_migrate_library.py`,
  `tests/test_layout.py`, `tests/test_asset_cache.py`, `tests/test_assets_scope.py`; `migrate_library.py` rehearsed on a copy of
  the real library (630 entries and 5,190 terms load from `.mdc/`); then the
  smoke test above, and `bin/deploy` ending with
  `https://mdc.rvanegas.co/healthz` answering the sha it sent.
- **Phase 2:** `tests/test_server.py` with FastAPI's `TestClient` checks that
  auth is required, path escapes are refused, a stale `If-Match` gets 409,
  `/run` streams and reports changed files, a user cannot reach another's
  library, and the per-library lock holds.
  `curl -N` a `reply` through Caddy and see it arrive progressively.
- **Phase 3:** `tests/test_remote.py` against an in-process server checks a
  reply round trip, that a local edit during a run produces `<name>.server.md`,
  and that deletions propagate both ways. By hand: Emacs `C-c C-r` on a real
  `.chat.md` with a `.document.md` companion, then `mdc reply -l`, then confirm
  `mdc config` shows no model key on the Mac.

## Ground rules

As in SITE.md: each change is committed on a branch in its own repository.
Landing, deploying, and any DNS or AWS change happen only when asked for at the
prompt, one verb at a time. Copying the library to the server is part of
deploying.
