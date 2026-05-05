# Recents Drawer Redesign — Overview Design

> 把当前的 "HISTORY" 抽屉重新设计成 Spotlight/Raycast 风格的双栏选择器：
> 左列简洁标题列表 + 右列富预览面板，键盘驱动主流程，鼠标兜底，
> 视觉风格与主面板保持完全一致。

## 1. Problem & Goals

### Problem

当前 HistoryDrawer 的体验问题：

1. **命名错位** — "HISTORY" 是名词，暗示"已经过去、不可挽回"。但功能是"恢复"——动词。名字与行为不匹配。
2. **平铺无锚** — 61 条全部顶在一起，没有任何视觉分块帮用户定位。
3. **字段挤在一行** — `cwd · 时间 · uuid8` 全挤在第二行，cwd 被截断、时间被淹没、uuid 大部分时候没用。
4. **图标语言贫弱** — 只有一个 `🛡` 盾牌，无法让用户一眼区分 session 的特征。
5. **交互单一** — 只能点击 Resume，没有 hover 多 action、没有右键、没有键盘流。
6. **last_prompt 不可见** — 这是用户识别 session 最有效的线索，但当前 UI 里完全没有展示。

### Goals

- **G1 命名清晰** — 标题改为 `RECENTS`，与左面板 `CLAUDE SESSIONS · N`（活跃）形成"过去 vs 现在"的时间轴对照
- **G2 视觉统一** — 复用主面板的 design tokens（颜色、字号、行高、圆角、字体），用户感觉是"同一个产品的两个表面"，不出现新的视觉语言
- **G3 键盘主路径** — 打开后焦点自动在搜索框，整个 "search → ↑↓ → Enter → Resume" 链路无需触摸鼠标
- **G4 鼠标兜底** — 任何键盘动作都有等价的鼠标动作；任何鼠标动作都不强制（避免误触）
- **G5 打开/关闭顺手** — 多入口打开（chip 点击、Ctrl+H、主面板键盘导航），多出口关闭（Esc、chip 再点、主面板关闭联动），失焦不自动关（用户可能切到 IDE 复制内容再回来）
- **G6 信息密度合理** — 左列只放标题（最多 32 字符），其余字段全部移到右侧 preview，避免单行拥挤

### Non-Goals

本期不做以下功能（结构上预留扩展点）：

- ❌ Star / 收藏置顶
- ❌ Rename / 自定义名字（需要本地 metadata store，另设计）
- ❌ Delete / 软删除
- ❌ 拖出到 file explorer / 拖到终端
- ❌ 时间分组（Today / Yesterday / Last 7 days）—— 方案 C 的特性，未来可叠加
- ❌ 重构数据层（`DormantSessionSource`、`Snapshotter`、`WorldSnapshot` 不动）
- ❌ 修复 hover 闪烁、空状态文案等独立 bug

---

## 2. Solution Design

### 2.1 命名

#### UI 文本

| 当前 | 改为 | 理由 |
|------|------|------|
| 标题 `HISTORY` | `RECENTS` | 与主面板 `CLAUDE SESSIONS · N` 形成时间轴延伸，不绑定具体动作（保留扩展空间） |
| 计数 `61` | `RECENTS · 61` | 与左面板 `CLAUDE SESSIONS · 7` 视觉同构 |
| Search placeholder `Search title / cwd / uuid` | `Search title, path, branch` | uuid 用户不会记，移出主搜索；搜索范围放大到 git_branch |

#### 代码层改名清单

UI 层（与新概念绑定，全部改名）：

| 当前 | 改为 |
|------|------|
| `claude_island/ui/history_drawer.py` | `claude_island/ui/recents_drawer.py` |
| `class HistoryDrawer` | `class RecentsDrawer` |
| `class _DormantRow` | `class _RecentRow` |
| `class _LaunchingRow` | 保留（仍准确描述"正在启动"） |
| `_DRAWER_WIDTH / _DRAWER_GAP / _ROW_GAP` | 保留（位置中性常量） |
| `tests/ui/test_history_drawer.py` | `tests/ui/test_recents_drawer.py` |
| `from .ui.history_drawer import HistoryDrawer` (in `__main__.py`) | `from .ui.recents_drawer import RecentsDrawer` |
| `expanded.set_history_toggle / update_history_count / _on_history_chip_clicked / _history_chip / _history_toggle` | `set_recents_toggle / update_recents_count / _on_recents_chip_clicked / _recents_chip / _recents_toggle` |
| `_history_subscription / _history_shortcut` | `_recents_subscription / _recents_shortcut` |
| 注释/docstring 中的 "History drawer" / "history chip" 等 | "Recents drawer" / "recents chip" |

数据/核心层（**不改名**，保留语义准确的技术词汇）：

| 保留原名 | 理由 |
|---------|------|
| `core/dormant_source.py` `DormantSessionSource` | "dormant" 描述数据源状态（jsonl-only, 无活进程），是准确的技术词汇 |
| `core/models.py` `DormantSession` | 同上，是数据模型概念 |
| `WorldSnapshot.dormant_sessions / launching_sessions` | API 字段不改（避免大范围 ripple） |
| `LaunchIntent / LaunchIntentRegistry` | 与 Recents 无关 |

**改名 ripple 评估**：UI 层改名涉及约 6 个文件、~30 处引用（含 import / 调用 / 注释 / 测试），全部由 IDE 重命名 + 测试覆盖兜底。一次性完成。

### 2.2 入口图标

#### 当前问题
当前 chip 用 `🗂`（CARD INDEX DIVIDERS）—— emoji 在 Windows 上彩色渲染，与主面板的低对比度灰色文本风格不协调；语义上 `🗂` 表"分类"，与 RECENTS（最近）也不匹配。

#### 候选与决策

| 候选 | 视觉特征 | 评价 |
|------|---------|------|
| `🗂 61`（当前） | 彩色 emoji | ❌ 跳脱、语义错位 |
| `🕘 61` / `⏱ 61` | 彩色 emoji | ❌ 仍跳脱 |
| `↻ 61` | 黑色 unicode 符号 (U+21BB) | 🔸 "刷新/恢复"语义对，但与"最近"略有偏差 |
| `◷ 61` | 黑色 unicode 符号 (U+25F7) | 🔸 钟面隐喻，但太抽象 |
| **`Recents · 61`** | 纯文字 | ✅ 与主面板 `CLAUDE SESSIONS · 7` 完全同构 |

#### 决策：纯文字 chip `Recents · 61`

**理由**：
- 主面板既有的 `CLAUDE SESSIONS · 7` 标题已经是"全大写 + 居中分隔点 + 数字"的语言；chip 沿用这套语言（小写 + 居中点 + 数字）形成"次级标题"的视觉层级
- 没有彩色 emoji，与产品的 Linear/Notion minimal 风格一致
- 视觉权重小（chip 的 padding 2px 6px，font-size 10px），不抢主面板焦点
- 数字直接展示数量，比图标语义更直接

视觉效果对比：

```
当前:                     新:
┌──────────────────┐      ┌────────────────────────┐
│  🗂 61            │  →  │  Recents · 61           │
└──────────────────┘      └────────────────────────┘
  彩色 emoji                  与左面板标题同构
```

样式调整（`expanded_window.py` 中 chip 现有样式仅改文字 + 略微加宽 padding）：

```
现有 background/border/border-radius/padding 全部保留；
仅文本从 "🗂 N" 改为 "Recents · N"
当 N == 0 时仍 hide()。
```

代码改动量：1 行（`f"\U0001f5c2 {n}"` → `f"Recents · {n}"`）+ chip 初始文本 `"🗂 0"` → `"Recents · 0"`。

### 2.3 整体布局

```
┌─ RECENTS · 61 ──────────────────────────────────  [Esc] ─┐
│  🔍 Search title, path, branch                           │   ← 搜索 bar (40px)
├──────────────────┬───────────────────────────────────────┤
│  ▶ resume-offl…  │  resume-offline-sessions              │
│    claude-isl…   │  ────────────────────────────────     │
│    ui-adaptiv…   │                                        │
│    你验证了吗?   │  📁 D:\coding projects\claude-island   │
│    按照全局…     │  🌿 master       · 1h ago             │
│    刚才做了…     │  💰 $59.21       · 23 turns · Opus    │
│    回滚刚才…     │  🛡  bypassPermissions                  │
│    add-zhipu…    │                                        │
│    search        │  Last prompt:                          │
│    Search the…   │    > "调研一下，开源社区如何做      │
│    MiniMax we…   │       History 的设计…"                │
│    ...           │                                        │
│                  │  uuid: f9d7aace…7854   [📋]            │
│                  │                                        │
│                  │  ┌────────────────────────────────┐   │
│                  │  │ ▶ Resume       (Enter)          │   │
│                  │  │ 📂 Open folder (Ctrl+O)         │   │
│                  │  │ 📋 Copy uuid   (Ctrl+C)         │   │
│                  │  └────────────────────────────────┘   │
│   [scrollbar]    │                                        │
└──────────────────┴───────────────────────────────────────┘
       ↑                      ↑
    ~160 px              ~240 px
                Total: 420 px
```

**关键尺寸**:
- 总宽 `_DRAWER_WIDTH = 420`（当前 360 → 420，多 60 px 容纳 preview）
- 左列固定 160 px（标题 + 滚动条）
- 右列 240 px（preview）
- 圆角 16 px、padding 14 px、行高 32 px（左列收紧，主面板 _ROW_HEIGHT=52 是因为它有两行；左列只有一行，所以收紧）
- 字号、颜色全部沿用 `_STYLE_NAME / _STYLE_AGE / _STYLE_COST_*` 等已有 tokens

### 2.4 视觉风格（与主面板一致的检查清单）

| 元素 | 复用的 token / 决策 |
|------|--------------------|
| 圆角 | 16 px（与 ExpandedWindow paintEvent 一致） |
| 背景 | `QColor(18, 18, 18, 240)`（与 ExpandedWindow 一致） |
| 行 BG | `_BG_SINGLE = #1e1e1e`，hover `_BG_HOVER_SINGLE = #2a2a2a`，pressed `_BG_PRESSED = #333333` |
| 选中行 BG | `_BG_HOVER_SINGLE`（与 hover 同色，但额外加左侧 2 px accent line） |
| 标题字体 | `_STYLE_TITLE`（"RECENTS · 61"）、`_STYLE_NAME`（行内标题） |
| 元数据字体 | `_STYLE_AGE`（cwd / 时间） |
| 成本字体 | `_STYLE_COST_DEFAULT` / `_STYLE_COST_HIGH` |
| 双栏分隔线 | 1 px `#2a2a2a`（与 SessionDetailPopup 的 `_divider()` 同色） |
| 字体 | `Segoe UI, sans-serif`（继承） |
| 滚动条 | 6 px 暗灰圆角（沿用 ExpandedWindow 的 `_session_scroll` 样式） |

**新增的视觉元素只有一个**：左列选中行的 2 px 左侧 accent line（颜色 `#6b7280`，即 `_STYLE_AGE` 灰）。这是双栏 selector UI 的必要标识，避免左右脱节。

### 2.4-bis 数据/UI 边界

> **核心约束**：core 只负责领域规则（数据从哪来、跨源怎么协调），UI 自己决定怎么呈现（排序、过滤、选中、展开等所有视图决策）。

#### 责任划分

```
┌─ core 的唯一职责（领域规则） ────────────────────────┐
│                                                       │
│  DormantSessionSource                                 │
│    └─ 过滤 subagent (agent-*)         [领域：什么是   │
│                                          可恢复的     │
│                                          session]    │
│                                                       │
│  Snapshotter._build_snapshot                          │
│    └─ live / launching reconcile     [领域：跨源     │
│                                          uuid 不重叠] │
│                                                       │
│  → 输出 snap.dormant_sessions: tuple[DormantSession]  │
│    顺序无承诺、字段保留原始值                          │
└───────────────────────────────────────────────────────┘
                           │
                           ▼
┌─ UI 的职责（呈现决策） ─────────────────────────────┐
│                                                       │
│  排序：按 last_activity 倒序                          │
│  过滤：搜索 query 匹配 name/cwd/branch/prompt/uuid    │
│  选中、展开、preview toggle 等所有视图状态            │
│                                                       │
│  以 module-level pure function 形式写，便于测试 + 复用│
└───────────────────────────────────────────────────────┘
```

#### 责任划分明细

| 加工 | 位置 | 性质 |
|------|------|------|
| Subagent 过滤 (`agent-*`) | `core/dormant_source.py` ✓ | 领域规则（数据源边界） |
| 与 live / launching 去重 | `core/snapshot.py: Snapshotter` ✓ | 领域规则（跨源 reconcile） |
| 按 `last_activity` 倒序 | UI 层 module-level fn | 呈现决策（默认值，未来可配） |
| 搜索匹配（字段、case） | UI 层 module-level fn | 呈现决策（默认值，未来可配） |
| 当前选中行 | UI state | 视图状态 (ephemeral) |
| 当前 search query | UI state | 视图状态 (ephemeral) |
| Preview 展开/折叠态 | UI state | 视图状态 (ephemeral) |

#### 判别原则（修订）

| 类别 | 归属 | 例子 |
|------|------|------|
| 领域规则：什么数据合法、跨源怎么协调 | core | "subagent 不算独立 session"、"live 已存在的 uuid 不重复出现" |
| 呈现决策：怎么排、怎么过滤、看不看得到、谁被选中 | UI | "默认按时间倒序"、"搜索匹配哪些字段"、"选中行 preview 显示什么" |

**为什么这条边界更合理**：
- 排序方式 / 搜索字段 未来很可能成为**用户偏好**（按时间 vs 按字母、搜不搜 prompt），把它们钉死在 core 限制了灵活性
- 当前没有第二个 UI surface 消费 dormant_sessions，core 暴露过滤函数是 YAGNI
- 真正的领域规则（subagent 是否算独立 session、live/launching uuid 去重）必须在 core，否则任何新 UI 都要重新发明

**关键工程约束**：UI 里的 filter / sort 写成 **module-level pure function**，不藏在 widget method 里。这样：
- 测试可以直接 import 函数（无需 pytest-qt）
- 阅读 widget 代码不被业务逻辑干扰
- 未来要搬出去也只需改 import

参考：VS Code Explorer (provider 给原始 file tree，sort 是 view 设置)、JetBrains Recent Projects (action 返回原始列表，UI 自己排序)、React + TanStack Query (fetcher 给数据，selector 在 component)。

### 2.5 架构图

```
┌────────────────────── HistoryDrawer (QWidget) ─────────────────────┐
│                                                                     │
│  ┌─ Header (QHBoxLayout) ─────────────────────────────────────┐   │
│  │  [Title "RECENTS · 61"]  [Esc hint]                         │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─ Search (QLineEdit) ──────────────────────────────────────┐    │
│  │  🔍 Search title, path, branch                             │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─ Body (QHBoxLayout, with 1px divider) ────────────────────┐    │
│  │ ┌─ ListColumn ────┐ │ ┌─ PreviewColumn ────────────────┐  │    │
│  │ │ QScrollArea     │ │ │ QScrollArea                    │  │    │
│  │ │  + RowList      │ │ │  + Header (title)              │  │    │
│  │ │   (compact 32px,│ │ │  + Meta block (cwd, branch,    │  │    │
│  │ │    title only,  │ │ │    time, cost, mode)            │  │    │
│  │ │    accent left  │ │ │  + LastPrompt (collapsible)    │  │    │
│  │ │    bar on       │ │ │  + UuidLine (small + copy btn) │  │    │
│  │ │    selected)    │ │ │  + ActionBar                   │  │    │
│  │ └─────────────────┘ │ └────────────────────────────────┘  │    │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─ Toast (hidden at rest) ──────────────────────────────────┐    │
│  └────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
        ↑                              ↑
        │                              │
   world.observable()           Selection state (内部)
   .pipe(distinct_until_changed)
   → render(snap)
   → 缓存 _last_snap, 重建 RowList
```

**关键决策标注**:
- `RowList` 和 `Preview` 是同一个 `WorldSnapshot` 的两个投影 —— **不引入第二份数据源**
- 选中状态只是 `RowList` 的内部 state（`_selected_uuid: str | None`），preview 由它驱动渲染
- `_last_snap` 缓存（已在上一次 fix 引入）继续承担"搜索/选中变化时无需重建快照"的职责

### 2.6 核心交互流（Goal G3 + G5）

#### Flow 1 — 打开 drawer

```
触发源（仅两个）：
   ① 用户点击主面板 `Recents · 61` chip
   ② 用户按 Ctrl+H
        │
        ▼
   RecentsDrawer.toggle()
        │
        ▼
   _reposition()  ← 复用现有的多显示器锚定逻辑
        │
        ▼
   show() + raise_()
        │
        ▼
   self._search.setFocus()  ← 关键：焦点直接落在搜索框
        │
        ▼
   if 列表非空 and _selected_uuid is None:
       _select_first_row()   ← 默认选中第一行，预览立即填充
```

#### Flow 2 — 搜索 + 切换 + Resume（键盘主路径）

```
焦点在搜索框 ─┬─ 用户输入字符 ─→ _search_query 更新 ─→ _render_rows(_last_snap)
              │                                              │
              │                                              ▼
              │                                       过滤后列表 + 自动选中第一行
              │
              ├─ ↓ 键 ─→ 焦点切到列表 + 选中下一行
              │
              ├─ ↑ 键 ─→（如果第一行）保持焦点在搜索框
              │
              ├─ Enter ─→ Resume 当前选中行
              │
              └─ Esc ─→ 关闭 drawer

焦点在列表  ─┬─ ↑↓ 键 ─→ 切换选中（preview 实时更新）
              │
              ├─ Enter ─→ Resume 当前选中行
              │
              ├─ Ctrl+O ─→ Open folder（cwd）
              │
              ├─ Ctrl+C ─→ Copy uuid
              │
              ├─ 任意可打印字符 ─→ 焦点回搜索框 + 字符插入
              │   （这是 Spotlight 的标志性体验：在结果上输入会回到搜索）
              │
              └─ Esc ─→ 关闭 drawer
```

#### Flow 3 — 鼠标交互（Goal G4 兜底）

```
单击行         ─→ 选中（更新 preview，不立即 Resume —— 防误触）
双击行         ─→ Resume
点击 [▶ Resume] ─→ Resume
右键行         ─→ context menu（v1：Open folder / Copy uuid；v2：Star / Rename / Delete）
hover 行       ─→ 行 BG 变 _BG_HOVER_SINGLE（与主面板一致）
```

#### Flow 4 — 关闭 drawer

```
触发源（任一）：
   ① 用户按 Esc
   ② 用户再次点击 `Recents · N` chip
   ③ 用户再次按 Ctrl+H
   ④ 主面板被 collapse 到 dot   ← 联动新增
   ⑤ 主面板被 hide              ← 联动新增
        │
        ▼
   self.hide()
        │
        ▼
   保留：_search_query (用户重开时是否记住？决策：不记住，清空)
   保留：_selected_uuid（决策：不记住，下次打开重新选第一行）
```

**显式不做**：失焦自动关闭。用户切到 IDE 复制 uuid 或 cwd 是合理动作，drawer 应该等他回来。

### 2.7 字段展示策略

#### 长内容折叠原则

**统一规则：所有可能超长的文本字段都用"折叠 + 展开链接"，与 SessionDetailPopup 的 prompt 折叠语言一致**。不使用 elide（`…` 截断）作为主策略——elide 让用户看不到全文，且与详情页风格不一致。

| 字段 | 默认折叠阈值 | 展开方式 | 备注 |
|------|------------|---------|------|
| 左列 row title | 32 字符 elide（行高约束） | hover ToolTip 显示完整 | 行内放不下展开链接，用 ToolTip 兜底；右侧 preview 也会立即给完整标题，所以 elide 无信息丢失 |
| 右侧 preview title | 60 字符 + `[展开]` | 点击 `[展开]` → 完整显示 | 与 SessionDetailPopup 的 `_prompt_expanded` 模式一致 |
| 右侧 cwd | 完整显示 | hover ToolTip | cwd 通常不长；超长（>60）才省略中段，hover 完整 |
| 右侧 last_prompt | 200 字符 + `[展开]` | 点击 `[展开]` → 完整 + 自动换行 | **复用 `SessionDetailPopup._prompt_expanded` 的状态机**：默认折叠、展开后变 `[收起]`、关闭/重开 drawer 时重置 |
| 右侧 uuid | 完整显示（小字） | — | 不需要折叠（统一长度 36 字符，小字一行容得下） |

**为什么不全用 hover**：
- hover 有 500ms+ 延迟，连续切选中行时体验差（preview 已经在更新）
- hover 不能被键盘触发（不友好键盘流，破坏 Goal G3）
- 展开/收起是显式动作，用户可控，与详情页一致

ToolTip 仅作为左列 elide 的兜底（左列没空间放展开链接）。

#### 字段总览

| 区域 | 字段 | 来源 | 展示规则 |
|------|------|------|---------|
| 左列行 | title | `name` → `last_prompt[:30]` → "Untitled" | 单行 elide 至 ~32 字符；选中行加左侧 2px accent；hover ToolTip 显示完整标题 |
| 右侧 header | title (full) | 同上 | 60 字符内直显；超过则折叠 + `[展开]` 链接 |
| 右侧 meta | cwd | `cwd` | `📁` + 完整路径；超长则中段省略 + hover ToolTip 完整 |
| 右侧 meta | branch + time | `git_branch` + `last_activity` | `🌿 master · 1h ago`（同行节省纵向空间） |
| 右侧 meta | cost + turns + model | `cost_usd` + `turn_count` + 主力 model | `💰 $59.21 · 23 turns · Opus` |
| 右侧 meta | permission_mode | `permission_mode` | 仅当 `!= "default"` 时显示一个 chip：`🛡 bypassPermissions` / `✏️ acceptEdits` / `📋 plan` |
| 右侧 prompt | last_prompt | `last_prompt` | 默认显示前 200 字符 + `[展开]` 链接（沿用 SessionDetailPopup 的 prompt 折叠组件） |
| 右侧 uuid | session_uuid | 完整 | 小字 + 旁边一个 `📋` 复制按钮；不在搜索框搜索（只显示） |
| 右侧 actions | — | — | `[▶ Resume]` 主按钮 + `[📂 Open folder]` `[📋 Copy uuid]` 次级 |

**图标体系**（仅本期用到的，不引入新视觉系统）：

```
权限模式:  🛡  bypass    ✏️  acceptEdits   📋  plan
元数据:    📁  cwd       🌿  branch        💰  cost
动作:      ▶   resume    📂  open          📋  copy
```

所有 emoji 都是已有渲染过的 BMP / SMP 字符，不依赖外部 icon font。

---

## 3. Research & Comparison

调研依据见前一轮对话的 web search 汇总（VS Code、JetBrains、ChatGPT/Claude.ai、Raycast、Warp、Linear/Notion）。

### 备选模式对比

| | A · Spotlight 双栏 (本方案) | B · VS Code Welcome 卡片 | C · ChatGPT 时间桶 |
|---|---|---|---|
| 键盘主路径 | ✅ 强 | 🔸 中 | ❌ 弱 |
| 信息密度 | 高（左简右富） | 中 | 中 |
| 学习成本 | 中 | 低 | 极低 |
| 适合规模 | 100+ | 10-50 | 50-200 |
| 主页 UI 兼容性 | ✅ 双栏可保持窄宽度（420px） | ❌ 卡片宽度需要 ≥320，影响多列 | 🔸 时间桶需要更多纵向空间 |
| 实现成本 | 中 | 中 | 高（需要时间桶逻辑 + 折叠状态） |

### 选 A 的理由

1. **键盘流最强** — 与你产品的整体哲学一致（Ctrl+H 全局快捷键、主面板已有键盘导航）
2. **last_prompt 全文显示** — 这是当前最缺失的识别线索，双栏 preview 是承载它的唯一合理布局
3. **窄宽度兼容** — 420 px 仍能 dock 在主面板右侧，不破坏既有的多显示器锚定逻辑
4. **扩展性** — 未来叠加 Star、时间桶、批量选择都能落进左列；preview 也能叠加更多 metadata。Selector 模式天然兼容增量演化。

### 风险

#### Type A — 选择此方案带来的取舍

- **没有时间分组** — 61 条压平的扫视成本仍存在。**缓解**：搜索框作为一等公民承担定位职责；未来可在左列叠加时间桶（Today / Yesterday / This week 折叠 header），架构无需改动。
- **没有项目分组** — common-learn 重复 cwd 的 session 仍会扁平展示。**缓解**：搜索 cwd 即时过滤；未来可叠加 `[All] [This project]` tab 切换。
- **窗口加宽 60 px** — 在窄屏（13 寸）+ 主面板靠右时可能挤出屏幕。**缓解**：现有的 `_reposition()` 已实现"右锚不下→左锚→居中"三级降级，加宽后仍能正确降级。

#### Type B — 方案本身的固有脆弱

##### B1. 左列对长标题不友好
elide 后 "调研一下，开源社区如何…" 与 "调研一下，开源社区给…" 难以区分。
**缓解**：
- 选中即触发右侧 preview，给出完整标题（即时反馈，无延迟）
- 左列 row hover 时 ToolTip 显示完整标题（鼠标用户）
- 残余风险：用户用键盘 ↑↓ 浏览时 ToolTip 不会触发，仅靠 preview 区分

##### B2. 双栏对屏幕宽度的底线要求

**业界主流方案**（调研结论）：

| 模式 | 代表产品 | 适用条件 |
|------|---------|---------|
| **Master-Detail 响应式折叠** | Microsoft UWP 标准、Oracle Alta UI、iOS Split View | 容器宽度可变（< 720 px 切换单栏） |
| **可拖拽分隔条** | VS Code、IDEA | 容器宽度大且需要用户控制比例 |
| **Toggle preview 显隐** | Mac Mail.app、Raycast | 容器宽度固定但用户偶尔需要更多列表空间 |
| **永远双栏，固定宽度** | Sublime Text Goto Anything、Alfred | 容器宽度极端小且单一场景 |

**本产品的实际场景特点**:
- drawer 是 `frameless + setFixedWidth`（用户不可手动调整宽度）
- 多显示器锚定逻辑已实现"右锚不下→左锚→居中"三级降级
- 触发条件：屏幕宽度 < 主面板 320 + drawer 420 + gap 6 = **746 px**
- 主流笔记本 1366×768 起步 → 实际触发概率 **接近零**

**决策：v1 采用「永远双栏 + Tab 键 toggle preview」**

```
默认状态:  [List] | [Preview]      ← 420 px (160 + 240)
Tab 切换:  [List]                  ← 220 px (仅左列)
再 Tab :   [List] | [Preview]      ← 420 px 还原
```

**理由**：
- 实际需求驱动：用户偶尔想看更多列表、或想给主面板让出空间，给一个用户主动控制的 toggle 即可
- 无需响应式监听屏幕变化逻辑（KISS）
- 与 Mail.app / Raycast 的"用户主动隐藏 preview"语言一致，学习成本零

**v2 才考虑响应式**：如果后续真有用户报告"屏幕装不下"，再升级为 master-detail 响应式（< 760 px 自动切单栏 + 选中后浮起 detail）。

**残余风险**：13" 笔记本 + 用户把主面板贴右边缘 + 加上 drawer，可能恰好 1280 px 屏幕装不下。降级路径：现有的 `_reposition()` 会切到左锚（drawer 在主面板左侧），主面板 320 + gap 6 + drawer 420 = 746 px，几乎所有屏幕都够。

##### B3. 失焦不关可能产生"野生"窗口
用户切到其他 app 回来后 drawer 还在。
**缓解**：
- 主面板 collapse / hide 时联动关闭（Flow 4 ④⑤）—— 覆盖 ~99% 的"我不要它了"场景
- 顶部右上角持续显示 `[Esc]` 提示，用户随时知道关闭方法
- 残余风险：用户长时间忘记 drawer 开着，遮挡屏幕。可接受——drawer 仅 420 px 宽，遮挡范围有限

##### B4. 右键 menu 在 v1 几乎为空
只有 Open folder / Copy uuid 两项，与右侧 action bar 重复。
**缓解**：v1 不实现右键 menu。单击 + 双击 + 主按钮 + 快捷键已覆盖所有 v1 操作。待 Star / Rename / Delete 进入 v2 时再加，届时右键 menu 才有独立价值。

##### B5. 长 last_prompt / 长标题 折叠后看不到全部
**缓解**：复用 SessionDetailPopup 的 `[展开] / [收起]` 链接组件（见 §2.7 字段策略）。展开状态在 drawer 关闭时重置——下次打开仍以折叠态呈现，避免"上次留下的展开"成为意外状态。

---

## 4. Open Questions

| # | 问题 | 决策时机 | 决策人 |
|---|------|----------|--------|
| 1 | 列表行高 32 px vs 40 px —— 32 更密集但 last_activity 可能被挤掉，40 更舒适但单屏少 2 行 | Detail Design 实测 | 实现方决定（默认 32） |
| 2 | 右侧 action bar 用三个等宽按钮还是一个主按钮 + 两个图标按钮？ | Detail Design | 实现方决定（默认三个等宽按钮，因为快捷键提示需要文字空间） |
| 3 | Tab 切换 preview 是否在 v1 实现？ | Detail Design | 用户决定（默认 v1 实现） |
| 4 | 拖动 / Star / Rename / Delete 列入 v2 还是更晚？ | v1 上线后看用户反馈 | 用户决定 |

---

# Detail Design

> **前置条件**：Overview Design 已确认。每节都追溯到上面的 Goal、Architecture 决策或 Risk 缓解项。

## D1. Module Responsibilities & Interfaces

### D1.1 内部组件结构

```
RecentsDrawer (QWidget, top-level frameless)
├─ _Header   .........  title + Esc 提示 + Tab toggle 提示
├─ _Search   .........  QLineEdit, 即时过滤
├─ _Body     .........  QHBoxLayout, 含 1 px 分隔线
│  ├─ _ListColumn  ...  QScrollArea + QVBoxLayout of rows
│  │  ├─ _RecentRow*    一个 dormant session 的紧凑行 (32 px)
│  │  └─ _LaunchingRow* 一个 launching intent 的行 (32 px)
│  └─ _PreviewColumn .  QScrollArea + 详细内容
│     ├─ _PreviewHeader   ...  title (折叠+[展开])
│     ├─ _PreviewMeta     ...  cwd / branch+time / cost+turns+model / mode chip
│     ├─ _PreviewPrompt   ...  last_prompt (折叠+[展开/收起])
│     ├─ _PreviewUuid     ...  uuid + 📋 copy
│     └─ _PreviewActions  ...  [▶ Resume] [📂 Open] [📋 Copy]
└─ _Toast    .........  错误反馈条 (existing)
```

只有 `_RecentRow` 和 `_LaunchingRow` 是独立类（被反复实例化）；其它区域是 `RecentsDrawer` 内的 layout/method。

### D1.2 接口契约

```
RecentsDrawer.__init__(*, expanded, dispatcher, launch_intent, on_wake)
  pre:   expanded 是已构造的 ExpandedWindow 实例
  side:  setFixedWidth(_DRAWER_WIDTH_FULL=420)
         初始 hidden、_search_query="" 、_selected_uuid=None
         _preview_visible=True 、_prompt_expanded=False
  契约: 不变（仅参数顺序保留），行为补充见下

RecentsDrawer.toggle() -> None
  side:  if visible: hide()
         else:       _reposition(); show(); raise_(); _focus_search()
  注意:  打开时若 _last_snap 已存在且列表非空，自动 _select_first_visible_row()

RecentsDrawer.render(snap: WorldSnapshot) -> None
  side:  _last_snap = snap
         _render_rows(snap)
         _reconcile_selection(snap)   ← 新增：选中行可能已不在过滤后的列表里
         _render_preview(snap, _selected_uuid)
         launching timeout toast 检测（保留现有逻辑）

RecentsDrawer.compute(snap) -> tuple
  staticmethod, dedup key.
  返回: (
    tuple of (uuid, last_activity, cost_usd, name, last_prompt, permission_mode)
      for d in dormant_sessions,
    tuple of (uuid, terminal_pid) for i in launching_sessions,
  )
  契约扩展:相比现有版本，新增 name / last_prompt / permission_mode 进 key，
         保证 ai_title 等字段更新会触发重渲染（修当前 dedup 漏字段问题）

RecentsDrawer._on_search_changed(text: str) -> None
  side:  _search_query = text
         if _last_snap is not None:
             # 调用 ui-layer pure function；widget 内不写匹配规则
             dormant = sort_by_recency(_last_snap.dormant_sessions)
             dormant = filter_by_query(dormant, text)
             _render_rows_from_filtered(dormant, _last_snap.launching_sessions)
             _reconcile_selection_against(dormant)
             _render_preview(_last_snap, _selected_uuid)
  contract: 不触发 snapshotter.wake (沿用上次 fix)
            过滤/排序规则在 ui/recents_filter 模块顶层，不在 widget method 里

RecentsDrawer._select_uuid(uuid: str | None) -> None
  side:  _selected_uuid = uuid
         _render_preview(_last_snap, uuid)
         滚动 _ListColumn 让选中行可见
         旧选中行 accent line 隐藏，新选中行 accent line 显示

RecentsDrawer._toggle_preview() -> None
  triggered by Tab key
  side:  _preview_visible = not _preview_visible
         setFixedWidth(_DRAWER_WIDTH_FULL if visible else _DRAWER_WIDTH_LIST_ONLY)
         _PreviewColumn.setVisible(_preview_visible)
         _reposition()  ← 宽度变了需要重新锚定

RecentsDrawer._on_prompt_expand_toggle() -> None
  side:  _prompt_expanded = not _prompt_expanded
         _render_preview(_last_snap, _selected_uuid)  ← 仅 prompt 段重建

_RecentRow.__init__(*, dormant, on_select, on_resume, ...)
  pre:   dormant.cwd 非 None，dormant.last_activity 非 None
         （由 DormantSessionSource 保证）
  side:  setFixedHeight(32)
         单行布局：[2px accent] [icon?] [title elided 32 chars] [···]
  signals/callbacks:
         on_select(uuid)  - 单击触发
         on_resume(uuid)  - 双击触发
  errors: 任意按钮 click handler 内部 try/except，失败 → on_toast

_RecentRow.set_selected(selected: bool) -> None
  side:  显示/隐藏左侧 2 px accent line
         BG 色 _BG_HOVER_SINGLE (selected) 或 _BG_SINGLE (not)
```

### D1.3 UI 层新增辅助模块（数据/UI 边界对齐）

#### `claude_island/ui/recents_filter.py`

```python
"""Pure filter + sort helpers for the Recents drawer.

Lives in ui (not core) because sorting key + matched fields are
*presentation decisions*, likely to become user preferences. They
operate on core types (DormantSession) but encode no domain rules.

Written as module-level pure functions — not methods on the widget —
so tests can exercise them without pytest-qt, and so the widget code
reads as pure layout/event wiring.
"""
from claude_island.core.models import DormantSession


def sort_by_recency(
    dormant: tuple[DormantSession, ...],
) -> list[DormantSession]:
    """Default Recents ordering: newest last_activity first.

    Returns a new list (input tuple unchanged). Stable for ties.
    """


def filter_by_query(
    dormant: tuple[DormantSession, ...] | list[DormantSession],
    query: str,
) -> list[DormantSession]:
    """Case-insensitive substring filter across the visible identity
    fields. Empty / whitespace-only `query` returns input unchanged
    (as a list copy).

    Matched fields: name, last_prompt, cwd, git_branch, session_uuid (full)

    Rationale:
      - uuid full match (not just first 8) — power users paste from logs
      - branch match — "find all work-on-feat-X sessions"
      - prompt match — most natural recall pattern
    """


def search_haystack(d: DormantSession) -> str:
    """The text that filter_by_query matches the query against.
    Exposed so tests + future ranking algorithms can reason about it
    without re-implementing the field decision."""
```

**契约约束**:
- 都是 pure function：相同输入永远相同输出，无副作用、不修改输入
- `sort_by_recency` 稳定排序（同 `last_activity` 时保留输入相对顺序）
- `filter_by_query` 保留输入顺序（不重新排序）
- 复杂度 O(N · L)；N=100, L≈500 实测 < 1ms

#### `Snapshotter._build_snapshot` 不变

`WorldSnapshot.dormant_sessions` 契约保持现状：经过 reconcile + 去重 subagent，**顺序无承诺**。UI 自己调 `sort_by_recency` 排序。

#### 在 RecentsDrawer 里的调用

```python
# 在 _render_rows 里：
from claude_island.ui.recents_filter import sort_by_recency, filter_by_query

dormant = sort_by_recency(snap.dormant_sessions)
if self._search_query:
    dormant = filter_by_query(dormant, self._search_query)
# 之后构建 row widgets
```

代码里完全见不到匹配规则、case 处理、字段选择——全部在 module-level 函数里。widget 只负责事件分发和 widget 构造。

### D1.4 复用 SessionDetailPopup 组件

`_PreviewPrompt` 区段直接抽取 `SessionDetailPopup._build_prompt_section` 中的折叠/展开链接控件为一个独立辅助类 `_CollapsiblePromptLabel`：

```
_CollapsiblePromptLabel(QFrame)
  __init__(*, full_text: str, collapse_at_chars: int = 200)
  signals: toggled  (collapsed -> expanded 或反之时发出)
  state:   _expanded: bool
  layout:  body label (text or text[:N]+'…') + [展开]/[收起] link
```

抽取位置：新建 `claude_island/ui/_collapsible.py`（或 `ui/widgets/`），同时 SessionDetailPopup 改为引用它，**消除两边的状态机重复实现**。

这是设计上的最小重构——共享视觉 token + 共享一个折叠组件，但不共享 inspector vs selector 的整体 widget。

## D2. Data Model / Schema

### D2.0 WorldSnapshot 字段契约（不变）

**`WorldSnapshot.dormant_sessions: tuple[DormantSession, ...]`**

| 契约 | 说明 |
|------|------|
| 已过滤 subagent (`agent-*` uuid) | `DormantSessionSource` 责任 |
| 已经过 live / launching reconcile（uuid 不重叠） | `Snapshotter._build_snapshot` 责任 |
| **顺序无承诺** | 消费方自行排序（UI 调 `sort_by_recency`） |

不强制 core 排序，是因为排序是呈现决策（见 §2.4-bis）。这避免了 core 强加视图偏好。

### D2.1 内部 state 字段

`RecentsDrawer` 实例字段（**唯一变化**）：

```python
# 既有（保留）
self._search_query: str = ""
self._last_snap: WorldSnapshot | None = None
self._prev_launching: set[str] = set()

# 新增
self._selected_uuid: str | None = None         # 当前选中行
self._preview_visible: bool = True             # Tab 切换 toggle
self._prompt_expanded: bool = False            # last_prompt 折叠态
self._title_expanded: bool = False             # preview title 折叠态
self._row_widgets: dict[str, _RecentRow] = {}  # uuid → row，便于 set_selected
```

线程安全：所有字段只在 Qt 主线程读写（`render` 由 `world.observable` 通过 `WorldMarshaler` queued connection 投递；用户输入在 Qt 主线程）。无锁。

### D2.2 数据流

```
WorldSnapshot.dormant_sessions   ┐
WorldSnapshot.launching_sessions ┼─→ _last_snap (cache)
                                 │
        _search_query ───────────┼─→ _filter(snap) ──┐
                                 │                    │
                                 │                    ├─→ _ListColumn (rows)
                                 │                    │
                                 ▼                    ▼
                          _selected_uuid ←─reconcile─ filtered list
                                 │
                                 ▼
                          _render_preview(snap, uuid) → _PreviewColumn
```

无新 schema、无新数据库表。

## D3. Core Algorithms / State Machines

### D3.1 选中状态机（核心）

```
                  open()
                    │
                    ▼
              ┌─ NoSelection ─────────────────┐
              │                                │
   _last_snap │ filtered_list 非空              │
   非空打开    │                                │
   或 search  │ ↓ _select_first_visible_row()  │
   清空后     │                                │
              ▼                                │
       ┌─ HasSelection ─┐                      │
       │ _selected_uuid │                      │
       │   in filtered  │                      │
       └────────────────┘                      │
              │                                │
              │ search/render → 选中行被过滤掉   │
              │ ↓ reconcile → fall back        │
              └────────────────────────────────┘
```

**Reconcile 规则**（`_reconcile_selection(snap)` 在每次 `render` / `_on_search_changed` 之后跑）：

```
filtered_uuids = {d.session_uuid for d in _filter(snap.dormant_sessions)}

if _selected_uuid in filtered_uuids:
    保持                                        # case 1: 选中仍在
elif filtered_uuids:
    _selected_uuid = first(filtered_uuids)     # case 2: 选中被过滤掉，回到首行
else:
    _selected_uuid = None                       # case 3: 列表为空，preview 显示占位
```

**不变量**：`_selected_uuid is None` ⇔ `_PreviewColumn` 显示空状态占位。

### D3.2 焦点状态机

```
                焦点初始
                   │
                   ▼
        ┌─ FocusOnSearch ─┐
        │                 │
        │    keyPress     │
        │  ┌──────────┐   │
        │  │ ↓        │───┼──→ FocusOnList (从选中行开始)
        │  │ Esc      │───┼──→ hide()
        │  │ Enter    │───┼──→ resume(_selected_uuid)
        │  │ printable│   │   (输入到 search)
        │  │ Tab      │───┼──→ _toggle_preview()
        │  └──────────┘   │
        └─────────────────┘
                   ▲                          ▲
                   │                          │
                   │ printable / search       │
                   │                          │
        ┌─ FocusOnList ───────────────────────┐
        │                                     │
        │    keyPress                         │
        │  ┌──────────┐                       │
        │  │ ↑↓       │ 切选中（preview 更新） │
        │  │ Enter    │ resume                │
        │  │ Esc      │ hide()                │
        │  │ Ctrl+C   │ copy uuid             │
        │  │ Ctrl+O   │ open folder           │
        │  │ Tab      │ _toggle_preview()     │
        │  │ printable│ → FocusOnSearch +     │
        │  │          │   插入字符            │
        │  └──────────┘                       │
        └─────────────────────────────────────┘
```

**实现要点**：
- "FocusOnList" 不是真正的"列表 widget 拿焦点"——`_RecentRow` 是 QPushButton，单独抓焦点会让搜索框失去高亮。改为**虚拟焦点**：`_search` 始终持有 Qt focus，"焦点在列表"由 `_keyboard_target: Literal["search","list"]` 内部状态表示，并在 `_search.eventFilter` 中分流 keyPress
- ↑↓ 的处理：当 `_keyboard_target=="search"` 且按 ↓，将 target 切到 list，并 `_select_uuid(filtered[0])`
- printable 字符的处理：当 `_keyboard_target=="list"` 且收到非控制字符，target 切回 search 并 `_search.setText(_search.text() + ch)` —— Spotlight 标志体验

### D3.3 Preview toggle 状态机

```
   _preview_visible=True   ──Tab──→  _preview_visible=False
   width = 420                       width = 220
   _PreviewColumn.show()             _PreviewColumn.hide()
                                          │
                                       reposition()
                                          ▲
   _preview_visible=False  ──Tab──→  _preview_visible=True
                                     reposition()
```

`_reposition()` 在 width 变化后必须重跑（避免 drawer 一边对齐主面板、另一边露出屏幕）。

### D3.4 Prompt 折叠状态机

复用 SessionDetailPopup 的状态机（已存在），通过 `_CollapsiblePromptLabel` 共享：

```
collapsed (default)  ──[展开]──→  expanded
                     ←─[收起]──   

drawer.hide()  ──→  全部 _CollapsiblePromptLabel 重置 collapsed
```

drawer 关闭时重置——下次打开是干净状态，不会留下"上次的展开痕迹"。这与 SessionDetailPopup 失去焦点关闭时重置语义一致。

## D4. Error Handling

| 调用点 | 失败模式 | 处理 | 用户感知 |
|-------|---------|------|---------|
| `dispatcher.adapters_with(LAUNCH)` | 抛异常 / 返回 `()` | 捕获 → 视为 `()` | "No terminal launcher available" toast |
| `dispatcher.launch(...)` | `LauncherSpawnError` | 捕获 + toast 错误信息 | "Failed to launch: <reason>" |
| `dispatcher.launch(...)` | 其它 Exception | 捕获 + log + toast 通用消息 | "Failed to launch (see logs)" |
| `_render_preview(snap, None)` | uuid 为 None | 显示占位 widget："Select a session to preview" | 灰色提示文字 |
| `_render_preview(snap, uuid)` | uuid 不在 dormant_sessions | reconcile 已经修正；若仍不在 → fall back to None | 同上占位 |
| `_filter(dormant)` | dormant 字段读取异常 | 只对单条捕获，跳过该条 | 该 session 不出现在列表（用户可能注意到一条少了） |
| Open folder | 平台命令失败 | toast 错误 | "Could not open folder: <reason>" |
| Copy uuid | QClipboard 失败（罕见） | 静默忽略 | 无反馈（用户会再点一次） |
| `_reposition()` | screen 信息读取异常 | fall back to current pos | drawer 可能位置不正常但仍显示 |

## D5. Implementation Flows

### D5.1 打开流程

```
RecentsDrawer.toggle()
 ├─ if isVisible(): hide(); return
 ├─ _reposition()                          [既有逻辑]
 ├─ show()
 ├─ raise_()
 ├─ _focus_search()
 │   └─ self._search.setFocus()
 │      self._keyboard_target = "search"
 └─ if _last_snap is not None:
      _reconcile_selection(_last_snap)
      _render_preview(_last_snap, _selected_uuid)
```

### D5.2 搜索 + 选择 + Resume

```
QLineEdit.textChanged → _on_search_changed("foo")
 ├─ _search_query = "foo"
 ├─ if _last_snap is None: return
 ├─ _render_rows(_last_snap)               [清空+重建左列 row widgets]
 │  └─ for d in _filter(_last_snap.dormant_sessions):
 │       row = _RecentRow(dormant=d, ...)
 │       _row_widgets[d.session_uuid] = row
 ├─ _reconcile_selection(_last_snap)       [选中可能被过滤掉]
 └─ _render_preview(_last_snap, _selected_uuid)


KeyEvent in _search ── ↓ ──→
 ├─ _keyboard_target = "list"
 ├─ if _selected_uuid is None and filtered非空:
 │     _select_uuid(filtered[0].session_uuid)
 │     _row_widgets[uuid].set_selected(True)
 └─ event accepted (不传给 search 的默认 ↓ behavior)


KeyEvent in _search ── Enter ──→
 └─ if _selected_uuid is not None:
      _on_resume(_selected_uuid)


KeyEvent on _search ── Tab ──→
 └─ _toggle_preview()


KeyEvent on _search ── Esc ──→
 └─ hide()


_on_resume(uuid)  [既有逻辑，沿用]
 ├─ candidates = dispatcher.adapters_with(LAUNCH)
 ├─ if not candidates: toast & return
 ├─ try: result = dispatcher.launch(...)
 │  except LauncherSpawnError: toast & return
 ├─ launch_intent.add(LaunchIntent(...))
 ├─ _on_wake()                              [snapshotter.wake]
 └─ disable row + label "⏳ Launching…"
```

### D5.3 主面板联动关闭

新增 wiring（在 `__main__.py`）：

```
expanded.signal_collapse_to_dot.connect(recents_drawer.hide)   # 新 signal
expanded.signal_hidden.connect(recents_drawer.hide)            # 新 signal
```

`ExpandedWindow` 需要新增两个 `Signal`，在状态变化时发出。这是对 `IslandController` 现有状态机的小扩展。

## D6. Performance Estimation

非定量 Goal，但定性要求：

| 操作 | 目标 | 实测预期 |
|-----|------|---------|
| 输入一个字符 → 列表更新 | < 16 ms (1 frame) | filter O(N=100) ≈ 0.1 ms；row 重建 ~3 ms；总 < 5 ms |
| ↑↓ 切选中 → preview 更新 | < 16 ms | _select_uuid + 2 个 row.set_selected ~0.5 ms；preview 重建 ~3 ms |
| Tab 切换 preview | < 50 ms | resize + reposition ~10 ms |
| 打开 drawer (列表 100 项) | < 100 ms | row 建 100 个 ≈ 30 ms；现有 expanded 同等量级也是这个数 |

无 SQLite / 网络调用在交互路径上——所有数据来自 `_last_snap`（in-memory）。

## D7. Testing Strategy

### 现有测试基线

- `tests/ui/test_history_drawer.py` 26 个 case（含本次新增的 8 个 search case 和 1 个 subagent 过滤 case）必须全部迁移到 `test_recents_drawer.py` 并通过。

### 新增 Test Cases

#### 选中状态机（Goal G3 + D3.1）

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| S1 | happy | drawer 打开，dormant 非空 | 默认选中 filtered[0]，preview 填充 | unit |
| S2 | happy | 用户搜索匹配当前选中 | 选中保持 | unit |
| S3 | edge | 用户搜索使当前选中被过滤掉 | 自动选 filtered[0] | unit |
| S4 | edge | 用户搜索结果为空 | _selected_uuid = None，preview 显示占位 | unit |
| S5 | edge | drawer 关闭重开 | _selected_uuid 重置为 None，再次按规则选首行 | unit |

#### 焦点 / 键盘（Goal G3 + D3.2）

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| K1 | happy | 焦点在 search，按 ↓ | _keyboard_target = "list"，preview 不变 | unit |
| K2 | happy | _target=list，按 ↑↓ | 选中切换，preview 更新 | unit |
| K3 | happy | _target=list，按 Enter | _on_resume 调用 | unit |
| K4 | happy | _target=list，按可打印字符 'a' | search.text 变 "a"，_target = "search" | unit |
| K5 | happy | _target=search，按 Esc | drawer.hide 调用 | unit |
| K6 | edge | _target=search 列表为空，按 ↓ | no-op（target 不切） | unit |

#### Preview Toggle（D3.3）

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| P1 | happy | preview 可见，按 Tab | width 220，_PreviewColumn.isVisible == False | unit |
| P2 | happy | preview 隐藏，按 Tab | width 420，_PreviewColumn.isVisible == True | unit |
| P3 | edge | toggle 后 _reposition 调用 | mock _reposition 被调用 | unit |

#### Prompt 折叠（D3.4 + 复用 SessionDetailPopup 组件）

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| C1 | happy | last_prompt > 200 字符 | 默认折叠，显示 [展开] | unit |
| C2 | happy | 点 [展开] | 显示完整文本，链接变 [收起] | unit |
| C3 | edge | drawer.hide() | _prompt_expanded 重置 False | unit |
| C4 | edge | 切到另一选中行 | _prompt_expanded 重置 False | unit |

#### UI helpers: recents_filter 纯函数（D1.3）

新建 `tests/ui/test_recents_filter.py`。**这些是 module-level pure function，不涉及 Qt**，所以放 tests/ui/ 仅出于"和被测代码同层"的组织习惯，本身不依赖 pytest-qt：

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| F1 | happy | filter_by_query: 命中 name | 返回包含该 session 的 list | unit |
| F2 | happy | filter_by_query: 命中 cwd | 返回包含该 session | unit |
| F3 | happy | filter_by_query: 命中 git_branch | 返回包含该 session | unit |
| F4 | happy | filter_by_query: 命中 last_prompt | 返回包含该 session | unit |
| F5 | happy | filter_by_query: 命中完整 uuid | 返回该 session | unit |
| F6 | edge | query 为空字符串 | 返回输入不变 | unit |
| F7 | edge | query 全空白 | 返回输入不变 | unit |
| F8 | edge | query 大写，字段小写 | 仍命中（case-insensitive） | unit |
| F9 | edge | 输入是空 tuple | 返回空 list | unit |
| F10 | happy | filter 多条命中 | 输出顺序 = 输入顺序（不 re-sort） | unit |
| F11 | happy | sort_by_recency: 乱序 3 条 | 输出按 last_activity 倒序 | unit |
| F12 | edge | sort_by_recency: 全部 last_activity 相同 | 输出顺序稳定（=输入顺序） | unit |
| F13 | edge | sort_by_recency: 输入空 tuple | 返回空 list | unit |

#### 改名兼容性

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| R1 | happy | `from claude_island.ui.recents_drawer import RecentsDrawer` | import 成功 | unit |
| R2 | edge | 老路径 `claude_island.ui.history_drawer` | ImportError（无 alias，干净改名） | unit |
| R3 | happy | `from claude_island.ui.recents_filter import sort_by_recency, filter_by_query` | import 成功 | unit |

#### 联动关闭

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| L1 | happy | drawer 可见，expanded.signal_collapse_to_dot 发射 | drawer hidden | integration |
| L2 | happy | drawer 可见，expanded.signal_hidden 发射 | drawer hidden | integration |

### Mock 边界

- `dispatcher`：mock。Resume 路径不测试真实 subprocess
- `launch_intent`：真实实例。verify add() 调用与 LaunchIntent 字段
- `snapshotter.wake`：mock，verify 不被搜索/选中切换调用（沿用上次 fix 的 T6）
- Qt widgets：不 mock。需要真实渲染验证 row 数量、selected accent

### Pass criteria

- 26 个迁移 case + 19 个新 case = 共 45+ case 全 PASS
- 全量 `pytest tests/` 无回归
- 改名后 `python -m import_linter` 仍通过

## D8. Migration & Compatibility

### 改名步骤（一次 PR 完成，分五步 commit）

```
1. refactor(ui): extract _CollapsiblePromptLabel from SessionDetailPopup
   - 新建 ui/_collapsible.py
   - SessionDetailPopup 改为引用
   - 验证 SessionDetailPopup 现有测试不变 (no behaviour change)

2. refactor(ui): extract sort/filter into module-level pure functions
   ↑ 关键：把当前藏在 widget method 里的逻辑抽到模块顶层
   - 新建 ui/recents_filter.py with sort_by_recency + filter_by_query + search_haystack
   - 现有 history_drawer.py 内的 _apply_filter 改为调 filter_by_query
   - 现有 _render_rows 内的 sorted(...) 改为调 sort_by_recency
   - 新增测试: tests/ui/test_recents_filter.py (F1-F13)
   - core 层完全不动（边界澄清：呈现决策属于 UI，core 只管领域规则）
   - 此 commit 后行为等价，只是逻辑位置从 widget method 提到 module-level fn

3. refactor(ui): rename history_drawer → recents_drawer (no behaviour change)
   - 文件 rename
   - 类 rename: HistoryDrawer → RecentsDrawer, _DormantRow → _RecentRow
   - 测试 rename + import 调整
   - expanded_window.py 中 chip 文本/方法名 rename
   - __main__.py import / 变量 rename
   - 此 commit 后所有现有测试仍 PASS（行为零变化，只改名）

4. feat(ui): redesign recents drawer to two-column selector
   - _DRAWER_WIDTH 360 → 420
   - 加 _ListColumn / _PreviewColumn 双栏布局
   - 加 _selected_uuid 状态 + 键盘流
   - 加 Tab toggle preview
   - 加新增的字段（cwd, branch+time, cost+turns+model, mode chip, last_prompt 折叠）
   - 新增 19 个 test cases
   - chip 文本 "🗂 N" → "Recents · N"

5. feat(ui): wire main panel collapse/hide to close recents drawer
   - ExpandedWindow 新增 signal_collapse_to_dot / signal_hidden
   - __main__.py 连接到 recents_drawer.hide
   - 2 个 integration test cases
```

**为什么 commit 2 必须在 commit 3/4 之前**：

如果先 rename 文件（commit 3）再做"提取 module-level 函数"（在 commit 4 内），rename 的 diff 会与"删掉 method、加新文件"的 diff 混在一起，git history 难以审阅。先把"逻辑搬到 module-level"作为独立 commit 完成，行为等价（仍是 history_drawer.py），但内部实现已是调 module-level 函数；这样 commit 3 的改名是纯机械操作，commit 4 的视觉重设计也只是 UI 工作，每个 commit 单一职责。

### 兼容性

- **API**：无外部 API（这是 UI 内部组件），仅 module path 变化。任何外部 import `from claude_island.ui.history_drawer import ...` 会 hard break——预期，因为这是单 app 内部模块
- **数据**：`WorldSnapshot.dormant_sessions / launching_sessions` 不变；`DormantSession` 字段不变
- **持久化**：无（drawer 状态全在内存）
- **import-linter contract**：无变化（仍是 ui → core 单向）

### 回滚策略

每个 commit 都保持单步可回滚：
- commit 4 回滚：drawer 仍工作，只是不联动关闭
- commit 3 回滚：回到老的 history drawer 视觉
- commit 2 回滚：回到 history_drawer 命名（unlikely needed）
- commit 1 回滚：把 _CollapsiblePromptLabel 内联回 SessionDetailPopup
