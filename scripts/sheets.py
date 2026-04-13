"""
Google Sheetsへの読み書きモジュール
Service Accountを使ったアクセス
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")

# GitHub SecretsからService AccountのJSONを環境変数で受け取る
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


def get_client():
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def append_post_record(record: dict) -> None:
    """投稿DBにレコードを追加（Sheet1）"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[Sheets] 認証情報が未設定のためスキップ")
        return

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("投稿DB")
    row = [
        record.get("post_id", ""),
        record.get("platform", ""),
        record.get("post_type", ""),
        record.get("content", ""),
        record.get("posted_at", ""),
        record.get("week_number", ""),
    ]
    sheet.append_row(row, value_input_option="RAW")
    print(f"[Sheets] 投稿DB記録: {record['platform']} / {record['post_id']}")


def upsert_metrics_record(record: dict) -> None:
    """メトリクスDBのレコードをpost_idで上書き（なければ末尾に追加）"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[Sheets] 認証情報が未設定のためスキップ")
        return

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("メトリクスDB")
    row = [
        record.get("post_id", ""),
        record.get("collected_at", ""),
        record.get("likes", 0),
        record.get("reposts", 0),
        record.get("replies", 0),
        record.get("impressions", 0),
        record.get("engagement_rate", 0.0),
    ]

    existing = sheet.get_all_records()
    normalized_id = _normalize_id(str(record.get("post_id", "")))
    for i, r in enumerate(existing):
        if _normalize_id(str(r.get("post_id", ""))) == normalized_id:
            row_num = i + 2  # 1-indexed + ヘッダー行
            sheet.update(range_name=f"A{row_num}:G{row_num}", values=[row])
            return

    sheet.append_row(row, value_input_option="RAW")


def append_competitor_record(record: dict) -> None:
    """競合分析DBにレコードを追加（Sheet3）"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[Sheets] 認証情報が未設定のためスキップ")
        return

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("競合分析DB")
    row = [
        record.get("competitor_id", ""),
        record.get("platform", ""),
        record.get("top_posts", ""),
        record.get("avg_engagement_rate", 0.0),
        record.get("dominant_themes", ""),
        record.get("positioning_gap", ""),
        record.get("collected_at", ""),
    ]
    sheet.append_row(row)


def get_recent_post_ids(days: int = 2) -> list[dict]:
    """直近N日分の投稿IDを投稿DBから取得"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return []

    import datetime
    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("投稿DB")
    records = sheet.get_all_records()

    cutoff = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))) - datetime.timedelta(days=days)
    result = []
    for r in records:
        if r.get("platform") in ("threads", "linkedin") and r.get("post_id"):
            try:
                posted_at = datetime.datetime.fromisoformat(r["posted_at"])
                if posted_at >= cutoff:
                    result.append({"post_id": _normalize_id(r["post_id"]), "platform": r["platform"]})
            except Exception:
                pass
    return result


def get_weekly_data(weeks: int = 1, days: int | None = None) -> dict:
    """過去N週分（またはN日分）の投稿・メトリクスデータを返す"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return {"posts": [], "metrics": []}

    client = get_client()
    spreadsheet = client.open_by_key(GOOGLE_SHEETS_ID)
    posts = spreadsheet.worksheet("投稿DB").get_all_records()
    metrics = spreadsheet.worksheet("メトリクスDB").get_all_records()

    import datetime
    delta = datetime.timedelta(days=days) if days is not None else datetime.timedelta(weeks=weeks)
    cutoff = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))) - delta
    recent_posts = [
        {**p, "post_id": _normalize_id(p["post_id"])}
        for p in posts if _is_recent(p.get("posted_at", ""), cutoff)
    ]
    post_ids = {p["post_id"] for p in recent_posts}
    recent_metrics = [
        {**m, "post_id": _normalize_id(m["post_id"])}
        for m in metrics
        if _normalize_id(m.get("post_id", "")) in post_ids
    ]

    return {"posts": recent_posts, "metrics": recent_metrics}


def get_recent_competitor_data() -> list[dict]:
    """直近の競合分析データを返す"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return []

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("競合分析DB")
    return sheet.get_all_records()


def get_competitor_accounts() -> list[str]:
    """競合分析DB Sheetから競合アカウントIDリストを取得"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return []

    client = get_client()
    try:
        sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("競合アカウント")
        records = sheet.get_all_records()
        return [r["account_id"] for r in records if r.get("account_id")]
    except Exception:
        return []


def _normalize_id(value) -> str:
    """Google Sheetsが科学表記に変換した数値IDを文字列に正規化する"""
    from decimal import Decimal, InvalidOperation
    s = str(value).strip()
    try:
        return str(int(Decimal(s)))
    except (InvalidOperation, ValueError):
        return s


def _is_recent(posted_at_str: str, cutoff) -> bool:
    import datetime
    try:
        posted_at = datetime.datetime.fromisoformat(posted_at_str)
        return posted_at >= cutoff
    except Exception:
        return False
