"""Lazy WinRT toast helper. Imported only when WindowsNotifyBackend
decides to try the WinRT path. Importing this module on a non-Windows
machine (or one without ``winsdk`` installed) raises ImportError, which
the caller catches and falls back to QSystemTrayIcon.

The real implementation uses the modern Toast XML schema:
  https://learn.microsoft.com/en-us/windows/apps/design/shell/tiles-and-notifications/adaptive-interactive-toasts

We expose one function: ``show_toast(app_id, title, body, kind)``.
``kind`` is ``"info" | "warn" | "error"`` matching NotifyKindHint values.
"""
from __future__ import annotations

import logging

# These imports raise ImportError on non-Windows or when winsdk isn't
# installed. WindowsNotifyBackend catches the ImportError on
# ``from . import _winrt_helper`` and falls back gracefully.
from winsdk.windows.data.xml.dom import XmlDocument  # type: ignore
from winsdk.windows.ui.notifications import (  # type: ignore
    ToastNotification,
    ToastNotificationManager,
)

log = logging.getLogger(__name__)


_TEMPLATE = """\
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{title}</text>
      <text>{body}</text>
    </binding>
  </visual>
  {audio_block}
</toast>
"""


_AUDIO_BY_KIND: dict[str, str] = {
    "info": "",
    "warn": '<audio src="ms-winsoundevent:Notification.Default" />',
    "error": '<audio src="ms-winsoundevent:Notification.Looping.Alarm" loop="false" />',
}


def show_toast(*, app_id: str, title: str, body: str, kind: str) -> None:
    """Build + dispatch a single toast. Caller owns timeout / dedup.

    XML is built via simple substitution; titles + bodies are XML-
    escaped to defend against angle brackets in user content.
    """
    audio_block = _AUDIO_BY_KIND.get(kind, "")
    xml = _TEMPLATE.format(
        title=_xml_escape(title),
        body=_xml_escape(body),
        audio_block=audio_block,
    )
    doc = XmlDocument()
    doc.load_xml(xml)
    notif = ToastNotification(doc)
    notifier = ToastNotificationManager.create_toast_notifier(app_id)
    notifier.show(notif)


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )
