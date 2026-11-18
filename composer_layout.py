"""Locate the free toolbar area using read-only Windows accessibility metadata."""
import comtypes.client
import time

UIA = comtypes.client.GetModule('UIAutomationCore.dll')


class ComposerLayout:
    def __init__(self):
        self.automation = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        self.cache = self.automation.CreateCacheRequest()
        for property_id in (30005, 30001, 30022):  # Name, bounding rectangle, offscreen.
            self.cache.AddProperty(property_id)
        buttons = self.automation.CreatePropertyCondition(30003, 50000)
        images = self.automation.CreatePropertyCondition(30003, 50006)
        self.condition = self.automation.CreateOrCondition(buttons, images)
        names = self.automation.CreateOrConditionFromArray([
            self.automation.CreatePropertyCondition(30005, name)
            for name in ('更改权限', 'Change permissions', 'Permissions', '完全访问', 'Full access')
        ])
        self.permissions = self.automation.CreateAndCondition(buttons, names)
        self.toolbar = None
        self.hwnd = None
        self.retry_at = 0

    def locate(self, hwnd):
        if hwnd != self.hwnd:
            self.toolbar = None
            self.hwnd = hwnd
            self.retry_at = 0
        if self.toolbar is not None:
            try:
                bounds = self.read_toolbar(self.toolbar)
                if bounds:
                    return bounds
            except comtypes.COMError:
                pass
            self.toolbar = None
        if time.monotonic() < self.retry_at:
            return None
        self.retry_at = time.monotonic() + 0.5
        root = self.automation.ElementFromHandle(hwnd)
        permissions = root.FindAll(4, self.permissions)
        if permissions.Length != 1:
            return None
        container = permissions.GetElement(0)
        # Cache the smallest common container. Subsequent frames fetch just the
        # composer controls in one UIA call instead of traversing the whole chat.
        for _ in range(6):
            container = self.automation.ControlViewWalker.GetParentElement(container)
            if not container:
                return None
            bounds = self.read_toolbar(container)
            if bounds:
                self.toolbar = container
                return bounds
        return None

    def read_toolbar(self, container):
        elements = container.FindAllBuildCache(4, self.condition, self.cache)
        controls = []
        for index in range(elements.Length):
            element = elements.GetElement(index)
            rect = element.CachedBoundingRectangle
            if not element.CachedIsOffscreen and rect.right > rect.left and rect.bottom > rect.top:
                controls.append((element.CachedName or '', (rect.left, rect.top, rect.right, rect.bottom)))
        permissions = [(name, rect) for name, rect in controls
                       if name in ('更改权限', 'Change permissions', 'Permissions', '完全访问', 'Full access')]
        if len(permissions) != 1:
            return None
        left = permissions[0][1]
        middle = (left[1] + left[3]) / 2
        right_controls = [rect for name, rect in controls if rect[0] >= left[2]
                          and rect[1] <= middle <= rect[3]]
        if not right_controls:
            return None
        right = min(right_controls, key=lambda rect: rect[0])
        # The original embedded badge is 30px tall alongside 28px controls.
        scale = (left[3] - left[1]) / 28
        margin = max(1, round(scale))
        gap = round(8 * scale)
        return (left[2] + gap, left[1] - margin, right[0] - gap, left[3] + margin)
