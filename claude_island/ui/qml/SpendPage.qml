import QtQuick
import QtQuick.Layouts
import "."

// Spend detail page — shown when user taps the cost/quota text in the top bar.
// Props:  spend  — the dict returned by vm.spendDetail()
//         quota  — the quota dict from vmQuota (may be null)
//         vm     — worldVm (for refreshQuota slot)
// Signal: back() — parent connects to `page = "home"`
Rectangle {
    id: spendPage

    required property var spend   // { cost, reqs, input_tokens, output_tokens, cache_read, hit_rate, per_model }
    required property var quota   // { five_hour_pct, weekly_pct, five_hour_reset_epoch, weekly_reset_epoch } | null
    required property var vm

    signal back()

    color: Theme.bg

    // Live "now" ticker — re-stamped every 30s so reset countdowns tick down
    // without waiting for a new snapshot.
    property double nowMs: 0
    Component.onCompleted: nowMs = Date.now()
    Timer { interval: 30000; repeat: true; running: true; onTriggered: spendPage.nowMs = Date.now() }

    // Format a quota reset countdown from an epoch-ms value.
    // Returns "—" for 0/missing, "now" for elapsed, "Xd Yh" / "Xh Ym" / "Xm" / "<1m".
    function fmtReset(epochMs) {
        if (!epochMs) return "—"
        var rem = epochMs - spendPage.nowMs
        if (rem <= 0) return "now"
        var mins = Math.floor(rem / 60000)
        var h = Math.floor(mins / 60), m = mins % 60, d = Math.floor(h / 24)
        if (d > 0) return d + "d " + (h % 24) + "h"
        if (h > 0) return h + "h " + m + "m"
        if (m > 0) return m + "m"
        return "<1m"
    }

    // Guard helpers — access spend/quota fields safely when they may be empty/null.
    function spendVal(key, def) {
        return (spend && spend[key] !== undefined) ? spend[key] : def
    }
    function quotaVal(key, def) {
        return (quota && quota[key] !== undefined) ? quota[key] : def
    }
    // Format a numeric count: ≥1e6 → "X.XM", ≥1e3 → "X.XK", else the bare number.
    function fmtNum(n) {
        n = n || 0
        if (n >= 1000000) return (n / 1000000).toFixed(1) + "M"
        if (n >= 1000)    return (n / 1000).toFixed(1) + "K"
        return "" + n
    }
    // Clamp a pct to [0,100]
    function clamp100(v) { return Math.max(0, Math.min(100, v || 0)) }

    // ── Shared icon button (matches SessionDetailPage) ──────────────────────
    // Small square icon-only button: muted glyph, subtle hover background.
    component IconButton: Rectangle {
        id: iconBtn
        required property string glyph
        signal tapped()
        Layout.preferredWidth: 30
        Layout.preferredHeight: 30
        radius: 7
        color: ma.containsMouse ? "#191d23" : "transparent"
        Text {
            anchors.centerIn: parent
            text: iconBtn.glyph
            color: "#6b7480"
            font.pixelSize: 18
        }
        MouseArea {
            id: ma
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: iconBtn.tapped()
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // ── Header ────────────────────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            Layout.leftMargin: 12
            Layout.rightMargin: 12
            spacing: 8

            IconButton { glyph: "‹"; onTapped: spendPage.back() }

            Text {
                text: "Today's usage"
                color: Theme.ink
                font.pixelSize: Theme.tTitle
                font.bold: true
                Layout.fillWidth: true
            }

            IconButton {
                glyph: "↻"
                onTapped: { if (spendPage.vm) spendPage.vm.refreshQuota() }
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

                // ── Big cost headline ─────────────────────────────────────
                Text {
                    Layout.leftMargin: 16
                    Layout.topMargin: 4
                    Layout.bottomMargin: 14
                    text: {
                        var c = spendVal("cost", 0)
                        return "$" + (c >= 100 ? c.toFixed(0) : c.toFixed(2))
                    }
                    color: Theme.gold
                    font.pixelSize: 33
                    font.bold: true
                    font.family: Theme.fontMono
                    font.letterSpacing: -0.5
                }

                // ── Stat rows (no section header) ─────────────────────────
                StatRow { label: "Requests";      value: "" + spendVal("reqs", 0) }
                StatRow { label: "Input tokens";  value: fmtNum(spendVal("input_tokens",  0)) }
                StatRow { label: "Output tokens"; value: fmtNum(spendVal("output_tokens", 0)) }
                StatRow { label: "Cache tokens";  value: fmtNum(spendVal("cache_read",    0)) }
                StatRow {
                    label: "Hit rate"
                    value: (spendVal("hit_rate", 0) * 100).toFixed(1) + "%"
                }

                // ── MODEL BREAKDOWN band ──────────────────────────────────
                BandLabel {
                    text: "MODEL BREAKDOWN"
                    Layout.topMargin: 18
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
                        Layout.bottomMargin: 8
                        Layout.preferredHeight: 30

                        // Largest cost among all models — drives proportional bar width.
                        property real maxCost: {
                            var pm = spendVal("per_model", [])
                            var mx = 0.01
                            for (var i = 0; i < pm.length; i++) {
                                if (pm[i].cost > mx) mx = pm[i].cost
                            }
                            return mx
                        }
                        property real barFrac: Math.max(0, Math.min(1, modelData.cost / maxCost))

                        // Label row: model name (fills) ... cost
                        RowLayout {
                            anchors.top: parent.top
                            anchors.left: parent.left
                            anchors.right: parent.right
                            height: 18

                            Text {
                                Layout.fillWidth: true
                                text: modelData.model
                                color: Theme.ink
                                font.pixelSize: Theme.tBody
                                elide: Text.ElideRight
                            }
                            Text {
                                text: "$" + (modelData.cost >= 100 ? modelData.cost.toFixed(0) : modelData.cost.toFixed(2))
                                color: Theme.gold
                                font.family: Theme.fontMono
                                font.pixelSize: Theme.tBody
                            }
                        }

                        // 7px track + teal-gradient fill ∝ cost/maxCost
                        Rectangle {
                            anchors.bottom: parent.bottom
                            anchors.left: parent.left
                            anchors.right: parent.right
                            height: 7
                            radius: 4
                            color: "#0b0f15"

                            Rectangle {
                                height: parent.height
                                radius: parent.radius
                                width: parent.width * barFrac
                                gradient: Gradient {
                                    orientation: Gradient.Horizontal
                                    GradientStop { position: 0.0; color: "#3fae87" }
                                    GradientStop { position: 1.0; color: "#5fd2a8" }
                                }
                            }
                        }
                    }
                }

                // ── QUOTA band ────────────────────────────────────────────
                BandLabel {
                    text: "QUOTA"
                    Layout.topMargin: 18
                    visible: quota !== null && quota !== undefined
                }

                // 5-hour window bar
                QuotaBar {
                    name: "5-hour window"
                    resetText: "resets " + spendPage.fmtReset(quotaVal("five_hour_reset_epoch", 0))
                    pct: clamp100(quotaVal("five_hour_pct", 0))
                    visible: quota !== null && quota !== undefined
                }

                // Weekly window bar
                QuotaBar {
                    name: "weekly"
                    resetText: "resets " + spendPage.fmtReset(quotaVal("weekly_reset_epoch", 0))
                    pct: clamp100(quotaVal("weekly_pct", 0))
                    Layout.bottomMargin: 16
                    visible: quota !== null && quota !== undefined && quotaVal("weekly_pct", -1) >= 0
                }
            }
        }
    }

    // ── Inline sub-components ───────────────────────────────────────────────

    // A stat row: label (faint) ... spacer ... value (mono), 1px bottom border.
    component StatRow: Rectangle {
        id: statRow
        required property string label
        required property string value
        Layout.fillWidth: true
        Layout.preferredHeight: 34
        color: "transparent"

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            Text {
                text: statRow.label
                color: Theme.faint
                font.pixelSize: Theme.tBody
                Layout.fillWidth: true
            }
            Text {
                text: statRow.value
                color: Theme.ink2
                font.family: Theme.fontMono
                font.pixelSize: Theme.tBody
            }
        }
        // Hairline separator
        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: "#ffffff"
            opacity: 0.045
        }
    }

    // A band header: 6px teal dot (glow) + uppercase micro label.
    component BandLabel: RowLayout {
        property alias text: bandText.text
        Layout.fillWidth: true
        Layout.leftMargin: 16
        Layout.rightMargin: 16
        Layout.bottomMargin: 8
        spacing: 8

        Rectangle {
            Layout.preferredWidth: 6
            Layout.preferredHeight: 6
            radius: 3
            color: Theme.teal
            // Soft glow ring around the dot.
            Rectangle {
                anchors.centerIn: parent
                width: 12; height: 12
                radius: 6
                color: Theme.teal
                opacity: 0.25
                z: -1
            }
        }
        Text {
            id: bandText
            color: Theme.teal
            font.pixelSize: Theme.tMicro
            font.bold: true
            font.letterSpacing: 1.6
        }
    }

    // A quota bar block: name ... resets <countdown> ... <pct>%, then 7px track.
    component QuotaBar: Item {
        id: quotaBar
        required property string name
        required property string resetText
        required property real pct
        Layout.fillWidth: true
        Layout.leftMargin: 16
        Layout.rightMargin: 16
        Layout.bottomMargin: 10
        Layout.preferredHeight: 34

        RowLayout {
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            height: 20
            spacing: 8
            Text {
                text: quotaBar.name
                color: Theme.ink2
                font.pixelSize: Theme.tBody
                Layout.fillWidth: true
            }
            Text {
                text: quotaBar.resetText
                color: Theme.faint
                font.family: Theme.fontMono
                font.pixelSize: Theme.tMeta
            }
            Text {
                text: quotaBar.pct + "%"
                color: Theme.gold
                font.family: Theme.fontMono
                font.pixelSize: Theme.tBody
            }
        }
        Rectangle {
            anchors.top: parent.top
            anchors.topMargin: 23
            anchors.left: parent.left
            anchors.right: parent.right
            height: 7
            radius: 4
            color: "#0b0f15"
            Rectangle {
                height: parent.height
                radius: parent.radius
                width: parent.width * quotaBar.pct / 100
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0.0; color: "#3fae87" }
                    GradientStop { position: 1.0; color: "#5fd2a8" }
                }
            }
        }
    }
}
