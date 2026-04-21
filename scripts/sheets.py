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


def append_note_record(record: dict) -> None:
    """note投稿DBにレコードを追加（シート名: note投稿DB）
    列構成: note_id | type | title | price | file_path | generated_at | posted_at | status
          | combination_pattern | title_type | hook_type | problem_type | solution_type
          | ref_threads_post_ids | views | likes | comments | selling_elements
    """
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[Sheets] 認証情報が未設定のためスキップ")
        return

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("note投稿DB")
    row = [
        "",                                           # A: note_id（投稿後に手動入力）
        record.get("type", ""),                      # B: free / paid
        record.get("title", ""),                     # C: 記事タイトル
        record.get("price", 0),                      # D: 0 or 1980
        record.get("file_path", ""),                 # E: output/notes/YYYY-MM-DD_free.md
        record.get("generated_at", ""),              # F: 生成日時（ISO形式）
        "",                                           # G: posted_at（投稿後に手動入力）
        record.get("status", "draft"),               # H: draft / posted
        record.get("combination_pattern", ""),       # I: 共感最大化 など
        record.get("title_type", ""),                # J: 共感直球型 など
        record.get("hook_type", ""),                 # K: 失敗談型 など
        record.get("problem_type", ""),              # L: ビフォー描写型 など
        record.get("solution_type", ""),             # M: Before/After型 など
        record.get("ref_threads_post_ids", ""),      # N: 参照したThreads投稿IDのカンマ区切り
        "",                                           # O: views（手動入力）
        "",                                           # P: likes（手動入力）
        "",                                           # Q: comments（手動入力）
        record.get("selling_element_ids", ""),       # R: 選択した売れる要素IDのカンマ区切り（paidのみ）
    ]
    sheet.append_row(row, value_input_option="RAW")
    print(f"[Sheets] note投稿DB記録: {record.get('type')} / {record.get('combination_pattern')} / {record.get('title')}")


def get_note_records(weeks: int = 4) -> list[dict]:
    """note投稿DBから過去N週分のレコードを返す（週次分析用）"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return []

    import datetime
    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("note投稿DB")
    records = sheet.get_all_records()

    cutoff = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ) - datetime.timedelta(weeks=weeks)

    result = []
    for r in records:
        if _is_recent(r.get("generated_at", ""), cutoff):
            result.append(r)
    return result


def bulk_upsert_metrics_records(records: list[dict]) -> None:
    """メトリクスDBのレコードを一括upsert（読み取り1回・post_idで末尾行を上書き）"""
    if not records:
        return
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[Sheets] 認証情報が未設定のためスキップ")
        return

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("メトリクスDB")

    # 既存レコードを1回だけ読み込み、post_id → 最後の行番号 のマップを作成
    existing = sheet.get_all_records()
    id_to_row: dict[str, int] = {}
    for i, r in enumerate(existing):
        normalized = _normalize_id(str(r.get("post_id", "")))
        if normalized:
            id_to_row[normalized] = i + 2  # 1-indexed + ヘッダー行

    batch_updates = []
    to_append = []
    for record in records:
        row = [
            record.get("post_id", ""),
            record.get("collected_at", ""),
            record.get("likes", 0),
            record.get("reposts", 0),
            record.get("replies", 0),
            record.get("impressions", 0),
            record.get("engagement_rate", 0.0),
        ]
        normalized_id = _normalize_id(str(record.get("post_id", "")))
        if normalized_id in id_to_row:
            batch_updates.append({"range": f"A{id_to_row[normalized_id]}:G{id_to_row[normalized_id]}", "values": [row]})
        else:
            to_append.append(row)

    if batch_updates:
        sheet.batch_update(batch_updates)
    if to_append:
        sheet.append_rows(to_append, value_input_option="RAW")




def get_recent_competitor_posts(days: int = 14, unanalyzed_only: bool = False) -> list[dict]:
    """競合投稿DBから投稿を返す。
    unanalyzed_only=True のとき analyzed が空の行のみ返し、_row に行番号（1始まり）を付与する。
    """
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return []

    import datetime
    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("競合投稿DB")
    records = sheet.get_all_records()

    result = []
    if unanalyzed_only:
        for i, r in enumerate(records):
            if str(r.get("analyzed", "")).strip().upper() != "TRUE":
                result.append({**r, "_row": i + 2})  # ヘッダー行分 +1、さらに1始まりで +1
    else:
        cutoff = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=9))
        ) - datetime.timedelta(days=days)
        for r in records:
            if _is_recent(r.get("posted_at", ""), cutoff):
                result.append(r)
    return result


def mark_competitor_posts_analyzed(row_numbers: list[int]) -> None:
    """競合投稿DBの指定行の analyzed カラム（G列）を TRUE にする"""
    if not row_numbers:
        return
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("競合投稿DB")
    updates = [{"range": f"G{row}", "values": [["TRUE"]]} for row in row_numbers]
    sheet.batch_update(updates)
    print(f"[Sheets] 競合投稿DB: {len(row_numbers)}件を分析済みにマーク")


def get_recent_posts_content(days: int = 14) -> list[dict]:
    """直近N日分の投稿テキストを投稿DBから取得（重複チェック・プロンプト注入用）"""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return []

    import datetime
    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("投稿DB")
    records = sheet.get_all_records()

    cutoff = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ) - datetime.timedelta(days=days)

    result = []
    for r in records:
        if r.get("platform") == "threads" and r.get("content") and _is_recent(r.get("posted_at", ""), cutoff):
            result.append({
                "content": r["content"],
                "post_type": r.get("post_type", ""),
                "posted_at": r.get("posted_at", ""),
            })
    return result


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



def append_cost_record(record: dict) -> None:
    """APIコストDBにトークン使用量とコストを追記する（シート名: APIコストDB）
    列構成: timestamp | script | model | input_tokens | output_tokens | cost_usd
    """
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[Sheets] 認証情報が未設定のためスキップ")
        return

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("APIコストDB")
    row = [
        record.get("timestamp", ""),
        record.get("script", ""),
        record.get("model", ""),
        record.get("input_tokens", 0),
        record.get("output_tokens", 0),
        record.get("cost_usd", 0.0),
    ]
    sheet.append_row(row, value_input_option="RAW")
    print(f"[Sheets] APIコストDB記録: {record.get('script')} / ${record.get('cost_usd', 0.0):.4f}")


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
