# SSJJ 大厅资源本地加速

把《生死狙击》(4399/Wooduan 客户端) 的大厅资源预下载到本地，让活动页、签到、
背包、仓库这些「点一下转半天圈」的界面变成毫秒级打开。

战斗资源（进图加载）是另一条链路，见下面「战斗资源」一节。

---

## 它是怎么工作的

客户端大厅是 Adobe AIR 程序，所有资源从 `http://cdn.4399ssjj.com/s5/...` 拉。
把域名指向本机，再在本地把这些资源回放出来：

```
hosts:  127.0.0.1 cdn.4399ssjj.com

   :80  → local_cdn.py
            命中 lobby_res_cache/  → 直接返回（实测 0~3ms）
            未命中                  → 回源真 CDN，顺手存进库（下次就命中）
   :443 → 纯 TCP 透传真 CDN（TLS 端到端，证书不受影响，HTTPS 功能正常）
```

关键在于 443 必须有人监听。上一轮「劫持后游戏起不来」就是因为 443 没人听 →
连接被拒。现在透传，不会再有这个问题。

第一次打开某个页面会慢一下（要回源），之后就快了。这是设计如此——
命中率会随着使用自然爬升，日志里能看到 `HIT` / `MISS`。

---

## 快速开始

双击 **`1-启动加速并进游戏.bat`**（会自己弹 UAC 要管理员权限，因为要写 hosts）。

其它入口：

| 文件 | 用途 |
| --- | --- |
| `2-只启动加速服务.bat` | 不启动游戏，只起加速服务 |
| `3-停止加速并还原.bat` | 停服务 + 还原 hosts |
| `4-查看加速状态.bat` | 命中率、回源次数、最近 20 条请求 |
| `5-安装开机自启.bat` / `6-取消开机自启.bat` | 开机自启开关 |
| `启动加速器.bat` | 图形界面 |
| `一键预下载全部.bat` | 战斗资源全量预下载（约 17.3GB） |
| `大厅资源-补齐缺失.bat` | 预下载清单 5264 个 + 补齐上局缺的（推荐先跑一遍） |
| `大厅资源-查看覆盖率.bat` | 查看还差多少，100% 就是再也不用等 |
| `大厅资源-穷举探测.bat` | 探测武器/道具/皮肤图标这类动态拼名的资源 |

---

## 战斗资源

战斗端的 AssetBundle 走另一条链路，不需要 hosts：

```
version.php → AssetBundles_<md5>2019.manifest → <bundleName>_<md5>
存到 %LOCALAPPDATA%\Wooduan\Preload\<md5>\data，客户端自己读本地缓存
```

`一键预下载全部.bat` 或 `python ssjj_boost.py preload --match all --jobs 12`。
断点续传，中途 Ctrl+C 也没关系，重跑会跳过已完成的。

---

## 命令行

```bash
python local_cdn.py run          # 前台跑服务
python local_cdn.py daemon       # 守护模式（会自动重拉）
python local_cdn.py install      # 只写 hosts
python local_cdn.py uninstall    # 停服务 + 还原 hosts
python local_cdn.py status       # 命中率 / 回源节点
python local_cdn.py startup      # 安装开机自启

python ssjj_boost.py status      # 战斗资源缓存情况
python ssjj_boost.py preload --list
python ssjj_boost.py gui

python exhaustive_fetch.py stats      # 大厅资源库统计
python exhaustive_fetch.py cover      # 补齐上局实际请求过但缺失的资源
python exhaustive_fetch.py cover --dry-run   # 只看覆盖率，不下载
python exhaustive_fetch.py prewarm    # 补清单里缺的
python exhaustive_fetch.py probe      # 采样看各类资源命中率
```

排障时可以带 `CDN_TRACE=1` 跑，会输出请求级的 trace 到 stderr。

---

## 覆盖率与 304 那个坑

`res_config` 清单只覆盖启动必需的 5264 个资源，已全部下完。但客户端还会按需
请求一批清单里**根本没有**的资源（角色动画、角色模型、大厅武器预览、武器图标
等，已发现 682 个），这些只能靠「实际请求过就补」来补全：

```
python exhaustive_fetch.py cover --dry-run   # 看还差多少
python exhaustive_fetch.py cover             # 补下来
```

服务以 `daemon` 方式启动时会**在后台静默跑一次 cover**，所以第一局玩到什么，
第二局进去那些就全是本地命中。

关于 `MISS ... 0B`：这是条件请求踩的坑。客户端带 `If-Modified-Since` 来问，
上游回 `304 Not Modified`（空正文），代理把它原样转发，但客户端本地其实没有
这份文件 → 拿到空内容 → 重试 → 又是 304，永远补不进库。现在回源时会剥掉
`If-Modified-Since` / `If-None-Match` / `If-Range`，强制拿 200 完整正文；
上游若仍回 304 且本地无副本，自动改用无条件 GET 重取。

---

## 排障

**服务起不来 / 状态显示未运行**
80 或 443 被别的程序占了。`netstat -ano | findstr :80` 看是谁。
注意别同时跑 `_trash/old_scripts/fps_proxy.py`，它也会抢 80。

**游戏里还是卡**
先看 `4-查看加速状态.bat` 的命中率。命中率低说明这些资源还没缓存过——
第一次慢是正常的。如果所有请求都是 MISS 且回源失败，看日志里的回源节点，
`upstream.json` 里记着测速结果。

**hosts 没生效**
hosts 里应该有 `127.0.0.1 cdn.4399ssjj.com # ssjj-local-cdn`。
那行带 `# ssjj-local-cdn` 标记，由脚本管理，别手改。
写不进去就是没管理员权限。

**服务反复重启**
守护模式启动失败时会指数退避（2/4/8/.../60 秒），日志里会写明第几次失败。

---

## 目录

```
local_cdn.py          本地加速服务主体
ssjj_paths.py         公共路径 + paras.dat 读取
core.py               战斗资源下载与校验
ssjj_boost.py         命令行入口
gui.py                图形界面
exhaustive_fetch.py   大厅零碎资源穷举探测
lobby_cache_tool.py   【已废弃】WinINet 方案，仅留工具函数
make_bats.py          bat 的唯一来源
lobby_res_cache/      大厅资源库（加速服务读这个）
_trash/               归档的废弃文件，确认不需要可整个删掉
```

---

## 两条注意事项

`paras.dat`（`C:\Program Files (x86)\Wooduan\SSJJ-4399\Lobby\paras.dat`）是
客户端启动参数文件，里面写着资源地址、版本号、CDN 节点。`ssjj_paths.py`
读它，回源节点优先用它的 `cdnIp`——客户端自己挑的节点通常比 DoH 给的快得多
（实测差 49 倍）。

`hall_config.xml` 是特例：客户端每次都带时间戳请求它（故意破缓存），所以它
永远不命中。加速服务固定回本地副本，本地副本超过 6 小时会重新回源，因为
官方也会改这个文件。
