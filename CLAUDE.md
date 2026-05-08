# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

SNS運用（Threads中心、note副次）の全工程—投稿生成→配信→メトリクス収集→競合分析→戦略改善レポート—を GitHub Actions + Python + Claude API で自動化する。ランタイムは全て GitHub Actions runner 上で、`pip install -r requirements.txt` → `python scripts/<name>.py` という単純なパターン。ローカルテスト・ビルド・lint は無い。

## Development commands

ローカル実行するときの典型フロー（全スクリプトは scripts/ 直下、リポジトリルートから実行する想定で相対パスが組まれている）:

```bash
pip install -r requirements.txt

# 必須の環境変数（GitHub Secrets と同名）
export ANTHROPIC_API_KEY=...
export THREADS_USER_ID=...
export THREADS_TOKEN=...
export SLACK_WEBHOOK=...
export SLACK_USER_ID=...                # メンション用、未設定可
export GOOGLE_SHEETS_ID=...
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'   # JSON文字列

# 投稿生成・配信（POST_SLOT 必須、0〜4）
POST_SLOT=0 python scripts/generate_post.py

# 他の主要スクリプト
python scripts/collect_metrics.py       # Threads インサイト取得 → メトリクスDB upsert
python scripts/analyze_competitors.py   # 競合投稿DB → Claude 分析 → Slack
python scripts/weekly_report.py         # 週2回改善レポート
MODE=free python scripts/generate_note.py       # note ドラフト（free または paid）
python scripts/analyze_note_performance.py      # note 週次パフォーマンス分析
python scripts/notify_db_update_reminder.py     # DB 更新リマインド Slack 通知
```

手動トリガー: GitHub → Actions → 該当ワークフロー → **Run workflow**。ワークフローには全て `workflow_dispatch` が入っている。

テストスイート・linter は存在しない。変更検証は「該当ワークフローを workflow_dispatch で手動実行してログと Slack を目視」が基本。

## High-level architecture

### Claude API 課金を守る preflight パターン
`scripts/preflight.py` の `run_all()` を **Claude API 呼び出し前に必ず実行**する。Threads 認証 / Slack Webhook / Google Sheets 接続のいずれかが失敗した時点で `SystemExit(1)` し、Anthropic API への無駄課金を防ぐのが目的。`generate_post.py:main` の先頭がこの契約を体現している。新しく Claude を叩くスクリプトを追加する際は同じ順番（preflight → 生成 → 配信 → 記録）を踏襲すること。

Slack の疎通チェックは「`text` フィールド欠落 JSON を POST → `HTTP 400 no_text` または `invalid_payload` を成功とみなす」サイレント方式。チャンネルに可視メッセージを残さないためにあえてエラー応答で判定しているので、書き換える際は挙動を壊さないこと（`preflight.py:check_slack`）。Slack 側は時期によってレスポンス文字列が `no_text` と `invalid_payload` で揺れるため両方受理する。

### 投稿タイプと POST_SLOT によるローテーション
投稿タイプは `config/strategy.json` の `post_rotation` 配列（長さ20、`permission`/`structure`/`personal`/`dialogue`/`opinion` のいずれか）。

決定式は `generate_post.py:determine_post_type`:
```
index = ((day_of_year - 1) * 5 + POST_SLOT) % len(rotation)
```

各時刻別ワークフローは環境変数 `POST_SLOT`（0〜4）を渡すことで、同じスクリプトを異なるスロットとして振る舞わせる。新しい投稿時刻を足すときは、既存スロットと衝突しない値を割り当てる。**`POST_SLOT=1` は特別扱い**で、`generate_post.py:build_prompt` が「フック形式（本文末尾をクリフハンガー→補足リプライ1で答え開示）」のプロンプトに切り替える。

`structure` 投稿は3投稿構成（本文＋補足リプライ1＋補足リプライ2）、他は2投稿構成。`_parse_post` が `【本文】`/`【補足リプライ1】`/`【補足リプライ2】`/`【補足リプライ】` マーカーでパースするので、プロンプト側の出力フォーマットを変更する場合はパーサと歩調を合わせること。

### ポジショニング・ペルソナは strategy.json に集約
投稿生成／競合分析／週次レポート／note 生成の4スクリプトすべてが `config/strategy.json` を読む。変更するときは下流全部に影響する前提で編集する:
- `positioning`: speaker / credibility（配列・3項目） / tobe / tobe_barrier / differentiation / `midend_product` (title/price_min/price_max) / `backend_product` (title/price)。商品体系は **バックエンド = 愛を深め続けるマインド構築講座（¥550,000）／ミドルエンド = 理想の相手の見つけ方ガイド（有料noteシリーズ ¥500〜4,980）** で構成され、`generate_note.py` の3テーマ提案プロンプトに「導線として機能する切り口を選ぶ」根拠として渡される。商品の `description` フィールドは持たず、ファネル上の役割は `funnel.midend_role` / `funnel.backend_path` に集約。
- `funnel`: 消費者心理5段階（認知→共感→興味→理解→納得）の `stages` 配列＋ `stage_intents`（動詞化 intent のマップ）、`sns_role` / `midend_role` / `backend_path` で SNS／midend／backend の役割を1〜2行で明示。`post_types.*.funnel_stage` から `stage_intents` の動詞を間接参照する設計。SNS（Threads／無料note）は認知/共感/興味段階を担当し、最大KPIは公式LINE登録。バックエンドは SNS から直接誘導しない（理解→納得→クロージングの3段階を経由）。
- `persona`: description / pain_points（プロンプトに注入される）
- `post_types`: 各タイプの label / description / ratio / funnel_stage（動詞形：「認知を獲得する」「共感を引き出す」等）
- `post_rotation`: 実際の出現順序（`ratio` は表示用で、実運用は rotation のカウント比で決まる）

発信者の事実情報（結婚・子どもの有無・キャリア年数など）と、そこから派生する自己開示スタンスは `docs/author_profile.md` に切り出してある。実行時には参照されず、`generate_post.py` / `generate_note.py` の共通ルールにハードコードされた制約の**根拠ドキュメント**として扱う。

投稿本文に関するポリシー（数字の丸め方、否定型フックの禁止、マイナス語での自己表現の禁止など）は `generate_post.py:build_prompt` の「共通ルール」ブロックに集中している。プロンプトを編集するときはそこを起点に探すこと。

### Google Sheets がシステムの唯一の永続ストレージ
DB は Google Sheets の 5 タブ。`scripts/sheets.py` が Python 側の全アクセスを仲介し、各タブ名を決め打ちで参照する（関心テーマDB だけは Claude Code Routines 側から Sheets MCP 経由で書き込まれるため `sheets.py` を通らない）:

| タブ名 | 役割 | 書き込み主 |
|---|---|---|
| 投稿DB | 投稿履歴（post_id / platform / post_type / content / posted_at / week_number / parent_post_id） | generate_post.py / post_note_promo.py |
| メトリクスDB | ER・インプレッション等（post_id で upsert、parent_post_id 列でスレッド帰属を保持） | collect_metrics.py |
| 競合投稿DB | 手動入力、analyzed=TRUE で済みマーク | 手動入力 / analyze_competitors.py |
| note投稿DB | note 記事のメタ（生成時に3テーマ提案を `status='proposed'` で3行 append・url/views/likes は手動） | generate_note.py |
| 関心テーマDB | ターゲット関心に沿う外部情報（news/trend/research 等）のネタ資料 DB | Claude Code Routines（`routines/interest_themes_collection.md`） |

gspread は数値IDを科学表記に暗黙変換するため、`sheets._normalize_id` で常に文字列に戻すこと（Threads の post_id は19桁前後の数値で、そのまま比較すると取りこぼす）。メトリクスの upsert は `bulk_upsert_metrics_records` が「1回の全読み取り → ID→行番号マップ → batch_update + append_rows」で API 呼び出しを最小化しているので、ループで write する書き方に戻さないこと。

### 重複投稿防止（Claude 非依存）
`generate_post.py` は生成後に `_jaccard_trigram_similarity`（文字トライグラム Jaccard 類似度）を計算し、同 post_type の直近14日投稿と比べて `SIMILARITY_THRESHOLD = 0.25` 以上なら `notify_slack_duplicate_warning` で警告する。**投稿自体は既に完了済みで自動削除はしない**—運用者が手動削除する前提。Claude API 消費を避けるため類似判定はローカル計算で完結させる設計なので、ここに LLM を差し込まない。

### note 生成パイプラインは別系統（ネタ出し専用）
`generate_note.py` は **本文を書かず、当日の note 記事テーマを 3 つ提案する**だけのスクリプト。出力は各テーマごとに `theme_label` / `title_candidate` / `reason`（200字以内・ペルソナの爬虫類脳/哺乳類脳に刺さる根拠）/ `target_brain`（reptilian / mammalian / both）。Claude API 呼び出しは 1 回のみで、過去テーマ（`note投稿DB.theme_label`）と意味的に被らないことだけを制約として渡す。
- 生成結果は `output/notes/YYYY-MM-DD_{free|paid}.md` に「## 提案1〜3」のフォーマットで書き出し、ワークフローが `git commit && git push` する（`note_generate.yml` 参照）。
- 同時に **note投稿DB に 3 行を `status='proposed'` で append**（同じ `generated_at` / `file_path` で 3 行・各 `title` は `title_candidate`、`theme_label` / `theme_description`(=reason) を埋める。`combination_pattern` / `*_type` / `ref_threads_post_ids` / `selling_element_ids` / `selected_*` 列は空欄）。運用者は 1 つを選んで note.com 用本文を別途作成し、投稿後に `url` / `status='posted'` を手動更新する。
- `analyze_note_performance.py` は同様に `output/reports/YYYY-MM-DD_note_analysis.md` をコミット。
- Slack 通知（`notify_slack_note`）は代表タイトル（先頭テーマ）+ GitHub blob URL のみで、本文・他2案は載せない（トークン節約＋詳細は GitHub view で確認）。
- 本文生成・組み合わせパターン選択・writing_guide 注入・selling_elements・angle_combo は **このスクリプトからは廃止済み**。`config/note_writing_guide.json` は現行 `generate_note.py` からは参照されないが、**運用者または Claude がこのリポジトリ内で note 記事本文を作成・編集するとき（提案された 3 テーマから 1 つ選んで note.com 用本文を書く工程）は必ずこのファイルを参照すること**（タイトル型 / 冒頭フック型 / 課題提示型 / 解決法型 / 高エンゲージメント実証パターン / Threads→note 引き継ぎ設計 / 有料note の売れる要素チェックリストが集約されている）。将来的に本文生成を再開する可能性も考えてファイル自体は残置している。

### note誘導Threads配信（3日に1回 20:00 JST）
`scripts/post_note_promo.py` は当日の `output/notes/YYYY-MM-DD_free.md` を読み、note記事を読みたくさせる「フック本文＋補足リプライ1＋URL単独リプライ2」の3投稿構成スレッドを配信する。
- 配信頻度は **3日に1回**。cron は毎日 20:00 JST に起動するが、スクリプト先頭で `date.toordinal() % 3 != 0` の日はSlack通知なしで即終了する。`*/3` 系cronだと月末で間隔が崩れる（例: 31日→翌月1日が1日間隔）ため、通日ordinal剰余で常に3日固定間隔を維持する設計。頻度を変える場合はスクリプトの剰余条件を編集する。
- URLは Claude を通さず、note投稿DB の **`url` 列**（手動入力、generated_at 当日かつ type=free の行）から取得する（`sheets.get_note_url_by_date`）。
- 当日note原稿が無い／URL列が空のいずれかに該当した場合、preflight および Claude API 呼び出しの**前**にスキップ判定し、`notify_slack_note_promo_skip`（メンション付き）で運用者に通知して終了する（無駄課金防止）。
- 本スクリプトのフック設計ルールは `generate_post.py` の共通ルールを継承せず、**この用途専用の独立した「爬虫類脳直撃のフック」プロンプト**を持つ。投稿スタイルを統一しに行かないこと（誘導目的が異なる）。
- 投稿DBには `post_type="note_promo"` で記録される。`notify_slack.POST_TYPE_LABELS` にも `note_promo: "note誘導系"` を追加済み。`strategy.json:post_rotation` には**入れない**（ローテーションに乗せない特殊スロット）。

### Threads API の2段階投稿
`post_threads.py:post_to_threads` は `threads` エンドポイントでコンテナ作成 → 5秒 sleep → `threads_publish` で公開、という2ステップ。セルフリプライも同じ関数に `reply_to_id` を渡して再帰的に呼ぶ。本文投稿直後にリプライを投げるとコンテナ処理が間に合わないため、`generate_post.py:main` 側でも追加の `time.sleep(5)` を入れている。タイミングを詰めると Threads 側でコンテナエラーになるので短縮しないこと。

ルート投稿だけでなくセルフリプライ1・2も投稿DBへ記録する（`parent_post_id` 列にルートの post_id を入れる）。これにより `collect_metrics.py` がリプライのインプレッション/いいね/返信数も拾い、メトリクスDBにも `parent_post_id` 列で帰属スレッドを保持する。**セルフリプライ2はThreads API上はリプライ1への返信だが、データ管理上の `parent_post_id` はルート（threads_id）で揃える**（「どのスレッドの返信か」を一意に集約するため）。重複チェック用の `get_recent_posts_content` と週次/note分析用の `get_weekly_data` は `parent_post_id` が空欄の行（=ルート）のみ返す仕様で、過去のリプライ本文が類似度判定や ER 集計に混ざらないようにしている。

### Slack 通知の責務分岐
`notify_slack.py` には用途別に複数関数がある。使い分け:
- `notify_slack`: 投稿完了（Header + 本文 + コンテキスト）
- `notify_slack_report`: 改善レポート・競合分析レポート（本文 or Actions ログ URL）
- `notify_slack_note` / `notify_slack_note_analysis`: note 関連、本文ではなく GitHub URL を送る
- `notify_slack_duplicate_warning`: 類似投稿警告（メンション付き）
- `notify_slack_db_update_reminder`: 分析前の DB 手動更新リマインド
- `notify_slack_note_promo_skip`: note誘導Threads投稿のスキップ通知（原稿不在 or URL未入力／メンション付き）
- アクション要求系（警告・リマインド・レポート完成）は `SLACK_USER_ID` が設定されていれば `<@UXXX>` メンションを頭につける（`_user_mention_prefix`）。自動完了通知にはメンションを付けない慣習。

### Claude Code Routines で走る別系統ジョブ
「関心テーマDB 収集」は **GitHub Actions ではなく Claude Code Routines（claude.ai）で実行される別系統ジョブ**。仕様は `routines/interest_themes_collection.md`。設計上の違い:

- 課金源が `ANTHROPIC_API_KEY`（従量）ではなく **Claude.ai サブスクリプション枠**。そのため `preflight.py` の「Claude API 課金を守る」契約の外側で動く
- 実行基盤が Anthropic 管理インフラ。ローカル再現不可（`python scripts/...` では動かせない）
- Sheets 書き込みは `sheets.py` を経由せず Sheets MCP 経由で直接。gspread の `_normalize_id` も通らないので、関心テーマDB は ID 正規化を必要とするカラムを持たせない方針
- 下流スクリプトは関心テーマDB を**参照しない**（現状維持）。将来注入を始める場合は `sheets.py` に読み取り関数を追加して `generate_post.py:build_prompt` に差す
- 失敗時・情報不足時の Slack 通知規約はプロンプト側に埋め込み済み（成功・失敗・高スコア item ゼロの 3 系統で必ず 1 通は出す）

Python スクリプトからこの DB を触る予定ができるまで、関心テーマDB は「運用者が目視でネタを拾う資料置き場」として独立運用する。

## Workflow スケジュール（JST／現行）

| Workflow | 時刻 / cron | POST_SLOT | 用途 |
|---|---|---|---|
| post_0805.yml | 毎日 08:05 | 0 | 投稿生成・配信 |
| post_0955.yml | 毎日 09:55 | 0 | 投稿生成・配信 |
| post_1145.yml | 毎日 11:45 | 1 | 投稿生成・配信（フック形式スロット） |
| post_1515.yml | （スケジュール停止中、手動のみ） | 2 | 投稿生成・配信 |
| post_1805.yml | 毎日 18:05 | 3 | 投稿生成・配信 |
| note_promo.yml | 3日に1回 20:00（cronは毎日／scriptが date.toordinal() % 3 で間引き） | — | 当日free noteを読みたくさせる3投稿構成スレッド（フック→補足→URL単独）|
| post_2100.yml | 毎日 21:02 | 4 | 投稿生成・配信 |
| daily_metrics.yml | 毎日 06:00 | — | 直近30日分のメトリクス upsert |
| competitor.yml | 火・金 08:00 | — | 競合投稿DB の未分析行を分析 |
| weekly_report.yml | 水・土 09:00 | — | 直近4日＋競合で改善レポート |
| note_generate.yml | 毎日 07:00 | — | 無料note ドラフト生成・自動コミット |
| note_analyze.yml | 月 10:00 | — | note 4週分析・レポートをコミット |
| db_update_reminder.yml | 日/月/木 01:00 | — | 分析前の DB 更新リマインド |

cron は UTC 指定。JST と9時間ずれるので、時刻を編集するときは両方ずらす必要がある点に注意。

README.md / DESIGN.md の時刻表は古い時代（post_0700 系）の名残りなので、ワークフロー実体と差異があるときは **ワークフローファイルが真**。

## Conventions specific to this repo

- すべての Python コード・コメント・ログ・プロンプト・Slack メッセージは **日本語**。生成されるコンテンツも日本語前提。
- Claude モデルは全スクリプトで `claude-opus-4-6`。モデルを変える場合は `grep -rn "claude-opus" scripts/` で網羅的に置換する。
- LinkedIn 関連コード（`post_linkedin.py`、`collect_metrics.py` 内の `collect_linkedin_metrics`、各ワークフローの `LINKEDIN_*` secret）は **意図的にコメントアウトで残されている**。再開時の差分を小さく保つ方針なので、「使われていないから」という理由で削除しない。
- 投稿の `post_type` は `permission` / `structure` / `personal` / `opinion` / `dialogue` の5種＋note誘導専用の `note_promo`。`opinion` は現状 ratio=0 で停止中だが、ラベル辞書やパースからは除外しない。`note_promo` は `post_rotation` に乗らない特殊スロットで `post_note_promo.py` のみが書き込む。
- `output/notes/` と `output/reports/` の Markdown は GitHub Actions bot が自動コミットする。手でコミットする機会は通常ない。
- 類似度閾値 `SIMILARITY_THRESHOLD = 0.25` はチューニング済み。上げると警告漏れ、下げるとノイズ、の観察を踏まえて決まった値なので触る前に値の変更理由を明示すること。
- `scripts/generate_post.py` / `scripts/generate_note.py` のプロンプト共通ルール／type_specific_rules／出力フォーマット、および `config/strategy.json` の `post_types.*.description` / `positioning` / `persona` など **Claude へ注入されるルール・説明文を追加・編集するときは、既存文との概念／意味内容の重複を必ず事前チェックする**こと。`description`（`build_prompt` 冒頭で注入）と `type_specific_rules`（投稿タイプ別ブロック）で同じ指示が2回並ぶ・共通ルール同士で締め方や禁止事項が二重定義される、といった事故が起きやすい。編集前に `grep` などで重複キーワード（「〜禁止」「〜しない」「〜で締める」等）を横断確認する。**重複が見つかった場合は自動判断で統合・削除せず、重複箇所と選択肢（どちらを残すか／統合するか）をユーザーに提示して判断を仰ぐこと。**
