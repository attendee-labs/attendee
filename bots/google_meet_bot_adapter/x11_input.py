import logging

from Xlib import XK, X, display
from Xlib.ext import xtest

logger = logging.getLogger(__name__)


class X11Input:
    def __init__(self):
        self.disp = display.Display()
        self.root = self.disp.screen().root

    def move_rel(self, dx: int, dy: int):
        ptr = self.root.query_pointer()._data
        new_x = ptr["root_x"] + dx
        new_y = ptr["root_y"] + dy
        xtest.fake_input(self.disp, X.MotionNotify, x=new_x, y=new_y)
        self.disp.sync()

    def left_click(self):
        xtest.fake_input(self.disp, X.ButtonPress, 1)
        xtest.fake_input(self.disp, X.ButtonRelease, 1)
        self.disp.sync()

    def key(self, keycode: int):
        xtest.fake_input(self.disp, X.KeyPress, keycode)
        xtest.fake_input(self.disp, X.KeyRelease, keycode)
        self.disp.sync()

    BUTTON_MAP = {"left": 1, "middle": 2, "right": 3}

    def button_press(self, button_name: str):
        btn = self.BUTTON_MAP.get(button_name, 1)
        xtest.fake_input(self.disp, X.ButtonPress, btn)
        self.disp.sync()

    def button_release(self, button_name: str):
        btn = self.BUTTON_MAP.get(button_name, 1)
        xtest.fake_input(self.disp, X.ButtonRelease, btn)
        self.disp.sync()

    SPECIAL_KEY_MAP = {
        "Enter": "Return",
        "Backspace": "BackSpace",
        "Tab": "Tab",
        "Escape": "Escape",
        "ArrowUp": "Up",
        "ArrowDown": "Down",
        "ArrowLeft": "Left",
        "ArrowRight": "Right",
        "Shift": "Shift_L",
        "Control": "Control_L",
        "Alt": "Alt_L",
        "Meta": "Super_L",
        "CapsLock": "Caps_Lock",
        "Delete": "Delete",
        "Home": "Home",
        "End": "End",
        "PageUp": "Page_Up",
        "PageDown": "Page_Down",
        "Insert": "Insert",
        "F1": "F1",
        "F2": "F2",
        "F3": "F3",
        "F4": "F4",
        "F5": "F5",
        "F6": "F6",
        "F7": "F7",
        "F8": "F8",
        "F9": "F9",
        "F10": "F10",
        "F11": "F11",
        "F12": "F12",
        " ": "space",
    }

    def _key_name_to_keycode(self, key_name: str) -> int | None:
        xk_name = self.SPECIAL_KEY_MAP.get(key_name, key_name)
        keysym = XK.string_to_keysym(xk_name)
        if keysym == 0:
            return None
        return self.disp.keysym_to_keycode(keysym)

    def key_press(self, key_name: str):
        kc = self._key_name_to_keycode(key_name)
        if kc is None:
            logger.warning("Unknown key name: %s", key_name)
            return
        xtest.fake_input(self.disp, X.KeyPress, kc)
        self.disp.sync()

    def key_release(self, key_name: str):
        kc = self._key_name_to_keycode(key_name)
        if kc is None:
            logger.warning("Unknown key name: %s", key_name)
            return
        xtest.fake_input(self.disp, X.KeyRelease, kc)
        self.disp.sync()
