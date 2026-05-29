import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtQuick.Controls

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

    // ── Page navigation: "home" | "spend" | "recents" | "session" ───────
    property string page: "home"
    // SpendPage data — populated before switching page="spend"
    property var spendData: ({})
    // SessionDetailPage data — populated before switching page="session"
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
                                onClicked: root.page = "recents"
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

                        // Collapse affordance
                        Text {
                            text: "  ⌄"
                            color: collapseArea.containsMouse ? "#c8d4de" : "#566069"
                            font.pixelSize: 16
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

                    // ── Home content ──────────────────────────────────────
                    Item {
                        id: homeContent
                        width: parent.width; height: parent.height
                        x: root.page === "home" ? 0 : -parent.width
                        opacity: root.page === "home" ? 1.0 : 0.6
                        Behavior on x       { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
                        Behavior on opacity { NumberAnimation { duration: 200 } }

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
                                // Section header: "Needs you · N"
                                Text {
                                    visible: root.vmDecisions.length > 0
                                    text: "● Needs you · " + root.vmDecisions.length
                                    color: "#e8884c"
                                    font.pixelSize: 10; font.letterSpacing: 1.5
                                    Layout.leftMargin: 16; Layout.topMargin: 11; Layout.bottomMargin: 6
                                }

                                // Primary decision card
                                Loader {
                                    Layout.fillWidth: true
                                    Layout.leftMargin: 13; Layout.rightMargin: 13; Layout.bottomMargin: 4
                                    active: root.vmDecisions.length > 0
                                    visible: active
                                    sourceComponent: Component {
                                        DecisionCard {
                                            decision: root.vmDecisions.length > 0 ? root.vmDecisions[0] : null
                                            vm: root.vm
                                        }
                                    }
                                }

                                // Queued decisions preview (index ≥ 1)
                                Repeater {
                                    model: root.vmDecisions
                                    delegate: RowLayout {
                                        required property var modelData
                                        required property int index
                                        visible: index >= 1
                                        Layout.fillWidth: true
                                        Layout.leftMargin: 16; Layout.rightMargin: 16
                                        Layout.preferredHeight: visible ? 24 : 0
                                        spacing: 6
                                        Rectangle {
                                            width: 6; height: 6; radius: 3
                                            color: root.riskColor(modelData.risk)
                                        }
                                        Text {
                                            text: modelData.session_name + " · " + root.kindLabel(modelData.kind)
                                            color: "#7e8a97"; font.pixelSize: 11
                                            elide: Text.ElideRight; Layout.fillWidth: true
                                        }
                                    }
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
                                                font.family: "monospace"
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
                                // Section header visible only when there are active sessions
                                Text {
                                    visible: root.workingCount() > 0
                                    text: "◉ Active · " + root.workingCount()
                                    color: "#5fd2a8"
                                    font.pixelSize: 10; font.letterSpacing: 1.5
                                    Layout.leftMargin: 16; Layout.topMargin: 13; Layout.bottomMargin: 6
                                }

                                Repeater {
                                    model: root.vmSessions
                                    delegate: Item {
                                        required property var modelData
                                        // Only render active sessions in this band
                                        visible: root.isActive(modelData.phase)
                                        Layout.fillWidth: true
                                        Layout.leftMargin: 13; Layout.rightMargin: 13
                                        Layout.bottomMargin: 6
                                        implicitHeight: visible ? liveCard.implicitHeight : 0

                                        // Live Console card
                                        Rectangle {
                                            id: liveCard
                                            anchors.left: parent.left
                                            anchors.right: parent.right
                                            anchors.top: parent.top
                                            implicitHeight: cardCol.implicitHeight + 16
                                            radius: 8
                                            color: liveArea.containsMouse ? "#0e141b" : "#0a1018"
                                            border.color: liveArea.containsMouse ? "#1c2632" : "#151b22"
                                            border.width: 1

                                            // Left breathing glow accent — color by phase
                                            Rectangle {
                                                anchors.left: parent.left
                                                anchors.top: parent.top
                                                anchors.bottom: parent.bottom
                                                anchors.topMargin: parent.radius
                                                anchors.bottomMargin: parent.radius
                                                width: 3
                                                radius: 1
                                                color: root.phaseColor(modelData.phase)
                                                // Bug 2 fix: reference the local property directly;
                                                // there is no id: glowAnim — glowOp lives on this Rectangle.
                                                opacity: glowOp

                                                property real glowOp: 0.7
                                                SequentialAnimation on glowOp {
                                                    loops: Animation.Infinite
                                                    running: root.isActive(modelData.phase)
                                                    NumberAnimation { to: 1.0; duration: 800; easing.type: Easing.InOutSine }
                                                    NumberAnimation { to: 0.4; duration: 800; easing.type: Easing.InOutSine }
                                                }
                                            }

                                            ColumnLayout {
                                                id: cardCol
                                                anchors.left: parent.left
                                                anchors.right: parent.right
                                                anchors.top: parent.top
                                                anchors.topMargin: 10
                                                anchors.leftMargin: 12
                                                anchors.rightMargin: 12
                                                spacing: 6

                                                // Top row: name + phase·elapsed + cost
                                                RowLayout {
                                                    Layout.fillWidth: true
                                                    spacing: 6

                                                    Text {
                                                        text: modelData.name || ""
                                                        color: "#e9edf2"
                                                        font.pixelSize: 13; font.bold: true
                                                        elide: Text.ElideRight
                                                        Layout.fillWidth: true
                                                    }
                                                    Text {
                                                        text: modelData.phase || ""
                                                        color: root.phaseColor(modelData.phase)
                                                        font.pixelSize: 10
                                                        font.letterSpacing: 0.8
                                                    }
                                                    Text {
                                                        text: root.fmtCost(modelData.cost_usd)
                                                        color: "#f0a860"
                                                        font.family: "monospace"
                                                        font.pixelSize: 12; font.bold: true
                                                    }
                                                }

                                                // Live tail line: current_tool_input or phase label
                                                Text {
                                                    Layout.fillWidth: true
                                                    text: root.tailLine(modelData)
                                                    color: "#566069"
                                                    font.family: "monospace"
                                                    font.pixelSize: 10
                                                    elide: Text.ElideRight
                                                    visible: text !== ""
                                                }

                                                // Activity waveform (#1): a glowing oscilloscope line,
                                                // not bars. Amplitude at each point = that sample's token
                                                // rate ÷ peak, so the more tokens Claude is producing, the
                                                // taller the wave swings — idle ≈ flat, busy = loud. A
                                                // continuously-animated `flow` phase scrolls the wave so it
                                                // reads as alive/breathing even between rate updates. Newest
                                                // sample is on the right (the live edge).
                                                Item {
                                                    id: waveItem
                                                    Layout.fillWidth: true
                                                    implicitHeight: 30

                                                    property var series: modelData.rate_series || []
                                                    property real peak: {
                                                        var s = series
                                                        var mx = 1
                                                        for (var i = 0; i < s.length; i++)
                                                            if (s[i] > mx) mx = s[i]
                                                        return mx
                                                    }
                                                    // 0→1 looping phase that animates the wave's flow.
                                                    property real flow: 0
                                                    NumberAnimation on flow {
                                                        from: 0; to: 1; duration: 1600
                                                        loops: Animation.Infinite
                                                        running: root.isActive(modelData.phase)
                                                    }
                                                    onFlowChanged: scope.requestPaint()
                                                    onSeriesChanged: scope.requestPaint()

                                                    Canvas {
                                                        id: scope
                                                        anchors.fill: parent
                                                        property color strokeCol: root.phaseColor(modelData.phase)
                                                        onStrokeColChanged: requestPaint()
                                                        onWidthChanged: requestPaint()
                                                        onPaint: {
                                                            var ctx = getContext("2d")
                                                            var w = width, h = height, mid = h * 0.52
                                                            ctx.clearRect(0, 0, w, h)
                                                            // faint baseline
                                                            ctx.strokeStyle = "rgba(255,255,255,0.05)"
                                                            ctx.lineWidth = 1
                                                            ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke()

                                                            var s = waveItem.series
                                                            var n = s.length
                                                            var N = 72            // render resolution
                                                            ctx.lineWidth = 2
                                                            ctx.lineJoin = "round"; ctx.lineCap = "round"
                                                            ctx.strokeStyle = scope.strokeCol
                                                            ctx.shadowColor = scope.strokeCol
                                                            ctx.shadowBlur = 6
                                                            ctx.beginPath()
                                                            for (var i = 0; i < N; i++) {
                                                                var frac = i / (N - 1)
                                                                var x = frac * w
                                                                // amplitude from the (right-aligned) rate series
                                                                var amp = 0
                                                                if (n > 0) {
                                                                    var si = Math.floor(frac * (n - 1))
                                                                    amp = s[si] / waveItem.peak   // 0..1
                                                                }
                                                                // oscillate around mid; flow scrolls the wave
                                                                var wob = Math.sin(i * 0.45 + waveItem.flow * Math.PI * 2)
                                                                var y = mid - amp * (h * 0.40) * wob
                                                                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                                                            }
                                                            ctx.stroke()
                                                            ctx.shadowBlur = 0
                                                        }
                                                    }
                                                }

                                                // Bottom info: tokens/min + model chip
                                                RowLayout {
                                                    Layout.fillWidth: true
                                                    spacing: 8

                                                    Text {
                                                        visible: modelData.tokens_per_min !== undefined && modelData.tokens_per_min !== null && modelData.tokens_per_min > 0
                                                        text: (modelData.tokens_per_min || 0) + " tk/min · last 60s"
                                                        color: "#566069"; font.pixelSize: 10
                                                    }
                                                    Item { Layout.fillWidth: true }
                                                    // Model chip (#2): color-coded by family + brighter/bolder
                                                    // so the model is obvious at a glance (was a dim 9px grey
                                                    // chip that disappeared). null-safe string coercion for
                                                    // visible; friendly short label from snapshot_projection.
                                                    Rectangle {
                                                        property string mc: root.modelColor(modelData.model)
                                                        visible: (modelData.model || "") !== ""
                                                        radius: 5
                                                        // subtle tinted fill from the family colour (hex + alpha)
                                                        color: Qt.rgba(0, 0, 0, 0.25)
                                                        border.color: mc
                                                        border.width: 1
                                                        width: modelChipLabel.width + 14
                                                        height: 20
                                                        Text {
                                                            id: modelChipLabel
                                                            anchors.centerIn: parent
                                                            text: modelData.model || ""
                                                            color: parent.mc
                                                            font.pixelSize: 11; font.bold: true
                                                        }
                                                    }
                                                }

                                                // Bottom spacer
                                                Item { implicitHeight: 2 }
                                            }

                                            MouseArea {
                                                id: liveArea
                                                anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor
                                                hoverEnabled: true
                                                // Left-click: focus terminal; right-click: open detail page
                                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                                onClicked: (mouse) => {
                                                    if (mouse.button === Qt.RightButton) {
                                                        root.detailData = root.vm
                                                            ? root.vm.sessionDetail(modelData.id)
                                                            : {}
                                                        root.page = "session"
                                                    } else {
                                                        if (root.vm) root.vm.focusSession(modelData.id)
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }

                                // ── IDLE band: compact chips ───────────────
                                // Section header visible only when there are idle sessions
                                Text {
                                    visible: root.quietCount() > 0
                                    text: "○ Idle · " + root.quietCount()
                                    color: "#566069"
                                    font.pixelSize: 10; font.letterSpacing: 1.5
                                    Layout.leftMargin: 16; Layout.topMargin: 13; Layout.bottomMargin: 6
                                }
                                Flow {
                                    visible: root.quietCount() > 0
                                    Layout.fillWidth: true
                                    Layout.leftMargin: 16; Layout.rightMargin: 16; Layout.bottomMargin: 16
                                    spacing: 8
                                    Repeater {
                                        model: root.vmSessions
                                        delegate: Rectangle {
                                            required property var modelData
                                            visible: !root.isActive(modelData.phase)
                                            width: visible ? (lbl.width + 22) : 0
                                            height: visible ? 26 : 0
                                            radius: 8
                                            color: chipArea.containsMouse ? "#0e141b" : "#0a1018"
                                            border.color: chipArea.containsMouse ? "#1c2632" : "#151b22"
                                            border.width: 1

                                            Text {
                                                id: lbl; anchors.centerIn: parent
                                                text: modelData.name + " · " + root.fmtCost(modelData.cost_usd)
                                                color: chipArea.containsMouse ? "#a0aab6" : "#828d99"
                                                font.pixelSize: 12
                                            }
                                            MouseArea {
                                                id: chipArea; anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                                // Left-click: focus terminal; right-click: open detail page
                                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                                onClicked: (mouse) => {
                                                    if (mouse.button === Qt.RightButton) {
                                                        root.detailData = root.vm
                                                            ? root.vm.sessionDetail(modelData.id)
                                                            : {}
                                                        root.page = "session"
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

                    // ── SpendPage drill-down ──────────────────────────────
                    Loader {
                        id: spendLoader
                        width: parent.width; height: parent.height
                        x: root.page === "spend" ? 0 : parent.width
                        visible: x < parent.width
                        Behavior on x { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
                        active: root.page === "spend"
                        sourceComponent: Component {
                            SpendPage {
                                spend:  root.spendData
                                quota:  root.vmQuota
                                vm:     root.vm
                                onBack: root.page = "home"
                            }
                        }
                    }

                    // ── RecentsPage drill-down ────────────────────────────
                    Loader {
                        id: recentsLoader
                        width: parent.width; height: parent.height
                        x: root.page === "recents" ? 0 : parent.width
                        visible: x < parent.width
                        Behavior on x { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
                        active: root.page === "recents"
                        sourceComponent: Component {
                            RecentsPage {
                                recents: root.vm ? root.vm.recents : []
                                vm:      root.vm
                                onBack:  root.page = "home"
                            }
                        }
                    }

                    // ── SessionDetailPage drill-down ──────────────────────
                    Loader {
                        id: sessionLoader
                        width: parent.width; height: parent.height
                        x: root.page === "session" ? 0 : parent.width
                        visible: x < parent.width
                        Behavior on x { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
                        active: root.page === "session"
                        sourceComponent: Component {
                            SessionDetailPage {
                                detail: root.detailData
                                vm:     root.vm
                                onBack: root.page = "home"
                            }
                        }
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

        radius: 8
        color: "#0a0d12"
        border.color: "#151b22"
        border.width: 1
        implicitHeight: todayCol.implicitHeight + 20

        ColumnLayout {
            id: todayCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 12
            spacing: 6

            // Header row: "TODAY · resets in X" left, big cost right
            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                ColumnLayout {
                    spacing: 2
                    Text {
                        text: "TODAY"
                        color: "#566069"
                        font.pixelSize: 9; font.letterSpacing: 2; font.bold: true
                    }
                    Text {
                        visible: vmQuota !== null && vmQuota !== undefined
                        text: "Anthropic · " + todayCard.fmtReset()
                        color: "#3a4752"
                        font.pixelSize: 9
                    }
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: {
                        var c = (todayData && todayData["cost"]) ? todayData["cost"] : 0
                        return "$" + (c >= 100 ? c.toFixed(0) : c.toFixed(2))
                    }
                    color: "#f0a860"
                    font.pixelSize: 22; font.bold: true; font.family: "monospace"
                }
            }

            // 5h progress bar
            Item {
                Layout.fillWidth: true
                implicitHeight: 18

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    height: 5
                    radius: 2
                    color: "#151b22"

                    Rectangle {
                        height: parent.height
                        radius: parent.radius
                        width: parent.width * Math.max(0, Math.min(100, quotaPct)) / 100
                        color: quotaPct > 80 ? "#e8743b" : "#5fd2a8"
                        Behavior on width { NumberAnimation { duration: 400 } }
                    }
                }
                Text {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    text: quotaPct + "% of 5h"
                    color: "#5fd2a8"; font.pixelSize: 9
                }
            }

            // Meta line: reqs · tokens · cache · hit%
            Text {
                Layout.fillWidth: true
                text: {
                    var reqs  = (todayData && todayData["reqs"])         ? todayData["reqs"] : 0
                    var toks  = (todayData && todayData["total_tokens"])  ? todayData["total_tokens"] : 0
                    var cache = (todayData && todayData["cache_read"])    ? todayData["cache_read"] : 0
                    var hr    = (todayData && todayData["hit_rate"])      ? todayData["hit_rate"] : 0
                    var parts = []
                    if (reqs > 0) parts.push(reqs + " reqs")
                    if (toks > 0) parts.push(fmtNum(toks) + " tokens")
                    if (cache > 0) parts.push(fmtNum(cache) + " cache")
                    if (hr > 0) parts.push((hr * 100).toFixed(0) + "% hit")
                    return parts.length > 0 ? parts.join(" · ") : "no usage today"
                }
                color: "#566069"; font.pixelSize: 10
                elide: Text.ElideRight
            }

            // Subagent sub-line: "↳ incl. N subagent reqs · $X"
            Text {
                Layout.fillWidth: true
                visible: (todayData && todayData["subagent_reqs"]) ? todayData["subagent_reqs"] > 0 : false
                text: {
                    var sr = (todayData && todayData["subagent_reqs"]) ? todayData["subagent_reqs"] : 0
                    var sc = (todayData && todayData["subagent_cost"]) ? todayData["subagent_cost"] : 0.0
                    return "↳ incl. " + sr + " subagent reqs · $" + sc.toFixed(2)
                }
                color: "#3a4752"; font.pixelSize: 9
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
                root.page = "spend"
            }
        }
    }
}
