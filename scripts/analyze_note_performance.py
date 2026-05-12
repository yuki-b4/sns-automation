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


def _format_record_line(r: dict, brain: str) -> str:
    """1 レコードを Markdown 箇条書き行に整形する。"""
    title = (r.get("title") or "").strip()
    theme = (r.get("theme_label") or "").strip()
    gen_at = str(r.get("generated_at", ""))[:10]
    status = str(r.get("status", "")).strip()
    likes = r.get("likes") or ""
    views = r.get("views") or ""
    comments = r.get("comments") or ""
    legacy_pattern = (r.get("combination_pattern") or "").strip()

    if status == "posted":
        metric_part = f" / 投稿済 likes={likes or '—'} views={views or '—'} comments={comments or '—'}"
    else:
        metric_part = f" / status={status or 'proposed'}"
    legacy_part = f" / 旧パターン={legacy_pattern}" if legacy_pattern else ""
    brain_part = f" / 想定刺激={brain}" if brain else ""
    theme_part = f"[{theme}] " if theme else ""
    return f"- {gen_at}｜{theme_part}{title}{brain_part}{metric_part}{legacy_part}"


def _format_brain_distribution(counts: dict[str, int]) -> str:
    """{爬虫類脳: n, ...} を「爬虫類脳 X本（Y%）/ ...」に整形する。空なら注記を返す。"""
    total = sum(counts.values())
    if not total:
        return "（target_brain 取得不可・MD ファイル未読込もしくは旧スキーマレコード）"
    parts = []
    for key in ("爬虫類脳", "哺乳類脳", "両方"):
        cnt = counts.get(key, 0)
        share = cnt / total if total else 0
        parts.append(f"{key} {cnt}本（{share:.0%}）")
    return " / ".join(parts)


def build_analysis_prompt(records: list[dict], article_contents: dict, threads_summary: str) -> str:
    """Claude用の分析プロンプトを構築（テーマ・target_brain 中心、posted/proposed 分離）"""

    today = datetime.date.today().strftime("%Y年%m月%d日")
    total = len(records)

    posted_lines: list[str] = []
    proposed_lines: list[str] = []
    brain_counts_all: dict[str, int] = defaultdict(int)
    brain_counts_posted: dict[str, int] = defaultdict(int)
    legacy_pattern_counts: dict[str, int] = defaultdict(int)

    posted_records: list[dict] = []
    with_metrics_count = 0

    for r in records:
        status = str(r.get("status", "")).strip()
        title = (r.get("title") or "").strip()
        file_path = r.get("file_path", "")
        legacy_pattern = (r.get("combination_pattern") or "").strip()
        if legacy_pattern:
            legacy_pattern_counts[legacy_pattern] += 1

        brain = parse_target_brain_from_proposal(article_contents.get(file_path, ""), title)
        if brain:
            brain_counts_all[brain] += 1

        line = _format_record_line(r, brain)
        if status == "posted":
            posted_records.append(r)
            if r.get("likes") or r.get("views"):
                with_metrics_count += 1
            if brain:
                brain_counts_posted[brain] += 1
            posted_lines.append(line)
        else:
            proposed_lines.append(line)

    brain_distribution_all = _format_brain_distribution(brain_counts_all)
    brain_distribution_posted = _format_brain_distribution(brain_counts_posted)

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
        f"投稿済 {len(posted_records)}本 / うちメトリクス入力済 {with_metrics_count}本"
        if posted_records else "投稿済レコードなし（定性分析中心）"
    )

    posted_block = "\n".join(posted_lines) if posted_lines else "（投稿済みレコードなし）"
    proposed_block = "\n".join(proposed_lines) if proposed_lines else "（未採択提案なし）"

    return f"""以下は過去4週間の note 記事データ（テーマ提案＋投稿済み記事のメトリクス）です。週次パフォーマンス分析を行ってください。

## 前提（重要）
- 現行 `generate_note.py` は本文を書かず、3 つの「テーマ提案」を生成し note投稿DB に 3 行を append する設計
- 1 行 = 1 つのテーマ提案。各レコードは theme_label / title / target_brain（爬虫類脳/哺乳類脳/両方）/ reason を持つ
- **運用実態**: 生成日のうち投稿する日でも 3 提案中 1〜2 件のみ採択。投稿しない日もある。残りの提案レコードは `status='proposed'` のまま残る
- **DB の `title` は提案時の仮タイトルで、note.com 上の実タイトルとは異なる場合がある**（運用者が改題する）。テーマ別評価は `theme_label` を主軸に行い、`title` は参考扱いとする
- 投稿の有無は `status` 列で判定する（posted = 採択・投稿済、proposed = 未採択 or 投稿予定なし）
- `combination_pattern` / `title_type` 等の旧スキーマ列は新規生成では空欄。値が入っている場合はレガシーで参考のみ扱い
- 設定ファイル `config/note_writing_guide.json` は現行スクリプトから参照されない（運用者・Claude が手書きで note 本文を書くときの参考資料として残置）

## 分析対象データ（{today}時点）
- 総レコード数: {total}本
- {metrics_status}
- target_brain 分布（提案全体）: {brain_distribution_all}
- target_brain 分布（投稿済みのみ）: {brain_distribution_posted}

## 投稿された記事（status='posted'・パフォーマンス分析の主軸）
{posted_block}

## 未採択の提案（status='proposed'・運用者が選ばなかった = 訴求が相対的に弱いと判断されたシグナル）
{proposed_block}
{legacy_section}
## 参照元Threads投稿（直近7日）
{threads_summary if threads_summary else "（データなし）"}

---

## 分析依頼

以下の構成で Markdown レポートを作成してください。**提示データに無いタイトル・記事本文の文言を引用・捏造しないこと**（記憶や類推で具体例を作り出さない）。

# 週次noteパフォーマンス分析レポート - {today}

## 1. パフォーマンスサマリー
- **メトリクス分析は `status='posted'` のレコードのみで行う**（未採択提案はメトリクス計算に含めない）
- 投稿済みレコードのメトリクス（likes / views / comments）が複数あればテーマ別／target_brain 別の平均と順位を表で
- target_brain 分布の偏りを 1〜2 文で講評。**「提案全体の分散」と「投稿済みの分散」の両方を見て、運用者が特定の脳タイプを偏って採択していないか**を述べる
- 投稿済みが少なくメトリクスが薄い場合は、その旨を明記してから定性的に1〜2文で締める

## 2. テーマ別評価
- **投稿された記事（status='posted'）の `theme_label` のみを対象に**、3〜5 件について強み（なぜそのテーマがペルソナに刺さるか）と改善点（切り口の磨きどころ）を述べる
- `title` ではなく `theme_label` を中心に評価する（実タイトルは運用者が改題しているため DB の title とは異なる）
- それぞれ target_brain の方向と整合しているかを評価（reptilian なら損失/縄張り訴求、mammalian なら所属/承認訴求）
- 旧スキーマレコードはここでは取り上げない（または1行で簡潔に「旧仕様の○本は参考扱い」と注記）

## 3. 採択シグナル分析（運用者キュレーション）
- **未採択提案（status='proposed'）の傾向を分析**し、運用者が選ばない切り口の共通点を 1〜2 文で述べる
- 例: 「特定の theme 系統が連続して未採択」「特定の target_brain が採択されにくい」など
- 提案生成プロンプト（generate_note.py）への含意がある場合は短く触れる

## 4. Threads → note の一貫性評価
- 参照 Threads 投稿のテーマと **投稿された note 記事のテーマ** の一貫性（未採択提案は除外）
- `config/note_writing_guide.json` の `engagement_design_rules.threads_to_note_handoff` ルール（Threads投稿の問いをnote冒頭300字以内で明示的に回収）が、手書き本文フェーズで実践しやすい形になっているか

## 5. 来週の推奨アクション
- 重点的に伸ばすべき切り口（理由付き、テーマ単位で具体的に）— 投稿された記事のパフォーマンス＋採択シグナルから導く
- 避けるべき／直近で重複気味の切り口（あれば）
- target_brain 分散の調整提案（投稿済みの分散に偏りがある場合のみ）
- テーマ × target_brain の試行案を 2〜3 件

## 6. 手書きnote本文ルールへの追加提案（自由記述）
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
