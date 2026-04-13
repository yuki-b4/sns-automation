# SNS自動化システム 設計書

## 概要

SNS運用の全工程（投稿生成→配信→データ収集→戦略改善提案）を自動化するシステム。
GitHub Actions + Python + Claude API で構成し、サーバーコスト¥0で常時稼働する。

---

## システム構成

```
sns-automation/
├── .github/workflows/        # GitHub Actions ワークフロー（8本）
│   ├── post_0700.yml         # 毎日 07:00 JST 投稿
│   ├── post_0730.yml         # 毎日 07:30 JST 投稿
│   ├── post_2045.yml         # 毎日 20:45 JST 投稿
│   ├── post_2100.yml         # 毎日 21:00 JST 投稿
│   ├── post_2130.yml         # 毎日 21:30 JST 投稿
│   ├── daily_metrics.yml     # 毎日 22:00 JST エンゲージメント収集
│   ├── competitor.yml        # 火・金 08:00 JST 競合分析
│   └── weekly_report.yml     # 毎週月曜 09:00 JST 改善レポート生成
├── scripts/
│   ├── generate_post.py      # 投稿生成・配信のメインスクリプト
│   ├── post_threads.py       # Threads API 投稿モジュール
│   ├── post_linkedin.py      # LinkedIn API 投稿モジュール（※一時無効化）
│   ├── notify_slack.py       # Slack 通知モジュール
│   ├── collect_metrics.py    # エンゲージメント収集スクリプト
│   ├── analyze_competitors.py# 競合分析スクリプト
│   ├── weekly_report.py      # 週次レポート生成スクリプト
│   └── sheets.py             # Google Sheets 読み書きモジュール
├── config/
│   └── strategy.json         # ポジショニング・投稿タイプ配分設定
└── requirements.txt
```

---

## フロー詳細

### Flow 1：Post hh:mm ワークフロー（1日5回）

**トリガー時刻（JST）：** 07:00 / 07:30 / 20:45 / 21:00 / 21:30

**実行される処理（順番）：**

```
1. config/strategy.json を読み込む

2. 投稿タイプを決定（ローテーション）
   - 「今日が今年の何日目か」×「スロット番号（0〜4）」で計算
   - strategy.json の post_rotation 配列（10要素）を循環
   - 結果：permission（許可系）/ structure（体系化系）/ personal（自己開示系）のいずれか

3. Claude API（claude-opus-4-6）に投稿文生成をリクエスト
   - プロンプトに以下を含める：
     - ポジショニング・コンセプト・差別化軸
     - ターゲットペルソナと悩み
     - 投稿タイプの説明
     - ルール（140〜200文字、断定的語尾、ハッシュタグ不要 等）
   - 出力：投稿本文のみ

4. Threads API に自動投稿（2段階）
   - Step1: /{THREADS_USER_ID}/threads にPOST → コンテナID取得
   - Step2: /threads_publish にPOST → 公開、投稿IDを取得
   - 失敗時：ログにエラーを出力してスキップ（クラッシュしない）

5. Slack に草稿通知（X・note用）
   - 投稿本文と投稿タイプラベルをBlock Kit形式で送信
   - 「X / noteにコピペして投稿してください」のガイドメッセージ付き

6. Google Sheets「投稿DB」に記録
   - Threads投稿が成功した場合のみ記録
   - 記録カラム：post_id / platform / post_type / content / posted_at / week_number
```

**各スロットと投稿タイプの対応（POST_SLOT環境変数）：**

| ワークフロー | POST_SLOT | 時刻 |
|---|---|---|
| post_0700.yml | 0 | 07:00 |
| post_0730.yml | 1 | 07:30 |
| post_2045.yml | 2 | 20:45 |
| post_2100.yml | 3 | 21:00 |
| post_2130.yml | 4 | 21:30 |

---

### Flow 2：Daily Metrics Collection（毎日 22:00 JST）

```
1. Google Sheets「投稿DB」から直近2日分の投稿IDを取得（platform=threads のみ）
2. 各投稿IDに対して Threads Insights API を叩く
   - 取得メトリクス：likes / reposts / replies / views（impressions）
   - エンゲージメント率 = (likes + reposts + replies) / impressions
3. Google Sheets「メトリクスDB」に記録
```

---

### Flow 3：Competitor Analysis（火・金 08:00 JST）

```
1. Google Sheets「競合投稿DB」から未分析（analyzed が空）の手動入力データを取得
   ※ 競合の投稿は Threads API では取得不可のため手動入力
   ※ 入力カラム：content / likes / replies / posted_at / thread_id / reply_order / analyzed
   ※ thread_id：同じスレッドに同じ値を振る（スタンドアロン投稿は空欄）
   ※ reply_order：ルートが 0、リプライが 1/2/3…（スタンドアロン投稿は空欄）
   ※ analyzed：分析済みで TRUE、未入力 = 未分析。分析スクリプト実行後に自動でマークされる
2. 未分析投稿がなければ終了
3. Claude API（claude-opus-4-6）でプロンプト用の分析テキストを生成
   - 高エンゲージメント投稿の傾向
   - 頻出テーマ・キーワード
   - 自社との差分・空白地帯
   - スレッド構成パターン（スレッド投稿がある場合のみ）
4. Slack に分析テキストを直接通知（AI への入力プロンプトとして利用可能な形式）
5. 分析済み投稿の analyzed カラムを TRUE にマーク
```

---

### Flow 4：Weekly Report（毎週月曜 09:00 JST）

```
1. Google Sheetsから過去7日分の投稿DB・メトリクスDBを取得
2. Google Sheets「競合投稿DB」から直近14日分の投稿サンプルを取得
3. 投稿タイプ別のエンゲージメント率・インプレッションを集計
4. Claude API（claude-opus-4-6）に統合分析をリクエスト
   - 自社データ＋競合データを渡す
   - 出力：以下4項目
     1. ポジショニング/差別化軸の調整案
     2. 投稿タイプ配分の調整案（%で提示）
     3. 競合が取れていない空白地帯・先手で取るべきテーマ（上位3件）
     4. その他改善案（上位3件）
5. Slack にレポートを通知
```

---

## Google Sheets 構成

| シート名 | 用途 | 主なカラム |
|---|---|---|
| 投稿DB | 全投稿の記録 | post_id / platform / post_type / content / posted_at / week_number |
| メトリクスDB | エンゲージメントデータ | post_id / collected_at / likes / reposts / replies / impressions / engagement_rate |
| 競合投稿DB（分析結果） | Slack 直接通知のためシート不使用 | - |
| 競合投稿DB | 競合の投稿単位データ（手動入力） | content / likes / replies / posted_at / thread_id / reply_order / analyzed |
| 競合アカウント | 分析対象の競合リスト | account_id |

---

## GitHub Secrets 一覧

| Secret名 | 内容 | 取得先 |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude APIキー | console.anthropic.com |
| `THREADS_USER_ID` | ThreadsユーザーID | Threads API |
| `THREADS_TOKEN` | Threadsアクセストークン | Meta開発者ポータル |
| `LINKEDIN_TOKEN` | LinkedIn APIトークン | ※一時無効化 |
| `LINKEDIN_ORG_ID` | LinkedIn法人ページID | ※一時無効化 |
| `SLACK_WEBHOOK` | Slack Incoming Webhook URL | api.slack.com/apps |
| `GOOGLE_SHEETS_ID` | スプレッドシートID | Google SheetsのURL内 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service Account JSONの中身 | Google Cloud Console |

---

## 外部API・権限一覧

### Threads API
| 権限 | 用途 |
|---|---|
| `threads_basic` | 競合アカウントの投稿取得 |
| `threads_content_publish` | 投稿の作成・公開 |
| `threads_manage_insights` | 投稿のインサイト取得 |

### Claude API
| 用途 | モデル | 月間コスト概算 |
|---|---|---|
| 投稿生成（150回/月） | claude-opus-4-6 | 約$0.50 |
| 競合分析（8回/月） | claude-opus-4-6 | 約$0.06 |
| 週次レポート（4回/月） | claude-opus-4-6 | 約$0.04 |
| **合計** | | **約$0.60/月（約90円）** |

---

## 現在の無効化状況

| 機能 | 状態 | 理由 |
|---|---|---|
| LinkedIn自動投稿 | **無効化中** | APIキー未取得。`post_linkedin.py` はコード保持済み |
| LinkedIn メトリクス収集 | **無効化中** | 同上 |

---

## 投稿タイプ・ローテーション設計

### タイプ定義

| タイプ | 比率 | 目的 |
|---|---|---|
| permission（許可系） | 50% | 共感・拡散・フォロー獲得 |
| structure（体系化系） | 30% | 権威性・信頼・法人リーチ |
| personal（自己開示系） | 20% | 説得力・AとBの橋渡し |

### ローテーション（strategy.jsonの post_rotation）

```
[permission, permission, permission, structure, structure,
 personal, permission, permission, permission, structure]
```

1日5投稿 × このローテーションで循環。`(day_of_year - 1) * 5 + slot) % 10` で決定。

---

## ポジショニング設定（strategy.json）

```json
{
  "position": "ハイパフォーマーのための、コンディション設計の専門家",
  "concept": "スマートに勝ち続ける設計力",
  "differentiation": "意思力に頼らないパフォーマンス設計"
}
```

---

## ログ確認方法

GitHub → Actionsタブ → 対象ワークフローを選択 → 実行履歴をクリック
→ `python scripts/generate_post.py` のステップにすべての print() 出力が表示される

## 手動実行方法

GitHub → Actionsタブ → 対象ワークフローを選択 → **Run workflow** ボタン
