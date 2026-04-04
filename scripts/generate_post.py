"""
投稿生成スクリプト
Claude APIを使ってその日の投稿タイプに応じた草稿を生成し、
ThreadsへAutomatic投稿、X/note草稿をSlack通知する
※ LinkedIn は一時無効化中（post_linkedin.py は保持）
"""

import os
import json
import datetime
import anthropic
from post_threads import post_to_threads
# from post_linkedin import post_to_linkedin  # LinkedIn 一時無効化
from notify_slack import notify_slack
from sheets import append_post_record

STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy():
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def determine_post_type(strategy: dict) -> str:
    """ローテーションインデックス（当日の投稿番号）から投稿タイプを決定"""
    rotation = strategy["post_rotation"]
    # 今日が何日目かでローテーション位置を決定
    day_of_year = datetime.date.today().timetuple().tm_yday
    # 1日5投稿 × ローテーション長で循環
    slot = int(os.environ.get("POST_SLOT", "0"))  # 0〜4（時刻ごとに異なるワークフローから渡される）
    index = ((day_of_year - 1) * 5 + slot) % len(rotation)
    return rotation[index]


def build_prompt(strategy: dict, post_type: str) -> str:
    positioning = strategy["positioning"]
    post_info = strategy["post_types"][post_type]
    persona = strategy["persona"]

    return f"""あなたはSNSコンテンツライターです。
以下の戦略に基づいて、日本語のSNS投稿文を1つ生成してください。

【ポジショニング】
- ポジション：{positioning["position"]}
- コンセプト：{positioning["concept"]}
- 差別化軸：{positioning["differentiation"]}

【ターゲット】
{persona["description"]}
悩み：{', '.join(persona["pain_points"])}

【今回の投稿タイプ】
{post_info["label"]}（{post_info["description"]}）

【ルール】
- 文字数：140〜200文字程度（X向け）
- 語尾は断定的・力強く
- ハッシュタグは不要
- 冒頭で目を引くフックを入れる
- 具体的な行動や言葉を使う
- 「ハイパフォーマー」「プロフェッショナル」「コンサル」「PM」「医師」「弁護士」など職種は適宜使ってよい

投稿文のみを出力してください。説明や前置きは不要です。"""


def generate_post(post_type: str, strategy: dict) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = build_prompt(strategy, post_type)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def main():
    strategy = load_strategy()
    post_type = determine_post_type(strategy)
    content = generate_post(post_type, strategy)

    print(f"[生成完了] タイプ: {post_type}")
    print(f"[本文]\n{content}\n")

    # Threads へ自動投稿
    threads_id = post_to_threads(content)
    # linkedin_id = post_to_linkedin(content)  # LinkedIn 一時無効化

    # X・note 草稿をSlack通知
    notify_slack(content, post_type)

    # 投稿DBに記録
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    week_number = now.isocalendar()[1]
    records = []
    if threads_id:
        records.append({
            "post_id": threads_id,
            "platform": "threads",
            "post_type": post_type,
            "content": content,
            "posted_at": now.isoformat(),
            "week_number": week_number,
        })
    # if linkedin_id:  # LinkedIn 一時無効化
    #     records.append({
    #         "post_id": linkedin_id,
    #         "platform": "linkedin",
    #         "post_type": post_type,
    #         "content": content,
    #         "posted_at": now.isoformat(),
    #         "week_number": week_number,
    #     })
    for record in records:
        append_post_record(record)

    print("[完了] 投稿・通知・DB記録が完了しました")


if __name__ == "__main__":
    main()
