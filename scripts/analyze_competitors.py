"""
競合分析スクリプト
Google Sheets「競合投稿DB」に手動入力された投稿データを読み込み、
Claude API で集計分析を行い「競合分析DB」にサマリーを記録する。
火・金 08:00 JST に実行。
"""

import os
import json
import datetime
import anthropic
from sheets import get_recent_competitor_posts, append_competitor_record


STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def analyze_with_claude(posts: list[dict], strategy: dict) -> dict:
    """Claude API で競合投稿を集計分析し、サマリーを返す"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    sorted_posts = sorted(
        posts,
        key=lambda p: int(p.get("likes", 0)) + int(p.get("replies", 0)),
        reverse=True,
    )

    posts_text = "\n\n".join([
        f"投稿{i + 1}（いいね:{p['likes']} リプライ:{p['replies']}）:\n{p['content']}"
        for i, p in enumerate(sorted_posts[:15])
        if str(p.get("content", "")).strip()
    ])

    if not posts_text:
        print("[競合分析] 本文のある投稿がないためスキップ")
        return {}

    positioning = strategy.get("positioning", {})

    prompt = f"""あなたはSNS戦略アナリストです。以下の競合投稿を分析し、日本語で回答してください。

【自社ポジション】
- ポジション：{positioning.get("position", "")}
- コンセプト：{positioning.get("concept", "")}
- 差別化軸：{positioning.get("differentiation", "")}

【競合の投稿（エンゲージメント高い順）】
{posts_text}

【出力形式】
JSON形式のみで出力してください（前後の説明文は不要）：
{{
  "top_posts": "エンゲージメント上位3件の共通点・要約（200字以内）",
  "avg_engagement_rate": いいね+リプライの合計を投稿数で割った数値（小数点2桁）,
  "dominant_themes": "頻出テーマ・キーワード（カンマ区切り、5件程度）",
  "positioning_gap": "自社ポジションとの差分・競合が取れていない空白地帯（200字以内）"
}}"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[競合分析] Claude分析のJSON解析失敗: {e}")
        return {
            "top_posts": raw[:200],
            "avg_engagement_rate": 0.0,
            "dominant_themes": "",
            "positioning_gap": "",
        }


def main():
    now = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ).isoformat()

    # 競合投稿DBから手動入力データを取得（直近14日分）
    posts = get_recent_competitor_posts(days=14)
    if not posts:
        print("[競合分析] 競合投稿DBにデータがありません。「競合投稿DB」シートに手動入力してください。")
        return

    print(f"[競合分析] 投稿数: {len(posts)}件")
    strategy = load_strategy()

    analysis = analyze_with_claude(posts, strategy)
    if not analysis:
        return

    append_competitor_record({
        "competitor_id": "",
        "platform": "threads",
        "top_posts": analysis.get("top_posts", ""),
        "avg_engagement_rate": analysis.get("avg_engagement_rate", 0.0),
        "dominant_themes": analysis.get("dominant_themes", ""),
        "positioning_gap": analysis.get("positioning_gap", ""),
        "collected_at": now,
    })
    print("[競合分析] 集計分析を競合分析DBに記録しました")


if __name__ == "__main__":
    main()
