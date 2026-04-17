"""
Slack通知モジュール
Threadsへの投稿内容をSlack Incoming Webhookで通知する
"""

import os
import requests
import json


SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK", "")

POST_TYPE_LABELS = {
    "permission": "許可系",
    "structure": "体系化系",
    "personal": "自己開示系",
    "opinion": "業界考察系",
    "dialogue": "対話系",
}


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


def notify_slack_note_analysis(date_str: str, github_url: str, summary: str = "") -> None:
    """note週次分析レポート完成をSlackに通知。サマリー（200字以内）＋GitHubレポートURLを送信。"""
    blocks: list = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 note週次分析レポート完成 ({date_str})"},
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
    _post_to_slack([
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⚠️ 類似投稿を検出（類似度 {score_pct}%）"},
        },
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
