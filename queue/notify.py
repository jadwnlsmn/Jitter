"""Jitter: Queue notifications: a Windows toast so the supervisor or a woken Claude can reach the user when no
session is open. No dependency: it shells out to PowerShell's built-in WinRT toast API.
"""
import subprocess

NO_WINDOW = 0x08000000
# ponytail: PowerShell WinRT toast, no BurntToast/plyer dependency. Falls silently back to nothing if the WinRT
# assemblies aren't present (older Windows); upgrade to a tray icon only if a user actually runs one.
_PS = r"""
try {{
 [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
 $t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
 $x = $t.GetElementsByTagName("text")
 $x.Item(0).AppendChild($t.CreateTextNode({title})) > $null
 $x.Item(1).AppendChild($t.CreateTextNode({body})) > $null
 $n = [Windows.UI.Notifications.ToastNotification]::new($t)
 [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
        "Jitter").Show($n)
}} catch {{}}
"""


def _ps_literal(s: str) -> str:
    return "'" + str(s).replace("'", "''")[:300] + "'"


def notify(title: str, body: str = "") -> None:
    script = _PS.format(title=_ps_literal(title), body=_ps_literal(body))
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         creationflags=NO_WINDOW)
    except Exception:
        pass  # a notification must never break the thing it was reporting
