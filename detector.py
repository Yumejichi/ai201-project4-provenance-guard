"""Detection signals for Provenance Guard.

Signal 1: Groq LLM attribution score (see planning.md, Detection Signals #1).
"""

import json
import os
import re
import string

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_MODEL = "llama-3.3-70b-versatile"

# Thresholds from planning.md, Uncertainty Representation.
HIGH_AI_THRESHOLD = 0.80
HIGH_HUMAN_THRESHOLD = 0.25

# Weights and penalty from planning.md, "How the signals will be combined".
LLM_WEIGHT = 0.65
STYLOMETRIC_WEIGHT = 0.35
DISAGREEMENT_PENALTY_WEIGHT = 0.15

# Normalization constants for the stylometric sub-scores. These aren't in
# planning.md — they're implementation-level judgment calls, see the
# explanation below the function.
SENTENCE_VARIANCE_CAP = 40.0
PUNCTUATION_DENSITY_CAP = 0.3

# Sentence-length variance computed from very few sentences is statistically
# noisy (a single outlier sentence swings it wildly) regardless of how the
# cap is tuned. Below this count, skip variance rather than trust a noisy
# estimate.
MIN_SENTENCES_FOR_VARIANCE = 4

_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

_SYSTEM_PROMPT = (
    "You are a writing-attribution assistant. Given a piece of text, judge how "
    "likely it is to be AI-generated based on its style, phrasing, tone, and "
    "semantic coherence. Respond with ONLY a JSON object, no other text, in "
    "this exact form: {\"score\": <float between 0.0 and 1.0>}. "
    "A score of 0.0 means the text feels strongly human-written. A score of "
    "1.0 means the text feels strongly AI-generated. Use the full range of "
    "the scale based on your genuine assessment."
)


def score_to_label(score):
    """Map a 0.0-1.0 score to likely_ai / likely_human / uncertain."""
    if score >= HIGH_AI_THRESHOLD:
        return "likely_ai"
    if score <= HIGH_HUMAN_THRESHOLD:
        return "likely_human"
    return "uncertain"


# Exact label text from planning.md, Transparency Label Design.
TRANSPARENCY_LABELS = {
    "likely_ai": (
        "This content is likely AI-generated. We are fairly confident in "
        "this label, but creators can appeal if they believe it is incorrect."
    ),
    "likely_human": (
        "This content is likely human-written. We are fairly confident in "
        "this label."
    ),
    "uncertain": (
        "We could not classify this content confidently. The signals are "
        "mixed or weak, so no strong attribution claim is shown."
    ),
}


def get_transparency_label(confidence):
    """Map a confidence score to the exact label text shown to readers."""
    return TRANSPARENCY_LABELS[score_to_label(confidence)]


def get_llm_attribution_score(text):
    """Ask Groq to assess how AI-like the text reads.

    Returns a dict: {"llm_score": float, "llm_label": str}
    """
    response = _client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    raw = response.choices[0].message.content.strip()

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Groq response did not contain JSON: {raw!r}")

    parsed = json.loads(match.group(0))
    score = float(parsed["score"])
    score = max(0.0, min(1.0, score))

    return {"llm_score": score, "llm_label": score_to_label(score)}


def _split_sentences(text):
    sentences = re.split(r"[.!?]+", text)
    return [s.strip() for s in sentences if s.strip()]


def _split_words(text):
    return re.findall(r"[A-Za-z']+", text)


def get_stylometric_score(text):
    """Compute a stylometric AI-likeness score from structural features.

    Returns a dict: {"stylometric_score": float}

    Uses two metrics from planning.md, Detection Signals #2:
    - sentence length variance (low variance -> more AI-like/uniform)
    - punctuation density (low density -> more AI-like/sanitized)

    A third metric, vocabulary diversity (type-token ratio / Root TTR), was
    tried and dropped: even after correcting for its known length confound,
    testing on both a short and a deliberately long/repetitive AI-style
    sample showed it consistently failed to register genuinely AI-generated
    text as AI-like, diluting the two metrics that did work. See README's
    spec reflection section for the full account.

    Each sub-score is normalized to 0.0-1.0 and averaged with equal weight.
    Equal weighting is a design choice, not something planning.md specifies —
    revisit if testing shows one metric should dominate.
    """
    sentences = _split_sentences(text)
    words = _split_words(text)

    if len(sentences) < 2 or len(words) < 5:
        # Not enough structure to measure reliably — planning.md's edge case
        # for very short content. Return a neutral score rather than a
        # misleadingly confident one.
        return {"stylometric_score": 0.5}

    # Sentence length variance — unreliable from very few sentences (a
    # single outlier sentence swings the number wildly), so fall back to a
    # neutral sub-score rather than trust noise.
    if len(sentences) < MIN_SENTENCES_FOR_VARIANCE:
        variance_score = 0.5
    else:
        word_counts = [len(_split_words(s)) for s in sentences]
        mean_len = sum(word_counts) / len(word_counts)
        variance = sum((c - mean_len) ** 2 for c in word_counts) / len(word_counts)
        variance_score = 1 - min(variance / SENTENCE_VARIANCE_CAP, 1.0)

    # Punctuation density (punctuation characters per word)
    punctuation_count = sum(1 for ch in text if ch in string.punctuation)
    density = punctuation_count / len(words)
    punctuation_score = 1 - min(density / PUNCTUATION_DENSITY_CAP, 1.0)

    stylometric_score = (variance_score + punctuation_score) / 2
    stylometric_score = max(0.0, min(1.0, stylometric_score))

    return {"stylometric_score": stylometric_score}


def combine_scores(llm_score, stylometric_score):
    """Combine signal 1 and signal 2 into a single confidence score.

    Formula from planning.md, "How the signals will be combined":
        combined_score = (0.65 * llm_score) + (0.35 * stylometric_score)
        disagreement_penalty = 0.15 * abs(llm_score - stylometric_score)
        confidence = combined_score - disagreement_penalty

    Returns a dict: {"confidence": float, "attribution": str}
    """
    combined_score = (LLM_WEIGHT * llm_score) + (STYLOMETRIC_WEIGHT * stylometric_score)
    disagreement_penalty = DISAGREEMENT_PENALTY_WEIGHT * abs(llm_score - stylometric_score)
    confidence = combined_score - disagreement_penalty
    confidence = max(0.0, min(1.0, confidence))

    return {"confidence": confidence, "attribution": score_to_label(confidence)}


if __name__ == "__main__":
    # Quick manual test — run `python detector.py` to sanity-check the signal
    # before wiring it into the Flask endpoint.
    samples = [
        "Artificial intelligence represents a transformative paradigm shift in "
        "modern society. It is important to note that while the benefits of AI "
        "are numerous, it is equally essential to consider the ethical "
        "implications.",
        "ok so i finally tried that new ramen place downtown and honestly? "
        "underwhelming. the broth was fine but they put WAY too much sodium "
        "in it and i was thirsty for like three hours after.",
    ]
    for sample in samples:
        llm_result = get_llm_attribution_score(sample)
        style_result = get_stylometric_score(sample)
        print(f"llm={llm_result['llm_score']:.2f} ({llm_result['llm_label']})  "
              f"stylometric={style_result['stylometric_score']:.2f}  -  {sample[:50]}")
