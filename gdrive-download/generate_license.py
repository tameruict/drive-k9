#!/usr/bin/env python3
import sys
from license_manager import generate_license, get_hwid

def main():
    print("=== PHẦN MỀM TẠO LICENSE KEY ===")
    
    # Cho phép tự nhập HWID hoặc lấy HWID máy hiện tại
    print("1. Lấy HWID của máy này")
    print("2. Nhập HWID của khách hàng")
    choice = input("Chọn (1 hoặc 2): ").strip()
    
    if choice == '1':
        hwid = get_hwid()
        print(f"\nHardware ID (HWID) máy này: {hwid}")
    else:
        hwid = input("\nNhập Hardware ID của khách: ").strip()
        
    if not hwid:
        print("Lỗi: HWID không được để trống!")
        return
        
    print("\nNhập ngày hết hạn (Định dạng: YYYY-MM-DD)")
    print("Để trống nếu muốn cấp vĩnh viễn (mặc định: 2099-12-31)")
    expiry = input("Ngày hết hạn: ").strip()
    if not expiry:
        expiry = "2099-12-31"
        
    key = generate_license(hwid, expiry)
    
    print("\n" + "="*50)
    print("TẠO KEY THÀNH CÔNG!")
    print("="*50)
    print(f"HWID khách: {hwid}")
    print(f"Ngày hết hạn: {expiry}")
    print("\nLICENSE KEY CỦA KHÁCH LÀ:")
    print(key)
    print("="*50)
    print("Copy đoạn mã trên và gửi cho khách hàng.")
    
    # Tự động lưu vào file quản lý CSV
    import csv
    from datetime import datetime
    
    db_file = "keys_database.csv"
    file_exists = False
    try:
        with open(db_file, 'r', encoding='utf-8'):
            file_exists = True
    except FileNotFoundError:
        pass
        
    try:
        with open(db_file, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Ngày Tạo", "HWID", "Ngày Hết Hạn", "License Key", "Ghi Chú/Tên Khách"])
            
            note = input("\n(Tùy chọn) Nhập tên khách hàng hoặc ghi chú để lưu vào Database: ").strip()
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hwid, expiry, key, note])
        print(f"\n[OK] Đã tự động lưu thông tin Key vào file '{db_file}' để bạn dễ quản lý!")
    except Exception as e:
        print(f"\n[LỖI] Không thể lưu vào database: {e}")

    input("\nNhấn Enter để thoát...")

if __name__ == "__main__":
    main()
