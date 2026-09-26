# 网易云游戏 · MAA 远程控制台

![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)
![Framework](https://img.shields.io/badge/Framework-AIOHTTP-red.svg)
![Engine](https://img.shields.io/badge/Engine-MaaCore-orange.svg)

本项目把网易云游戏（cg.163.com）封装为一套 **HTTP API + 网页控制台**，让 MAA 直接在云端运行《明日方舟》日常 —— 本机无需模拟器、真机与 ADB。引擎使用 **MaaCore 官方核心**（兼容 ClickSelf 等官方 pipeline 动作），通过「假 adb 桥」把 adb 命令翻译成云游戏 HTTP 调用。

# 严肃警告

本软件使用 AGPL-3.0 传播，maa-cli使用 AGPL-3.0 传播，本项目遵循开源协议传播

本软件开源、免费，仅供学习交流使用。若您遇到商家使用本软件进行代练并收费，可能是设备与时间等费用，产生的问题及后果与本软件无关。

---

## 功能特性

-   **网页控制台**：浏览器内实时查看云游戏画面（WebRTC 拉流、约 8 FPS 推送），支持单击点击、按住拖拽滑动（兼容触屏）、文本输入、截图保存、坐标显示。
-   **账号登录**：网页控制台内支持「短信验证码」与「手机号 + 密码」两种登录方式（密码登录需当前 token 有效，加密流程与网页端一致）；存在 token 文件即显示「已登录」，token 自动落盘复用。
-   **一键长草**：开始唤醒、领取奖励、自动公招、基建换班、每周剿灭、理智作战、信用收支，按依赖顺序自动排队执行；勾选「开始唤醒」时若云游戏未连接会自动先启动云游戏；单个任务内子任务出错达到 5 次时自动跳过该任务并继续后续任务。
-   **作战可配置**：关卡、战斗次数（数字 / AUTO 刷完当前理智自动停）、理智药（关 / AUTO / 指定数量）、剿灭（AUTO 刷满止 / 固定场次）、领取奖励细分项，设置即时保存到 `maa_settings.json`。
-   **每日定时执行**：到点自动「启动云游戏 → 一键长草」，当天只触发一次；旁边提供「测试执行」按钮可立即手动跑一次完整流程（不影响定时记录）。
-   **云游戏签到**：一键领取网易云游戏平台每日奖励时长（接口 `POST /api/v2/sign-today`，与网页端一致）；只要已登录（有 token 文件）即可签到，无需先启动云游戏。
-   **可视化日志**：一键长草日志实时输出，同时自动落盘 `logs/maa_*.log` 便于排查。
-   **双通道**：HTTP API（外部脚本 / MAA 桥接）+ WebSocket（控制台画面与状态同步）。
-   **部署友好**：容器化部署支持环境变量注入 token，并在首次启动时自动拉取 MAA 官方 Linux 核心与资源（支持镜像加速）；Windows 下启动自动清理占用端口的残留进程。

---

## 工作原理

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
-   **容器/无头链路**：容器内使用官方 **Linux MaaCore**（`libMaaCore.so` + 官方 `resource`，由 `scripts/fetch_maa_resource.py` 在首次启动时自动拉取），假 adb 换成 `fake_adb/adb.sh`，链路与本机一致；`maa_bridge/`（MaaFw CustomController）保留为备选桥接方案。

---


## 目录结构

```
server.py                  # 云游戏 HTTP 服务 + WebUI 后端 + 每日定时检查
maa_coordinator.py         # 一键长草协调器(MaaCore 在子线程执行, 日志落盘)
maa_core_wrapper.py        # MaaCore 的 ctypes 封装(Windows MaaCore.dll / Linux libMaaCore.so)
maa_settings.py            # 一键长草设置读写(原子化落盘 maa_settings.json)
netease_login.py           # 网易云游戏登录(短信/密码)与每日签到(按线上接口抓包实现)
fake_adb/                  # 假 adb 桥(Windows adb.bat / Linux adb.sh + fake_adb.py)
maa_pipeline/              # pipeline 占位示例(真实任务请指向官方 resource)
maa_bridge/                # MaaFramework 自定义控制器(备选桥接模式)
webui/                     # 前端控制台(index.html + static/)
sdk/                       # 内置的网易云游戏 SDK(连接 / 签到)
scripts/fetch_maa_resource.py  # MAA 核心与资源自动拉取(容器首次启动调用)
scripts/verify_maacore.py  # 验证 MaaCore 可被 ctypes 驱动的自检脚本(跨平台)
tests/                     # unittest 测试集
maa_data/                  # 容器自动拉取的 MaaCore + resource(运行时生成, 已 gitignore)
Dockerfile / docker-compose.yml / entrypoint.sh
```

---

## 快速开始（Windows 本机）

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
.venv\Scripts\Activate.ps1
python server.py
```

-   服务地址：`http://127.0.0.1:22888`
-   **登录方式（任选其一）**
    1.  **网页控制台登录**（推荐）：打开控制台 → 右侧「云游戏账号」卡片 → 选「短信验证码」（手机号 → 发送验证码 → 输入验证码）或「密码登录」（手机号 + 账号密码）→ 登录；token 自动保存到根目录 `token` 文件，徽标随即显示「已登录」。
        > 密码登录依赖接口 `/api/v2/user-pwd-info`（要求当前 token 有效）获取加密参数；若 token 已失效，请先用短信验证码登录。
    2.  **终端交互登录**：若根目录没有 `token` 文件，程序会在**运行 `server.py` 的终端**中提示输入手机号（手机号不落盘）。
    3.  **环境变量注入**：无交互环境可设置 `NETEASE_TOKEN`（容器部署常用）。
-   `token` 文件已被 `.gitignore` 忽略，后续启动自动复用；更换账号在网页控制台重新登录即可（云游戏会话进行中时新 token 下次启动生效）。

服务启动后终端会输出：

```
[OK] API server is running at http://127.0.0.1:22888
Send POST to /start to connect to the cloud game.
```

### 5. 打开网页控制台

浏览器访问 `http://127.0.0.1:22888/ui`（根路径 `/` 同样直达控制台）：

1.  点击「**启动云游戏**」，等待 WebRTC 建连与首帧画面（终端出现 `[OK] Cloud game ready. API is active.`）。
2.  画面出现后即可直接点击 / 拖拽操作，或在「一键长草」卡片中勾选任务并点「**开始执行**」。

---

## 网页控制台说明

-   **顶部栏**：连接状态徽章、启动云游戏、断开连接。
-   **左侧栏**：上方为实时游戏画面（单击=点击，按住拖拽=滑动，支持触屏拖拽；可开启坐标显示），下方为一键长草运行日志与日志落盘路径。
-   **右侧栏（设置）**
    -   云游戏账号：短信验证码 / 密码登录两种方式切换（发送验证码后 60 秒倒计时）；徽标显示「已登录 / 未登录」与云游戏剩余时长；有 `token` 文件即视为已登录。
    -   任务开关：开始唤醒、自动公招、基建换班、理智作战、信用收支、领取奖励。勾选「开始唤醒」后点「开始执行」，若云游戏尚未连接会自动先启动云游戏并等待画面就绪。
    -   作战设置：作战关卡（留空=识别当前/上次）、次数（数字 / AUTO 刷完当前理智自动停；代理倍率恒为 AUTO）、理智药（关 / AUTO / 指定数量）、每周剿灭（AUTO 刷满止 / 固定场次）。
    -   基建设置：换班模式（常规模式 / 自定义基建模式 / 队列轮换）、参与换班的设施（制造站、贸易站、控制中心、发电站、会客室、办公室、宿舍、加工站、训练室）、无人机用途、心情阈值、源石碎片自动补货、宿舍信赖位 / 未进驻、会客室信息板 / 线索交流 / 赠送线索。
    -   领取奖励选项：每日/每周任务、所有邮件、限定池每日单抽、幸运墙合成玉、开采许可合成玉、周年月卡奖励。
    -   运行前可勾选「云游戏签到」；另提供独立的「云游戏签到」按钮。
-   **每日定时自动执行**：启用并设置时间（HH:MM），到点自动启动云游戏并按上方配置执行；`last_daily_run` 防止当日重复触发；旁边的「测试执行」按钮可立即手动跑一次「启动云游戏 → 一键长草」（不写入 `last_daily_run`）。
-   **小工具 / 连接信息**：截图保存、坐标显示；状态、分辨率、服务地址、云游戏剩余时长。
-   所有设置即时自动保存到 `maa_settings.json`，重启服务后自动恢复。

---

## HTTP API 接口

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
| `POST` | `/maa/signin` | 执行网易云游戏签到（`POST /api/v2/sign-today`；只要已登录即可，无需先启动云游戏） |
| `POST` | `/maa/daily/test` | 「测试执行」：立即跑一次「启动云游戏 → 一键长草」（不写入 `last_daily_run`） |
| `POST` | `/maa/adb-log` | 假 adb 命令日志上报（内部使用） |

`/maa/start` 调用示例：

```powershell
curl.exe -X POST -H "Content-Type: application/json" -d '{"tasks":["awaken","recruit","combat"],"options":{"fight":{"stage":"1-7","times":5,"medicine_mode":"auto"}}}' http://127.0.0.1:22888/maa/start
```

任务 key：`awaken`（开始唤醒）、`reward`（领取奖励）、`recruit`（自动公招）、`infrast`（基建换班）、`annihilation`（每周剿灭）、`combat`（理智作战）、`credit`（信用收支）。

说明：`options.fight.times` 支持传数字或 `"auto"`（等于刷完当前理智自动停）；代理倍率固定为 AUTO，无需配置。

### 云游戏账号登录接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/login/sms` | 发送短信验证码，请求体 `{"phone": "138..."}` |
| `POST` | `/api/login/verify` | 短信验证码登录，请求体 `{"phone": "...", "code": "..."}`；成功后 token 落盘 |
| `POST` | `/api/login/password` | 密码登录，请求体 `{"phone": "...", "password": "..."}`；需当前 token 有效（用于获取加密参数） |
| `GET` | `/api/login/status` | 登录状态（存在 token 文件即为已登录）；带 `?remaining=1` 时附带云游戏剩余时长 |

登录成功后，若当前没有进行中的云游戏会话，token 立即对进程内签到等接口生效；否则下次启动云游戏时生效。

```powershell
curl.exe -X POST -H "Content-Type: application/json" -d '{"phone":"13800001111"}' http://127.0.0.1:22888/api/login/sms
curl.exe -X POST -H "Content-Type: application/json" -d '{"phone":"13800001111","code":"123456"}' http://127.0.0.1:22888/api/login/verify
curl.exe -X POST -H "Content-Type: application/json" -d '{"phone":"13800001111","password":"你的密码"}' http://127.0.0.1:22888/api/login/password
```

---

## 环境变量配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NETEASE_TOKEN` | 空 | 登录凭证；无交互部署时自动写入 token 文件 |
| `NETEASE_TOKEN_FILE` | `token` | token 文件路径 |
| `NETEASE_API_BASE` | `https://n.cg.163.com` | 云游戏开放接口根地址（网页控制台登录使用，可指向代理或测试服务） |
| `NETEASE_GAME_CODE` | `mrfz` | 游戏代码，`mrfz` 即《明日方舟》 |
| `NETEASE_HOST` / `NETEASE_PORT` | `127.0.0.1` / `22888` | API 服务监听地址与端口 |
| `NETEASE_WIDTH` / `NETEASE_HEIGHT` | `1280` / `720` | 请求的云游戏分辨率 |
| `NETEASE_WEBUI_DIR` | `webui/` | 前端静态资源目录 |
| `NETEASE_MAA_SETTINGS` | `maa_settings.json` | 一键长草设置文件路径 |
| `MAA_AUTO_PULL` | `1` | 容器自动拉取 MAA：`1` 缺失时拉取 / `0` 关闭 / `force` 强制更新 |
| `MAA_RESOURCE_MIRROR` | 空 | MAA 下载镜像前缀（如 `https://ghfast.top/`）；留空时直连优先、失败自动切内置镜像 |
| `MAA_RESOURCE_URL` | 空 | 显式指定 MAA 压缩包地址（zip / tar.gz），跳过版本 API |
| `MAA_LIB_DIR` | Windows `%APPDATA%\loong\maa\data\lib`；Linux `./maa_data` | MaaCore 动态库目录（`MaaCore.dll` / `libMaaCore.so`） |
| `MAA_DATA_DIR` | Windows `%APPDATA%\loong\maa\data`；Linux `./maa_data` | MAA 数据目录（需含 `resource/`） |
| `MAA_USER_DIR` | `<MAA_DATA_DIR>/debug` | MaaCore 实例用户目录（必须已存在） |
| `FAKE_ADB_PATH` | Windows `fake_adb/adb.bat`；Linux `fake_adb/adb.sh` | 传给 MaaCore 的假 adb 路径 |
| `FAKE_ADB_BASE` | `http://127.0.0.1:22888` | 假 adb 转发的云游戏 HTTP 地址 |
| `FAKE_ADB_SERIAL` | `127.0.0.1:5555` | 伪设备序列号（与 `AsstConnect` 的 address 一致） |

---

## MAA 核心与资源准备

-   引擎从 `MAA_LIB_DIR` 加载动态库（Windows `MaaCore.dll` / Linux `libMaaCore.so`；初始化顺序严格对齐 maa-cli：定位库目录 → `AsstSetUserDir` → `AsstLoadResource` → `AsstCreate` → `AsstConnect`）。
-   路径不同时请通过环境变量覆盖，并先运行自检脚本（跨平台）确认可驱动：

```powershell
.\.venv\Scripts\python.exe scripts/verify_maacore.py
```

-   缺少核心或资源时可用脚本自动拉取官方 Linux 包（含 `libMaaCore.so` 与 `resource/`）：

```powershell
.\.venv\Scripts\python.exe scripts/fetch_maa_resource.py .\maa_data
```

-   仓库内的 `maa_pipeline/` 仅为占位示例 pipeline，无法用于真实任务；请使用官方 `resource`（MAA 发行版或 maa-cli 资源目录）。
-   一键长草执行日志自动保存到 `logs/maa_YYYYMMDD_HHMMSS.log`（含 MaaCore 回调与假 adb 命令记录）。

---

## 容器部署

适用于服务器 / NAS / 无图形界面的 Linux 环境：容器内运行**官方 Linux MaaCore**（`libMaaCore.so` + 官方 `resource`）与假 adb 桥，一键长草能力与 Windows 本机一致；`maa_bridge`（MaaFw 自定义控制器）保留作备选。

### 一键部署（推荐）

```bash
git clone https://github.com/shunianssy/maa-use-adb-wangyi-cloud_game_server.git \
  && cd maa-use-adb-wangyi-cloud_game_server \
  && docker compose up -d --build
```

PowerShell（不支持 `&&`，改用 `;` 分隔）：

```powershell
git clone https://github.com/shunianssy/maa-use-adb-wangyi-cloud_game_server.git; cd maa-use-adb-wangyi-cloud_game_server; docker compose up -d --build
```

该命令依次完成「拉取代码 → 构建镜像 → 启动容器（首次启动自动拉取 MAA 核心与资源）」，无需手工准备资源；完成后浏览器打开 `http://<服务器IP>:22888/ui` 即可使用。

### 更新部署

已有部署需要更新代码时，在项目目录执行：

```bash
git pull && docker compose up -d --build
```

PowerShell：

```powershell
git pull; docker compose up -d --build
```

-   **更新 MAA 核心与资源版本**：`MAA_AUTO_PULL=force docker compose up -d`（或先 `rm -rf ./maa_data` 再启动）。
-   **仅改环境变量 / compose 配置**：`docker compose up -d`，无需重新构建镜像。
-   **排查提示**：若日志出现 `ModuleNotFoundError`（如 `No module named 'maa_settings'`）或 `[entrypoint] starting server.py ...` 这类旧格式日志，说明容器仍在运行旧镜像，执行上面的 `git pull && docker compose up -d --build` 重新构建即可。

### MAA 自动拉取说明

-   **来源**：官方版本 API（`https://api.maa.plus/MaaAssistantArknights/api/version/stable.json`）→ 资产 `MAA-vX.Y.Z-linux-x86_64.tar.gz`；解压后规范化为 `./maa_data/libMaaCore.so` 与 `./maa_data/resource/`。
-   **通道**：直连 GitHub 优先，失败自动切换内置镜像（ghfast.top / gh-proxy.com / ghproxy.net）；也可显式指定 `MAA_RESOURCE_MIRROR`。
-   **幂等**：`./maa_data` 已就绪时跳过下载，容器重启不会重复拉取。
-   **开关**：`MAA_AUTO_PULL=1`（默认，缺失时拉取）/ `0`（关闭，自行准备）/ `force`（强制更新到最新版）。
-   **预拉取（可选，需 Python 3.10+）**：`python scripts/fetch_maa_resource.py ./maa_data`；容器启动时检测到已就绪会自动跳过。
-   **失败不阻断**：拉取失败仅告警，网页控制台与手动操作仍可用，修复后重启容器即可恢复一键长草。

### 方式二：手动 docker build / docker run

```bash
# 1) 构建镜像
docker build -t netease-cloud-game-maa .

# 2) 启动(MaaCore 模式: 首次启动自动拉取 MAA 到容器内 /app/maa_data)
docker run -d --name netease-maa \
  -p 22888:22888 \
  -e NETEASE_HOST=0.0.0.0 \
  -e NETEASE_PORT=22888 \
  -e NETEASE_TOKEN=你的token \
  -v "$PWD/maa_data:/app/maa_data" \
  netease-cloud-game-maa

# 3) 备选: 桥接模式(ENTRYPOINT 后带参数即启用, 前台运行 maa_bridge, 任务结束后容器退出)
docker run --rm \
  -p 22888:22888 \
  -e NETEASE_HOST=0.0.0.0 \
  -v "$PWD/maa_fw_resource:/app/resource:ro" \
  netease-cloud-game-maa --resource /app/resource --task Main
```

桥接模式参数（由 `entrypoint.sh` 透传给 `maa_bridge`）：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:22888` | 容器内云游戏 HTTP 服务地址 |
| `--resource` | 无 | MaaFramework 资源 bundle 目录（需含 `pipeline/`、`image/`） |
| `--task` | 无 | 任务名（需与 `--resource` 配合；缺省则驻留等待） |
| `--connect-timeout` | `180` | 等待云游戏连接就绪的超时秒数 |
| `--width` / `--height` | `1280` / `720` | 兜底分辨率 |
| `--log-level` | `INFO` | 日志级别 |

### 容器启动流程

`entrypoint.sh` 依次执行：注入 `NETEASE_TOKEN`（可选，不覆盖已有 token 文件）→ 检测/自动拉取 MAA 到 `MAA_DATA_DIR`（默认 `/app/maa_data`）→ 导出 `MAA_LIB_DIR` / `MAA_DATA_DIR` / `MAA_USER_DIR` / `FAKE_ADB_PATH`（`fake_adb/adb.sh`）与 `LD_LIBRARY_PATH` → 前台运行 `server.py`。带参数启动时切换为桥接模式：后台 `server.py` → 等待 `/info` 就绪（最多 60s）→ 前台 `maa_bridge`。

### 数据持久化（可选）

容器重建（如 `docker compose up -d --build`）会丢失写入容器内的文件，需要保留时追加卷挂载：

| 容器内路径 | 内容 | 挂载示例 |
| --- | --- | --- |
| `/app/maa_data` | MAA 核心与资源（compose 已默认挂载） | `-v ./maa_data:/app/maa_data` |
| `/app/token` | 登录 token | `-v ./data/token:/app/token` |
| `/app/maa_settings.json` | 一键长草设置 | `-v ./data/maa_settings.json:/app/maa_settings.json` |
| `/app/logs` | 任务日志 | `-v ./data/logs:/app/logs` |

### 访问与验证

1.  浏览器打开 `http://<服务器IP>:22888/ui`（根路径 `/` 同样直达控制台）。
2.  首次使用在「云游戏账号」卡片内登录（短信 / 密码），或提前通过 `NETEASE_TOKEN` 注入。
3.  点「启动云游戏」验证画面；`GET /info` 返回 `{"status":"ok", ...}` 即连接正常。
4.  验证 MaaCore 就绪：`docker compose exec netease-maa python scripts/verify_maacore.py`（应输出 `AsstLoadResource ... 资源加载成功`）。
5.  `docker compose logs -f` 可见 `[entrypoint] ensuring MAA ...`、`MAA ready` 与后续运行日志；一键长草日志同时落盘 `/app/logs/maa_*.log`。

### 常见问题（容器）

-   **首次启动停在 `ensuring MAA core & resource`**：正在下载约 220MB，等待即可；直连 GitHub 缓慢时脚本会自动切换镜像，也可设置 `MAA_RESOURCE_MIRROR` 后重启容器。
-   **拉取失败 / 一键长草提示 MaaCore 不可用**：确认 `./maa_data` 内含 `libMaaCore.so` 与 `resource/`；或 `MAA_AUTO_PULL=force docker compose up -d` 重新拉取。
-   **容器外访问不到控制台**：确认 `NETEASE_HOST=0.0.0.0` 且端口映射正确，可用 `docker compose ps` 查看端口绑定。
-   **日志报 `Cloud game 连接失败`**：token 缺失或失效，重新登录 / 注入 `NETEASE_TOKEN` 后重启容器；云游戏免费时长耗尽同样会导致建连失败。
-   **`server.py did not become ready within 60s`**：多为依赖初始化失败（如 aiortc / ffmpeg），请查看 `docker compose logs` 的完整输出。
-   **更新代码 / MAA 版本**：见上文「更新部署」（`git pull && docker compose up -d --build`）。

---

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

覆盖内容：一键长草参数构造与任务状态机、假 adb 命令翻译、设置读写与原子落盘、控制器契约、端口占用清理、MAA 自动拉取（版本 API 解析 / 镜像回退 / 断点续传 / 解压安装）、MaaCore 跨平台默认值等。

---

## 常见问题

-   **提示需要重新登录 / token 失效**：在网页控制台「云游戏账号」卡片重新登录（或用上文终端交互 / `NETEASE_TOKEN` 方式）。
-   **网页控制台登录失败**：确认手机号为中国大陆 11 位号码、验证码未过期；接口异常时控制台操作日志会给出具体原因（如 HTTP 4xx / 网络异常）。
-   **密码登录提示「当前登录已失效」**：网易云游戏的密码登录需要先有有效 token 才能拉取加密参数，此时请改用短信验证码登录一次；短信登录成功后再用密码登录即可。
-   **签到提示「当天已签到」**：属正常提示（当日奖励已领取，次日可再签）。
-   **连接失败、截图超时**：云游戏免费时长耗尽时云端会拒绝建连，请先在网易云游戏完成签到或充值后重试。
-   **端口被占用**：Windows 下启动时会自动结束占用 `22888` 端口的残留进程（最多重试 3 次）。
-   **浏览器显示「未连接」但终端已就绪**：后端通过 WebSocket 广播 + 每 2 秒轮询 `/maa/status` 双通道同步；若仍异常请强制刷新页面（Ctrl+Shift+R）。
-   **为什么不用 MaaFramework 跑官方任务**：官方 `resource` 是 MaaCore 格式（`algorithm`、`ClickSelf` 等），MaaFw 只认 `recognition`、`Click` 等 Fw 动作，直接加载会失败；因此容器部署自动拉取的是**官方 Linux MaaCore**，`maa_bridge` 仅作为自定义 Fw pipeline 的备选桥接。

---

## 致谢

-   内置 SDK（`sdk/` 目录）：提供网易云游戏 WebRTC / WebSocket 连接与每日签到能力。
-   [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)：MaaCore / MaaFramework 官方核心与框架。
-   [aiohttp](https://github.com/aio-libs/aiohttp)、[aiortc](https://github.com/aiortc/aiortc)：异步 HTTP 服务与 WebRTC 支持。

## License

AGPL-3.0