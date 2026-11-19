# media-control

**躺在沙发上，用手机遥控电脑放歌。** 手机只当遥控器，声音从电脑的音响出来。

服务端是 0 依赖的 Python，Windows 侧一个 CDP 桥进程，手机端可以直接用浏览器或用自带的小壳 APK。
支持 B 站搜索 / 队列连播 / 睡前渐弱 / 锁屏媒体键 / 语音点歌。

```
手机浏览器 或 APK（WebView）
   │  HTTP(S)
   ▼
media-web  :8097        ← 鉴权、CSRF、限流、页面、API
   │  常驻 SSH 隧道 127.0.0.1:8765
   ▼
Windows bridge :8765    ← 多线程 HTTP + Chrome DevTools Protocol
   │
   ▼
Edge（受控实例，--remote-debugging-port=9222）→ B 站播放器
```

## 功能

| 分类 | 能力 |
|---|---|
| 播放 | 播放 / 暂停 / 上一首 / 下一首 / 拖动进度 / 倍速 0.75–2x |
| 音量 | **双音量**：视频(bilibili 播放器)音量 与 系统音量 分开调，各带 ±1 微调 |
| 画面 | 网页全屏、弹幕开关、响度均衡（WebAudio 压限） |
| 队列 | 设队列 / 加入 / 删除 / 按相关推荐填充 / 合集或分P一键入队 / 开关连播 / 自动续播防抖 |
| 睡前 | 定时暂停 + 最后 20 秒音量渐弱（到点恢复原音量） |
| 浏览 | 关键词搜索（音乐区过滤、按播放量/最新排序、翻页）、稍后再看、历史、收藏夹 |
| 语音 | Windows 本地识别（sherpa-onnx + silero VAD + Vosk 关键词）：唤醒词、"下一首"、"放一首 XX"，说话时自动压低音乐 |
| 锁屏 | Android 壳 1.9+ 提供系统媒体会话：锁屏 / 通知栏 / 蓝牙耳机按键都能 上/下一首、播放/暂停 |
| 其他 | 局域网内可唤醒电脑（WOL）、可选温湿度卡片、飞书扫码登录、一次性 H5 链接 |

## 快速开始

### 1. Windows 侧：受控浏览器 + 桥

```bat
:: 用独立 profile 起 Edge，开着调试端口
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" ^
  --remote-debugging-port=9222 --remote-allow-origins=* ^
  --user-data-dir=%USERPROFILE%\edge-music --no-first-run ^
  --new-window "https://www.bilibili.com/"
```

```powershell
# 依赖：python 3.9+，pip install websocket-client pycaw comtypes sounddevice vosk sherpa-onnx
# 常驻：注册计划任务，登录即启动（相当于 Windows 的 systemd）
schtasks /Create /TN DodaiosBridge /SC ONLOGON ^
  /TR "pythonw C:\path\to\dodaios-bridge.py" /RL LIMITED /F
```

桥只监听 `127.0.0.1:8765`，不对外暴露。Linux 侧用 SSH 隧道把它接过来：

```ini
# /etc/systemd/system/winpc-bridge-tunnel.service
[Service]
ExecStart=/usr/bin/ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
          -L 127.0.0.1:8765:127.0.0.1:8765 winpc
Restart=always
```

### 2. Linux 侧：media-web

```bash
sudo install -Dm644 server/app.py /opt/media-web/app.py
sudo install -Dm600 bridge/dodaios-bridge.py /opt/media-web/bridge/dodaios-bridge.py

sudo tee /etc/media-web.env >/dev/null <<'EOF'
MEDIA_WEB_USER=media
MEDIA_WEB_PASSWORD=换成你的密码
MEDIA_WIN_HOST=192.168.1.20      # 电脑的局域网 IP，用于在线探测 + WOL
MEDIA_WIN_MAC=aa:bb:cc:dd:ee:ff
EOF
sudo chmod 600 /etc/media-web.env

sudo cp deploy/media-web.service /etc/systemd/system/
sudo systemctl enable --now media-web
```

打开 `http://<服务器IP>:8097/`，用上面那组账号密码登录即可。

**没有配置 `MEDIA_WEB_PASSWORD` 时服务会拒绝所有请求**（fail closed），不会出现"空密码后台"。

### 3. 手机端

- 直接浏览器访问（加桌面快捷方式也行）；
- 或编译自带壳：`cd android && cp config.example.properties config.properties`（填地址和账号）`SDK=~/android-sdk ./build.sh`，得到 `MediaControl-vX.Y.apk`。壳会优先探测局域网地址，不行再走公网；支持锁屏媒体键和自更新。

## 配置项

### media-web（`/etc/media-web.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEDIA_WEB_HOST` / `MEDIA_WEB_PORT` | `0.0.0.0` / `8097` | 监听地址 |
| `MEDIA_WEB_USER` / `MEDIA_WEB_PASSWORD` | `media` / 空 | Basic 认证；密码为空则整体拒服务 |
| `MEDIA_BRIDGE_URL` | `http://127.0.0.1:8765/` | 桥地址（走隧道） |
| `MEDIA_BRIDGE_SSH` | `1` | 隧道挂了时是否退回 `ssh winpc curl` 兜底 |
| `MEDIA_SSH_TARGET` / `MEDIA_SSH_PORT` | `winpc` / `22` | 兜底用的 SSH 目标与在线探测端口 |
| `MEDIA_WIN_HOST` / `MEDIA_WIN_MAC` | 空 | 在线探测（TCP）+ 唤醒（WOL） |
| `MEDIA_STATUS_TTL` | `0.8` | `/api/status` 缓存秒数（多端轮询共享） |
| `MEDIA_FAIL_MAX` / `MEDIA_FAIL_WINDOW` / `MEDIA_LOCK_SECONDS` | `5` / `600` / `900` | 登录失败锁定策略 |
| `MEDIA_TRUST_PROXY` | `1` | 只在本机来源时信任 `CF-Connecting-IP` |
| `MEDIA_PUBLIC_URL` | 空 | 对外地址，用于语音页回调 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_REDIRECT_URI` | 空 | 飞书 OAuth 登录（可不用） |
| `MEDIA_FEISHU_OPENIDS` | 空 | 飞书白名单；留空时退化为"首次登录绑定" |
| `MEDIA_H5_TOKEN_FILE` | `/etc/media-web-h5.token` | 一次性链接 token |
| `MEDIA_CLIMATE_HOST` / `MEDIA_CLIMATE_CMD` | 空 | 可选温湿度卡片，不配就不启用 |

### bridge（Windows）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEDIA_HOME` | `%USERPROFILE%` | 状态/日志/模型的基础目录 |
| `BRIDGE_PORT` | `8765` | 监听端口（只绑 127.0.0.1） |
| `BRIDGE_CDP` | `http://127.0.0.1:9222` | 受控浏览器的调试端点 |
| `BRIDGE_STATE` | `%USERPROFILE%\media-state.json` | 队列/进度/倍速持久化 |
| `BRIDGE_STATUS_TTL` | `0.8` | 状态快照缓存秒数 |
| `BRIDGE_VOICE_DIR` / `BRIDGE_VAD_MODEL` / `BRIDGE_VOSK_DIR` | 见代码默认 | 语音模型目录 |
| `BRIDGE_LOG` | `%USERPROFILE%\dodaios-bridge.log` | 日志 |

## HTTP API

除 `/feishu*`、`/h5*` 外都要求认证；写操作还要带页面下发的 `X-CSRF-Token`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/voice` | 控制页 / 语音页 |
| GET | `/api/health` | 版本、运行时长（用于部署健康检查） |
| GET | `/api/status` | 播放状态（标题、进度、双音量、队列、原因码），带 0.8s 缓存 |
| GET | `/api/pc` | 电脑是否在线、能否唤醒 |
| GET | `/api/tabs` `/api/videos` | 受控浏览器的标签页 / 当前页视频卡片 |
| GET | `/api/search?q=&page=&music=1&order=` | 搜索 |
| GET | `/api/queue/status`、`/api/bili/{watchlater,history,favs,fav}` | 队列与 B 站个人列表 |
| POST | `/api/{play,pause,next,prev,mute,volume,seek,speed,vvol,view,danmaku,eq}` | 播放控制 |
| POST | `/api/{queue/set,queue/add,queue/del,queue/fill,queue/collection,queue/next,queue/prev,queue/auto}` | 队列 |
| POST | `/api/{sleep,wake,tab,open}` | 定时暂停 / 唤醒 / 切标签 / 按 BV 打开 |
| POST | `/api/voice/{test,ack,on,audio}` | 语音 |

失败响应统一 `{"ok": false, "reason": "...", "error": "..."}`，`reason` ∈ `offline` / `bridge_down` / `no_player`，前端据此给不同提示。

## 安全模型

- **鉴权三条路**：Basic（公网走 TLS 反代）、飞书 OAuth、H5 一次性 token，后两者签发 HMAC Cookie。
- **先鉴权再读 body**：未认证请求不会让服务端读请求体（大文件上传接口尤其重要）。
- **CSRF**：写操作必须带页面 nonce 注入的 `X-CSRF-Token`。
- **限流与锁定**：分 IP 令牌桶 60 req/min；同一 IP 10 分钟内口令错 5 次锁 15 分钟。
- **真实 IP**：反代后从 `CF-Connecting-IP` 取，且只在来源是本机时信任该头。
- **CSP**：`default-src 'none'` + 每请求 nonce，禁止内联脚本、外链脚本、iframe。
- **无命令注入面**：端点白名单 + argv 直传，进 shell 的唯一位置是 SSH 兜底，且只传 `shlex.quote` 后的 URL。
- **桥不对外**：Windows 桥只绑 127.0.0.1，靠 SSH 隧道接入。

## 目录结构

```
server/app.py              media-web：HTTP 服务 + 内嵌页面（生成了 nonce/CSRF 注入）
bridge/dodaios-bridge.py   Windows 桥：CDP、队列、语音、媒体键
android/                   手机壳（WebView + 媒体会话前台服务）
deploy/                    systemd unit、部署脚本、Windows 开机自启脚本
tests/                     pytest：假桥 + 纯函数 + 鉴权/限流/缓存行为
```

## 开发与测试

```bash
python3 -m pytest tests -q          # 22 项：服务端行为 + 桥纯函数
./deploy/deploy.sh                 # 同步到 /opt/media-web → 重启 → 健康检查
```

不需要 Windows 也能跑测试：桥里的网络/声卡依赖都是函数内延迟导入，假桥用标准库 HTTP 服务模拟。

## 已知限制

- 只是"遥控器"，音频走电脑音响；手机本身不出声。
- 播放依赖受控浏览器里登录的 B 站账号（会员内容需要会员）。
- 语音识别跑在 Windows 本机，需要麦克风；USB 麦克风热插拔后要重启桥。
- 队列按 BV + 分P 去重，跨 P 的合集识别依赖 B 站页面结构，改版可能失效。

## 版本记录

- **3.5.0**（2026-11-19）：桥改多线程 + 状态快照缓存；`/api/status` 0.8s 缓存；进度条本地外推；轮询自适应；登录失败锁定、真实 IP、先鉴权后读 body、HSTS；搜索/播放改用专属标签页；状态原子落盘；APK 1.9 加入锁屏媒体会话；补 22 项测试与部署脚本。
- **3.4.x**：B 站控制增强（双音量/队列连播/倍速/续播/全屏/弹幕/收藏夹）、本地语音识别。
- **3.3.0**：多源元数据、发现页。

## License

MIT
