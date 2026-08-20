# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

SNS運用（Threads中心、note副次）の全工程—投稿生成→配信→メトリクス収集→競合分析→戦略改善レポート—を GitHub Actions + Python + Claude API で自動化する。ランタイムは全て GitHub Actions runner 上で、`pip install -r requirements.txt` → `python scripts/<name>.py` という単純なパターン。ローカルテスト・ビルド・lint は無い。

**現行の稼働形態（2026-07-30〜）**: Threads への自動投稿は停止し、**毎日 07:00 JST に投稿アイデアを3案提案して Slack へ送る**運用に切り替えている（`post_ideas.yml` / `scripts/generate_post_ideas.py`）。本文執筆と Threads への投稿は運用者が手動で行う。旧・自動投稿系（`post_*.yml` 6本 + `generate_post.py`）は `schedule` をコメントアウトして残置してあり、`workflow_dispatch` での手動実行と、本文執筆ルールの参照元としては生きている。

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

# 投稿アイデア生成（現行の主系統・毎日07:00 JST）
python scripts/generate_post_ideas.py

# 投稿生成・配信（自動実行は停止中。手動実行のみ。POST_SLOT 必須、0〜4）
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

`run_all(checks=...)` で実行するチェックを絞れる（既定は `("threads", "slack", "sheets")` の全実行なので既存呼び出しは無変更で従来通り）。**Threads へ投稿しないスクリプトは `threads` を外す**こと—Threads トークン失効が Threads と無関係な処理まで巻き込んで止めるのを防ぐため。`generate_post_ideas.py` は `checks=("slack", "sheets")` で呼んでいる。

Slack の疎通チェックは「`text` フィールド欠落 JSON を POST → `HTTP 400 no_text` または `invalid_payload` を成功とみなす」サイレント方式。チャンネルに可視メッセージを残さないためにあえてエラー応答で判定しているので、書き換える際は挙動を壊さないこと（`preflight.py:check_slack`）。Slack 側は時期によってレスポンス文字列が `no_text` と `invalid_payload` で揺れるため両方受理する。

### 投稿アイデア生成パイプライン（現行の主系統）
`scripts/generate_post_ideas.py` は **本文を書かず、当日の Threads 投稿アイデアを3案提案する**だけのスクリプト。Threads API には一切触らない。出力は各案ごとに `post_type`（Python 側がローテーションで決定）/ `priority`（1〜3の順列・1が本日の推し）/ `theme_label`（8〜18字）/ `ideal_state`（理想状態の言語化・15〜35字）/ `method_name`（実現手段の名前・8〜20字）/ `angle`（切り口・80字以内）/ `hook_candidates`（**型違いで2案**・各 `type` と `text`）/ `reply_items`（`method_name` の中身＝実行行動・3〜5個各25字以内）/ `method_caveat`（手順を効かせる条件・30〜60字）/ `follow_cta`（フォロー誘導1行・25〜45字）/ `selection_note`（選考基準のどれで引っかけるか・80字以内）/ `reason`（150字以内）。Claude API 呼び出しは1回のみ、`max_tokens=5000`。

- 生成結果は `output/ideas/YYYY-MM-DD.md` に「## 提案1〜3」のフォーマットで書き出し、ワークフローが `git commit && git push` する（`post_ideas.yml` 参照）。案は割り当て順（＝ローテーション順）に並べ、推し順は優先度欄で示す（並べ替えない）。
- Slack 通知（`notify_slack_post_ideas`）は各案の「タイプ／テーマラベル／設計骨格（`ideal_state` ／ `method_name` を🎯行で併記）／フック候補2案」だけを載せ、`angle` / `reply_items` / `method_caveat` / `follow_cta` / `reason` は GitHub blob URL 側で確認させる（note 通知と同じ流儀）。骨格はフックの良し悪しを判断する基準そのものなのでフックと並べる。`priority=1` の案には ⭐ を付ける。生成完了通知なのでメンションは付けない。生成失敗時のみ `notify_slack_post_ideas_failure`（メンション付き）で停止理由を通知して `SystemExit(1)`。
- **テーマ重複回避の主軸は `output/ideas/*.md` の読み戻し**（`load_recent_idea_labels`・直近30日）。自動投稿を止めた以上、投稿DBには新規行が積まれず14日で空になるため。投稿DB（直近14日のルート投稿・`load_recent_posts`）も併せてプロンプトに渡すが、Sheets 読み取り失敗時は空リストで続行し生成自体はブロックしない。
- Markdown からの読み戻しは**3系統ある**（すべて `_iter_recent_idea_files` → `_load_recent_matches` 経由）。**`save_ideas_md` の書式を変えるときは3つの正規表現を必ず揃える**こと（崩れても例外は出ず、重複回避・偏り回避が黙って効かなくなる）:
  - 見出し行 `## 提案N: <theme_label>` → `_IDEA_HEADING_RE`（テーマ重複回避・直近30日）
  - 理想状態行 `- **理想状態**: <ideal_state>` → `_IDEAL_STATE_RE`（理想状態の収束回避・直近30日）。テーマラベルが違っても `ideal_state` が「浮気されない」「愛される」に毎回収束するとフックが同じ顔になるため、テーマとは別軸で被りを見せる
  - フック候補行 `  N. （型名）本文` → `_HOOK_TYPE_RE`（フック型の偏り回避・直近14日）。型ごとの使用回数をプロンプトに見せて、特定の型に固まるのを防ぐ
- **フック型カタログ（型F〜型I＋型B）は `HOOK_TYPES` 定数**に型名と1行要約だけを持つ。型番号は `generate_post.py` の型カタログ（型A〜型I）と**共有**しているので、型を追加・改名するときは両方を揃えること。詳細仕様の正本は `generate_post.py` 側。
- **【投稿の設計骨格】ブロック（`ideal_state` ＋ `method_name` の2層）は、実測の高パフォーマンス投稿4本から起こした投稿全体の骨格**（2026-08-12・いいね10以上／リポスト1以上の投稿群を分解）。「ペルソナがぼんやり理想だと思っている状態の言語化」＋「それを実現する手段の名前」をルート1行に畳み、補足リプライ1で手段の中身を実行行動として開く形が共通していた。設計上の要点:
  - `hook_candidates` / `reply_items` / `method_caveat` / `follow_cta` は**すべてこの2層から導出させる**。フィールドを独立に埋めさせるとフックとリプライが「状態→実現手段」のペアにならず、リプライが洞察の羅列になる（骨格導入前の実出力で起きていた）
  - `follow_cta` の〇〇は `ideal_state` を一人称の願いに言い換えたものにし、語彙をずらさない
  - **フック型カタログの型F（ベネフィット直球型）とはレイヤーが違う**。型F＝「骨格をルート1行に畳む書き方」、設計骨格＝「投稿全体の設計」。型Fは温存し、骨格は上位レイヤーとして新設した（2026-08-12・運用者判断）。型Fの spec に骨格を吸わせて一本化する案は見送っている
  - **`HOOK_TYPES` の spec は「骨格の2層をどう1行に落とすか」の変換規則として書く**（2026-08-12 に重複整理）。骨格導入直後は型Fの spec が骨格そのものの言い換えで二重定義だったため、5型すべてを `ideal_state` / `method_name` の語彙で書き直してある。型Bは「`ideal_state` に向かう場面から入り、`method_name` は補足リプライ1で合流させる」と接続してあり、これで場面型も骨格から外れない
  - 重複整理で**移動した記述**（元の場所に書き戻さないこと）: 「賛否が割れてよい」は型Hの spec からテーマ選考の優先基準3へ一本化（型Hの使用は基準3の枠＝3案中1案に収める、とフック作成ルールに明記）／恐れ・欲しい結果の例示は優先基準1から骨格第1層へ集約（基準1は骨格第1層への参照だけを持つ）／「一般論の言い換えにしない」は骨格第2層から削除（提案ルールの「AIでも量産できる切り口は採らない」がカバー）。第2層には代わりに造語ガード（〇〇効果・〇〇の法則にしない）を置き、優先基準1の「心理メカニズムが主役のテーマは採らない」と整合させてある
  - **重複を承知で残している箇所**（運用者判断で削除しなかったもの。整理する場合は勝手に判断せず確認すること）: `strategy.json:post_types.structure.description` の「今日から動ける形まで開示」（5スクリプトが読む共通定義なので削ると波及する。`reply_items` のルールとは粒度が違う＝前者は方針、後者は出力仕様）
- プロンプトが持つルールは「テーマ選考の優先基準」＋「投稿の設計骨格」＋「アイデア設計」＋「`hook_candidates` の書き方」に限定してある。フック制約は `generate_post.py` の共通ルールのうちフックに効くもの（否定型の入り禁止／抽象標語で締めない／研究・統計の引用禁止／読者を優劣で分類する対比の禁止／`思考・行動のクセ` の読者所有明示／emダッシュ禁止／一人称「僕」）だけを**最小限のサブセットとして**持たせている。**本文執筆時のルール（共通ルール全文／型カタログ）の正本は引き続き `generate_post.py:build_prompt`** で、運用者または Claude が Threads 本文を書くときはそちらを参照する。アイデア側のプロンプトを共通ルールの写しに育てないこと（二重管理になり、片側だけ古くなる）。
- **テーマ選考の優先基準5項目**（一次感情への直結／夫側の実感の翻訳／賛否が割れる論点の許容／男女どちらが読んでも成立／読者のライフステージ特定性）のうち基準1〜4は、実測の高パフォーマンス投稿（2026-08-08 の運用者観察：読者の約7割がルート1行目だけでフォローを判断、否定的な反応が着火剤になって閲覧数が伸びた、フォロワーは女性優勢〔当時の観察値は約4:6。実測は下記「フォロワー構成の実測」を参照〕）を反映して入れたもの。基準5 は2026-08-13 のフォロワー構成実測を反映して追加した（同項参照）。基準2には当初「夫にこう接すれば夫が動く」という夫を操作する手順に着地させない歯止めを置いていたが、**運用者の判断で削除済み**（2026-08-08）。夫側の実感の翻訳を「夫はこういう時こう動く」まで書き切ってよい。同じ判断で `strategy.json:positioning.differentiation` も『まず自分を満たす／自己犠牲の優しさは続かない／自分の内側を見直す／テクニックではなく内面を磨く』の四つから、**『浮気や離婚は許さない』『自然体でいるために女性の行動変容を促す』の二つに差し替え済み**（2026-08-08）。内面重視の縛りを外して行動変容側に振る意図なので、**この歯止めを再び入れる／differentiation を内面寄りに戻す判断は、勝手にやらず運用者に確認すること**（一度外すと決めた制約なので、良かれと思って戻さない）。

  この差し替えに合わせて、内面寄りだった記述を横断的に行動変容寄りへ揃えてある（2026-08-08・運用者判断）。**片方だけ内面寄りに戻すと軸がねじれる**ので、戻すなら一括で戻すこと:
  - `strategy.json`: `speaker`（内側を整える→行動を変えていく）／ `post_types.structure.description`（心理学・脳科学で構造化→変えられる行動を構造化）／ `post_types.personal.description`（内面と向き合って脱却→実際の行動を変えて脱却）
  - `strategy.json`: `tobe_barrier` と `persona.pain_points`（8項目に増）へ「浮気・離婚への恐れ」を追加。新 `persona.description` の恐れが悩みリストに実体を持たず浮いていたのを解消したもの
  - `generate_post.py` の体系化系専用ルール2ブロック（通常／フック形式）から「心理学・脳科学の専門的トーン」「『再現性』『マインド』『思考・行動のクセ』『書き換え』のいずれかを含める」強制を削除
  - `config/note_writing_guide.json` 3箇所から「テクニック・ノウハウではなく思考・行動のクセの問題だ」という再定義の強制を削除（`high_performance_patterns.intellectual_curiosity_paradox` / `combination_patterns.patterns[2].instructions.problem` / `paid_note_selling_elements.elements[2]`）
  - `credibility` は発信者の事実記述なので**意図的に据え置き**（内面と向き合って脱却した経験、は事実として残す）
  - **未対応**: `docs/seminar_slides.md` のスライド8「テクニック型ノウハウの限界」（対比図を含む1セクション全体が旧軸前提）と `docs/consultation_script.md`。実行時に読まれない販売資料で、書き換えは体裁調整でなく内容判断になるため手を入れていない
- **フォロワー構成の実測**（2026-08-13・Threads インサイト／フォロワー103名。**割合の母数は103ではなく、年齢・性別が取れている87名**）。基準5 と、フックの属性語ルールの根拠:
  - 性別は 女性33 ／ 男性13 ／ その他41（＝性別未設定と推定）。開示ベースの女:男は約 **7:3**。旧記述の「約4:6」は更新済み
  - 女性33名の **85%（28名）が35歳以上**（35-44が15名・45-54が8名・55-64が4名・65+が1名）で、`persona.description` の「30代後半〜40代中心」と一致する。コアど真ん中（女性35-54）は **23名**
  - 全体の最大ボリューム帯は 25-34（25名）だが、そこに女性は5名しかいない（男性7名＋未設定13名）。旧コンセプト（`docs/brand_strategy.md` の30代エンジニア向け）時代の残存フォロワーと推定され、**この帯に寄せない**方針
  - **この構成を根拠に基準4（男女どちらが読んでも成立）を弱めないこと**。フォロワー構成は「フォローした人」であって「投稿を見た人」ではなく、47%が性別未設定で比率自体の信頼度も低い。基準4 は2026-08-08 の運用者観察（否定的な反応が着火剤になって閲覧数が伸びた）という別系統の証拠に基づく
  - セル単位の数字（例: 女性55-64=12.12%＝実数4名）は1名の増減で数ポイント動くため、意思決定の根拠にしない。使えるのは「女性の85%が35歳以上」程度の粗い事実まで。次に見直すのは200〜300名到達時が妥当
- `follow_cta` は `generate_post.py` の structure 3投稿目にある「続きは note に書く」型フォロー誘導と**用途が重なる**。重ねると1スレッドでフォロー誘導が2回出るため、プロンプト側に「structure ではどちらか一方だけを使う（案としては常に出し、運用者が選ぶ）」と明記してある。どちらかを削る判断はしていないので、統合するなら運用実績を見てから。

### 投稿タイプのローテーション
投稿タイプは `config/strategy.json` の `post_rotation` 配列（長さ20、`permission`/`structure`/`personal`/`dialogue`/`opinion` のいずれか）。

現行の決定式は `generate_post_ideas.py:determine_post_types`（1日1回・3案まとめて取り出す）:
```
index = ((day_of_year - 1) * 3 + i) % len(rotation)   # i = 0..2、day_of_year は JST 基準
```
rotation長20と3案は互いに素なので20日で全要素を1周し、出現比は `ratio`（structure 40% / dialogue 20% / personal 15% / opinion 15% / permission 10%）と一致する。案数を変えるときは `IDEA_COUNT` を変えるが、`len(rotation)` と互いに素でない値（4・5・10等）を選ぶと一部の index が永久に選ばれなくなる点に注意。連続する3案に同じタイプが混じることはある（例: `structure, opinion, structure`）ので、プロンプト側で「同じタイプが複数割り当てられた場合は切り口を明確に変える」と指示している。

停止中の自動投稿系（`generate_post.py:determine_post_type`）の決定式は以下で、こちらは残置してある:
```
index = ((day_of_year - 1) * 5 + POST_SLOT) % len(rotation)
```

各時刻別ワークフローは環境変数 `POST_SLOT`（0〜4）を渡すことで、同じスクリプトを異なるスロットとして振る舞わせる。新しい投稿時刻を足すときは、既存スロットと衝突しない値を割り当てる。**`POST_SLOT=1` は特別扱い**で、`generate_post.py:build_prompt` が「フック形式（本文末尾をクリフハンガー→補足リプライ1で答え開示）」のプロンプトに切り替える。

`structure` 投稿は3投稿構成（本文＋補足リプライ1＋補足リプライ2）、他は2投稿構成。`_parse_post` が `【本文】`/`【補足リプライ1】`/`【補足リプライ2】`/`【補足リプライ】` マーカーでパースするので、プロンプト側の出力フォーマットを変更する場合はパーサと歩調を合わせること。

### ポジショニング・ペルソナは strategy.json に集約
投稿アイデア生成／投稿生成／競合分析／週次レポート／note 生成の5スクリプトすべてが `config/strategy.json` を読む。変更するときは下流全部に影響する前提で編集する:
- `positioning`: speaker / credibility（配列・3項目） / tobe / tobe_barrier / differentiation / `midend_product` (title/price_min/price_max) / `backend_product` (title/price)。商品体系は **バックエンド = 愛される自分を取り戻すパートナーシップ講座（¥550,000・講座型3〜10名・結婚歴3〜15年の既婚女性限定）／ミドルエンド = 夫に本音が言えなくなってきた時に読みたい愛されガイド（有料noteシリーズ ¥500〜4,980）** で構成され、`generate_note.py` の3テーマ提案プロンプトに「導線として機能する切り口を選ぶ」根拠として渡される。商品の `description` フィールドは持たず、ファネル上の役割は `funnel.midend_role` / `funnel.backend_path` に集約。
- `funnel`: 消費者心理5段階（認知→共感→興味→理解→納得）の `stages` 配列＋ `stage_intents`（動詞化 intent のマップ）、`sns_role` / `midend_role` / `backend_path` で SNS／midend／backend の役割を1〜2行で明示。`post_types.*.funnel_stage` から `stage_intents` の動詞を間接参照する設計。SNS（Threads／無料note）は認知/共感/興味段階を担当し、最大KPIは公式LINE登録。バックエンドは SNS から直接誘導しない（理解→納得→クロージングの3段階を経由）。
- `persona`: description / pain_points（プロンプトに注入される）
- `post_types`: 各タイプの label / description / ratio / funnel_stage（動詞形：「認知を獲得する」「共感を引き出す」等）
- `post_rotation`: 実際の出現順序（`ratio` は表示用で、実運用は rotation のカウント比で決まる）

発信者の事実情報（結婚・子どもの有無・キャリア年数など）と、そこから派生する自己開示スタンスは `docs/author_profile.md` に切り出してある。実行時には参照されず、`generate_post.py` / `generate_note.py` の共通ルールにハードコードされた制約の**根拠ドキュメント**として扱う。

投稿本文に関するポリシー（数字の丸め方、否定型フックの禁止、マイナス語での自己表現の禁止、`思考・行動のクセ` 語句の読者所有明示〔`あなたの／自分の` を冠する〕、研究結果・統計数値・著者名等の本文記載禁止 など）は `generate_post.py:build_prompt` の「共通ルール」ブロックに集中している。プロンプトを編集するときはそこを起点に探すこと。自動投稿は停止したが、**このブロックは Threads 本文執筆ルールの正本として引き続き有効**で、`generate_post_ideas.py` が提案した3案から1つ選んで本文を書く工程では必ずここを参照する（`config/note_writing_guide.json` が note 本文執筆の参照元として残置されているのと同じ扱い）。

**型F〜型Iは2026-08-12 まで各 `output_format` の【本文】に列挙されておらず、事実上選べない状態だった**（型カタログは「全タイプ」と書いてあるのに、structure/opinion/permission は型A・型C・型E、personal/dialogue は型A・型B・型D・型E しか許していなかった）。実測で伸びたのが型F・型Hだったため、全5ブロックの【本文】に型F〜型Iを追加し、【補足リプライ】側にも各型の開き方（型F・型I＝伏せた手段の中身／型G＝予告した件数どおり／型H＝断定の理由）を追記して解消済み。**型カタログに型を足すときは `output_format` 側にも必ず追加すること**（カタログだけ足しても選ばれない）。

ルート1行目の設計は同ブロック末尾の**型カタログ（型A〜型I）が正本**。型A〜型Eは場面・体験から入る従来型、**型F〜型I（ベネフィット直球／件数予告／逆説断定／状態提示切断）は実測の高パフォーマンス投稿から起こした「読者が得たい結果・避けたい状態を1行目に先置きする型」**で、2026-08-08 に追加した。型F〜型Iの追加に伴い共通ルール3項目を条件付きに変更してあり、**型カタログと下記3項目はセットで動く**ので片方だけ編集しないこと:
- 「抽象主語で始めず具体的な場面から入る」→ 型A〜型Eのみに適用（型F〜型Iは場面から入らなくてよい。ただし1行目に読者の生活語を1つ以上入れる）
- 「読者を優劣・属性で分類する対比の禁止」→ 対比の禁止は維持したまま、型Iのような片側の状態の単独提示は可
- 「決めフレーズ・標語化した断定の禁止」→ 禁止対象は抽象概念を対比させた標語（「テクニックじゃなくマインド」等）に限定し、型Hのような具体的な状態・行動を指す断定は可

体系化系専用ルール（通常／フック形式の2ブロック）の「補足リプライ1の行動は1アクションに絞る」は、**2026-08-12 に条件付きで緩和済み**。旧ルールは「型Gで件数を予告した場合のみ列挙可（予告なしに手順を長く列挙しない）」だったが、実測の高パフォーマンス投稿は**件数予告なしの型F（手段の名前を提示）で4〜5手順を並べて伸びていた**ため、型F・型Iで手段の名前を出した場合も3〜5個の行動を並べてよい形に変えた。並べた場合は末尾に「効かせる条件」（順番の指定／前提の否定／添える態度のいずれか1つ）を添えるところまでが型。1アクションに絞るのは引き続き基本形で、ルートで件数も手段名も出していないのに列挙するのは禁止のまま。**この緩和は `generate_post_ideas.py` の `reply_items`（実行行動の列挙）・`method_caveat` とセット**なので、片方だけ元に戻すとアイデアと本文執筆ルールが噛み合わなくなる。

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

### note は単発2本モデル（現行・2026-08-13〜）
無料note 1本・有料note 1本を**単発で作り、そこにスキと購入を集中させる**運用に切り替えた。設計の正本は `docs/note_two_asset_design.md`。要点だけ:

- **無料note = 証明装置（KPIはスキ数）／有料note = 収益装置**。note は購入数を公開表示しないため、可視の社会的証明は無料note のスキ数でしか作れない
- テーマ軸は**2段構え**（2026-08-13 に入口を変更）: **入口＝「浮気されない妻の特徴」**（Threads 実測でいいね→閲覧→フォローの動線を作った不安。`pain_points` #4）／**着地＝「本音を言えなくなってきたこと」**（`midend_product.title` と直結しており、有料note がそのままミドルエンドの実体になる）。接続は無料note 内の逆説「浮気されない妻の特徴の正体＝本音が行き来していること」が担う。**入口を変えても着地と商品名は変えていない**（商品名の変更は運用者判断）
- 貫く理想状態は同じ骨格の裏表2形: 恐れの解消形「夫の浮気が頭をよぎらない毎日でいられる」（無料note 入口）／獲得形「夫に本音を言った翌朝も、いつも通りの空気でいられる」（ピン留め `docs/pinned_post_proposals.md` 案A・有料note）。無料note が記事内で両者を接続する
- 無料note の構成は「**理想状態の詳細描写 → 落とし穴 → 手段の名前 → ブリッジ**」の順（`note_writing_guide.json:free_mode_principles.structure_template_override`）。**手段より先に落とし穴を置く順序が設計の核心**で、逆にすると「読んで満足」で閉じられる。並べ替えないこと
- ここでの「落とし穴」は**読者が自己流でやろうとしてすでにハマっている場所**であって、「手段を実行した人の失敗」ではない（手段より前に置く構成のため）
- **日次生成（`generate_note.py` / `note_generate.yml`）は再開しない。`note_analyze.yml` も同様。`post_note_promo.py` / `note_promo.yml` も再開しない**（再開する場合、`post_note_promo.py` は当日ファイル＋当日DB行を見る日付結合のままなので、固定note参照への変更が前提。そのままだと常設1本モデルでは毎回スキップ通知が飛ぶ）
- 2026-08-13 に `note_writing_guide.json` から**連載前提の仕組みを削除**した（`pattern_distribution` / `combination_patterns` / `paid_mode_overrides` / `recommended_frequency` / 有料要素の「心理学・脳科学の根拠」）。削除の経緯と理由は `docs/note_two_asset_design.md` §7。**単発運用を続ける限り復活させない**

### note 生成パイプラインは別系統（ネタ出し専用・停止中）
`generate_note.py` は **本文を書かず、当日の note 記事テーマを 3 つ提案する**だけのスクリプト。出力は各テーマごとに `theme_label` / `title_candidate` / `reason`（200字以内・ペルソナの爬虫類脳/哺乳類脳に刺さる根拠）/ `target_brain`（reptilian / mammalian / both）。Claude API 呼び出しは 1 回のみで、過去テーマ（`note投稿DB.theme_label`）と意味的に被らないことだけを制約として渡す。
- 生成結果は `output/notes/YYYY-MM-DD_{free|paid}.md` に「## 提案1〜3」のフォーマットで書き出し、ワークフローが `git commit && git push` する（`note_generate.yml` 参照）。
- 同時に **note投稿DB に 3 行を `status='proposed'` で append**（同じ `generated_at` / `file_path` で 3 行・各 `title` は `title_candidate`、`theme_label` / `theme_description`(=reason) を埋める。`combination_pattern` / `*_type` / `ref_threads_post_ids` / `selling_element_ids` / `selected_*` 列は空欄）。運用者は 1 つを選んで note.com 用本文を別途作成し、投稿後に `url` / `status='posted'` を手動更新する。
- `analyze_note_performance.py` は同様に `output/reports/YYYY-MM-DD_note_analysis.md` をコミット。
- Slack 通知（`notify_slack_note`）は代表タイトル（先頭テーマ）+ GitHub blob URL のみで、本文・他2案は載せない（トークン節約＋詳細は GitHub view で確認）。
- 本文生成・組み合わせパターン選択・writing_guide 注入・selling_elements・angle_combo は **このスクリプトからは廃止済み**。`config/note_writing_guide.json` は現行 `generate_note.py` からは参照されないが、**運用者または Claude がこのリポジトリ内で note 記事本文を作成・編集するとき（現行は上記の単発2本モデルで note.com 用本文を書く工程）は必ずこのファイルを参照すること**（タイトル型 / 冒頭フック型 / 課題提示型 / 解決法型 / 高エンゲージメント実証パターン / Threads→note 引き継ぎ設計 / 有料note の売れる要素チェックリスト / `engagement_design_rules` に集約されている `inner_pattern_phrasing_rule`〔`思考・行動のクセ` を `あなたの／自分の` 付きで使う〕／ `negation_assertion_pattern_limit`〔『〇〇ではなく〇〇です』型レトリックの上限2箇所＋分散レトリック列挙〕／ `research_citation_rule`〔本文に出典情報を織り込まず `（※N）` 脚注マーカー＋記事末 `## 参考文献` セクションで管理〕 が集約されている）。将来的に本文生成を再開する可能性も考えてファイル自体は残置している。

### note誘導Threads配信（3日に1回 20:00 JST・停止中／再開しない）
> 2026-08-13 の単発2本モデル移行に伴い**再開しない**方針。再開する場合は下記の日付結合を固定note参照へ変更するのが前提（`docs/note_two_asset_design.md` §8）。以下は停止時点の仕様。

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
- 下流スクリプトは関心テーマDB を**参照しない**（現状維持）。将来注入を始める場合は `sheets.py` に読み取り関数を追加して `generate_post_ideas.py:build_prompt`（自動投稿を再開するなら `generate_post.py:build_prompt` も）に差す
- 失敗時・情報不足時の Slack 通知規約はプロンプト側に埋め込み済み（成功・失敗・高スコア item ゼロの 3 系統で必ず 1 通は出す）

Python スクリプトからこの DB を触る予定ができるまで、関心テーマDB は「運用者が目視でネタを拾う資料置き場」として独立運用する。

## Workflow スケジュール（JST／現行）

**稼働中**（`schedule` が有効なもの）:

**定時実行は `post_ideas.yml` の1本だけ**（2026-08-08〜）。他は全て `schedule` をコメントアウトしてある。

| Workflow | 時刻 / cron | POST_SLOT | 用途 |
|---|---|---|---|
| post_ideas.yml | 毎日 07:00 | — | 投稿アイデア3案の提案・`output/ideas/` へ自動コミット・Slack通知 |

**停止中**（`schedule` をコメントアウト。`workflow_dispatch` の手動実行のみ可能）:

| Workflow | 停止時の時刻 / cron | POST_SLOT | 停止時期 / 用途 |
|---|---|---|---|
| daily_metrics.yml | 毎日 06:00 | — | 2026-08-08〜／直近30日分のメトリクス upsert |
| db_update_reminder.yml | 日/月/木 01:00 | — | 2026-08-08〜／分析前の DB 更新リマインド |
| threads_token_reminder.yml | 毎日 12:00（失効7日前以内の日だけ通知／窓外はscriptが即終了） | — | 2026-08-08〜／Threadsトークン失効リマインド（`config/threads_token.json` の `token_updated_at` 起点） |
| post_0805.yml | 毎日 08:05 | 0 | 2026-07-30〜／投稿生成・配信 |
| post_0955.yml | 毎日 09:55 | 0 | 2026-07-30〜／投稿生成・配信 |
| post_1145.yml | 毎日 11:45 | 1 | 2026-07-30〜／投稿生成・配信（フック形式スロット） |
| post_1515.yml | 毎日 15:15 | 2 | 2026-07-30〜／投稿生成・配信 |
| post_1805.yml | 毎日 18:05 | 3 | 2026-07-30〜／投稿生成・配信 |
| post_2100.yml | 毎日 21:02 | 4 | 2026-07-30〜／投稿生成・配信 |
| competitor.yml | 火・金 08:00 | — | 2026-06-16〜／競合投稿DB の未分析行を分析 |
| weekly_report.yml | 水・土 09:00 | — | 2026-06-16〜／直近4日＋競合で改善レポート |
| note_generate.yml | 毎日 07:00 | — | 2026-06-16〜／無料note テーマ提案・自動コミット |
| note_analyze.yml | 月 10:00 | — | 2026-06-16〜／note 4週分析・レポートをコミット |
| note_promo.yml | 3日に1回 20:00（cronは毎日／scriptが date.toordinal() % 3 で間引き） | — | 2026-06-16〜／当日free noteを読みたくさせる3投稿構成スレッド（フック→補足→URL単独）|
| update_note_metrics.yml | （cronなし・元から手動専用） | — | note投稿DB のメトリクス更新（dry-run / apply） |

cron は UTC 指定。JST と9時間ずれるので、時刻を編集するときは両方ずらす必要がある点に注意。

**メトリクス収集・リマインド系を止めたことの含意**（2026-08-08）:

- `daily_metrics.yml` を止めたので、**メトリクスDBは自動更新されない**。Threads のインサイトが必要になったら `workflow_dispatch` で手動実行する。ただし Threads API のインサイトは取得可能期間に限りがあるため、長期間放置した分は遡って取れない可能性がある
- `threads_token_reminder.yml` も止めた。定時実行で Threads トークンを使うジョブが無くなったための措置だが、**トークンが失効しても通知されない**。`daily_metrics.yml` や自動投稿を再開するときは、このリマインドも併せて再開すること
- `db_update_reminder.yml` はリマインド先（note週次分析・競合分析）が既に停止中で単独で動かす意味が薄いため停止
- `post_ideas.yml` は Threads API に触れないので、トークンが失効していても動き続ける（preflight も `checks=("slack", "sheets")` で Threads を外してある）

README.md / DESIGN.md の時刻表は古い時代（post_0700 系）の名残りなので、ワークフロー実体と差異があるときは **ワークフローファイルが真**。

## Conventions specific to this repo

- すべての Python コード・コメント・ログ・プロンプト・Slack メッセージは **日本語**。生成されるコンテンツも日本語前提。
- Claude モデルは全スクリプトで `claude-opus-5`、effort は `output_config={"effort": "high"}` を明示指定。モデルを変える場合は `grep -rn "claude-opus" scripts/` で網羅的に置換し、`token_cost.py:_MODEL_PRICING` に料金行を追加する。
- **Opus 5 は `thinking` を省略すると adaptive thinking が ON になる**（Opus 4.6〜4.8 は省略＝OFF だった）。`max_tokens` は thinking と本文の合計に効くため、省略すると本文だけを見積もった値では途中で切れる。この既定に依存しないよう **全スクリプトが `thinking` を明示指定する**方針で、`generate_post.py` の structure のみ `{"type": "adaptive"}`（3投稿構成のため）、それ以外は全て `{"type": "disabled"}`。新しく Claude を叩くスクリプトを足すときも `thinking` を必ず明示すること。`generate_post_ideas.py` も `{"type": "disabled"}`。
- thinking ON のとき `content` の先頭は thinking ブロックになり得るので、本文は必ず `next((b.text for b in message.content if b.type == "text"), "")` で text ブロックを明示抽出する（`content[0].text` は AttributeError になる）。thinking OFF のスクリプトでも既定変更に備えて同じ書き方で統一している。
- `thinking={"type": "disabled"}` は **effort が high 以下のときのみ許可**。`xhigh` / `max` と組み合わせると 400 になる。
- LinkedIn 関連コード（`post_linkedin.py`、`collect_metrics.py` 内の `collect_linkedin_metrics`、各ワークフローの `LINKEDIN_*` secret）は **意図的にコメントアウトで残されている**。再開時の差分を小さく保つ方針なので、「使われていないから」という理由で削除しない。
- 投稿の `post_type` は `permission` / `structure` / `personal` / `opinion` / `dialogue` の5種＋note誘導専用の `note_promo`。`note_promo` は `post_rotation` に乗らない特殊スロットで `post_note_promo.py` のみが書き込む。
- `output/ideas/` と `output/notes/` と `output/reports/` の Markdown は GitHub Actions bot が自動コミットする。手でコミットする機会は通常ない。`output/ideas/` は投稿アイデアの履歴であると同時に**テーマ重複回避の入力そのもの**なので、過去ファイルを整理・削除すると重複チェックの効きが落ちる点に注意。
- 類似度閾値 `SIMILARITY_THRESHOLD = 0.25` はチューニング済み。上げると警告漏れ、下げるとノイズ、の観察を踏まえて決まった値なので触る前に値の変更理由を明示すること。
- `scripts/generate_post_ideas.py` / `scripts/generate_post.py` / `scripts/generate_note.py` のプロンプト共通ルール／type_specific_rules／出力フォーマット、および `config/strategy.json` の `post_types.*.description` / `positioning` / `persona` など **Claude へ注入されるルール・説明文を追加・編集するときは、既存文との概念／意味内容の重複を必ず事前チェックする**こと。`description`（`build_prompt` 冒頭で注入）と `type_specific_rules`（投稿タイプ別ブロック）で同じ指示が2回並ぶ・共通ルール同士で締め方や禁止事項が二重定義される、といった事故が起きやすい。編集前に `grep` などで重複キーワード（「〜禁止」「〜しない」「〜で締める」等）を横断確認する。**重複が見つかった場合は自動判断で統合・削除せず、重複箇所と選択肢（どちらを残すか／統合するか）をユーザーに提示して判断を仰ぐこと。**
