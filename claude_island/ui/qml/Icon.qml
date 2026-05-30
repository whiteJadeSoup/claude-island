import QtQuick

// Cross-platform vector icon — Canvas-drawn so it renders identically on
// Windows / macOS / Linux at any DPI. We use this ONLY for glyphs that are
// NOT in the bundled Inter / JetBrains Mono fonts (verified via QRawFont.
// supportsCharacter): a missing glyph would otherwise fall back to a
// per-OS system symbol font (Segoe UI Symbol vs Apple Symbols), the exact
// cross-platform inconsistency this component eliminates.
//
// Glyphs already present in the bundled fonts (· ‹ ← → ↗ ↻ ✎ ✓ ↳ ╰ ✕)
// stay as plain Text — no need to redraw them.
//
// Usage:  Icon { name: "copy"; size: 14; color: "#888" }
// Add a new icon = one more case in the switch below (the only edit site).
Canvas {
    id: ic
    property string name: ""
    property int size: 14
    property color color: "#cccccc"
    property real lw: 1.5          // stroke width

    implicitWidth: size
    implicitHeight: size
    width: size
    height: size

    onColorChanged: requestPaint()
    onNameChanged: requestPaint()
    onWidthChanged: requestPaint()

    onPaint: {
        var ctx = getContext("2d")
        var w = width, h = height
        ctx.clearRect(0, 0, w, h)
        ctx.strokeStyle = ic.color
        ctx.fillStyle = ic.color
        ctx.lineWidth = ic.lw
        ctx.lineCap = "round"
        ctx.lineJoin = "round"
        // Normalised drawing on a 16x16 grid, scaled to the actual size.
        var s = w / 16
        function X(v) { return v * s }

        if (name === "chevron-down") {
            // Collapse affordance (replaces "⌄"): a wide downward chevron.
            ctx.beginPath()
            ctx.moveTo(X(4), X(6.5)); ctx.lineTo(X(8), X(10.5)); ctx.lineTo(X(12), X(6.5))
            ctx.stroke()

        } else if (name === "copy") {
            // Copy id (replaces "⧉"): two overlapping rounded rects.
            // Back sheet (offset up-right), then front sheet on top.
            function rrect(x, y, ww, hh, r) {
                ctx.beginPath()
                ctx.moveTo(x + r, y)
                ctx.arcTo(x + ww, y, x + ww, y + hh, r)
                ctx.arcTo(x + ww, y + hh, x, y + hh, r)
                ctx.arcTo(x, y + hh, x, y, r)
                ctx.arcTo(x, y, x + ww, y, r)
                ctx.closePath()
                ctx.stroke()
            }
            rrect(X(6), X(3), X(7), X(7), X(1.5))   // back sheet
            rrect(X(3), X(6), X(7), X(7), X(1.5))   // front sheet

        } else if (name === "reset") {
            // Reset thinking (replaces "⟲"): a counter-clockwise arc with an
            // arrowhead at the open (upper-left) end — a "rewind/undo" mark.
            ctx.beginPath()
            ctx.arc(X(8), X(8), X(5), Math.PI * 0.30, Math.PI * 1.85, false)
            ctx.stroke()
            // Arrowhead at the start of the arc (upper-left), pointing back.
            var ax = X(8) + X(5) * Math.cos(Math.PI * 1.85)
            var ay = X(8) + X(5) * Math.sin(Math.PI * 1.85)
            ctx.beginPath()
            ctx.moveTo(ax - X(3.0), ay - X(0.2))
            ctx.lineTo(ax, ay)
            ctx.lineTo(ax + X(0.4), ay - X(3.0))
            ctx.stroke()

        } else if (name === "folder") {
            // History cwd marker (replaces the "📁" color emoji): a flat
            // folder outline — body + a small tab on the top-left.
            ctx.beginPath()
            // tab
            ctx.moveTo(X(2.5), X(5))
            ctx.lineTo(X(2.5), X(4))
            ctx.lineTo(X(6), X(4))
            ctx.lineTo(X(7), X(5.5))
            ctx.lineTo(X(13.5), X(5.5))
            ctx.lineTo(X(13.5), X(12))
            ctx.lineTo(X(2.5), X(12))
            ctx.closePath()
            ctx.stroke()
        }
        // Unknown name → draw nothing (graceful: a missing icon is invisible,
        // not a crash or a tofu box).
    }
}
