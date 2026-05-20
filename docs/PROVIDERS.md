# Providers

`vigil` keeps the LLM and embedding backend behind two ABCs in
`vigil.providers`:

- `LLMProvider.generate(prompt, system=None, *, stream=True)` — yields
  text deltas as `str`. Non-text events (tool-call deltas, stop reasons,
  usage stats) are silently dropped at this layer; if a v0.4 caller needs
  them, they go through a different method.
- `EmbeddingProvider.embed(texts)` and `.dim` — fixed-dimension float
  vectors. The `dim` is load-bearing; see "Index versioning" below.

## What ships in v0.3

| Provider | LLM | Embedding | Notes |
|----------|:---:|:---------:|-------|
| `ollama` | yes | yes | Default. Free, fully local, runs on a laptop. |
| `none`   |  — | — | Disable — falls back to keyword search. |

The abstraction is real, but only the Ollama path is implemented in v0.3.
This is the abstraction floor — the seam exists so v0.4 can add backends
without touching call sites.

## Roadmap (v0.4)

- `anthropic` — Claude API. Streaming, batch embeddings.
- `openai` — OpenAI / Azure OpenAI.
- `http` — generic HTTP gateway. For corporate deployments where the only
  egress is an internal proxy that brokers an LLM, with OpenAI-shaped or
  Anthropic-shaped JSON.

## Configuration

Precedence (highest first):

1. Environment variables (`TEAMMATE_LLM_*` / `TEAMMATE_EMBEDDING_*`).
2. Per-repo: `<brain_root>/.vigil/config.toml`.
3. Per-user: `~/.vigil/config.toml`.
4. Built-in defaults (Ollama on `localhost:11434`).

### TOML schema

```toml
# .vigil/config.toml
[llm]
provider = "ollama"
model    = "llama3.2:3b"
host     = "http://localhost:11434"

[embedding]
provider = "ollama"
model    = "nomic-embed-text"
host     = "http://localhost:11434"
```

Per-section keys recognized in v0.3:

| Key | Meaning |
|-----|---------|
| `provider` | Backend name. v0.3: `ollama` or `none`. |
| `model` | Model identifier (e.g. `llama3.2:3b`). |
| `host` / `base_url` | Transport endpoint. |
| `api_key_env` | Env-var name to read the API key from (v0.4). |
| `timeout_s` | Per-request timeout (default 30s). |
| `dim` | Embedding dimension override. |

### Environment variables

| Var | Purpose |
|-----|---------|
| `TEAMMATE_LLM_PROVIDER` | Override the LLM provider name. |
| `TEAMMATE_LLM_MODEL` | Override the LLM model. |
| `TEAMMATE_LLM_HOST` (alias `_BASE_URL`) | Override the LLM endpoint. |
| `TEAMMATE_LLM_API_KEY_ENV` | Env-var name to read the API key from. |
| `TEAMMATE_EMBEDDING_PROVIDER` | Same, for the embedding side. |
| `TEAMMATE_EMBEDDING_MODEL` | … |
| `TEAMMATE_EMBEDDING_HOST` | … |
| `TEAMMATE_EMBEDDING_API_KEY_ENV` | … |

Run `vigil config show` to see the effective config. Any `*api_key*`
value (other than `_env` indirection) is redacted in the output.

## Index versioning (read this if you change providers)

The first time `vigil index` runs with a given embedder, it stamps the
index file with `(provider, embedding_model, embedding_dim, created_at,
vigil_version)`. Every subsequent open re-checks the stamp.

If the stamp disagrees with the configured embedder — different provider,
different model, different `dim` — `vigil` refuses to query and tells
you to rebuild:

```
Index was built by `nomic-embed-text` (768d, ollama) but current config is
`text-embedding-3-small` (1536d, openai). Run `vigil index --rebuild`
to re-embed under the new provider.
```

This is non-optional. Embeddings produced by different models live in
different geometries — cosine similarity across them is silently meaningless.
The stamp turns a silent corruption into a loud error.

`vigil index --rebuild` wipes the chunks table, re-stamps the index
under the current provider, and re-embeds from scratch.

## Migration notes

- `from vigil.rag.ollama import OllamaClient` still works in v0.3 but
  emits a `DeprecationWarning`. Import from `vigil.providers` instead.
- `index_paths(... ollama=...)` is now `index_paths(... embedder=...)`.
  Pass an `EmbeddingProvider`, not an `OllamaClient`.
- `answer(... ollama=...)` is now `answer(... embedder=..., llm=...)`. The
  embedder and LLM can be different providers (you can run a local Ollama
  embedder alongside an Anthropic LLM in v0.4, for example).

## When to override `host`

The default Ollama host is `http://localhost:11434`. Override `host` when:

- You run Ollama on a sidecar machine: `host = "http://10.0.1.4:11434"`.
- Your team runs an internal Ollama behind a corporate proxy: see
  `examples/configs/airgapped-ollama.toml`. Set `HTTPX_VERIFY` to point
  `httpx` at your custom CA bundle if your proxy injects TLS.
