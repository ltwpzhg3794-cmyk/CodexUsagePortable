"""Attach a native child usage strip to an already-running Codex window."""
import ctypes
from collections import deque
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
import comtypes

from active_task import ActiveTask
from server import Store, ROOT

BASE = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32', use_last_error=True)

# The user's muted-green Codex theme, matching the previous embedded strip.
THEME = {'surface': '#2b2e28', 'border': '#3b4235', 'label': '#7f8c73',
         'value': '#b6c8a8', 'divider': '#4b5343'}


def colorref(value):
    red, green, blue = bytes.fromhex(value.lstrip('#'))
    return red | green << 8 | blue << 16


def api(lib, name, result, args):
    function = getattr(lib, name)
    function.restype = result
    function.argtypes = args
    return function


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
ENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


class WindowClass(ctypes.Structure):
    _fields_ = [('style', W.UINT), ('proc', WNDPROC), ('class_extra', ctypes.c_int),
                ('window_extra', ctypes.c_int), ('instance', W.HINSTANCE),
                ('icon', W.HICON), ('cursor', W.HANDLE), ('background', W.HBRUSH),
                ('menu', W.LPCWSTR), ('name', W.LPCWSTR)]


class PaintStruct(ctypes.Structure):
    _fields_ = [('dc', W.HDC), ('erase', W.BOOL), ('rect', W.RECT),
                ('restore', W.BOOL), ('update', W.BOOL), ('reserved', ctypes.c_byte * 32)]


class NotifyIconData(ctypes.Structure):
    _fields_ = [('size', W.DWORD), ('hwnd', W.HWND), ('id', W.UINT),
                ('flags', W.UINT), ('message', W.UINT), ('icon', W.HICON),
                ('tip', W.WCHAR * 128), ('state', W.DWORD), ('state_mask', W.DWORD),
                ('info', W.WCHAR * 256), ('version', W.UINT), ('title', W.WCHAR * 64),
                ('info_flags', W.DWORD), ('guid', ctypes.c_byte * 16), ('balloon', W.HICON)]


enum_windows = api(user32, 'EnumWindows', W.BOOL, [ENUMPROC, W.LPARAM])
window_pid = api(user32, 'GetWindowThreadProcessId', W.DWORD, [W.HWND, ctypes.POINTER(W.DWORD)])
is_window = api(user32, 'IsWindow', W.BOOL, [W.HWND])
is_visible = api(user32, 'IsWindowVisible', W.BOOL, [W.HWND])
get_parent = api(user32, 'GetParent', W.HWND, [W.HWND])
get_class = api(user32, 'GetClassNameW', ctypes.c_int, [W.HWND, W.LPWSTR, ctypes.c_int])
client_rect = api(user32, 'GetClientRect', W.BOOL, [W.HWND, ctypes.POINTER(W.RECT)])
client_to_screen = api(user32, 'ClientToScreen', W.BOOL, [W.HWND, ctypes.POINTER(W.POINT)])
get_dc = api(user32, 'GetDC', W.HDC, [W.HWND])
release_dc = api(user32, 'ReleaseDC', ctypes.c_int, [W.HWND, W.HDC])
text_extent = api(gdi32, 'GetTextExtentPoint32W', W.BOOL, [W.HDC, W.LPCWSTR, ctypes.c_int, ctypes.POINTER(W.SIZE)])
open_process = api(kernel32, 'OpenProcess', W.HANDLE, [W.DWORD, W.BOOL, W.DWORD])
close_handle = api(kernel32, 'CloseHandle', W.BOOL, [W.HANDLE])
process_path = api(kernel32, 'QueryFullProcessImageNameW', W.BOOL,
                   [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)])
instance = api(kernel32, 'GetModuleHandleW', W.HINSTANCE, [W.LPCWSTR])(None)
register_class = api(user32, 'RegisterClassW', W.ATOM, [ctypes.POINTER(WindowClass)])
create_window = api(user32, 'CreateWindowExW', W.HWND,
                    [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, ctypes.c_void_p])
destroy_window = api(user32, 'DestroyWindow', W.BOOL, [W.HWND])
show_window = api(user32, 'ShowWindow', W.BOOL, [W.HWND, ctypes.c_int])
layered_alpha = api(user32, 'SetLayeredWindowAttributes', W.BOOL, [W.HWND, W.DWORD, W.BYTE, W.DWORD])
position_window = api(user32, 'SetWindowPos', W.BOOL,
                      [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT])
default_proc = api(user32, 'DefWindowProcW', ctypes.c_ssize_t, [W.HWND, W.UINT, W.WPARAM, W.LPARAM])
invalidate = api(user32, 'InvalidateRect', W.BOOL, [W.HWND, ctypes.c_void_p, W.BOOL])
begin_paint = api(user32, 'BeginPaint', W.HDC, [W.HWND, ctypes.POINTER(PaintStruct)])
end_paint = api(user32, 'EndPaint', W.BOOL, [W.HWND, ctypes.POINTER(PaintStruct)])
fill_rect = api(user32, 'FillRect', ctypes.c_int, [W.HDC, ctypes.POINTER(W.RECT), W.HBRUSH])
draw_text = api(user32, 'DrawTextW', ctypes.c_int, [W.HDC, W.LPCWSTR, ctypes.c_int, ctypes.POINTER(W.RECT), W.UINT])
set_text_color = api(gdi32, 'SetTextColor', W.DWORD, [W.HDC, W.DWORD])
set_background_mode = api(gdi32, 'SetBkMode', ctypes.c_int, [W.HDC, ctypes.c_int])
create_brush = api(gdi32, 'CreateSolidBrush', W.HBRUSH, [W.DWORD])
create_pen = api(gdi32, 'CreatePen', W.HANDLE, [ctypes.c_int, ctypes.c_int, W.DWORD])
round_rect = api(gdi32, 'RoundRect', W.BOOL, [W.HDC] + [ctypes.c_int] * 6)
round_region = api(gdi32, 'CreateRoundRectRgn', W.HANDLE, [ctypes.c_int] * 6)
set_region = api(user32, 'SetWindowRgn', ctypes.c_int, [W.HWND, W.HANDLE, W.BOOL])
delete_object = api(gdi32, 'DeleteObject', W.BOOL, [W.HANDLE])
select_object = api(gdi32, 'SelectObject', W.HANDLE, [W.HDC, W.HANDLE])
create_font = api(gdi32, 'CreateFontW', W.HANDLE,
                  [ctypes.c_int] * 5 + [W.DWORD] * 8 + [W.LPCWSTR])
get_dpi_context = api(user32, 'GetWindowDpiAwarenessContext', W.HANDLE, [W.HWND])
set_dpi_context = api(user32, 'SetThreadDpiAwarenessContext', W.HANDLE, [W.HANDLE])
post_message = api(user32, 'PostMessageW', W.BOOL, [W.HWND, W.UINT, W.WPARAM, W.LPARAM])
LAYOUT_CHANGED = 0x8001
TRAY_CALLBACK = 0x8002
notify_icon = api(shell32, 'Shell_NotifyIconW', W.BOOL, [W.DWORD, ctypes.POINTER(NotifyIconData)])
register_message = api(user32, 'RegisterWindowMessageW', W.UINT, [W.LPCWSTR])
load_image = api(user32, 'LoadImageW', W.HANDLE,
                 [W.HINSTANCE, W.LPCWSTR, W.UINT, ctypes.c_int, ctypes.c_int, W.UINT])
destroy_icon = api(user32, 'DestroyIcon', W.BOOL, [W.HICON])
create_menu = api(user32, 'CreatePopupMenu', W.HMENU, [])
append_menu = api(user32, 'AppendMenuW', W.BOOL, [W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR])
destroy_menu = api(user32, 'DestroyMenu', W.BOOL, [W.HMENU])
track_menu = api(user32, 'TrackPopupMenu', W.UINT,
                 [W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, ctypes.c_void_p])
cursor_position = api(user32, 'GetCursorPos', W.BOOL, [ctypes.POINTER(W.POINT)])
foreground_window = api(user32, 'SetForegroundWindow', W.BOOL, [W.HWND])


class TrayIcon:
    def __init__(self, app):
        self.app = app
        self.added = False
        self.closed = False
        self.last_tip = None
        self.retry_at = 0
        self.pending_commands = deque()
        self.taskbar_created = register_message('TaskbarCreated')
        self.callback = WNDPROC(self.window_proc)
        window_class = WindowClass()
        window_class.proc = self.callback
        window_class.instance = instance
        window_class.name = 'CodexUsagePortableTray'
        if not register_class(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        # A hidden top-level tool window receives taskbar restart broadcasts.
        # It never acquires a taskbar button or an Alt+Tab entry.
        self.hwnd = create_window(0x80, window_class.name, 'Codex 便携用量条',
                                  0, 0, 0, 0, 0, None, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        resources = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        self.icon = load_image(None, str(resources / 'tray.ico'), 1, 32, 32, 0x10)
        if not self.icon:
            destroy_window(self.hwnd)
            raise ctypes.WinError(ctypes.get_last_error())
        self.data = NotifyIconData()
        self.data.size = ctypes.sizeof(NotifyIconData)
        self.data.hwnd = self.hwnd
        self.data.id = 1
        self.data.flags = 1 | 2 | 4 | 0x80  # Message, icon, tooltip, standard tooltip.
        self.data.message = TRAY_CALLBACK
        self.data.icon = self.icon
        self.data.tip = 'Codex 便携用量条 · 正在启动'
        self.add()

    def add(self):
        self.added = bool(notify_icon(0, ctypes.byref(self.data)))
        self.retry_at = time.monotonic() + 2
        if self.added:
            self.data.version = 4
            notify_icon(4, ctypes.byref(self.data))

    def update(self, status, detail):
        if self.closed:
            return
        tip = ('Codex 便携用量条\n' + status + '\n' + detail.replace('   ', '  '))[:127]
        self.data.tip = tip
        if not self.added:
            if time.monotonic() >= self.retry_at:
                self.add()
        elif tip != self.last_tip:
            self.added = bool(notify_icon(1, ctypes.byref(self.data)))
        self.last_tip = tip

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == self.taskbar_created:
            if not self.closed:
                self.add()
            return 0
        if message == TRAY_CALLBACK:
            if lparam & 0xffff == 0x007B:  # WM_CONTEXTMENU, including keyboard activation.
                self.popup()
            return 0
        if message == 0x0010:  # WM_CLOSE
            self.pending_commands.append(3)
            return 0
        return default_proc(hwnd, message, wparam, lparam)

    def popup(self):
        menu = create_menu()
        if not menu:
            return
        try:
            append_menu(menu, 8 if self.app.enabled else 0, 1, '显示用量条')
            append_menu(menu, 0 if self.app.enabled else 8, 2, '移除用量条')
            append_menu(menu, 0x800, 0, None)
            append_menu(menu, 0, 3, '退出工具')
            point = W.POINT()
            cursor_position(ctypes.byref(point))
            foreground_window(self.hwnd)
            command = track_menu(menu, 0x100 | 0x2, point.x, point.y, 0, self.hwnd, None)
            post_message(self.hwnd, 0, 0, 0)
            notify_icon(3, ctypes.byref(self.data))
        finally:
            destroy_menu(menu)
        if command in (1, 2, 3):
            # Native menu callbacks can arrive outside Tk's Python thread-state
            # context. Even root.after() is unsafe there. Only enqueue plain data.
            self.pending_commands.append(command)

    def dispatch_commands(self):
        while self.pending_commands and not self.app.closed.is_set():
            command = self.pending_commands.popleft()
            action = {1: self.app.enable, 2: self.app.disable, 3: self.app.close}[command]
            action()

    def close(self):
        if self.closed:
            return
        self.closed = True
        notify_icon(2, ctypes.byref(self.data))
        self.added = False
        destroy_window(self.hwnd)
        destroy_icon(self.icon)


def codex_windows():
    found = []

    @ENUMPROC
    def collect(hwnd, _):
        if not is_visible(hwnd):
            return True
        name = ctypes.create_unicode_buffer(128)
        get_class(hwnd, name, len(name))
        if not name.value.startswith('Chrome_WidgetWin_'):
            return True
        pid = W.DWORD()
        window_pid(hwnd, ctypes.byref(pid))
        handle = open_process(0x1000, False, pid.value)
        if not handle:
            return True
        try:
            path = ctypes.create_unicode_buffer(4096)
            size = W.DWORD(len(path))
            if process_path(handle, 0, path, ctypes.byref(size)):
                value = path.value.lower().replace('\\', '/')
                if ('/openai.codex_' in value or '/openai/codex/' in value) and value.endswith('/chatgpt.exe'):
                    rect = W.RECT()
                    client_rect(hwnd, ctypes.byref(rect))
                    if rect.right > 400 and rect.bottom > 250:
                        found.append((hwnd, pid.value))
        finally:
            close_handle(handle)
        return True

    enum_windows(collect, 0)
    return found


def metric(value):
    if value is None:
        return '—'
    if value >= 1_000_000:
        return f'{value / 1_000_000:.2f}M'
    if value >= 1000:
        return f'{value / 1000:.1f}K'
    return str(value)


class Portable:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.status = tk.StringVar(value='正在寻找 Codex 窗口')
        self.detail = tk.StringVar(value='')
        self.child = None
        self.parent = None
        self.pid = None
        self.enabled = True
        self.text = '等待当前聊天用量'
        self.latest = None
        self.layout = None
        self.layout_error = None
        self.closed = threading.Event()
        self.callback = WNDPROC(self.window_proc)
        self.brush = create_brush(colorref(THEME['surface']))
        self.divider_brush = create_brush(colorref(THEME['divider']))
        self.font = None
        self.border_pen = None
        self.style_height = None
        self.window_size = None
        self.placement = None
        self.next_window_scan = 0
        self.windows = []
        self.runs = []
        self.update_style(30)
        window_class = WindowClass()
        window_class.proc = self.callback
        window_class.instance = instance
        window_class.name = 'CodexPortableUsageChild'
        if not register_class(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.tray = TrayIcon(self)
        threading.Thread(target=self.read_usage, daemon=True).start()
        threading.Thread(target=self.track_layout, daemon=True).start()
        self.tick()
        self.render_tick()

    def window_proc(self, hwnd, message, wparam, lparam):
        if message == LAYOUT_CHANGED:
            if hwnd == self.child and not self.closed.is_set():
                try:
                    self.place_in_composer()
                except Exception as error:
                    self.layout_error = str(error)
            return 0
        if message == 0x000F:
            paint = PaintStruct()
            dc = begin_paint(hwnd, ctypes.byref(paint))
            rect = W.RECT()
            client_rect(hwnd, ctypes.byref(rect))
            fill_rect(dc, ctypes.byref(rect), self.brush)
            previous_brush = select_object(dc, self.brush)
            previous_pen = select_object(dc, self.border_pen)
            round_rect(dc, 0, 0, rect.right, rect.bottom, self.radius * 2, self.radius * 2)
            select_object(dc, previous_pen)
            select_object(dc, previous_brush)
            set_background_mode(dc, 1)
            previous_font = select_object(dc, self.font)
            x = self.padding
            for kind, text, width in self.runs:
                if kind == 'divider':
                    middle = x + (width - self.line_width) // 2
                    line = W.RECT(middle, (rect.bottom - self.divider_height) // 2,
                                  middle + self.line_width, (rect.bottom + self.divider_height) // 2)
                    fill_rect(dc, ctypes.byref(line), self.divider_brush)
                else:
                    set_text_color(dc, colorref(THEME[kind]))
                    text_rect = W.RECT(x, 0, x + width, rect.bottom)
                    draw_text(dc, text, len(text), ctypes.byref(text_rect), 0x0004 | 0x0020 | 0x0800)
                x += width
            select_object(dc, previous_font)
            end_paint(hwnd, ctypes.byref(paint))
            return 0
        if message == 0x0021:
            return 3  # MA_NOACTIVATE
        return default_proc(hwnd, message, wparam, lparam)

    def update_style(self, height):
        if height == self.style_height:
            return
        scale = height / 30
        font = create_font(-round(12 * scale), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
                           'Microsoft YaHei UI')
        line_width = max(1, round(scale))
        pen = create_pen(0, line_width, colorref(THEME['border']))
        if not font or not pen:
            if font:
                delete_object(font)
            if pen:
                delete_object(pen)
            raise ctypes.WinError(ctypes.get_last_error())
        if self.font:
            delete_object(self.font)
        if self.border_pen:
            delete_object(self.border_pen)
        self.font, self.border_pen = font, pen
        self.style_height = height
        self.padding = round(10 * scale)
        self.gap = round(17 * scale)
        self.radius = round(8 * scale)
        self.line_width = line_width
        self.divider_height = round(12 * scale)

    def attach(self, parent, pid):
        self.detach()
        previous_context = set_dpi_context(get_dpi_context(parent))
        try:
            # A layered child receives its own composited surface above Chromium's
            # accelerated content. An ordinary GDI child can be painted underneath it.
            self.child = create_window(0x08000000 | 0x00080000, 'CodexPortableUsageChild', 'Codex 用量',
                                       0x40000000, 0, 0, 400, 28, parent, None, instance, None)
            if not self.child:
                raise ctypes.WinError(ctypes.get_last_error())
            if not layered_alpha(self.child, 0, 255, 2):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            set_dpi_context(previous_context)
        if get_parent(self.child) != parent:
            self.detach()
            raise RuntimeError('子窗口归属验证失败')
        self.parent, self.pid = parent, pid

    def detach(self):
        if self.child and is_window(self.child):
            destroy_window(self.child)
        self.child = self.parent = self.pid = None
        self.layout = None
        self.window_size = None
        self.placement = None

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False
        self.detach()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        self.tray.close()
        self.detach()
        delete_object(self.brush)
        delete_object(self.divider_brush)
        delete_object(self.border_pen)
        delete_object(self.font)
        self.root.destroy()

    def read_usage(self):
        tracker = ActiveTask()
        store = Store(ROOT)
        while not self.closed.wait(1):
            pid = self.pid
            if not pid:
                continue
            try:
                sid = tracker.poll(pid)
                data = store.detail(sid) if sid else {}
                total = data.get('total') or {}
                turn = data.get('turn') or {}
                inputs = total.get('input_tokens')
                cached = total.get('cached_input_tokens')
                cache = f'{cached / inputs:.1%}' if inputs and cached is not None else '—'
                speed = data.get('tps')
                speed_text = f'≈{speed:.1f}' if speed is not None else '—'
                text = f'总 {metric(total.get("total_tokens"))}   本轮 {metric(turn.get("total_tokens"))}   缓存 {cache}   TPS {speed_text}'
                if not data.get('available'):
                    text = '等待当前聊天用量'
                self.latest = (pid, text, sid, f'缓存 {cache}   TPS {speed_text}')
            except Exception:
                self.latest = (pid, '用量暂不可用，正在重试', None, '等待用量')

    def track_layout(self):
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            from composer_layout import ComposerLayout
            locator = ComposerLayout()
            while not self.closed.wait(0.016):
                parent = self.parent
                if not parent:
                    continue
                try:
                    bounds = locator.locate(parent)
                    origin = W.POINT()
                    client_to_screen(parent, ctypes.byref(origin))
                    rect = W.RECT()
                    client_rect(parent, ctypes.byref(rect))
                    if parent != self.parent:
                        continue
                    if bounds:
                        left, top, right, bottom = bounds
                        bounds = (left - origin.x, top - origin.y, right - origin.x, bottom - origin.y)
                        self.layout = (parent, time.monotonic(), (rect.right, rect.bottom), bounds)
                        # Wake the child window directly. Waiting for a second Tk
                        # timer adds another frame of lag to UIA's notification.
                        if self.child:
                            post_message(self.child, LAYOUT_CHANGED, 0, 0)
                    self.layout_error = None
                except Exception as error:
                    self.layout_error = type(error).__name__
        except Exception as error:
            self.layout_error = str(error)
        finally:
            comtypes.CoUninitialize()

    def fit_text(self, available):
        variants = [self.latest[1], self.latest[3]] if self.latest and self.latest[0] == self.pid else ['等待用量']
        dc = get_dc(self.child)
        previous_font = select_object(dc, self.font)
        try:
            for text in variants:
                runs = []
                for index, part in enumerate(text.split('   ')):
                    if index:
                        runs.append(('divider', '', self.gap))
                    label, space, value = part.partition(' ')
                    pieces = [('label', part)]
                    if space and label in ('总', '本轮', '缓存', 'TPS'):
                        pieces = [('label', label + space), ('value', value)]
                    for kind, piece in pieces:
                        size = W.SIZE()
                        text_extent(dc, piece, len(piece), ctypes.byref(size))
                        runs.append((kind, piece, size.cx))
                width = sum(run[2] for run in runs) + self.padding * 2
                if width <= available:
                    return text, width, runs
        finally:
            select_object(dc, previous_font)
            release_dc(self.child, dc)
        return None

    def place_in_composer(self):
        rect = W.RECT()
        client_rect(self.parent, ctypes.byref(rect))
        layout = self.layout
        if (not layout or layout[0] != self.parent or time.monotonic() - layout[1] > 0.5
                or not layout[3]):
            show_window(self.child, 0)
            return '正在定位输入框' if not self.layout_error else '输入框定位暂不可用'
        left, top, right, bottom = layout[3]
        self.update_style(bottom - top)
        fitted = self.fit_text(right - left)
        if not fitted or left < 0 or top < 0:
            show_window(self.child, 0)
            return '工具栏空间不足，等待布局恢复'
        text, width, runs = fitted
        height = bottom - top
        # A fresh sample may lag one render frame during a resize. Keep the last
        # valid placement until the next sample instead of toggling visibility.
        if left + width > rect.right or bottom > rect.bottom:
            return '正在跟随输入框布局'
        content_changed = text != self.text or runs != self.runs
        self.text, self.runs = text, runs
        size = (width, height)
        size_changed = self.window_size != size
        if size_changed:
            region = round_region(0, 0, width + 1, height + 1, self.radius * 2, self.radius * 2)
            if not region:
                raise ctypes.WinError(ctypes.get_last_error())
            if not set_region(self.child, region, False):
                delete_object(region)
                raise ctypes.WinError(ctypes.get_last_error())
            # Windows owns the region after SetWindowRgn succeeds.
            self.window_size = size
        placement = (left, top, width, height)
        if self.placement != placement or not is_visible(self.child):
            position_window(self.child, W.HWND(0), left, top, width, height, 0x0010 | 0x0040)
            self.placement = placement
        if content_changed or size_changed:
            invalidate(self.child, None, False)
        return '已自动嵌入输入框工具栏'

    def render_tick(self):
        if self.closed.is_set():
            return
        try:
            if self.child and is_window(self.child):
                self.status.set(self.place_in_composer())
        except Exception as error:
            self.detach()
            self.status.set('挂载失败')
            self.detail.set(str(error))
        self.root.after(16, self.render_tick)

    def tick(self):
        self.tray.dispatch_commands()
        if self.closed.is_set():
            return
        try:
            now = time.monotonic()
            if now >= self.next_window_scan:
                self.windows = codex_windows()
                self.next_window_scan = now + 0.5
            windows = self.windows if self.enabled else []
            if len(windows) != 1:
                self.detach()
                self.status.set('已移除用量条' if not self.enabled else
                                '等待打开 Codex' if not windows else '检测到多个窗口，暂停挂载')
                self.detail.set('打开便携工具后，可以随时显示、移除或退出。')
            else:
                parent, pid = windows[0]
                if parent != self.parent or not self.child or not is_window(self.child):
                    self.attach(parent, pid)
                self.detail.set(f'进程 {pid} · 本地读取用量\n' + self.text)
        except Exception as error:
            self.detach()
            self.status.set('挂载失败')
            self.detail.set(str(error))
        self.tray.update(self.status.get(), self.text)
        self.root.after(150, self.tick)


def main():
    api(user32, 'SetProcessDpiAwarenessContext', W.BOOL, [W.HANDLE])(W.HANDLE(-4))
    mutex_api = api(kernel32, 'CreateMutexW', W.HANDLE, [ctypes.c_void_p, W.BOOL, W.LPCWSTR])
    mutex = mutex_api(None, False, 'Local\\CodexUsagePortableNative')
    if ctypes.get_last_error() == 183:
        return
    try:
        app = Portable()
        if '--verify' in sys.argv:
            def verify():
                report = {'attached': bool(app.child), 'codex_pid': app.pid,
                          'parent_verified': bool(app.child and get_parent(app.child) == app.parent),
                          'text': app.text, 'thread': app.latest[2] if app.latest else None,
                          'visible': bool(app.child and is_visible(app.child)),
                          'layout': app.layout, 'layout_error': app.layout_error,
                          'theme': THEME, 'strip_size': app.window_size,
                          'text_runs': app.runs, 'tray_added': app.tray.added,
                          'controller_hidden': app.root.state() == 'withdrawn',
                          'tray_window_hidden': not bool(is_visible(app.tray.hwnd))}
                parent, child = app.parent, app.child
                app.disable()
                report['removed'] = bool(child and not is_window(child))
                report['codex_still_running'] = bool(parent and is_window(parent))
                app.enable()
                app.tick()
                report['reattached'] = bool(app.child and get_parent(app.child) == parent)
                app.close()
                report['tray_removed_on_exit'] = not app.tray.added and not is_window(app.tray.hwnd)
                (BASE / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            app.root.after(4000, verify)
        app.root.mainloop()
    finally:
        if mutex:
            close_handle(mutex)


if __name__ == '__main__':
    main()
