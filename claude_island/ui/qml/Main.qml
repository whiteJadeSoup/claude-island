import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtQuick.Controls
import "."

// The root Window resizes to match islandState so the OS window itself
// shrinks to pill size when collapsed — no transparent screen-occupying
// region left behind. rootRect fills the window and provides the chrome.
Window {
    id: root
    // Window dimensions driven by islandState. Behavior animates the OS
    // window resize so the morph is visible at the OS level, not just inside.
    width:  islandState === "collapsed" ? 240 : 480
    height: islandState === "collapsed" ? 44  : (islandState === "decision" ? 200 : 460)
    Behavior on width  { NumberAnimation { duration: 340; easing.type: Easing.OutCubic } }
    Behavior on height { NumberAnimation { duration: 340; easing.type: Easing.OutCubic } }
    visible: true
    // On macOS Qt.Tool maps to NSPanel which silently refuses to paint a
    // WA_TranslucentBackground surface — the window reports isVisible=True
    // but nothing reaches the screen.  The existing CapsuleWindow._setup_window
    // drops Qt.Tool on darwin for the same reason (see capsule_window.py).
    // isMac is injected from qml_app.py via engine.rootContext().
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
           (typeof isMac !== "undefined" && isMac ? 0 : Qt.Tool)
    color: "transparent"

    // ── Detail navigation: touch-to-grow morph (replaces the old `page`
    // x-slide). A tapped source element (TODAY card / session card / History
    // button) makes its detail page GROW from that element's position+size
    // into a full overlay (detailHost below); detailHost.close() collapses it
    // back. The home content stays the static base layer underneath. ───────
    // SpendPage data — populated before detailHost.open("spend", …)
    property var spendData: ({})
    // SessionDetailPage data — populated before detailHost.open("session", …)
    property var detailData: ({})

    // ── Today card data — fetched from spendDetail() and kept fresh ───────
    property var today: ({})

    // ── Null-safe VM accessors ─────────────────────────────────────────────
    readonly property var vm:           worldVm || null
    readonly property var vmSessions:   (vm && vm.sessions)  ? vm.sessions  : []
    readonly property var vmDecisions:  (vm && vm.decisions) ? vm.decisions : []
    readonly property string vmTodayCost: vm ? vm.todayCost : "$0.00"
    readonly property int    vmQuotaPct:  vm ? vm.quotaPct  : 0
    readonly property var vmQuota:      (vm && vm.quota) ? vm.quota : null

    // ── Helpers ───────────────────────────────────────────────────────────
    readonly property var activePhases: ["thinking", "tool_use", "compacting", "waiting_approval"]
    function isActive(p) { return activePhases.indexOf(p) !== -1 }
    function fmtCost(n)  { return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2)) }
    function fmtElapsed(s){ s = s || 0; return s >= 60 ? (Math.floor(s/60) + "m " + (s%60) + "s") : (s + "s") }
    function cwdParent(p){ p = (p||""); p = p.replace(/^\/Users\/[^/]+/, "~"); var i = p.lastIndexOf("/"); return i > 0 ? p.slice(0, i+1) : "" }
    function cwdLeaf(p){ p = (p||""); var i = p.lastIndexOf("/"); return i >= 0 ? p.slice(i+1) : p }

    // Format token counts: ≥1M → "X.YM", ≥1K → "XK", else raw int
    function fmtNum(n) {
        n = n || 0
        if (n >= 1000000) return (n / 1000000).toFixed(1) + "M"
        if (n >= 1000)    return (n / 1000).toFixed(0) + "K"
        return String(n)
    }

    function workingCount() {
        var n = 0
        for (var i = 0; i < vmSessions.length; i++)
            if (isActive(vmSessions[i].phase)) n++
        return n
    }
    function quietCount() { return vmSessions.length - workingCount() }
    function riskColor(risk) {
        if (risk === "high")   return "#e8743b"
        if (risk === "medium") return "#d29922"
        return "#5fd2a8"
    }
    function kindLabel(kind) {
        if (kind === "ask_question")       return "question"
        if (kind === "user_prompt_submit") return "review"
        return "approval"
    }
    // Phase accent colour: amber for thinking, teal for tool_use/other active
    function phaseColor(phase) {
        return phase === "thinking" ? "#f0b860" : "#5fd2a8"
    }

    // Model-family accent so the model chip is identifiable at a glance and
    // visually prominent (#2): opus=gold, sonnet=blue, haiku=green.
    function modelColor(m) {
        m = (m || "").toLowerCase()
        if (m.indexOf("opus")   !== -1) return "#f0a860"
        if (m.indexOf("sonnet") !== -1) return "#7aa2f7"
        if (m.indexOf("haiku")  !== -1) return "#5fd2a8"
        return "#8a96a3"
    }

    // Live "now", re-stamped every 30s, so relative quota-reset countdowns
    // ("resets in 1h 38m") tick down on their own between snapshots.
    property double nowMs: 0
    Timer {
        interval: 30000; repeat: true; running: true
        onTriggered: root.nowMs = Date.now()
    }
    // "resets in 1h 38m" from an epoch-ms value (the projection emits
    // *_reset_epoch). 0/missing → "resets in —"; elapsed → "resets now".
    function fmtReset(epochMs) {
        if (!epochMs) return "resets in —"
        var rem = epochMs - root.nowMs
        if (rem <= 0) return "resets now"
        var mins = Math.floor(rem / 60000)
        var h = Math.floor(mins / 60)
        var m = mins % 60
        if (h > 0) return "resets in " + h + "h " + m + "m"
        if (m > 0) return "resets in " + m + "m"
        return "resets in <1m"
    }
    function quotaResetEpoch() {
        return (vmQuota && vmQuota["five_hour_reset_epoch"]) ? vmQuota["five_hour_reset_epoch"] : 0
    }
    // Live tail prefix for the current_tool_input display
    function tailLine(s) {
        if (!s) return ""
        var ti = s.current_tool_input || ""
        if (ti) return (s.phase === "thinking" ? "╰ " : "$ ") + ti
        // Fallback to phase label when no command is running
        return s.phase || ""
    }

    // Per-phase animated indicator for the ACTIVE card. Full Canvas version
    // lands in commit 5B; this is a placeholder stub so 5A loads clean.
    component PhaseIndicator: Item {
        property string phase: ""
        property bool stuck: false
        property color ac: "#5fe0b4"
        property real rate: 0
        property bool running: false
        Rectangle { anchors.centerIn: parent; width: 11; height: 11; radius: 6; color: ac }
    }

    // ── Island state: "expanded" | "collapsed" | "decision" ──────────────
    property string islandState: "expanded"

    // ── Auto-transition: decision drains → collapsed ───────────────────────
    Connections {
        target: root.vm
        function onChanged() {
            if (root.islandState === "decision" && root.vmDecisions.length === 0)
                root.islandState = "collapsed"
            // Refresh Today data on every VM change so the card stays live
            if (root.vm && root.islandState === "expanded")
                root.today = root.vm.spendDetail()
        }
    }

    // Initial Today data fetch when the engine first connects worldVm
    Component.onCompleted: {
        root.nowMs = Date.now()
        if (root.vm) root.today = root.vm.spendDetail()
    }

    // ── Root island rectangle (chrome) ────────────────────────────────────
    // Fills the Window, which now resizes to match islandState directly.
    // The inner size animation has been removed — the OS window resize (driven
    // by the Window Behavior above) is the sole morph animation now.
    Rectangle {
        id: rootRect
        anchors.fill: parent

        radius: 18
        color: "#0c0f14"
        border.color: "#1c2632"
        border.width: 1
        clip: true

        // Ambient top glow — keeps the island from reading as flat black.
        Rectangle {
            z: -1
            width: parent.width * 1.4; height: parent.height * 0.7
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top; anchors.topMargin: -parent.height * 0.35
            radius: width / 2
            color: Theme.teal
            opacity: 0.06
        }

        // ── LAYER 1: Pill (collapsed) ─────────────────────────────────────
        Item {
            id: pillLayer
            anchors.fill: parent
            // Pill content visible only in collapsed state
            opacity: root.islandState === "collapsed" ? 1.0 : 0.0
            enabled: opacity > 0.1
            Behavior on opacity { NumberAnimation { duration: 200 } }

            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    if (root.vmDecisions.length > 0)
                        root.islandState = "decision"
                    else
                        root.islandState = "expanded"
                }
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 10

                // Breathing activity dot
                Rectangle {
                    id: pillDot
                    width: 8; height: 8; radius: 4
                    color: root.vmDecisions.length > 0 ? "#e8743b" : "#5fd2a8"

                    property real dotScale: 1.0
                    transform: Scale {
                        xScale: pillDot.dotScale
                        yScale: pillDot.dotScale
                        origin.x: 4; origin.y: 4
                    }

                    SequentialAnimation on dotScale {
                        loops: Animation.Infinite
                        running: root.islandState === "collapsed"
                        NumberAnimation {
                            to: root.vmDecisions.length > 0 ? 1.25 : 1.15
                            duration: root.vmDecisions.length > 0 ? 300 : 600
                            easing.type: Easing.InOutSine
                        }
                        NumberAnimation {
                            to: 0.8
                            duration: root.vmDecisions.length > 0 ? 300 : 600
                            easing.type: Easing.InOutSine
                        }
                    }
                }

                // Status text
                Text {
                    Layout.fillWidth: true
                    text: root.vmDecisions.length > 0
                          ? (root.vmDecisions[0].session_name + " needs you")
                          : (root.workingCount() + " running · " + root.vmTodayCost)
                    color: root.vmDecisions.length > 0 ? "#f4d0a0" : "#c8d4de"
                    font.pixelSize: 13
                    elide: Text.ElideRight
                }

                // 4-bar mini equalizer — when sessions are working
                Row {
                    spacing: 2
                    visible: root.vmDecisions.length === 0 && root.workingCount() > 0

                    Repeater {
                        model: 4
                        delegate: Rectangle {
                            required property int index
                            width: 3; height: 12; radius: 1; color: "#5fd2a8"

                            property real barH: 6
                            NumberAnimation on barH {
                                loops: Animation.Infinite
                                running: root.islandState === "collapsed"
                                from: 3; to: 14
                                duration: 300 + index * 80
                                easing.type: Easing.InOutSine
                            }

                            transform: Scale {
                                yScale: barH / 12
                                origin.x: 0; origin.y: 6
                            }
                        }
                    }
                }
            }
        }

        // ── LAYER 2: Decision focus ────────────────────────────────────────
        Item {
            id: decisionLayer
            anchors.fill: parent
            opacity: root.islandState === "decision" ? 1.0 : 0.0
            enabled: opacity > 0.1
            Behavior on opacity { NumberAnimation { duration: 200 } }

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                // Mini top bar
                Rectangle {
                    Layout.fillWidth: true; height: 38; color: "transparent"

                    MouseArea {
                        anchors.fill: parent; property point s
                        onPressed:         (m) => { s = Qt.point(m.x, m.y) }
                        onPositionChanged: (m) => { root.x += m.x - s.x; root.y += m.y - s.y }
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 14; anchors.rightMargin: 14

                        Text {
                            text: "Needs you"; color: "#e8884c"
                            font.pixelSize: 12; font.bold: true
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: "Expand"; color: "#7e8a97"; font.pixelSize: 11
                            MouseArea {
                                anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                onClicked: root.islandState = "expanded"
                            }
                        }
                        Text {
                            text: "  ✕"; color: "#566069"; font.pixelSize: 13
                            MouseArea {
                                anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                onClicked: root.islandState = "collapsed"
                            }
                        }
                    }
                }

                // Single decision card
                Loader {
                    Layout.fillWidth: true
                    Layout.leftMargin: 13; Layout.rightMargin: 13
                    active: root.vmDecisions.length > 0 && root.islandState === "decision"
                    visible: active
                    sourceComponent: Component {
                        DecisionCard {
                            decision: root.vmDecisions.length > 0 ? root.vmDecisions[0] : null
                            vm: root.vm
                        }
                    }
                }

                Item { Layout.fillHeight: true }
            }
        }

        // ── LAYER 3: Full expanded panel ──────────────────────────────────
        Item {
            id: panelLayer
            anchors.fill: parent
            opacity: root.islandState === "expanded" ? 1.0 : 0.0
            enabled: opacity > 0.1
            Behavior on opacity { NumberAnimation { duration: 200 } }

            ColumnLayout {
                anchors.fill: parent
                spacing: 0

                // ── Top bar (draggable) ────────────────────────────────────
                Rectangle {
                    Layout.fillWidth: true; height: 44; color: "transparent"

                    MouseArea {
                        anchors.fill: parent; property point s
                        onPressed:         (m) => { s = Qt.point(m.x, m.y) }
                        onPositionChanged: (m) => { root.x += m.x - s.x; root.y += m.y - s.y }
                    }

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 16; anchors.rightMargin: 16

                        Text {
                            text: "Claude Island"; color: "#e9edf2"
                            font.pixelSize: 13; font.bold: true
                        }
                        Item { Layout.fillWidth: true }

                        // #5: the redundant "Today $X · NN%" readout was removed —
                        // the TODAY card directly below already shows cost + quota.

                        // #6: History as a proper pill button (rounded container +
                        // hover fill + counterclockwise "history" glyph), instead of
                        // the bare icon+text that read as unstyled.
                        Rectangle {
                            id: historyBtn
                            implicitWidth: histInner.implicitWidth + 18
                            implicitHeight: 26
                            radius: 7
                            color: histArea.containsMouse ? "#1b2530" : "transparent"
                            border.color: histArea.containsMouse ? "#2a3744" : "transparent"
                            border.width: 1
                            Behavior on color { ColorAnimation { duration: 120 } }

                            RowLayout {
                                id: histInner
                                anchors.centerIn: parent
                                spacing: 5
                                // "History" glyph: clock dial + a counter-clockwise
                                // arrow sweeping back — the universal "rewind time"
                                // / history mark, clearer than a plain clock.
                                Canvas {
                                    id: histGlyph
                                    Layout.preferredWidth: 14; Layout.preferredHeight: 14
                                    property color strokeColor: histArea.containsMouse ? "#c8d4de" : "#8a96a3"
                                    onStrokeColorChanged: requestPaint()
                                    onPaint: {
                                        var ctx = getContext("2d")
                                        ctx.clearRect(0, 0, width, height)
                                        ctx.strokeStyle = strokeColor
                                        ctx.lineWidth = 1.4
                                        ctx.lineCap = "round"; ctx.lineJoin = "round"
                                        // ¾ circle, open at top-left (counter-clockwise gap)
                                        ctx.beginPath()
                                        ctx.arc(7, 7.5, 5, Math.PI * 0.85, Math.PI * 2.55, false)
                                        ctx.stroke()
                                        // arrow head at the open (top-left) end, pointing back
                                        ctx.beginPath()
                                        ctx.moveTo(2.6, 4.0); ctx.lineTo(2.0, 7.4)
                                        ctx.moveTo(2.0, 7.4); ctx.lineTo(5.0, 6.6)
                                        ctx.stroke()
                                        // clock hands
                                        ctx.beginPath()
                                        ctx.moveTo(7, 7.5); ctx.lineTo(7, 4.5)
                                        ctx.moveTo(7, 7.5); ctx.lineTo(9.4, 8.6)
                                        ctx.stroke()
                                    }
                                }
                                Text {
                                    text: "History"
                                    color: histArea.containsMouse ? "#c8d4de" : "#8a96a3"
                                    font.pixelSize: 12
                                }
                            }
                            MouseArea {
                                id: histArea; anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                // Grow the recents overlay out of the History pill itself.
                                onClicked: detailHost.open("recents", historyBtn)
                            }
                        }

                        Item { implicitWidth: 2 }

                        // #4: explicit Quit. Qt.quit() returns from app.exec(),
                        // after which qml_app.main() runs its shutdown (hook_server
                        // .stop() unlinks port.txt) — so this also prevents the
                        // stale-port / duplicate-instance confusion on exit.
                        Rectangle {
                            id: quitBtn
                            implicitWidth: 26; implicitHeight: 26
                            radius: 7
                            color: quitArea.containsMouse ? "#2a1714" : "transparent"
                            Behavior on color { ColorAnimation { duration: 120 } }
                            Canvas {
                                anchors.centerIn: parent
                                width: 14; height: 14
                                property color strokeColor: quitArea.containsMouse ? "#e8743b" : "#566069"
                                onStrokeColorChanged: requestPaint()
                                onPaint: {
                                    var ctx = getContext("2d")
                                    ctx.clearRect(0, 0, width, height)
                                    ctx.strokeStyle = strokeColor
                                    ctx.lineWidth = 1.5
                                    ctx.lineCap = "round"
                                    // power ring with a gap at top
                                    ctx.beginPath()
                                    ctx.arc(7, 8, 4.6, Math.PI * -0.30, Math.PI * 1.30, false)
                                    ctx.stroke()
                                    // power stem
                                    ctx.beginPath()
                                    ctx.moveTo(7, 2.6); ctx.lineTo(7, 7.5)
                                    ctx.stroke()
                                }
                            }
                            MouseArea {
                                id: quitArea; anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                onClicked: Qt.quit()
                            }
                        }

                        // Collapse affordance — self-drawn chevron (the "⌄"
                        // glyph isn't in the bundled fonts; self-draw keeps it
                        // identical on macOS/Windows beside the canvas power +
                        // history icons already in this bar).
                        Item {
                            implicitWidth: 22; implicitHeight: 22
                            Layout.leftMargin: 4
                            Icon {
                                anchors.centerIn: parent
                                name: "chevron-down"; size: 14
                                color: collapseArea.containsMouse ? "#c8d4de" : "#566069"
                            }
                            MouseArea {
                                id: collapseArea; anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                onClicked: root.islandState = "collapsed"
                            }
                        }
                    }
                }

                // ── Page container ─────────────────────────────────────────
                Item {
                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true

                    // ── Home content (static base layer under detailHost) ──
                    // No longer slides: the detail pages now GROW over the top
                    // (detailHost, z:5) instead of x-sliding home off-screen.
                    Item {
                        id: homeContent
                        anchors.fill: parent

                        // ── Empty state: no sessions and no decisions ─────
                        // Shown when there is nothing to display.
                        // Today card still shows above it (inside the Flickable).
                        Item {
                            anchors.fill: parent
                            visible: root.vmSessions.length === 0 && root.vmDecisions.length === 0

                            // Today card at top (empty state variant)
                            TodayCard {
                                id: todayCardEmpty
                                anchors.top: parent.top
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.margins: 0
                                todayData: root.today
                                quotaPct: root.vmQuotaPct
                                vmQuota: root.vmQuota
                                collapsed: false
                            }

                            Column {
                                anchors.centerIn: parent
                                anchors.verticalCenterOffset: 30
                                spacing: 10

                                // Breathing dot — subtle ambient presence indicator
                                Rectangle {
                                    id: emptyDot
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    width: 8; height: 8; radius: 4
                                    color: "#26303c"

                                    property real dotScale: 1.0
                                    transform: Scale {
                                        xScale: emptyDot.dotScale
                                        yScale: emptyDot.dotScale
                                        origin.x: 4; origin.y: 4
                                    }
                                    SequentialAnimation on dotScale {
                                        loops: Animation.Infinite
                                        running: true
                                        NumberAnimation { to: 1.3; duration: 1200; easing.type: Easing.InOutSine }
                                        NumberAnimation { to: 0.7; duration: 1200; easing.type: Easing.InOutSine }
                                    }
                                }

                                Text {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    text: "No active sessions · all quiet"
                                    color: "#3a4752"
                                    font.pixelSize: 12
                                }
                            }
                        }

                        // ── Main scrollable content (sessions + decisions) ─
                        Flickable {
                            anchors.fill: parent
                            contentHeight: bands.implicitHeight
                            clip: true
                            visible: root.vmSessions.length > 0 || root.vmDecisions.length > 0

                            // Thin subtle scrollbar — only appears when content overflows
                            ScrollBar.vertical: ScrollBar {
                                width: 5
                                policy: ScrollBar.AsNeeded
                                contentItem: Rectangle {
                                    implicitWidth: 5
                                    radius: 2
                                    color: "#26303c"
                                    opacity: parent.active ? 0.8 : 0.4
                                }
                                background: Item {}
                            }

                            ColumnLayout {
                                id: bands
                                width: parent.width
                                spacing: 0

                                // ── Decision pending: decision on top, Today collapses ──
                                // FIFO album — interactive front card + ghost-edge peek
                                // of the queue + "第 1 / N 张" counter. Replaces the old
                                // header + single Loader + queued-preview Repeater.
                                DecisionAlbum {
                                    Layout.fillWidth: true
                                    Layout.leftMargin: 13; Layout.rightMargin: 13; Layout.topMargin: 11; Layout.bottomMargin: 4
                                    visible: root.vmDecisions.length > 0
                                    decisions: root.vmDecisions
                                    vm: root.vm
                                }

                                // ── TODAY card (collapsed one-liner when decision pending) ──
                                // Full card shown when no decisions; one-liner when decisions present.
                                Item {
                                    Layout.fillWidth: true
                                    Layout.leftMargin: 13; Layout.rightMargin: 13
                                    Layout.topMargin: root.vmDecisions.length > 0 ? 4 : 11
                                    Layout.bottomMargin: 4
                                    // Height: one-liner (28px) or full card height
                                    implicitHeight: root.vmDecisions.length > 0 ? 28 : todayCardFull.implicitHeight

                                    // Full today card — shown when no decision pending
                                    TodayCard {
                                        id: todayCardFull
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        todayData: root.today
                                        quotaPct: root.vmQuotaPct
                                        vmQuota: root.vmQuota
                                        collapsed: false
                                        visible: root.vmDecisions.length === 0
                                    }

                                    // Collapsed one-liner — shown when decision is pending
                                    Rectangle {
                                        anchors.fill: parent
                                        visible: root.vmDecisions.length > 0
                                        radius: 7
                                        color: "#0a0d12"
                                        border.color: "#151b22"
                                        border.width: 1

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 10; anchors.rightMargin: 10
                                            spacing: 6
                                            // 3x14px vertical accent bar — replaces the calendar emoji
                                            Rectangle {
                                                width: 3; height: 14
                                                radius: 2
                                                color: "#5fd2a8"
                                                Layout.alignment: Qt.AlignVCenter
                                            }
                                            // Outlined "TODAY" chip
                                            Rectangle {
                                                height: 14; radius: 3
                                                color: "transparent"
                                                border.color: "#2a3a30"; border.width: 1
                                                Layout.alignment: Qt.AlignVCenter
                                                implicitWidth: todayChipLabel.implicitWidth + 6
                                                Text {
                                                    id: todayChipLabel
                                                    anchors.centerIn: parent
                                                    text: "TODAY"
                                                    color: "#566069"; font.pixelSize: 8
                                                    font.letterSpacing: 0.5
                                                }
                                            }
                                            Text {
                                                text: root.vmTodayCost
                                                color: "#7e8a97"; font.pixelSize: 10
                                                font.family: Theme.fontMono
                                            }
                                            Text {
                                                visible: (root.today && root.today["total_tokens"]) ? root.today["total_tokens"] > 0 : false
                                                text: "· " + root.fmtNum((root.today && root.today["total_tokens"]) ? root.today["total_tokens"] : 0) + " tok"
                                                color: "#566069"; font.pixelSize: 10
                                            }
                                            Text {
                                                text: "· " + root.vmQuotaPct + "% of 5h"
                                                color: "#566069"; font.pixelSize: 10
                                            }
                                            Text {
                                                visible: root.vmQuota !== null && root.vmQuota !== undefined
                                                text: "· " + root.fmtReset(root.quotaResetEpoch())
                                                color: "#566069"; font.pixelSize: 10
                                                elide: Text.ElideRight
                                                Layout.fillWidth: true
                                            }
                                        }
                                    }
                                }

                                // ── ACTIVE band: "Live Console" cards ─────────
                                // Section header — mirrors the IDLE band (dot + UPPERCASE
                                // label + right-aligned count) to match the prototype.
                                RowLayout {
                                    visible: root.workingCount() > 0
                                    Layout.fillWidth: true
                                    Layout.leftMargin: 16; Layout.rightMargin: 16
                                    Layout.topMargin: 13; Layout.bottomMargin: 6
                                    spacing: 8
                                    Rectangle {
                                        Layout.preferredWidth: 6; Layout.preferredHeight: 6; radius: 3
                                        color: Theme.teal
                                        // soft glow to match the prototype's teal status dot
                                        border.color: Qt.rgba(0.37, 0.84, 0.67, 0.5); border.width: 1
                                    }
                                    Text {
                                        text: "ACTIVE"; color: Theme.teal
                                        font.pixelSize: Theme.tMicro; font.letterSpacing: 1.6; font.bold: true
                                    }
                                    Item { Layout.fillWidth: true }
                                    Text {
                                        text: root.workingCount(); color: Theme.faint
                                        font.pixelSize: Theme.tMicro
                                    }
                                }

                                Repeater {
                                    model: root.vmSessions
                                    delegate: Item {
                                        required property var modelData
                                        readonly property string phz: modelData.phase || ""
                                        readonly property int secs: modelData.seconds_since_token || 0
                                        readonly property color ac: Theme.railColor(phz, secs)
                                        readonly property bool stuck: Theme.isStuck(phz, secs)

                                        visible: root.isActive(phz)
                                        Layout.fillWidth: true
                                        Layout.leftMargin: 13; Layout.rightMargin: 13
                                        Layout.topMargin: 4; Layout.bottomMargin: 9
                                        implicitHeight: visible ? actCard.implicitHeight : 0

                                        Rectangle {
                                            id: actCard
                                            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                                            radius: 14
                                            implicitHeight: actCol.implicitHeight + 24
                                            gradient: Gradient {
                                                orientation: Gradient.Horizontal
                                                GradientStop { position: 0.0; color: Qt.rgba(ac.r, ac.g, ac.b, stuck ? 0.05 : 0.06) }
                                                GradientStop { position: 0.45; color: Qt.rgba(ac.r, ac.g, ac.b, 0.012) }
                                                GradientStop { position: 0.75; color: "transparent" }
                                            }
                                            Rectangle {
                                                id: rail
                                                anchors.left: parent.left; anchors.leftMargin: 4
                                                anchors.top: parent.top; anchors.topMargin: 12
                                                anchors.bottom: parent.bottom; anchors.bottomMargin: 12
                                                width: 3; radius: 2; color: ac
                                                opacity: stuck ? 0.6 : 1.0
                                            }
                                            ColumnLayout {
                                                id: actCol
                                                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                                                anchors.leftMargin: 19; anchors.rightMargin: 16; anchors.topMargin: 12
                                                spacing: 0
                                                RowLayout {
                                                    Layout.fillWidth: true; spacing: 14
                                                    PhaseIndicator {
                                                        Layout.preferredWidth: 46; Layout.preferredHeight: 46
                                                        phase: phz
                                                        stuck: stuck
                                                        ac: ac
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
                                                Text {
                                                    Layout.fillWidth: true; Layout.topMargin: 11
                                                    visible: stuck
                                                    text: "⚠ no new output · " + secs + "s — open the terminal"
                                                    color: Theme.pStuck; font.family: Theme.fontMono; font.pixelSize: Theme.tMeta
                                                }
                                                RowLayout {
                                                    Layout.fillWidth: true; Layout.topMargin: 13; spacing: 14
                                                    Text {
                                                        textFormat: Text.StyledText
                                                        text: "<font color='#5b636d'>" + root.cwdParent(modelData.cwd) + "</font>" + root.cwdLeaf(modelData.cwd)
                                                        color: "#8b94a0"; font.family: Theme.fontMono; font.pixelSize: Theme.tMeta; elide: Text.ElideMiddle
                                                    }
                                                    RowLayout {
                                                        spacing: 6; visible: (modelData.git_branch || "") !== ""
                                                        Canvas {
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

                                // ── IDLE band: compact list ────────────────
                                // Section header row: "IDLE" left, count right.
                                RowLayout {
                                    visible: root.quietCount() > 0
                                    Layout.fillWidth: true
                                    Layout.leftMargin: 16; Layout.rightMargin: 16
                                    Layout.topMargin: 13; Layout.bottomMargin: 6
                                    Text {
                                        text: "IDLE"
                                        color: Theme.faint
                                        font.pixelSize: Theme.tMicro
                                        font.letterSpacing: 1.6
                                        font.bold: true
                                    }
                                    Item { Layout.fillWidth: true }
                                    Text {
                                        text: root.quietCount()
                                        color: Theme.faint
                                        font.pixelSize: Theme.tMicro
                                    }
                                }

                                // Compact list container — one row per idle session,
                                // a 1px top divider between rows (not on the first).
                                Rectangle {
                                    visible: root.quietCount() > 0
                                    Layout.fillWidth: true
                                    Layout.leftMargin: 13; Layout.rightMargin: 13; Layout.bottomMargin: 16
                                    radius: 13
                                    color: Theme.surface
                                    border.color: Theme.bd
                                    border.width: 1
                                    clip: true
                                    implicitHeight: idleCol.implicitHeight

                                    ColumnLayout {
                                        id: idleCol
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.top: parent.top
                                        spacing: 0

                                        Repeater {
                                            model: root.vmSessions
                                            delegate: Item {
                                                id: idleRow
                                                required property var modelData
                                                required property int index
                                                visible: !root.isActive(modelData.phase)
                                                Layout.fillWidth: true
                                                implicitHeight: visible ? (Theme.tBody + 20) : 0

                                                // 1px top divider — skipped on the
                                                // first visible row so the list edge stays clean.
                                                Rectangle {
                                                    anchors.left: parent.left
                                                    anchors.right: parent.right
                                                    anchors.top: parent.top
                                                    height: 1
                                                    color: Theme.bd
                                                    visible: idleRow.index > 0
                                                }

                                                RowLayout {
                                                    anchors.fill: parent
                                                    anchors.leftMargin: 14; anchors.rightMargin: 14
                                                    spacing: 9

                                                    // Dim idle dot
                                                    Rectangle {
                                                        Layout.preferredWidth: 5
                                                        Layout.preferredHeight: 5
                                                        radius: 2.5
                                                        color: "#39414b"
                                                        Layout.alignment: Qt.AlignVCenter
                                                    }
                                                    Text {
                                                        text: modelData.name || ""
                                                        color: Theme.ink2
                                                        font.pixelSize: Theme.tBody
                                                        elide: Text.ElideRight
                                                        Layout.fillWidth: true
                                                        Layout.alignment: Qt.AlignVCenter
                                                    }
                                                    Text {
                                                        // Prototype shows whole-dollar idle cost ("$X").
                                                        text: "$" + ((modelData.cost_usd || 0)).toFixed(0)
                                                        color: Theme.faint
                                                        font.family: Theme.fontMono
                                                        font.bold: true
                                                        font.pixelSize: Theme.tBody
                                                        Layout.alignment: Qt.AlignVCenter
                                                    }
                                                }

                                                MouseArea {
                                                    id: idleArea; anchors.fill: parent
                                                    cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                                    // Left-click: focus terminal; right-click: open detail page
                                                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                                                    onClicked: (mouse) => {
                                                        if (mouse.button === Qt.RightButton) {
                                                            root.detailData = root.vm
                                                                ? root.vm.sessionDetail(modelData.id)
                                                                : {}
                                                            // Grow the session detail out of this row.
                                                            detailHost.open("session", idleRow)
                                                        } else {
                                                            if (root.vm) root.vm.focusSession(modelData.id)
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // ── Detail morph host (touch-to-grow overlay) ──────────
                    // Overlays homeContent (z:5). open(kind, srcItem) records
                    // the source element's position+size (mapped into this
                    // host's coords), then grows the loaded detail page from
                    // that rect to full-fill; close() collapses it back.
                    // Replaces the old per-page x-slide Loaders.
                    Item {
                        id: detailHost
                        objectName: "detailHost"
                        // Reparented to panelLayer so the detail overlay covers the
                        // WHOLE panel — including the "Claude Island" top bar — to
                        // match the prototype's full-layer replacement (each layer
                        // owns its OWN header). Filling only the Page container left
                        // the global top bar + the page's own back row stacked as a
                        // double header, and pushed the session name into a subtitle.
                        //
                        // Reparent ONCE imperatively (not `parent: panelLayer` as a
                        // binding) — a parent *binding* on a lexically-nested item makes
                        // Qt re-evaluate it against the original parent, tripping a
                        // "Binding loop for property parent". onCompleted assigns it
                        // exactly once; anchors.fill: parent then re-resolves to the
                        // new parent automatically.
                        anchors.fill: parent
                        Component.onCompleted: parent = panelLayer
                        z: 10
                        visible: detailLoader.active
                        property string detailKind: ""
                        property real sx0: 0; property real sy0: 0; property real sw0: 0; property real sh0: 0

                        function open(kind, srcItem) {
                            var p = srcItem.mapToItem(detailHost, 0, 0)
                            sx0 = p.x; sy0 = p.y; sw0 = srcItem.width; sh0 = srcItem.height
                            detailKind = kind
                            detailLoader.active = true
                            morphIn.restart()
                        }
                        function close() { morphOut.restart() }

                        Loader {
                            id: detailLoader
                            objectName: "detailLoader"
                            anchors.fill: parent
                            active: false
                            opacity: 0
                            transform: [
                                Scale { id: msc; origin.x: 0; origin.y: 0; xScale: 1; yScale: 1 },
                                Translate { id: mtr; x: 0; y: 0 }
                            ]
                            sourceComponent: detailHost.detailKind === "spend" ? spendComp
                                           : detailHost.detailKind === "session" ? sessionComp
                                           : recentsComp
                        }

                        ParallelAnimation {
                            id: morphIn
                            NumberAnimation { target: detailLoader; property: "opacity"; from: 0; to: 1; duration: 240 }
                            NumberAnimation { target: msc; property: "xScale"; from: (detailHost.width  > 0 ? detailHost.sw0/detailHost.width  : 0.6); to: 1; duration: 440; easing.type: Easing.OutBack; easing.overshoot: 0.6 }
                            NumberAnimation { target: msc; property: "yScale"; from: (detailHost.height > 0 ? detailHost.sh0/detailHost.height : 0.4); to: 1; duration: 440; easing.type: Easing.OutBack; easing.overshoot: 0.6 }
                            NumberAnimation { target: mtr; property: "x"; from: detailHost.sx0; to: 0; duration: 420; easing.type: Easing.OutCubic }
                            NumberAnimation { target: mtr; property: "y"; from: detailHost.sy0; to: 0; duration: 420; easing.type: Easing.OutCubic }
                        }
                        SequentialAnimation {
                            id: morphOut
                            ParallelAnimation {
                                NumberAnimation { target: detailLoader; property: "opacity"; to: 0; duration: 220 }
                                NumberAnimation { target: msc; property: "xScale"; to: (detailHost.width  > 0 ? detailHost.sw0/detailHost.width  : 0.6); duration: 300; easing.type: Easing.InCubic }
                                NumberAnimation { target: msc; property: "yScale"; to: (detailHost.height > 0 ? detailHost.sh0/detailHost.height : 0.4); duration: 300; easing.type: Easing.InCubic }
                                NumberAnimation { target: mtr; property: "x"; to: detailHost.sx0; duration: 300; easing.type: Easing.InCubic }
                                NumberAnimation { target: mtr; property: "y"; to: detailHost.sy0; duration: 300; easing.type: Easing.InCubic }
                            }
                            ScriptAction { script: detailLoader.active = false }
                        }

                        Component { id: spendComp;   SpendPage         { spend: root.spendData; quota: root.vmQuota; vm: root.vm; onBack: detailHost.close() } }
                        Component { id: sessionComp; SessionDetailPage { detail: root.detailData; vm: root.vm; onBack: detailHost.close() } }
                        Component { id: recentsComp; RecentsPage       { recents: root.vm ? root.vm.recents : []; vm: root.vm; onBack: detailHost.close() } }
                    }
                }
            }
        }
    }

    // ── TodayCard inline component ────────────────────────────────────────
    // Displayed at top of home content; full version when no decisions pending,
    // collapsed to a one-liner when decisions take priority.
    component TodayCard: Rectangle {
        id: todayCard
        required property var todayData         // spendDetail() result dict
        required property int quotaPct          // vmQuotaPct
        required property var vmQuota           // vmQuota dict or null
        property bool collapsed: false

        // Relative reset countdown delegates to root.fmtReset/root.nowMs so the
        // ticker lives in exactly one place (DRY with the collapsed today strip).
        function fmtReset() {
            return root.fmtReset((vmQuota && vmQuota["five_hour_reset_epoch"]) ? vmQuota["five_hour_reset_epoch"] : 0)
        }

        // Just the duration ("1h 38m" / "38m" / "<1m" / "—") with no "resets in "
        // prefix — the prototype renders "resets <dur>" (no "in"), so the prefix
        // is composed at the call site below. Mirrors root.fmtReset's math.
        function fmtDur(epochMs) {
            if (!epochMs) return "—"
            var rem = epochMs - root.nowMs
            if (rem <= 0) return "<1m"
            var mins = Math.floor(rem / 60000)
            var h = Math.floor(mins / 60)
            var m = mins % 60
            if (h > 0) return h + "h " + m + "m"
            if (m > 0) return m + "m"
            return "<1m"
        }

        radius: 15
        color: Theme.surface
        border.color: Theme.bd
        border.width: 1
        implicitHeight: todayCol.implicitHeight + 30
        clip: true

        // Faint top-teal wash so the card doesn't read as flat surface.
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            gradient: Gradient {
                GradientStop { position: 0.0; color: Qt.rgba(0.37, 0.82, 0.66, 0.05) }
                GradientStop { position: 0.4; color: "transparent" }
            }
        }

        ColumnLayout {
            id: todayCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.topMargin: 15
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            spacing: 0

            // 1. Header row: "TODAY · ANTHROPIC" left, big "$cost" right
            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                Text {
                    text: "TODAY · ANTHROPIC"
                    color: Theme.faint
                    font.pixelSize: Theme.tMicro
                    font.letterSpacing: 1.8
                    font.bold: true
                    Layout.alignment: Qt.AlignVCenter
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: {
                        var c = (todayData && todayData["cost"]) ? todayData["cost"] : 0
                        return "$" + (c >= 100 ? c.toFixed(0) : c.toFixed(2))
                    }
                    color: Theme.gold
                    font.bold: true
                    font.pixelSize: Theme.tDisplay
                    font.family: Theme.fontMono
                    Layout.alignment: Qt.AlignVCenter
                }
            }

            // 2. 5h progress bar — full-width track + teal fill with a soft glow
            Item {
                Layout.fillWidth: true
                Layout.topMargin: 12
                implicitHeight: 5

                Rectangle {
                    anchors.fill: parent
                    radius: 3
                    color: "#0b0f15"

                    Rectangle {
                        height: parent.height
                        radius: parent.radius
                        width: parent.width * Math.max(0, Math.min(100, quotaPct)) / 100
                        color: Theme.teal
                        // Soft teal glow — a faint teal border bleeds the fill
                        // outward so it reads as "live" without an FBO layer.
                        border.color: Qt.rgba(0.37, 0.82, 0.66, 0.5)
                        border.width: 1
                        Behavior on width { NumberAnimation { duration: 400 } }
                    }
                }
            }

            // 3. Quota row: "<pct>% of 5h" left, "resets <dur>" right
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 9
                spacing: 8

                Text {
                    text: quotaPct + "% of 5h"
                    color: Theme.teal
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.tMeta
                }
                Item { Layout.fillWidth: true }
                Text {
                    visible: vmQuota !== null && vmQuota !== undefined
                    text: "resets " + todayCard.fmtDur((vmQuota && vmQuota["five_hour_reset_epoch"]) ? vmQuota["five_hour_reset_epoch"] : 0)
                    color: Theme.faint
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.tMeta
                }
            }

            // 4. Stat strip: bold bright numbers + dim units (StyledText so the
            //    figures stand out while the unit words recede — matches the
            //    prototype where 331 / 467K / 69.9M / 85.4% read as the data and
            //    "reqs / tokens / cache / hit" read as labels).
            Text {
                Layout.fillWidth: true
                Layout.topMargin: 6
                textFormat: Text.StyledText
                text: {
                    // Wrap a value in a brighter bold span; the surrounding
                    // Text.color (Theme.dim) renders the unit words.
                    function b(x) { return '<font color="' + Theme.ink2 + '"><b>' + x + '</b></font>' }
                    var reqs  = (todayData && todayData["reqs"])             ? todayData["reqs"] : 0
                    var toks  = (todayData && todayData["total_tokens"])     ? todayData["total_tokens"] : 0
                    var cw    = (todayData && todayData["cache_creation"])   ? todayData["cache_creation"] : 0
                    var cr    = (todayData && todayData["cache_read"])       ? todayData["cache_read"] : 0
                    var hr    = (todayData && todayData["hit_rate"])         ? todayData["hit_rate"] : 0
                    // cw = cache create (write), cr = cache read — shown
                    // separately to match ccusage's Cache Create / Cache Read.
                    return b(reqs) + " reqs · " + b(root.fmtNum(toks)) + " tokens · "
                         + b(root.fmtNum(cw)) + " cw · " + b(root.fmtNum(cr)) + " cr · "
                         + b((hr * 100).toFixed(1) + "%") + " hit"
                }
                color: Theme.dim
                font.family: Theme.fontMono
                font.pixelSize: Theme.tMeta
                elide: Text.ElideRight
            }

            // 5. Subagent breakdown — its OWN line below the stat strip, only
            //    when there were subagent (sidechain) requests today. Mirrors
            //    the prototype: "↳ incl. 197 subagent reqs · $17" with the
            //    figures bold. Hidden entirely when sub == 0 so the card stays
            //    compact for sessions that never spawned subagents.
            Text {
                Layout.fillWidth: true
                Layout.topMargin: 3
                visible: (todayData && todayData["subagent_reqs"] || 0) > 0
                textFormat: Text.StyledText
                text: {
                    function b(x) { return '<font color="' + Theme.ink2 + '"><b>' + x + '</b></font>' }
                    var sub  = (todayData && todayData["subagent_reqs"]) ? todayData["subagent_reqs"] : 0
                    var cost = (todayData && todayData["subagent_cost"]) ? todayData["subagent_cost"] : 0
                    var costStr = "$" + (cost >= 10 ? Math.round(cost) : cost.toFixed(2))
                    return "↳ incl. " + b(sub) + " subagent reqs · " + b(costStr)
                }
                color: Theme.dim
                font.family: Theme.fontMono
                font.pixelSize: Theme.tMeta
                elide: Text.ElideRight
            }
        }

        // The TODAY card is now the entry to the spend breakdown (replacing
        // the removed top-right "Today $X · NN%" link, #5). Last child so it
        // sits atop the display-only content and receives the click.
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: {
                if (root.vm) root.spendData = root.vm.spendDetail()
                // Grow the spend page out of the TODAY card itself.
                detailHost.open("spend", todayCard)
            }
        }
    }
}
