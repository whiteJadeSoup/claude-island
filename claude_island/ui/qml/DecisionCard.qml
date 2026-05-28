import QtQuick
import QtQuick.Layouts

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
        Item { height: 6 }
    }

    // ── Question body component ───────────────────────────────────────────
    Component {
        id: questionBody
        ColumnLayout {
            spacing: 6
            width: parent ? parent.width : 0

            // Question text
            Text {
                Layout.fillWidth: true
                text: card.decision ? (card.decision.question_text || "") : ""
                color: "#ecdfd3"
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }

            // Option rows
            Repeater {
                model: card.decision ? card.decision.options : []
                delegate: Rectangle {
                    required property var modelData
                    required property int index

                    Layout.fillWidth: true
                    height: optCol.height + 14
                    radius: 6
                    color: optArea.containsMouse ? "#231a10" : "transparent"
                    border.color: optArea.containsMouse ? "#4a3320" : "transparent"
                    border.width: 1

                    ColumnLayout {
                        id: optCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 8
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
                            color: "#7e8a97"
                            font.pixelSize: 11
                            wrapMode: Text.Wrap
                        }
                    }

                    MouseArea {
                        id: optArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: {
                            if (card.decision)
                                card.vm.answerQuestion(
                                    card.decision.id,
                                    card.decision.question_text,
                                    modelData
                                )
                        }
                    }
                }
            }

            // Terminal fallback row (dim, no-op placeholder)
            Rectangle {
                Layout.fillWidth: true
                height: 34
                radius: 6
                color: termArea.containsMouse ? "#1a1410" : "transparent"
                border.color: "transparent"
                Text {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: 8
                    text: "Jump to terminal"
                    color: "#566069"
                    font.pixelSize: 11
                }
                MouseArea {
                    id: termArea
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    // no-op placeholder
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
                font.family: "monospace"
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
                    height: 32
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
                    height: 32
                    radius: 7
                    color: "transparent"
                    border.color: "#4a3320"
                    border.width: 1
                    Layout.fillWidth: true
                    Text {
                        anchors.centerIn: parent
                        text: "Always allow"
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
                    height: 32
                    radius: 7
                    color: "transparent"
                    border.color: "#4a3320"
                    border.width: 1
                    width: denyLabel.width + 24
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
        }
    }
}
