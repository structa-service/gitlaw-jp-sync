# gitlaw-jp-sync

**gitlaw-jp → Structa 自動同期 GitHub Actions**

日本の法律データ OSS プロジェクト [gitlaw-jp](https://github.com/aluqas/gitlaw-jp) の更新を週次で検出し、
South Gate / IWC が追跡する **66件のビジネス関連法律** を Structa MCP API 経由で自動更新します。

---

## 概要

| 項目 | 内容 |
|------|------|
| 実行スケジュール | 毎週月曜日 9:00 JST（自動） |
| 手動実行 | GitHub Actions → `sync-laws.yml` → `Run workflow` |
| 追跡法律数 | 66件（ビジネス関連のみ） |
| 更新先 | Structa `規制-法務` カテゴリ（South Gate）|

## ファイル構成

```
├── .github/workflows/sync-laws.yml   # 週次スケジュール＋手動トリガー
├── filter_list.yaml                  # 追跡対象66法律（law_id + カテゴリ）
├── scripts/sync_to_structa.py        # メイン同期スクリプト
└── state/last_commit.txt             # 前回処理済みコミット SHA（自動更新）
```

## セットアップ（初回のみ）

このリポジトリの **Settings → Secrets and variables → Actions** で以下を登録:

| Secret 名 | 説明 | 取得方法 |
|-----------|------|----------|
| `STRUCTA_MCP_KEY` | Structa MCP API の Bearer トークン | Structa 管理画面 → API Keys |
| `STRUCTA_CUSTOMER_ID` | South Gate の customer_id | `21006b15-f32a-4b15-80e3-bfd360925a72` |

> `GITHUB_TOKEN` は GitHub Actions が自動提供するため設定不要です。

## 動作の仕組み

1. `state/last_commit.txt` から前回処理した gitlaw-jp の SHA を読み込む
2. GitHub API で gitlaw-jp の最新 SHA と比較
3. 変更があった `current.json` ファイルから追跡対象 law_id を特定
4. Structa `structa_capture` API を呼び出して wiki を upsert（同一 wiki_path は上書き更新）
5. `state/last_commit.txt` を新しい SHA で自動コミット

## 追跡法律カテゴリ（66件）

| カテゴリ | 件数 |
|---------|------|
| クロスボーダー・外為 | 5 |
| 会社・商事・契約基盤 | 7 |
| 税務 | 10 |
| 金融・投資 | 10 |
| 労働・雇用 | 8 |
| 知的財産 | 5 |
| 関税・輸出入 | 5 |
| 個人情報・IT | 5 |
| 取引・消費者 | 3 |
| 仲裁・ADR | 3 |
| 倒産・事業再生 | 3 |
| 競争法 | 1 |
| マイナンバー・行政手続 | 1 |

## 廃止された法律の扱い

- Structa の wiki ページは**削除しない**（履歴を保持）
- `ステータス: ⚠️ 廃止済み` を付与して内容を更新
- e-GOV へのリンクで正式情報を参照可能

## フル再同期

全66件を強制的に再同期したい場合：
1. GitHub Actions → `Sync Japanese Laws to Structa`
2. `Run workflow` をクリック
3. `全法律を強制再同期する` を `true` に設定して実行
