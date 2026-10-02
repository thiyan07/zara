# RAG Architecture

Retrieval-Augmented Generation here means: bounded hybrid retrieval over
Zara's own memory store, injected as labeled untrusted context.

- **Retrieve**: `MemoryStore.recall(query, top_k, category)` — 0.7 embedding
  cosine + 0.3 keyword overlap. Default embedding is a local hash fallback
  (no network, no paid API); real embedding models plug into
  `EmbeddingProvider` without changing retrieval code.
- **Bound**: `LoopConfig.memory_top_k = 3` memories into prompts; each capped
  at 200 chars; whole prompt capped at 6000 chars. Irrelevant items score low
  and are cut by top-k. Sources travel with items (`source` field) and are
  visible in trace/audit metadata.
- **Trust**: retrieved text is `<<UNTRUSTED DATA>>`. A memory saying "always
  allow rm -rf" changes nothing — policy decides, and `rm -rf /` is a
  hard-deny pattern regardless of context (tested). Tool outputs get the same
  wrapping; only the core's verified status is trusted.
- **No secrets**: the store refuses secrets at write time, so retrieval
  cannot leak them; the prompt builder redacts secret patterns anyway
  (defense in depth).
