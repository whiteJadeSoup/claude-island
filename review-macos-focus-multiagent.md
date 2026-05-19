# Code Review — `3c4f6c1` + `d046941`（macOS focus 修复链）

**审查方式**：全局 `~/.claude/instructions/code-review.md` 的 Part 0 多-agent 架构（A: 正确性/安全 + B: 质量/韧性 + C: 清晰度/覆盖率，并行 → orchestrator 整合）。

**对照基线**：上次按项目 CLAUDE.md（Design Principles）的 review（`review-macos-focus.md`）结论是 "通过 + 7 个 P2/P3 follow-up"。本次按全局规范更严格的口径（[blocking] 标准 = "若不修，生产会出现可观察的错误或数据问题"）+ multi-agent 视角，得出**不同结论**。

---

## 总体结论：**Request Changes**

> 主路径（happy path）实现质量很高，与上次 review 一致。本次 review 把视角放到 **degradation paths**——这些路径在 multi-agent 流程下浮出 3 条 blocking + 3 条 question + 1 条 suggestion。这些项与 PR 描述的核心承诺（"诚实信号胜过静默失败"）直接相关。

3 条 blocking 全部聚焦在**降级路径与 UX 反馈**，不是 happy path 的逻辑错误：

1. cache 把临时失败固化 30s → 全局 FOCUS 雪崩
2. focus 在 Qt 主线程同步跑 osascript chain → ~850ms / 最坏 9s 卡顿
3. 三个新文件零日志 + tooltip 撒谎归因 → 用户无法诊断

---

## 🔴 [blocking] B1：cache 把临时失败固化 30s（A-001 + B-003 + C-001 合并）

**Location**: `claude_island/platform_/terminals/_macos_common.py:129-141, 144-170`，级联到 `generic_mac.py:62-78`

**What**: `_query_ui_app_pids` 在 `TimeoutExpired` / 非零 returncode / `OSError` 三种失败路径上都返回 `frozenset()`；`_ui_app_pids` 把这个空 set 与 `_cached_at = now` 一起写入缓存，TTL 30s。在这 30s 里 `find_ui_app_ancestor(pid)` 对**任何 pid** 都返回 `None`，`generic_mac.group()` 把 `Capability.FOCUS` 从**所有非 iTerm/非 Terminal.app** view 上 strip，UI 把每一行翻成 ArrowCursor + "unavailable" tooltip。

**Why**: 单次 osascript 抖动（macOS Spotlight 重建索引、System Events 短暂忙、用户拒绝又同意权限的瞬间）就能触发**整个 macOS 用户群** 30s 内点不动 focus，且 `snapshotter.wake()` 不会 bypass cache，用户手动刷新无效。

```
t=0.0   osascript timeout (System Events 瞬时忙)
        _query_ui_app_pids  → frozenset()
        _cached_ui_pids = frozenset();  _cached_at = 0.0
t=0.5   generic_mac.group(views=[v1..v8])
        for v in views: find_ui_app_ancestor(v.pid) → None  (cache hit, 空集)
        每个 v 都被 strip 掉 FOCUS
t=0.5   UI 渲染 8 行，全部 ArrowCursor + tooltip "unavailable"
t=0.5..29.9  用户点 ↻ Refresh、点 row、什么都点不动
t=30.0  cache 过期，下一次 osascript 成功，FOCUS 恢复
```

**Proof**:

```python
# tests/platform_/test_macos_common.py 现有 fixture 已能跑此场景：
with mock.patch("subprocess.run", side_effect=TimeoutExpired(...)):
    assert _ui_app_pids() == frozenset()   # 写入空集
with mock.patch("subprocess.run") as m:    # 第二次会成功
    m.return_value.stdout = "..."          # 但永远不会被调用
    for _ in range(100):                   # 因为 cache hit
        find_ui_app_ancestor(12345)        # 全部返回 None
    assert m.call_count == 0               # 真的没再 query
```

**Suggestion**: 不缓存失败，或显著缩短失败 TTL。两种实现：

(a) 在 `_query_ui_app_pids` 里区分「查询失败 → raise / sentinel」与「查询成功 → frozenset(...)」，`_ui_app_pids` 失败时**不更新缓存**（保留上一次 last-known-good，自动在下次 wake 时重试）；

(b) 维护 `(pids, last_success_at, last_attempt_at)`，empty 走更短 TTL（1-2s），populated 走 30s。

```
失败时:
  ┌─ 当前 ─┐                      ┌─ 修复后 ─┐
  cache := frozenset()             保留 last-known-good
  下次 30s 内 cache hit             下次 wake 立刻重试
  全局 FOCUS 雪崩                   单次失败 = 单次 wake 退化
```

> 上一轮项目 CLAUDE.md review 把该项标 P2/F1（不阻塞），全局规范下我升为 [blocking]——因为现象是"一次普通 timeout → 全 macOS 用户 30s 内 focus 失效"，符合"若不修，生产会出现可观察问题"的标准。

---

## 🔴 [blocking] B2：focus 在 Qt 主线程同步跑 osascript chain

**Location**: `expanded_window.py:5036-5048` → `dispatcher.py:170` → `iterm2.py:282-284` → `_macos_common.py` (`_focus_app_fallback`)

**What**: 一次 row click，最坏情况下 Qt 主线程被 3 个 osascript subprocess 串行阻塞 ~850ms（实测）。如果 System Events 或 iTerm2 卡死，3s × 3 = **最长 9s** 主线程冻结，期间 hover、scroll、tooltip 全部停摆。

**Why**:

```
user click  (Qt main thread, dispatcher 同步调用)
  └─ ITerm2Adapter.focus(view)
       ├─ psutil.Process(pid).terminal()           ≈   1 ms
       ├─ _focus_by_tty(tty)  → osascript run     ≈ 290 ms (window/tab/session 全枚举)
       │     └─ tty miss (e.g. tmux-in-iTerm2)
       └─ _focus_app_fallback(view)
            ├─ find_ui_app_ancestor(pid)
            │    └─ _ui_app_pids() cold → osascript ≈ 270 ms
            └─ frontmost_app(ui_pid) → osascript    ≈ 290 ms
  TOTAL: ~850 ms 阻塞                              [3s timeout × 3 = 9s 上限]
```

**Proof**: 实测当前机器：

```bash
$ /usr/bin/time -p osascript -e 'tell application "System Events" to get unix id of every process'
real 0.27
$ /usr/bin/time -p osascript -e 'tell application "System Events" to set frontmost of (first process whose unix id is 76392) to true'
real 0.29
```

触发条件：tmux-in-iTerm2 session（`_focus_by_tty` 返回 'miss' → 走 fallback chain，3 次 osascript 串行）。`dispatcher.py:170` 的 `bool(method(view, **kwargs))` 是同步调用，Qt event loop 真的被卡。

**Suggestion**: 至少做下面之一：

(a) **Prewarm cache**（最低门槛）：让 `Snapshotter.wake()` 在 worker 线程上把 `_ui_app_pids()` 拉热，click 时只剩 2 次 osascript 而不是 3 次。

(b) **QThreadPool 异步 dispatch**（结构正确）：`focus()` 的返回值现在已经是"尽力而为"的（panel 自己靠 WindowDeactivate 关闭），不依赖 bool 同步返回——很适合甩到 thread pool。

最低接受 (a)；(b) 跟进。

---

## 🔴 [blocking] B3：三个新文件零日志，tooltip 错误归因

**Location**: `_macos_common.py` / `generic_mac.py` / `terminal_app.py` 全文 + `expanded_window.py:4730-4737`

**What**: 三个新文件都没有 `import logging` / `log.*`。`_query_ui_app_pids` 把 `result.stderr`（含 `errAEPrivilegeError -1743 "Not authorized to send Apple events"`）直接丢弃。三种失败路径（permission denied / timeout / OSError）汇聚到同一行 tooltip：「typical for tmux/screen sessions」——而**实际原因是用户拒绝了 System Events 权限**。用户被引导去检查 tmux，永远找不到 Privacy & Security 设置。

**Why**:

```
_query_ui_app_pids():
  ├─ TimeoutExpired           → frozenset()  [无日志, stderr 丢弃]
  ├─ result.returncode != 0   → frozenset()  [无日志, stderr 丢弃]
  └─ OSError                  → frozenset()  [无日志]
        ↓
find_ui_app_ancestor(pid) → None
        ↓
generic_mac.group(): caps - {FOCUS}
        ↓
expanded_window: tooltip = "typical for tmux/screen sessions"   ← 错误归因
```

`dispatcher.py` 已经用 `log = logging.getLogger(__name__)`，但只覆盖 dispatch 阶段；上游"为什么 FOCUS 在 group() 时被 strip"对调试者完全不可见。

**Proof**: System Settings ▶ Privacy & Security ▶ Automation 里禁掉 claude-island 对 System Events 的权限，重启 app，开一个普通的 iTerm2 + claude session：

- 期望（PR 承诺）："诚实信号告诉用户为什么不能 focus"
- 实际：tooltip 说 "typical for tmux/screen"，用户没在 tmux 里，开始怀疑是 bug
- `result.stderr.decode()` 里就有 `Not authorized to send Apple events`，但被代码 `result.stderr` 之后没读丢弃了

**Suggestion**:

(a) `_query_ui_app_pids` 三种失败路径 `log.warning("ui_app_pids query failed: %s", reason)`，并对 `result.stderr` 做 once-only 记录（避免持续被拒时刷屏）。

(b) `compose_session_view` 在 strip FOCUS 时把"为什么"也算出来（permission_denied / timeout / no_ui_ancestor），写入 `view.focus_unavailable_reason: str | None`。`expanded_window` 改读这个字段渲染——同时解决 B-004 的「policy in render 违反原则 3」问题。

```
当前 (render 决策):                       修复后 (compose 决策, render 绘制):
if FOCUS in caps: ...                     if view.focus_unavailable_reason:
else: tooltip = "tmux/screen"                tooltip = view.focus_unavailable_reason
                                          else: ...
```

---

## 🟡 [question] Q1：FOCUS 被 strip 时 row 仍然连着 click handler（A-002）

`expanded_window.py:4602-4605, 4727-4740`：cursor 改成箭头 + tooltip 改了，但 `btn.clicked.connect(_on_row_clicked)` 没断，click 仍触发 `_dispatch(view, FOCUS, ...)` → dispatcher 因 cap 缺失返回 False → 静默 no-op。"诚实信号"对眼睛兑现了，对手指没兑现。

**Suggestion**：要么 `btn.setEnabled(False)`，要么 click 后弹一个 transient toast。

---

## 🟡 [question] Q2：tooltip 文本属于 render 决策（B-004）

`expanded_window.py:4727-4737` 让 render 同时承担「FOCUS 是否在 caps」的判断 **+** 「不在时该说什么」的策略。CLAUDE.md 原则 3「compose pre-resolves; render paints」明确反例。修复方式见 B3 的 (b)：让 compose 给一个 `focus_unavailable_reason: str | None`，render 只读不算。第二个 surface（tray menu / 通知）想复用这块时不必复制 string。

---

## 🟡 [question] Q3：`can_handle`（psutil）vs `find_ui_app_ancestor`（System Events）的非对称性（C-003）

`TerminalAppAdapter.can_handle` 用 psutil 走父链找 'terminal'；`find_ui_app_ancestor` 用 System Events 的 UI-pid 集合做交集。两者答相邻但不同的问题，且**信任边界不同**：

```
父链: claude → zsh → Terminal (psutil 一定能看到)
UI-pid set: {Finder, iTerm2, ...} (osascript 可能权限被拒, 返回空集)
```

可触发 `can_handle()=True` 但 `_focus_app_fallback()=False`：adapter 认领了 session 却 focus 不动。当前没文档说明。Suggestion：在 docstring 里加一句明确两者职责差异，或者由 compose 阶段把这个不一致折算成 `focus_unavailable_reason`。

---

## 🔵 [suggestion] S1：三处 `_focus_app_fallback` + 三处 `_OSASCRIPT_TIMEOUT_S`（B-005）

`iterm2.py:390`、`terminal_app.py:326`、`generic_mac.py` 三处近乎相同的 fallback 代码，加上三处独立 `_OSASCRIPT_TIMEOUT_S = 3.0`。CLAUDE.md 原则 5："第三次出现就该提取"——目前正好第三次。

对比 3 种方案（已 web 调研 Python stdlib `email.message_from_*` 同辈 helper 模式 + Anthropic SDK 内部 client utils 同辈共享模式 + 项目自身现有 `iterm2._escape_applescript_string` 已被两处复用的事实）：

| | A. 现状（3× 重复） | B. 提到 `_macos_common`（推荐） | C. 抽 `MacOsAdapterMixin` 基类 |
|---|---|---|---|
| 节省行数 | 0 | ~9 helper + 2 const | ~30 group/focus 骨架 |
| 耦合 | 无 | 轻——共享 module-level helper | 重——继承层级 |
| 第 4 个 macOS adapter（Warp / Ghostty / Kitty）落地风险 | 高 | 低 | 中 |
| 与项目现有 pattern 一致 | ✗（违反原则 5）| ✓（`_macos_common` 已是这角色）| ✗（项目用 `@adapter` decorator 而非继承）|

**推荐 B**：把 `focus_host_app(view: SessionView) -> bool` 和 `_OSASCRIPT_TIMEOUT_S` 提到 `_macos_common`；三个 adapter 改成 `from _macos_common import focus_host_app`。

**Trade-off**：B 锁定了"所有 macOS adapter 想要相同 fallback 行为"的假设；如果未来 Warp 想要 fallback + 通知，它直接不 import 即可，零成本。C 在当前项目规模下是错的抽象——项目刻意走 decorator-based composition。

---

## ⚪ [nit] 一行收拾

| ID | 位置 | 问题 |
|---|---|---|
| N1 | `terminal_app.py:49, :68` | `import shlex` / `from ... TerminalAdapter` 未使用，从 iterm2.py copy-paste 残留 |
| N2 | `iterm2.py:282-284` | fallback 重新走 `find_ui_app_ancestor`，丢弃了 `can_handle` 已遍历过的父链结果——可在 SessionView 上 stash `host_app_pid` 优化 |
| N3 | `tests/platform_/test_macos_common.py` | 缺 TTL 过期重 query 的测试（`mock.patch monotonic` 让时钟跨过 30s，断言 `subprocess.run.call_count == 2`）|
| N4 | `expanded_window.py:4730-4737` | 新加的 tooltip 文本是英文；用户偏好简体中文，新加 user-facing 字符串是最便宜的合规时机 |
| N5 | `_macos_common.py:165-169` | `tok.isdigit()` 静默吞错——returncode 0 但 stdout 含错误 token 时无法观察。和 B3 一起加日志 |

---

## 整合后的 finding 统计

| Severity | 数量 | 阻塞合并？ |
|---|---|---|
| [blocking] | 3 | 是——必须修才能进 next iteration |
| [question] | 3 | 视作者答复决定 |
| [suggestion] | 1 | 不阻塞 |
| [nit] | 5 | 不阻塞 |

**与上次项目 CLAUDE.md review 的差异**：

- 上次结论"通过"是基于 happy path 评估 + 8 条 Design Principle 自检
- 本次基于全局 multi-agent 流程，**A agent 主动构造 degradation 场景做 executable proof**，把 cache 行为（上次标 P2/F1）升级为 [blocking]
- B agent 实测 osascript 时延，识别 Qt 主线程阻塞为 [blocking]
- 上次完全没覆盖的「无日志 + tooltip 错误归因」由 B agent 补齐为 [blocking]

**Conclusion: Request Changes**——B1/B2/B3 都是直接关联到 PR 标题承诺的"诚实信号胜过静默失败"，不修这三条等于 PR 的核心价值打折。Q1-Q3 + S1 + N1-N5 在下一轮一并处理或拆 follow-up。

建议优先级：**B1 → B3 →（用 compose-time `focus_unavailable_reason` 一次性吃掉 B3 + Q2 + Q3）→ B2**。
