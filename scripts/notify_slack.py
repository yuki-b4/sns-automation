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


def notify_slack(content: str, post_type: str, title: str = "Threads投稿完了") -> None:
    if not SLACK_WEBHOOK:
        print("[Slack] WebhookURLが未設定のためスキップ")
        return

    label = POST_TYPE_LABELS.get(post_type, post_type)
    message = {
        "blocks": [
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
        ]
    }

    resp = requests.post(SLACK_WEBHOOK, data=json.dumps(message), headers={"Content-Type": "application/json"})
    if resp.status_code != 200:
        print(f"[Slack] 通知失敗: {resp.status_code} {resp.text}")
    else:
        print("[Slack] 通知成功")


def notify_slack_report(report_text: str, title: str = "改善レポート") -> None:
    """レポート生成完了をSlackに通知（全文はActionsログで確認）"""
    if not SLACK_WEBHOOK:
        print("[Slack] WebhookURLが未設定のためスキップ")
        return

    run_id = os.environ.get("GITHUB_RUN_ID", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    actions_url = f"https://github.com/{repo}/actions/runs/{run_id}" if run_id and repo else ""

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 {title}が生成されました"},
        },
    ]

    if actions_url:
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

    message = {"blocks": blocks}

    resp = requests.post(SLACK_WEBHOOK, data=json.dumps(message), headers={"Content-Type": "application/json"})
    if resp.status_code != 200:
        print(f"[Slack] レポート通知失敗: {resp.status_code} {resp.text}")
    else:
        print("[Slack] レポート通知成功")
