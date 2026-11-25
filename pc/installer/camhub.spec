# PyInstaller recipe for camhub.exe.  Build from the pc folder:
#     pyinstaller --noconfirm installer/camhub.spec
import os

from PyInstaller.utils.hooks import collect_submodules

pc = os.path.abspath(".")
datas = [(os.path.join(pc, "camhub", "static"), os.path.join("camhub", "static"))]
if os.path.isdir(os.path.join(pc, "models")):
    datas.append((os.path.join(pc, "models"), "models"))

a = Analysis(
    [os.path.join(pc, "installer", "camhub_entry.py")],
    pathex=[pc],
    datas=datas,
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("camhub"),
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide6", "IPython", "pytest"],
)
pyz = PYZ(a.pure)
icon = os.path.join(pc, "installer", "homecamera.ico")
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="camhub",
    console=True,
    icon=icon if os.path.exists(icon) else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="camhub")
