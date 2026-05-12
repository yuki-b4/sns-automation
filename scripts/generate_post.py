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
from token_cost import log_token_cost
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
    funnel = strategy["funnel"]
    midend = positioning["midend_product"]
    backend = positioning["backend_product"]

    # slot 1（11:45 JST）は1日1回のフック形式スロット
    slot = int(os.environ.get("POST_SLOT", "0"))
    use_hook = (slot == 1)

    # 投稿タイプ別の追加ルールと出力フォーマットを設定
    if post_type == "structure":
        if use_hook:
            type_specific_rules = """
【体系化系専用ルール（フック形式）】
- 本文（ルート）は1行・20〜40字に絞り、「多くの人が誤解している〇〇」「〇割の人が気づいていない〇〇」などの意外性のある一言＋「残念ながら違います。実は、」のようなクリフハンガーで終える。経験・立場の前置きはルートに入れず補足リプライ1の冒頭に回す
- 補足リプライ1の冒頭で、コーチ／既婚男性／異色キャリアの当事者としての立場を一言で示す（例：「コーチとして人間関係の内側に向き合ってきた経験から言える」「妻との関係を築き続けてきた立場として言える」）。具体的な年数は断定せず信頼の根拠として使う
- 続けて補足リプライ1で「実は〜」の答えを具体的なあなたの内側のパターンと書き換えステップ（最低3項目）で開示する
- 読者を優劣で分類する対比フォーマット（『モテる人vsモテない人』等）は使わない（競合との差別化）
- 一般大衆向けの啓発トーンではなく、心理学・脳科学を本で学んできた読者がさらに自分の問題に応用できる専門的トーンで書く
- 補足リプライ2の締めで、より深く知りたい読者向けに「プロフィールのnoteで更に詳しく書いている」旨を自然に入れてよい（投稿だけで完結する内容の場合は無理に入れず省略）。毎回異なる表現にし、固定フレーズ化を避ける
"""
            output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（1行・20〜40字。意外性のある一言＋クリフハンガーに絞る。例：「多くの人が誤解している〇〇——残念ながら違います。実は、」。経験根拠はここに入れない）

【補足リプライ1】
（冒頭で経験・立場の信頼根拠を一言→「実は〜」の答え→具体的なあなたの内側のパターンと書き換えステップ3〜5項目。150〜250字）

【補足リプライ2】
（読者への問いかけ、または再現性・持続性を強調する締めの一言。30〜60字）"""
        else:
            type_specific_rules = """
【体系化系専用ルール】
- 本文（ルート）は1行・20〜40字の意外性のある問題提起／常識を覆す一言で読者の手を止める
- 補足リプライ1で「なぜ自分が苦しくなるパターンが繰り返されるか」のロジック→あなたの内側のパターンと書き換えプロセスを最低3ステップで具体的に示す（「毎日〇分」「〇回」「3段階で」など数字・行動レベルで）
- 「気持ちを切り替える」「前向きに」「マインドセット」のような抽象的精神論だけで終わらない。具体的なあなたの内側のパターン認識・言語化・書き換え方法まで落とす
- 「再現性」「マインド」「あなたの内側のパターン」「言語化」「書き換え」のいずれかを補足リプライ1に含める
- 読者を優劣で分類する対比フォーマット（『モテる人vsモテない人』等）は使わない（競合との差別化）
- 一般大衆向けの啓発トーンではなく、心理学・脳科学を本で学んできた読者がさらに自分の問題に応用できる専門的トーンで書く
- 補足リプライ2の締めで、より深く知りたい読者向けに「プロフィールのnoteで更に詳しく書いている」旨を自然に入れてよい（投稿だけで完結する内容の場合は無理に入れず省略）。毎回異なる表現にし、固定フレーズ化を避ける
"""
            output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（1行・20〜40字。意外性のある問題提起／常識を覆す一言。読者の手を止めるフックに徹する）

【補足リプライ1】
（なぜ繰り返すか→あなたの内側のパターン→具体的な書き換えステップ3〜5項目。150〜250字）

【補足リプライ2】
（読者への問いかけ、または再現性・持続性を強調する締めの一言。30〜60字）"""
    elif post_type == "opinion":
        type_specific_rules = """
【業界考察系専用ルール】
- 本文（ルート）は1行・20〜40字の意外性／逆説で読者の手を止める。詳細な考察は補足リプライで展開する
- 補足リプライでは独身ITエンジニア・PM男性特化の視点で語る。一般向けの啓発トーンは禁止
- 「この人にしか語れない」専門的・具体的視点を最低1つ入れる（回避型・不安型・愛着理論・自己効力感・内側のパターンなどの心理学・脳科学概念を活用）
- 感情論・精神論で終わらず、構造的な理由・あなたの内側のパターンの視点で締める
- 読者を優劣で分類する対比フォーマット（『モテる人vsモテない人』等）は使わない（競合との差別化）
"""
        output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（1行・20〜40字。意外性のある一言／逆説で読者の手を止める）

【補足リプライ】
（150〜250字。独身ITエンジニア・PM男性特化の視点で構造的理由・あなたの内側のパターンの視点を展開）"""
    elif post_type == "permission":
        type_specific_rules = """
【許可系専用ルール】
- 補足リプライの末尾で、投稿内容と地続きの流れで「プロフィールのnoteに置いている」旨を1行で添える。基本的に入れるが、投稿が単体で完結しすぎていて不自然になる場合は省略可。毎回異なる表現にし、固定フレーズ化を避ける（例：「同じパターンに気づいた経緯、プロフィールのnoteに置いています」「自分の内側を見つめ直す具体的な手順はプロフィールのnoteで」）。広告臭のある「詳しくはこちら」的な誘導はNG
"""
        output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（1行・20〜40字。意外性のある問題提起／常識を覆す一言。詳細・体験談・エピソードはここに入れず補足リプライで展開する）

【補足リプライ】
（150〜250字。本文の一言に対する具体的な場面・体験・気づき・根拠を共感的に展開。末尾で投稿内容と地続きの流れでプロフィールのnoteへ1行の誘導を添える）"""
    else:
        type_specific_rules = ""
        output_format = """以下の形式で出力してください（他の説明・前置き不要）：

【本文】
（1行・20〜40字。意外性のある問題提起／常識を覆す一言。詳細・体験談・エピソードはここに入れず補足リプライで展開する）

【補足リプライ】
（150〜250字。本文の一言に対する具体的な場面・体験・気づき・根拠を共感的に展開）"""

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

【発信者】
- 立ち位置：{positioning["speaker"]}
- credibility（一次経験ソース）：
{chr(10).join(f"  - {c}" for c in positioning["credibility"])}
- 差別化メソッド：{positioning["differentiation"]}

【読者の到達点（ToBe）】{positioning["tobe"]}
【ToBeを阻む構造】{positioning["tobe_barrier"]}

【商品ラダー＋ファネル】
- ミドルエンド：{midend["title"]}（¥{midend["price_min"]}〜{midend["price_max"]}）／{funnel["midend_role"]}
- バックエンド：{backend["title"]}（¥{backend["price"]}）／{funnel["backend_path"]}
- SNS担当範囲：{funnel["sns_role"]}

【ターゲット】
{persona["description"]}
悩み：{', '.join(persona["pain_points"])}

【今回の投稿タイプ】
{post_info["label"]}（{post_info["description"]}）
ファネル段階：{post_info["funnel_stage"]}
{type_specific_rules}
【共通ルール】
- ルート（本文）：1行・20〜40字に圧縮し、意外性のある問題提起または常識を覆す一言に絞る。読者の手を止めて続きを読ませるフックに徹する（競合分析より、ルート短文＋リプライ1詳細展開の2投稿構成が最高ERを記録）
- 詳細解説・設計ステップ・体験談・根拠は必ずリプライ1以降で150〜250字のボリュームで共感的に展開する。ルートで立てた"引き"に必ず応える
- 語尾は断定で力強く。ただし毎回断定で終わらず、体験談・問いかけ・数値で締める形も交える
- ルートは具体的な行動・言葉を使って目を引くフックにする
- 自慢に見える表現は避け、共感・学び・プロセスとして語る
- ITエンジニア・PMなどペルソナの所属領域・職種・職場場面（評価面談・1on1・コードレビュー・リリース等）は文脈に応じて使ってよい
- 冒頭は「〇〇な人は△△だと思ってる。違う。」のような否定型パターンや抽象主語で始めず、具体的な場面・体験・数字から入る。「否定→転換→正解提示」の三段構成も毎回繰り返さない
- 本文（ルート）で具体的な場面・体験・行動を描いた場合、そのあとに「——」や「実は」「つまり」「それは」「それ、〇〇ない」などの接続で自分の解釈・結論・オチを付け足さない。場面そのもので手を止めさせ、意味の受け取りは読者に委ねる（余白を残す）。悪い例：「彼女の前でスマホを握ってた——それ、心ここにあらず」→ 良い例：「彼女の前でスマホを握ってた」で止める
- ①②③の箇条書き構造を毎回同じ形式で使わない（問いかけ→列挙→結論、場面描写→原因→解決策など構成を変える）
- 「もう終わり」「〜からしか生まれない」等の締めフレーズや、「テクニックの問題じゃなくてマインドの問題」「外側じゃなく内側」のような決めフレーズを標語化して複数投稿で繰り返さない。同じ趣旨も一人称の実感ベースで毎回違う言い回しに書き換える
- 「〜だった」「ようやく気づいた」「〜けっこう衝撃だった」のような余韻系のエピソード締めフレーズや、「場面描写→気づき→教訓」の3段オチ構造を連続投稿で繰り返さない
- 句読点・文末のリズムを整えすぎない。「〜なんだけど」「〜ですよね」のような文末の揺らし、言い淀み・訂正・具体的な固有名詞（場所・場面・固有名）を1投稿に1〜2箇所混ぜてよい。AI生成的な均質さよりも、一人称の生っぽさを優先する
- 対話系の場合は、質問する前に自分の考えや経験を先に開示すること
- 自分のエピソード・数字・変化を盛り込み内面と向き合う過程の透明性を出す（例：「自分が苦しくなる場面を1週間メモしただけで、同じパターンに気づけた」）
- 数値は「30分以上／週3時間程度」のようにキリの良い値＋「以上／程度／前後」で表現。端数の具体値（48分・23%等）はAI生成感が出るのでNG
- 動作を書く際は対象が伝わる目的語を含める（「言語化する」ではなく「苦しくなる場面のパターンを言語化する」など）
- クライアント・知り合いの恋愛・関係性の事例を引く際は明示的に区別する（「クライアントの〇〇さん」「知り合いに〇〇な人がいて」のように示す）。夫婦（妻との関係）や自身の過去の恋愛経験は自分の体験として直接語ってよい
- 自分の普通さ・限界は「自分はダメ」「変われない」「臆病者」等のマイナス語で表現せず、「完璧な人間ではない」「特別な何かを持っているわけではない」のように「{{プラス語}}ではない」の形で書く
- 「内側のパターン」という語は無修飾で使わず、必ず読者所有を明示する修飾語を伴うこと。author voice（著者→読者）の場面は「あなたの内側のパターン」、reader voice（読者の内面・気づきを描写する場面）は「自分の内側のパターン」を使い分ける（修飾語なしの単独使用は『誰の』パターンか曖昧になり、読者の自分事化が弱まる）
- 研究結果・統計数値の引用（例：「〇〇の研究では」「人口の約25%が」「〜と報告されている」）や著者名・年・媒体名・URLは本文に入れない。背景知識を使う場合は、断定的な一般論または体験ベースで言い切る。回避型・愛着理論などの概念名そのものは（既存ルールどおり）使ってよいが、研究エビデンスとしてのフレーミングは避ける。詳細出典の補完はnote等の長文媒体で行う
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
    log_token_cost("claude-opus-4-6", threads_message.usage, "generate_post")
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
    reply2_id = None
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

    # 投稿DBに記録（ルート + セルフリプライをすべて記録し、メトリクス収集対象にする）
    # parent_post_id にはスレッドのルート post_id（=threads_id）を入れる。
    # セルフリプライ2はThreads側ではセルフリプライ1への返信だが、データ管理上の
    # 「どのスレッドの返信か」はルートで揃える方が分析しやすいためルートIDを採用。
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
            "parent_post_id": "",
        })
    if reply_id:
        records.append({
            "post_id": reply_id,
            "platform": "threads",
            "post_type": post_type,
            "content": self_reply,
            "posted_at": now.isoformat(),
            "week_number": week_number,
            "parent_post_id": threads_id,
        })
    if reply2_id:
        records.append({
            "post_id": reply2_id,
            "platform": "threads",
            "post_type": post_type,
            "content": self_reply2,
            "posted_at": now.isoformat(),
            "week_number": week_number,
            "parent_post_id": threads_id,
        })
    # if linkedin_id:  # LinkedIn 一時無効化
    #     records.append({
    #         "post_id": linkedin_id,
    #         "platform": "linkedin",
    #         "post_type": post_type,
    #         "content": content,
    #         "posted_at": now.isoformat(),
    #         "week_number": week_number,
    #         "parent_post_id": "",
    #     })
    for record in records:
        append_post_record(record)

    print("[完了] 投稿・通知・DB記録が完了しました")


if __name__ == "__main__":
    main()
