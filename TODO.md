# TODO

- Related and References just show additions

- Progress report

- versioning

- list names before predicates, enforce ranges to split alphabet, limit number of each to keep args small

- repeat if run out of letters

- latex formalization

## After SERVER.md (noted 2026-10-27)

- validate asks the server: it still reads the Mac's local .mdc/ index state to resolve Related titles, which no longer updates; then delete the Mac's .mdc/ (sync.json moves elsewhere or stays alone)

- carry moves through sync: a move (mdc fix into the year directory) syncs as delete + create, and the server summarizes the file again; detect moves by hash, or add a move endpoint that calls library.move_documents

- faster deploys: install dependencies in their own Dockerfile layer before copying mdc/, so a code change does not reinstall them

- broken links in rodvandur/philosophy: 2026-07-09-responsible-models.chat.md (schulte.pdf) and 2026-07-09-theory-of-desire.chat.md link deleted PDFs; 2026-03-26-amelioration-without-redefinition.chat.md links CappelenConceptualEngineering.pdf and concepteng.pdf, gone; 2026-03-24-verbal-disputes.chat.md links 2025-05-19-money-and-marriage-by-authoritative-assertion.txt, which never existed (the .md document?); 2026-06-16-all-together-now.chat.md line 665 holds the system prompt's reference template as a reference

- API for clients other than the CLI (SERVER.md, Phase 2, Later): structured endpoints (create a session, list documents, reply with a JSON result) by having cmd_* return results and stream through on_delta, which retires the /run subprocess; wait for a client that needs it (The Floor)

- sign-in for those users with Roxana's Cognito pool rather than minted tokens (dianoia verifies them since SITE Phase 4)

- POST /libraries/{user}/{library} to create a library, with the first client that needs it

- a real identity plan, for mdc's tools, The Floor and claude.ai together: The Floor signs in with emailed codes and no passwords; mdc's OAuth server (oauth.py) meanwhile signs people in by pasting their mdc token, the one step such a plan replaces (SERVER.md, Phase 5b)

- manual tests for mdc import (SERVER.md, Phase 4; the tests use fixtures written from the formats as documented, not real files): run `mdc import` on a real claude.ai data export (Settings → Privacy → Export data; the zip, then `--only` one conversation) and on a real saveai.net Claude export; check turns, the branch kept for edited messages, attachments, and that each imported chat passes `mdc check`; then `mdc reply` to one imported chat. If either format differs from tests/fixtures/, re-record the fixture from the real file and adjust cmd_import.py
