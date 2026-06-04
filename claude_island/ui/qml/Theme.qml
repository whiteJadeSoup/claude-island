pragma Singleton
import QtQuick

QtObject {
    readonly property color bg:"#0a0c11"; readonly property color surface:"#0f131a"
    readonly property color surface2:"#141922"; readonly property color bd:"#1b212a"; readonly property color bd2:"#2a3340"
    readonly property color ink:"#edf0f4"; readonly property color ink2:"#b4bcc6"; readonly property color dim:"#838c98"; readonly property color faint:"#525b66"
    readonly property color teal:"#5fd6ac"; readonly property color amber:"#f3b95e"; readonly property color gold:"#efa860"
    readonly property color coral:"#ec6a3a"; readonly property color phos:"#9be6bf"; readonly property color violet:"#9a8cff"
    // ── Redesign phase palette ──
    readonly property color pRunning:"#5fe0b4"   // tool_use (running)
    readonly property color pThinking:"#9a8cff"  // thinking
    readonly property color pCompact:"#e6b96e"   // compacting
    readonly property color pWaiting:"#f3b95e"   // waiting_approval
    readonly property color pStuck:"#e0795a"     // derived stuck
    readonly property color quotaFill:"#e0b86a"  // TODAY quota bar (gold, was teal)
    readonly property color costDim:"#bf9056"    // dimmed cost in active card
    readonly property int tDisplay:25; readonly property int tTitle:14; readonly property int tHero:15
    readonly property int tBody:13; readonly property int tMeta:11; readonly property int tMicro:10
    readonly property int sp:8
    // Bundled prototype fonts (loaded via QFontDatabase in qml_app). UI default
    // is Inter (set app-wide); mono Text sets font.family: Theme.fontMono.
    readonly property string fontUI: "Inter"
    readonly property string fontMono: "JetBrains Mono"
    readonly property string fontDisplay: "Bricolage Grotesque"
    // staleness threshold (seconds) past which an active session reads "stuck"
    readonly property int stuckAfterS: 18
    function phaseColor(p){ return p==="thinking" ? amber : teal }
    function isStuck(p, secs){ return (p==="tool_use" || p==="thinking") && secs >= stuckAfterS }
    function railColor(p, secs){
        if (isStuck(p, secs)) return pStuck
        if (p==="thinking") return pThinking
        if (p==="compacting") return pCompact
        if (p==="waiting_approval") return pWaiting
        return pRunning
    }
    function phaseLabel(p, secs){
        if (isStuck(p, secs)) return "stuck"
        if (p==="tool_use") return "running"
        if (p==="waiting_approval") return "awaiting approval"
        return p
    }
    function modelColor(m){ m=(m||"").toLowerCase(); return m.indexOf("opus")>-1 ? "#f0b07a" : (m.indexOf("sonnet")>-1 ? "#7aa2f7" : (m.indexOf("haiku")>-1 ? violet : dim)) }
    function costColor(n){ return n>=200 ? coral : (n>=50 ? gold : "#7e9a86") }
}
