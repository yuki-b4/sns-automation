"""
note週次パフォーマンス分析スクリプト
週1回（月曜JST 10:00）実行し、過去4週分のnoteテーマ提案・投稿済み記事のメトリクスを
分析して改善提言をレポートにまとめる。

現行 generate_note.py は本文を書かず 3 テーマ提案のみを行う設計のため、
本スクリプトの分析軸も次の3点に置く:
- テーマ（theme_label / title）の傾向
- target_brain（爬虫類脳 / 哺乳類脳 / 両方）の刺激分散
- 投稿済み記事の閲覧・いいね・コメント

組み合わせパターン（combination_pattern / *_type）は旧スキーマで、レガシーレコードが
残っている場合のみ参考情報として注記する。
note_writing_guide.json は現行スクリプトから参照されない（手書き本文用の参考資料）ため、
本スクリプトも guide JSON は読まず、提言は「手書き本文ルールへの追記案」として出力する。

出力:
- output/reports/YYYY-MM-DD_note_analysis.md（GitHubにコミット）
- Slack通知（サマリー + GitHubレポートURL）
"""

import os
import re
import datetime
import anthropic
from collections import defaultdict
from sheets import get_note_records, get_weekly_data
from notify_slack import notify_slack_note_analysis
from token_cost import log_token_cost

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "../output/reports")
NOTES_DIR = os.path.join(SCRIPT_DIR, "../output/notes")

_BRAIN_LABEL_JA = {
    "reptilian": "爬虫類脳",
    "mammalian": "哺乳類脳",
    "both": "両方",
}


def read_note_file(file_path: str) -> str:
    """output/notes/ から3テーマ提案 Markdown を読み込む。存在しない場合は空文字。"""
    full_path = os.path.join(SCRIPT_DIR, "..", file_path)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def parse_target_brain_from_proposal(content: str, title: str) -> str:
    """3テーマ提案 Markdown から、指定タイトルに対応する想定刺激（爬虫類脳/哺乳類脳/両方）を抽出する。
    取れない場合は空文字を返す。generate_note.save_themes_md のフォーマットに依存。"""
    if not content or not title:
        return ""
    blocks = re.split(r"\n##\s+提案\d+:", content)
    for block in blocks:
        if title in block:
            m = re.search(r"想定刺激\*\*:\s*(\S+)", block)
            if m:
                return m.group(1)
    return ""


def build_analysis_prompt(records: list[dict], article_contents: dict, threads_summary: str) -> str:
    """Claude用の分析プロンプトを構築（テーマ・target_brain 中心）"""

    today = datetime.date.today().strftime("%Y年%m月%d日")
    total = len(records)
    posted = [r for r in records if str(r.get("status", "")).strip() == "posted"]
    with_metrics = [r for r in posted if r.get("likes") or r.get("views")]

    record_lines: list[str] = []
    brain_counts: dict[str, int] = defaultdict(int)
    legacy_pattern_counts: dict[str, int] = defaultdict(int)

    for r in records:
        title = (r.get("title") or "").strip()
        theme = (r.get("theme_label") or "").strip()
        gen_at = str(r.get("generated_at", ""))[:10]
        status = str(r.get("status", "")).strip()
        likes = r.get("likes") or ""
        views = r.get("views") or ""
        comments = r.get("comments") or ""
        file_path = r.get("file_path", "")
        legacy_pattern = (r.get("combination_pattern") or "").strip()
        if legacy_pattern:
            legacy_pattern_counts[legacy_pattern] += 1

        brain = parse_target_brain_from_proposal(article_contents.get(file_path, ""), title)
        if brain:
            # 日本語ラベル（爬虫類脳/哺乳類脳/両方）をキーに集計
            brain_counts[brain] += 1

        if status == "posted":
            metric_part = f" / 投稿済 likes={likes or '—'} views={views or '—'} comments={comments or '—'}"
        else:
            metric_part = f" / status={status or 'proposed'}"
        legacy_part = f" / 旧パターン={legacy_pattern}" if legacy_pattern else ""
        brain_part = f" / 想定刺激={brain}" if brain else ""
        theme_part = f"[{theme}] " if theme else ""

        record_lines.append(
            f"- {gen_at}｜{theme_part}{title}{brain_part}{metric_part}{legacy_part}"
        )

    if brain_counts:
        total_brain = sum(brain_counts.values())
        parts = []
        for key in ("爬虫類脳", "哺乳類脳", "両方"):
            cnt = brain_counts.get(key, 0)
            share = cnt / total_brain if total_brain else 0
            parts.append(f"{key} {cnt}本（{share:.0%}）")
        brain_distribution = " / ".join(parts)
    else:
        brain_distribution = "（取得不可・MD ファイル未読込もしくは旧スキーマレコード）"

    legacy_section = ""
    if legacy_pattern_counts:
        items = sorted(legacy_pattern_counts.items(), key=lambda x: -x[1])
        legacy_section = (
            "\n## レガシースキーマ（参考のみ）\n"
            "現行 generate_note.py は `combination_pattern` を書き込まない。以下は旧仕様時代のレコード件数で、"
            "今後の運用判断の根拠には使わない（過去傾向の参考のみ）。\n"
            + "\n".join(f"- {name}: {cnt}本" for name, cnt in items)
            + "\n"
        )

    metrics_status = (
        f"投稿済 {len(posted)}本 / うちメトリクス入力済 {len(with_metrics)}本"
        if posted else "投稿済レコードなし（定性分析中心）"
    )

    records_block = "\n".join(record_lines) if record_lines else "（過去4週レコードなし）"

    return f"""以下は過去4週間の note 記事データ（テーマ提案＋投稿済み記事のメトリクス）です。週次パフォーマンス分析を行ってください。

## 前提（重要）
- 現行 `generate_note.py` は本文を書かず、3 つの「テーマ提案」を生成し note投稿DB に 3 行を append する設計
- 1 行 = 1 つのテーマ提案。各レコードは theme_label / title / target_brain（爬虫類脳/哺乳類脳/両方）/ reason を持つ
- `combination_pattern` / `title_type` 等の旧スキーマ列は新規生成では空欄。値が入っている場合はレガシーで参考のみ扱い
- 設定ファイル `config/note_writing_guide.json` は現行スクリプトから参照されない（運用者・Claude が手書きで note 本文を書くときの参考資料として残置）

## 分析対象データ（{today}時点）
- 総レコード数: {total}本（= 3テーマ × 提案日数 + 旧仕様時代の単発記録）
- {metrics_status}
- target_brain 分布: {brain_distribution}

## レコード一覧
{records_block}
{legacy_section}
## 参照元Threads投稿（直近7日）
{threads_summary if threads_summary else "（データなし）"}

---

## 分析依頼

以下の構成で Markdown レポートを作成してください。**提示データに無いタイトル・記事本文の文言を引用・捏造しないこと**（記憶や類推で具体例を作り出さない）。

# 週次noteパフォーマンス分析レポート - {today}

## 1. パフォーマンスサマリー
- 投稿済みレコードのメトリクス（likes / views / comments）が複数あればテーマ別／target_brain 別の平均と順位を表で
- target_brain 分布の偏りを 1〜2 文で講評（爬虫類脳 / 哺乳類脳 / 両方 のバランス、generate_note.py の「3 テーマで偏らない」設計に対する実績）
- 投稿済みが少なくメトリクスが薄い場合は、その旨を明記してから定性的に1〜2文で締める

## 2. テーマ別評価
- 直近の theme_label のうち 3〜5 件について、強み（なぜそのテーマがペルソナに刺さるか）と改善点（タイトル・切り口の磨きどころ）を述べる
- それぞれ target_brain の方向と整合しているかを評価（reptilian なら損失/縄張り訴求が効いているか、mammalian なら所属/承認訴求が効いているか）
- 旧スキーマレコードはここでは取り上げない（または1行で簡潔に「旧仕様の○本は参考扱い」と注記するに留める）

## 3. Threads → note の一貫性評価
- 参照 Threads 投稿のテーマと note 提案テーマの一貫性
- `config/note_writing_guide.json` の `engagement_design_rules.threads_to_note_handoff` ルール（Threads投稿の問いをnote冒頭300字以内で明示的に回収）が、手書き本文フェーズで実践しやすい形になっているか

## 4. 来週の推奨アクション
- 重点的に伸ばすべき切り口（理由付き、テーマ単位で具体的に）
- 避けるべき／直近で重複気味の切り口（あれば）
- target_brain 分散の調整提案（偏っている場合のみ）
- テーマ × target_brain の試行案を 2〜3 件

## 5. 手書きnote本文ルールへの追加提案（自由記述）
- このセクションは `note_writing_guide.json` の構造変更や `pattern_distribution` / `free_mode_weekly_caps` 等の**数値・キー変更を提案しない**（運用者がnote本文を手書きするときの参考に対する**追記案**のみ）
- 例: 冒頭の引用ブロック設計／タイトル型の使い分け／Before-After の組立方／読了後アクション設計 など、運用者の執筆判断に直接役立つ短い気づきを 2〜4 件、箇条書きで
- 既存ルール（`high_performance_patterns` / `title_anti_patterns` / `engagement_design_rules` / `title_rules`）と概念が重複する場合は提案しない
"""


def save_report(content: str, date_str: str) -> str:
    """レポートをMarkdownファイルとして保存"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{date_str}_note_analysis.md"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


def extract_summary(report: str, max_chars: int = 400) -> str:
    """レポートからSlack通知用サマリー（パフォーマンスサマリー節）を抽出。
    見出しレベル（## / ### / ####）に依存しないよう正規表現で検索する。"""
    m = re.search(r"^#+\s*1\.?\s*パフォーマンスサマリー\s*$", report, flags=re.MULTILINE)
    if m:
        start = m.end()
        next_m = re.search(r"^#+\s+", report[start:], flags=re.MULTILINE)
        end = start + next_m.start() if next_m else start + max_chars
        return report[start:end].strip()[:max_chars]
    return report[:max_chars]


def main():
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")

    print("[analyze_note] note投稿DBからレコード取得中...")
    records = get_note_records(weeks=4)
    print(f"[analyze_note] 取得レコード: {len(records)}件")

    # 3テーマ提案 Markdown を読み込み（target_brain 抽出用）
    article_contents = {}
    for r in records:
        fp = r.get("file_path", "")
        if fp and fp not in article_contents:
            article_contents[fp] = read_note_file(fp)

    # 直近7日のThreads投稿（参照元）
    threads_data = get_weekly_data(days=7)
    threads_posts = threads_data.get("posts", [])
    threads_summary = "\n".join(
        f"- [{p.get('post_type','')}] {p.get('content','')[:100]}"
        for p in threads_posts[:10]
    )

    print(f"[analyze_note] Claude API で分析中（参照Threads投稿: {len(threads_posts)}件）...")
    prompt = build_analysis_prompt(records, article_contents, threads_summary)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    log_token_cost("claude-opus-4-7", message.usage, "analyze_note_performance")
    report = message.content[0].text.strip()

    filepath = save_report(report, date_str)
    rel_path = f"output/reports/{date_str}_note_analysis.md"
    repo = os.environ.get("GITHUB_REPOSITORY", "yuki-b4/sns-automation")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    github_url = f"https://github.com/{repo}/blob/{branch}/{rel_path}"

    print(f"[analyze_note] レポート保存: {filepath}")
    print(f"[analyze_note] GitHub URL: {github_url}")

    summary = extract_summary(report)
    notify_slack_note_analysis(date_str, github_url, summary)

    print("[analyze_note] 完了")


if __name__ == "__main__":
    main()
