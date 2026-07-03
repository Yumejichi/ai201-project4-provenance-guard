import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from detector import (
    get_llm_attribution_score,
    get_stylometric_score,
    combine_scores,
    get_transparency_label,
)

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

AUDIT_LOG = []

@app.route("/submit", methods=["POST"])
@limiter.limit("5 per minute;50 per day")
def submit():
    data = request.get_json()
    text = data.get("text")
    creator_id = data.get("creator_id")

    content_id = str(uuid.uuid4())
    signal_1 = get_llm_attribution_score(text)
    signal_2 = get_stylometric_score(text)
    combined = combine_scores(signal_1["llm_score"], signal_2["stylometric_score"])

    attribution = combined["attribution"]
    confidence = combined["confidence"]
    label = get_transparency_label(confidence)

    AUDIT_LOG.append({
        "event_type": "submission",
        "content_id": content_id,
        "creator_id": creator_id,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attribution": attribution,
        "confidence": confidence,
        "llm_score": signal_1["llm_score"],
        "stylometric_score": signal_2["stylometric_score"],
        "status": "classified",
        "appeal_reasoning": None
    })

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": confidence,
        "label": label,
        "signals": {
            "llm_score": signal_1["llm_score"],
            "stylometric_score": signal_2["stylometric_score"]
        }
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json()
    content_id = data.get("content_id")
    creator_reasoning = data.get("creator_reasoning")

    if not content_id or not creator_reasoning:
        return jsonify({"error": "content_id and creator_reasoning are required."}), 400

    original = next(
        (e for e in AUDIT_LOG if e["content_id"] == content_id and e["event_type"] == "submission"),
        None
    )
    if original is None:
        return jsonify({"error": f"No submission found for content_id {content_id}"}), 404

    original["status"] = "under_review"

    AUDIT_LOG.append({
        "event_type": "appeal",
        "content_id": content_id,
        "creator_id": original["creator_id"],
        "text": original["text"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attribution": original["attribution"],
        "confidence": original["confidence"],
        "llm_score": original["llm_score"],
        "stylometric_score": original["stylometric_score"],
        "status": "under_review",
        "appeal_reasoning": creator_reasoning
    })

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Appeal received"
    })


def get_log():
    return AUDIT_LOG


@app.route("/log", methods=["GET"])
def log():
    return jsonify({"entries": get_log()})


if __name__ == "__main__":
    app.run(port=5050)
