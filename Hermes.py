"""
Hermes Rate Limit Tester
========================

GUI and headless utility for exercising API rate limits. This implementation
is based on an earlier internal tool and has been adapted for the Hermes
project with a reusable run configuration, optional preset handling, and the
ability to export logs and summaries.

Usage
-----
GUI:
    python Hermes.py

Headless example:
    python Hermes.py --url https://example.com/health \\
        --rps 10 --duration 60 --timeout 5 --headers headers.txt --log-file out.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter import ttk

try:
    import aiohttp
except ImportError as exc:  # noqa: F401
    raise SystemExit(
        "필수 라이브러리 'aiohttp'가 설치되어 있지 않습니다.\n"
        "다음 명령으로 설치 후 다시 실행하세요:\n"
        "    pip install aiohttp>=3.9"
    ) from exc

# 고정 폭 컬럼 헤더 텍스트 (로그 상단 고정 표기)
HEADER_LINE = f"{'TIME':23}  {'SEQ':>6}  {'STAT':>5}  {'LAT(ms)':>8}  ERROR"


@dataclass
class LatencyStats:
    count: int = 0
    total_ms: float = 0.0
    min_ms: Optional[float] = None
    max_ms: Optional[float] = None

    def add(self, latency_ms: float) -> None:
        self.count += 1
        self.total_ms += latency_ms
        if self.min_ms is None or latency_ms < self.min_ms:
            self.min_ms = latency_ms
        if self.max_ms is None or latency_ms > self.max_ms:
            self.max_ms = latency_ms

    def mean(self) -> Optional[float]:
        if not self.count:
            return None
        return self.total_ms / self.count


@dataclass
class RunSummary:
    total_sent: int
    status_counts: Counter
    elapsed_s: float
    latencies: LatencyStats
    started_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Union[int, float, Dict[str, int]]]:
        data = {
            "total_sent": self.total_sent,
            "elapsed_s": round(self.elapsed_s, 3),
            "status_counts": {str(k): v for k, v in self.status_counts.items()},
            "started_at": self.started_at.isoformat() + "Z",
        }
        if self.latencies.count:
            data["latency"] = {
                "count": self.latencies.count,
                "average_ms": round(self.latencies.mean() or 0.0, 3),
                "min_ms": round(self.latencies.min_ms or 0.0, 3),
                "max_ms": round(self.latencies.max_ms or 0.0, 3),
            }
        else:
            data["latency"] = {"count": 0}
        return data


@dataclass
class RunConfig:
    url: str
    method: str
    rps: int
    duration_s: int
    timeout_s: float
    headers: Dict[str, str]
    body: Optional[bytes]
    headers_text: str = ""
    body_text: str = ""

    @classmethod
    def from_fields(
        cls,
        url: str,
        method: str,
        rps: int,
        duration_s: int,
        timeout_s: float,
        headers_text: str,
        body_text: str,
    ) -> "RunConfig":
        headers = parse_header_lines(headers_text)
        body = body_text.encode("utf-8") if body_text else None
        return cls(
            url=url,
            method=method,
            rps=rps,
            duration_s=duration_s,
            timeout_s=timeout_s,
            headers=headers,
            body=body,
            headers_text=headers_text,
            body_text=body_text,
        )

    def to_dict(self) -> Dict[str, Union[str, int, float]]:
        return {
            "url": self.url,
            "method": self.method,
            "rps": self.rps,
            "duration_s": self.duration_s,
            "timeout_s": self.timeout_s,
            "headers": self.headers_text,
            "body": self.body_text,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Union[str, int, float]]) -> "RunConfig":
        url = str(data["url"]).strip()
        method = str(data.get("method", "GET")).strip().upper() or "GET"
        rps = int(data.get("rps", 1))
        duration_s = int(data.get("duration_s", 1))
        timeout_s = float(data.get("timeout_s", 10))
        headers_txt = str(data.get("headers", "") or "")
        body_txt = str(data.get("body", "") or "")
        return cls.from_fields(url, method, rps, duration_s, timeout_s, headers_txt, body_txt)


def parse_header_lines(raw: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for line in raw.splitlines():
        striped = line.strip()
        if not striped:
            continue
        if ":" not in striped:
            raise ValueError(f"헤더 형식 오류: {line!r} (Name: Value)")
        name, value = striped.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            raise ValueError(f"헤더 이름이 비어 있습니다: {line!r}")
        headers[name] = value
    return headers


async def fetch(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    timeout_s: float,
    headers: Dict[str, str],
    body: Optional[bytes],
) -> Tuple[Optional[int], float, Optional[str]]:
    t0 = time.perf_counter()
    try:
        async with session.request(
            method,
            url,
            timeout=timeout_s,
            headers=headers,
            data=body,
        ) as resp:
            await resp.read()
            dt = (time.perf_counter() - t0) * 1000.0
            return resp.status, dt, None
    except Exception as exc:  # noqa: BLE001
        dt = (time.perf_counter() - t0) * 1000.0
        return None, dt, str(exc)


async def run_probe(
    url: str,
    method: str,
    rps: int,
    duration_s: int,
    timeout_s: float,
    headers: Dict[str, str],
    body: Optional[bytes],
    log_queue: "queue.Queue[str]",
    stop_flag: threading.Event,
) -> RunSummary:
    connector = aiohttp.TCPConnector(ssl=False, limit=None)
    timeout = aiohttp.ClientTimeout(total=None)
    status_counts: Counter = Counter()
    latencies = LatencyStats()

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        sent = 0
        start = time.perf_counter()
        summary_start = datetime.utcnow()
        end_time = start + float(duration_s)

        while time.perf_counter() < end_time and not stop_flag.is_set():
            sec_start = time.perf_counter()
            tasks = [
                asyncio.create_task(
                    fetch(session, method, url, timeout_s, headers, body)
                )
                for _ in range(rps)
            ]
            pending = set(tasks)

            while pending:
                if stop_flag.is_set():
                    for task in pending:
                        task.cancel()
                    done, pending = await asyncio.wait(pending, timeout=0.05)
                else:
                    done, pending = await asyncio.wait(
                        pending,
                        timeout=0.05,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                for finished in done:
                    try:
                        status, latency_ms, error = finished.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as exc:  # noqa: BLE001
                        status, latency_ms, error = None, 0.0, f"{type(exc).__name__}: {exc}"

                    sent += 1
                    if latency_ms is not None:
                        latencies.add(latency_ms)

                    status_key: Union[int, str]
                    if status is None:
                        status_key = "ERR"
                    else:
                        status_key = status
                    status_counts[status_key] += 1

                    ts_iso = datetime.now().isoformat(timespec="milliseconds")
                    status_display = str(status_key)
                    lat_display = f"{latency_ms:.2f}" if latency_ms is not None else "-"
                    err_txt = (error or "").strip()
                    line = f"{ts_iso:23}  {sent:6d}  {status_display:>5}  {lat_display:>8}  {err_txt}"
                    log_queue.put(line)

            elapsed = time.perf_counter() - sec_start
            to_sleep = 1.0 - elapsed
            if to_sleep > 0 and not stop_flag.is_set():
                await asyncio.sleep(to_sleep)

        total = time.perf_counter() - start
        log_queue.put(f"종료: 총 {sent}건, 경과 {total:.2f}s")

    return RunSummary(
        total_sent=sent,
        status_counts=status_counts,
        elapsed_s=total,
        latencies=latencies,
        started_at=summary_start,
    )


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, payload: Dict[str, object]) -> None:
    _ensure_parent_dir(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class RateLimitTesterGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Rate Limit Tester")
        try:
            self.root.lift()
            self.root.focus_force()
            self.root.after(500, lambda: self.root.attributes("-topmost", False))
            self.root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.root.tk.call("tk", "scaling", 1.8)
        except Exception:  # noqa: BLE001
            pass
        try:
            style = ttk.Style()
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass

        self.root.minsize(960, 640)
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.summary_queue: "queue.Queue[RunSummary]" = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.last_summary: Optional[RunSummary] = None
        self.summary_window: Optional[tk.Toplevel] = None
        self.current_config: Optional[RunConfig] = None
        self.last_saved_log: Optional[Path] = None

        frm = ttk.Frame(root, padding=(16, 14))
        frm.pack(fill="both", expand=True)
        pad_x = 12
        pad_y = 8

        ttk.Label(frm, text="URL").grid(row=0, column=0, sticky="w")
        self.e_url = ttk.Entry(frm)
        self.e_url.insert(0, "https://example.com/health")
        self.e_url.grid(row=0, column=1, columnspan=6, sticky="we", padx=(pad_x // 2, pad_x), pady=(0, pad_y))

        self.btn_paste_url = ttk.Button(frm, text="붙여넣기", command=self.paste_url_from_clipboard)
        self.btn_paste_url.grid(row=0, column=7, sticky="we", padx=(0, pad_x // 2), pady=(0, pad_y))

        controls = ttk.Frame(frm)
        controls.grid(row=1, column=0, columnspan=8, sticky="we", padx=(pad_x // 2, pad_x), pady=(0, pad_y))

        ttk.Label(controls, text="초당 요청 수 (RPS)").grid(row=0, column=0, sticky="w", padx=(0, pad_x // 2))
        self.e_rps = ttk.Entry(controls, width=10)
        self.e_rps.insert(0, "5")
        self.e_rps.grid(row=0, column=1, sticky="we", padx=(0, pad_x))

        ttk.Label(controls, text="실행 시간(초)").grid(row=0, column=2, sticky="w", padx=(0, pad_x // 2))
        self.e_dur = ttk.Entry(controls, width=10)
        self.e_dur.insert(0, "30")
        self.e_dur.grid(row=0, column=3, sticky="we", padx=(0, pad_x))

        ttk.Label(controls, text="타임아웃(초)").grid(row=0, column=4, sticky="w", padx=(0, pad_x // 2))
        self.e_to = ttk.Entry(controls, width=10)
        self.e_to.insert(0, "10")
        self.e_to.grid(row=0, column=5, sticky="we", padx=(0, pad_x))

        ttk.Label(controls, text="HTTP 메서드").grid(row=0, column=6, sticky="w", padx=(0, pad_x // 2))
        self.cb_method = ttk.Combobox(
            controls,
            values=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            width=10,
        )
        self.cb_method.set("GET")
        self.cb_method.grid(row=0, column=7, sticky="we")

        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(3, weight=1)
        controls.grid_columnconfigure(5, weight=1)
        controls.grid_columnconfigure(7, weight=1)

        preset_buttons = ttk.Frame(frm)
        preset_buttons.grid(row=2, column=0, columnspan=8, sticky="we", padx=(pad_x // 2, pad_x), pady=(0, pad_y))
        ttk.Button(preset_buttons, text="구성 불러오기", command=self.load_preset).pack(side="left")
        ttk.Button(preset_buttons, text="구성 저장", command=self.save_preset).pack(side="left", padx=(pad_x // 2, 0))

        buttons_frame = ttk.Frame(frm)
        buttons_frame.grid(row=8, column=3, columnspan=5, sticky="e", padx=(0, pad_x // 2), pady=(pad_y, 0))

        self.btn_start = ttk.Button(buttons_frame, text="시작", width=12, command=self.on_start)
        self.btn_start.pack(side="left", padx=(0, pad_x // 2))

        self.btn_stop = ttk.Button(
            buttons_frame,
            text="중지",
            width=12,
            state="disabled",
            command=self.on_stop,
        )
        self.btn_stop.pack(side="left", padx=(0, pad_x // 2))

        self.btn_show_summary = ttk.Button(
            buttons_frame,
            text="요약 보기",
            width=12,
            command=self.on_show_summary,
            state="disabled",
        )
        self.btn_show_summary.pack(side="left", padx=(0, pad_x // 2))

        self.btn_export_log = ttk.Button(
            buttons_frame,
            text="로그 저장",
            width=12,
            command=self.export_log,
            state="disabled",
        )
        self.btn_export_log.pack(side="left")

        headers_frame = ttk.Labelframe(frm, text="헤더 (한 줄당 'Name: Value')")
        headers_frame.grid(row=3, column=0, columnspan=8, sticky="nsew", padx=(pad_x // 2, pad_x), pady=(0, pad_y))
        self.txt_headers = tk.Text(headers_frame, height=4, wrap="none")
        self.txt_headers.pack(fill="both", expand=True)
        self._setup_context_menu(self.txt_headers)

        body_frame = ttk.Labelframe(frm, text="본문 (UTF-8, 비워두면 전송 안 함)")
        body_frame.grid(row=4, column=0, columnspan=8, sticky="nsew", padx=(pad_x // 2, pad_x), pady=(0, pad_y))
        body_toolbar = ttk.Frame(body_frame)
        body_toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(body_toolbar, text="파일 불러오기", command=self.load_body_from_file).pack(side="left")
        ttk.Button(body_toolbar, text="파일 저장", command=self.save_body_to_file).pack(side="left", padx=(pad_x // 2, 0))
        self.lbl_body_size = ttk.Label(body_toolbar, text="본문 없음")
        self.lbl_body_size.pack(side="right")
        self.txt_body = tk.Text(body_frame, height=6, wrap="none")
        self.txt_body.pack(fill="both", expand=True)
        self._setup_context_menu(self.txt_body)
        self.txt_body.bind("<<Modified>>", self._on_body_modified)
        self.txt_body.edit_modified(False)
        self.update_body_size_label()

        self.lbl_status = ttk.Label(frm, text="대기")
        self.lbl_status.grid(row=5, column=0, columnspan=8, sticky="w", padx=(pad_x // 2, pad_x), pady=(0, pad_y))

        self.lbl_head = tk.Label(frm, text=HEADER_LINE, anchor="w")
        try:
            monospace = tkfont.nametofont("TkFixedFont")
        except Exception:  # noqa: BLE001
            monospace = tkfont.Font(family="Menlo", size=12)
        self.lbl_head.configure(font=monospace, background="#1e1e1e", foreground="#a0a0a0")
        self.lbl_head.grid(row=6, column=0, columnspan=8, sticky="we", padx=(pad_x // 2, pad_x), pady=(pad_y, 0))

        self.txt_log = tk.Text(frm, height=18, wrap="none")
        try:
            monospace = tkfont.nametofont("TkFixedFont")
        except Exception:  # noqa: BLE001
            monospace = tkfont.Font(family="Menlo", size=12)
        self.txt_log.configure(font=monospace, background="#1e1e1e", foreground="#d0d0d0", insertbackground="#ffffff")
        self.txt_log.grid(row=7, column=0, columnspan=8, sticky="nsew", padx=(pad_x // 2, pad_x), pady=(0, pad_y))
        self.txt_log.configure(state="disabled")
        self.txt_log.tag_configure("ok", foreground="#2ecc71")
        self.txt_log.tag_configure("warn", foreground="#ffd166")
        self.txt_log.tag_configure("err", foreground="#ff6b6b")
        self.txt_log.tag_configure("info", foreground="#a0a0a0")
        self._setup_context_menu(self.txt_log, is_text=True)

        yscroll = ttk.Scrollbar(frm, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=7, column=8, sticky="ns", padx=(0, pad_x // 2), pady=(0, pad_y))

        self.lbl_prog = ttk.Label(frm, text="0000회 전송")
        self.lbl_prog.grid(row=8, column=0, columnspan=3, sticky="w", padx=(pad_x // 2, pad_x // 2), pady=(pad_y, 0))

        for c in range(9):
            frm.grid_columnconfigure(c, weight=0)
        frm.grid_columnconfigure(1, weight=3)
        frm.grid_columnconfigure(3, weight=2)
        frm.grid_columnconfigure(4, weight=1)
        frm.grid_columnconfigure(6, weight=1)
        frm.grid_rowconfigure(3, weight=1)
        frm.grid_rowconfigure(4, weight=1)
        frm.grid_rowconfigure(7, weight=3)

        self._setup_context_menu(self.e_url)
        self._setup_context_menu(self.e_rps)
        self._setup_context_menu(self.e_dur)
        self._setup_context_menu(self.e_to)
        self._setup_context_menu(self.cb_method)

        self.e_url.focus_set()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.reset_idle()
        self._drain_queue()

        def _force_paste(event):  # noqa: ANN001
            try:
                widget = self.root.focus_get()
                if not widget:
                    return
                if "Entry" in str(getattr(widget, "winfo_class", lambda: "")()):
                    widget.event_generate("<<Paste>>")
                    return "break"
            except Exception:  # noqa: BLE001
                return

        for seq in ("<Command-v>", "<Command-V>", "<Control-v>", "<Control-V>", "<Meta-v>", "<Meta-V>"):
            self.root.bind_all(seq, _force_paste, add=True)

    def _setup_context_menu(self, widget, is_text: bool = False):
        menu = tk.Menu(widget, tearoff=False)
        menu.add_command(label="잘라내기", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="복사", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="붙여넣기", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="전체 선택", command=lambda: widget.event_generate("<<SelectAll>>"))

        def show_menu(event):  # noqa: ANN001
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show_menu)
        widget.bind("<Button-2>", show_menu)
        for seq in ("<Control-a>", "<Command-a>", "<Meta-a>", "<Control-A>", "<Command-A>", "<Meta-A>"):
            widget.bind(seq, lambda e: (widget.event_generate("<<SelectAll>>"), "break"))
        for seq in ("<Control-c>", "<Command-c>", "<Meta-c>", "<Control-C>", "<Command-C>", "<Meta-C>"):
            widget.bind(seq, lambda e: (widget.event_generate("<<Copy>>"), "break"))
        for seq in ("<Control-v>", "<Command-v>", "<Meta-v>", "<Control-V>", "<Command-V>", "<Meta-V>"):
            widget.bind(seq, lambda e: (widget.event_generate("<<Paste>>"), "break"))
        for seq in ("<Control-x>", "<Command-x>", "<Meta-x>", "<Control-X>", "<Command-X>", "<Meta-X>"):
            widget.bind(seq, lambda e: (widget.event_generate("<<Cut>>"), "break"))
        if is_text:
            widget.bind("<Control-c>", lambda e: (widget.event_generate("<<Copy>>"), "break"))
            widget.bind("<Command-c>", lambda e: (widget.event_generate("<<Copy>>"), "break"))

            def block_input(event):  # noqa: ANN001
                return "break"

            widget.bind("<Key>", block_input)

    def collect_config(self) -> Optional[RunConfig]:
        try:
            url = self.e_url.get().strip()
            rps_val = int(self.e_rps.get().strip())
            duration = int(self.e_dur.get().strip())
            timeout = float(self.e_to.get().strip())
            method = self.cb_method.get().strip().upper() or "GET"
            if not url or rps_val <= 0 or duration <= 0 or timeout <= 0:
                raise ValueError
        except Exception:  # noqa: BLE001
            messagebox.showerror("오류", "입력 값을 확인")
            return None

        header_text = self.txt_headers.get("1.0", "end-1c")
        body_text = self.txt_body.get("1.0", "end-1c")
        try:
            config = RunConfig.from_fields(
                url=url,
                method=method,
                rps=rps_val,
                duration_s=duration,
                timeout_s=timeout,
                headers_text=header_text,
                body_text=body_text,
            )
        except ValueError as exc:
            messagebox.showerror("오류", str(exc))
            return None
        return config

    def on_start(self):
        config = self.collect_config()
        if config is None:
            return

        self.current_config = config
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_show_summary.config(state="disabled")
        self.btn_export_log.config(state="disabled")
        self.lbl_status.config(text="실행 중")
        self.lbl_prog.config(text="0000회 전송")
        self.stop_flag.clear()
        self.last_summary = None
        while not self.summary_queue.empty():
            try:
                self.summary_queue.get_nowait()
            except queue.Empty:
                break

        self.log_queue.put(
            f"START url={config.url} method={config.method} rps={config.rps} duration={config.duration_s}s timeout={config.timeout_s}s"
        )
        if config.headers:
            header_preview = ", ".join(f"{k}: {v}" for k, v in config.headers.items())
            self.log_queue.put(f"HEADERS: {header_preview}")
        if config.body:
            self.log_queue.put(f"BODY: {len(config.body)} bytes")

        def worker():
            try:
                summary = asyncio.run(
                    run_probe(
                        url=config.url,
                        method=config.method,
                        rps=config.rps,
                        duration_s=config.duration_s,
                        timeout_s=config.timeout_s,
                        headers=config.headers,
                        body=config.body,
                        log_queue=self.log_queue,
                        stop_flag=self.stop_flag,
                    )
                )
                self.summary_queue.put(summary)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: messagebox.showerror("오류", f"실행 실패 {exc}"))
            finally:
                self.root.after(0, self.reset_idle)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def on_stop(self):
        self.stop_flag.set()
        self.lbl_status.config(text="중지 요청")

    def on_show_summary(self):
        if self.last_summary is not None and self.current_config is not None:
            self.show_summary(self.current_config, self.last_summary)

    def reset_idle(self):
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        if self.last_summary is not None:
            self.btn_show_summary.config(state="normal")
            self.btn_export_log.config(state="normal")
            self.lbl_status.config(text="완료")
        else:
            self.btn_show_summary.config(state="disabled")
            self.btn_export_log.config(state="disabled")
            self.lbl_status.config(text="대기")

    def on_close(self):
        self.stop_flag.set()
        self.root.destroy()

    def _drain_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                print(msg)
                if msg and msg[0].isdigit() and len(msg) >= 31:
                    try:
                        seq_str = msg[25:31].strip()
                        count = int(seq_str)
                        self.lbl_prog.config(text=f"{count:04d}회 전송")
                    except Exception:  # noqa: BLE001
                        pass
                tag = None
                if msg.startswith("TIME ") or (msg and set(msg) == {"-"}):
                    tag = "info"
                else:
                    if msg and msg[0].isdigit() and len(msg) >= 40:
                        stat = msg[33:38].strip()
                        tag = "info"
                        if stat == "ERR":
                            tag = "err"
                        else:
                            try:
                                sc = int(stat)
                                if 200 <= sc < 300:
                                    tag = "ok"
                                elif sc == 429:
                                    tag = "warn"
                                elif 400 <= sc < 600:
                                    tag = "err"
                                else:
                                    tag = None
                            except ValueError:
                                tag = None

                self.txt_log.configure(state="normal")
                if tag:
                    self.txt_log.insert("end", msg + "\n", tag)
                else:
                    self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass

        try:
            while True:
                summary = self.summary_queue.get_nowait()
                self.last_summary = summary
                self.btn_show_summary.config(state="normal")
                self.btn_export_log.config(state="normal")
                if self.current_config is not None:
                    self.show_summary(self.current_config, summary)
        except queue.Empty:
            pass

        self.root.after(50, self._drain_queue)

    def paste_url_from_clipboard(self):
        data = None
        try:
            data = self.root.clipboard_get()
        except Exception:  # noqa: BLE001
            try:
                data = self.root.selection_get(selection="CLIPBOARD")
            except Exception:  # noqa: BLE001
                data = None
        if data:
            text = str(data).strip()
            self.e_url.delete(0, tk.END)
            self.e_url.insert(0, text)
            self.e_url.focus_set()
            try:
                self.e_url.icursor(tk.END)
            except Exception:  # noqa: BLE001
                pass
            self.lbl_status.config(text="URL 붙여넣기 완료")
        else:
            self.lbl_status.config(text="클립보드가 비어있습니다")

    def load_body_from_file(self):
        path = filedialog.askopenfilename(title="본문 파일 선택")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = fh.read()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("오류", f"파일을 불러올 수 없습니다: {exc}")
            return
        self.txt_body.delete("1.0", tk.END)
        self.txt_body.insert("1.0", data)
        self.update_body_size_label()
        self.lbl_status.config(text=f"본문 로드: {path}")

    def save_body_to_file(self):
        path = filedialog.asksaveasfilename(title="본문 저장", defaultextension=".txt")
        if not path:
            return
        content = self.txt_body.get("1.0", "end-1c")
        try:
            Path(path).write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("오류", f"저장 실패: {exc}")
            return
        self.lbl_status.config(text=f"본문 저장: {path}")

    def _on_body_modified(self, event):  # noqa: ANN001
        if self.txt_body.edit_modified():
            self.update_body_size_label()
            self.txt_body.edit_modified(False)

    def update_body_size_label(self):
        content = self.txt_body.get("1.0", "end-1c")
        if content:
            self.lbl_body_size.config(text=f"{len(content.encode('utf-8'))} bytes")
        else:
            self.lbl_body_size.config(text="본문 없음")

    def show_summary(self, config: RunConfig, summary: RunSummary):
        try:
            if self.summary_window is not None and tk.Toplevel.winfo_exists(self.summary_window):
                self.summary_window.destroy()
        except Exception:  # noqa: BLE001
            pass

        success = sum(
            count for status, count in summary.status_counts.items()
            if isinstance(status, int) and 200 <= status < 300
        )
        rate_limited = summary.status_counts.get(429, 0)
        error_count = summary.status_counts.get("ERR", 0)
        other_failures = sum(
            count for status, count in summary.status_counts.items()
            if isinstance(status, int) and status >= 400 and status != 429
        ) + error_count
        achieved_rps = summary.total_sent / summary.elapsed_s if summary.elapsed_s else 0.0

        win = tk.Toplevel(self.root)
        win.title("요약 통계")
        win.transient(self.root)
        win.grab_set()
        self.summary_window = win

        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="실행 요약", font=("", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frm, text=f"요청 시작: {summary.started_at.astimezone().isoformat(timespec='seconds')}").grid(row=1, column=0, sticky="w")
        ttk.Label(frm, text=f"목표 RPS: {config.rps}").grid(row=2, column=0, sticky="w")
        ttk.Label(frm, text=f"달성 RPS: {achieved_rps:.2f}").grid(row=3, column=0, sticky="w")
        ttk.Label(frm, text=f"총 요청 수: {summary.total_sent}").grid(row=4, column=0, sticky="w")
        ttk.Label(frm, text=f"경과 시간: {summary.elapsed_s:.2f}s").grid(row=5, column=0, sticky="w")
        ttk.Label(frm, text=f"성공(2xx): {success}").grid(row=6, column=0, sticky="w")
        ttk.Label(frm, text=f"429: {rate_limited}").grid(row=7, column=0, sticky="w")
        ttk.Label(frm, text=f"기타 실패: {other_failures}").grid(row=8, column=0, sticky="w")

        if summary.latencies.count:
            avg = summary.latencies.mean() or 0.0
            min_ms = summary.latencies.min_ms or 0.0
            max_ms = summary.latencies.max_ms or 0.0
            ttk.Label(frm, text=f"평균 지연: {avg:.2f} ms").grid(row=9, column=0, sticky="w")
            ttk.Label(frm, text=f"최소 지연: {min_ms:.2f} ms").grid(row=10, column=0, sticky="w")
            ttk.Label(frm, text=f"최대 지연: {max_ms:.2f} ms").grid(row=11, column=0, sticky="w")
        else:
            ttk.Label(frm, text="지연 데이터 없음").grid(row=9, column=0, sticky="w")

        ttk.Label(frm, text="상세 응답 코드", font=("", 11, "bold")).grid(row=12, column=0, sticky="w", pady=(12, 4))
        tree = ttk.Treeview(frm, columns=("status", "count"), show="headings", height=6)
        tree.heading("status", text="코드")
        tree.heading("count", text="건수")
        tree.column("status", width=80, anchor="center")
        tree.column("count", width=120, anchor="center")
        for status, count in summary.status_counts.most_common():
            tree.insert("", "end", values=(status, count))
        tree.grid(row=13, column=0, columnspan=2, sticky="nsew")

        scrollbar = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=13, column=2, sticky="ns")

        btns = ttk.Frame(frm)
        btns.grid(row=14, column=0, columnspan=2, sticky="e", pady=(12, 0))

        ttk.Button(btns, text="클립보드 복사", command=lambda: self.copy_summary_to_clipboard(config, summary)).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="JSON 저장", command=lambda: self.save_summary_as_json(config, summary)).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="닫기", command=win.destroy).pack(side="left")

        frm.grid_columnconfigure(0, weight=1)
        frm.grid_rowconfigure(13, weight=1)

    def copy_summary_to_clipboard(self, config: RunConfig, summary: RunSummary):
        payload = {
            "config": config.to_dict(),
            "summary": summary.to_dict(),
        }
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(json.dumps(payload, ensure_ascii=False, indent=2))
            messagebox.showinfo("복사 완료", "요약 정보가 클립보드에 복사되었습니다.")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("오류", f"복사 실패: {exc}")

    def save_summary_as_json(self, config: RunConfig, summary: RunSummary):
        path = filedialog.asksaveasfilename(title="요약 저장", defaultextension=".json")
        if not path:
            return
        payload = {"config": config.to_dict(), "summary": summary.to_dict()}
        try:
            save_json(Path(path), payload)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("오류", f"저장 실패: {exc}")
            return
        messagebox.showinfo("저장 완료", f"요약이 저장되었습니다:\n{path}")

    def export_log(self):
        path = filedialog.asksaveasfilename(
            title="로그 저장",
            defaultextension=".log",
            initialfile="rate-limit-log.log",
        )
        if not path:
            return
        try:
            content = self.txt_log.get("1.0", "end-1c")
            Path(path).write_text(content, encoding="utf-8")
            self.last_saved_log = Path(path)
            messagebox.showinfo("저장 완료", f"로그를 저장했습니다:\n{path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("오류", f"로그 저장 실패: {exc}")

    def load_preset(self):
        path = filedialog.askopenfilename(title="구성 불러오기", filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")])
        if not path:
            return
        try:
            data = load_json(Path(path))
            config = RunConfig.from_dict(data["config"] if "config" in data else data)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("오류", f"구성을 읽을 수 없습니다: {exc}")
            return
        self.apply_config(config)
        messagebox.showinfo("불러오기 완료", f"구성을 불러왔습니다:\n{path}")

    def save_preset(self):
        config = self.collect_config()
        if config is None:
            return
        path = filedialog.asksaveasfilename(
            title="구성 저장",
            defaultextension=".json",
            initialfile="rate-limit-config.json",
        )
        if not path:
            return
        payload = {"config": config.to_dict()}
        try:
            save_json(Path(path), payload)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("오류", f"저장 실패: {exc}")
            return
        messagebox.showinfo("저장 완료", f"구성을 저장했습니다:\n{path}")

    def apply_config(self, config: RunConfig):
        self.e_url.delete(0, tk.END)
        self.e_url.insert(0, config.url)
        self.cb_method.set(config.method)
        self.e_rps.delete(0, tk.END)
        self.e_rps.insert(0, str(config.rps))
        self.e_dur.delete(0, tk.END)
        self.e_dur.insert(0, str(config.duration_s))
        self.e_to.delete(0, tk.END)
        self.e_to.insert(0, str(config.timeout_s))
        self.txt_headers.delete("1.0", tk.END)
        self.txt_headers.insert("1.0", config.headers_text)
        self.txt_body.delete("1.0", tk.END)
        self.txt_body.insert("1.0", config.body_text)
        self.update_body_size_label()


def run_headless(args: argparse.Namespace) -> None:
    if args.headers and args.headers_path:
        raise ValueError("--headers and --headers-path cannot be used together")
    if args.body and args.body_path:
        raise ValueError("--body and --body-path cannot be used together")

    headers_text = ""
    if args.headers:
        headers_text = args.headers
    elif args.headers_path:
        headers_text = Path(args.headers_path).read_text(encoding="utf-8")

    body_text = ""
    if args.body:
        body_text = args.body
    elif args.body_path:
        body_text = Path(args.body_path).read_text(encoding="utf-8")

    if args.preset:
        data = load_json(Path(args.preset))
        preset = RunConfig.from_dict(data["config"] if "config" in data else data)
        url = args.url or preset.url
        method = args.method or preset.method
        rps = args.rps or preset.rps
        duration = args.duration or preset.duration_s
        timeout = args.timeout or preset.timeout_s
        if not headers_text:
            headers_text = preset.headers_text
        if not body_text:
            body_text = preset.body_text
    else:
        url = args.url
        method = (args.method or "GET").upper()
        rps = args.rps
        duration = args.duration
        timeout = args.timeout

    if not url:
        raise ValueError("URL is required in headless mode (use --url or preset with URL)")

    config = RunConfig.from_fields(
        url=url.strip(),
        method=method.strip().upper() or "GET",
        rps=int(rps),
        duration_s=int(duration),
        timeout_s=float(timeout),
        headers_text=headers_text,
        body_text=body_text,
    )

    log_queue: "queue.Queue[str]" = queue.Queue()
    stop_flag = threading.Event()

    print(f"Running headless probe: {config.url} ({config.method})")

    async def runner():
        return await run_probe(
            url=config.url,
            method=config.method,
            rps=config.rps,
            duration_s=config.duration_s,
            timeout_s=config.timeout_s,
            headers=config.headers,
            body=config.body,
            log_queue=log_queue,
            stop_flag=stop_flag,
        )

    summary = asyncio.run(runner())

    logs: List[str] = []
    while not log_queue.empty():
        logs.append(log_queue.get())
    if args.log_file:
        Path(args.log_file).write_text("\n".join(logs), encoding="utf-8")
    if args.print_log:
        for line in logs:
            print(line)

    payload = {"config": config.to_dict(), "summary": summary.to_dict()}
    if args.summary_json:
        save_json(Path(args.summary_json), payload)

    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Rate Limit Tester")
    parser.add_argument("--url", help="Target URL for the probe")
    parser.add_argument("--method", default="GET", help="HTTP method (default: GET)")
    parser.add_argument("--rps", type=int, default=5, help="Requests per second")
    parser.add_argument("--duration", type=int, default=30, help="Duration in seconds")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds")
    parser.add_argument("--headers", help="Raw header text (Name: Value per line)")
    parser.add_argument("--headers-path", help="Path to file containing header definitions")
    parser.add_argument("--body", help="Request body text")
    parser.add_argument("--body-path", help="Path to file with request body")
    parser.add_argument("--preset", help="Path to JSON preset generated by the GUI")
    parser.add_argument("--log-file", help="If provided, write execution log to this path")
    parser.add_argument("--summary-json", help="If provided, write summary JSON to this path")
    parser.add_argument("--print-log", action="store_true", help="Print individual log lines to stdout")
    parser.add_argument("--headless", action="store_true", help="Run without GUI")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.headless or any(
        getattr(args, opt)
        for opt in (
            "url",
            "preset",
            "log_file",
            "summary_json",
            "print_log",
            "headers",
            "headers_path",
            "body",
            "body_path",
        )
    ):
        run_headless(args)
        return

    root = tk.Tk()
    app = RateLimitTesterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
