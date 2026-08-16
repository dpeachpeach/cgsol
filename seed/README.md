# Seed corpus

Drop-in for the solution repo. Data + invariants for `make seed`; the runner itself is not built yet.

## Contents

| File | What |
|---|---|
| `issues.yaml` | 22 issues — 15 eligible, 6 decline candidates, 1 held back |
| `labels.yaml` | 13 labels — 10 pipeline states + 3 tiers |
| `sanitize.py` | Body sanitizer + leak assertions. Already applied to `issues.yaml` |

## What `make seed` must do

1. **Labels first, then issues.** `labels.yaml` order is not meaningful; creation order is (issues reference labels).
2. **Skip `hold_back: true`.** Issue `34143` is reserved for filing live on camera.
3. **Idempotent — skip if an open issue with the same title exists.** Re-running must not duplicate.
4. **Sleep 1–2s between creates.** GitHub secondary rate limits return `403`, not `429`, so naive retry-on-429 will not catch it.
5. **Do not assign `tier`.** It is `null` for every entry by design — triage assigns it. A seed that pre-assigns tiers is scoring its own exam.
6. **Do not apply pipeline labels either.** Everything enters at `needs-triage` via the orchestrator, not the seeder.

## Field reference

```yaml
source: 36406            # apache/superset issue number (provenance only, never a live #ref)
disposition: eligible    # eligible | decline  — ground truth for scoring triage
decline_reason: null     # set on decline: why a human is required
assessment: confident    # confident | plausible | declined  — prior confidence
tier: null               # ALWAYS null in seed; triage assigns trivial|medium|hard
hold_back: false         # true = do not file; reserved for live demo
body: |                  # already sanitized
```

`disposition` is the answer key. The scout does not see it — it exists so triage accuracy is measurable
rather than asserted.

## Decline candidates

Six, spanning five distinct reasons, so "declined" is a reasoned category rather than one archetype:

| Source | Reason | Why a human is required |
|---|---|---|
| 33756 | `product-semantics` | What *should* the row limit do on chart-type switch? Undecided upstream |
| 36876 | `product-semantics` | Should "Clear All" auto-apply? UX decision, not a bug |
| 34682 | `feature-request` | New behavior, not a defect |
| 36223 | `security-design` | Admin password reset needs a security model decision first |
| 36670 | `design-review` | Subjective typography judgement |
| 33900 | `algorithm-policy` | Needs a colour-assignment policy before any code |

These are real unresolved Superset issues, not strawmen — each sat open for months precisely because
it needs a decision no agent can make.

## Sanitization — do not skip

`issues.yaml` bodies are already sanitized. If you re-import from source, run `sanitize.py` first and
call `assert_clean()` on every body. Three rewrites matter:

- **`@mentions`** notify real Apache contributors
- **`owner/repo#N` and issue URLs** create backlinks on `apache/superset`
- **bare `#N`** autolinks to an unrelated issue in the target repo

Provenance footers use plain text (`apache/superset issue 34143`) for exactly this reason — a `#`-style
footer would autolink to the wrong issue in the fork.

Verified on the current corpus: zero surviving mentions, refs, or backlinking URLs.

## Known gaps

- **Count is 22, plan calls for ~20 with a 6/6/4/4 mix.** The 15 eligible skew toward frontend chart
  bugs; there is no dependency-bump or lint-class work in the corpus, which the "security/dependency
  backlog" framing implies. Worth adding 3–4 before the demo.
- **`tier` unassigned**, per instruction.
- **Escalation-reason labels not created** — only pipeline + tier exist so far.
