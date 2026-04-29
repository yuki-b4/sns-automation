"""
過去スレッドのセルフリプライを投稿DBに遡及記録する一回きりスクリプト

GitHub Actions（workflow_dispatch）から手動実行する想定。

背景:
2026-04-29 以前は generate_post.py / post_note_promo.py がセルフリプライの
post_id を投稿DBに保存していなかったため、collect_metrics.py の収集対象から
漏れていた。本スクリプトは Threads Graph API の `/{root_id}/conversation` を
叩いて、自分のアカウントが付けたセルフリプライ（`is_reply_owned_by_me=true`）
を全部拾い、投稿DBに parent_post_id=root の行として追記する。

メトリクスDB への反映は次回 daily_metrics.yml 実行時に collect_metrics.py が
自動的に行う（投稿DB に新しい post_id が入っていれば拾われる）。

冪等性:
投稿DB に既に存在する post_id は重複追記しない。複数回実行しても安全。
"""

import os
import time
import datetime
import requests

from preflight import run_all as preflight_check
from sheets import get_client, _normalize_id
from notify_slack import notify_slack_report


THREADS_USER_ID = os.environ.get("THREADS_USER_ID", "")
THREADS_TOKEN = os.environ.get("THREADS_TOKEN", "")
GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")
BASE_URL = "https://graph.threads.net/v1.0"
JST = datetime.timezone(datetime.timedelta(hours=9))

CONVERSATION_FIELDS = "id,text,timestamp,is_reply_owned_by_me,replied_to,root_post"
PAGE_LIMIT = 50
SLEEP_BETWEEN_ROOTS = 0.5  # Threads API への礼節


def _fetch_conversation(root_id: str) -> list[dict]:
    """指定ルート投稿の conversation を全ページ取得する。
    取得失敗時は空リストを返してそのルートをスキップする（処理続行優先）。"""
    items: list[dict] = []
    url = f"{BASE_URL}/{root_id}/conversation"
    params: dict | None = {
        "fields": CONVERSATION_FIELDS,
        "access_token": THREADS_TOKEN,
        "limit": PAGE_LIMIT,
    }
    while True:
        resp = requests.get(url, params=params)
        try:
            data = resp.json()
        except ValueError:
            print(f"[Backfill] /{root_id}/conversation JSONパース失敗 status={resp.status_code}")
            return items

        if "data" not in data:
            print(f"[Backfill] /{root_id}/conversation 取得失敗: {data}")
            return items

        items.extend(data["data"])
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            break
        url = next_url
        params = None  # next_url はクエリ込み完全URL
        time.sleep(0.2)
    return items


def _parse_threads_timestamp(ts: str) -> tuple[str, int | str]:
    """Threads API の timestamp（ISO8601 / 末尾Z）を JST ISO 文字列と週番号に変換"""
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)
        return dt.isoformat(), dt.isocalendar()[1]
    except Exception:
        return ts, ""


def main() -> None:
    if not all([THREADS_USER_ID, THREADS_TOKEN, GOOGLE_SHEETS_ID]):
        raise SystemExit("[Backfill] 必須の環境変数（THREADS_USER_ID/THREADS_TOKEN/GOOGLE_SHEETS_ID）が未設定")

    # Claude API は使わないが Threads/Sheets/Slack 疎通確認のため preflight を流す
    preflight_check()

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("投稿DB")
    records = sheet.get_all_records()

    existing_ids: set[str] = set()
    for r in records:
        normalized = _normalize_id(str(r.get("post_id", "")))
        if normalized:
            existing_ids.add(normalized)

    roots = [
        r for r in records
        if r.get("platform") == "threads"
        and r.get("post_id")
        and not str(r.get("parent_post_id", "")).strip()
    ]

    print(f"[Backfill] ルート投稿 {len(roots)} 件を走査します")

    new_rows: list[list] = []  # batch append 用
    new_count = 0
    skipped_existing = 0
    failed_roots = 0

    for i, root in enumerate(roots, 1):
        root_id = _normalize_id(str(root["post_id"]))
        post_type = root.get("post_type", "")
        print(f"[Backfill] ({i}/{len(roots)}) root={root_id} type={post_type}")

        items = _fetch_conversation(root_id)
        if not items:
            failed_roots += 1
            time.sleep(SLEEP_BETWEEN_ROOTS)
            continue

        for item in items:
            reply_id = _normalize_id(str(item.get("id", "")))
            if not reply_id or reply_id == root_id:
                continue
            if not item.get("is_reply_owned_by_me"):
                continue
            if reply_id in existing_ids:
                skipped_existing += 1
                continue

            posted_at_iso, week_number = _parse_threads_timestamp(item.get("timestamp", ""))
            new_rows.append([
                reply_id,                     # A: post_id
                "threads",                    # B: platform
                post_type,                    # C: post_type（親と同じ）
                item.get("text", ""),         # D: content
                posted_at_iso,                # E: posted_at（JST）
                week_number,                  # F: week_number
                root_id,                      # G: parent_post_id（ルートで揃える）
            ])
            existing_ids.add(reply_id)
            new_count += 1
            print(f"[Backfill]   → 追記対象: {reply_id}")

        time.sleep(SLEEP_BETWEEN_ROOTS)

    # 投稿DBへ一括 append（per-row API呼び出しを避ける）
    if new_rows:
        sheet.append_rows(new_rows, value_input_option="RAW")

    print(
        f"[Backfill] 完了: 新規追記={new_count} / 既存スキップ={skipped_existing} / "
        f"取得失敗ルート={failed_roots}"
    )

    body = (
        f"・走査ルート投稿: {len(roots)} 件\n"
        f"・新規追記: {new_count} 件\n"
        f"・既存スキップ: {skipped_existing} 件\n"
        f"・取得失敗ルート: {failed_roots} 件\n\n"
        f"次回 daily_metrics.yml 実行で メトリクスDB にも反映されます。"
    )
    notify_slack_report("", title="セルフリプライ過去分バックフィル", body=body)


if __name__ == "__main__":
    main()
