"""
note記事テーマ提案スクリプト
Claude APIを使って当日のnote記事テーマを3つ提案し、
output/notes/YYYY-MM-DD_{mode}.md に書き出して Slack に GitHub URL を通知する。
本文の自動生成は行わず、ネタ出し（テーマ／タイトル候補／狙い）のみを担当する。

モード:
  free  (デフォルト) - 無料note向けの3テーマ提案
  paid               - 有料note（¥1,980）向けの3テーマ提案
"""

import os
import re
import json
import datetime
import anthropic
from sheets import append_note_record, get_note_records
from notify_slack import notify_slack_note, notify_slack_note_generation_failure
from token_cost import log_token_cost

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_PATH = os.path.join(SCRIPT_DIR, "../config/strategy.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "../output/notes")

REASON_MAX_LEN = 200

_BRAIN_LABEL_JA = {
    "reptilian": "爬虫類脳",
    "mammalian": "哺乳類脳",
    "both": "両方",
}


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_past_note_titles_from_sheets(weeks: int = 4, limit: int = 20) -> list[dict]:
    """Sheetsのnote投稿DBから直近N週・最大limit件のレコードを返す。
    過去テーマ（theme_label）の重複回避プロンプトに使用する。
    ネットワーク失敗時は空リストを返して生成自体はブロックしない。"""
    try:
        records = get_note_records(weeks=weeks)
    except Exception as e:
        print(f"[generate_note] Sheetsからのnote履歴取得に失敗: {e}", flush=True)
        return []
    records = sorted(records, key=lambda r: r.get("generated_at", ""), reverse=True)
    return records[:limit]


def build_past_themes_avoid_section(past_records: list[dict]) -> str:
    """Sheets履歴から直近のテーマ（theme_label）を抽出し、テーマ生成プロンプト用に整形する。
    過去テーマと被らないテーマ案を Claude に提案させるための入力。"""
    if not past_records:
        return "（過去テーマ履歴なし）"
    seen: set[str] = set()
    lines: list[str] = []
    for r in past_records:
        label = (r.get("theme_label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        date = (str(r.get("generated_at", "")) or "")[:10]
        lines.append(f"- {date}｜{label}")
    if not lines:
        return "（過去テーマ履歴なし）"
    return "\n".join(lines)


class ThemeGenerationError(RuntimeError):
    """テーマ提案生成の致命的失敗。main() で Slack 通知＋停止する。"""


def propose_three_themes(
    client: anthropic.Anthropic,
    strategy: dict,
    mode: str,
    past_records: list[dict],
) -> list[dict]:
    """その日のnote記事テーマを3つ Claude API に提案させる。
    各テーマに theme_label / title_candidate / reason / target_brain を含める。
    生成失敗時は ThemeGenerationError を送出する（呼び出し側で Slack 通知＋停止）。"""
    positioning = strategy["positioning"]
    persona = strategy["persona"]
    backend = positioning.get("backend_product", {})
    midend = positioning.get("midend_product", {})
    funnel = strategy.get("funnel", {})
    past_themes_text = build_past_themes_avoid_section(past_records)

    if mode == "free":
        mode_directive = (
            f"無料記事のテーマ提案。ペルソナの悩みに直接刺さり、ミドルエンド商品『"
            f"{midend.get('title', '')}』への導線として機能する切り口を選ぶ。"
        )
    else:
        mode_directive = (
            f"有料記事（ミドルエンド商品『{midend.get('title', '')}』、"
            f"¥{midend.get('price_min', 500)}〜{midend.get('price_max', 4980)}）のテーマ提案。"
            f"ファネル上の役割は「読者のバックエンド商品『{backend.get('title', '')}』"
            "の必要性に関する理解を深め、公式LINE登録 または "
            "バックエンド商品への興味付けにつなぐ」こと。"
            "有料note→有料noteのファネルは想定していないため、"
            "reason は「このテーマがバックエンド商品の必要性に関する理解を"
            "どう深め、次の一歩（公式LINE登録／バックエンド興味付け）に"
            "どう接続するか」を中心に書く。"
        )

    prompt = f"""あなたは note 記事の編集者です。以下の発信者情報・ペルソナから逆算し、本日生成する note 記事のテーマを 3 つ提案してください。各テーマは「ペルソナの爬虫類脳または哺乳類脳に直撃する」観点で設計し、なぜそのテーマを推すかを根拠つきで述べてください。

【立ち位置】{positioning["speaker"]}
【credibility（発信者の一次経験ソース）】
{chr(10).join(f"- {c}" for c in positioning["credibility"])}
【ToBe（読者の到達点）】{positioning["tobe"]}
【ToBeを阻む構造】{positioning["tobe_barrier"]}
【差別化メソッド】{positioning["differentiation"]}
【ミドルエンド商品】{midend.get("title", "")}（¥{midend.get("price_min", 500)}〜{midend.get("price_max", 4980)}）
ミドルエンドの役割：{funnel.get("midend_role", "")}
【バックエンド商品】{backend.get("title", "")}（¥{backend.get("price", 550000)}）
バックエンドへの導線：{funnel.get("backend_path", "")}

【ターゲット】{persona["description"]}
ペルソナの悩み:
{chr(10).join(f"- {p}" for p in persona["pain_points"])}

【モード別方針】
{mode_directive}

【脳の階層と刺激の定義】
- 爬虫類脳（reptilian）: 生存本能・損失回避・地位・縄張り・身体反応・即時性。
  刺さる要素 = 「このままだと失う／削られる」「危険・不可逆」「数値で迫る損失」「身体が壊れる」「奪われる縄張り」
- 哺乳類脳（mammalian）: 所属・愛着・承認・安心・社会的比較・関係性。
  刺さる要素 = 「親密な人との繋がり」「仲間の評価」「孤立への怖さ」「肯定されたい」「比較で生まれる焦り」

【過去noteで扱ったテーマ（重複させない）】
{past_themes_text}

【提案ルール】
- ペルソナの pain_point を中心に据え、上記 ToBe・ToBeを阻む構造・差別化メソッドに整合させる
- 3 テーマで target_brain が偏らないようにする（爬虫類脳寄り 2 + 哺乳類脳寄り 1、もしくは "both" を 1 つ含める等、刺激ルートを分散）
- 上記「過去noteで扱ったテーマ」と意味的に被らない（同じ単語の言い換えだけの近接テーマは避ける）
- 抽象的な大テーマ（例: 「人間関係について」「結婚について」）ではなく、記事 1 本で扱える具体的な切り口にする
- title_candidate は 18〜30 字、theme_label は 8〜18 字
- reason は 200 字以内（厳守・超過禁止）。「このテーマがなぜ今のペルソナに刺さるか／なぜ他の切り口より優先したいか」を 1〜2 文で書く
- paid モードでは reason に「有料note自体の購入動機」「追加の有料noteへの誘導」を書かない（有料note→有料noteのファネルは存在しないため）

出力形式（他の説明・前置き不要・JSON のみ）:
{{
  "themes": [
    {{ "theme_label": "...", "title_candidate": "...", "reason": "...", "target_brain": "reptilian" }},
    {{ "theme_label": "...", "title_candidate": "...", "reason": "...", "target_brain": "mammalian" }},
    {{ "theme_label": "...", "title_candidate": "...", "reason": "...", "target_brain": "both" }}
  ]
}}"""

    try:
        message = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise ThemeGenerationError(f"Claude API呼び出しに失敗: {type(e).__name__}: {e}") from e

    log_token_cost("claude-opus-4-7", message.usage, "generate_note_themes")
    raw = message.content[0].text.strip()
    # ```json などのフェンス除去
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ThemeGenerationError(
            f"JSONパース失敗: {e}\n生成テキスト先頭500字: {raw[:500]}"
        ) from e

    themes = data.get("themes")
    if not isinstance(themes, list) or len(themes) != 3:
        raise ThemeGenerationError(
            f"themes が配列で長さ3ではない。生成データ: {data}"
        )

    valid_brains = {"reptilian", "mammalian", "both"}
    cleaned: list[dict] = []
    for i, t in enumerate(themes):
        if not isinstance(t, dict):
            raise ThemeGenerationError(f"themes[{i}] が辞書ではない: {t}")
        label = (t.get("theme_label") or "").strip()
        title = (t.get("title_candidate") or "").strip()
        reason = (t.get("reason") or "").strip()
        brain = (t.get("target_brain") or "").strip()
        if not (label and title and reason):
            raise ThemeGenerationError(
                f"themes[{i}] に theme_label / title_candidate / reason のいずれかが欠落: {t}"
            )
        if brain not in valid_brains:
            raise ThemeGenerationError(
                f"themes[{i}] の target_brain が不正値（{brain}）。許容: {valid_brains}"
            )
        if len(reason) > REASON_MAX_LEN:
            print(
                f"[generate_note] 警告: themes[{i}] の reason が {len(reason)} 字（>{REASON_MAX_LEN}）"
                "。切り詰めずそのまま出力します。",
                flush=True,
            )
        cleaned.append({
            "theme_label": label,
            "title_candidate": title,
            "reason": reason,
            "target_brain": brain,
        })
    return cleaned


def save_themes_md(themes: list[dict], mode: str, date_str: str) -> str:
    """3テーマ提案をMarkdownとして保存し、ファイルパスを返す。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{date_str}_{mode}.md"
    filepath = os.path.join(OUTPUT_DIR, filename)

    mode_label = "無料note" if mode == "free" else "有料note"
    lines: list[str] = [f"# {date_str} note記事テーマ提案（{mode_label}）", ""]
    for i, t in enumerate(themes, start=1):
        brain_ja = _BRAIN_LABEL_JA.get(t["target_brain"], t["target_brain"])
        reason = t["reason"]
        lines.append(f"## 提案{i}: {t['theme_label']}")
        lines.append("")
        lines.append(f"- **タイトル候補**: {t['title_candidate']}")
        lines.append(f"- **想定刺激**: {brain_ja}")
        lines.append(f"- **狙い・根拠（{len(reason)}文字）**: {reason}")
        lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return filepath


def main():
    mode = os.environ.get("MODE", "free").lower()
    strategy = load_strategy()

    past_note_records = load_past_note_titles_from_sheets(weeks=4, limit=20)
    print(f"[generate_note] 過去note履歴: {len(past_note_records)}件 (テーマ重複回避用)")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    try:
        themes = propose_three_themes(client, strategy, mode, past_note_records)
    except ThemeGenerationError as e:
        print(f"[generate_note] 3テーマ提案生成に失敗: {e}", flush=True)
        notify_slack_note_generation_failure(
            stage="3テーマ提案生成",
            mode=mode,
            error=str(e),
        )
        raise SystemExit(1)

    print(f"[generate_note] 3テーマ生成完了: {[t['theme_label'] for t in themes]}")

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_str = now_jst.strftime("%Y-%m-%d")

    filepath = save_themes_md(themes, mode, today_str)
    rel_path = f"output/notes/{today_str}_{mode}.md"
    repo = os.environ.get("GITHUB_REPOSITORY", "yuki-b4/sns-automation")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    github_url = f"https://github.com/{repo}/blob/{branch}/{rel_path}"

    print(f"[generate_note] 保存先: {filepath}")
    print(f"[generate_note] GitHub URL: {github_url}")

    # Slack: 代表タイトル（先頭テーマ）+ GitHub URL
    notify_slack_note(themes[0]["title_candidate"], mode, github_url)

    # 3テーマを3行で note投稿DB に append（status='proposed'）
    generated_at_iso = now_jst.isoformat()
    price = 0 if mode == "free" else 1980
    for t in themes:
        append_note_record({
            "type": mode,
            "title": t["title_candidate"],
            "price": price,
            "file_path": rel_path,
            "generated_at": generated_at_iso,
            "status": "proposed",
            "theme_label": t["theme_label"],
            "theme_description": t["reason"],
        })

    print("[generate_note] 完了（3テーマ提案）")


if __name__ == "__main__":
    main()
