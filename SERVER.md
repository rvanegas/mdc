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

## Phase 1 — mdc on `commons` — built, not yet deployed

**Built** 2026-10-25 on `server-phase1` (landed on master `49e68a2`):
`Dockerfile`, `docker-compose.yml`, `.dockerignore`, `bin/deploy`, `mdc serve`
with `GET /healthz` (`mdc/server.py`), and the Caddy block on rvanegas-site's
master (not yet installed on the box). Reworked 2026-10-26 on
`library-layout` for *Where things live*. The image has not been built
anywhere yet (no Docker on the Mac); its first build is the first deploy.

**Done:** the `mdc` A record (2026-10-25, DNS.md); the box's `~/mdc/.env`
with `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ROXANA_API_KEY` and
`DIANOIA_TOKEN` (2026-10-26; the token matches dianoia's `MDC_TOKEN`).

**Left, in order, each on its own go-ahead:** land `library-layout`; migrate
the Mac; seed `rodvandur/philosophy`; install the Caddyfile; `bin/deploy`;
the smoke test.

Decided while building:

- **`/healthz` needs a server, so `mdc serve` starts here** with that one
  route (FastAPI + uvicorn), and is the image's command from the start. Phase
  2 adds routes to it. `serve` skips writing a default config.
- **The pytest extra is in the image**, so `pytest tests/` runs there.
- **`pdf` needs no change:** it only calls `open` when it exists, and the
  container has none.
- **The Caddyfile goes in before the deploy**, since `bin/deploy` ends by
  checking `https://mdc.rvanegas.co/healthz`. Until the container is up,
  Caddy answers 502 there, which is harmless.

**Migrating the Mac** comes first, because `.mdc/` is what seeding sends, and
goes right after landing: the Mac's `mdc` is an editable install of the
`~/src/mdc` checkout, so landing switches it to `.mdc/` at once, and an
`mdc index` before the migration finds no state and reindexes everything.
That happened on 2026-10-26 while the checkout sat on this branch (7
documents indexed, `MANIFEST.md` overwritten, then rebuilt from the intact
state). Branch work stays in a worktree (`~/src/mdc-library-layout`).

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
cache=$(mktemp)
python3 scripts/rebase_cache.py ~/.cache/mdc/anthropic-asset-cache.json $cache \
    --old-root ~/Documents/Writing/Chats --new-root /data/libraries/rodvandur/philosophy
rsync -e "$SSH" $cache $BOX:/srv/mdc/cache/anthropic-asset-cache.json
rm $cache
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

## Phase 2 — the API (`mdc serve`)

FastAPI + uvicorn in `mdc/server.py`, the same stack as dianoia, grown from
Phase 1's `/healthz`.

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

## Phase 3 — the local CLI becomes a client

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
- Then remove what the Mac no longer uses: model keys and Ollama settings in
  `config.py`, the Mac's local `.mdc/` index state, and `cmd_diff.py`'s key
  lookup. The server runs the same package, so the model clients themselves
  stay.

---

## Files touched

- **mdc:** `SERVER.md`, `mdc/dianoia_client.py`, `mdc/config.py`,
  `mdc/cli.py`, `mdc/cmd_pdf.py`, new `mdc/server.py` and `mdc/remote.py`,
  `mdc/library.py`, `mdc/review.py`, `mdc/cmd_*.py`, `etc/config.toml`,
  `Dockerfile`, `docker-compose.yml`, `bin/deploy`,
  `scripts/migrate_library.py`, `scripts/rebase_cache.py`, `tests/`,
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
  `tests/test_rebase_cache.py`; `migrate_library.py` rehearsed on a copy of
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
