"""
投稿アイデア生成スクリプト
Claude APIを使って当日のThreads投稿アイデアを3案提案し、
output/ideas/YYYY-MM-DD.md に書き出して Slack に GitHub URL を通知する。

本文の生成・Threadsへの自動投稿は行わない（ネタ出し専用）。
提案されたテーマから1案を選び、本文は運用者が書いて手動投稿する運用を前提とする。
本文執筆時のルールは generate_post.py:build_prompt の「共通ルール」／型カタログを参照すること。
"""

import os
import re
import glob
import json
import datetime
import anthropic

from preflight import run_all as preflight_check
from notify_slack import notify_slack_post_ideas, notify_slack_post_ideas_failure
from sheets import get_recent_posts_content
from token_cost import log_token_cost

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_PATH = os.path.join(SCRIPT_DIR, "../config/strategy.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "../output/ideas")

JST = datetime.timezone(datetime.timedelta(hours=9))

IDEA_COUNT = 3
REASON_MAX_LEN = 150
RECENT_IDEA_DAYS = 30   # 過去アイデア（output/ideas/*.md）を遡る日数
RECENT_POST_DAYS = 14   # 投稿DBの実投稿を遡る日数

# save_ideas_md が書き出す見出し（`## 提案1: テーマラベル`）から
# テーマラベルを読み戻すための正規表現。書式を変えるときは両方を揃えること。
_IDEA_HEADING_RE = re.compile(r"^##\s*提案\d+[:：]\s*(.+?)\s*$")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def determine_post_types(strategy: dict, count: int = IDEA_COUNT) -> list[str]:
    """当日提案する count 件分の投稿タイプを post_rotation から順に取り出す。

    generate_post.py:determine_post_type と同じ「通日でローテーションを進める」考え方だが、
    1日1回まとめて count 件取り出すため POST_SLOT の概念はない。日付はファイル名と
    揃えるためJST基準で数える。rotation長20・count3は互いに素なので全要素を均等に消化する。
    """
    rotation = strategy["post_rotation"]
    day_of_year = datetime.datetime.now(JST).date().timetuple().tm_yday
    base = (day_of_year - 1) * count
    return [rotation[(base + i) % len(rotation)] for i in range(count)]


def load_recent_idea_labels(days: int = RECENT_IDEA_DAYS) -> list[dict]:
    """過去に提案したテーマラベルを output/ideas/*.md から読み出す。

    投稿を止めた後は投稿DBに新規行が積まれないため、テーマ重複回避の主軸はこちら。
    ファイル名（YYYY-MM-DD.md）の日付で直近N日に絞る。
    """
    if not os.path.isdir(OUTPUT_DIR):
        return []

    cutoff = datetime.datetime.now(JST).date() - datetime.timedelta(days=days)
    results: list[dict] = []
    for path in sorted(glob.glob(os.path.join(OUTPUT_DIR, "*.md"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            file_date = datetime.date.fromisoformat(stem)
        except ValueError:
            continue  # 日付以外のファイル名は対象外
        if file_date < cutoff:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            print(f"[generate_post_ideas] アイデアファイル読み込み失敗（スキップ）: {path}: {e}", flush=True)
            continue
        for line in content.splitlines():
            m = _IDEA_HEADING_RE.match(line)
            if m:
                results.append({"date": stem, "theme_label": m.group(1)})
    return results


def load_recent_posts(days: int = RECENT_POST_DAYS) -> list[dict]:
    """投稿DBから直近N日のルート投稿を取得する。
    ネットワーク失敗時は空リストを返して生成自体はブロックしない。"""
    try:
        return get_recent_posts_content(days=days)
    except Exception as e:
        print(f"[generate_post_ideas] Sheetsからの投稿履歴取得に失敗: {e}", flush=True)
        return []


def build_avoid_section(recent_ideas: list[dict], recent_posts: list[dict]) -> str:
    """過去アイデアのテーマラベルと直近実投稿の冒頭を、重複回避用の入力に整形する。"""
    lines: list[str] = []
    seen: set[str] = set()
    for r in recent_ideas:
        label = (r.get("theme_label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        lines.append(f"- {r.get('date', '')}｜{label}")
    for p in recent_posts[-20:]:
        snippet = (p.get("content") or "").replace("\n", " ")[:50]
        if not snippet:
            continue
        lines.append(f"- {(p.get('posted_at') or '')[:10]}｜（実投稿）{snippet}")
    if not lines:
        return "（過去アイデア・投稿の履歴なし）"
    return "\n".join(lines)


class IdeaGenerationError(RuntimeError):
    """アイデア提案生成の致命的失敗。main() で Slack 通知＋停止する。"""


def build_prompt(strategy: dict, post_types: list[str], avoid_section: str) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]
    funnel = strategy["funnel"]
    midend = positioning["midend_product"]
    backend = positioning["backend_product"]

    type_lines = []
    for i, t in enumerate(post_types, start=1):
        info = strategy["post_types"][t]
        type_lines.append(
            f"提案{i}: {info['label']}（{t}）／ファネル段階：{info['funnel_stage']}\n"
            f"  {info['description']}"
        )
    types_section = "\n".join(type_lines)

    example_items = ",\n".join(
        f'    {{ "post_type": "{t}", "theme_label": "...", "angle": "...", '
        f'"hook_candidate": "...", "reason": "..." }}'
        for t in post_types
    )

    return f"""あなたはSNSコンテンツの企画者です。
以下の戦略に基づいて、本日のThreads投稿のアイデアを{IDEA_COUNT}案提案してください。
本文は書かず、「どのテーマを・どの角度で・どんな入り口から書くか」の設計だけを出します。

【発信者】
- 立ち位置：{positioning["speaker"]}
- credibility（一次経験ソース）：
{chr(10).join(f"  - {c}" for c in positioning["credibility"])}
- 差別化軸（曲げない信念＋それを届けるメソッド）：{positioning["differentiation"]}

【読者の到達点（ToBe）】{positioning["tobe"]}
【ToBeを阻む構造】{positioning["tobe_barrier"]}

【商品ラダー＋ファネル】
- ミドルエンド：{midend["title"]}（¥{midend["price_min"]}〜{midend["price_max"]}）／{funnel["midend_role"]}
- バックエンド：{backend["title"]}（¥{backend["price"]}）／{funnel["backend_path"]}
- SNS担当範囲：{funnel["sns_role"]}

【ターゲット】
{persona["description"]}
悩み：
{chr(10).join(f"- {p}" for p in persona["pain_points"])}

【今回提案する投稿タイプ（この順番・この割り当てを守る）】
{types_section}

【過去に提案したテーマ・直近の実投稿（意味的に被らせない）】
{avoid_section}

【提案ルール】
- 各案は割り当てられた投稿タイプの狙いに沿わせ、ペルソナの悩みのどれかを中心に据える
- {IDEA_COUNT}案で扱う悩み・場面を分散させる。同じ投稿タイプが複数割り当てられた場合は、切り口を明確に変える
- 各案は【発信者】差別化軸の“曲げない信念”のいずれかに必ず根ざす。中立的な情報提供・一般論で完結する切り口（発信者でなくても・AIでも量産できる切り口）は採らない
- 上記の過去テーマ・直近投稿と意味的に被らない（同じ単語の言い換えだけの近接テーマは避ける）
- 抽象的な大テーマ（例:「夫婦関係について」）ではなく、投稿1本で扱える具体的な切り口にする
- theme_label は8〜18字。何の話かが一目で分かる名詞句にする
- angle は80字以内。「どの角度から切り込み、読者に何を発見させるか」を1〜2文で書く（本文は書かない）
- hook_candidate は20〜40字。ルート1行目に置く入り口の案（そのまま使える精度を目指すが、運用者が書き換える前提の素材でよい）
- reason は{REASON_MAX_LEN}字以内（厳守・超過禁止）。「なぜ今のペルソナに刺さるか／なぜ他の切り口より優先したいか」を1〜2文で書く

【hook_candidate の制約】
- 語り手は既婚男性、一人称の代名詞は「僕」。読者＝既婚女性の感情は「あなた」で描く
- 否定型の入り（「〇〇な人は△△だと思ってる。違う。」）や抽象主語で始めず、具体的な場面・体験から入る
- 決めフレーズ・標語化した断定（「テクニックじゃなくマインド」等）で締めない
- 研究・統計の引用や著者名・年・媒体は入れない（概念名の使用は可）
- 読者を優劣・属性で分類する対比（『〇〇な人 vs 〇〇な人』等）は使わない
- 「思考・行動のクセ」は無修飾で使わず『あなたの／自分の』を冠する
- 「——」（emダッシュ/二倍ダッシュ）は使わない

出力形式（他の説明・前置き不要・JSONのみ）:
{{
  "ideas": [
{example_items}
  ]
}}"""


def propose_ideas(
    client: anthropic.Anthropic,
    strategy: dict,
    post_types: list[str],
    avoid_section: str,
) -> list[dict]:
    """本日の投稿アイデアを Claude API に提案させる。Claude 呼び出しは1回のみ。
    生成失敗時は IdeaGenerationError を送出する（呼び出し側で Slack 通知＋停止）。"""
    prompt = build_prompt(strategy, post_types, avoid_section)

    try:
        message = client.messages.create(
            model="claude-opus-5",
            max_tokens=2000,
            thinking={"type": "disabled"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise IdeaGenerationError(f"Claude API呼び出しに失敗: {type(e).__name__}: {e}") from e

    log_token_cost("claude-opus-5", message.usage, "generate_post_ideas")
    # thinking ON のとき content 先頭が thinking ブロックになり得るため text ブロックを明示抽出
    raw = next((b.text for b in message.content if b.type == "text"), "").strip()
    # ```json などのフェンス除去
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise IdeaGenerationError(
            f"JSONパース失敗: {e}\n生成テキスト先頭500字: {raw[:500]}"
        ) from e

    ideas = data.get("ideas")
    if not isinstance(ideas, list) or len(ideas) != len(post_types):
        raise IdeaGenerationError(
            f"ideas が配列で長さ{len(post_types)}ではない。生成データ: {data}"
        )

    cleaned: list[dict] = []
    for i, (item, assigned_type) in enumerate(zip(ideas, post_types)):
        if not isinstance(item, dict):
            raise IdeaGenerationError(f"ideas[{i}] が辞書ではない: {item}")
        theme_label = (item.get("theme_label") or "").strip()
        angle = (item.get("angle") or "").strip()
        hook = (item.get("hook_candidate") or "").strip()
        reason = (item.get("reason") or "").strip()
        if not (theme_label and angle and hook and reason):
            raise IdeaGenerationError(
                f"ideas[{i}] に theme_label / angle / hook_candidate / reason のいずれかが欠落: {item}"
            )
        returned_type = (item.get("post_type") or "").strip()
        if returned_type != assigned_type:
            # 投稿タイプはローテーションからPython側で決める値が正。取り違えても致命傷にしない。
            print(
                f"[generate_post_ideas] 警告: ideas[{i}] の post_type が割り当てと不一致"
                f"（返却={returned_type or '空'} / 割り当て={assigned_type}）。割り当て側を採用します。",
                flush=True,
            )
        if len(reason) > REASON_MAX_LEN:
            print(
                f"[generate_post_ideas] 警告: ideas[{i}] の reason が {len(reason)} 字"
                f"（>{REASON_MAX_LEN}）。切り詰めずそのまま出力します。",
                flush=True,
            )
        cleaned.append({
            "post_type": assigned_type,
            "theme_label": theme_label,
            "angle": angle,
            "hook_candidate": hook,
            "reason": reason,
        })
    return cleaned


def save_ideas_md(ideas: list[dict], strategy: dict, date_str: str) -> str:
    """アイデア提案をMarkdownとして保存し、ファイルパスを返す。
    見出し行は load_recent_idea_labels が読み戻すので書式を変えるときは正規表現も揃えること。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, f"{date_str}.md")

    lines: list[str] = [f"# {date_str} Threads投稿アイデア（{len(ideas)}案）", ""]
    for i, idea in enumerate(ideas, start=1):
        post_type = idea["post_type"]
        label = strategy["post_types"].get(post_type, {}).get("label", post_type)
        reason = idea["reason"]
        lines.append(f"## 提案{i}: {idea['theme_label']}")
        lines.append("")
        lines.append(f"- **投稿タイプ**: {label}（{post_type}）")
        lines.append(f"- **切り口**: {idea['angle']}")
        lines.append(f"- **フック候補**: {idea['hook_candidate']}")
        lines.append(f"- **狙い・根拠（{len(reason)}文字）**: {reason}")
        lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return filepath


def main():
    # Claude API呼び出し前に外部サービスの接続確認。
    # Threadsへは投稿しないため threads チェックは外す（トークン失効で止めない）。
    preflight_check(checks=("slack", "sheets"))

    strategy = load_strategy()
    post_types = determine_post_types(strategy)
    print(f"[generate_post_ideas] 本日の投稿タイプ割り当て: {post_types}")

    recent_ideas = load_recent_idea_labels()
    recent_posts = load_recent_posts()
    print(
        f"[generate_post_ideas] 重複回避入力: 過去アイデア {len(recent_ideas)}件 / "
        f"直近投稿 {len(recent_posts)}件"
    )
    avoid_section = build_avoid_section(recent_ideas, recent_posts)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    try:
        ideas = propose_ideas(client, strategy, post_types, avoid_section)
    except IdeaGenerationError as e:
        print(f"[generate_post_ideas] アイデア提案生成に失敗: {e}", flush=True)
        notify_slack_post_ideas_failure(stage=f"{IDEA_COUNT}案提案生成", error=str(e))
        raise SystemExit(1)

    print(f"[generate_post_ideas] {len(ideas)}案生成完了: {[i['theme_label'] for i in ideas]}")

    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    filepath = save_ideas_md(ideas, strategy, today_str)
    rel_path = f"output/ideas/{today_str}.md"
    repo = os.environ.get("GITHUB_REPOSITORY", "yuki-b4/sns-automation")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    github_url = f"https://github.com/{repo}/blob/{branch}/{rel_path}"

    print(f"[generate_post_ideas] 保存先: {filepath}")
    print(f"[generate_post_ideas] GitHub URL: {github_url}")

    notify_slack_post_ideas(ideas, today_str, github_url)

    print("[generate_post_ideas] 完了（投稿アイデア提案）")


if __name__ == "__main__":
    main()
