import QtQuick
import QtQuick.Layouts
import "."

// FIFO decision album. decisions[0] = interactive front; the rest peek as
// ghost edges (non-interactive → enforces one-at-a-time). Counter + dots
// signal how many remain. implicitHeight feeds the parent ColumnLayout.
Item {
    id: album
    property var decisions: []
    property var vm: null
    visible: decisions.length > 0
    implicitHeight: visible ? col.implicitHeight : 0

    ColumnLayout {
        id: col
        anchors.left: parent.left; anchors.right: parent.right
        spacing: 8

        // ── NEEDS YOU band ──
        RowLayout {
            Layout.fillWidth: true; Layout.leftMargin: 4
            Rectangle {
                Layout.preferredWidth: 6; Layout.preferredHeight: 6; radius: 3; color: Theme.coral
                SequentialAnimation on opacity { loops: Animation.Infinite
                    NumberAnimation { to: 0.35; duration: 650 } NumberAnimation { to: 1; duration: 650 } }
            }
            Text { text: "NEEDS YOU"; color: Theme.coral; font.pixelSize: Theme.tMicro; font.bold: true }
            Item { Layout.fillWidth: true }
            Text { text: "" + album.decisions.length; color: Theme.coral; font.family: "monospace"; font.pixelSize: Theme.tMicro }
        }

        // ── stack: ghosts behind + interactive front ──
        Item {
            Layout.fillWidth: true
            implicitHeight: front.implicitHeight + (album.decisions.length >= 2 ? 8 : 0)
            // ghost 2 (deepest)
            Rectangle {
                visible: album.decisions.length >= 3
                anchors.left: parent.left; anchors.right: parent.right
                anchors.leftMargin: 18; anchors.rightMargin: 18
                y: front.implicitHeight - 2; height: 16; radius: 12
                color: "#120c07"; border.color: "#3a3320"; border.width: 1; opacity: 0.4
            }
            // ghost 1
            Rectangle {
                visible: album.decisions.length >= 2
                anchors.left: parent.left; anchors.right: parent.right
                anchors.leftMargin: 9; anchors.rightMargin: 9
                y: front.implicitHeight - 5; height: 16; radius: 12
                color: "#120c07"; border.color: "#3a3320"; border.width: 1; opacity: 0.65
            }
            // interactive front (only this one)
            DecisionCard {
                id: front; z: 3
                anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
                decision: album.decisions.length > 0 ? album.decisions[0] : null
                vm: album.vm
            }
        }

        // ── counter + dots ──
        RowLayout {
            Layout.fillWidth: true; spacing: 8; visible: album.decisions.length > 1
            Item { Layout.fillWidth: true }
            Text { text: "第 1 / " + album.decisions.length + " 张 · 处理完自动下一张"
                   color: Theme.faint; font.family: "monospace"; font.pixelSize: 11 }
            Row { spacing: 5
                Repeater { model: album.decisions.length
                    delegate: Rectangle { width: 6; height: 6; radius: 3
                        required property int index
                        color: index === 0 ? Theme.coral : "#39414b" } } }
            Item { Layout.fillWidth: true }
        }
    }
}
