"""
投稿生成スクリプト
Claude APIを使ってその日の投稿タイプに応じた草稿を生成し、
ThreadsへAutomatic投稿、投稿内容をSlack通知する
※ LinkedIn は一時無効化中（post_linkedin.py は保持）
"""

import os
import re
import json
import time
import datetime
import anthropic
from preflight import run_all as preflight_check
from post_threads import post_to_threads
# from post_linkedin import post_to_linkedin  # LinkedIn 一時無効化
from notify_slack import notify_slack, notify_slack_duplicate_warning
from sheets import append_post_record, get_recent_posts_content

SIMILARITY_THRESHOLD = 0.25

STRATEGY_PATH = os.path.join(os.path.dirname(__file__), "../config/strategy.json")


def load_strategy():
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _jaccard_trigram_similarity(text_a: str, text_b: str) -> float:
    """文字トライグラムのJaccard類似度を計算（Claude API不使用）"""
    def trigrams(text):
        cleaned = re.sub(r'[\s\u3000「」『』【】。、！？…・\-\(\)（）""]', '', text)
        return set(cleaned[i:i+3] for i in range(max(0, len(cleaned) - 2)))
    a, b = trigrams(text_a), trigrams(text_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def determine_post_type(strategy: dict) -> str:
    """ローテーションインデックス（当日の投稿番号）から投稿タイプを決定"""
    rotation = strategy["post_rotation"]
    # 今日が何日目かでローテーション位置を決定
    day_of_year = datetime.date.today().timetuple().tm_yday
    # 1日5投稿 × ローテーション長で循環
    slot = int(os.environ.get("POST_SLOT", "0"))  # 0〜5（時刻ごとに異なるワークフローから渡される）
    index = ((day_of_year - 1) * 5 + slot) % len(rotation)
    return rotation[index]


def build_prompt(strategy: dict, post_type: str, recent_posts: list[dict] | None = None) -> str:
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
- 冒頭でエンジニア・PM・コーチとしての経験や実績を一言で示し（例：「長年エンジニアとしてのキャリアを歩んできた私が断言する」「長年プロの開発者（エンジニア）として現場を見てきた経験から言える」など）、具体的な年数は断定せず信頼の根拠を与える
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
- 「心構え」「マインドセット」「根性」「精神力」だけで終わらない。具体的な行動・環境設計まで落とす
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

    recent_posts_section = ""
    if recent_posts:
        snippets = []
        for p in recent_posts[-20:]:
            date_str = p.get("posted_at", "")[:10]
            snippet = p["content"][:50]
            snippets.append(f"- {snippet}（{date_str}）")
        recent_posts_section = "\n【直近の投稿（これらと同じエピソード・表現・話題は絶対に使わないこと）】\n" + "\n".join(snippets) + "\n"

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
- 語尾は断定で力強く。ただし毎回断定で終わらず、体験談・問いかけ・数値で締める形も交える
- 冒頭で目を引くフックを入れ、具体的な行動や言葉を使う
- 自慢に見える表現は避け、共感・学び・プロセスとして語る
- 「ハイパフォーマー」「コンサル」「PM」「医師」「弁護士」など職種は適宜使ってよい
- 冒頭は「〇〇な人は△△だと思ってる。違う。」のような否定型パターンや抽象主語で始めず、具体的な場面・体験・数字から入る。「否定→転換→正解提示」の三段構成も毎回繰り返さない
- ①②③の箇条書き構造を毎回同じ形式で使わない（問いかけ→列挙→結論、場面描写→原因→解決策など構成を変える）
- 「もう終わり」「〜からしか生まれない」等の締めフレーズや、「能力の問題じゃなくて設計の問題」「才能じゃなく設計」のような決めフレーズを標語化して複数投稿で繰り返さない。同じ趣旨も一人称の実感ベースで毎回違う言い回しに書き換える
- 対話系の場合は、質問する前に自分の考えや経験を先に開示すること
- 自分のエピソード・数字・変化を盛り込み設計過程の透明性を出す（例：「判断を3つ減らしただけで金曜まで持つようになった」）
- 数値は「30分以上／週3時間程度」のようにキリの良い値＋「以上／程度／前後」で表現。端数の具体値（48分・23%等）はAI生成感が出るのでNG
- 動作を書く際は対象が伝わる目的語を含める（「設計する」ではなく「判断の優先順位を設計する」など）
- 事実でない個人体験はNG（子どもがいる・家族具体エピソードなど）。クライアントや読者の例は「クライアントの〇〇さんが」「よくある話で」の形で書く
- 自分の普通さ・限界は「IQ高くない」「頭が悪い」等のマイナス語で表現せず、「天才ではない」「特別な才能があるわけではない」のように「{{プラス語}}ではない」の形で書く
{recent_posts_section}
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


def generate_post(post_type: str, strategy: dict, recent_posts: list[dict] | None = None) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Threads用コンテンツ生成（structure は3投稿構成のためmax_tokens拡張）
    threads_prompt = build_prompt(strategy, post_type, recent_posts)
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

    # 直近投稿を取得し、同じpost_typeのみ絞り込む（プロンプト注入 + 類似チェック用）
    all_recent_posts = get_recent_posts_content(days=14)
    recent_posts = [p for p in all_recent_posts if p.get("post_type") == post_type]

    result = generate_post(post_type, strategy, recent_posts)
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

    # 類似度チェック（Claude API不使用、文字トライグラムJaccard）
    if recent_posts:
        most_similar = max(recent_posts, key=lambda r: _jaccard_trigram_similarity(content, r["content"]))
        score = _jaccard_trigram_similarity(content, most_similar["content"])
        print(f"[類似度チェック] 最高類似度: {score:.2f}（閾値: {SIMILARITY_THRESHOLD}）")
        if score >= SIMILARITY_THRESHOLD:
            notify_slack_duplicate_warning(content, most_similar["content"], score, most_similar.get("posted_at", ""))

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
