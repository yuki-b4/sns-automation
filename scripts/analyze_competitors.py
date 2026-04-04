"""
競合分析スクリプト
Threads APIで競合アカウントの直近投稿・メトリクスを取得し、
Claude APIで分析して競合分析DBに記録する
火・金 8:00 JSTに実行
"""

import os
import json
import datetime
import requests
import anthropic
from sheets import get_competitor_accounts, append_competitor_record

THREADS_TOKEN = os.environ.get("THREADS_TOKEN", "")
BASE_THREADS_URL = "https://graph.threads.net/v1.0"
STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_competitor_posts(account_id: str) -> list[dict]:
    """競合アカウントの直近投稿を取得"""
    if not THREADS_TOKEN:
        return []

    url = f"{BASE_THREADS_URL}/{account_id}/threads"
    resp = requests.get(url, params={
        "fields": "id,text,timestamp,like_count,repost_count,reply_count,views",
        "limit": 10,
        "access_token": THREADS_TOKEN,
    })
    data = resp.json()

    if "data" not in data:
        print(f"[競合] 投稿取得失敗 {account_id}: {data}")
        return []

    return data["data"]


def analyze_competitor_with_claude(account_id: str, posts: list[dict], strategy: dict) -> dict:
    """Claude APIで競合投稿を分析"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    posts_text = "\n\n".join([
        f"投稿{i+1}: {p.get('text', '')}\n"
        f"（いいね:{p.get('like_count', 0)} リポスト:{p.get('repost_count', 0)} "
        f"返信:{p.get('reply_count', 0)} 表示:{p.get('views', 0)}）"
        for i, p in enumerate(posts[:10])
    ])

    positioning = strategy["positioning"]
    own_position = f"{positioning['position']} / {positioning['differentiation']}"
    own_tagline = positioning["tagline"]

    prompt = f"""以下は競合SNSアカウント（{account_id}）の直近投稿です。

{posts_text}

以下の観点で分析し、JSON形式で出力してください：

{{
  "top_posts": "最も高エンゲージメントの投稿3件の要約",
  "avg_engagement_rate": 平均エンゲージメント率（数値）,
  "dominant_themes": "頻出テーマ・キーワード（カンマ区切り）",
  "positioning_gap": "自社ポジション「{own_position}」（想起ワード：{own_tagline}）との差分・空白地帯"
}}

JSONのみ出力してください。"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    import json
    try:
        result = json.loads(message.content[0].text.strip())
    except Exception:
        result = {
            "top_posts": "解析失敗",
            "avg_engagement_rate": 0.0,
            "dominant_themes": "",
            "positioning_gap": "",
        }

    return result


def main():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat()
    strategy = load_strategy()
    competitor_accounts = get_competitor_accounts()

    if not competitor_accounts:
        print("[競合分析] 競合アカウントリストが未登録。Google SheetsのSheet「競合アカウント」にaccount_idを追加してください。")
        return

    for account_id in competitor_accounts:
        print(f"[競合分析] 取得中: {account_id}")
        posts = fetch_competitor_posts(account_id)
        if not posts:
            continue

        analysis = analyze_competitor_with_claude(account_id, posts, strategy)
        record = {
            "competitor_id": account_id,
            "platform": "threads",
            "top_posts": analysis.get("top_posts", ""),
            "avg_engagement_rate": analysis.get("avg_engagement_rate", 0.0),
            "dominant_themes": analysis.get("dominant_themes", ""),
            "positioning_gap": analysis.get("positioning_gap", ""),
            "collected_at": now,
        }
        append_competitor_record(record)
        print(f"[競合分析] 記録完了: {account_id}")

    print("[競合分析] 完了")


if __name__ == "__main__":
    main()
