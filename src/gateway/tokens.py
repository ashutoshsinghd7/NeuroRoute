"""Token and cost estimate (CP2). Simple on purpose: 1 token is about 4 characters."""
EXPECTED_ANSWER_TOKENS = 200  # we guess the answer will be about this long


def estimate_tokens(prompt: str) -> int:
    return max(1, len(prompt) // 4)


def needed_context(required_context: int, tokens: int) -> int:
    """Model must read the prompt AND write the answer, or the user's own number if bigger."""
    return max(required_context, tokens + EXPECTED_ANSWER_TOKENS)


def estimate_cost(tokens: int, cost_per_1k: float) -> float:
    return round((tokens + EXPECTED_ANSWER_TOKENS) / 1000 * cost_per_1k, 6)
