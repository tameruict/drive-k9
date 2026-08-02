#!/usr/bin/env python3
"""Pink glassmorphism desktop UI for gdrive_stream_downloader.py."""

from __future__ import annotations

import os
import json
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def get_app_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

APP_DIR = get_app_dir()
DOWNLOADER = APP_DIR / "gdrive_stream_downloader.py"
# If frozen, downloader logic is bundled, so DOWNLOADER path may be different
# For the UI settings:
CONFIG_PATH = APP_DIR / ".gdrive_downloader_ui_settings.json"
PERCENT_RE = re.compile(r"\(\s*([0-9]+(?:\.[0-9]+)?)%\)")


COLORS = {
    "bg_top": "#130914",
    "bg_bottom": "#2b1123",
    "panel": "#241522",
    "panel_2": "#2f1a2c",
    "panel_3": "#1a1119",
    "border": "#ff8fcb",
    "border_dim": "#76415f",
    "text": "#ffeaf5",
    "muted": "#b891a8",
    "accent": "#ff4fa3",
    "accent_2": "#ff9ed2",
    "accent_dark": "#b91f6b",
    "field": "#1b1119",
    "field_focus": "#2a1725",
    "error": "#ff6d8f",
    "ok": "#ffd0e7",
}


class PinkDriveUI:
    def __init__(self, root: tk.Tk, expiry_date: str = "") -> None:
        self.root = root
        self.expiry_date = expiry_date
        title = "GDrive Downloader - Pink Glass"
        if expiry_date:
            title += f" [Hạn dùng: {expiry_date}]"
        self.root.title(title)
        self.root.geometry("860x860")
        self.root.minsize(780, 760)
        self.root.configure(bg=COLORS["bg_bottom"])
        try:
            self.root.attributes("-alpha", 0.98)
        except tk.TclError:
            pass

        self.process: Optional[subprocess.Popen[str]] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.settings_save_after_id: Optional[str] = None
        self.settings = self._load_settings()

        self.link_var = tk.StringVar()
        self.cookie_var = tk.StringVar(
            value=self._normalized_path_setting(self._setting("cookie_file", ""))
        )
        self.output_dir_var = tk.StringVar(
            value=self._normalized_path_setting(
                self._setting("output_dir", str(Path.home() / "Downloads")),
                str(Path.home() / "Downloads"),
            )
        )
        self.file_name_var = tk.StringVar()
        self.mode_var = tk.StringVar(value=self._choice_setting("mode", "auto", ["auto", "file", "folder"]))
        self.chunk_var = tk.StringVar(value=self._choice_setting("chunk_size", "4M", ["1M", "4M", "8M", "16M", "32M"]))
        self.segments_var = tk.StringVar(value=self._integer_setting("segments", "4"))
        self.workspace_var = tk.StringVar(value=self._choice_setting("workspace_format", "office", ["office", "pdf"]))
        self.resume_var = tk.BooleanVar(value=self._bool_setting("resume", True))
        self.allow_partial_var = tk.BooleanVar(value=self._bool_setting("allow_partial_folder", False))

        self._build_styles()
        self._build_layout()
        self._bind_settings_autosave()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_settings(self) -> dict[str, object]:
        try:
            if not CONFIG_PATH.exists():
                return {}
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _setting(self, key: str, default: str) -> str:
        value = self.settings.get(key, default)
        return value if isinstance(value, str) else default

    def _choice_setting(self, key: str, default: str, choices: list[str]) -> str:
        value = self._setting(key, default)
        return value if value in choices else default

    def _integer_setting(self, key: str, default: str) -> str:
        value = self._setting(key, default).strip()
        if not value.isdigit():
            return default
        return value if int(value) >= 1 else default

    def _bool_setting(self, key: str, default: bool) -> bool:
        value = self.settings.get(key, default)
        return value if isinstance(value, bool) else default

    def _normalized_path_setting(self, value: str, default: str = "") -> str:
        value = value.strip()
        if not value:
            value = default
        if not value:
            return ""

        path = Path(value).expanduser()
        if not path.is_absolute():
            path = APP_DIR / path
        try:
            return str(path.resolve(strict=False))
        except OSError:
            return str(path)

    def _normalized_segments_setting(self) -> str:
        value = self.segments_var.get().strip()
        return value if value.isdigit() and int(value) >= 1 else "4"

    def _save_settings(self) -> None:
        output_dir = self._normalized_path_setting(
            self.output_dir_var.get(),
            str(Path.home() / "Downloads"),
        )
        cookie_file = self._normalized_path_setting(self.cookie_var.get())
        data = {
            "cookie_file": cookie_file,
            "output_dir": output_dir,
            "mode": self.mode_var.get(),
            "chunk_size": self.chunk_var.get(),
            "segments": self._normalized_segments_setting(),
            "workspace_format": self.workspace_var.get(),
            "resume": bool(self.resume_var.get()),
            "allow_partial_folder": bool(self.allow_partial_var.get()),
        }
        try:
            temp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(CONFIG_PATH)
        except OSError:
            return

    def _queue_save_settings(self, *_: object) -> None:
        if self.settings_save_after_id is not None:
            self.root.after_cancel(self.settings_save_after_id)
        self.settings_save_after_id = self.root.after(500, self._autosave_settings)

    def _autosave_settings(self) -> None:
        self.settings_save_after_id = None
        self._save_settings()

    def _bind_settings_autosave(self) -> None:
        for variable in (
            self.cookie_var,
            self.output_dir_var,
            self.mode_var,
            self.chunk_var,
            self.segments_var,
            self.workspace_var,
            self.resume_var,
            self.allow_partial_var,
        ):
            variable.trace_add("write", self._queue_save_settings)

    def _build_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Pink.Horizontal.TProgressbar",
            troughcolor="#3b2534",
            background=COLORS["accent"],
            bordercolor=COLORS["border_dim"],
            lightcolor=COLORS["accent_2"],
            darkcolor=COLORS["accent_dark"],
            thickness=18,
        )
        style.configure(
            "Pink.TCheckbutton",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=("Consolas", 10),
            focuscolor=COLORS["panel"],
        )
        style.map(
            "Pink.TCheckbutton",
            background=[("active", COLORS["panel"])],
            foreground=[("active", COLORS["accent_2"])],
        )

    def _build_layout(self) -> None:
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._redraw_background)

        self.content = tk.Frame(self.canvas, bg=COLORS["panel"])
        self.window_id = self.canvas.create_window(
            32,
            24,
            anchor="nw",
            window=self.content,
            width=796,
            height=812,
        )

        self.root.bind("<Configure>", self._resize_content)

        self._build_header()
        self._build_form()
        self._build_progress()
        self._build_log()

    def _resize_content(self, event: tk.Event) -> None:
        if event.widget is self.root:
            width = max(event.width - 64, 720)
            height = max(event.height - 48, 700)
            self.canvas.itemconfigure(self.window_id, width=width, height=height)

    def _redraw_background(self, event: tk.Event) -> None:
        width = event.width
        height = event.height
        self.canvas.delete("bg")
        self._draw_vertical_gradient(width, height, COLORS["bg_top"], COLORS["bg_bottom"])
        self._draw_round_rect(28, 20, width - 28, height - 24, 8, COLORS["panel"], COLORS["border_dim"])
        self.canvas.create_line(42, 36, width - 42, 36, fill="#5c2c4a", tags="bg")
        self.canvas.create_line(42, height - 40, width - 42, height - 40, fill="#3f2035", tags="bg")
        self.canvas.tag_lower("bg")

    def _draw_vertical_gradient(self, width: int, height: int, top: str, bottom: str) -> None:
        top_rgb = self.root.winfo_rgb(top)
        bottom_rgb = self.root.winfo_rgb(bottom)
        steps = max(height, 1)
        for y in range(steps):
            ratio = y / steps
            rgb = tuple(
                int((top_rgb[i] * (1 - ratio) + bottom_rgb[i] * ratio) / 256)
                for i in range(3)
            )
            color = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
            self.canvas.create_line(0, y, width, y, fill=color, tags="bg")

    def _draw_round_rect(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int,
        fill: str,
        outline: str,
    ) -> None:
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        self.canvas.create_polygon(
            points,
            smooth=True,
            fill=fill,
            outline=outline,
            width=1,
            tags="bg",
        )

    def _build_header(self) -> None:
        header = tk.Frame(self.content, bg=COLORS["panel"], padx=20, pady=10)
        header.pack(fill="x")

        title_row = tk.Frame(header, bg=COLORS["panel"])
        title_row.pack(fill="x")

        tk.Label(
            title_row,
            text="TOOL TẢI VIDEO GG DRIVE BLOCK",
            bg=COLORS["panel"],
            fg=COLORS["accent_2"],
            font=("Consolas", 18, "bold"),
        ).pack(side="left")
        
        tk.Label(
            title_row,
            text="PINK GLASS UI",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 9, "bold"),
        ).pack(side="right", pady=(4, 0))

        info_row = tk.Frame(header, bg=COLORS["panel"])
        info_row.pack(fill="x", pady=(8, 0))
        
        tk.Label(
            info_row,
            text="Website: dautruonghoctap.io.vn",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Consolas", 10),
        ).pack(side="left")
        
        tk.Label(
            info_row,
            text=" | ",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 10),
        ).pack(side="left")
        
        tk.Label(
            info_row,
            text="Telegram: @nhitool",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Consolas", 10),
        ).pack(side="left")
        
        if self.expiry_date:
            tk.Label(
                info_row,
                text=" | ",
                bg=COLORS["panel"],
                fg=COLORS["muted"],
                font=("Consolas", 10),
            ).pack(side="left")
            
            tk.Label(
                info_row,
                text=f"Hạn dùng: {self.expiry_date}",
                bg=COLORS["panel"],
                fg=COLORS["ok"],
                font=("Consolas", 10, "bold"),
            ).pack(side="left")

        self._divider(header, pady=(15, 0))

    def _build_form(self) -> None:
        form = tk.Frame(self.content, bg=COLORS["panel"], padx=20)
        form.pack(fill="x")

        self._field(
            form,
            label="GOOGLE DRIVE LINK / ID",
            variable=self.link_var,
            clear=True,
        )

        self._file_field(
            form,
            label="FILE COOKIES.TXT (không bắt buộc)",
            variable=self.cookie_var,
            button_text="Chọn file",
            command=self._choose_cookie,
        )

        self._file_field(
            form,
            label="THƯ MỤC LƯU",
            variable=self.output_dir_var,
            button_text="Chọn thư mục",
            command=self._choose_output_dir,
        )

        self._field(
            form,
            label="TÊN FILE (để trống = tự động, chỉ áp dụng khi tải 1 file)",
            variable=self.file_name_var,
            clear=False,
        )

        options = tk.Frame(form, bg=COLORS["panel"])
        options.pack(fill="x", pady=(8, 6))

        self._select_box(options, "MODE", self.mode_var, ["auto", "file", "folder"], width=7)
        self._select_box(options, "CHUNK", self.chunk_var, ["1M", "4M", "8M", "16M", "32M"], width=7)
        self._number_box(options, "SEGMENTS", self.segments_var, width=7)
        self._select_box(options, "EXPORT", self.workspace_var, ["office", "pdf"], width=7)

        checks = tk.Frame(options, bg=COLORS["panel"])
        checks.pack(side="left", padx=(8, 0))

        ttk.Checkbutton(
            checks,
            text="Resume",
            variable=self.resume_var,
            style="Pink.TCheckbutton",
        ).pack(side="left")
        ttk.Checkbutton(
            checks,
            text="Allow partial folder",
            variable=self.allow_partial_var,
            style="Pink.TCheckbutton",
        ).pack(side="left", padx=(16, 0))

    def _build_progress(self) -> None:
        progress_area = tk.Frame(self.content, bg=COLORS["panel"], padx=20)
        progress_area.pack(fill="x")

        self.status_var = tk.StringVar(value="Sẵn sàng")
        tk.Label(
            progress_area,
            textvariable=self.status_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 9, "bold"),
        ).pack(anchor="w", pady=(2, 0))

        self.progress = ttk.Progressbar(
            progress_area,
            style="Pink.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.progress.pack(fill="x", pady=(5, 8))

        button_row = tk.Frame(progress_area, bg=COLORS["panel"])
        button_row.pack(fill="x", pady=(0, 10))

        self.download_button = self._button(
            button_row,
            "TẢI XUỐNG",
            self._start_download,
            primary=True,
        )
        self.download_button.pack(side="left", fill="x", expand=True)

        self.stop_button = self._button(button_row, "DỪNG", self._stop_download, primary=False)
        self.stop_button.pack(side="left", padx=(12, 0))
        self.stop_button.configure(state="disabled")

        self._divider(progress_area, pady=(8, 0))

    def _build_log(self) -> None:
        log_area = tk.Frame(self.content, bg=COLORS["panel"], padx=20, pady=6)
        log_area.pack(fill="both", expand=True)

        tk.Label(
            log_area,
            text="LOG",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 9, "bold"),
        ).pack(anchor="w", pady=(0, 5))

        log_frame = tk.Frame(log_area, bg=COLORS["panel_3"], highlightthickness=1, highlightbackground=COLORS["border_dim"])
        log_frame.pack(fill="both", expand=True)

        self.log = tk.Text(
            log_frame,
            bg=COLORS["panel_3"],
            fg=COLORS["text"],
            insertbackground=COLORS["accent_2"],
            relief="flat",
            bd=0,
            padx=12,
            pady=8,
            wrap="word",
            font=("Consolas", 10),
        )
        scrollbar = tk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _field(
        self,
        parent: tk.Frame,
        label: str,
        variable: tk.StringVar,
        clear: bool = False,
    ) -> None:
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(fill="x", pady=(5, 0))
        self._field_label(row, label).pack(side="left")
        entry = self._entry(row, variable)
        entry.pack(side="left", fill="x", expand=True)
        if clear:
            self._button(row, "X", lambda: variable.set(""), primary=False, compact=True).pack(
                side="left",
                padx=(8, 0),
            )

    def _file_field(
        self,
        parent: tk.Frame,
        label: str,
        variable: tk.StringVar,
        button_text: str,
        command: Callable[[], None],
    ) -> None:
        row = tk.Frame(parent, bg=COLORS["panel"])
        row.pack(fill="x", pady=(5, 0))
        self._field_label(row, label).pack(side="left")
        self._entry(row, variable).pack(side="left", fill="x", expand=True)
        self._button(row, button_text, command, primary=False, compact=True).pack(side="left", padx=(8, 0))

    def _field_label(self, parent: tk.Frame, text: str) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 9, "bold"),
            anchor="w",
            width=54,
        )

    def _entry(self, parent: tk.Frame, variable: tk.StringVar) -> tk.Entry:
        entry = tk.Entry(
            parent,
            textvariable=variable,
            bg=COLORS["field"],
            fg=COLORS["text"],
            insertbackground=COLORS["accent_2"],
            relief="flat",
            highlightthickness=1,
            highlightbackground="#3e2638",
            highlightcolor=COLORS["border"],
            font=("Consolas", 10),
        )
        entry.configure(disabledbackground=COLORS["field"], disabledforeground=COLORS["muted"])
        return entry

    def _select_box(
        self,
        parent: tk.Frame,
        label: str,
        variable: tk.StringVar,
        values: list[str],
        width: int,
    ) -> None:
        group = tk.Frame(parent, bg=COLORS["panel"])
        group.pack(side="left", padx=(0, 12))

        tk.Label(
            group,
            text=f"{label}:",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 9, "bold"),
        ).pack(side="left", padx=(0, 6))

        box = tk.OptionMenu(group, variable, *values)
        box.configure(
            width=width,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            activebackground=COLORS["accent_dark"],
            activeforeground=COLORS["text"],
            highlightthickness=1,
            highlightbackground="#4c2d42",
            relief="flat",
            font=("Consolas", 9, "bold"),
        )
        menu = box.nametowidget(box.menuname)
        menu.configure(
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            activebackground=COLORS["accent"],
            activeforeground="#120910",
            relief="flat",
            font=("Consolas", 9),
        )
        box.pack(side="left")

    def _number_box(
        self,
        parent: tk.Frame,
        label: str,
        variable: tk.StringVar,
        width: int,
    ) -> None:
        group = tk.Frame(parent, bg=COLORS["panel"])
        group.pack(side="left", padx=(0, 12))

        tk.Label(
            group,
            text=f"{label}:",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Consolas", 9, "bold"),
        ).pack(side="left", padx=(0, 6))

        box = tk.Spinbox(
            group,
            from_=1,
            to=64,
            increment=1,
            textvariable=variable,
            width=width,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            buttonbackground=COLORS["panel_2"],
            insertbackground=COLORS["accent_2"],
            highlightthickness=1,
            highlightbackground="#4c2d42",
            highlightcolor=COLORS["border"],
            relief="flat",
            font=("Consolas", 9, "bold"),
        )
        box.pack(side="left")

    def _button(
        self,
        parent: tk.Frame,
        text: str,
        command: Callable[[], None],
        primary: bool,
        compact: bool = False,
    ) -> tk.Button:
        bg = COLORS["accent"] if primary else COLORS["panel_2"]
        active = COLORS["accent_2"] if primary else "#46263d"
        fg = "#180b14" if primary else COLORS["text"]
        kwargs = {
            "text": text,
            "command": command,
            "bg": bg,
            "fg": fg,
            "activebackground": active,
            "activeforeground": "#180b14" if primary else COLORS["text"],
            "relief": "flat",
            "bd": 0,
            "padx": 14 if not compact else 9,
            "pady": 8 if primary else 6,
            "font": ("Consolas", 11 if primary else 9, "bold"),
            "cursor": "hand2",
        }
        if compact:
            kwargs["width"] = 4 if len(text) <= 2 else 12
        return tk.Button(parent, **kwargs)

    def _divider(self, parent: tk.Frame, pady: tuple[int, int] = (18, 0)) -> None:
        tk.Frame(parent, height=1, bg="#4a2a41").pack(fill="x", pady=pady)

    def _choose_cookie(self) -> None:
        initial_dir = self._initial_cookie_dir()
        path = filedialog.askopenfilename(
            title="Chọn cookies.txt",
            initialdir=str(initial_dir) if initial_dir else None,
            filetypes=[
                ("Cookie files", "*.txt *.cookies"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.cookie_var.set(path)
            self._save_settings()

    def _choose_output_dir(self) -> None:
        initial_dir = self._initial_output_dir()
        path = filedialog.askdirectory(
            title="Chọn thư mục lưu",
            initialdir=str(initial_dir) if initial_dir else None,
        )
        if path:
            self.output_dir_var.set(path)
            self._save_settings()

    def _initial_cookie_dir(self) -> Optional[Path]:
        cookie_file = self.cookie_var.get().strip()
        if cookie_file:
            cookie_path = Path(cookie_file).expanduser()
            parent = cookie_path.parent
            if parent.exists():
                return parent
        return self._initial_output_dir()

    def _initial_output_dir(self) -> Optional[Path]:
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            return None
        path = Path(output_dir).expanduser()
        if path.exists() and path.is_dir():
            return path
        parent = path.parent
        return parent if parent.exists() else None

    def _start_download(self) -> None:
        if self.process is not None:
            return
        self._save_settings()

        try:
            command, cwd = self._build_command()
        except ValueError as exc:
            messagebox.showerror("Thiếu thông tin", str(exc))
            return

        self.log.delete("1.0", "end")
        self.progress.configure(value=0)
        self.status_var.set("Đang tải")
        self._append_log("> " + " ".join(command) + "\n\n")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self.process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=env,
        )
        self.download_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.reader_thread = threading.Thread(target=self._read_process_output, daemon=True)
        self.reader_thread.start()

    def _build_command(self) -> tuple[list[str], Path]:
        source = self.link_var.get().strip()
        if not source:
            raise ValueError("Nhập Google Drive link hoặc ID trước khi tải.")
        if not getattr(sys, 'frozen', False) and not DOWNLOADER.exists():
            raise ValueError(f"Không tìm thấy downloader: {DOWNLOADER}")

        output_dir = Path(
            self._normalized_path_setting(
                self.output_dir_var.get(),
                str(Path.home() / "Downloads"),
            )
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir_var.set(str(output_dir))

        if getattr(sys, 'frozen', False):
            command = [sys.executable, "--run-downloader"]
        else:
            command = [sys.executable, "-X", "utf8", str(DOWNLOADER)]

        mode = self.mode_var.get()
        if self._is_folder_source(source, mode):
            if source.startswith(("http://", "https://")):
                command += ["--folder-url", source]
            else:
                command += ["--folder-id", source]
            command += ["-o", str(output_dir)]
        else:
            if source.startswith(("http://", "https://")):
                command += ["--url", source]
            else:
                command += ["--file-id", source]

            file_name = self.file_name_var.get().strip()
            if file_name:
                command += ["-o", str(output_dir / file_name)]

        cookie_file = self.cookie_var.get().strip()
        if cookie_file:
            cookie_path = Path(self._normalized_path_setting(cookie_file))
            self.cookie_var.set(str(cookie_path))
            command += ["--cookie-file", str(cookie_path)]
            command += ["--cookie-refresh-timeout", "900"]

        segments = self.segments_var.get().strip()
        if not segments.isdigit() or int(segments) < 1:
            raise ValueError("SEGMENTS phai la so nguyen lon hon hoac bang 1.")

        command += ["--chunk-size", self.chunk_var.get()]
        command += ["--segments", segments]
        command += ["--workspace-format", self.workspace_var.get()]

        if self.resume_var.get():
            command.append("--resume")
        if self.allow_partial_var.get():
            command.append("--allow-partial-folder")

        self._save_settings()
        return command, output_dir

    def _is_folder_source(self, source: str, mode: str) -> bool:
        if mode == "folder":
            return True
        if mode == "file":
            return False
        return "/folders/" in source

    def _read_process_output(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        buffer = ""
        while True:
            char = self.process.stdout.read(1)
            if char == "" and self.process.poll() is not None:
                break
            if not char:
                continue
            if char == "\r":
                if buffer:
                    self.events.put(("progress", buffer))
                    buffer = ""
            elif char == "\n":
                self.events.put(("line", buffer + "\n"))
                buffer = ""
            else:
                buffer += char

        if buffer:
            self.events.put(("line", buffer + "\n"))
        return_code = self.process.wait()
        self.events.put(("done", str(return_code)))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._handle_progress(payload)
                elif kind == "line":
                    self._append_log(payload)
                    self._handle_progress(payload)
                elif kind == "done":
                    self._finish_process(int(payload))
        except queue.Empty:
            pass

        self.root.after(80, self._poll_events)

    def _handle_progress(self, text: str) -> None:
        match = PERCENT_RE.search(text)
        if match:
            value = float(match.group(1))
            self.progress.configure(value=max(0.0, min(100.0, value)))
            self.status_var.set(text.strip())
        elif text.strip():
            self.status_var.set(text.strip()[:120])

    def _finish_process(self, return_code: int) -> None:
        self.process = None
        self.download_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

        if return_code == 0:
            self.progress.configure(value=100)
            self.status_var.set("Hoàn tất")
            self._append_log("\nHoàn tất.\n")
        elif return_code == -15:
            self.status_var.set("Đã dừng")
            self._append_log("\nĐã dừng.\n")
        else:
            self.status_var.set("Có lỗi, xem log")
            self._append_log(f"\nThoát với mã lỗi {return_code}.\n")

    def _stop_download(self) -> None:
        if self.process is None:
            return
        self.status_var.set("Đang dừng")
        self._append_log("\nĐang dừng tiến trình tải...\n")
        self.process.terminate()

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")

    def _on_close(self) -> None:
        if self.settings_save_after_id is not None:
            self.root.after_cancel(self.settings_save_after_id)
            self.settings_save_after_id = None
        self._save_settings()
        if self.process is not None and self.process.poll() is None:
            if messagebox.askyesno(
                "Đang tải",
                "Tiến trình tải vẫn đang chạy. Bạn có muốn dừng và thoát không?",
            ):
                self.process.terminate()
            else:
                return
        self.root.destroy()


def main() -> int:
    import license_manager
    
    root = tk.Tk()
    root.withdraw() # Hide main window temporarily
    
    saved_key = license_manager.load_saved_license()
    valid, msg = False, ""
    if saved_key:
        valid, msg = license_manager.verify_license(saved_key)
        
    if not valid:
        hwid = license_manager.get_hwid()
        
        dialog = tk.Toplevel(root)
        dialog.title("Bản quyền phần mềm")
        dialog.geometry("480x320")
        dialog.configure(bg=COLORS["bg_bottom"])
        dialog.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))
        
        tk.Label(dialog, text="CHƯA KÍCH HOẠT", bg=COLORS["bg_bottom"], fg=COLORS["error"], font=("Consolas", 14, "bold")).pack(pady=(20, 10))
        tk.Label(dialog, text="Hardware ID (HWID) của bạn là:", bg=COLORS["bg_bottom"], fg=COLORS["text"], font=("Consolas", 10)).pack()
        
        hwid_frame = tk.Frame(dialog, bg=COLORS["bg_bottom"])
        hwid_frame.pack(pady=5)
        
        hwid_entry = tk.Entry(hwid_frame, width=35, font=("Consolas", 11, "bold"), fg=COLORS["accent"], bg=COLORS["panel"], relief="flat", justify="center")
        hwid_entry.insert(0, hwid)
        hwid_entry.configure(state="readonly")
        hwid_entry.pack(side="left", padx=5)
        
        def copy_hwid():
            dialog.clipboard_clear()
            dialog.clipboard_append(hwid)
            messagebox.showinfo("Copy", "Đã copy HWID!\nHãy gửi cho Admin để nhận key.", parent=dialog)
            
        tk.Button(hwid_frame, text="Copy", command=copy_hwid, bg=COLORS["panel_2"], fg=COLORS["text"], relief="flat", font=("Consolas", 9)).pack(side="left")
        
        tk.Label(dialog, text="Nhập License Key:", bg=COLORS["bg_bottom"], fg=COLORS["text"], font=("Consolas", 10)).pack(pady=(15, 5))
        
        key_var = tk.StringVar()
        tk.Entry(dialog, textvariable=key_var, width=50, font=("Consolas", 10), bg=COLORS["field"], fg=COLORS["text"], insertbackground=COLORS["accent_2"], relief="flat").pack()
        
        def activate():
            k = key_var.get().strip()
            if not k:
                return
            is_valid, out_msg = license_manager.verify_license(k)
            if is_valid:
                license_manager.save_license(k)
                messagebox.showinfo("Thành công", f"Kích hoạt thành công!\nHạn dùng: {out_msg}", parent=dialog)
                dialog.destroy()
                root.expiry_date = out_msg
            else:
                messagebox.showerror("Lỗi", out_msg, parent=dialog)
                
        tk.Button(dialog, text="KÍCH HOẠT", command=activate, bg=COLORS["accent"], fg="#180b14", font=("Consolas", 11, "bold"), relief="flat", padx=20, pady=5).pack(pady=20)
        
        # Center the dialog
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - dialog.winfo_reqwidth()) // 2
        y = (dialog.winfo_screenheight() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{x}+{y}")
        
        dialog.wait_window()
    else:
        root.expiry_date = msg
        
    if not hasattr(root, "expiry_date"):
        sys.exit(0)
        
    root.deiconify()
    PinkDriveUI(root, root.expiry_date)
    root.mainloop()
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--run-downloader":
        # Remove the flag so argparse in the downloader doesn't see it
        sys.argv.pop(1)
        import gdrive_stream_downloader
        try:
            sys.exit(gdrive_stream_downloader.main())
        except AttributeError:
            # If main() is not defined, we can just run the script
            # But gdrive_stream_downloader probably has its own __name__ == "__main__" block.
            # We can run it via runpy if needed, or rely on it having a main() function.
            pass
            
    raise SystemExit(main())
