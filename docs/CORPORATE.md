# Corporate Deployment Guide

## Why this doc exists

`vigil` was built local-first, but the first real-world adopters all run
inside corporate networks where the LLM is an *internal* Ollama mirror, egress
runs through a TLS-inspecting proxy, and the laptop trusts a private CA. None
of that requires a code change — it requires three knobs (host, proxy, CA
bundle) and a fast diagnostic when nothing answers. This guide covers the
knobs and the diagnostic.

## The four corporate scenarios

| Scenario | Substrate | LLM source | Proxy / CA |
|---|---|---|---|
| Personal laptop | Local git clone | `localhost:11434` Ollama | None |
| Corporate laptop, internet-allowed | Local clone or internal Git | Internal Ollama mirror | Corporate proxy |
| Corporate laptop, restricted | Internal Git only | Internal Ollama mirror | Proxy + custom CA |
| Air-gapped | Internal Git mirror | Internal Ollama mirror | None (offline) |

The first row is the README quickstart. Rows two through four all need the
configuration in this doc.

## Configuring the internal Ollama host

Per-repo `.vigil/config.toml`:

```toml
[llm]
provider = "ollama"
model    = "llama3.2:3b"
host     = "https://ollama.internal.your-team.local:11434"

[embedding]
provider = "ollama"
model    = "nomic-embed-text"
host     = "https://ollama.internal.your-team.local:11434"
```

Or via environment variables (override the file):

```bash
export TEAMMATE_LLM_HOST="https://ollama.internal.your-team.local:11434"
export TEAMMATE_EMBEDDING_HOST="https://ollama.internal.your-team.local:11434"
# or, the common Ollama-native var (file `host` still wins if set):
export OLLAMA_HOST="https://ollama.internal.your-team.local:11434"
```

The TOML `host` value beats every env var. Use env vars for one-off
overrides; commit the file when you want the team default. See
[`examples/configs/corporate-ollama.toml`](../examples/configs/corporate-ollama.toml)
for a copy-paste-ready starter.

## Proxy configuration

`vigil` makes outbound HTTP calls via `httpx`. `httpx` reads `HTTPS_PROXY`,
`HTTP_PROXY`, and `NO_PROXY` from the environment — no vigil-side
configuration required.

```bash
export HTTPS_PROXY="http://proxy.your-team.local:3128"
export HTTP_PROXY="http://proxy.your-team.local:3128"
# Critical: keep traffic to the internal mirror OUT of the proxy.
export NO_PROXY="localhost,127.0.0.1,*.internal,*.your-team.local"
```

If the proxy demands authentication, embed credentials in the URL — `vigil
doctor` redacts them on display:

```bash
export HTTPS_PROXY="http://alice:hunter2@proxy.your-team.local:3128"
```

`NO_PROXY` is the most commonly missed knob. If your internal Ollama is at
`*.your-team.local` and `NO_PROXY` doesn't include it, the corporate proxy will
return `502 Bad Gateway` for traffic that should never have left the LAN.

## Custom CA bundles

When the corporate proxy MITM-inspects TLS, the certificate `httpx` sees on
the wire is signed by a private root. `httpx` honors three env vars, in this
order:

1. `SSL_CERT_FILE` — explicit PEM bundle. Honored natively by `httpx`. Use this.
2. `REQUESTS_CA_BUNDLE` — fallback. Honored by `httpx` if `SSL_CERT_FILE` is unset.
3. `HTTPX_VERIFY` — per-process override. Set to a path or `false` (last-resort
   debugging only — disabling verification leaks secrets to whoever owns the proxy).

The path varies by distro — common locations:

```bash
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt        # Debian/Ubuntu
export SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt          # RHEL/AL
export SSL_CERT_FILE=/usr/local/share/ca-certificates/corp.pem # Custom
```

Append your corporate root to the system bundle when possible — anything
linked against OpenSSL picks it up automatically that way.

## Air-gapped install

When the laptop cannot reach PyPI:

```bash
# On an internet-connected machine:
mkdir wheels && cd wheels
pip download vigil

# Copy the `wheels/` folder to the air-gapped laptop, then:
pip install --no-index --find-links=./wheels vigil
```

`vigil` itself is pure Python; the only transitive deps are `click`,
`pyyaml`, `rich`, `httpx`, and `sqlite-vec`. All wheel-only. No native
compilation step.

## Verifying with `vigil doctor`

Once the env is set, run `vigil doctor` from inside a brain repo:

```
$ vigil doctor
vigil doctor v0.3.1

[PASS] config              source=repo  llm=ollama:llama3.2:3b  embedding=ollama:nomic-embed-text
[PASS] brain               CLAUDE.md present at /home/alice/team-brain
[PASS] llm.reachable       https://ollama.internal.your-team.local:11434  42 ms
[PASS] embedding.reachable https://ollama.internal.your-team.local:11434  39 ms
[PASS] models              llama3.2:3b, nomic-embed-text both pulled
[PASS] index               provider=ollama model=nomic-embed-text dim=768 chunks=312
[PASS] proxy               HTTPS_PROXY=http://***:***@proxy.your-team.local:3128
                           NO_PROXY=localhost,127.0.0.1,*.internal,*.your-team.local
                           SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
[PASS] runtime             python=3.12.3  vigil=0.3.1

OK
```

For machine-readable output (CI gate, monitoring):

```bash
vigil doctor --json | jq .
```

Exit codes: `0` all PASS, `1` any FAIL, `2` only WARNs.

## Common issues + troubleshooting

- **DNS does not resolve `ollama.internal.your-team.local`** — VPN is not up,
  or split-horizon DNS isn't routing the internal zone through the corporate
  resolver. `dig` from the same shell that ran `vigil doctor`; if it fails,
  fix VPN / `/etc/resolv.conf` first.
- **TLS handshake fails (`CERTIFICATE_VERIFY_FAILED`)** — your corporate CA is
  not in the trust store. Export `SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt`
  (path varies by distro) and re-run.
- **Proxy returns `407 Proxy Authentication Required`** — the proxy needs
  credentials. `export HTTPS_PROXY="http://user:pass@proxy.your-team.local:3128"`.
  `vigil doctor` redacts the password in its output.
- **Proxy returns `502 Bad Gateway` when targeting the internal mirror** —
  `NO_PROXY` is missing the internal zone. Add `*.your-team.local` (or whatever
  matches your hostname) to `NO_PROXY`.
- **`Model not found` on the internal mirror** — the mirror has Ollama
  running but hasn't pulled the models. `vigil doctor` prints the model
  list; if `llama3.2:3b` or `nomic-embed-text` is missing, pull them on the
  mirror box: `ollama pull llama3.2:3b && ollama pull nomic-embed-text`.
- **Index says version mismatch after a model swap** — expected. Run
  `vigil index --rebuild` once after switching the embedding model.

## See also

- [`docs/PROVIDERS.md`](PROVIDERS.md) — provider abstraction and config schema
- [`examples/configs/corporate-ollama.toml`](../examples/configs/corporate-ollama.toml) — proxy + CA starter
- [`examples/configs/airgapped-ollama.toml`](../examples/configs/airgapped-ollama.toml) — internal mirror, no proxy
