# -*- coding: utf-8 -*-
"""maya_live_sync.py の回帰テスト(2026.07.25の緊急バグ修正一式向け)。

実行方法(mayapyから直接。Mayaを起動する必要はない):

    "C:/Program Files/Autodesk/Maya2027/bin/mayapy.exe" -m unittest discover -s tests -v

前提・制約:
    - maya.standalone を使うため、mayapy(Maya本体のPython)からのみ実行できる。
      通常のPython(このリポジトリのCIには相当するものが無い)では
      maya.cmds が無く import 自体が失敗する。
    - CONFIG_DIR/CONFIG_PATH をテストごとに専用の一時フォルダへ差し替える
      ため、実際の C:/SPMayaLiveSync (本番の設定・プロジェクトデータ)には
      一切触れない。うっかり本番ディレクトリを参照するテストを書かないよう、
      新規追加時は必ず _IsolatedConfigTestCase を経由すること。
    - 対応する sp_live_sync_plugin.py 側(substance_painter.* に依存)は
      Painterの外からはimportできないため、mayapyではカバーできない
      (CLAUDE.md記載の制約と同じ)。SP側の回帰確認は引き続き実機で行う。
    - 範囲は2026.07.25の修正(正規化のstorage/compare分離、パス比較の
      ケース非依存化、.sptキー除去、Live/Finalフォールバック廃止)に
      意図的に絞っている。maya_live_sync.py全体の網羅的なテストではない。
"""
import os
import sys
import shutil
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import maya.standalone
maya.standalone.initialize()

import maya.cmds as cmds  # noqa: E402 (standalone初期化後でないとimportできない)
import maya_live_sync as mls  # noqa: E402 (同上)


def tearDownModule():
    maya.standalone.uninitialize()


class _IsolatedConfigTestCase(unittest.TestCase):
    """CONFIG_DIR/CONFIG_PATH/CONFIG_LOCK_PATHをテスト専用の一時フォルダへ
    差し替え、シーンを新規状態にリセットしてから各テストを実行する共通
    基底クラス。

    注意: CONFIG_LOCK_PATH は "CONFIG_PATH + '.lock'" としてモジュール
    import時に一度だけ計算される定数であり、CONFIG_PATHを後から書き換え
    ても連動しない。これを差し替え忘れると、save_config()が呼ぶ
    _config_file_lock()が本番の C:/SPMayaLiveSync/live_sync_config.json.lock
    を(内容は書き換えないものの)一瞬ロックしてしまう。テストが本番の
    共有フォルダに一切触れないよう、CONFIG_LOCK_PATHも明示的に差し替える。
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="livesync_test_")
        self._orig_config_dir = mls.CONFIG_DIR
        self._orig_config_path = mls.CONFIG_PATH
        self._orig_config_lock_path = mls.CONFIG_LOCK_PATH
        mls.CONFIG_DIR = self._tmpdir
        mls.CONFIG_PATH = os.path.join(self._tmpdir, "live_sync_config.json")
        mls.CONFIG_LOCK_PATH = mls.CONFIG_PATH + ".lock"
        cmds.file(new=True, force=True)

    def tearDown(self):
        cmds.file(new=True, force=True)
        mls.CONFIG_LOCK_PATH = self._orig_config_lock_path
        mls.CONFIG_DIR = self._orig_config_dir
        mls.CONFIG_PATH = self._orig_config_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase A: _normalize_project_key_for_storage() / _for_compare() の分離
# ---------------------------------------------------------------------------

class NormalizeProjectKeyTests(unittest.TestCase):
    def test_storage_preserves_case_and_unifies_slashes(self):
        self.assertEqual(
            mls._normalize_project_key_for_storage("C:\\MayaPrefs\\SP\\Bar_Table2.spp"),
            "C:/MayaPrefs/SP/Bar_Table2.spp",
        )

    def test_compare_casefolds(self):
        self.assertEqual(
            mls._normalize_project_key_for_compare("C:\\MayaPrefs\\SP\\Bar_Table2.spp"),
            "c:/mayaprefs/sp/bar_table2.spp",
        )

    def test_compare_is_case_and_separator_insensitive(self):
        a = mls._normalize_project_key_for_compare("C:/MayaPrefs/SP/Bar_Table2.spp")
        b = mls._normalize_project_key_for_compare("c:\\mayaprefs\\sp\\bar_table2.spp")
        self.assertEqual(a, b)

    def test_none_and_empty_are_passthrough(self):
        self.assertIsNone(mls._normalize_project_key_for_storage(None))
        self.assertEqual(mls._normalize_project_key_for_storage(""), "")
        self.assertIsNone(mls._normalize_project_key_for_compare(None))


# ---------------------------------------------------------------------------
# Phase B: _canonical_path() / _canonical_dir_map()
# ---------------------------------------------------------------------------

class CanonicalPathTests(unittest.TestCase):
    def test_case_insensitive_membership(self):
        canon_map = mls._canonical_dir_map(["C:/SPMayaLiveSync/live/Bar_Table2_445ba2"])
        probe = mls._canonical_path("c:\\spmayalivesync\\live\\Bar_Table2_445ba2")
        self.assertIn(probe, canon_map)

    def test_preserves_original_casing_for_lookup(self):
        original = "C:/SPMayaLiveSync/live/Bar_Table2_445ba2"
        canon_map = mls._canonical_dir_map([original])
        key = mls._canonical_path("c:/spmayalivesync/live/bar_table2_445ba2")
        self.assertEqual(canon_map[key], original)

    def test_falsy_paths_are_skipped(self):
        canon_map = mls._canonical_dir_map([None, "", "C:/foo"])
        self.assertEqual(len(canon_map), 1)

    def test_canonical_path_of_falsy_is_falsy(self):
        self.assertIsNone(mls._canonical_path(None))
        self.assertEqual(mls._canonical_path(""), "")


# ---------------------------------------------------------------------------
# Phase A: シーンlinkの保存/読み出し(未保存シーン経路で検証)
# ---------------------------------------------------------------------------

class SceneLinkNormalizationTests(_IsolatedConfigTestCase):
    def test_add_scene_project_link_preserves_original_case_on_disk(self):
        link = mls._add_scene_project_link("C:\\MayaPrefs\\SP\\Bar_Table2.spp")
        self.assertIsNotNone(link)
        self.assertEqual(link["sp_project_key"], "C:/MayaPrefs/SP/Bar_Table2.spp")

        # 2026.07.24のバグ(casefold済みで焼き付く)の直接の回帰確認:
        # ディスク上の値も原文ケースのまま保存されていること。
        cfg = mls.load_config()
        stored = cfg["last_scene_project_links"]["__unsaved__"]["links"][0]["sp_project_key"]
        self.assertEqual(stored, "C:/MayaPrefs/SP/Bar_Table2.spp")

    def test_add_scene_project_link_dedupes_case_insensitively(self):
        first = mls._add_scene_project_link("C:/MayaPrefs/SP/Bar_Table2.spp")
        second = mls._add_scene_project_link("c:/mayaprefs/sp/bar_table2.spp")
        payload = mls._get_scene_project_links()
        self.assertEqual(len(payload["links"]), 1)
        self.assertEqual(first["id"], second["id"])

    def test_get_scene_project_links_normalizes_legacy_casefolded_entry(self):
        # 2026.07.24のバグが実際に書き込んでいた形(casefold済み)を
        # 手動で仕込み、読み出し後にSP側の現在の表記と比較正規化で
        # 一致すること(自動停止・二重登録の再発防止)を確認する。
        # 大文字小文字そのものの復元は不可能(失われた情報のため)。
        cfg = mls.load_config()
        cfg["last_scene_project_links"] = {
            "__unsaved__": {
                "links": [{
                    "id": "link_test01",
                    "sp_project_key": "c:/mayaprefs/sp/bar_table2.spp",
                    "label": "",
                    "bound_nodes": [],
                }],
                "active_link_id": "link_test01",
            }
        }
        mls.save_config({"last_scene_project_links": cfg["last_scene_project_links"]})

        payload = mls._get_scene_project_links()
        key = payload["links"][0]["sp_project_key"]
        self.assertEqual(
            mls._normalize_project_key_for_compare(key),
            mls._normalize_project_key_for_compare("C:/MayaPrefs/SP/Bar_Table2.spp"),
        )


# ---------------------------------------------------------------------------
# Phase A-6: _link_display_name() の大文字小文字を無視した重複抑制
# ---------------------------------------------------------------------------

class LinkDisplayNameTests(unittest.TestCase):
    def test_case_insensitive_label_basename_dedup(self):
        link = {
            "sp_project_key": "c:/mayaprefs/sp/bar_table2.spp",
            "label": "Bar_Table2.spp",
        }
        name = mls._link_display_name(link)
        self.assertEqual(name, "bar_table2.spp")
        self.assertNotIn("（", name)


# ---------------------------------------------------------------------------
# D-3: .sptテンプレートキーの除去(Maya側の移行処理)
# ---------------------------------------------------------------------------

class PurgeSptTemplateKeysTests(unittest.TestCase):
    def test_purges_spt_keys_from_by_project_maps(self):
        cfg = {
            "watch_subfolder_by_project": {
                "C:/Templates/Foo.spt": "Foo_abc123",
                "C:/MayaPrefs/SP/Real.spp": "Real_def456",
            },
            "known_texture_sets_by_project": {
                "C:/Templates/Foo.spt": ["Body"],
            },
        }
        result = mls._purge_spt_template_keys(dict(cfg))
        self.assertNotIn("C:/Templates/Foo.spt", result["watch_subfolder_by_project"])
        self.assertIn("C:/MayaPrefs/SP/Real.spp", result["watch_subfolder_by_project"])
        self.assertNotIn("C:/Templates/Foo.spt", result["known_texture_sets_by_project"])
        self.assertTrue(result.get("_purged_spt_keys"))

    def test_no_op_when_no_spt_keys(self):
        cfg = {"watch_subfolder_by_project": {"C:/MayaPrefs/SP/Real.spp": "Real_def456"}}
        result = mls._purge_spt_template_keys(dict(cfg))
        self.assertNotIn("_purged_spt_keys", result)


# ---------------------------------------------------------------------------
# Phase C: _watch_dir_for_project()/_final_dir_for_project() のフォールバック廃止
# ---------------------------------------------------------------------------

class WatchDirForProjectTests(_IsolatedConfigTestCase):
    def test_returns_none_when_unregistered(self):
        w = mls.LiveSyncWatcher()
        w.config["watch_dir"] = os.path.join(self._tmpdir, "live")
        w.config["watch_subfolder_by_project"] = {}
        result = w._watch_dir_for_project("C:/MayaPrefs/SP/NeverExported.spp")
        self.assertIsNone(result)

    def test_returns_subfolder_when_registered(self):
        w = mls.LiveSyncWatcher()
        watch_root = os.path.join(self._tmpdir, "live")
        w.config["watch_dir"] = watch_root
        w.config["watch_subfolder_by_project"] = {
            "C:/MayaPrefs/SP/Real.spp": "Real_abc123",
        }
        result = w._watch_dir_for_project("C:/MayaPrefs/SP/Real.spp")
        self.assertEqual(
            os.path.normpath(result),
            os.path.normpath(os.path.join(watch_root, "Real_abc123")),
        )


# ---------------------------------------------------------------------------
# Phase B+C 統合: switch_texture_quality() のドライブレター大文字小文字
# 不一致に対する回帰テスト(「切り替え対象の file ノードが見つかりません
# でした」の直接の再現・検証)
# ---------------------------------------------------------------------------

class SwitchTextureQualityCaseInsensitiveTests(_IsolatedConfigTestCase):
    def test_switch_finds_node_despite_drive_letter_case_mismatch(self):
        live_root = os.path.join(self._tmpdir, "live")
        final_root = os.path.join(self._tmpdir, "final")
        live_dir = os.path.join(live_root, "Proj_abc123")
        final_dir = os.path.join(final_root, "Proj_abc123")
        os.makedirs(live_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)
        tex_name = "M_Test_BaseColor.png"
        open(os.path.join(live_dir, tex_name), "wb").close()
        open(os.path.join(final_dir, tex_name), "wb").close()

        # switch_texture_quality() は先頭で _refresh_dynamic_config() を
        # 呼び、watch_subfolder_by_project 等の「動的キー」をディスクから
        # 読み直して self.config を上書きする(SP側の更新を追従するための
        # 仕様、_refresh_dynamic_config() のdocstring参照)。そのため
        # w.config への直接代入だけでは _refresh_dynamic_config() の時点で
        # 上書きされて消えてしまう。save_config() でディスク側(この
        # テストでは分離済みの一時フォルダ)にも書き込む必要がある。
        settings = {
            # 設定ファイル側は小文字ドライブレターで保持している状況を再現する。
            "watch_dir": live_root[0].lower() + live_root[1:],
            "final_export_dir": final_root[0].lower() + final_root[1:],
            "watch_subfolder_by_project": {"C:/MayaPrefs/SP/Proj.spp": "Proj_abc123"},
            "final_subfolder_by_project": {"C:/MayaPrefs/SP/Proj.spp": "Proj_abc123"},
        }
        mls.save_config(settings)
        w = mls.LiveSyncWatcher()
        w.config.update(settings)

        # fileノードは大文字ドライブレターのパスを参照している(SP側が
        # 実際に書き出したパス表記との食い違いを再現)。
        node = cmds.shadingNode("file", asTexture=True, isColorManaged=True, name="test_file")
        node_path = os.path.join(live_dir[0].upper() + live_dir[1:], tex_name)
        cmds.setAttr(node + ".fileTextureName", node_path, type="string")

        switched = w.switch_texture_quality(True, project_key="C:/MayaPrefs/SP/Proj.spp")

        self.assertEqual(switched, 1)
        new_path = cmds.getAttr(node + ".fileTextureName")
        # 切り替え先のドライブレターは settings["final_export_dir"] で
        # 意図的に小文字にしているため、比較もケース非依存(_canonical_
        # path())で行う(このテストの主題は「大文字小文字が違っても
        # 同じファイルとして解決できること」そのものなので、これで正しい)。
        self.assertEqual(
            mls._canonical_path(new_path),
            mls._canonical_path(os.path.join(final_dir, tex_name)),
        )

    def test_switch_reports_zero_when_destination_unregistered(self):
        # Phase C: サブフォルダ未登録のプロジェクトへの切り替えは、
        # ルートフォールバックを掴まずに0件・案内メッセージで終わること。
        w = mls.LiveSyncWatcher()
        w.config["watch_dir"] = os.path.join(self._tmpdir, "live")
        w.config["final_export_dir"] = os.path.join(self._tmpdir, "final")
        w.config["watch_subfolder_by_project"] = {}
        w.config["final_subfolder_by_project"] = {}

        switched = w.switch_texture_quality(True, project_key="C:/MayaPrefs/SP/NeverExported.spp")
        self.assertEqual(switched, 0)


# ---------------------------------------------------------------------------
# 2026.07.26: sourceimagesへのFinalテクスチャバックアップ(ユーザー相談で追加)
# ---------------------------------------------------------------------------

class CopyDirContentsAtomicTests(_IsolatedConfigTestCase):
    def test_copies_files_and_returns_count(self):
        src_dir = os.path.join(self._tmpdir, "src")
        dest_dir = os.path.join(self._tmpdir, "dest")
        os.makedirs(src_dir, exist_ok=True)
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(src_dir, "a.png"), "wb") as f:
            f.write(b"fake-image-a")
        with open(os.path.join(src_dir, "b.png"), "wb") as f:
            f.write(b"fake-image-b")
        # サブフォルダはコピー対象外(直下のファイルのみ)であることも確認する。
        os.makedirs(os.path.join(src_dir, "nested"), exist_ok=True)

        copied = mls._copy_dir_contents_atomic(src_dir, dest_dir)

        self.assertEqual(copied, 2)
        with open(os.path.join(dest_dir, "a.png"), "rb") as f:
            self.assertEqual(f.read(), b"fake-image-a")
        with open(os.path.join(dest_dir, "b.png"), "rb") as f:
            self.assertEqual(f.read(), b"fake-image-b")
        self.assertFalse(os.path.isdir(os.path.join(dest_dir, "nested")))
        # 一時ファイル(*.tmpNNN)が残っていないこと。
        leftovers = [n for n in os.listdir(dest_dir) if ".tmp" in n]
        self.assertEqual(leftovers, [])


class BackupFinalTexturesToSourceimagesTests(_IsolatedConfigTestCase):
    def _prepare_workspace(self):
        project_dir = os.path.join(self._tmpdir, "maya_project")
        os.makedirs(project_dir, exist_ok=True)
        cmds.workspace(project_dir, openWorkspace=True)
        return project_dir

    def test_disabled_by_default_does_nothing(self):
        self._prepare_workspace()
        final_dir = os.path.join(self._tmpdir, "final", "Proj_abc123")
        os.makedirs(final_dir, exist_ok=True)
        open(os.path.join(final_dir, "tex.png"), "wb").close()

        w = mls.LiveSyncWatcher()
        w.config["final_export_dir"] = os.path.join(self._tmpdir, "final")
        w.config["final_subfolder_by_project"] = {"C:/MayaPrefs/SP/Proj.spp": "Proj_abc123"}
        # sourceimages_backup_enabled は既定でFalseのまま。
        mls._add_scene_project_link("C:/MayaPrefs/SP/Proj.spp")

        w.backup_final_textures_to_sourceimages()

        sourceimages_dir = mls._maya_project_sourceimages_dir()
        self.assertFalse(os.path.isdir(os.path.join(sourceimages_dir, "Proj_abc123")))

    def test_enabled_copies_final_textures_into_sourceimages_subfolder(self):
        self._prepare_workspace()
        final_dir = os.path.join(self._tmpdir, "final", "Proj_abc123")
        os.makedirs(final_dir, exist_ok=True)
        with open(os.path.join(final_dir, "M_Test_BaseColor.png"), "wb") as f:
            f.write(b"fake-final-texture")

        w = mls.LiveSyncWatcher()
        w.config["final_export_dir"] = os.path.join(self._tmpdir, "final")
        w.config["final_subfolder_by_project"] = {"C:/MayaPrefs/SP/Proj.spp": "Proj_abc123"}
        w.config["sourceimages_backup_enabled"] = True
        mls._add_scene_project_link("C:/MayaPrefs/SP/Proj.spp")

        w.backup_final_textures_to_sourceimages()

        sourceimages_dir = mls._maya_project_sourceimages_dir()
        copied_path = os.path.join(sourceimages_dir, "Proj_abc123", "M_Test_BaseColor.png")
        self.assertTrue(os.path.isfile(copied_path))
        with open(copied_path, "rb") as f:
            self.assertEqual(f.read(), b"fake-final-texture")

    def test_skips_project_without_final_export_yet(self):
        self._prepare_workspace()
        w = mls.LiveSyncWatcher()
        w.config["final_export_dir"] = os.path.join(self._tmpdir, "final")
        w.config["final_subfolder_by_project"] = {}
        w.config["sourceimages_backup_enabled"] = True
        mls._add_scene_project_link("C:/MayaPrefs/SP/NeverExported.spp")

        # 例外を出さずに何もしない(コピー実績0)ことだけを確認する。
        w.backup_final_textures_to_sourceimages()


# ---------------------------------------------------------------------------
# 2026.07.26: stop(reason="reinstall") がwatch_enabledを永続化しないこと
# (ユーザー報告「再インストール後にSPプロジェクト連携が切れる」の回帰テスト)
# ---------------------------------------------------------------------------

class StopReasonTests(_IsolatedConfigTestCase):
    def test_reinstall_reason_preserves_watch_enabled_on_disk(self):
        mls.save_config({"watch_enabled": True})
        w = mls.LiveSyncWatcher()
        w.enabled = True

        w.stop(reason="reinstall")

        self.assertFalse(w.enabled)
        self.assertTrue(mls.load_config().get("watch_enabled"))

    def test_manual_reason_still_persists_watch_enabled_false(self):
        # reason="reinstall"を新設した際に、既存のreason="manual"(ユーザーが
        # 明示的にOFFボタンを押した場合)の挙動まで壊していないことを確認する。
        mls.save_config({"watch_enabled": True})
        w = mls.LiveSyncWatcher()
        w.enabled = True

        w.stop(reason="manual")

        self.assertFalse(w.enabled)
        self.assertFalse(mls.load_config().get("watch_enabled"))


if __name__ == "__main__":
    unittest.main()
