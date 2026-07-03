# ai201-project4-provenance-guard

Provenance Guard is a backend system that classifies submitted text as likely AI-generated, likely human-written, or uncertain, returns a confidence score, shows a transparency label, logs every decision, and lets creators appeal. Full design rationale lives in [planning.md](planning.md); this README documents what was actually built, tested, and why.

## Architecture Overview

A submission enters through `POST /submit` with `text` and `creator_id`. The route generates a unique `content_id`, then runs the text through two independent detection signals: a Groq LLM call (`get_llm_attribution_score`) and a stylometric heuristic check (`get_stylometric_score`). Their two scores are combined into a single `confidence` value and an `attribution` label (`combine_scores`), which is then mapped to the exact transparency label text a reader would see (`get_transparency_label`). The full result — both individual signal scores, the combined confidence, the attribution, and the original text — is written to a structured audit log entry before the JSON response (`content_id`, `attribution`, `confidence`, `label`, `signals`) is returned to the client. `POST /submit` is rate-limited per client to protect against flooding.

If a creator disagrees with their result, `POST /appeal` accepts their `content_id` and `creator_reasoning`. It looks up the original log entry, flips its `status` to `under_review`, and appends a new log entry recording the appeal alongside the original decision — attribution and confidence are never silently recalculated. `GET /log` exposes the full structured log for review.

```text
POST /submit                              POST /appeal
   |                                          |
   v                                          v
generate content_id                  find original entry by content_id
   |                                          |
   v                                          v
signal 1: Groq LLM score          set status = "under_review"
   |                                          |
   v                                          v
signal 2: stylometric score        append appeal log entry
   |                                          |    (original text, scores,
   v                                          |     + creator_reasoning)
combine_scores -> confidence, attribution     |
   |                                          v
   v                                   return confirmation
get_transparency_label -> label text
   |
   v
append audit log entry
   |
   v
return JSON response
```

## Detection Signals

### Signal 1 — Groq LLM attribution score (`get_llm_attribution_score`)

Sends the submitted text to `llama-3.3-70b-versatile` with a prompt asking it to judge how AI-generated the text feels based on style, phrasing, tone, and semantic coherence, returning a `0.0`-`1.0` score. This was chosen because it captures *holistic* properties — the kind of thing a human reader notices ("this reads generically" or "this has a distinct voice") that are hard to reduce to countable structural rules.

**Why this signal:** it's the only one of the two that can reason about meaning and phrasing rather than just counting characters, which is where a lot of AI-writing "tells" actually live (generic transitions, hedging language, overly balanced argument structure).

**What it misses:** confirmed directly in testing — a formal, jargon-heavy human-written passage about monetary policy scored `llm_score: 0.7`, well into AI-leaning territory, purely because polished/academic prose reads similarly to typical LLM output. It can also be fooled by AI text a human has substantially rewritten, since the rewriting removes the stylistic tells it's judging.

### Signal 2 — Stylometric heuristics (`get_stylometric_score`)

Computes two structural metrics and averages them:
- **Sentence length variance** — the variance in word-count across a text's sentences. Low variance (uniform sentence lengths) scores as more AI-like. Skipped (returns a neutral `0.5`) when there are fewer than 4 sentences, since variance from 2-3 data points is statistical noise regardless of how it's normalized.
- **Punctuation density** — punctuation characters per word. Low density scores as more AI-like (plainer, more "sanitized" text).

**Why this signal:** it's structural rather than semantic, so it fails independently of signal 1 — a genuinely different way of catching uniformity that doesn't depend on the LLM's own judgment being right.

**What it misses, and what changed:** the original design (see planning.md) also included type-token ratio (vocabulary diversity) as a third metric. Testing showed it consistently misread genuinely AI-generated text as human-like, even after correcting for its known length confound (Root TTR / Guiraud's index). This was tested twice — once on a short sample, once on a longer sample deliberately written with repetitive AI-style phrasing to give it a fair chance — and it failed both times, diluting the two metrics that were working. It was dropped; see Spec Reflection below for the full account. The remaining two metrics still struggle on short content in general (under ~40 words / 4 sentences), and on intentionally repetitive creative writing (poems, lyrics), which can look artificially "uniform" the same way AI text does.

### Combining the two signals

```
combined_score = (0.65 * llm_score) + (0.35 * stylometric_score)
disagreement_penalty = 0.15 * abs(llm_score - stylometric_score)
confidence = combined_score - disagreement_penalty
```

LLM score is weighted higher (0.65) because testing consistently showed it discriminates better than the structural signal. The disagreement penalty deliberately pulls `confidence` down whenever the two signals disagree — this means the system requires *both* signals to roughly agree before it will confidently call something AI-generated, which was a deliberate choice given the project's stated priority: a false accusation of AI use is worse than a missed detection.

## Confidence Scoring

**What a score means:** `0.0` = strongly human-like, `0.5` = mixed/unclear evidence, `1.0` = strongly AI-like. Thresholds are asymmetric on purpose: `confidence >= 0.80` → `likely_ai`, `confidence <= 0.25` → `likely_human`, anything in between → `uncertain`. The AI bar is deliberately harder to clear than the human bar, so the system defaults to caution rather than confidently accusing a human writer.

**How this was validated:** rather than trust the formula by inspection, every stage was tested against real text and the actual API output was hand-checked against the formula's predicted result (this caught a real bug — a stale, unrestarted server returning pre-update values that didn't match hand-calculated expectations). Across 8+ test submissions spanning intentionally-AI, intentionally-human, and borderline text, confidence scores ranged from `0.150` to `0.805` and reached all three label categories — not a constant value regardless of input.

**Two examples showing meaningfully different scores** (both from real test runs):

| | Text (abridged) | llm_score | stylometric_score | confidence | attribution |
|---|---|---|---|---|---|
| High-confidence | "Effective communication is essential for success in any organization. It plays a critical role..." (uniform, low-punctuation, formulaic paragraph) | 0.80 | 0.825 | **0.805** | `likely_ai` |
| Lower-confidence | "honestly i just want to vent for a sec. my landlord still hasnt fixed the heater..." (casual, irregular) | 0.10 | 0.713 | **0.223** | `likely_human` |

The two signals don't just move together mechanically — on the second example, the stylometric score alone (`0.713`) actually leans AI-like (short, informal text can score unpredictably on structural metrics), but the LLM's strong human read (`0.10`) combined with the disagreement penalty still pulls the final confidence decisively toward `likely_human`. This is the combination formula doing real work, not just echoing one dominant signal.

**If deploying this for real:** the biggest gap is that neither signal has been calibrated against a real labeled dataset — every threshold and weight here is a reasoned guess validated against a handful of hand-picked examples, not statistics. A production version would need a labeled corpus to actually tune `LLM_WEIGHT`, `DISAGREEMENT_PENALTY_WEIGHT`, and the stylometric normalization constants, plus ongoing recalibration as LLM writing styles evolve.

## Transparency Label

The exact text returned to a reader, mapped directly from the confidence score (`get_transparency_label` in `detector.py`):

| Variant | Confidence range | Exact label text |
|---|---|---|
| High-confidence AI | `>= 0.80` | "This content is likely AI-generated. We are fairly confident in this label, but creators can appeal if they believe it is incorrect." |
| High-confidence human | `<= 0.25` | "This content is likely human-written. We are fairly confident in this label." |
| Uncertain | `0.25 < x < 0.80` | "We could not classify this content confidently. The signals are mixed or weak, so no strong attribution claim is shown." |

All three were confirmed reachable with real submissions (see Confidence Scoring examples above, plus an `uncertain` result at confidence `0.686` on a borderline formal-writing sample).

## Rate Limiting

`POST /submit` is limited to **5 requests per minute and 50 per day per client** (via Flask-Limiter, in-memory storage, keyed by remote address).

**Reasoning:** A legitimate creator submitting their own work isn't going to hit `/submit` rapid-fire — realistically they submit one piece, read the result, maybe revise and resubmit once or twice while drafting. 5 per minute comfortably covers that kind of iterative testing/editing burst without feeling throttled, while still being far below what a scripted flood would attempt. 50 per day caps sustained abuse (or a buggy client retrying in a loop) while still covering a creator submitting many separate pieces of work over a day. Each submission also costs a real Groq API call against a free-tier quota, so keeping per-client limits modest protects the whole system's shared quota from being exhausted by one client, not just guards against malicious abuse.

**Evidence:** sending 12 rapid requests in a row (limit is 5/minute) produced:
```
200
200
200
200
200
429
429
429
429
429
429
429
```
The first 5 succeeded; every request after that was rejected with `429` until the window resets.

## Audit Log

Every attribution decision and appeal is written to a structured, in-memory JSON log (`AUDIT_LOG` in `app.py`), retrievable via `GET /log`. Each entry captures:

- `timestamp` — ISO-8601 UTC
- `content_id` — unique per submission
- `event_type` — `"submission"` or `"appeal"`, so the two can be told apart
- `attribution` — `likely_ai` / `likely_human` / `uncertain`
- `confidence` — the combined score
- `llm_score` and `stylometric_score` — both individual signal scores
- `status` — `"classified"` or `"under_review"`, showing whether an appeal has been filed
- `appeal_reasoning` — `null` for submissions, populated with the creator's reasoning for appeal entries
- `text` — the original submitted content, so a reviewer has full context without cross-referencing

**Sample entries** (from a live test run — one `uncertain`, one `likely_ai` that was then appealed, one `likely_human`):

```json
{
  "attribution": "uncertain",
  "confidence": 0.26,
  "content_id": "3f3b7e8e-08ab-4498-a9e1-cc8ae6859f71",
  "creator_id": "writer-amara",
  "event_type": "submission",
  "llm_score": 0.2,
  "status": "classified",
  "stylometric_score": 0.5,
  "appeal_reasoning": null,
  "timestamp": "2026-07-03T05:29:46.276262+00:00"
},
{
  "attribution": "likely_ai",
  "confidence": 0.8049990807133665,
  "content_id": "82e86fbd-48db-420e-b7bc-b466fb15efac",
  "creator_id": "writer-devon",
  "event_type": "submission",
  "llm_score": 0.8,
  "status": "under_review",
  "stylometric_score": 0.8249954035668321,
  "appeal_reasoning": null,
  "timestamp": "2026-07-03T05:29:58.021292+00:00"
},
{
  "attribution": "likely_ai",
  "confidence": 0.8049990807133665,
  "content_id": "82e86fbd-48db-420e-b7bc-b466fb15efac",
  "creator_id": "writer-devon",
  "event_type": "appeal",
  "llm_score": 0.8,
  "status": "under_review",
  "stylometric_score": 0.8249954035668321,
  "appeal_reasoning": "This is my own writing style from years of technical documentation work. I did not use AI to generate this.",
  "timestamp": "2026-07-03T05:30:23.176885+00:00"
},
{
  "attribution": "likely_human",
  "confidence": 0.22262896825396827,
  "content_id": "c8b9a8e9-37cd-484e-a1fb-f0f199eb9026",
  "creator_id": "writer-priya",
  "event_type": "submission",
  "llm_score": 0.1,
  "status": "classified",
  "stylometric_score": 0.7131448412698413,
  "appeal_reasoning": null,
  "timestamp": "2026-07-03T05:30:44.469797+00:00"
}
```

Note the second and third entries share a `content_id`: the submission was flagged `likely_ai`, the creator appealed, and both the original entry's `status` and the new appeal entry reflect `under_review` — the original attribution/confidence are preserved untouched, since no automated re-classification happens on appeal.

## Known Limitations

1. **Short AI-generated content struggles to reach `likely_ai`.** Both stylometric metrics need enough text to say anything meaningful — sentence variance is skipped entirely under 4 sentences, and even a clearly AI-sounding 3-sentence, 45-word paragraph only reached `confidence: 0.565` (`uncertain`), well short of the `0.80` bar. Reaching `likely_ai` required a much longer, deliberately uniform 74-word sample. This is a direct consequence of using structural metrics that only manifest with sufficient text — a short AI-generated comment or caption will likely land as `uncertain`, not `likely_ai`, even when a human reader would say it obviously reads like AI output.

2. **Formal, technical human writing scores ambiguously.** A genuinely human-written passage about monetary policy — dense academic prose — scored `llm_score: 0.7` (AI-leaning) and `stylometric_score: 0.672` (also AI-leaning, likely from its low comma density and uniform sentence structure), landing as `uncertain` rather than clearly `likely_human`. Both signals independently misread the same property (polished, formal, low-variance prose) as AI-like, so they don't cross-check each other in this case the way they do elsewhere — this is a shared blind spot, not something the disagreement penalty catches.

## Spec Reflection

**How the spec helped:** planning.md's "How the signals will be combined" section pinned down exact weights (`0.65`/`0.35`) and a disagreement-penalty formula (`0.15 * |llm_score - stylometric_score|`) before any code was written. That made it possible to verify the implementation was actually correct by hand-computing the expected `confidence` for a given pair of signal scores and comparing it to what the API returned — a bug (stale server not reflecting new code) was caught this way during Milestone 4 testing, because the numbers didn't match what the spec's formula predicted.

**Where implementation diverged from the spec, and why:** planning.md's Detection Signals #2 section lists sentence length variance, type-token ratio, punctuation density, repetition, and average sentence complexity as candidate stylometric metrics. The final implementation uses only two — sentence length variance and punctuation density — and drops type-token ratio.

This wasn't a shortcut; it came out of testing. During Milestone 4 calibration, a text everyone would confidently call AI-generated produced only a middling combined confidence score (`uncertain` rather than `likely_ai`). Investigating why revealed that raw type-token ratio is mostly driven by text length, not authorship — short passages naturally show high vocabulary diversity regardless of who wrote them. Correcting for this with Root TTR (Guiraud's index: `unique_words / sqrt(total_words)`) helped only marginally. To rule out "it just needs more text," a second, longer sample was deliberately written with repetitive AI-style phrasing ("furthermore," "moreover," "it is important to note") to give the metric a fair chance — and it still read as human-like. Since it was actively diluting the two metrics that were discriminating correctly (sentence variance in particular scored the repetitive sample as strongly AI-like, 0.87), type-token ratio was dropped rather than kept to match the original list. Re-testing across 5 samples (2 confidently-labeled, 2 borderline, 1 long-form) after dropping it showed every score moving in the intuitively correct direction with no regressions.

## AI Usage

1. **Generating the confidence-scoring combination logic (Milestone 4).** Directed the AI tool to implement `combine_scores()` directly from planning.md's stated formula (weights and disagreement penalty), plus a second stylometric signal function based on the Detection Signals section. It produced a working implementation, including an initial version of the stylometric signal that used raw type-token ratio as one of three sub-metrics. This was overridden after testing: raw TTR was flagged as unreliable on short text (a known statistical confound — TTR is driven by length, not authorship), so it was first corrected to Root TTR, and when that still didn't discriminate correctly even on a fair, longer test case, it was removed from the implementation entirely rather than kept to match the original plan.

2. **Debugging a result that didn't match expectations.** When a combined `confidence` value returned by the API didn't match the value hand-calculated from planning.md's formula, the AI tool's suggested debugging approach was to independently verify the running server was actually serving the latest code (Flask wasn't running in debug/auto-reload mode). This caught a real, recurring issue across multiple milestones — edits to `app.py`/`detector.py` require a manual server restart — rather than incorrectly concluding the formula itself was implemented wrong.

## Walkthrough Video

🎥 **Project Walkthrough:** [Video demo]([https://www.youtube.com/watch?v=lD65hC3HxyI])
