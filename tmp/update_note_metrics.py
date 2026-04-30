"""
note投稿DBの views / likes / comments を画像から抽出した値で一括更新するtmpスクリプト。

実行前に以下を環境変数に設定:
  - GOOGLE_SHEETS_ID
  - GOOGLE_SERVICE_ACCOUNT_JSON

実行:
  python tmp/update_note_metrics.py            # ドライラン（書き込みなし）
  python tmp/update_note_metrics.py --apply    # 実際に書き込み

タイトル完全一致で C列 を検索し、O / P / Q 列を batch_update する。
DB に同タイトルが複数あれば全行を上書きする（基本的に重複しない前提）。

DB のタイトルは手動で変更されることがあるため、画像にあって DB に無いタイトルが
1件でもあれば書き込みモードでは NoteTitleNotFoundError を送出して中断する。
（手動編集の取りこぼしを silent skip させないため）

ドライランの場合は中断せず、output/notes/*.md の1行目（# 見出し）と照合して
一致するファイルがあればログに「ファイル名」を併記する。一致するファイルが
無いタイトルはタイトルだけログに残す。
"""

import os
import sys
import glob

# scripts/ を import path に追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from sheets import GOOGLE_SHEETS_ID, GOOGLE_SERVICE_ACCOUNT_JSON, get_client


NOTES_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "notes")


class NoteTitleNotFoundError(Exception):
    """画像にあるタイトルが note投稿DB に見つからなかった場合に送出"""

    def __init__(self, missing_titles: list[str]):
        self.missing_titles = missing_titles
        super().__init__(
            f"note投稿DB に存在しないタイトルが {len(missing_titles)} 件あります"
        )


def _find_note_file_by_title(title: str) -> str | None:
    """output/notes/*.md のうち1行目（# 見出し）が title と一致するファイル名を返す"""
    if not os.path.isdir(NOTES_DIR):
        return None
    for path in sorted(glob.glob(os.path.join(NOTES_DIR, "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                first_line = f.readline().strip()
        except OSError:
            continue
        # markdown 見出しの "# " を剥がして比較
        heading = first_line.lstrip("#").strip()
        if heading == title:
            return os.path.basename(path)
    return None


# 画像から抽出した値（記事タイトル → views / likes / comments）
# 集計時刻: 2026-04-27 07:41
NOTE_METRICS = [
    {"title": "頑張れてしまう人ほど働き方を変えられない理由",         "views": 44, "likes": 11, "comments": 0},
    {"title": "意志に頼らず月曜日の集中力を覚醒させる3つの回復術",     "views": 31, "likes": 0,  "comments": 0},
    {"title": "30代エンジニアが金曜午後も頭が冴えるリソース分配設計",  "views": 30, "likes": 2,  "comments": 0},
    {"title": "定時で帰れない自分が嫌だ",                              "views": 29, "likes": 2,  "comments": 0},
    {"title": "Slack通知を3つ変えたら集中時間が週5時間増えた話",       "views": 25, "likes": 1,  "comments": 0},
    {"title": "大事な意思決定を後回しにしてしまった夜",                "views": 23, "likes": 3,  "comments": 0},
    {"title": "効率的に結果を出す人ほど家族時間とのねじれに苦しむ理由", "views": 17, "likes": 6,  "comments": 0},
    {"title": "月曜朝の1時間を受動的に使うと家族との時間が減る",       "views": 13, "likes": 0,  "comments": 0},
    {"title": "「全部大事」に見える時こそ、決める回数を減らしてみる",  "views": 12, "likes": 0,  "comments": 0},
    {"title": "休日に罪悪感がある人生なんてうんざりだ",                "views": 4,  "likes": 0,  "comments": 0},
]


def main(apply: bool) -> None:
    if not GOOGLE_SHEETS_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("[ERROR] GOOGLE_SHEETS_ID / GOOGLE_SERVICE_ACCOUNT_JSON が未設定")
        sys.exit(1)

    client = get_client()
    sheet = client.open_by_key(GOOGLE_SHEETS_ID).worksheet("note投稿DB")
    rows = sheet.get_all_values()

    if not rows:
        print("[ERROR] note投稿DBが空")
        sys.exit(1)

    header = rows[0]
    # title が C列(index=2) であることを念のため確認
    if len(header) < 3 or header[2] != "title":
        print(f"[WARN] 想定ヘッダーと異なる: {header[:5]}")

    # title -> [行番号(1始まり), ...]
    title_to_rows: dict[str, list[int]] = {}
    for i, row in enumerate(rows[1:], start=2):
        if len(row) < 3:
            continue
        title = row[2].strip()
        if title:
            title_to_rows.setdefault(title, []).append(i)

    batch_updates = []
    matched = 0
    missing = []

    for m in NOTE_METRICS:
        title = m["title"]
        target_rows = title_to_rows.get(title, [])
        if not target_rows:
            missing.append(title)
            continue
        for r in target_rows:
            batch_updates.append({
                "range": f"O{r}:Q{r}",
                "values": [[m["views"], m["likes"], m["comments"]]],
            })
            matched += 1
            print(f"[MATCH] row={r}  views={m['views']:>3}  likes={m['likes']:>2}  comments={m['comments']:>2}  {title}")

    print()
    print(f"更新対象: {matched}行  (画像の項目: {len(NOTE_METRICS)}件)")

    if missing:
        print()
        print(f"[WARN] note投稿DBに存在しないタイトル: {len(missing)}件")
        for t in missing:
            note_file = _find_note_file_by_title(t)
            if note_file:
                print(f"  - {t}  → output/notes/{note_file} と一致（DB側のタイトルが手動変更された可能性）")
            else:
                print(f"  - {t}")

        # 書き込みモードでは silent skip させずに中断する
        if apply:
            raise NoteTitleNotFoundError(missing)

    if not apply:
        print()
        print("ドライラン完了（--apply 未指定のため書き込みなし）")
        return

    if not batch_updates:
        print("書き込み対象なし")
        return

    sheet.batch_update(batch_updates, value_input_option="RAW")
    print(f"[DONE] note投稿DB {matched}行を更新")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    try:
        main(apply)
    except NoteTitleNotFoundError as e:
        print()
        print(f"[ERROR] {e}")
        print("対処: NOTE_METRICS のタイトルを DB の現状値に合わせて書き換えてから再実行してください。")
        sys.exit(2)
