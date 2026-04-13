"""
競合分析スクリプト
Google Sheets「競合投稿DB」に手動入力された投稿データを読み込み、
Claude API で集計分析を行い「競合分析DB」にサマリーを記録する。
火・金 08:00 JST に実行。
"""

import os
import json
import datetime
import anthropic
from sheets import get_recent_competitor_posts, append_competitor_record, mark_competitor_posts_analyzed


STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_posts_text(posts: list[dict]) -> str:
    """投稿リストをスレッド構造を保ったプロンプト用テキストに変換する"""
    standalone = [p for p in posts if not str(p.get("thread_id", "")).strip()]
    threaded = [p for p in posts if str(p.get("thread_id", "")).strip()]

    blocks: list[str] = []

    # スタンドアロン投稿（エンゲージメント降順）
    if standalone:
        sorted_standalone = sorted(
            standalone,
            key=lambda p: int(p.get("likes", 0)) + int(p.get("replies", 0)),
            reverse=True,
        )
        blocks.append("＜スタンドアロン投稿＞")
        for i, p in enumerate(sorted_standalone[:10], 1):
            content = str(p.get("content", "")).strip()
            if not content:
                continue
            blocks.append(
                f"投稿{i}（いいね:{p.get('likes', 0)} リプライ:{p.get('replies', 0)}）:\n{content}"
            )

    # スレッド投稿（thread_idごとにグループ化・ルートのエンゲージメント降順）
    if threaded:
        groups: dict[str, list[dict]] = {}
        for p in threaded:
            tid = str(p.get("thread_id", ""))
            groups.setdefault(tid, []).append(p)

        def root_engagement(group: list[dict]) -> int:
            root = next((p for p in group if str(p.get("reply_order", "")) == "0"), group[0])
            return int(root.get("likes", 0)) + int(root.get("replies", 0))

        sorted_groups = sorted(groups.values(), key=root_engagement, reverse=True)

        blocks.append("\n＜スレッド投稿＞")
        for t_i, group in enumerate(sorted_groups[:5], 1):
            sorted_group = sorted(group, key=lambda p: int(p.get("reply_order") or 0))
            thread_lines = [f"スレッド{t_i}:"]
            for p in sorted_group:
                content = str(p.get("content", "")).strip()
                if not content:
                    continue
                order = p.get("reply_order", "")
                label = "ルート" if str(order) == "0" else f"リプライ{order}"
                thread_lines.append(
                    f"  {label}（いいね:{p.get('likes', 0)} リプライ:{p.get('replies', 0)}）:\n  {content}"
                )
            blocks.append("\n".join(thread_lines))

    return "\n\n".join(blocks)


def analyze_with_claude(posts: list[dict], strategy: dict) -> dict:
    """Claude API で競合投稿を集計分析し、サマリーを返す"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    posts_text = _build_posts_text(posts)

    if not posts_text.strip():
        print("[競合分析] 本文のある投稿がないためスキップ")
        return {}

    positioning = strategy.get("positioning", {})

    prompt = f"""あなたはSNS戦略アナリストです。以下の競合投稿を分析し、日本語で回答してください。
投稿は「スタンドアロン投稿」と「スレッド投稿（ルート＋リプライ1/2/3…）」に分かれています。
スレッド投稿は一連の文章として読み、構成パターンも分析対象に含めてください。

【自社ポジション】
- ポジション：{positioning.get("position", "")}
- コンセプト：{positioning.get("concept", "")}
- 差別化軸：{positioning.get("differentiation", "")}

【競合の投稿】
{posts_text}

【出力形式】
JSON形式のみで出力してください（前後の説明文は不要）：
{{
  "top_posts": "エンゲージメント上位3件の共通点・要約（300字以内）",
  "avg_engagement_rate": いいね+リプライの合計を投稿数で割った数値（小数点2桁）,
  "dominant_themes": "頻出テーマ・キーワード（カンマ区切り、5件程度）",
  "positioning_gap": "自社ポジションとの差分・競合が取れていない空白地帯（300字以内）",
  "thread_analysis": "スレッド投稿の構成パターン分析。何投稿構成か・各リプライの役割・どの構成が高エンゲージメントかを具体的に記述（300字以内）。スレッド投稿がない場合は空文字"
}}

【文字数】全体で約1200文字を目安にすること"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    if "```json" in raw:
        raw = raw.split("```json")[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```")[1].split("```")[0].strip()

    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[競合分析] Claude分析のJSON解析失敗: {e}")
        return {
            "top_posts": raw[:300],
            "avg_engagement_rate": 0.0,
            "dominant_themes": "",
            "positioning_gap": "",
            "thread_analysis": "",
        }


def main():
    now = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ).isoformat()

    # 競合投稿DBから未分析の投稿のみ取得
    posts = get_recent_competitor_posts(unanalyzed_only=True)
    if not posts:
        print("[競合分析] 未分析の投稿がありません。終了します。")
        return

    print(f"[競合分析] 未分析投稿数: {len(posts)}件")
    strategy = load_strategy()

    analysis = analyze_with_claude(posts, strategy)
    if not analysis:
        return

    append_competitor_record({
        "competitor_id": "",
        "platform": "threads",
        "top_posts": analysis.get("top_posts", ""),
        "avg_engagement_rate": analysis.get("avg_engagement_rate", 0.0),
        "dominant_themes": analysis.get("dominant_themes", ""),
        "positioning_gap": analysis.get("positioning_gap", ""),
        "thread_analysis": analysis.get("thread_analysis", ""),
        "collected_at": now,
    })
    print("[競合分析] 集計分析を競合分析DBに記録しました")

    # 分析済みフラグを立てる
    row_numbers = [p["_row"] for p in posts]
    mark_competitor_posts_analyzed(row_numbers)


if __name__ == "__main__":
    main()
