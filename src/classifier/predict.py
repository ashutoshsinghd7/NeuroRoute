"""COMPLEXITY CLASSIFIER (CP3). Guesses how hard a prompt is (level 1-5) and how sure we are.

TWO versions live in this file:
  A) HARDCODED rules  -> ACTIVE by default. No install needed. Easy to read.
  B) ML model (TF-IDF + Logistic Regression) -> COMMENTED OUT. Turn it on in 3 steps below.

SAFETY RULE (both versions): if confidence < 0.6, assume the prompt is HARDER (level + 1).
The result is only a guess. The hard filter still decides who is allowed.
"""
CONFIDENCE_THRESHOLD = 0.6

# ---------- A) HARDCODED version (active) ----------
HARD_WORDS = ["analyse", "analyze", "contract", "clause", "legal", "liability", "prove", "proof",
              "compare", "risk", "architecture", "derive", "optimi", "algorithm", "step by step"]
EASY_WORDS = ["what is", "capital of", "who is", "define", "hello", "translate", "how many", "spell"]


def hardcoded_classify(prompt: str) -> dict:
    text = prompt.lower()
    words = len(text.split())
    hard = sum(1 for w in HARD_WORDS if w in text)   # clues that the task is hard
    easy = sum(1 for w in EASY_WORDS if w in text)   # clues that the task is easy
    has_code = "```" in prompt
    score = 1 + 0.8 * hard - 0.5 * easy
    score += 1 if words > 150 else 0
    score += 1 if words > 600 else 0
    score += 1 if has_code else 0
    level = min(5, max(1, round(score)))
    clues = hard + easy + (words > 150) + has_code
    confidence = 0.4 if clues == 0 else min(0.9, 0.55 + 0.1 * clues)  # no clues = unsure
    if hard and easy:
        confidence -= 0.2                                            # clues disagree = unsure
    return {"level": level, "confidence": round(confidence, 2), "source": "hardcoded-rules"}


# ---------- B) AI version (commented out) ----------
# STEP 1: pip install scikit-learn joblib      (also uncomment them in requirements.txt)
# STEP 2: python src/classifier/train.py       (creates src/classifier/model.joblib)
# STEP 3: uncomment this block AND the "STEP 3" lines inside classify() below.
#
# import os
# import joblib
# MODEL_PATH = os.path.join(os.path.dirname(__file__), "model.joblib")
# _pipe = None
#
# def ml_classify(prompt: str) -> dict:
#     global _pipe
#     if _pipe is None:
#         _pipe = joblib.load(MODEL_PATH)
#     probs = _pipe.predict_proba([prompt])[0]
#     best = int(probs.argmax())
#     return {"level": int(_pipe.classes_[best]), "confidence": round(float(probs[best]), 2),
#             "source": "tfidf-logreg"}


def classify(prompt: str) -> dict:
    raw = hardcoded_classify(prompt)
    # STEP 3 (uncomment these 4 lines to use the AI version, falls back to rules if it fails):
    # try:
    #     raw = ml_classify(prompt)
    # except Exception:
    #     pass
    low = raw["confidence"] < CONFIDENCE_THRESHOLD
    level = min(5, raw["level"] + 1) if low else raw["level"]  # unsure -> assume harder
    return {"level": level, "raw_level": raw["level"], "confidence": raw["confidence"],
            "low_confidence_routing": low, "source": raw["source"]}
