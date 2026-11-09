from pathlib import Path


class Backlight:
    """Only use an explicitly configured backlight; never change permissions."""
    def __init__(self, path=""):
        self.path = Path(path) if path else None
        self.previous = None

    def off(self):
        if self.path:
            self.previous = self.path.read_text().strip()
            self.path.write_text("0")

    def wake(self):
        if self.path and self.previous is not None:
            self.path.write_text(self.previous)
            self.previous = None
