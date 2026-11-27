"""The A.S.D Home Security icon in the Windows system tray (camtray.exe).

The recorder itself (camhub.exe) runs as a background task from boot, before anyone signs in,
and has no window. This small program runs for each signed-in user and puts the logo in the
system tray, with a menu to open the viewer window. Closing the viewer window only closes the
window: the recorder keeps recording and the tray icon stays. "Quit" removes the tray icon;
recording still carries on.

    camtray.exe           start in the tray only (used at sign-in)
    camtray.exe --open    open the viewer, starting the tray icon too if it isn't running

Only the standard library is used (ctypes for the Windows calls), so this stays small.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

APP_NAME = "A.S.D Home Security"
MUTEX_NAME = "Local\\ASDHomeSecurityTray"
DEFAULT_PORT = 8080
CHECK_EVERY = 30  # seconds between checks that the recorder is answering


def config_path():
    return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "HomeCamera", "config.json")


def read_port(path=None):
    """The viewer's port from config.json, or 8080 if it can't be read."""
    try:
        with open(path or config_path(), encoding="utf-8") as f:
            port = int(json.load(f).get("server", {}).get("port", DEFAULT_PORT))
        return port if 0 < port < 65536 else DEFAULT_PORT
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_PORT


def viewer_url(port):
    return f"http://localhost:{port}/"


def edge_path(env=None):
    env = os.environ if env is None else env
    for base in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return None


def viewer_command(port, edge=None):
    """How to open the viewer: in its own Edge app window (like the Start menu shortcut did),
    or None to fall back to the default browser."""
    edge = edge if edge is not None else edge_path()
    return [edge, "--app=" + viewer_url(port)] if edge else None


def open_viewer(port):
    cmd = viewer_command(port)
    if cmd:
        subprocess.Popen(cmd, close_fds=True)
    else:
        os.startfile(viewer_url(port))  # noqa: only exists on Windows


def recorder_running(port, timeout=3):
    """True when the recorder answers on its port (the sign-in page needs no password)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def status_text(running):
    return "Recording in the background" if running else "Recorder not running"


def tooltip(running):
    return f"{APP_NAME}: {status_text(running).lower()}"


def app_dir():
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def icon_file():
    """homecamera.ico (the logo): next to camtray.exe when installed, else in the bundle."""
    here = app_dir()
    for d in (here, getattr(sys, "_MEIPASS", ""), os.path.join(here, "..", "installer")):
        p = os.path.join(d, "homecamera.ico")
        if d and os.path.exists(p):
            return os.path.abspath(p)
    return None


def log(msg):
    try:
        folder = os.path.join(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".", "HomeCamera")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "tray.log")
        if os.path.exists(path) and os.path.getsize(path) > 200_000:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


# ---- Windows tray icon (ctypes) ---------------------------------------------------------------

def run_tray(port, open_now=False):
    import ctypes
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                    ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                    ("lpszClassName", w.LPCWSTR)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                    ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                    ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                    ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", w.LONG), ("y", w.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", w.HWND), ("message", w.UINT), ("wParam", w.WPARAM), ("lParam", w.LPARAM),
                    ("time", w.DWORD), ("pt", POINT)]

    user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = w.ATOM
    user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.CreateWindowExW.restype = w.HWND
    user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
    user32.RegisterWindowMessageW.restype = w.UINT
    user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
    user32.LoadImageW.restype = w.HANDLE
    user32.LoadIconW.argtypes = [w.HINSTANCE, w.LPVOID]
    user32.LoadIconW.restype = w.HICON
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.CreatePopupMenu.restype = w.HMENU
    user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
    user32.SetMenuDefaultItem.argtypes = [w.HMENU, w.UINT, w.UINT]
    user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [w.HMENU]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.SetForegroundWindow.argtypes = [w.HWND]
    user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [w.HWND, ctypes.c_size_t]
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), w.HWND, w.UINT, w.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.DestroyWindow.argtypes = [w.HWND]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = w.BOOL
    shell32.ShellExecuteW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = w.HINSTANCE
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel32.GetModuleHandleW.restype = w.HMODULE

    WM_DESTROY, WM_COMMAND, WM_TIMER, WM_NULL = 0x0002, 0x0111, 0x0113, 0x0000
    WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
    WM_TRAY = 0x0400 + 1      # WM_USER + 1: mouse events on the icon
    WM_STATUS = 0x0400 + 2    # the background check has a new answer
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
    MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0, 0x1, 0x800
    TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
    IMAGE_ICON, LR_LOADFROMFILE, SM_CXSMICON, SM_CYSMICON = 1, 0x10, 49, 50
    IDI_APPLICATION = 32512
    ID_OPEN, ID_RESTART, ID_QUIT, ID_STATUS = 1, 2, 3, 4
    TIMER_ADD = 1

    hinst = kernel32.GetModuleHandleW(None)
    state = {"running": recorder_running(port, timeout=1), "added": False, "tries": 0}
    taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    hicon = None
    ico = icon_file()
    if ico:
        hicon = user32.LoadImageW(None, ico, IMAGE_ICON, user32.GetSystemMetrics(SM_CXSMICON),
                                  user32.GetSystemMetrics(SM_CYSMICON), LR_LOADFROMFILE)
    if not hicon:
        hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def nid(hwnd):
        d = NOTIFYICONDATAW()
        d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        d.hWnd = hwnd
        d.uID = 1
        d.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        d.uCallbackMessage = WM_TRAY
        d.hIcon = hicon
        d.szTip = tooltip(state["running"])[:127]
        return d

    def add_icon(hwnd):
        # Right after sign-in the taskbar may not be ready yet, so keep trying for a few minutes.
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid(hwnd))):
            state["added"] = True
            user32.KillTimer(hwnd, TIMER_ADD)
            log("tray icon shown")
        else:
            state["tries"] += 1
            if state["tries"] == 1:
                log("taskbar not ready, retrying")
            if state["tries"] < 90:
                user32.SetTimer(hwnd, TIMER_ADD, 2000, None)
            else:
                user32.KillTimer(hwnd, TIMER_ADD)

    def show_menu(hwnd):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open " + APP_NAME)
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, ID_STATUS, status_text(state["running"]))
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_RESTART, "Restart recorder")
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit (recording keeps running)")
        user32.SetMenuDefaultItem(menu, ID_OPEN, 0)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)  # so the menu closes when you click elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == ID_OPEN:
            safe_open()
        elif cmd == ID_RESTART:
            restart = os.path.join(app_dir(), "restart.cmd")
            if os.path.exists(restart):  # asks Windows for permission itself
                shell32.ShellExecuteW(None, "open", restart, None, app_dir(), 1)
        elif cmd == ID_QUIT:
            user32.DestroyWindow(hwnd)

    def safe_open():
        try:
            open_viewer(port)
        except Exception as e:
            log(f"couldn't open the viewer: {e!r}")

    @WNDPROC
    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    safe_open()
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    show_menu(hwnd)
                return 0
            if msg == WM_TIMER and wparam == TIMER_ADD:
                add_icon(hwnd)
                return 0
            if msg == WM_STATUS:
                if state["added"]:
                    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid(hwnd)))
                return 0
            if taskbar_created and msg == taskbar_created:
                # Explorer restarted: the old icon is gone, so add it again.
                state["added"], state["tries"] = False, 0
                add_icon(hwnd)
                return 0
            if msg == WM_DESTROY:
                if state["added"]:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid(hwnd)))
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            log(f"error: {e!r}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.hIcon = hicon
    wc.lpszClassName = "ASDHomeSecurityTray"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
    # A hidden top-level window (not message-only, so it hears that the taskbar was recreated).
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")

    add_icon(hwnd)
    if open_now:
        safe_open()

    stop = threading.Event()

    def watch():
        while not stop.wait(CHECK_EVERY):
            running = recorder_running(port)
            if running != state["running"]:
                state["running"] = running
                log(status_text(running))
                user32.PostMessageW(hwnd, WM_STATUS, 0, 0)

    threading.Thread(target=watch, daemon=True).start()

    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    stop.set()
    log("tray closed")


def already_running():
    """Takes the one-per-user lock; False if another tray icon already holds it."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    already_running.handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)  # kept open while we run
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    open_now = "--open" in argv
    port = read_port()
    if already_running():
        # The icon is already in the tray: just open the window.
        if open_now:
            open_viewer(port)
        return 0
    log(f"starting (port {port})")
    try:
        run_tray(port, open_now)
    except Exception as e:
        log(f"tray failed: {e!r}")
        if open_now:
            open_viewer(port)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
