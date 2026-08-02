#!/bin/bash
echo "===================================================="
echo "DONG GOI VA MA HOA PHAN MEM GDRIVE DOWNLOADER CHO LINUX"
echo "===================================================="

# Cài đặt python3-tk nếu chưa có (dành cho Ubuntu/Debian)
echo "Kiem tra cac goi he thong..."
if command -v apt-get &> /dev/null; then
    sudo apt-get update
    sudo apt-get install -y python3-tk python3-pip
fi

echo -e "\n1. Cai dat cac thu vien Python can thiet..."
pip3 install pyinstaller pyarmor requests urllib3

echo -e "\n2. Xoa thu muc build cu..."
rm -rf build dist build_obf

echo -e "\n3. Chuan bi moi truong build..."
mkdir -p build_obf
cp *.py build_obf/

cd build_obf

echo -e "\n4. Dang ma hoa code bang PyArmor..."
pyarmor gen -O obfuscated gdrive_downloader_ui.py license_manager.py
cp -f obfuscated/gdrive_downloader_ui.py .
cp -f obfuscated/license_manager.py .
cp -R obfuscated/pyarmor_runtime_000000 pyarmor_runtime_000000

echo -e "\n5. Tao file Launcher de PyInstaller nhan dien thu vien..."
cat << 'EOF' > launcher.py
import __future__
import os, json, queue, re, subprocess, sys, threading, pathlib
import tkinter, tkinter.filedialog, tkinter.messagebox, tkinter.ttk
import base64, hashlib, hmac, datetime, uuid
import gdrive_stream_downloader
import idm_downloader
import pyarmor_runtime_000000
if hasattr(sys, '_MEIPASS'):
    sys.path.insert(0, sys._MEIPASS)
if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--run-downloader':
        sys.argv.pop(1)
        sys.exit(gdrive_stream_downloader.main())
    import gdrive_downloader_ui
    sys.exit(gdrive_downloader_ui.main())
EOF

echo -e "\n6. Dang dong goi thanh file thuc thi cho Linux bang PyInstaller..."
# Trên Linux, giao diện tkinter yêu cầu X11, ta thường dùng --windowed hoặc không thêm flag gì
# Thêm các file obfuscated vào data (lưu ý dùng dấu hai chấm ":" trên Linux thay vì chấm phẩy ";")
pyinstaller --onefile --name "GDriveDownloader-Linux" \
    --add-data "obfuscated/gdrive_downloader_ui.py:." \
    --add-data "obfuscated/license_manager.py:." \
    --add-data "obfuscated/pyarmor_runtime_000000:pyarmor_runtime_000000" \
    --hidden-import pyarmor_runtime_000000 \
    launcher.py

cd ..
mkdir -p dist
cp build_obf/dist/GDriveDownloader-Linux dist/

echo -e "\n7. Don dep file rac..."
rm -rf build_obf GDriveDownloader-Linux.spec

echo -e "\n===================================================="
echo "THANH CONG!"
echo "File chay chuong trinh nam o: dist/GDriveDownloader-Linux"
echo "Ban co the cap quyen thuc thi va chay: chmod +x dist/GDriveDownloader-Linux"
echo "Lưu ý: Tool yêu cầu giao diện (Tkinter), nên chỉ chạy trên Linux Desktop."
echo "===================================================="
