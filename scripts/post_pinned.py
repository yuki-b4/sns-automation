"""
ピン留め投稿用 臨時バッチスクリプト
確定済みのピン留め投稿文をThreadsに投稿し、投稿DBに記録する。
（Claude API不使用・固定テキストで投稿）
"""

import os
import sys
import time
import datetime
from post_threads import post_to_threads
from notify_slack import notify_slack
from sheets import append_post_record

# ─────────────────────────────────────────
# ピン留め投稿 確定文
# ─────────────────────────────────────────
PINNED_CONTENT = """「頑張ってるのに成果が安定しない」

そのとき足りないのは努力量じゃなく、設計。

脳の負荷が高い仕事でハイパフォーマーであり続けたい人が、意志力に頼らず再現性のある成果を出すための構造設計を毎日発信しています。"""

PINNED_REPLY = """フォローすると届く内容：

・意志力に依存しない行動設計
・パフォーマンスの波をなくすコンディション設計
・ハイパフォーマーが陥りやすい罠と脱出方法の解説

「もっと頑張れば解決する」からスマートな自分へとアップデートしたい方へ。"""

POST_TYPE = "pinned"


def main():
    print("[ピン留め投稿] 開始")
    print(f"[本文]\n{PINNED_CONTENT}\n")
    print(f"[リプライ]\n{PINNED_REPLY}\n")

    # 本文投稿
    threads_id = post_to_threads(PINNED_CONTENT)
    if not threads_id:
        print("[ピン留め投稿] 本文投稿失敗。処理を中断します。")
        sys.exit(1)

    # リプライ投稿
    reply_id = None
    time.sleep(5)
    reply_id = post_to_threads(PINNED_REPLY, reply_to_id=threads_id)
    if reply_id:
        print(f"[ピン留め投稿] リプライ投稿成功: {reply_id}")
    else:
        print("[ピン留め投稿] リプライ投稿失敗（本文は投稿済み）")

    # Slack通知
    slack_content = PINNED_CONTENT
    if reply_id:
        slack_content += f"\n\n↩️ リプライ：{PINNED_REPLY}"
    notify_slack(slack_content, POST_TYPE, title="📌 ピン留め投稿完了")

    # 投稿DBに記録
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    week_number = now.isocalendar()[1]
    append_post_record({
        "post_id": threads_id,
        "platform": "threads",
        "post_type": POST_TYPE,
        "content": PINNED_CONTENT,
        "posted_at": now.isoformat(),
        "week_number": week_number,
    })

    print("[ピン留め投稿] 完了")


if __name__ == "__main__":
    main()
