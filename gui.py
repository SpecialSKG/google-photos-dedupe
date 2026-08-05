"""Interfaz gratuita para google-photos-dedupe (tkinter, sin dependencias extra)."""

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Google Photos - Deduplicador")
        self.geometry("860x640")
        self.minsize(720, 520)

        self.runner = None
        self.output_queue = queue.Queue()

        self._build_ui()
        self.cfg_var.trace_add("write", self._on_cfg_changed)
        self._on_cfg_changed()
        self.after(100, self._poll_output)

    # ---------- UI ----------
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        # Row 0: Config YAML file
        ttk.Label(top, text="Archivo de configuración (config.yaml):").grid(row=0, column=0, sticky="w")
        self.cfg_var = tk.StringVar(value=str(ROOT / "config.yaml"))
        ttk.Entry(top, textvariable=self.cfg_var, width=60).grid(row=0, column=1, padx=6)
        ttk.Button(top, text="…", width=3, command=self._browse_cfg).grid(row=0, column=2)
        ttk.Button(top, text="Abrir", width=6, command=self._open_cfg).grid(row=0, column=3, padx=(6, 0))

        # Row 1: Inputs Frame (directories list)
        inputs_frame = ttk.LabelFrame(top, text="Directorios de entrada (inputs) — se cargan de config.yaml", padding=6)
        inputs_frame.grid(row=1, column=0, columnspan=4, sticky="we", pady=(8, 0))

        self.inputs_listbox = tk.Listbox(inputs_frame, height=3, selectmode="single")
        self.inputs_listbox.pack(side="left", fill="both", expand=True, padx=(0, 6))

        btn_sub = ttk.Frame(inputs_frame)
        btn_sub.pack(side="right", fill="y")
        ttk.Button(btn_sub, text="Añadir folder", command=self._add_input).pack(fill="x", pady=2)
        ttk.Button(btn_sub, text="Quitar", command=self._remove_input).pack(fill="x", pady=2)

        # Row 2: Action
        ttk.Label(top, text="Acción:").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        self.action_var = tk.StringVar(value="dry-run")
        action_frame = ttk.Frame(top)
        action_frame.grid(row=2, column=1, sticky="w", pady=(4, 0))
        for text, value in [
            ("Dry-run (preview, seguro)", "dry-run"),
            ("Copy (copia sin tocar exports)", "copy"),
            ("Move (mueve, destruye la fuente)", "move"),
        ]:
            ttk.Radiobutton(action_frame, text=text, value=value, variable=self.action_var).pack(anchor="w")

        # Row 3: Buttons Exec / Cancel
        btn_frame = ttk.Frame(top)
        btn_frame.grid(row=3, column=1, sticky="w", pady=(10, 0))
        self.confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            btn_frame,
            text="Confirmar MOVE (no se puede deshacer)",
            variable=self.confirm_var,
        ).pack(side="left")
        ttk.Button(btn_frame, text="Ejecutar", command=self._run).pack(side="left", padx=(18, 6))
        ttk.Button(btn_frame, text="Cancelar", command=self._cancel).pack(side="left")

        # Row 4: Shortcuts Open Folders
        view_frame = ttk.Frame(top)
        view_frame.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(view_frame, text="Abrir logs (última ejecución)", command=lambda: self._open_path(self._latest_run() / "LOGS")).pack(side="left")
        ttk.Button(view_frame, text="Abrir reportes (última ejecución)", command=lambda: self._open_path(self._latest_run() / "REPORTS")).pack(side="left", padx=6)
        ttk.Button(view_frame, text="Abrir carpeta de salida", command=lambda: self._open_path(self._latest_run())).pack(side="left")

        # Row 5: Advanced Options (Labelframe)
        adv = ttk.LabelFrame(top, text="Opciones avanzadas (opcional) — vacío = usar config.yaml", padding=8)
        adv.grid(row=5, column=0, columnspan=4, sticky="we", pady=(8, 0))

        ttk.Label(adv, text="Carpeta de salida (out-dir):").grid(row=0, column=0, sticky="w")
        self.outdir_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.outdir_var, width=34).grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(adv, text="Modo (mode):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.mode_var = tk.StringVar(value="(usar config)")
        ttk.Combobox(adv, textvariable=self.mode_var, width=32, state="readonly",
                     values=["(usar config)", "exact", "perceptual", "exact+perceptual"]).grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(adv, text="Umbral pHash (phash-threshold):").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.phash_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.phash_var, width=34).grid(row=2, column=1, sticky="w", padx=6)

        ttk.Label(adv, text="Workers (subprocesos):").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.workers_var = tk.StringVar()
        ttk.Entry(adv, textvariable=self.workers_var, width=34).grid(row=3, column=1, sticky="w", padx=6)

        self.keep_structure_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(adv, text="Keep structure (anidar subcarpetas)", variable=self.keep_structure_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Bottom text output and status
        self.out = scrolledtext.ScrolledText(self, height=22, state="disabled", font=("Consolas", 10))
        self.out.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        self.status = tk.StringVar(value="Listo.")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=10, pady=(0, 8))

    # ---------- helpers ----------
    def _browse_cfg(self):
        path = filedialog.askopenfilename(
            title="Seleccionar config",
            filetypes=[("YAML", "*.yaml *.yml"), ("Todos", "*.*")],
            initialdir=str(ROOT),
        )
        if path:
            self.cfg_var.set(path)

    def _open_cfg(self):
        args = f'start "" "{self.cfg_var.get()}"'
        try:
            subprocess.Popen(args, shell=True)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _open_path(self, path: Path, *other):
        path = Path(path)
        if not path.exists():
            messagebox.showinfo("No existe", f"No existe: {path}")
            return
        subprocess.Popen(f'explorer "{path}"')

    def _out_base(self) -> Path:
        base = ROOT / "output_consolidado_struct"
        cfg = self.cfg_var.get().strip()
        try:
            if cfg and Path(cfg).exists():
                import yaml
                with open(cfg, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if data and data.get("out_dir"):
                    p = Path(str(data["out_dir"]))
                    base = p if p.is_absolute() else (ROOT / p)
        except Exception:
            pass
        out = self.outdir_var.get().strip()
        if out:
            p = Path(out)
            base = p if p.is_absolute() else (ROOT / p)
        return base

    def _latest_run(self) -> Path:
        base = self._out_base()
        if not base.is_dir():
            return base
        runs = sorted(
            (p for p in base.iterdir() if p.is_dir() and p.name.startswith("run_")),
            key=lambda p: p.name,
        )
        return runs[-1] if runs else base

    def _on_cfg_changed(self, *args):
        cfg = self.cfg_var.get().strip()
        self.inputs_listbox.delete(0, "end")
        if not cfg or not Path(cfg).exists():
            return
        try:
            import yaml
            with open(cfg, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and "inputs" in data:
                for inp in data["inputs"]:
                    self.inputs_listbox.insert("end", str(inp))
        except Exception:
            pass

    def _add_input(self):
        path = filedialog.askdirectory(title="Seleccionar carpeta de entrada (input)", initialdir=str(ROOT))
        if path:
            self.inputs_listbox.insert("end", path)

    def _remove_input(self):
        sel = self.inputs_listbox.curselection()
        if sel:
            self.inputs_listbox.delete(sel[0])

    def _log(self, text: str):
        self._append(text + "\n")

    def _append(self, text: str):
        self.out.configure(state="normal")
        self.out.insert("end", text)
        self.out.see("end")
        self.out.configure(state="disabled")

    # ---------- execution ----------
    def _build_cmd(self, cfg, action):
        py = str(PY or sys.executable)
        cmd = [py, "-m", "photos_dedupe", "--config", cfg, "--action", action]
        inputs = self.inputs_listbox.get(0, "end")
        if inputs:
            cmd += ["--inputs"] + list(inputs)
        out = self.outdir_var.get().strip()
        if out:
            cmd += ["--out-dir", out]
        mode = self.mode_var.get().strip()
        if mode and mode != "(usar config)":
            cmd += ["--mode", mode]
        ph = self.phash_var.get().strip()
        if ph:
            cmd += ["--phash-threshold", ph]
        wk = self.workers_var.get().strip()
        if wk:
            cmd += ["--workers", wk]
        if self.keep_structure_var.get():
            cmd.append("--keep-structure")
        if action == "move" and self.confirm_var.get():
            cmd.append("--confirm-move")
        return cmd

    def _run(self):
        cfg = self.cfg_var.get().strip()
        if not cfg or not Path(cfg).exists():
            messagebox.showerror("Config", "Seleccioná un archivo de configuración válido.")
            return
        action = self.action_var.get()
        if action == "move" and not self.confirm_var.get():
            if not messagebox.askyesno(
                "Move destructivo",
                "MOVER quitará archivos de los exports al out_dir y no se puede deshacer.\n¿Confirmás?",
            ):
                return
            self.confirm_var.set(True)
        if PY is not None and not PY.exists():
            messagebox.showerror("Entorno", f"No se encontró el venv en:\n{PY}")
            return

        cmd = self._build_cmd(cfg, action)
        self._append(f"\n$ {' '.join(cmd)}\n")
        self.runner = Runner(cmd, self.output_queue)
        self.runner.start()
        self.status.set(f"Ejecutando ({action})…")

    def _cancel(self):
        if self.runner:
            self.runner.terminate()
            self.status.set("Cancelado.")

    def _poll_output(self):
        try:
            while True:
                line = self.output_queue.get_nowait()
                self._append(line)
                self.status.set("Ejecutando…")
        except queue.Empty:
            pass
        if self.runner and not self.runner.is_alive() and self.runner.done is False:
            self.runner.done = True
            if self.runner.returncode == 0:
                self.status.set("Proceso finalizado correctamente.")
            elif self.runner.returncode == 2:
                self.status.set("Move cancelado: requiere --confirm-move.")
            else:
                self.status.set(f"Proceso finalizado con código {self.runner.returncode}")
        self.after(100, self._poll_output)


class Runner(threading.Thread):
    def __init__(self, cmd, q):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.q = q
        self.proc = None
        self.returncode = None
        self.done = False

    def run(self):
        self.proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in self.proc.stdout:
            self.q.put(line)
        self.proc.wait()
        self.returncode = self.proc.returncode
        self.q.put(f"\n>> Exit code: {self.returncode}\n")

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()