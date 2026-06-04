# Active Session UI Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the active-session "Live Console" card in the QML expanded panel with a containerless, phase-driven design (name as a Bricolage display headline; CWD + git branch; a distinct animated indicator per phase; no raw command), tune the panel for color coherence, and update the collapsed floating pill to match.

**Architecture:** Pure-Python `core` already parses everything we need; we add two derived fields to `SessionView` (`git_branch`, `seconds_since_token`) and project them to QML. All visual work happens in `claude_island/ui/qml/Main.qml` + `Theme.qml`, with one new bundled font. The 5 phase indicators are drawn with QML `Canvas` (same primitive the current waveform already uses). No new dependencies.

**Tech Stack:** Python 3.14, PySide6 / QtQuick (QML), `pytest`, `import-linter`. Design is frozen in the mockups under `design/2026-05-island-redesign/active-redesign/` (open the `.html` files with a browser; `04-design-sheet.html` is the master reference).

**Design source of truth (open these before starting):**
- `design/2026-05-island-redesign/active-redesign/04-design-sheet.html` — full panel + 5 states + pill
- `design/2026-05-island-redesign/active-redesign/01-working-states.html` — running / thinking / compacting indicators
- `design/2026-05-island-redesign/active-redesign/02-wait-stuck-states.html` — waiting_approval / stuck
- `design/2026-05-island-redesign/active-redesign/03-overall-panel.html` — coherence pass (gold quota bar, elevated active)

**Design contract (what each state shows):**

```
[phase indicator]  PHASE · elapsed          (mono caps, phase-colored)
                   claude-island · opus 4.8  (Bricolage display name + mono model)
~/coding-projects/claude-island  ⎇ master  $0.41   (mono cwd + branch + dimmed cost)
^ phase-colored glowing left rail; containerless surface with faint phase-tinted elevation bg
```

| phase (SessionPhase value) | label | rail/indicator color | indicator |
|---|---|---|---|
| `tool_use` | `running` | teal `#5fe0b4` | filled core dot + halo + faint outer ring (pulse) |
| `thinking` | `thinking` | violet `#9a8cff` | 8-spoke Claude spark + faint echo |
| `compacting` | `compacting` | gold `#e6b96e` | 3 concentric rings (empty center) + 4 inward ticks |
| `waiting_approval` | `awaiting approval` | amber `#f3b95e` | `!` in ring (signal only — Allow/Deny stays in the existing top DecisionCard) |
| derived "stuck" (`tool_use`/`thinking` with `seconds_since_token >= 18`) | `stuck` | coral `#e0795a` | hollow ring (no fill, no glow) + `⚠ no new tokens · Ns` line; rail dimmed |

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `claude_island/core/snapshot.py` | `SessionView` dataclass + `compose_session_view()` + cache-volatility check | add 2 fields, populate them, mark `seconds_since_token` volatile |
| `claude_island/ui/snapshot_projection.py` | per-session dict for QML | project the 2 new keys |
| `claude_island/ui/qml/Theme.qml` | color/size/font tokens + `phaseColor()` | add phase colors, `fontDisplay`, `railColor()`/`phaseLabel()` helpers |
| `claude_island/ui/qml/fonts/BricolageGrotesque-*.ttf` | bundled display font | new files (auto-loaded by existing glob) |
| `claude_island/ui/qml/Main.qml` | the panel | replace ACTIVE card delegate (678–878); recolor TODAY quota bar; spacing |
| `tests/ui/test_snapshot_projection.py` | projection unit test | new test for the 2 keys |
| `tests/ui/test_qml_no_warnings.py` | QML smoke/binding test | extend snapshot fixture with the 2 fields + a stuck/waiting session |

---

## Phase 0: Branch

- [ ] **Step 0.1: Create a feature branch**

Run:
```bash
cd /Users/constantine/coding-projects/claude-island
git checkout -b feat/active-session-redesign
```
Expected: `Switched to a new branch 'feat/active-session-redesign'`

---

## Phase 1: Data — surface `git_branch` and `seconds_since_token`

### Task 1: Project the two new keys (pure layer, TDD first)

**Files:**
- Test: `tests/ui/test_snapshot_projection.py` (create)
- Modify: `claude_island/ui/snapshot_projection.py` (the `_session(v)` function, ~lines 61–83)
- Modify: `claude_island/core/snapshot.py` (`SessionView` dataclass, after line 215)

- [ ] **Step 1.1: Write the failing projection test**

Create `tests/ui/test_snapshot_projection.py`:
```python
"""Unit tests for the SessionView -> QML dict projection."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from claude_island.core.models import Session
from claude_island.core.snapshot import SessionView
from claude_island.core.session_phase import SessionPhase
from claude_island.ui.snapshot_projection import _session


def _make_view(**overrides) -> SessionView:
    """Minimal SessionView; overrides patch specific fields under test."""
    base = dict(
        pid=1234,
        name="claude-island",
        project_path=Path("/Users/me/coding-projects/claude-island"),
        project_basename="claude-island",
        last_activity=datetime.now(timezone.utc),
        cost_usd=0.41,
        is_high_cost=False,
        latest_model="claude-opus-4-8",
        status_word="busy",
        session=Session(cwd=Path("/Users/me/coding-projects/claude-island"), pid=1234),
        session_uuid="uuid-1",
        phase=SessionPhase.TOOL_USE,
    )
    base.update(overrides)
    return SessionView(**base)


def test_projection_includes_git_branch():
    d = _session(_make_view(git_branch="master"))
    assert d["git_branch"] == "master"


def test_projection_git_branch_none_is_passed_through():
    d = _session(_make_view(git_branch=None))
    assert d["git_branch"] is None


def test_projection_includes_seconds_since_token():
    d = _session(_make_view(seconds_since_token=18.7))
    assert d["seconds_since_token"] == 18  # int-truncated for QML
```

- [ ] **Step 1.2: Run the test to verify it fails**

Run: `pytest tests/ui/test_snapshot_projection.py -v`
Expected: FAIL — either `TypeError: __init__() got an unexpected keyword argument 'git_branch'` (field not on SessionView yet) or `KeyError: 'git_branch'` (not projected yet).

- [ ] **Step 1.3: Add the two fields to `SessionView`**

In `claude_island/core/snapshot.py`, immediately after line 215 (`last_command_elapsed_s: float | None = None`) and before `def __post_init__`:
```python
    # ── CWD context (redesign) ──────────────────────────────────────
    # Current git branch for this session, parsed from the transcript's
    # ``gitBranch`` field by JsonlParser and exposed via
    # ``get_session_metadata()``. None when the transcript has no branch
    # row yet (e.g. non-git cwd or transcript not written). UI shows it
    # next to the cwd in the active card's context row.
    git_branch: str | None = None
    # Seconds since this session's last hook event / activity, computed at
    # build time (now - live.last_hook_at, falling back to last_activity).
    # Drives the "stuck" derived state ("no new tokens · Ns") when an
    # active session goes silent. None when no timestamp is available.
    seconds_since_token: float | None = None
```

- [ ] **Step 1.4: Project the two keys**

In `claude_island/ui/snapshot_projection.py`, inside the `_session(v)` return dict (after the `"turn_count"` / `"elapsed_s"` entries), add:
```python
        "git_branch": v.git_branch,
        "seconds_since_token": int(v.seconds_since_token or 0),
```

- [ ] **Step 1.5: Run the projection test to verify it passes**

Run: `pytest tests/ui/test_snapshot_projection.py -v`
Expected: PASS (3 passed)

- [ ] **Step 1.6: Commit**

```bash
git add claude_island/core/snapshot.py claude_island/ui/snapshot_projection.py tests/ui/test_snapshot_projection.py
git commit -m "feat(core): add git_branch + seconds_since_token to SessionView projection"
```

### Task 2: Populate the two fields in `compose_session_view()`

**Files:**
- Modify: `claude_island/core/snapshot.py` (`compose_session_view`, meta-read ~575–595, elapsed-compute ~704–741, `return SessionView(...)` ~753–778, `_has_volatile_time_field` ~1028–1043)

- [ ] **Step 2.1: Extract `git_branch` from meta**

In `compose_session_view`, find the block that reads `meta = ...get_session_metadata(...)` and resolves `name` (~lines 575–595). Right after the `name = (...)` assignment, add:
```python
    git_branch = (
        meta.get("git_branch")
        if isinstance(meta.get("git_branch"), str)
        else None
    )
```

- [ ] **Step 2.2: Compute `seconds_since_token`**

In the same function, find the elapsed-time computation block that sets `last_command_elapsed_s` (~lines 734–741, uses `now_utc` and `live`). Immediately after that block, add:
```python
    # Staleness: seconds since the last hook event (fresher than JSONL,
    # fires between turns). Falls back to last_activity when no live state.
    seconds_since_token: float | None = None
    _ts = getattr(live, "last_hook_at", None) if live is not None else None
    if _ts is None:
        _ts = last_activity
    if _ts is not None:
        try:
            seconds_since_token = (now_utc - _ts).total_seconds()
            if seconds_since_token < 0:
                seconds_since_token = 0.0
        except Exception:
            seconds_since_token = None
```
NOTE: confirm the local variable holding the session's activity datetime is named `last_activity` in this scope (it is the value assigned to `SessionView.last_activity`). If it is named differently, use that name.

- [ ] **Step 2.3: Pass both to the constructor**

In the `return SessionView(...)` statement (~753–778), after `last_command_elapsed_s=last_command_elapsed_s,` add:
```python
        git_branch=git_branch,
        seconds_since_token=seconds_since_token,
```

- [ ] **Step 2.4: Mark `seconds_since_token` volatile (cache bypass)**

In `_has_volatile_time_field(v)` (~1028–1043), add a clause so views recompose every build:
```python
        or v.seconds_since_token is not None
```

- [ ] **Step 2.5: Run the full core + ui test suite (regression — adding optional fields must not break anything)**

Run: `pytest tests/core tests/ui -q`
Expected: PASS (all green; the new optional fields default to None so existing fixtures are unaffected).

- [ ] **Step 2.6: Verify architecture layering is intact**

Run: `python -m import_linter`
Expected: `Contracts: N kept, 0 broken.`

- [ ] **Step 2.7: Commit**

```bash
git add claude_island/core/snapshot.py
git commit -m "feat(core): populate git_branch + seconds_since_token in compose_session_view"
```

## Phase 2: Bundle the display font + extend Theme tokens

### Task 3: Bundle Bricolage Grotesque

**Files:**
- Create: `claude_island/ui/qml/fonts/BricolageGrotesque[opsz,wdth,wght].ttf`
- (No code change — `qml_app.py:181–194` already globs `fonts/*.ttf` and registers each via `QFontDatabase.addApplicationFont`.)

- [ ] **Step 3.1: Download the OFL variable font into the fonts dir**

Run:
```bash
cd /Users/constantine/coding-projects/claude-island
curl -L -o "claude_island/ui/qml/fonts/BricolageGrotesque[opsz,wdth,wght].ttf" \
  "https://github.com/google/fonts/raw/main/ofl/bricolagegrotesque/BricolageGrotesque%5Bopsz%2Cwdth%2Cwght%5D.ttf"
```
Expected: a ~250–400 KB `.ttf` file in `claude_island/ui/qml/fonts/`.

- [ ] **Step 3.2: Verify the family name registers as "Bricolage Grotesque"**

Run:
```bash
python -c "
from PySide6.QtGui import QGuiApplication, QFontDatabase
app = QGuiApplication([])
from pathlib import Path
p = list(Path('claude_island/ui/qml/fonts').glob('Bricolage*'))[0]
fid = QFontDatabase.addApplicationFont(str(p))
print(QFontDatabase.applicationFontFamilies(fid))
"
```
Expected output includes: `['Bricolage Grotesque']`. If the printed family differs, use that exact string for `Theme.fontDisplay` in Task 4.

- [ ] **Step 3.3: Commit**

```bash
git add "claude_island/ui/qml/fonts/BricolageGrotesque[opsz,wdth,wght].ttf"
git commit -m "chore(ui): bundle Bricolage Grotesque display font"
```

### Task 4: Extend `Theme.qml` with phase colors, display font, and helpers

**Files:**
- Modify: `claude_island/ui/qml/Theme.qml`

- [ ] **Step 4.1: Add tokens + helpers**

In `claude_island/ui/qml/Theme.qml`, inside the `QtObject{ ... }`:

(a) After the existing color block (line 9), add the redesign phase palette:
```qml
    // ── Redesign phase palette ──
    readonly property color pRunning:"#5fe0b4"   // tool_use (running)
    readonly property color pThinking:"#9a8cff"  // thinking
    readonly property color pCompact:"#e6b96e"   // compacting
    readonly property color pWaiting:"#f3b95e"   // waiting_approval
    readonly property color pStuck:"#e0795a"     // derived stuck
    readonly property color quotaFill:"#e0b86a"  // TODAY quota bar (gold, was teal)
    readonly property color costDim:"#bf9056"    // dimmed cost in active card
```

(b) After the font block (line 16), add:
```qml
    readonly property string fontDisplay: "Bricolage Grotesque"
    // staleness threshold (seconds) past which an active session reads "stuck"
    readonly property int stuckAfterS: 18
```

(c) After `phaseColor()` (line 17), add the redesign helpers:
```qml
    function isStuck(p, secs){ return (p==="tool_use" || p==="thinking") && secs >= stuckAfterS }
    function railColor(p, secs){
        if (isStuck(p, secs)) return pStuck
        if (p==="thinking") return pThinking
        if (p==="compacting") return pCompact
        if (p==="waiting_approval") return pWaiting
        return pRunning
    }
    function phaseLabel(p, secs){
        if (isStuck(p, secs)) return "stuck"
        if (p==="tool_use") return "running"
        if (p==="waiting_approval") return "awaiting approval"
        return p   // "thinking" / "compacting"
    }
```

- [ ] **Step 4.2: Verify QML still loads with zero warnings**

Run: `pytest tests/ui/test_qml_no_warnings.py -v`
Expected: PASS (Theme is a singleton; new readonly props + functions don't break bindings).

- [ ] **Step 4.3: Commit**

```bash
git add claude_island/ui/qml/Theme.qml
git commit -m "feat(ui): add phase palette, display font + rail/label helpers to Theme"
```

---

## Phase 3: Redesign the ACTIVE card delegate in `Main.qml`

This replaces the "Live Console" card (lines 704–878). Build it in three commits: (A) the new layout shell + context row, (B) the per-phase indicator Canvas, (C) the stuck/waiting state treatment.

### Task 5A: Replace the delegate with the new containerless layout

**Files:**
- Modify: `claude_island/ui/qml/Main.qml` (the `Repeater { model: root.vmSessions; delegate: Item { ... } }` at lines 704–878)

- [ ] **Step 5A.1: Replace the delegate Item (lines 704–878) with the new layout**

Replace the entire `Repeater { ... }` block (704–878) with:
```qml
                                Repeater {
                                    model: root.vmSessions
                                    delegate: Item {
                                        required property var modelData
                                        // local derived helpers
                                        readonly property string phz: modelData.phase || ""
                                        readonly property int secs: modelData.seconds_since_token || 0
                                        readonly property color ac: Theme.railColor(phz, secs)
                                        readonly property bool stuck: Theme.isStuck(phz, secs)

                                        visible: root.isActive(phz)
                                        Layout.fillWidth: true
                                        Layout.leftMargin: 13; Layout.rightMargin: 13
                                        Layout.topMargin: 4; Layout.bottomMargin: 9
                                        implicitHeight: visible ? actCard.implicitHeight : 0

                                        // containerless surface: faint phase-tinted elevation, no border
                                        Rectangle {
                                            id: actCard
                                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                                            radius: 14
                                            implicitHeight: actCol.implicitHeight + 24
                                            // elevation gradient anchored to the rail (skip when stuck = cooled)
                                            gradient: Gradient {
                                                orientation: Gradient.Horizontal
                                                GradientStop { position: 0.0; color: Qt.rgba(ac.r, ac.g, ac.b, stuck ? 0.05 : 0.06) }
                                                GradientStop { position: 0.45; color: Qt.rgba(ac.r, ac.g, ac.b, 0.012) }
                                                GradientStop { position: 0.75; color: "transparent" }
                                            }

                                            // left rail (phase color, glow; dimmed when stuck)
                                            Rectangle {
                                                id: rail
                                                anchors.left: parent.left; anchors.leftMargin: 4
                                                anchors.top: parent.top; anchors.topMargin: 12
                                                anchors.bottom: parent.bottom; anchors.bottomMargin: 12
                                                width: 3; radius: 2; color: ac
                                                opacity: stuck ? 0.6 : 1.0
                                                Rectangle {  // soft glow
                                                    anchors.fill: parent; radius: 2; color: ac
                                                    visible: !stuck; opacity: 0.5
                                                    layer.enabled: true
                                                }
                                            }

                                            ColumnLayout {
                                                id: actCol
                                                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                                                anchors.leftMargin: 19; anchors.rightMargin: 16; anchors.topMargin: 12
                                                spacing: 0

                                                // ── top: indicator + (phase·elapsed / name·model) ──
                                                RowLayout {
                                                    Layout.fillWidth: true; spacing: 14
                                                    PhaseIndicator {        // defined in Task 5B (inline component)
                                                        Layout.preferredWidth: 46; Layout.preferredHeight: 46
                                                        phase: phz; stuck: parent.parent ? false : false  // bound below
                                                        ac: parent ? ac : ac
                                                        rate: modelData.tokens_per_min || 0
                                                        running: root.isActive(phz)
                                                    }
                                                    ColumnLayout {
                                                        Layout.fillWidth: true; spacing: 6
                                                        RowLayout {
                                                            Layout.fillWidth: true; spacing: 0
                                                            Text {
                                                                text: Theme.phaseLabel(phz, secs)
                                                                color: ac; font.family: Theme.fontMono
                                                                font.pixelSize: Theme.tMicro; font.letterSpacing: 1.5
                                                                font.bold: true; font.capitalization: Font.AllUppercase
                                                            }
                                                            Item { Layout.fillWidth: true }
                                                            Text {
                                                                text: root.fmtElapsed(modelData.elapsed_s)
                                                                visible: (modelData.elapsed_s || 0) > 0
                                                                color: Theme.faint; font.family: Theme.fontMono; font.pixelSize: Theme.tMeta
                                                            }
                                                        }
                                                        Text {
                                                            Layout.fillWidth: true
                                                            textFormat: Text.StyledText
                                                            text: (modelData.name || "") +
                                                                  (modelData.model ? " <font color='#6b7280'>· " + modelData.model + "</font>" : "")
                                                            color: "#f0f3f6"
                                                            font.family: Theme.fontDisplay
                                                            font.weight: Font.DemiBold
                                                            font.pixelSize: 20; elide: Text.ElideRight
                                                        }
                                                    }
                                                }

                                                // ── stuck warning line (only when stuck) ──
                                                Text {
                                                    Layout.fillWidth: true; Layout.topMargin: 11
                                                    visible: stuck
                                                    text: "⚠ no new output · " + secs + "s — open the terminal"
                                                    color: Theme.pStuck; font.family: Theme.fontMono; font.pixelSize: Theme.tMeta
                                                }

                                                // ── context row: cwd · branch · cost ──
                                                RowLayout {
                                                    Layout.fillWidth: true; Layout.topMargin: 13; spacing: 14
                                                    Text {
                                                        textFormat: Text.StyledText
                                                        text: "<font color='#5b636d'>" + root.cwdParent(modelData.cwd) + "</font>" + root.cwdLeaf(modelData.cwd)
                                                        color: "#8b94a0"; font.family: Theme.fontMono; font.pixelSize: Theme.tMeta; elide: Text.ElideMiddle
                                                    }
                                                    RowLayout {
                                                        spacing: 6; visible: (modelData.git_branch || "") !== ""
                                                        Canvas {  // tiny git-branch glyph
                                                            width: 12; height: 13
                                                            onPaint: {
                                                                var c = getContext("2d"); c.clearRect(0,0,12,13)
                                                                c.strokeStyle = "#5a6168"; c.lineWidth = 1.3; c.lineCap = "round"
                                                                c.beginPath(); c.arc(3,3,1.7,0,6.3); c.moveTo(4.7,10); c.arc(3,10,1.7,0,6.3)
                                                                c.moveTo(10.7,3); c.arc(9,3,1.7,0,6.3); c.stroke()
                                                                c.beginPath(); c.moveTo(3,4.7); c.lineTo(3,8.3)
                                                                c.moveTo(9,4.7); c.bezierCurveTo(9,7.1,3,6.3,3,8.3); c.stroke()
                                                            }
                                                        }
                                                        Text { text: modelData.git_branch || ""; color: "#9aa3ad"; font.family: Theme.fontMono; font.pixelSize: Theme.tMeta }
                                                    }
                                                    Item { Layout.fillWidth: true }
                                                    Text {
                                                        text: root.fmtCost(modelData.cost_usd)
                                                        color: Theme.costDim; font.family: Theme.fontMono; font.bold: true; font.pixelSize: Theme.tBody
                                                    }
                                                }
                                            }

                                            MouseArea {
                                                anchors.fill: parent; cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                                onClicked: (mouse) => {
                                                    if (mouse.button === Qt.RightButton) {
                                                        root.detailData = root.vm ? root.vm.sessionDetail(modelData.id) : {}
                                                        detailHost.open("session", actCard)
                                                    } else if (root.vm) { root.vm.focusSession(modelData.id) }
                                                }
                                            }
                                        }
                                    }
                                }
```

- [ ] **Step 5A.2: Add the two cwd-helper functions to `root`**

In `Main.qml`, next to the existing helper functions (near `isActive`/`fmtElapsed`, ~lines 50–122), add:
```qml
    function cwdParent(p){ p = (p||""); p = p.replace(/^\/Users\/[^/]+/, "~"); var i = p.lastIndexOf("/"); return i > 0 ? p.slice(0, i+1) : "" }
    function cwdLeaf(p){ p = (p||""); var i = p.lastIndexOf("/"); return i >= 0 ? p.slice(i+1) : p }
```

- [ ] **Step 5A.3: Temporarily stub `PhaseIndicator`** so the file parses before Task 5B

Add a placeholder inline component near the top of the file's root `Item` (it will be fully implemented in 5B). For now:
```qml
    component PhaseIndicator: Item {
        property string phase: ""
        property bool stuck: false
        property color ac: "#5fe0b4"
        property real rate: 0
        property bool running: false
        Rectangle { anchors.centerIn: parent; width: 11; height: 11; radius: 6; color: ac }
    }
```

- [ ] **Step 5A.4: Verify it loads with zero warnings**

Run: `pytest tests/ui/test_qml_no_warnings.py -v`
Expected: PASS. (If it fails on `modelData.seconds_since_token`/`git_branch` being undefined, the snapshot fixture in the test lacks the keys — fix in Task 8; for now confirm no *binding* errors on the new layout.)

- [ ] **Step 5A.5: Commit**

```bash
git add claude_island/ui/qml/Main.qml
git commit -m "feat(ui): containerless active card layout (name/cwd/branch, no command)"
```

### Task 5B: Implement the 5 phase indicators (Canvas)

**Files:**
- Modify: `claude_island/ui/qml/Main.qml` (replace the `PhaseIndicator` stub from 5A.3)

- [ ] **Step 5B.1: Replace the `PhaseIndicator` stub with the full Canvas indicator**

```qml
    component PhaseIndicator: Item {
        id: pi
        property string phase: ""
        property bool stuck: false
        property color ac: "#5fe0b4"
        property real rate: 0
        property bool running: false
        // 0→1 motion driver; speed nudged by token rate (faster work = quicker)
        property real t: 0
        NumberAnimation on t {
            from: 0; to: 1; loops: Animation.Infinite
            duration: pi.stuck ? 100000 : Math.max(700, 1800 - Math.min(1000, pi.rate/8))
            running: pi.running && !pi.stuck
        }
        onTChanged: cv.requestPaint()
        onAcChanged: cv.requestPaint()
        onStuckChanged: cv.requestPaint()
        onPhaseChanged: cv.requestPaint()

        Canvas {
            id: cv; anchors.fill: parent
            onPaint: {
                var c = getContext("2d"); var W = width, H = height, cx = W/2, cy = H/2
                c.clearRect(0,0,W,H)
                c.lineCap = "round"; c.lineJoin = "round"
                var col = pi.ac
                c.strokeStyle = col; c.fillStyle = col
                if (pi.stuck) {                                   // hollow ring, no pulse
                    c.globalAlpha = 0.14; c.lineWidth = 1.2; c.beginPath(); c.arc(cx,cy,19,0,6.2832); c.stroke()
                    c.globalAlpha = 0.7;  c.lineWidth = 1.8; c.beginPath(); c.arc(cx,cy,6,0,6.2832); c.stroke()
                    c.globalAlpha = 1; return
                }
                var pulse = 0.5 + 0.5*Math.sin(pi.t*6.2832)       // 0..1 breath
                if (pi.phase === "tool_use") {                    // running: dot + halo + ring
                    c.globalAlpha = 0.14; c.lineWidth = 1.2; c.beginPath(); c.arc(cx,cy,20.5,0,6.2832); c.stroke()
                    c.globalAlpha = 0.10; c.lineWidth = 6;   c.beginPath(); c.arc(cx,cy,11,0,6.2832); c.stroke()
                    c.globalAlpha = 1;    c.beginPath(); c.arc(cx,cy,5+pulse*0.8,0,6.2832); c.fill()
                } else if (pi.phase === "thinking") {             // spark, slow rotate
                    c.save(); c.translate(cx,cy); c.rotate(pi.t*0.7)
                    c.globalAlpha = 0.9; c.lineWidth = 2.6
                    c.beginPath(); c.moveTo(0,-19); c.lineTo(0,19); c.moveTo(-19,0); c.lineTo(19,0); c.stroke()
                    c.lineWidth = 2
                    c.beginPath(); c.moveTo(-13,-13); c.lineTo(13,13); c.moveTo(13,-13); c.lineTo(-13,13); c.stroke()
                    c.globalAlpha = 0.24; c.lineWidth = 1.7; c.rotate(0.33)
                    c.beginPath(); c.moveTo(0,-14); c.lineTo(0,14); c.moveTo(-14,0); c.lineTo(14,0); c.stroke()
                    c.restore()
                } else if (pi.phase === "compacting") {           // 3 rings + inward ticks
                    c.globalAlpha = 0.13; c.lineWidth = 1.3; c.beginPath(); c.arc(cx,cy,20,0,6.2832); c.stroke()
                    c.globalAlpha = 0.34; c.lineWidth = 1.6; c.beginPath(); c.arc(cx,cy,13,0,6.2832); c.stroke()
                    c.globalAlpha = 1;    c.lineWidth = 2;   c.beginPath(); c.arc(cx,cy,6.5,0,6.2832); c.stroke()
                    c.globalAlpha = 0.6; c.lineWidth = 1.5
                    c.beginPath(); c.moveTo(cx,3.5); c.lineTo(cx,7); c.moveTo(cx,H-3.5); c.lineTo(cx,H-7)
                    c.moveTo(3.5,cy); c.lineTo(7,cy); c.moveTo(W-3.5,cy); c.lineTo(W-7,cy); c.stroke()
                } else if (pi.phase === "waiting_approval") {     // "!" in ring (signal only)
                    c.globalAlpha = 0.14; c.lineWidth = 1.2; c.beginPath(); c.arc(cx,cy,22,0,6.2832); c.stroke()
                    c.globalAlpha = 0.5;  c.lineWidth = 1.6; c.beginPath(); c.arc(cx,cy,17.5,0,6.2832); c.stroke()
                    c.globalAlpha = 0.55 + 0.45*pulse
                    c.fillRect(cx-1.8,cy-10.5,3.6,12.5); c.beginPath(); c.arc(cx,cy+7.8,2.1,0,6.2832); c.fill()
                }
                c.globalAlpha = 1
            }
        }
    }
```

- [ ] **Step 5B.2: Visual smoke — render Main.qml in all active phases (no warnings)**

Run: `pytest tests/ui/test_qml_no_warnings.py -v`
Expected: PASS (Canvas paint runs without binding errors).

- [ ] **Step 5B.3: Commit**

```bash
git add claude_island/ui/qml/Main.qml
git commit -m "feat(ui): per-phase Canvas indicators (running/thinking/compacting/waiting/stuck)"
```

## Phase 4: Panel coherence pass

### Task 6: Free the green channel + soften chrome in `Main.qml`

**Files:**
- Modify: `claude_island/ui/qml/Main.qml` (TODAY card quota bar ~1167–1212; ACTIVE/IDLE section spacing ~681–702, 882–900)

- [ ] **Step 6.1: Recolor the TODAY quota bar from teal → gold**

In the TODAY card's quota progress bar (~lines 1173–1189), change the FILL rectangle's `color: Theme.teal` to `color: Theme.quotaFill`, and its soft-glow border `Qt.rgba(0.37, 0.82, 0.66, 0.5)` to `Qt.rgba(0.88, 0.72, 0.41, 0.45)`. In the quota row below (~1192–1212), change the `"% of 5h"` Text `color: Theme.teal` to `color: Theme.quotaFill`. (This leaves vivid teal as the ACTIVE session's exclusive color — fixes the green-on-green collision flagged in design review.)

- [ ] **Step 6.2: Keep ACTIVE the only glowing-green element**

Confirm the ACTIVE section-header dot (lines 687–692) stays `Theme.teal` (it should — that's the live accent). No change needed; this step is a visual confirmation when you run the app in Task 9.

- [ ] **Step 6.3: Verify zero warnings**

Run: `pytest tests/ui/test_qml_no_warnings.py -v`
Expected: PASS

- [ ] **Step 6.4: Commit**

```bash
git add claude_island/ui/qml/Main.qml
git commit -m "feat(ui): coherence pass — gold quota bar frees green for the active session"
```

---

## Phase 5: Collapsed floating pill alignment

### Task 7: Align the collapsed pill content to the redesign

**Files:**
- Modify: `claude_island/ui/qml/Main.qml` (collapsed pill content ~193–266)

The pill already renders: breathing dot + status text + a 4-bar mini equalizer + cost, and (when `vmDecisions.length > 0`) `"{name} needs you"`. The redesign keeps this; only the working-state text changes to lead with the active session name.

- [ ] **Step 7.1: Lead the pill text with the active session name when exactly one is running**

In the collapsed pill status `Text` (~lines 227–238), change the working-state text expression so a single running session shows its name + verb, falling back to the count for multiples:
```qml
                    text: root.vmDecisions.length > 0
                          ? (root.vmDecisions[0].session_name + " needs you")
                          : (root.workingCount() === 1
                                ? (root.activeName() + "  running")
                                : (root.workingCount() + " running"))
```
Add the `activeName()` helper to `root` (near the other helpers):
```qml
    function activeName(){ for (var i=0;i<vmSessions.length;i++){ if (isActive(vmSessions[i].phase)) return vmSessions[i].name } return "" }
```
(The dot color, equalizer, cost, drag behavior, and the `needs you` decision path are unchanged — the dedicated DecisionCard still owns approvals.)

- [ ] **Step 7.2: Verify zero warnings across all island states (collapsed/decision/expanded)**

Run: `pytest tests/ui/test_qml_no_warnings.py -v`
Expected: PASS (the test drives `islandState` through collapsed/decision/expanded).

- [ ] **Step 7.3: Commit**

```bash
git add claude_island/ui/qml/Main.qml
git commit -m "feat(ui): collapsed pill leads with the running session name"
```

---

## Phase 6: Verification

### Task 8: Extend the QML binding test to exercise all 5 states + new fields

**Files:**
- Modify: `tests/ui/test_qml_no_warnings.py` (the snapshot fixture `_full_snap(...)` and its active-session dicts)

- [ ] **Step 8.1: Add the new keys + a stuck and a waiting session to the fixture**

In `_full_snap(...)`, ensure each active-session dict in the snapshot includes the new keys, and add two extra sessions covering the edge states:
```python
# every active session dict gains:
#   "git_branch": "master",
#   "seconds_since_token": 3,
# plus add these two sessions to the active list:
{ "id": "s-stuck", "name": "stuck-repo", "phase": "tool_use",
  "cwd": "/Users/me/coding-projects/stuck-repo", "git_branch": "main",
  "cost_usd": 0.36, "model": "opus-4.8", "tokens_per_min": 0,
  "rate_series": [], "elapsed_s": 40, "seconds_since_token": 22,
  "current_tool_input": "", "turn_count": 3, "command": "" },
{ "id": "s-wait", "name": "review-pr", "phase": "waiting_approval",
  "cwd": "/Users/me/coding-projects/review-pr", "git_branch": "feat/x",
  "cost_usd": 0.12, "model": "sonnet-4.6", "tokens_per_min": 0,
  "rate_series": [], "elapsed_s": 12, "seconds_since_token": 5,
  "current_tool_input": "", "turn_count": 1, "command": "" },
```
(Match the exact dict shape the existing fixture already uses for active sessions — copy an existing one and add the two new keys. The two extra sessions force the QML to bind the `stuck` and `waiting_approval` branches of `PhaseIndicator` and the stuck warning line.)

- [ ] **Step 8.2: Run the QML test**

Run: `pytest tests/ui/test_qml_no_warnings.py -v`
Expected: PASS — zero binding errors / zero un-whitelisted warnings while rendering all 5 phases.

- [ ] **Step 8.3: Commit**

```bash
git add tests/ui/test_qml_no_warnings.py
git commit -m "test(ui): exercise all 5 active phases + new session fields in QML smoke test"
```

### Task 9: Visual verification against the design sheet

**Files:**
- Create: `scripts/preview_active.py` (dev-only renderer + screenshot)

- [ ] **Step 9.1: Write a synthetic-snapshot preview that screenshots the panel**

Create `scripts/preview_active.py` that loads `Main.qml` with a fake `worldVm` exposing one active session per phase (running/thinking/compacting/waiting_approval + one stuck), sets `islandState="expanded"`, and saves a PNG via `window.grabWindow()`. Base it on the engine-setup + fixture in `tests/ui/test_qml_no_warnings.py` (reuse `_full_snap` shape and the context-property wiring at lines 308–314). Save the grab to `/tmp/island_preview.png`.

- [ ] **Step 9.2: Run the preview and open the screenshot**

Run:
```bash
python scripts/preview_active.py && open /tmp/island_preview.png
```
Expected: a PNG of the expanded panel.

- [ ] **Step 9.3: Compare to the design sheet — checklist**

Open `design/2026-05-island-redesign/active-redesign/04-design-sheet.html` side-by-side and confirm:
- [ ] active card is containerless with a glowing phase-colored left rail and a faint elevation tint (not a bordered box)
- [ ] name renders in **Bricolage Grotesque** (rounded, distinct from the Inter elsewhere); NO `$ command` line anywhere
- [ ] context row shows `~/…/<repo>` + branch glyph + branch name + dimmed cost
- [ ] running = filled dot/halo/ring (teal) · thinking = spark (violet) · compacting = rings+ticks (gold) · waiting = `!` ring (amber, no buttons) · stuck = hollow ring (coral) + "no new output · Ns" line
- [ ] TODAY quota bar is **gold** (only the active session is vivid green)
- [ ] collapsed pill leads with the running session name

- [ ] **Step 9.4: Live run (real sessions)**

Run: `python -m claude_island`
Drive a real Claude Code session through phases (start a task → thinking; tool call → running; trigger a permission prompt → the top DecisionCard appears AND the session card shows amber `awaiting approval`; let it sit idle mid-task → stuck after ~18s). Confirm each matches the checklist. Fix any deviation, re-run Task 8 + 9.

- [ ] **Step 9.5: Commit the preview script**

```bash
git add scripts/preview_active.py
git commit -m "test(ui): synthetic-snapshot preview screenshot for visual verification"
```

### Task 10: Full regression gate

- [ ] **Step 10.1: Run everything**

Run:
```bash
pytest tests/ -q && python -m import_linter
```
Expected: all tests pass; `Contracts: N kept, 0 broken.`

- [ ] **Step 10.2: Self-review the diff**

Run: `git diff master --stat` and skim the diff for debug code / stray changes. Confirm only the files in the File Structure table changed.

---

## Self-Review (plan author's checklist — completed)

**Spec coverage:** every design-contract row maps to a task — data (Task 1–2), font+theme (Task 3–4), layout+indicators+states (Task 5A/5B), coherence/gold-quota (Task 6), pill (Task 7), verification (Task 8–10). ✅

**Known follow-ups / risks to watch during execution (not blockers):**
1. **`live.last_hook_at`** (Task 2.2) — confirm this attribute exists on `SessionLiveState`; if the attribute is named differently (e.g. `last_event_at`), use that. The "stuck" state degrades gracefully (just never fires) if the timestamp is missing.
2. **Variable-font weight** (Task 4/5A) — if `font.weight: Font.DemiBold` on the variable Bricolage renders too light/heavy, set an explicit `font.weight: 600`. If the variable font doesn't expose weights cleanly under QtQuick, download the static `BricolageGrotesque-SemiBold.ttf` instead (same Google Fonts dir) and reference family "Bricolage Grotesque SemiBold".
3. **`rail` glow** (Task 5A) — `layer.enabled` alone won't blur; if a real glow is wanted, wrap with `Qt5Compat.GraphicalEffects` `Glow`/`DropShadow` (already used elsewhere in this repo per `Qt5Compat` import), or accept the solid rail (the elevation tint already conveys "lit"). Keep it simple first.
4. **`stuck` vs `compacting`/`waiting`** — `isStuck()` deliberately excludes `compacting` and `waiting_approval` (those are legitimately quiet). Confirmed in `Theme.isStuck`.

---


