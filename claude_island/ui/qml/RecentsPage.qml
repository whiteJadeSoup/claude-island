import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "."

// Recents (dormant sessions) — History redesign as a left-rail TIMELINE.
// Props:  recents — QVariantList of {
//             name, cwd, last_activity_ts (epoch seconds, float),
//             cost_usd, session_uuid, turns (optional), transcript_path
//         }  — model info is intentionally NOT shown (path matters, model doesn't)
//         vm      — worldVm (for resumeSession slot)
// Signal: back()  — parent connects to `page = "home"`
Rectangle {
    id: recentsPage

    required property var recents
    required property var vm

    signal back()

    color: Theme.bg

    // ── Helper: format cost ────────────────────────────────────────────────
    function fmtCost(n) {
        n = n || 0
        return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2))
    }

    // ── Helper: relative time from epoch seconds ───────────────────────────
    // Coarse buckets: "Nm ago" / "Nh ago" / "Nd ago". (Task-specified shape.)
    function fmtRelative(tsSec) {
        if (!tsSec) return ""
        var diff = Date.now() / 1000 - tsSec
        if (diff < 3600)  return Math.max(1, Math.floor(diff / 60)) + "m ago"
        if (diff < 86400) return Math.floor(diff / 3600) + "h ago"
        return Math.floor(diff / 86400) + "d ago"
    }

    // ── Helper: recency group label from epoch seconds ─────────────────────
    // Rolling-window buckets (Task-specified): "Today" / "Yesterday" / "Earlier".
    function groupOf(tsSec) {
        var diff = Date.now() / 1000 - tsSec
        if (diff < 86400)  return "Today"
        if (diff < 172800) return "Yesterday"
        return "Earlier"
    }

    // ── Helper: build the flat list fed to the Repeater ───────────────────
    // Returns [{type:"header", label} | {type:"row", item}].  Using a flat
    // model (vs comparing prior index inside the delegate) keeps header
    // insertion logic in one place and the delegate a pure painter.
    function buildFlatList() {
        // Copy before sort: recents may be a live model reference; sorting in
        // place would mutate the source and is undefined for QVariantList.
        var sorted = (recents || []).slice()

        // Fixed newest-first order by last_activity_ts (no sort control).
        sorted.sort(function(a, b) {
            return (b.last_activity_ts || 0) - (a.last_activity_ts || 0)
        })

        // Group — insert header entries before the first item of each group.
        var flat = []
        var lastGroup = ""
        for (var j = 0; j < sorted.length; j++) {
            var it  = sorted[j]
            var grp = groupOf(it.last_activity_ts || 0)
            if (grp !== lastGroup) {
                flat.push({ type: "header", label: grp })
                lastGroup = grp
            }
            flat.push({ type: "row", item: it })
        }
        return flat
    }

    // ── Reactive flat list — rebuilds when recents changes ────────────────
    property var flatList: []

    onRecentsChanged: flatList = buildFlatList()

    Component.onCompleted: flatList = buildFlatList()

    // ── Meta line for a row: "<cwd> · <turns> turns · <relative>" ──────────
    // No folder glyph in the string — it's drawn as a self-drawn Icon at the
    // row's leading edge (the "📁" emoji renders colour on macOS vs mono on
    // Windows; self-draw keeps it consistent cross-platform).
    function metaLine(item) {
        var parts = []
        if (item.cwd) parts.push(item.cwd)
        if (item.turns !== undefined && item.turns !== null)
            parts.push(item.turns + " turns")
        var ago = fmtRelative(item.last_activity_ts || 0)
        if (ago) parts.push(ago)
        return parts.join(" · ")
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Header — ‹ back · History · {N} sessions ─────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            color: "transparent"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                spacing: 8

                // Back: icon-only ‹ glyph (matches SpendPage header pattern)
                Text {
                    text: "‹"
                    color: backArea.containsMouse ? Theme.ink : Theme.dim
                    font.pixelSize: 20
                    Layout.alignment: Qt.AlignVCenter
                    MouseArea {
                        id: backArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: recentsPage.back()
                    }
                }

                Text {
                    text: "History"
                    color: Theme.ink
                    font.pixelSize: Theme.tTitle
                    font.bold: true
                }

                Item { Layout.fillWidth: true }

                // Session count, right-aligned, faint mono.
                Text {
                    text: (recentsPage.recents ? recentsPage.recents.length : 0) + " sessions"
                    color: Theme.faint
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.tMicro
                    Layout.alignment: Qt.AlignVCenter
                }
            }
        }

        // ── Empty state: no sessions at all ──────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: !recents || recents.length === 0

            Text {
                anchors.centerIn: parent
                text: "No history yet"
                color: Theme.faint
                font.pixelSize: 13
            }
        }

        // ── Timeline list ─────────────────────────────────────────────────
        Flickable {
            // objectName used by test_qml_no_warnings.py to locate this
            // Flickable and assert contentHeight > 50 (geometry regression
            // guard — rows must have real height, never collapse to 0).
            objectName: "recentsListFlickable"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentHeight: listCol.height
            clip: true
            visible: recents && recents.length > 0

            ScrollBar.vertical: ScrollBar {
                width: 5
                policy: ScrollBar.AsNeeded
                contentItem: Rectangle {
                    implicitWidth: 5
                    radius: 2
                    color: Theme.bd2
                    opacity: parent.active ? 0.8 : 0.4
                }
                background: Item {}
            }

            // Left timeline rail — a thin vertical line spanning the full list
            // height. It sits behind the rows; each row paints its own node dot
            // aligned to this rail's x.  RAIL_X is the rail centre, in list
            // coordinates; row delegates reuse it to place their node.
            readonly property int railX: 22

            Rectangle {
                // The rail runs the height of the content, behind every row.
                x: parent.railX - 1
                y: 0
                width: 2
                height: listCol.height
                color: "#181d24"
            }

            ColumnLayout {
                id: listCol
                width: parent.width
                spacing: 0

                Repeater {
                    model: recentsPage.flatList
                    // Inline delegate (NOT a Loader): a ColumnLayout child Loader
                    // does not adopt its item's height, collapsing every row to
                    // 0 and emptying the list. A direct Item with an explicit
                    // Layout.preferredHeight measures correctly under Qt 6.
                    delegate: Item {
                        id: rowRoot
                        required property var modelData
                        required property int index

                        readonly property bool isHeader: modelData.type === "header"
                        // The dict for a row entry ({} for a header so bindings stay safe)
                        readonly property var rowItem: isHeader ? ({}) : (modelData.item || {})

                        Layout.fillWidth: true
                        Layout.preferredHeight: isHeader ? 30 : (rowContentHolder.implicitHeight + 22)

                        // ── Group header variant ──────────────────────────────
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 16
                            anchors.rightMargin: 16
                            visible: rowRoot.isHeader

                            Text {
                                text: rowRoot.isHeader ? (rowRoot.modelData.label || "") : ""
                                color: Theme.faint
                                font.pixelSize: 10
                                font.letterSpacing: 1.5
                                font.bold: true
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 1
                                Layout.alignment: Qt.AlignVCenter
                                Layout.leftMargin: 8
                                color: Theme.bd
                            }
                        }

                        // ── Timeline node dot — aligned to the rail ───────────
                        // 9px circle on the rail; teal + glow when the row is
                        // hovered, otherwise a dim filled node with a ring.
                        Rectangle {
                            id: node
                            visible: !rowRoot.isHeader
                            width: 9
                            height: 9
                            radius: 4.5
                            // Centre on the rail line, vertically near the title.
                            x: 22 - width / 2
                            y: 18
                            color: rowHover.containsMouse ? Theme.teal : Theme.bg
                            border.width: 2
                            border.color: rowHover.containsMouse ? Theme.teal : "#2e3742"

                            // Hover glow ring around the node
                            Rectangle {
                                anchors.centerIn: parent
                                width: 18
                                height: 18
                                radius: 9
                                color: "transparent"
                                border.width: 1
                                border.color: Theme.teal
                                opacity: rowHover.containsMouse ? 0.45 : 0.0
                                Behavior on opacity { NumberAnimation { duration: 140 } }
                            }
                        }

                        // ── Session row variant ───────────────────────────────
                        Rectangle {
                            id: rowCard
                            anchors.left: parent.left
                            anchors.right: parent.right
                            // Indent past the rail so content clears the node.
                            anchors.leftMargin: 38
                            anchors.rightMargin: 13
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            anchors.bottomMargin: 6
                            visible: !rowRoot.isHeader
                            radius: 8
                            color: rowHover.containsMouse ? Theme.surface2 : "transparent"
                            border.color: rowHover.containsMouse ? Theme.bd2 : "transparent"
                            border.width: 1

                            // Card-wide hover/click area FIRST (behind content) so the
                            // nested resume-pill MouseArea, which sits on top, receives
                            // its own clicks. A plain card click resumes the session
                            // (primary action for a dormant session); nested morph
                            // detail is out of scope here.
                            MouseArea {
                                id: rowHover
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    if (recentsPage.vm && rowRoot.rowItem.session_uuid)
                                        recentsPage.vm.resumeSession(rowRoot.rowItem.session_uuid)
                                }
                            }

                            ColumnLayout {
                                id: rowContentHolder
                                anchors.top: parent.top
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.topMargin: 9
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                spacing: 3

                                // First line: project name (phosphor) + cost
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8
                                    Text {
                                        Layout.fillWidth: true
                                        text: rowRoot.rowItem.name || "Unknown session"
                                        color: Theme.phos
                                        font.pixelSize: 13
                                        font.bold: true
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        text: recentsPage.fmtCost(rowRoot.rowItem.cost_usd || 0)
                                        color: Theme.costColor(rowRoot.rowItem.cost_usd || 0)
                                        font.family: Theme.fontMono
                                        font.pixelSize: 12
                                        font.bold: true
                                    }
                                }

                                // Second line: [folder icon] <cwd> · <turns> · <relative>
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 5
                                    visible: metaText.text !== ""
                                    Icon {
                                        name: "folder"
                                        size: 11
                                        color: Theme.faint
                                        Layout.alignment: Qt.AlignVCenter
                                        visible: (rowRoot.rowItem.cwd || "") !== ""
                                    }
                                    Text {
                                        id: metaText
                                        Layout.fillWidth: true
                                        text: recentsPage.metaLine(rowRoot.rowItem)
                                        color: Theme.faint
                                        font.family: Theme.fontMono
                                        font.pixelSize: 10
                                        elide: Text.ElideRight
                                    }
                                }

                                // Hover-revealed action row: ↻ resume pill
                                RowLayout {
                                    Layout.fillWidth: true
                                    Layout.topMargin: 4
                                    spacing: 8
                                    visible: rowHover.containsMouse

                                    Rectangle {
                                        Layout.preferredWidth: resumeLabel.width + 16
                                        Layout.preferredHeight: 24
                                        radius: 6
                                        color: resumeArea.containsMouse ? "#16261f" : "transparent"
                                        border.color: Theme.teal
                                        border.width: 1
                                        Text {
                                            id: resumeLabel
                                            anchors.centerIn: parent
                                            text: "↻ resume"
                                            color: Theme.teal
                                            font.pixelSize: 11
                                        }
                                        MouseArea {
                                            id: resumeArea
                                            anchors.fill: parent
                                            cursorShape: Qt.PointingHandCursor
                                            hoverEnabled: true
                                            onClicked: {
                                                if (recentsPage.vm && rowRoot.rowItem.session_uuid)
                                                    recentsPage.vm.resumeSession(rowRoot.rowItem.session_uuid)
                                            }
                                        }
                                    }

                                    Item { Layout.fillWidth: true }
                                }
                            }
                        }
                    }
                }

                // Bottom padding
                Item { Layout.preferredHeight: 12 }
            }
        }
    }
}
