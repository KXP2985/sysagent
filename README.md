# SysAgent —— 跨平台系统运维智能体

一个可部署在 **Windows / Linux** 上的本地智能体服务：感知系统状态、按指令分析问题、
实时监听项目日志并在出问题时告警 + 给出解决建议；变更类操作限定在白名单内并可人工审批。

> 可行性分析与架构设计见 [`docs/DESIGN.md`](docs/DESIGN.md)（实事求是地说明了哪些能做、哪些有边界）。

## 功能一览

| 级别 | 功能 | 说明 |
|------|------|------|
| 基础 | 系统状态感知 | CPU / 内存 / 磁盘 / 网络 / Top 进程 / 运行时长，2s 实时推送 |
| 基础 | 日志实时监听 | 增量 tail（支持日志轮转），ERROR/WARN 正则命中即告警 |
| 基础 | 告警上报 | Web 控制台实时弹出 + 告警列表，可一键「让 AI 分析此告警」 |
| 基础 | 智能分析 | 对话中按指令取证（查状态、翻日志、列进程）后给出定位与解决建议 |
| 进阶 | 白名单操作 | 只能执行配置里列出的命令；`needs_approval` 的命令执行前人工批准 |
| 进阶 | 项目启停 | `start_project` / `stop_project`（只能管自己启动的进程，均需审批） |
| 进阶 | 记忆与学习 | 业务事实 / 故障经验 / 运维剧本三类长期记忆；排障后自动沉淀，回答时自动注入相关记忆 |
| 进阶 | 自我进化 | 缺命令、缺日志入口时可**提案新能力**（加武器），经你审批后热加载生效 |
| 进阶 | 定时汇报 | 每隔 N 分钟自动汇总时段告警/系统状态推送到对话（可选 LLM 观察、转发 QQ 群） |
| 进阶 | QQ 对话 | 对接 OneBot v11（NapCat/Lagrange 等），白名单 QQ 号/群实时对话，回「批准/拒绝」过审批 |
| 进阶 | 代码学习 | 扫描本地代码项目、提炼风格画像入记忆；可生成脚手架到沙箱（写入需审批） |

**安全模型（三级）**：只读查询直接执行 → 变更操作人工审批 → 白名单外命令/目录一律拒绝。
LLM 无法拼接任意命令，也无法读取白名单目录以外的文件；
**能力进化同样受约束**——只能提案「白名单命令 / 日志监听 / 运维剧本」三类，且前两类必须你批准。

## 快速开始

### 方式一：便携版 / 安装版（推荐给使用者）

已构建产物见 `dist/`：

- `dist/SysAgent-Installer.exe` —— **正式安装版**：向导安装、开始菜单/桌面快捷方式、
  可选开机自启、系统卸载入口；详见 `docs/安装版说明.md`
- `dist/SysAgent-Portable-win64.zip` —— **便携版**：解压双击 **`启动.bat`** 即用；
  数据在解压目录内，整个文件夹可拷走；详见 `docs/便携版说明.md`

重新构建（开发者）：

```bash
.venv\Scripts\pip install pyinstaller
.venv\Scripts\python scripts\build_portable.py    # 便携版
.venv\Scripts\python scripts\build_installer.py   # 安装版
```

### 方式二：源码项目模式（开发者，本机直接跑）

```bash
python3 -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m agentd

# Linux
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m agentd
```

两种方式互不干扰，各自维护自己的 `config.yaml` 与 `data\`。浏览器打开 **http://127.0.0.1:8765/** 即可使用。

### 配置要点（两种方式通用）

首次启动会自动创建 `config.yaml`（便携版）或先 `cp config.example.yaml config.yaml`（源码模式）。编辑要点：

- **LLM**（默认 **Ollama 本地模型**，零 API 费用、数据不出本机）：
  ```yaml
  llm:
    provider: ollama          # 默认
    ollama:
      base_url: "http://127.0.0.1:11434"
      model: "qwen2.5:7b"     # 需已 ollama pull qwen2.5:7b
  ```
  先启动 `ollama serve` 并拉取模型。也可换成 `qwen3:8b` 等支持 tools 的模型。
- 或使用 OpenAI 兼容服务（OpenAI / DeepSeek / 通义 / MiMo 等）：
  ```yaml
  llm:
    provider: openai_compatible
    openai_compatible:
      base_url: "https://api.deepseek.com/v1"
      api_key: "${DEEPSEEK_API_KEY}"   # 环境变量写法，密钥不进配置文件
      model: "deepseek-chat"
  ```
- **推荐用控制台切换/添加**：「设置 → 模型管理」支持添加多个云端模型（OpenAI 兼容）、
  连通性测试、一键「设为默认」并热生效（无需重启），配置存在 `data/models.json`；
  `config.yaml` 的 llm 段仅是首次启动的种子。
- **监控项目**：把 `monitor.projects` 换成你自己的项目日志路径与错误正则；
- **白名单**：按需增删 `whitelist.commands`（改系统状态的命令建议 `needs_approval: true`）。

### 试一试（自带演示项目）

配置里的 `demo` 项目会监听 `logs/demo.log`。在控制台「对话」里说：

> 启动 demo 项目

Agent 会弹出审批卡片，点「批准」后 `scripts/demo_app.py` 开始写日志，
每隔几秒模拟一次 ERROR/Traceback —— 告警会实时出现在控制台，点「让 AI 分析此告警」
即可让它结合日志上下文和系统状态给出定位与建议。

## 作为系统服务运行（可选）

**Linux（systemd）** `/etc/systemd/system/sysagent.service`：

```ini
[Unit]
Description=SysAgent
After=network.target

[Service]
WorkingDirectory=/opt/sysagent
ExecStart=/opt/sysagent/.venv/bin/python -m agentd
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now sysagent
```

**Windows**：用 [nssm](https://nssm.cc/) 注册为服务，或计划任务开机自启：

```powershell
nssm install SysAgent "C:\path\to\sysagent\.venv\Scripts\python.exe" "-m agentd"
nssm set SysAgent AppDirectory "C:\path\to\sysagent"
nssm start SysAgent
```

## 对话示例

- 「现在系统状态怎么样？」
- 「检查 demo 项目最近有没有报错，分析下原因」
- 「磁盘快满了，帮我看看哪些目录占空间大」（需要对应白名单命令）
- 「启动/停止 demo 项目」
- 「记住：订单服务部署在 ns=trade，端口 8080」→ 写入长期记忆，下次排障自动关联
- 「帮我加一个能查看内存的命令」→ 智能体提案新能力，你在控制台点「批准进化」后立即可用

工具调用过程在对话里可展开查看；变更操作与能力进化都会先弹审批卡片。

## 记忆与自我进化

| 概念 | 存储 | 谁写入 | 生效方式 |
|------|------|--------|----------|
| 业务事实 fact | `data/memory.jsonl` | 智能体 `remember` / 你说「记住：…」 | 自动注入相关记忆到提示词 |
| 故障经验 lesson | 同上 | 排障出根因后自动沉淀 | 同上，越用越准 |
| 运维剧本 playbook | 同上 | `propose_capability`（免审批） | 可检索复用的标准步骤 |
| 白名单命令 | `data/config.runtime.yaml` | 能力提案 + 你批准 | 热加载，立即可执行 |
| 日志监听项目 | 同上 | 能力提案 + 你批准 | 热加载，立即开始监听 |

要点：

1. **记忆是可审计的**：都在文本文件里，控制台「记忆」页可搜索/删除；
2. **进化是可控的**：提案不会自作主张生效，且只能扩「命令 / 监听 / 剧本」三类，
   不能加载任意代码、不能绕过白名单；
3. **每次进化会留痕**：批准后自动写一条「新增能力」事实，方便回顾能力是怎么长出来的。

## 定时汇报

`config.yaml → report`：

```yaml
report:
  enabled: true
  interval_minutes: 30   # 汇报周期
  llm_summary: false     # true 则附加 LLM 观察（按次计费）
```

汇报内容（规则汇总，零成本）：时段内 ERROR/WARN 分布与样例、CPU/内存快照与均值、
磁盘≥80% 预警、各项目新增行数。开 `llm_summary` 后附加 2-4 条智能体观察。
配置了 `qq.report_group_id` 时会同步转发到该群。

## QQ 机器人

前置：自建/本地运行 OneBot v11 兼容实现（如 NapCat、Lagrange），拿到 HTTP API 地址。

```yaml
qq:
  enabled: true
  api_base: "http://127.0.0.1:3000"
  access_token: "对应 OneBot 的 token"
  webhook_token: "自定义随机串"     # OneBot 把上报地址设为 .../qq/onebot?token=该串
  allowed_user_ids: [12345678]     # 只允许这些 QQ 号私聊
  allowed_group_ids: [87654321]    # 只允许这些群
  report_group_id: 87654321        # 定时汇报转发目标群
```

对话中回复「批准」「拒绝」可处理最近一条待审批操作；能力提案会提示到 Web 端审批。

⚠️ 安全与合规提醒（实事求是）：
- QQ 是远程控制入口，**allowed_user_ids 必须收紧**，webhook_token 随机且保密；
- 建议用官方 QQ 机器人渠道或自建 OneBot 服务；第三方逆向协议有封号与 ToS 风险，自行评估；
- 变更类操作仍然全部走审批，QQ 只是消息通道，不放宽权限。

## 代码学习（受限版）

```yaml
code:
  scan_dirs: ["D:/code"]       # 允许学习的代码根目录
  workspace_dir: "./workspace" # 脚手架输出沙箱
```

对话里可以：

- 「扫描一下 D:/code 下有哪些代码项目」→ `list_code_projects`
- 「分析 samples/tiny_webapp 的代码风格」→ `analyze_code_project`（提炼风格画像自动入记忆）
- 「按刚才的风格生成一个 hello 项目」→ `scaffold_project`（写 `workspace/`，**需审批**）

边界：只读 `scan_dirs`、只写 `workspace_dir`、路径含 `..` 一律拒绝、不执行生成的代码。
「风格七八成像、能否直接用需要你 review」——这是诚实预期，不承诺自动产出生产级项目。

## 项目结构

```
agent/
├── config.example.yaml     # 配置模板
├── requirements.txt
├── docs/DESIGN.md          # 架构与可行性分析
├── agentd/                 # 后端
│   ├── system.py           # 系统状态采集 (psutil)
│   ├── logmon.py           # 日志 tail + 规则告警
│   ├── llm.py              # OpenAI 兼容 / Ollama 双 Provider
│   ├── memory.py           # 长期记忆（事实/经验/剧本）
│   ├── capabilities.py     # 能力进化（提案→审批→热加载）
│   ├── reporting.py        # 定时汇报
│   ├── qqchannel.py        # QQ OneBot 通道
│   ├── codescan.py         # 代码学习（扫描/分析/沙箱脚手架）
│   ├── tools.py            # 工具注册 + 白名单 + 审批
│   ├── agent.py            # Agent 工具循环 + 记忆注入
│   └── server.py           # FastAPI + WebSocket
├── static/                 # Web 控制台（零依赖 HTML/JS/CSS）
├── scripts/                # demo_app.py / build_portable.py / 启动.bat
├── samples/tiny_webapp/    # 代码学习演示样例
├── data/                   # 运行时数据（记忆、runtime 能力，自动创建）
├── dist/                   # 便携版构建产物（SysAgent-Portable + zip）
└── tests/                  # 离线测试 / E2E（Mock LLM）
```

## 测试

```bash
.venv\Scripts\python tests\test_agent_offline.py   # Agent 循环 + 审批 + 白名单（无需 LLM）
.venv\Scripts\python tests\test_e2e_ws.py          # 全链路（内置 Mock LLM，无需 API key）
.venv\Scripts\python tests\test_new_features.py    # 定时汇报 / QQ 通道 / 代码学习
```

## 边界与说明（实事求是）

1. **「给出解决方案」是辅助建议**，不是保证正确：LLM 会基于日志/状态取证后回答，落地执行仍受白名单+审批约束。
2. **规则层负责及时**（毫秒级、零成本），**LLM 层负责语义分析**（有 2–10s 延迟、按 token 计费）。
3. `stop_project` 只能停止由本智能体启动的进程；外部启动的进程请配置白名单命令处理。
4. 未配置 LLM（Ollama 未启动或模型未拉取）时，状态监控、日志告警、审批、定时汇报全部正常，仅「智能分析」不可用。
5. 本地小模型的工具调用能力有差异：推荐 `qwen2.5:7b` 及以上（已实测多轮工具调用可用）；
6. 第一版刻意不做鼠标键盘/系统设置控制（风险高），相关能力预留为二期。
