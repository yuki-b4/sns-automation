# 関心テーマDB収集ルーチン（Claude Code Routines）

ターゲット（30代 IT エンジニア・PM、家族あり）の関心に沿った外部情報を、SNS 以外のソースから日次で収集し、Google Sheets の「関心テーマDB」タブに蓄積する自律ルーチン。

投稿生成パイプライン（`scripts/generate_post.py` ほか）には**直接連動しない**。運用者がネタ探し・テーマ検討のために参照する資料 DB として機能する。下流スクリプトを変更しないことで、ルーチンが失敗しても投稿運用はゼロ影響。

---

## なぜ Routines か（コスト設計）

本リポジトリの他スクリプトが使う `ANTHROPIC_API_KEY`（従量課金）ではなく、**Claude.ai サブスクリプション（Pro/Max/Team）の月度枠**を消費する。`preflight.py` の「Claude API 課金を守る」契約の**外側**で運用するジョブ、という位置付け。

| 項目 | 値 |
|---|---|
| 実行基盤 | Anthropic 管理インフラ（claude.ai Routines） |
| 課金源 | Claude.ai サブスクリプション枠 |
| 日次実行上限 | Pro: 5 回 / Max: 15 回 / Team: 25 回 |
| スケジュール | 最小 1 時間間隔 |
| API キー | 不要（`ANTHROPIC_API_KEY` は未使用） |

このルーチンは 1 日 1 回実行なので Pro プランでも枠に収まるが、対話セッションと予算を共有する点に注意。

---

## セットアップ

### 1. claude.ai で Routine を作成
1. claude.ai にログイン → Settings → Routines → **New Routine**
2. 以下を設定:
   - **Name**: `関心テーマDB収集`
   - **Schedule**: Daily at `05:30 JST`（`20:30 UTC`）※ `daily_metrics.yml`（06:00 JST）より前
   - **Prompt**: 後述の「ルーチンプロンプト」を全文貼り付け

### 2. MCP コネクタの接続
Routine の設定画面で以下 3 つを接続する:

| コネクタ | 用途 | 必要権限 |
|---|---|---|
| GitHub | `config/strategy.json` を読み込む | `yuki-b4/sns-automation` Read |
| Google Sheets | 「関心テーマDB」タブに書き込み | 対象スプレッドシートの Edit |
| Slack | 結果通知 | `chat:write`（対象チャンネル） |

### 3. Google Sheets にタブを追加
対象スプレッドシート（`GOOGLE_SHEETS_ID`）に「**関心テーマDB**」という名前でタブを追加し、1 行目に以下のヘッダーを置く（列順固定、ルーチンがこの順で書き込む）:

```
fetched_at | category | source | url | headline | summary | relevance_score | pain_point | priority_theme | angle_hint
```

### 4. Routine 環境変数（任意）
`SLACK_USER_ID`（U から始まる ID）を Routine の環境変数に登録すると、警告系通知にメンションを付与する。未設定なら `<!channel>` にフォールバック。

### 5. 手動実行で疎通確認
Routine 詳細画面の **Run now** で初回実行し、Sheets にレコードが書かれ、Slack に成功通知が届くことを確認。

---

## Google Sheets「関心テーマDB」タブのスキーマ

| カラム | 型 / 例 | メモ |
|---|---|---|
| `fetched_at` | `2026-04-21T05:30:00+09:00` | JST ISO8601 |
| `category` | `news` / `trend` / `research` / `book` / `podcast` / `other` | SNS は除外 |
| `source` | `NHK` / `日経XTECH` / `Google Trends JP` | 発信元名 |
| `url` | `https://...` | 元記事 URL |
| `headline` | 原文タイトル | そのまま |
| `summary` | 120 字以内の日本語 | 一般要約ではなく「このターゲットにとっての意味」 |
| `relevance_score` | 1〜5 | 評価基準は後述 |
| `pain_point` | 例: `育休や休日でも仕事のことが頭から離れず、罪悪感がある` or 空 | `strategy.json:persona.pain_points` の該当本文をそのまま格納（配列要素の文字列）。該当なしは空セル |
| `priority_theme` | 例: `「やらないことリスト」の日次運用実況` or 空 | `strategy.json:priority_themes.themes[].label` をそのまま格納（id ではなく読み取りやすい日本語ラベル）。該当なしは空セル |
| `angle_hint` | 40 字以内 | `post_type × 切り口` を 1 案 |

### 書き込みルール
- `relevance_score >= 3` のみ書き込み（1〜2 は捨てる）
- 同じ `headline` が**直近 30 日内**に既存レコードとして存在する場合はスキップ（重複防止）
- 1 実行あたりの追記上限は 15 行（枠の爆発防止）

---

## Slack 通知仕様

本ルーチンは**成功・失敗・情報不足のいずれでも必ず 1 通は Slack に投げる**。無言終了は禁止。

### ✅ 成功通知（メンション無し）
```
✅ 関心テーマDB更新完了（<日付>）
追記: <N> 件 / 評価総数: <M> 件 / スキップ: <S> 件（重複 or score<3）

📌 今日のトップ3:
1. [score 5] <headline>
   → <angle_hint>
   🔗 <url>
2. ...
3. ...
```

### ⚠️ 情報不足通知（メンション付き）
`relevance_score >= 4` の item が 1 つも取れなかった場合。
```
⚠️ <@SLACK_USER_ID> 関心テーマDB: 高スコア item が本日ゼロでした
考えられる原因:
- （Claude が推定する原因を最大 3 つ箇条書き）
追加で知りたい情報:
- （運用者に投げる 1 行の質問）
```

### ❌ 失敗通知（メンション付き）
いずれかのステップで例外が出た場合、処理をそこで止めて Slack に投げる。
```
❌ <@SLACK_USER_ID> 関心テーマDB収集: 失敗
失敗ステップ: <strategy.json 取得 | 外部情報収集 | Sheets 書き込み | その他>
詳細: <エラー要約 200 字以内>
```

---

## ルーチンプロンプト

以下を claude.ai の Routine 設定画面にそのまま貼り付ける。

```text
あなたは SNS 運用のリサーチアシスタントです。下記の手順で、ターゲットの関心に沿った外部情報を日次収集し、Google Sheets に追記してください。投稿生成には直接使われず、運用者のネタ資料 DB として機能します。

【絶対ルール】
- 成功・失敗・情報不足のいずれで終わっても、必ず Slack に 1 通は通知すること。無言終了は禁止
- 推測で relevance_score を膨らませない。迷ったら低めに付ける
- SNS（X / Threads / Instagram / LinkedIn / Facebook / TikTok / YouTube Shorts）からの引用は行わない

─────────────────────────────
ステップ 1: ターゲット情報の取得
─────────────────────────────
GitHub MCP で yuki-b4/sns-automation リポジトリの config/strategy.json を取得し、以下を読み込む:
- persona.description
- persona.pain_points（0-indexed の配列として扱う）
- positioning.position / positioning.differentiation
- priority_themes.themes（id, label, rationale の配列）

取得に失敗した場合は Slack に「❌ 失敗通知」を投げて即終了。

─────────────────────────────
ステップ 2: 外部情報の収集（20〜40 件）
─────────────────────────────
以下のソース種別をバランスよく混ぜて収集する。同じサイト・同じトピックに偏らないこと。

- 日本のビジネス / テック系ニュース（IT・DX・エンジニアリング組織・PM 論・生産性）
- 働き方 / 労働時間 / リモートワーク関連の調査・政府発表・統計
- 家族 / 育児 / ワークライフバランスに関する研究・新書・記事
- 心理学・脳科学・行動科学の新しい研究（注意 / 認知負荷 / 意思決定疲れ / 回復 / 睡眠）
- 日本の書籍新刊・話題書（働き方・自己啓発・家族論）
- Google Trends JP の直近急上昇キーワードのうち、働き方・子育て文脈で解釈可能なもの
- Podcast / Substack / Zenn / note / ブログ（SNS 本体ではなく長文プラットフォームのみ可）

各 item について以下のフィールドをメモ:
- category: news / trend / research / book / podcast / other
- source: 発信元名
- url
- headline（原文タイトル）
- raw_body: 冒頭 200〜500 字程度の抜粋

収集に完全に失敗した（0 件）場合は Slack に「❌ 失敗通知」を投げて終了。

─────────────────────────────
ステップ 3: ターゲット適合度の評価
─────────────────────────────
各 item について以下を判定する。

relevance_score（1〜5）
  5 = ペルソナの痛みのど真ん中、かつ positioning.differentiation に直結
  4 = 痛みに刺さる、または priority_themes のいずれかに接続できる
  3 = 周辺話題として有用（運用者のネタ候補になりうる）
  2 = 遠いが捨てるほどではない
  1 = ノイズ

pain_point: 最も関連する persona.pain_points の該当本文をそのまま格納する（配列要素の文字列をコピー。言い換え・要約・ID化はしない）。該当なしは空文字
priority_theme: 該当する priority_themes.themes[].label をそのまま格納する（id ではなく label。言い換えはしない）。該当なしは空文字
angle_hint: 40 字以内で「どの post_type（permission / structure / personal / dialogue）で、どの切り口で使えるか」を 1 案。「設計」「仕組み」「透明性」のどれかの軸に寄せる。競合模倣や一般啓発トーンは禁止
summary: 120 字以内。「このターゲットにとって何が問題 or 機会か」の視点で書く。一般ニュース要約は禁止

─────────────────────────────
ステップ 4: Google Sheets への書き込み
─────────────────────────────
対象: 「関心テーマDB」タブ

書き込み条件:
- relevance_score >= 3 のもののみ（1〜2 は捨てる）
- 既存レコードの中で、同じ headline が直近 30 日以内に存在するものはスキップ（重複防止）
- 1 実行あたりの追記上限は 15 行。relevance_score の高い順に採用し、超過分は捨てる

列順（固定）:
fetched_at | category | source | url | headline | summary | relevance_score | pain_point | priority_theme | angle_hint

fetched_at は実行時刻を JST ISO8601 で。pain_point / priority_theme が該当なしのときは空セル。

書き込みに失敗した場合は Slack に「❌ 失敗通知」を投げて終了。

─────────────────────────────
ステップ 5: Slack 通知
─────────────────────────────
書き込みが 1 件でも成功した場合:

  ✅ 関心テーマDB更新完了（YYYY-MM-DD）
  追記: N 件 / 評価総数: M 件 / スキップ: S 件（重複 or score<3）

  📌 今日のトップ3:
  1. [score X] <headline>
     → <angle_hint>
     🔗 <url>
  2. ...
  3. ...

（メンション無し。追記した中から relevance_score 降順で最大 3 件。同点は pain_point の網羅を優先）

書き込み自体は成功したが relevance_score >= 4 の item が 1 つも取れなかった場合は、上記の成功通知に加えて以下も投げる:

  ⚠️ <@SLACK_USER_ID> 関心テーマDB: 高スコア item が本日ゼロでした
  考えられる原因:
  - <箇条書き最大 3 つ。ペルソナ情報の不足 / 取得ソースの偏り / 季節要因 など>
  追加で知りたい情報:
  - <運用者に投げる 1 行の質問>

SLACK_USER_ID は Routine 環境変数から取得。未設定なら <!channel> で代替。

─────────────────────────────
失敗時の Slack フォーマット（共通）
─────────────────────────────
いずれかのステップで例外が出たら:

  ❌ <@SLACK_USER_ID> 関心テーマDB収集: 失敗
  失敗ステップ: <strategy.json 取得 | 外部情報収集 | Sheets 書き込み | その他>
  詳細: <エラー要約 200 字以内>

Slack 通知自体が失敗した場合は、Routine のログに失敗事実を print して終了。Slack が落ちている可能性以外は原則ここに到達しない。
```

---

## 運用上の注意

### 下流スクリプトとの関係
- `generate_post.py` / `weekly_report.py` 等は本タブを**参照しない**（下流は現状維持）。将来注入したくなった場合は、`sheets.py` に `get_interest_themes_for_injection(post_type, min_score=4)` 相当を足し、`generate_post.py:build_prompt` で該当 post_type 向けの行を 1〜2 件追加するのが最小差分

### `strategy.json` 更新時
`persona` / `priority_themes` を編集したら、本ルーチンも自動で追随する（GitHub MCP で毎回最新を読むため）。プロンプト側を書き換える必要はない

### 枠切れ対策
Pro プランで対話セッションを多用していると月末に枠が切れる可能性あり。切れた場合は Routine が単に実行されないだけで、Slack 通知も出ない。月末に追記が途切れていたら claude.ai ダッシュボードで使用量を確認すること

### モデル指定
Routines のモデルは claude.ai 側で選択する。本リポジトリの他スクリプトが `claude-opus-4-6` を使っているため、Routine も同系統（Opus 4.x 系）を選ぶと評価基準がズレにくい

### 無効化
一時停止したい場合は claude.ai → Routines → 該当ルーチンを **Pause**。削除すると MCP 接続設定も消えるので再設定が面倒になる点に注意
