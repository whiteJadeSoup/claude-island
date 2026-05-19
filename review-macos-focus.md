# Code Review — macOS Focus 修复链 (commits `3c4f6c1` + `d046941`)

**审查范围**：`master` 分支最近两个提交，新增 1444 行 / 删除 49 行：

```
claude_island/platform_/terminals/_macos_common.py   (新, 179 行)
claude_island/platform_/terminals/terminal_app.py    (新, 355 行)
claude_island/platform_/terminals/generic_mac.py     (改, +48/-30)
claude_island/platform_/terminals/iterm2.py          (改, +33/-12)
claude_island/platform_/terminals/__init__.py        (改, +1)
claude_island/ui/expanded_window.py                  (改, +33/-12)
.gitignore                                           (改, +7)
+ 4 个测试文件 (60 个新测试)
```

**审查依据**：项目根 `CLAUDE.md` 的 8 条 Design Principles + 三层架构契约。

---

## 总体结论

**通过**。代码质量高，符合 CLAUDE.md 8 条原则中的全部，layer contract（`lint-imports`）干净。

亮点：
- Fix 3 的"诚实 capability"是 Capability framework 原则的范本演示——用框架解决问题，没引入新的 bool 字段
- 60 个测试，全部 mock 在 trust boundary（`subprocess.run` / `psutil.Process`），跨平台可跑
- 提交前做了**真实环境验证**（`find_ui_app_ancestor` 对用户实际运行的 3 个 claude pid 都返回了 iTerm2 的 76392），不是只跑测试

7 个 follow-up 项见末尾，都是 P2/P3，不阻塞发布。

---

## 逐条对照 Design Principles

### 1. Single source of truth ✅

| 状态 | 真理来源 |
|---|---|
| "哪些 pid 是 UI app" | `_macos_common._cached_ui_pids` 一处 |
| "这个 view 能不能 FOCUS" | `view.capabilities` 一处 |
| "进程 → 宿主 UI app 的映射" | `find_ui_app_ancestor(pid)` 一处 |

iterm2 / terminal_app / generic_mac 三个 adapter 都通过同一个 `_macos_common` helper 解决"找 UI app"问题——而不是各自维护一份父链遍历逻辑。

### 2. Identity by the most specific key ✅

`find_ui_app_ancestor(pid)` 用 pid 作为最具体的 key——既不是 cwd 也不是 project_hash。`frontmost_app(pid)` 同理。

`TerminalAppAdapter` 的 group_id 用 `(window_id, tty)` 而非仅 window_id——即便 Terminal.app 没有 split pane（每个 tab 实际只有一个 tty），保留 tty 维度让 group_id 在跨 snapshot 时稳定（window_id 短暂重用的概率极小，加上 tty 是绝对稳）。

### 3. Compose pre-resolves; render paints ✅

**Fix 3 是这条原则的标杆体现**：

- `generic_mac.group()` 在**构图时**就决定了某个 view 是否带 `Capability.FOCUS`。决策依据是 `find_ui_app_ancestor` 的结果。
- `expanded_window._update_row()` 只做绘制：`setCursor(PointingHand if Capability.FOCUS in view.capabilities else Arrow)`——纯检查，零策略。

如果违反该原则，可能的实现是：UI 在每个 click handler 里都调用 `find_ui_app_ancestor` 决定是否要 dispatch，把策略逻辑撒到每个消费者。Fix 3 没有走那条路。

### 4. Declarative subscription over hand-wired chains ✅ (N/A — 没改订阅)

本次改动不涉及新 UI surface 或新订阅链。capsule / expanded 订阅 `world.observable()` 的现有契约保持不变。

### 5. Protocols at layer boundaries ✅

- `_macos_common` 是 platform_ 层内部 helper，不跨层。无需新 Protocol。
- `TerminalAppAdapter` 实现现有的 `TerminalAdapter` Protocol（`platform_/terminals/protocols.py`），符合"adapter 加新文件 + decorator + 一行 import"的扩展模式。

### 6. Capability framework over per-feature branches ✅

**强烈遵循。** 这次 Fix 3 完美演示了框架的价值：

- 没有在 `Session` / `SessionView` 上加 `is_focusable: bool`
- 没有在 dispatcher 里加 `if session.is_in_tmux: ...` 分支
- 通过移除 `Capability.FOCUS` 即可让 UI 自动失能 click affordance

iterm2 的 `_focus_app_fallback` 也是同源思路——用一个统一 helper 替代每个 adapter 各自的兜底分支。

### 7. Frozen value objects with structural equality ✅

`SessionView` 是 `@dataclass(frozen=True, slots=True)`。group() 用 `dataclasses.replace()` 产生新 view，不变易变。`distinct_until_changed` 仍能正确工作。

新增的 `_macos_common` 模块级 cache（`_cached_ui_pids: frozenset[int] | None`）用 `frozenset` 而非 mutable set——保留结构化相等。

### 8. Architecture as code, not aspirational docstring ✅

实测：`.venv/bin/lint-imports` 通过 4/4 contract，`Analyzed 93 files, 390 dependencies`。

---

## 跨层依赖核验

```
ui/expanded_window.py ──→ core/capabilities.Capability  ✅
platform_/terminals/_macos_common.py ──→ subprocess, threading, psutil  ✅ (无 core/ui 依赖)
platform_/terminals/terminal_app.py ──→ core/capabilities, core/models, core/snapshot  ✅
platform_/terminals/iterm2.py ──→ + _macos_common (同层)  ✅
platform_/terminals/generic_mac.py ──→ + _macos_common (同层)  ✅
```

UI 层只引入了 `Capability.FOCUS` 的 enum 比较，未引入 platform 层。Capability framework 边界保持得很干净。

---

## 测试质量

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_macos_common.py` | 17 | parser / cache TTL / ancestor walk / frontmost / 各种失败模式 |
| `test_terminal_app_adapter.py` | 25 | parse / can_handle / group / focus / capability 表面 |
| `test_iterm2_adapter.py` (扩展) | +5 | tty-miss fallback 的多种触发路径 |
| `test_generic_mac_adapter.py` (扩展) | +9 | UI ancestor focus 路径 / FOCUS drop 行为 |

**Mock 边界正确**：在 `subprocess.run` 和 `psutil.Process` 这两个真正的 trust boundary 处 mock，使得测试在 macOS / Linux / Windows 都能跑（只在 macOS 真实运行 osascript）。

**真实场景的边界用例被锁定**：`test_osascript_timeout_falls_back_to_singletons` 把 `-1712 AppleEvent timed out`（实测在 Terminal.app 无窗口时复现）这个边界条件钉死成测试。这是从真实调研中提炼的回归保护，不是想象出来的。

**断言精度好**：例如 `test_focus_script_selects_window_and_activates` 不仅断言了 AppleScript 包含 `select`/`frontmost`/`activate`，还断言了**顺序**——任何重排都会被测试抓住。

---

## Follow-up 清单（按优先级）

### P2（值得在下个迭代做）

**F1. AppleScript 权限拒绝后的恢复路径不够好**

如果用户首次拒绝了 `tell application "System Events"` 的权限提示，`_query_ui_app_pids` 永远返回空集，`find_ui_app_ancestor` 永远返回 None，**所有** macOS session 在 UI 上都显示为"FOCUS unavailable"。

更糟：30s 的 cache TTL 意味着即使用户后续在 Privacy & Security 里授权了，应用也要等下一次 cache 过期后才会自我修复。

建议：
- 用户点击 ↻ 刷新按钮时绕过 cache（force-refresh 路径）
- 或：在 `_query_ui_app_pids` 返回空集时记录原因（permission denied vs timeout vs other），UI 给出 actionable 提示

**F2. iterm2/generic_mac/terminal_app 三处 `_focus_app_fallback` 重复**

虽然代码很短（3-4 行），但同样的模式在三个 adapter 文件里重复了：

```python
def _focus_app_fallback(view):
    ui_pid = find_ui_app_ancestor(view.session.pid)
    if ui_pid is None:
        return False
    return frontmost_app(ui_pid)
```

可以提到 `_macos_common` 里：

```python
def focus_host_app(view: SessionView) -> bool: ...
```

让三个 adapter 直接 import + 调用。轻微重构，省 ~9 行。

### P3（nice-to-have）

**F3. Cache 失效仅靠时间，缺乏 invalidation 信号**

新启动的 Terminal.app 进入 UI app 集合需要等下次 cache 过期（最多 30s）。理论上可以让 `Snapshotter.wake()` 在某些信号（比如新 session 加入）时连带 invalidate cache，但收益有限——newly-launched 的 Terminal 多半也不会马上有 claude session 跑在里面。当前 30s 是合理的权衡。

**F4. 缺少 cache 过期后重新查询的测试**

`test_second_call_within_ttl_uses_cache` 验证了"在 TTL 内复用缓存"。但没有验证"TTL 过期后会重新发起 osascript"。可以加一个 `test_call_after_ttl_re_queries` mock 时间走过 30s 验证。

**F5. `_OSASCRIPT_TIMEOUT_S` 在三处独立常量**

`_macos_common.py`、`iterm2.py`、`terminal_app.py` 三处各自定义了 `_OSASCRIPT_TIMEOUT_S = 3.0`。常量值一致但是分散。值得在某次清理里集中到 `_macos_common`。

**F6. `frontmost_app` 路径硬编码为 `/usr/bin/osascript`，但 iterm2 用 `osascript`**

```python
# _macos_common.frontmost_app:
["/usr/bin/osascript", "-e", ...]

# iterm2._focus_by_tty / generic_mac.launch:
["osascript", "-e", ...]
```

混用了绝对路径和 PATH 解析。`/usr/bin/osascript` 略安全（不被 PATH 注入污染），但不一致。任选一种统一即可。

**F7. tmux/screen 场景的 UI 提示文案可以更具体**

当前 tooltip 是：

> "Click-to-focus unavailable — no host terminal app in this session's process tree (typical for tmux/screen sessions). Right-click for session details."

中文用户看到这行英文可能有点突兀（用户的 memory 偏好是简体中文）。如果 UI 决定走 i18n，这条文案是首批候选。

---

## 实战观察记录

值得保留的几条 macOS 平台层调研产物（如果项目想加 `docs/macos-quirks.md`）：

1. **`/usr/bin/security` ACL 信任**：`Claude Code-credentials` keychain item 把 `/usr/bin/security` 列入可信应用，所以 subprocess shell-out 不会触发 keychain 弹框。Apple 签名的二进制承担了"代理"角色。

2. **`System Events` 的 `process` 元素只枚举 UI 应用**：CLI pid 传给 `process whose unix id is X` 一定返回错误 `-1719 Invalid index`。这是 macOS 的设计而非 bug。

3. **Terminal.app 无窗口 → AppleScript 全卡死**：`Terminal` 进程跑着但没有窗口时，连 `count windows` 都返回 `-1712 AppleEvent timed out`。3s timeout + singleton fallback 是保护机制。

4. **psutil 在 macOS 报告版本号作为 process name**：用户从官方 installer 装的 `~/.local/share/claude/versions/2.1.129/claude` 在 psutil 里 `proc.name() == "2.1.129"`。`_VERSION_LIKE` 正则 + cmdline argv0 confirm 同时命中才能正确识别。

5. **iTerm2 进程链**：`claude → -zsh → login → iTermServer-3.6.10 → iTerm2`。父链 4 跳到 iTerm2。`_MAX_DEPTH = 12` 留足余量。

6. **tmux daemonize**：tmux server 的 ppid 是 launchd（1），父链断开。任何 in-tmux claude 都无法通过祖先走法找到宿主 iTerm2。这是结构性限制，不是 bug。

---

## 一句话评价

**这两个 commit 是项目里 Capability framework + Architecture-as-code 原则的优秀落地范本**。建议合并。所有 follow-up 项都是 nice-to-have，不阻塞发布。
