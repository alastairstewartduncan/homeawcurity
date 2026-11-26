"""Home Camera: motion recording, live view and playback for IP cameras."""
try:
    # Written by the build (GitHub Actions) so each installer carries its own number.
    from ._build import BUILT, COMMIT, VERSION as __version__
except ImportError:  # running from source
    __version__, COMMIT, BUILT = "dev", "", ""
