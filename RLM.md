# RLM vs. mdc: Comparison and Lessons

Paper: *Recursive Language Models* — Zhang, Kraska, Khattab (MIT CSAIL, arXiv 2512.24601, January 2026)

---

## What mdc does and where it sits relative to RLM

**mdc's techniques for handling a large document library:**

1. **Offline indexing** (`mdc index`): AI generates per-document summaries and terms → cached state, MANIFEST.md, INDEX.md
2. **Term relations** (`mdc relate`): AI semantic pass + co-occurrence supplementation → relations map
3. **Tool-based retrieval at query time** (`mdc reply -l`): the model gets `lookup_term` and `read_document` as tools, navigates the term graph, requests full documents as needed
4. **Writing assistant**: `edit_file` tool for symbolic document manipulation instead of verbalizing full rewrites

---

## Where mdc already applies RLM's core insight

The central RLM claim is: *don't dump the corpus into the attention window; give the model symbolic handles and let it request what it needs.* mdc already does this. `lookup_term` and `read_document` are exactly that — the library is never loaded wholesale, the model navigates it via tool calls. The paper's Algorithm 2 critique (putting the full context in the window and compacting when full) doesn't apply to mdc's `-l` mode. mdc's architecture is sound on this axis.

The writing assistant goes further: instead of the model autoregressively verbalizing the full revised document, it emits `edit_file` calls. The paper calls this "passing recursive LM outputs through variables for long output tasks" — building up the result symbolically rather than in one pass. mdc independently arrived at the same idea.

---

## The critical gap: no sub-calls

The biggest thing mdc can't do is what the paper calls *symbolic recursion*: the model writing code that loops over slices of the corpus and invokes a sub-LLM per slice, accumulating results in variables.

Consider a query like: *"across all my notes, what are the recurring unresolved tensions in my thinking about X?"* This requires processing most documents. With `lookup_term` + `read_document`, the model can read maybe 10–15 full documents before filling its context window. It can't loop over 200 documents, run a sub-LLM on each batch, and synthesize the results — there's no mechanism for it.

mdc's tool loop terminates when the model stops calling tools and writes a reply. RLM's loop continues until the model says `FINAL()`. That difference in control flow is everything for aggregation queries.

---

## Actionable lessons

**1. Add a `query_documents` tool**

The minimal RLM-inspired addition to mdc: a library tool that takes a list of paths and a question, makes an internal sub-LLM call (a cheap model — the paper uses GPT-5-mini as the sub-call model while GPT-5 roots), and returns a synthesized answer. The root model could then loop over document batches without filling its context window. This fits cleanly into the existing tool framework alongside `lookup_term` and `read_document`.

**2. The 3-tier lazy loading plan already addresses the right problem**

The planned `lookup_term → get_summary → read_document` progression is the correct direction. The paper's "no sub-calls" ablation result is encouraging: even without recursive sub-calls, just keeping context out of the attention window until needed provides large gains. The 3-tier plan does this. The remaining gap is that the model can only *read* documents, not *process* them at scale.

**3. The offline index is a genuine competitive advantage**

RLM pays full frontier model cost per query to discover what to process. mdc pays index time once and queries cheaply. The paper shows RLM costs are comparable to base model costs on median queries, but spike at the tail. mdc's term index short-circuits that entirely for the common case. The architecture is right; it just needs sub-calls added for the cases where it currently fails.

**4. The training result is interesting for the sub-call model**

Training Qwen3-8B on 1000 RLM trajectories improved it by 28.3%. This suggests that a cheap, specialized sub-model fine-tuned for document processing could be effective as the inner-loop model for a `query_documents` tool — you don't need a full frontier model for "extract the relevant passage from this document."

**5. The failure mode to watch**

The paper's OOLONG benchmark is the diagnostic: queries where the answer depends on nearly every document, not just a few. mdc's current design will quietly return a plausible but incomplete answer for those queries, with no signal that it missed most of the corpus. That's worth being aware of as a user of `mdc reply -l` — it's a retrieval system, not an exhaustive processor.

---

## Summary

mdc and RLM converge on the same core insight (symbolic handles, not context dumps) but diverge on what the model can *do* with those handles. mdc's model can navigate and read; an RLM's model can also loop, delegate, and accumulate. The one concrete thing mdc could learn from the paper is `query_documents` — a sub-call tool that lets the model do batch processing within a reply, which would close the gap for aggregation queries.
