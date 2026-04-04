"""
LinkedIn APIへの投稿モジュール
法人ページ（Organization）へのテキスト投稿
"""

import os
import requests


LINKEDIN_TOKEN = os.environ.get("LINKEDIN_TOKEN", "")
LINKEDIN_ORG_ID = os.environ.get("LINKEDIN_ORG_ID", "")
API_URL = "https://api.linkedin.com/v2/ugcPosts"


def post_to_linkedin(content: str) -> str | None:
    if not LINKEDIN_TOKEN or not LINKEDIN_ORG_ID:
        print("[LinkedIn] トークンまたは組織IDが未設定のためスキップ")
        return None

    headers = {
        "Authorization": f"Bearer {LINKEDIN_TOKEN}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }

    payload = {
        "author": f"urn:li:organization:{LINKEDIN_ORG_ID}",
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": content},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        },
    }

    resp = requests.post(API_URL, headers=headers, json=payload)

    if resp.status_code not in (200, 201):
        print(f"[LinkedIn] 投稿失敗: {resp.status_code} {resp.text}")
        return None

    post_id = resp.headers.get("X-RestLi-Id", resp.json().get("id", "unknown"))
    print(f"[LinkedIn] 投稿成功: {post_id}")
    return post_id
