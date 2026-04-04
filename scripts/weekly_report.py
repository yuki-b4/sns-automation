"""
週次改善レポート生成スクリプト
Google Sheetsから過去7日分のデータ＋競合データを取得し、
Claude APIで分析してSlackにレポートを送信する
毎週月曜 9:00 JSTに実行
"""

import os
import json
import anthropic
from sheets import get_weekly_data, get_recent_competitor_data
from notify_slack import notify_slack_report


STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def summarize_own_data(weekly_data: dict) -> str:
    posts = weekly_data["posts"]
    metrics = weekly_data["metrics"]

    if not posts:
        return "データなし（まだ投稿がありません）"

    metrics_map = {m["post_id"]: m for m in metrics}

    type_stats: dict[str, dict] = {}
    for post in posts:
        pt = post.get("post_type", "unknown")
        mid = post.get("post_id", "")
        m = metrics_map.get(mid, {})
        er = float(m.get("engagement_rate", 0))
        if pt not in type_stats:
            type_stats[pt] = {"count": 0, "total_er": 0.0, "total_impressions": 0}
        type_stats[pt]["count"] += 1
        type_stats[pt]["total_er"] += er
        type_stats[pt]["total_impressions"] += int(m.get("impressions", 0))

    lines = [f"過去7日間の投稿数: {len(posts)}件"]
    for pt, stats in type_stats.items():
        avg_er = round(stats["total_er"] / stats["count"], 4) if stats["count"] > 0 else 0
        lines.append(
            f"- {pt}（{stats['count']}件）: 平均ER={avg_er:.2%} 合計インプレッション={stats['total_impressions']}"
        )
    return "\n".join(lines)


def summarize_competitor_data(competitor_data: list[dict]) -> str:
    if not competitor_data:
        return "競合データなし"

    lines = []
    for r in competitor_data[-10:]:  # 直近10件
        lines.append(
            f"- {r.get('competitor_id', '')}: "
            f"テーマ={r.get('dominant_themes', '')} / "
            f"空白地帯={r.get('positioning_gap', '')} / "
            f"ER={r.get('avg_engagement_rate', 0)}"
        )
    return "\n".join(lines)


def generate_report(strategy: dict, own_summary: str, competitor_summary: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    positioning = strategy["positioning"]
    post_types = strategy["post_types"]

    current_ratios = "\n".join([
        f"- {v['label']}：{int(v['ratio'] * 100)}%"
        for v in post_types.values()
    ])

    prompt = f"""あなたはSNS戦略アドバイザーです。
以下のデータを分析し、日本語で改善提案を出してください。

【ポジショニング】
- ポジション：{positioning["position"]}
- コンセプト：{positioning["concept"]}
- 差別化軸：{positioning["differentiation"]}

【現在の投稿タイプ配分】
{current_ratios}

【過去7日間の自社データ】
{own_summary}

【競合データ（直近）】
{competitor_summary}

【出力形式】
以下の4項目を番号付きで出力してください：

1. ポジショニング/差別化軸の調整案（自社データ+競合との差分に基づく）
2. 投稿タイプ配分の調整案（許可系X%・体系化系X%・自己開示系X%で提示）
3. 競合が取れていない空白地帯・先手で取るべきテーマ（上位3件）
4. その他改善案（上位3件）"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def main():
    strategy = load_strategy()
    weekly_data = get_weekly_data(weeks=1)
    competitor_data = get_recent_competitor_data()

    own_summary = summarize_own_data(weekly_data)
    competitor_summary = summarize_competitor_data(competitor_data)

    print("[週次レポート] データ収集完了")
    print(f"自社データ:\n{own_summary}\n")
    print(f"競合データ:\n{competitor_summary}\n")

    report = generate_report(strategy, own_summary, competitor_summary)
    print(f"[週次レポート] 生成完了:\n{report}")

    notify_slack_report(report, title="週次改善レポート")
    print("[週次レポート] Slack通知完了")


if __name__ == "__main__":
    main()
