# 媒体控制（Media Control Web）

躺在床上，用手机控制电脑上的音乐。

![界面截图](docs/screenshot.png)

## 为什么写这个项目

晚上躺在床上，电脑在书桌上接着音箱放歌。想切歌、暂停、把音量调小，或者干脆关掉，都得爬起来走到电脑前。

用远程桌面又太重：要解锁、要看清桌面、要在一堆窗口里点来点去；手机上的远程控制 App 也往往要装客户端、配对、保持后台。很多时候我只是想“把歌关掉”或者“换一首”，不需要看到整个桌面。

于是就写了这个网页：手机、平板、另一台电脑打开浏览器，就能看到电脑上正在播什么、暂停/播放、调系统音量，还能直接搜 B站 歌单，点封面就让电脑播放。它只做“媒体控制”这一件事，打开即用，不碰桌面，不抢焦点，也不需要把 Windows 解锁到桌面。

后来需求慢慢长出来：

- 不想每次都去 B站 搜索，于是网页里直接搜歌，显示封面、UP 主、播放量、弹幕数和时长；
- 想让列表自己更新，于是加了 10 分钟自动刷新和手动刷新；
- 想换一批歌，于是加了「换一批」翻页；
- 想在卧室、客厅、书房都能用，于是手机和电脑共用同一个页面；
- 想让它稳定一点，于是把播放控制、搜索、点播拆成固定端点，不让网页执行任意命令。

这个项目的目标不是做“远程桌面”，而是把“躺在床上控制电脑放歌”这件小事做得足够顺手。它也适合接音箱的客厅电脑、HTPC、书房台式机：家里人打开链接就能暂停/切歌，不用学远程桌面。

## 功能

- 播放 / 暂停当前媒体会话（Windows SMTC；失败时回退媒体键）。
- 系统音量滑条（pycaw，调的是 Windows 主音量，不是单个网页的音量）。
- B站 搜索：封面、标题、UP 主、播放量、弹幕数、时长。
- 点任意结果：在受控 Edge 中打开并播放；自动复用播放器标签，不堆一堆标签。
- 10 分钟自动刷新（页面可见时）+ 手动「刷新」+「换一批」翻页。
- 顶部显示上次刷新时间和倒计时；刷新失败保留旧列表，不白屏。
- 手机 / 平板 / 电脑浏览器均可使用；HTTPS + Basic 认证。

## 架构

```
手机 / 电脑浏览器
        │ HTTPS + Basic Auth（反向代理 / Tunnel）
        ▼
media-web（Linux，Python 标准库）
        │ SSH（固定命令，参数校验）
        ▼
Windows Bridge（127.0.0.1:8765）
        ├─ WinRT SMTC / pycaw（播放、系统音量）
        └─ CDP 127.0.0.1:9222（受控 Edge：标签、搜索、点播）
```

- `server/app.py`：网页与 API。固定动作端点，不拼接 shell，参数只接受整数或 BV 号。
- `bridge/dodaios-bridge.py`：Windows 侧小 HTTP 服务，只绑 `127.0.0.1`。
- 受控 Edge 用独立 profile 启动，开 `--remote-debugging-port=9222`；网页通过 CDP 读取 B站 页面并点播。
- 服务器不保存 B站 账号 Cookie；搜索和播放都走受控浏览器。

## 快速开始

### 1. Windows：启动桥

1. 把 `bridge/dodaios-bridge.py` 放到 Windows 用户目录。
2. 安装 Python 依赖：pywinrt（`winrt-*`）、`pycaw`、`pywinauto`、`psutil`、`websocket-client`。
3. 创建一个计划任务，在用户登录时启动桥，并勾选“只在用户登录时运行”（桥必须跑在交互桌面会话里，UI Automation 和音频控制才能用）。
4. 验证：

```bat
curl http://127.0.0.1:8765/health
```

### 2. Windows：启动受控 Edge

把 `deploy/start-music-edge.bat` 和 `deploy/register-music-edge.ps1` 放到同一目录，按需修改 Edge 路径，然后运行：

```powershell
powershell -ExecutionPolicy Bypass -File register-music-edge.ps1
```

它会用独立 profile（`%USERPROFILE%\edge-music`）启动 Edge，并开启只绑本机的 CDP 端口 `9222`。B站 登录是可选的：不登录也能搜索和播放，登录后画质/推荐更完整。

### 3. Linux：启动网页

1. 把 `server/app.py` 放到 `/opt/media-web/app.py`。
2. 复制 `config.example.env` 为 `/etc/media-web.env`，权限 600，填入用户名、强密码、Windows 主机 SSH 别名。
3. 安装 `deploy/media-web.service` 到 `/etc/systemd/system/media-web.service`，然后：

```bash
systemctl daemon-reload
systemctl enable --now media-web
```

4. 用 Nginx/Caddy/Cloudflare Tunnel 等做 HTTPS 反向代理，并加一层认证。**不要把 8765 或 9222 暴露到网络。**

## 配置

参考 `config.example.env`：

| 变量 | 说明 |
|---|---|
| `MEDIA_WEB_USER` | 网页 Basic 认证用户名 |
| `MEDIA_WEB_PASSWORD` | 网页 Basic 认证密码；**必须设置**，为空时拒绝登录 |
| `MEDIA_WEB_HOST` | 监听地址，默认 `127.0.0.1`；局域网用可设 `0.0.0.0` |
| `MEDIA_WEB_PORT` | 监听端口，默认 `8097` |
| `MEDIA_WIN_HOST` | SSH 到 Windows 的主机别名/地址 |
| `MEDIA_BRIDGE_URL` | 桥地址，默认 `http://127.0.0.1:8765/` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 可选的飞书 OAuth 登录 |
| `FEISHU_REDIRECT_URI` | 可选，飞书 OAuth 回调地址 |

## 安全说明

- 网页本身只提供固定动作：暂停、播放、音量、搜索、打开 BV 号；没有任意命令、任意路径或任意 URL 执行。
- 所有参数在服务端校验：音量 0–100，BV 号只允许 `BV[0-9A-Za-z]+`，CDP target id 只允许十六进制。
- Basic 认证 + 持久化 CSRF token（`/etc/media-web-csrf.token`，600）+ 每 IP 限速。
- 桥和 CDP 都只绑 `127.0.0.1`，不要做端口转发到公网。
- 仓库里不包含密码、域名、IP、主机名或个人路径；请使用自己的环境变量。
- 这是家庭/个人场景的工具，不建议直接裸露在公网；优先 VPN、Tailscale、Cloudflare Access 或反向代理认证。

## 已知限制

- B站 页面结构会变；如果列表变空，更新 `bridge/dodaios-bridge.py` 里的 `VIDEO_JS` 选择器。
- Windows 必须开机且用户已登录；桥不在时无法控制。
- 受控 Edge 必须运行；CDP 不可用时会回退到 UI Automation/媒体会话，但搜索/点播需要 CDP。
- 系统音量是全局的，会同时影响电脑上其他声音。
- 同一关键词短时间刷新通常还是同一批结果，用「换一批」翻页可以看到更多。
- 不保存 B站 账号，因此默认只能搜索公开内容；收藏夹/稍后再看需要在受控 Edge 里登录后另行扩展。

## 目录结构

```
media-control/
├── server/app.py                 # 网页 + API（Linux）
├── bridge/dodaios-bridge.py      # Windows 桥（SMTC / 音量 / CDP）
├── deploy/
│   ├── media-web.service         # systemd 模板
│   ├── start-music-edge.bat      # 受控 Edge 启动模板
│   └── register-music-edge.ps1   # 计划任务模板
├── config.example.env
├── docs/screenshot.png
└── README.md
```

## License

MIT
