"""分析ジョブ実行前のDB更新リマインドをSlackに送るスクリプト。

JST曜日から当日実行される分析ジョブを判定する:
  - 月曜 JST 10:00: note週次分析 (note_analyze.yml)
  - 火・金 JST 08:00: 競合分析 (competitor.yml)

GitHub Actionsのcron登録は UTC 16:00 日/月/木（= JST 01:00 月/火/金）に設定し、
本スクリプトが当日JST曜日を判定して適切な通知メッセージを構築する。
"""

import datetime
from notify_slack import notify_slack_db_update_reminder


# JST weekday (0=Mon, 6=Sun) → (分析ラベル, 実行時刻表記)
DAILY_ANALYSES: dict[int, list[tuple[str, str]]] = {
    0: [("note週次分析", "本日 10:00 JST")],        # 月曜
    1: [("競合分析", "本日 08:00 JST")],            # 火曜
    4: [("競合分析", "本日 08:00 JST")],            # 金曜
}


def main() -> None:
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    weekday = now_jst.weekday()
    scheduled = DAILY_ANALYSES.get(weekday, [])

    if not scheduled:
        print(f"[db_update_reminder] {now_jst:%Y-%m-%d}(weekday={weekday}) は対象外のためスキップ")
        return

    # 同じ実行時刻の分析ラベルをまとめる
    by_time: dict[str, list[str]] = {}
    for label, run_time in scheduled:
        by_time.setdefault(run_time, []).append(label)

    for run_time, labels in by_time.items():
        print(f"[db_update_reminder] 通知送信: {labels} / {run_time}")
        notify_slack_db_update_reminder(labels, run_time)


if __name__ == "__main__":
    main()
