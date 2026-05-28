import QtQuick
import QtQuick.Window
import QtQuick.Layouts

Window {
    id: root
    width: 480; height: 460; visible: true
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
    color: "transparent"

    // Null-safe accessors — worldVm context property may be null during
    // the first binding evaluation pass before the engine fully wires
    // the context. All UI expressions go through these helpers.
    readonly property var vm: worldVm || null
    readonly property var vmSessions:  (vm && vm.sessions)  ? vm.sessions  : []
    readonly property var vmDecisions: (vm && vm.decisions) ? vm.decisions : []
    readonly property string vmTodayCost: vm ? vm.todayCost : "$0.00"
    readonly property int    vmQuotaPct:  vm ? vm.quotaPct  : 0

    readonly property var activePhases: ["thinking", "tool_use", "compacting", "waiting_approval"]
    function isActive(p) { return activePhases.indexOf(p) !== -1 }
    function fmtCost(n) { return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2)) }
    function workingCount() {
        var n = 0; for (var i = 0; i < vmSessions.length; i++)
            if (isActive(vmSessions[i].phase)) n++; return n
    }
    function quietCount() { return vmSessions.length - workingCount() }

    Rectangle {
        anchors.fill: parent; radius: 18; color: "#0c0f14"
        border.color: "#1c2632"; border.width: 1; clip: true

        ColumnLayout {
            anchors.fill: parent; spacing: 0

            // 顶栏(可拖)
            Rectangle {
                Layout.fillWidth: true; height: 44; color: "transparent"
                MouseArea {
                    anchors.fill: parent; property point s
                    onPressed: (m) => s = Qt.point(m.x, m.y)
                    onPositionChanged: (m) => { root.x += m.x - s.x; root.y += m.y - s.y }
                }
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 16; anchors.rightMargin: 16
                    Text { text: "Claude Island"; color: "#e9edf2"; font.pixelSize: 13; font.bold: true }
                    Item { Layout.fillWidth: true }
                    Text {
                        color: "#a0aab6"; font.family: "monospace"; font.pixelSize: 12
                        text: "今天 " + vmTodayCost + " · " + vmQuotaPct + "%"
                    }
                }
            }

            Flickable {
                Layout.fillWidth: true; Layout.fillHeight: true
                contentHeight: bands.height; clip: true
                ColumnLayout {
                    id: bands; width: parent.width; spacing: 0

                    // 等你决策(只展队首,静态)
                    Text {
                        visible: vmDecisions.length > 0
                        text: "● 等你决策 · " + vmDecisions.length
                        color: "#e8884c"; font.pixelSize: 10; font.letterSpacing: 1.5
                        Layout.leftMargin: 16; Layout.topMargin: 11; Layout.bottomMargin: 6
                    }
                    Rectangle {
                        visible: vmDecisions.length > 0
                        Layout.fillWidth: true; Layout.leftMargin: 13; Layout.rightMargin: 13
                        Layout.preferredHeight: dcol.height + 22; radius: 11
                        color: "#1a1410"; border.color: "#4a3320"; border.width: 1
                        Column {
                            id: dcol; x: 13; y: 11; width: parent.width - 26; spacing: 5
                            Text {
                                text: vmDecisions.length ? vmDecisions[0].session_name : ""
                                color: "#f4efe9"; font.pixelSize: 14; font.bold: true
                            }
                            Text {
                                text: vmDecisions.length
                                    ? (vmDecisions[0].question_text || vmDecisions[0].tool_name || "")
                                    : ""
                                color: "#ecdfd3"; font.pixelSize: 12; width: parent.width; wrapMode: Text.Wrap
                            }
                        }
                    }

                    // 正在干活
                    Text {
                        text: "◉ 正在干活 · " + root.workingCount()
                        color: "#5fd2a8"; font.pixelSize: 10; font.letterSpacing: 1.5
                        Layout.leftMargin: 16; Layout.topMargin: 13; Layout.bottomMargin: 6
                    }
                    Repeater {
                        model: vmSessions
                        delegate: RowLayout {
                            required property var modelData
                            visible: root.isActive(modelData.phase)
                            Layout.fillWidth: true; Layout.leftMargin: 16; Layout.rightMargin: 16
                            Layout.preferredHeight: visible ? 40 : 0
                            ColumnLayout {
                                spacing: 2
                                Text { text: modelData.name; color: "#e9edf2"; font.pixelSize: 13; font.bold: true }
                                Text {
                                    text: modelData.current_tool_input || modelData.cwd
                                    color: "#7e8a97"; font.family: "monospace"; font.pixelSize: 11; elide: Text.ElideRight
                                }
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: root.fmtCost(modelData.cost_usd); color: "#f0a860"
                                font.family: "monospace"; font.pixelSize: 13; font.bold: true
                            }
                        }
                    }

                    // 安静(chips)
                    Text {
                        text: "○ 安静 · " + root.quietCount()
                        color: "#566069"; font.pixelSize: 10; font.letterSpacing: 1.5
                        Layout.leftMargin: 16; Layout.topMargin: 13; Layout.bottomMargin: 6
                    }
                    Flow {
                        Layout.fillWidth: true; Layout.leftMargin: 16; Layout.rightMargin: 16
                        Layout.bottomMargin: 16; spacing: 8
                        Repeater {
                            model: vmSessions
                            delegate: Rectangle {
                                required property var modelData
                                visible: !root.isActive(modelData.phase)
                                width: visible ? (lbl.width + 22) : 0; height: visible ? 26 : 0
                                radius: 8; color: "#0e141b"; border.color: "#151b22"; border.width: 1
                                Text {
                                    id: lbl; anchors.centerIn: parent
                                    text: modelData.name + " · " + root.fmtCost(modelData.cost_usd)
                                    color: "#828d99"; font.pixelSize: 12
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
