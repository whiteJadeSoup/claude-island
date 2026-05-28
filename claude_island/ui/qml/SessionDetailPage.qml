import QtQuick
import QtQuick.Layouts
import QtQuick.Controls

// Session detail drill-down page.
// Props:  detail — the dict returned by vm.sessionDetail(session_id)
//         vm     — worldVm (for focusSession slot)
// Signal: back() — parent connects to `page = "home"`
Rectangle {
    id: detailPage

    required property var detail   // { name, model, cost, turns, cwd, branch, created,
                                   //   ai_title, transcript_path, latest_prompt,
                                   //   uuid, per_model:[{model,cost}] }
    required property var vm

    signal back()

    color: "#0c0f14"

    // Null-safe detail accessors — detail may be {} on first render
    function dv(key, def) {
        return (detail && detail[key] !== undefined && detail[key] !== null && detail[key] !== "")
               ? detail[key] : (def !== undefined ? def : "")
    }
    function dvNum(key, def) {
        var v = detail && detail[key]
        return (v !== undefined && v !== null) ? v : (def !== undefined ? def : 0)
    }

    function fmtCost(n) {
        n = n || 0
        return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2))
    }

    // Short 8-char uuid prefix for display
    function shortUuid(u) {
        u = u || ""
        return u.length > 8 ? u.substring(0, 8) : u
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

                // Back arrow
                Text {
                    text: "‹ Back"
                    color: backArea.containsMouse ? "#c8d4de" : "#7e8a97"
                    font.pixelSize: 13
                    MouseArea {
                        id: backArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: detailPage.back()
                    }
                }

                // Session name (center)
                Text {
                    Layout.fillWidth: true
                    text: dv("name", "Session")
                    color: "#e9edf2"
                    font.pixelSize: 13
                    font.bold: true
                    elide: Text.ElideRight
                    horizontalAlignment: Text.AlignHCenter
                }

                // Right-side action icons
                Row {
                    spacing: 12

                    // ✎ rename — present, inert (no VM slot yet)
                    Text {
                        text: "✎"
                        color: renameArea.containsMouse ? "#a0aab6" : "#566069"
                        font.pixelSize: 14
                        MouseArea {
                            id: renameArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            // rename slot not yet wired — inert
                        }
                    }

                    // ⧉ copy session id to clipboard
                    Text {
                        text: "⧉"
                        color: copyArea.containsMouse ? "#a0aab6" : "#566069"
                        font.pixelSize: 14
                        MouseArea {
                            id: copyArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            // Qt clipboard access from QML: use Qt.labs.platform Clipboard
                            // or the no-op path — leave as visual affordance for now.
                            onClicked: {
                                // No-op: clipboard API not available in this QML context
                            }
                        }
                    }

                    // ↗ open folder
                    Text {
                        text: "↗"
                        color: folderArea.containsMouse ? "#a0aab6" : "#566069"
                        font.pixelSize: 14
                        MouseArea {
                            id: folderArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: {
                                // openFolder slot wired in VM if available
                                if (detailPage.vm && detailPage.vm.openFolder
                                        && dv("cwd") !== "")
                                    detailPage.vm.openFolder(dv("cwd"))
                            }
                        }
                    }

                    // ⟲ reset thinking — present, inert (destructive, no VM slot yet)
                    Text {
                        text: "⟲"
                        color: resetArea.containsMouse ? "#e8743b" : "#4a2222"
                        font.pixelSize: 14
                        MouseArea {
                            id: resetArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            // reset-thinking slot not yet wired — inert
                        }
                    }
                }
            }
        }

        // ── Subtitle: status · model · ai_title ──────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 16
            Layout.rightMargin: 16
            Layout.bottomMargin: 8
            spacing: 6

            Text {
                text: "active"
                color: "#5fd2a8"
                font.pixelSize: 10
                visible: dv("model") !== ""
            }
            Text {
                text: "·"
                color: "#3a4752"
                font.pixelSize: 10
                visible: dv("model") !== ""
            }
            Text {
                text: dv("model") !== "" ? ("v" + dv("model")) : ""
                color: "#566069"
                font.pixelSize: 10
                visible: dv("model") !== ""
                elide: Text.ElideRight
                Layout.fillWidth: !aiTitleText.visible
            }
            Text {
                text: "·"
                color: "#3a4752"
                font.pixelSize: 10
                visible: dv("model") !== "" && dv("ai_title") !== ""
            }
            Text {
                id: aiTitleText
                text: dv("ai_title")
                color: "#7e8a97"
                font.pixelSize: 10
                font.italic: true
                visible: dv("ai_title") !== ""
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }

        // ── Scrollable body ───────────────────────────────────────────────
        Flickable {
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentHeight: bodyCol.implicitHeight
            clip: true

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
                id: bodyCol
                width: parent.width
                spacing: 0

                // ── Jump to terminal button ───────────────────────────────
                Item {
                    Layout.fillWidth: true
                    Layout.leftMargin: 16
                    Layout.rightMargin: 16
                    Layout.bottomMargin: 14
                    Layout.topMargin: 2
                    implicitHeight: jumpBtn.height

                    Rectangle {
                        id: jumpBtn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        height: 36
                        radius: 8
                        color: jumpArea.containsMouse ? "#1a2a20" : "#0e1a14"
                        border.color: jumpArea.containsMouse ? "#5fd2a8" : "#1e3028"
                        border.width: 1

                        RowLayout {
                            anchors.centerIn: parent
                            spacing: 6
                            Text {
                                text: "Jump to terminal"
                                color: jumpArea.containsMouse ? "#5fd2a8" : "#7eb89a"
                                font.pixelSize: 13
                                font.bold: true
                            }
                            Text {
                                text: "↗"
                                color: jumpArea.containsMouse ? "#5fd2a8" : "#7eb89a"
                                font.pixelSize: 13
                            }
                        }

                        MouseArea {
                            id: jumpArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: {
                                if (detailPage.vm && dv("uuid") !== "")
                                    detailPage.vm.focusSession(dv("uuid"))
                            }
                        }
                    }
                }

                // ── Info section ──────────────────────────────────────────
                Text {
                    Layout.leftMargin: 16
                    Layout.bottomMargin: 4
                    text: "SESSION INFO"
                    color: "#566069"
                    font.pixelSize: 10
                    font.letterSpacing: 1.5
                }

                // ID row
                DetailRow {
                    label: "ID"
                    value: shortUuid(dv("uuid"))
                    monospace: true
                }

                // Path row
                DetailRow {
                    label: "Path"
                    value: dv("cwd")
                    monospace: true
                }

                // Transcript row (only when non-empty)
                DetailRow {
                    label: "Transcript"
                    value: dv("transcript_path")
                    monospace: true
                    visible: dv("transcript_path") !== ""
                }

                // Branch row (only when non-empty and not HEAD)
                DetailRow {
                    label: "Branch"
                    value: dv("branch")
                    visible: dv("branch") !== "" && dv("branch") !== "HEAD"
                }

                // Created row
                DetailRow {
                    label: "Created"
                    value: dv("created")
                    visible: dv("created") !== ""
                }

                // ── Tokens & cost ─────────────────────────────────────────
                // Section header: "$X.XX · N turns"
                Item {
                    Layout.fillWidth: true
                    Layout.leftMargin: 16
                    Layout.rightMargin: 16
                    Layout.topMargin: 16
                    Layout.bottomMargin: 4
                    implicitHeight: costHeaderRow.height

                    RowLayout {
                        id: costHeaderRow
                        anchors.left: parent.left
                        anchors.right: parent.right
                        spacing: 6

                        Text {
                            text: fmtCost(dvNum("cost"))
                            color: "#f0a860"
                            font.pixelSize: 18
                            font.bold: true
                            font.family: "monospace"
                        }
                        Text {
                            text: "·"
                            color: "#3a4752"
                            font.pixelSize: 14
                        }
                        Text {
                            text: dvNum("turns") + " turns"
                            color: "#a0aab6"
                            font.pixelSize: 13
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                // Per-model bars
                Text {
                    Layout.leftMargin: 16
                    Layout.bottomMargin: 6
                    text: "MODEL BREAKDOWN"
                    color: "#566069"
                    font.pixelSize: 10
                    font.letterSpacing: 1.5
                    visible: {
                        var pm = (detail && detail["per_model"]) ? detail["per_model"] : []
                        return pm && pm.length > 0
                    }
                }

                Repeater {
                    model: (detail && detail["per_model"]) ? detail["per_model"] : []
                    delegate: Item {
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        Layout.bottomMargin: 6
                        height: 34

                        // Max cost for proportional bar width
                        property real maxCost: {
                            var pm = (detail && detail["per_model"]) ? detail["per_model"] : []
                            var mx = 0.01
                            for (var i = 0; i < pm.length; i++) {
                                if ((pm[i].cost || 0) > mx) mx = pm[i].cost
                            }
                            return mx
                        }
                        property real barFrac: Math.max(0, Math.min(1, (modelData.cost || 0) / maxCost))

                        RowLayout {
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            height: 18
                            Text {
                                Layout.fillWidth: true
                                text: modelData.model || ""
                                color: "#c8d4de"
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                            Text {
                                text: fmtCost(modelData.cost || 0)
                                color: "#f0a860"
                                font.family: "monospace"
                                font.pixelSize: 11
                            }
                        }

                        Rectangle {
                            anchors.bottom: parent.bottom
                            anchors.left: parent.left
                            height: 6
                            radius: 3
                            width: parent.width
                            color: "#151b22"
                            Rectangle {
                                height: parent.height
                                radius: parent.radius
                                width: parent.width * barFrac
                                color: "#5fd2a8"
                            }
                        }
                    }
                }

                // ── Latest prompt ─────────────────────────────────────────
                Item {
                    Layout.fillWidth: true
                    Layout.leftMargin: 16
                    Layout.rightMargin: 16
                    Layout.topMargin: 14
                    Layout.bottomMargin: 6
                    implicitHeight: promptSection.implicitHeight
                    visible: dv("latest_prompt") !== ""

                    ColumnLayout {
                        id: promptSection
                        anchors.left: parent.left
                        anchors.right: parent.right
                        spacing: 4

                        Text {
                            text: "LATEST PROMPT"
                            color: "#566069"
                            font.pixelSize: 10
                            font.letterSpacing: 1.5
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: promptText.implicitHeight + 16
                            radius: 6
                            color: "#080b10"
                            border.color: "#151b22"
                            border.width: 1

                            Text {
                                id: promptText
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 10
                                text: dv("latest_prompt")
                                color: "#566069"
                                font.pixelSize: 11
                                font.family: "monospace"
                                wrapMode: Text.WrapAtWordBoundaryOrAnywhere
                            }
                        }
                    }
                }

                // ── Review mode toggle (visual only) ──────────────────────
                Item {
                    Layout.fillWidth: true
                    Layout.leftMargin: 16
                    Layout.rightMargin: 16
                    Layout.topMargin: 14
                    Layout.bottomMargin: 14
                    implicitHeight: reviewRow.height

                    RowLayout {
                        id: reviewRow
                        anchors.left: parent.left
                        anchors.right: parent.right

                        Text {
                            text: "Review mode"
                            color: "#7e8a97"
                            font.pixelSize: 12
                            Layout.fillWidth: true
                        }

                        // Toggle pill — visual only, not wired to a VM slot yet
                        Rectangle {
                            id: toggleTrack
                            width: 40; height: 22; radius: 11
                            color: toggleOn ? "#1a3a28" : "#151b22"
                            border.color: toggleOn ? "#5fd2a8" : "#26303c"
                            border.width: 1

                            property bool toggleOn: false

                            Behavior on color { ColorAnimation { duration: 150 } }

                            Rectangle {
                                width: 16; height: 16; radius: 8
                                anchors.verticalCenter: parent.verticalCenter
                                x: toggleTrack.toggleOn ? (parent.width - width - 3) : 3
                                color: toggleTrack.toggleOn ? "#5fd2a8" : "#3a4752"
                                Behavior on x { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: toggleTrack.toggleOn = !toggleTrack.toggleOn
                            }
                        }
                    }
                }
            }
        }
    }

    // Inline sub-component for info rows (avoids a separate file)
    component DetailRow: Rectangle {
        required property string label
        required property string value
        property bool monospace: false

        Layout.fillWidth: true
        height: 32
        color: "transparent"

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            Text {
                text: label
                color: "#7e8a97"
                font.pixelSize: 12
                Layout.preferredWidth: 80
                Layout.minimumWidth: 80
            }
            Text {
                text: value
                color: "#c8d4de"
                font.family: monospace ? "monospace" : ""
                font.pixelSize: 11
                elide: Text.ElideLeft
                Layout.fillWidth: true
            }
        }
        // Separator line
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 16
            height: 1
            color: "#0f141a"
        }
    }
}
