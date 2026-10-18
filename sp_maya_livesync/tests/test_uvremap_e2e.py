# -*- coding: utf-8 -*-
"""UV再マップ: 本物の core + 本物の worker をつないだ統合テスト(レーン A3)。

単体テスト(test_uvremap_core / test_uvremap_worker)は、ワーカー側を「スタブ core」で検証している。
ここでは ``uvremap_core`` の実物を使い、ワーカーを CLI(subprocess)または ``run_job`` で動かして、
契約 CONTRACT.md §3〜§5 の接続が実際に成り立つことを確かめる。

実行:

    python -m pytest sp_maya_livesync/tests/test_uvremap_e2e.py -q

    # 重いシナリオ(強制終了の復帰・時間計測)も含める。計測表は -s で見える
    UVREMAP_SLOW=1 python -m pytest sp_maya_livesync/tests/test_uvremap_e2e.py -q -s -m slow

合成データ(球 + UVレイアウト + 解析信号)は test_uvremap_core.py のヘルパーを再利用し、
8bit PNG に量子化して一時フォルダへ書く。実在のユーザーフォルダには触れない。外部通信はしない。

シナリオ(docs/uvremap/E2E_REPORT.md に対応表):
    a  UDIM 正常系(6チャンネル、移動+回転、出力名、ledger/status/flag、プローブ、精度)
       + タイル数が変わるときの出力名と旧出力の整理
    b  非UDIM(udim: false)での折り返し / normal_mode "off"
    c  トポロジー変更 -> 11、出力・ledger・flag は不変
    d  プローブ失敗の注入 -> 12、公開されない
    e  古い世代 -> 10 / flag が変化し続ける -> 13
    f  変換中の強制終了 -> 出力は壊れず、次の実行で復帰(slow)
    g  手編集された出力の退避
    h  core の例外(ConversionStopped / TextureSetNotFound / SnapshotError)の写像
    i  16bit のグレー / RGB / RGBA 原本(ビット深度・モードの引き継ぎ、精度、out_res の dict)
    j  時間計測(slow。合否判定なし): 4タイル x 6チャンネルの内訳を 1024^2 と 2048^2 で
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pytest

_TESTS = Path(__file__).resolve().parent
_PKG = _TESTS.parent
for _p in (str(_PKG), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_uvremap_core as tc  # noqa: E402  合成メッシュ/ベイカー/レイアウトの再利用
import uvremap_core as uvc  # noqa: E402  本物の core(スタブではない)
import uvremap_io  # noqa: E402
import uvremap_worker as W  # noqa: E402
from png_testkit import fast_encode_png  # noqa: E402  Pillow なしで任意のフィルタの PNG を作る

WORKER_PY = _PKG / "uvremap_worker.py"

_RUN_SLOW = bool(os.environ.get("UVREMAP_SLOW"))
warnings.filterwarnings("ignore", category=pytest.PytestUnknownMarkWarning)


def slow(fn):
    fn = pytest.mark.slow(fn)
    return fn if _RUN_SLOW else pytest.mark.skip(reason="slow test: set UVREMAP_SLOW=1")(fn)


@pytest.fixture(autouse=True)
def _real_core():
    """このファイルのテストは本物の core を使う(他テストのスタブが残っていたら検出する)。"""
    mod = sys.modules.get("uvremap_core")
    assert mod is uvc and not hasattr(mod, "CONFIG"), "uvremap_core がスタブに差し替わっている"
    assert Path(uvc.__file__).resolve() == (_PKG / "uvremap_core.py").resolve()
    yield


# ============================================================================
# 合成データ
# ============================================================================
TS = "Body"

# (suffix, colorspace, kind)
CH_ALL = [("BaseColor", "srgb", "color"), ("Roughness", "raw", "scalar"),
          ("Metallic", "raw", "scalar"), ("Normal", "raw", "normal"),
          ("Height", "raw", "scalar"), ("Emissive", "srgb", "color")]
CH_LIGHT = [CH_ALL[0], CH_ALL[3]]          # BaseColor + Normal(軽いシナリオ用)

# 4タイル(1001/1002/1011/1012)にまたがる新旧レイアウト(時間計測用。シェルの移動・回転・反転)
LAYOUT_4T_OLD = {
    0: ("id", (0.45, 0.45), (0.03, 0.03)),
    1: ("id", (0.45, 0.45), (1.03, 0.03)),
    2: ("id", (0.45, 0.45), (0.03, 1.03)),
    3: ("id", (0.45, 0.45), (1.03, 1.03)),
    4: ("id", (0.45, 0.45), (0.52, 0.52)),
    5: ("id", (0.45, 0.45), (1.52, 1.52)),
}
LAYOUT_4T_NEW = {
    0: ("rot90", (0.45, 0.45), (1.03, 1.03)),
    1: ("id", (0.45, 0.45), (0.03, 0.03)),
    2: ("rot180", (0.45, 0.45), (1.52, 0.03)),
    3: ("mirror", (0.45, 0.45), (0.03, 1.03)),
    4: ("rot270_mirror", (0.45, 0.45), (1.03, 0.03)),
    5: ("rot90", (0.45, 0.45), (0.52, 1.52)),
}


def _signal(suffix: str, base: str):
    """チャンネルごとの解析信号 fn(3D点) -> (N, C)。None = 法線(ベイカーが生成)。"""
    sm = tc.f_smooth
    if suffix == "BaseColor":
        return tc.f_detail if base == "detail" else sm
    if suffix == "Roughness":
        return lambda p: sm(p)[..., 0:1]
    if suffix == "Metallic":
        return lambda p: sm(p)[..., 1:2]
    if suffix == "Height":
        return lambda p: sm(p)[..., 2:3]
    if suffix == "Emissive":
        return lambda p: (1.0 - sm(p)).astype(np.float32)
    return None


def _png_meta(arr):
    return {"bit_depth": 8, "mode": uvremap_io.mode_for_channels(8, arr.shape[2]), "ext": ".png"}


class World:
    """球 + 旧/新UV + 6チャンネルの旧テクスチャ(8bit PNG として書き出し済み)+ 地上真値(遅延)。"""

    def __init__(self, base_dir: Path, res: int, old_layout, new_layout, *, udim=True,
                 channels=CH_ALL, base_signal="smooth", n=10):
        self.res, self.udim, self.channels = res, udim, list(channels)
        self.base_signal = base_signal
        self.mesh = tc.cube_sphere(n)
        self.A = tc.snap_of(self.mesh, tc.apply_layout(self.mesh, old_layout))
        self.B = tc.with_uv(self.A, tc.apply_layout(self.mesh, new_layout))
        self.input_dir = Path(base_dir)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.old_tiles = {}
        for suffix, _cs, kind in self.channels:
            tiles, masks = self._bake(self.A, suffix, kind)
            tiles = tc.pad_tiles(tiles, masks, 16)
            self.old_tiles[suffix] = tiles
            for tile, arr in tiles.items():
                name = "{0}_{1}.{2}.png".format(TS, suffix, tile) if udim else \
                    "{0}_{1}.png".format(TS, suffix)
                uvremap_io.write_image(self.input_dir / name, arr, _png_meta(arr))
        self._gt = {}

    def _bake(self, snap, suffix, kind):
        return tc.bake(snap, TS, self.res, _signal(suffix, self.base_signal), udim=self.udim,
                       kind="normal" if kind == "normal" else "color")

    def gt(self, suffix):
        """新UVで直接焼いた地上真値 (tiles, masks)。"""
        if suffix not in self._gt:
            kind = next(k for s, _c, k in self.channels if s == suffix)
            self._gt[suffix] = self._bake(self.B, suffix, kind)
        return self._gt[suffix]


_WORLDS: dict = {}


@pytest.fixture(scope="module")
def world_factory(tmp_path_factory):
    def make(key, *args, **kw):
        if key not in _WORLDS:
            _WORLDS[key] = World(tmp_path_factory.mktemp("w_" + str(key[0])), *args, **kw)
        return _WORLDS[key]
    yield make
    _WORLDS.clear()


# ============================================================================
# 環境(1 プロジェクト = 原本フォルダ + uvremap_root)
# ============================================================================
def sha1_path(p) -> str:
    return hashlib.sha1(Path(p).read_bytes()).hexdigest()


def file_states(folder) -> dict:
    """{名前: (sha1, mtime_ns)}(フォルダが無ければ None)。"""
    folder = Path(folder)
    if not folder.exists():
        return None
    return {p.name: (sha1_path(p), p.stat().st_mtime_ns) for p in sorted(folder.iterdir()) if p.is_file()}


class Env:
    def __init__(self, base: Path, world: World):
        self.world = world
        self.src = base / "painter" / "Final"
        self.root = base / "uvremap" / "proj"
        self.out = self.root / "output" / "final"
        self.flag = self.src / "_sync_complete.flag"
        self.baseline = self.root / "baseline" / "bl1"
        self.src.mkdir(parents=True)
        for p in sorted(world.input_dir.iterdir()):
            shutil.copy2(p, self.src / p.name)
        self.flag.write_text("{}", encoding="utf-8")
        uvc.save_snapshot(self.baseline, world.A)
        # 原本は固定の mtime にして、書き込みがあれば検出できるようにする
        self._t0 = 1_600_000_000_000_000_000
        for p in self.src.iterdir():
            os.utime(p, ns=(self._t0, self._t0))
        os.utime(self.src, ns=(self._t0, self._t0))
        self.src_state = file_states(self.src)

    # ---- ジョブ
    def write_job(self, job_id="j1", generation=1, *, current=None, channels=None, **over) -> Path:
        jd = self.root / "jobs" / job_id
        cur_dir = jd / "current"
        uvc.save_snapshot(cur_dir, current if current is not None else self.world.B)
        chans = channels if channels is not None else self.world.channels
        job = {
            "schema": 1, "job_id": job_id, "generation": generation, "ts_name": TS,
            "quality": "final", "baseline_dir": str(self.baseline),
            "current_snapshot_dir": str(cur_dir), "source_dir": str(self.src),
            "source_flag": str(self.flag), "output_dir": str(self.out),
            "uvremap_root": str(self.root), "udim": self.world.udim, "out_res": None,
            "dilate_px": 16, "normal_mode": "uv",
            "channels": [{"suffix": s, "colorspace": c, "kind": k} for s, c, k in chans],
            "timeout_s": 300, "copy_retries": 3, "lock_timeout_s": 10,
        }
        job.update(over)
        path = jd / "job.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        return path

    @staticmethod
    def result(job_path) -> dict:
        return json.loads((Path(job_path).parent / "result.json").read_text(encoding="utf-8"))

    def cli(self, job_path, timeout=900):
        """本物のワーカーを CLI として起動する。(終了コード, result.json, stdout)。"""
        proc = subprocess.run([sys.executable, str(WORKER_PY), "run", str(job_path)],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, self.result(job_path), proc.stdout + proc.stderr

    def inproc(self, job_path):
        """同一プロセスで ``run_job`` を呼ぶ(core への注入用)。(終了コード, result.json)。"""
        return W.run_job(job_path), self.result(job_path)

    # ---- 状態
    def ledger(self) -> dict:
        return json.loads((self.root / "ledger.json").read_text(encoding="utf-8"))

    def status(self) -> dict:
        return json.loads((self.root / "status.json").read_text(encoding="utf-8"))["ts"]

    def flag_json(self) -> dict:
        return json.loads((self.out / W.FLAG_NAME).read_text(encoding="utf-8"))

    def published(self) -> dict:
        """公開状態の全体(出力フォルダの各ファイル、ledger、status の生バイト列)。"""
        def raw(p):
            p = Path(p)
            return p.read_bytes() if p.exists() else None
        return {"out": file_states(self.out), "ledger": raw(self.root / "ledger.json"),
                "status": raw(self.root / "status.json")}

    def rewrite_source(self, name, arr, meta):
        """原本の 1 ファイルを別の形式(ビット深度/モード)で書き直し、固定 mtime と記録を更新する。"""
        p = self.src / name
        uvremap_io.write_image(p, arr, meta)
        os.utime(p, ns=(self._t0, self._t0))
        self.src_state = file_states(self.src)

    def assert_source_untouched(self):
        assert file_states(self.src) == self.src_state, "原本(source_dir)が変化した(I1)"

    def out_tiles(self, suffix) -> dict:
        """出力フォルダから suffix のタイルを読む {tile: float32 (H,W,C)}。"""
        tiles = {}
        for p in sorted(self.out.glob("{0}_{1}.*png".format(TS, suffix))):
            parts = p.name.split(".")
            tile = int(parts[-2]) if len(parts) >= 3 and parts[-2].isdigit() else 1001
            tiles[tile] = uvremap_io.read_image(p)[0]
        return tiles


@pytest.fixture
def light(world_factory, tmp_path):
    """軽い世界: 512²、A -> B(UDIM 2タイル)、BaseColor(平滑)+ Normal。"""
    w = world_factory(("light",), 512, tc.LAYOUT_A, tc.LAYOUT_B, channels=CH_LIGHT)
    return Env(tmp_path, w)


# ============================================================================
# 精度(理想の変換結果との比較。8bit PNG の量子化を含む)
# ============================================================================
def accuracy(env: Env, suffix: str) -> dict:
    """出力を地上真値と比較する。色/スカラー = 内部 PSNR[dB]、法線 = 平均/最大の角度誤差[deg]。"""
    gt, masks = env.world.gt(suffix)
    out = env.out_tiles(suffix)
    assert set(out) >= set(gt), "出力に無いタイル: {0}".format(sorted(set(gt) - set(out)))
    kind = next(k for s, _c, k in env.world.channels if s == suffix)
    if kind == "normal":
        errs = []
        for t in gt:
            sel = tc._shrink(masks[t], 2)
            errs.append(tc.angle_err_deg(out[t][sel], gt[t][sel]))
        e = np.concatenate(errs)
        return {"mean_deg": float(e.mean()), "p99_deg": float(np.percentile(e, 99)), "max_deg": float(e.max())}
    return {"psnr": tc.psnr(out, gt, masks, region="interior")}


# ============================================================================
# a. UDIM 正常系
# ============================================================================
def test_a_udim_full_pipeline_six_channels(world_factory, tmp_path):
    w = world_factory(("full",), 1024, tc.LAYOUT_A, tc.LAYOUT_B, channels=CH_ALL, base_signal="detail")
    env = Env(tmp_path, w)
    job = env.write_job("j1", 1)
    code, res, log = env.cli(job)
    assert code == 0, (res["messages"], log)
    assert res["status"] == "ok" and res["exit_code"] == 0 and res["method"] == "F"
    assert res["generation"] == 1 and res["ts_name"] == TS

    # 出力ファイル名: 新UVが触れるタイル(1001, 1002)の数で決まる -> 複数なので .<tile> 付き
    expect = sorted("{0}_{1}.{2}.png".format(TS, s, t) for s, _c, _k in CH_ALL for t in (1001, 1002))
    assert sorted(f["name"] for f in res["files"]) == expect
    on_disk = sorted(p.name for p in env.out.iterdir() if p.name != W.FLAG_NAME)
    assert on_disk == expect, "出力フォルダに余計なファイル/一時ファイルがある"
    for f in res["files"]:
        assert f["sha1"] == sha1_path(env.out / f["name"]) and f["tile"] in (1001, 1002)
        assert f["name"].split(".")[-2] == str(f["tile"])

    # プローブ: 全チャンネル pass(法線も)
    assert set(res["probe"]) == {s for s, _c, _k in CH_ALL}
    for s, rep in res["probe"].items():
        assert rep["status"] == "pass", (s, rep)
        assert rep["n_valid"] >= 64 and rep["n_bad"] == 0, (s, rep)
    assert res["stats"]["Normal"]["normal_mode"] == "uv" and res["stats"]["Normal"]["udim"] is True

    # ledger / status / flag
    led = env.ledger()["ts"][TS]
    assert led["generation"] == 1 and led["last_good_generation"] == 1 and led["method"] == "F"
    assert led["frozen"] is False
    assert led["uv_hash"] == w.B.meta["uv_hash"] and led["baseline_id"] == w.A.meta["baseline_id"]
    assert led["outputs"] == {f["name"]: f["sha1"] for f in res["files"]}
    assert led["sources"] == {n: sha for n, (sha, _m) in env.src_state.items() if n != W.FLAG_NAME}
    st = env.status()[TS]
    assert st["state"] == "CONVERTED_OK" and st["generation"] == 1
    flag = env.flag_json()
    assert flag["generation"] == 1 and flag["uv_hash"] == w.B.meta["uv_hash"]
    assert flag["files"] == sorted(expect)
    flag_m = (env.out / W.FLAG_NAME).stat().st_mtime_ns
    assert all(p.stat().st_mtime_ns <= flag_m for p in env.out.iterdir()), "flag は最後に書く(I3)"
    assert not list(env.out.glob("*" + W.TMP_SUFFIX))
    assert not (env.root / "jobs" / "j1" / "input").exists() and not (env.root / "jobs" / "j1" / "staging").exists()
    env.assert_source_untouched()
    assert set(res["timings"]) >= {"copy", "convert", "probe", "publish"}

    # 精度: 理想の変換結果(新UVで直接ベイク)との差。8bit 量子化(入力+出力)を含む
    for s, _c, kind in CH_ALL:
        acc = accuracy(env, s)
        print("[a] {0:10s} {1}".format(s, {k: round(v, 3) for k, v in acc.items()}))
        if kind == "normal":
            assert acc["mean_deg"] <= 2.0 and acc["p99_deg"] <= 6.0, (s, acc)
        elif s == "BaseColor":          # 細部テクスチャ(文字様 + チェッカー)は再サンプリングでぼける
            assert acc["psnr"] >= 20.0, (s, acc)    # core 単体(float, PNG なし)でも約 21.4 dB
        else:                           # 平滑信号: ほぼ量子化誤差だけ
            assert acc["psnr"] >= 45.0, (s, acc)

    # ワーカー経由(PNG 読み込み -> 変換 -> PNG 書き込み)は、core を直接呼んだ結果と量子化誤差の範囲で一致する
    for s, cs, kind in (CH_ALL[0], CH_ALL[3]):
        spec = uvc.ChannelSpec(s, cs, kind)
        direct = uvc.convert_channel(w.old_tiles[s], w.A, w.B, TS, uvc.classify(w.A, w.B, TS), spec, w.res)
        out = env.out_tiles(s)
        assert set(out) == set(direct.tiles) == {1001, 1002}
        d = np.concatenate([(out[t] - direct.tiles[t])[direct.coverage[t]].ravel() for t in out]).astype(np.float64)
        psnr_direct = 10 * np.log10(1.0 / max(float((d ** 2).mean()), 1e-20))
        print("[a] {0:10s} worker vs core-direct: {1:.1f} dB".format(s, psnr_direct))
        assert psnr_direct >= 40.0, (s, psnr_direct)

    # 同じ内容を次の世代でもう一度変換しても、同じ出力になる(劣化の累積なし・決定的)
    before = {n: sha for n, (sha, _m) in file_states(env.out).items() if n != W.FLAG_NAME}
    code2, res2, _ = env.cli(env.write_job("j2", 2))
    assert code2 == 0
    assert {n: sha for n, (sha, _m) in file_states(env.out).items() if n != W.FLAG_NAME} == before
    assert env.ledger()["ts"][TS]["generation"] == 2 and env.flag_json()["generation"] == 2


def test_a_output_names_follow_tile_count_and_stale_outputs_are_removed(world_factory, tmp_path):
    """新UVが 1 タイルだけなら無印の名前、複数なら .<tile> 付き。切り替わるとき旧名の出力を整理する。"""
    w = world_factory(("light",), 512, tc.LAYOUT_A, tc.LAYOUT_B, channels=CH_LIGHT)
    env = Env(tmp_path, w)
    single = tc.with_uv(w.A, tc.apply_layout(w.mesh, tc.LAYOUTS["scaled"]))   # 全シェルが 1001
    code, res, _ = env.cli(env.write_job("j1", 1, current=single))
    assert code == 0, res["messages"]
    assert sorted(f["name"] for f in res["files"]) == ["Body_BaseColor.png", "Body_Normal.png"]
    assert sorted(p.name for p in env.out.iterdir()) == ["Body_BaseColor.png", "Body_Normal.png", W.FLAG_NAME]
    assert [f["tile"] for f in res["files"]] == [1001, 1001]

    code, res, _ = env.cli(env.write_job("j2", 2))                              # 1001 + 1002
    assert code == 0, res["messages"]
    names = sorted(p.name for p in env.out.iterdir())
    assert names == sorted([W.FLAG_NAME] + ["Body_{0}.{1}.png".format(s, t)
                                            for s in ("BaseColor", "Normal") for t in (1001, 1002)])
    assert set(env.ledger()["ts"][TS]["outputs"]) == set(names) - {W.FLAG_NAME}
    assert any("不要になった旧出力" in m["text"] for m in res["messages"])
    assert not any(".edited_" in n for n in names)            # 旧名は「手編集」ではなく整理として消える

    code, res, _ = env.cli(env.write_job("j3", 3, current=single))              # 再び 1 タイル
    assert code == 0
    assert sorted(p.name for p in env.out.iterdir()) == ["Body_BaseColor.png", "Body_Normal.png", W.FLAG_NAME]
    env.assert_source_untouched()


def test_a_inputs_with_paeth_and_average_filters_give_byte_identical_outputs(light):
    """Painter 由来の PNG(Paeth / Average を含む)を入力にしても、画素が同じなら出力は完全に同じ。

    自前の PNG 読み込みの反対角線処理を、実際の変換パイプラインの入口で通す。
    """
    code, res, _ = light.cli(light.write_job("j1", 1))
    assert code == 0, res["messages"]
    first = {n: sha for n, (sha, _m) in file_states(light.out).items() if n != W.FLAG_NAME}
    rng = np.random.default_rng(11)
    kinds = set()
    for p in sorted(light.src.glob("*.png")):
        arr, meta = uvremap_io.read_image(p)
        h, _w, ch = arr.shape
        ftypes = rng.integers(0, 5, h)
        kinds |= set(ftypes.tolist())
        img = np.rint(arr * 255).astype(np.uint8)
        before = p.read_bytes()
        p.write_bytes(fast_encode_png(img, ftypes))
        assert p.read_bytes() != before and np.array_equal(uvremap_io.read_image(p)[0], arr)
        os.utime(p, ns=(light._t0, light._t0))
    assert kinds == {0, 1, 2, 3, 4}
    light.src_state = file_states(light.src)
    code, res, _ = light.cli(light.write_job("j2", 2))
    assert code == 0, res["messages"]
    second = {n: sha for n, (sha, _m) in file_states(light.out).items() if n != W.FLAG_NAME}
    assert second == first
    led = light.ledger()["ts"][TS]
    assert led["sources"] == {n: sha for n, (sha, _m) in light.src_state.items() if n != W.FLAG_NAME}   # 入力の sha1 は更新される


# ============================================================================
# b. 非UDIM / normal_mode
# ============================================================================
def test_b_non_udim_wraps_uv_outside_0_1(world_factory, tmp_path):
    w = world_factory(("wrap",), 512, tc.LAYOUT_A, tc.LAYOUT_WRAPPED, udim=False, channels=CH_LIGHT)
    env = Env(tmp_path, w)
    assert sorted(p.name for p in env.src.iterdir() if p.suffix == ".png") == [
        "Body_BaseColor.png", "Body_Normal.png"]               # 非UDIM: タイル番号なしの入力
    # 新UVは u/v が 0〜1 の外(-0.32, 1.02, 2.05, 3.02, 5.55 ...)にあるシェルを含む
    uv = w.B.corner_uv
    assert uv.min() < 0 and uv.max() > 3
    code, res, log = env.cli(env.write_job("j1", 1))
    assert code == 0, (res["messages"], log)
    assert sorted(f["name"] for f in res["files"]) == ["Body_BaseColor.png", "Body_Normal.png"]
    assert res["stats"]["BaseColor"]["udim"] is False and res["stats"]["Normal"]["udim"] is False
    for s, rep in res["probe"].items():
        assert rep["status"] == "pass" and rep["n_valid"] >= 64, (s, rep)
    assert set(env.out_tiles("BaseColor")) == {1001}
    acc = accuracy(env, "BaseColor")
    assert acc["psnr"] >= 45.0, acc
    assert accuracy(env, "Normal")["mean_deg"] <= 2.0


def test_b_normal_mode_off_is_passed_to_convert_and_probe(light):
    """normal_mode "off" で変換したら、プローブにも "off" を渡す(QUESTIONS_core Q4)。"""
    code, res, log = light.cli(light.write_job("j1", 1, normal_mode="off"))
    assert code == 0, (res["messages"], log)
    assert res["stats"]["Normal"]["normal_mode"] == "off"
    assert res["probe"]["Normal"]["status"] == "pass", res["probe"]["Normal"]
    # 補正なしなので、"uv" の理想値とは(回転したシェルで)角度がずれる = 本当に "off" で変換されている
    assert accuracy(light, "Normal")["max_deg"] > 20.0


def test_b_flipped_normals_flow(light):
    """全フェースが逆順(法線反転)の現行 -> F_FLIPPED。変換・プローブとも通る。"""
    flipped = tc.reverse_faces(light.world.B)
    code, res, log = light.cli(light.write_job("j1", 1, current=flipped,
                                               channels=[("BaseColor", "srgb", "color")]))
    assert code == 0, (res["messages"], log)
    assert res["method"] == "F_FLIPPED" and res["probe"]["BaseColor"]["status"] == "pass"
    assert any("normals_flipped" in m["text"] for m in res["messages"])


def test_b_uv_hash_in_ledger_and_flag_is_per_texture_set(light):
    """メッシュに TS が複数あるとき、ledger/flag の uv_hash は TS 単位(meta.json のメッシュ全体の値ではない)。"""
    w = light.world

    def two_ts(snap):
        n = len(snap.face_vertex_counts)
        return uvc.make_snapshot(snap.face_vertex_counts, snap.corner_vid, snap.corner_pos, snap.corner_uv,
                                 ts_faces={TS: np.arange(n // 2), "Other": np.arange(n // 2, n)},
                                 tri_face=snap.tri_face, tri_corner=snap.tri_corner, face_mesh=snap.face_mesh)

    A2, B2 = two_ts(w.A), two_ts(w.B)
    uvc.save_snapshot(light.baseline, A2)
    assert B2.meta["uv_hash"] != uvc.compute_uv_hash(B2, TS)
    code, res, _ = light.cli(light.write_job("j1", 1, current=B2))
    assert code == 0, res["messages"]
    want = uvc.compute_uv_hash(B2, TS)
    assert light.ledger()["ts"][TS]["uv_hash"] == want and light.flag_json()["uv_hash"] == want
    # job が uv_hash を渡せば、それを優先する(Maya が決める)
    code, res, _ = light.cli(light.write_job("j2", 2, current=B2, uv_hash="from-job"))
    assert code == 0 and light.ledger()["ts"][TS]["uv_hash"] == "from-job"


# ============================================================================
# c. トポロジー変更
# ============================================================================
def _topology_variants(w: World):
    B = w.B
    counts = B.face_vertex_counts
    nc = int(counts[-1])
    dropped = uvc.make_snapshot(counts[:-1], B.corner_vid[:-nc], B.corner_pos[:-nc], B.corner_uv[:-nc],
                                ts_faces={TS: np.arange(len(counts) - 1)})
    vid = B.corner_vid.copy()
    vid[0], vid[1] = vid[1], vid[0]                 # 1 つのフェースの頂点順だけを入れ替え
    swapped = uvc.make_snapshot(counts, vid, B.corner_pos, B.corner_uv, ts_faces={TS: np.arange(len(counts))},
                                tri_face=B.tri_face, tri_corner=B.tri_corner)
    return {"face_removed": dropped, "vertex_order_changed": swapped}


@pytest.mark.parametrize("variant", ["face_removed", "vertex_order_changed"])
def test_c_topology_change_stops_without_touching_outputs(light, variant):
    code, res, _ = light.cli(light.write_job("j1", 1))
    assert code == 0, res["messages"]
    before = light.published()
    flag_before = (light.out / W.FLAG_NAME).read_bytes()
    cur = _topology_variants(light.world)[variant]
    code, res, log = light.cli(light.write_job("j2", 2, current=cur))
    assert code == 11, (res["messages"], log)
    assert res["status"] == "blocked" and res["exit_code"] == 11 and res["method"] == "STOP"
    assert any("topology_changed" in m["text"] for m in res["messages"])
    assert res["files"] == []
    after = light.published()
    assert after["out"] == before["out"], "出力フォルダが変化した"
    assert after["ledger"] == before["ledger"], "ledger が変化した"
    assert (light.out / W.FLAG_NAME).read_bytes() == flag_before
    st = light.status()[TS]
    assert st["state"] == "BLOCKED" and st["generation"] == 2
    light.assert_source_untouched()


def test_c_topology_change_on_first_run_creates_no_output(light):
    code, res, _ = light.cli(light.write_job("j1", 1, current=_topology_variants(light.world)["face_removed"]))
    assert code == 11 and res["status"] == "blocked"
    assert not light.out.exists() and not (light.root / "ledger.json").exists()
    assert light.status()[TS]["state"] == "BLOCKED"


# ============================================================================
# d. プローブ失敗の注入
# ============================================================================
def _shifted_convert(real, suffix, frac=0.06):
    """convert_channel の出力を U 方向にずらす(プローブが検出すべき不具合)。"""
    def wrapped(old_tiles, baseline, current, ts_name, decision, spec, out_res, **kw):
        r = real(old_tiles, baseline, current, ts_name, decision, spec, out_res, **kw)
        if spec.suffix == suffix:
            r.tiles = tc.inject_shift_u(r.tiles, frac)
        return r
    return wrapped


def test_d_probe_failure_is_not_published_after_a_good_generation(light, monkeypatch):
    code, res, _ = light.cli(light.write_job("j1", 1))
    assert code == 0
    before = light.published()
    monkeypatch.setattr(uvc, "convert_channel", _shifted_convert(uvc.convert_channel, "Normal"))
    code, res = light.inproc(light.write_job("j2", 2))
    assert code == 12, res["messages"]
    assert res["status"] == "failed" and res["files"] == []
    assert res["probe"]["Normal"]["status"] == "fail" and res["probe"]["Normal"]["n_bad"] > 0
    assert res["probe"]["BaseColor"]["status"] == "pass"            # 他のチャンネルは正常でも公開しない
    assert any("プローブ" in m["text"] for m in res["messages"])
    after = light.published()
    assert after["out"] == before["out"] and after["ledger"] == before["ledger"]
    st = light.status()[TS]
    assert st["state"] == "FAILED" and st["generation"] == 2
    assert not list(light.out.glob("*" + W.TMP_SUFFIX))


def test_d_probe_failure_on_first_run_publishes_nothing(light, monkeypatch):
    monkeypatch.setattr(uvc, "convert_channel", _shifted_convert(uvc.convert_channel, "BaseColor"))
    code, res = light.inproc(light.write_job("j1", 1))
    assert code == 12 and res["probe"]["BaseColor"]["status"] == "fail"
    assert not light.out.exists() or not any(light.out.iterdir())
    assert not (light.root / "ledger.json").exists()
    assert light.status()[TS]["state"] == "FAILED"


# ============================================================================
# e. 世代 / flag
# ============================================================================
def test_e_old_generation_is_discarded(light):
    code, res, _ = light.cli(light.write_job("j5", 5))
    assert code == 0
    before = light.published()
    flag_before = (light.out / W.FLAG_NAME).stat().st_mtime_ns
    code, res, _ = light.cli(light.write_job("j3", 3))
    assert code == 10 and res["status"] == "superseded" and res["exit_code"] == 10
    assert light.published() == before
    assert (light.out / W.FLAG_NAME).stat().st_mtime_ns == flag_before
    assert light.ledger()["ts"][TS]["generation"] == 5


def test_e_flag_that_keeps_changing_exhausts_retries(light, monkeypatch):
    real_copy = W._Run._copy_input
    tick = [0]

    def touching_copy(self, src, dst):
        sha = real_copy(self, src, dst)
        tick[0] += 1                                         # Painter が書き出し直し続けている状況
        t = light._t0 + tick[0] * 1_000_000
        os.utime(light.flag, ns=(t, t))
        return sha

    monkeypatch.setattr(W._Run, "_copy_input", touching_copy)
    code, res = light.inproc(light.write_job("j1", 1, copy_retries=2))
    assert code == 13, res["messages"]
    assert res["status"] == "retry_exhausted"
    assert not light.out.exists() and not (light.root / "ledger.json").exists()
    assert light.status()[TS]["state"] == "FAILED"
    # flag の mtime はこのテストが意図して触っている。それ以外の原本は内容を比べる
    for n, (sha, _m) in light.src_state.items():
        if n != W.FLAG_NAME:
            assert sha1_path(light.src / n) == sha


_FLAG_TOUCHING_RUNNER = """
import os, sys
sys.path.insert(0, {pkg!r})
import uvremap_worker as W
real = W._Run._copy_input
tick = [0]
def touching(self, src, dst):
    sha = real(self, src, dst)
    tick[0] += 1                                    # 1 ファイルをコピーするたびに source_flag を更新する
    t = {t0} + tick[0] * 1000000
    os.utime({flag!r}, ns=(t, t))
    return sha
W._Run._copy_input = touching
sys.exit(W.main(["run", {job!r}]))
"""


def test_e_flag_touched_while_copying_exhausts_retries_with_13_via_cli(light, tmp_path):
    """Painter が書き出し直し続ける状況を、実際の CLI(別プロセス)で決定的に再現する。

    ワーカーのコピー関数だけを差し替えた起動スクリプトで動かし、コピーのたびに source_flag の
    mtime を進める(タイミング依存の別スレッドは使わない)。終了コード 13 がプロセスの終了状態に届くこと。
    """
    job = light.write_job("j1", 1, copy_retries=2)
    runner = tmp_path / "run_touching.py"
    runner.write_text(_FLAG_TOUCHING_RUNNER.format(pkg=str(_PKG), t0=light._t0, flag=str(light.flag), job=str(job)),
                      encoding="utf-8")
    proc = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, timeout=300)
    res = light.result(job)
    assert proc.returncode == 13, (res["messages"], proc.stdout, proc.stderr)
    assert res["status"] == "retry_exhausted" and res["exit_code"] == 13
    assert sum("やり直し" in m["text"] for m in res["messages"]) >= 2
    assert not light.out.exists() and not (light.root / "ledger.json").exists()
    assert light.status()[TS]["state"] == "FAILED"
    # 触るのをやめれば、同じジョブがそのまま通る
    os.utime(light.flag, ns=(light._t0, light._t0))
    code, res, _ = light.cli(light.write_job("j2", 2))
    assert code == 0, res["messages"]


# ============================================================================
# g. 手編集された出力の退避
# ============================================================================
def test_g_hand_edited_output_is_set_aside(light):
    code, res, _ = light.cli(light.write_job("j1", 1))
    assert code == 0
    edited = light.out / "Body_BaseColor.1001.png"
    untouched = light.out / "Body_Normal.1001.png"
    good_sha = sha1_path(edited)
    arr, meta = uvremap_io.read_image(edited)
    uvremap_io.write_image(edited, np.clip(arr * 0.5, 0, 1).astype(np.float32), meta)   # 利用者が手で編集
    edited_sha = sha1_path(edited)
    assert edited_sha != good_sha
    untouched_sha = sha1_path(untouched)
    code, res, _ = light.cli(light.write_job("j2", 2))
    assert code == 0, res["messages"]
    asides = sorted(p.name for p in light.out.iterdir() if ".edited_" in p.name)
    assert len(asides) == 1 and asides[0].startswith("Body_BaseColor.1001.png.edited_")
    stamp = asides[0].rsplit(".edited_", 1)[1]
    assert len(stamp) == 15 and stamp[8] == "-" and stamp.replace("-", "").isdigit()   # YYYYmmdd-HHMMSS
    assert sha1_path(light.out / asides[0]) == edited_sha          # 手編集の内容がそのまま残る
    assert sha1_path(edited) == good_sha                           # 新しい出力は変換結果
    assert sha1_path(untouched) == untouched_sha
    assert any("手編集" in m["text"] and m["level"] == "warn" for m in res["messages"])
    led = light.ledger()["ts"][TS]["outputs"]
    assert led["Body_BaseColor.1001.png"] == good_sha and not any(".edited_" in n for n in led)


# ============================================================================
# h. core の例外の写像
# ============================================================================
def _raiser(exc):
    def f(*a, **k):
        raise exc
    return f


def test_h_conversion_stopped_from_convert_is_blocked(light, monkeypatch):
    monkeypatch.setattr(uvc, "convert_channel", _raiser(uvc.ConversionStopped(["no_convertible_faces"])))
    code, res = light.inproc(light.write_job("j1", 1))
    assert code == 11, res["messages"]
    assert res["status"] == "blocked"
    assert any("no_convertible_faces" in m["text"] for m in res["messages"])
    assert not any("予期しない例外" in m["text"] for m in res["messages"])
    assert light.status()[TS]["state"] == "BLOCKED"
    assert not light.out.exists()


def test_h_conversion_stopped_from_probe_is_blocked(light, monkeypatch):
    monkeypatch.setattr(uvc, "probe_verify", _raiser(uvc.ConversionStopped(["topology_mismatch"])))
    code, res = light.inproc(light.write_job("j1", 1))
    assert code == 11 and res["status"] == "blocked"
    assert any("topology_mismatch" in m["text"] for m in res["messages"])
    assert not light.out.exists() and not (light.root / "ledger.json").exists()


def test_h_real_conversion_stopped_when_no_face_has_uv(light):
    """現行 UV がすべて NaN(UV 無し)-> 本物の core が ConversionStopped(no_convertible_faces)。"""
    B = light.world.B
    nouv = uvc.make_snapshot(B.face_vertex_counts, B.corner_vid, B.corner_pos,
                             np.full_like(B.corner_uv, np.nan), ts_faces={TS: np.arange(len(B.face_vertex_counts))},
                             tri_face=B.tri_face, tri_corner=B.tri_corner)
    code, res, _ = light.cli(light.write_job("j1", 1, current=nouv))
    assert code == 11, res["messages"]
    assert res["status"] == "blocked"
    assert any("no_convertible_faces" in m["text"] for m in res["messages"])
    assert light.status()[TS]["state"] == "BLOCKED" and not light.out.exists()


def test_h_channel_format_error_from_a_gray_normal_map_is_blocked(light):
    """法線マップの原本が 1 チャンネル(グレー)-> core が ChannelFormatError -> 11(入力を直すまで再実行しても同じ)。"""
    gray = light.world.old_tiles["Normal"][1001][..., 0:1]
    uvremap_io.write_image(light.src / "Body_Normal.1001.png", gray, {"bit_depth": 8, "mode": "L", "ext": ".png"})
    code, res, _ = light.cli(light.write_job("j1", 1))
    assert code == 11, res["messages"]
    assert res["status"] == "blocked" and "3 channels" in " ".join(m["text"] for m in res["messages"])
    assert "予期しない例外" not in " ".join(m["text"] for m in res["messages"])
    assert light.status()[TS]["state"] == "BLOCKED" and not light.out.exists()


def test_h_texture_set_not_found_is_blocked(light, monkeypatch):
    # 実フロー: 現行スナップショットに TS が無い -> classify が STOP(ts_missing)
    B = light.world.B
    other = uvc.make_snapshot(B.face_vertex_counts, B.corner_vid, B.corner_pos, B.corner_uv,
                              ts_faces={"Other": np.arange(len(B.face_vertex_counts))},
                              tri_face=B.tri_face, tri_corner=B.tri_corner)
    code, res, _ = light.cli(light.write_job("j1", 1, current=other))
    assert code == 11 and any("ts_missing" in m["text"] for m in res["messages"]), res["messages"]
    # 例外として送出された場合も同じ扱い
    monkeypatch.setattr(uvc, "convert_channel", _raiser(uvc.TextureSetNotFound("texture set 'Body' not found")))
    code, res = light.inproc(light.write_job("j2", 2))
    assert code == 11 and res["status"] == "blocked", res["messages"]
    assert any("Body" in m["text"] for m in res["messages"])
    assert not any("予期しない例外" in m["text"] for m in res["messages"])
    assert light.status()[TS]["state"] == "BLOCKED"


@pytest.mark.parametrize("which", ["baseline", "current"])
def test_h_broken_snapshot_fails_with_a_clear_message(light, which):
    job = light.write_job("j1", 1)
    target = light.baseline if which == "baseline" else light.root / "jobs" / "j1" / "current"
    (target / "mesh.npz").write_bytes(b"this is not an npz")
    code, res, _ = light.cli(job)
    assert code == 12, res["messages"]
    assert res["status"] == "failed" and res["exit_code"] == 12
    texts = " ".join(m["text"] for m in res["messages"])
    assert "スナップショット" in texts and "予期しない例外" not in texts
    assert light.status()[TS]["state"] == "FAILED"
    assert not light.out.exists() and not (light.root / "ledger.json").exists()


def test_h_missing_snapshot_dir_fails(light):
    job = light.write_job("j1", 1)
    shutil.rmtree(light.baseline)
    code, res, _ = light.cli(job)
    assert code == 12 and res["status"] == "failed"
    assert "スナップショット" in " ".join(m["text"] for m in res["messages"])


# ============================================================================
# i. 16bit / RGBA の原本(ビット深度・モードの引き継ぎ、精度)
# ============================================================================
def test_i_out_res_dict_and_source_formats_are_inherited(light):
    """job の out_res(タイル別の dict)と、原本のビット深度・チャンネル構成(RGBA, 16bit)の引き継ぎ。"""
    w = light.world
    # BaseColor を RGBA(アルファ付き)、Height を 16bit グレースケールの原本にする
    for tile, arr in w.old_tiles["BaseColor"].items():
        rgba = np.concatenate([arr, np.full(arr.shape[:2] + (1,), 1.0, np.float32)], axis=2)
        light.rewrite_source("Body_BaseColor.{0}.png".format(tile), rgba, _png_meta(rgba))
    h16 = w.old_tiles["Normal"][1001][..., 0:1]
    light.rewrite_source("Body_Height.1001.png", h16, {"bit_depth": 16, "mode": "I;16", "ext": ".png"})
    # Emissive は 16bit の RGB(Painter が 16bit で書き出す場合)
    rgb16 = w.old_tiles["BaseColor"][1001]
    light.rewrite_source("Body_Emissive.1001.png", rgb16, {"bit_depth": 16, "mode": "RGB", "ext": ".png"})
    chans = [("BaseColor", "srgb", "color"), ("Height", "raw", "scalar"), ("Emissive", "srgb", "color")]
    code, res, log = light.cli(light.write_job("j1", 1, channels=chans,
                                               out_res={"1001": [256, 256], "1002": [512, 512]}))
    assert code == 0, (res["messages"], log)
    assert all(v["status"] == "pass" for v in res["probe"].values()), res["probe"]
    for name, size, mode, depth in (("Body_BaseColor.1001.png", 256, "RGBA", 8), ("Body_BaseColor.1002.png", 512, "RGBA", 8),
                                    ("Body_Height.1001.png", 256, "I;16", 16), ("Body_Emissive.1001.png", 256, "RGB", 16),
                                    ("Body_Emissive.1002.png", 512, "RGB", 16)):
        arr, meta = uvremap_io.read_image(light.out / name)
        assert arr.shape[:2] == (size, size) and meta["mode"] == mode and meta["bit_depth"] == depth, name
    out = uvremap_io.read_image(light.out / "Body_BaseColor.1001.png")[0]
    filled = out[..., :3].sum(-1) > 0.05
    assert filled.mean() > 0.1 and out[..., 3][filled].min() > 0.99     # 描かれた部分のアルファは 1 のまま
    light.assert_source_untouched()


@pytest.mark.parametrize("depth", [8, 16])
def test_i_gray_and_rgb_sources_at_8_and_16_bit(world_factory, tmp_path, depth, capsys):
    """Height(グレー)と Emissive(sRGB の RGB)を 8bit / 16bit の原本で変換し、精度を理想値と比べる。

    16bit の原本は、ビット深度・モード(グレーは I;16、カラーは RGB)を保って出力され、
    量子化誤差が小さい分だけ 8bit より精度が上がる(平滑な信号では量子化が支配的)。
    """
    chans = [CH_ALL[4], CH_ALL[5]]                      # Height(raw, scalar) / Emissive(srgb, color)
    w = world_factory(("gray_rgb",), 512, tc.LAYOUT_A, tc.LAYOUT_B, channels=chans)
    env = Env(tmp_path, w)
    if depth == 16:
        for tile, arr in w.old_tiles["Height"].items():
            env.rewrite_source("Body_Height.{0}.png".format(tile), arr,
                               {"bit_depth": 16, "mode": "I;16", "ext": ".png"})
        for tile, arr in w.old_tiles["Emissive"].items():
            env.rewrite_source("Body_Emissive.{0}.png".format(tile), arr,
                               {"bit_depth": 16, "mode": "RGB", "ext": ".png"})
    code, res, log = env.cli(env.write_job("j1", 1))
    assert code == 0, (res["messages"], log)
    assert all(v["status"] == "pass" and v["n_bad"] == 0 for v in res["probe"].values()), res["probe"]
    expect_modes = {"Height": "I;16" if depth == 16 else "L", "Emissive": "RGB"}
    for s in ("Height", "Emissive"):
        for tile in (1001, 1002):
            arr, meta = uvremap_io.read_image(env.out / "Body_{0}.{1}.png".format(s, tile))
            assert meta["bit_depth"] == depth and meta["mode"] == expect_modes[s], (s, tile, meta)
    psnr = {s: accuracy(env, s)["psnr"] for s in ("Height", "Emissive")}
    with capsys.disabled():
        print("\n[i] {0}bit gray/RGB source PSNR vs ideal: {1}".format(depth, {k: round(v, 1) for k, v in psnr.items()}))
    floor = 45.0 if depth == 8 else 75.0
    assert all(v >= floor for v in psnr.values()), psnr
    env.assert_source_untouched()


# ============================================================================
# f. 強制終了(slow)
# ============================================================================
@slow
def test_f_sigkill_during_conversion_leaves_outputs_intact_and_recovers(world_factory, tmp_path):
    w = world_factory(("big",), 2048, tc.LAYOUT_A, tc.LAYOUT_B, channels=CH_LIGHT)
    env = Env(tmp_path, w)
    code, res, _ = env.cli(env.write_job("j1", 1))
    assert code == 0, res["messages"]
    before = env.published()
    flag_before = (env.out / W.FLAG_NAME).read_bytes()

    job = env.write_job("j2", 2)
    proc = subprocess.Popen([sys.executable, str(WORKER_PY), "run", str(job)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    staging = env.root / "jobs" / "j2" / "staging"
    t0 = time.time()
    while not staging.exists():                      # 変換ループに入るまで待つ
        assert proc.poll() is None and time.time() - t0 < 120, "ワーカーが変換に入る前に終了/停滞した"
        time.sleep(0.02)
    time.sleep(1.0)
    assert proc.poll() is None, "変換が速すぎて途中で kill できない。テクスチャを大きくすること"
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=30)
    proc.stdout.close()
    assert proc.returncode == -signal.SIGKILL

    # 出力は壊れない(旧世代のまま)。flag も ledger もそのまま。status は CONVERTING のまま(pid が死んでいる)
    after = env.published()
    assert after["out"] == before["out"] and after["ledger"] == before["ledger"]
    assert (env.out / W.FLAG_NAME).read_bytes() == flag_before
    assert env.status()[TS]["state"] == "CONVERTING" and env.status()[TS]["generation"] == 2
    for n in before["out"]:
        if n.endswith(".png"):
            uvremap_io.read_image(env.out / n)               # まだ読める
    env.assert_source_untouched()

    # 次の実行(同じ job の再実行)で復帰する。worker.lock は OS が解放している
    code, res, log = env.cli(job)
    assert code == 0, (res["messages"], log)
    assert env.status()[TS]["state"] == "CONVERTED_OK" and env.flag_json()["generation"] == 2
    assert env.ledger()["ts"][TS]["generation"] == 2
    assert not list(env.out.glob("*" + W.TMP_SUFFIX))
    assert not any(".edited_" in p.name for p in env.out.iterdir())
    assert not (env.root / "jobs" / "j2" / "staging").exists()
    for s, _c, _k in CH_LIGHT:
        for tile in (1001, 1002):
            uvremap_io.read_image(env.out / "{0}_{1}.{2}.png".format(TS, s, tile))


# ============================================================================
# j. 時間計測(slow。合否判定なし)
# ============================================================================
class _Timers:
    """worker が呼ぶ画像 I/O と core の変換/プローブの所要時間を足し込む。"""

    def __init__(self, monkeypatch):
        self.t = {"read": 0.0, "core_convert": 0.0, "png_write": 0.0, "core_probe": 0.0}
        for obj, attr, key in ((uvremap_io, "read_image", "read"), (uvremap_io, "write_image", "png_write"),
                               (uvc, "convert_channel", "core_convert"), (uvc, "probe_verify", "core_probe")):
            monkeypatch.setattr(obj, attr, self._wrap(getattr(obj, attr), key))

    def _wrap(self, fn, key):
        def f(*a, **k):
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                self.t[key] += time.perf_counter() - t0
        return f


TIMING_ROWS: list = []


@slow
@pytest.mark.parametrize("res", [1024, 2048])
def test_j_timing_four_tiles_six_channels(res, world_factory, tmp_path, monkeypatch, capsys):
    w = world_factory(("t4", res), res, LAYOUT_4T_OLD, LAYOUT_4T_NEW, channels=CH_ALL)
    env = Env(tmp_path, w)
    assert sorted(w.old_tiles["BaseColor"]) == [1001, 1002, 1011, 1012]
    timers = _Timers(monkeypatch)
    job = env.write_job("j1", 1)
    t0 = time.perf_counter()
    code, r = env.inproc(job)
    wall = time.perf_counter() - t0
    assert code == 0, r["messages"]
    assert len(r["files"]) == 24 and all(v["status"] == "pass" for v in r["probe"].values()), r["probe"]
    tm = r["timings"]
    row = {"res": res, "wall": wall, "copy": tm["copy"], "convert": tm["convert"], "probe": tm["probe"],
           "publish": tm["publish"], **timers.t}
    TIMING_ROWS.append(row)
    with capsys.disabled():
        print("\n[j] 4 tiles x 6 channels @ {0}^2 (in-process, {1} cores)".format(res, os.cpu_count()))
        print("    wall {wall:6.1f}s | copy {copy:5.2f} | convert {convert:6.2f} "
              "(read {read:5.2f} + core {core_convert:6.2f} + png_write {png_write:5.2f}) | "
              "probe {probe:5.2f} | publish {publish:5.2f}".format(**row))
        for s in ("BaseColor", "Normal"):
            print("    probe {0}: {1}".format(s, {k: r["probe"][s][k] for k in ("status", "n_valid", "n_bad")}))
