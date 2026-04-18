# sns-automation

SNS運用の全工程（投稿生成→配信→データ収集→戦略改善提案）を自動化するシステム。
GitHub Actions + Python + Claude API で構成し、**サーバーコスト¥0・月約90円**で常時稼働する。

---

## セットアップ

### 1. GitHubリポジトリの準備

```bash
git clone <このリポジトリのURL>
cd sns-automation
```

### 2. GitHub Secrets の登録

リポジトリの **Settings → Secrets and variables → Actions** で以下を登録する。

| Secret名 | 内容 | 取得先 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude APIキー | console.anthropic.com |
| `THREADS_USER_ID` | ThreadsユーザーID | Threads API |
| `THREADS_TOKEN` | Threadsアクセストークン | Meta開発者ポータル |
| `SLACK_WEBHOOK` | Slack Incoming Webhook URL | api.slack.com/apps |
| `GOOGLE_SHEETS_ID` | スプレッドシートID | Google SheetsのURL内 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service Account JSONの中身 | Google Cloud Console |
| `LINKEDIN_TOKEN` | LinkedIn APIトークン | ※一時無効化中 |
| `LINKEDIN_ORG_ID` | LinkedIn法人ページID | ※一時無効化中 |

### 3. Google Sheets の準備

新規スプレッドシートを作成し、以下の4シートを用意する。

| シート名 | 用途 |
|---|---|
| 投稿DB | 全投稿の記録（post_id / platform / post_type / content / posted_at / week_number） |
| メトリクスDB | エンゲージメントデータ（post_id / collected_at / likes / reposts / replies / impressions / engagement_rate） |
| 競合投稿DB | 手動入力の競合投稿データ（content / likes / replies / posted_at / thread_id / reply_order / analyzed） |
| note投稿DB | note記事のメタ情報（type / title / price / file_path / generated_at / status 他） |

スプレッドシートの共有設定で、Service Accountのメールアドレス（`GOOGLE_SERVICE_ACCOUNT_JSON` 内の `client_email`）を**編集者**として招待する。

### 4. 必要な Threads API 権限

| 権限 | 用途 |
|---|---|
| `threads_basic` | 自アカウントの基本情報取得 |
| `threads_content_publish` | 投稿の作成・公開 |
| `threads_manage_insights` | 投稿のインサイト取得 |

---

## 動作スケジュール

| ワークフロー | 実行時刻（JST） | 内容 |
|---|---|---|
| post_0700.yml | 毎日 07:00 | 投稿生成・配信 |
| post_0730.yml | 毎日 07:30 | 投稿生成・配信 |
| post_2045.yml | 毎日 20:45 | 投稿生成・配信 |
| post_2100.yml | 毎日 21:00 | 投稿生成・配信 |
| post_2130.yml | 毎日 21:30 | 投稿生成・配信 |
| daily_metrics.yml | 毎日 22:00 | エンゲージメント収集 |
| competitor.yml | 火・金 08:00 | 競合分析 |
| weekly_report.yml | 毎週月曜 09:00 | 改善レポート生成・Slack通知 |

---

## Post hh:mm 実行時の処理フロー

```
1. 事前チェック（preflight）
   ├── Threads: トークン有効性確認
   ├── Slack: Webhook URL疎通確認
   └── Google Sheets: 接続・シート存在確認
   ↓ いずれか失敗 → 処理中断（Claude API未使用）

2. strategy.json 読み込み

3. 投稿タイプ決定（ローテーション）
   - 日付 × スロット番号（0〜4）で post_rotation 配列を循環
   - 結果: permission / structure / personal のいずれか

4. Claude API → 投稿文生成（140〜200文字）

5. Threads API → 自動投稿（コンテナ作成 → 公開の2段階）

6. Slack → 同じ内容を草稿通知（X・note用にコピペ）

7. Google Sheets「投稿DB」→ 投稿IDと本文を記録
```

---

## 投稿タイプ設計

| タイプ | 比率 | 目的 |
|---|---|---|
| permission（許可系） | 50% | 共感・拡散・フォロー獲得 |
| structure（体系化系） | 30% | 権威性・信頼・法人リーチ |
| personal（自己開示系） | 20% | 説得力・AとBの橋渡し |

ローテーション（`config/strategy.json` の `post_rotation`）：

```
[permission, permission, permission, structure, structure,
 personal, permission, permission, permission, structure]
```

---

## ポジショニング設定（`config/strategy.json`）

投稿生成プロンプトの基盤となる設定。変更する場合はこのファイルを編集する。

```json
{
  "position": "ハイパフォーマーのための、コンディション設計の専門家",
  "concept": "スマートに勝ち続ける設計力",
  "differentiation": "意思力に頼らないパフォーマンス設計"
}
```

---

## コスト

### Claude API 使用量内訳（月間）

モデル: `claude-opus-4-6`（$15 / MTok 入力・$75 / MTok 出力）

| スクリプト | 呼び出し頻度 | 月間呼び出し数 | 入力 tok/回 | 出力 tok/回 | 月間小計 |
|---|---|---|---|---|---|
| generate_post（Threads用） | 6回/日 | 180回 | 550 | 160 | $2.97 + $2.16 |
| generate_post（X用） | 6回/日 | 180回 | 550 | 160 | ↑ 合算 |
| analyze_competitors | 火・金 × 競合3件 | 24回 | 900 | 150 | $0.32 + $0.27 |
| weekly_report | 週1回 | 4回 | 1,500 | 800 | $0.09 + $0.24 |

> 競合投稿数は `競合投稿DB` シートへの手動入力数に依存。上記は3件想定。

| 集計 | 入力 | 出力 | 合計 |
|---|---|---|---|
| 月間トークン数 | 約 226,000 tok（0.226 MTok） | 約 64,400 tok（0.064 MTok） | — |
| 月間コスト | $3.38 | $4.83 | **約 $8.21（約 1,230 円）** |

> ※ X用コンテンツを別途生成する構成に変更したため、投稿生成の API 呼び出しは 1実行あたり2回（Threads用 + X用）。

### インフラコスト

| 項目 | 月額 |
|---|---|
| Claude API | 約 $8.21（約 1,230 円） |
| GitHub Actions | 無料（月2,000分枠、実使用量 約120分） |
| サーバー | ¥0 |

---

## ファイル構成

```
sns-automation/
├── .github/workflows/
│   ├── post_0700.yml
│   ├── post_0730.yml
│   ├── post_2045.yml
│   ├── post_2100.yml
│   ├── post_2130.yml
│   ├── daily_metrics.yml
│   ├── competitor.yml
│   └── weekly_report.yml
├── scripts/
│   ├── generate_post.py        # 投稿生成・配信メインスクリプト
│   ├── preflight.py            # 事前チェックモジュール
│   ├── post_threads.py         # Threads API 投稿
│   ├── post_linkedin.py        # LinkedIn API 投稿（※一時無効化）
│   ├── notify_slack.py         # Slack 通知
│   ├── collect_metrics.py      # エンゲージメント収集
│   ├── analyze_competitors.py  # 競合分析
│   ├── weekly_report.py        # 週次レポート生成
│   └── sheets.py               # Google Sheets 読み書き
├── config/
│   └── strategy.json           # ポジショニング・投稿タイプ配分設定
└── requirements.txt
```

---

## ログ確認・手動実行

**ログ確認：** GitHub → Actions タブ → 対象ワークフロー → 実行履歴をクリック

**手動実行：** GitHub → Actions タブ → 対象ワークフロー → **Run workflow** ボタン

---

## 現在の無効化状況

| 機能 | 状態 |
|---|---|
| LinkedIn自動投稿 | 無効化中（`post_linkedin.py` はコード保持済み） |
| LinkedIn メトリクス収集 | 無効化中 |
