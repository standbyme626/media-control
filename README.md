<div align="center">

# media-control

**躺在被窝里，用手机控制电脑上的音乐**

Windows SMTC · 受控 Edge + CDP · B 站搜索直连 · 10 分钟自动刷新 · 飞书 OAuth

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/Windows-SMTC%20%2F%20WinRT-0078D4?logo=windows&logoColor=white)
![Edge](https://img.shields.io/badge/Edge-CDP-0078D7?logo=microsoftedge&logoColor=white)
![Linux](https://img.shields.io/badge/Server-Linux%20systemd-FCC624?logo=linux&logoColor=black)
![License](https://img.shields.io/badge/License-MIT-green)

[项目概览](#1-项目概览) · [核心特性](#2-核心特性) · [界面](#3-界面) · [系统架构](#4-系统架构) · [快速开始](#6-快速开始) · [API](#7-api) · [踩坑](#11-工程实践与踩坑)

</div>

> **项目定位：家庭局域网的个人工具，不是远程桌面。**
> 只做「媒体控制」一件事：看现在放什么、暂停/继续、调音量、搜歌点播。
> 不碰桌面、不抢焦点、不需要把 Windows 解锁到桌面。

**2 个进程** · **1034 行服务端** · **571 行 Windows 桥** · **0 第三方依赖（服务端）** · **10 分钟自动刷新**

---

## 目录

- [1. 项目概览](#1-项目概览)
- [2. 核心特性](#2-核心特性)
- [3. 界面](#3-界面)
- [4. 系统架构](#4-系统架构)
- [5. 关键设计](#5-关键设计)
- [6. 快速开始](#6-快速开始)
- [7. API](#7-api)
- [8. 部署](#8-部署)
- [9. 测试与验证](#9-测试与验证)
- [10. 已知限制](#10-已知限制)
- [11. 工程实践与踩坑](#11-工程实践与踩坑)
- [12. 目录结构](#12-目录结构)
- [13. 安全须知](#13-安全须知)
- [14. Roadmap](#14-roadmap)

---

## 1. 项目概览

### 1.1 要解决的问题

晚上躺在床上，电脑在书桌上接着音箱放歌。想切歌、暂停、把音量调小，或者干脆关掉，都得爬起来走到电脑前。

远程桌面太重：要解锁、要看清桌面、要在一堆窗口里点来点去；手机上的远程控制 App 还要装客户端、配对、保持后台。很多时候我只是想「把歌关掉」或者「换一首」，不需要看到整个桌面。

### 1.2 方案

把「放」和「控」拆开：

- **放**：一个专用受控浏览器（Edge，独立 profile）负责打开 B 站并播放；
- **控**：一个网页（服务器上跑）负责展示状态、发指令；
- **桥**：Windows 上的小服务，把网页指令翻译成 SMTC / 音量 / CDP 动作。

手机、平板、另一台电脑，打开浏览器就能用，不装任何客户端。

## 2. 核心特性

| 能力 | 实现 | 说明 |
|---|---|---|
| 播放 / 暂停 | WinRT SMTC | 读系统媒体会话，失败时回退媒体键 |
| 系统音量 | pycaw | 调 Windows 主音量，不是单个网页的音量 |
| B 站搜索 | 受控 Edge + CDP | 返回封面、标题、UP 主、播放量、弹幕数、时长 |
| 点播 | CDP `Page.navigate` | 在专用播放器标签打开并播放，自动复用标签 |
| 自动刷新 | 前端 10 分钟定时 | 页面不可见时不刷，回来补刷 |
| 换一批 | `page + 1` | 翻到下一页搜索结果 |
| 登录鉴权 | Basic + 飞书 OAuth | 飞书 OAuth 可选，open_id 白名单 |
| 状态展示 | `/api/status` | 曲目、App、播放状态、音量 |

## 3. 界面

![界面截图](docs/screenshot.png)

左侧是遥控器（当前播放、进度、暂停/播放、音量滑条），右侧是 B 站搜索结果网格。窄屏自动退回单列，手机竖屏也能用。

## 4. 系统架构

### 4.1 总览

```
手机 / 电脑浏览器
        │ HTTPS + Basic Auth（Nginx / Tunnel）
        ▼
media-web（Linux，Python 标准库，:8097）
        │ SSH 固定命令（参数白名单）
        ▼
Windows Bridge（127.0.0.1:8765）
        ├─ WinRT SMTC / pycaw（播放、系统音量）
        └─ CDP 127.0.0.1:9222（受控 Edge：标签、搜索、点播）
        ▼
受控 Edge 播放 → Windows 音频输出 → 音箱
```

### 4.2 一次点歌的完整过程

```
1. 手机输入关键词 → GET /api/search?q=xxx&page=1
2. media-web 校验参数 → ssh winpc "curl 127.0.0.1:8765/cdp/search?q=..."
3. 桥通过 CDP 让受控 Edge 打开 B 站搜索页
4. 桥等待渲染，执行 JS 抓视频卡片 → 返回 JSON
5. 前端渲染网格；用户点卡片 → POST /api/open?bv=BVxxxx
6. 桥导航到视频页 → 执行 video.play()
7. 前端每 4 秒轮询 /api/status 更新进度与状态
```

## 5. 关键设计

### 5.1 为什么拆成三个进程

Web 服务跑在 Linux 服务器上（常开、有反向代理和证书），桥必须跑在 **Windows 交互桌面会话** 里——UI Automation 和音频控制离开交互会话就用不了，所以桥不能和 Web 服务同机同进程。

### 5.2 为什么用受控 Edge 而不是接口

不保存 B 站 Cookie、不模拟登录，直接在真实浏览器里操作：登录态天然复用，页面改版时改选择器即可，也不会触发风控。

### 5.3 为什么所有动作都是固定端点

网页不能传任意命令、任意路径、任意 URL 给服务器。每个动作对应一个写死的分支，参数只接受整数或 BV 号，从根上杜绝注入。

## 6. 快速开始

### 6.1 组件清单

| 位置 | 组件 | 依赖 |
|---|---|---|
| Windows | 受控 Edge | 无（系统自带/安装 Edge） |
| Windows | `bridge/dodaios-bridge.py` | `websocket-client`、`pycaw`、`pywin32`、`psutil` |
| Linux | `server/app.py` | 无（Python 标准库） |
| Linux | systemd + 反向代理 | 可选 |

### 6.2 配置

复制 `config.example.env`，按需填写：

| 变量 | 说明 |
|---|---|
| `MEDIA_WEB_USER` / `MEDIA_WEB_PASSWORD` | 网页 Basic 认证；密码为空时拒绝登录 |
| `MEDIA_WEB_HOST` / `MEDIA_WEB_PORT` | 监听地址与端口，默认 `127.0.0.1:8097` |
| `MEDIA_WIN_HOST` | SSH 到 Windows 的主机别名 |
| `MEDIA_BRIDGE_URL` | 桥地址，默认 `http://127.0.0.1:8765/` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 可选，飞书 OAuth 登录 |
| `FEISHU_REDIRECT_URI` | 可选，飞书 OAuth 回调地址 |

### 6.3 三步启动

**第一步：Windows 侧起桥**

```bat
pip install websocket-client pycaw pywin32 psutil
python dodaios-bridge.py
curl http://127.0.0.1:8765/health
```

建议用计划任务在登录时启动，勾选「只在用户登录时运行」。

**第二步：Windows 侧起受控 Edge**

```powershell
powershell -ExecutionPolicy Bypass -File register-music-edge.ps1
```

它会用独立 profile（`%USERPROFILE%\edge-music`）启动 Edge，CDP 只绑 `127.0.0.1:9222`。B 站登录可选。

**第三步：Linux 侧起网页**

```bash
mkdir -p /opt/media-web
cp server/app.py /opt/media-web/
cp config.example.env /etc/media-web.env   # chmod 600
systemctl enable --now media-web
```

浏览器打开 `http://服务器IP:8097/`。

## 7. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 控制页面 |
| GET | `/api/status` | 当前曲目、App、播放状态、音量 |
| POST | `/api/pause` | 暂停 |
| POST | `/api/play` | 播放 |
| POST | `/api/volume?level=` | 系统音量 0–100 |
| GET | `/api/search?q=&page=` | B 站搜索 |
| POST | `/api/open?bv=` | 打开并播放指定视频 |
| GET | `/api/videos` | 抓当前搜索页的卡片 |

## 8. 部署

```bash
# 1. systemd 服务
cp deploy/media-web.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now media-web

# 2. 反向代理（示例：Caddy）
# media.example.com {
#     reverse_proxy 127.0.0.1:8097
# }
```

**不要把 8765 / 9222 暴露到公网。**

## 9. 测试与验证

| 检查 | 方法 |
|---|---|
| 桥存活 | `curl http://127.0.0.1:8765/health` |
| CDP 可用 | `curl http://127.0.0.1:9222/json/version` |
| 反代与鉴权 | 未认证访问应返回 401 |
| 播放链路 | 网页点「播放」，观察 Windows 媒体会话变化 |

## 10. 已知限制

- Windows 必须开机且用户已登录，桥不在时无法控制。
- 受控 Edge 必须运行；CDP 挂了搜索和点播就没法用。
- 系统音量是全局的，会同时影响电脑上其他声音。
- B 站页面结构会变，列表变空时需要更新选择器。
- 同一关键词短时间刷新通常还是同一批结果，用「换一批」翻页看更多。

## 11. 工程实践与踩坑

| 坑 | 现象 | 处理 |
|---|---|---|
| UI Automation 读不到 | SSH 里直接跑 bridge 拿不到媒体信息 | 桥必须跑在 **session 1 交互会话**里 |
| CSRF 每次重启失效 | 页面按钮全 403 | token 持久化到文件，前端遇 403 自动刷新 |
| URL 里的 `&` 被 cmd 拆开 | 搜索参数丢失 | 整条 URL 加双引号 |
| 默认 profile 被禁调试 | Chrome 136+ 静默禁用 CDP | 必须用独立 `--user-data-dir` |
| SSH 反向隧道 `localhost` 解析 | `Empty reply from server` | Windows 把 localhost 解析成 `::1`，用 `127.0.0.1` |

## 12. 目录结构

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

## 13. 安全须知

- 网页只提供固定动作，没有任意命令 / 路径 / URL 执行。
- 服务端校验全部参数：音量 0–100，BV 号 `^BV[0-9A-Za-z]+$`。
- Basic 认证 + 持久化 CSRF + 每 IP 限速。
- 桥与 CDP 只绑 `127.0.0.1`。
- 仓库不含密码、域名、IP、主机名或个人路径。
- 家庭场景请优先 VPN / Tailscale / Cloudflare Access。

## 14. Roadmap

- [ ] 收藏夹 / 稍后再看（受控 Edge 登录后扩展）
- [ ] 歌词显示
- [ ] 飞书卡片按钮直接控制
- [ ] macOS 版本（已完成：[media-control-mac](https://github.com/standbyme626/media-control-mac)）

## License

MIT
