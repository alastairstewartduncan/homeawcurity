"""Entry point for the packaged camtray.exe: the tray icon (see camhub/tray.py)."""
import sys

from camhub.tray import main

if __name__ == "__main__":
    sys.exit(main())
