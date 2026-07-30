"""
事前チェックモジュール
Claude API呼び出しの前に、各外部サービスへの接続・書き込み可否を確認する。
いずれかのチェックが失敗した場合は SystemExit で処理を中断し、
Claude APIへの無駄な課金を防ぐ。
"""

import os
import json
import requests


def check_threads() -> None:
    """Threadsトークン・ユーザーIDの有効性を確認（軽量なプロフィール取得で検証）"""
    token = os.environ.get("THREADS_TOKEN", "")
    user_id = os.environ.get("THREADS_USER_ID", "")

    if not token or not user_id:
        raise EnvironmentError("[Preflight] THREADS_TOKEN または THREADS_USER_ID が未設定です")

    resp = requests.get(
        f"https://graph.threads.net/v1.0/{user_id}",
        params={"fields": "id,username", "access_token": token},
        timeout=10,
    )
    data = resp.json()

    if "error" in data:
        code = data["error"].get("code", "")
        msg = data["error"].get("message", "")
        raise ConnectionError(f"[Preflight] Threads認証エラー (code={code}): {msg}")

    print(f"[Preflight] Threads OK: @{data.get('username', user_id)}")


def check_slack() -> None:
    """Slack Webhook URLの疎通確認（チャンネルに可視メッセージを残さないサイレント方式）。

    Slack Incoming Webhook は不正な本文に対しては HTTP 400 を返し、URLそのものが
    無効な場合は 404、トークン失効は 403 を返す。本文として故意に空 JSON を送ることで
    可視メッセージを発生させずに到達性のみを確認する。
    - URLが有効 → 400 "no_text" / "invalid_payload"（＝OK、可視メッセージなし）
    - URL自体が無効 → 404 "no_service" / "no_service_id"
    - トークン失効 → 403 "invalid_token"
    Slack は時期によって "no_text" と "invalid_payload" を使い分けるため、両方を成功とみなす。"""
    webhook = os.environ.get("SLACK_WEBHOOK", "")

    if not webhook:
        raise EnvironmentError("[Preflight] SLACK_WEBHOOK が未設定です")

    if not webhook.startswith("https://hooks.slack.com/"):
        raise ValueError(f"[Preflight] SLACK_WEBHOOK の形式が不正です: {webhook[:40]}...")

    resp = requests.post(
        webhook,
        data=json.dumps({}),  # 故意に text を欠落させる → 到達すれば 400 no_text / invalid_payload が返る
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    body = (resp.text or "").strip().lower()

    if resp.status_code == 400 and ("no_text" in body or "invalid_payload" in body):
        # Webhook URL 自体は有効。可視メッセージは送信されない。
        print("[Preflight] Slack OK (silent check)")
        return

    if resp.status_code == 404:
        raise ConnectionError(f"[Preflight] Slack Webhook URLが無効: HTTP 404 {body}")
    if resp.status_code == 403:
        raise ConnectionError(f"[Preflight] Slack Webhookトークン失効の可能性: HTTP 403 {body}")

    raise ConnectionError(f"[Preflight] Slack接続エラー: HTTP {resp.status_code} {body}")


def check_google_sheets() -> None:
    """Google Sheetsへの書き込み権限を確認（スプレッドシートのメタデータ取得で検証）"""
    sheets_id = os.environ.get("GOOGLE_SHEETS_ID", "")
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    if not sheets_id or not sa_json:
        raise EnvironmentError("[Preflight] GOOGLE_SHEETS_ID または GOOGLE_SERVICE_ACCOUNT_JSON が未設定です")

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_dict = json.loads(sa_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(sheets_id)

        # 必要なシートが存在するか確認
        sheet_titles = [ws.title for ws in spreadsheet.worksheets()]
        required = ["投稿DB"]
        missing = [s for s in required if s not in sheet_titles]
        if missing:
            raise ValueError(f"[Preflight] Google Sheetsに必要なシートが見つかりません: {missing}")

    except json.JSONDecodeError:
        raise ValueError("[Preflight] GOOGLE_SERVICE_ACCOUNT_JSON のJSON形式が不正です")
    except Exception as e:
        raise ConnectionError(f"[Preflight] Google Sheets接続エラー: {e}")

    print(f"[Preflight] Google Sheets OK: {spreadsheet.title}")


_CHECKS = {
    "threads": ("Threads", check_threads),
    "slack": ("Slack", check_slack),
    "sheets": ("Google Sheets", check_google_sheets),
}

DEFAULT_CHECKS = ("threads", "slack", "sheets")


def run_all(checks: tuple[str, ...] = DEFAULT_CHECKS) -> None:
    """指定チェックを実行。1つでも失敗したら処理を中断する。

    checks を絞れるのは Threads へ投稿しないスクリプト（投稿アイデア生成など）のため。
    Threadsトークンが失効していても、Threadsを使わない処理まで巻き込んで
    止めないようにする。省略時は従来通り全チェックを実行する。
    """
    print("[Preflight] 事前チェック開始...")
    errors = []

    unknown = [c for c in checks if c not in _CHECKS]
    if unknown:
        raise ValueError(f"[Preflight] 未知のチェック名: {unknown}（利用可能: {list(_CHECKS)}）")

    for key in checks:
        name, check_fn = _CHECKS[key]
        try:
            check_fn()
        except Exception as e:
            errors.append(str(e))
            print(f"[Preflight] ✗ {name}: {e}")

    if errors:
        print(f"\n[Preflight] {len(errors)}件のエラーが発生したため処理を中断します")
        raise SystemExit(1)

    print("[Preflight] 全チェック通過 ✓\n")
