import unittest

from risk.lobby_streamer_prototype import patch_sources, patch_join_source


PORTRAIT = """   import assets.CommonAssets;
      private var rankIcon:Sprite;
         var _loc8_:Object = null;
         playerNameTF.text = SensitiveWordFilter.replaceSensitiveWord(param1.playerName);
         expBar.removeEventListener("mouseOut",onMouseHandler);
"""
ROOM = """   import assets.MainAssets;
   import flash.utils.getDefinitionByName;
      public var overBg:Sprite;
         _data = param1;
         _loc2_ = _data.playerName.charAt(0) + "*****";
         teamNameLb.text = "";
         super.dispose();
"""
JOIN = """   import lobby.model.ChatProxy;
      private var _socialProxy:SocialProxy;
         _loc2_.userJoin(_loc3_);
         _loc4_.friendJoinMessage(_loc3_.playerName,_loc3_.vipLv);
         _loc4_.userJoinMessage(_loc3_.playerName,0,_loc3_.vipLv);
"""


class LobbyStreamerPrototypeTest(unittest.TestCase):
    def test_display_uses_config_and_only_room_self(self):
        portrait, room = patch_sources(PORTRAIT, ROOM, r"C:\测试\streamer_mode.json")
        self.assertIn(r"C:\\\u6d4b\u8bd5\\streamer_mode.json", portrait)
        self.assertIn('cfg.enabled && cfg.lobby_overlay', portrait)
        self.assertIn("streamerName(param1.playerName)", portrait)
        for source in (portrait, room):
            self.assertEqual(source.count("import flash.utils.getDefinitionByName;"), 1)
            self.assertIn('getDefinitionByName("flash.filesystem::File")', source)
            self.assertNotIn("flash.utils.getDefinitionByName(", source)
        self.assertIn("!_data.isSelf", room)
        self.assertIn('_data.isSelf ? streamerName(_data.playerName) : _data.playerName', room)
        self.assertIn('removeEventListener(Event.ENTER_FRAME,streamerUpdate)', room)
        self.assertNotIn("_data.playerName = ", room)

    def test_unknown_or_duplicated_layout_is_rejected(self):
        with self.assertRaises(ValueError):
            patch_sources(PORTRAIT.replace("rankIcon", "unknown"), ROOM, r"C:\cfg.json")
        with self.assertRaises(ValueError):
            patch_sources(PORTRAIT, ROOM + '         _data = param1;', r"C:\cfg.json")

    def test_diagnostic_build_displays_config_read_failures(self):
        portrait, room = patch_sources(PORTRAIT, ROOM, r"C:\cfg.json", diagnostic=True)
        for source in (portrait, room):
            self.assertIn("[诊断:配置文件不存在]", source)
            self.assertIn("[诊断:配置读取异常]", source)
            self.assertIn("cfg.enabled && cfg.lobby_overlay", source)

    def test_join_notice_rewrites_only_name_for_matching_character(self):
        source = patch_join_source(JOIN, r"C:\cfg.json")
        self.assertIn("characterId == user.userInfo.globalCharId", source)
        self.assertIn("_loc2_.userJoin(_loc3_);", source)
        self.assertIn("friendJoinMessage(streamerJoinName(_loc3_.playerName,_loc3_.characterId)", source)
        self.assertIn("userJoinMessage(streamerJoinName(_loc3_.playerName,_loc3_.characterId)", source)
        self.assertNotIn("_loc3_.playerName =", source)
        self.assertIn("return original;", source)
        with self.assertRaises(ValueError):
            patch_join_source(JOIN.replace("userJoinMessage", "unknown"), r"C:\cfg.json")


if __name__ == "__main__":
    unittest.main()
