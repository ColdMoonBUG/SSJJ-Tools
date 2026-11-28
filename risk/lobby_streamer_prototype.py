"""Build an isolated AIR lobby streamer-mode experiment.

This module does not install the result or change the running lobby.  The
generated SWF is an experiment until its AIR sandbox and refresh behaviour
have been verified in a running client.
"""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


_PORTRAIT = "lobby.view.main.SubNewPlayerProtrait"
_ROOM = "lobby.view.room.TeamCellRenderer"
_JOIN_ROOM = "lobby.controller.UserJoinRoomCommand"
_JOIN_GLOBAL_ROOM = "globalRoom.controller.room.UserJoinGRoomCommand"

_IMPORTS = """   import flash.events.Event;
   import flash.utils.getTimer;
"""

_HELPER = """
      private var streamerNext:int = 0;
      private function streamerName(original:String) : String
      {
         var fs:Object = null;
         try
         {
            var fileClass:Class = getDefinitionByName("flash.filesystem::File") as Class;
            var f:Object = new fileClass((CONFIG_PATH).split("|")[0]);
            if(!f.exists) return original;
            var streamClass:Class = getDefinitionByName("flash.filesystem::FileStream") as Class;
            fs = new streamClass();
            fs.open(f,"read");
            var raw:String = fs.readUTFBytes(fs.bytesAvailable);
            fs.close();
            fs = null;
            var cfg:Object = JSON.parse(raw);
            if(cfg && cfg.enabled && cfg.lobby_overlay && cfg.fake_name) return String(cfg.fake_name);
         }
         catch(e:Error) { if(fs) { try { fs.close(); } catch(ignored:Error) {} } }
         return original;
      }
"""

_PORTRAIT_HELPER = """
      private var streamerInfo:UserInfo;
      private function streamerUpdate(e:Event) : void
      {
         if(getTimer() < streamerNext || !streamerInfo || !playerNameTF) return;
         streamerNext = getTimer() + 500;
         playerNameTF.text = "Lv:" + streamerInfo.lv + " " + SensitiveWordFilter.replaceSensitiveWord(streamerName(streamerInfo.playerName));
      }
"""

_ROOM_HELPER = """
      private function streamerUpdate(e:Event) : void
      {
         if(getTimer() < streamerNext || !_data || !_data.isSelf || !playerName) return;
         streamerNext = getTimer() + 500;
         var name:String = streamerName(_data.playerName);
         if(ProxyUtils.instance.channelsProxy.isNewGlobalChannel())
         {
            name = name.charAt(0).split("&").join("&amp;").split("<").join("&lt;").split(">").join("&gt;");
            playerName.htmlText = "<font color='#ffffff'><font color='#C53F53'>[" + _data.countryName + "]</font>" + name + "*****</font>";
         }
         else
         {
            name = name.split("&").join("&amp;").split("<").join("&lt;").split(">").join("&gt;");
            var text:String = _data.dName.length ? "<font color='#00ff00'>[" + _data.dName + "]</font>" + name : name;
            playerName.htmlText = "<font color='#ffffff'>" + SensitiveWordFilter.replaceSensitiveWord(text) + "</font>";
         }
      }
"""


def _once(source, old, new):
    count = source.count(old)
    if count != 1:
        raise ValueError("大厅脚本与预期版本不一致：%r 出现 %d 次" % (old, count))
    return source.replace(old, new, 1)


def _config_helper(config_path, diagnostic=False):
    path_literal = json.dumps(str(config_path), ensure_ascii=True)
    helper = _HELPER.replace("CONFIG_PATH", path_literal)
    if diagnostic:
        helper = _once(helper, "if(!f.exists) return original;",
                       'if(!f.exists) return "[诊断:配置文件不存在]";')
        helper = _once(helper, "catch(ignored:Error) {} } }\n         return original;",
                       'catch(ignored:Error) {} } return "[诊断:配置读取异常]"; }\n'
                       "         return original;")
    return helper


def patch_sources(portrait, room, config_path, diagnostic=False):
    """Prepare two AS3 classes; reject unknown layouts before building."""
    helper = _config_helper(config_path, diagnostic)
    portrait = _once(portrait, "   import assets.CommonAssets;",
                     _IMPORTS + "   import flash.utils.getDefinitionByName;\n"
                     "   import assets.CommonAssets;")
    portrait = _once(portrait, "      private var rankIcon:Sprite;",
                     helper + _PORTRAIT_HELPER + "      private var rankIcon:Sprite;")
    portrait = _once(portrait, "         var _loc8_:Object = null;",
                     "         var _loc8_:Object = null;\n"
                     "         streamerInfo = param1;\n"
                     "         addEventListener(Event.ENTER_FRAME,streamerUpdate,false,0,true);")
    portrait = _once(portrait, "SensitiveWordFilter.replaceSensitiveWord(param1.playerName)",
                     "SensitiveWordFilter.replaceSensitiveWord(streamerName(param1.playerName))")
    portrait = _once(portrait, '         expBar.removeEventListener("mouseOut",onMouseHandler);',
                     "         removeEventListener(Event.ENTER_FRAME,streamerUpdate);\n"
                     "         streamerInfo = null;\n"
                     '         expBar.removeEventListener("mouseOut",onMouseHandler);')

    room = _once(room, "   import assets.MainAssets;", _IMPORTS + "   import assets.MainAssets;")
    room = _once(room, "      public var overBg:Sprite;",
                 helper + _ROOM_HELPER + "      public var overBg:Sprite;")
    room = _once(room, "         _data = param1;",
                 "         _data = param1;\n"
                 "         addEventListener(Event.ENTER_FRAME,streamerUpdate,false,0,true);")
    room = _once(room, '_data.playerName.charAt(0) + "*****"',
                 '(_data.isSelf ? streamerName(_data.playerName) : _data.playerName).charAt(0) + "*****"')
    room = _once(room, '         teamNameLb.text = "";',
                 '         streamerNext = 0;\n'
                 '         streamerUpdate(null);\n'
                 '         teamNameLb.text = "";')
    room = _once(room, "         super.dispose();",
                 "         removeEventListener(Event.ENTER_FRAME,streamerUpdate);\n"
                 "         super.dispose();")
    return portrait, room


def patch_join_source(source, config_path, diagnostic=False):
    """Replace only the displayed name in the local player's room-entry notice."""
    helper = _config_helper(config_path, diagnostic)
    helper += """
      private function streamerJoinName(original:String, characterId:Number) : String
      {
         try
         {
            var user:UserProxy = facade.retrieveProxy("UserProxy") as UserProxy;
            if(user && user.userInfo && characterId == user.userInfo.globalCharId)
               return streamerName(original);
         }
         catch(e:Error) {}
         return original;
      }
"""
    source = _once(source, "   import lobby.model.ChatProxy;",
                   "   import flash.utils.getDefinitionByName;\n"
                   "   import lobby.model.UserProxy;\n"
                   "   import lobby.model.ChatProxy;")
    source = _once(source, "      private var _socialProxy:SocialProxy;",
                   helper + "      private var _socialProxy:SocialProxy;")
    for method in ("friendJoinMessage", "userJoinMessage"):
        source = _once(source, "_loc4_.%s(_loc3_.playerName" % method,
                       "_loc4_.%s(streamerJoinName(_loc3_.playerName,_loc3_.characterId)" % method)
    return source


def _run(args):
    result = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if result.returncode:
        raise RuntimeError("大厅实验构建失败：%s" % (result.stderr or result.stdout).strip())


def build_prototype(ffdec, source, output, config_path, diagnostic=False):
    """Write a separate SWF; never modify the source or install the result."""
    ffdec, source, output = Path(ffdec), Path(source), Path(output)
    if not ffdec.is_file() or not source.is_file():
        raise FileNotFoundError("找不到 FFDec 或源大厅 SWF")
    if source.resolve() == output.resolve():
        raise ValueError("输出文件不能覆盖源大厅")
    if output.exists():
        raise FileExistsError("输出文件已存在：%s" % output)
    if not Path(config_path).is_absolute():
        raise ValueError("直播配置路径必须为绝对路径")
    with tempfile.TemporaryDirectory(prefix="ssjj-streamer-") as tmp:
        tmp = Path(tmp)
        _run([str(ffdec), "-selectclass", ",".join((_PORTRAIT, _ROOM, _JOIN_ROOM, _JOIN_GLOBAL_ROOM)),
              "-export", "script", str(tmp / "export"), str(source)])
        base = tmp / "export" / "scripts"
        portrait = (base / "lobby/view/main/SubNewPlayerProtrait.as").read_text(encoding="utf-8-sig")
        room = (base / "lobby/view/room/TeamCellRenderer.as").read_text(encoding="utf-8-sig")
        join = (base / "lobby/controller/UserJoinRoomCommand.as").read_text(encoding="utf-8-sig")
        global_join = (base / "globalRoom/controller/room/UserJoinGRoomCommand.as").read_text(encoding="utf-8-sig")
        portrait, room = patch_sources(portrait, room, config_path, diagnostic=diagnostic)
        join = patch_join_source(join, config_path, diagnostic=diagnostic)
        global_join = patch_join_source(global_join, config_path, diagnostic=diagnostic)
        pfile, rfile = tmp / "portrait.as", tmp / "room.as"
        jfile, gfile = tmp / "join.as", tmp / "global_join.as"
        pfile.write_text(portrait, encoding="utf-8")
        rfile.write_text(room, encoding="utf-8")
        jfile.write_text(join, encoding="utf-8")
        gfile.write_text(global_join, encoding="utf-8")
        built = tmp / "result.swf"
        _run([str(ffdec), "-air", "-replace", str(source), str(built),
              _PORTRAIT, str(pfile), _ROOM, str(rfile),
              _JOIN_ROOM, str(jfile), _JOIN_GLOBAL_ROOM, str(gfile)])
        if not built.is_file() or built.stat().st_size == 0:
            raise RuntimeError("FFDec 未生成大厅实验副本")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as target:
            target.write(built.read_bytes())
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffdec", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", default=os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "ssjj-toolbox", "streamer_mode.json"))
    args = parser.parse_args()
    print(build_prototype(args.ffdec, args.source, args.output, args.config))


if __name__ == "__main__":
    main()
