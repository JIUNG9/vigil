# Threat Model — teammate signed compliance attestations

The PDF attestation produced by `teammate score --sign` is signed using
[sigstore](https://www.sigstore.dev/) keyless: a Fulcio-issued certificate
bound to a GitHub OIDC identity, with the signing event recorded in the
public Rekor transparency log.

## What the signed PDF DOES prove

- **Provenance:** the PDF was produced by a GitHub-authenticated identity
  whose subject is recorded in the certificate.
- **Integrity:** the PDF has not been modified since the signature was
  generated. Any byte-level change invalidates the signature.
- **Timestamp:** the signing event is recorded in Rekor with an inclusion
  proof. Even if the signing identity is later revoked, the proof remains
  for the recorded moment.
- **Non-repudiation (within the OIDC trust model):** assuming GitHub's
  OIDC issuer is trusted, the signing identity cannot later credibly deny
  signing.

## What the signed PDF does NOT prove

- **Compliance.** The score is mechanical. It reflects what the local
  probes can verify on the target commit at the recorded timestamp. A
  team can game any individual probe (e.g., add an empty CODEOWNERS file
  to make `codeowners-exists` pass without anyone actually owning code).
- **GitHub-side state at the time of audit.** Probes that fall back to
  `partial` haven't actually checked the GitHub API. The signed PDF
  records that fact honestly.
- **Continued compliance.** The attestation is a snapshot. The repo can
  diverge afterwards. Re-run periodically.

## Verification

For an auditor who has the PDF, the `.sig`, and the `.crt` produced by
`teammate score --sign`:

```bash
pip install sigstore
sigstore verify-blob compliance-vault/attestations/<timestamp>.pdf \
  --signature  compliance-vault/attestations/<timestamp>.pdf.sig \
  --certificate compliance-vault/attestations/<timestamp>.pdf.crt \
  --certificate-identity <expected-github-actions-oidc-identity> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The expected `--certificate-identity` for the example artifact shipped in
this repo is:

```
https://github.com/placen-org/teammate/.github/workflows/sign-example.yml@refs/heads/main
```

For attestations produced by a team's own `--sign` flow, the identity
will be the user's GitHub OIDC subject (e.g.,
`https://github.com/<user>` or a CI workflow URL). The verifying party
needs to know what identity to expect; this is part of the team's audit
trust policy and should be documented in their compliance program.

## What if the Fulcio root rotates?

`sigstore verify-blob` fetches the current trust root from a TUF
repository at verification time. Rekor inclusion proofs remain valid
across root rotations because the proof is anchored in the log itself,
not in the current root. In practice, signatures produced today will
verify in five years assuming sigstore's TUF infrastructure remains
operational.

## What if the attestation file is older than the cert validity window?

Fulcio short-lived certificates expire (~10 minutes). Long-term
verifiability comes from Rekor: the verifier checks that the signature
was made *while the cert was valid* by consulting Rekor's record of the
signing event. The CLI handles this transparently.

## What is intentionally out of scope

- **Hardware attestation.** teammate runs on the user's laptop. The
  signed PDF doesn't claim anything about the integrity of the execution
  environment.
- **Replay protection.** Two identical score runs at the same timestamp
  on the same commit will produce equivalent PDFs. This is by design;
  the attestation is the *output*, not the *execution trace*.
- **Cryptographic agility for the catalogs themselves.** The control
  catalog YAMLs are not signed in v0.1. A malicious catalog modification
  would change probe-to-control mappings without invalidating the PDF
  signature. v0.2 will add catalog signing as a separate concern.

## Reporting

Suspected forged or replayed attestations: please report via the
GitHub Security Advisory mechanism described in `/SECURITY.md` at the
repo root.
