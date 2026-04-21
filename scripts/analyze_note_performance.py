"""
note週次パフォーマンス分析スクリプト
週1回（日曜JST 10:00）実行し、過去4週分の記事データを分析して
組み合わせパターンの効果を評価・改善提言をレポートにまとめる。

分析内容:
- 各組み合わせパターン（共感最大化/信頼構築/行動変容/ファン化/知的好奇心）の評価
- 参照したThreads投稿とnote記事の内容一貫性
- メトリクスがある場合は定量分析、なければ定性分析
- note_writing_guide.jsonへの具体的な更新提案

出力:
- output/reports/YYYY-MM-DD_note_analysis.md（GitHubにコミット）
- Slack通知（サマリー + GitHubレポートURL）
"""

import os
import datetime
import anthropic
from collections import defaultdict
from sheets import get_note_records, get_weekly_data
from notify_slack import notify_slack_note_analysis
from token_cost import log_token_cost

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "../output/reports")
NOTES_DIR = os.path.join(SCRIPT_DIR, "../output/notes")


def read_note_file(file_path: str) -> str:
    """output/notes/ からMarkdown記事を読み込む"""
    full_path = os.path.join(SCRIPT_DIR, "..", file_path)
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def build_analysis_prompt(records: list[dict], article_contents: dict, threads_summary: str) -> str:
    """Claude用の分析プロンプトを構築"""

    # パターン別に集計
    by_pattern = defaultdict(list)
    for r in records:
        pattern = r.get("combination_pattern") or "未設定"
        by_pattern[pattern].append(r)

    today = datetime.date.today().strftime("%Y年%m月%d日")
    total = len(records)
    posted = [r for r in records if r.get("status") == "posted"]
    with_metrics = [r for r in posted if r.get("likes") or r.get("views")]

    # パターン別サマリー
    pattern_sections = []
    for pattern, recs in sorted(by_pattern.items()):
        posted_recs = [r for r in recs if r.get("status") == "posted"]
        metric_recs = [r for r in posted_recs if r.get("likes") or r.get("views")]

        # メトリクス集計
        if metric_recs:
            avg_likes = sum(int(r.get("likes") or 0) for r in metric_recs) / len(metric_recs)
            avg_views = sum(int(r.get("views") or 0) for r in metric_recs) / len(metric_recs)
            avg_comments = sum(int(r.get("comments") or 0) for r in metric_recs) / len(metric_recs)
            metrics_text = (
                f"  - いいね平均: {avg_likes:.1f} / 閲覧数平均: {avg_views:.1f} / "
                f"コメント平均: {avg_comments:.1f}（{len(metric_recs)}本データあり）"
            )
        else:
            metrics_text = "  - メトリクス: 未入力（note.comから手動入力が必要）"

        # 型構成
        if recs:
            sample = recs[0]
            types_text = (
                f"  - 型構成: {sample.get('title_type','')} × "
                f"{sample.get('hook_type','')} × "
                f"{sample.get('problem_type','')} × "
                f"{sample.get('solution_type','')}"
            )
        else:
            types_text = ""

        # 記事コンテンツ（直近1本の冒頭500字）
        content_preview = ""
        for r in reversed(recs):
            fp = r.get("file_path", "")
            content = article_contents.get(fp, "")
            if content:
                content_preview = f"  - 記事冒頭:\n```\n{content[:500]}\n```"
                break

        section = (
            f"\n### {pattern}\n"
            f"  - 生成: {len(recs)}本 / 投稿済み: {len(posted_recs)}本\n"
            f"{metrics_text}\n"
            f"{types_text}\n"
            f"{content_preview}"
        )
        pattern_sections.append(section)

    patterns_text = "\n".join(pattern_sections)

    metrics_status = (
        f"{len(with_metrics)}本にメトリクスあり" if with_metrics
        else "メトリクス未入力（定性分析のみ実施）"
    )

    return f"""以下は過去4週間のnote記事データです。週次パフォーマンス分析を行ってください。

## 分析対象データ（{today}時点）
- 総生成数: {total}本 / 投稿済み: {len(posted)}本 / {metrics_status}

## パターン別データ
{patterns_text}

## 参照元Threads投稿（直近7日）
{threads_summary if threads_summary else "（データなし）"}

---

## 分析依頼

以下の構成でMarkdownレポートを作成してください。

### 週次noteパフォーマンス分析レポート - {today}

#### 1. パフォーマンスサマリー
- メトリクスがある場合: パターン別のいいね・閲覧数・コメント数の比較と順位
- メトリクスがない場合: コンテンツの質・構成・訴求力の観点から各パターンを5段階評価（★）

#### 2. パターン別評価（各パターンに対して）
- 強み: このパターンが効果を発揮している理由
- 改善点: 記事の構成・表現・訴求で改善できる点
- 推定パフォーマンス: いいね/閲覧/コメント/転換率の各指標の期待値（高/中/低）

#### 3. Threads → note の一貫性評価
- 参照したThreads投稿のテーマとnote記事の内容は一貫しているか
- SNSからnoteへの誘導が自然に設計されているか

#### 4. 来週の推奨アクション
- 重点的に活用すべきパターン（理由付き）
- 調整が必要なパターン（具体的な改善案）
- テーマとパターンの組み合わせで試すべき変更案

#### 5. note_writing_guide.json 更新提案
- 追加すべき知見・ルール（具体的なJSON項目として記述）
- 修正すべき内容（現状と修正案を対比して記述）"""


def save_report(content: str, date_str: str) -> str:
    """レポートをMarkdownファイルとして保存"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{date_str}_note_analysis.md"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


def extract_summary(report: str, max_chars: int = 400) -> str:
    """レポートからSlack通知用サマリー（パフォーマンスサマリー節）を抽出"""
    marker = "#### 1. パフォーマンスサマリー"
    if marker in report:
        start = report.index(marker) + len(marker)
        end = report.find("####", start)
        summary = report[start:end].strip() if end > start else report[start:start + max_chars].strip()
        return summary[:max_chars]
    return report[:max_chars]


def main():
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date_str = now_jst.strftime("%Y-%m-%d")

    print("[analyze_note] note投稿DBからレコード取得中...")
    records = get_note_records(weeks=4)
    print(f"[analyze_note] 取得レコード: {len(records)}件")

    # 記事Markdownを読み込む
    article_contents = {}
    for r in records:
        fp = r.get("file_path", "")
        if fp and fp not in article_contents:
            article_contents[fp] = read_note_file(fp)

    # 直近7日のThreads投稿を取得（参照元として分析に含める）
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

    # レポートを保存
    filepath = save_report(report, date_str)
    rel_path = f"output/reports/{date_str}_note_analysis.md"
    repo = os.environ.get("GITHUB_REPOSITORY", "yuki-b4/sns-automation")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    github_url = f"https://github.com/{repo}/blob/{branch}/{rel_path}"

    print(f"[analyze_note] レポート保存: {filepath}")
    print(f"[analyze_note] GitHub URL: {github_url}")

    # Slack通知（サマリー + URL）
    summary = extract_summary(report)
    notify_slack_note_analysis(date_str, github_url, summary)

    print("[analyze_note] 完了")


if __name__ == "__main__":
    main()
