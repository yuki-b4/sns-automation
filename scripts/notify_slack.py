"""
Slack通知モジュール
Threadsへの投稿内容をSlack Incoming Webhookで通知する
"""

import os
import requests
import json


SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK", "")
SLACK_USER_ID = os.environ.get("SLACK_USER_ID", "")

POST_TYPE_LABELS = {
    "permission": "許可系",
    "structure": "体系化系",
    "personal": "自己開示系",
    "opinion": "業界考察系",
    "dialogue": "対話系",
    "note_promo": "note誘導系",
    "gw_note_promo": "GW note拡散系",
}


def _user_mention_prefix() -> str:
    """SLACK_USER_ID が設定されていれば '<@UXXX> ' を返す。未設定なら空文字。
    アクション要求通知（リマインド・警告・レポート完成）で先頭に付与する。"""
    return f"<@{SLACK_USER_ID}> " if SLACK_USER_ID else ""


def _post_to_slack(blocks: list) -> None:
    """Slack Incoming Webhook にブロックを送信する共通関数"""
    if not SLACK_WEBHOOK:
        print("[Slack] WebhookURLが未設定のためスキップ")
        return
    resp = requests.post(
        SLACK_WEBHOOK,
        data=json.dumps({"blocks": blocks}),
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code != 200:
        print(f"[Slack] 通知失敗: {resp.status_code} {resp.text}")
    else:
        print("[Slack] 通知成功")


def notify_slack(content: str, post_type: str, title: str = "Threads投稿完了") -> None:
    label = POST_TYPE_LABELS.get(post_type, post_type)
    _post_to_slack([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"✅ {title}（{label}）"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": content},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "⚡ 投稿後30分以内にリプライ返信を確認してください（返信は最大のエンゲージメントシグナルです）"}
            ],
        },
    ])


def notify_slack_note(title: str, mode: str, github_url: str) -> None:
    """note記事ドラフト生成完了をSlackに通知。本文は含めずGitHub URLのみを送信。"""
    mode_label = "無料note" if mode == "free" else "有料note"
    _post_to_slack([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📝 note記事ドラフト生成完了（{mode_label}）"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{title}*"},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "記事を開く"},
                "url": github_url,
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "✏️ 確認・微修正後、note.comに手動で投稿してください"}
            ],
        },
    ])


def notify_slack_note_generation_failure(stage: str, mode: str, error: str) -> None:
    """note生成パイプラインの致命的失敗をSlackに通知（メンション付き）。
    stage: 失敗工程名（例: "テーマ動的生成（JSONパース）"）
    mode:  free / paid
    error: 例外メッセージや失敗理由の詳細（長い場合は先頭500字に切り詰める）"""
    mode_label = "無料note" if mode == "free" else "有料note"
    mention = _user_mention_prefix()
    error_excerpt = (error or "（詳細なし）").strip()
    if len(error_excerpt) > 500:
        error_excerpt = error_excerpt[:500] + "…"
    _post_to_slack([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"❌ note生成失敗（{mode_label}）"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{mention}*失敗工程:* {stage}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*原因:*\n```\n{error_excerpt}\n```"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "🔧 ワークフローログを確認し、必要に応じて手動で再実行してください"}
            ],
        },
    ])


def notify_slack_note_analysis(date_str: str, github_url: str, summary: str = "") -> None:
    """note週次分析レポート完成をSlackに通知。サマリー（200字以内）＋GitHubレポートURLを送信。"""
    mention = _user_mention_prefix()
    lead_text = f"{mention}note週次分析レポートが完成しました。" if mention else "note週次分析レポートが完成しました。"
    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 note週次分析レポート完成 ({date_str})"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": lead_text},
        },
    ]
    if summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary[:400]},
        })
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "全文はGitHubで確認してください。"},
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "レポートを開く"},
            "url": github_url,
        },
    })
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": "💡 提言に基づいてnote_writing_guide.jsonを更新してください"}
        ],
    })
    _post_to_slack(blocks)


def notify_slack_duplicate_warning(new_content: str, similar_content: str, score: float, posted_at: str) -> None:
    """類似投稿検出時の警告通知（投稿はすでに実行済み）"""
    score_pct = int(score * 100)
    posted_date = posted_at[:10] if posted_at else "不明"
    mention = _user_mention_prefix()
    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⚠️ 類似投稿を検出（類似度 {score_pct}%）"},
        },
    ]
    if mention:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{mention}類似投稿が検出されました。手動削除・編集の要否を確認してください。"},
        })
    blocks.extend([
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*今回の投稿:*\n{new_content[:200]}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*類似した過去投稿（{posted_date}）:*\n{similar_content[:200]}"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "⚠️ 投稿は完了済みです。必要に応じて手動で削除・編集してください。"}
            ],
        },
    ])
    _post_to_slack(blocks)


def notify_slack_note_promo_skip(reason: str, date_str: str) -> None:
    """note誘導Threads投稿のスキップ通知（メンション付き）。
    note原稿不在 / note投稿DB の url 未入力など、運用者の手当てが必要なケースで使う。"""
    mention = _user_mention_prefix()
    _post_to_slack([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⏭️ note誘導Threads投稿をスキップ"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{mention}*対象日:* {date_str} (JST)\n*理由:* {reason}"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "📝 note投稿DBの該当行に `url` 列を埋めるか、note原稿を確認してください"}
            ],
        },
    ])


def notify_slack_db_update_reminder(analysis_labels: list[str], run_time_label: str) -> None:
    """分析ジョブ実行前のDB更新リマインド通知。
    analysis_labels: 当日実行される分析名（例: ["note週次分析"]）
    run_time_label:  実行予定時刻の表記（例: "本日 10:00 JST"）
    """
    if not analysis_labels:
        return
    items = "\n".join(f"• {name}" for name in analysis_labels)
    mention = _user_mention_prefix()

    # 分析種別に応じた具体的な更新項目を構築
    details = "実行前にGoogle SheetsのDB値を最新に手動更新してください。"
    if "note週次分析" in analysis_labels:
        details += "\n\n*note投稿DB*：投稿済みの記事は *status* 列を `posted` に変更"

    _post_to_slack([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "⏰ 分析実行前のDB更新リマインド"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{mention}本日は以下の分析ジョブが *{run_time_label}* に実行されます。\n\n"
                    f"{items}\n\n"
                    f"{details}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "📝 更新が完了したらこのメッセージはスルーでOKです"}
            ],
        },
    ])


def notify_slack_report(report_text: str, title: str = "改善レポート", body: str = "") -> None:
    """レポート生成完了をSlackに通知。
    body が指定された場合はその本文を直接Slackメッセージに含める（最大2800字）。
    省略時は全文をActionsログで確認するリンクのみ送信。
    """
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    actions_url = f"https://github.com/{repo}/actions/runs/{run_id}" if run_id and repo else ""

    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 {title}が生成されました"},
        },
    ]
    if body:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": body[:2800]},
        })
    elif actions_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "全文はGitHub Actionsのログで確認できます。"},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "ログを開く"},
                "url": actions_url,
            },
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "全文はGitHub Actionsのログで確認してください。"},
        })
    _post_to_slack(blocks)
