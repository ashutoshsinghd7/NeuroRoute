"""CP2: estimate tokens, needed context, and cost.

Plain math only. No AI is used here.
We always round UP, because guessing too small is the unsafe mistake.
"""
import math
from dataclasses import dataclass
from typing import Optional

CHARS_PER_TOKEN = 4            # simple rule: 1 token is about 4 characters
EXPECTED_ANSWER_TOKENS = 500   # room we keep for the model's answer


@dataclass
class TokenInfo:
    prompt_tokens: int      # tokens in the prompt
    total_tokens: int       # prompt + expected answer
    needed_context: int     # what a model's context_limit must cover


def estimate_tokens(prompt: str) -> int:
    """Rough token count of the prompt (length / 4, rounded up)."""
    if not prompt:
        return 0
    return max(1, math.ceil(len(prompt) / CHARS_PER_TOKEN))


def compute_needed_context(
    prompt_tokens: int,
    required_context: Optional[int] = None,
    answer_tokens: int = EXPECTED_ANSWER_TOKENS,
) -> int:
    """Context a model must hold: the bigger of the user's number
    and (prompt + answer)."""
    return max(required_context or 0, prompt_tokens + answer_tokens)


def estimate_cost(total_tokens: int, cost_per_1k: float) -> float:
    """Cost of one call on one model."""
    return round(total_tokens / 1000 * cost_per_1k, 6)


def analyse_request(prompt: str, required_context: Optional[int] = None) -> TokenInfo:
    """One call that gives CP4 everything it needs from CP2."""
    prompt_tokens = estimate_tokens(prompt)
    total = prompt_tokens + EXPECTED_ANSWER_TOKENS
    needed = compute_needed_context(prompt_tokens, required_context)
    return TokenInfo(prompt_tokens, total, needed)