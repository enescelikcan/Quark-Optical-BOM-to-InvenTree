"""
main.py

PyQt6 desktop app: select one or more Altium BOM (.xlsx) exports, and for
each one, create (or update) a matching "project" Assembly Part in
InvenTree with a BOM built from parts already in InvenTree.

Workflow per file:
    1. Parse the BOM file (bom_parser.parse_bom_file).
    2. Project name = the file's name (without extension).
    3. For every BOM line, look up an existing InvenTree Part whose name
       matches the line's Comment.
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
from pathlib import Path
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
from stock_report import write_stock_report

CONFIG_PATH = "config.json"
PROJECT_CATEGORY_NAME = "Projects"


# =====================================================================
# Dialogs (always shown on the GUI thread)
# =====================================================================


class MissingPartsDialog(QDialog):
    """Shown when one or more BOM lines could not be matched to an
    existing InvenTree part by name. Lets the user continue (those lines
    will simply be left out of the BOM) or cancel this file entirely."""

    def __init__(self, project_name: str, missing_lines: List[BomLine], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Unmatched parts in '{project_name}'")
        self.resize(700, 400)

        layout = QVBoxLayout(self)

        message = QLabel(
            f"{len(missing_lines)} part(s) in this BOM have no matching "
            f"name in InvenTree. They will be skipped if you continue.\n"
            f"Add them to InvenTree first if they should be included."
        )
        message.setWordWrap(True)
        layout.addWidget(message)

        table = QTableWidget(len(missing_lines), 2)
        table.setHorizontalHeaderLabels(["Comment", "Quantity"])
        for row, line in enumerate(missing_lines):
            table.setItem(row, 0, QTableWidgetItem(line.comment))
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

        # Fetch every InvenTree part ONCE and index it by name, instead of
        # doing a separate network request per BOM line. This is the
        # single biggest speedup: an 82-line BOM used to mean 82 round
        # trips just for matching -- now it's one, shared across every
        # file in this run.
        self.log.emit("Fetching parts from InvenTree for name matching...")
        name_index = self.client.build_name_index()
        matched_name_count = sum(len(parts) for parts in name_index.values())
        self.log.emit(f"Indexed {matched_name_count} part(s) by name.\n")

        for file_path in self.file_paths:
            try:
                self.process_file(file_path, category, name_index)
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

    def process_file(self, file_path: str, category, name_index: dict) -> None:
        project_name = project_name_from_file(file_path)
        self.log.emit(f"=== {project_name} ===")

        try:
            lines = parse_bom_file(file_path)
        except BomParseError as exc:
            self.log.emit(f"[ERROR] {exc}")
            return

        self.log.emit(f"Parsed {len(lines)} BOM line(s).")

        matched, missing = self.match_lines(lines, name_index)

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
            self.log.emit(f"[ERROR] Failed to add '{line.comment}': {exc}")

        self.log.emit(
            f"Imported {imported_count} BOM item(s) into "
            f"'{assembly.name}' ({len(missing)} skipped, {len(failures)} failed)."
        )

    def match_lines(
        self, lines: List[BomLine], name_index: dict
    ) -> Tuple[List[Tuple[BomLine, object]], List[BomLine]]:
        matched = []
        missing = []
        for line in lines:
            try:
                part = self.client.resolve_part_by_name(line.comment, name_index)
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
# Background worker: Check (read-only, never writes anything)
# =====================================================================


class CheckWorker(QObject):
    """Compares BOM files against what's already in InvenTree, without
    changing anything. Much simpler than ImportWorker: there's nothing
    to decide (no missing-parts confirmation, no update/new-version
    choice), because a read-only comparison can't conflict with
    anything -- so this worker needs no cross-thread decision signals
    at all, just `log` and `finished`.
    """

    log = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, file_paths: List[str]):
        super().__init__()
        self.file_paths = file_paths
        self.client: Optional[InventreeClient] = None

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

        self.log.emit("Fetching parts from InvenTree for name matching...")
        name_index = self.client.build_name_index()
        matched_name_count = sum(len(parts) for parts in name_index.values())
        self.log.emit(f"Indexed {matched_name_count} part(s) by name.")

        self.log.emit("Fetching part names for reporting...")
        pk_index = self.client.build_pk_index()
        self.log.emit("")

        for file_path in self.file_paths:
            try:
                self.check_file(file_path, category, name_index, pk_index)
            except Exception as exc:
                self.log.emit(f"[ERROR] Unexpected error checking '{file_path}': {exc}")
                self.log.emit(traceback.format_exc())
            self.log.emit("")  # blank line between files

        self.log.emit("Done.")
        self.finished.emit()

    def check_file(self, file_path: str, category, name_index: dict, pk_index: dict) -> None:
        project_name = project_name_from_file(file_path)
        self.log.emit(f"=== {project_name} ===")

        try:
            lines = parse_bom_file(file_path)
        except BomParseError as exc:
            self.log.emit(f"[ERROR] {exc}")
            return

        self.log.emit(f"Parsed {len(lines)} BOM line(s).")

        assembly = self.client.find_assembly_by_name(project_name, category)
        if assembly is None:
            self.log.emit(
                f"[WARNING] '{project_name}' was not found in InvenTree "
                f"-- it has not been imported yet."
            )
            return

        # What the file says (only for lines we can actually match to an
        # InvenTree part -- an unmatched line has no pk to compare with).
        file_contents: dict = {}   # part_pk -> (comment, quantity)
        unmatched_lines: List[BomLine] = []
        for line in lines:
            part = self.client.resolve_part_by_name(line.comment, name_index)
            if part is None:
                unmatched_lines.append(line)
            else:
                file_contents[part.pk] = (line.comment, line.quantity)

        # What InvenTree actually has right now.
        inventree_contents = self.client.get_bom_contents(assembly)  # part_pk -> quantity

        missing_in_inventree = []   # in file, not in InvenTree
        quantity_mismatches = []    # in both, different quantity
        for pk, (comment, file_qty) in file_contents.items():
            if pk not in inventree_contents:
                missing_in_inventree.append((comment, file_qty))
            elif inventree_contents[pk] != file_qty:
                quantity_mismatches.append((comment, file_qty, inventree_contents[pk]))

        extra_in_inventree = []     # in InvenTree, not in file
        for pk, qty in inventree_contents.items():
            if pk not in file_contents:
                part = pk_index.get(pk)
                label = part.name if part is not None else f"(unknown part, pk={pk})"
                extra_in_inventree.append((label, qty))

        if unmatched_lines:
            self.log.emit(
                f"[WARNING] {len(unmatched_lines)} BOM line(s) have no "
                f"matching name in InvenTree, skipped from comparison:"
            )
            for line in unmatched_lines:
                self.log.emit(f"    - {line.comment} (qty {line.quantity})")

        if not missing_in_inventree and not quantity_mismatches and not extra_in_inventree:
            self.log.emit("MATCH -- InvenTree's BOM matches the file exactly.")
            return

        if missing_in_inventree:
            self.log.emit(f"In file but missing from InvenTree ({len(missing_in_inventree)}):")
            for comment, qty in missing_in_inventree:
                self.log.emit(f"    - {comment} (qty {qty})")

        if quantity_mismatches:
            self.log.emit(f"Quantity mismatches ({len(quantity_mismatches)}):")
            for comment, file_qty, inventree_qty in quantity_mismatches:
                self.log.emit(
                    f"    - {comment}: file says {file_qty}, InvenTree has {inventree_qty}"
                )

        if extra_in_inventree:
            self.log.emit(f"In InvenTree but not in file ({len(extra_in_inventree)}):")
            for label, qty in extra_in_inventree:
                self.log.emit(f"    - {label} (qty {qty})")


# =====================================================================
# Background worker: Stock Report (read-only, writes a new .xlsx file
# next to the source BOM -- never touches InvenTree or the BOM itself)
# =====================================================================


class StockReportWorker(QObject):
    """For each selected BOM file, matches every line's Comment against
    InvenTree by name and writes a stock-availability report (.xlsx)
    next to the source file, via stock_report.write_stock_report().

    Simpler than CheckWorker: there's no assembly/BOM comparison here,
    just "for each line in this file, what's InvenTree's current
    In Stock quantity for that part" -- so this worker doesn't need to
    look up an Assembly Part at all, only match names and read
    part.in_stock, which is a field already present on the Part
    objects returned by build_name_index().
    """

    log = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, file_paths: List[str]):
        super().__init__()
        self.file_paths = file_paths
        self.client: Optional[InventreeClient] = None

    def run(self) -> None:
        try:
            config = InventreeConfig.load(CONFIG_PATH)
            self.client = InventreeClient(config)
        except Exception as exc:
            self.log.emit(f"[ERROR] Could not connect to InvenTree: {exc}")
            self.finished.emit()
            return

        self.log.emit("Connected to InvenTree.")
        self.log.emit("Fetching parts from InvenTree for name matching...")
        name_index = self.client.build_name_index()
        matched_name_count = sum(len(parts) for parts in name_index.values())
        self.log.emit(f"Indexed {matched_name_count} part(s) by name.")
        self.log.emit("")

        for file_path in self.file_paths:
            try:
                self.export_file(file_path, name_index)
            except Exception as exc:
                self.log.emit(f"[ERROR] Unexpected error processing '{file_path}': {exc}")
                self.log.emit(traceback.format_exc())
            self.log.emit("")  # blank line between files

        self.log.emit("Done.")
        self.finished.emit()

    def export_file(self, file_path: str, name_index: dict) -> None:
        project_name = project_name_from_file(file_path)
        self.log.emit(f"=== {project_name} ===")

        try:
            lines = parse_bom_file(file_path)
        except BomParseError as exc:
            self.log.emit(f"[ERROR] {exc}")
            return

        self.log.emit(f"Parsed {len(lines)} BOM line(s).")

        rows: list = []   # (comment, stock or None)
        unmatched_count = 0
        for line in lines:
            try:
                part = self.client.resolve_part_by_name(line.comment, name_index)
            except LookupError as exc:
                self.log.emit(f"[ERROR] {exc}")
                part = None
            if part is None:
                rows.append((line.comment, None))
                unmatched_count += 1
            else:
                rows.append((line.comment, part.in_stock))

        output_path = str(Path(file_path).parent / f"{project_name}_stock_report.xlsx")
        write_stock_report(output_path, rows)

        self.log.emit(
            f"Wrote stock report to '{output_path}' "
            f"({len(rows) - unmatched_count} matched, {unmatched_count} not found)."
        )


# =====================================================================
# Main window (GUI thread only)
# =====================================================================


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Quark Optical BOM to Inventree")
        self.resize(720, 480)

        self.selected_files: List[str] = []
        self.thread: Optional[QThread] = None
        self.worker: Optional[QObject] = None

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

        button_row = QHBoxLayout()
        self.start_button = QPushButton("Load to Inventree")
        self.start_button.clicked.connect(self.start_import)
        self.start_button.setEnabled(False)
        self.check_button = QPushButton("Check Loaded Project")
        self.check_button.clicked.connect(self.start_check)
        self.check_button.setEnabled(False)
        self.stock_report_button = QPushButton("Export Stock Report")
        self.stock_report_button.clicked.connect(self.start_stock_report)
        self.stock_report_button.setEnabled(False)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.check_button)
        button_row.addWidget(self.stock_report_button)
        layout.addLayout(button_row)

        log_row = QHBoxLayout()
        self.log_row_label = QLabel("Logs:")
        log_row.addWidget(self.log_row_label)
        layout.addLayout(log_row)

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
            self.check_button.setEnabled(True)
            self.stock_report_button.setEnabled(True)

    # -----------------------------------------------------------------
    # Processing: spin up a worker thread and wire up its signals.
    # Shared by Start (ImportWorker), Check (CheckWorker), and Export
    # Stock Report (StockReportWorker) -- they differ only in which
    # worker gets created and, for Start, two extra decision signals
    # that the other two workers don't have because a read-only
    # operation never needs to ask the user anything.
    # -----------------------------------------------------------------

    def _launch_worker(self, worker: QObject, extra_connections=None) -> None:
        self.start_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.stock_report_button.setEnabled(False)
        self.select_button.setEnabled(False)
        self.log_view.clear()

        self.thread = QThread()
        self.worker = worker
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.finished.connect(self.on_processing_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        for signal, slot in extra_connections or []:
            signal.connect(slot)

        self.thread.start()

    def start_import(self) -> None:
        worker = ImportWorker(self.selected_files)
        self._launch_worker(worker, extra_connections=[
            (worker.missing_parts_decision_needed, self.handle_missing_parts_decision),
            (worker.existing_project_decision_needed, self.handle_existing_project_decision),
        ])

    def start_check(self) -> None:
        worker = CheckWorker(self.selected_files)
        self._launch_worker(worker)

    def start_stock_report(self) -> None:
        worker = StockReportWorker(self.selected_files)
        self._launch_worker(worker)

    def on_processing_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.check_button.setEnabled(True)
        self.stock_report_button.setEnabled(True)
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
