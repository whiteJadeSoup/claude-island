import "."
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

    // ── Rename state ──────────────────────────────────────────────────────
    // isRenaming toggles the header name between a read-only label and an
    // inline TextField so the user can type a new name.
    property bool isRenaming: false

    // ── Copy feedback state ───────────────────────────────────────────────
    // copiedFlash shows "✓" on the copy icon for 1 second after a copy.
    property bool copiedFlash: false
    Timer {
        id: copyFlashTimer
        interval: 1000
        repeat: false
        onTriggered: detailPage.copiedFlash = false
    }

    // ── Reset-thinking confirm state ──────────────────────────────────────
    // Two-step confirm: first click arms, second click within 3s fires.
    property bool resetArmed: false
    Timer {
        id: resetArmTimer
        interval: 3000
        repeat: false
        onTriggered: detailPage.resetArmed = false
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

                // Back arrow — icon-only "‹" (matches the prototype header,
                // which drops the "Claude Island" chrome and leads with ‹ + name).
                Text {
                    text: "‹"
                    color: backArea.containsMouse ? "#c8d4de" : "#7e8a97"
                    font.family: Theme.fontUI
                    font.pixelSize: 22
                    Layout.preferredWidth: 16
                    MouseArea {
                        id: backArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: detailPage.back()
                    }
                }

                // Session name (center) — label when not renaming, TextField when renaming
                // Bug fix: inline component declarations inside a Loader are not visible
                // to the Loader's own sourceComponent binding (evaluated in the parent
                // scope).  Give the Loader an id and reference its components via
                // nameLoader.nameLabelComp / nameLoader.renameFieldComp so QML can
                // resolve them correctly at runtime.
                Loader {
                    id: nameLoader
                    Layout.fillWidth: true
                    sourceComponent: detailPage.isRenaming ? nameLoader.renameFieldComp : nameLoader.nameLabelComp

                    component nameLabelComp: Text {
                        text: dv("name", "Session")
                        color: "#e9edf2"
                        font.family: Theme.fontUI
                        font.pixelSize: 15
                        font.bold: true
                        elide: Text.ElideRight
                        horizontalAlignment: Text.AlignLeft
                    }

                    component renameFieldComp: TextField {
                        id: renameField
                        text: dv("name", "")
                        color: "#e9edf2"
                        font.family: Theme.fontUI
                        font.pixelSize: 15
                        font.bold: true
                        horizontalAlignment: Text.AlignLeft
                        background: Rectangle {
                            radius: 4
                            color: "#0e141b"
                            border.color: "#2a3a50"
                            border.width: 1
                        }
                        Component.onCompleted: {
                            forceActiveFocus()
                            selectAll()
                        }
                        Keys.onReturnPressed: {
                            if (text.trim() !== "" && detailPage.vm)
                                detailPage.vm.renameSession(dv("uuid"), text.trim())
                            detailPage.isRenaming = false
                        }
                        Keys.onEnterPressed: {
                            if (text.trim() !== "" && detailPage.vm)
                                detailPage.vm.renameSession(dv("uuid"), text.trim())
                            detailPage.isRenaming = false
                        }
                        Keys.onEscapePressed: {
                            detailPage.isRenaming = false
                        }
                    }
                }

                // Right-side action icons
                Row {
                    spacing: 12

                    // ✎ rename — clicking swaps the title to an inline TextField
                    Text {
                        text: "✎"
                        color: renameArea.containsMouse ? "#a0aab6" : "#566069"
                        font.pixelSize: 14
                        MouseArea {
                            id: renameArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: detailPage.isRenaming = !detailPage.isRenaming
                        }
                    }

                    // Copy session id — self-drawn copy icon (cross-platform;
                    // "⧉" isn't in the bundled fonts, would tofu on macOS).
                    // A "✓" flash for 1s after copy stays as text (✓ is in
                    // every system font; the brief confirm is low-risk).
                    Item {
                        width: 16; height: 16
                        Layout.alignment: Qt.AlignVCenter
                        Icon {
                            anchors.centerIn: parent
                            name: "copy"; size: 14
                            visible: !detailPage.copiedFlash
                            color: copyArea.containsMouse ? "#a0aab6" : "#566069"
                        }
                        Text {
                            anchors.centerIn: parent
                            text: "✓"
                            visible: detailPage.copiedFlash
                            color: "#5fd2a8"
                            font.pixelSize: 14
                        }
                        MouseArea {
                            id: copyArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: {
                                if (detailPage.vm && dv("uuid") !== "") {
                                    detailPage.vm.copyId(dv("uuid"))
                                    detailPage.copiedFlash = true
                                    copyFlashTimer.restart()
                                }
                            }
                        }
                    }

                    // ↗ open folder — dispatches REVEAL_CWD via VM
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
                                if (detailPage.vm && dv("uuid") !== "")
                                    detailPage.vm.openFolder(dv("uuid"))
                            }
                        }
                    }

                    // Reset thinking — self-drawn rewind/undo icon (cross-platform;
                    // "⟲" isn't in the bundled fonts). Two-step confirm: first tap
                    // arms (shows "Confirm?" text), second tap within 3s fires.
                    Item {
                        width: detailPage.resetArmed ? confirmLbl.implicitWidth : 16
                        height: 16
                        Layout.alignment: Qt.AlignVCenter
                        Icon {
                            anchors.centerIn: parent
                            name: "reset"; size: 14
                            visible: !detailPage.resetArmed
                            color: resetArea.containsMouse ? "#e8743b" : "#4a2222"
                        }
                        Text {
                            id: confirmLbl
                            anchors.centerIn: parent
                            text: "Confirm?"
                            visible: detailPage.resetArmed
                            color: "#ef4444"
                            font.pixelSize: 11
                        }
                        MouseArea {
                            id: resetArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            onClicked: {
                                if (!detailPage.resetArmed) {
                                    // First click: arm the action
                                    detailPage.resetArmed = true
                                    resetArmTimer.restart()
                                } else {
                                    // Second click within timeout: fire
                                    detailPage.resetArmed = false
                                    resetArmTimer.stop()
                                    if (detailPage.vm && dv("uuid") !== "")
                                        detailPage.vm.resetThinking(dv("uuid"))
                                }
                            }
                        }
                    }
                }
            }
        }

        // ── dpttl: phase badge · model · cost (right) ─────────────────────
        // Mirrors the prototype's ".dpttl" row: a coloured phase pill, the
        // model id in mono, and the session cost pushed to the right in gold.
        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 16
            Layout.rightMargin: 16
            Layout.topMargin: 2
            Layout.bottomMargin: 10
            spacing: 8

            // Phase badge — coloured pill (amber=thinking, teal=active, grey=idle).
            Rectangle {
                visible: dv("phase") !== ""
                radius: 5
                color: Qt.rgba(Theme.phaseColor(dv("phase")).r,
                               Theme.phaseColor(dv("phase")).g,
                               Theme.phaseColor(dv("phase")).b, 0.12)
                Layout.preferredWidth: phaseLbl.implicitWidth + 14
                Layout.preferredHeight: 20
                Text {
                    id: phaseLbl
                    anchors.centerIn: parent
                    text: dv("phase")
                    color: dv("phase") === "idle" ? "#8a96a3" : Theme.phaseColor(dv("phase"))
                    font.family: Theme.fontMono
                    font.pixelSize: 11
                }
            }
            // Model id — mono, model-tinted.
            Text {
                text: dv("model")
                color: Theme.modelColor(dv("model"))
                font.family: Theme.fontMono
                font.pixelSize: 12
                visible: dv("model") !== ""
                elide: Text.ElideRight
            }
            Item { Layout.fillWidth: true }
            // Cost — gold, bold, mono, right-aligned.
            Text {
                text: fmtCost(dvNum("cost"))
                color: "#f0a860"
                font.family: Theme.fontMono
                font.pixelSize: 14
                font.bold: true
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

                // Transcript row — clickable to open in default app (only when non-empty)
                DetailRowClickable {
                    label: "Transcript"
                    value: dv("transcript_path")
                    monospace: true
                    visible: dv("transcript_path") !== ""
                    onRowClicked: {
                        if (detailPage.vm && dv("transcript_path") !== "")
                            detailPage.vm.openTranscript(dv("transcript_path"))
                    }
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
                            font.family: Theme.fontMono
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
                                font.family: Theme.fontMono
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
                                font.family: Theme.fontMono
                                wrapMode: Text.WrapAtWordBoundaryOrAnywhere
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
                font.family: Theme.fontUI
                font.pixelSize: 12
                Layout.preferredWidth: 80
                Layout.minimumWidth: 80
            }
            Text {
                text: value
                color: "#c8d4de"
                // Explicit family on BOTH branches — an empty "" string here
                // left Qt to pick an arbitrary fallback (the garbled-font bug);
                // the bundled Inter / JetBrains Mono are the only two we ship.
                font.family: monospace ? Theme.fontMono : Theme.fontUI
                font.pixelSize: 12
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

    // Clickable variant of DetailRow — emits rowClicked when the row is pressed.
    // Used for the Transcript row so the user can open the file directly.
    component DetailRowClickable: Rectangle {
        required property string label
        required property string value
        property bool monospace: false

        signal rowClicked()

        Layout.fillWidth: true
        height: 32
        color: rowClickArea.containsMouse ? "#080c12" : "transparent"

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            Text {
                text: label
                color: "#7e8a97"
                font.family: Theme.fontUI
                font.pixelSize: 12
                Layout.preferredWidth: 80
                Layout.minimumWidth: 80
            }
            Text {
                text: value
                // Highlight the value on hover to indicate clickability
                color: rowClickArea.containsMouse ? "#5fa8d2" : "#c8d4de"
                // Explicit family on both branches (see DetailRow note).
                font.family: monospace ? Theme.fontMono : Theme.fontUI
                font.pixelSize: 12
                elide: Text.ElideLeft
                Layout.fillWidth: true
            }
            // Small "open" affordance shown on hover
            Text {
                text: "↗"
                color: "#5fa8d2"
                font.pixelSize: 10
                visible: rowClickArea.containsMouse
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
        MouseArea {
            id: rowClickArea
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            hoverEnabled: true
            onClicked: parent.rowClicked()
        }
    }
}
