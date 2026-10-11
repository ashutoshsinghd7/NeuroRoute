"""Train the OPTIONAL AI classifier (TF-IDF + Logistic Regression). Needs: scikit-learn, joblib.

Run from the repo root:  python src/classifier/train.py
Add more rows to prompts.csv (aim for 60+, about 12 per level) for better accuracy.
"""
import csv
import os

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.pipeline import make_pipeline

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, "prompts.csv"), encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
texts = [r["prompt"] for r in rows]
levels = [int(r["level"]) for r in rows]

pipe = make_pipeline(TfidfVectorizer(ngram_range=(1, 2), lowercase=True),
                     LogisticRegression(max_iter=1000))
print("cross-val accuracy:", cross_val_score(pipe, texts, levels, cv=3).mean().round(2))
pipe.fit(texts, levels)
joblib.dump(pipe, os.path.join(HERE, "model.joblib"))
print("saved model.joblib")
