"""ClaudeIsland entry point — `python -m claude_island`.

Delegates entirely to qml_app.main(), which builds the full backend
pipeline and launches the QML island UI.  All pre-Qt setup (stderr
noise filter, macOS dock-hide, Qt message handler) runs inside
qml_app.main() before QGuiApplication is constructed.
"""

from claude_island.qml_app import main

if __name__ == "__main__":
    raise SystemExit(main())
