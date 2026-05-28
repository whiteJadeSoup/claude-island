import QtQuick
import QtQuick.Layouts

// Spend detail page — shown when user taps the cost/quota text in the top bar.
// Props:  spend  — the dict returned by vm.spendDetail()
//         quota  — the quota dict from vmQuota (may be null)
//         vm     — worldVm (for refreshQuota slot)
// Signal: back() — parent connects to `page = "home"`
Rectangle {
    id: spendPage

    required property var spend   // { cost, reqs, input_tokens, output_tokens, cache_read, hit_rate, per_model }
    required property var quota   // { five_hour_pct, weekly_pct, five_hour_reset, weekly_reset } | null
    required property var vm

    signal back()

    color: "#0c0f14"

    // Guard helpers — access spend fields safely when spend may be empty
    function spendVal(key, def) {
        return (spend && spend[key] !== undefined) ? spend[key] : def
    }
    function quotaVal(key, def) {
        return (quota && quota[key] !== undefined) ? quota[key] : def
    }
    // Format a token count with K/M suffix
    function fmtTok(n) {
        n = n || 0
        if (n >= 1000000) return (n / 1000000).toFixed(1) + "M"
        if (n >= 1000)    return (n / 1000).toFixed(1) + "K"
        return String(n)
    }
    // Clamp a pct to [0,100]
    function clamp100(v) { return Math.max(0, Math.min(100, v || 0)) }

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
                        onClicked: spendPage.back()
                    }
                }

                Item { Layout.fillWidth: true }

                Text {
                    text: "Today's usage"
                    color: "#e9edf2"
                    font.pixelSize: 13
                    font.bold: true
                }

                Item { Layout.fillWidth: true }

                // Refresh quota button
                Text {
                    text: "↻ Refresh quota"
                    color: refreshArea.containsMouse ? "#5fd2a8" : "#7e8a97"
                    font.pixelSize: 12
                    MouseArea {
                        id: refreshArea
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        hoverEnabled: true
                        onClicked: { if (spendPage.vm) spendPage.vm.refreshQuota() }
                    }
                }
            }
        }

        // ── Scrollable body ───────────────────────────────────────────────
        Flickable {
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentHeight: bodyCol.height
            clip: true

            ColumnLayout {
                id: bodyCol
                width: parent.width
                spacing: 0

                // ── Cost headline ─────────────────────────────────────────
                Text {
                    Layout.leftMargin: 16
                    Layout.topMargin: 4
                    Layout.bottomMargin: 12
                    text: {
                        var c = spendVal("cost", 0)
                        return "$" + (c >= 100 ? c.toFixed(0) : c.toFixed(2))
                    }
                    color: "#f0a860"
                    font.pixelSize: 28
                    font.bold: true
                    font.family: "monospace"
                }

                // ── Stat rows ─────────────────────────────────────────────
                // Section label
                Text {
                    Layout.leftMargin: 16
                    Layout.bottomMargin: 4
                    text: "USAGE DETAIL"
                    color: "#566069"
                    font.pixelSize: 10
                    font.letterSpacing: 1.5
                }

                // Each stat row is a small horizontal layout
                // reqs
                SpendRow { label: "Requests"; value: String(spendVal("reqs", 0)) }
                SpendRow { label: "Input tokens";  value: fmtTok(spendVal("input_tokens",  0)) }
                SpendRow { label: "Output tokens";  value: fmtTok(spendVal("output_tokens", 0)) }
                SpendRow { label: "Cache tokens";  value: fmtTok(spendVal("cache_read",    0)) }
                SpendRow {
                    label: "Hit rate"
                    value: {
                        var r = spendVal("hit_rate", 0)
                        return (r * 100).toFixed(1) + "%"
                    }
                }

                // ── Per-model bars ────────────────────────────────────────
                Text {
                    Layout.leftMargin: 16
                    Layout.topMargin: 16
                    Layout.bottomMargin: 4
                    text: "MODEL BREAKDOWN"
                    color: "#566069"
                    font.pixelSize: 10
                    font.letterSpacing: 1.5
                    visible: {
                        var pm = spendVal("per_model", [])
                        return pm && pm.length > 0
                    }
                }

                Repeater {
                    model: spendVal("per_model", [])
                    delegate: Item {
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.leftMargin: 16
                        Layout.rightMargin: 16
                        Layout.bottomMargin: 6
                        height: 34

                        // Compute max cost among all models for proportional bars
                        property real maxCost: {
                            var pm = spendVal("per_model", [])
                            var mx = 0.01
                            for (var i = 0; i < pm.length; i++) {
                                if (pm[i].cost > mx) mx = pm[i].cost
                            }
                            return mx
                        }
                        property real barFrac: Math.max(0, Math.min(1, modelData.cost / maxCost))

                        // Label row
                        RowLayout {
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            height: 18

                            Text {
                                Layout.fillWidth: true
                                text: modelData.model
                                color: "#c8d4de"
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                            Text {
                                text: "$" + (modelData.cost >= 100 ? modelData.cost.toFixed(0) : modelData.cost.toFixed(2))
                                color: "#f0a860"
                                font.family: "monospace"
                                font.pixelSize: 11
                            }
                        }

                        // Bar
                        Rectangle {
                            anchors.bottom: parent.bottom
                            anchors.left: parent.left
                            height: 6
                            radius: 3
                            // Full-width track
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

                // ── Quota section ─────────────────────────────────────────
                Text {
                    Layout.leftMargin: 16
                    Layout.topMargin: 16
                    Layout.bottomMargin: 4
                    text: "QUOTA USAGE"
                    color: "#566069"
                    font.pixelSize: 10
                    font.letterSpacing: 1.5
                    visible: quota !== null && quota !== undefined
                }

                // 5-hour bar
                Item {
                    Layout.fillWidth: true
                    Layout.leftMargin: 16
                    Layout.rightMargin: 16
                    Layout.bottomMargin: 10
                    height: 46
                    visible: quota !== null && quota !== undefined

                    RowLayout {
                        anchors.top: parent.top
                        anchors.left: parent.left
                        anchors.right: parent.right
                        height: 20
                        Text {
                            text: "5-hour window"
                            color: "#a0aab6"
                            font.pixelSize: 11
                            Layout.fillWidth: true
                        }
                        Text {
                            text: clamp100(quotaVal("five_hour_pct", 0)) + "%"
                            color: "#f0a860"
                            font.family: "monospace"
                            font.pixelSize: 11
                        }
                    }
                    Rectangle {
                        anchors.top: parent.top
                        anchors.topMargin: 22
                        anchors.left: parent.left
                        anchors.right: parent.right
                        height: 8
                        radius: 4
                        color: "#151b22"
                        Rectangle {
                            height: parent.height
                            radius: parent.radius
                            width: parent.width * clamp100(quotaVal("five_hour_pct", 0)) / 100
                            color: quotaVal("five_hour_pct", 0) > 80 ? "#e8743b" : "#5fd2a8"
                        }
                    }
                    Text {
                        anchors.bottom: parent.bottom
                        text: "Resets: " + (quotaVal("five_hour_reset", "") || "—")
                        color: "#566069"
                        font.pixelSize: 10
                    }
                }

                // Weekly bar
                Item {
                    Layout.fillWidth: true
                    Layout.leftMargin: 16
                    Layout.rightMargin: 16
                    Layout.bottomMargin: 16
                    height: 46
                    visible: quota !== null && quota !== undefined && quotaVal("weekly_pct", -1) >= 0

                    RowLayout {
                        anchors.top: parent.top
                        anchors.left: parent.left
                        anchors.right: parent.right
                        height: 20
                        Text {
                            text: "7-day window"
                            color: "#a0aab6"
                            font.pixelSize: 11
                            Layout.fillWidth: true
                        }
                        Text {
                            text: clamp100(quotaVal("weekly_pct", 0)) + "%"
                            color: "#f0a860"
                            font.family: "monospace"
                            font.pixelSize: 11
                        }
                    }
                    Rectangle {
                        anchors.top: parent.top
                        anchors.topMargin: 22
                        anchors.left: parent.left
                        anchors.right: parent.right
                        height: 8
                        radius: 4
                        color: "#151b22"
                        Rectangle {
                            height: parent.height
                            radius: parent.radius
                            width: parent.width * clamp100(quotaVal("weekly_pct", 0)) / 100
                            color: quotaVal("weekly_pct", 0) > 80 ? "#e8743b" : "#5fd2a8"
                        }
                    }
                    Text {
                        anchors.bottom: parent.bottom
                        text: "Resets: " + (quotaVal("weekly_reset", "") || "—")
                        color: "#566069"
                        font.pixelSize: 10
                    }
                }
            }
        }
    }

    // Inline sub-component for stat rows (avoids a separate file for tiny widgets)
    component SpendRow: Rectangle {
        required property string label
        required property string value
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
                Layout.fillWidth: true
            }
            Text {
                text: value
                color: "#e9edf2"
                font.family: "monospace"
                font.pixelSize: 12
            }
        }
        // Separator line
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 16
            height: 1
            color: "#151b22"
        }
    }
}
