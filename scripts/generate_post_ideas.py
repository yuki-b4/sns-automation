"""
投稿アイデア生成スクリプト
Claude APIを使って当日のThreads投稿アイデアを3案提案し、
output/ideas/YYYY-MM-DD.md に書き出して Slack に GitHub URL を通知する。

本文の生成・Threadsへの自動投稿は行わない（ネタ出し専用）。
提案されたテーマから1案を選び、本文は運用者が書いて手動投稿する運用を前提とする。
本文執筆時のルールは generate_post.py:build_prompt の「共通ルール」／型カタログを参照すること。
"""

import os
import re
import glob
import json
import datetime
import anthropic

from preflight import run_all as preflight_check
from notify_slack import notify_slack_post_ideas, notify_slack_post_ideas_failure
from sheets import get_recent_posts_content
from token_cost import log_token_cost

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_PATH = os.path.join(SCRIPT_DIR, "../config/strategy.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "../output/ideas")

JST = datetime.timezone(datetime.timedelta(hours=9))

IDEA_COUNT = 3
REASON_MAX_LEN = 150
SELECTION_NOTE_MAX_LEN = 80
HOOK_PER_IDEA = 2       # 各案が出すフック候補の数（必ず異なる型で出させる）
REPLY_ITEM_MIN = 3      # 補足リプライ1で開く要素の下限
REPLY_ITEM_MAX = 5      # 同上限
RECENT_IDEA_DAYS = 30   # 過去アイデア（output/ideas/*.md）を遡る日数
RECENT_POST_DAYS = 14   # 投稿DBの実投稿を遡る日数
RECENT_HOOK_TYPE_DAYS = 14  # フック型の偏りを見るために遡る日数

# フック型カタログ。ルート1行目の入り口の型で、generate_post.py:build_prompt の
# 型カタログ（型A〜型I）と型名を共有する。ここには型の要約だけを持たせ、本文執筆時の
# 詳細ルールは generate_post.py 側を正本とする（アイデア側を共通ルールの写しにしない）。
HOOK_TYPES = {
    "型F": {
        "name": "ベネフィット直球型",
        "spec": "「[読者が欲しい結果]＋[それをもたらす手段の名前]」を1行に畳む。結果を先に置き、手段は名前だけ出して中身は伏せる。20〜40字",
    },
    "型G": {
        "name": "件数予告型",
        "spec": "「[読者が恐れる状態／目指す状態]＋〇〇した方が良いこと2選」等、件数だけ予告して中身は補足リプライへ送る。件数は2〜5。20〜40字",
    },
    "型H": {
        "name": "逆説断定型",
        "spec": "読者の常識と逆を向く短い断定を言い切る。理由は書かず断定だけで止める。賛否が割れてよい。15〜35字",
    },
    "型I": {
        "name": "状態提示切断型",
        "spec": "「[理想の状態／避けたい状態]な〇〇の特徴は」のように状態を提示して体言止めで切り、答えを伏せる。20〜40字",
    },
    "型B": {
        # 型名は generate_post.py の型カタログ（正本）と一字一句揃えること。
        # ここがズレると、運用者が本文執筆時に正本側で型を引けなくなる。
        "name": "ストーリー切断型",
        "spec": "一次経験（妻との関係／コーチとして向き合った場面）から1場面を描き、感情・思考が動いた瞬間で切る。40〜80字",
    },
}

# save_ideas_md が書き出す見出し（`## 提案1: テーマラベル`）から
# テーマラベルを読み戻すための正規表現。書式を変えるときは両方を揃えること。
_IDEA_HEADING_RE = re.compile(r"^##\s*提案\d+[:：]\s*(.+?)\s*$")

# save_ideas_md が書き出すフック候補行（`  1. （ベネフィット直球型）...`）から
# 型名を読み戻すための正規表現。フック型の偏りを検出するために使う。
# マッチしなくても空の集計になるだけで生成はブロックしない（劣化して続行）。
_HOOK_TYPE_RE = re.compile(r"^\s*\d+\.\s*[（(]([^）)]+)[）)]")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def determine_post_types(strategy: dict, count: int = IDEA_COUNT) -> list[str]:
    """当日提案する count 件分の投稿タイプを post_rotation から順に取り出す。

    generate_post.py:determine_post_type と同じ「通日でローテーションを進める」考え方だが、
    1日1回まとめて count 件取り出すため POST_SLOT の概念はない。日付はファイル名と
    揃えるためJST基準で数える。rotation長20・count3は互いに素なので全要素を均等に消化する。
    """
    rotation = strategy["post_rotation"]
    day_of_year = datetime.datetime.now(JST).date().timetuple().tm_yday
    base = (day_of_year - 1) * count
    return [rotation[(base + i) % len(rotation)] for i in range(count)]


def _iter_recent_idea_files(days: int):
    """output/ideas/*.md のうち直近N日分を (日付文字列, 本文) で順に返す。
    ファイル名（YYYY-MM-DD.md）の日付で絞る。読めないファイルはスキップする。"""
    if not os.path.isdir(OUTPUT_DIR):
        return

    cutoff = datetime.datetime.now(JST).date() - datetime.timedelta(days=days)
    for path in sorted(glob.glob(os.path.join(OUTPUT_DIR, "*.md"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            file_date = datetime.date.fromisoformat(stem)
        except ValueError:
            continue  # 日付以外のファイル名は対象外
        if file_date < cutoff:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            print(f"[generate_post_ideas] アイデアファイル読み込み失敗（スキップ）: {path}: {e}", flush=True)
            continue
        yield stem, content


def load_recent_idea_labels(days: int = RECENT_IDEA_DAYS) -> list[dict]:
    """過去に提案したテーマラベルを output/ideas/*.md から読み出す。

    投稿を止めた後は投稿DBに新規行が積まれないため、テーマ重複回避の主軸はこちら。
    """
    results: list[dict] = []
    for stem, content in _iter_recent_idea_files(days):
        for line in content.splitlines():
            m = _IDEA_HEADING_RE.match(line)
            if m:
                results.append({"date": stem, "theme_label": m.group(1)})
    return results


def load_recent_hook_type_counts(days: int = RECENT_HOOK_TYPE_DAYS) -> dict[str, int]:
    """過去に提案したフック候補の型名と出現回数を output/ideas/*.md から集計する。

    フック型が特定の1型に固まるのを防ぐための入力。パースに失敗しても空の集計を
    返すだけで生成はブロックしない（型の偏り回避が効かなくなるだけ）。
    """
    counts: dict[str, int] = {}
    for _stem, content in _iter_recent_idea_files(days):
        for line in content.splitlines():
            m = _HOOK_TYPE_RE.match(line)
            if m:
                name = m.group(1).strip()
                counts[name] = counts.get(name, 0) + 1
    return counts


def load_recent_posts(days: int = RECENT_POST_DAYS) -> list[dict]:
    """投稿DBから直近N日のルート投稿を取得する。
    ネットワーク失敗時は空リストを返して生成自体はブロックしない。"""
    try:
        return get_recent_posts_content(days=days)
    except Exception as e:
        print(f"[generate_post_ideas] Sheetsからの投稿履歴取得に失敗: {e}", flush=True)
        return []


def build_avoid_section(recent_ideas: list[dict], recent_posts: list[dict]) -> str:
    """過去アイデアのテーマラベルと直近実投稿の冒頭を、重複回避用の入力に整形する。"""
    lines: list[str] = []
    seen: set[str] = set()
    for r in recent_ideas:
        label = (r.get("theme_label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        lines.append(f"- {r.get('date', '')}｜{label}")
    for p in recent_posts[-20:]:
        snippet = (p.get("content") or "").replace("\n", " ")[:50]
        if not snippet:
            continue
        lines.append(f"- {(p.get('posted_at') or '')[:10]}｜（実投稿）{snippet}")
    if not lines:
        return "（過去アイデア・投稿の履歴なし）"
    return "\n".join(lines)


def build_hook_type_section(hook_type_counts: dict[str, int]) -> str:
    """フック型カタログと、直近の使用回数を1つのブロックに整形する。
    使用回数を見せることで、特定の型に固まるのを Claude 側で避けさせる。"""
    lines: list[str] = []
    for key, info in HOOK_TYPES.items():
        used = hook_type_counts.get(info["name"], 0)
        used_label = f"直近{RECENT_HOOK_TYPE_DAYS}日の使用 {used}回" if hook_type_counts else "使用履歴なし"
        lines.append(f"{key}：{info['name']}（{used_label}）\n  {info['spec']}")
    return "\n".join(lines)


class IdeaGenerationError(RuntimeError):
    """アイデア提案生成の致命的失敗。main() で Slack 通知＋停止する。"""


def build_prompt(
    strategy: dict,
    post_types: list[str],
    avoid_section: str,
    hook_type_section: str,
) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]
    funnel = strategy["funnel"]
    midend = positioning["midend_product"]
    backend = positioning["backend_product"]

    type_lines = []
    for i, t in enumerate(post_types, start=1):
        info = strategy["post_types"][t]
        type_lines.append(
            f"提案{i}: {info['label']}（{t}）／ファネル段階：{info['funnel_stage']}\n"
            f"  {info['description']}"
        )
    types_section = "\n".join(type_lines)

    example_items = ",\n".join(
        f'    {{ "post_type": "{t}", "priority": {i}, "theme_label": "...", "angle": "...", '
        f'"hook_candidates": [{{ "type": "型名", "text": "..." }}, {{ "type": "型名", "text": "..." }}], '
        f'"reply_items": ["...", "..."], "follow_cta": "...", '
        f'"selection_note": "...", "reason": "..." }}'
        for i, t in enumerate(post_types, start=1)
    )

    return f"""あなたはSNSコンテンツの企画者です。
以下の戦略に基づいて、本日のThreads投稿のアイデアを{IDEA_COUNT}案提案してください。
本文は書かず、「どのテーマを・どの角度で・どんな入り口から書くか」の設計だけを出します。

読者の約7割はルート1行目（フック）だけを見てフォローするかを決める。
フックはおまけではなく、この提案物の主成果と考えてください。

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
悩み：
{chr(10).join(f"- {p}" for p in persona["pain_points"])}

【今回提案する投稿タイプ（この順番・この割り当てを守る）】
{types_section}

【過去に提案したテーマ・直近の実投稿（意味的に被らせない）】
{avoid_section}

【テーマ選考の優先基準（この順で効かせる）】
1. 一次感情への直結：読者が恐れる結果（夫の心が離れる／このまま自分が消える／愛されないまま年を重ねる）か、欲しい結果（自然体のまま愛される／本音を言えるようになる）に、フック1行で接続できるテーマを優先する。心理メカニズムの名前（〇〇効果・〇〇回路・〇〇誤差）が主役になるテーマは、読者にとっての結果へ翻訳できない限り採らない。メカニズムは補足リプライで支える裏付けであって、テーマの看板にはしない
2. 夫側の実感の翻訳：「その場面で夫（男性）側が実際に何を感じているか」を1つ含められるテーマを優先する。読者が自力では手に入らない情報になり、発信者の立場でしか出せない価値になる
3. 賛否が割れる論点：読者の常識と逆を向く主張を{IDEA_COUNT}案のうち1案には含めてよい。反対意見が付くこと自体は避けない。ただし読者を責める・見下す方向の賛否は採らない（矛先は世間の通念や夫側の鈍さに向ける）
4. 男女どちらが読んでも成立：主たる読者は既婚女性だが、男性が読んでも「確かに自分もそうだ」と頷ける翻訳になっているテーマを優先する

【提案ルール】
- 各案は割り当てられた投稿タイプの狙いに沿わせ、ペルソナの悩みのどれかを中心に据える
- {IDEA_COUNT}案で扱う悩み・場面を分散させる。同じ投稿タイプが複数割り当てられた場合は、切り口を明確に変える
- 各案は【発信者】差別化軸の“曲げない信念”のいずれかに必ず根ざす。中立的な情報提供・一般論で完結する切り口（発信者でなくても・AIでも量産できる切り口）は採らない
- 上記の過去テーマ・直近投稿と意味的に被らない（同じ単語の言い換えだけの近接テーマは避ける）
- 抽象的な大テーマ（例:「夫婦関係について」）ではなく、投稿1本で扱える具体的な切り口にする
- priority は1〜{IDEA_COUNT}を1つずつ使い、上の選考基準に最も強く合致する案を1にする（本日の推し）。順位は theme_label の good/bad ではなく「今日この1本を出すならどれか」で決める
- theme_label は8〜18字。何の話かが一目で分かる名詞句にする
- angle は80字以内。「どの角度から切り込み、読者に何を発見させるか」を1〜2文で書く（本文は書かない）
- reply_items は{REPLY_ITEM_MIN}〜{REPLY_ITEM_MAX}個・各25字以内。補足リプライ1で開く要素を短い句で並べる。番号リストで出すか散文で書くかは運用者が決めるので、ここでは材料だけを粒度を揃えて出す
- follow_cta は25〜45字。「〇〇したい人はフォローも忘れずに。」の形で、〇〇にはそのテーマ固有の読者の願い（例「夫に本音を言えるようになりたい」）を入れる。毎回同じ言い回しにせず、テーマごとに読者の自己認識ワードを変える。いいね・コメント・保存を直接要求するエンゲージメントベイトにはせず、フォロー導線に限る。体系化系（structure）は3投稿目に「続きはnoteに書く」型のフォロー誘導を置く運用があるので、その場合はこの follow_cta と重ねず、どちらか一方だけを使う（運用者が選ぶ前提で、案としては常に出す）
- selection_note は{SELECTION_NOTE_MAX_LEN}字以内。上の選考基準1〜4のどれで読者を引っかける案なのかを明示する（例「基準1（恐れ）＋基準2で組んだ案」）
- reason は{REASON_MAX_LEN}字以内（厳守・超過禁止）。「なぜ今のペルソナに刺さるか／なぜ他の切り口より優先したいか」を1〜2文で書く

【フック型カタログ（この中から選ぶ）】
{hook_type_section}

【hook_candidates の作り方】
- 各案につき{HOOK_PER_IDEA}案、必ず異なる型で出す。type にはカタログの型名（例「ベネフィット直球型」）をそのまま書く
- {IDEA_COUNT}案全体で同じ型に寄せない。直近の使用回数が多い型は避け、使用が少ない型を優先的に当てる
- 型の指定字数を守る。読者が1行目だけを見て「自分の話だ」と分かる具体語（夫・妻・浮気・本音・我慢・機嫌など読者の生活語）を必ず1つ以上入れる
- そのまま使える精度を目指すが、運用者が書き換える前提の素材でよい

【hook_candidates の制約】
- 型F・型G・型H・型Iは結果や状態を先に置く型なので、場面描写から入らなくてよい（場面から入るのは型Bのときだけ）
- 否定型の入り（「〇〇な人は△△だと思ってる。違う。」）は型を問わず使わない
- 型Hの断定は、具体的な状態・行動を指す断定にする（例「夫に迷惑をかけてる妻ほど浮気されない」）。抽象概念を対比させた標語（「テクニックじゃなくマインド」「外側じゃなく内側」等）で締めるのは型を問わず禁止
- 型Iで状態を提示するのは可だが、読者を優劣で分類する対比（『〇〇な人 vs 〇〇な人』『良い妻 vs ダメな妻』等）は型を問わず使わない
- 語り手は既婚男性。フックに一人称を出す場合の代名詞は「僕」、読者＝既婚女性の感情は「あなた」で描く（型F・型G・型Iは一人称を出さない形が基本）
- 研究・統計の引用や著者名・年・媒体は入れない（概念名の使用は可）
- 「思考・行動のクセ」は無修飾で使わず『あなたの／自分の』を冠する
- 「——」（emダッシュ/二倍ダッシュ）は使わない

出力形式（他の説明・前置き不要・JSONのみ）:
{{
  "ideas": [
{example_items}
  ]
}}"""


def _clean_hook_candidates(index: int, raw_hooks) -> list[dict]:
    """hook_candidates を検証して整形する。
    フックはこの提案物の主成果なので、欠落・空文字は致命扱いにする。
    型の重複は警告のみ（案自体は使えるため）。"""
    if not isinstance(raw_hooks, list) or not raw_hooks:
        raise IdeaGenerationError(
            f"ideas[{index}] の hook_candidates が空、または配列ではない: {raw_hooks}"
        )
    if len(raw_hooks) != HOOK_PER_IDEA:
        print(
            f"[generate_post_ideas] 警告: ideas[{index}] の hook_candidates が "
            f"{len(raw_hooks)}件（想定={HOOK_PER_IDEA}件）。そのまま出力します。",
            flush=True,
        )

    cleaned: list[dict] = []
    for h in raw_hooks:
        if not isinstance(h, dict):
            raise IdeaGenerationError(f"ideas[{index}] の hook_candidates 要素が辞書ではない: {h}")
        text = (h.get("text") or "").strip()
        if not text:
            raise IdeaGenerationError(f"ideas[{index}] の hook_candidates に本文が空の要素がある: {h}")
        cleaned.append({"type": (h.get("type") or "型不明").strip(), "text": text})

    used_types = [h["type"] for h in cleaned]
    if len(set(used_types)) < len(used_types):
        print(
            f"[generate_post_ideas] 警告: ideas[{index}] のフック型が重複している（{used_types}）。"
            "案としては使えるためそのまま出力します。",
            flush=True,
        )
    return cleaned


def _clean_reply_items(index: int, raw_items) -> list[str]:
    """reply_items を検証して整形する。件数が範囲外でも切り詰めず警告のみ。"""
    if not isinstance(raw_items, list):
        raise IdeaGenerationError(f"ideas[{index}] の reply_items が配列ではない: {raw_items}")
    items = [str(v).strip() for v in raw_items if str(v).strip()]
    if not items:
        raise IdeaGenerationError(f"ideas[{index}] の reply_items が空")
    if not (REPLY_ITEM_MIN <= len(items) <= REPLY_ITEM_MAX):
        print(
            f"[generate_post_ideas] 警告: ideas[{index}] の reply_items が {len(items)}件"
            f"（想定={REPLY_ITEM_MIN}〜{REPLY_ITEM_MAX}件）。そのまま出力します。",
            flush=True,
        )
    return items


def _resolve_priorities(cleaned: list[dict]) -> None:
    """priority が 1..N の順列になっているか検証し、崩れていれば割り当て順で振り直す。
    順位は運用者が見る参考値なので、崩れても停止はしない。"""
    values = [c.get("priority") for c in cleaned]
    if sorted(v for v in values if isinstance(v, int)) == list(range(1, len(cleaned) + 1)):
        return
    print(
        f"[generate_post_ideas] 警告: priority が1〜{len(cleaned)}の順列ではない（{values}）。"
        "割り当て順で振り直します。",
        flush=True,
    )
    for i, c in enumerate(cleaned, start=1):
        c["priority"] = i


def propose_ideas(
    client: anthropic.Anthropic,
    strategy: dict,
    post_types: list[str],
    avoid_section: str,
    hook_type_section: str,
) -> list[dict]:
    """本日の投稿アイデアを Claude API に提案させる。Claude 呼び出しは1回のみ。
    生成失敗時は IdeaGenerationError を送出する（呼び出し側で Slack 通知＋停止）。"""
    prompt = build_prompt(strategy, post_types, avoid_section, hook_type_section)

    try:
        message = client.messages.create(
            model="claude-opus-5",
            max_tokens=4500,
            thinking={"type": "disabled"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise IdeaGenerationError(f"Claude API呼び出しに失敗: {type(e).__name__}: {e}") from e

    log_token_cost("claude-opus-5", message.usage, "generate_post_ideas")
    # thinking ON のとき content 先頭が thinking ブロックになり得るため text ブロックを明示抽出
    raw = next((b.text for b in message.content if b.type == "text"), "").strip()
    # ```json などのフェンス除去
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise IdeaGenerationError(
            f"JSONパース失敗: {e}\n生成テキスト先頭500字: {raw[:500]}"
        ) from e

    ideas = data.get("ideas")
    if not isinstance(ideas, list) or len(ideas) != len(post_types):
        raise IdeaGenerationError(
            f"ideas が配列で長さ{len(post_types)}ではない。生成データ: {data}"
        )

    cleaned: list[dict] = []
    for i, (item, assigned_type) in enumerate(zip(ideas, post_types)):
        if not isinstance(item, dict):
            raise IdeaGenerationError(f"ideas[{i}] が辞書ではない: {item}")
        theme_label = (item.get("theme_label") or "").strip()
        angle = (item.get("angle") or "").strip()
        reason = (item.get("reason") or "").strip()
        follow_cta = (item.get("follow_cta") or "").strip()
        selection_note = (item.get("selection_note") or "").strip()
        if not (theme_label and angle and reason and follow_cta and selection_note):
            raise IdeaGenerationError(
                f"ideas[{i}] に theme_label / angle / reason / follow_cta / selection_note の"
                f"いずれかが欠落: {item}"
            )
        hooks = _clean_hook_candidates(i, item.get("hook_candidates"))
        reply_items = _clean_reply_items(i, item.get("reply_items"))
        returned_type = (item.get("post_type") or "").strip()
        if returned_type != assigned_type:
            # 投稿タイプはローテーションからPython側で決める値が正。取り違えても致命傷にしない。
            print(
                f"[generate_post_ideas] 警告: ideas[{i}] の post_type が割り当てと不一致"
                f"（返却={returned_type or '空'} / 割り当て={assigned_type}）。割り当て側を採用します。",
                flush=True,
            )
        if len(reason) > REASON_MAX_LEN:
            print(
                f"[generate_post_ideas] 警告: ideas[{i}] の reason が {len(reason)} 字"
                f"（>{REASON_MAX_LEN}）。切り詰めずそのまま出力します。",
                flush=True,
            )
        cleaned.append({
            "post_type": assigned_type,
            "priority": item.get("priority"),
            "theme_label": theme_label,
            "angle": angle,
            "hook_candidates": hooks,
            "reply_items": reply_items,
            "follow_cta": follow_cta,
            "selection_note": selection_note,
            "reason": reason,
        })

    _resolve_priorities(cleaned)
    return cleaned


def save_ideas_md(ideas: list[dict], strategy: dict, date_str: str) -> str:
    """アイデア提案をMarkdownとして保存し、ファイルパスを返す。

    読み戻しに使う行が2種類あるので、書式を変えるときは正規表現も揃えること:
      - 見出し `## 提案N: テーマラベル` → _IDEA_HEADING_RE（テーマ重複回避）
      - フック候補行 `  1. （型名）本文` → _HOOK_TYPE_RE（フック型の偏り回避）
    案は割り当て順（＝投稿タイプのローテーション順）に並べ、推し順は優先度欄で示す。
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filepath = os.path.join(OUTPUT_DIR, f"{date_str}.md")

    lines: list[str] = [f"# {date_str} Threads投稿アイデア（{len(ideas)}案）", ""]
    for i, idea in enumerate(ideas, start=1):
        post_type = idea["post_type"]
        label = strategy["post_types"].get(post_type, {}).get("label", post_type)
        reason = idea["reason"]
        priority = idea.get("priority", i)
        priority_label = f"{priority}（本日の推し）" if priority == 1 else str(priority)

        lines.append(f"## 提案{i}: {idea['theme_label']}")
        lines.append("")
        lines.append(f"- **投稿タイプ**: {label}（{post_type}）")
        lines.append(f"- **優先度**: {priority_label}")
        lines.append(f"- **切り口**: {idea['angle']}")
        lines.append("- **フック候補**:")
        for j, hook in enumerate(idea["hook_candidates"], start=1):
            lines.append(f"  {j}. （{hook['type']}）{hook['text']}")
        lines.append("- **補足リプライ1の要素**:")
        for item in idea["reply_items"]:
            lines.append(f"  - {item}")
        lines.append(f"- **フォロー誘導CTA**: {idea['follow_cta']}")
        lines.append(f"- **選考メモ**: {idea['selection_note']}")
        lines.append(f"- **狙い・根拠（{len(reason)}文字）**: {reason}")
        lines.append("")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return filepath


def main():
    # Claude API呼び出し前に外部サービスの接続確認。
    # Threadsへは投稿しないため threads チェックは外す（トークン失効で止めない）。
    preflight_check(checks=("slack", "sheets"))

    strategy = load_strategy()
    post_types = determine_post_types(strategy)
    print(f"[generate_post_ideas] 本日の投稿タイプ割り当て: {post_types}")

    recent_ideas = load_recent_idea_labels()
    recent_posts = load_recent_posts()
    hook_type_counts = load_recent_hook_type_counts()
    print(
        f"[generate_post_ideas] 重複回避入力: 過去アイデア {len(recent_ideas)}件 / "
        f"直近投稿 {len(recent_posts)}件"
    )
    print(f"[generate_post_ideas] 直近{RECENT_HOOK_TYPE_DAYS}日のフック型使用回数: {hook_type_counts or 'なし'}")
    avoid_section = build_avoid_section(recent_ideas, recent_posts)
    hook_type_section = build_hook_type_section(hook_type_counts)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    try:
        ideas = propose_ideas(client, strategy, post_types, avoid_section, hook_type_section)
    except IdeaGenerationError as e:
        print(f"[generate_post_ideas] アイデア提案生成に失敗: {e}", flush=True)
        notify_slack_post_ideas_failure(stage=f"{IDEA_COUNT}案提案生成", error=str(e))
        raise SystemExit(1)

    print(f"[generate_post_ideas] {len(ideas)}案生成完了: {[i['theme_label'] for i in ideas]}")

    today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    filepath = save_ideas_md(ideas, strategy, today_str)
    rel_path = f"output/ideas/{today_str}.md"
    repo = os.environ.get("GITHUB_REPOSITORY", "yuki-b4/sns-automation")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    github_url = f"https://github.com/{repo}/blob/{branch}/{rel_path}"

    print(f"[generate_post_ideas] 保存先: {filepath}")
    print(f"[generate_post_ideas] GitHub URL: {github_url}")

    notify_slack_post_ideas(ideas, today_str, github_url)

    print("[generate_post_ideas] 完了（投稿アイデア提案）")


if __name__ == "__main__":
    main()
