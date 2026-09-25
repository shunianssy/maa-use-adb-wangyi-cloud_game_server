# 网易云游戏 · MAA 远程控制台

![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)
![Framework](https://img.shields.io/badge/Framework-AIOHTTP-red.svg)
![Engine](https://img.shields.io/badge/Engine-MaaCore-orange.svg)

本项目把网易云游戏（cg.163.com）封装为一套 **HTTP API + 网页控制台**，让 MAA 直接在云端运行《明日方舟》日常 —— 本机无需模拟器、真机与 ADB。引擎使用 **MaaCore 官方核心**（兼容 ClickSelf 等官方 pipeline 动作），通过「假 adb 桥」把 adb 命令翻译成云游戏 HTTP 调用。

---

## ✨ 功能特性

-   **网页控制台**：浏览器内实时查看云游戏画面（WebRTC 拉流、约 8 FPS 推送），支持单击点击、按住拖拽滑动（兼容触屏）、文本输入、截图保存、坐标显示。
-   **一键长草**：开始唤醒、领取奖励、自动公招、基建换班、每周剿灭、理智作战、信用收支，按依赖顺序自动排队执行。
-   **作战可配置**：关卡、战斗次数、代理倍率、理智药（关 / AUTO / 指定数量）、剿灭（AUTO 刷满止 / 固定场次）、领取奖励细分项，设置即时保存到 `maa_settings.json`。
-   **每日定时执行**：到点自动「启动云游戏 → 一键长草」，当天只触发一次。
-   **云游戏签到**：一键领取网易云游戏平台每日奖励时长（可设为长草前置任务）。
-   **可视化日志**：一键长草日志实时输出，同时自动落盘 `logs/maa_*.log` 便于排查。
-   **双通道**：HTTP API（外部脚本 / MAA 桥接）+ WebSocket（控制台画面与状态同步）。
-   **部署友好**：支持环境变量注入 token 的容器化部署；Windows 下启动自动清理占用端口的残留进程。

---

## 🏗️ 工作原理

```
                         ┌──────────────────────────────┐
   浏览器 WebUI  ⇄HTTP/WS⇄│  server.py (aiohttp :22888)  │
                         │   ├─ WebRTC ←→ 网易云游戏      │
                         │   └─ MaaCoordinator 后台线程   │
                         └──────────────────────────────┘
                                      ↑ HTTP(/screencap /click /swipe /input)
   MaaCore.dll ──adb 命令──> fake_adb/adb.bat ─┘
```

-   **设备链路（本机 Windows）**：MaaCore 通过 `AsstConnect(adb_path=...)` 调用 adb，`fake_adb/adb.bat` 伪装成 adb 可执行文件，把 `exec-out screencap -p`、`shell input tap/swipe/text` 等命令转发到 `server.py` 的 HTTP 接口；实例的 `TouchMode` 设为 `adb`，规避 minitouch/maatouch 部署。
-   **WebUI 链路**：前端通过 WebSocket 接收状态与 JPEG 帧（约 8 FPS），并用每 2 秒轮询 `/maa/status` 兜底同步连接状态。
-   **容器/无头链路**：`maa_bridge/` 使用 MaaFw（MaaFramework Python 绑定）的 CustomController 直接对接 HTTP 接口，由容器入口脚本启动，无需假 adb。

---

## 📁 目录结构

```
server.py                  # 云游戏 HTTP 服务 + WebUI 后端 + 每日定时检查
maa_coordinator.py         # 一键长草协调器(MaaCore 在子线程执行, 日志落盘)
maa_core_wrapper.py        # MaaCore.dll 的 ctypes 封装(Asst* C 接口)
maa_settings.py            # 一键长草设置读写(原子化落盘 maa_settings.json)
fake_adb/                  # 假 adb 桥(adb.bat + fake_adb.py)
maa_pipeline/              # pipeline 占位示例(真实任务请指向官方 resource)
maa_bridge/                # MaaFramework 自定义控制器(容器/无头场景)
webui/                     # 前端控制台(index.html + static/)
sdk/                       # 内置的网易云游戏 SDK(连接 / 签到)
scripts/verify_maacore.py  # 验证 MaaCore.dll 可被 ctypes 驱动的自检脚本
tests/                     # unittest 测试集
Dockerfile / docker-compose.yml / entrypoint.sh
```

---

## 🚀 快速开始（Windows 本机）

### 1. 环境准备

-   Python 3.10+（推荐 3.12，与容器镜像一致）
-   Git
-   MAA 官方核心与资源：`MaaCore.dll` 与 `resource` 目录（已装 maa-cli 时默认位于 `%APPDATA%\loong\maa\data`）

### 2. 克隆项目

SDK 已内置在 `sdk/` 目录，普通克隆即可，无需子模块参数。

```powershell
git clone https://github.com/shunianssy/maa-use-adb-wangyi-cloud_game_server.git
cd maa-use-adb-wangyi-cloud_game_server
```

### 3. 创建虚拟环境并安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **注意**：`aiortc` / `av` 在部分平台需要系统编译或 ffmpeg 运行时（容器镜像已内置 ffmpeg）。依赖清单位于 `sdk/requirements.txt`，根目录 `requirements.txt` 会一并引入。

### 4. 启动服务

```powershell
python server.py
```

-   服务地址：`http://127.0.0.1:22888`
-   **首次运行**时，若根目录没有 `token` 文件，程序会在**运行 `server.py` 的终端**中提示输入手机号以获取登录 token（手机号不落盘），token 保存到 `token` 文件（已被 `.gitignore` 忽略），后续启动自动复用。
-   无交互环境可用环境变量 `NETEASE_TOKEN` 注入 token。

服务启动后终端会输出：

```
[✓] API server is running at http://127.0.0.1:22888
Send POST to /start to connect to the cloud game.
```

### 5. 打开网页控制台

浏览器访问 `http://127.0.0.1:22888/ui`（根路径 `/` 同样直达控制台）：

1.  点击「**启动云游戏**」，等待 WebRTC 建连与首帧画面（终端出现 `[✓] Cloud game ready. API is active.`）。
2.  画面出现后即可直接点击 / 拖拽操作，或在「一键长草」卡片中勾选任务并点「**开始执行**」。

---

## 🖥️ 网页控制台说明

-   **顶部栏**：连接状态徽章、启动云游戏、断开连接。
-   **实时画面**：单击=点击，按住拖拽=滑动，支持触屏拖拽；可开启坐标显示（小工具）。
-   **一键长草卡片**
    -   任务开关：开始唤醒、自动公招、基建换班、理智作战、信用收支、领取奖励。
    -   作战设置：作战关卡（留空=识别当前/上次）、次数、代理倍率、理智药（关 / AUTO / 指定数量）、每周剿灭（AUTO 刷满止 / 固定场次）。
    -   领取奖励选项：每日/每周任务、所有邮件、限定池每日单抽、幸运墙合成玉、开采许可合成玉、周年月卡奖励。
    -   运行前可勾选「云游戏签到」；另提供独立的「云游戏签到」按钮。
    -   实时日志与当前日志文件路径展示。
-   **每日定时自动执行**：启用并设置时间（HH:MM），到点自动启动云游戏并按上方配置执行；`last_daily_run` 防止当日重复触发。
-   **小工具 / 连接信息**：截图保存、坐标显示；状态、分辨率、服务地址、云游戏剩余时长。
-   所有设置即时自动保存到 `maa_settings.json`，重启服务后自动恢复。

---

## 🎮 HTTP API 接口

服务运行在 `http://127.0.0.1:22888`（下例使用 Windows 自带 `curl.exe`，PowerShell 可直接执行）。

### `POST /start`

启动并连接网易云游戏服务。

```powershell
curl.exe -X POST http://127.0.0.1:22888/start
```

```json
{ "status": "ok", "message": "Cloud game connection initiated." }
```

### `GET /info`

获取云游戏状态、分辨率与剩余时长。

-   状态取值：`disconnected`（未连接）/ `connecting`（连接中）/ `ok`（已就绪）。

```json
{ "status": "ok", "width": 1280, "height": 720, "remaining_time": 3600 }
```

```powershell
curl.exe http://127.0.0.1:22888/info
```

### `GET /screencap`

获取当前游戏画面截图（`image/jpeg`）。**需要先成功 `/start`。**

```powershell
curl.exe -o screenshot.jpg http://127.0.0.1:22888/screencap
```

### `POST /click`

在指定坐标点击。**需要先成功 `/start`。**

```powershell
curl.exe -X POST -H "Content-Type: application/json" -d '{"x":640,"y":360}' http://127.0.0.1:22888/click
```

### `POST /swipe`

模拟一次滑动。**需要先成功 `/start`。**

```powershell
curl.exe -X POST -H "Content-Type: application/json" -d '{"x1":100,"y1":200,"x2":800,"y2":200,"duration":500}' http://127.0.0.1:22888/swipe
```

### `POST /input`

输入一段文本（逐字输入）。**需要先成功 `/start`。**

```powershell
curl.exe -X POST -H "Content-Type: application/json" -d '{"text":"arknights"}' http://127.0.0.1:22888/input
```

### `POST /exit`

断开云游戏连接并释放资源。**不会关闭 API 服务本身。**

```powershell
curl.exe -X POST http://127.0.0.1:22888/exit
```

### `GET /ui` · `GET /`

返回网页控制台页面。

### `GET /ws`

WebSocket 端点（控制台使用）：

-   服务器 → 客户端：`{"type":"status",...}`、`{"type":"frame","image":"<base64 JPEG>"}`、`{"type":"ack",...}`
-   客户端 → 服务器：`{"type":"click"|"swipe"|"text"|"start"|"exit"|"resizeRequest", ...}`

### 一键长草接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/maa/start` | 启动一键长草，请求体 `{"tasks": [...], "options": {...}}`（省略的作战项回落到已保存设置） |
| `POST` | `/maa/stop` | 停止当前任务 |
| `GET` | `/maa/status` | 任务状态快照（含云游戏连接状态与 WS 客户端数，前端轮询兜底） |
| `GET` | `/maa/settings` | 读取已保存设置 |
| `POST` | `/maa/settings` | 合并保存设置（前端表单改动时调用） |
| `POST` | `/maa/signin` | 执行网易云游戏签到（需已登录） |
| `POST` | `/maa/adb-log` | 假 adb 命令日志上报（内部使用） |

`/maa/start` 调用示例：

```powershell
curl.exe -X POST -H "Content-Type: application/json" -d '{"tasks":["awaken","recruit","combat"],"options":{"fight":{"stage":"1-7","times":5,"medicine_mode":"auto"}}}' http://127.0.0.1:22888/maa/start
```

任务 key：`awaken`（开始唤醒）、`reward`（领取奖励）、`recruit`（自动公招）、`infrast`（基建换班）、`annihilation`（每周剿灭）、`combat`（理智作战）、`credit`（信用收支）。

---

## ⚙️ 环境变量配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NETEASE_TOKEN` | 空 | 登录凭证；无交互部署时自动写入 token 文件 |
| `NETEASE_TOKEN_FILE` | `token` | token 文件路径 |
| `NETEASE_GAME_CODE` | `mrfz` | 游戏代码，`mrfz` 即《明日方舟》 |
| `NETEASE_HOST` / `NETEASE_PORT` | `127.0.0.1` / `22888` | API 服务监听地址与端口 |
| `NETEASE_WIDTH` / `NETEASE_HEIGHT` | `1280` / `720` | 请求的云游戏分辨率 |
| `NETEASE_WEBUI_DIR` | `webui/` | 前端静态资源目录 |
| `NETEASE_MAA_SETTINGS` | `maa_settings.json` | 一键长草设置文件路径 |
| `MAA_LIB_DIR` | `%APPDATA%\loong\maa\data\lib` | `MaaCore.dll` 所在目录 |
| `MAA_DATA_DIR` | `%APPDATA%\loong\maa\data` | MAA 数据目录（需含 `resource/`） |
| `MAA_USER_DIR` | `<MAA_DATA_DIR>\debug` | MaaCore 实例用户目录（必须已存在） |
| `FAKE_ADB_PATH` | `fake_adb/adb.bat` | 传给 MaaCore 的假 adb 路径 |
| `FAKE_ADB_BASE` | `http://127.0.0.1:22888` | 假 adb 转发的云游戏 HTTP 地址 |
| `FAKE_ADB_SERIAL` | `127.0.0.1:5555` | 伪设备序列号（与 `AsstConnect` 的 address 一致） |

---

## 🧩 MAA 核心与资源准备

-   引擎从 `MAA_LIB_DIR` 加载 `MaaCore.dll`（初始化顺序严格对齐 maa-cli：`SetDllDirectoryW` → `AsstSetUserDir` → `AsstLoadResource` → `AsstCreate` → `AsstConnect`）。
-   路径不同时请通过环境变量覆盖，并先运行自检脚本确认可驱动：

```powershell
.\.venv\Scripts\python.exe scripts/verify_maacore.py
```

-   仓库内的 `maa_pipeline/` 仅为占位示例 pipeline，无法用于真实任务；请使用官方 `resource`（MAA 发行版或 maa-cli 资源目录）。
-   一键长草执行日志自动保存到 `logs/maa_YYYYMMDD_HHMMSS.log`（含 MaaCore 回调与假 adb 命令记录）。

---

## 🐳 容器部署

服务器 / NAS 上部署时使用 `docker compose`，本机浏览器直接访问控制台。

```bash
docker compose up -d --build
docker compose logs -f
```

-   入口脚本 `entrypoint.sh` 流程：注入 `NETEASE_TOKEN`（可选）→ 后台启动 `server.py` → 等待 `/info` 就绪（最多 60s）→ 前台运行 `maa_bridge`。
-   资源挂载：`./maa_resource:/app/resource:ro`（compose 已配置 `--resource /app/resource`，也可替换为 `--task <任务名>` 直接执行）。
-   端口映射：`22888:22888`；环境变量 `NETEASE_HOST` / `NETEASE_PORT` / `NETEASE_GAME_CODE` 可按需调整。

---

## ✅ 运行测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

覆盖内容：一键长草参数构造与任务状态机、假 adb 命令翻译、设置读写与原子落盘、控制器契约、端口占用清理等。

---

## ❓ 常见问题

-   **提示需要重新登录 / token 失效**：删除 `token` 文件后重启 `server.py` 重新输入手机号，或设置 `NETEASE_TOKEN` 环境变量。
-   **连接失败、截图超时**：云游戏免费时长耗尽时云端会拒绝建连，请先在网易云游戏完成签到或充值后重试。
-   **端口被占用**：Windows 下启动时会自动结束占用 `22888` 端口的残留进程（最多重试 3 次）。
-   **浏览器显示「未连接」但终端已就绪**：后端通过 WebSocket 广播 + 每 2 秒轮询 `/maa/status` 双通道同步；若仍异常请强制刷新页面（Ctrl+Shift+R）。
-   **为什么不用 MaaFramework 内置资源跑任务**：官方 `resource` 含 `ClickSelf` 等动作，需由 MaaCore 核心解析；容器场景的 `maa_bridge` 则用 MaaFw 绑定实现 HTTP 自定义控制器。

---

## 🙏 致谢

-   内置 SDK（`sdk/` 目录）：提供网易云游戏 WebRTC / WebSocket 连接与每日签到能力。
-   [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)：MaaCore / MaaFramework 官方核心与框架。
-   [aiohttp](https://github.com/aio-libs/aiohttp)、[aiortc](https://github.com/aiortc/aiortc)：异步 HTTP 服务与 WebRTC 支持。

## 📄 License

Apache License 2.0