import QtQuick
import QtQuick.Layouts

// Recents (dormant sessions) page.
// Props:  recents — QVariantList of { name, cwd, last_seen, cost_usd, session_uuid }
//         vm      — worldVm (for resumeSession slot)
// Signal: back()  — parent connects to `page = "home"`
Rectangle {
    id: recentsPage

    required property var recents
    required property var vm

    signal back()

    color: "#0c0f14"

    function fmtCost(n) {
        n = n || 0
        return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2))
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Header ────────────────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            height: 44
            color: "transparent"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 8

                Text {
                    text: "‹ 返回"
                    color: backArea.containsMouse ? "#c8d4de" : "#7e8a97"
                    font.pixelSize: 13
                    MouseArea {
                        id: backArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: recentsPage.back()
                    }
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: "历史会话"
                    color: "#e9edf2"
                    font.pixelSize: 13
                    font.bold: true
                }

                Item { Layout.fillWidth: true }
                // Spacer to keep title centered (mirrors back arrow width)
                Item { width: 42 }
            }
        }

        // ── Empty state ───────────────────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !recents || recents.length === 0

            Text {
                anchors.centerIn: parent
                text: "暂无历史会话"
                color: "#566069"
                font.pixelSize: 13
            }
        }

        // ── Session list ──────────────────────────────────────────────────
        Flickable {
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentHeight: listCol.height
            clip: true
            visible: recents && recents.length > 0

            ColumnLayout {
                id: listCol
                width: parent.width
                spacing: 0

                Repeater {
                    model: recents
                    delegate: Rectangle {
                        required property var modelData
                        required property int index

                        Layout.fillWidth: true
                        Layout.leftMargin: 13
                        Layout.rightMargin: 13
                        Layout.bottomMargin: 6

                        height: rowContent.height + 16
                        radius: 8
                        color: rowHover.containsMouse ? "#0e141b" : "#0a1018"
                        border.color: rowHover.containsMouse ? "#1c2632" : "#151b22"
                        border.width: 1

                        RowLayout {
                            id: rowContent
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.topMargin: 10
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12
                            spacing: 8

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 3

                                // Session name
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.name || "未知会话"
                                    color: "#e9edf2"
                                    font.pixelSize: 13
                                    font.bold: true
                                    elide: Text.ElideRight
                                }

                                // Working directory
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.cwd || ""
                                    color: "#566069"
                                    font.family: "monospace"
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }

                                // Last seen timestamp
                                Text {
                                    Layout.fillWidth: true
                                    text: modelData.last_seen || ""
                                    color: "#7e8a97"
                                    font.pixelSize: 10
                                    elide: Text.ElideRight
                                }
                            }

                            ColumnLayout {
                                spacing: 4

                                // Cost
                                Text {
                                    text: fmtCost(modelData.cost_usd)
                                    color: "#f0a860"
                                    font.family: "monospace"
                                    font.pixelSize: 12
                                    font.bold: true
                                    horizontalAlignment: Text.AlignRight
                                }

                                // Resume button
                                Rectangle {
                                    width: resumeLabel.width + 16
                                    height: 26
                                    radius: 6
                                    color: resumeArea.containsMouse ? "#1a2a20" : "transparent"
                                    border.color: resumeArea.containsMouse ? "#5fd2a8" : "#2a3a30"
                                    border.width: 1

                                    Text {
                                        id: resumeLabel
                                        anchors.centerIn: parent
                                        text: "Resume ↗"
                                        color: resumeArea.containsMouse ? "#5fd2a8" : "#7e8a97"
                                        font.pixelSize: 11
                                    }

                                    MouseArea {
                                        id: resumeArea
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        hoverEnabled: true
                                        onClicked: {
                                            if (recentsPage.vm && modelData.session_uuid)
                                                recentsPage.vm.resumeSession(modelData.session_uuid)
                                        }
                                    }
                                }
                            }
                        }

                        // Hover area for the whole row
                        MouseArea {
                            id: rowHover
                            anchors.fill: parent
                            hoverEnabled: true
                            // Row click does nothing (use Resume button)
                        }
                    }
                }

                // Bottom padding
                Item { height: 12 }
            }
        }
    }
}
