import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtQuick.Controls

// Fixed window at max size (480×460). The visible "island" is the inner
// rootRect which morphs between pill/decision/expanded shapes — this avoids
// the Qt 6 limitation where Window does not support the `states` property.
// The window stays transparent and frameless; rootRect provides the chrome.
Window {
    id: root
    width: 480; height: 460; visible: true
    // On macOS Qt.Tool maps to NSPanel which silently refuses to paint a
    // WA_TranslucentBackground surface — the window reports isVisible=True
    // but nothing reaches the screen.  The existing CapsuleWindow._setup_window
    // drops Qt.Tool on darwin for the same reason (see capsule_window.py).
    // isMac is injected from qml_app.py via engine.rootContext().
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
           (typeof isMac !== "undefined" && isMac ? 0 : Qt.Tool)
    color: "transparent"

    // ── Page navigation: "home" | "spend" | "recents" ────────────────────
    property string page: "home"
    // SpendPage data — populated before switching page="spend"
    property var spendData: ({})

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
        if (kind === "ask_question")       return "提问"
        if (kind === "user_prompt_submit") return "审核"
        return "审批"
    }

    // ── Island state: "expanded" | "collapsed" | "decision" ──────────────
    // Island state drives rootRect geometry; the Window stays fixed at 480×460.
    // The transparent background means unused window area is click-through.
    property string islandState: "expanded"

    // ── Auto-transition: decision drains → collapsed ───────────────────────
    Connections {
        target: root.vm
        function onChanged() {
            if (root.islandState === "decision" && root.vmDecisions.length === 0)
                root.islandState = "collapsed"
        }
    }

    // ── Root island rectangle (morphing chrome) ───────────────────────────
    Rectangle {
        id: rootRect
        // Geometry driven by islandState
        width:  root.islandState === "collapsed" ? 240
              : root.islandState === "decision"  ? 480 : 480
        height: root.islandState === "collapsed" ? 44
              : root.islandState === "decision"  ? 180 : 460
        // Anchor to top-right of the transparent window so the pill
        // appears at the same screen position as the expanded panel's top bar.
        anchors.top: parent.top
        anchors.right: parent.right

        // Smooth morph
        Behavior on width  { NumberAnimation { duration: 340; easing.type: Easing.OutCubic } }
        Behavior on height { NumberAnimation { duration: 340; easing.type: Easing.OutCubic } }

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
                          ? (root.vmDecisions[0].session_name + " 等你")
                          : (root.workingCount() + " 在跑 · " + root.vmTodayCost)
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
                            text: "等你决策"; color: "#e8884c"
                            font.pixelSize: 12; font.bold: true
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: "展开全部"; color: "#7e8a97"; font.pixelSize: 11
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

                        // Today cost / quota — clickable → SpendPage
                        Text {
                            color: spendArea.containsMouse ? "#f0a860" : "#a0aab6"
                            font.family: "monospace"; font.pixelSize: 12
                            text: "今天 " + root.vmTodayCost + " · " + root.vmQuotaPct + "%"
                            MouseArea {
                                id: spendArea; anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                onClicked: {
                                    if (root.vm) root.spendData = root.vm.spendDetail()
                                    root.page = "spend"
                                }
                            }
                        }

                        // History link → RecentsPage
                        Text {
                            text: "  🕘 历史"
                            color: recentsArea.containsMouse ? "#c8d4de" : "#7e8a97"
                            font.pixelSize: 12
                            MouseArea {
                                id: recentsArea; anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                onClicked: root.page = "recents"
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

                        // ── Empty state: no sessions and no decisions ─────────────
                        // Shown in place of the three band headers when there is
                        // nothing to display; hides itself the moment any session
                        // or decision appears.
                        Item {
                            anchors.fill: parent
                            visible: root.vmSessions.length === 0 && root.vmDecisions.length === 0

                            Column {
                                anchors.centerIn: parent
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
                                    text: "没有运行中的会话 · 一切安静"
                                    color: "#3a4752"
                                    font.pixelSize: 12
                                }
                            }
                        }

                        Flickable {
                            anchors.fill: parent
                            contentHeight: bands.height; clip: true
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
                                id: bands; width: parent.width; spacing: 0

                                // ── 等你决策 ─────────────────────────────
                                Text {
                                    visible: root.vmDecisions.length > 0
                                    text: "● 等你决策 · " + root.vmDecisions.length
                                    color: "#e8884c"
                                    font.pixelSize: 10; font.letterSpacing: 1.5
                                    Layout.leftMargin: 16; Layout.topMargin: 11; Layout.bottomMargin: 6
                                }

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

                                // ── 正在干活 ──────────────────────────────
                                Text {
                                    text: "◉ 正在干活 · " + root.workingCount()
                                    color: "#5fd2a8"
                                    font.pixelSize: 10; font.letterSpacing: 1.5
                                    Layout.leftMargin: 16; Layout.topMargin: 13; Layout.bottomMargin: 6
                                }
                                Repeater {
                                    model: root.vmSessions
                                    delegate: Rectangle {
                                        required property var modelData
                                        visible: root.isActive(modelData.phase)
                                        Layout.fillWidth: true
                                        Layout.leftMargin: 13; Layout.rightMargin: 13
                                        Layout.preferredHeight: visible ? 44 : 0
                                        radius: 7
                                        color: rowArea.containsMouse ? "#0e141b" : "transparent"
                                        border.color: rowArea.containsMouse ? "#1c2632" : "transparent"
                                        border.width: 1

                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 6; anchors.rightMargin: 6
                                            spacing: 0
                                            ColumnLayout {
                                                spacing: 2
                                                Layout.fillWidth: true
                                                Text { text: modelData.name; color: "#e9edf2"; font.pixelSize: 13; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                                                Text {
                                                    text: modelData.current_tool_input || modelData.cwd
                                                    color: "#7e8a97"; font.family: "monospace"; font.pixelSize: 11; elide: Text.ElideRight
                                                    Layout.fillWidth: true
                                                }
                                            }
                                            // ── Scan-wave accent ──────────────────────────────
                                            // 4 bars that pulse with a travelling wave — live
                                            // indicator signature.  Color tracks phase:
                                            //   thinking   → amber  #f0b860
                                            //   tool_use   → teal   #5fd2a8
                                            //   other active → teal
                                            // Kept small (3×10 each, 40 px total) and right-
                                            // anchored near the cost so it reads as a discreet
                                            // "live" cue, not a spotlight.
                                            Row {
                                                spacing: 3
                                                // phase-derived colour — amber for thinking, teal otherwise
                                                property string waveColor: modelData.phase === "thinking"
                                                    ? "#f0b860" : "#5fd2a8"

                                                Repeater {
                                                    model: 4
                                                    delegate: Rectangle {
                                                        required property int index
                                                        width: 3; height: 10; radius: 1
                                                        color: parent.waveColor
                                                        opacity: 0.85

                                                        // Each bar pulses from 3 px to 10 px with a
                                                        // staggered delay so they form a travelling
                                                        // wave from left to right.
                                                        property real barH: 5
                                                        transform: Scale {
                                                            yScale: barH / 10
                                                            origin.x: 0; origin.y: 5
                                                        }
                                                        SequentialAnimation on barH {
                                                            loops: Animation.Infinite
                                                            running: true
                                                            // Leading pause creates the stagger:
                                                            // bar 0 starts immediately, bar 3 starts
                                                            // after ~225 ms so the wave sweeps left→right.
                                                            PauseAnimation { duration: index * 75 }
                                                            NumberAnimation { to: 10; duration: 250; easing.type: Easing.InOutSine }
                                                            NumberAnimation { to: 3;  duration: 250; easing.type: Easing.InOutSine }
                                                            // Trailing pause so the full cycle is ~800 ms
                                                            // (4 bars × 75 ms stagger + 500 ms wave = calm).
                                                            PauseAnimation { duration: 300 - index * 75 }
                                                        }
                                                    }
                                                }
                                            }
                                            Item { width: 6 }
                                            Text {
                                                text: root.fmtCost(modelData.cost_usd); color: "#f0a860"
                                                font.family: "monospace"; font.pixelSize: 13; font.bold: true
                                            }
                                        }
                                        MouseArea {
                                            id: rowArea; anchors.fill: parent
                                            cursorShape: Qt.PointingHandCursor; hoverEnabled: true
                                            onClicked: root.vm.focusSession(modelData.id)
                                        }
                                    }
                                }

                                // ── 安静(chips) ───────────────────────────
                                Text {
                                    text: "○ 安静 · " + root.quietCount()
                                    color: "#566069"
                                    font.pixelSize: 10; font.letterSpacing: 1.5
                                    Layout.leftMargin: 16; Layout.topMargin: 13; Layout.bottomMargin: 6
                                }
                                Flow {
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
                                                onClicked: root.vm.focusSession(modelData.id)
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
                }
            }
        }
    }
}
