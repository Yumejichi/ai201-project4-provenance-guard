# Provenance Guard — Planning Document

## Project Goal

Provenance Guard is a backend system that classifies submitted text as likely AI-generated, likely human-written, or uncertain. It returns a confidence score, displays a transparency label, stores a structured audit log, and lets creators appeal decisions they believe are wrong.

The design goal is to be careful about false positives. On a creative platform, wrongly labeling human work as AI-generated is a worse mistake than missing some AI-generated text.

---

## 1) Detection Signals

### Signal 1: Groq LLM attribution score

**What it measures:**
This signal looks at the overall writing style, semantic coherence, phrasing patterns, and tone to estimate whether the text feels more human or more AI-generated.

**Output format:**
A score from `0.0` to `1.0`, where `0.0` means strongly human-like and `1.0` means strongly AI-like.

**Why this signal is useful:**
LLMs can catch broad stylistic patterns that are hard to reduce to simple rules, such as overly polished transitions, repetitive phrasing, or writing that feels generic and uniform.

**Blind spot:**
It can misclassify polished human writing, academic prose, or text by non-native English speakers as AI-generated. It can also be fooled by AI text that has been heavily edited by a human.

---

### Signal 2: Stylometric heuristic score

**What it measures:**
This signal checks measurable structural properties of the text, such as sentence length variance, type-token ratio, punctuation density, repetition, and average sentence complexity.

**Output format:**
A score from `0.0` to `1.0`, where `0.0` means strongly human-like and `1.0` means strongly AI-like.

**Why this signal is useful:**
AI-generated text often has more even structure, more regular sentence lengths, and less natural variation than casual human writing.

**Blind spot:**
It may misread poems, lyrics, very short passages, highly formal prose, or intentionally repetitive creative writing as AI-generated. It also struggles when the sample is too short to measure reliably.

---

### How the signals will be combined

Both signals produce a score on the same scale, so they can be combined into a single confidence score.

Planned formula:

```text
combined_score = (0.65 * llm_score) + (0.35 * stylometric_score)
confidence = combined_score - disagreement_penalty
```

Where:

```text
disagreement_penalty = 0.15 * abs(llm_score - stylometric_score)
```

The penalty lowers confidence when the two signals disagree, which helps keep the system cautious on borderline cases.

---

## 2) Uncertainty Representation

### What confidence means

A confidence score of `0.6` means the system sees mixed evidence leaning slightly toward AI-generated, but not enough to make a strong claim.

In other words:

* `0.0` = strongly human-like
* `0.5` = unclear / mixed evidence
* `1.0` = strongly AI-like

### Thresholds

I will use three label ranges:

* `confidence >= 0.80` → high-confidence AI
* `confidence <= 0.25` → high-confidence human
* `0.25 < confidence < 0.80` → uncertain

### Why these thresholds

The thresholds are intentionally asymmetric. The system should require stronger evidence before labeling something as AI-generated, because false positives are more harmful than false negatives.

### Calibration idea

I will test the score with clearly AI-like, clearly human-like, and borderline examples. If the score is too extreme or too flat, I will adjust the weighting or disagreement penalty so the score varies meaningfully across different inputs.

---

## 3) Transparency Label Design

These are the exact label texts that will be shown to users.

| Variant               | Exact label text                                                                                                                         |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| High-confidence AI    | `"This content is likely AI-generated. We are fairly confident in this label, but creators can appeal if they believe it is incorrect."` |
| High-confidence human | `"This content is likely human-written. We are fairly confident in this label."`                                                         |
| Uncertain             | `"We could not classify this content confidently. The signals are mixed or weak, so no strong attribution claim is shown."`              |

The API will return the full label text, not just a short code.

---

## 4) Appeals Workflow

### Who can submit an appeal

The creator who submitted the content can submit an appeal.

### What they provide

The appeal request must include:

* `content_id`
* `creator_reasoning`

The reasoning should explain why the creator believes the classification is wrong. It does not need to prove authorship, but it should give context for review.

### What the system does

When an appeal is received:

1. The system validates the `content_id`.
2. The system checks that the appeal is tied to the original submission.
3. The creator’s reasoning is stored.
4. The content status is updated to `under_review`.
5. A new structured audit-log entry is written.
6. The system returns confirmation that the appeal was received.

### What a human reviewer would see

A reviewer should be able to see:

* the original text
* the original attribution result
* the confidence score
* both signal scores
* the creator’s appeal reasoning
* the current status (`under_review`)

The appeal does not automatically re-run classification. It preserves the original decision and marks it for review.

---

## 5) Anticipated Edge Cases

These are specific cases the system may handle poorly:

1. **A poem with repeated phrases and simple vocabulary**
   The stylometric signal may read this as AI-like because the structure is repetitive and uniform, even if the writing is clearly human and intentional.

2. **A formal essay written by a non-native English speaker**
   The LLM signal may overestimate AI-likeness because the writing is unusually polished, grammatically careful, or generic in tone.

3. **Very short content, like a 1–2 sentence post**
   The stylometric signal may be too weak to measure reliably because there is not enough text to analyze.

4. **Lightly edited AI output**
   If a human rewrites the AI text enough, both signals may become less reliable and the system may fall into the uncertain range.

---

## Architecture

### Submission flow

```text
POST /submit
   |
   v
Validate request + create content_id
   |
   v
Signal 1: Groq LLM attribution score
   |
   v
Signal 2: Stylometric heuristic score
   |
   v
Confidence scoring
   |
   v
Transparency label generation
   |
   v
Structured audit log write
   |
   v
JSON response to client
```

### Appeal flow

```text
POST /appeal
   |
   v
Validate content_id + creator_reasoning
   |
   v
Update status to under_review
   |
   v
Write appeal to audit log
   |
   v
JSON confirmation response
```

### Narrative

A piece of text enters the system through `POST /submit`, where the request is validated and a unique `content_id` is created. The text is then analyzed by two independent signals: a Groq-based LLM check and a stylometric heuristic check. Their outputs are combined into a confidence score, which is mapped to a transparency label. The final result and all supporting signal data are written to a structured audit log before the API response is returned.

If the creator disagrees with the result, they can submit an appeal through `POST /appeal`. The appeal stores the creator’s reasoning, changes the content status to `under_review`, and appends a new audit entry without overwriting the original classification.

---

## API Surface

### `POST /submit`

Submits a text sample for attribution analysis.

**Request body**

```json
{
  "text": "string",
  "creator_id": "string"
}
```

**Response body**

```json
{
  "content_id": "string",
  "creator_id": "string",
  "attribution": "likely_ai | likely_human | uncertain",
  "confidence": 0.0,
  "label": "string",
  "signals": {
    "llm_score": 0.0,
    "stylometric_score": 0.0
  },
  "status": "classified"
}
```

---

### `POST /appeal`

Submits a creator appeal for a previously classified item.

**Request body**

```json
{
  "content_id": "string",
  "creator_reasoning": "string"
}
```

**Response body**

```json
{
  "content_id": "string",
  "status": "under_review",
  "message": "Appeal received"
}
```

---

### `GET /log`

Returns recent structured audit-log entries.

**Response body**

```json
{
  "entries": [
    {
      "timestamp": "string",
      "event_type": "submission | appeal",
      "content_id": "string",
      "creator_id": "string",
      "attribution": "likely_ai | likely_human | uncertain",
      "confidence": 0.0,
      "llm_score": 0.0,
      "stylometric_score": 0.0,
      "status": "classified | under_review",
      "appeal_reasoning": "string or null"
    }
  ]
}
```

---

## AI Tool Plan

### Milestone 3: submission endpoint + first signal

**Spec sections to provide:**

* Detection Signals
* Architecture
* API Surface

**What I will ask the AI tool to generate:**

* Flask app skeleton
* `POST /submit` route stub
* first detection signal function

**How I will verify it:**

* test the first signal directly with a few sample inputs
* confirm the function returns the expected score format
* wire it into `/submit` only after the standalone test works

---

### Milestone 4: second signal + confidence scoring

**Spec sections to provide:**

* Detection Signals
* Uncertainty Representation
* Architecture

**What I will ask the AI tool to generate:**

* second signal function
* confidence scoring logic
* label-threshold helper logic if needed

**How I will verify it:**

* test clearly AI-like and clearly human-like samples
* check that the two signals vary independently
* confirm the combined score maps to the intended label ranges

---

### Milestone 5: production layer

**Spec sections to provide:**

* Transparency Label Design
* Appeals Workflow
* Architecture
* API Surface

**What I will ask the AI tool to generate:**

* label generation function
* `POST /appeal` route
* audit-log updates
* rate-limiting wiring if needed

**How I will verify it:**

* confirm all three label variants are reachable
* test that an appeal updates status to `under_review`
* inspect the audit log to make sure the appeal is recorded

---

