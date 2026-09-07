# English editorial comparison

This evaluation uses real Gemini writing/editing calls and the pinned Humanizer
skill. Promptfoo calls `provider.py`; an independently configured OpenAI model
grades the resulting articles without receiving the A/B/C labels. API calls use
the existing `.env` configuration and incur provider charges. Keys are never
written to the results.

## Run

Requires Node >=22.22.0, Python 3.11+, the existing Python requirements and the
configured Gemini/OpenAI credentials. Promptfoo is pinned in package-lock.json.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm ci
npm test
npm run eval
```

A nonzero Promptfoo exit can mean an article failed the factual checks. It is
not automatically a tool failure; inspect the exported JSON. The evaluation
never invokes the publication writer, creates website articles, updates
published state, generates images, pushes commits or deploys.

## Comparison contract

- A loads the old writing functions from the git commit in `manifest.json`.
- B uses the new writer with the legacy editor.
- C uses the exact B draft with the pinned Humanizer editor.
- All three receive the same source packet, observation date, two-section
  outline, writer model, temperature and token limit. B/C draft hashes must match.
- Source packets are compact, manually checked excerpts plus explicitly labeled
  editorial notes. They are NOT complete raw web snapshots or a live research
  benchmark. The evidence and outline stages are frozen to isolate writing.
- Both section writer and assembler run. The quality gate is recorded without revision loops. For this offline comparison,
  an editor still runs on a held draft; production stops at the failed gate.
  The new source audit then checks each output. Held drafts
  remain visible in the results and must not be interpreted as approved output.
- The independent judge checks against the same packet. Its numerical style
  scores are model judgments, not human preference or traffic measurements.
- A/B compares writing changes; B/C compares editors. Neither measures retrieval
  improvement, production throughput, title CTR, image quality or deployed SEO.
- `originals/` holds byte-for-byte copies of three existing English articles.
  Those are a separate historical comparison: their original research inputs
  are unknown, so do not call them the controlled A group.

## Reproducibility and cost

Artifacts are under `results/<fingerprint>/<case>/`. They include drafts, final
texts, source/draft hashes, audit outcomes, model identities, calls, durations
and token usage reported by Gemini. Judge tokens are not measured by the current
existing Responses client, so totals must be labeled writer-side only.

Completed cases are reused to resume interrupted runs. Each metadata record
reports `reused`; B/C intentionally share one draft. For a fresh stochastic run,
move the prior results directory aside first. Do not edit pipeline code during a
run. The fingerprint includes pipeline/evaluator code, cases, models and skill.
One run per case is an exploratory comparison, not a statistical performance claim.

Regression tests run without APIs. They cover punctuation/data preservation,
missing evidence, reviewer failures, non-publishable fallbacks, final chart/metadata
audit, build failures and completion-state order. Lexical preservation alone does
not establish semantic truth; the audit and a human reading remain necessary.

## Audit-only replay

To recheck saved articles after an auditor change, copy the YAML config and add
`replay_run: <original fingerprint>` beside each provider's `variant`. Keep it in
`evals/` so relative provider paths resolve, or use absolute `file://` paths.
Replay validates source and draft hashes, leaves all nine article texts unchanged,
and records the old issues plus new audits. The independent judge/style scores
are reused, not regenerated. This separates evaluator fixes from writer gains.
The report distinguishes zero-revision comparison failures from a separate run
with the production revision loop enabled.
