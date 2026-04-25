# Sources & Provenance

## K-ISMS-P (Korea Information Security Management System — Personal)

The K-ISMS-P framework is published by the [Korea Internet & Security Agency
(KISA)](https://www.kisa.or.kr/) — a government agency under Korea's Ministry
of Science and ICT. The official framework is published in Korean, with
periodic version revisions.

### What this catalog ships

`catalogs/k-isms-p.yaml` contains:

- **Control IDs** — the official K-ISMS-P numbering (e.g., `1.1.1`, `2.5.1`,
  `3.7.2`). These are factual reference data, not copyrightable.
- **Domain structure** — the three-domain split (Management System,
  Protection Measures, Personal Information Processing). Public structural
  data.
- **English summaries** — written from scratch by the catalog contributor
  by reading the Korean source. **NOT verbatim translations of KISA's
  prose.** Each summary is the contributor's plain-English explanation
  written for engineers, not auditors.
- **Cross-mappings to ISO 27001:2022 Annex A** — the contributor's
  professional judgment about which K-ISMS-P controls correspond to
  which ISO controls. This is original work, not a KISA position.

### Why a derivative work, not a verbatim translation

KISA holds copyright on the official Korean text. Verbatim English
translations would require KISA's permission. The derivative-work
approach (control IDs + original English summaries) is publishable
under MIT without that permission and gives engineers the practical
information they need without the legal complexity.

If you are a Korean-speaking compliance practitioner who wants to verify
the accuracy of a summary, the official K-ISMS-P framework is at
[https://isms.kisa.or.kr/](https://isms.kisa.or.kr/). The Korean text is
the source of truth; this catalog is one practitioner's English reading
of it.

### Roadmap

- v0.1 ships the top 25 highest-impact controls.
- The full ~80-control catalog is tracked in
  [GitHub issue #1](https://github.com/placen-org/teammate/issues/1).
- Translation contributions from Korean-speaking compliance practitioners
  are explicitly welcome — please open a PR with a fresh control entry
  following the same format.

## ISO/IEC 27001:2022 Annex A

`catalogs/iso-27001-annex-a.yaml` contains:

- **Control IDs** (`A.5.1` through `A.8.34`) — factual reference data.
- **Theme grouping** (Organizational, People, Physical, Technological) —
  public structural data from the standard.
- **Engineering summaries** — written by the catalog contributor for
  engineers, not auditors. NOT the official ISO text.

The official normative text of ISO/IEC 27001:2022 Annex A is published
by [ISO](https://www.iso.org/standard/27001) and is sold (typically
USD ~150). For audit purposes, purchase the standard from ISO or your
country's standards body. The summaries here are *guidance*, not the
*standard*.

v0.1 ships 16 of the 93 Annex A:2022 controls — the ones referenced by
v0.1's probes plus their K-ISMS-P cross-mappings. v0.2 plans to switch
to OSCAL-format catalogs via [compliance-trestle](
https://github.com/IBM/compliance-trestle) for fuller coverage.

## CVE / Advisory Feeds

`teammate watch` fetches:

- **KISA RSS** at `https://www.kisa.or.kr/rss/notice.xml` — public,
  unauthenticated, Korean-language notices feed.
- **NVD CVE 2.0 API** at `https://services.nvd.nist.gov/rest/json/cves/2.0`
  — public, unauthenticated, English. NVD asks consumers to identify
  themselves via `User-Agent`; teammate sends a polite identifier. Heavy
  users should request an API key.

These are public feeds; we do not redistribute their content beyond the
local cached copy in the user's `compliance-vault/advisories/`.
