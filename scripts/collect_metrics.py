"""
エンゲージメント収集スクリプト
Threads の投稿メトリクスを取得してGoogle Sheetsに記録する（post_idで上書き）
毎晩22:00 JSTに実行・直近30日分の投稿を対象
※ LinkedIn は一時無効化中（collect_linkedin_metrics は保持）
"""

import os
import datetime
import requests
from sheets import get_recent_post_ids, bulk_upsert_metrics_records

THREADS_TOKEN = os.environ.get("THREADS_TOKEN", "")
# LINKEDIN_TOKEN = os.environ.get("LINKEDIN_TOKEN", "")  # LinkedIn 一時無効化
BASE_THREADS_URL = "https://graph.threads.net/v1.0"


def collect_threads_metrics(post_id: str) -> dict | None:
    if not THREADS_TOKEN:
        return None

    url = f"{BASE_THREADS_URL}/{post_id}/insights"
    resp = requests.get(url, params={
        "metric": "likes,reposts,replies,views",
        "access_token": THREADS_TOKEN,
    })
    data = resp.json()

    if "data" not in data:
        print(f"[Threads Metrics] 取得失敗 {post_id}: {data}")
        return None

    metrics = {item["name"]: item["values"][0]["value"] for item in data["data"]}
    likes = metrics.get("likes", 0)
    reposts = metrics.get("reposts", 0)
    replies = metrics.get("replies", 0)
    impressions = metrics.get("views", 0)
    engagement_rate = round((likes + reposts + replies) / impressions, 4) if impressions > 0 else 0.0

    return {
        "post_id": post_id,
        "likes": likes,
        "reposts": reposts,
        "replies": replies,
        "impressions": impressions,
        "engagement_rate": engagement_rate,
    }


# LinkedIn 一時無効化（関数は保持）
# def collect_linkedin_metrics(post_id: str) -> dict | None:
#     if not LINKEDIN_TOKEN:
#         return None
#
#     headers = {
#         "Authorization": f"Bearer {LINKEDIN_TOKEN}",
#         "X-Restli-Protocol-Version": "2.0.0",
#     }
#     url = f"https://api.linkedin.com/v2/organizationalEntityShareStatistics"
#     resp = requests.get(url, headers=headers, params={
#         "q": "organizationalEntity",
#         "shares[0]": post_id,
#     })
#     data = resp.json()
#
#     if "elements" not in data or not data["elements"]:
#         print(f"[LinkedIn Metrics] 取得失敗 {post_id}: {data}")
#         return None
#
#     stats = data["elements"][0].get("totalShareStatistics", {})
#     likes = stats.get("likeCount", 0)
#     reposts = stats.get("shareCount", 0)
#     replies = stats.get("commentCount", 0)
#     impressions = stats.get("impressionCount", 0)
#     engagement_rate = round((likes + reposts + replies) / impressions, 4) if impressions > 0 else 0.0
#
#     return {
#         "post_id": post_id,
#         "likes": likes,
#         "reposts": reposts,
#         "replies": replies,
#         "impressions": impressions,
#         "engagement_rate": engagement_rate,
#     }


def main():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat()
    post_ids = get_recent_post_ids(days=30)

    if not post_ids:
        print("[Metrics] 対象投稿なし")
        return

    collected = []
    for item in post_ids:
        post_id = item["post_id"]
        platform = item["platform"]

        if platform == "threads":
            metrics = collect_threads_metrics(post_id)
        # elif platform == "linkedin":  # LinkedIn 一時無効化
        #     metrics = collect_linkedin_metrics(post_id)
        else:
            continue

        if metrics:
            metrics["collected_at"] = now
            collected.append(metrics)
            print(f"[Metrics] 取得完了: {platform} / {post_id} / ER={metrics['engagement_rate']}")

    bulk_upsert_metrics_records(collected)
    print(f"[Metrics] 収集完了（{len(collected)}件記録）")


if __name__ == "__main__":
    main()
