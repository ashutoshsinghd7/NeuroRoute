import io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')
from src.classifier.predict import classify
import joblib

tests = [
    ('What is the capital of France?', 1),
    ('How many days in a leap year?', 1),
    ('Translate hello into Spanish.', 1),
    ('Write a polite email asking for leave.', 2),
    ('List five tips to study better.', 2),
    ('Summarise this paragraph in three lines.', 2),
    ('Write a function to remove duplicates and explain it.', 3),
    ('Explain how a hash map handles collisions with an example.', 3),
    ('Plan a four-week study schedule with weekly goals.', 3),
    ('Analyse the trade-offs between SQL and NoSQL for a reporting system.', 4),
    ('Compare three caching strategies and justify one for read-heavy systems.', 4),
    ('Debug this code and explain the root cause step by step.', 4),
    ('Analyse risky clauses in this 30-page contract and compare liability terms.', 5),
    ('Prove the algorithm is correct and derive its worst-case complexity.', 5),
    ('Compare liability clauses across five agreements and rank the risks.', 5),
]

SEP = '=' * 72
print(SEP)
print('HARDCODED RULES CLASSIFIER')
print(SEP)
print('%-45s %4s %4s %6s %5s' % ('PROMPT', 'EXP', 'GOT', 'CONF', 'OK?'))
print('-' * 72)
correct = 0
for prompt, expected in tests:
    result = classify(prompt)
    got = result['level']
    conf = result['confidence']
    low = result['low_confidence_routing']
    ok = 'PASS' if got == expected else 'FAIL'
    if got == expected:
        correct += 1
    short = (prompt[:42] + '..') if len(prompt) > 44 else prompt
    flag = ' <low-conf bumped>' if low else ''
    print('%-45s %4d %4d %6.2f %5s%s' % (short, expected, got, conf, ok, flag))
print(SEP)
print('Rules Accuracy: %d/%d = %.0f%%' % (correct, len(tests), correct/len(tests)*100))
print('Source:', classify('hello')['source'])

model_path = os.path.join('src', 'classifier', 'model.joblib')
if os.path.exists(model_path):
    size_kb = os.path.getsize(model_path) / 1024
    print('\nML model file found: %.1f KB' % size_kb)
    pipe = joblib.load(model_path)
    print()
    print(SEP)
    print('ML MODEL (TF-IDF + Logistic Regression)')
    print(SEP)
    print('%-45s %4s %4s %6s %5s' % ('PROMPT', 'EXP', 'GOT', 'CONF', 'OK?'))
    print('-' * 72)
    ml_correct = 0
    for prompt, expected in tests:
        probs = pipe.predict_proba([prompt])[0]
        bi = probs.argmax()
        got = int(pipe.classes_[bi])
        conf = round(float(probs[bi]), 2)
        ok = 'PASS' if got == expected else 'FAIL'
        if got == expected:
            ml_correct += 1
        short = (prompt[:42] + '..') if len(prompt) > 44 else prompt
        print('%-45s %4d %4d %6.2f %5s' % (short, expected, got, conf, ok))
    print(SEP)
    print('ML Accuracy: %d/%d = %.0f%%' % (ml_correct, len(tests), ml_correct/len(tests)*100))
    print('NOTE: Only 31 training rows (~6 per level) - very small dataset')
    print()
    print('VERDICT:')
    if ml_correct > correct:
        print('  ML model is BETTER than hardcoded rules on this test set.')
    elif ml_correct == correct:
        print('  ML model is the SAME as hardcoded rules.')
    else:
        print('  Hardcoded rules are BETTER than the ML model (too little training data).')
else:
    print('\nML model: model.joblib NOT found - run: python src/classifier/train.py')
