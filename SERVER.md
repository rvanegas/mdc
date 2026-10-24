# mdc as a service on the commons box

A plan, not yet executed (written 2026-10-24). It moves all of mdc's
functionality onto the server and leaves the Mac holding only the files Rodrigo
edits. It sits inside the site plan, `~/src/rvanegas-site/SITE.md`: mdc becomes
one more service on the `commons` box behind Caddy, at `mdc.rvanegas.co`.

The goal: one API that every client uses.

- **Rodrigo** keeps editing plain markdown in their editor and typing `mdc …` in
  the terminal. The local `mdc` becomes a client of the API: it syncs their
  files to their server workspace, runs the command there, streams the output, and
  writes changed files back. No model API key and no model call stays on the
  Mac.
- **Naive users** never touch a terminal or a local file. A richer client
  (later) drives the same API against their server-side workspace.

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

mdc Phase 0 → SITE Phase 1 → SITE Phase 2 (`commons` + Caddy) → mdc Phase 1 on
`commons` → then SITE Phases 3–4 and mdc Phases 2–3 in parallel.

mdc Phase 0 depends on no box and is assumed by SITE Phase 4. mdc Phase 1
waits for `commons` so mdc moves only once.

---

## Phase 0 — mdc calls dianoia over HTTPS

- **dianoia:** two new routes in `src/api/argument.py`, alongside the existing
  `POST /argument/audit`:
  - `POST /argument/extract` `{text, max_props}` → `extract_argument`.
  - `POST /argument/evaluate` `{argument}` → starts the agents
    (`coordinator.react_to_user_argument_change`, as `cli/argue.py:cmd_evaluate`
    does) and returns `{conversation_id, snapshot_id}`. The client polls the
    existing `GET /agents/results` and `/agents/active`. Check that these
    report completion the way `coordinator.are_conversation_tasks_complete`
    does; if not, add a `complete` field to `/agents/results`.
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

## Phase 1 — mdc on `commons` (after SITE Phase 2)

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
- **Workspace layout:** `/data/workspaces/<user>/home/` holds `.config/mdc`,
  `.local/state/mdc` and `.cache/mdc`. `/data/workspaces/<user>/files/` holds the
  library tree.
- **Seeding Rodrigo's workspace (operator task):** copy
  `~/Documents/Writing/Chats` → `files/`, `~/.config/mdc` without keys →
  `home/.config/mdc`, and the state and cache directories → `home/…`. Then
  `scripts/migrate_state.py` rewrites `library_path` in
  `library-manifest.json` to `/data/workspaces/rodrigo/files` and drops the
  stale review files. Server config: `index_model = "claude-haiku-4-5"`, since
  this box has no Ollama.
- **Caddy:** in `rvanegas-site`, the `Caddyfile` gets `mdc.rvanegas.co` →
  `127.0.0.1:8001`, `DNS.md` gets an `mdc` A record, and `PROJECTS.md` gets
  mdc's row. Caddy streams `text/event-stream` without extra configuration.
- **`GET /healthz`** reports the deployed sha (thefloor's
  `server/deployed.json` pattern). `bin/deploy` ends in a health check against
  `https://mdc.rvanegas.co/healthz`.
- **Smoke test (operator task):** `docker compose run --rm -e HOME=… mdc mdc
  <cmd>` for `check`, `reply` on a `.chat.md` with companions and assets,
  `reply -l`, `index` (expect 0 changed), `argue`, `analyze`, `audit`,
  `export`, `pdf`. `pytest tests/` in the image.

## Phase 2 — the API (`mdc serve`)

FastAPI + uvicorn in `mdc/server.py`, the same stack as dianoia. The image's
command becomes `mdc serve`.

- **Auth:** a bearer token per user, hashed in `/data/users.toml` (user →
  token hash → workspace), required on every route except `/healthz`. Naive
  users could later sign in with Roxana's Cognito pool instead (SITE Phase 4
  verifies those tokens for dianoia already).
- **Files**, for every kind of client:
  - `GET /files` → `{rel_path: sha256}` for the workspace.
  - `GET /files/{rel_path}` → content and hash.
  - `PUT /files/{rel_path}` with `If-Match: <base hash>` (`*` for a new file)
    → 409 on mismatch.
  - `DELETE /files/{rel_path}` with `If-Match`.
  - Paths are confined to `files/`, reusing the containment check in
    `assets.py`. Symlinks are refused.
- **Commands:** `POST /run` `{argv}` → a server-sent-event stream of output
  lines, ending with `{exit_code, changed: {rel_path: sha256}, deleted: […]}`.
  - The first version runs `mdc <argv>` as a subprocess with `cwd=files/` and
    `HOME=…/home`, so `config.py`'s home-based directories give each user
    their own config, state and cache. This is internal to the service, not
    a second entry point. It avoids reworking the ~215 `print` calls in
    `cmd_*` before the API is useful.
  - Changed and deleted files come from hashing `files/` before and after the
    run.
  - The allowed subcommands exclude `edit`, `config`, `files` and
    `reply --watch` (client-side concerns). `pdf` never runs `open` on the
    server.
  - One run at a time per workspace; a second request gets 409.
- **Scheduled:** a nightly `index` + `relate` per workspace goes through
  `POST /run` with a service token, from cron `curl` on the box, not `docker
  exec`.
- **Later:** structured endpoints for the naive client (create a session, list
  documents, reply with a JSON result) by refactoring `cmd_*` to return results
  and stream through the clients' existing `on_delta` callbacks. That
  retires the subprocess.

## Phase 3 — the local CLI becomes a client

- `~/.config/mdc/config.toml`: `server_url`, `server_token`, `local_root =
  "~/Documents/Writing/Chats"`. No model keys.
- `check`, `validate`, `fix`, `new`, `edit` and `diff` stay local: text work on
  local files with no model calls, fast in the editor loop. Everything else
  runs remotely through `mdc/remote.py`:
  1. Hash the local tree and `GET /files`. `PUT` each differing file with
     `If-Match` = its hash at the last successful sync (kept in
     `~/.local/state/mdc/sync.json`). A 409 aborts before running.
  2. Translate path arguments to be relative to `local_root`, call `POST /run`,
     and print the stream as it arrives.
  3. For each changed file, write the server's version if the local file still
     matches what was pushed. Otherwise save it as `<name>.server.md` and
     report a conflict. Never merge automatically.
  4. Update `sync.json`. Deletions propagate in both directions through it.
- The editor plugins (`editors/emacs/mdc.el`, `editors/sublime/mdc.py`) keep
  shelling out to the local `mdc` and reverting the buffer. They make the
  buffer read-only while `reply` runs.
- Then remove what the Mac no longer uses: model keys and Ollama settings in
  `config.py`, and `cmd_diff.py`'s key lookup. The server runs the same
  package, so the model clients themselves stay.

---

## Files touched

- **mdc:** `SERVER.md`, `mdc/dianoia_client.py`, `mdc/config.py`,
  `mdc/cli.py`, `mdc/cmd_pdf.py`, new `mdc/server.py` and `mdc/remote.py`,
  `Dockerfile`, `docker-compose.yml`, `bin/deploy`,
  `scripts/migrate_state.py`, `tests/`, `pyproject.toml` (fastapi, uvicorn),
  `CLAUDE.md`, `mdc/README.md`
- **dianoia:** `src/api/argument.py` (`extract`, `evaluate`, token dependency),
  `src/tests/`, `.env-sample`
- **rvanegas-site:** `PROJECTS.md`, `Caddyfile`, `DNS.md`, `SITE.md`

## Verification

- **Phase 0:** a recorded-response fixture test of `dianoia_client.py` (SITE
  Phase 4's drift guard). Dianoia route tests: a missing or wrong token gets
  401. Against the live service: `mdc argue`, `mdc analyze`, `mdc audit` on a
  sample document with no `dianoia` on the Mac's `PATH`.
- **Phase 1:** the smoke test above. `curl -sI https://mdc.rvanegas.co/healthz`
  returns 200 with the expected sha.
- **Phase 2:** `tests/test_server.py` with FastAPI's `TestClient` checks that
  auth is required, path escapes are refused, a stale `If-Match` gets 409,
  `/run` streams and reports changed files, and the workspace lock holds.
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
