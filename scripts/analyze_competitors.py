"""
競合分析スクリプト
Google Sheets「競合投稿DB」に手動入力された投稿データを読み込み、
Claude API でプロンプト用の分析テキストを生成して Slack に通知する。
火・金 08:00 JST に実行。
"""

import os
import json
import datetime
import anthropic
from sheets import get_recent_competitor_posts, mark_competitor_posts_analyzed
from notify_slack import notify_slack_report
from token_cost import log_token_cost

STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_posts_text(posts: list[dict]) -> str:
    """投稿リストをスレッド構造を保ったプロンプト用テキストに変換する"""
    standalone = [p for p in posts if not str(p.get("thread_id", "")).strip()]
    threaded = [p for p in posts if str(p.get("thread_id", "")).strip()]

    blocks: list[str] = []

    # スタンドアロン投稿（エンゲージメント降順）
    if standalone:
        sorted_standalone = sorted(
            standalone,
            key=lambda p: int(p.get("likes", 0)) + int(p.get("replies", 0)),
            reverse=True,
        )
        blocks.append("＜スタンドアロン投稿＞")
        for i, p in enumerate(sorted_standalone[:10], 1):
            content = str(p.get("content", "")).strip()
            if not content:
                continue
            blocks.append(
                f"投稿{i}（いいね:{p.get('likes', 0)} リプライ:{p.get('replies', 0)}）:\n{content}"
            )

    # スレッド投稿（thread_idごとにグループ化・ルートのエンゲージメント降順）
    if threaded:
        groups: dict[str, list[dict]] = {}
        for p in threaded:
            tid = str(p.get("thread_id", ""))
            groups.setdefault(tid, []).append(p)

        def root_engagement(group: list[dict]) -> int:
            root = next((p for p in group if str(p.get("reply_order", "")) == "0"), group[0])
            return int(root.get("likes", 0)) + int(root.get("replies", 0))

        sorted_groups = sorted(groups.values(), key=root_engagement, reverse=True)

        blocks.append("\n＜スレッド投稿＞")
        for t_i, group in enumerate(sorted_groups[:5], 1):
            sorted_group = sorted(group, key=lambda p: int(p.get("reply_order") or 0))
            thread_lines = [f"スレッド{t_i}:"]
            for p in sorted_group:
                content = str(p.get("content", "")).strip()
                if not content:
                    continue
                order = p.get("reply_order", "")
                label = "ルート" if str(order) == "0" else f"リプライ{order}"
                thread_lines.append(
                    f"  {label}（いいね:{p.get('likes', 0)} リプライ:{p.get('replies', 0)}）:\n  {content}"
                )
            blocks.append("\n".join(thread_lines))

    return "\n\n".join(blocks)


def analyze_with_claude(posts: list[dict], strategy: dict) -> str:
    """Claude API で競合投稿を分析し、AI に渡すプロンプト用テキストを返す"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    posts_text = _build_posts_text(posts)

    if not posts_text.strip():
        print("[競合分析] 本文のある投稿がないためスキップ")
        return ""

    positioning = strategy.get("positioning", {})

    prompt = f"""あなたはSNS戦略アナリストです。以下の競合投稿を分析し、日本語で回答してください。
投稿は「スタンドアロン投稿」と「スレッド投稿（ルート＋リプライ1/2/3…）」に分かれています。
スレッド投稿は一連の文章として読み、構成パターンも分析対象に含めてください。

【自社ポジション】
- ポジション：{positioning.get("position", "")}
- コンセプト：{positioning.get("concept", "")}
- 差別化軸：{positioning.get("differentiation", "")}

【競合の投稿】
{posts_text}

【出力形式】
以下の構造でプレーンテキストのみ出力してください（コードブロック・装飾記号不要）：

■ 高エンゲージメント投稿の傾向
（エンゲージメント上位3件の共通点・要約を300字以内で）

■ 頻出テーマ・キーワード
（カンマ区切り、5件程度）

■ 自社との差分・空白地帯
（自社ポジションとの差分・競合が取れていない空白地帯を300字以内で）

■ スレッド構成パターン
（スレッド投稿がある場合のみ。何投稿構成か・各リプライの役割・どの構成が高エンゲージメントかを300字以内で。なければこの項目ごと省略）

【文字数】全体で約1200文字を目安にすること"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    log_token_cost("claude-opus-4-6", message.usage, "analyze_competitors")
    return message.content[0].text.strip()


def main():
    # 競合投稿DBから未分析の投稿のみ取得
    posts = get_recent_competitor_posts(unanalyzed_only=True)
    if not posts:
        print("[競合分析] 未分析の投稿がありません。終了します。")
        return

    print(f"[競合分析] 未分析投稿数: {len(posts)}件")
    strategy = load_strategy()

    result = analyze_with_claude(posts, strategy)
    if not result:
        return

    print(f"[競合分析] 分析完了:\n{result}")
    notify_slack_report(result, title="競合分析レポート", body=result)

    # 分析済みフラグを立てる
    row_numbers = [p["_row"] for p in posts]
    mark_competitor_posts_analyzed(row_numbers)


if __name__ == "__main__":
    main()
