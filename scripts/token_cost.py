# Claude API トークンコスト計算ユーティリティ
# 料金出典: https://www.anthropic.com/pricing
# 単位: USD / 100万トークン

_MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
}

_DEFAULT_MODEL = "claude-opus-4-6"


def calc_token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """トークン使用量からコスト（USD）を計算する。未知モデルはデフォルト料金で近似。"""
    pricing = _MODEL_PRICING.get(model, _MODEL_PRICING[_DEFAULT_MODEL])
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]


def log_token_cost(model: str, usage, label: str = "") -> None:
    """API呼び出し後のトークン使用量とコストを標準出力に記録する。"""
    cost = calc_token_cost(model, usage.input_tokens, usage.output_tokens)
    prefix = f"[{label}] " if label else ""
    print(
        f"{prefix}トークン使用量: 入力={usage.input_tokens:,}, 出力={usage.output_tokens:,} "
        f"/ 推定コスト: ${cost:.4f}"
    )
