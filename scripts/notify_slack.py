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


def notify_slack_report(report_text: str, title: str = "週次改善レポート") -> None:
    """レポートテキストをSlackに送信"""
    if not SLACK_WEBHOOK:
        print("[Slack] WebhookURLが未設定のためスキップ")
        return

    message = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📊 {title}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": report_text[:3000]},  # Slack上限対策
            },
        ]
    }

    resp = requests.post(SLACK_WEBHOOK, data=json.dumps(message), headers={"Content-Type": "application/json"})
    if resp.status_code != 200:
        print(f"[Slack] レポート通知失敗: {resp.status_code} {resp.text}")
    else:
        print("[Slack] レポート通知成功")
