"""
投稿生成スクリプト
Claude APIを使ってその日の投稿タイプに応じた草稿を生成し、
ThreadsへAutomatic投稿、投稿内容をSlack通知する
※ LinkedIn は一時無効化中（post_linkedin.py は保持）
"""

import os
import json
import time
import datetime
import anthropic
from preflight import run_all as preflight_check
from post_threads import post_to_threads
# from post_linkedin import post_to_linkedin  # LinkedIn 一時無効化
from notify_slack import notify_slack
from sheets import append_post_record

STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy():
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def determine_post_type(strategy: dict) -> str:
    """ローテーションインデックス（当日の投稿番号）から投稿タイプを決定"""
    rotation = strategy["post_rotation"]
    # 今日が何日目かでローテーション位置を決定
    day_of_year = datetime.date.today().timetuple().tm_yday
    # 1日5投稿 × ローテーション長で循環
    slot = int(os.environ.get("POST_SLOT", "0"))  # 0〜5（時刻ごとに異なるワークフローから渡される）
    index = ((day_of_year - 1) * 5 + slot) % len(rotation)
    return rotation[index]


def build_prompt(strategy: dict, post_type: str) -> str:
    positioning = strategy["positioning"]
    post_info = strategy["post_types"][post_type]
    persona = strategy["persona"]

    # slot 1（11:45 JST）は1日1回のフック形式スロット
    slot = int(os.environ.get("POST_SLOT", "0"))
    use_hook = (slot == 1)

    # 投稿タイプ別の追加ルールと出力フォーマットを設定
    if post_type == "structure":
        if use_hook:
            type_specific_rules = """
【体系化系専用ルール（フック形式）】
- 冒頭でエンジニア・PM・コーチとしての経験や実績を一言で示し（例：「エンジニアとしてキャリアを積んできた私が断言する」「長年プロとして働いてきた経験から言える」など）、具体的な年数は断定せず信頼の根拠を与える
- 「〇〇についての考え方、多くの人が誤解している」「〇割の人が気づいていない」などのつかみを続ける
- 本文の末尾を「残念ながら違います。実は、」「でも、それが正解じゃない。実は〜」のようなクリフハンガーで終え、補足リプライ1で答えを明かす
- 補足リプライ1では「実は〜」の答えを具体的な仕組み・設計ステップ（最低3項目）で開示する
- 「できる人vsできない人」型の対比フォーマットは使わない（競合との差別化）
- 一般大衆向けの啓発トーンではなく、既に高い成果を出している人がさらに上へ行くための専門的トーンで書く
"""
            output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（冒頭：自分の経験・立場を根拠として一言示す→「〇〇の考え方、多くの人が誤解している」→具体的な誤解の中身を示し→最後を「残念ながら違います。実は、」などのクリフハンガーで終える。全体100〜150文字）

【補足リプライ1】
（本文の「実は〜」の答えを明かす。具体的な設計ステップを3〜5項目で説明。60〜120文字）

【補足リプライ2】
（読者への問いかけ、または再現性・持続性を強調する締めの一言。30〜60文字）"""
        else:
            type_specific_rules = """
【体系化系専用ルール】
- 「なぜ意志力に頼ると限界があるか」のロジックを1文で示してから仕組みの話に入る
- 設計プロセス・手順を最低3ステップで具体的に示す（「毎朝〇時に」「〇分以内に」「3段階で」など数字・行動レベルで）
- 「心構え」「マインドセット」「根性」「精神力」だけで終わらない。必ず具体的な行動・環境設計まで落とす
- 「再現性」「設計」「仕組み」「構造」のいずれかを本文に含める
- 「できる人vsできない人」型の対比フォーマットは使わない（競合との差別化）
- 一般大衆向けの啓発トーンではなく、既に高い成果を出している人がさらに上へ行くための専門的トーンで書く
"""
            output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（フックとなる問い・場面・数字で始め、意志力の限界→仕組みが必要な理由を100〜150文字で）

【補足リプライ1】
（具体的な設計ステップを3〜5項目のリスト形式で。60〜120文字）

【補足リプライ2】
（読者への問いかけ、または再現性・持続性を強調する締めの一言。30〜60文字）"""
    elif post_type == "opinion":
        type_specific_rules = """
【業界考察系専用ルール】
- ハイパフォーマー（コンサル・PM・医師・弁護士・スタートアップ創業者）特化の視点で語る。一般向けの啓発トーンは禁止
- 「この人にしか語れない」専門的・具体的視点を最低1つ入れる（HRV・判断コスト・認知負荷・行動設計・環境設計などの専門概念を活用）
- 感情論・精神論で終わらず、構造的な理由・設計視点で締める
- 「できる人vsできない人」型の対比フォーマットは使わない（競合との差別化）
"""
        output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（ここに本文）

【補足リプライ】
（ここに補足リプライ）"""
    else:
        type_specific_rules = ""
        output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（ここに本文）

【補足リプライ】
（ここに補足リプライ）"""

    return f"""あなたはSNSコンテンツライターです。
以下の戦略に基づいて、日本語のThreads投稿文と補足セルフリプライを生成してください。

【ポジショニング】
- ポジション：{positioning["position"]}
- コンセプト：{positioning["concept"]}
- 差別化軸：{positioning["differentiation"]}
- 想起ワード：{positioning["tagline"]}
- ステートメント：{positioning["statement"]}

【ターゲット】
{persona["description"]}
悩み：{', '.join(persona["pain_points"])}

【今回の投稿タイプ】
{post_info["label"]}（{post_info["description"]}）
{type_specific_rules}
【共通ルール】
- 本文：100〜180文字程度（Threadsのカジュアル・会話的なトーンで）
- 語尾は断定的・力強くするが、毎回断定で終わらず、体験談・問いかけ・数値で締める投稿も交える
- 冒頭で目を引くフックを入れる
- 具体的な行動や言葉を使う
- 自慢に見える表現は避け、共感・学び・プロセスとして語る
- 「ハイパフォーマー」「プロフェッショナル」「コンサル」「PM」「医師」「弁護士」など職種は適宜使ってよい
- 「否定→転換→正解提示」の三段構成を毎回繰り返さないこと（構成にバリエーションを持たせる）
- 冒頭を「〇〇な人は△△だと思ってる。違う。」の否定型パターンで始めないこと（既視感を生む）
- ①②③の箇条書き構造を毎回同じ形式で使わない（問いかけ→列挙→結論、場面描写→原因→解決策など構成を変える）
- 「もう終わり」「〜からしか生まれない」など同じ締めフレーズを使い回さないこと
- 冒頭は抽象的な主語（「〇〇な人は」）より具体的な場面・体験・数字から入ることを優先する
- 対話系の場合は、質問する前に必ず自分の考えや経験を先に開示すること
- 自分自身の具体的なエピソード・数字・変化を盛り込み、設計過程の透明性を出す（例：「木曜の午後が毎週キツかったが、判断を3つ減らしただけで金曜まで持つようになった」）
- 「〇〇をする」の形で動作を書く場合は、何を対象にするかが明確に伝わる目的語を必ず含めること（「設計する」ではなく「判断の優先順位を設計する」、「仕組みをつくる」ではなく「退勤後の切り替え手順を仕組み化する」など）
- 事実でない個人的体験を書かない。自分に子どもがいる・家族の具体エピソードなど実際と異なる内容はNG。クライアントや読者の例を使う場合は「クライアントの〇〇さんが」「よくある話で」などの形にすること
- 自分の普通さ・限界を表現する際は「IQ高くない」「頭が悪い」のようなマイナス語を使わず、「天才ではない」「特別な才能があるわけではない」のように「{プラスに捉えられる言葉}ではない」の形で表現すること

{output_format}"""


def _parse_post(raw: str) -> dict:
    """生成テキストを本文・補足リプライ1・補足リプライ2にパース。
    体系化系（structure）は3投稿構成のため補足リプライ2まで対応。"""
    content = ""
    self_reply = ""
    self_reply2 = ""

    if "【補足リプライ1】" in raw and "【補足リプライ2】" in raw:
        # 3投稿構成（structure用）
        parts_r2 = raw.split("【補足リプライ2】")
        self_reply2 = parts_r2[1].strip()
        parts_r1 = parts_r2[0].split("【補足リプライ1】")
        content = parts_r1[0].replace("【本文】", "").strip()
        self_reply = parts_r1[1].strip()
    elif "【本文】" in raw and "【補足リプライ】" in raw:
        # 2投稿構成（通常）
        parts = raw.split("【補足リプライ】")
        content = parts[0].replace("【本文】", "").strip()
        self_reply = parts[1].strip()
    else:
        content = raw

    return {"content": content, "self_reply": self_reply, "self_reply2": self_reply2}


def generate_post(post_type: str, strategy: dict) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Threads用コンテンツ生成（structure は3投稿構成のためmax_tokens拡張）
    threads_prompt = build_prompt(strategy, post_type)
    max_tokens = 800 if post_type == "structure" else 512
    threads_message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": threads_prompt}],
    )
    threads_result = _parse_post(threads_message.content[0].text.strip())

    return {
        "content": threads_result["content"],
        "self_reply": threads_result["self_reply"],
        "self_reply2": threads_result.get("self_reply2", ""),
    }


def main():
    # Claude API呼び出し前に外部サービスの接続確認
    preflight_check()

    strategy = load_strategy()
    post_type = determine_post_type(strategy)
    result = generate_post(post_type, strategy)
    content = result["content"]
    self_reply = result["self_reply"]
    self_reply2 = result.get("self_reply2", "")

    print(f"[生成完了] タイプ: {post_type}")
    print(f"[Threads本文]\n{content}\n")
    if self_reply:
        print(f"[Threads補足リプライ1]\n{self_reply}\n")
    if self_reply2:
        print(f"[Threads補足リプライ2]\n{self_reply2}\n")

    # Threads へ自動投稿
    threads_id = post_to_threads(content)
    # linkedin_id = post_to_linkedin(content)  # LinkedIn 一時無効化

    # セルフリプライ1投稿（投稿成功かつ補足リプライがある場合）
    reply_id = None
    if threads_id and self_reply:
        time.sleep(5)  # 本文投稿がThreads側で処理されるのを待つ
        reply_id = post_to_threads(self_reply, reply_to_id=threads_id)
        if reply_id:
            print(f"[Threads] セルフリプライ1投稿成功: {reply_id}")

    # セルフリプライ2投稿（structure 3投稿構成）
    if reply_id and self_reply2:
        time.sleep(5)  # セルフリプライ1がThreads側で処理されるのを待つ
        reply2_id = post_to_threads(self_reply2, reply_to_id=reply_id)
        if reply2_id:
            print(f"[Threads] セルフリプライ2投稿成功: {reply2_id}")

    # Threadsへの投稿内容をSlack通知
    slack_content = content
    if self_reply:
        slack_content += f"\n\n↩️ セルフリプライ1：{self_reply}"
    if self_reply2:
        slack_content += f"\n\n↩️ セルフリプライ2：{self_reply2}"
    notify_slack(slack_content, post_type)

    # 投稿DBに記録
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    week_number = now.isocalendar()[1]
    records = []
    if threads_id:
        records.append({
            "post_id": threads_id,
            "platform": "threads",
            "post_type": post_type,
            "content": content,
            "posted_at": now.isoformat(),
            "week_number": week_number,
        })
    # if linkedin_id:  # LinkedIn 一時無効化
    #     records.append({
    #         "post_id": linkedin_id,
    #         "platform": "linkedin",
    #         "post_type": post_type,
    #         "content": content,
    #         "posted_at": now.isoformat(),
    #         "week_number": week_number,
    #     })
    for record in records:
        append_post_record(record)

    print("[完了] 投稿・通知・DB記録が完了しました")


if __name__ == "__main__":
    main()
