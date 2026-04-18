"""
note記事生成スクリプト
Claude APIを使って毎日1本のnote記事ドラフトをMarkdownで生成し、
output/notes/YYYY-MM-DD_{mode}.md に保存して Slack にGitHub URLを通知する

モード:
  free  (デフォルト) - 過去7日のThreads投稿を参考に1200〜1500字の無料note記事を生成
  paid               - strategy.jsonの5本柱から2500〜3500字の有料note記事を生成
"""

import os
import re
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


def load_recent_note_excerpts(n: int = 5, exclude_date: str | None = None) -> list[dict]:
    """直近N件のnote Markdownファイルをファイル名の新しい順で返す。
    exclude_date（YYYY-MM-DD）で始まるファイルは除外する（同日リトライ時の自己参照防止）。
    心理学・脳科学の用語／研究結果の重複回避プロンプトに使用する。"""
    if not os.path.isdir(OUTPUT_DIR):
        return []
    files = sorted(
        (f for f in os.listdir(OUTPUT_DIR) if f.endswith(".md")),
        reverse=True,
    )
    if exclude_date:
        files = [f for f in files if not f.startswith(exclude_date)]

    excerpts: list[dict] = []
    for filename in files[:n]:
        filepath = os.path.join(OUTPUT_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                excerpts.append({"filename": filename, "content": fh.read()})
        except OSError:
            continue
    return excerpts


# 心理学・脳科学の言及を含む可能性が高い文を抽出するためのキーワード群
_PSYCH_KEYWORD_RE = re.compile(
    r"(効果|博士|研究|ネットワーク|因子|ホルモン|症候群|理論|枯渇|疲労|"
    r"メラトニン|セロトニン|ドーパミン|コルチゾール|アドレナリン|オキシトシン|"
    r"大学|HRV|DMN|BDNF|ツァイガルニク|デフォルトモード|認知資源|"
    r"ワーキングメモリ|交感神経|副交感神経|自律神経|前頭前野|海馬)"
)


def _extract_title(content: str) -> str:
    """Markdown本文から最初の見出し行をタイトルとして取り出す。"""
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s.lstrip("# ").strip()
    return ""


def _extract_psych_mentions(content: str, max_lines: int = 3) -> list[str]:
    """心理学・脳科学の言及を含む段落を最大N件返す（1件200字に切り詰め）。"""
    hits: list[str] = []
    for para in content.splitlines():
        p = para.strip()
        if not p or p.startswith("#"):
            continue
        if _PSYCH_KEYWORD_RE.search(p):
            hits.append(p[:200])
        if len(hits) >= max_lines:
            break
    return hits


def format_recent_notes_for_avoidance(excerpts: list[dict]) -> str:
    """直近noteから心理学・脳科学の既出言及のみを抜粋してプロンプトに整形する。
    本文全体ではなく抽出結果だけを渡すことでトークンを大幅削減する。"""
    if not excerpts:
        return ""
    lines = ["【直近のnote記事で既出の心理学・脳科学言及（再利用禁止）】"]
    any_hits = False
    for e in excerpts:
        date = e["filename"][:10]
        title = _extract_title(e["content"])
        mentions = _extract_psych_mentions(e["content"])
        header = f"- {date}「{title}」" if title else f"- {date}"
        if mentions:
            any_hits = True
            lines.append(header)
            for m in mentions:
                lines.append(f"  · {m}")
        else:
            lines.append(f"{header}: 心理学・脳科学の言及なし")
    return "\n".join(lines)


def _find_pattern(patterns: list[dict], type_name: str) -> dict:
    """patternリストからtypeが一致する要素を返す（見つからなければ空辞書）。"""
    return next((p for p in patterns if p.get("type") == type_name), {})


def format_writing_guide(guide: dict, combination: dict) -> str:
    """note執筆ガイドをプロンプト注入用テキストに変換。
    combinationで指定された4型（title/hook/problem/solution）だけを展開し、
    21パターン全展開を避けてトークンを大幅削減する。
    engagement_principles・structure_templateは要約形式で付加。"""
    lines: list[str] = []

    # 当日採用する4型のみ展開
    tp = _find_pattern(guide["title_rules"]["patterns"], combination["title_type"])
    if tp:
        lines.append(f"【タイトル型：{tp['type']}】例: {tp['example']}（{tp['note']}）")
    lines.append(f"タイトルNG: {' / '.join(guide['title_rules']['avoid'])}")

    hp = _find_pattern(guide["opening_hook_rules"]["patterns"], combination["hook_type"])
    if hp:
        lines.append(f"\n【冒頭フック型：{hp['type']}】例: {hp['example']}")
    lines.append("冒頭ルール: " + " / ".join(guide["opening_hook_rules"]["rules"]))

    pp = _find_pattern(guide["problem_presentation_rules"]["patterns"], combination["problem_type"])
    if pp:
        lines.append(f"\n【課題提示型：{pp['type']}】{pp['point']}")
        lines.append(f"例: {pp['example']}")

    sp = _find_pattern(guide["solution_presentation_rules"]["patterns"], combination["solution_type"])
    if sp:
        lines.append(f"\n【解決法型：{sp['type']}】{sp['point']}")
        lines.append(f"例: {sp['example']}")
    lines.append("解決法NG: " + " / ".join(guide["solution_presentation_rules"]["avoid"]))

    # いいね原則は見出しの羅列のみ（詳細は4型の指示に含まれる）
    principle_names = " / ".join(p["name"] for p in guide["engagement_principles"]["principles"])
    lines.append(f"\n【いいね5原則】{principle_names}")

    # 構成テンプレート（1行に圧縮）
    tmpl = guide["engagement_principles"]["structure_template"]
    lines.append("【構成】" + " → ".join(tmpl["sections"]))

    return "\n".join(lines)


# Threads post_type → 表示ラベル（プロンプト内で使用）
_POST_TYPE_LABELS = {
    "permission": "許可系（罪悪感・後ろめたさへの共感メッセージ）",
    "structure":  "体系化系（仕組み・設計・手順を構造的に伝える投稿）",
    "personal":   "自己開示系（著者自身の失敗・経験・変化のプロセス）",
    "opinion":    "業界考察系（働き方・残業文化などを設計視点で分析）",
    "dialogue":   "対話系（読者への問いかけ・エンゲージメント促進）",
}

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

- タイトル → {combination['title_type']}：{inst['title']}
- 冒頭フック（最初の100字）→ {combination['hook_type']}：{inst['hook']}
- 課題提示 → {combination['problem_type']}：{inst['problem']}
- 解決法 → {combination['solution_type']}：{inst['solution']}

この組み合わせの相乗効果：{combination['synergy']}"""


# writing_guide（title/hook/problem/solutionの4型）でカバー済みの要素IDは
# 重複注入を避けるため selling_elements から除外する。
# 除外: 1(ターゲット明示)・2(悩み解像度)・3(構造的再定義)・8(実践ガイド) → writing_guide側で同等以上を指示
# 残す: 4(脳科学根拠)・5(独自メソッド)・6(具体数字)・7(再現性)・9(独自経験)・10(読後変化)・11(価格正当化)・12(次のCTA)
_PAID_UNIQUE_SELLING_ELEMENT_IDS = {4, 5, 6, 7, 9, 10, 11, 12}


def select_top_selling_elements(guide: dict) -> list[dict]:
    """有料note固有の売れる要素だけを priority 昇順で返す。
    writing_guide と重複する汎用要素（1/2/3/8）は除外してトークンを削減する。"""
    elements = guide["paid_note_selling_elements"]["elements"]
    filtered = [e for e in elements if e.get("id") in _PAID_UNIQUE_SELLING_ELEMENT_IDS]
    return sorted(filtered, key=lambda e: e.get("priority", 99))


def format_selling_elements(guide: dict) -> str:
    """有料note 売れる要素チェックリストをプロンプト注入用テキストに変換（paid固有要素のみ）"""
    selected = select_top_selling_elements(guide)
    lines = [f"（以下{len(selected)}個すべてを含めること。writing_guideで既に指示された型の実践に上乗せする有料note固有の要素）"]
    for e in selected:
        lines.append(f"{e['id']}. 【{e['name']}】{e['description']} ／ 配置: {e['placement']}")
    return "\n".join(lines)


def build_free_note_prompt(
    strategy: dict,
    recent_posts: list[dict],
    theme_label: str,
    theme_desc: str,
    writing_guide: str,
    combination: dict,
    recent_notes_section: str = "",
) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]

    # エンゲージメント上位の投稿タイプを見出しで示し、数値は省略して投稿内容のみ表示
    seen_types: list[str] = []
    for p in recent_posts:
        pt = p.get("post_type", "")
        if pt and pt not in seen_types:
            seen_types.append(pt)
        if len(seen_types) >= 3:
            break
    top_labels = [_POST_TYPE_LABELS.get(t, t) for t in seen_types]
    top_types_header = (
        f"（エンゲージメント上位の投稿タイプ: {' > '.join(top_labels)} の順）\n"
        if top_labels else ""
    )
    posts_text = top_types_header + "\n".join(
        f"- [{_POST_TYPE_LABELS.get(p.get('post_type',''), p.get('post_type',''))}] {p.get('content','')}"
        for p in recent_posts[:15]
    )

    return f"""以下の戦略・執筆ガイド・Threads投稿履歴を参考に、ペルソナ向け無料note記事を生成してください。

【ポジショニング】{positioning["position"]}｜{positioning["concept"]}｜差別化: {positioning["differentiation"]}
【ステートメント】{positioning["statement"]}

【ターゲット】{persona["description"]}
悩み: {', '.join(persona["pain_points"])}

【今日のテーマ】{theme_label}：{theme_desc}

{format_combination_instruction(combination)}

【過去7日のThreads投稿（参考・発展のベース）】
{posts_text if posts_text else "（参考投稿なし）"}

{recent_notes_section}

【note執筆ガイド】
{writing_guide}

【記事の目的】
- SNS → 無料note → 有料コンテンツのファネル入口として機能させる
- 悩みに共感しつつ手法の入口を示し「この人の有料も読みたい」と思わせる
- Threads投稿をコピーせず深掘り・発展させる

【ルール】
- 1200〜1500字、見出しは `##`（Markdown）
- 設計・仕組み視点。精神論・根性論はNG
- CTAは「〜はこちらで詳しく書いています」程度の自然な誘導。不自然なら省略可
- クライアント例は「クライアントの方から」「よくある例として」の形で。事実でない体験談は書かない
- 数値は「30分以上／1時間以上／2割以上／週3時間程度」のようにキリの良い値＋「以上／程度／前後」で表現。端数の具体値（48分・23%等）はAI生成感が出るのでNG

出力形式（他の説明・前置き不要）：

【タイトル】
（ここにタイトル）

【本文】
（ここにMarkdown形式の本文）"""


def build_paid_note_prompt(
    strategy: dict,
    theme_label: str,
    theme_desc: str,
    writing_guide: str,
    combination: dict,
    guide: dict,
    recent_notes_section: str = "",
) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]
    selling_elements = format_selling_elements(guide)

    return f"""以下の戦略・執筆ガイドに基づき、ペルソナ向け有料note記事（¥1,980相当）を生成してください。

【ポジショニング】{positioning["position"]}｜{positioning["concept"]}
【商品】{positioning["product_title"]}（{positioning["product_subtitle"]}）
【ステートメント】{positioning["statement"]}

【ターゲット】{persona["description"]}
悩み: {', '.join(persona["pain_points"])}

【テーマ】{theme_label}：{theme_desc}

{format_combination_instruction(combination)}

{recent_notes_section}

【note執筆ガイド】
{writing_guide}

【有料note 売れる要素チェックリスト】
{selling_elements}

【記事の目的】
- ¥1,980に見合う具体的価値を提供する
- 「読んだだけで行動が変わった」と感じさせる再現性の高い手法を出す
- 価値提供で信頼を構築し、上位商材への橋渡しにする

【ルール】
- 2500〜3500字、見出しは `##` / `###`（Markdown）
- 心理学・脳科学の根拠を最低1つ含める（要素4）
- 数値は「30分以上／1時間以上／2割以上／週3時間程度」のようにキリの良い値＋「以上／程度／前後」。端数の具体値（48分・23%等）はAI生成感が出るのでNG（要素6）
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

    # 執筆ガイドは combination に応じて当日採用する4型だけ展開
    writing_guide = format_writing_guide(guide, combination)

    ref_post_ids = ",".join(
        str(p.get("post_id", "")) for p in recent_posts_raw if p.get("post_id")
    )

    # 直近note（過去5本まで）を読み込み、心理学・脳科学の言及重複を避けるコンテキストとして注入
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_str = now_jst.strftime("%Y-%m-%d")
    recent_note_excerpts = load_recent_note_excerpts(n=5, exclude_date=today_str)
    recent_notes_section = format_recent_notes_for_avoidance(recent_note_excerpts)
    print(f"[generate_note] 直近note参照: {len(recent_note_excerpts)}件 (重複回避用)")

    # Claude API で記事生成
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if mode == "free":
        prompt = build_free_note_prompt(
            strategy, recent_posts, theme_label, theme_desc, writing_guide, combination,
            recent_notes_section=recent_notes_section,
        )
        max_tokens = 2500
        selected_element_ids = ""
    else:
        prompt = build_paid_note_prompt(
            strategy, theme_label, theme_desc, writing_guide, combination, guide,
            recent_notes_section=recent_notes_section,
        )
        max_tokens = 5500  # チェックリスト分を追加
        selected_element_ids = ",".join(
            str(e["id"]) for e in select_top_selling_elements(guide)
        )

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

    # Markdownファイルとして保存（now_jst / today_str は関数冒頭で確定済み）
    date_str = today_str
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
        "selling_element_ids": selected_element_ids,
    })

    print("[generate_note] 完了")


if __name__ == "__main__":
    main()
