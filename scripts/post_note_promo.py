"""
note誘導用Threads投稿スクリプト（3日に1回 20:00 JST）
当日生成した無料note記事を読みたくさせる目的で、3投稿構成のスレッドを配信する。

- 本文        : note記事を読みたくさせる爬虫類脳直撃のフック（ベネフィット or ペイン提示）
- 補足リプライ1: フックを一段深掘りし、URL を踏ませる動機を最大化する続きの一手
- 補足リプライ2: noteのURLのみ（Claudeを通さず、note投稿DBの url 列をそのまま貼る）

実行頻度:
- ワークフローは毎日 20:00 JST に起動するが、本スクリプトは date.toordinal() % 3 == 0 の日のみ
  実投稿を行う。月末/年末を跨いでも常に3日間隔を維持するため、cron の `*/3` ではなく
  通日ベースの剰余で制御する。

スキップ条件（いずれかに該当したら投稿せず終了）:
- 当日が3日サイクルの実行日でない（Slack通知なし、ログのみ）
- 当日の output/notes/YYYY-MM-DD_free.md が存在しない（Slack通知あり）
- note投稿DBの当日 free レコードの url 列が空 / 該当行が存在しない（Slack通知あり）

通常の投稿フローに倣い preflight → 生成 → Threads投稿 → Slack通知 → 投稿DB記録 の順で処理する。
本スクリプトの本文・補足リプライ1のスタイルは generate_post.py のルールを継承せず、
note誘導専用の独立したフック設計ルールに従う。
"""

import os
import time
import datetime
import anthropic

from preflight import run_all as preflight_check
from token_cost import log_token_cost
from post_threads import post_to_threads
from notify_slack import notify_slack, notify_slack_note_promo_skip
from sheets import append_post_record, get_note_url_by_date
from generate_post import load_strategy


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(SCRIPT_DIR, "../output/notes")
POST_TYPE = "note_promo"


def _today_jst() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def _load_today_note(date_str: str) -> str | None:
    """当日の無料note Markdown を読み込む。存在しなければ None。"""
    path = os.path.join(NOTES_DIR, f"{date_str}_free.md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_prompt(strategy: dict, note_markdown: str) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]

    return f"""あなたはSNSコンテンツライターです。
以下のnote記事を「絶対に読みたくさせる」ためのThreads誘導スレッドを日本語で生成してください。
本文（ルート）と補足リプライ1の2投稿だけを書きます。URLを貼る補足リプライ2は別途付与するため出力不要です。

【ポジショニング（語り手のトーン）】
- ポジション：{positioning["position"]}
- コンセプト：{positioning["concept"]}
- ステートメント：{positioning["statement"]}

【ターゲット読者（このnoteを読ませたい相手）】
{persona["description"]}
ペイン：{', '.join(persona["pain_points"])}

【誘導対象のnote記事（本文Markdown全文）】
---
{note_markdown}
---

【このスレッド限定のフック設計ルール】
- 目的は単一：本文と補足リプライ1を読み終えた瞬間に「URLを今すぐ踏みたい」と感じさせる
- 爬虫類脳（生存・損失・即時報酬・社会的地位）に直撃する切り口を選ぶ：
  - 損失回避：このまま続けると失う具体的なもの（家族時間／成果／健康／信用）を可視化する
  - ベネフィット：noteを読むと得られる即効性のある変化を、抽象語ではなく具体的に描く
  - 内的承認：「自分だけが知らないのでは」という社会的取り残し感に静かに触れる
- 本文は必ずnote記事の核となる「変化のbefore→after」または「読まないと損する具体的事実」のどちらか1点に絞る
- note記事に書かれていない事実・数字・概念を勝手に作らない（記事の射程内で表現を尖らせる）
- 「詳しくはこちら」「下記リンクから」のような誘導定型句は禁止。読み手の内的衝動として自然にURLへ進ませる
- 「ぜひ読んでください」「気づきがあれば」のようなお願い／お礼ベースの締めは禁止
- 否定型フック（「〇〇な人は△△だと思ってる。違う。」）や「能力じゃなく設計」のような決めフレーズは使わない
- 「IQが低い」「頭が悪い」のようなマイナス語での自己卑下は禁止。「天才ではない」「特別な才能はない」のように {{プラス語}}ではない の形で書く
- 数字は「30分以上／週3時間程度」のようにキリの良い値＋「以上／程度／前後」で丸める
- 子育ての具体エピソードは自分の体験として書かず、知り合い・クライアントの事例として扱う（夫婦の話は自分の体験で可）
- 句読点・文末のリズムは整えすぎない。「〜なんだけど」「〜ですよね」のような揺らしを1〜2箇所混ぜてAI生成感を消す

【出力要件】
- 本文（ルート）：1行・20〜40字。記事の核を1点に絞った爬虫類脳直撃のフック。読み手の手を完全に止める一言
- 補足リプライ1：120〜200字。本文のフックを一段深く掘り下げ、「この続きはnoteに書いてある」と読者が自分の意思で踏みに行く心理状態をつくる。最後の一文は問いかけ・余韻・短い宣言のいずれかで終え、URLへ自然に視線が落ちる流れを作る。URL自体は書かない

以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（本文の1行）

【補足リプライ1】
（補足リプライ1の本文）"""


def _parse(raw: str) -> dict:
    """Claudeの出力を 本文 / 補足リプライ1 にパースする。"""
    content = ""
    self_reply = ""
    if "【本文】" in raw and "【補足リプライ1】" in raw:
        parts = raw.split("【補足リプライ1】")
        content = parts[0].replace("【本文】", "").strip()
        self_reply = parts[1].strip()
    else:
        content = raw.strip()
    return {"content": content, "self_reply": self_reply}


def _generate_hook(strategy: dict, note_markdown: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = _build_prompt(strategy, note_markdown)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    log_token_cost("claude-opus-4-6", message.usage, "post_note_promo")
    return _parse(message.content[0].text.strip())


def main() -> None:
    now = _today_jst()
    date_str = now.date().isoformat()

    # 0) 3日に1回の頻度制御（通日ordinalの剰余で月跨ぎも一定間隔を維持）
    if now.date().toordinal() % 3 != 0:
        print(f"[note_promo] {date_str} は3日サイクルの実行日ではありません。スキップします。")
        return

    # 1) 当日のnote原稿チェック（無ければスキップ通知して終了）
    note_markdown = _load_today_note(date_str)
    if not note_markdown:
        print(f"[note_promo] 当日 ({date_str}) の無料note原稿が見つかりません。スキップします。")
        notify_slack_note_promo_skip(
            reason=f"output/notes/{date_str}_free.md が存在しません。note原稿生成（07:00 JST）の成否を確認してください。",
            date_str=date_str,
        )
        return

    # 2) note投稿DBから当日free記事のURLを取得（preflightより前にSheets疎通する形だが、
    #    URL不在で20:00時点に落ちる確率が高いケースを Claude API 課金前に弾くのが目的）
    note_url = get_note_url_by_date(date_str, mode="free")
    if not note_url:
        print(f"[note_promo] note投稿DBに当日 ({date_str}) free 記事のURLが未入力です。スキップします。")
        notify_slack_note_promo_skip(
            reason="note投稿DBの当日free記事に url が入力されていません。note.comに投稿後、url 列を埋めてください。",
            date_str=date_str,
        )
        return

    # 3) Claude API 呼び出し前に外部サービス疎通確認
    preflight_check()

    # 4) フック生成（本文 + 補足リプライ1）
    strategy = load_strategy()
    parsed = _generate_hook(strategy, note_markdown)
    content = parsed["content"]
    self_reply = parsed["self_reply"]
    if not content or not self_reply:
        raise SystemExit("[note_promo] Claudeの出力パースに失敗しました（本文 or 補足リプライ1が空）")

    self_reply2 = note_url  # URL単独リプライ

    print(f"[note_promo] 投稿対象日付: {date_str} (JST)")
    print(f"[Threads本文]\n{content}\n")
    print(f"[Threads補足リプライ1]\n{self_reply}\n")
    print(f"[Threads補足リプライ2]\n{self_reply2}\n")

    # 5) Threads へ3段階投稿
    threads_id = post_to_threads(content)
    reply_id = None
    reply2_id = None
    if threads_id and self_reply:
        time.sleep(5)  # 本文コンテナの処理完了を待つ
        reply_id = post_to_threads(self_reply, reply_to_id=threads_id)
        if reply_id:
            print(f"[Threads] セルフリプライ1投稿成功: {reply_id}")

    if reply_id:
        time.sleep(5)  # セルフリプライ1のコンテナ処理完了を待つ
        reply2_id = post_to_threads(self_reply2, reply_to_id=reply_id)
        if reply2_id:
            print(f"[Threads] セルフリプライ2投稿成功: {reply2_id}")

    # 6) Slack通知（完了系・メンションなし）
    slack_content = (
        f"{content}\n\n"
        f"↩️ セルフリプライ1：{self_reply}\n\n"
        f"↩️ セルフリプライ2：{self_reply2}"
    )
    notify_slack(slack_content, POST_TYPE, title="note誘導Threads投稿完了")

    # 7) 投稿DB記録（ルート + セルフリプライをすべて記録、メトリクス収集対象にする）
    posted_at_iso = now.isoformat()
    week_number = now.isocalendar()[1]
    if threads_id:
        append_post_record({
            "post_id": threads_id,
            "platform": "threads",
            "post_type": POST_TYPE,
            "content": content,
            "posted_at": posted_at_iso,
            "week_number": week_number,
            "parent_post_id": "",
        })
    if reply_id:
        append_post_record({
            "post_id": reply_id,
            "platform": "threads",
            "post_type": POST_TYPE,
            "content": self_reply,
            "posted_at": posted_at_iso,
            "week_number": week_number,
            "parent_post_id": threads_id,
        })
    if reply2_id:
        append_post_record({
            "post_id": reply2_id,
            "platform": "threads",
            "post_type": POST_TYPE,
            "content": self_reply2,
            "posted_at": posted_at_iso,
            "week_number": week_number,
            "parent_post_id": threads_id,
        })

    print("[note_promo] 投稿・通知・DB記録が完了しました")


if __name__ == "__main__":
    main()
