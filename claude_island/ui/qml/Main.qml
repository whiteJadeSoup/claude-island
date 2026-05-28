import QtQuick
import QtQuick.Window

Window {
    id: root
    width: 480; height: 460
    visible: true
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
    color: "transparent"

    Rectangle {
        anchors.fill: parent
        radius: 18
        color: "#0c0f14"
        border.color: "#1c2632"; border.width: 1

        Text {
            anchors.centerIn: parent
            text: "Claude Island — QML skeleton"
            color: "#5fd2a8"; font.pixelSize: 14
        }
        MouseArea {
            anchors.fill: parent
            property point start
            onPressed: (m) => start = Qt.point(m.x, m.y)
            onPositionChanged: (m) => {
                root.x += m.x - start.x; root.y += m.y - start.y
            }
        }
    }
}
