# -*- coding: utf-8 -*-
"""トポロジー指紋の純粋ロジック(Maya 非依存)。pytest 対象。"""
from __future__ import annotations

import hashlib


def canonical_cycle(ids):
    """頂点ID列の巡回正規形(辞書順で最小の回転)。向きは保つ。"""
    seq = [int(x) for x in ids]
    n = len(seq)
    if n == 0:
        return []
    if n == 1:
        return list(seq)
    best = 0
    for i in range(1, n):
        # 回転 i が best より小さいか
        smaller = False
        for k in range(n):
            a = seq[(i + k) % n]
            b = seq[(best + k) % n]
            if a < b:
                smaller = True
                break
            if a > b:
                break
        if smaller:
            best = i
    return seq[best:] + seq[:best]


def reverse_winding(ids):
    """同じ始点で向きだけ逆にした列。"""
    seq = [int(x) for x in ids]
    if len(seq) <= 1:
        return list(seq)
    return [seq[0]] + list(reversed(seq[1:]))


def encode_faces(faces):
    lines = ["F={0}".format(len(faces))]
    for face in faces:
        canon = canonical_cycle(face)
        lines.append(",".join(str(i) for i in canon))
    return "\n".join(lines) + "\n"


def compute_topology_fingerprint(faces):
    """フェースごとの巡回正規形を連結してハッシュする。

    フェース順は入力順(Maya のフェース番号順)を保つ。
    """
    payload = encode_faces(faces).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def faces_all_reversed(before_faces, after_faces):
    """全フェースの頂点ID列が逆順になったか。

    生の列が reverse_winding(before) と一致するか、
    巡回正規形同士が reverse と一致するかで判定する。
    フェース数が違う場合は False。
    """
    if len(before_faces) != len(after_faces):
        return False
    if not before_faces:
        return True
    for before, after in zip(before_faces, after_faces):
        after_list = [int(x) for x in after]
        raw_rev = reverse_winding(before)
        canon_rev = canonical_cycle(raw_rev)
        canon_after = canonical_cycle(after_list)
        if after_list != raw_rev and canon_after != canon_rev:
            return False
    return True
