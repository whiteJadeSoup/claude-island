# Fix: HistoryDrawer 搜索过滤被 distinct_until_changed 阻断

## 1. Problem & Goals

### Problem

HistoryDrawer 的搜索框输入无效——用户输入关键词后，列表不更新。

**根因链**：

```
用户输入 "foo"
  → _on_search_changed("foo")
  → _search_query = "foo"           ← UI 状态已更新
  → snapshotter.wake()              ← 触发全量快照重建（IO）
  → _build_snapshot() 完成
  → world.push(snap)
  → HistoryDrawer.compute(snap)     ← 只读 snap.dormant/launching
  → 返回的 key 与上次相同（数据没变）
  → distinct_until_changed 丢弃    ← render 未被调用
  → 搜索过滤未应用                  ← BUG
```

`_on_search_changed` 依赖 snapshotter→world→dedup 这条**数据管道**来触发重渲染，但搜索是**UI 层过滤**，不改变快照数据。当快照数据稳定（无活跃会话、无 JSONL 变化）时，`distinct_until_changed` 必然丢弃渲染，搜索永远不生效。

即使快照数据有变化（恰好有进程扫描触发），搜索也必须等待 ~100ms debounce + 全量重建才能生效，响应延迟不可接受。

**附带问题**：每次按键都调用 `snapshotter.wake()`，触发 SQLite 查询 + 进程扫描 + JSONL 元数据读取——对一个纯 UI 过滤操作而言是严重的资源浪费。

### Goals

- G1: 搜索框输入后列表**立即**更新，不依赖快照数据是否变化
- G2: 搜索操作**不触发** snapshotter 重建（消除不必要的 IO）
- G3: 不破坏现有 render 管道的 dedup 语义（dormant/launching 数据不变时仍应跳过渲染）

### Non-Goals

- 不重构 HistoryDrawer 的整体架构（row 回收、虚拟滚动等）
- 不修改 `compute` 的 dedup key 范围（name/permission_mode 缺失是独立问题）
- 不修复 hover 闪烁或空状态文案问题

## 2. Solution Design

### Architecture

```
修复前：

  snapshotter.wake() → _build_snapshot() → world.push(snap)
       ↑                                       │
  _on_search_changed()              distinct_until_changed(compute)
                                           │
                                    data 变了？ ──否──→ 丢弃 ← 搜索无效
                                           │
                                          是
                                           ↓
                                    render(snap) → _render_rows(snap)
                                                    ↑ 读取 _search_query

修复后：

  ┌─ 数据变化路径（不变）─────────────────────────────────────┐
  │  snapshotter → world → distinct_until_changed → render  │
  │      (只响应快照数据变化)                                  │
  └──────────────────────────────────────────────────────────┘

  ┌─ UI 状态变化路径（新增）──────────────────────────────────┐
  │  _on_search_changed()                                    │
  │    → _last_snap 缓存命中？                                │
  │        是 → 直接 _render_rows(_last_snap)  ← 即时，无IO  │
  │        否 → no-op（首次 render 还没发生，无内容可过滤）     │
  └──────────────────────────────────────────────────────────┘

  render(snap) 新增：self._last_snap = snap  ← 缓存给搜索用
```

### Flow

**核心流程 — 搜索触发重渲染**：

```
1. 用户输入 "foo"
2. _on_search_changed("foo")
3.   _search_query = "foo"
4.   if _last_snap is not None:         ← 缓存存在
5.     _render_rows(_last_snap)         ← 直接重渲染，不走 snapshotter
6.     return
7.   # else: 首次 render 还没发生，搜索框为空时无意义，no-op
```

**数据变化流程（不变）**：

```
1. snapshotter.build → world.push(snap)
2. distinct_until_changed(compute)
3. data 变了 → render(snap)
4.   _last_snap = snap                  ← 新增：缓存
5.   _render_rows(snap)                 ← _search_query 已在步骤 3 之前设置
6.   launching timeout toast 检测
```

### 变更清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `ui/history_drawer.py` L464 | 新增 `self._last_snap: WorldSnapshot \| None = None` | 缓存最近一次快照 |
| `ui/history_drawer.py` L512 | `render()` 内新增 `self._last_snap = snap` | 每次 render 时更新缓存 |
| `ui/history_drawer.py` L704-706 | `_on_search_changed` 改为直接调 `_render_rows(_last_snap)` | 不再调 `snapshotter.wake()` |

共 3 处改动，不涉及其他文件。

## 3. Research & Comparison

### Alternatives

| | A: 缓存 snapshot + 直接重渲染 | B: 去掉 distinct_until_changed | C: 把搜索词加入 compute key |
|---|---|---|---|
| 搜索即时生效 | ✅ | ✅ | ⚠️ 需额外触发 snapshotter |
| 不触发 IO | ✅ | ✅ | ❌ 仍需 wake snapshotter |
| 保留 dedup 语义 | ✅ 数据变化时仍跳过无意义渲染 | ❌ 每次 push 都渲染 | ✅ |
| 实现复杂度 | 低（3 行改动） | 低（删 1 行） | 中（需把 UI 状态注入 compute） |

### 决策：方案 A

**Why**: 搜索是纯 UI 过滤，不应穿透到数据层。方案 A 让搜索和 dedup 各管各的——dedup 只关心快照数据是否变化，搜索只关心缓存快照 + 当前 query。两者正交，互不干扰。

**Risks**:

- **Type A — cost of choosing**: 缓存的 `_last_snap` 在 drawer 隐藏期间不会更新（render 不被调用）。如果 drawer 隐藏时快照变了，打开后第一次 render 会用新快照覆盖缓存，搜索基于新数据——行为正确。但如果用户在 drawer 隐藏时输入搜索词（不可能，搜索框不可见），缓存是旧的——这个场景不存在，因为搜索框在 drawer 内部。

- **Type B — intrinsic fragility**: `_render_rows` 同时被 `render()` 和 `_on_search_changed` 调用，必须确保它只依赖 `_search_query` + 传入的 snap，不依赖其他可变状态。当前 `_render_rows` 确实只读 `_search_query` 和传入的 snap，满足约束。

---

# Detail Design

## 1. Module Responsibilities & Interfaces

本次改动仅涉及 `HistoryDrawer`，无新增模块/子模块。改动集中在两个内部方法的契约变化：

### 变更前

```
HistoryDrawer._on_search_changed(text: str) -> None
  side-effect: self._search_query = text; self._on_wake()  # snapshotter.wake()
  contract: 搜索触发快照重建，间接驱动重渲染
```

### 变更后

```
HistoryDrawer._on_search_changed(text: str) -> None
  pre:     None
  side-effect: self._search_query = text
              if self._last_snap is not None:
                  self._render_rows(self._last_snap)
  contract: 搜索直接驱动重渲染，不触发快照重建
  errors:  _last_snap 为 None 时 no-op（首次 render 还没发生）
```

```
HistoryDrawer.render(snap: WorldSnapshot) -> None
  side-effect: self._last_snap = snap  ← 新增
              self._render_rows(snap)
              launching timeout toast 检测
  contract: 不变，仅新增缓存写入
```

### `_render_rows` 的调用者约束

`_render_rows` 现在有两个调用者，它依赖的隐式状态必须明确：

| 依赖 | 来源 | 可变性 |
|------|------|--------|
| `self._search_query` | `_on_search_changed` 设置 | 任意时刻可变，但只在 Qt 主线程（textChanged signal） |
| `snap` 参数 | `render()` 或 `_on_search_changed` 传入 | 不可变（WorldSnapshot 是 frozen dataclass） |
| `self._rows_box` | Qt 布局 | 只在 Qt 主线程操作 |

**不变量**：`_render_rows` 不读取 `self._last_snap`、`self._prev_launching`、`self._dispatcher` 等。当前实现满足——`_render_rows` 只读 `_search_query` + `snap` 参数。

## 2. Data Model / Schema

无 schema 变更。新增一个实例字段：

```python
# HistoryDrawer.__init__
self._last_snap: WorldSnapshot | None = None
```

- 类型：`WorldSnapshot | None`（frozen dataclass，不可变）
- 生命周期：随 HistoryDrawer 实例，在 `render()` 中更新
- 初始值：`None`（表示从未 render 过）
- 线程安全：只在 Qt 主线程读写（`render` 和 `_on_search_changed` 都在 Qt 主线程）

## 3. Core Algorithms / State Machines

无状态机。搜索的决策逻辑是一个简单的 guard：

```
_on_search_changed(text)
  │
  ├─ _search_query = text
  │
  └─ _last_snap is not None?
       │
       是 → _render_rows(_last_snap)   ← 即时，无 IO
       │
       否 → no-op
            （首次 render 未发生，搜索框为空，无意义）
```

**不变量**：`_last_snap` 一旦被设置（首次 `render()` 调用后），永远不为 `None`。因为 `world.push` 在 app 启动时就会推送 `WorldSnapshot.empty()`，而 subscription 会调用 `render(snap)`。

## 4. Error Handling

| 调用 | 失败模式 | 处理 | 调用者看到 |
|------|---------|------|-----------|
| `_render_rows(_last_snap)` | _last_snap 为 None | guard 跳过 | 无渲染（搜索框为空，无内容可过滤） |
| `_render_rows(_last_snap)` | _last_snap 过期（drawer 隐藏期间数据变了） | 正常渲染——基于旧数据过滤，列表可能短暂过时 | 下次 `render()` 会用新 snap 覆盖缓存，列表自动修正 |

**_last_snap 过期场景分析**：

drawer 隐藏期间，`render()` 不会被调用（`distinct_until_changed` 在 subscription 层跳过，且 drawer 不可见）。当用户重新打开 drawer 并输入搜索时，`_last_snap` 是关闭前最后一次 render 的快照。但 `_on_search_changed` 在 `toggle()` → `show()` 之后才可能触发（搜索框在 drawer 内部），而 `show()` 不会触发 `render()`。

这是否是问题？不是。因为：
1. 打开 drawer 后，用户先看到的是关闭前的列表状态
2. 如果快照数据在隐藏期间变了，下一个 snapshotter tick 会推送新快照
3. `distinct_until_changed` 检测到数据变化 → 调用 `render()` → 更新 `_last_snap`
4. debounce 最多 100ms，用户几乎感知不到

## 5. Implementation Flows

```
HistoryDrawer.__init__()
  └─ self._last_snap = None              ← 新增字段

HistoryDrawer.render(snap)
  ├─ self._last_snap = snap              ← 新增：缓存
  ├─ self._render_rows(snap)
  └─ launching timeout toast 检测

HistoryDrawer._on_search_changed(text)
  ├─ self._search_query = text
  └─ if self._last_snap is not None:     ← 改动：替换 self._on_wake()
       self._render_rows(self._last_snap)
```

## 6. Performance Estimation

G2 是定性的（"不触发 IO"），不要求精确数值。定性分析：

| 路径 | 修复前 | 修复后 |
|------|-------|-------|
| 搜索按键 | snapshotter.wake() → 100ms debounce → _build_snapshot (SQLite + process scan) → render | 直接 _render_rows（纯 widget 操作） |
| 单次搜索延迟 | ~100-200ms（debounce + build + marshal） | <1ms（widget 重建） |
| 搜索期间 CPU/IO | 每次按键触发 SQLite + psutil | 无 |

## 7. Testing Strategy

### 现有测试覆盖

`tests/ui/test_history_drawer.py` 已有 HistoryDrawer 渲染测试。需新增搜索相关测试。

### Test Cases

**Goal G1: 搜索即时生效**

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| T1 | happy | drawer 已 render 过；输入搜索词匹配 1 个 dormant | 列表只显示 1 个匹配行 | unit |
| T2 | happy | drawer 已 render 过；输入搜索词不匹配任何 dormant | 列表显示空状态 | unit |
| T3 | edge | `_last_snap` 为 None（render 从未调用）；输入搜索词 | 不崩溃，no-op | unit |
| T4 | edge | 搜索框清空 | 列表恢复显示全部 dormant | unit |
| T5 | edge | 搜索词匹配 uuid 前 8 位 | 命中对应 session | unit |

**Goal G2: 不触发 snapshotter**

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| T6 | happy | 输入搜索词 | `snapshotter.wake()` 未被调用 | unit |

**Goal G3: dedup 语义不变**

| ID | Path | Input | Expected | Level |
|----|------|-------|----------|-------|
| T7 | happy | 快照数据不变，连续 push 两次 | `render()` 只调用一次 | unit |
| T8 | edge | 快照数据不变 + 用户搜索 → render 被搜索路径触发 → 数据变化 push | 搜索渲染 + 数据渲染各一次，无冲突 | unit |

### Mock boundaries

- `WorldSnapshot`: 用 `WorldSnapshot.empty()` 或手动构造，不 mock
- `_dispatcher`: mock（不测试 launch 逻辑）
- `snapshotter.wake`: mock（用于 T6 验证不被调用）
- Qt widgets: 不 mock（需要真实 widget 测试渲染结果）

### Pass criteria

- T1-T5: 搜索后 `_rows_box` 中的 row 数量与预期匹配
- T6: `mock_wake.assert_not_called()`
- T7-T8: render 调用次数符合预期

## 8. Migration & Compatibility

无兼容性风险。改动是 HistoryDrawer 的内部实现，不影响：
- 公共 API（`render()`、`compute()`、`toggle()` 签名不变）
- 订阅管道（`__main__.py` 的 subscription 不变）
- 快照数据结构（`WorldSnapshot` 不变）
- 其他 UI surface（capsule、expanded 不受影响）
