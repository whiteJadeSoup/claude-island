import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "."

// Single decision card — approval swipe or question options.
// Required properties: decision (dict from vmDecisions), vm (worldVm).
// The card is stateless between re-parenting; all transient animation
// state lives in the implicitly-created item tree below.
Rectangle {
    id: card

    required property var decision
    required property var vm

    // Guard every binding with decision && so the component survives a
    // momentary null during the first pass.
    readonly property bool isQuestion: decision && decision.kind === "ask_question"
    readonly property bool isHighRisk: decision && decision.risk === "high"
    readonly property string displayText:
        decision ? (decision.tool_input_preview || decision.tool_name || "") : ""

    // Swipe thresholds (approval cards only)
    readonly property real swipeThreshold: 80

    // Swipe drag state — tracks translation driven by the DragHandler
    property real cardX: 0

    // Computed border colour tracks drag direction
    readonly property color borderColor: {
        if (isQuestion) return "#4a3320"
        if (cardX > swipeThreshold)  return "#5fd2a8"
        if (cardX < -swipeThreshold) return "#e8743b"
        return "#4a3320"
    }

    radius: 12
    color: "#1a1410"
    border.color: borderColor
    border.width: 1

    // Drive the card's height from its content. The content ColumnLayout is
    // anchored top/left/right (not bottom/fill), so without this the Rectangle's
    // implicitHeight stays 0 — and the Loader that hosts this card in Main.qml's
    // `bands` ColumnLayout then reserves zero vertical space, letting TODAY /
    // Active / Idle overlap the card (Images #6/#7). +28 = the 14px top + 14px
    // bottom anchors.margins around contentCol.
    implicitHeight: contentCol.implicitHeight + 28

    // Left accent strip (orange) — visual design detail
    Rectangle {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: card.radius
        anchors.bottomMargin: card.radius
        width: 3
        color: "#e8743b"
    }

    // The card x-translation is updated by the DragHandler (approval only)
    transform: Translate { x: card.cardX }

    // ── Swipe drag (approval cards only) ─────────────────────────────────
    // DragHandler coexists with child MouseAreas/TapHandlers; a covering
    // MouseArea would steal button clicks — do NOT replace this.
    DragHandler {
        id: dragHandler
        enabled: !card.isQuestion
        yAxis.enabled: false
        xAxis.enabled: true

        onActiveChanged: {
            if (!active) {
                // Release: commit or snap back
                if (card.cardX > card.swipeThreshold) {
                    // approve this-time
                    card.vm.approve(card.decision.id, false)
                    snapBack.start()
                } else if (card.cardX < -card.swipeThreshold) {
                    // deny
                    card.vm.deny(card.decision.id)
                    snapBack.start()
                } else {
                    snapBack.start()
                }
            }
        }

        // Bind cardX to the drag translation while active
        onTranslationChanged: {
            if (active) {
                card.cardX = translation.x
            }
        }
    }

    NumberAnimation {
        id: snapBack
        target: card
        property: "cardX"
        to: 0
        duration: 200
        easing.type: Easing.OutCubic
    }

    // ── Swipe hint labels (approval cards only) ──────────────────────────
    Text {
        visible: !card.isQuestion
        anchors.left: parent.left
        anchors.leftMargin: 16
        anchors.verticalCenter: parent.verticalCenter
        text: "Allow →"
        color: "#5fd2a8"
        font.pixelSize: 12
        font.bold: true
        // Fade in as user drags right past 20 px
        opacity: Math.min(1.0, Math.max(0.0, (card.cardX - 20) / (card.swipeThreshold - 20)))
    }
    Text {
        visible: !card.isQuestion
        anchors.right: parent.right
        anchors.rightMargin: 16
        anchors.verticalCenter: parent.verticalCenter
        text: "← Deny"
        color: "#e8743b"
        font.pixelSize: 12
        font.bold: true
        opacity: Math.min(1.0, Math.max(0.0, (-card.cardX - 20) / (card.swipeThreshold - 20)))
    }

    // ── Main content column ───────────────────────────────────────────────
    ColumnLayout {
        id: contentCol
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 14
        spacing: 8

        // Header row: session name + kind tag + (optional) high-risk badge
        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Text {
                text: card.decision ? card.decision.session_name : ""
                color: "#f4efe9"
                font.pixelSize: 13
                font.bold: true
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            // Kind tag
            Rectangle {
                radius: 4
                color: "#2a1e0f"
                border.color: "#4a3320"
                border.width: 1
                width: tagLabel.width + 10
                height: 20
                Text {
                    id: tagLabel
                    anchors.centerIn: parent
                    text: card.isQuestion ? "question" : "approval"
                    color: "#e8884c"
                    font.pixelSize: 10
                }
            }

            // High-risk badge (approval cards)
            Rectangle {
                visible: !card.isQuestion && card.isHighRisk
                radius: 4
                color: "#3a1005"
                border.color: "#e8743b"
                border.width: 1
                width: riskLabel.width + 10
                height: 20
                Text {
                    id: riskLabel
                    anchors.centerIn: parent
                    text: "high risk"
                    color: "#e8743b"
                    font.pixelSize: 10
                    font.bold: true
                }
            }
        }

        // ── QUESTION card body ────────────────────────────────────────────
        Loader {
            Layout.fillWidth: true
            active: card.isQuestion
            visible: active
            sourceComponent: questionBody
        }

        // ── APPROVAL card body ────────────────────────────────────────────
        Loader {
            Layout.fillWidth: true
            active: !card.isQuestion
            visible: active
            sourceComponent: approvalBody
        }

        // Bottom margin spacer
        Item { Layout.preferredHeight: 6 }
    }

    // ── Question body component ───────────────────────────────────────────
    Component {
        id: questionBody
        ColumnLayout {
            spacing: 6
            width: parent ? parent.width : 0

            // Multi-select state: tracks which option indices are checked.
            // Stored as a JS object (used as a set) for O(1) toggle/check.
            // Reset when the card decision changes so a new question starts clean.
            property var selectedIndices: ({})

            // "Other…" state: whether the free-text field is visible
            property bool showOther: false

            // Question text
            Text {
                Layout.fillWidth: true
                text: card.decision ? (card.decision.question_text || "") : ""
                color: "#ecdfd3"
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }

            // Option rows — behaviour differs for multi-select vs single-select:
            //   single: click immediately resolves the question (existing behaviour)
            //   multi:  click toggles a checkbox; a Submit button commits the choice
            Repeater {
                model: card.decision ? card.decision.options : []
                delegate: Rectangle {
                    id: optBox
                    required property var modelData
                    required property int index

                    // Alias for brevity; parent is the ColumnLayout
                    property bool isMulti: card.decision && card.decision.multi_select
                    property bool isChecked: parent.selectedIndices && (index in parent.selectedIndices)

                    Layout.fillWidth: true
                    // Layout child: reserve height via Layout.preferredHeight,
                    // NOT `height:` (which the ColumnLayout ignores → rows would
                    // reserve 0 and overlap — the unreadable question card bug).
                    // +16 = the RowLayout's 8px top + 8px bottom anchors.margins.
                    Layout.preferredHeight: optCol.implicitHeight + 16
                    radius: 6
                    color: optArea.containsMouse ? "#231a10" : (isChecked ? "#1a2010" : "transparent")
                    border.color: optArea.containsMouse ? "#4a3320"
                                : (isChecked ? "#3a5020" : "transparent")
                    border.width: 1

                    RowLayout {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 8
                        spacing: 8

                        // Leading boxed number — the terminal-menu affordance.
                        // For multi-select it doubles as the checked indicator
                        // (shows ✓ + filled when selected), so the old separate
                        // checkbox Rectangle was removed to avoid two indicators.
                        Rectangle {
                            Layout.preferredWidth: 22; Layout.preferredHeight: 22
                            Layout.alignment: Qt.AlignTop
                            radius: 6
                            color: (optBox.isMulti && optBox.isChecked) ? "#1a3a28" : "transparent"
                            border.color: (optBox.isMulti && optBox.isChecked) ? Theme.teal : "#4d4220"
                            border.width: 1
                            Text {
                                anchors.centerIn: parent
                                text: (optBox.isMulti && optBox.isChecked) ? "✓" : (optBox.index + 1)
                                color: Theme.amber
                                font.family: Theme.fontMono
                                font.pixelSize: 11
                                font.bold: true
                            }
                        }

                        ColumnLayout {
                            id: optCol
                            Layout.fillWidth: true
                            spacing: 2

                            Text {
                                Layout.fillWidth: true
                                text: modelData
                                color: "#f4efe9"
                                font.pixelSize: 12
                                font.bold: true
                                wrapMode: Text.Wrap
                            }
                            Text {
                                Layout.fillWidth: true
                                visible: {
                                    var desc = card.decision && card.decision.option_descriptions
                                    return desc && index < desc.length && desc[index] !== ""
                                }
                                text: {
                                    var desc = card.decision && card.decision.option_descriptions
                                    return (desc && index < desc.length) ? desc[index] : ""
                                }
                                color: Theme.dim
                                font.pixelSize: 11
                                wrapMode: Text.Wrap
                            }
                        }
                    }

                    MouseArea {
                        id: optArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: {
                            if (!card.decision) return
                            if (parent.isMulti) {
                                // Toggle selection: add or remove from selectedIndices
                                var sel = Object.assign({}, parent.parent.selectedIndices)
                                if (index in sel) {
                                    delete sel[index]
                                } else {
                                    sel[index] = modelData
                                }
                                parent.parent.selectedIndices = sel
                            } else {
                                // Single-select: resolve immediately
                                card.vm.answerQuestion(
                                    card.decision.id,
                                    card.decision.question_text,
                                    modelData
                                )
                            }
                        }
                    }
                }
            }

            // "Other…" toggle row — always visible for question cards
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 34
                radius: 6
                color: otherToggleArea.containsMouse ? "#231a10" : "transparent"
                border.color: "transparent"

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 8
                    anchors.rightMargin: 8
                    spacing: 6

                    // Small pencil icon
                    Text {
                        text: "✎"
                        color: "#7e5a3a"
                        font.pixelSize: 11
                    }
                    Text {
                        text: parent.parent.parent.showOther ? "Other… (hide)" : "Other…"
                        color: otherToggleArea.containsMouse ? "#c8a080" : "#566069"
                        font.pixelSize: 11
                    }
                    Item { Layout.fillWidth: true }
                }

                MouseArea {
                    id: otherToggleArea
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    onClicked: parent.parent.showOther = !parent.parent.showOther
                }
            }

            // Free-text field — revealed when showOther is true
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6
                visible: parent.showOther

                TextField {
                    id: otherField
                    Layout.fillWidth: true
                    placeholderText: "Type your answer…"
                    color: "#e9edf2"
                    font.pixelSize: 12
                    background: Rectangle {
                        radius: 6
                        color: "#0e141b"
                        border.color: otherField.activeFocus ? "#3a5020" : "#1c2632"
                        border.width: 1
                    }
                    Keys.onReturnPressed: {
                        if (text.trim() !== "" && card.decision)
                            card.vm.answerQuestion(card.decision.id, card.decision.question_text, text.trim())
                    }
                }

                // Submit free-text button
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 28
                    radius: 6
                    color: otherSubmitArea.containsMouse ? "#1a2a20" : "#0e1a14"
                    border.color: otherSubmitArea.containsMouse ? "#5fd2a8" : "#2a3a20"
                    border.width: 1

                    Text {
                        anchors.centerIn: parent
                        text: "Submit answer"
                        color: otherSubmitArea.containsMouse ? "#5fd2a8" : "#7e8a97"
                        font.pixelSize: 11
                    }

                    MouseArea {
                        id: otherSubmitArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: {
                            var txt = otherField.text.trim()
                            if (txt !== "" && card.decision)
                                card.vm.answerQuestion(card.decision.id, card.decision.question_text, txt)
                        }
                    }
                }
            }

            // Multi-select Submit button — only shown when multi_select and ≥1 selected
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 30
                radius: 6
                visible: card.decision && card.decision.multi_select &&
                         Object.keys(parent.selectedIndices).length > 0
                color: multiSubmitArea.containsMouse ? "#1a3020" : "#0e2018"
                border.color: multiSubmitArea.containsMouse ? "#5fd2a8" : "#2a4030"
                border.width: 1

                Text {
                    anchors.centerIn: parent
                    text: "Submit (" + Object.keys(parent.parent.selectedIndices).length + ")"
                    color: multiSubmitArea.containsMouse ? "#5fd2a8" : "#7eaa8a"
                    font.pixelSize: 12
                    font.bold: true
                }

                MouseArea {
                    id: multiSubmitArea
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    onClicked: {
                        if (!card.decision) return
                        var sel = parent.parent.selectedIndices
                        var keys = Object.keys(sel)
                        if (keys.length === 0) return
                        // Build the labels array in original option order
                        var opts = card.decision.options
                        var labels = []
                        for (var i = 0; i < opts.length; i++) {
                            if (i in sel) labels.push(opts[i])
                        }
                        card.vm.answerQuestionMulti(card.decision.id, card.decision.question_text, labels)
                    }
                }
            }

            // Jump to terminal row — wired to vm.focusSession via session_uuid
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 34
                radius: 6
                color: termArea.containsMouse ? "#1a1410" : "transparent"
                border.color: "transparent"

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 8
                    anchors.rightMargin: 8
                    spacing: 6
                    Text {
                        text: "↗"
                        color: termArea.containsMouse ? "#7eb8d2" : "#566069"
                        font.pixelSize: 11
                    }
                    Text {
                        text: "Jump to terminal"
                        color: termArea.containsMouse ? "#7eb8d2" : "#566069"
                        font.pixelSize: 11
                    }
                }

                MouseArea {
                    id: termArea
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    onClicked: {
                        // session_uuid is included in the decision dict by _decision()
                        // in snapshot_projection.py so focusSession can look up the view.
                        if (card.vm && card.decision && card.decision.session_uuid)
                            card.vm.focusSession(card.decision.session_uuid)
                    }
                }
            }
        }
    }

    // ── Approval body component ───────────────────────────────────────────
    Component {
        id: approvalBody
        ColumnLayout {
            spacing: 10
            width: parent ? parent.width : 0

            // Command/preview text
            Text {
                Layout.fillWidth: true
                text: card.displayText
                color: "#ecdfd3"
                font.pixelSize: 12
                font.family: Theme.fontMono
                wrapMode: Text.Wrap
                maximumLineCount: 4
                elide: Text.ElideRight
            }

            // Button row
            RowLayout {
                Layout.fillWidth: true
                spacing: 8

                // Allow once (primary)
                Rectangle {
                    Layout.preferredHeight: 32
                    radius: 7
                    color: allowOnce.containsMouse ? "#d06830" : "#e8743b"
                    Layout.fillWidth: true
                    Text {
                        anchors.centerIn: parent
                        text: "Allow once"
                        color: "#180c05"
                        font.pixelSize: 12
                        font.bold: true
                    }
                    MouseArea {
                        id: allowOnce
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: { if (card.decision) card.vm.approve(card.decision.id, false) }
                    }
                }

                // Always allow (secondary)
                Rectangle {
                    Layout.preferredHeight: 32
                    radius: 7
                    color: "transparent"
                    border.color: "#4a3320"
                    border.width: 1
                    Layout.fillWidth: true
                    Text {
                        anchors.centerIn: parent
                        text: "Always"
                        color: allowAlways.containsMouse ? "#f4efe9" : "#e9c9b3"
                        font.pixelSize: 12
                    }
                    MouseArea {
                        id: allowAlways
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: { if (card.decision) card.vm.approve(card.decision.id, true) }
                    }
                }

                // Deny (secondary)
                Rectangle {
                    Layout.preferredHeight: 32
                    radius: 7
                    color: "transparent"
                    border.color: "#4a3320"
                    border.width: 1
                    Layout.preferredWidth: denyLabel.implicitWidth + 24
                    Text {
                        id: denyLabel
                        anchors.centerIn: parent
                        text: "Deny"
                        color: denyBtn.containsMouse ? "#e8743b" : "#e9c9b3"
                        font.pixelSize: 12
                    }
                    MouseArea {
                        id: denyBtn
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: { if (card.decision) card.vm.deny(card.decision.id) }
                    }
                }
            }

            // Persistent swipe hint, language-neutral so it renders identically
            // cross-platform (the bundled latin fonts have no CJK glyphs; a
            // Chinese hint would fall back to a per-OS CJK system font). Mirrors
            // the prototype's centered rest-state line; the edge labels still
            // fade in with directional colour during the actual drag.
            Text {
                Layout.fillWidth: true
                Layout.topMargin: 2
                text: "← swipe to deny · swipe to allow →"
                color: "#7d5838"
                font.pixelSize: 10
                horizontalAlignment: Text.AlignHCenter
            }
        }
    }
}
