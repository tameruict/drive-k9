@echo off
echo ====================================================
echo DONG GOI VA MA HOA PHAN MEM GDRIVE DOWNLOADER
echo ====================================================

echo 1. Cai dat cac thu vien can thiet...
pip install pyinstaller pyarmor requests urllib3

echo.
echo 2. Xoa thu muc build cu...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist build_obf rmdir /s /q build_obf

echo.
echo 3. Chuan bi moi truong build...
mkdir build_obf
copy *.py build_obf\ >nul

cd build_obf

echo.
echo 4. Dang ma hoa code bang PyArmor...
pyarmor gen -O obfuscated gdrive_downloader_ui.py license_manager.py
copy /y obfuscated\gdrive_downloader_ui.py . >nul
copy /y obfuscated\license_manager.py . >nul
xcopy /y /e /i obfuscated\pyarmor_runtime_000000 pyarmor_runtime_000000 >nul

echo.
echo 5. Tao file Launcher de PyInstaller nhan dien thu vien...
echo import __future__ > launcher.py
echo import os, json, queue, re, subprocess, sys, threading, pathlib >> launcher.py
echo import tkinter, tkinter.filedialog, tkinter.messagebox, tkinter.ttk >> launcher.py
echo import base64, hashlib, hmac, datetime, uuid >> launcher.py
echo import gdrive_stream_downloader >> launcher.py
echo import idm_downloader >> launcher.py
echo import pyarmor_runtime_000000 >> launcher.py
echo if hasattr(sys, '_MEIPASS'): >> launcher.py
echo     sys.path.insert(0, sys._MEIPASS) >> launcher.py
echo if __name__ == '__main__': >> launcher.py
echo     if len(sys.argv) ^> 1 and sys.argv[1] == '--run-downloader': >> launcher.py
echo         sys.argv.pop(1) >> launcher.py
echo         sys.exit(gdrive_stream_downloader.main()) >> launcher.py
echo     import gdrive_downloader_ui >> launcher.py
echo     sys.exit(gdrive_downloader_ui.main()) >> launcher.py

echo.
echo 6. Dang dong goi thanh file .exe bang PyInstaller...
if exist app.ico (
    pyinstaller --noconsole --onefile --icon="app.ico" --name "GDriveDownloader" --add-data "obfuscated\gdrive_downloader_ui.py;." --add-data "obfuscated\license_manager.py;." --add-data "obfuscated\pyarmor_runtime_000000;pyarmor_runtime_000000" --hidden-import pyarmor_runtime_000000 launcher.py
) else (
    pyinstaller --noconsole --onefile --name "GDriveDownloader" --add-data "obfuscated\gdrive_downloader_ui.py;." --add-data "obfuscated\license_manager.py;." --add-data "obfuscated\pyarmor_runtime_000000;pyarmor_runtime_000000" --hidden-import pyarmor_runtime_000000 launcher.py
)

cd ..
if not exist dist mkdir dist
copy build_obf\dist\GDriveDownloader.exe dist\ >nul

echo.
echo 7. Don dep file rac...
rmdir /s /q build_obf
if exist GDriveDownloader.spec del /q GDriveDownloader.spec

echo.
echo ====================================================
echo THANH CONG! 
echo File chay chuong trinh nam o: dist\GDriveDownloader.exe
echo ====================================================
pause
