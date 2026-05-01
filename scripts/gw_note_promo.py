"""
GW期間限定（2026-05-01〜2026-05-06）の有料note拡散用Threads投稿スクリプト。

- 対象note: config/gw_note_promo_prompts.json の note_path（既定 output/notes/2026-04-21_paid.md）
- 1日2投稿（GW_SLOT=0/1）、本文単独投稿（補足リプライなし）
- 投稿ごとに事前定義した <500字 のプロンプトを Claude に渡し、本文1つを生成させる
- プロンプトには「記事の拡散戦略を内部分析→その分析を踏まえて本文生成」の手順を必ず含む
- ネタバレ防止：記事本文の表現はそのまま使わず、ペイン／ベネフィットに翻訳して利用
- 直接URL投稿は禁止。文末で「プロフィール固定note」へ自然誘導する文言で締める
- 期間外、または該当(date,slot)のプロンプトが存在しない場合はSlack通知なしでスキップ終了

通常の投稿フローに倣い preflight → 生成 → Threads投稿 → Slack通知 → 投稿DB記録 の順で処理する。
"""

import os
import json
import datetime
import anthropic

from preflight import run_all as preflight_check
from token_cost import log_token_cost
from post_threads import post_to_threads
from notify_slack import notify_slack
from sheets import append_post_record


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
PROMPT_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "gw_note_promo_prompts.json")
POST_TYPE = "gw_note_promo"


def _today_jst() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def _load_config() -> dict:
    with open(PROMPT_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _in_window(date_str: str, window: dict) -> bool:
    return window["start"] <= date_str <= window["end"]


def _load_note_markdown(note_path: str) -> str:
    abs_path = os.path.join(REPO_ROOT, note_path)
    with open(abs_path, "r", encoding="utf-8") as f:
        return f.read()


def _parse_post(raw: str) -> str:
    """Claude出力から【投稿】ブロックの本文だけを抽出。"""
    text = raw.strip()
    if "【投稿】" in text:
        text = text.split("【投稿】", 1)[1].strip()
    # 万一以降に他のヘッダが続いた場合は最初のブロックだけ取る
    for marker in ("【参考", "【本文】", "【補足"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def _generate_post(prompt_template: str, note_markdown: str) -> str:
    prompt = prompt_template.replace("{NOTE_MARKDOWN}", note_markdown)
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    log_token_cost("claude-opus-4-6", message.usage, "gw_note_promo")
    return _parse_post(message.content[0].text)


def main() -> None:
    slot_raw = os.environ.get("GW_SLOT", "")
    if slot_raw not in ("0", "1"):
        raise SystemExit(f"[gw_note_promo] GW_SLOT は '0' または '1' を指定してください（受領: {slot_raw!r}）")
    slot = int(slot_raw)

    now = _today_jst()
    date_str = now.date().isoformat()

    config = _load_config()
    window = config["campaign_window"]

    # 1) GW期間外はSlack通知なしで静かに終了（cron は毎日起動するが配信は5/1〜5/6のみ）
    if not _in_window(date_str, window):
        print(f"[gw_note_promo] {date_str} はGW配信期間外（{window['start']}〜{window['end']}）。スキップします。")
        return

    # 2) 該当 (date, slot) のプロンプトが無ければスキップ
    prompts = config["prompts_by_date"].get(date_str)
    if not prompts or slot >= len(prompts):
        print(f"[gw_note_promo] {date_str} slot{slot} のプロンプトが未定義のためスキップします。")
        return
    prompt_template = prompts[slot]

    # 3) 対象note原稿の読み込み（無ければ即終了：拡散対象が無いと本文生成できない）
    note_path = config["note_path"]
    try:
        note_markdown = _load_note_markdown(note_path)
    except FileNotFoundError:
        raise SystemExit(f"[gw_note_promo] 対象note原稿が見つかりません: {note_path}")

    # 4) Claude API 呼び出し前に外部サービス疎通確認（generate_post と同じ契約）
    preflight_check()

    # 5) Threads投稿本文を生成
    content = _generate_post(prompt_template, note_markdown)
    if not content:
        raise SystemExit("[gw_note_promo] Claudeの出力パースに失敗しました（本文が空）")

    print(f"[gw_note_promo] 投稿対象日付: {date_str} (JST) / GW_SLOT={slot}")
    print(f"[Threads本文]\n{content}\n")

    # 6) Threadsへ単独投稿（プロフィール固定noteへの誘導なので補足リプライなし）
    threads_id = post_to_threads(content)

    # 7) Slack通知（完了系・メンションなし）
    title = f"GW note拡散投稿完了 ({date_str} slot{slot})"
    notify_slack(content, POST_TYPE, title=title)

    # 8) 投稿DB記録（ルートのみ。parent_post_id 空欄）
    if threads_id:
        append_post_record({
            "post_id": threads_id,
            "platform": "threads",
            "post_type": POST_TYPE,
            "content": content,
            "posted_at": now.isoformat(),
            "week_number": now.isocalendar()[1],
            "parent_post_id": "",
        })

    print("[gw_note_promo] 投稿・通知・DB記録が完了しました")


if __name__ == "__main__":
    main()
