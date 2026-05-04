#!/usr/bin/env python3
"""
sync_to_structa.py
------------------
gitlaw-jp の変更を検出し、追跡対象の法律を Structa MCP API へ同期するスクリプト。

動作フロー:
  1. state/last_commit.txt から前回処理済みの gitlaw-jp コミット SHA を読む
  2. GitHub API で gitlaw-jp の最新コミット SHA を取得
  3. 差分（変更ファイル）を比較して追跡対象法律を特定
  4. 変更された各法律の current.json（メタデータ）を取得
  5. Structa MCP API（structa_capture）へ POST して wiki を更新（upsert）
  6. state/last_commit.txt を新しい SHA で上書き

環境変数:
  GITHUB_TOKEN          - GitHub Personal Access Token（rate limit対策）
  STRUCTA_MCP_KEY       - Structa MCP API の Bearer トークン
  STRUCTA_CUSTOMER_ID   - Structa の customer_id（South Gate）
  FORCE_FULL_SYNC       - "true" を指定すると全法律を強制再同期（省略可）
"""

import os
import sys
import json
import yaml
import base64
import logging
import requests
from pathlib import Path

# ─── ログ設定 ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ─── 定数 ────────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).parent.parent  # repo root
FILTER_LIST     = SCRIPT_DIR / "filter_list.yaml"
STATE_FILE      = SCRIPT_DIR / "state" / "last_commit.txt"
GITLAW_OWNER    = "aluqas"
GITLAW_REPO     = "gitlaw-jp"
GITLAW_BRANCH   = "dev"
STRUCTA_API_URL = "https://structa.me/api/mcp"
STRUCTA_CATEGORY = "規制-法務"

# ─── 環境変数 ────────────────────────────────────────────────────────────────
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
STRUCTA_MCP_KEY    = os.environ.get("STRUCTA_MCP_KEY", "")
STRUCTA_CUSTOMER_ID = os.environ.get("STRUCTA_CUSTOMER_ID", "")
FORCE_FULL_SYNC    = os.environ.get("FORCE_FULL_SYNC", "").lower() == "true"

GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}


def validate_env():
    """必須環境変数のチェック"""
    missing = []
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    if not STRUCTA_MCP_KEY:
        missing.append("STRUCTA_MCP_KEY")
    if not STRUCTA_CUSTOMER_ID:
        missing.append("STRUCTA_CUSTOMER_ID")
    if missing:
        log.error(f"必須環境変数が未設定です: {', '.join(missing)}")
        sys.exit(1)


def load_filter_list():
    """filter_list.yaml を読み込み、law_id → law_info の辞書を返す"""
    with open(FILTER_LIST, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {item["law_id"]: item for item in data["laws"]}


def load_last_sha():
    """state/last_commit.txt から前回 SHA を読む。なければ空文字を返す"""
    if STATE_FILE.exists():
        sha = STATE_FILE.read_text().strip()
        return sha if sha else ""
    return ""


def save_current_sha(sha: str):
    """state/last_commit.txt に新しい SHA を保存"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(sha + "\n")
    log.info(f"state/last_commit.txt を更新: {sha[:12]}...")


def get_latest_sha():
    """gitlaw-jp の最新コミット SHA を取得"""
    url = f"https://api.github.com/repos/{GITLAW_OWNER}/{GITLAW_REPO}/commits/{GITLAW_BRANCH}"
    r = requests.get(url, headers=GH_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["sha"]


def get_changed_law_ids(last_sha: str, current_sha: str, law_map: dict) -> list:
    """
    last_sha から current_sha の間に変更された追跡対象 law_id を返す。
    比較件数が上限を超えた場合や SHA が見つからない場合は全件を返す。
    """
    url = f"https://api.github.com/repos/{GITLAW_OWNER}/{GITLAW_REPO}/compare/{last_sha}...{current_sha}"
    r = requests.get(url, headers=GH_HEADERS, timeout=60)

    if r.status_code == 404:
        log.warning("比較対象の SHA が見つかりません。全件フル同期を実行します。")
        return list(law_map.keys())

    data = r.json()
    status = data.get("status", "")

    if status == "diverged":
        log.warning("コミット差分が大きすぎます（diverged）。全件フル同期を実行します。")
        return list(law_map.keys())

    # 変更ファイルから law_id を抽出
    # gitlaw-jp のファイル構造: laws/{prefix}/{law_id}/current.json など
    changed_ids = set()
    for f in data.get("files", []):
        parts = f["filename"].split("/")
        if len(parts) >= 2 and parts[-1] in ("current.json", "current.xml"):
            law_id = parts[-2]
            if law_id in law_map:
                changed_ids.add(law_id)

    log.info(f"変更された追跡対象法律: {len(changed_ids)} 件")
    return list(changed_ids)


def fetch_law_metadata(law_id: str, sha: str) -> dict | None:
    """
    GitHub Contents API で current.json を取得してデコード。
    ファイルが見つからない場合は None を返す。
    """
    # まずリポジトリのルートにある laws/ ディレクトリからパスを特定
    # gitlaw-jp は laws/{2桁prefix}/{law_id}/current.json の構造
    prefix = law_id[:3]  # e.g. "324"
    paths_to_try = [
        f"laws/{law_id}/current.json",
        f"laws/{prefix}/{law_id}/current.json",
    ]

    for path in paths_to_try:
        url = f"https://api.github.com/repos/{GITLAW_OWNER}/{GITLAW_REPO}/contents/{path}"
        r = requests.get(url, headers=GH_HEADERS, params={"ref": sha}, timeout=30)
        if r.status_code == 200:
            content_b64 = r.json().get("content", "")
            raw = base64.b64decode(content_b64).decode("utf-8")
            return json.loads(raw)
        elif r.status_code == 404:
            continue
        else:
            log.warning(f"current.json 取得失敗 ({r.status_code}): {path}")

    # パスを動的に検索（フォールバック）
    return fetch_law_metadata_dynamic(law_id, sha)


def fetch_law_metadata_dynamic(law_id: str, sha: str) -> dict | None:
    """
    ディレクトリ一覧から law_id を検索してメタデータを取得（フォールバック）
    """
    # laws/ 直下を一覧取得（キャッシュ代わりに1回だけ）
    url = f"https://api.github.com/repos/{GITLAW_OWNER}/{GITLAW_REPO}/git/trees/{sha}"
    r = requests.get(url, headers=GH_HEADERS, params={"recursive": "1"}, timeout=60)
    if r.status_code != 200:
        log.error(f"ツリー取得失敗: {r.status_code}")
        return None

    tree = r.json().get("tree", [])
    # law_id を含む current.json パスを検索
    target_path = None
    for item in tree:
        if item["path"].endswith(f"{law_id}/current.json"):
            target_path = item["path"]
            break

    if not target_path:
        log.warning(f"law_id {law_id} が git tree に見つかりません。廃止された可能性があります。")
        return None

    url = f"https://api.github.com/repos/{GITLAW_OWNER}/{GITLAW_REPO}/contents/{target_path}"
    r = requests.get(url, headers=GH_HEADERS, params={"ref": sha}, timeout=30)
    if r.status_code == 200:
        raw = base64.b64decode(r.json()["content"]).decode("utf-8")
        return json.loads(raw)

    return None


def build_capture_text(law_id: str, law_info: dict, metadata: dict | None) -> str:
    """
    Structa capture 用のテキストを生成。
    metadata が None の場合（廃止等）は廃止マーカーを付与。
    """
    name = law_info["name"]
    category = law_info["structa_category"]

    if metadata is None:
        # 法律が見つからない場合 → 廃止の可能性
        return f"""【法律情報：{name}】
法令ID: {law_id}
カテゴリ: {category}
ステータス: ⚠️ 廃止または削除（gitlaw-jp から当該ファイルが消失）
最終確認: 自動同期スクリプトによる検出

この法律は gitlaw-jp から削除されています。
廃止・統合・改番された可能性があります。内容は引き続きこの wiki ページで参照可能ですが、
最新の正式テキストは e-GOV 法令検索（https://laws.e-gov.go.jp/）で確認してください。
"""

    # メタデータから主要フィールドを抽出
    law_num    = metadata.get("lawNum", "")
    law_title  = metadata.get("lawTitle", name)
    enact_date = metadata.get("promulgationDate", "")
    enf_date   = metadata.get("enforcementDate", "")
    revision   = metadata.get("lastAmendLawNum", "")
    rev_date   = metadata.get("lastAmendEnforcementDate", "")
    status     = metadata.get("remainInForce", True)

    status_label = "現行" if status else "⚠️ 廃止済み"

    return f"""【法律情報：{law_title}】
法令ID: {law_id}
法令番号: {law_num}
カテゴリ: {category}
ステータス: {status_label}
公布日: {enact_date}
施行日: {enf_date}
最終改正法令番号: {revision}
最終改正施行日: {rev_date}

## 概要
{law_title}は日本の{category}分野における重要法律です。
South Gate / IWC が関わるクロスボーダー取引・事業開発・投資活動において準拠すべき法規制として管理しています。

## ビジネス関連性
- 対象ビジネス領域: {category}
- 適用シーン: 日本↔インドネシア間のクロスボーダー取引、事業設計、契約締結等

## 参照リンク
- e-GOV 法令検索: https://laws.e-gov.go.jp/law/{law_id}
- gitlaw-jp: https://github.com/aluqas/gitlaw-jp

## 改正履歴追跡
gitlaw-jp の GitHub Webhook（自動週次同期）で改正を自動検知。
このページは改正検知時に自動更新されます。
"""


def post_to_structa(text: str, law_name: str) -> bool:
    """
    Structa MCP API（structa_capture）へ POST。
    成功時 True、失敗時 False を返す。
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "structa_capture",
            "arguments": {
                "text": text,
                "customer_id": STRUCTA_CUSTOMER_ID,
                "category": STRUCTA_CATEGORY
            }
        }
    }
    headers = {
        "Authorization": f"Bearer {STRUCTA_MCP_KEY}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(STRUCTA_API_URL, json=payload, headers=headers, timeout=30)
        if r.status_code == 200:
            result = r.json()
            if "error" in result:
                log.error(f"Structa API エラー [{law_name}]: {result['error']}")
                return False
            log.info(f"✅ 同期完了: {law_name}")
            return True
        else:
            log.error(f"Structa API HTTP {r.status_code} [{law_name}]: {r.text[:200]}")
            return False
    except requests.RequestException as e:
        log.error(f"Structa API 接続エラー [{law_name}]: {e}")
        return False


def main():
    log.info("=== gitlaw-jp → Structa 同期スクリプト開始 ===")
    validate_env()

    # フィルターリスト読み込み
    law_map = load_filter_list()
    log.info(f"追跡対象法律: {len(law_map)} 件")

    # 最新コミット SHA 取得
    current_sha = get_latest_sha()
    log.info(f"gitlaw-jp 最新 SHA: {current_sha[:12]}...")

    # 前回 SHA 読み込み
    last_sha = load_last_sha()

    if FORCE_FULL_SYNC or not last_sha:
        log.info("フル同期モード: 全追跡法律を同期します")
        changed_ids = list(law_map.keys())
    elif last_sha == current_sha:
        log.info("変更なし。スキップします。")
        sys.exit(0)
    else:
        log.info(f"前回 SHA: {last_sha[:12]}...")
        changed_ids = get_changed_law_ids(last_sha, current_sha, law_map)

    if not changed_ids:
        log.info("追跡対象の変更なし。state を更新して終了します。")
        save_current_sha(current_sha)
        sys.exit(0)

    log.info(f"同期対象: {len(changed_ids)} 件")

    # 各法律を同期
    success_count = 0
    fail_count = 0

    for law_id in changed_ids:
        law_info = law_map[law_id]
        law_name = law_info["name"]
        log.info(f"処理中: {law_name} ({law_id})")

        # メタデータ取得
        metadata = fetch_law_metadata(law_id, current_sha)

        # キャプチャテキスト生成
        capture_text = build_capture_text(law_id, law_info, metadata)

        # Structa へ POST
        if post_to_structa(capture_text, law_name):
            success_count += 1
        else:
            fail_count += 1

    # 結果サマリー
    log.info(f"=== 同期完了: 成功 {success_count} 件 / 失敗 {fail_count} 件 ===")

    if fail_count > 0:
        log.warning(f"{fail_count} 件の同期に失敗しました。")
        # 失敗があっても state は更新する（次回で再試行は差分のみ）
        # 全件失敗の場合は state を更新しない（次回に再試行）
        if success_count == 0:
            log.error("全件失敗のため state は更新しません。次回起動時に再試行します。")
            sys.exit(1)

    # state 更新
    save_current_sha(current_sha)
    log.info("✅ state/last_commit.txt を更新しました。")


if __name__ == "__main__":
    main()
