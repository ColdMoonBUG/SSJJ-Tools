import hashlib
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "toolbox"), str(ROOT)]

import custom_mods
import skin_pack


def _fake_unityfs_bytes(unity_ver="2019.4.14f1", payload=b"FAKE_BUNDLE_PAYLOAD"):
    return (
        b"UnityFS\x00"
        + struct.pack(">I", 6)
        + b"5.x.x\x00"
        + unity_ver.encode("ascii")
        + b"\x00"
        + payload
    )


class CustomModsTest(unittest.TestCase):
    def _patch_env(self, root: Path):
        state_dir = root / "state"
        mods_dir = state_dir / "custom_mods"
        overrides = state_dir / "bundle_overrides.json"
        out_dir = root / "static_mods"
        preload = root / "preload"
        return (
            patch.object(custom_mods, "STATE_DIR", state_dir),
            patch.object(custom_mods, "CUSTOM_MODS_DIR", mods_dir),
            patch.object(custom_mods, "OVERRIDES", overrides),
            patch.object(custom_mods, "MOD_OUT_DIR", out_dir),
            patch.object(custom_mods, "PRELOAD", preload),
            overrides,
            out_dir,
        )

    def test_import_assetbundle_header_validation_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p1, p2, p3, p4, p5, overrides, _out_dir = self._patch_env(root)

            # Seed an existing skin_pack override that must NOT be clobbered by rebuild_overrides.
            overrides.parent.mkdir(parents=True, exist_ok=True)
            overrides.write_text(json.dumps({
                "bundles": {
                    "weapon/m4a1": {
                        "md5": "0" * 32,
                        "crc": 123,
                        "size": 100,
                        "path": str(root / "legacy_skin_pack.bundle"),
                    }
                }
            }), encoding="utf-8")

            bad_file = root / "not_a_bundle.bundle"
            bad_file.write_bytes(b"NOT_UNITY_HEADER_123456")

            ab_ok = root / "my_ak47.bundle"
            ab_ok_bytes = _fake_unityfs_bytes("2019.4.14f1", b"CUSTOM_AK47_MODEL_V1")
            ab_ok.write_bytes(ab_ok_bytes)

            ab_other_ver = root / "other_ver.bundle"
            ab_other_ver.write_bytes(_fake_unityfs_bytes("2022.3.10f1", b"OTHER_VER_MODEL"))

            with p1, p2, p3, p4, p5, patch.object(skin_pack, "index", return_value={
                "weapon/ak47": {"md5": "a" * 32, "size": 1024},
            }):
                # 1) Invalid header rejected
                res_bad = custom_mods.import_mod(str(bad_file), target_bundle="weapon/ak47")
                self.assertFalse(res_bad["ok"])

                # 2) Valid Unity 2019.4.14f1 bundle imported cleanly without version warning
                res_ok = custom_mods.import_mod(str(ab_ok), target_bundle="ak47", mod_name="自制AK47")
                self.assertTrue(res_ok["ok"], res_ok)
                self.assertEqual(res_ok["warning"], "")
                mod_id = res_ok["mod"]["id"]
                self.assertEqual(res_ok["mod"]["target_bundle"], "weapon/ak47")
                self.assertEqual(res_ok["mod"]["unity_version"], "2019.4.14f1")

                # Verify overrides merged cleanly while keeping skin_pack entry
                ov = json.loads(overrides.read_text(encoding="utf-8"))["bundles"]
                self.assertIn("weapon/m4a1", ov)
                self.assertIn("weapon/ak47", ov)
                self.assertEqual(ov["weapon/ak47"]["source"], "custom_mod")
                self.assertEqual(ov["weapon/ak47"]["md5"], hashlib.md5(ab_ok_bytes).hexdigest())

                # 3) Non-2019.4 bundle still imports with a clear version warning
                res_warn = custom_mods.import_mod(str(ab_other_ver), target_bundle="weapon/awm")
                self.assertTrue(res_warn["ok"], res_warn)
                self.assertIn("2022.3.10f1", res_warn["warning"])

                # 4) list_mods shows both mods
                mods = custom_mods.list_mods()
                self.assertEqual(len(mods), 2)

                # 5) Disable mod -> removed from bundle_overrides.json, skin_pack preserved
                res_dis = custom_mods.set_mod_enabled(mod_id, False)
                self.assertTrue(res_dis["ok"], res_dis)
                ov2 = json.loads(overrides.read_text(encoding="utf-8"))["bundles"]
                self.assertNotIn("weapon/ak47", ov2)
                self.assertIn("weapon/m4a1", ov2)

                # 6) Re-enable mod -> restored in bundle_overrides.json
                res_en = custom_mods.set_mod_enabled(mod_id, True)
                self.assertTrue(res_en["ok"], res_en)
                ov3 = json.loads(overrides.read_text(encoding="utf-8"))["bundles"]
                self.assertIn("weapon/ak47", ov3)

                # 7) Delete mod -> removed from disk and overrides
                res_del = custom_mods.delete_mod(mod_id)
                self.assertTrue(res_del["ok"], res_del)
                ov4 = json.loads(overrides.read_text(encoding="utf-8"))["bundles"]
                self.assertNotIn("weapon/ak47", ov4)
                self.assertEqual(len(custom_mods.list_mods()), 1)

    def test_import_texture_pack_dir_and_zip_and_reject_zip_slip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p1, p2, p3, p4, p5, overrides, out_dir = self._patch_env(root)

            # Build a fake preload bundle file so _resolve_target_bundle succeeds
            preload_file = root / "preload" / ("b" * 32) / "data"
            preload_file.parent.mkdir(parents=True, exist_ok=True)
            preload_file.write_bytes(_fake_unityfs_bytes("2019.4.14f1", b"ORIG_AK47"))

            # 1) Directory texture_pack
            tp_dir = root / "src_tp"
            tp_dir.mkdir()
            (tp_dir / "ak47_d.png").write_bytes(b"\x89PNG\r\n\x1a\nFAKEPNG")
            (tp_dir / "mod.json").write_text(json.dumps({
                "id": "tp_dir_mod",
                "name": "目录贴图包",
                "type": "texture_pack",
                "target_bundle": "weapon/ak47",
                "textures": {"ak47_d": "ak47_d.png"},
            }, ensure_ascii=False), encoding="utf-8")

            # 2) Zip texture_pack
            tp_zip = root / "tp_zip_mod.zip"
            with zipfile.ZipFile(tp_zip, "w") as zf:
                zf.writestr("inner/mod.json", json.dumps({
                    "id": "tp_zip_mod",
                    "name": "ZIP贴图包",
                    "type": "texture_pack",
                    "target_bundle": "weapon/ak47",
                    "textures": {"ak47_d": "ak47_d.png"},
                }, ensure_ascii=False))
                zf.writestr("inner/ak47_d.png", b"\x89PNG\r\n\x1a\nFAKEPNG2")

            # 3) Malicious Zip Slip archive
            bad_zip = root / "evil.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("../escape.txt", "pwned")

            def fake_build(mod, _mod_dir, _orig_path):
                out_dir.mkdir(parents=True, exist_ok=True)
                out_bundle = out_dir / ("custom_%s.bundle" % mod["id"])
                out_bundle.write_bytes(_fake_unityfs_bytes("2019.4.14f1", b"BUILT_" + mod["id"].encode()))
                return out_bundle, 1, ""

            with p1, p2, p3, p4, p5, \
                 patch.object(skin_pack, "index", return_value={"weapon/ak47": {"md5": "b" * 32, "size": 512}}), \
                 patch.object(custom_mods, "_build_texture_pack_bundle", side_effect=fake_build):
                res_dir = custom_mods.import_mod(str(tp_dir))
                self.assertTrue(res_dir["ok"], res_dir)
                self.assertEqual(res_dir["mod"]["id"], "tp_dir_mod")

                res_zip = custom_mods.import_mod(str(tp_zip))
                self.assertTrue(res_zip["ok"], res_zip)
                self.assertEqual(res_zip["mod"]["id"], "tp_zip_mod")

                res_evil = custom_mods.import_mod(str(bad_zip))
                self.assertFalse(res_evil["ok"])

                rb = custom_mods.rebuild_overrides()
                self.assertTrue(rb["ok"], rb)
                self.assertEqual(rb["applied"], 1)  # both target weapon/ak47 -> 1 bundle slot
                ov = json.loads(overrides.read_text(encoding="utf-8"))["bundles"]
                self.assertIn("weapon/ak47", ov)
                self.assertEqual(ov["weapon/ak47"]["source"], "custom_mod")


if __name__ == "__main__":
    unittest.main()
