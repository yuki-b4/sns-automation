"""
競合分析スクリプト
Threads API で競合アカウントの投稿（内容・いいね・リプライ）を取得し、
競合投稿DB に記録する。また Claude API で集計分析を行い競合分析DB にも記録する。
火・金 08:00 JST に実行。
"""

import os
import json
import datetime
import requests
import anthropic
from sheets import (
    get_competitor_accounts,
    append_competitor_posts,
    append_competitor_record,
)

THREADS_TOKEN = os.environ.get("THREADS_TOKEN", "")
BASE_THREADS_URL = "https://graph.threads.net/v1.0"
STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_competitor_posts(account_id: str, limit: int = 20) -> list[dict]:
    """競合アカウントの投稿一覧を Threads API で取得する"""
    if not THREADS_TOKEN:
        print("[競合分析] Threadsトークンが未設定のためスキップ")
        return []

    url = f"{BASE_THREADS_URL}/{account_id}/threads"
    resp = requests.get(url, params={
        "fields": "id,text,timestamp,like_count,replies_count",
        "limit": limit,
        "access_token": THREADS_TOKEN,
    })
    data = resp.json()

    if "data" not in data:
        print(f"[競合分析] 投稿取得失敗 {account_id}: {data}")
        return []

    posts = []
    for item in data["data"]:
        posts.append({
            "post_id": str(item.get("id", "")),
            "content": item.get("text", ""),
            "likes": int(item.get("like_count", 0)),
            "replies": int(item.get("replies_count", 0)),
            "posted_at": item.get("timestamp", ""),
        })

    print(f"[競合分析] {account_id}: {len(posts)}件取得")
    return posts


def analyze_with_claude(account_id: str, posts: list[dict], strategy: dict) -> dict:
    """Claude API で競合投稿を分析し、集計結果を返す"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # いいね＋リプライ順にソートして上位を分析対象に
    sorted_posts = sorted(posts, key=lambda p: p["likes"] + p["replies"], reverse=True)

    posts_text = "\n\n".join([
        f"投稿{i + 1}（いいね:{p['likes']} リプライ:{p['replies']}）:\n{p['content']}"
        for i, p in enumerate(sorted_posts[:15])
        if p.get("content")
    ])

    positioning = strategy.get("positioning", {})

    prompt = f"""あなたはSNS戦略アナリストです。以下の競合アカウントの投稿を分析し、日本語で回答してください。

【自社ポジション】
- ポジション：{positioning.get("position", "")}
- コンセプト：{positioning.get("concept", "")}
- 差別化軸：{positioning.get("differentiation", "")}

【競合の投稿（直近・エンゲージメント高い順）】
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
    # コードブロックがある場合は除去
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        result = json.loads(raw)
    except Exception as e:
        print(f"[競合分析] Claude分析のJSON解析失敗 ({account_id}): {e}")
        result = {
            "top_posts": raw[:200],
            "avg_engagement_rate": 0.0,
            "dominant_themes": "",
            "positioning_gap": "",
        }

    return result


def main():
    now = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ).isoformat()

    accounts = get_competitor_accounts()
    if not accounts:
        print("[競合分析] 競合アカウントが未登録。「競合アカウント」シートに account_id を追加してください。")
        return

    strategy = load_strategy()
    print(f"[競合分析] 対象アカウント: {len(accounts)}件")

    all_posts: list[dict] = []
    for account_id in accounts:
        posts = fetch_competitor_posts(account_id)
        if not posts:
            continue

        # 投稿単位レコードを作成
        for post in posts:
            all_posts.append({
                "competitor_id": account_id,
                "post_id": post["post_id"],
                "content": post["content"],
                "likes": post["likes"],
                "replies": post["replies"],
                "posted_at": post["posted_at"],
                "collected_at": now,
            })

        # Claude で集計分析
        analysis = analyze_with_claude(account_id, posts, strategy)
        append_competitor_record({
            "competitor_id": account_id,
            "platform": "threads",
            "top_posts": analysis.get("top_posts", ""),
            "avg_engagement_rate": analysis.get("avg_engagement_rate", 0.0),
            "dominant_themes": analysis.get("dominant_themes", ""),
            "positioning_gap": analysis.get("positioning_gap", ""),
            "collected_at": now,
        })
        print(f"[競合分析] {account_id}: 集計分析を記録しました")

    # 投稿内容・メトリクスを一括記録
    if all_posts:
        append_competitor_posts(all_posts)
        print(f"[競合分析] 競合投稿DB記録完了（{len(all_posts)}件）")
    else:
        print("[競合分析] 記録対象の投稿がありませんでした")


if __name__ == "__main__":
    main()
