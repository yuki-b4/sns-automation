"""
Threads APIへの投稿モジュール
Meta Threads API（2段階: コンテナ作成→公開）を使用
"""

import os
import time
import requests


THREADS_USER_ID = os.environ.get("THREADS_USER_ID", "")
THREADS_TOKEN = os.environ.get("THREADS_TOKEN", "")
BASE_URL = "https://graph.threads.net/v1.0"


def post_to_threads(content: str, reply_to_id: str | None = None) -> str | None:
    if not THREADS_TOKEN or not THREADS_USER_ID:
        print("[Threads] トークンまたはユーザーIDが未設定のためスキップ")
        return None

    # Step 1: メディアコンテナ作成
    create_url = f"{BASE_URL}/{THREADS_USER_ID}/threads"
    params = {
        "media_type": "TEXT",
        "text": content,
        "access_token": THREADS_TOKEN,
    }
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    create_resp = requests.post(create_url, params=params)
    create_data = create_resp.json()

    if "id" not in create_data:
        print(f"[Threads] コンテナ作成失敗: {create_data}")
        return None

    container_id = create_data["id"]

    # Step 2: 公開（コンテナ処理完了を待ってから公開）
    time.sleep(5)
    publish_url = f"{BASE_URL}/{THREADS_USER_ID}/threads_publish"
    publish_resp = requests.post(publish_url, params={
        "creation_id": container_id,
        "access_token": THREADS_TOKEN,
    })
    publish_data = publish_resp.json()

    if "id" not in publish_data:
        print(f"[Threads] 公開失敗: {publish_data}")
        return None

    post_id = publish_data["id"]
    print(f"[Threads] 投稿成功: {post_id}")
    return post_id
