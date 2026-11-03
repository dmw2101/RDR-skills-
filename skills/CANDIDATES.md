# Procedural skill candidates

Start with each folder's `initial.md`; its `README.md` explains the original
source, benchmark pairing, and any adaptation. Word counts below are whitespace
counts of the inspection files, **not model-token counts**.

| Candidate | Entry point | Words | What is included |
|---|---|---:|---|
| SOPBench Bank (Li et al.) | [initial.md](sopbench_bank/initial.md) | 1,654 | Original bank prompt; account, payment, loan, and administrative actions with AND/OR/ordered preconditions |
| Amazon SOP-Bench video annotation | [initial.md — local only](sopbench_video_annotation/initial.md) | 3,442 | Original video/LiDAR annotation workflow, validation, quality control, and recovery |
| Amazon SOP-Bench referral abuse v2 | [initial.md](sopbench_referral_abuse/initial.md) | 1,503 | Investigation stages, temporal/relationship checks, risk thresholds, history, and enforcement |
| Tau-Knowledge / Tau-Banking | [initial.md](tau_banking/initial.md), [document index](tau_banking/DOCUMENTS.md) | 142,379 | All 698 original knowledge documents, plus a readable combined view; a corpus, not one existing skill |
| SkillsBench COBOL GL reconciliation | [initial.md](cobol_gl_reconciliation/initial.md), [original skills](cobol_gl_reconciliation/procedures/) | 3,709 | Six original skills: COMP-3, EBCDIC overpunch, posting codes, GNUCobol batch processing, reversal cancellation, and FX triangulation |
| Telecom — already prepared | [initial.md](telecom/initial.md) | 3,715 | Original account policy and technical-support manual |

Each candidate has a `source.json` containing its exact upstream commit and
SHA-256 checksums, with applicable license notices retained. Original instructions
were not paraphrased. The COBOL bundle and banking corpus add only inspection
headings/separators; their original individual files are also included.

## Benchmark status

- **Bank:** [SOPBench](https://github.com/Leezekun/SOPBench), not Amazon SOP-Bench.
- **Video and referral abuse:** [Amazon SOP-Bench](https://github.com/amazon-science/SOP-Bench);
  the referral candidate is specifically `referral_abuse_detection_v2`.
- **Banking corpus:** [Tau-Knowledge / Tau-Banking](https://arxiv.org/abs/2603.04370),
  using tau2-bench `v1.0.1` sources, not the later 2.0 release.
- **COBOL:** one `tasks-extra/cobol-gl-batch-reconcile` task in
  [SkillsBench](https://github.com/benchflow-ai/skillsbench); not asserted to be
  part of the paper's standard evaluated suite.

The five additions are **inspection assets only**: no schemas, IRs, trees,
benchmark installations, or three-arm evaluation adapters have been added for
them. No task datasets, expected answers, or oracle solutions are imported into
these skill folders. Telecom retains its separately prepared offline runtime.

## Video license exception

The video domain includes extra distribution/access-control conditions beyond
the repository's CC BY-NC 4.0 license. Its `initial.md` and supporting references
are present locally but **ignored by Git**. Only its README, provenance, and
license notices are intended for version control. Review
[DOMAIN_LICENSE.md](sopbench_video_annotation/DOMAIN_LICENSE.md) before reusing or
redistributing it. The [pinned source SOP](https://github.com/amazon-science/SOP-Bench/blob/f6d43858b94cf439854038101bce0eecd3e16eae/src/amazon_sop_bench/benchmarks/data/video_annotation/sop.txt)
remains available when the local copy is absent. Amazon's referral-abuse SOP
retains the upstream noncommercial license.

## Reimport/check — PowerShell 5.1, from the repository root

Recreate missing files from pinned sources, without overwriting edited files:

```powershell
py -3.12 scripts/import_skill_candidates.py
```

Verify imported files against already downloaded source checkouts, offline:

```powershell
py -3.12 scripts/import_skill_candidates.py --check-only
```

These commands need Git and Python, not API keys or model dependencies. Source
checkouts are under ignored `benchmarks/local-data/`. The import command also
creates the local-only video review copy; it does not publish it or run a model.
