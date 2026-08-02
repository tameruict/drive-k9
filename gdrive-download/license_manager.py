import base64
import hashlib
import hmac
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import sys

# Tạo khóa bí mật động (làm khó quá trình dịch ngược tĩnh)
# Thay vì chuỗi rõ ràng, gộp từ nhiều mảnh base64 và đảo chuỗi
_K1 = "R0RyaXZlUGluaw==" # GDrivePink
_K2 = "RG93bmxvYWRlckAyMDI2" # Downloader@2026
_K3 = "IVNlY3JldEtleVhZWg==" # !SecretKeyXYZ
def _get_secret() -> str:
    p1 = base64.b64decode(_K1).decode()
    p2 = base64.b64decode(_K2).decode()
    p3 = base64.b64decode(_K3).decode()
    return f"{p1}{p2}{p3}"

SECRET_KEY = _get_secret()

def _check_debugger():
    """Kiểm tra cơ bản xem có đang bị debug/hook không"""
    gettrace = getattr(sys, 'gettrace', None)
    if gettrace is not None and gettrace():
        print("Debugger detected!")
        sys.exit(1)
        
_check_debugger()

def get_app_dir() -> Path:
    """Trả về thư mục chứa file exe (nếu đã đóng gói) hoặc thư mục chứa file py."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

LICENSE_FILE = get_app_dir() / ".license.json"

def get_hwid() -> str:
    """Lấy Hardware ID độc nhất của máy tính."""
    try:
        if os.name == 'nt':
            # Thử lấy UUID bằng PowerShell (thay thế cho wmic bị khai tử trên Windows mới)
            cmd = ['powershell', '-Command', '(Get-CimInstance -Class Win32_ComputerSystemProduct).UUID']
            output = subprocess.check_output(cmd, creationflags=subprocess.CREATE_NO_WINDOW).decode().strip()
            if output and len(output) > 10 and output != "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF":
                return output
    except Exception:
        pass
        
    try:
        if os.name == 'nt':
            # Fallback 1: Dùng MachineGuid trong Registry (Rất ổn định trên Windows)
            import winreg
            registry = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
            key = winreg.OpenKey(registry, r"SOFTWARE\Microsoft\Cryptography")
            machine_guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            if machine_guid:
                return machine_guid
    except Exception:
        pass

    # Fallback 2: dùng MAC address của máy tính (luôn hoạt động nhưng có thể thay đổi do VPN/Mạng ảo)
    import uuid
    mac = uuid.getnode()
    # Format lại MAC cho đẹp
    mac_str = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
    return f"MAC-{mac_str}"

def generate_license(hwid: str, expiry_date: str) -> str:
    """
    Tạo license key.
    expiry_date định dạng: 'YYYY-MM-DD' hoặc '2099-12-31' cho vĩnh viễn
    """
    payload = f"{hwid}|{expiry_date}"
    signature = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    raw_key = f"{payload}|{signature}"
    return base64.b64encode(raw_key.encode('utf-8')).decode('utf-8')

def verify_license(key: str) -> tuple[bool, str]:
    """
    Kiểm tra tính hợp lệ của key.
    Trả về (True/False, Thông_báo_hoặc_ngày_hết_hạn)
    """
    try:
        decoded = base64.b64decode(key).decode('utf-8')
        parts = decoded.split('|')
        if len(parts) != 3:
            return False, "Key không đúng định dạng."
        
        hwid, expiry_date, signature = parts
        
        # Kiểm tra signature
        expected_payload = f"{hwid}|{expiry_date}"
        expected_signature = hmac.new(SECRET_KEY.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()[:16]
        
        if signature != expected_signature:
            return False, "Key không hợp lệ hoặc đã bị thay đổi."
            
        # Kiểm tra HWID
        current_hwid = get_hwid()
        if hwid != current_hwid:
            return False, "Key không dành cho máy tính này."
            
        # Kiểm tra hạn sử dụng
        try:
            expiry_dt = datetime.strptime(expiry_date, "%Y-%m-%d")
            # Set to end of day
            expiry_dt = expiry_dt.replace(hour=23, minute=59, second=59)
            now_dt = datetime.now()
            if now_dt > expiry_dt:
                return False, f"Key đã hết hạn vào ngày {expiry_date}."
        except ValueError:
            return False, "Lỗi định dạng ngày tháng trong key."
            
        return True, expiry_date
    except Exception:
        return False, "Key không đúng định dạng."

def load_saved_license() -> str:
    """Đọc key đã lưu."""
    try:
        if LICENSE_FILE.exists():
            data = json.loads(LICENSE_FILE.read_text(encoding='utf-8'))
            return data.get("license_key", "")
    except Exception:
        pass
    return ""

def save_license(key: str) -> None:
    """Lưu key vào file cục bộ."""
    try:
        data = {"license_key": key}
        LICENSE_FILE.write_text(json.dumps(data, indent=4), encoding='utf-8')
    except Exception:
        pass
