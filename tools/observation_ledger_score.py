#!/usr/bin/env python3
"""観測台帳の貼り付け前検算スクリプト。

事業ロードマップ スプレッドシートの `観測台帳` シートに実装されている数式
（境界判定 J / 継続週数 M / スコア P / ランク Q）をそのまま再現し、
シート側では「黙って減点されるだけ」で気づけない事故を貼る前に落とす。

  - 業種名がマスタ11種と不一致 → VLOOKUP が外れて 5 点に落ちる（最大25点の損）
  - トリガー名がマスタ5種と不一致 → 0 点に落ちる（最大30点の損）
  - 単一拠点 / 中間管理職 に「不明」 → 未確認なのに「適合」+20 が付く水増し
  - 掲載開始日が日付として読めない → 継続週数が出ず最大20点の取り逃し
  - 推定人数が数値でない → 境界判定が「要確認」に落ちる

使い方:
    python3 tools/observation_ledger_score.py tools/observation_ledger_template.tsv
    python3 tools/observation_ledger_score.py 収集.tsv --today 2026-08-08

入力は「貼り付けブロック順」の14列 TSV（1行目はヘッダー）。
列順は docs/observation_ledger_collection.md §6 の表と一致させてある。
"""

from __future__ import annotations

import argparse
import csv
import sys
import unicodedata
from datetime import date, datetime

# --- マスタ_選択肢と配点 の控え（正本はスプレッドシート側） -----------------

INDUSTRY_POINTS = {
    "珈琲屋・カフェ": 30,
    "美容室・サロン": 25,
    "小規模宿・ゲストハウス": 25,
    "学習塾": 20,
    "フィットネス・小規模ジム": 20,
    "整骨院・接骨院": 15,
    "歯科医院": 15,
    "クリニック": 15,
    "小規模介護・保育": 10,
    "飲食（カフェ以外）": 20,
    "その他": 5,
}
INDUSTRY_FALLBACK = 5  # VLOOKUP が外れたときに IFERROR が返す値

TRIGGER_POINTS = {
    "求人を出し続けている": 30,
    "急募・アットホームの文言": 20,
    "新店・増床・増員": 15,
    "代替わり・事業承継": 15,
    "なし・不明": 0,
}
TRIGGER_FALLBACK = 0

# 貼り付けブロック順（= 台帳の B,C,D,E,F,G,H,I,K,L,N,O,R,S）
COLUMNS = [
    "事業所名",
    "業種",
    "エリア",
    "責任者の呼び名",
    "推定人数",
    "単一拠点",
    "中間管理職",
    "時給・シフト中心",
    "トリガー",
    "掲載開始日",
    "観測メモ",
    "出所",
    "予定チャネル",
    "ステータス",
]

DATE_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日", "%y/%m/%d")


def pad(text: str, width: int) -> str:
    """全角を2桁で数えて左詰めする（「除外」と「A」で列がずれないように）。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(0, width - shown)


def parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_headcount(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def judge_boundary(headcount: int | None, single_site: str, middle_mgr: str) -> str:
    """J列: =IF(OR(G="いいえ",H="あり"),"対象外",
              IF(OR(F="",G="",H=""),"要確認",
              IF(AND(F>=5,F<=30),"適合","対象外")))

    「不明」は空文字ではないので「要確認」に落ちない、というシートの挙動を忠実に再現する。
    """
    if single_site == "いいえ" or middle_mgr == "あり":
        return "対象外"
    if headcount is None or single_site == "" or middle_mgr == "":
        return "要確認"
    if 5 <= headcount <= 30:
        return "適合"
    return "対象外"


def weeks_running(posted_on: date | None, today: date) -> int | None:
    """M列: =ROUNDDOWN((TODAY()-L)/7,0)"""
    if posted_on is None:
        return None
    return (today - posted_on).days // 7


def calc_score(industry: str, trigger: str, weeks: int | None, boundary: str, hourly: str) -> int:
    """P列の再現。境界が「対象外」なら 0。"""
    if boundary == "対象外":
        return 0
    score = INDUSTRY_POINTS.get(industry, INDUSTRY_FALLBACK)
    score += TRIGGER_POINTS.get(trigger, TRIGGER_FALLBACK)
    if weeks is not None:
        score += 20 if weeks >= 8 else (10 if weeks >= 4 else 0)
    score += 20 if boundary == "適合" else 10
    if hourly == "はい":
        score += 5
    return score


def calc_rank(boundary: str, score: int) -> str:
    """Q列の再現。"""
    if boundary == "対象外":
        return "除外"
    if score >= 70:
        return "A"
    if score >= 50:
        return "B"
    return "C"


def evaluate(rows: list[dict[str, str]], today: date) -> tuple[list[dict], list[str]]:
    results: list[dict] = []
    warnings: list[str] = []

    for i, row in enumerate(rows, start=1):
        name = row["事業所名"].strip()
        if not name:
            continue

        sheet_row = i + 3  # 台帳の1件目は4行目
        label = f"[{i:>2}行目 / シート{sheet_row}行] {name}"

        industry = row["業種"].strip()
        trigger = row["トリガー"].strip()
        single_site = row["単一拠点"].strip()
        middle_mgr = row["中間管理職"].strip()
        hourly = row["時給・シフト中心"].strip()

        if industry not in INDUSTRY_POINTS:
            loss = max(INDUSTRY_POINTS.values()) - INDUSTRY_FALLBACK
            warnings.append(
                f"{label} 業種『{industry or '(空欄)'}』がマスタ11種に無い"
                f" → {INDUSTRY_FALLBACK}点に落ちる（最大{loss}点の損）"
            )
        if trigger not in TRIGGER_POINTS:
            warnings.append(
                f"{label} トリガー『{trigger or '(空欄)'}』がマスタ5種に無い"
                f" → {TRIGGER_FALLBACK}点に落ちる（最大{max(TRIGGER_POINTS.values())}点の損）"
            )
        for col, value in (("単一拠点", single_site), ("中間管理職", middle_mgr)):
            if value == "不明":
                warnings.append(
                    f"{label} {col}が『不明』 → 空欄扱いにならず「適合」+20が付く。"
                    f"未確認なら空欄にする（→「要確認」+10）"
                )

        headcount = parse_headcount(row["推定人数"])
        if row["推定人数"].strip() and headcount is None:
            warnings.append(f"{label} 推定人数『{row['推定人数']}』が数値でない → 境界判定が「要確認」に落ちる")

        posted_on = parse_date(row["掲載開始日"])
        if row["掲載開始日"].strip() and posted_on is None:
            warnings.append(
                f"{label} 掲載開始日『{row['掲載開始日']}』が日付として読めない"
                f" → 継続週数が出ず最大20点の取り逃し（YYYY/MM/DD で入れる）"
            )

        boundary = judge_boundary(headcount, single_site, middle_mgr)
        weeks = weeks_running(posted_on, today)
        score = calc_score(industry, trigger, weeks, boundary, hourly)
        rank = calc_rank(boundary, score)

        results.append(
            {
                "no": i,
                "sheet_row": sheet_row,
                "name": name,
                "industry": industry,
                "boundary": boundary,
                "weeks": weeks,
                "score": score,
                "rank": rank,
                "status": row["ステータス"].strip(),
            }
        )

    return results, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="観測台帳の貼り付け前検算")
    parser.add_argument("tsv", help="貼り付けブロック順14列のTSV（1行目ヘッダー）")
    parser.add_argument("--today", help="基準日 YYYY-MM-DD（既定は今日）")
    args = parser.parse_args()

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()

    with open(args.tsv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            print(f"ヘッダーに不足があります: {', '.join(missing)}", file=sys.stderr)
            print(f"想定の列順: {' / '.join(COLUMNS)}", file=sys.stderr)
            return 1
        rows = [{c: (r.get(c) or "") for c in COLUMNS} for r in reader]

    results, warnings = evaluate(rows, today)

    if not results:
        print("事業所名が入っている行がありません。")
        return 0

    print(f"基準日: {today}　／　入力 {len(results)} 件\n")
    print(f"{'No':>3} {pad('シート行', 8)} {pad('ランク', 6)}{'点':>4}  {pad('境界', 8)}{pad('継続', 5)}  事業所名")
    print("-" * 78)
    for r in results:
        weeks = "—" if r["weeks"] is None else f"{r['weeks']}週"
        print(
            f"{r['no']:>3} {r['sheet_row']:>8} {pad(r['rank'], 6)}{r['score']:>4}  "
            f"{pad(r['boundary'], 8)}{pad(weeks, 5)}  {r['name']}"
        )

    ranks = {k: sum(1 for r in results if r["rank"] == k) for k in ("A", "B", "C", "除外")}
    scored = [r["score"] for r in results if r["rank"] != "除外"]
    avg = sum(scored) / len(scored) if scored else 0
    fit = sum(1 for r in results if r["boundary"] == "適合")
    a_untouched = sum(1 for r in results if r["rank"] == "A" and r["status"] == "未接触")

    print("\n■ サマリー")
    print(f"　A {ranks['A']} 件 / B {ranks['B']} 件 / C {ranks['C']} 件 / 除外 {ranks['除外']} 件")
    print(f"　適合 {fit} 件　平均スコア {avg:.1f} 点（除外を除く）")
    print(f"　A×未接触 {a_untouched} 件　← 今週の声かけ先はここから取る")

    if warnings:
        print(f"\n■ 警告 {len(warnings)} 件（シート側ではエラーが出ず、黙って減点されるものだけ）")
        for w in warnings:
            print(f"　- {w}")
    else:
        print("\n■ 警告なし。貼り付けて問題ありません。")

    print("\n貼り付けは4ブロックに分けて「値のみ貼り付け（Ctrl+Shift+V）」で:")
    print("　B4:I33 ← 1〜8列目 / K4:L33 ← 9〜10列目 / N4:O33 ← 11〜12列目 / R4:S33 ← 13〜14列目")
    return 0


if __name__ == "__main__":
    sys.exit(main())
