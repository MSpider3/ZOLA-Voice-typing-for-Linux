# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

a = Analysis(
    ['app.py'],
    pathex=[],
    # sherpa_onnx is imported lazily (only for sherpa-onnx models), so it is
    # listed explicitly together with its native libraries
    binaries=collect_dynamic_libs('sherpa_onnx'),
    datas=collect_data_files('faster_whisper'),
    hiddenimports=['sherpa_onnx'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
# Use the desktop's own PortAudio/ALSA (the rpm/deb depend on PortAudio). Copies
# from the build machine (Ubuntu) cannot load the system's ALSA plugins, e.g.
# PipeWire's on Fedora, so the default microphone fell back to a raw device and
# failed with "Invalid sample rate".
a.binaries = [b for b in a.binaries if not b[0].startswith(('libportaudio', 'libasound.so', 'libjack'))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='zola-daemon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
