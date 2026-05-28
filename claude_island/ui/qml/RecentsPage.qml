import QtQuick
import QtQuick.Layouts
import QtQuick.Controls

// Recents (dormant sessions) — History redesign.
// Props:  recents — QVariantList of {
//             name, cwd, last_seen, last_activity_ts, cost_usd, session_uuid,
//             turns (optional), model (optional)
//         }
//         vm      — worldVm (for resumeSession slot)
// Signal: back()  — parent connects to `page = "home"`
Rectangle {
    id: recentsPage

    required property var recents
    required property var vm

    signal back()

    color: "#0c0f14"

    // ── Sort mode: "recent" | "cost" ───────────────────────────────────────
    property string sortMode: "recent"

    // ── Search text ────────────────────────────────────────────────────────
    property string searchText: ""

    // ── Helper: format cost ────────────────────────────────────────────────
    function fmtCost(n) {
        n = n || 0
        return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2))
    }

    // ── Helper: relative time from epoch seconds ───────────────────────────
    // Returns "3h ago" / "1d ago" / "5d ago" / "just now" etc.
    function fmtAgo(ts) {
        if (!ts || ts <= 0) return ""
        var now = Date.now() / 1000  // epoch seconds
        var diff = Math.max(0, now - ts)
        if (diff < 60)          return "just now"
        if (diff < 3600)        return Math.floor(diff / 60) + "m ago"
        if (diff < 86400)       return Math.floor(diff / 3600) + "h ago"
        if (diff < 7 * 86400)   return Math.floor(diff / 86400) + "d ago"
        return Math.floor(diff / 86400) + "d ago"
    }

    // ── Helper: compute group label from epoch seconds ─────────────────────
    // Returns "Today" / "Yesterday" / "Earlier"
    function groupOf(ts) {
        if (!ts || ts <= 0) return "Earlier"
        var now = new Date()
        var d   = new Date(ts * 1000)
        // Compare calendar dates in local time
        var nowMidnight = new Date(now.getFullYear(), now.getMonth(), now.getDate())
        var dMidnight   = new Date(d.getFullYear(),   d.getMonth(),   d.getDate())
        var diffDays = Math.round((nowMidnight - dMidnight) / 86400000)
        if (diffDays === 0) return "Today"
        if (diffDays === 1) return "Yesterday"
        return "Earlier"
    }

    // ── Helper: build the flat list fed to the Repeater ───────────────────
    // Returns [{type:"header", label:""} | {type:"row", ...item...}]
    // Applies search filter, sort, and group insertion.
    function buildFlatList() {
        var src = recents || []

        // 1. Search filter (name + cwd, case-insensitive)
        var q = searchText.trim().toLowerCase()
        var filtered = []
        for (var i = 0; i < src.length; i++) {
            var item = src[i]
            if (q === "") {
                filtered.push(item)
            } else {
                var name = (item.name || "").toLowerCase()
                var cwd  = (item.cwd  || "").toLowerCase()
                if (name.indexOf(q) !== -1 || cwd.indexOf(q) !== -1)
                    filtered.push(item)
            }
        }

        // 2. Sort
        filtered.sort(function(a, b) {
            if (sortMode === "cost") {
                return (b.cost_usd || 0) - (a.cost_usd || 0)
            }
            // default: most-recent first
            return (b.last_activity_ts || 0) - (a.last_activity_ts || 0)
        })

        // 3. Group — insert header entries before the first item of each group
        var flat = []
        var lastGroup = ""
        for (var j = 0; j < filtered.length; j++) {
            var it  = filtered[j]
            var grp = groupOf(it.last_activity_ts || 0)
            if (grp !== lastGroup) {
                flat.push({ type: "header", label: grp })
                lastGroup = grp
            }
            flat.push({ type: "row", item: it })
        }
        return flat
    }

    // ── Reactive flat list — rebuilds when recents / search / sort changes ─
    property var flatList: []

    // Recompute whenever any of the three inputs change
    onRecentsChanged:    flatList = buildFlatList()
    onSearchTextChanged: flatList = buildFlatList()
    onSortModeChanged:   flatList = buildFlatList()

    Component.onCompleted: flatList = buildFlatList()

    // ── Build meta line from an item dict ─────────────────────────────────
    function metaLine(item) {
        var parts = []
        var ago = fmtAgo(item.last_activity_ts || 0)
        if (ago) parts.push(ago)
        if (item.turns !== undefined && item.turns !== null) parts.push(item.turns + " turns")
        if (item.model) parts.push(item.model)
        // Append cwd as dimmer trailing element (separator from leading parts)
        var line = parts.join(" · ")
        return line
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
                    text: "‹ Back"
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
                    text: "History"
                    color: "#e9edf2"
                    font.pixelSize: 13
                    font.bold: true
                }

                Item { Layout.fillWidth: true }
                // Spacer to keep title centered (mirrors back arrow width)
                Item { width: 42 }
            }
        }

        // ── Search + Sort bar ─────────────────────────────────────────────
        Rectangle {
            Layout.fillWidth: true
            Layout.leftMargin: 13
            Layout.rightMargin: 13
            Layout.bottomMargin: 8
            height: 34
            color: "#080c11"
            radius: 8
            border.color: searchField.activeFocus ? "#2a3a50" : "#151b22"
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 10
                anchors.rightMargin: 10
                spacing: 6

                // Search icon
                Text {
                    text: "⌕"
                    color: "#566069"
                    font.pixelSize: 14
                }

                // Search input + placeholder stack
                Item {
                    Layout.fillWidth: true
                    height: 20

                    TextInput {
                        id: searchField
                        anchors.fill: parent
                        color: "#c8d4de"
                        font.pixelSize: 12
                        selectionColor: "#2a3a50"
                        selectedTextColor: "#e9edf2"
                        clip: true
                        verticalAlignment: TextInput.AlignVCenter
                        onTextChanged: recentsPage.searchText = text
                    }

                    // Placeholder shown when field is empty and not focused
                    Text {
                        anchors.fill: parent
                        text: "Search sessions…"
                        color: "#3a4752"
                        font.pixelSize: 12
                        verticalAlignment: Text.AlignVCenter
                        visible: searchField.text === "" && !searchField.activeFocus
                    }
                }

                // Sort toggle
                Text {
                    id: sortToggle
                    text: recentsPage.sortMode === "recent" ? "Recent ↓" : "Cost ↓"
                    color: sortToggleArea.containsMouse ? "#5fd2a8" : "#566069"
                    font.pixelSize: 11
                    MouseArea {
                        id: sortToggleArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: recentsPage.sortMode =
                            recentsPage.sortMode === "recent" ? "cost" : "recent"
                    }
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
                color: "#566069"
                font.pixelSize: 13
            }
        }

        // ── Filtered-empty state ──────────────────────────────────────────
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: recents && recents.length > 0 &&
                     recentsPage.searchText.trim() !== "" &&
                     recentsPage.flatList.length === 0

            Text {
                anchors.centerIn: parent
                text: "No matching sessions"
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
            visible: recents && recents.length > 0 && recentsPage.flatList.length > 0

            // Thin scrollbar
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
                id: listCol
                width: parent.width
                spacing: 0

                Repeater {
                    model: recentsPage.flatList
                    delegate: Loader {
                        required property var modelData
                        required property int index

                        Layout.fillWidth: true

                        // Choose header vs row component by type
                        sourceComponent: modelData.type === "header"
                                         ? GroupHeaderComp : SessionRowComp

                        // Pass the data down into whichever component is loaded
                        onLoaded: {
                            if (modelData.type === "header") {
                                item.label = modelData.label
                            } else {
                                item.item   = modelData.item
                                item.vmRef  = recentsPage.vm
                                item.fmtCostFn  = recentsPage.fmtCost
                                item.fmtAgoFn   = recentsPage.fmtAgo
                                item.metaLineFn = recentsPage.metaLine
                            }
                        }
                    }
                }

                // Bottom padding
                Item { height: 12 }
            }
        }
    }

    // ── Group header component ─────────────────────────────────────────────
    component GroupHeaderComp: Item {
        // label property set by Loader.onLoaded
        property string label: ""
        Layout.fillWidth: true
        height: 28

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16

            Text {
                text: label
                color: "#566069"
                font.pixelSize: 10
                font.letterSpacing: 1.5
                font.bold: true
            }
            // Divider line
            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: "#151b22"
                Layout.alignment: Qt.AlignVCenter
                Layout.leftMargin: 8
            }
        }
    }

    // ── Session row component ──────────────────────────────────────────────
    component SessionRowComp: Item {
        // These properties are set by Loader.onLoaded
        property var item: ({})
        property var vmRef: null
        property var fmtCostFn: function(n) { return "$" + n.toFixed(2) }
        property var fmtAgoFn:  function(ts) { return "" }
        property var metaLineFn: function(it) { return "" }

        Layout.fillWidth: true
        Layout.leftMargin: 13
        Layout.rightMargin: 13
        Layout.bottomMargin: 6

        height: rowContent.height + 20

        // Row card background
        Rectangle {
            id: rowCard
            anchors.fill: parent
            radius: 8
            color: rowHover.containsMouse ? "#0e141b" : "#0a1018"
            border.color: rowHover.containsMouse ? "#1c2632" : "#151b22"
            border.width: 1

            // ── Main content ──────────────────────────────────────────────
            ColumnLayout {
                id: rowContent
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.topMargin: 10
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 3

                // ── Top row: name + cost ─────────────────────────────────
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8

                    // Session name
                    Text {
                        Layout.fillWidth: true
                        text: item.name || "Unknown session"
                        color: "#e9edf2"
                        font.pixelSize: 13
                        font.bold: true
                        elide: Text.ElideRight
                    }

                    // Cost
                    Text {
                        text: fmtCostFn(item.cost_usd || 0)
                        color: "#f0a860"
                        font.family: "monospace"
                        font.pixelSize: 12
                        font.bold: true
                    }
                }

                // ── Meta line: cwd ───────────────────────────────────────
                Text {
                    Layout.fillWidth: true
                    text: item.cwd || ""
                    color: "#3a4752"
                    font.family: "monospace"
                    font.pixelSize: 10
                    elide: Text.ElideRight
                    visible: text !== ""
                }

                // ── Meta line: ago · turns · model ───────────────────────
                Text {
                    Layout.fillWidth: true
                    text: metaLineFn(item)
                    color: "#566069"
                    font.pixelSize: 10
                    elide: Text.ElideRight
                    visible: text !== ""
                }

                // ── Hover-revealed action row ─────────────────────────────
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 4
                    spacing: 8
                    visible: rowHover.containsMouse

                    // Resume button
                    Rectangle {
                        width: resumeLabel.width + 16
                        height: 24
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
                                if (vmRef && item.session_uuid)
                                    vmRef.resumeSession(item.session_uuid)
                            }
                        }
                    }

                    // Open transcript button (inert affordance — no slot wired)
                    Rectangle {
                        width: transcriptLabel.width + 16
                        height: 24
                        radius: 6
                        color: transcriptArea.containsMouse ? "#1a1a2a" : "transparent"
                        border.color: transcriptArea.containsMouse ? "#5fa8d2" : "#1c2030"
                        border.width: 1

                        Text {
                            id: transcriptLabel
                            anchors.centerIn: parent
                            text: "⧉ Transcript"
                            color: transcriptArea.containsMouse ? "#5fa8d2" : "#566069"
                            font.pixelSize: 11
                        }

                        MouseArea {
                            id: transcriptArea
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            hoverEnabled: true
                            // Inert — no slot wired; placeholder for future open-transcript action.
                            onClicked: {} // no-op
                        }
                    }

                    Item { Layout.fillWidth: true }
                }
            }

            // Hover area for the entire card — must be on top of content
            MouseArea {
                id: rowHover
                anchors.fill: parent
                hoverEnabled: true
                // Row click does nothing — use action buttons
            }
        }
    }
}
