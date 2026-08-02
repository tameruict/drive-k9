# Google Drive stream downloader

Tool CLI Python de tai video/file tu Google Drive bang cookie va ghi file theo luong stream tung chunk. Dung cho file cua ban hoac file ban co quyen truy cap.

## Cai dat

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Chay giao dien

Mo UI mau hong glassmorphism:

```powershell
python .\gdrive_downloader_ui.py
```

Hoac double click:

```powershell
.\run_ui.bat
```

Trong UI:

- `MODE auto`: tu nhan link folder neu URL co `/folders/`, con lai xem nhu file.
- `MODE file`: dung khi ban nhap file ID truc tiep.
- `MODE folder`: dung khi ban nhap folder ID truc tiep.
- `FILE COOKIES.TXT`: co the de trong neu file public.
- `TEN FILE`: de trong de script tu lay ten file; voi folder thi truong nay khong duoc dung.
- `SEGMENTS`: so ket noi song song cho mot file lon. Co the nhap so tuy y trong UI; de `4` hoac `6` neu Google Drive bop toc do mot stream.
- UI tu luu lai `FILE COOKIES.TXT`, `THU MUC LUU`, va cac tuy chon tai vao `.gdrive_downloader_ui_settings.json`.

## Cach dung nhanh

Tai bang link Google Drive:

```powershell
$env:GDRIVE_COOKIE = 'PASTE_COOKIE_HEADER_HERE'
python .\gdrive_stream_downloader.py --url "https://drive.google.com/file/d/FILE_ID/view" -o video.mp4 --resume
```

Tai bang file id:

```powershell
python .\gdrive_stream_downloader.py --file-id "FILE_ID" -o video.mp4 --resume
```

Tai toan bo folder, bao gom folder con:

```powershell
python .\gdrive_stream_downloader.py --folder-url "https://drive.google.com/drive/folders/FOLDER_ID" -o ".\downloads" --cookie-file .\cookies.txt --resume
```

Voi folder Google Drive, `-o ".\downloads"` la thu muc cha. Tool se tu tao them `.\downloads\<ten folder Drive>\` va giu nguyen cac folder con ben trong.

Xem cay thu muc truoc khi tai:

```powershell
python .\gdrive_stream_downloader.py --folder-id "FOLDER_ID" --cookie-file .\cookies.txt --list-only
```

Dung cookie tu file `cookies.txt` dang Netscape hoac mot file text chua raw Cookie header:

```powershell
python .\gdrive_stream_downloader.py --url "https://drive.google.com/file/d/FILE_ID/view" --cookie-file .\cookies.txt -o video.mp4 --resume
```

Tai mot URL stream truc tiep da bat duoc tu DevTools:

```powershell
python .\gdrive_stream_downloader.py --url "https://...stream-url..." --cookie-file .\cookies.txt -o video.mp4 --resume
```

Neu URL stream yeu cau gui nguyen raw Cookie header, them `--force-cookie-header`. Tuy nhien tuy chon nay co the gui cookie sang host redirect, nen chi dung voi URL ban tin cay.

## Lay cookie

Khuyen nghi dung extension xuat cookie ra file Netscape `cookies.txt`, hoac copy gia tri request header `Cookie` tu trinh duyet cua chinh ban. Khong dua cookie vao chat, khong commit cookie vao git, va xoa/rotate cookie neu lo bi ro ri.

## Tuy chon huu ich

```powershell
python .\gdrive_stream_downloader.py --help
```

- `--resume`: tiep tuc file dang tai neu server ho tro `Range`.
- `--chunk-size 8M`: tang kich thuoc chunk khi mang on dinh.
- `--segments 4`: chia mot file lon thanh 4 ket noi Range song song de cai thien toc do khi Drive throttle.
- `--cookie-env NAME`: doc Cookie header tu bien moi truong khac thay vi `GDRIVE_COOKIE`.
- `--referer URL`: doi referer neu stream endpoint yeu cau.
- `--folder-url URL` / `--folder-id ID`: tai folder Google Drive de quy.
- `--list-only`: in cay thu muc folder ma khong tai.
- `--workspace-format office`: export Google Docs/Sheets/Slides thanh `.docx`, `.xlsx`, `.pptx`.
- `--workspace-format pdf`: export Google Docs/Sheets/Slides/Drawings thanh PDF neu co the.

## Video Google Drive bi chan download

Voi mot so file `.mp4`, endpoint download cua Google Drive co the tra ve trang `Google Drive - Can't download file` du file van xem duoc trong preview. Tool se tu thu fallback sang video stream preview neu file co MIME `video/*`, ten output la video, hoac khong xac dinh duoc MIME. Fallback nay van ho tro `--resume` bang header `Range`.

Neu toc do chi khoang vai tram KB/s, day thuong la gioi han tren tung ket noi cua Google Drive, khong phai gioi han chunk size. Dung `--segments 4` den `--segments 8` de mo nhieu Range request song song cho file con tren 8 MB. UI mac dinh dung `SEGMENTS = 4`. Luu y `10 Mb/s` theo nha mang tuong duong khoang `1.25 MB/s`; con `10 MB/s` moi tuong duong khoang `80 Mb/s`.

## Dung module IDMDownloader trong code Python

`idm_downloader.py` cung cap class `IDMDownloader` dung `asyncio + aiohttp`, HTTP Range, connection pooling, dynamic segmentation, checkpoint `.idm.json`, file tam `.part`, retry `403/429/5xx`, va tu follow trang confirm cua Google Drive cho file lon.

```python
from idm_downloader import IDMDownloader

downloader = IDMDownloader(
    "https://drive.google.com/file/d/FILE_ID/view",
    "video.mp4",
    cookies="PASTE_COOKIE_HEADER_HERE",  # hoac bo trong neu file public
    headers={
        "Referer": "https://drive.google.com/",
    },
    concurrency=8,
    chunk_size=4 * 1024 * 1024,
    min_split_size=8 * 1024 * 1024,
    timeout=30,
    max_retries=5,
)

downloader.download_sync()
```

Neu dang o trong async app:

```python
await downloader.download()
```

Khi chay lai sau loi mang, class se doc checkpoint `.idm.json` va chi tai tiep cac byte range con thieu.

## Gioi han

Tool nay khong vuot qua quyen truy cap, DRM, quota bi khoa, hay file bi chan boi chu so huu. Cookie chi giup script co cung session dang nhap nhu trinh duyet cua ban.

Che do tai folder doc danh sach file tu du lieu Google Drive nhung trong trang folder. Google Drive thuong chi nhung toi da 50 item moi folder page; neu gap nguong nay tool se dung de tranh tai thieu file ma ban khong biet. Chi dung `--allow-partial-folder` khi ban chap nhan rui ro danh sach khong day du.

Mot so loai Google Workspace dac biet nhu Forms, Sites, Maps co the khong co endpoint export truc tiep. Tool se bao loi file do va tiep tuc cac file khac trong folder, sau do tra exit code loi neu co file that bai.
