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


def determine_theme_and_combination(guide: dict) -> tuple[str, str, dict]:
    """テーマとその日の組み合わせパターンを返す（5日周期ローテーション）"""
    day_of_year = datetime.date.today().timetuple().tm_yday
    theme_index = day_of_year % len(NOTE_THEMES)
    theme_label, theme_desc = NOTE_THEMES[theme_index]
    combination = guide["combination_patterns"]["patterns"][theme_index]
    return theme_label, theme_desc, combination


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


def build_free_note_prompt(strategy: dict, recent_posts: list[dict], theme_label: str, theme_desc: str, writing_guide: str, combination: dict) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]

    posts_text = "\n".join([
        f"- [{p.get('post_type', '')}] {p.get('content', '')}"
        for p in recent_posts[:15]
    ])

    return f"""あなたはnoteのコンテンツライターです。
以下の戦略・執筆ガイド・Threads投稿履歴を参考に、ペルソナに向けた無料note記事を生成してください。

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


def build_paid_note_prompt(strategy: dict, theme_label: str, theme_desc: str, writing_guide: str, combination: dict) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]

    return f"""あなたはnoteのコンテンツライターです。
以下の戦略・執筆ガイドに基づいて、ペルソナ向けの有料note記事（¥1,980相当）を生成してください。

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

【記事の目的】
- ¥1,980の有料noteとして十分な具体的価値を提供する
- 「読んだだけで行動が変わった」と感じさせる再現性の高い手法を提供
- 価値提供を通じて信頼を構築し、上位商材への橋渡しにする

【ルール】
- 文字数：2500〜3500字程度
- 見出しは ## / ### で記述（Markdown形式）
- 心理学・脳科学の根拠を最低1つ含める
- 具体的な数字・事例を盛り込む（「〇分」「〇件削減」「〇週間で」など）
- 「できる人vsできない人」型の対比フォーマットは使わない
- CTAは無料コーチング体験や次の有料コンテンツへの自然な誘導

以下の形式で出力してください（他の説明・前置き不要）：

【タイトル】
（ここにタイトル）

【本文】
（ここにMarkdown形式の本文）"""


def parse_note(raw: str) -> dict:
    """生成テキストをタイトルと本文に分割"""
    title = ""
    body = ""
    if "【タイトル】" in raw and "【本文】" in raw:
        parts = raw.split("【本文】", 1)
        title = parts[0].replace("【タイトル】", "").strip()
        body = parts[1].strip()
    else:
        body = raw
    return {"title": title, "body": body}


def save_note(title: str, body: str, mode: str, date_str: str) -> str:
    """Markdownファイルとして保存し、ファイルパスを返す"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{date_str}_{mode}.md"
    filepath = os.path.join(OUTPUT_DIR, filename)

    content = f"# {title}\n\n{body}" if title else body
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


def main():
    mode = os.environ.get("MODE", "free").lower()
    strategy = load_strategy()
    guide = load_writing_guide()
    writing_guide = format_writing_guide(guide)
    theme_label, theme_desc, combination = determine_theme_and_combination(guide)

    # 過去7日のThreads投稿を取得（freeモードのみ）
    # 取得した投稿はnote記事の参照元として使い、週次分析でもどのThreads投稿が
    # ベースになったかを追跡できるよう post_ids を記録する
    recent_posts = []
    ref_post_ids = ""
    if mode == "free":
        data = get_weekly_data(days=7)
        recent_posts = data.get("posts", [])
        ref_post_ids = ",".join(p.get("post_id", "") for p in recent_posts if p.get("post_id"))
        print(f"[generate_note] 参照Threads投稿: {len(recent_posts)}件")

    # Claude API で記事生成
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if mode == "free":
        prompt = build_free_note_prompt(strategy, recent_posts, theme_label, theme_desc, writing_guide, combination)
        max_tokens = 2500
    else:
        prompt = build_paid_note_prompt(strategy, theme_label, theme_desc, writing_guide, combination)
        max_tokens = 5000

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
    print(f"[generate_note] 生成完了: {title}")

    # Markdownファイルとして保存
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")
    filepath = save_note(title, body, mode, date_str)

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
