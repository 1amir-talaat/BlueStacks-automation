from PIL import Image


class UIActions:
    """High-level UI interaction actions."""

    def __init__(self, adb_controller, screen_capture):
        self.adb = adb_controller
        self.screen = screen_capture

    def wait_for_text(self, text: str, timeout: int = 30) -> bool:
        pass

    def click_button_by_text(self, text: str):
        pass

    def wait_and_click(self, text: str, timeout: int = 30):
        pass

    def handle_popup(self, accept_text: str = "Accept", decline_text: str = "Decline"):
        pass
