"""
YT-DLP GUI Downloader  —  v2.0  (QA/QC clean rewrite)
=======================================================
A modern CustomTkinter GUI wrapper for yt-dlp.

Requirements:
    pip install customtkinter yt-dlp Pillow requests

FFmpeg must be installed (added to PATH, or placed next to this script).

QA fixes applied in this version:
  - Removed unused imports (json, subprocess)
  - Fixed `str | None` type hint → Optional[str]  (Python 3.9 compat)
  - Moved `import glob` to top level
  - Removed unused `pad` variable
  - Fixed return type hint on _build_format_label
  - Fixed lambda closure bug in on_status callback
  - Fixed outtmpl: use %(ext)s so temp files are named correctly
  - Removed redundant FFmpegVideoConvertor postprocessor
  - Added None guard on info dict (private/deleted video)
  - Fixed CTkLabel image=None init (use text=" " placeholder instead)
  - Fixed shutil.which called twice
  - Fixed yt-dlp option: keepvideo -> keep_video
  - Fixed postprocessor_args key format -> targets FFmpegMergerPP specifically
  - Fixed status bar layout order (pack bottom BEFORE content)
  - Added import traceback for detailed error reporting
  - Bind Enter key on URL entry to trigger fetch
"""

# == Standard library =========================================================
import glob
import os
import sys
import threading
import traceback
import tkinter as tk
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Dict, List, Optional

# == Third-party ==============================================================
import customtkinter as ctk
import shutil
import yt_dlp

# Pillow + requests are optional (thumbnail preview only)
try:
    from PIL import Image
    import requests
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# =============================================================================
#  App-wide constants
# =============================================================================
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_TITLE  = "Youtube Downloader by Haekal"
APP_WIDTH  = 880
APP_HEIGHT = 660
ACCENT     = "#3B82F6"
SUCCESS    = "#22C55E"
ERROR      = "#EF4444"
WARNING    = "#F59E0B"
BG_CARD    = "#1E1E2E"


# =============================================================================
#  FFmpeg detection
# =============================================================================
_FFMPEG_CANDIDATES: List[str] = [
    str(Path(sys.executable).parent / "ffmpeg.exe"),  # next to python.exe
    str(Path(__file__).parent       / "ffmpeg.exe"),  # next to this script (easiest)
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    str(Path.home() / "ffmpeg"           / "bin" / "ffmpeg.exe"),
    str(Path.home() / "Downloads/ffmpeg" / "bin" / "ffmpeg.exe"),
]


def _find_ffmpeg() -> Optional[str]:
    """
    Return the absolute path to ffmpeg, or None if not found.
    Search order: system PATH first, then common Windows directories.
    """
    found = shutil.which("ffmpeg")   # returns full path or None
    if found:
        return found
    for candidate in _FFMPEG_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


FFMPEG_PATH: Optional[str] = _find_ffmpeg()   # resolved once at startup


# =============================================================================
#  Format helpers
# =============================================================================
def _filesize_str(size_bytes: Optional[float]) -> str:
    """Convert bytes to a human-readable string."""
    if size_bytes is None:
        return "~"
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _build_format_label(fmt: dict) -> Optional[str]:
    """
    Return a display label for one yt-dlp format entry.
    Returns None for audio-only streams (they are skipped in the dropdown).
    """
    vcodec = fmt.get("vcodec") or "none"
    if vcodec == "none":
        return None                          # skip audio-only streams

    res     = fmt.get("resolution") or f"{fmt.get('width','?')}x{fmt.get('height','?')}"
    ext     = fmt.get("ext", "?")
    fps     = fmt.get("fps") or ""
    size    = _filesize_str(fmt.get("filesize") or fmt.get("filesize_approx"))
    fps_str = f"  {fps}fps" if fps else ""

    return f"{res}{fps_str}  |  {ext.upper()}  |  {vcodec}  |  ~{size}"


# =============================================================================
#  DownloadManager
# =============================================================================
class DownloadManager:
    """
    All yt-dlp interactions live here.
    Heavy work runs in daemon threads; results come back via callbacks
    so the Tkinter main loop never blocks.
    """

    def __init__(self) -> None:
        self._cancel_flag = False

    def cancel(self) -> None:
        """Signal the active operation to abort."""
        self._cancel_flag = True

    # -- Fetch available formats ----------------------------------------------
    def fetch_formats(
        self,
        url:        str,
        on_success: Callable,
        on_error:   Callable,
    ) -> None:
        """
        Extract format list without downloading anything.
        Calls on_success(formats, info) or on_error(msg).
        """
        def _worker() -> None:
            try:
                ydl_opts = {
                    "quiet":         True,
                    "no_warnings":   True,
                    "skip_download": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)

                # Guard: info is None for private / deleted / unavailable videos
                if not info:
                    on_error("Could not retrieve video info.\nThe video may be private or unavailable.")
                    return

                formats: List[Dict] = []
                seen: set = set()

                for fmt in info.get("formats", []):
                    label = _build_format_label(fmt)
                    if label is None:
                        continue
                    # Deduplicate by (height, fps)
                    key = (fmt.get("height"), fmt.get("fps"))
                    if key in seen:
                        continue
                    seen.add(key)
                    formats.append({
                        "label":     label,
                        "format_id": fmt["format_id"],
                        "height":    fmt.get("height") or 0,
                        "fps":       fmt.get("fps")    or 0,
                        "ext":       fmt.get("ext", ""),
                        "vcodec":    fmt.get("vcodec", ""),
                    })

                formats.sort(key=lambda f: (f["height"], f["fps"]), reverse=True)
                on_success(formats, info)

            except yt_dlp.utils.DownloadError as exc:
                on_error(f"yt-dlp error:\n{exc}")
            except Exception as exc:
                on_error(f"Unexpected error: {exc}\n\n{traceback.format_exc()}")

        self._cancel_flag = False
        threading.Thread(target=_worker, daemon=True).start()

    # -- Download -------------------------------------------------------------
    def download(
        self,
        url:         str,
        format_id:   str,
        height:      int,
        output_dir:  str,
        on_progress: Callable,
        on_status:   Callable,
        on_done:     Callable,
        on_error:    Callable,
    ) -> None:
        """
        Download chosen video stream + best audio, merge to MP4.

        Format selector tiers (first match wins):
          1. <id>+bestaudio[ext=m4a]   -- video + AAC audio  (ideal for MP4)
          2. <id>+bestaudio[ext=webm]  -- video + Opus audio
          3. <id>+bestaudio            -- video + any best audio
          4. best[height<=N][ext=mp4]  -- fallback: pre-muxed MP4
          5. best[height<=N]           -- last resort: any muxed stream

        Audio is always re-encoded to AAC 192k to ensure MP4 compatibility.
        Video is stream-copied (fast, no quality loss).
        """
        def _progress_hook(d: dict) -> None:
            if self._cancel_flag:
                raise yt_dlp.utils.DownloadError("Cancelled by user.")

            status = d.get("status")
            if status == "downloading":
                total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                speed      = d.get("speed") or 0
                eta        = d.get("eta") or 0

                pct     = int(downloaded / total * 100) if total > 0 else 0
                spd_str = f"{_filesize_str(speed)}/s" if speed else "..."
                eta_str = f"  ETA {eta}s" if eta else ""
                on_progress(pct, f"Downloading {pct}%  {spd_str}{eta_str}")

            elif status == "finished":
                on_status("Merging video + audio...", WARNING)

        def _worker() -> None:
            try:
                on_status("Starting download...", ACCENT)

                h = height or 9999
                fmt_selector = (
                    f"{format_id}+bestaudio[ext=m4a]/"
                    f"{format_id}+bestaudio[ext=webm]/"
                    f"{format_id}+bestaudio/"
                    f"best[height<={h}][ext=mp4]/"
                    f"best[height<={h}]"
                )

                # ffmpeg_location must be a DIRECTORY, not the exe itself
                ffmpeg_dir = str(Path(FFMPEG_PATH).parent) if FFMPEG_PATH else None

                # Keep %(ext)s so each temp stream gets its natural extension.
                # yt-dlp renames the merged result to .mp4 automatically via
                # merge_output_format.
                outtmpl = os.path.join(output_dir, "%(title)s.%(ext)s")

                ydl_opts: dict = {
                    "format":              fmt_selector,
                    "outtmpl":             outtmpl,
                    "merge_output_format": "mp4",   # merged file -> .mp4
                    "keep_video":          False,    # delete temp streams after merge
                    "progress_hooks":      [_progress_hook],
                    "quiet":               True,
                    "no_warnings":         True,
                    # Target the merger step: copy video, re-encode audio -> AAC
                    # "FFmpegMergerPP" is the internal key yt-dlp uses for merging
                    "postprocessor_args": {
                        "FFmpegMergerPP": [
                            "-c:v", "copy",
                            "-c:a", "aac",
                            "-b:a", "192k",
                        ]
                    },
                }

                if ffmpeg_dir:
                    ydl_opts["ffmpeg_location"] = ffmpeg_dir

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])

                # Safety sweep: remove stray .part files
                for leftover in glob.glob(os.path.join(output_dir, "*.part")):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass

                on_done()

            except yt_dlp.utils.DownloadError as exc:
                msg = str(exc)
                if "Cancelled" in msg:
                    on_status("Download cancelled.", WARNING)
                else:
                    on_error(f"Download failed:\n{msg}")
            except Exception as exc:
                on_error(f"Unexpected error: {exc}\n\n{traceback.format_exc()}")

        self._cancel_flag = False
        threading.Thread(target=_worker, daemon=True).start()


# =============================================================================
#  Main Application
# =============================================================================
class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.geometry(f"{APP_WIDTH}x{APP_HEIGHT}")
        self.resizable(True, True)
        self.minsize(700, 560)

        self._manager:    DownloadManager = DownloadManager()
        self._formats:    List[Dict]      = []
        self._info:       Dict            = {}
        self._output_dir: str             = str(Path.home() / "Downloads")
        self._thumb_ref                   = None   # holds CTkImage to prevent GC

        self._build_ui()

        # Show ffmpeg warning AFTER window is fully built
        if not FFMPEG_PATH:
            self._set_status("WARNING: FFmpeg not detected - merging will fail!", WARNING)
            messagebox.showwarning(
                "FFmpeg Not Found",
                "FFmpeg was not found on your system.\n\n"
                "Merging video + audio into MP4 will FAIL without it.\n\n"
                "Quick fix options:\n"
                "  A) Place ffmpeg.exe in the same folder as this script\n"
                "  B) Install to C:\\ffmpeg\\bin and add it to PATH\n\n"
                "Download: https://ffmpeg.org/download.html\n"
                "Restart the app after installing."
            )

    # -- UI layout ------------------------------------------------------------
    def _build_ui(self) -> None:
        # IMPORTANT: pack bottom widgets BEFORE fill/expand widgets.
        # If you pack side=bottom AFTER a fill+expand widget, it gets pushed off screen.

        # 1. Header (top)
        self._build_header()

        # 2. Status bar (bottom) -- packed BEFORE main content
        self._build_status_bar()

        # 3. Main content (fills all remaining space)
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(14, 8))

        left = ctk.CTkFrame(content, fg_color=BG_CARD, corner_radius=12)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        right = ctk.CTkFrame(content, fg_color=BG_CARD, corner_radius=12, width=240)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        self._build_thumbnail_panel(right)
        self._build_controls(left)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=54)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        ctk.CTkLabel(
            header,
            text="  > YT Downloader by Haekal",
            font=ctk.CTkFont(family="Consolas", size=18, weight="bold"),
            text_color=ACCENT,
        ).pack(side="left", padx=18)

        ffmpeg_status = (
            f"ffmpeg: {Path(FFMPEG_PATH).name}" if FFMPEG_PATH else "ffmpeg: NOT FOUND"
        )
        ctk.CTkLabel(
            header,
            text=ffmpeg_status,
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=SUCCESS if FFMPEG_PATH else ERROR,
        ).pack(side="right", padx=18)

    def _build_status_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=30)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._status_lbl = ctk.CTkLabel(
            bar, text="  Ready.",
            font=ctk.CTkFont(size=11),
            text_color="#8888AA",
            anchor="w",
        )
        self._status_lbl.pack(fill="x", padx=8)

    def _build_thumbnail_panel(self, parent: ctk.CTkFrame) -> None:
        ctk.CTkLabel(
            parent, text="Preview",
            font=ctk.CTkFont(size=11), text_color="#555577",
        ).pack(pady=(14, 4))

        # Do NOT pass image=None — use empty text instead
        self._thumb_lbl = ctk.CTkLabel(parent, text="")
        self._thumb_lbl.pack(padx=10, pady=4)

        self._title_lbl = ctk.CTkLabel(
            parent, text="",
            font=ctk.CTkFont(size=11), text_color="#CCCCDD",
            wraplength=210, justify="center",
        )
        self._title_lbl.pack(padx=10, pady=(4, 6))

        self._dur_lbl = ctk.CTkLabel(
            parent, text="",
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color="#8888AA",
        )
        self._dur_lbl.pack()

    def _build_controls(self, parent: ctk.CTkFrame) -> None:
        # URL row
        ctk.CTkLabel(
            parent, text="YouTube URL",
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#AAAACC",
        ).pack(anchor="w", padx=18, pady=(16, 2))

        url_row = ctk.CTkFrame(parent, fg_color="transparent")
        url_row.pack(fill="x", padx=18, pady=(0, 10))

        self._url_entry = ctk.CTkEntry(
            url_row,
            placeholder_text="https://www.youtube.com/watch?v=...",
            font=ctk.CTkFont(family="Consolas", size=13),
            height=38,
        )
        self._url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._url_entry.bind("<Return>", lambda _e: self._on_fetch())  # Enter key support

        self._fetch_btn = ctk.CTkButton(
            url_row, text="Fetch Formats", width=130, height=38,
            fg_color=ACCENT, hover_color="#2563EB",
            font=ctk.CTkFont(weight="bold"),
            command=self._on_fetch,
        )
        self._fetch_btn.pack(side="right")

        # Quality dropdown
        ctk.CTkLabel(
            parent, text="Quality / Format",
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#AAAACC",
        ).pack(anchor="w", padx=18, pady=(4, 2))

        self._format_var = tk.StringVar(value="-- press Fetch Formats first --")
        self._format_menu = ctk.CTkOptionMenu(
            parent,
            variable=self._format_var,
            values=["-- press Fetch Formats first --"],
            font=ctk.CTkFont(family="Consolas", size=12),
            height=36,
            state="disabled",
            fg_color="#252535",
            button_color=ACCENT,
            button_hover_color="#2563EB",
        )
        self._format_menu.pack(fill="x", padx=18, pady=(0, 10))

        # Output folder
        ctk.CTkLabel(
            parent, text="Save To",
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#AAAACC",
        ).pack(anchor="w", padx=18, pady=(4, 2))

        folder_row = ctk.CTkFrame(parent, fg_color="transparent")
        folder_row.pack(fill="x", padx=18, pady=(0, 16))

        self._folder_lbl = ctk.CTkLabel(
            folder_row, text=self._output_dir,
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color="#8888AA", anchor="w",
        )
        self._folder_lbl.pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            folder_row, text="Browse", width=80, height=30,
            fg_color="#333355", hover_color="#444466",
            command=self._on_browse,
        ).pack(side="right")

        # Progress
        ctk.CTkLabel(
            parent, text="Progress",
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#AAAACC",
        ).pack(anchor="w", padx=18, pady=(0, 2))

        self._progress_bar = ctk.CTkProgressBar(
            parent, height=14, corner_radius=7, progress_color=ACCENT,
        )
        self._progress_bar.pack(fill="x", padx=18, pady=(0, 4))
        self._progress_bar.set(0)

        self._progress_lbl = ctk.CTkLabel(
            parent, text="",
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color="#8888AA",
        )
        self._progress_lbl.pack(anchor="w", padx=18)

        # Buttons
        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=18, pady=(18, 10))

        self._dl_btn = ctk.CTkButton(
            btn_row, text="Download", height=42,
            fg_color=SUCCESS, hover_color="#16A34A",
            font=ctk.CTkFont(size=14, weight="bold"),
            state="disabled",
            command=self._on_download,
        )
        self._dl_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self._cancel_btn = ctk.CTkButton(
            btn_row, text="Cancel", height=42, width=100,
            fg_color="#444444", hover_color="#555555",
            font=ctk.CTkFont(size=13),
            state="disabled",
            command=self._on_cancel,
        )
        self._cancel_btn.pack(side="right")

    # -- Event handlers -------------------------------------------------------
    def _on_browse(self) -> None:
        folder = filedialog.askdirectory(
            initialdir=self._output_dir, title="Choose download folder"
        )
        if folder:
            self._output_dir = folder
            self._folder_lbl.configure(text=folder)

    def _on_fetch(self) -> None:
        url = self._url_entry.get().strip()
        if not url:
            self._set_status("Please paste a YouTube URL.", ERROR)
            return

        self._set_status("Fetching formats...", ACCENT)
        self._fetch_btn.configure(state="disabled")
        self._dl_btn.configure(state="disabled")
        self._format_menu.configure(state="disabled", values=["Fetching..."])
        self._format_var.set("Fetching...")
        self._progress_bar.set(0)
        self._progress_lbl.configure(text="")
        self._formats = []

        self._manager.fetch_formats(
            url,
            on_success=self._cb_formats_ready,
            on_error=self._cb_fetch_error,
        )

    def _on_download(self) -> None:
        url = self._url_entry.get().strip()
        if not url or not self._formats:
            return

        selected = self._format_var.get()
        fmt = next((f for f in self._formats if f["label"] == selected), None)
        if fmt is None:
            self._set_status("Please select a valid format.", ERROR)
            return

        self._dl_btn.configure(state="disabled")
        self._fetch_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._progress_bar.set(0)
        self._progress_lbl.configure(text="")

        # Use default arg to capture color value at call time (fixes closure bug)
        def _status_cb(msg: str, color: str = ACCENT) -> None:
            self.after(0, lambda m=msg, c=color: self._set_status(m, c))

        self._manager.download(
            url         = url,
            format_id   = fmt["format_id"],
            height      = fmt["height"],
            output_dir  = self._output_dir,
            on_progress = self._cb_progress,
            on_status   = _status_cb,
            on_done     = self._cb_done,
            on_error    = self._cb_error,
        )

    def _on_cancel(self) -> None:
        self._manager.cancel()
        self._cancel_btn.configure(state="disabled")
        self._set_status("Cancelling...", WARNING)

    # -- Callbacks (called from worker threads) --------------------------------
    def _cb_formats_ready(self, formats: List[Dict], info: Dict) -> None:
        self.after(0, lambda: self._apply_formats(formats, info))

    def _apply_formats(self, formats: List[Dict], info: Dict) -> None:
        self._formats = formats
        self._info    = info
        self._fetch_btn.configure(state="normal")

        if not formats:
            self._set_status("No downloadable video formats found.", ERROR)
            self._format_menu.configure(values=["-- none found --"])
            self._format_var.set("-- none found --")
            return

        labels = [f["label"] for f in formats]
        self._format_menu.configure(state="normal", values=labels)
        self._format_var.set(labels[0])
        self._dl_btn.configure(state="normal")
        self._set_status(
            f"Found {len(formats)} format(s). Select quality and click Download.", SUCCESS
        )

        self._title_lbl.configure(text=info.get("title", ""))
        duration = info.get("duration")
        if duration:
            m, s = divmod(int(duration), 60)
            h, m = divmod(m, 60)
            self._dur_lbl.configure(
                text=f"Duration: {h}:{m:02d}:{s:02d}" if h else f"Duration: {m}:{s:02d}"
            )

        if PIL_AVAILABLE:
            thumb_url = info.get("thumbnail") or ""
            if thumb_url:
                threading.Thread(
                    target=self._load_thumbnail, args=(thumb_url,), daemon=True
                ).start()

    def _cb_fetch_error(self, msg: str) -> None:
        self.after(0, lambda m=msg: self._handle_fetch_error(m))

    def _handle_fetch_error(self, msg: str) -> None:
        self._set_status(msg, ERROR)
        self._fetch_btn.configure(state="normal")
        self._format_menu.configure(values=["-- error --"])
        self._format_var.set("-- error --")

    def _cb_progress(self, pct: int, label: str) -> None:
        # Use default args to capture values at call time (avoids closure bug)
        self.after(0, lambda p=pct, l=label: self._update_progress(p, l))

    def _update_progress(self, pct: int, label: str) -> None:
        self._progress_bar.set(pct / 100)
        self._progress_lbl.configure(text=label)

    def _cb_done(self) -> None:
        self.after(0, self._handle_done)

    def _handle_done(self) -> None:
        self._progress_bar.set(1)
        self._progress_lbl.configure(text="Complete!")
        self._set_status(f"Saved to: {self._output_dir}", SUCCESS)
        self._dl_btn.configure(state="normal")
        self._fetch_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")

    def _cb_error(self, msg: str) -> None:
        self.after(0, lambda m=msg: self._handle_error(m))

    def _handle_error(self, msg: str) -> None:
        self._set_status("Download failed. See error dialog.", ERROR)
        self._dl_btn.configure(state="normal")
        self._fetch_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        messagebox.showerror("Download Error", msg)

    def _load_thumbnail(self, url: str) -> None:
        """Fetch and display the video thumbnail (runs in background thread)."""
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            img     = Image.open(BytesIO(resp.content))
            img.thumbnail((220, 130), Image.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            self._thumb_ref = ctk_img    # hold reference so GC doesn't collect it
            self.after(0, lambda i=ctk_img: self._thumb_lbl.configure(image=i, text=""))
        except Exception:
            pass   # thumbnail is decorative; never crash the app for this

    # -- Helper ---------------------------------------------------------------
    def _set_status(self, msg: str, color: str = "#8888AA") -> None:
        self._status_lbl.configure(text=f"  {msg}", text_color=color)


# =============================================================================
#  Entry point
# =============================================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()
