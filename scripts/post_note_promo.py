"""
note誘導用のThreads布石投稿スクリプト
config/note_promo_posts.json に登録された日付の投稿を21:00 JSTに配信する。
- 本日のJST日付が config に無ければ何もせず終了（ワークフロー誤発火に対する安全弁）
- URL プレースホルダ (XXXXXXXXXXXX) が残っていればエラー終了（誤投稿防止）
- 通常の投稿フローに倣い preflight → Threads投稿 → Slack通知 → 投稿DB記録
"""

import os
import json
import time
import datetime

from preflight import run_all as preflight_check
from post_threads import post_to_threads
from notify_slack import notify_slack
from sheets import append_post_record


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/note_promo_posts.json")
URL_PLACEHOLDER_MARKER = "XXXXXXXXXXXX"


def _today_jst() -> str:
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return now.date().isoformat()


def _load_today_post() -> dict | None:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    today = _today_jst()
    for post in config.get("posts", []):
        if post.get("date") == today:
            return post
    return None


def main() -> None:
    post = _load_today_post()
    if not post:
        print(f"[note_promo] 本日 ({_today_jst()} JST) は投稿予定なし。処理をスキップします。")
        return

    content = post["content"]
    self_reply = post["self_reply"]
    post_type = post.get("post_type", "structure")

    # URL 未置換の誤投稿を防ぐ
    if URL_PLACEHOLDER_MARKER in self_reply:
        raise SystemExit(
            f"[note_promo] self_reply に URL プレースホルダ '{URL_PLACEHOLDER_MARKER}' が残っています。"
            f"config/note_promo_posts.json の {post['date']} の URL を実 URL に置換してください。"
        )

    # 外部サービス疎通確認（Threads / Slack / Google Sheets）
    preflight_check()

    print(f"[note_promo] 投稿対象日付: {post['date']} (JST)")
    print(f"[Threads本文]\n{content}\n")
    print(f"[Threads補足リプライ]\n{self_reply}\n")

    # Threads へ投稿（本文 → セルフリプライ）
    threads_id = post_to_threads(content)
    reply_id = None
    if threads_id and self_reply:
        time.sleep(5)  # 本文コンテナ処理完了を待つ
        reply_id = post_to_threads(self_reply, reply_to_id=threads_id)
        if reply_id:
            print(f"[Threads] セルフリプライ投稿成功: {reply_id}")

    # Slack 通知
    slack_content = content
    if self_reply:
        slack_content += f"\n\n↩️ セルフリプライ：{self_reply}"
    notify_slack(slack_content, post_type, title="note誘導Threads投稿完了")

    # 投稿DB に記録
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if threads_id:
        append_post_record({
            "post_id": threads_id,
            "platform": "threads",
            "post_type": post_type,
            "content": content,
            "posted_at": now.isoformat(),
            "week_number": now.isocalendar()[1],
        })

    print("[note_promo] 投稿・通知・DB記録が完了しました")


if __name__ == "__main__":
    main()
