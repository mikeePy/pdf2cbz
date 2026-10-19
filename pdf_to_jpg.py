import math
import multiprocessing as mp
import os
import tempfile
import threading
import tkinter as tk
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    import fitz  # PyMuPDF
except ImportError:
    raise SystemExit("PyMuPDF is required. Install with:  pip install PyMuPDF")

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
    _RootClass = TkinterDnD.Tk
except ImportError:
    _HAS_DND = False
    _RootClass = tk.Tk


# ---- module-level constants ----
MAX_HEIGHT_PX = 1500        # hard cap on rendered image height
MAX_WIDTH_PX = 3000         # hard cap on rendered image width
MAX_PIXELS = 4_000_000      # hard cap on total pixels (~12 MB RGB pixmap)


# ---- top-level helpers (must be importable by child processes) ----
def _page_native_dpi(page, min_cover=0.80):
    """If a single embedded image covers >= min_cover of the page, return
    its native rendering DPI as (dpi, src_width_px, src_height_px).
    Otherwise return None (vector / text / mixed page)."""
    try:
        page_rect = page.rect
        if page_rect.width <= 0 or page_rect.height <= 0:
            return None
        page_area = page_rect.width * page_rect.height
        imgs = page.get_images(full=True)
        if not imgs:
            return None
        imgs = sorted(
            imgs, key=lambda im: (im[2] or 0) * (im[3] or 0), reverse=True
        )
        for im in imgs:
            xref = im[0]
            w_px = im[2] or 0
            h_px = im[3] or 0
            if w_px <= 0 or h_px <= 0:
                continue
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                continue
            if not rects:
                continue
            r = max(rects, key=lambda rr: rr.width * rr.height)
            if r.width <= 0 or r.height <= 0:
                continue
            if (r.width * r.height) / page_area < min_cover:
                continue
            dpi_w = w_px * 72.0 / r.width
            dpi_h = h_px * 72.0 / r.height
            native = min(dpi_w, dpi_h)
            if native > 0:
                return (native, w_px, h_px)
        return None
    except Exception:
        return None


def _render_chunk(args):
    """Render a chunk of pages. Runs in a child process.

    Returns a list of 10-tuples, one per page in the chunk:
        (index, jpg_path_or_None, w, h, eff_dpi,
         native_dpi, src_w, src_h, cap_str, error_or_None)

    cap_str is "" if no cap applied, otherwise e.g.
    "height≤1500, width≤3000, pixels≤4M".
    """
    (pdf_path, password, chunk, workdir, dpi, quality,
     colorspace, cap_dpi) = args
    import fitz as _fitz
    cs = _fitz.csGRAY if colorspace == "gray" else _fitz.csRGB

    cap_labels = {
        "height": f"height≤{MAX_HEIGHT_PX}",
        "width": f"width≤{MAX_WIDTH_PX}",
        "pixels": f"pixels≤{MAX_PIXELS // 1_000_000}M",
    }

    results = []
    doc = None
    try:
        doc = _fitz.open(pdf_path)
        if password and doc.needs_pass:
            if not doc.authenticate(password):
                raise RuntimeError("Incorrect password")

        for i in chunk:
            try:
                page = doc.load_page(i)
                target_dpi = int(dpi)
                native_dpi = None
                src_w = src_h = None
                caps = set()

                # 1) optional per-page cap to source image resolution
                if cap_dpi:
                    native = _page_native_dpi(page)
                    if native is not None:
                        native_dpi, src_w, src_h = native
                        if native_dpi < target_dpi:
                            target_dpi = max(1, int(native_dpi))

                page_w_pt = max(0.0, page.rect.width or 0.0)
                page_h_pt = max(0.0, page.rect.height or 0.0)

                dpi_final = target_dpi

                # 2) pre-render caps
                if page_h_pt > 0:
                    m = (MAX_HEIGHT_PX * 72.0) / page_h_pt
                    if dpi_final > m:
                        dpi_final = max(1, int(m))
                        caps.add("height")
                if page_w_pt > 0:
                    m = (MAX_WIDTH_PX * 72.0) / page_w_pt
                    if dpi_final > m:
                        dpi_final = max(1, int(m))
                        caps.add("width")
                if page_w_pt > 0 and page_h_pt > 0:
                    m = math.sqrt(MAX_PIXELS * 72.0 * 72.0 /
                                  (page_w_pt * page_h_pt))
                    if dpi_final > m:
                        dpi_final = max(1, int(m))
                        caps.add("pixels")

                dpi_final = max(1, int(dpi_final))

                # 3) render with retry-on-failure (halve dpi up to 4 times)
                attempts = [dpi_final]
                cur = dpi_final
                for _ in range(4):
                    cur = max(36, cur // 2)
                    if cur >= dpi_final or cur < 36:
                        break
                    attempts.append(cur)

                pix = None
                last_err = None
                for attempt_dpi in attempts:
                    try:
                        pix = page.get_pixmap(
                            dpi=attempt_dpi, alpha=False, colorspace=cs
                        )
                        if attempt_dpi != dpi_final:
                            caps.add("retry")
                        dpi_final = attempt_dpi
                        break
                    except Exception as e:
                        last_err = e
                        pix = None
                        continue

                if pix is None:
                    results.append(
                        (i, None, 0, 0, int(dpi),
                         native_dpi, src_w, src_h, "",
                         f"page {i + 1}: render failed after retries: "
                         f"{last_err}")
                    )
                    continue

                # 4) post-render safety loop: re-render if any cap violated
                safety = 0
                while safety < 6 and dpi_final > 1:
                    over_h = pix.height > MAX_HEIGHT_PX
                    over_w = pix.width > MAX_WIDTH_PX
                    over_p = (pix.width * pix.height) > MAX_PIXELS
                    if not (over_h or over_w or over_p):
                        break
                    factors = []
                    if over_h:
                        factors.append(MAX_HEIGHT_PX / pix.height)
                        caps.add("height")
                    if over_w:
                        factors.append(MAX_WIDTH_PX / pix.width)
                        caps.add("width")
                    if over_p:
                        factors.append(
                            math.sqrt(MAX_PIXELS / (pix.width * pix.height))
                        )
                        caps.add("pixels")
                    factor = min(factors)
                    new_dpi = max(1, int(dpi_final * factor))
                    if new_dpi >= dpi_final:
                        new_dpi = dpi_final - 1
                    dpi_final = new_dpi
                    try:
                        pix = page.get_pixmap(
                            dpi=dpi_final, alpha=False, colorspace=cs
                        )
                    except Exception:
                        pix = None
                        break
                    safety += 1

                if pix is None:
                    results.append(
                        (i, None, 0, 0, int(dpi),
                         native_dpi, src_w, src_h, "",
                         f"page {i + 1}: render failed in safety loop")
                    )
                    continue

                fname = f"page_{i + 1:04d}.jpg"
                fpath = os.path.join(workdir, fname)
                pix.save(fpath, jpg_quality=quality)
                cap_str = ", ".join(
                    cap_labels[k] if k in cap_labels else "retry"
                    for k in ("height", "width", "pixels", "retry")
                    if k in caps
                )
                results.append(
                    (i, fpath, pix.width, pix.height, dpi_final,
                     native_dpi, src_w, src_h, cap_str, None)
                )
            except Exception as e:
                results.append(
                    (i, None, 0, 0, int(dpi),
                     None, None, None, "", f"page {i + 1}: {e}")
                )
    except Exception as e:
        first = chunk[0] if chunk else 0
        results.append(
            (first, None, 0, 0, int(dpi),
             None, None, None, "", f"worker: {e}")
        )
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return results


class PDFtoJPGApp(_RootClass):
    def __init__(self):
        super().__init__()
        self.title("PDF → CBZ Queue Converter")
        self.geometry("820x800")
        self.minsize(700, 680)

        self.out_dir = tk.StringVar()
        self.temp_dir = tk.StringVar()
        self.dpi = tk.IntVar(value=200)
        self.quality = tk.IntVar(value=60)
        self.workers = tk.IntVar(value=min(os.cpu_count() or 2, 8))
        self.grayscale = tk.BooleanVar(value=False)
        self.cap_dpi = tk.BooleanVar(value=True)
        self.auto_start = tk.BooleanVar(value=False)
        self.status = tk.StringVar(
            value="Ready — drag PDFs into the window, or click Add Files..."
        )

        self._converting = False
        self._cancel_requested = False
        self._path_to_row = {}
        self._row_to_path = {}

        self._build_ui()
        self._setup_dnd()

    # ---------- DnD ----------
    def _setup_dnd(self):
        if not _HAS_DND:
            self._log(
                "Drag-and-drop unavailable — install tkinterdnd2:\n"
                "    pip install tkinterdnd2\n"
                "The app still works via the Add Files... button."
            )
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as e:
            self._log(f"Drag-and-drop init failed: {e}")

    def _on_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = str(event.data).split()
        added = self._add_paths(paths)
        if added and self.auto_start.get() and not self._converting:
            self._start_batch(silent_validate=True)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- Queue ---
        frm_q = ttk.LabelFrame(
            self,
            text="Queue  (drag PDF files or folders into this window, or use Add Files...)",
        )
        frm_q.pack(fill="both", expand=True, **pad)

        cols = ("file", "folder", "status")
        self.tree = ttk.Treeview(frm_q, columns=cols, show="headings", height=8)
        self.tree.heading("file", text="File")
        self.tree.heading("folder", text="Folder")
        self.tree.heading("status", text="Status")
        self.tree.column("file", width=280, anchor="w")
        self.tree.column("folder", width=300, anchor="w")
        self.tree.column("status", width=220, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        sb = ttk.Scrollbar(frm_q, command=self.tree.yview)
        sb.pack(side="left", fill="y", pady=6)
        self.tree.config(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self._remove_selected())
        self.tree.bind("<Delete>", lambda e: self._remove_selected())

        frm_qbtns = ttk.Frame(frm_q)
        frm_qbtns.pack(side="left", fill="y", padx=6, pady=6)
        self.btn_add = ttk.Button(
            frm_qbtns, text="Add Files...", command=self._add_files_dialog
        )
        self.btn_add.pack(fill="x", pady=2)
        self.btn_remove = ttk.Button(
            frm_qbtns, text="Remove Selected", command=self._remove_selected
        )
        self.btn_remove.pack(fill="x", pady=2)
        self.btn_clear = ttk.Button(
            frm_qbtns, text="Clear Queue", command=self._clear_queue
        )
        self.btn_clear.pack(fill="x", pady=2)
        ttk.Checkbutton(
            frm_qbtns, text="Auto-start\non drop", variable=self.auto_start
        ).pack(fill="x", pady=(10, 2))

        # --- Output folder ---
        frm_out = ttk.LabelFrame(
            self, text="Output Folder  (leave empty to put each .cbz next to its PDF)"
        )
        frm_out.pack(fill="x", **pad)
        ttk.Entry(frm_out, textvariable=self.out_dir).pack(
            side="left", fill="x", expand=True, padx=6, pady=6
        )
        ttk.Button(frm_out, text="Browse...", command=self._pick_out).pack(
            side="left", padx=6, pady=6
        )
        ttk.Button(
            frm_out, text="Clear field", command=lambda: self.out_dir.set("")
        ).pack(side="left", padx=(0, 6), pady=6)

        # --- Temp folder ---
        frm_tmp = ttk.LabelFrame(
            self, text="Temp Folder for JPGs  (leave empty to use system temp)"
        )
        frm_tmp.pack(fill="x", **pad)
        ttk.Entry(frm_tmp, textvariable=self.temp_dir).pack(
            side="left", fill="x", expand=True, padx=6, pady=6
        )
        ttk.Button(frm_tmp, text="Browse...", command=self._pick_temp).pack(
            side="left", padx=6, pady=6
        )
        ttk.Button(
            frm_tmp, text="Clear field", command=lambda: self.temp_dir.set("")
        ).pack(side="left", padx=(0, 6), pady=6)

        # --- Options ---
        frm_opt = ttk.LabelFrame(self, text="Options")
        frm_opt.pack(fill="x", **pad)

        row1 = ttk.Frame(frm_opt)
        row1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row1, text="DPI:").pack(side="left")
        ttk.Spinbox(
            row1, from_=72, to=600, increment=10, textvariable=self.dpi, width=8
        ).pack(side="left", padx=(2, 14))
        ttk.Label(row1, text="JPEG Quality:").pack(side="left")
        ttk.Spinbox(
            row1, from_=10, to=100, increment=2, textvariable=self.quality, width=8
        ).pack(side="left", padx=(2, 14))
        ttk.Label(row1, text="Workers:").pack(side="left")
        ttk.Spinbox(
            row1,
            from_=1,
            to=max(2, (os.cpu_count() or 2) * 2),
            increment=1,
            textvariable=self.workers,
            width=8,
        ).pack(side="left", padx=(2, 14))
        ttk.Label(
            row1, text="(≈ CPU cores; more than cores rarely helps)",
            foreground="#666",
        ).pack(side="left")

        row2 = ttk.Frame(frm_opt)
        row2.pack(fill="x", padx=6, pady=(2, 2))
        ttk.Checkbutton(
            row2,
            text="Grayscale  (≈2–3× smaller for B&W scans)",
            variable=self.grayscale,
        ).pack(side="left")
        ttk.Checkbutton(
            row2,
            text="Cap DPI to source image resolution  (per page)",
            variable=self.cap_dpi,
        ).pack(side="left", padx=(24, 0))

        ttk.Label(
            frm_opt,
            text=f"Image caps per page: height ≤ {MAX_HEIGHT_PX}px, "
                 f"width ≤ {MAX_WIDTH_PX}px, "
                 f"total ≤ {MAX_PIXELS // 1_000_000}M pixels  "
                 f"(aspect ratio always preserved)",
            foreground="#666",
        ).pack(anchor="w", padx=6, pady=(0, 6))

        # --- Convert / Cancel ---
        frm_act = ttk.Frame(self)
        frm_act.pack(pady=8)
        self.btn_convert = ttk.Button(
            frm_act, text="Convert Queue", command=self._start_batch
        )
        self.btn_convert.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(
            frm_act, text="Cancel", command=self._request_cancel, state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)

        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8)

        frm_log = ttk.LabelFrame(self, text="Log")
        frm_log.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(frm_log, height=8, wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(frm_log, command=self.log.yview)
        sb2.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb2.set)

    # ---------- Queue ops ----------
    def _add_files_dialog(self):
        paths = filedialog.askopenfilenames(
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
        )
        if paths:
            added = self._add_paths(paths)
            if added and self.auto_start.get() and not self._converting:
                self._start_batch(silent_validate=True)

    def _add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for name in sorted(os.listdir(p)):
                    fp = os.path.join(p, name)
                    if os.path.isfile(fp) and fp.lower().endswith(".pdf"):
                        if self._enqueue(fp):
                            added += 1
            elif os.path.isfile(p) and p.lower().endswith(".pdf"):
                if self._enqueue(p):
                    added += 1
        if added:
            self.status.set(
                f"Added {added} file(s). Queue size: {len(self._row_to_path)}"
            )
        return added

    def _enqueue(self, path):
        path = os.path.normpath(path)
        if path in self._path_to_row:
            return False
        row = self.tree.insert(
            "",
            "end",
            values=(os.path.basename(path), os.path.dirname(path), "Pending"),
        )
        self._path_to_row[path] = row
        self._row_to_path[row] = path
        return True

    def _remove_selected(self):
        if self._converting:
            return
        for row in self.tree.selection():
            path = self._row_to_path.pop(row, None)
            if path:
                self._path_to_row.pop(path, None)
            self.tree.delete(row)
        self.status.set(f"Queue size: {len(self._row_to_path)}")

    def _clear_queue(self):
        if self._converting:
            return
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._path_to_row.clear()
        self._row_to_path.clear()
        self.progress.config(value=0)
        self.log.delete("1.0", "end")
        self.status.set("Queue cleared.")
        self._log("Queue cleared.")

    # ---------- Browse ----------
    def _pick_out(self):
        path = filedialog.askdirectory()
        if path:
            self.out_dir.set(path)

    def _pick_temp(self):
        path = filedialog.askdirectory()
        if path:
            self.temp_dir.set(path)

    # ---------- Batch control ----------
    def _request_cancel(self):
        if self._converting:
            self._cancel_requested = True
            self.status.set("Cancelling — waiting for current chunk...")

    def _start_batch(self, silent_validate=False):
        if self._converting:
            return
        items = [(row, self._row_to_path[row]) for row in self.tree.get_children()]
        if not items:
            if not silent_validate:
                messagebox.showwarning("Empty queue", "Add some PDF files first.")
            return
        try:
            dpi = int(self.dpi.get())
            quality = int(self.quality.get())
            workers = int(self.workers.get())
        except Exception:
            self.status.set("Invalid settings — fix DPI / Quality / Workers.")
            if not silent_validate:
                messagebox.showerror(
                    "Invalid settings",
                    "DPI, quality and workers must be whole numbers.",
                )
            return
        if not (10 <= quality <= 100):
            self.status.set("JPEG quality must be 10–100.")
            return
        if not (36 <= dpi <= 1200):
            self.status.set("DPI must be 36–1200.")
            return
        workers = max(1, min(workers, (os.cpu_count() or 2) * 2))

        out_root = self.out_dir.get().strip()
        temp_root = self.temp_dir.get().strip()
        grayscale = bool(self.grayscale.get())
        cap_dpi = bool(self.cap_dpi.get())
        colorspace = "gray" if grayscale else "rgb"

        for row, _ in items:
            self.tree.set(row, "status", "Pending")

        self._converting = True
        self._cancel_requested = False
        self._set_busy(True)
        self.log.delete("1.0", "end")
        self.progress.config(maximum=max(1, len(items)), value=0)
        self.status.set(f"Starting batch of {len(items)} file(s)...")

        threading.Thread(
            target=self._batch_worker,
            args=(items, out_root, dpi, quality, temp_root,
                  workers, colorspace, cap_dpi),
            daemon=True,
        ).start()

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.btn_convert.config(state=state)
        self.btn_add.config(state=state)
        self.btn_remove.config(state=state)
        self.btn_clear.config(state=state)
        self.btn_cancel.config(state="normal" if busy else "disabled")

    def _batch_worker(self, items, out_root, dpi, quality,
                      temp_root, workers, colorspace, cap_dpi):
        total_files = len(items)
        ctx = mp.get_context("spawn")
        try:
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
                for idx, (row, pdf) in enumerate(items, 1):
                    if self._cancel_requested:
                        self._ui(lambda r=row: self.tree.set(r, "status", "Cancelled"))
                        continue

                    self._ui(lambda r=row: self.tree.set(r, "status", "Converting..."))
                    self._ui(
                        lambda i=idx, t=total_files, n=os.path.basename(pdf):
                        self.status.set(f"File {i}/{t}: {n}")
                    )

                    out = out_root if out_root else os.path.dirname(pdf)

                    def log_cb(msg, _i=idx, _t=total_files, _n=os.path.basename(pdf)):
                        self._log(f"[{_i}/{_t}] {_n}: {msg}")

                    def progress_cb(done, total, _i=idx, _t=total_files):
                        def upd():
                            self.progress.config(maximum=max(1, total), value=done)
                            self.status.set(f"File {_i}/{_t} — page {done}/{total}")
                        self._ui(upd)

                    try:
                        cbz, skipped = self._convert_one(
                            pdf, out, dpi, quality, temp_root, workers, ex,
                            colorspace, cap_dpi,
                            log=log_cb,
                            set_progress=progress_cb,
                            ask_password=self._ask_password_threadsafe,
                        )
                        if skipped:
                            status_txt = (
                                f"Done ({skipped} p. skipped) → "
                                f"{os.path.basename(cbz)}"
                            )
                        else:
                            status_txt = f"Done → {os.path.basename(cbz)}"
                        self._ui(
                            lambda r=row, s=status_txt:
                            self.tree.set(r, "status", s)
                        )
                    except Exception as e:
                        if self._cancel_requested:
                            self._ui(
                                lambda r=row: self.tree.set(r, "status", "Cancelled")
                            )
                        else:
                            err = str(e)
                            self._ui(
                                lambda r=row, s=err:
                                self.tree.set(r, "status", f"Failed: {s}")
                            )
                            self._log(f"  FAILED: {e}")

                    self._ui(lambda v=idx: self.progress.config(value=v))
        except Exception as e:
            self._log(f"Batch error: {e}")
        finally:
            self._ui(self._end_batch)

    def _end_batch(self):
        cancelled = self._cancel_requested
        self._converting = False
        self._cancel_requested = False
        self._set_busy(False)

        rows = self.tree.get_children()
        done = sum(
            1 for r in rows if str(self.tree.set(r, "status")).startswith("Done")
        )
        failed = sum(
            1 for r in rows if str(self.tree.set(r, "status")).startswith("Failed")
        )
        cancelled_n = sum(
            1 for r in rows if str(self.tree.set(r, "status")).startswith("Cancelled")
        )

        if cancelled:
            self.status.set(
                f"Cancelled — done {done}, cancelled {cancelled_n}, failed {failed}."
            )
            messagebox.showinfo(
                "Cancelled",
                f"Batch cancelled.\n\nDone: {done}\nCancelled: {cancelled_n}"
                f"\nFailed: {failed}",
            )
        elif failed:
            self.status.set(f"Finished — {done} succeeded, {failed} failed.")
            messagebox.showwarning(
                "Finished with errors",
                f"Succeeded: {done}\nFailed: {failed}\n\nSee the queue for details.",
            )
        else:
            self.status.set(f"Batch complete — {done} file(s) converted.")
            messagebox.showinfo("Complete", f"All {done} file(s) converted.")

    # ---------- Single-file conversion (executor shared across batch) ----------
    def _convert_one(
        self,
        pdf, out, dpi, quality, temp_root, workers, executor,
        colorspace, cap_dpi,
        log, set_progress, ask_password,
    ):
        """Convert one PDF to a CBZ.

        Returns (cbz_path, skipped_page_count). Skipped pages are those that
        failed to render even after retries — the CBZ is still produced with
        the remaining pages.
        """
        created_files = []
        workdir = None
        workdir_created_here = False
        try:
            os.makedirs(out, exist_ok=True)

            if temp_root:
                if not os.path.isdir(temp_root):
                    os.makedirs(temp_root)
                    workdir_created_here = True
                workdir = temp_root
            else:
                workdir = tempfile.mkdtemp(prefix="pdf2cbz_")
                workdir_created_here = True

            log(f"Temp folder: {workdir}")
            log(f"Workers:     {workers}")
            log(f"Image caps:  height≤{MAX_HEIGHT_PX}px, "
                f"width≤{MAX_WIDTH_PX}px, "
                f"pixels≤{MAX_PIXELS // 1_000_000}M")

            probe = fitz.open(pdf)
            password = None
            try:
                if probe.needs_pass:
                    password = ask_password()
                    if password is None:
                        raise RuntimeError("Cancelled: password required.")
                    if not probe.authenticate(password):
                        raise RuntimeError("Incorrect password.")
                total = probe.page_count
            finally:
                probe.close()

            cbz_path = os.path.join(
                out, f"{os.path.splitext(os.path.basename(pdf))[0]}.cbz"
            )

            log(f"Opened:     {pdf}")
            log(f"Pages:      {total}")
            log(f"Output:     {cbz_path}")
            log(f"DPI:        {dpi}   Quality: {quality}   "
                f"Colorspace: {colorspace.upper()}")
            log(f"Cap DPI:    {'on (per-page source match)' if cap_dpi else 'off'}")
            log("-" * 60)

            pages = list(range(total))
            if workers <= 1 or total <= 1:
                chunks = [pages]
            else:
                chunk_size = max(1, math.ceil(total / (workers * 4)))
                chunks = [
                    pages[i:i + chunk_size] for i in range(0, total, chunk_size)
                ]

            args_list = [
                (pdf, password, chunk, workdir, dpi, quality,
                 colorspace, cap_dpi)
                for chunk in chunks
            ]

            page_path = {}
            done = 0
            page_errors = []
            futures = {executor.submit(_render_chunk, a): a[2] for a in args_list}
            try:
                for fut in as_completed(futures):
                    if self._cancel_requested:
                        for f in futures:
                            f.cancel()
                        raise RuntimeError("Cancelled by user")
                    res = fut.result()

                    # tuple: (i, fpath, w, h, eff_dpi,
                    #         native_dpi, src_w, src_h, cap_str, err)
                    for (i, fpath, w, h, eff_dpi,
                         native_dpi, src_w, src_h,
                         cap_str, err) in res:
                        if err is not None:
                            page_errors.append(err)
                            log(f"  [{i + 1}/{total}] SKIPPED — {err}")
                            continue
                        page_path[i] = fpath
                        created_files.append(fpath)
                        done += 1

                        info = (
                            f"  [{i + 1}/{total}] "
                            f"{os.path.basename(fpath)}  ({w}x{h})  "
                            f"@{eff_dpi} DPI"
                        )
                        parts = []
                        if native_dpi is not None:
                            parts.append(
                                f"source {src_w}×{src_h} ≈{native_dpi:.0f} DPI"
                            )
                        if cap_str:
                            parts.append(cap_str)
                        if parts:
                            info += "  [" + "; ".join(parts) + "]"
                        log(info)
                    set_progress(done, total)

                if page_errors and done == 0:
                    for f in futures:
                        f.cancel()
                    raise RuntimeError(
                        f"All {len(page_errors)} page(s) failed — "
                        f"first: {page_errors[0]}"
                    )
            except RuntimeError:
                raise
            except Exception:
                for f in futures:
                    f.cancel()
                raise

            if page_errors:
                log("-" * 60)
                log(
                    f"WARNING: {len(page_errors)} of {total} page(s) skipped — "
                    f"CBZ will be produced with {done} page(s)."
                )

            log("-" * 60)
            log(f"Packing CBZ: {cbz_path}")
            with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_STORED) as zf:
                for i in sorted(page_path.keys()):
                    fpath = page_path[i]
                    zf.write(fpath, arcname=os.path.basename(fpath))

            return (cbz_path, len(page_errors))
        finally:
            deleted = 0
            for fpath in created_files:
                try:
                    os.remove(fpath)
                    deleted += 1
                except OSError:
                    pass
            if deleted:
                log(f"Removed {deleted} temp JPG(s).")
            if workdir and workdir_created_here:
                try:
                    os.rmdir(workdir)
                    log(f"Removed temp folder: {workdir}")
                except OSError:
                    pass

    # ---------- Helpers ----------
    def _ask_password_threadsafe(self):
        result = {}
        ev = threading.Event()

        def ask():
            result["pw"] = simpledialog.askstring(
                "Password required",
                "This PDF is password protected.\nEnter password:",
                show="*",
                parent=self,
            )
            ev.set()

        self._ui(ask)
        ev.wait()
        return result.get("pw")

    def _ui(self, fn):
        self.after(0, fn)

    def _log(self, msg):
        self._ui(lambda: (self.log.insert("end", msg + "\n"), self.log.see("end")))


if __name__ == "__main__":
    mp.freeze_support()
    PDFtoJPGApp().mainloop()