"""
anypath.bridge.root_manager  -  Bridge層: 探索ルートの追加・削除（設定ファイルI/O）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.4（探索ルートの自動推定とガード）
再設計計画書: AnyPath_再設計計画書_v1.md §2 柱3, §4.3

v1.1での方針転換（重要）:
    旧仕様（v1.0）は追加を「お試し（セッション限り）」と「恒久
    （machine.jsonへ永続化）」に分けて利用者に選ばせていたが、
    再設計計画書の指摘「原則OKボタン/選択ボタンを押すだけで良いように
    したい」に基づき、この二択そのものを廃止した。

    新方針: 追加は常に machine.json へ自動的に永続化する（利用者に
    「お試しですか？恒久ですか？」を聞かない）。上限件数
    （SEARCH_ROOTS_MAX、Core層のクランプと同じ定数）に達した場合は、
    最も長く使われていない（last_used_at が最古の）ルートを自動的に
    入れ替える（LRU: Least Recently Used）。

    「使われた」の記録: そのルート配下から実際に解決に使えたファイルが
    見つかった時点（S4照合で候補が得られた時点）で touch_root_used() を
    呼び、last_used_at を更新する想定（呼び出しはBridge層の解決パス側が
    担当。本モジュールはメタデータの読み書きのみを提供する）。

    メタデータの保存場所: machine.json 内に新設した search_roots_meta
    キー（{path: {"added_at":..., "last_used_at":...}, ...}）に持たせる。
    config.py の DEFAULT_CONFIG / apply_clamps は search_roots
    （文字列リスト）のみを見るため、search_roots_meta を追加しても
    Core層の設定クランプ処理には影響しない（未知キーとして無視される
    設計になっている）。

§6.4-5: 自己診断画面から、追加済みの探索ルートを個別に削除できること
（設定ファイルを開かせない）。この操作性は維持する。

ファイル数概算ロジック本体は anypath.core.root_guard（Maya非依存）に
既に実装済み。本モジュールはその結果を用いた設定ファイルへの実書き込みを
担当する。

設定ファイルの書き込みはCLAUDE.md規約（一時ファイル＋os.replace()）に
従う。

変更履歴:
    1.1.0 (2026.07.30):
        - 再設計計画書 v1 §4.3 に基づき、「お試し／恒久」の二択選択を
          廃止。add_trial_root/add_permanent_root の2つのAPIを
          add_search_root() 1本に統合し、常に自動永続化+LRU入れ替えに
          変更した。旧APIは後方互換のため薄いラッパーとして残す
          （呼び出し元がまだ移行していない場合に備える）。
        - search_roots_meta（追加日時・最終使用日時）を新設。
        - 後方互換性: MINOR。machine.json の search_roots
          （文字列リスト）のスキーマ自体は変更しない。search_roots_meta
          は新規キーとして追加するのみで、旧バージョンのAnyPathが
          同じmachine.jsonを読んでも search_roots 自体は変わらず動作する
          （未知キーは config.py 側で無視される）。
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.4 に基づき新規作成。
"""

__version__ = "1.1.0"

import datetime
import json
import os

from anypath.core.config import SEARCH_ROOTS_MAX
from anypath.core.root_guard import (
    DEFAULT_FILE_COUNT_THRESHOLD,
    estimate_file_count,
    suggest_deeper_subfolders,
)


def evaluate_candidate_root(candidate_path, threshold=DEFAULT_FILE_COUNT_THRESHOLD):
    """
    追加候補フォルダを評価する（§6.4-1, §6.4-2）。
    戻り値: (RootEstimate, [より深いサブフォルダの候補一覧])
    """
    estimate = estimate_file_count(candidate_path, threshold=threshold)
    suggestions = []
    if estimate.exceeds_threshold:
        suggestions = suggest_deeper_subfolders(candidate_path)
    return estimate, suggestions


def _load_machine_config(machine_config_path):
    if not machine_config_path or not os.path.isfile(machine_config_path):
        return {}
    try:
        with open(machine_config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_machine_config(machine_config_path, data):
    """CLAUDE.md規約: 一時ファイル＋os.replace()でアトミックに書き込む。"""
    os.makedirs(os.path.dirname(machine_config_path), exist_ok=True)
    tmp_path = machine_config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, machine_config_path)


def _now_iso():
    return datetime.datetime.now().isoformat()


def add_search_root(candidate_path, machine_config_path):
    """
    再設計後の唯一の追加API。常に自動的に永続化する（お試し/恒久を
    利用者に選ばせない）。

    上限（SEARCH_ROOTS_MAX）に達している場合は、search_roots_meta の
    last_used_at が最も古い（＝一度も使われていない場合は added_at が
    最も古い）ルートを1件自動で外してから追加する（LRU入れ替え）。

    戻り値: (ok: bool, message_or_None, evicted_path_or_None)
        evicted_path_or_None: LRUで自動的に外されたルートがあればそのパス
        （UIが「代わりにこのフォルダは外れました」と一言添えるための情報。
        通常は表示しなくてもよいが、上位層が使いたい場合に備えて返す）。
    """
    posix = candidate_path.replace("\\", "/")
    data = _load_machine_config(machine_config_path)
    roots = data.get("search_roots", [])
    if not isinstance(roots, list):
        roots = []
    meta = data.get("search_roots_meta", {})
    if not isinstance(meta, dict):
        meta = {}

    now = _now_iso()

    if posix in roots:
        # 既に登録済み: 最終使用日時だけ更新して終了（再追加は「使った」と同義）。
        entry = meta.get(posix, {})
        entry["last_used_at"] = now
        entry.setdefault("added_at", now)
        meta[posix] = entry
        data["search_roots"] = roots
        data["search_roots_meta"] = meta
        try:
            _write_machine_config(machine_config_path, data)
        except Exception as e:
            return False, "設定の保存に失敗しました: {0}".format(e), None
        return True, None, None

    evicted_path = None
    if len(roots) >= SEARCH_ROOTS_MAX:
        evicted_path = _pick_lru_root(roots, meta)
        if evicted_path is not None:
            roots = [r for r in roots if r != evicted_path]
            meta.pop(evicted_path, None)
        else:
            # メタ情報が無いなど万一の場合は先頭（最も古く追加された想定）を外す。
            evicted_path = roots[0]
            roots = roots[1:]
            meta.pop(evicted_path, None)

    roots.append(posix)
    meta[posix] = {"added_at": now, "last_used_at": now}
    data["search_roots"] = roots
    data["search_roots_meta"] = meta
    try:
        _write_machine_config(machine_config_path, data)
        return True, None, evicted_path
    except Exception as e:
        return False, "設定の保存に失敗しました: {0}".format(e), None


def _pick_lru_root(roots, meta):
    """
    search_roots のうち、最終使用日時が最も古いものを1件選ぶ
    （一度も使われていない=last_used_atが無いものは added_at で比較。
    それも無ければ最も古参＝リスト先頭側を優先的に外す）。
    """
    if not roots:
        return None

    def sort_key(path):
        entry = meta.get(path, {})
        stamp = entry.get("last_used_at") or entry.get("added_at") or ""
        return (stamp, path)

    return sorted(roots, key=sort_key)[0]


def touch_root_used(root_path, machine_config_path):
    """
    このルートが実際に解決に使われた（S4照合で候補が得られた等）ことを
    記録する。LRU入れ替えの判断材料になる。

    root_path は search_roots に登録されている値（または、そのルート
    配下にある任意の絶対パス）のどちらでも受け付ける。configの
    search_roots に含まれるどのルートの配下かを判定して該当エントリの
    last_used_at を更新する。
    """
    if not root_path or not machine_config_path:
        return
    posix = root_path.replace("\\", "/")
    data = _load_machine_config(machine_config_path)
    roots = data.get("search_roots", [])
    if not isinstance(roots, list) or not roots:
        return
    meta = data.get("search_roots_meta", {})
    if not isinstance(meta, dict):
        meta = {}

    matched = None
    for root in roots:
        if posix == root or posix.startswith(root.rstrip("/") + "/"):
            matched = root
            break
    if matched is None:
        return

    entry = meta.get(matched, {})
    entry["last_used_at"] = _now_iso()
    entry.setdefault("added_at", entry["last_used_at"])
    meta[matched] = entry
    data["search_roots_meta"] = meta
    try:
        _write_machine_config(machine_config_path, data)
    except Exception:
        pass


def remove_search_root(candidate_path, machine_config_path):
    """
    §6.4-5: 自己診断画面からの個別削除。machine.json の search_roots から
    該当パスを取り除く（search_roots_meta の対応エントリも合わせて削除）。
    """
    posix = candidate_path.replace("\\", "/")
    data = _load_machine_config(machine_config_path)
    roots = data.get("search_roots", [])
    if not isinstance(roots, list) or posix not in roots:
        return True, None

    roots = [r for r in roots if r != posix]
    data["search_roots"] = roots
    meta = data.get("search_roots_meta", {})
    if isinstance(meta, dict):
        meta.pop(posix, None)
        data["search_roots_meta"] = meta
    try:
        _write_machine_config(machine_config_path, data)
        return True, None
    except Exception as e:
        return False, "設定の保存に失敗しました: {0}".format(e)


def get_root_usage_info(machine_config_path):
    """
    自己診断画面向け: search_roots と、それぞれの added_at/last_used_at を
    まとめて返す。戻り値: [{"path":..., "added_at":..., "last_used_at":...}, ...]
    （search_roots の順序をそのまま保持する）。
    """
    data = _load_machine_config(machine_config_path)
    roots = data.get("search_roots", [])
    if not isinstance(roots, list):
        return []
    meta = data.get("search_roots_meta", {})
    if not isinstance(meta, dict):
        meta = {}
    infos = []
    for path in roots:
        entry = meta.get(path, {})
        infos.append({
            "path": path,
            "added_at": entry.get("added_at"),
            "last_used_at": entry.get("last_used_at"),
        })
    return infos


# --- 後方互換ラッパー（v1.0 API。新規コードは add_search_root() を使うこと） ---

def add_trial_root(candidate_path):
    """
    後方互換: 旧「お試し」追加。新方針では区別が無いため、呼び出しても
    実際には何もしない（machine_config_pathを持たないためファイルへ
    永続化できず、以前のようなセッション限りの一覧を提供する意味が
    薄れたため）。呼び出し元は add_search_root() への移行を推奨する。
    """
    return False, (
        "「お試し」追加は廃止されました。add_search_root() を"
        "machine_config_path付きで呼び出してください。")


def get_trial_roots():
    """後方互換: 常に空リストを返す（お試し概念が廃止されたため）。"""
    return []


def clear_trial_roots():
    """後方互換: 何もしない（お試し概念が廃止されたため）。"""
    return


def add_permanent_root(candidate_path, machine_config_path):
    """後方互換ラッパー。内部的には add_search_root() を呼ぶ。"""
    ok, message, _evicted = add_search_root(candidate_path, machine_config_path)
    return ok, message


def remove_permanent_root(candidate_path, machine_config_path):
    """後方互換ラッパー。内部的には remove_search_root() を呼ぶ。"""
    return remove_search_root(candidate_path, machine_config_path)


def remove_trial_root(candidate_path):
    """後方互換: 何もしない（お試し概念が廃止されたため）。"""
    return
