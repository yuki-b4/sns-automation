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
- ルートは1行20〜40字。「多くの人が誤解している〇〇」「〇割が気づいていない〇〇」等の意外性ある一言＋「残念ながら、違います。」「本当の理由は別にあります。」で答えを伏せて終える。経験・立場の前置きはルートに入れず補足リプライ1冒頭へ
- 補足リプライ1冒頭でコーチ／既婚男性の立場を一言（例「コーチとして多くの夫婦の場面に立ち会ってきた立場から」）。年数は断定せず信頼の根拠に
- 続けて伏せた答えを開示し、「何をやめて何を始めるか」の具体行動に落とす。行動は「まずは〇〇から始めてみて。」の1アクションに絞る。ただし型Gで件数を予告した場合のみ、予告した件数どおりに並べる（予告なしに手順を長く列挙しない）
- そのアクションに添えて自責解除／手放し許可のトーン（例「気づけたなら、もう自分を責めなくていい」）を1箇所だけ。固定フレーズ化せず毎回変える
- 理屈の解説で終わらせず、読んだ人が今日から動ける状態にして終える"""
            output_format = """以下の形式で出力（説明・前置き不要）：

【本文】
（型Aまたは型E。20〜40字。意外性ある一言＋クリフハンガー、または答えを示さない問い。経験根拠は入れない）

【補足リプライ1】
（経験・立場の信頼根拠を一言→伏せた答え→思考・行動のクセ→変える最初の一歩「まずは〇〇から始めてみて。」の1アクション。150〜250字）

【補足リプライ2】
（続きはnoteに書く旨を伝えフォロー誘導。例「この続きはnoteに書こうと思うので、フォローしてお待ちください。」広告臭なし・毎回別の言い回し。30〜60字）"""
        else:
            type_specific_rules = """
【体系化系専用ルール】
- ルートは1行20〜40字。意外性ある問題提起／常識を覆す一言で手を止める
- 補足リプライ1で「なぜそのパターンが繰り返されるか」の理由を1〜2文で押さえ、そこから「何をやめて何を始めるか」の具体行動に落とす。行動は「まずは〇〇から始めてみて。」の1アクションに絞る。ただし型Gで件数を予告した場合のみ、予告した件数どおりに並べる（予告なしに手順を長く列挙しない）
- 「気持ちを切り替える」「前向きに」「マインドセット」等の抽象的精神論で終わらせず、いつ・何を・どうするかが分かる行動レベルまで落とす（「〇分」「〇回」等の具体性は残す）
- そのアクションに添えて自責解除／手放し許可のトーン（例「気づけたなら、もう自分を責めなくていい」）を1箇所だけ。固定フレーズ化せず毎回変える
- 理屈の解説で終わらせず、読んだ人が今日から動ける状態にして終える"""
            output_format = """以下の形式で出力（説明・前置き不要）：

【本文】
（型A・型C・型Eのいずれか。型A/型E=20〜40字／型C=80〜200字（見出し1行＋列挙最大5行・各15字以内）。手を止めるフックに徹する）

【補足リプライ1】
（型A=クリフハンガーの答え／型C=「では書き換えるには？」と逆側開示／型E=問いの答え。続けてなぜ繰り返すか→思考・行動のクセ→最初の一歩「まずは〇〇から始めてみて。」の1アクション。150〜250字）

【補足リプライ2】
（続きはnoteに書く旨を伝えフォロー誘導。例「この続きはnoteに書こうと思うので、フォローしてお待ちください。」広告臭なし・毎回別の言い回し。30〜60字）"""
    elif post_type == "opinion":
        type_specific_rules = """
【業界考察系専用ルール】
- ルートは1行20〜40字の意外性／逆説で手を止める。詳細考察は補足リプライで展開
- 補足リプライは夫側の本音・機微を翻訳する既婚男性視点で。一般向け夫婦コラム・恋愛ノウハウ・心理学トリビア紹介は禁止
- 「この人にしか語れない」専門的・具体的視点を最低1つ（回避型・不安型・愛着理論・自己効力感・「思考・行動のクセ」等の心理学・脳科学概念を活用）
- 感情論・精神論で終わらず、構造的な理由や「思考・行動のクセ」の視点で締める
"""
        output_format = """以下の形式で出力（説明・前置き不要）：

【本文】
（型Aまたは型E。20〜40字。意外性ある一言／逆説／答えを示さない問いで手を止める）

【補足リプライ】
（150〜250字。夫側の本音を翻訳する既婚男性視点で、夫の行動の理由・構造に言及）"""
    elif post_type == "permission":
        type_specific_rules = ""
        output_format = """以下の形式で出力（説明・前置き不要）：

【本文】
（型Aまたは型C。型A=20〜40字／型C=80〜200字（見出し1行＋列挙最大5行・各15字以内）。詳細・体験談はここに入れず補足リプライで展開）

【補足リプライ】
（150〜250字。型A=本文の一言に対する具体的な場面・体験・気づき・根拠を共感的に展開／型C=「では書き換えるには？」と逆側開示）"""
    else:
        type_specific_rules = ""
        output_format = """以下の形式で出力（説明・前置き不要）：

【本文】
（投稿タイプ（personal/dialogue）に応じ型カタログから選ぶ：personal=型B（40〜80字、ストーリー切断）／型D（60〜150字、同一人物の時系列対比）／型A（20〜40字）、dialogue=型E（20〜40字、答えを示さない問い）／型D（60〜150字）。詳細・体験談は入れず、選んだ型に必要な要素だけをルートに）

【補足リプライ】
（150〜250字。選んだ型の開示：型B=ストーリーの続きと思考・行動のクセ／型D=何が変わったかの構造／型A=クリフハンガーの答え／型E=問いの答え。共感的に）"""

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
- 差別化軸（曲げない信念＋それを届けるメソッド）：{positioning["differentiation"]}

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
- 語り手は既婚男性、一人称の代名詞は「僕」に統一（自称するときは「僕」。「俺」「私」「わたし」等は使わない。※「自分の〜」のような再帰的用法の『自分』は一人称代名詞ではないので可）。読者＝既婚女性に向け、自分は夫（男性）側の立場で語り、読者の感情は「あなた」で描く（語り手を女性一人称にしない）
- ルート＝続きを読ませるフックに徹する。字数は下記型カタログの型ごと上限に従う（基本は短文ルート＋リプライ1で詳細展開の2投稿構成、型C・型Dのみルート複数行）。ルートは具体的な行動・言葉で目を引く
- 詳細解説・設計ステップ・体験談・根拠はリプライ1以降で150〜250字・共感的に展開し、ルートの引きに必ず応える
- 語尾は断定基調。ただし毎回断定で終えず、体験談・問いかけ・数値での締めも交える
- 自慢調・正解提示は避け、学び・過程・葛藤・修正をオープンに語る
- 読者が自分事化できる生活場面（家事の合間、共働きの通勤・在宅、夫とのLINE・会話、寝室、記念日、週末等）は文脈に応じ可
- 冒頭を否定型「〇〇な人は△△だと思ってる。違う。」で始めない。「否定→転換→正解提示」の三段も毎回繰り返さない。抽象主語で始めず具体的な場面・体験・数字から入るのは型A〜型Eを選んだ場合のルールで、型F〜型I（結果・状態を先に置く型）を選んだ場合は場面から入らなくてよい。ただしその場合も1行目に読者の生活語（夫・妻・本音・我慢・機嫌等）を必ず1つ以上入れる
- 「——」（emダッシュ/二倍ダッシュ）は一切使わない。余韻切り・持ち越しは「。」「、」「？」か文中での意図的な中断で表す
- ルートで具体的な場面・体験・行動を描いたら、後ろに「実は」「つまり」「それは」「それ、〇〇ない」等で解釈・オチを付け足さない。場面で止め、意味は読者に委ねる（×「夫の分の味噌汁だけ少し多めによそってた／それ、見返りを待ってた」→○「〜少し多めによそってた」で止める）
- 比喩を断定で締める「キレの良い一言」を避ける（×「〇〇は△△の自動生成だった」「結局△△に過ぎなかった」）。比喩は「〇〇みたいなもの」「例えるなら〇〇」等の口語の余白を伴わせる
- ①②③の箇条書き構造を毎回同じ形式で使わない（問いかけ→列挙→結論、場面→原因→解決策など構成を変える）
- 読者を優劣・属性で分類する対比（『〇〇な人 vs 〇〇な人』『良い妻 vs ダメな妻』等）は投稿タイプを問わず使わない。型Iのように片側の状態だけを単独で提示するのは可（優劣をつけて並べないこと）
- 決めフレーズ／余韻締めの標語化を避け連続投稿で繰り返さない（「もう終わり」「〜からしか生まれない」「テクニックじゃなくマインド」「外側じゃなく内側」等の断定、「〜だった」「ようやく気づいた」等の余韻締め、「場面→気づき→教訓」の3段オチ）。同趣旨も毎回一人称の実感で別の言い回しに。禁止するのは抽象概念を対比させた標語で、型Hのように具体的な状態・行動を指す断定は可
- 句読点・文末リズムを整えすぎない。「〜なんだけど」「〜ですよね」等の文末の揺らし・言い淀み・訂正・固有名詞（場所・場面・固有名）を1投稿1〜2箇所混ぜてよい。均質さより一人称の生っぽさを優先
- 対話系は、質問する前に自分の考え・経験を先に開示する
- 自分のエピソード・数字・変化を盛り込み、内面と向き合う過程の透明性を出す（例「苦しくなる場面を1週間メモしただけで同じパターンに気づけた」）
- 数値はキリの良い値＋「以上／程度／前後」で（例「30分以上／週3時間程度」）。端数の具体値（48分・23%等）はAI感が出るのでNG
- 述語契約①（目的語明示）：述語（動詞・形容詞）が「何を／誰を」対象にするか文内に明示。「考える」「書き出す」「動く」「感じる」「向き合う」等でも対象を書く（×「書き出す」→○「苦しくなる場面のパターンを書き出す」、×「身体が感知している」→○「身体が夫との距離の変化を感知している」）
- 述語契約②（コロケーション）：「Xを〜する」「Xに〜する」はネイティブの自然な組み合わせのみ（×「シグナルを送る／感覚を持つ／判断を行う／変化を起こす」→○「シグナルを発する／感覚を覚える／判断を下す／変化が起きる」）
- 述語契約③（比喩の意味カテゴリ）：比喩は本体と述語の意味カテゴリを一致（○「会話の温度が下がる」／×「会話の温度が消える」）
- 述語契約④（長修飾節）：修飾節が15字を超えたら、主節の目的語が文内に明示されているか書く前に確認
- クライアント・知人の事例は明示区別する（「クライアントの〇〇さん」「知り合いに〇〇な人がいて」）。妻との関係や、尽くすほど苦しくなっていた頃の経験は自分の体験として直接語ってよい
- 自分の限界はマイナス語（「ダメ」「変われない」「臆病者」等）で書かず、「完璧ではない」「特別な何かを持つわけではない」のように「〈プラス語〉ではない」で表す
- 「思考・行動のクセ」は無修飾で使わず必ず読者所有を明示：著者が読者に語る時は『あなたの思考・行動のクセ』、読者の内面描写は『自分の思考・行動のクセ』（この『あなた』は読者＝Threads閲覧者で、Claude自身ではない）
- 研究・統計の引用（「〇〇の研究では」「約25%が」「〜と報告されている」等）や著者名・年・媒体・URLは本文に入れない。背景知識は断定的な一般論か体験として言い切る。概念名（回避型・愛着理論等）は可だが研究エビデンスとしては提示しない。詳細出典はnote等で補う
- 投稿の根に【発信者】差別化軸の“曲げない信念”のいずれかを必ず据える。AIでも量産できる一般論・中立的な情報提供で完結させず、発信者の価値観が立つ角度で書く。ただし信念の文言を標語として直貼り・連呼はせず（上の標語化禁止に従う）、一人称の具体的な場面・体験に翻訳して滲ませる
- AI臭い定型表現を避け自然な日本語で書く。以下は使わない：
  ・まとめ／結論の押し付け：「〜と言えるでしょう」「〜のではないでしょうか」「結論から言うと」「まとめると」「いかがでしたか」
  ・過剰な強調・空虚な形容：「非常に／極めて重要」「言うまでもなく」「まさしく」「不可欠」「核心的」「鍵となる」「根本的」「多角的／包括的／総合的」
  ・定型導入・予告口調：「さて、」「それでは、」「このように」「見ていきましょう」「紹介していきます」「解説していきます」「掘り下げる」「深掘りする」「探求する」「言語化する」「正面から〜する」
  ・予防線・免責：「一概には言えません」「個人差がありますが」「あくまで一例ですが」
  ・「実は」：クリフハンガー・オチ・接続のいずれでも使わない
  ・翻訳調：「〜することができる」→「〜できる」、「〜という点で／観点から」→直截に、「〜することによって」→「〜すると」、無生物主語＋他動詞（「〇〇は〜を示している」→「〇〇から〜と分かる」）、Cleft（「それは〜。なぜなら〜からだ」）、「〜に他ならない」
  ・汎用の空語：「様々な」「多様な」「〜ということです」「今回は〜について紹介します」「ぜひ〜してみてください」

【「次を期待させる終わり方」の型カタログ】
ルート末尾を「読者が続きを期待する」状態で締める型。投稿タイプに適合する型を1つ選び、直近投稿で同じ型が連続していないか確認してルート設計。型ごとに本文字数上限が違うので合わせる。

型A：クリフハンガー一言型（structure/opinion、20〜40字）
- 意外性ある一言＋「残念ながら、違います。」「本当の理由は別にあります。」で答えを伏せて終え、補足リプライ1で開示
- 例「多くの女性が誤解している『夫が冷たくなる理由』。残念ながら、違います。」

型B：ストーリー切断型（personal、40〜80字）
- 一次経験ソース（妻との関係／クライアント事例〔「クライアントの〇〇さん」「知り合いに〇〇な人がいて」と明示区別〕／コーチとして向き合った場面）から1場面を描き、感情・思考が動いた瞬間で切って次リプライへ持ち越す
- signal例「気づいたのは、その夜だった。」「最初に出てきた感覚は、」「あとから思えば、そこが分岐点だった。」
- 著名人の言葉・逸話の引用形式は使わない（一次経験ソースのみ）

型C：片側列挙→逆側予告型（structure通常スロット／permission、80〜200字）
- 「苦しくなる前にとりがちな行動」「うまくいかない時の自分の思考・行動のクセ」等、片側だけを箇条書き列挙し、補足リプライ1で「では書き換えるには？」と逆側開示
- 列挙は最大5項目・各15字以内（見出し1行＋列挙最大5行）で、「自分／読者自身」を主語にした自己観察リストに

型D：同一人物の時系列対比型（personal/dialogue、60〜150字）
- 同一人物（「自分」または明示区別した「クライアントの〇〇さん」等の一次経験ソース）の変化前／変化後の思考・行動を並列見出しで提示し、補足リプライ1で「何が変わったか」を開示

型E：問いかけ持ち越し型（opinion/dialogue、20〜40字）
- 答えを示さない問いを立て、補足リプライ1で開示
- 例「なぜ尽くすほど、夫に大切にされている実感が薄れていくのか。」

以下の型F〜型Iは、場面描写ではなく「読者が得たい結果／避けたい状態」を1行目に先置きする型。
ルートだけを見てフォローを判断する読者に向けた入り口で、投稿タイプを問わず選べる。
いずれも1行目に読者の生活語（夫・妻・本音・我慢・機嫌等）を1つ以上入れ、詳細は補足リプライで開く。

型F：ベネフィット直球型（全タイプ、20〜40字）
- 「[読者が欲しい結果]＋[それをもたらす手段の名前]」を1行に畳む。結果を先に置き、手段は名前だけ出して中身は伏せ、補足リプライ1で開示
- 例「夫の顔色を伺わなくなる、自分の機嫌の取り方。」

型G：件数予告型（全タイプ、20〜40字）
- 「[読者が恐れる状態／目指す状態]＋〇〇した方が良いこと2選」等、件数だけ予告して中身を補足リプライ1へ送る。件数は2〜5
- 補足リプライ1は予告した件数どおりに並べる。列挙の形式（①②③／改行のみ／散文）は連続投稿で同じにしない
- 例「距離が離れていく夫婦が今すぐやめた方が良いこと3選」

型H：逆説断定型（全タイプ、15〜35字）
- 読者の常識と逆を向く短い断定を言い切り、理由は書かずに止める。補足リプライ1で理由を開示
- 賛否が割れてよい。ただし矛先は世間の通念や夫側の鈍さに向け、読者を責める・見下す断定にはしない
- 例「夫にわがままを言えてる妻ほど、大切にされてる。」

型I：状態提示切断型（全タイプ、20〜40字）
- 「[理想の状態／避けたい状態]な〇〇の特徴は」のように状態を提示し、体言止めで切って答えを伏せる
- 提示するのは片側の状態のみ。優劣をつけた二者の並置にはしない
- 例「言いたいことを言えてる妻が、夫にしていることは」
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

    # Threads用コンテンツ生成
    # structure は3投稿構成のため adaptive thinking を ON にし、思考トークンが本文を
    # 圧迫しないよう max_tokens を 4096 に拡張する。他タイプは thinking OFF（disabled）。
    # effort は output_config で high を明示（disabled は effort high 以下でのみ許可。
    # xhigh / max と組み合わせると 400 になる）。
    threads_prompt = build_prompt(strategy, post_type, recent_posts)
    if post_type == "structure":
        max_tokens = 4096
        thinking = {"type": "adaptive"}
    else:
        max_tokens = 768
        thinking = {"type": "disabled"}
    threads_message = client.messages.create(
        model="claude-opus-5",
        max_tokens=max_tokens,
        thinking=thinking,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": threads_prompt}],
    )
    log_token_cost("claude-opus-5", threads_message.usage, "generate_post")
    # thinking ON のとき content 先頭が thinking ブロックになり得るため text ブロックを明示抽出
    threads_text = next((b.text for b in threads_message.content if b.type == "text"), "")
    threads_result = _parse_post(threads_text.strip())

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
