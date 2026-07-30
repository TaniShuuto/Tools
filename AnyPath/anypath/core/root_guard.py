"""
anypath.core.root_guard  -  探索ルート追加時のガード（ファイル数概算）
================================================================================
設計書: AnyPath_技術設計書_v2.1.md §6.4（探索ルートの自動推定とガード）

「要確認パネルの『このフォルダを探索先に追加して再試行』は、非エンジニアが
押す最も主要なボタンであり、同時にシステムを恒久的に遅くしうる唯一の操作
です。」

実装必須要件:
    1. 追加を確定する前に、対象フォルダのファイル数を概算する（浅い階層
       から順に走査し、2秒で打ち切る推定でよい）
    2. 概算が閾値（既定50,000件）を超える場合、確定前に警告する
    3. 警告時は「より深いフォルダを選び直す」を第一の選択肢として提示する
    4. 追加はそのセッション限りの「お試し」と、恒久的な追加を分けて
       選択できること。既定は「お試し」
    5. 自己診断画面から、追加済みの探索ルートを個別に削除できること

本モジュールはファイル数概算ロジック（1・2）を提供する。3・4・5はUI
（Shell層）と設定の責務。ファイルシステム走査を伴うが、Maya API には
一切依存しないため anypath.core 配下に置く
（§9.1「Gate 0 G3 の対象範囲外」であり、pytestでtmp_path検証できる）。

変更履歴:
    1.0.0 (2026.07.30):
        - 初版。設計書 v2.1 §6.4 に基づき新規作成。
"""

__version__ = "1.0.0"

import os
import time

DEFAULT_FILE_COUNT_THRESHOLD = 50000
DEFAULT_ESTIMATE_TIMEOUT_SEC = 2.0


class RootEstimate:
    """探索ルート追加候補フォルダの概算結果。"""
    __slots__ = ("path", "estimated_count", "timed_out", "exceeds_threshold")

    def __init__(self, path, estimated_count, timed_out, exceeds_threshold):
        self.path = path
        self.estimated_count = estimated_count
        self.timed_out = timed_out
        self.exceeds_threshold = exceeds_threshold

    def __repr__(self):
        return "RootEstimate(path={0!r}, count~={1}, timed_out={2}, exceeds={3})".format(
            self.path, self.estimated_count, self.timed_out, self.exceeds_threshold)


def estimate_file_count(root_path, timeout_sec=DEFAULT_ESTIMATE_TIMEOUT_SEC,
                         threshold=DEFAULT_FILE_COUNT_THRESHOLD,
                         clock=time.monotonic, listdir_fn=None, isdir_fn=None):
    """
    対象フォルダのファイル数を概算する（§6.4-1: 「浅い階層から順に走査し、
    2秒で打ち切る推定でよい」）。

    浅い階層優先のBFSで走査し、timeout_sec に達したら打ち切る。
    打ち切った場合、それまでに数えた件数をそのまま概算値として返す
    （「少なくともこれだけはある」という下限値としての性質を持つ。
    閾値判定はこの下限値でも十分に安全側 ―― 概算が既に閾値を超えていれば
    実数はさらに多いはずであり、警告を出すべき、という判断に矛盾しない）。

    戻り値: RootEstimate
    """
    if listdir_fn is None:
        listdir_fn = os.listdir
    if isdir_fn is None:
        isdir_fn = os.path.isdir

    if not root_path or not isdir_fn(root_path):
        return RootEstimate(path=root_path, estimated_count=0, timed_out=False,
                             exceeds_threshold=False)

    start = clock()
    deadline = start + timeout_sec
    count = 0
    timed_out = False

    # BFS: 浅い階層から順に走査する（キューを使う。DFSのスタックだと
    # 深い階層へ先に潜ってしまい「浅い階層優先」の要件に反するため）。
    queue = [root_path]
    while queue:
        if clock() >= deadline:
            timed_out = True
            break
        current_dir = queue.pop(0)
        try:
            entries = listdir_fn(current_dir)
        except OSError:
            continue

        next_dirs = []
        for entry in entries:
            if clock() >= deadline:
                timed_out = True
                break
            full = current_dir.rstrip("/\\") + "/" + entry
            try:
                if isdir_fn(full):
                    next_dirs.append(full)
                else:
                    count += 1
            except OSError:
                continue
        queue.extend(next_dirs)
        if timed_out:
            break

    exceeds = count >= threshold
    return RootEstimate(
        path=root_path, estimated_count=count, timed_out=timed_out,
        exceeds_threshold=exceeds)


def suggest_deeper_subfolders(root_path, max_suggestions=5, listdir_fn=None, isdir_fn=None):
    """
    §6.4-3: 「警告時は『より深いフォルダを選び直す』を第一の選択肢として
    提示する」ための候補一覧を作る。root_path直下のサブフォルダを列挙する
    （利用者が選び直しやすいよう、直下1階層のみを提示する単純な実装）。
    """
    if listdir_fn is None:
        listdir_fn = os.listdir
    if isdir_fn is None:
        isdir_fn = os.path.isdir

    if not root_path or not isdir_fn(root_path):
        return []

    try:
        entries = sorted(listdir_fn(root_path))
    except OSError:
        return []

    subfolders = []
    for entry in entries:
        full = root_path.rstrip("/\\") + "/" + entry
        if isdir_fn(full):
            subfolders.append(full)
        if len(subfolders) >= max_suggestions:
            break
    return subfolders
