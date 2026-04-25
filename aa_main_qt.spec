# -*- mode: python ; coding: utf-8 -*-
#
# AA 創作翻譯輔助小工具 — PyInstaller spec
#
# 打包方式：pyinstaller aa_main_qt.spec
# 輸出：dist/aa_main_qt/
#   aa_main_qt.exe        主程式
#   aa_url_fetch_qt.exe   URL 抓取子程序（由主程式以 subprocess 啟動）
#   _internal/            所有依賴與資料
#
# 注意事項：
#   - 不使用 UPX（避免誤報）
#   - noconsole（GUI 程式）
#   - AA_Settings.json / aa_settings_cache.json / aa_original_cache.json
#     為執行期產生，不打包；首次執行時程式自行在 exe 旁建立。

block_cipher = None

# ════════════════════════════════════════════════
#  共用 datas（兩個 EXE 都需要）
# ════════════════════════════════════════════════

shared_datas = [
    ('fonts',           'fonts'),           # 內建字體（Monapo 等）
    ('aa_tool/dark_theme.qss', 'aa_tool'),  # Qt stylesheet
]

shared_hiddenimports = [
    'PyQt6.sip',
    'aa_settings_dialog_qt',     # lazy import（在 function 內才 import）
    'aa_wiki_name_dialog_qt',    # 同上
]

# ════════════════════════════════════════════════
#  Analysis 1：主程式 aa_main_qt.py
# ════════════════════════════════════════════════

a1 = Analysis(
    ['aa_main_qt.py'],
    pathex=[],
    binaries=[],
    datas=shared_datas,
    hiddenimports=shared_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',       # Pillow 已安裝但本專案未使用
        'customtkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz1 = PYZ(a1.pure, cipher=block_cipher)

exe1 = EXE(
    pyz1,
    a1.scripts,
    [],
    exclude_binaries=True,   # onedir：binary 交由 COLLECT 統一管理
    name='aa_main_qt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # 不使用 UPX
    console=False,           # noconsole（GUI 程式）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='path/to/icon.ico',  # 有圖示時取消注釋
)

# ════════════════════════════════════════════════
#  Analysis 2：URL 抓取子程序 aa_url_fetch_qt.py
# ════════════════════════════════════════════════

a2 = Analysis(
    ['aa_url_fetch_qt.py'],
    pathex=[],
    binaries=[],
    datas=shared_datas,
    hiddenimports=shared_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
        'customtkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz2 = PYZ(a2.pure, cipher=block_cipher)

exe2 = EXE(
    pyz2,
    a2.scripts,
    [],
    exclude_binaries=True,
    name='aa_url_fetch_qt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='path/to/icon.ico',
)

# ════════════════════════════════════════════════
#  COLLECT：合併成單一 onedir 輸出資料夾
# ════════════════════════════════════════════════

coll = COLLECT(
    exe1,
    a1.binaries,
    a1.zipfiles,
    a1.datas,
    exe2,
    a2.binaries,
    a2.zipfiles,
    a2.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='aa_main_qt',   # 輸出資料夾名稱：dist/aa_main_qt/
)
