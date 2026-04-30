# ClaudeIsland 设计文档

> 一个跨平台（Windows + macOS）的 Claude Code 会话聚合胶囊悬浮窗，视觉模仿 iPhone 灵动岛。
>
> 本文档由 Overview Design 与 Detail Design 两部分组成，按顺序阅读——Overview 提供完整上下文，Detail 给出实现细节。

---

# Part 1: Overview Design

## 1. 问题陈述

用户在本地并行跑多个 Claude Code 会话时，没有统一的"状态层"可以：

- 一眼看到当前有哪些 Claude 会话在跑、分别属于哪个项目
- 快速切换到指定会话所在的终端窗口
- 察觉某个会话已经完成工具调用、正在等待用户输入

现状痛点：
- 多个终端窗口散落在任务栏 / Dock 里，图标看不出哪个在跑 Claude
- 切换靠 Alt+Tab / Cmd+Tab 猜，或去任务栏逐个 hover 看标题
- Claude 在长任务（≥30s 工具调用）时，用户常切走做别的事，错过"可以继续输入"的时机；回来又忘了在哪个窗口

**本设计的定位**：跨窗口的**会话聚合与导航器**，不是新终端、不是 Claude Code 的替代。

## 2. 目标 & 非目标

### 目标（v1）
- **G1** 自动发现本机正在运行的 Claude Code 会话，覆盖 Windows Terminal / cmd / PowerShell / iTerm2 / Terminal.app / Warp 等宿主
- **G2** 屏幕顶部居中（或刘海下方）显示胶囊悬浮窗，收起态展示"当前活跃会话"摘要（项目名 + 状态点）
- **G3** 点击胶囊展开会话列表；点击列表项激活对应**宿主终端窗口**到前台
- **G4** Windows + macOS 双平台共用核心代码，仅各自的 platform 适配层不同
- **G5** 会话增减与状态变化（工作中 / 等待输入 / 空闲）实时反映到 UI
- **G6** 在展开态底部展示**今日 / 本周 / 本月**的 Claude Code 总 token 消耗与 USD 金额（基于本地 JSONL + 内置 pricing 表）

### 非目标（v1 明确不做）
- **N1** 不显示 Claude 对话内容或输出——只做导航，不做窗口内容展示
- **N2** 不做 Linux（X11 / Wayland 窗口激活权限模型差异大，单独一期）
- **N3** Windows Terminal 多 tab 场景下**不保证**精准聚焦到 Claude 所在 tab（只激活到宿主窗口；见 D5）
- **N4** 不做 Claude 会话启停（仍由用户通过终端创建）
- **N5** 不做历史会话回看（归属 `claude-JSONL-browser` 等工具）
- **N6** 不做 Windows 10 以下版本
- **N7** v1 不做全局快捷键召唤（留给 v2）
- **N8** 用量**只显示总额**，不按模型 / 项目 / 缓存类型拆分（留给 v2 详情面板）
- **N9** Pricing 表内置不做联网自动更新（留给 v2 或用户手动覆盖）
- **N10** 不做"预算告警 / 阈值通知"（留给 v2）

## 3. 架构概览

```
┌──────────────────────────────────────────────────────────────────┐
│                       UI 层 (PySide6)                             │
│  ┌─────────────────┐      ┌──────────────────────────────────┐   │
│  │ CapsuleWindow   │◄────►│ ExpandedListWindow                │   │
│  │ (收起态胶囊)     │ 动画 │  ┌──────────────────────────────┐ │   │
│  │  frameless      │      │  │ 会话列表                      │ │   │
│  │  always-on-top  │      │  ├──────────────────────────────┤ │   │
│  │                 │      │  │ UsageBar (今日/周/月 USD)     │ │   │
│  │                 │      │  └──────────────────────────────┘ │   │
│  └────────┬────────┘      └──────────────┬───────────────────┘   │
│           │  IslandController (DOT↔COLLAPSED↔EXPANDED)            │
└───────────┼──────────────────────────────┼───────────────────────┘
            │   QtBridge: 核心 Event[T] → Qt Signal (主线程派发)     │
            ▼                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                   核心层 (platform-agnostic)                      │
│                                                                  │
│  ┌────────────────────┐      ┌────────────────────────┐         │
│  │ SessionRegistry    │◄─────│ SessionDiscovery       │         │
│  │ sessions_changed   │ 事件 │  - ProcessScanner       │         │
│  └─────────┬──────────┘      │    (定时 psutil 扫描)   │         │
│            │                 └────────┬───────────────┘         │
│            │                          │ 查询 latest_activity     │
│            │                          ▼                          │
│  ┌────────────────────┐      ┌────────────────────────┐         │
│  │ UsageRegistry      │◄─────│ JsonlParser            │         │
│  │ + PricingTable     │ 写入 │  增量 parse JSONL        │         │
│  │ (SQLite 存储)      │      │  - 维护 byte offset      │         │
│  │ totals_changed     │      │  - 输出 usage 记录       │         │
│  └─────────┬──────────┘      │  - 维护 activity 索引    │         │
│            │                 └────────┬───────────────┘         │
│            │ activate(sid)            ▲                          │
│            ▼                          │ 文件事件                  │
│  ┌────────────────────┐      ┌────────┴───────────┐             │
│  │ Protocols:         │      │ FileWatcher        │             │
│  │  WindowActivator   │      │ (watchdog)         │             │
│  │  ProcessInspector  │      │ ~/.claude/projects │             │
│  │  TabAwareActivator │      └────────────────────┘             │
│  └─────────┬──────────┘                                          │
└────────────┼─────────────────────────────────────────────────────┘
             │ 依赖注入
        ┌────┴────┐
        ▼         ▼
┌──────────────┐ ┌──────────────┐
│ platform/    │ │ platform/    │
│  windows.py  │ │  macos.py    │
│              │ │              │
│ pywin32 +    │ │ pyobjc +     │
│ psutil       │ │ psutil       │
└──────────────┘ └──────────────┘
```

### 3.1 模块职责

**核心层组件**：
- `SessionRegistry` — 会话状态的内存真相源；变化时 `emits sessions_changed` Event
- `SessionDiscovery` — 进程通道的编排器；定时跑 `ProcessScanner`，查 `JsonlParser` 拿活动时间，写回 Registry
- `JsonlParser` — 文件通道的核心；增量解析 JSONL，**一次解析两用**：写 `UsageRegistry` + 维护 activity 索引（取代原 v1 设计中 SessionDiscovery 内部的 FileWatcher 子组件）
- `UsageRegistry` — SQLite 之上的薄封装，按时间窗口聚合 USD/token；不缓存整张表
- `PricingTable` — 加载内置 TOML 快照 + 用户覆盖，`cost_for(...)` 计算单条 record 金额
- `FileWatcher` — `watchdog` 的低层封装，只发文件变化事件
- Protocol 定义：`WindowActivator` / `ProcessInspector` / `TabAwareActivator` / `PermissionChecker`

**UI 层组件**：
- `CapsuleWindow` / `ExpandedListWindow` / `UsageBar` — 三个无边框置顶 Widget
- `IslandController` — UI 状态机（`DOT ↔ COLLAPSED ↔ EXPANDED`）；UI 层独有
- `QtBridge` — **唯一允许同时 import 核心和 PySide6 的地方**，把核心 Event 桥到 Qt Signal

**平台层组件**：
- `windows.py` / `macos.py` — 实现核心定义的 Protocol，封装 pywin32 / pyobjc

### 3.2 解耦原则

四条原则共同保证层次解耦，每条都对应一个**机器可验证的契约**（见 §3.5 的 import-linter）。

#### P1：依赖倒置（Dependency Inversion Principle）
> 高层不依赖低层，二者都依赖抽象。

- **实现**：核心层定义 Protocol（`WindowActivator` / `ProcessInspector` 等）声明 WHAT 需要；平台层实现这些 Protocol 决定 HOW；`main.py` 启动时注入。核心收到的是 Protocol 实例，永远不知道背后是 pywin32 还是 pyobjc。
- **验证**：`import-linter` 禁 `claude_island.core` import 任何 `pywin32` / `pyobjc-*` / `AppKit` / `Quartz` / `win32*` 模块

#### P2：稳定依赖方向（Stable Dependencies Principle）
> 依赖指向变化最少的方向。

变化频率由低到高：Core（领域逻辑） < Platform（OS API 偶尔升级） < UI（视觉迭代频繁）。所以依赖方向：
```
UI ─────► Core ◄───── Platform
       (Core 不主动指向任何外部模块)
```
- **实现**：核心模块只 import：stdlib + 跨平台 I/O 抽象（`watchdog`、`sqlite3`）+ 业务库（`transitions`）+ 自身代码。**不 import** PySide6 / pywin32 / pyobjc。
- **验证**：lint 黑名单同 P1 + 加 `PySide6` / `PyQt6`

#### P3：观察者代替直调（Observer over Direct Call）
> 核心**发事件**，不**调** UI——核心不知道 UI 存在。

- **实现**：核心层自带 `Event[T]`（30 行纯 Python，零三方依赖）。核心组件暴露 Event 字段：`registry.sessions_changed: Event[list[Session]]`。UI 在边界处用 `QtBridge` 把 Event 桥成 Qt Signal 并 marshal 到主线程。
- **验证**：核心模块禁 import `QObject` / `Signal`（被 P2 的 lint 覆盖）
- **why this matters**：解耦的真正含义不是"分文件夹"，而是"核心可以在没有 UI 的情况下运行"。装上 Event[T]，核心才能跑 CLI 模式 / 单元测试无需 QApplication / 将来换 UI 框架（QtQuick、Tauri shell）零改动。

#### P4：单向依赖、无环（Acyclic Dependencies Principle）
> 模块依赖图必须无环。

```
ui ──► core ──► (only stdlib + cross-platform libs)
         ▲
         │
      platform (只 import core 的 Protocol，不 import core 实体)
```
- **实现**：UI 依赖 Core（读数据 + 订阅事件 + 调命令）；Platform 只依赖 Core 中的 Protocol 模块（`core.platform_protocols`），不接触 `Session` / `UsageRecord` 等实体类
- **验证**：`import-linter` 的 `layers` 合约，反向 / 跨层 import 即红

### 3.3 跨层通信契约

| 通信类型 | 实现 | 发起方 | 消费方 | 备注 |
|---------|------|------|------|------|
| **核心 → UI**：状态变化 | `Event[T]` + `QtBridge` | 核心 emit | UI 订阅 | 跨线程经 QtBridge marshal |
| **核心 → Platform**：能力调用 | Protocol 方法 | 核心调 | Platform 实现 | 同步返回 |
| **UI → 核心**：用户动作 | 直接调 Controller / Registry 公开方法 | UI 调 | 核心同步处理 | 主线程内 |
| **Platform → 核心**：异常/降级 | 返回值 (`bool` / `None` / `Result`) | Platform 返回 | 核心判别 | **不**主动 push |

注意：**Platform 永远不主动 push 事件给核心**——只通过返回值告知。"异步推送"（如文件事件）由核心层自己用 `watchdog` 发起；watchdog 的事件被 `JsonlParser` 立即转化为核心 Event，跨线程边界由 QtBridge 处理（§3.4）。

### 3.4 线程模型

```
┌─────────────────────────────────────────────────────┐
│ 主线程 (Qt event loop)                              │
│  ─ UI 渲染                                           │
│  ─ ProcessScanner (QTimer 触发)                      │
│  ─ QtBridge 接收 forwarded Signal → 派发 slot        │
└──────────────▲──────────────────────────────────────┘
               │ Qt.QueuedConnection
               │ ◄── 跨线程的唯一闸口 ──
┌──────────────┴──────────────────────────────────────┐
│ Watchdog 工作线程                                    │
│  ─ FileWatcher 收 OS 文件事件                        │
│  ─ JsonlParser 解析                                   │
│  ─ UsageRegistry SQLite 写入                          │
│  ─ Event[T].emit() 在此线程发起                       │
└─────────────────────────────────────────────────────┘
```

**关键不变量**：
- 核心层**不 own 任何线程**——`Event.emit()` 是同步函数调用；线程归属由 caller 决定
- SQLite 在工作线程写：`sqlite3.connect(..., check_same_thread=False)`，靠 GIL + 串行单连接保证一致性
- **跨线程的唯一通道是 QtBridge**——UI 渲染从不在工作线程执行
- 这条规则的好处：要排查 UI 卡顿 / 数据竞争，只需要看 QtBridge 一个文件，不用全代码库追踪

### 3.5 用 import-linter 把契约机器化

四条原则全部表达为 `pyproject.toml` 中的**数据**——不是注释、不是 review checklist：

```toml
[tool.importlinter]
root_package = "claude_island"

[[tool.importlinter.contracts]]
name = "P1+P2: core 不 import UI 框架"
type = "forbidden"
source_modules = ["claude_island.core"]
forbidden_modules = ["PySide6", "PyQt6", "PyQt5"]

[[tool.importlinter.contracts]]
name = "P1+P2: core 不 import OS/平台 API"
type = "forbidden"
source_modules = ["claude_island.core"]
forbidden_modules = [
    "win32api", "win32gui", "win32con", "win32process",
    "AppKit", "Quartz", "Cocoa", "ApplicationServices",
    "pyobjc",
]

[[tool.importlinter.contracts]]
name = "P2: platform 不反向依赖 UI"
type = "forbidden"
source_modules = ["claude_island.platform"]
forbidden_modules = ["claude_island.ui", "PySide6"]

[[tool.importlinter.contracts]]
name = "P4: 层次单向无环"
type = "layers"
layers = ["claude_island.ui", "claude_island.core", "claude_island.platform"]
```

CI 跑 `lint-imports`；违反**即红**。架构腐烂在 PR 阶段就被卡住，不靠 review 撞运气。这也是**声明式**的最高境界——规则是数据，违反规则的诊断是机器的事。

## 4. 核心流程

### 4.1 流程 A：启动与会话发现

```
用户启动 ClaudeIsland
      │
      ▼
┌─────────────────┐        ┌──────────────────────┐
│ main.py         │───1───▶│ SessionDiscovery     │
│  启动 Qt 应用    │        │  .start()            │
└─────────────────┘        └──────────┬───────────┘
                                      │
                  ┌───────────────────┼───────────────────┐
                  │ 2a. 定时扫描       │ 2b. 启动文件监听   │
                  ▼                   ▼                   │
         ┌─────────────────┐  ┌──────────────────┐       │
         │ ProcessScanner   │  │ FileWatcher      │       │
         │ psutil 枚举进程  │  │ watchdog         │       │
         │  匹配 claude    │  │  ~/.claude/      │       │
         │  + 回溯父进程    │  │    projects/     │       │
         │  找到有窗口的宿主│  │                  │       │
         └────────┬─────────┘  └────────┬─────────┘       │
                  │ ProcessSnapshot      │ jsonl mtime     │
                  │ + ClaudeHost         │   changed       │
                  ▼                      ▼                 │
         ┌─────────────────────────────────────┐           │
         │ SessionRegistry.upsert(session)     │           │
         │  - 按 id = f"{cwd}:{pid}" 去重      │           │
         │  - 合并两路信息                      │           │
         │  - emits sessions_changed Signal    │           │
         └──────────────────┬──────────────────┘           │
                            │                              │
                            ▼                              │
         ┌─────────────────────────────────────┐           │
         │ UI 层订阅 Signal                     │           │
         │  - 更新胶囊内容                      │           │
         │  - 更新展开列表                      │           │
         └─────────────────────────────────────┘           │
                            ▲                              │
                            └──────3. 循环─────────────────┘
```

**双通道融合理由**：
- 进程通道是**窗口激活的唯一真相源**（PID、宿主 hwnd）
- 文件通道是**元数据增强**（上次活动时间、会话项目反解）
- 进程的 cwd ↔ 会话文件的 `projects/<hash>` 路径建立关联

### 4.2 流程 B：点击激活终端窗口

```
用户点击展开列表中的会话项
      │
      ▼
┌─────────────────────────┐
│ ExpandedListWindow       │
│  item_clicked(sid) signal│
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ IslandController         │
│  .on_session_clicked()   │
└──────────┬──────────────┘
           │ activate(session_id)
           ▼
┌─────────────────────────┐      没有窗口 ID
│ SessionRegistry.get(sid) │──────────┐（如 headless）
└──────────┬──────────────┘           │
           │ 有 window_id              ▼
           │                    ┌────────────────┐
           │                    │ 降级：toast 提示 │
           │                    │ "无法定位窗口"   │
           │                    └────────────────┘
           ▼
┌─────────────────────────────────────────┐
│ 平台层能力探测 (声明式 fallback 链)        │
│                                         │
│  platform 实现 TabAwareActivator?       │
│    ├─ 是 → activate_tab(hint)           │
│    │       └─ 失败 ↓                     │
│    └─ 否 ↓                               │
│                                         │
│  WindowActivator.activate(window_id)     │
└──────────┬──────────────────────────────┘
           │
  ┌────────┴────────┐
  ▼                 ▼
Windows 平台      macOS 平台
SetForeground    NSRunningApplication
Window(hwnd)     .activate(options:
+ AllowSet         activateIgnoring
ForegroundWindow   OtherApps)
 解锁
```

**为什么核心层经过平台层能力探测**：核心层**声明**"我要激活这个 session"，平台层**决定**用哪种机制。这是 D5 设计的关键，把 tab 级聚焦的"何时能做"下沉到平台层判断，核心层和 UI 层完全无感。

## 5. 关键决策 & 备选方案

### D1：UI 框架选 PySide6（LGPL）

**选择理由**：
- LGPL 允许闭源分发和商用，PyQt6 是 GPL 需要购买商业证
- Qt 的 `QPropertyAnimation` 能做形变动画（宽度 / 圆角 / 阴影），是灵动岛体验的核心
- 跨平台无边框置顶窗口（`FramelessWindowHint | WindowStaysOnTopHint`）成熟稳定

**Alternatives Considered**：
- **PyQt6**：API 几乎一致，GPL 许可证让未来分发受限
- **Flet**（Flutter-in-Python）：视觉现代但窗口行为受限，打包 100MB+
- **pywebview + HTML/CSS**：CSS 动画最丝滑，但窗口形状 / 点击穿透 / 阴影控制弱，且要同时维护 Python + JS

### D2：双通道会话发现

**核心洞察**：单通道都不完整。
- 仅文件监听：能知道会话活跃过，但**没有 PID、没有窗口**——激活无从谈起
- 仅进程扫描：能拿到 PID 和窗口，但识别 Claude 靠进程名 / cmdline 启发式，且拿不到会话元数据

**选择**：进程扫描是**窗口激活的唯一真相源**；文件监听是**元数据增强**（项目名、活动时间）。两路在 `SessionRegistry` 按 `id = f"{cwd}:{pid}"` 融合。

**Alternatives Considered**：Claude Code hook 方案（`SessionStart` / `Stop` 写共享文件）——最准、最省 CPU，但要求用户每台机器都装 hook 配置，且 hook 拿不到"宿主终端窗口"（Claude 自己无 hwnd）。耦合代价大于收益。

### D3：平台抽象用 `typing.Protocol`

**选择理由**：鸭子类型 + 零运行时开销 + 测试时直接塞 mock；避免 ABC 继承体系的样板和钻石继承。

**Alternatives Considered**：`ABC + abstractmethod`——仪式感重，运行时检查对动态类型的 Python 意义不大。

### D4：胶囊位置——顶部居中，macOS 刘海机型贴刘海

- Windows / macOS 无刘海：距屏幕顶边 4px，居中；收起态 180×32px
- macOS 刘海机型（M1+ MacBook Pro）：检测刘海后贴合底边，视觉对齐 iPhone 灵动岛

**Alternatives Considered**：
- 永远贴顶边：放弃 macOS 刘海核心卖点
- 菜单栏 / 托盘插件：没法做"展开成大窗口"的动画，违背灵动岛体验

### D5：v1 激活到宿主窗口即止，tab 级聚焦走可选 Protocol 留给 v2

**根本约束**：Windows 下**没有任何 OS API 能回答"PID X 位于窗口 Y 的哪个 tab"**。Windows Terminal 单进程多 tab，Win32 层只可见一个 hwnd。

| 方案 | 激活到窗口 | tab 精准聚焦 | 复杂度 | 核心问题 |
|------|----------|-------------|--------|---------|
| **A. 只激活宿主窗口** | ✓ | ✗ | 低 | 当前 tab 可能不对 |
| B. UI Automation 爬 tab 标题匹配 | ✓ | △ 依赖标题 | 中 | 默认标题不含 cwd；调用 100-500ms 卡顿 |
| C. 模拟 Ctrl+Tab 轮询 | ✓ | △ 慢且打扰 | 高 | 体感差，标题撞名就废 |

**v1 选 A**，但在 Protocol 层留好 `TabAwareActivator` 扩展点：

```python
class WindowActivator(Protocol):          # v1 所有平台必实现
    def activate(self, window_id: int) -> bool: ...

class TabAwareActivator(Protocol):         # v2 增量；先在 iTerm2 落地
    def activate_tab(self, hint: TabHint) -> bool: ...
```

核心层按"有能力则用，无则降级"的声明式链路调度（见 4.2 流程图）。

**核心 tradeoff**：放弃 tab 级精度，换取（1）v1 按期交付，（2）跨平台行为一致。放弃的是"最后 5% 精度"；保住的是"两边都能用"。

### D6：用量数据自己 parse JSONL，不调 ccusage 子进程

**选择理由**：
- JSONL 格式稳定（社区已逆向，append-only event stream），自己解析可控
- 避免依赖 Node.js 子进程：~1-2s 启动延迟、与 ccusage CLI 输出契约耦合
- 我们本就在监听 `~/.claude/projects/` 做 mtime 探测，复用文件事件零增量成本

**Alternatives Considered**：

| 方案 | 工作量 | 用户依赖 | 否决理由 |
|------|-------|---------|---------|
| 自己 parse JSONL | ~400 行 + 测试 | 无 | （胜出） |
| `npx ccusage --json` 子进程 | ~80 行 | 需要 Node | 启动慢 1-2s；CLI 输出格式变更即坏 |
| 共享 ccusage 内部实现（移植 TS→Py） | 高 | 无 | 跨语言移植 + 跟踪上游迭代成本 |

### D7：Pricing 表内置 + 用户可覆盖

**选择理由**：Anthropic 调价频率约半年到一年一次（历史数据），release 时同步快照足够新鲜。用户配置可覆盖以应对预发模型 / 私有 endpoint / 临时调价。

**实现**：
- 内置 `claude_island/data/pricing_snapshot.toml`（带 `snapshot_date`），随包发布
- `config.toml` 的 `[pricing.overrides.<model>]` 优先级高于内置
- 遇到未知 model：金额显示 `--`，token 数仍显示，UI 角落标注 "未知模型: X"

**Alternatives Considered**：
- 启动时联网拉 [LiteLLM `model_prices.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)：永远最新，但 v1 不引入网络依赖；留给 v2 选项
- 嵌入 LiteLLM 数据快照：等价于自己内置 TOML，徒增间接

### D8：核心层用 `Event[T]` 解耦 Qt，UI 在 `QtBridge` 处架桥

**问题**：直觉做法是 `SessionRegistry(QObject)` + `Signal`——但这把 PySide6 拖进了核心层，违反 P2 稳定依赖原则；核心离不开 Qt 就不能跑 CLI / 不能脱 Qt 测试 / 不能换 UI 框架。

**选择**：核心层自带 30 行的 `Event[T]`（纯 Python，零依赖）；UI 在唯一允许的边界文件 `ui/qt_bridge.py` 把 Event 桥接成 Qt Signal，并用 `Qt.QueuedConnection` 把工作线程发起的事件 marshal 到主线程。

**Alternatives Considered**：

| 方案 | 工作量 | 核心可独立测试 | 否决理由 |
|------|-------|--------------|--------|
| `QObject + Signal` 进核心 | 0 | ✗ 需 QApplication | 核心被 Qt 绑死，违反 P2/P3 |
| `blinker` 三方观察者库 | 0 (但加依赖) | ✓ | 5 个事件不值得加三方依赖 |
| **自己 30 行 `Event[T]` + `QtBridge`** | ~80 行 | ✓ | （胜出） |
| Python `asyncio` 事件循环 | 高 | ✓ | 与 Qt 事件循环融合复杂，v1 不需要 |

**核心 tradeoff**：多写 80 行边界代码，换核心层（1）单元测试 5-10x 提速（不需 QApplication fixture）；（2）可独立发包 / 跑 CLI 模式；（3）将来换 UI 框架时核心零改动。

### 决策总表

| ID | 决策 | 关键 tradeoff |
|----|------|--------------|
| D1 | UI 框架 = PySide6 (LGPL) | 放弃 HTML/CSS 丝滑，换动画可控 + 分发自由 |
| D2 | 双通道会话发现 | 多一倍代码，换单点失败时的降级能力 |
| D3 | Protocol 而非 ABC | 失去编译期检查，换轻量 + 测试友好 |
| D4 | 顶部居中 / 贴刘海 | 放弃菜单栏集成，换灵动岛核心体验 |
| D5 | v1 不做 tab 聚焦，可选 Protocol 留门 | 放弃 tab 精度，换交付速度 + 平台一致性 |
| D6 | 自己 parse JSONL 而非调 ccusage | 多 ~400 行代码，换无 Node 依赖 + 启动快 |
| D7 | Pricing 内置快照，用户可覆盖 | 偶尔调价需要发版，换零网络依赖 |
| D8 | 核心 Event[T] + UI QtBridge 解耦 Qt | 多 ~80 行桥接，换核心可独立测试 / CLI / 换框架 |

## 6. 风险 & 未决问题

### 风险

**R1：Claude 进程识别的启发式脆弱**
- **影响**：漏检 → 会话列表不全；误检 → 混入假会话
- **缓解**：（a）默认规则表覆盖三种已知安装模式（`claude.exe` / `node @anthropic-ai/claude-code` / `npx` 包装）；（b）用户可自定义匹配规则；（c）用 cwd 反查 `~/.claude/projects/<hash>` 存在性做二次验证

**R2：macOS 辅助功能权限门槛**
- **影响**：无权限时无法激活别的 App 窗口，软件核心功能退化为只读
- **缓解**：（a）启动时显式检查 `AXIsProcessTrusted()`，缺失则胶囊变橙色 + 引导面板；（b）首次引导截图 + 一键跳转系统设置

**R3：`~/.claude/projects` 目录结构未来可能变**
- **影响**：文件通道失效；但进程通道仍工作，只损失活动时间元数据
- **缓解**：D2 双通道架构天然降级；CI 对已知 Claude Code 版本跑兼容性测试

**R4：高刷屏下 PyQt 动画可能掉帧**
- **影响**：形变不丝滑，视觉廉价感
- **缓解**：（a）动画限 240ms；（b）优先用 `QGraphicsOpacityEffect` + 整体缩放，减少 repaint；（c）如严重则 v2 切 QtQuick（GPU 加速）

**R5：Pricing 表过期导致 USD 金额不准**
- **影响**：Anthropic 调价后，UI 显示偏差（历史经验偏差幅度 < 30%，不影响数量级判断）
- **缓解**：（a）UsageBar 角落标注 "价格快照 YYYY-MM-DD"，让用户知情；（b）发现未知 model 时显示 `--` 而非乱算；（c）每次发版强制 review pricing 快照；（d）用户可在 `config.toml` 即时覆盖

**R6：JSONL 文件中途损坏 / 单行格式异常**
- **影响**：单文件本批数据丢失（直到下次新行追加重启解析）；不影响其他文件
- **缓解**：parser 按行处理，单行解析失败时 log + 跳过 + 推进 byte offset；不让一行坏掉的 JSON 卡住整个文件

### 已确认决策
- **Q1**（展开态优先级）：按最新 mtime
- **Q2**（无会话时）：折成小点保留存在感（24×24，hover 展开）
- **Q3**（全局快捷键）：v1 不做
- **Q4**（配置路径）：XDG 风格，用 `platformdirs` 自动适配（Windows 下落到 `%APPDATA%\ClaudeIsland\`，macOS 下 `~/Library/Application Support/ClaudeIsland/`）

---

# Part 2: Detail Design

## 1. 模块职责与接口

### 1.1 目录结构

```
claude_island/
├── core/                    # 核心层：纯 Python，禁 import PySide6 / OS API
│   ├── events.py            # Event[T] 观察者（30 行，零依赖）
│   ├── session.py           # Session / SessionStatus
│   ├── registry.py          # SessionRegistry（暴露 Event 字段，非 QObject）
│   ├── discovery.py         # SessionDiscovery orchestrator
│   ├── process_scanner.py   # ProcessScanner
│   ├── file_watcher.py      # FileWatcher (低层 watchdog 包装)
│   ├── jsonl_parser.py      # 增量 JSONL 解析 + activity 索引
│   ├── usage.py             # UsageRegistry + UsageRecord (SQLite)
│   ├── pricing.py           # PricingTable + USD 计算
│   ├── platform_protocols.py # WindowActivator / ProcessInspector 等 Protocol
│   ├── status.py            # 状态机 / 阈值计算
│   └── config.py            # 配置加载
├── data/                    # 随包资源
│   └── pricing_snapshot.toml # 内置 pricing 快照
├── platform/                # 平台层：thin adapters，import core.platform_protocols
│   ├── windows.py           # Windows 实现
│   ├── macos.py             # macOS 实现
│   └── factory.py           # 平台探测 + 依赖注入
├── ui/                      # UI 层：Qt widgets
│   ├── qt_bridge.py         # 唯一允许 import core+PySide6 的边界文件
│   ├── capsule.py           # CapsuleWindow
│   ├── expanded.py          # ExpandedListWindow
│   ├── usage_bar.py         # 展开态底部用量条
│   ├── controller.py        # IslandController (UI 状态机)
│   └── theme.py             # 声明式状态 → 视觉映射
├── main.py                  # 入口：组装依赖 + 装 QtBridge
└── tests/
    ├── core/                # 纯 pytest，无 QApplication
    ├── platform/
    └── ui/                  # pytest-qt
```

### 1.2 平台层 Protocol（`platform/base.py`）

全量定义（核心层仅依赖这些抽象）：

```python
from typing import Protocol
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    name: str
    cmdline: tuple[str, ...]
    cwd: Path | None
    parent_pid: int | None
    create_time: float          # epoch seconds

@dataclass(frozen=True)
class HostWindow:
    window_id: int              # Windows: HWND; macOS: CGWindowID
    title: str
    owner_pid: int              # 拥有该窗口的进程

class ProcessInspector(Protocol):
    """枚举进程 + 从进程回溯到宿主终端窗口"""
    def list_processes(self) -> list[ProcessSnapshot]: ...
    def find_host_window(self, pid: int) -> HostWindow | None: ...

class WindowActivator(Protocol):
    """激活窗口到前台。v1 必选。"""
    def activate(self, window_id: int) -> bool: ...

class TabAwareActivator(Protocol):
    """tab 级精准激活。v2 可选；v1 所有平台返回 None。"""
    def activate_tab(self, host_window_id: int, cwd_hint: Path) -> bool: ...

class PermissionChecker(Protocol):
    """macOS 辅助功能权限检查；Windows 实现永远返回 True"""
    def has_accessibility_permission(self) -> bool: ...
    def request_accessibility_permission(self) -> None: ...
```

### 1.3 核心层对外接口

```python
# core/events.py — 30 行的纯 Python 观察者；核心层独享，零三方依赖
T = TypeVar("T")

class Event(Generic[T]):
    """轻量观察者；不依赖 Qt，从任何线程 emit 都安全（线程归属由调用者决定）。"""
    def __init__(self) -> None:
        self._handlers: list[Callable[[T], None]] = []

    def subscribe(self, handler: Callable[[T], None]) -> Callable[[], None]:
        self._handlers.append(handler)
        return lambda: self._handlers.remove(handler)   # 返回反订阅 closure

    def emit(self, payload: T) -> None:
        for h in list(self._handlers):                  # 拷贝防迭代时被改
            h(payload)

# core/session.py
class SessionStatus(Enum):
    WORKING = "working"
    WAITING = "waiting"
    IDLE    = "idle"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class Session:
    id: str                              # f"{project_path}:{pid}"
    pid: int
    project_path: Path
    project_name: str                    # project_path.name
    host_window_id: int | None
    last_activity_at: datetime | None

# core/registry.py — 注意：不是 QObject，没有 Qt 依赖
class SessionRegistry:
    def __init__(self) -> None:
        self.sessions_changed: Event[list[Session]] = Event()
        self.permission_required: Event[None] = Event()

    def upsert(self, session: Session) -> None: ...
    def remove(self, session_id: str) -> None: ...
    def get(self, session_id: str) -> Session | None: ...
    def list_all(self) -> list[Session]: ...
    def prune_by_pid(self, alive_pids: set[int]) -> None: ...

# core/discovery.py
class SessionDiscovery(QObject):
    def __init__(self, *,
                 inspector: ProcessInspector,
                 registry: SessionRegistry,
                 jsonl_parser: JsonlParser,        # 用于查 latest_activity
                 config: DiscoveryConfig) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...

# core/jsonl_parser.py — 非 QObject
class JsonlParser:
    """增量 parse ~/.claude/projects/**/*.jsonl，输出 usage + activity 索引。"""
    def __init__(self, *,
                 watcher: FileWatcher,
                 usage_registry: UsageRegistry) -> None:
        self.activity_updated: Event[tuple[Path, datetime]] = Event()
        ...
    def start(self) -> None: ...                  # 启动时回放历史 + 订阅 watcher
    def latest_activity(self, project_path: Path) -> datetime | None: ...

# core/usage.py
@dataclass(frozen=True)
class UsageRecord:
    timestamp: datetime
    project_path: Path
    session_uuid: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float | None                        # None = 未知 model

@dataclass(frozen=True)
class UsageTotal:
    total_input_tokens: int
    total_output_tokens: int
    total_cache_creation_tokens: int
    total_cache_read_tokens: int
    total_cost_usd: float                         # 未知 model 的部分跳过累计
    has_unknown_model: bool                       # UI 用来显示提示
    record_count: int

    @property
    def total_tokens(self) -> int: ...

class UsageRegistry:
    """非 QObject；用核心层 Event[T] 通知用量变化。"""
    def __init__(self, *, db_path: Path,
                 pricing: PricingTable) -> None:
        self.totals_changed: Event[None] = Event()  # 节流：≤1Hz（详见 §5.4）
        ...
    def record(self, record: UsageRecord) -> None: ...
    def get_total(self, since: datetime,
                  until: datetime | None = None) -> UsageTotal: ...
    def get_today(self) -> UsageTotal: ...        # 本地时区 00:00 起
    def get_this_week(self) -> UsageTotal: ...    # ISO 周（周一起）
    def get_this_month(self) -> UsageTotal: ...   # 本月 1 号起

# core/pricing.py
@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float
    cache_write_multiplier: float = 1.25          # Anthropic 标准
    cache_read_multiplier: float = 0.1            # Anthropic 标准

class PricingTable:
    def __init__(self, *,
                 snapshot_date: date,
                 prices: dict[str, ModelPricing]) -> None: ...
    @classmethod
    def load(cls, snapshot_path: Path,
             user_overrides: dict[str, ModelPricing] | None = None) -> "PricingTable": ...
    def cost_for(self, *,
                 model: str,
                 input_tokens: int,
                 output_tokens: int,
                 cache_creation_tokens: int,
                 cache_read_tokens: int) -> float | None: ...   # None = 未知 model
    def is_known(self, model: str) -> bool: ...
    @property
    def snapshot_date(self) -> date: ...

# core/config.py
@dataclass(frozen=True)
class StatusThresholds:
    working_seconds: float = 5.0
    waiting_seconds: float = 30.0

@dataclass(frozen=True)
class DiscoveryConfig:
    scan_interval_ms: int = 2000
    claude_process_patterns: tuple[ProcessPattern, ...] = ...
    thresholds: StatusThresholds = StatusThresholds()

@dataclass(frozen=True)
class ProcessPattern:
    name_regex: str
    cmdline_regex: str | None = None
    cwd_must_exist_in_claude_projects: bool = True
```

### 1.4 UI 层对外接口

```python
# ui/controller.py
class IslandState(Enum):
    DOT       = "dot"          # 无会话时（Q2=C）
    COLLAPSED = "collapsed"    # 默认胶囊态
    EXPANDED  = "expanded"     # 展开列表

class IslandController(QObject):
    """UI 状态机；可以是 QObject（UI 层允许 Qt）。
    
    注意：不直接订阅 registry 的 Event——main.py 通过 QtBridge 接核心事件
    到下面这些 on_* 槽方法。
    """
    state_changed = Signal(IslandState)

    def __init__(self, *,
                 registry: SessionRegistry,           # 用于读数据（list_all / get）
                 usage_registry: UsageRegistry,       # 用于查 today/week/month
                 activator: WindowActivator,
                 tab_activator: TabAwareActivator | None,
                 config: UIConfig) -> None: ...

    # 核心事件 → 经 QtBridge 注入到这些槽（主线程）
    def on_sessions_updated(self, sessions: list[Session]) -> None: ...
    def on_permission_required(self, _: None) -> None: ...

    # UI → Controller (用户动作，主线程内)
    def on_capsule_clicked(self) -> None: ...
    def on_session_clicked(self, session_id: str) -> None: ...
    def on_dismiss_requested(self) -> None: ...
```

### 1.5 跨层桥接：QtBridge

`ui/qt_bridge.py` 是**唯一允许同时 import `claude_island.core` 和 `PySide6` 的文件**。约 30 行：

```python
# ui/qt_bridge.py
from typing import Callable, Generic, TypeVar
from PySide6.QtCore import QObject, Signal, Qt
from claude_island.core.events import Event

T = TypeVar("T")

class QtBridge(QObject, Generic[T]):
    """把核心 Event[T] 桥接到 Qt 主线程上的 callable。

    线程安全：从任何线程调 source.emit()；slot 始终在主线程执行
    （由 QueuedConnection 保证）。一个 bridge 对应一个核心 Event。
    """
    forwarded = Signal(object)

    def __init__(self, source: Event[T]) -> None:
        super().__init__()
        # forwarded.emit 是 Qt Signal 的 bound emit，跨线程调用安全
        source.subscribe(self.forwarded.emit)

    def connect_to(self, slot: Callable[[T], None]) -> None:
        self.forwarded.connect(slot, Qt.ConnectionType.QueuedConnection)
```

**为什么这一个文件是 Qt 解耦的关键**：
- 核心层 5 个 Event 只通过 QtBridge 进 UI；**不存在第二条**核心-UI 通信路径
- 跨线程 marshal 集中在这里——以后出现"线程问题 / UI 卡顿"，先怀疑这一个文件
- 想换 UI 框架（QtQuick / Tauri shell / web 前端）时，**只改这一个文件**——核心层零改动

main.py 装配示例见 §5.1。

## 2. 数据模型与 Schema

### 2.1 会话标识策略

```
id = f"{project_path}:{pid}"
```

**选择理由**：
- 唯一：同一 pid 不会同时出现在不同 project_path
- 进程生命周期内稳定：pid 不变则 id 不变，UI 列表动画不闪烁
- 天然处理"同项目多会话"：两个 Claude 在同 cwd 有不同 pid，会被视为两个 session（正确）

**拒绝的备选**：
- session-uuid（来自 jsonl 文件名）：需要文件通道先生效，冷启动阶段无 id
- hash(project_path) 单独：两个 Claude 在同 cwd 会冲突

### 2.2 配置文件（`config.toml`）

路径（由 `platformdirs` 决定）：
- Windows: `%APPDATA%\ClaudeIsland\config.toml`
- macOS: `~/Library/Application Support/ClaudeIsland/config.toml`

Schema：

```toml
[thresholds]
working_seconds = 5.0           # mtime 距今 < 此值 → WORKING
waiting_seconds = 30.0          # ≤ 此值 → WAITING；超过 → IDLE

[discovery]
scan_interval_ms = 2000

# 进程识别规则（声明式，按顺序匹配任一）
[[discovery.claude_process_patterns]]
name_regex = "^claude(\\.exe)?$"

[[discovery.claude_process_patterns]]
name_regex = "^node(\\.exe)?$"
cmdline_regex = "@anthropic-ai/claude-code"

[[discovery.claude_process_patterns]]
name_regex = "^npm(\\.cmd)?$"
cmdline_regex = "claude-code"

[ui]
always_on_top = true
capsule_width = 180
capsule_height = 32
dot_size = 24
animation_duration_ms = 240
stick_to_notch = true           # macOS 刘海机型贴刘海

# 状态 → 颜色的声明式映射
[ui.status_colors]
working = "#30D158"
waiting = "#FF9F0A"
idle    = "#8E8E93"
unknown = "#5E5E5E"

# Pricing 覆盖（可选）。优先级高于内置 pricing_snapshot.toml
# 例：用预发或私有 endpoint
# [pricing.overrides."claude-opus-4-8"]
# input_per_mtok        = 5.0
# output_per_mtok       = 25.0
# cache_write_multiplier = 1.25
# cache_read_multiplier  = 0.1
```

内置 `pricing_snapshot.toml` 示例（随包发布，发版同步）：

```toml
snapshot_date = "2026-04-30"

[prices."claude-opus-4-7"]
input_per_mtok  = 5.0
output_per_mtok = 25.0

[prices."claude-sonnet-4-6"]
input_per_mtok  = 3.0
output_per_mtok = 15.0

[prices."claude-haiku-4-5"]
input_per_mtok  = 1.0
output_per_mtok = 5.0
# cache_write_multiplier / cache_read_multiplier 未列时使用默认 1.25 / 0.1
```

**设计原则**：任何"规则 / 阈值 / 映射"都放配置，**代码不硬编码**。用户无需改代码即可调整行为；测试时注入不同配置即可覆盖边界。

### 2.3 Claude Code 会话文件结构（外部依赖）

```
~/.claude/projects/
└── <project-path-hash>/            # 路径编码：/Users/x/foo → -Users-x-foo
    ├── <session-uuid-1>.jsonl      # append-only event stream
    └── <session-uuid-2>.jsonl
```

我们只读 mtime，不解析 jsonl 内容。`project-path-hash` 的反解规则：
- Windows: `D--coding-projects-common-learn` ↔ `D:\coding projects\common-learn`
- macOS: `-Users-x-foo` ↔ `/Users/x/foo`

编码规则：分隔符 `/` / `\` / `:` 替换为 `-`，空格替换为 `-`。

### 2.4 JSONL 用量字段（外部依赖）

每条 `assistant` 消息形如：

```jsonc
{
  "type": "assistant",
  "timestamp": "2026-04-30T10:23:11.234Z",
  "message": {
    "id": "msg_abc...",
    "model": "claude-opus-4-7",
    "content": [...],
    "usage": {
      "input_tokens": 1234,
      "output_tokens": 567,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 8910
    }
  }
}
```

我们**只读** `timestamp` / `message.model` / `message.usage` 四个字段，其它一概忽略——降低对未来 schema 变化的敏感度。

### 2.5 SQLite Schema（用量持久化）

DB 路径（由 `platformdirs.user_cache_dir` 决定）：
- Windows: `%LOCALAPPDATA%\ClaudeIsland\Cache\usage.db`
- macOS: `~/Library/Caches/ClaudeIsland/usage.db`

```sql
CREATE TABLE usage_records (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                TEXT    NOT NULL,    -- ISO8601 UTC
    project_path             TEXT    NOT NULL,
    session_uuid             TEXT    NOT NULL,
    model                    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL,
    output_tokens            INTEGER NOT NULL,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cost_usd                 REAL                 -- NULL = 未知 model
);
CREATE INDEX idx_usage_timestamp ON usage_records(timestamp);

-- 解析进度：避免重启后重复解析
CREATE TABLE parse_offsets (
    file_path        TEXT PRIMARY KEY,
    byte_offset      INTEGER NOT NULL,
    last_parsed_at   TEXT    NOT NULL
);

-- 不变量：(session_uuid, message_id) 应天然唯一；唯一性由 JSONL append-only 语义保证
-- 之所以不在表上加 UNIQUE 约束：v1 不解析 message_id 字段（避免对 schema 多耦合）
```

## 3. 核心算法与状态机

### 3.1 会话状态机（SessionStatus）

状态由 `last_activity_at` 与当前时间的差值决定——是**纯函数**，不是传统有副作用的状态机：

```python
def compute_status(last_activity_at: datetime | None,
                   now: datetime,
                   thresholds: StatusThresholds) -> SessionStatus:
    if last_activity_at is None:
        return SessionStatus.UNKNOWN
    delta = (now - last_activity_at).total_seconds()
    if delta < thresholds.working_seconds:
        return SessionStatus.WORKING
    if delta < thresholds.waiting_seconds:
        return SessionStatus.WAITING
    return SessionStatus.IDLE
```

状态转换图（仅示意，实际通过周期性重算触发）：

```
        mtime 更新
    ┌─────────────────┐
    │                 │
    ▼                 │
┌─────────┐ 5s  ┌─────────┐ 30s  ┌──────┐
│ WORKING │────▶│ WAITING │─────▶│ IDLE │
└─────────┘     └─────────┘      └──────┘
    ▲                                │
    └────────────────────────────────┘
                mtime 更新

   无 jsonl → UNKNOWN（独立分支）
```

**关键不变量**：状态计算是**幂等**且**纯函数**——同样输入永远得同样输出。这让测试直接 `assert compute_status(...) == WORKING`，不需要复杂 fixture。

### 3.2 UI 状态机（IslandState）

```
┌─────────┐   session_count > 0    ┌──────────┐
│   DOT   │───────────────────────▶│COLLAPSED │
│ (无会话) │◀───────────────────────│          │
└─────────┘   session_count == 0   └────┬─────┘
                                        │ capsule_clicked
                                        ▼
                                  ┌──────────┐
                                  │ EXPANDED │
                                  └────┬─────┘
                                       │ dismiss / outside_click
                                       ▼
                                  (回到 COLLAPSED)
```

状态转换用 `transitions` 库声明式建模：

```python
from transitions import Machine

states = [IslandState.DOT, IslandState.COLLAPSED, IslandState.EXPANDED]
transitions = [
    {"trigger": "on_sessions_appeared",  "source": IslandState.DOT,       "dest": IslandState.COLLAPSED},
    {"trigger": "on_sessions_empty",     "source": "*",                   "dest": IslandState.DOT},
    {"trigger": "on_capsule_clicked",    "source": IslandState.COLLAPSED, "dest": IslandState.EXPANDED},
    {"trigger": "on_dismiss_requested",  "source": IslandState.EXPANDED,  "dest": IslandState.COLLAPSED},
]
```

**为什么用状态机而不是 if/else**：UI 过渡动画依赖"从什么状态到什么状态"，if/else 很快会变成难以维护的嵌套。状态机显式建模 transitions，新增状态（如 v2 的 `HUD` 状态）是加一行，不是改十处判断。

### 3.3 进程到宿主窗口回溯

```python
def find_host_window(claude_pid: int,
                     inspector: ProcessInspector,
                     max_depth: int = 8) -> HostWindow | None:
    """
    从 Claude 进程向父进程走，找到第一个拥有可见顶层窗口的祖先。
    典型深度：
      Windows: claude.exe → node.exe → pwsh.exe → conhost.exe → WindowsTerminal.exe（深度 4）
      macOS:   claude → node → zsh → iTerm2（深度 3）
    """
    current_pid = claude_pid
    for _ in range(max_depth):
        host = inspector.find_host_window(current_pid)
        if host is not None:
            return host
        parent = inspector.get_parent_pid(current_pid)
        if parent is None or parent == current_pid:
            return None
        current_pid = parent
    return None
```

边界：
- `max_depth = 8` 防止父进程链过长（比如 tmux 里跑 Claude）导致性能问题
- 循环检测：`parent == current_pid` 防止 init(pid=1) 自环

### 3.4 会话发现主循环

```python
class SessionDiscovery:
    def _scan_tick(self) -> None:
        """每 scan_interval_ms 触发一次"""
        processes = self._inspector.list_processes()
        claude_procs = [p for p in processes if self._is_claude(p)]

        alive_pids: set[int] = set()
        for proc in claude_procs:
            if proc.cwd is None:
                continue
            host = find_host_window(proc.pid, self._inspector)
            last_activity = self._file_watcher.last_mtime_for_project(proc.cwd)
            session = Session(
                id=f"{proc.cwd}:{proc.pid}",
                pid=proc.pid,
                project_path=proc.cwd,
                project_name=proc.cwd.name,
                host_window_id=host.window_id if host else None,
                last_activity_at=last_activity,
            )
            self._registry.upsert(session)
            alive_pids.add(proc.pid)

        # 移除已退出的 Claude 会话
        self._registry.prune_by_pid(alive_pids)

    def _is_claude(self, proc: ProcessSnapshot) -> bool:
        """按配置的正则表匹配（声明式）"""
        for pattern in self._config.claude_process_patterns:
            if not re.match(pattern.name_regex, proc.name):
                continue
            if pattern.cmdline_regex:
                joined = " ".join(proc.cmdline)
                if not re.search(pattern.cmdline_regex, joined):
                    continue
            if pattern.cwd_must_exist_in_claude_projects:
                if not self._has_project_dir(proc.cwd):
                    continue
            return True
        return False
```

**声明式体现**：`_is_claude` 不写死判定逻辑，而是按配置表循环；新增识别规则是加配置条目，不是改代码。

### 3.5 增量 JSONL 解析

JSONL 文件 append-only，按 byte offset 增量读取：

```python
def parse_incremental(self, file_path: Path) -> Iterator[UsageRecord]:
    """从 byte offset 处读到文件末尾，逐行 parse。
    只在成功解析完一行后推进 offset，避免读到半行。"""
    offset = self._offset_store.get(file_path) or 0
    with open(file_path, "rb") as f:
        f.seek(offset)
        while True:
            pos_before = f.tell()
            line = f.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                # 半行（写入未完成），下次再来
                break
            try:
                record = self._line_to_record(line, file_path)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # 单行坏：跳过 + 推进 offset，不卡住整个文件
                logger.warning("skip malformed line at %s:%d (%s)", file_path, pos_before, e)
                self._offset_store.set(file_path, f.tell())
                continue
            if record is not None:                  # None = 该行不是 assistant 消息
                yield record
            self._offset_store.set(file_path, f.tell())
```

**关键不变量**：
- offset 只在该 byte 之前的所有数据都已**最终处理完毕**（产出 record 或显式跳过）后推进
- `parse_incremental` 是幂等的——重复调用直到文件末尾稳定，不会重复发出 record

启动时回放：扫描 `~/.claude/projects/**/*.jsonl`，对每个文件调用 `parse_incremental`；之后切换为事件驱动（`watchdog` 通知文件变化）。

### 3.6 USD 计算公式

```python
def cost_for(model, input_tokens, output_tokens,
             cache_creation_tokens, cache_read_tokens) -> float | None:
    pricing = self._prices.get(model)
    if pricing is None:
        return None                                 # 未知 model → 标记 NULL
    cost = (
        input_tokens                  / 1_000_000 * pricing.input_per_mtok
      + output_tokens                 / 1_000_000 * pricing.output_per_mtok
      + cache_creation_tokens         / 1_000_000 * pricing.input_per_mtok * pricing.cache_write_multiplier
      + cache_read_tokens             / 1_000_000 * pricing.input_per_mtok * pricing.cache_read_multiplier
    )
    return round(cost, 6)                           # 保留到微分
```

**显式不变量**：
- 缓存写入按 `input_per_mtok × 1.25`，缓存读取按 `input_per_mtok × 0.1`（Anthropic 标准）
- 输出 token 不参与缓存折扣（Anthropic 不缓存 output）
- 未知 model 返回 `None`，由 UI 决定如何展示（v1：金额显示 `--`，token 数仍累计但 cost 不进总额）

## 4. 错误处理

**原则**：在系统边界（syscall / 文件 I/O / 第三方 API）处理失败；核心层不做"防御式" try/except。

| 边界点 | 失败形式 | 处理 |
|--------|----------|------|
| `psutil.process_iter()` | 整体失败（罕见） | 本轮 scan 跳过，log.error，下一轮重试 |
| `psutil.Process.cmdline()` / `.cwd()` | `NoSuchProcess` / `AccessDenied` per-process | 跳过该进程，不影响其他；不 log（量大） |
| macOS `CGWindowListCopyWindowInfo` | 无辅助功能权限 | 返回空列表；discovery 首次检测到空时触发 `permission_required` signal |
| `SetForegroundWindow` / `NSRunningApplication.activate` | 目标窗口已关闭 / 系统拒绝 | 返回 `False` → UI 层 toast "无法激活，窗口可能已关闭" |
| `watchdog` 文件事件 | IOError / 文件被删 | 忽略单次事件，watcher 自愈；严重错误（目录本身消失）触发降级 |
| 配置加载 | 缺失 / 格式错误 | 使用默认配置 + log.warning，UI 右下角一次性 toast 提示 |
| JSONL 单行解析 | `json.JSONDecodeError` / `KeyError` | 跳过该行 + 推进 offset + log.warning；不阻塞后续行 |
| JSONL 整文件读取 | `OSError`（文件被删 / 权限） | 跳过该文件，下次 watcher 事件重试 |
| Pricing 快照加载 | 缺失 / 格式错误 | log.error；fall back 到内置硬编码"应急 pricing"（Opus/Sonnet/Haiku 三档）；UI toast 提示 |
| `cost_for` 遇未知 model | 返回 `None` | UI 用 `--` 显示金额；token 数仍累计 |
| SQLite I/O | 写失败（磁盘满 / 锁） | 该条 record 丢弃 + log.error；用量数据非关键路径，不阻塞主流程 |

**类型层表达失败**：优先用返回类型，不用异常。

```python
# 好：失败是类型契约的一部分
def activate(self, window_id: int) -> bool: ...       # False = 失败
def find_host_window(self, pid: int) -> HostWindow | None: ...  # None = 失败

# 避免：调用方必须 try/except 才知道会失败
def activate(self, window_id: int) -> None:  # 失败抛 RuntimeError，调用方被迫防御
    ...
```

**异常场景**（极少数，仅限真正不可恢复的）：
- 核心启动时 `QApplication` 创建失败 → 不 catch，直接退出
- 配置文件写入失败（持久化用户设置）→ 抛异常到顶层 handler，toast 提示

## 5. 关键流程详细设计

### 5.1 启动序列（main.py 组装依赖）

```python
def main() -> int:
    app = QApplication(sys.argv)
    config = load_config()                                   # 步骤 1
    inspector, activator, tab_activator, perms = \
        platform.factory.create()                            # 步骤 2

    # ── 核心层（无 Qt 依赖）─────────────────────────
    pricing = PricingTable.load(
        snapshot_path=resource_path("data/pricing_snapshot.toml"),
        user_overrides=config.pricing_overrides,
    )
    usage_registry = UsageRegistry(
        db_path=user_cache_dir() / "usage.db",
        pricing=pricing,
    )
    file_watcher = FileWatcher(claude_dir=Path.home() / ".claude" / "projects")
    jsonl_parser = JsonlParser(
        watcher=file_watcher,
        usage_registry=usage_registry,
    )
    registry = SessionRegistry()
    discovery = SessionDiscovery(
        inspector=inspector,
        registry=registry,
        jsonl_parser=jsonl_parser,
        config=config.discovery,
    )

    # ── UI 层（Qt 依赖）──────────────────────────
    controller = IslandController(
        registry=registry,
        usage_registry=usage_registry,
        activator=activator,
        tab_activator=tab_activator,
        config=config.ui,
    )
    capsule = CapsuleWindow(controller)
    expanded = ExpandedListWindow(controller, registry, usage_registry)

    # ── 边界：QtBridge 装配（唯一耦合点）─────────────
    # 声明式接线表：(核心 Event, UI 槽) → 一个 bridge
    wiring: list[tuple[Event, Callable]] = [
        (registry.sessions_changed,      controller.on_sessions_updated),
        (registry.permission_required,   controller.on_permission_required),
        (usage_registry.totals_changed,  expanded.refresh_usage_bar),
    ]
    bridges = [QtBridge(event) for event, _ in wiring]
    for bridge, (_, slot) in zip(bridges, wiring):
        bridge.connect_to(slot)
    # bridges 列表持有引用，防止 QObject 被 GC

    # UI 内部连接（Qt-only，不跨层）
    controller.state_changed.connect(capsule.update_for_state)
    controller.state_changed.connect(expanded.update_for_state)

    # 平台权限检查
    if not perms.has_accessibility_permission():
        show_permission_onboarding(perms)

    file_watcher.start()
    jsonl_parser.start()                                     # 内含历史回放
    discovery.start()
    capsule.show()
    return app.exec()
```

**装配的语义清晰可读**：
- 第一段是核心层组装——只看这段就知道核心需要什么；可独立单测
- 第二段是 UI 层组装——只看这段就知道 UI 需要什么
- 第三段是边界 bridges——每条都是"哪个核心 Event 喂给哪个 UI 槽"，一目了然
- 任何"核心层接到 Qt 信号"的代码都不会出现在这里之外

**关键点**：所有跨层连接都通过 Qt Signal/Slot——UI 不主动拉状态，注册一次永久生效；符合"声明式订阅，what/how 分开"原则。

### 5.2 点击激活的完整调用链

```
ExpandedListWindow.item_clicked(sid)
  │
  └─► IslandController.on_session_clicked(sid)
        │
        ├─► session = self._registry.get(sid)
        │     └─ None → 无事发生（可能已过期）
        │
        ├─► window_id = session.host_window_id
        │     └─ None → controller.emit(toast_requested, "无法定位窗口")
        │
        ├─► if self._tab_activator is not None:
        │       ok = self._tab_activator.activate_tab(
        │                window_id, session.project_path
        │            )
        │       if ok: return
        │
        └─► ok = self._activator.activate(window_id)
              if not ok: controller.emit(toast_requested, "激活失败")
              else:     controller.trigger("on_dismiss_requested")  # 收起展开
```

### 5.3 FileWatcher → JsonlParser → 用量与活动

FileWatcher 是低层 watchdog 包装；解析与索引交给 JsonlParser：

```
~/.claude/projects/<hash>/<uuid>.jsonl 被追加
            │
            ▼
┌────────────────────────┐
│ FileWatcher (watchdog) │
│  emit file_modified    │
└──────────┬─────────────┘
           ▼
┌─────────────────────────────────────┐
│ JsonlParser.on_file_modified(path)  │
│  1. 取 byte offset                  │
│  2. parse_incremental → records    │
│  3. 推进 offset                      │
│  4. 取最新 record.timestamp          │
│     更新 activity_index             │
│  5. 对每个 record:                   │
│     usage_registry.record(record)   │
└──────┬──────────────────┬───────────┘
       │ activity_updated │ totals_changed
       ▼                  ▼
┌────────────────┐  ┌──────────────────┐
│SessionDiscovery│  │ ExpandedListWindow│
│ 下次 scan 取最新│  │  .refresh_        │
│ activity       │  │   usage_bar()     │
└────────────────┘  └──────────────────┘
```

JsonlParser 暴露两个东西：
- `latest_activity(project_path)` 给 SessionDiscovery 查会话状态
- 写入 `UsageRegistry` 给 UI 展示用量

**为什么解析做一次、两个用途**：避免 FileWatcher 触发两次解析；JSON 解析是这条链路里成本最高的一步（参见 §6）。

### 5.4 用量从 JSONL 到 UsageBar

```
新 JSONL 行追加
      │
      ▼
JsonlParser 解析 → UsageRecord(model, tokens, timestamp)
      │
      ▼
PricingTable.cost_for(record) → cost_usd | None
      │
      ▼
UsageRegistry.record(record)
      │ INSERT INTO usage_records (...)
      ▼
emit totals_changed (节流：≤ 1Hz，避免 SQL 风暴)
      │
      ▼
ExpandedListWindow.refresh_usage_bar()
      │
      ├─ usage_registry.get_today()        → SQL: WHERE timestamp >= today_start
      ├─ usage_registry.get_this_week()    → SQL: WHERE timestamp >= week_start (ISO)
      └─ usage_registry.get_this_month()   → SQL: WHERE timestamp >= month_start
      │
      ▼
UI 渲染:  "今日 $2.34  本周 $12.50  本月 $48.20"
          (角标: 价格快照 2026-04-30 · 未知模型: 0)
```

**节流逻辑**：`UsageRegistry.totals_changed` 用 QTimer 单次触发模式去抖（最多 1Hz）。理由：JsonlParser 一次回放可能产生数百条 record，每条都触发 UI 重绘没意义；攒一窗口一起刷。

## 6. 性能估算

### 6.1 目标

| 指标 | 目标 |
|------|------|
| scan 周期 | 2000 ms（可配置） |
| 单次 scan 耗时 | < 500 ms（含 psutil + 父进程回溯） |
| 会话数量 | ≤ 20（更多视为异常场景） |
| 展开动画 | 240 ms，≥ 60 fps（高刷屏 ≥ 120 fps） |

### 6.2 关键路径估算

**进程扫描**（典型机器 ~300 进程）：
- `psutil.process_iter(['pid', 'name', 'cmdline', 'cwd', 'ppid'])` — C 实现，~50-200 ms
- 正则过滤 Claude 进程 — O(n)，Python 侧 ~5 ms
- 每个 Claude 进程回溯找窗口 — 深度 ≤ 8，每层一次 syscall，~5-20 ms
- 20 个会话总回溯 ≤ 400 ms
- **总计：< 500 ms，留足余量**

**动画**：
- PySide `QPropertyAnimation` 单属性更新 → GPU 合成（`QGraphicsOpacityEffect` / transform）
- 形变动画属性数 ≤ 3（width、opacity、blur radius），帧预算 8.3 ms（120 fps），PySide 光栅化足够

**内存**：
- 单 Session 对象 ~200 字节；20 会话 ~4 KB
- FileWatcher / JsonlParser 索引 ~同量级
- UsageRegistry 不缓存全表，靠 SQL 聚合；常驻内存 ~1 MB（offset 表 + 统计 cache）
- **总占用 < 50 MB（主要是 Qt 运行时）**

### 6.4 JSONL 解析预算

**首次启动回放**（最坏场景）：
- 假设用户有 3 个月历史会话，~30 个项目，每项目 10 个 session 文件，每文件 5MB（含 ~5000 行）
- 总数据量 ~1.5 GB；逐行 JSON parse 约 2-5 万行/秒（CPython + json stdlib）
- 总耗时 ~30-60s，作为后台任务（QThread），UI 不阻塞
- 之后只增量解析新追加的尾部，单次 ms 级

**SQLite 写入**：
- 单条 INSERT 约 50-100 μs；首次回放 ~2 万条 record → 1-2s
- 批量提交（每 1000 条 commit 一次），减少 fsync 开销

**SQLite 查询**（`get_today` / `get_this_week` / `get_this_month`）：
- `idx_usage_timestamp` 索引 + SUM 聚合，全表 10 万条记录下 < 5ms
- UI 触发频率限 1Hz（§5.4 节流）→ 总查询负担可忽略

### 6.3 为什么 2s 扫描足够

Claude 会话变化的典型频率：
- 新开：几十秒到几小时
- 关闭：同上
- 状态变化（WORKING→WAITING）：秒到分钟级

2s 扫描对用户感知是"即时"，同时 CPU 占用可忽略（每秒平均 < 0.5% 单核）。如需更灵敏可配置到 500ms，但无实际意义。

## 7. 测试策略

### 7.1 测试什么

按模块、按优先级：

| 优先级 | 模块 | 测什么 |
|--------|------|--------|
| P0 | `core/session.py` / `status.py` | 状态计算是纯函数 → 全边界值覆盖 |
| P0 | `core/registry.py` | upsert 去重、prune 正确、信号触发 |
| P0 | `core/discovery.py` | 双通道融合、`_is_claude` 匹配、父进程回溯 |
| P0 | `core/jsonl_parser.py` | 增量 offset 推进、半行处理、坏行跳过、幂等 |
| P0 | `core/pricing.py` | USD 计算公式、未知 model 返回 None、用户覆盖优先级 |
| P0 | `core/usage.py` | 时间窗口聚合（today/week/month）、未知 model 不污染总额 |
| P1 | `platform/*` | 各 Protocol 实现能跑通真实 API（需平台特定） |
| P1 | `ui/controller.py` | 状态机转换正确 |
| P1 | `ui/usage_bar.py` | pricing 快照日期显示、未知 model 提示、节流 |
| P2 | `ui/*.py` | 快照渲染（视觉回归） |

### 7.2 怎么测

**Mock 边界：平台层**。核心层测试完全不碰 OS、**也不碰 Qt**——纯 pytest，无需 `QApplication` fixture。这正是 D8 解耦带来的关键收益：

```python
# tests/core/test_session_registry.py — 纯 pytest，无 QApplication
def test_upsert_emits_event():
    registry = SessionRegistry()
    received: list[list[Session]] = []
    registry.sessions_changed.subscribe(received.append)
    registry.upsert(make_session("foo", pid=100))
    registry.upsert(make_session("bar", pid=200))
    assert len(received) == 2
    assert received[-1][0].pid == 100
    assert received[-1][1].pid == 200

def test_unsubscribe_works():
    registry = SessionRegistry()
    received = []
    unsubscribe = registry.sessions_changed.subscribe(received.append)
    registry.upsert(make_session("a"))
    unsubscribe()
    registry.upsert(make_session("b"))
    assert len(received) == 1                       # 第二次 emit 后没收到
```

UI / QtBridge 测试用 `pytest-qt`（需 QApplication），与核心测试集分开跑——速度差 5-10x：

```python
class FakeInspector:
    """实现 ProcessInspector Protocol，内存数据驱动"""
    def __init__(self, processes: list[ProcessSnapshot],
                 windows: dict[int, HostWindow]) -> None:
        self._processes = processes
        self._windows = windows

    def list_processes(self) -> list[ProcessSnapshot]:
        return self._processes

    def find_host_window(self, pid: int) -> HostWindow | None:
        return self._windows.get(pid)

def test_discovery_picks_up_claude_with_wt_host():
    inspector = FakeInspector(
        processes=[
            ProcessSnapshot(pid=100, name="claude.exe", ..., cwd=Path("/proj/foo"), parent_pid=200),
            ProcessSnapshot(pid=200, name="pwsh.exe",   ..., parent_pid=300),
            ProcessSnapshot(pid=300, name="WindowsTerminal.exe", ..., parent_pid=None),
        ],
        windows={300: HostWindow(window_id=0xABCD, title="WT", owner_pid=300)},
    )
    registry = SessionRegistry()
    discovery = SessionDiscovery(inspector=inspector, registry=registry, config=DEFAULT_CONFIG)
    discovery._scan_tick()
    sessions = registry.list_all()
    assert len(sessions) == 1
    assert sessions[0].host_window_id == 0xABCD
```

**Usage 子系统测试要点**：

```python
# test_pricing.py
def test_cost_includes_cache_multipliers():
    pricing = PricingTable(snapshot_date=date(2026, 4, 30), prices={
        "claude-opus-4-7": ModelPricing(input_per_mtok=5.0, output_per_mtok=25.0),
    })
    cost = pricing.cost_for(
        model="claude-opus-4-7",
        input_tokens=1_000_000, output_tokens=0,
        cache_creation_tokens=1_000_000,            # 1.25x
        cache_read_tokens=10_000_000,               # 0.1x
    )
    assert cost == pytest.approx(5.0 + 5.0 * 1.25 + 5.0 * 0.1 * 10)

def test_unknown_model_returns_none():
    pricing = PricingTable(snapshot_date=date(2026, 4, 30), prices={})
    assert pricing.cost_for(model="claude-future-9", ...) is None

# test_jsonl_parser.py
def test_incremental_skips_partial_line(tmp_path):
    """半行写入时不应推进 offset，等下次完整再处理。"""
    f = tmp_path / "session.jsonl"
    f.write_bytes(b'{"type":"assistant",..."usage":{...}}\n{"type":"asst')  # 后半行未完成
    parser = JsonlParser(...)
    records = list(parser.parse_incremental(f))
    assert len(records) == 1                        # 只产出第一行
    # offset 停在第二行起始处
    assert parser._offset_store.get(f) == len(b'{"type":"assistant",..."usage":{...}}\n')

def test_malformed_line_advances_offset(tmp_path):
    """坏行不应卡住后续行。"""
    f = tmp_path / "session.jsonl"
    f.write_bytes(b'{"good":1}\n{not json}\n{"good":2}\n')
    parser = JsonlParser(...)
    records = list(parser.parse_incremental(f))
    assert len(records) == 2                        # 两条好的都收到了
```

**集成测试 fixture**：构造一个 mini `~/.claude/projects/` 目录（多文件、多 session、多 model），跑完整链路 → 断言 `get_today()` 总额正确、token 数正确、未知 model 不污染金额但 token 仍累计。

**平台层测试**：
- Windows：有 Claude 进程的集成测试（CI 里跑不了，本地 opt-in）
- macOS：同上 + 辅助功能权限场景（需手工验证）

**UI 测试**：
- `pytest-qt` + `QTest` 触发点击事件
- 状态机转换用 `transitions` 库原生测试助手
- 视觉快照用 `QWidget.grab()` 存 PNG，Git 存参考图

### 7.3 通过标准

- **单元测试**：覆盖率 ≥ 80%，核心模块 100%
- **集成测试**：在 Windows + macOS 各一台真机跑通"启动 → 发现 Claude → 激活终端"完整链路
- **回归**：双通道降级场景（文件通道挂 / 权限缺失）手动验证

## 8. 迁移与兼容

N/A（新项目，无既有系统需要迁移）。

---

## 附录 A：依赖清单

| 依赖 | 用途 | 许可证 |
|------|------|--------|
| PySide6 | UI 框架 | LGPL-3 |
| psutil | 跨平台进程枚举 | BSD |
| watchdog | 文件监听 | Apache-2 |
| platformdirs | XDG 风格路径 | MIT |
| transitions | 状态机 | MIT |
| pywin32 | Windows 专用：SetForegroundWindow / EnumWindows | PSF |
| pyobjc-framework-Cocoa | macOS 专用：AppKit / NSRunningApplication | MIT |
| pyobjc-framework-Quartz | macOS 专用：CGWindowList | MIT |
| pyobjc-framework-ApplicationServices | macOS 专用：AXIsProcessTrusted | MIT |

**Python stdlib 已足够**（无需新增三方依赖）：
- `sqlite3` — usage.db 存储
- `tomllib` (Python ≥ 3.11) — pricing 快照 / config 读取（写入用 `tomli-w` 如有需要）
- `json` — JSONL 行解析
- `importlib.resources` — 内置 pricing 快照定位

**开发期工具（dev dependencies，不进运行时）**：
| 工具 | 用途 | 许可证 |
|------|------|--------|
| import-linter | §3.5 跨层依赖契约的机器化强制（CI） | BSD |
| pytest | 测试框架 | MIT |
| pytest-qt | UI 测试 | MIT |

## 附录 B：术语

- **宿主窗口**：Claude 进程运行其中的终端模拟器窗口（Windows Terminal / iTerm2 等）
- **双通道**：进程扫描（ProcessScanner）+ 文件监听（FileWatcher → JsonlParser）两条独立的会话发现路径
- **project-path-hash**：Claude Code 将项目路径编码为单层目录名的规则（`/` → `-`）
- **MTok**：Million Tokens（百万 token），Anthropic 计价单位
- **Pricing 快照**：随包发布的 `pricing_snapshot.toml`，包含发版时刻的各模型单价 + `snapshot_date`
- **未知 model**：Pricing 表里没有的模型 ID（新发布或私有 endpoint）；UI 显示金额为 `--`，token 数仍累计
