"""PAGE PFP - Event-driven, well-typed version.

Requirements:
    pip install watchdog keyboard pyperclip win10toast rich
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional, List, Set

import pyperclip
import keyboard  # type: ignore
from watchdog.events import FileSystemEventHandler  # type: ignore
from watchdog.observers import Observer  # type: ignore
from rich.console import Console
from win10toast import ToastNotifier  # type: ignore
import re
import tkinter as tk
from tkinter import filedialog, messagebox

# ---------- typing aliases ----------
DownloadQueue = queue.Queue["DownloadEvent"]

# ---------- constants ----------
DEFAULT_DOWNLOADS: Final[Path] = Path.home() / "Downloads"

# ---------- console & toaster ----------
console: Console = Console()
print = console.print

console.clear()


@dataclass(frozen=True)
class DownloadEvent:
    """Represents a file creation event in the Downloads folder."""

    filename: str
    path: Path


class DownloadsHandler(FileSystemEventHandler):
    """Watchdog handler that pushes file creation events into a queue."""

    def __init__(self, downloads_q: DownloadQueue) -> None:
        super().__init__()
        self._q: DownloadQueue = downloads_q

    def on_created(
        self, event  # type: ignore
    ) -> None:  # watchdog Event has a complex type; keep it simple
        if event.is_directory:
            return
        created = Path(event.src_path)  # type: ignore
        try:
            self._q.put_nowait(DownloadEvent(filename=created.name, path=created))
        except queue.Full:
            # queue is bounded? default is infinite; keep safe guard
            console.print("[yellow]Downloads queue full — event dropped[/yellow]")


class PagePFP:
    """Main class that encapsulates the program logic and event-driven flow."""

    def __init__(
        self,
        downloads_dir: Path,
        images_dir: Path,
        names_file: Path = Path("names.txt"),
    ) -> None:
        self.downloads_dir: Path = downloads_dir
        self.images_dir: Path = images_dir
        self.names_file: Path = names_file

        self._downloads_q: DownloadQueue = queue.Queue()
        self._observer: Optional[Observer] = None  # type: ignore

        # synchronization events
        self._paste_event: threading.Event = threading.Event()
        self._skip_prompt_event: threading.Event = threading.Event()
        self._skip_wait_download_event: threading.Event = threading.Event()
        self._expected_clipboard_text: Optional[str] = None

        self._toaster: ToastNotifier = ToastNotifier()

        self._register_hotkeys()

    # ---- hotkeys ----
    def _register_hotkeys(self) -> None:
        """Register application-level hotkeys (global)."""
        keyboard.add_hotkey("ctrl+v", self._on_paste_hotkey)
        keyboard.add_hotkey("ctrl+r", self._on_skip_prompt_hotkey)
        keyboard.add_hotkey("ctrl+shift+c", self._on_skip_wait_download_hotkey)

    def _on_paste_hotkey(self) -> None:
        """Handler for Ctrl+V: verify clipboard equals expected string and set paste event."""
        current: str = pyperclip.paste()
        if self._expected_clipboard_text is None:
            return
        if current == self._expected_clipboard_text:
            self._paste_event.set()
        else:
            console.print(
                "[yellow]Conteúdo do clipboard não corresponde ao esperado.[/yellow]"
            )

    def _on_skip_prompt_hotkey(self) -> None:
        """Handler for Ctrl+R: skip the current prompt step."""
        self._skip_prompt_event.set()

    def _on_skip_wait_download_hotkey(self) -> None:
        """Handler for Ctrl+Shift+C: skip waiting for download."""
        self._skip_wait_download_event.set()

    # ---- watcher ----
    def start_downloads_watcher(self) -> None:
        """Start the watchdog observer for the downloads folder."""
        handler = DownloadsHandler(self._downloads_q)
        observer = Observer()
        observer.schedule(handler, str(self.downloads_dir), recursive=False)
        observer.start()
        self._observer = observer
        console.print(
            f"[dim]Observando {self.downloads_dir} por novos arquivos...[/dim]"
        )

    def stop_downloads_watcher(self) -> None:
        """Stop observer if running."""
        if self._observer is not None:  # type: ignore
            self._observer.stop()  # type: ignore
            self._observer.join()  # type: ignore
            self._observer = None

    # ---- IO and helpers ----
    def read_names(self) -> List[str]:
        """Read names file and return non-empty, stripped lines."""
        if not self.names_file.exists():
            raise FileNotFoundError(f"{self.names_file} não encontrado.")
        content: str = self.names_file.read_text(encoding="utf-8")
        names: List[str] = [n.strip() for n in re.split(r"\n+", content) if n.strip()]
        return names

    def _wait_for_user_paste_or_skip(
        self, expected_text: str, timeout: Optional[float] = None
    ) -> bool:
        """Wait until user pastes the expected text (Ctrl+V) or presses skip (Ctrl+R).
        Returns True when the paste was confirmed, False when skipped or timed out."""
        self._expected_clipboard_text = expected_text
        self._paste_event.clear()
        self._skip_prompt_event.clear()

        console.print(
            "\nAguardando o conteúdo ser colado (Ctrl+V) ou Ctrl+R para pular.\n"
        )

        start = time.time()
        while True:
            if self._paste_event.is_set():
                self._expected_clipboard_text = None
                return True
            if self._skip_prompt_event.is_set():
                self._expected_clipboard_text = None
                return False
            if timeout is not None and (time.time() - start) > timeout:
                self._expected_clipboard_text = None
                return False
            time.sleep(0.05)

    def _wait_for_new_png_download(
        self, original_snapshot: Set[str], timeout: Optional[float] = None
    ) -> Optional[Path]:
        """Wait for a new .png file event that is not in the original snapshot.
        Returns Path to the new file or None if skipped/timed out."""
        self._skip_wait_download_event.clear()
        start = time.time()

        while True:
            if self._skip_wait_download_event.is_set():
                return None
            try:
                evt: DownloadEvent = self._downloads_q.get(timeout=0.1)
            except queue.Empty:
                if timeout is not None and (time.time() - start) > timeout:
                    return None
                continue

            if not evt.filename.lower().endswith(".png"):
                continue

            if evt.filename in original_snapshot:
                continue

            full_path: Path = evt.path

            # skip suspicious short or random-looking filenames (likely temporary)
            if len(evt.filename) < 8 or re.match(
                r"^[A-Za-z0-9]{6,12}\.png$", evt.filename
            ):
                time.sleep(1.0)  # wait a bit; browser might rename it soon
                possible_new = list(evt.path.parent.glob("*.png"))
                # pick the newest file that isn't in snapshot
                candidates = [
                    p for p in possible_new if p.name not in original_snapshot
                ]
                if candidates:
                    latest = max(candidates, key=lambda p: p.stat().st_mtime)
                    if self._wait_until_file_is_ready(latest, max_wait=5.0):
                        return latest
                continue

            if self._wait_until_file_is_ready(full_path, max_wait=5.0):
                return full_path

    @staticmethod
    def _wait_until_file_is_ready(path: Path, max_wait: float = 5.0) -> bool:
        """Try opening file to ensure it's finished writing."""
        start = time.time()
        while True:
            try:
                if not path.exists():
                    return False
                with open(path, "rb"):
                    return True
            except (PermissionError, OSError):
                if (time.time() - start) > max_wait:
                    return False
                time.sleep(0.1)

    def move_and_rename_file(self, downloaded_path: Path, target_name: str) -> bool:
        """Move downloaded file into images_dir with a safe name (appends counter if needed)."""
        self.images_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{target_name}.png"
        dest = self.images_dir / safe_name
        counter = 1
        while dest.exists():
            dest = self.images_dir / f"{target_name} {counter}.png"
            counter += 1
        try:
            downloaded_path.rename(dest)
            console.print(
                f"[green]Arquivo '{downloaded_path.name}' movido e renomeado para '{dest}'[/green]"
            )
            return True
        except Exception as exc:
            console.print(f"[red]Erro ao mover '{downloaded_path}': {exc}[/red]")
            return False

    # ---- main flow ----
    def run(self) -> None:
        """Run the main flow: copy prompts, wait for downloads, rename, then copy file paths."""
        try:
            original_snapshot: Set[str] = set(os.listdir(self.downloads_dir))
            self.start_downloads_watcher()

            names: List[str] = self.read_names()
            self.console_rule("Iniciando cópia de prompts")

            for index, name in enumerate(names):
                count_display = f"{index + 1}/{len(names)}"
                prompt = f"Create a '{name}' logo"

                pyperclip.copy(prompt)
                console.print(
                    f'\n{count_display}: Prompt "{prompt}" copiado. (Ctrl+R para pular)'
                )
                proceeded: bool = self._wait_for_user_paste_or_skip(prompt)

                if not proceeded:
                    console.print("[yellow]Prompt pulado pelo usuário.[/yellow]")
                    continue

                console.print(
                    "Aguardando arquivo ser baixado (pressione Ctrl+Shift+C para continuar sem download)\n"
                )
                new_png = self._wait_for_new_png_download(
                    original_snapshot, timeout=None
                )

                if new_png is None:
                    console.print(
                        "[yellow]Nenhum arquivo novo encontrado. Pulando renomeação.[/yellow]"
                    )
                    original_snapshot = set(os.listdir(self.downloads_dir))
                    continue

                moved_ok: bool = self.move_and_rename_file(new_png, name)
                original_snapshot = set(os.listdir(self.downloads_dir))

                if not moved_ok:
                    console.print(f"[red]Falha ao mover '{new_png.name}'[/red]")
                    continue

                if index + 1 == len(names):
                    self._toaster.show_toast("PFP", "Última página copiada")  # type: ignore

                console.rule()

            # Copy image paths step
            self.console_rule("Copiar caminhos das imagens")
            for index, name in enumerate(names):
                page_path = self.images_dir / f"{name}.png"
                if not page_path.exists():
                    matches = list(self.images_dir.glob(f"{name}*.png"))
                    if matches:
                        page_path = matches[0]
                    else:
                        console.print(
                            f"[red]Arquivo para '{name}' não encontrado em {self.images_dir}[/red]"
                        )
                        continue

                pyperclip.copy(str(page_path))
                self._expected_clipboard_text = str(page_path)
                console.print(
                    f"\n{index + 1}/{len(names)}: Caminho '{page_path}' copiado (Ctrl+V para confirmar, Ctrl+R para pular)."
                )
                proceeded_path: bool = self._wait_for_user_paste_or_skip(str(page_path))
                if not proceeded_path:
                    console.print("[yellow]Caminho pulado pelo usuário.[/yellow]")
                time.sleep(0.15)

                console.rule()

            console.print("\n[green]Processo finalizado.[/green]")

        finally:
            self.stop_downloads_watcher()

    @staticmethod
    def console_rule(title: str) -> None:
        console.rule(f"[bold]{title}[/bold]")


# ---- interactive helpers for folder picking ----
def ask_or_browse_for_directory(
    prompt_text: str, default: Optional[Path] = None
) -> Path:
    """Ask user for directory path or open the native folder browser when they press Enter.
    If path doesn't exist, ask to create it using a yes/no dialog.
    """
    root = tk.Tk()
    root.withdraw()  # hide main window

    while True:
        console.print(f"\n{prompt_text}")
        console.print(
            "[dim]Digite um caminho e tecle Enter, ou apenas tecle Enter para abrir o explorador[/dim]"
        )
        raw = input("> ").strip()

        if raw == "":
            initialdir = str(default) if default else None
            selected = filedialog.askdirectory(
                title="Escolha uma pasta", initialdir=initialdir
            )
            if not selected:
                console.print(
                    "[yellow]Nenhuma pasta selecionada. Tente novamente ou pressione Ctrl+C para sair.[/yellow]"
                )
                continue
            p = Path(selected)
        else:
            p = Path(os.path.normpath(raw))

        if p.exists():
            if p.is_dir():
                return p
            else:
                console.print("[red]O caminho especificado não é uma pasta.[/red]")
                continue

        # not exists -> ask to create
        create = messagebox.askyesno(
            "Criar pasta?", f"A pasta '{p}' não existe. Deseja criá-la?"
        )
        if create:
            try:
                p.mkdir(parents=True, exist_ok=True)
                console.print(f"[green]Pasta criada: {p}[/green]")
                return p
            except Exception as exc:
                console.print(f"[red]Falha ao criar a pasta: {exc}[/red]")
                continue
        else:
            console.print("[yellow]Pasta não criada. Escolha outro caminho.[/yellow]")


# ---- script entrypoint ----
def main() -> None:
    try:
        print("[bold]PAGE PFP - Typed event-driven version[/bold]\n")
        downloads_dir: Path = (
            DEFAULT_DOWNLOADS
            if DEFAULT_DOWNLOADS.exists() and DEFAULT_DOWNLOADS.is_dir()
            else ask_or_browse_for_directory(
                "Onde está a pasta Downloads? (ou tecle Enter para Buscar)",
                default=DEFAULT_DOWNLOADS,
            )
        )
        images_dir: Path = ask_or_browse_for_directory(
            "Onde estão localizadas as fotos de perfil? (ou tecle Enter para Buscar)",
            default=Path.home(),
        )
        app = PagePFP(downloads_dir=downloads_dir, images_dir=images_dir)
        app.run()
    except KeyboardInterrupt:
        print("\n[red]Interrompido pelo usuário.[/red]")


if __name__ == "__main__":
    main()
