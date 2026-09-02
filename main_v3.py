"""
main.py

PyQt6 desktop app: select one or more Altium BOM (.xlsx) exports, and for
each one, create (or update) a matching "project" Assembly Part in
InvenTree with a BOM built from parts already in InvenTree.

Workflow per file:
    1. Parse the BOM file (bom_parser.parse_bom_file).
    2. Project name = the file's name (without extension).
    3. For every BOM line, look up an existing InvenTree Part whose IPN
       matches the line's Manufacturer Part Number.
    4. If any lines have no match, show them to the user and ask whether
       to continue (ignoring the unmatched lines) or cancel this file.
    5. If a project with this name already exists, ask the user whether
       to update its existing BOM (replace it) or create a new
       versioned Assembly Part instead.
    6. Create/update the Assembly Part and add one BomItem per matched
       line, with quantity taken from the BOM's Quantity column.

Threading design
-----------------
All parsing and InvenTree API calls run on a background QThread
(ImportWorker), so the GUI never freezes -- this matters once you're
processing several files with dozens of API calls each.

Qt widgets (including QDialog) may only be created and shown on the
main/GUI thread, but the two confirmation dialogs are triggered by
decisions made *inside* process_file(), which runs on the worker
thread. To bridge that:

  - The worker emits a signal (e.g. missing_parts_decision_needed)
    carrying the data needed to build the dialog, plus a shared
    (result_dict, threading.Event) pair.
  - Because the worker lives on a different thread than MainWindow,
    PyQt automatically delivers that signal via a *queued* connection,
    so the connected slot runs on the GUI thread.
  - The GUI-thread slot shows the modal dialog, writes the user's
    choice into result_dict, and sets the Event.
  - Meanwhile, the worker thread is blocked on event.wait() right
    after emitting the signal, so it simply resumes once the Event is
    set, and reads its answer out of result_dict.

This keeps all Qt widget creation on the GUI thread while letting the
worker thread block-and-wait for a human decision, without any manual
locking beyond the two threading.Event objects.
"""

import sys
import threading
import traceback
from datetime import datetime
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from bom_parser import BomLine, BomParseError, parse_bom_file, project_name_from_file
from inventree_client import InventreeClient, InventreeConfig

CONFIG_PATH = "config.json"
PROJECT_CATEGORY_NAME = "Projects"


# =====================================================================
# Dialogs (always shown on the GUI thread)
# =====================================================================


class MissingPartsDialog(QDialog):
    """Shown when one or more BOM lines could not be matched to an
    existing InvenTree part by IPN. Lets the user continue (those lines
    will simply be left out of the BOM) or cancel this file entirely."""

    def __init__(self, project_name: str, missing_lines: List[BomLine], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Unmatched parts in '{project_name}'")
        self.resize(700, 400)

        layout = QVBoxLayout(self)

        message = QLabel(
            f"{len(missing_lines)} part(s) in this BOM have no matching "
            f"IPN in InvenTree. They will be skipped if you continue.\n"
            f"Add them to InvenTree first if they should be included."
        )
        message.setWordWrap(True)
        layout.addWidget(message)

        table = QTableWidget(len(missing_lines), 2)
        table.setHorizontalHeaderLabels(["Manufacturer Part Number", "Quantity"])
        for row, line in enumerate(missing_lines):
            table.setItem(row, 0, QTableWidgetItem(line.mpn))
            table.setItem(row, 1, QTableWidgetItem(str(line.quantity)))
        table.resizeColumnsToContents()
        layout.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continue without these parts")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancel this file")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ExistingProjectDialog(QDialog):
    """Shown when an Assembly Part with this project's name already
    exists. Lets the user choose to update its BOM in place, or create a
    new versioned Assembly Part alongside it."""

    UPDATE = "update"
    NEW_VERSION = "new_version"

    def __init__(self, project_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Project '{project_name}' already exists")
        self.choice: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"An Assembly Part named '{project_name}' already exists in "
            f"InvenTree. What would you like to do?"
        ))

        self.update_radio = QRadioButton("Update existing BOM (replace its current contents)")
        self.new_version_radio = QRadioButton("Create a new version instead")
        self.update_radio.setChecked(True)
        layout.addWidget(self.update_radio)
        layout.addWidget(self.new_version_radio)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        self.choice = self.UPDATE if self.update_radio.isChecked() else self.NEW_VERSION
        self.accept()


# =====================================================================
# Background worker (runs on a QThread, never touches widgets directly)
# =====================================================================


class ImportWorker(QObject):
    """Does all the parsing and InvenTree API work off the GUI thread.

    Emits `log` for anything that should appear in the log view, and
    the two `*_decision_needed` signals whenever it needs a human
    decision -- see the module docstring for how those are handled
    across threads.
    """

    log = pyqtSignal(str)
    finished = pyqtSignal()

    # (project_name, missing_lines, (result_dict, threading.Event))
    missing_parts_decision_needed = pyqtSignal(str, list, object)
    # (project_name, (result_dict, threading.Event))
    existing_project_decision_needed = pyqtSignal(str, object)

    def __init__(self, file_paths: List[str]):
        super().__init__()
        self.file_paths = file_paths
        self.client: Optional[InventreeClient] = None

    # -----------------------------------------------------------------
    # Entry point (connected to QThread.started)
    # -----------------------------------------------------------------

    def run(self) -> None:
        try:
            config = InventreeConfig.load(CONFIG_PATH)
            self.client = InventreeClient(config)
            category = self.client.get_category_by_name(PROJECT_CATEGORY_NAME)
        except Exception as exc:
            self.log.emit(f"[ERROR] Could not connect to InvenTree: {exc}")
            self.finished.emit()
            return

        self.log.emit(f"Connected to InvenTree. Using category '{PROJECT_CATEGORY_NAME}'.")

        # Fetch every InvenTree part ONCE and index it by IPN, instead of
        # doing a separate network request per BOM line. This is the
        # single biggest speedup: an 82-line BOM used to mean 82 round
        # trips just for matching -- now it's one, shared across every
        # file in this run.
        self.log.emit("Fetching parts from InvenTree for IPN matching...")
        ipn_index = self.client.build_ipn_index()
        matched_ipn_count = sum(len(parts) for parts in ipn_index.values())
        self.log.emit(f"Indexed {matched_ipn_count} part(s) by IPN.\n")

        for file_path in self.file_paths:
            try:
                self.process_file(file_path, category, ipn_index)
            except Exception as exc:
                self.log.emit(f"[ERROR] Unexpected error processing '{file_path}': {exc}")
                self.log.emit(traceback.format_exc())
            self.log.emit("")  # blank line between files

        self.log.emit("Done.")
        self.finished.emit()

    # -----------------------------------------------------------------
    # Cross-thread decision helpers
    # -----------------------------------------------------------------
    # Each of these emits a signal (handled on the GUI thread) and then
    # blocks *this* worker thread on a threading.Event until the GUI
    # thread has shown its dialog and recorded the user's answer.

    def ask_continue_without_missing_parts(
        self, project_name: str, missing_lines: List[BomLine]
    ) -> bool:
        result: dict = {}
        event = threading.Event()
        self.missing_parts_decision_needed.emit(project_name, missing_lines, (result, event))
        event.wait()
        return result.get("continue", False)

    def ask_existing_project_choice(self, project_name: str) -> Optional[str]:
        result: dict = {}
        event = threading.Event()
        self.existing_project_decision_needed.emit(project_name, (result, event))
        event.wait()
        return result.get("choice")

    # -----------------------------------------------------------------
    # Per-file processing
    # -----------------------------------------------------------------

    def process_file(self, file_path: str, category, ipn_index: dict) -> None:
        project_name = project_name_from_file(file_path)
        self.log.emit(f"=== {project_name} ===")

        try:
            lines = parse_bom_file(file_path)
        except BomParseError as exc:
            self.log.emit(f"[ERROR] {exc}")
            return

        self.log.emit(f"Parsed {len(lines)} BOM line(s).")

        matched, missing = self.match_lines(lines, ipn_index)

        if missing:
            if not self.ask_continue_without_missing_parts(project_name, missing):
                self.log.emit(f"Cancelled '{project_name}' -- no changes made.")
                return
            self.log.emit(f"Continuing without {len(missing)} unmatched part(s).")

        if not matched:
            self.log.emit(f"[ERROR] No parts could be matched for '{project_name}' -- nothing to import.")
            return

        assembly = self.resolve_assembly(project_name, category)
        if assembly is None:
            self.log.emit(f"Cancelled '{project_name}' -- no changes made.")
            return

        failures = self.client.add_bom_items_concurrently(assembly, matched)
        imported_count = len(matched) - len(failures)

        for (line, part), exc in failures:
            self.log.emit(f"[ERROR] Failed to add '{line.mpn}': {exc}")

        self.log.emit(
            f"Imported {imported_count} BOM item(s) into "
            f"'{assembly.name}' ({len(missing)} skipped, {len(failures)} failed)."
        )

    def match_lines(
        self, lines: List[BomLine], ipn_index: dict
    ) -> Tuple[List[Tuple[BomLine, object]], List[BomLine]]:
        matched = []
        missing = []
        for line in lines:
            try:
                part = self.client.resolve_part_by_ipn(line.mpn, ipn_index)
            except LookupError as exc:
                self.log.emit(f"[ERROR] {exc}")
                missing.append(line)
                continue
            if part is None:
                missing.append(line)
            else:
                matched.append((line, part))
        return matched, missing

    def resolve_assembly(self, project_name: str, category):
        """Returns the Assembly Part to write the BOM into, or None if
        the user cancelled."""
        existing = self.client.find_assembly_by_name(project_name, category)

        if existing is None:
            return self.client.create_assembly(project_name, category)

        choice = self.ask_existing_project_choice(project_name)
        if choice is None:
            return None

        if choice == ExistingProjectDialog.UPDATE:
            failures = self.client.clear_bom(existing)
            if failures:
                for bom_item, exc in failures:
                    self.log.emit(f"[ERROR] Could not delete existing BOM item: {exc}")
                self.log.emit(
                    f"[ERROR] '{project_name}' BOM was not fully cleared "
                    f"({len(failures)} item(s) left behind) -- aborting "
                    f"rather than adding new items on top of a stale BOM."
                )
                return None
            self.log.emit(f"Cleared existing BOM for '{project_name}'.")
            return existing

        new_name = self.client.next_version_name(project_name, category)
        self.log.emit(f"Creating new version '{new_name}'.")
        return self.client.create_assembly(new_name, category)


# =====================================================================
# Main window (GUI thread only)
# =====================================================================


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Altium BOM -> InvenTree Importer")
        self.resize(720, 480)

        self.selected_files: List[str] = []
        self.thread: Optional[QThread] = None
        self.worker: Optional[ImportWorker] = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        file_row = QHBoxLayout()
        self.select_button = QPushButton("Select BOM file(s)...")
        self.select_button.clicked.connect(self.select_files)
        self.selected_label = QLabel("No files selected.")
        file_row.addWidget(self.select_button)
        file_row.addWidget(self.selected_label, stretch=1)
        layout.addLayout(file_row)

        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start_processing)
        self.start_button.setEnabled(False)
        layout.addWidget(self.start_button)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, stretch=1)

    # -----------------------------------------------------------------
    # Logging helper
    # -----------------------------------------------------------------

    def append_log(self, message: str) -> None:
        # Timestamp every real message so we can tell how long each step
        # actually took -- but leave the blank-line separators between
        # files (emitted as "") untouched, so we don't end up with a
        # log full of empty "[12:34:56] " lines.
        if message:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_view.append(f"[{timestamp}] {message}")
        else:
            self.log_view.append(message)

    # -----------------------------------------------------------------
    # File selection
    # -----------------------------------------------------------------

    def select_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Altium BOM export(s)", "", "Excel files (*.xlsx)"
        )
        if files:
            self.selected_files = files
            names = ", ".join(f.split("/")[-1] for f in files)
            self.selected_label.setText(f"{len(files)} file(s) selected: {names}")
            self.start_button.setEnabled(True)

    # -----------------------------------------------------------------
    # Processing: spin up the worker thread and wire up its signals
    # -----------------------------------------------------------------

    def start_processing(self) -> None:
        self.start_button.setEnabled(False)
        self.select_button.setEnabled(False)
        self.log_view.clear()

        self.thread = QThread()
        self.worker = ImportWorker(self.selected_files)
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.missing_parts_decision_needed.connect(self.handle_missing_parts_decision)
        self.worker.existing_project_decision_needed.connect(self.handle_existing_project_decision)
        self.worker.finished.connect(self.on_processing_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()

    def on_processing_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.select_button.setEnabled(True)

    # -----------------------------------------------------------------
    # Decision slots -- these run on the GUI thread (the worker thread
    # is blocked on its threading.Event until we call event.set()).
    # -----------------------------------------------------------------

    def handle_missing_parts_decision(
        self, project_name: str, missing_lines: List[BomLine], payload
    ) -> None:
        result, event = payload
        dialog = MissingPartsDialog(project_name, missing_lines, self)
        result["continue"] = dialog.exec() == QDialog.DialogCode.Accepted
        event.set()

    def handle_existing_project_decision(self, project_name: str, payload) -> None:
        result, event = payload
        dialog = ExistingProjectDialog(project_name, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            result["choice"] = dialog.choice
        else:
            result["choice"] = None
        event.set()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
