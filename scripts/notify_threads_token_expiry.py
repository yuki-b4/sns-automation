"""Threadsトークンの失効が近づいたらSlackにリマインドするスクリプト。

Threads の長期アクセストークンは発行から約60日で失効する。失効日をAPIで取得する
手段が無い（debug_token 用の app secret を本リポジトリは持たない）ため、
config/threads_token.json の token_updated_at（運用者がトークン更新時に手動で書き換える
日付）を起点に失効予定日を計算し、残りが remind_days_before 日以内になった日だけ通知する。

Claude API は呼ばないため preflight は不要（db_update_reminder と同じ系統）。
失効窓の外の日は Slack に何も送らず即終了する（通知ノイズ防止）。
"""

import os
import json
import datetime

from notify_slack import notify_slack_token_expiry_reminder


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/threads_token.json")


def main() -> None:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    updated_at_str = cfg["token_updated_at"]
    valid_days = int(cfg.get("valid_days", 60))
    remind_days_before = int(cfg.get("remind_days_before", 7))

    updated_at = datetime.date.fromisoformat(updated_at_str)
    expiry = updated_at + datetime.timedelta(days=valid_days)

    today_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
    days_left = (expiry - today_jst).days

    if days_left > remind_days_before:
        print(
            f"[threads_token_reminder] 失効まで{days_left}日"
            f"（閾値{remind_days_before}日超）のためスキップ（更新日={updated_at_str} / 失効予定={expiry}）"
        )
        return

    print(f"[threads_token_reminder] 失効まで{days_left}日 → Slack通知（失効予定={expiry}）")
    notify_slack_token_expiry_reminder(days_left, expiry.isoformat(), updated_at_str)


if __name__ == "__main__":
    main()
