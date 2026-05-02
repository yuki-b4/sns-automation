"""
GW期間限定（2026-05-01〜2026-05-06）の有料note拡散用Threads投稿スクリプト。

- 対象note: config/gw_note_promo_brief.json に集約した拡散ブリーフ（元記事の全文ではない）
- 1日2投稿（朝7:00 / 夜20:30 JST）、本文単独投稿（補足リプライなし）
- 共有プロンプトテンプレート1本で運用。投稿ごとの角度設計は Claude に委ねる
- 重複回避：実行時に投稿DBから過去の gw_note_promo 投稿本文を読み込み、
  プロンプトに【既出の角度】として注入する
- ネタバレ防止：拡散ブリーフ自体が一字一句の引用ではない構造化サマリ。さらに
  プロンプト側でも「ネタバレ禁止ゾーン」の中身は具体的に書かない制約を明示
- 直接URL投稿は禁止。文末で「プロフィール固定note」へ自然誘導する文言で締める
- 期間外、または(date,slot)未設定の場合はSlack通知なしでスキップ終了

通常の投稿フローに倣い preflight → 生成 → Threads投稿 → Slack通知 → 投稿DB記録 の順。
"""

import os
import json
import datetime
import anthropic

from preflight import run_all as preflight_check
from token_cost import log_token_cost
from post_threads import post_to_threads
from notify_slack import notify_slack
from sheets import append_post_record, get_recent_posts_content


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
PROMPT_CONFIG_PATH = os.path.join(REPO_ROOT, "config", "gw_note_promo_prompts.json")
POST_TYPE = "gw_note_promo"
PAST_POSTS_LOOKBACK_DAYS = 14  # GW全期間（6日）を確実にカバーする日数


def _today_jst() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def _load_prompt_config() -> dict:
    with open(PROMPT_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_brief(brief_path: str) -> dict:
    abs_path = os.path.join(REPO_ROOT, brief_path)
    with open(abs_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _render_brief_markdown(brief: dict) -> str:
    """拡散ブリーフJSONをClaude注入用のMarkdownに整形する。"""
    out = []
    out.append(f"# {brief['title']}")
    out.append("")
    out.append(brief["one_liner"])
    out.append("")
    out.append("## ターゲットのペイン")
    out.extend(f"- {p}" for p in brief["target_persona_pains"])
    out.append("")
    out.append("## 約束されるベネフィット")
    out.extend(f"- {p}" for p in brief["promised_benefits"])
    out.append("")
    out.append("## フレームワーク・概念")
    out.extend(f"- **{f['name']}**：{f['essence']}" for f in brief["frameworks_concepts"])
    out.append("")
    out.append("## 構造的な転換点（読者が意外に感じるポイント）")
    out.extend(f"- {t}" for t in brief["structural_turns"])
    out.append("")
    out.append("## 根拠・素材")
    out.extend(f"- {e}" for e in brief["evidence"])
    out.append("")
    out.append("## ネタバレ禁止ゾーン（投稿に書いてはいけない範囲）")
    out.extend(f"- {s}" for s in brief["spoiler_no_go"])
    out.append("")
    out.append("## 候補となる切り口プール（参考。ここから選ぶ／変奏する／新規追加してOK）")
    out.extend(f"- {a}" for a in brief["candidate_angles_pool"])
    return "\n".join(out)


def _fetch_past_promo_posts() -> list[str]:
    """投稿DBから直近の gw_note_promo 投稿本文（ルートのみ）を古い順で返す。"""
    records = get_recent_posts_content(days=PAST_POSTS_LOOKBACK_DAYS)
    promo = [r for r in records if r.get("post_type") == POST_TYPE and r.get("content")]
    promo.sort(key=lambda r: r.get("posted_at", ""))
    return [r["content"] for r in promo]


def _format_past_posts(past_posts: list[str]) -> str:
    if not past_posts:
        return "なし"
    return "\n\n".join(f"({i+1}) {body}" for i, body in enumerate(past_posts))


def _in_window(date_str: str, window: dict) -> bool:
    return window["start"] <= date_str <= window["end"]


def _parse_post(raw: str) -> str:
    """Claude出力から【投稿】ブロックの本文だけを抽出。"""
    text = raw.strip()
    if "【投稿】" in text:
        text = text.split("【投稿】", 1)[1].strip()
    for marker in ("【参考", "【本文】", "【補足", "【既出"):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text


def _generate_post(prompt_template: str, brief_md: str, past_posts_block: str) -> str:
    prompt = prompt_template.replace("{NOTE_BRIEF}", brief_md).replace("{PAST_POSTS}", past_posts_block)
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

    config = _load_prompt_config()
    window = config["campaign_window"]

    # 1) GW期間外はSlack通知なしで静かに終了
    if not _in_window(date_str, window):
        print(f"[gw_note_promo] {date_str} はGW配信期間外（{window['start']}〜{window['end']}）。スキップします。")
        return

    # 2) Claude API 呼び出し前に外部サービス疎通確認（preflight契約）
    preflight_check()

    # 3) ブリーフ・過去投稿を取得しプロンプトを構築
    brief = _load_brief(config["brief_path"])
    brief_md = _render_brief_markdown(brief)
    past_posts = _fetch_past_promo_posts()
    past_posts_block = _format_past_posts(past_posts)

    print(f"[gw_note_promo] 日付: {date_str} (JST) / GW_SLOT={slot}")
    print(f"[gw_note_promo] 既出の角度数: {len(past_posts)}（直近{PAST_POSTS_LOOKBACK_DAYS}日のgw_note_promoルート投稿）")

    # 4) Threads投稿本文を生成
    content = _generate_post(config["prompt_template"], brief_md, past_posts_block)
    if not content:
        raise SystemExit("[gw_note_promo] Claudeの出力パースに失敗しました（本文が空）")

    print(f"[Threads本文]\n{content}\n")

    # 5) Threadsへ単独投稿（プロフィール固定noteへの誘導なので補足リプライなし）
    threads_id = post_to_threads(content)

    # 6) Slack通知（完了系・メンションなし）
    title = f"GW note拡散投稿完了 ({date_str} slot{slot})"
    notify_slack(content, POST_TYPE, title=title)

    # 7) 投稿DB記録（ルートのみ。parent_post_id 空欄）
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
