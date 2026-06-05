# mdc — Markdown Chat

**Terminal-native LLM research platform**
Python · Anthropic API

---

## What it is

mdc is a command-line replacement for the standard chat UI, built around the needs of sustained, document-intensive research with language models. It manages structured conversation transcripts, a searchable personal document library, and argument analysis via the Dianoia backend — all from the terminal, integrated with a text editor.

It is the primary interface through which a 3M-word body of philosophical research was developed, indexed, and analyzed across a 200k-token context window.

---

## Why it exists

Standard chat interfaces are designed for discrete, stateless conversations. Research — philosophical, technical, or otherwise — is cumulative. Ideas develop across sessions, earlier conclusions inform later questions, and the ability to retrieve, inject, and reason about prior work is essential.

mdc was built because no existing tool provided: persistent indexed transcript history, flexible document injection into context, argument extraction and analysis, and the kind of fine-grained control over context composition that sustained research requires.

---

## Architecture

**Transcript management**: Conversations are stored as structured markdown files with consistent naming and metadata. A library indexer generates AI semantic indexes and term-relation maps across the full transcript corpus, with a second co-occurrence supplementation pass for corpus-specific clustering. All transcripts are searchable and injectable into new conversation contexts.

**Context composition**: mdc gives precise control over what enters the context window. System prompt, injected documents, conversation history, and cached assets are managed as distinct layers. A prompt caching budget manager allocates Anthropic's four cache slots across system prompt and assets to minimize token costs on repeated operations.

**Document library**: A personal document store with AI-generated indexes. A server-side file ID cache avoids re-uploading unchanged PDFs across sessions. Documents can be injected selectively into any conversation context.

**Argument analysis**: A subprocess bridge to the Dianoia backend enables argument extraction and evaluation directly from the mdc interface. The `mdc argue extract` command formalizes an argument from free-form text; `mdc argue evaluate` assesses each step for validity and strength.

**Safety**: Versioned backup before every automated file edit. No conversation history or document is modified without a recoverable prior state.

**Multi-provider**: Supports multiple LLM provider backends with consistent interface abstraction.

---

## Scale

mdc has been used to:
- Process and analyze 3M+ words of philosophical research
- Maintain and search a library of 500+ indexed conversation transcripts
- Generate a comprehensive multi-domain philosophical assessment from a 200k-token context window composed of documents drawn from the library

---

## What it demonstrates

- Production-quality CLI design for a complex, stateful application
- Sophisticated context management for long-horizon LLM research
- RAG-style document retrieval integrated into a conversational workflow
- Prompt caching strategy across a multi-session research practice
- Subprocess orchestration for multi-tool AI pipelines

---

## Related projects

- [Dianoia](https://github.com/rvanegas/dianoia) — argument analysis backend
- [Noesis](https://github.com/rvanegas/noesis) — React frontend for argument evaluation
- [Roxana](https://github.com/rvanegas/roxana) — collaborative dialectic platform
