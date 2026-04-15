"""
note記事生成スクリプト
Claude APIを使って毎日1本のnote記事ドラフトをMarkdownで生成し、
output/notes/YYYY-MM-DD_{mode}.md に保存して Slack にGitHub URLを通知する

モード:
  free  (デフォルト) - 過去7日のThreads投稿を参考に1200〜1500字の無料note記事を生成
  paid               - strategy.jsonの5本柱から2500〜3500字の有料note記事を生成
"""

import os
import json
import datetime
import anthropic
from collections import defaultdict
from sheets import get_weekly_data, append_note_record
from notify_slack import notify_slack_note

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_PATH = os.path.join(SCRIPT_DIR, "../config/strategy.json")
NOTE_GUIDE_PATH = os.path.join(SCRIPT_DIR, "../config/note_writing_guide.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "../output/notes")

# 5テーマのローテーション（day_of_year % 5 で決定）
NOTE_THEMES = [
    ("行動設計", "判断の優先順位設計と意思決定の自動化により、残業ゼロで成果を出す行動プロトコル"),
    ("環境設計", "Slack・通知・会議構造を最適化して集中環境を構築し、深い仕事を守る仕組み"),
    ("回復設計", "睡眠・休息を構造化してパフォーマンスを持続させる、脳科学に基づくリカバリー設計"),
    ("判断疲れの解消", "判断コストを定量化して削減することで、金曜午後まで認知負荷を維持する方法"),
    ("キャリア設計", "珈琲屋→エンジニア→コーチの異色経験から生まれた、才能不要の働き方設計メソッド全体像"),
]


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_writing_guide() -> dict:
    with open(NOTE_GUIDE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def format_writing_guide(guide: dict) -> str:
    """note執筆ガイドをプロンプト注入用テキストに変換"""
    lines = []

    # タイトル法則
    lines.append("【タイトルの法則】")
    for p in guide["title_rules"]["patterns"]:
        lines.append(f"- {p['type']}：{p['example']}（{p['note']}）")
    lines.append(f"避けること：{' / '.join(guide['title_rules']['avoid'])}")

    # 冒頭フック
    lines.append("\n【冒頭フックの型（最初の100字以内）】")
    for p in guide["opening_hook_rules"]["patterns"]:
        lines.append(f"- {p['type']}：{p['example']}")
    lines.append("ルール：" + " / ".join(guide["opening_hook_rules"]["rules"]))

    # 課題提示
    lines.append("\n【課題提示の型】")
    for p in guide["problem_presentation_rules"]["patterns"]:
        lines.append(f"- {p['type']}：{p['point']}")
        lines.append(f"  例：{p['example']}")

    # 解決法提示
    lines.append("\n【解決法提示の型】")
    for p in guide["solution_presentation_rules"]["patterns"]:
        lines.append(f"- {p['type']}：{p['point']}")
        lines.append(f"  例：{p['example']}")
    lines.append(f"避けること：{' / '.join(guide['solution_presentation_rules']['avoid'])}")

    # 構成テンプレート
    tmpl = guide["engagement_principles"]["structure_template"]
    lines.append(f"\n【推奨構成テンプレート】")
    for s in tmpl["sections"]:
        lines.append(f"  {s}")

    # いいね原則
    lines.append("\n【いいねを増やす5原則】")
    for p in guide["engagement_principles"]["principles"]:
        lines.append(f"- {p['name']}：{p['detail']}")

    return "\n".join(lines)


# Threads post_type → note combination index のマッピング
# 「最も反応が高かった投稿の種類」からnote記事の方向性を決定する
_POST_TYPE_TO_COMBINATION_INDEX = {
    "structure":  1,  # 体系化系  → 信頼構築（数字×根拠）
    "opinion":    4,  # 業界考察系 → 知的好奇心（逆説×設計図）
    "personal":   0,  # 自己開示系 → 共感最大化（失敗談×Before/After）
    "permission": 0,  # 許可系    → 共感最大化（感情共鳴）
    "dialogue":   3,  # 対話系    → ファン化（場面描写×プロトコル）
}


def annotate_posts_with_metrics(posts: list[dict], metrics: list[dict]) -> list[dict]:
    """投稿リストにメトリクス情報を付加してエンゲージメント率の高い順にソート"""
    metrics_map = {str(m.get("post_id", "")): m for m in metrics}
    annotated = []
    for post in posts:
        m = metrics_map.get(str(post.get("post_id", "")), {})
        annotated.append({
            **post,
            "_engagement_rate": float(m.get("engagement_rate", 0) or 0),
            "_likes": int(m.get("likes", 0) or 0),
            "_impressions": int(m.get("impressions", 0) or 0),
        })
    return sorted(annotated, key=lambda p: p["_engagement_rate"], reverse=True)


def determine_theme_and_combination(
    guide: dict,
    posts: list[dict] | None = None,
    metrics: list[dict] | None = None,
) -> tuple[str, str, dict, str]:
    """テーマと組み合わせパターンを決定する。
    メトリクスデータがあればエンゲージメント上位のpost_typeに基づき選択し、
    データ不足の場合はday_of_yearローテーションにフォールバックする。
    Returns: (theme_label, theme_desc, combination, selection_reason)
    """
    if posts and metrics:
        # post_type別の平均エンゲージメント率を計算
        type_scores: dict[str, list[float]] = defaultdict(list)
        metrics_map = {str(m.get("post_id", "")): m for m in metrics}
        for post in posts:
            pt = post.get("post_type", "")
            m = metrics_map.get(str(post.get("post_id", "")), {})
            rate = float(m.get("engagement_rate", 0) or 0)
            if pt and rate > 0:
                type_scores[pt].append(rate)

        if type_scores:
            avg_by_type = {pt: sum(v) / len(v) for pt, v in type_scores.items()}
            best_type = max(avg_by_type, key=lambda t: avg_by_type[t])
            best_avg = avg_by_type[best_type]
            combo_index = _POST_TYPE_TO_COMBINATION_INDEX.get(
                best_type, datetime.date.today().timetuple().tm_yday % len(NOTE_THEMES)
            )
            theme_label, theme_desc = NOTE_THEMES[combo_index]
            combination = guide["combination_patterns"]["patterns"][combo_index]
            reason = (
                f"エンゲージメント最高post_type: {best_type} "
                f"(avg {best_avg:.2%}, {len(type_scores[best_type])}件) "
                f"→ {combination['name']}パターンを選択"
            )
            return theme_label, theme_desc, combination, reason

    # フォールバック: day_of_yearローテーション
    day_of_year = datetime.date.today().timetuple().tm_yday
    theme_index = day_of_year % len(NOTE_THEMES)
    theme_label, theme_desc = NOTE_THEMES[theme_index]
    combination = guide["combination_patterns"]["patterns"][theme_index]
    reason = f"メトリクスデータなし → ローテーション({theme_index}番目)を使用: {theme_label}"
    return theme_label, theme_desc, combination, reason


def format_combination_instruction(combination: dict) -> str:
    """組み合わせパターンをプロンプト注入用テキストに変換"""
    inst = combination["instructions"]
    return f"""【今回の組み合わせパターン：{combination['name']}（目標：{combination['target_goal']}）】
必ず以下の4型を組み合わせて記事を書いてください。

- タイトル → {combination['title_type']}：{inst['title']}
- 冒頭フック（最初の100字）→ {combination['hook_type']}：{inst['hook']}
- 課題提示 → {combination['problem_type']}：{inst['problem']}
- 解決法 → {combination['solution_type']}：{inst['solution']}

この組み合わせの相乗効果：{combination['synergy']}"""


def format_selling_elements(guide: dict) -> str:
    """有料note 売れる要素チェックリストをプロンプト注入用テキストに変換"""
    elements = guide["paid_note_selling_elements"]["elements"]
    required = guide["paid_note_selling_elements"]["required_count"]
    lines = [f"（必ず{required}個以上含めること）"]
    for e in elements:
        lines.append(f"{e['id']}. 【{e['name']}】{e['description']}  ／ 配置推奨: {e['placement']}  ／ 例: {e['example']}")
    return "\n".join(lines)


def build_free_note_prompt(strategy: dict, recent_posts: list[dict], theme_label: str, theme_desc: str, writing_guide: str, combination: dict) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]

    # エンゲージメントデータを表示（あれば高い順、なければ通常表示）
    posts_text = "\n".join([
        f"- [{p.get('post_type','')} / エンゲージ:{p.get('_engagement_rate',0):.1%} / いいね:{p.get('_likes',0)}] {p.get('content','')}"
        if p.get("_engagement_rate", 0) > 0
        else f"- [{p.get('post_type','')}] {p.get('content','')}"
        for p in recent_posts[:15]
    ])

    return f"""以下の戦略・執筆ガイド・Threads投稿履歴を必ず参考に、ペルソナに向けた無料note記事を生成してください。

【ポジショニング】
- ポジション：{positioning["position"]}
- コンセプト：{positioning["concept"]}
- 差別化軸：{positioning["differentiation"]}
- ステートメント：{positioning["statement"]}

【ターゲット】
{persona["description"]}
悩み：{', '.join(persona["pain_points"])}

【今日のテーマ】
{theme_label}：{theme_desc}

{format_combination_instruction(combination)}

【過去7日のThreads投稿（参考・発展のベースにする）】
{posts_text if posts_text else "（参考投稿なし）"}

【note執筆ガイド（型の詳細定義）】
{writing_guide}

【記事の目的】
- セールスファネルの入口として機能する（SNS → 無料note → 有料コンテンツ）
- ペルソナの悩みに共感し、考え方・手法の入口を示すことで「この人の有料コンテンツも読みたい」と思わせる
- Threads投稿の視点を深掘り・展開した内容にする（コピーではなく発展系）

【ルール】
- 文字数：1200〜1500字程度
- 見出しは ## で記述（Markdown形式）
- 専門的だが親しみやすいトーン。精神論・根性論ではなく設計・仕組みの視点
- CTAは「〜についてはこちらで詳しく書いています」などの自然な形にし、直接的な売り込みはしない
- クライアント例は「クライアントの方から」「よくある例として」の形式で
- 事実でない体験談はNG

以下の形式で出力してください（他の説明・前置き不要）：

【タイトル】
（ここにタイトル）

【本文】
（ここにMarkdown形式の本文）"""


def build_paid_note_prompt(strategy: dict, theme_label: str, theme_desc: str, writing_guide: str, combination: dict, guide: dict) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]
    selling_elements = format_selling_elements(guide)

    return f"""以下の戦略・執筆ガイドに基づいて、ペルソナ向けの有料note記事（¥1,980相当）を生成してください。

【ポジショニング】
- ポジション：{positioning["position"]}
- コンセプト：{positioning["concept"]}
- 商品タイトル：{positioning["product_title"]}
- 商品サブタイトル：{positioning["product_subtitle"]}
- ステートメント：{positioning["statement"]}

【ターゲット】
{persona["description"]}
悩み：{', '.join(persona["pain_points"])}

【テーマ】
{theme_label}：{theme_desc}

{format_combination_instruction(combination)}

【note執筆ガイド（型の詳細定義）】
{writing_guide}

【有料note 売れる要素チェックリスト】
{selling_elements}

【記事の目的】
- ¥1,980の有料noteとして十分な具体的価値を提供する
- 「読んだだけで行動が変わった」と感じさせる再現性の高い手法を提供
- 価値提供を通じて信頼を構築し、上位商材への橋渡しにする

【ルール】
- 文字数：2500〜3500字程度
- 見出しは ## / ### で記述（Markdown形式）
- 心理学・脳科学の根拠を最低1つ含める（要素4）
- 具体的な数字・事例を盛り込む（「〇分」「〇件削減」「〇週間で」など）（要素6）
- 「できる人vsできない人」型の対比フォーマットは使わない
- CTAは上位商材への低圧力な自然な誘導（要素12）

以下の形式で出力してください（他の説明・前置き不要）：

【タイトル】
（ここにタイトル）

【本文】
（ここにMarkdown形式の本文）

【売れる要素チェック】
（各要素について ✅ 含まれている / ❌ 含まれていない を記載。含まれていない場合は改善案を1行で添えること。）
例: ✅ 1.ターゲット明示: リード文「30代エンジニア・PM」に明記
例: ❌ 11.価格正当化: 未記載 → まとめ章に「3種のプロトコル＋チェックリスト」の記述を追加推奨"""


def parse_note(raw: str) -> dict:
    """生成テキストをタイトル・本文・売れる要素チェック（有料noteのみ）に分割"""
    title = ""
    body = ""
    checklist = ""

    if "【タイトル】" in raw and "【本文】" in raw:
        # 売れる要素チェックが含まれる場合（有料noteモード）
        if "【売れる要素チェック】" in raw:
            parts_check = raw.split("【売れる要素チェック】", 1)
            checklist = parts_check[1].strip()
            raw = parts_check[0]

        parts = raw.split("【本文】", 1)
        title = parts[0].replace("【タイトル】", "").strip()
        body = parts[1].strip()
    else:
        body = raw

    return {"title": title, "body": body, "checklist": checklist}


def save_note(title: str, body: str, mode: str, date_str: str, checklist: str = "") -> str:
    """Markdownファイルとして保存し、ファイルパスを返す。
    有料noteの場合は売れる要素チェックをファイル末尾に付記する。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{date_str}_{mode}.md"
    filepath = os.path.join(OUTPUT_DIR, filename)

    content = f"# {title}\n\n{body}" if title else body
    if checklist:
        content += f"\n\n---\n\n## 売れる要素チェック（生成時の自己評価）\n\n{checklist}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


def main():
    mode = os.environ.get("MODE", "free").lower()
    strategy = load_strategy()
    guide = load_writing_guide()
    writing_guide = format_writing_guide(guide)

    # 過去7日のThreads投稿＋メトリクスを取得（両モード共通）
    # - freeモード: エンゲージメント上位のpost_typeからnoteテーマを決定 + 参照記事として使用
    # - paidモード: エンゲージメント上位のpost_typeからnoteテーマのみ決定
    data = get_weekly_data(days=7)
    recent_posts_raw = data.get("posts", [])
    recent_metrics = data.get("metrics", [])

    # エンゲージメント情報を付加してソート
    recent_posts = annotate_posts_with_metrics(recent_posts_raw, recent_metrics)
    print(f"[generate_note] 過去7日Threads投稿: {len(recent_posts)}件 / メトリクスあり: {len(recent_metrics)}件")

    # エンゲージメントデータからテーマ・組み合わせを決定
    theme_label, theme_desc, combination, selection_reason = determine_theme_and_combination(
        guide, recent_posts_raw, recent_metrics
    )
    print(f"[generate_note] テーマ選択: {selection_reason}")

    ref_post_ids = ",".join(
        str(p.get("post_id", "")) for p in recent_posts_raw if p.get("post_id")
    )

    # Claude API で記事生成
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if mode == "free":
        prompt = build_free_note_prompt(strategy, recent_posts, theme_label, theme_desc, writing_guide, combination)
        max_tokens = 2500
    else:
        prompt = build_paid_note_prompt(strategy, theme_label, theme_desc, writing_guide, combination, guide)
        max_tokens = 5500  # チェックリスト分を追加

    print(f"[generate_note] モード: {mode} / テーマ: {theme_label} / パターン: {combination['name']} / Claude API 呼び出し中...")
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    result = parse_note(raw)

    title = result["title"]
    body = result["body"]
    checklist = result.get("checklist", "")
    print(f"[generate_note] 生成完了: {title}")
    if checklist:
        passed = checklist.count("✅")
        failed = checklist.count("❌")
        print(f"[generate_note] 売れる要素チェック: ✅{passed}個 / ❌{failed}個")

    # Markdownファイルとして保存
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")
    filepath = save_note(title, body, mode, date_str, checklist)

    rel_path = f"output/notes/{date_str}_{mode}.md"
    repo = os.environ.get("GITHUB_REPOSITORY", "yuki-b4/sns-automation")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    github_url = f"https://github.com/{repo}/blob/{branch}/{rel_path}"

    print(f"[generate_note] 保存先: {filepath}")
    print(f"[generate_note] GitHub URL: {github_url}")

    # Slack通知（タイトル + GitHub URL のみ、本文は含めない）
    notify_slack_note(title, mode, github_url)

    # Google Sheetsに記録（組み合わせパターン情報も含む）
    append_note_record({
        "type": mode,
        "title": title,
        "price": 0 if mode == "free" else 1980,
        "file_path": rel_path,
        "generated_at": now_jst.isoformat(),
        "status": "draft",
        "combination_pattern": combination["name"],
        "title_type": combination["title_type"],
        "hook_type": combination["hook_type"],
        "problem_type": combination["problem_type"],
        "solution_type": combination["solution_type"],
        "ref_threads_post_ids": ref_post_ids,
    })

    print("[generate_note] 完了")


if __name__ == "__main__":
    main()
