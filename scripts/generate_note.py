"""
note記事生成スクリプト
Claude APIを使って毎日1本のnote記事ドラフトをMarkdownで生成し、
output/notes/YYYY-MM-DD_{mode}.md に保存して Slack にGitHub URLを通知する

モード:
  free  (デフォルト) - 過去7日のThreads投稿を参考に1200〜1500字の無料note記事を生成
  paid               - strategy.jsonの5本柱から2500〜3500字の有料note記事を生成
"""

import os
import re
import json
import random
import datetime
import anthropic
from collections import defaultdict
from sheets import get_weekly_data, append_note_record, get_note_records
from notify_slack import notify_slack_note, notify_slack_note_generation_failure
from token_cost import log_token_cost

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_PATH = os.path.join(SCRIPT_DIR, "../config/strategy.json")
NOTE_GUIDE_PATH = os.path.join(SCRIPT_DIR, "../config/note_writing_guide.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "../output/notes")


def load_strategy() -> dict:
    with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_writing_guide() -> dict:
    with open(NOTE_GUIDE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_recent_note_excerpts(n: int = 5, exclude_date: str | None = None) -> list[dict]:
    """直近N件のnote Markdownファイルをファイル名の新しい順で返す。
    exclude_date（YYYY-MM-DD）で始まるファイルは除外する（同日リトライ時の自己参照防止）。
    心理学・脳科学の用語／研究結果の重複回避プロンプトに使用する。"""
    if not os.path.isdir(OUTPUT_DIR):
        return []
    files = sorted(
        (f for f in os.listdir(OUTPUT_DIR) if f.endswith(".md")),
        reverse=True,
    )
    if exclude_date:
        files = [f for f in files if not f.startswith(exclude_date)]

    excerpts: list[dict] = []
    for filename in files[:n]:
        filepath = os.path.join(OUTPUT_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                excerpts.append({"filename": filename, "content": fh.read()})
        except OSError:
            continue
    return excerpts


# 心理学・脳科学の固有用語を抽出するための複合語正規表現
# - プレフィックス(英字/カナ 2文字以上) ＋ 肩書/施設/現象名
# - 単独で用語として成立する固有名詞
_PSYCH_KEYWORD_RE = re.compile(
    r"[A-Za-zァ-ヴー]{2,}(?:[ ・][A-Za-zァ-ヴー]+)*"
    r"(?:博士|教授|ら|大学|効果|症候群|理論|ネットワーク|因子|ホルモン)"
    r"|ツァイガルニク|デフォルトモード|認知資源|自己制御資源|ワーキングメモリ"
    r"|ドーパミン|セロトニン|コルチゾール|メラトニン|オキシトシン|アドレナリン|ノルアドレナリン"
    r"|HRV|DMN|BDNF|前頭前野|海馬|扁桃体|自律神経|交感神経|副交感神経|判断疲れ"
)


def _extract_psych_terms(content: str, max_terms: int = 6) -> list[str]:
    """心理学・脳科学の固有用語だけを本文から抽出（記事内で重複除去、最大N個）。
    段落ダンプではなく用語列挙にすることでトークンを大幅に削減する。"""
    seen: set[str] = set()
    terms: list[str] = []
    for m in _PSYCH_KEYWORD_RE.finditer(content):
        t = m.group(0).strip()
        if t and t not in seen:
            seen.add(t)
            terms.append(t)
        if len(terms) >= max_terms:
            break
    return terms


def load_past_note_titles_from_sheets(weeks: int = 4, limit: int = 10) -> list[dict]:
    """Sheetsのnote投稿DBから直近N週・最大limit件のタイトル＋combinationを返す。
    切り口（テーマ・場面・核概念語）の重複回避プロンプトに使用する。
    ネットワーク失敗時は空リストを返して生成自体はブロックしない。"""
    try:
        records = get_note_records(weeks=weeks)
    except Exception as e:
        print(f"[generate_note] Sheetsからのnote履歴取得に失敗: {e}", flush=True)
        return []
    records = sorted(records, key=lambda r: r.get("generated_at", ""), reverse=True)
    return records[:limit]


def select_angle_combo(
    strategy: dict,
    seed_str: str,
    past_records: list[dict] | None = None,
) -> dict | None:
    """pain_point / 場面 / 現れ方 の3要素を Python 側で1組だけ事前選択する。
    候補を全量Claudeに渡す方式からの切り替え（トークン削減＋選択履歴のDB記録のため）。
    直近の past_records で既に使われた situation/manifestation は除外する（枯渇時はプール全体に戻す）。
    seed_str（YYYY-MM-DD）で再現性を担保する。"""
    persona = strategy.get("persona", {})
    pains = persona.get("pain_points", [])
    angles = strategy.get("note_angles", {})
    situations = angles.get("situations", [])
    manifestations = angles.get("manifestations", [])
    if not (pains and situations and manifestations):
        return None

    recent_situations = {
        (r.get("selected_situation") or "").strip()
        for r in (past_records or [])
        if (r.get("selected_situation") or "").strip()
    }
    recent_manifestations = {
        (r.get("selected_manifestation") or "").strip()
        for r in (past_records or [])
        if (r.get("selected_manifestation") or "").strip()
    }

    avail_situations = [s for s in situations if s not in recent_situations] or situations
    avail_manifestations = [m for m in manifestations if m not in recent_manifestations] or manifestations

    rng = random.Random(seed_str)
    return {
        "pain_point": rng.choice(pains),
        "situation": rng.choice(avail_situations),
        "manifestation": rng.choice(avail_manifestations),
    }


def build_angle_matrix_section(combo: dict | None) -> str:
    """Python側で事前選択した切り口（pain/場面/現れ方）をプロンプト用に整形。
    候補30項目の列挙ではなく1組に絞ることでトークン削減＋選択履歴をDB記録可能にする。"""
    if not combo:
        return ""
    return (
        "【今回のnoteで扱う切り口（Python側で事前選択済み・意味は差し替え禁止）】\n"
        f"- pain_point: {combo['pain_point']}\n"
        f"- 場面: {combo['situation']}\n"
        f"- 現れ方: {combo['manifestation']}\n"
        "※ pain_point は記事の中心テーマ。タイトルかリード文で扱う対象として明示する。\n"
        "※ 場面・現れ方 は本文中の情景描写・例示・エピソードの素材として活かす。"
        "ラベル文言（例: 「リリース直前の週末」「集中力が切れる」）はそのまま literal に引用しない。\n"
        "※ 時間や曜日の明示（「土曜午後」「金曜夜」「日曜深夜」など）はマストではない。"
        "情景上必要なときだけ自然に出すに留め、無理に書き込まないこと。"
    )


def build_past_notes_avoid_section(past_records: list[dict]) -> str:
    """Sheets履歴を「過去noteで扱った切り口（避けるべき核概念語の参照元）」としてテキスト化。
    date+title のみ注入する（type/combination は核概念語の重複回避には寄与しないため除外）。"""
    if not past_records:
        return "【過去noteで扱った切り口（重複回避）】\n（履歴なし）"
    lines = ["【過去noteで扱った切り口（今回は核概念語を変えること）】"]
    for r in past_records:
        date = (str(r.get("generated_at", "")) or "")[:10]
        title = r.get("title", "")
        lines.append(f"- {date}｜{title}")
    return "\n".join(lines)


def format_recent_notes_for_avoidance(excerpts: list[dict]) -> str:
    """直近noteから心理学・脳科学の既出用語だけをプロンプト用に整形する。
    段落ダンプではなく用語リストに圧縮してトークンを抑える。"""
    if not excerpts:
        return ""
    lines = ["【直近noteで既出の心理学・脳科学用語（再利用禁止）】"]
    for e in excerpts:
        date = e["filename"][:10]
        terms = _extract_psych_terms(e["content"])
        lines.append(f"- {date}: {', '.join(terms) if terms else 'なし'}")
    return "\n".join(lines)


def _find_pattern(patterns: list[dict], type_name: str) -> dict:
    """patternリストからtypeが一致する要素を返す（見つからなければ空辞書）。"""
    return next((p for p in patterns if p.get("type") == type_name), {})


def _format_high_performance_block(guide: dict, combination: dict) -> str:
    """high_performance_patterns のうち、現在の combination_id に紐づく必須要素だけを注入する。
    全 combination で常時注入はしないでトークン肥大を避ける。"""
    hp_section = guide.get("high_performance_patterns") or {}
    cid = combination.get("id", "")
    for key, body in hp_section.items():
        if key == "description":
            continue
        if not isinstance(body, dict):
            continue
        if body.get("applies_to_combination_id") != cid:
            continue
        elements = body.get("required_elements") or []
        if not elements:
            continue
        bullet = "\n".join(f"- {e}" for e in elements)
        return (
            f"\n【高エンゲージメント実証パターン：{body.get('description', '')}】"
            f"（{body.get('evidence', '')}）\n"
            f"必須要素（全て満たすこと）:\n{bullet}"
        )
    return ""


def _format_handoff_block(guide: dict) -> str:
    """engagement_design_rules.threads_to_note_handoff を全 combination 共通で注入する。"""
    rules = guide.get("engagement_design_rules") or {}
    handoff = rules.get("threads_to_note_handoff") or {}
    if not handoff:
        return ""
    return (
        f"\n【Threads→note 引き継ぎ設計】{handoff.get('rule', '')}\n"
        f"実装: {handoff.get('implementation', '')}"
    )


def format_writing_guide(guide: dict, combination: dict) -> str:
    """note執筆ガイドをプロンプト注入用テキストに変換。
    combinationで指定された4型（title/hook/problem/solution）だけを展開し、
    21パターン全展開を避けてトークンを大幅削減する。
    engagement_principles・structure_templateは combination の指示でカバーされるため注入しない。
    curiosity_trigger 採用時は high_performance_patterns の必須要素も追記する。"""
    lines: list[str] = []

    # 当日採用する4型のみ展開
    tp = _find_pattern(guide["title_rules"]["patterns"], combination["title_type"])
    if tp:
        lines.append(f"【タイトル型：{tp['type']}】例: {tp['example']}（{tp['note']}）")
    lines.append(f"タイトルNG: {' / '.join(guide['title_rules']['avoid'])}")

    hp = _find_pattern(guide["opening_hook_rules"]["patterns"], combination["hook_type"])
    if hp:
        lines.append(f"\n【冒頭フック型：{hp['type']}】例: {hp['example']}")
    lines.append("冒頭ルール: " + " / ".join(guide["opening_hook_rules"]["rules"]))

    pp = _find_pattern(guide["problem_presentation_rules"]["patterns"], combination["problem_type"])
    if pp:
        lines.append(f"\n【課題提示型：{pp['type']}】{pp['point']}")
        lines.append(f"例: {pp['example']}")

    sp = _find_pattern(guide["solution_presentation_rules"]["patterns"], combination["solution_type"])
    if sp:
        lines.append(f"\n【解決法型：{sp['type']}】{sp['point']}")
        lines.append(f"例: {sp['example']}")
    lines.append("解決法NG: " + " / ".join(guide["solution_presentation_rules"]["avoid"]))

    hp_block = _format_high_performance_block(guide, combination)
    if hp_block:
        lines.append(hp_block)

    handoff_block = _format_handoff_block(guide)
    if handoff_block:
        lines.append(handoff_block)

    return "\n".join(lines)


# Threads post_type → 表示ラベル（プロンプト内で使用）
_POST_TYPE_LABELS = {
    "permission": "許可系（罪悪感・後ろめたさへの共感メッセージ）",
    "structure":  "体系化系（仕組み・設計・手順を構造的に伝える投稿）",
    "personal":   "自己開示系（著者自身の失敗・経験・変化のプロセス）",
    "opinion":    "業界考察系（働き方・残業文化などを設計視点で分析）",
    "dialogue":   "対話系（読者への問いかけ・エンゲージメント促進）",
}

# Threads post_type → note combination id のマッピング
# 「最も反応が高かった投稿の種類」からnote記事の方向性を決定する。
# index ではなく id で参照する（combination_patterns.patterns の並び順変更に強くする）。
# permission を empathy_max → curiosity_trigger に振替（2026-04-27分析: 共感最大化6本量産でも閲覧16.3。
# 許可系は「頑張らなくていい」自体が逆説的価値観のため知的好奇心と相性が良い）。
_POST_TYPE_TO_COMBINATION_ID = {
    "structure":  "trust_builder",      # 体系化系  → 信頼構築（場面描写×根拠）
    "opinion":    "curiosity_trigger",  # 業界考察系 → 知的好奇心（逆説×設計図）
    "personal":   "empathy_max",        # 自己開示系 → 共感最大化（失敗談×構造分析）
    "permission": "curiosity_trigger",  # 許可系    → 知的好奇心（逆説×設計図）
    "dialogue":   "fan_builder",        # 対話系    → ファン化（場面描写×プロトコル）
}


def annotate_posts_with_metrics(posts: list[dict], metrics: list[dict]) -> list[dict]:
    """投稿リストにメトリクス情報を付加してエンゲージメント率の高い順にソート"""
    metrics_map = {str(m.get("post_id", "")): m for m in metrics}
    annotated = []
    for post in posts:
        m = metrics_map.get(str(post.get("post_id", "")), {})
        annotated.append({
            **post,
            "_engagement_rate": float(m.get("engagement_rate", 0) or 0),
            "_likes": int(m.get("likes", 0) or 0),
            "_impressions": int(m.get("impressions", 0) or 0),
        })
    return sorted(annotated, key=lambda p: p["_engagement_rate"], reverse=True)


def _filter_eligible_patterns(patterns: list[dict], mode: str) -> list[dict]:
    """combination_patterns を mode (free/paid) で絞り込む。
    available_modes が未指定のものは両方で許可する旧来挙動に従う。"""
    eligible: list[dict] = []
    for p in patterns:
        modes = p.get("available_modes") or ["free", "paid"]
        if mode in modes:
            eligible.append(p)
    return eligible


def _count_recent_combinations(past_records: list[dict], days: int, patterns: list[dict]) -> dict[str, int]:
    """過去N日に生成された note の combination_id 別カウント。
    旧データに combination_id が無い場合は combination_pattern (日本語名) → id にフォールバック解決。"""
    if not past_records:
        return {}
    name_to_id = {p["name"]: p["id"] for p in patterns}
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    counts: dict[str, int] = defaultdict(int)
    for r in past_records:
        gen_at = str(r.get("generated_at") or "")[:10]
        if not gen_at or gen_at < cutoff:
            continue
        cid = (r.get("combination_id") or "").strip()
        if not cid:
            cid = name_to_id.get((r.get("combination_pattern") or "").strip(), "")
        if cid:
            counts[cid] += 1
    return dict(counts)


def determine_combination_pattern(
    guide: dict,
    posts: list[dict] | None = None,
    metrics: list[dict] | None = None,
    past_records: list[dict] | None = None,
    mode: str = "free",
) -> tuple[dict, str]:
    """記事の構成型（タイトル型・フック型・課題型・解決法型の組み合わせ）を決定する。

    Hybrid B 選択ロジック (2026-04-27分析の配分目標を反映):
    1. mode に応じて使用可能パターンを絞り込む（free では action_driver を除外）
    2. Threads ER 上位 post_type から第1候補を決定
    3. 過去14日のカウントが weekly cap (例: empathy_max=2) を超えていたら次候補に進む
    4. post_type 候補が尽きたら recommended_weight 降順でフォールバック
    5. それでも全部キャップ超過なら最大ウェイトのパターンを採用（cap 無視）

    記事の内容軸（テーマ）はここでは決めず、propose_dynamic_theme で別途生成する。
    Returns: (combination, selection_reason)
    """
    patterns = guide["combination_patterns"]["patterns"]
    eligible = _filter_eligible_patterns(patterns, mode)
    eligible_ids = {p["id"] for p in eligible}

    distribution = guide.get("pattern_distribution") or {}
    weekly_caps: dict[str, int] = (
        distribution.get("free_mode_weekly_caps") or {}
    ) if mode == "free" else {}

    recent_counts = _count_recent_combinations(past_records or [], days=14, patterns=patterns)

    # 第1群: post_type ER 順に対応する combination id を並べる
    candidates: list[tuple[str, str]] = []
    if posts and metrics:
        type_scores: dict[str, list[float]] = defaultdict(list)
        metrics_map = {str(m.get("post_id", "")): m for m in metrics}
        for post in posts:
            pt = post.get("post_type", "")
            m = metrics_map.get(str(post.get("post_id", "")), {})
            rate = float(m.get("engagement_rate", 0) or 0)
            if pt and rate > 0:
                type_scores[pt].append(rate)
        if type_scores:
            avg_by_type = {pt: sum(v) / len(v) for pt, v in type_scores.items()}
            for pt, avg in sorted(avg_by_type.items(), key=lambda x: -x[1]):
                cid = _POST_TYPE_TO_COMBINATION_ID.get(pt)
                if cid and not any(c[0] == cid for c in candidates):
                    candidates.append(
                        (cid, f"post_type={pt} (ER avg {avg:.2%}, n={len(type_scores[pt])})")
                    )

    # 第2群フォールバック: recommended_weight 降順
    for p in sorted(eligible, key=lambda x: -float(x.get("recommended_weight", 0) or 0)):
        cid = p["id"]
        if not any(c[0] == cid for c in candidates):
            w = float(p.get("recommended_weight", 0) or 0)
            candidates.append((cid, f"weight fallback ({w:.0%})"))

    # 候補を順番に評価し、mode で許可 & cap 未超過のものを採用
    for cid, source in candidates:
        if cid not in eligible_ids:
            continue
        cap = weekly_caps.get(cid)
        cnt = recent_counts.get(cid, 0)
        if cap is not None and cnt >= cap:
            continue
        combination = next(p for p in patterns if p["id"] == cid)
        reason = (
            f"{source} → {combination['name']}を選択"
            f"（過去14日={cnt}本"
            f"{'/cap='+str(cap) if cap is not None else ''}）"
        )
        return combination, reason

    # 全候補が cap 超過 (異常時): 最大ウェイトのパターンを cap 無視で採用
    fallback = sorted(eligible, key=lambda p: -float(p.get("recommended_weight", 0) or 0))[0]
    return fallback, f"全候補がcap超過 → fallback採用: {fallback['name']}"


def apply_mode_overrides(combination: dict, guide: dict, mode: str) -> dict:
    """mode==paid のとき paid_mode_overrides.patterns[id] を combination にディープマージする。
    free mode（または overrides 未定義）はそのまま返す。
    instructions のような dict フィールドはネストしてマージする。"""
    if mode != "paid":
        return combination
    overrides = (
        (guide.get("paid_mode_overrides") or {})
        .get("patterns", {})
        .get(combination.get("id", ""), {})
    )
    if not overrides:
        return combination
    merged = dict(combination)
    for k, v in overrides.items():
        if k == "instructions" and isinstance(v, dict):
            merged["instructions"] = {**combination.get("instructions", {}), **v}
        else:
            merged[k] = v
    return merged


def build_past_themes_avoid_section(past_records: list[dict]) -> str:
    """Sheets履歴から直近のテーマ（theme_label）を抽出し、テーマ生成プロンプト用に整形する。
    過去テーマと被らないテーマ案を Claude に提案させるための入力。"""
    if not past_records:
        return "（過去テーマ履歴なし）"
    seen: set[str] = set()
    lines: list[str] = []
    for r in past_records:
        label = (r.get("theme_label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        date = (str(r.get("generated_at", "")) or "")[:10]
        lines.append(f"- {date}｜{label}")
    if not lines:
        return "（過去テーマ履歴なし）"
    return "\n".join(lines)


class ThemeGenerationError(RuntimeError):
    """テーマ動的生成の致命的失敗。フォールバックせず main() で Slack 通知＋停止する。"""


def propose_dynamic_theme(
    client: anthropic.Anthropic,
    strategy: dict,
    mode: str,
    past_records: list[dict],
    combination: dict,
    best_post_type_hint: str = "",
) -> tuple[str, str]:
    """その日のnote記事テーマを Claude API に提案させる。
    ペルソナ・コンセプト・商品から逆算し、過去テーマと重複しない切り口を1つ返させる。

    angle_combo（pain/場面/現れ方）はテーマ生成側に渡さない。
    Pythonで事前選択した切り口を見せると Claude がそこに引っ張られて
    テーマの自由度が落ちるため、本記事生成側にだけ注入する。

    Returns: (theme_label, theme_description)
    生成失敗時は ThemeGenerationError を送出する（呼び出し側で Slack 通知＋停止）。
    """
    positioning = strategy["positioning"]
    persona = strategy["persona"]
    past_themes_text = build_past_themes_avoid_section(past_records)

    mode_directive = (
        "無料記事のテーマ。ペルソナの悩みに直接刺さり、SNS流入から有料コンテンツへの導線として機能する切り口を選ぶ。"
        if mode == "free"
        else "有料記事（¥1,980）のテーマ。商品『" + positioning.get("product_title", "") + "』の入り口として機能し、"
             "「これは買う価値がある」と感じさせる具体性・独自性のある切り口を選ぶ。"
    )

    prompt = f"""あなたは note 記事の編集者です。以下の発信者情報・ペルソナ・商品から逆算し、本日生成する note 記事のテーマを 1 つだけ提案してください。

【ポジショニング】{positioning["position"]}
【コンセプト】{positioning["concept"]}
【差別化】{positioning["differentiation"]}
【売りたい商品】{positioning.get("product_title", "")}（{positioning.get("product_subtitle", "")}）
【ステートメント】{positioning["statement"]}

【ターゲット】{persona["description"]}
ペルソナの悩み:
{chr(10).join(f"- {p}" for p in persona["pain_points"])}

【今回採用する記事構成型】{combination["name"]}（目標: {combination["target_goal"]}）

【モード別方針】
{mode_directive}

【参考: 直近Threadsで最も反応の高かった投稿タイプ】{best_post_type_hint or "（データなし）"}

【過去noteで扱ったテーマ】
{past_themes_text}

【テーマ提案のルール】
- 商品・コンセプト・ペルソナのpain_pointから逆算し、毎回違う切り口で 1 つ生成すること
- 上記の「過去noteで扱ったテーマ」と意味的に被らないテーマにする（同じ単語の言い換えだけの近接テーマは避ける）
- pain_point を中心テーマに据え、商品の世界観（成果は落とさず家族時間を取り戻す／設計で解決する）に整合させる
- 抽象的な大テーマ（例: 「働き方について」）ではなく、記事1本で扱える具体的な切り口にする
- theme_label は 8〜18字程度、theme_description は 50〜90字で「この記事で何を扱い、読者にどんな変化を提供するか」を1〜2文で書く

出力形式（他の説明・前置き不要・JSON のみ）:
{{"theme_label": "（テーマラベル）", "theme_description": "（テーマの概要・記事で扱う具体的な内容と読者への変化）"}}"""

    try:
        message = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise ThemeGenerationError(f"Claude API呼び出しに失敗: {type(e).__name__}: {e}") from e

    log_token_cost("claude-opus-4-7", message.usage, "generate_note_theme")
    raw = message.content[0].text.strip()
    # ```json などのフェンス除去
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ThemeGenerationError(
            f"JSONパース失敗: {e}\n生成テキスト先頭500字: {raw[:500]}"
        ) from e
    label = (data.get("theme_label") or "").strip()
    desc = (data.get("theme_description") or "").strip()
    if not label or not desc:
        raise ThemeGenerationError(
            f"theme_label / theme_description が空または欠落。生成データ: {data}"
        )
    return label, desc


def format_combination_instruction(combination: dict) -> str:
    """組み合わせパターンをプロンプト注入用テキストに変換"""
    inst = combination["instructions"]
    return f"""【今回の組み合わせパターン：{combination['name']}（目標：{combination['target_goal']}）】

- タイトル → {combination['title_type']}：{inst['title']}
- 冒頭フック（最初の100字）→ {combination['hook_type']}：{inst['hook']}
- 課題提示 → {combination['problem_type']}：{inst['problem']}
- 解決法 → {combination['solution_type']}：{inst['solution']}

この組み合わせの相乗効果：{combination['synergy']}"""


# writing_guide（title/hook/problem/solutionの4型）でカバー済みの要素IDは
# 重複注入を避けるため selling_elements から除外する。
# 除外: 1(ターゲット明示)・2(悩み解像度)・3(構造的再定義)・8(実践ガイド) → writing_guide側で同等以上を指示
# 残す: 4(脳科学根拠)・5(独自メソッド)・6(具体数字)・7(再現性)・9(独自経験)・10(読後変化)・11(価格正当化)・12(次のCTA)
_PAID_UNIQUE_SELLING_ELEMENT_IDS = {4, 5, 6, 7, 9, 10, 11, 12}


def select_top_selling_elements(guide: dict) -> list[dict]:
    """有料note固有の売れる要素だけを priority 昇順で返す。
    writing_guide と重複する汎用要素（1/2/3/8）は除外してトークンを削減する。"""
    elements = guide["paid_note_selling_elements"]["elements"]
    filtered = [e for e in elements if e.get("id") in _PAID_UNIQUE_SELLING_ELEMENT_IDS]
    return sorted(filtered, key=lambda e: e.get("priority", 99))


def format_selling_elements(guide: dict) -> str:
    """有料note 売れる要素チェックリストをプロンプト注入用テキストに変換（paid固有要素のみ）"""
    selected = select_top_selling_elements(guide)
    lines = [f"（以下{len(selected)}個すべてを含めること。writing_guideで既に指示された型の実践に上乗せする有料note固有の要素）"]
    for e in selected:
        lines.append(f"{e['id']}. 【{e['name']}】{e['description']} ／ 配置: {e['placement']}")
    return "\n".join(lines)


def build_free_note_prompt(
    strategy: dict,
    recent_posts: list[dict],
    theme_label: str,
    theme_desc: str,
    writing_guide: str,
    combination: dict,
    recent_notes_section: str = "",
    angle_matrix_section: str = "",
    past_notes_avoid_section: str = "",
) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]

    # エンゲージメント上位の投稿タイプを見出しで示し、数値は省略して投稿内容のみ表示
    seen_types: list[str] = []
    for p in recent_posts:
        pt = p.get("post_type", "")
        if pt and pt not in seen_types:
            seen_types.append(pt)
        if len(seen_types) >= 3:
            break
    top_labels = [_POST_TYPE_LABELS.get(t, t) for t in seen_types]
    top_types_header = (
        f"（エンゲージメント上位の投稿タイプ: {' > '.join(top_labels)} の順）\n"
        if top_labels else ""
    )
    posts_text = top_types_header + "\n".join(
        f"- [{_POST_TYPE_LABELS.get(p.get('post_type',''), p.get('post_type',''))}] {p.get('content','')}"
        for p in recent_posts[:15]
    )

    return f"""以下の戦略・執筆ガイド・Threads投稿履歴を参考に、ペルソナ向け無料note記事を生成してください。

【ポジショニング】{positioning["position"]}｜{positioning["concept"]}｜差別化: {positioning["differentiation"]}
【ステートメント】{positioning["statement"]}

【ターゲット】{persona["description"]}
悩み: {', '.join(persona["pain_points"])}

【今日のテーマ】{theme_label}：{theme_desc}

{angle_matrix_section}

{past_notes_avoid_section}

{format_combination_instruction(combination)}

【過去7日のThreads投稿（参考・発展のベース）】
{posts_text if posts_text else "（参考投稿なし）"}

{recent_notes_section}

【note執筆ガイド】
{writing_guide}

【記事の目的】
- SNS → 無料note → 有料コンテンツのファネル入口として機能させる
- 悩みに共感しつつ手法の入口を示し「この人の有料も読みたい」と思わせる
- Threads投稿をコピーせず深掘り・発展させる

【ルール】
- 1200〜1500字、見出しは `##`（Markdown）
- 設計・仕組み視点。精神論・根性論はNG
- CTAは「〜はこちらで詳しく書いています」程度の自然な誘導。不自然なら省略可
- ペルソナは月〜金勤務・土日休みの平日勤務サラリーマン前提。時間表現（「平日」「終業後」「週末」「週明け」「金曜夜」など）はこの前提で使う（週末＝土日の休日、週明け＝月曜の始業、終業後＝平日の退勤後、家族時間が増える典型は平日夜と土日）。シフト勤務・土日出勤を常態とする想定や、平日休み前提の表現は避ける
- 子育ての具体エピソードは自分の体験として書かず、知り合い・クライアントの事例として書く（「クライアントの方から」「周囲の子育て中の仲間で」「よくある例として」）。夫婦（妻との関係）の実体験は語ってよい。家族時間のペインは多数の子育て世代を観察してきた立場から書く
- 数値は「30分以上／1時間以上／2割以上／週3時間程度」のようにキリの良い値＋「以上／程度／前後」で表現。端数の具体値（48分・23%等）はAI生成感が出るのでNG
- 【過去noteで扱った切り口】のタイトルに出てきた核概念語（例: 判断コスト・判断疲れ・金曜午後・集中力切れ）を、今回のタイトル・リード文・H2見出しのうち2箇所以上では核として使わない
- 【今回のnoteで扱う切り口】は意味の差し替え禁止。pain_point はタイトルかリード文で扱う対象として明示し、場面・現れ方 は本文の情景・例示の素材として活かす（ラベル文言の literal 引用および時間/曜日の明示は不要）

出力形式（他の説明・前置き不要）：

【タイトル】
（ここにタイトル）

【本文】
（ここにMarkdown形式の本文）"""


def build_paid_note_prompt(
    strategy: dict,
    theme_label: str,
    theme_desc: str,
    writing_guide: str,
    combination: dict,
    guide: dict,
    recent_notes_section: str = "",
    angle_matrix_section: str = "",
    past_notes_avoid_section: str = "",
) -> str:
    positioning = strategy["positioning"]
    persona = strategy["persona"]
    selling_elements = format_selling_elements(guide)

    return f"""以下の戦略・執筆ガイドに基づき、ペルソナ向け有料note記事（¥1,980相当）を生成してください。

【ポジショニング】{positioning["position"]}｜{positioning["concept"]}
【商品】{positioning["product_title"]}（{positioning["product_subtitle"]}）
【ステートメント】{positioning["statement"]}

【ターゲット】{persona["description"]}
悩み: {', '.join(persona["pain_points"])}

【テーマ】{theme_label}：{theme_desc}

{angle_matrix_section}

{past_notes_avoid_section}

{format_combination_instruction(combination)}

{recent_notes_section}

【note執筆ガイド】
{writing_guide}

【有料note 売れる要素チェックリスト】
{selling_elements}

【ペイウォール設計】
- 無料ゾーン（全体の30〜35%・800〜1000字）：リード文 → 原因解説（脳科学根拠を含む）→ 「この先では〇〇の[N]ステップを詳しく解説する」と予告して終える
- 有料ゾーン：メソッド詳細・実践ガイド・まとめ・CTA
- 境界に `---\n**【ここから有料エリア】**\n---` を1箇所だけ挿入し、直前の段落で必ず「この先では〜」と続きを予告すること

【記事の目的】
- ¥1,980に見合う具体的価値を提供する
- 「読んだだけで行動が変わった」と感じさせる再現性の高い手法を出す
- 価値提供で信頼を構築し、上位商材への橋渡しにする

【ルール】
- 2500〜3500字、見出しは `##` / `###`（Markdown）
- 心理学・脳科学の根拠を最低1つ含める（要素4）
- 数値は「30分以上／1時間以上／2割以上／週3時間程度」のようにキリの良い値＋「以上／程度／前後」。端数の具体値（48分・23%等）はAI生成感が出るのでNG（要素6）
- 「できる人vsできない人」型の対比フォーマットは使わない
- CTAは上位商材への低圧力な自然な誘導（要素12）
- ペイウォール区切り（`---\n**【ここから有料エリア】**\n---`）は1箇所のみ
- ペルソナは月〜金勤務・土日休みの平日勤務サラリーマン前提。時間表現（「平日」「終業後」「週末」「週明け」「金曜夜」など）はこの前提で使う（週末＝土日の休日、週明け＝月曜の始業、終業後＝平日の退勤後、家族時間が増える典型は平日夜と土日）。シフト勤務・土日出勤を常態とする想定や、平日休み前提の表現は避ける
- 【過去noteで扱った切り口】のタイトルに出てきた核概念語（例: 判断コスト・判断疲れ・金曜午後・集中力切れ）を、今回のタイトル・リード文・H2見出しのうち2箇所以上では核として使わない
- 【今回のnoteで扱う切り口】は意味の差し替え禁止。pain_point はタイトルか無料ゾーンのリード文で扱う対象として明示し、場面・現れ方 は本文の情景・例示の素材として活かす（ラベル文言の literal 引用および時間/曜日の明示は不要）

以下の形式で出力してください（他の説明・前置き不要）：

【タイトル】
（ここにタイトル）

【本文】
（ここにMarkdown形式の本文。ペイウォール区切りを含む）

【売れる要素チェック】
（各要素について ✅ 含まれている / ❌ 含まれていない を記載。含まれていない場合は改善案を1行で添えること。）
例: ✅ 1.ターゲット明示: リード文「30代エンジニア・PM」に明記
例: ❌ 11.価格正当化: 未記載 → まとめ章に「3種のプロトコル＋チェックリスト」の記述を追加推奨"""


def parse_note(raw: str) -> dict:
    """生成テキストをタイトル・本文・売れる要素チェック（有料noteのみ）に分割"""
    title = ""
    body = ""
    checklist = ""

    if "【タイトル】" in raw and "【本文】" in raw:
        # 売れる要素チェックが含まれる場合（有料noteモード）
        if "【売れる要素チェック】" in raw:
            parts_check = raw.split("【売れる要素チェック】", 1)
            checklist = parts_check[1].strip()
            raw = parts_check[0]

        parts = raw.split("【本文】", 1)
        title = parts[0].replace("【タイトル】", "").strip()
        body = parts[1].strip()
    else:
        body = raw

    return {"title": title, "body": body, "checklist": checklist}


def save_note(title: str, body: str, mode: str, date_str: str, checklist: str = "") -> str:
    """Markdownファイルとして保存し、ファイルパスを返す。
    有料noteの場合は売れる要素チェックをファイル末尾に付記する。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{date_str}_{mode}.md"
    filepath = os.path.join(OUTPUT_DIR, filename)

    content = f"# {title}\n\n{body}" if title else body
    if checklist:
        content += f"\n\n---\n\n## 売れる要素チェック（生成時の自己評価）\n\n{checklist}"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    return filepath


def main():
    mode = os.environ.get("MODE", "free").lower()
    strategy = load_strategy()
    guide = load_writing_guide()

    # 過去7日のThreads投稿＋メトリクスを取得（両モード共通）
    # - freeモード: エンゲージメント上位のpost_typeからnoteテーマを決定 + 参照記事として使用
    # - paidモード: エンゲージメント上位のpost_typeからnoteテーマのみ決定
    data = get_weekly_data(days=7)
    recent_posts_raw = data.get("posts", [])
    recent_metrics = data.get("metrics", [])

    # エンゲージメント情報を付加してソート
    recent_posts = annotate_posts_with_metrics(recent_posts_raw, recent_metrics)
    print(f"[generate_note] 過去7日Threads投稿: {len(recent_posts)}件 / メトリクスあり: {len(recent_metrics)}件")

    # Sheets note投稿DBから過去4週・最大20件のレコードを取得
    # combination 選択（14日カウント）と後段の重複回避（テーマ・切り口）の両方で使う
    past_note_records = load_past_note_titles_from_sheets(weeks=4, limit=20)
    print(f"[generate_note] Sheets note履歴: {len(past_note_records)}件 (切り口・テーマ重複回避＋配分判定用)")

    # 記事の構成型（タイトル型・フック型・課題型・解決法型の組み合わせ）を決定
    # 内容軸（テーマ）はここでは決めず、後段で Claude に動的生成させる
    combination, selection_reason = determine_combination_pattern(
        guide,
        recent_posts_raw,
        recent_metrics,
        past_records=past_note_records,
        mode=mode,
    )
    # paid mode のときは paid_mode_overrides を適用（trust_builder の hook を「数字インパクト型」に戻す等）
    combination = apply_mode_overrides(combination, guide, mode)
    print(f"[generate_note] 構成型選択: {selection_reason} / mode={mode}")

    # 執筆ガイドは combination に応じて当日採用する4型だけ展開
    writing_guide = format_writing_guide(guide, combination)

    ref_post_ids = ",".join(
        str(p.get("post_id", "")) for p in recent_posts_raw if p.get("post_id")
    )

    # 直近note（過去3本まで）を読み込み、心理学・脳科学の既出用語を回避するコンテキストとして注入
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_str = now_jst.strftime("%Y-%m-%d")
    recent_note_excerpts = load_recent_note_excerpts(n=3, exclude_date=today_str)
    recent_notes_section = format_recent_notes_for_avoidance(recent_note_excerpts)
    print(f"[generate_note] 直近note参照: {len(recent_note_excerpts)}件 (心理学用語重複回避用)")

    # 切り口（核概念語）の重複回避＋テーマ重複回避は冒頭で取得済みの past_note_records を再利用
    past_notes_avoid_section = build_past_notes_avoid_section(past_note_records)

    # pain_point / 場面 / 現れ方 の3要素は Python 側で事前選択し、DBに記録する
    # past_note_records の selected_situation / selected_manifestation を避けて重複を防ぐ
    angle_combo = select_angle_combo(strategy, seed_str=today_str, past_records=past_note_records)
    angle_matrix_section = build_angle_matrix_section(angle_combo)
    if angle_combo:
        print(
            f"[generate_note] 切り口選択: pain={angle_combo['pain_point'][:20]}… / "
            f"場面={angle_combo['situation']} / 現れ方={angle_combo['manifestation']}"
        )

    # Claude API で記事生成
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ペルソナ・コンセプト・商品から逆算してテーマを Claude に動的提案させる（メイン記事生成の前段）
    best_post_type_hint = ""
    if recent_posts:
        seen_pt: list[str] = []
        for p in recent_posts:
            pt = p.get("post_type", "")
            if pt and pt not in seen_pt:
                seen_pt.append(pt)
            if len(seen_pt) >= 3:
                break
        best_post_type_hint = " > ".join(_POST_TYPE_LABELS.get(t, t) for t in seen_pt)
    try:
        theme_label, theme_desc = propose_dynamic_theme(
            client, strategy, mode, past_note_records, combination, best_post_type_hint
        )
    except ThemeGenerationError as e:
        print(f"[generate_note] テーマ動的生成に失敗: {e}", flush=True)
        notify_slack_note_generation_failure(
            stage="テーマ動的生成（Claude APIまたはJSONパース）",
            mode=mode,
            error=str(e),
        )
        raise SystemExit(1)
    print(f"[generate_note] テーマ動的生成: {theme_label}｜{theme_desc}")

    if mode == "free":
        prompt = build_free_note_prompt(
            strategy, recent_posts, theme_label, theme_desc, writing_guide, combination,
            recent_notes_section=recent_notes_section,
            angle_matrix_section=angle_matrix_section,
            past_notes_avoid_section=past_notes_avoid_section,
        )
        max_tokens = 2500
        selected_element_ids = ""
    else:
        prompt = build_paid_note_prompt(
            strategy, theme_label, theme_desc, writing_guide, combination, guide,
            recent_notes_section=recent_notes_section,
            angle_matrix_section=angle_matrix_section,
            past_notes_avoid_section=past_notes_avoid_section,
        )
        max_tokens = 5500  # チェックリスト分を追加
        selected_element_ids = ",".join(
            str(e["id"]) for e in select_top_selling_elements(guide)
        )

    print(f"[generate_note] モード: {mode} / テーマ: {theme_label} / パターン: {combination['name']} / Claude API 呼び出し中...")
    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    log_token_cost("claude-opus-4-7", message.usage, "generate_note")
    raw = message.content[0].text.strip()
    result = parse_note(raw)

    title = result["title"]
    body = result["body"]
    checklist = result.get("checklist", "")
    print(f"[generate_note] 生成完了: {title}")
    if checklist:
        passed = checklist.count("✅")
        failed = checklist.count("❌")
        print(f"[generate_note] 売れる要素チェック: ✅{passed}個 / ❌{failed}個")

    # Markdownファイルとして保存（now_jst / today_str は関数冒頭で確定済み）
    date_str = today_str
    filepath = save_note(title, body, mode, date_str, checklist)

    rel_path = f"output/notes/{date_str}_{mode}.md"
    repo = os.environ.get("GITHUB_REPOSITORY", "yuki-b4/sns-automation")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    github_url = f"https://github.com/{repo}/blob/{branch}/{rel_path}"

    print(f"[generate_note] 保存先: {filepath}")
    print(f"[generate_note] GitHub URL: {github_url}")

    # Slack通知（タイトル + GitHub URL のみ、本文は含めない）
    notify_slack_note(title, mode, github_url)

    # Google Sheetsに記録（組み合わせパターン情報＋事前選択した切り口＋動的生成テーマも含む）
    append_note_record({
        "type": mode,
        "title": title,
        "price": 0 if mode == "free" else 1980,
        "file_path": rel_path,
        "generated_at": now_jst.isoformat(),
        "status": "draft",
        "combination_pattern": combination["name"],
        "title_type": combination["title_type"],
        "hook_type": combination["hook_type"],
        "problem_type": combination["problem_type"],
        "solution_type": combination["solution_type"],
        "ref_threads_post_ids": ref_post_ids,
        "selling_element_ids": selected_element_ids,
        "selected_pain_point": (angle_combo or {}).get("pain_point", ""),
        "selected_situation": (angle_combo or {}).get("situation", ""),
        "selected_manifestation": (angle_combo or {}).get("manifestation", ""),
        "theme_label": theme_label,
        "theme_description": theme_desc,
    })

    print("[generate_note] 完了")


if __name__ == "__main__":
    main()
