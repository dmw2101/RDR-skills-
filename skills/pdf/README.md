# PDF skill reference

Provenance status: **external reference only** in [source.json](source.json), pinned to Anthropic commit `33375500bcea98d610eb30ce10ac4e59b89c390d`. The repository currently also contains an existing experimental `initial.md` snapshot and compiled artifacts. This does not clear upstream usage terms. Supporting `FORMS.md`, `REFERENCE.md`, and scripts are absent; evaluation uses the supplied snapshot, not a complete upstream skill package.

The [entry skill](https://github.com/anthropics/skills/blob/33375500bcea98d610eb30ce10ac4e59b89c390d/skills/pdf/SKILL.md) covers extraction, creation, page operations, OCR, forms, and encryption. Its [forms guide](https://github.com/anthropics/skills/blob/33375500bcea98d610eb30ce10ac4e59b89c390d/skills/pdf/FORMS.md) and [advanced reference](https://github.com/anthropics/skills/blob/33375500bcea98d610eb30ce10ac4e59b89c390d/skills/pdf/REFERENCE.md) supply additional procedural knowledge.

The [upstream license](https://github.com/anthropics/skills/blob/33375500bcea98d610eb30ce10ac4e59b89c390d/skills/pdf/LICENSE.txt) restricts copying, retention outside Anthropic services, derivative works, and redistribution. This directory records links and provenance; importing or converting that source requires suitable permission from Anthropic.

A candidate for a reusable PDF source is [PDF Pro](https://github.com/DXBMark/pdf-pro), which has an [MIT license](https://github.com/DXBMark/pdf-pro/blob/main/LICENSE) and guides for extraction, forms, editing, redaction, and validation. It has not been imported or evaluated here. After selecting a reusable source, PDF integration will need its feature/condition schema, rule extraction, and support for its linked reference documents.
