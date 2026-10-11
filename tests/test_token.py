from src.gateway.tokens import (
    estimate_tokens, compute_needed_context, estimate_cost, analyse_request,
)

def test_short_prompt_is_small():
    assert estimate_tokens("What is 2+2?") <= 5

def test_long_prompt_is_big():
    assert estimate_tokens("a" * 40000) == 10000

def test_empty_prompt_is_zero():
    assert estimate_tokens("") == 0

def test_needed_context_uses_user_number_when_bigger():
    assert compute_needed_context(100, required_context=20000) == 20000

def test_needed_context_uses_prompt_plus_answer_when_bigger():
    assert compute_needed_context(10000, required_context=500) == 10500

def test_cost_math():
    assert estimate_cost(1000, 0.30) == 0.30
    assert estimate_cost(2000, 0.05) == 0.10

def test_analyse_without_required_context():
    info = analyse_request("hello world")
    assert info.needed_context == info.prompt_tokens + 500  