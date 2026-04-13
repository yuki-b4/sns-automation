"""
投稿生成スクリプト
Claude APIを使ってその日の投稿タイプに応じた草稿を生成し、
ThreadsへAutomatic投稿、投稿内容をSlack通知する
※ LinkedIn は一時無効化中（post_linkedin.py は保持）
"""

import os
import json
import datetime
import anthropic
from preflight import run_all as preflight_check
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
    # 1日6投稿 × ローテーション長で循環
    slot = int(os.environ.get("POST_SLOT", "0"))  # 0〜5（時刻ごとに異なるワークフローから渡される）
    index = ((day_of_year - 1) * 6 + slot) % len(rotation)
    return rotation[index]


def build_prompt(strategy: dict, post_type: str) -> str:
    positioning = strategy["positioning"]
    post_info = strategy["post_types"][post_type]
    persona = strategy["persona"]

    return f"""あなたはSNSコンテンツライターです。
以下の戦略に基づいて、日本語のThreads投稿文と補足セルフリプライを生成してください。

【ポジショニング】
- ポジション：{positioning["position"]}
- コンセプト：{positioning["concept"]}
- 差別化軸：{positioning["differentiation"]}
- 想起ワード：{positioning["tagline"]}
- ステートメント：{positioning["statement"]}

【ターゲット】
{persona["description"]}
悩み：{', '.join(persona["pain_points"])}

【今回の投稿タイプ】
{post_info["label"]}（{post_info["description"]}）

【ルール】
- 本文：100〜180文字程度（Threadsのカジュアル・会話的なトーンで）
- 語尾は断定的・力強く
- 冒頭で目を引くフックを入れる
- 具体的な行動や言葉を使う
- 自慢に見える表現は避け、共感・学び・プロセスとして語る
- 「ハイパフォーマー」「プロフェッショナル」「コンサル」「PM」「医師」「弁護士」など職種は適宜使ってよい
- 補足リプライ：30〜60文字の追加情報・問いかけ・CTAのいずれか（本文の続きや深掘りとなる内容）
- 「否定→転換→正解提示」の三段構成を毎回繰り返さないこと（構成にバリエーションを持たせる）
- 「もう終わり」「〜からしか生まれない」など同じ締めフレーズを使い回さないこと
- 冒頭は抽象的な主語（「〇〇な人は」）より具体的な場面・体験・数字から入ることを優先する
- 対話系の場合は、質問する前に必ず自分の考えや経験を先に開示すること

以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（ここに本文）

【補足リプライ】
（ここに補足リプライ）"""


def _parse_post(raw: str) -> dict:
    """生成テキストを本文と補足リプライにパース"""
    content = ""
    self_reply = ""
    if "【本文】" in raw and "【補足リプライ】" in raw:
        parts = raw.split("【補足リプライ】")
        content = parts[0].replace("【本文】", "").strip()
        self_reply = parts[1].strip()
    else:
        content = raw
    return {"content": content, "self_reply": self_reply}


def generate_post(post_type: str, strategy: dict) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Threads用コンテンツ生成
    threads_prompt = build_prompt(strategy, post_type)
    threads_message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": threads_prompt}],
    )
    threads_result = _parse_post(threads_message.content[0].text.strip())

    return {
        "content": threads_result["content"],
        "self_reply": threads_result["self_reply"],
    }


def main():
    # Claude API呼び出し前に外部サービスの接続確認
    preflight_check()

    strategy = load_strategy()
    post_type = determine_post_type(strategy)
    result = generate_post(post_type, strategy)
    content = result["content"]
    self_reply = result["self_reply"]

    print(f"[生成完了] タイプ: {post_type}")
    print(f"[Threads本文]\n{content}\n")
    if self_reply:
        print(f"[Threads補足リプライ]\n{self_reply}\n")

    # Threads へ自動投稿
    threads_id = post_to_threads(content)
    # linkedin_id = post_to_linkedin(content)  # LinkedIn 一時無効化

    # セルフリプライ投稿（投稿成功かつ補足リプライがある場合）
    if threads_id and self_reply:
        reply_id = post_to_threads(self_reply, reply_to_id=threads_id)
        if reply_id:
            print(f"[Threads] セルフリプライ投稿成功: {reply_id}")

    # Threadsへの投稿内容をSlack通知
    slack_content = content
    if self_reply:
        slack_content += f"\n\n↩️ セルフリプライ：{self_reply}"
    notify_slack(slack_content, post_type)

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
