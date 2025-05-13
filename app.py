import sys
import io
import json
import csv
import ssl
import tempfile
import shutil
from pathlib import Path
from functools import partial
from datetime import datetime, timedelta
from urllib.parse import quote

import httplib2
import pickle
from googleapiclient import errors
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

import google_auth_httplib2
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTableWidget, QTableWidgetItem,
    QPushButton, QFileDialog, QInputDialog, QMessageBox,
    QVBoxLayout, QHBoxLayout, QWidget, QDialog, QDialogButtonBox,
    QComboBox, QLabel, QSplitter, QLineEdit, QDateEdit,
    QProgressDialog, QTextEdit
)
from PyQt6.QtCore import (
    Qt, QUrl, QDate, QObject, QRunnable, QThreadPool,
    pyqtSignal, pyqtSlot, QSettings, QTimer
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings

# ─── CONFIG ────────────────────────────────────────────────────────────────
# Path to your Google service account JSON file (must be created in GCP & shared
# with the target Drive folder).
SERVICE_ACCOUNT_FILE = Path("path/to/your-service-account.json")

# Path to your OAuth 2.0 client secrets JSON file (for user consent).
# You must download this from the Google Cloud Console and name it (or update
# the constant below).
OAUTH_CREDENTIALS_FILE = Path("path/to/your-oauth-credentials.json")

# Google Drive folder ID where all topic subfolders will live.
SHARED_ROOT_FOLDER_ID = "<YOUR_SHARED_ROOT_FOLDER_ID>"

# Your personal email address (also your default Calendar ID).
USER_EMAIL = "<YOUR_EMAIL_ADDRESS>"

# Filenames for the on-Drive CSV logs. You can change these names if you like,
# but keep the same filenames locally.
CSV_FILENAME = "review_log.csv"
STUDY_LOG_FILENAME = "study_log.csv"
RECORDS_FOLDER_NAME = "records"

REVIEW_FIELDS = [
    "topic", "files", "last_review", "next_review",
    "calendar_event_id", "drive_folder_id"
]
LOG_FIELDS = ["topic", "review_date", "difficulty", "comment"]

# Local cache directory and path to PDF.js viewer shipped alongside this script.
LOCAL_CACHE = Path("local_records")
PDFJS_VIEWER = Path(__file__).parent / "pdfjs" / "web" / "viewer.html"

# ─── DRIVE UPLOADER ─────────────────────────────────────────────────────
class DriveUploader:
    def __init__(self, creds, root_folder_id):
        raw_http = httplib2.Http()
        auth_http = google_auth_httplib2.AuthorizedHttp(creds, http=raw_http)
        self.drive = build("drive", "v3", http=auth_http, cache_discovery=False)
        self.root_id = root_folder_id
        self.records_id = self._get_or_create_folder(RECORDS_FOLDER_NAME, root_folder_id)
        self.csv_id = self._get_or_create_file(CSV_FILENAME, REVIEW_FIELDS, prepopulate=True)
        self.log_id = self._get_or_create_file(STUDY_LOG_FILENAME, LOG_FIELDS, prepopulate=False)

    def _get_or_create_folder(self, name, parent_id):
        q = (
            f"'{parent_id}' in parents and name='{name}' "
            "and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        res = self.drive.files().list(q=q, fields="files(id)").execute().get("files", [])
        if res:
            return res[0]["id"]
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
        return self.drive.files().create(body=meta, fields="id").execute()["id"]

    def _get_or_create_file(self, name, fields, prepopulate=False):
        # Look for an existing file in the records folder
        q = f"'{self.records_id}' in parents and name='{name}' and trashed=false"
        found = self.drive.files().list(q=q, fields="files(id)").execute().get("files", [])
        if found:
            return found[0]["id"]

        # Create a fresh CSV with header (and optionally a first pass of topics)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        if prepopulate and name == CSV_FILENAME:
            for fld in self.list_topic_folders():
                files_meta = []
                for f in self.list_files_in_folder(fld["id"]):
                    files_meta.append({
                        "id": f["id"],
                        "name": f["name"],
                        "link": f"https://drive.google.com/uc?export=download&id={f['id']}"
                    })
                writer.writerow({
                    "topic": fld["name"],
                    "files": json.dumps(files_meta),
                    "last_review": "",
                    "next_review": datetime.utcnow().date().isoformat(),
                    "calendar_event_id": "",
                    "drive_folder_id": fld["id"],
                })

        media = MediaIoBaseUpload(io.BytesIO(buf.getvalue().encode()), mimetype="text/csv")
        meta = {"name": name, "parents": [self.records_id], "mimeType": "text/csv"}
        newf = self.drive.files().create(body=meta, media_body=media, fields="id").execute()
        return newf["id"]

    def list_topic_folders(self):
        q = (
            f"'{self.root_id}' in parents and "
            "mimeType='application/vnd.google-apps.folder' and name!='records' and trashed=false"
        )
        files = self.drive.files().list(q=q, fields="files(id,name)").execute()
        return files.get("files", [])

    def list_files_in_folder(self, folder_id):
        q = f"'{folder_id}' in parents and trashed=false"
        resp = self.drive.files().list(q=q, fields="files(id,name)").execute()
        return resp.get("files", [])

    def read_csv(self):
        return self._read_file(self.csv_id)

    def write_csv(self, rows):
        self._write_file(self.csv_id, REVIEW_FIELDS, rows)

    def read_log(self):
        return self._read_file(self.log_id)

    def _read_file(self, file_id):
        req = self.drive.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)
        return list(csv.DictReader(io.StringIO(buf.read().decode())))

    def _write_file(self, file_id, fields, rows):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        filtered = [{k: v for k, v in row.items() if k in fields} for row in rows]
        writer.writerows(filtered)
        media = MediaIoBaseUpload(io.BytesIO(buf.getvalue().encode()), mimetype="text/csv")
        self.drive.files().update(fileId=file_id, media_body=media).execute()

    def append_log(self, entry):
        logs = self.read_log()
        logs.append(entry)
        self._write_file(self.log_id, LOG_FIELDS, logs)

    def create_topic_folder(self, name):
        return self._get_or_create_folder(name, self.root_id)

    def upload_file(self, path, folder_id):
        media = MediaFileUpload(path, resumable=True)
        meta = {"name": Path(path).name, "parents": [folder_id]}
        info = self.drive.files().create(body=meta, media_body=media, fields="id,name").execute()
        link = f"https://drive.google.com/uc?export=download&id={info['id']}"
        return info["id"], info["name"], link

    def delete_file(self, file_id):
        self.drive.files().delete(fileId=file_id).execute()

    def delete_folder(self, folder_id):
        try:
            self.drive.files().delete(fileId=folder_id).execute()
        except Exception:
            pass

    def download_file_to_path(self, file_id: str, dest_path: str):
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        try:
            req = self.drive.files().get_media(fileId=file_id)
            with open(dest_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, req)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
        except errors.HttpError as e:
            if e.resp.status != 404:
                raise

# ─── CALENDAR MANAGER ─────────────────────────────────────────────────────
class CalendarManager:
    def __init__(self, creds, calendar_id):
        raw_http = httplib2.Http(disable_ssl_certificate_validation=True)
        auth_http = google_auth_httplib2.AuthorizedHttp(creds, http=raw_http)
        self.cal = build("calendar", "v3", http=auth_http, cache_discovery=False)
        self.cal_id = calendar_id

    def create_event(self, topic, date_str):
        try:
            dt1 = datetime.strptime(date_str, "%Y-%m-%d").date()
            start_dt = datetime.combine(dt1, datetime.min.time()) + timedelta(hours=9)
            end_dt = start_dt + timedelta(minutes=30)
        except Exception:
            return None

        event = self.cal.events().insert(
            calendarId=self.cal_id,
            body={
                "summary": f"Review: {topic}",
                "description": f"Scheduled review for topic '{topic}'",
                "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Rome"},
                "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Europe/Rome"},
                "attendees": [{"email": USER_EMAIL}],
                "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 0}]}
            },
            sendUpdates="all"
        ).execute()
        return event.get("id")

    def delete_event(self, event_id):
        try:
            self.cal.events().delete(calendarId=self.cal_id, eventId=event_id).execute()
        except Exception:
            pass

    def delete_future_events(self, topic: str):
        now = datetime.utcnow().isoformat() + "Z"
        token = None
        while True:
            resp = self.cal.events().list(calendarId=self.cal_id, timeMin=now, pageToken=token).execute()
            for ev in resp.get("items", []):
                if ev.get("summary") == f"Review: {topic}":
                    try:
                        self.cal.events().delete(calendarId=self.cal_id, eventId=ev["id"]).execute()
                    except Exception:
                        pass
            token = resp.get("nextPageToken")
            if not token:
                break

# ─── THREADING ───────────────────────────────────────────────────────────
class TaskSignals(QObject):
    finished = pyqtSignal(object)
    error    = pyqtSignal(str)

class Worker(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn = fn
        self.args = args
        self.signals = TaskSignals()

    @pyqtSlot()
    def run(self):
        try:
            res = self.fn(*self.args)
        except Exception as e:
            self.signals.error.emit(str(e))
            return
        self.signals.finished.emit(res)

# ─── SETTINGS DIALOG ──────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    def __init__(self, files, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File Settings")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a file to delete:"))
        self.combo = QComboBox()
        self.combo.addItems([f["name"] for f in files])
        layout.addWidget(self.combo)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_index(self):
        return self.combo.currentIndex()

# ─── DASHBOARD DIALOG ─────────────────────────────────────────────────────
class DashboardDialog(QDialog):
    def __init__(self, stats, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dashboard")
        layout = QVBoxLayout(self)
        for k, v in stats.items():
            layout.addWidget(QLabel(f"{k}: {v}"))
        btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn.rejected.connect(self.reject)
        layout.addWidget(btn)

# OAuth & credential helpers
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

def get_user_credentials():
    creds = None
    token_path = Path("token.pickle")
    if token_path.exists():
        with open(token_path, "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as token:
            pickle.dump(creds, token)
    return creds

# ─── MAIN APP ─────────────────────────────────────────────────────────────
class ReviewApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Spaced Repetition Review")
        self.resize(1200, 600)
        # Change the QSettings namespace to your own, e.g. ("MyOrg", "MyApp")
        self.settings = QSettings("MyOrg", "MyApp")

        creds = get_user_credentials()
        self.uploader = DriveUploader(creds, SHARED_ROOT_FOLDER_ID)

        bot_creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        self.bot_uploader = DriveUploader(bot_creds, SHARED_ROOT_FOLDER_ID)

        self.ensure_root_shared()
        self.calendar = CalendarManager(creds, USER_EMAIL)
        self.pool = QThreadPool()
        self.full_data = []
        self.data = []
        self.logs = []
        self.sort_states = {}
        self.current_row = -1
        self.current_file_index = 0

        self._startup_sync()
        self._init_ui()
        self._restore_ui_settings()
        self._set_ui_enabled(False)
        self._restore_last_opened()
        self._load_data()

    def _startup_sync(self):
        # 1) get all Drive topic names
        drive_folders = self.uploader.list_topic_folders()
        drive_topics  = {f["name"] for f in drive_folders}

        # 2) prune review_log.csv to only existing Drive topics
        csv_rows = self.uploader.read_csv()
        kept     = [r for r in csv_rows if r["topic"] in drive_topics]
        if len(kept) != len(csv_rows):
            self.uploader.write_csv(kept)

        # 3) prune study_log.csv to those same topics
        log_rows = self.uploader.read_log()
        kept_logs = [l for l in log_rows if l["topic"] in drive_topics]
        if len(kept_logs) != len(log_rows):
            self.uploader.write_log(kept_logs)

        # 4) sync local cache (delete any dirs not on Drive, redownload missing)
        self._do_sync()

    def _init_ui(self):
        # — Table —
        self.table = QTableWidget()
        self.table.horizontalHeader().sectionClicked.connect(self.handle_header_clicked)
        self.table.selectionModel().currentRowChanged.connect(self.on_selection_changed)

        # — Controls —
        self.search_bar    = QLineEdit()
        self.search_bar.setPlaceholderText("Search…")
        self.search_bar.textChanged.connect(self.on_search)

        self.add_btn       = QPushButton("Add Topic")
        self.add_btn.clicked.connect(self.add_topic)
        self.remove_btn    = QPushButton("Remove Topic")
        self.remove_btn.clicked.connect(self.remove_topic)
        self.dashboard_btn = QPushButton("Dashboard")
        self.dashboard_btn.clicked.connect(self.open_dashboard)

        # Add Sync button
        self.sync_btn      = QPushButton("Sync")
        self.sync_btn.clicked.connect(self.sync_local_cache)

        # — Toolbar actions —
        self.open_btn     = QPushButton("Open File")
        self.open_btn.clicked.connect(self.open_selected)
        self.upload_btn   = QPushButton("Add File")
        self.upload_btn.clicked.connect(self.upload_selected)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.settings_selected)
        self.review_btn   = QPushButton("Reviewed")
        self.review_btn.clicked.connect(self.review_selected)

        # Include Sync button in the toolbar layout
        top = QHBoxLayout()
        for w in (
            self.add_btn, self.remove_btn, self.dashboard_btn,
            self.sync_btn,  # Added Sync button here
            self.open_btn, self.upload_btn, self.settings_btn, self.review_btn
        ):
            top.addWidget(w)
        top.addStretch()
        top.addWidget(self.search_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # left: table + last-note
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.addWidget(self.table)

        # Define log_label and log_view
        self.log_label = QLabel("Last Note:")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)

        llay.addWidget(self.log_label)
        llay.addWidget(self.log_view)
        splitter.addWidget(left)

        # right: pdf viewer
        right = QWidget()
        rlay  = QVBoxLayout(right)
        nav_layout = QHBoxLayout()

        # Close button (half-width)
        self.close_btn = QPushButton("Close")
        self.close_btn.setMaximumWidth(100)  # control size
        self.close_btn.clicked.connect(self.clear_pdf)
        nav_layout.addWidget(self.close_btn)

        # Filename label
        self.file_label = QLabel("No file")
        self.file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav_layout.addWidget(self.file_label)

        # Navigation arrows
        self.prev_btn = QPushButton("◀")
        self.next_btn = QPushButton("▶")
        self.prev_btn.clicked.connect(self.open_prev_file)
        self.next_btn.clicked.connect(self.open_next_file)
        nav_layout.addWidget(self.prev_btn)
        nav_layout.addWidget(self.next_btn)

        rlay.addLayout(nav_layout)
        self.placeholder = QLabel("No file selected")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rlay.addWidget(self.placeholder)

        # PDF viewer setup
        self.pdf = QWebEngineView()

        # Enable local file access for the PDF viewer
        s = self.pdf.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

        rlay.addWidget(self.pdf)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        main = QVBoxLayout()
        main.addLayout(top)
        main.addWidget(splitter)

        container = QWidget()
        container.setLayout(main)
        self.setCentralWidget(container)

        self.splitter = splitter
        if (state := self.settings.value("splitterState")) is not None:
            self.splitter.restoreState(state)

    def _sync_csv_with_drive(self):
        # 1) Load the existing CSV into a dict by topic
        old_rows = {r["topic"]: r for r in self.uploader.read_csv()}

        # 2) Fetch all topic folders on Drive
        new_rows = []
        for fld in self.uploader.list_topic_folders():
            topic = fld["name"]
            fld_id = fld["id"]

            # 3) Drive’s current listing
            drive_files = self.uploader.list_files_in_folder(fld_id)
            drive_meta = {
                f["name"]: {
                    "id":   f["id"],
                    "name": f["name"],
                    "link": f"https://drive.google.com/uc?export=download&id="+f["id"]
                }
                for f in drive_files
            }

            # 4) Merge with whatever was in the old CSV
            old_files = json.loads(old_rows.get(topic, {}).get("files", "[]"))
            for f in old_files:
                # keep any old entry that still exists (by name)
                if f["name"] in drive_meta:
                    drive_meta[f["name"]] = f

            merged = list(drive_meta.values())

            # 5) Build the new row, preserving reviews/calendar
            prev = old_rows.get(topic, {})
            new_rows.append({
                "topic":             topic,
                "files":             json.dumps(merged),
                "last_review":       prev.get("last_review",""),
                "next_review":       prev.get("next_review",""),
                "calendar_event_id": prev.get("calendar_event_id",""),
                "drive_folder_id":   fld_id,
            })

        # 6) Overwrite the CSV on Drive and refresh the UI
        self.uploader.write_csv(new_rows)
        self._load_data()



    def sync_local_cache(self):
        """Worker‐friendly kickoff for a full re-sync of local_records/."""
        pd = QProgressDialog("Syncing local cache…", None, 0, 0, self)
        pd.setWindowModality(Qt.WindowModality.WindowModal)
        pd.setCancelButton(None)
        pd.show()

        w = Worker(self._do_sync)
        # When the sync finishes, close dialog AND update the CSV to match current Drive folders
        w.signals.finished.connect(lambda _: (pd.close(), self._sync_csv_with_drive()))
        w.signals.error.connect(lambda m: (
            pd.close(),
            QMessageBox.critical(self, "Sync Error", m)
        ))
        self.pool.start(w)

    def _do_sync(self):
        # 1) fetch all topic folders on Drive
        topics = self.uploader.list_topic_folders()
        drive_map = {t["name"]: t["id"] for t in topics}

        # ensure cache dir exists
        LOCAL_CACHE.mkdir(exist_ok=True)

        # 2) remove any local dirs that no longer exist on Drive
        for d in LOCAL_CACHE.iterdir():
            if d.is_dir() and d.name not in drive_map:
                shutil.rmtree(d)

        # 3) for each Drive folder, download missing files
        for name, fid in drive_map.items():
            topic_dir = LOCAL_CACHE / name
            topic_dir.mkdir(exist_ok=True)

            # list all files in that Drive folder
            flist = self.uploader.list_files_in_folder(fid)
            for f in flist:
                local_path = topic_dir / f["name"]
                if not local_path.exists():
                    self.uploader.download_file_to_path(f["id"], str(local_path))

        return True
    
    def _restore_ui_settings(self):
        if geom := self.settings.value("geometry"):
            self.restoreGeometry(geom)
        if ws := self.settings.value("windowState"):
            self.restoreState(ws)
        if hs := self.settings.value("headerState"):
            self.table.horizontalHeader().restoreState(hs)

    def _on_viewer_loaded(self, ok):
        # once the HTML is loaded, keep it hidden until first PDF open
        # you could also disable the placeholder here if you like.
        pass

    def closeEvent(self, e):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.settings.setValue("headerState", self.table.horizontalHeader().saveState())
        self.settings.setValue("splitterState", self.splitter.saveState())
        super().closeEvent(e)

    def _set_ui_enabled(self, en):
        for w in (
            self.table, self.search_bar,
            self.add_btn, self.remove_btn, self.dashboard_btn,
            self.open_btn, self.upload_btn, self.settings_btn, self.review_btn,
            self.close_btn
        ):
            w.setEnabled(en)

    def _load_data(self):
        pd = QProgressDialog("Loading…", None, 0, 0, self)
        pd.setWindowModality(Qt.WindowModality.WindowModal)
        pd.setCancelButton(None)
        pd.show()
        w = Worker(self.uploader.read_csv)
        w.signals.finished.connect(lambda rows, pd=pd: self._on_loaded(rows, pd))
        w.signals.error.connect(lambda m, pd=pd: self._on_load_err(m, pd))
        self.pool.start(w)

    # filepath: c:\Users\Guido\Desktop\Learning app\app.py
    def _on_loaded(self, rows, pd):
        pd.close()
        self.full_data = rows
        self.data = list(rows)
        self.logs = self.uploader.read_log()

        # Sync Drive files into local cache
        LOCAL_CACHE.mkdir(exist_ok=True)
        for ent in self.full_data:
            topic_dir = LOCAL_CACHE / ent["topic"]
            topic_dir.mkdir(exist_ok=True)
            flist = json.loads(ent.get("files") or "[]")
            valid_files = []
            for f in flist:
                local_path = topic_dir / f["name"]
                try:
                    if not local_path.exists():
                        self.uploader.download_file_to_path(f["id"], str(local_path))
                    valid_files.append(f)
                except googleapiclient.errors.HttpError as e:
                    if e.resp.status == 404:
                        print(f"Warning: Skipping missing file {f['name']} (ID: {f['id']})")
                    else:
                        raise
            ent["files"] = json.dumps(valid_files)

        # Calendar sync
        today = QDate.currentDate()
        for ent in self.full_data:
            nr = ent.get("next_review", "")
            qnr = QDate.fromString(nr, "yyyy-MM-dd")
            if not qnr.isValid():
                ent["calendar_event_id"] = ""
            elif qnr > today:
                # Only schedule if next_review is in the future
                self.calendar.delete_future_events(ent["topic"])
                eid = self.calendar.create_event(ent["topic"], nr)
                ent["calendar_event_id"] = eid or ""
            else:
                # Past or today → mark expired and don’t schedule
                ent["calendar_event_id"] = ""
                ent["_expired"] = True
        self._save_bg()
        self._set_ui_enabled(True)
        self.populate_table()

        # Now that everything’s ready, show the window
        self.show()

    def open_file(self, r):
        flist = json.loads(self.data[r].get("files") or "[]")
        if not flist:
            QMessageBox.information(self, "No Files", "No files added.")
            return

        name, ok = QInputDialog.getItem(
            self, "Choose File", "Select:", [f["name"] for f in flist], editable=False
        )
        if not ok:
            return

        local = LOCAL_CACHE / self.data[r]["topic"] / name
        if not local.exists():
            fid = next(f["id"] for f in flist if f["name"] == name)
            self.uploader.download_file_to_path(fid, str(local))

        # Build the file URL
        raw_path = local.resolve().as_posix()
        enc_path = quote(raw_path, safe="/:")
        pdf_url = f"file:///{enc_path}"

        viewer_url = QUrl.fromLocalFile(str(PDFJS_VIEWER.resolve())).toString()
        full_url = f"{viewer_url}?file={pdf_url}"

        self.pdf.hide()
        self.pdf.loadFinished.connect(self._on_pdf_load_finished)
        self.pdf.load(QUrl(full_url))

        # After successfully showing the PDF
        self.pdf.show()

        # Remember for next launch
        self.settings.setValue("last_topic", self.data[r]["topic"])
        self.settings.setValue("last_file", name)

    def _restore_last_opened(self):
        t = self.settings.value("last_topic", "")
        f = self.settings.value("last_file", "")
        if not t or not f:
            return

        # defer until after data is loaded
        def try_open(_):
            # find the row for that topic
            for i, ent in enumerate(self.full_data):
                if ent["topic"] == t:
                    # manually invoke open with that file name
                    self._open_file_by_name(i, f)
                    break

        # hook into your existing load signal
        QTimer.singleShot(0, lambda: None)  # ensure UI ready
        self._on_loaded = (lambda orig:
            lambda rows, pd: (orig(rows, pd), try_open(rows))
        )(self._on_loaded)

    def _open_file_by_name(self, row, filename):
        topic = self.data[row]["topic"]
        local = LOCAL_CACHE / topic / filename
        if not local.exists():
            fid = next(
                f["id"] for f in json.loads(self.data[row]["files"])
                if f["name"] == filename
            )
            self.uploader.download_file_to_path(fid, str(local))

        # Build the file URL
        raw_path = local.resolve().as_posix()
        enc_path = quote(raw_path, safe="/:")
        pdf_url = f"file:///{enc_path}"

        viewer_url = QUrl.fromLocalFile(str(PDFJS_VIEWER.resolve())).toString()
        full_url = f"{viewer_url}?file={pdf_url}"

        self.pdf.hide()
        self.pdf.loadFinished.connect(self._on_pdf_load_finished)
        self.pdf.load(QUrl(full_url))

        # Show the filename in the label
        self.file_label.setText(filename)

    def _open_file_by_index(self, row, index):
        topic = self.data[row]["topic"]
        flist = json.loads(self.data[row].get("files") or "[]")
        if not flist:
            return

        index %= len(flist)  # wrap-around
        file_info = flist[index]
        filename = file_info["name"]
        local = LOCAL_CACHE / topic / filename

        if not local.exists():
            self.uploader.download_file_to_path(file_info["id"], str(local))

        # Build the file URL
        raw_path = local.resolve().as_posix()
        enc_path = quote(raw_path, safe="/:")
        pdf_url = f"file:///{enc_path}"

        viewer_url = QUrl.fromLocalFile(str(PDFJS_VIEWER.resolve())).toString()
        full_url = f"{viewer_url}?file={pdf_url}"

        self.pdf.hide()
        self.pdf.loadFinished.connect(self._on_pdf_load_finished)
        self.pdf.load(QUrl(full_url))
        self.pdf.show()

        self.file_label.setText(filename)
        # Persist last opened file/topic
        self.settings.setValue("last_topic", topic)
        self.settings.setValue("last_file", filename)

    def _on_pdf_load_finished(self, ok: bool):
        """
        Slot for QWebEngineView.loadFinished.
        Only show the PDF view once it's actually rendered to avoid flicker.
        """
        # disconnect immediately so it only fires once
        self.pdf.loadFinished.disconnect(self._on_pdf_load_finished)

        if ok:
            self.placeholder.hide()
            self.pdf.show()
        else:
            QMessageBox.critical(self, "Load Error", "Failed to load PDF.")

    def clear_pdf(self):
        self.pdf.hide()
        self.pdf.setUrl(QUrl())
        self.placeholder.show()

    def _on_load_err(self, msg, pd):
        pd.close()
        QMessageBox.critical(self, "Error loading", msg)
        sys.exit(1)

    def on_search(self, txt):
        t = txt.strip().lower()
        self.data = (
            self.full_data
            if not t
            else [r for r in self.full_data if t in r["topic"].lower()]
        )
        self.populate_table()

    def populate_table(self):
        self.table.setUpdatesEnabled(False)
        self.table.clear()
        self.table.setColumnCount(4)
        self.table.setRowCount(len(self.data))
        self.table.setHorizontalHeaderLabels(
            ["Topic", "Files", "Last Review", "Next Review"]
        )
        for r, e in enumerate(self.data):
            # ── Topic and Files ────────────────────────────────────────────────
            self.table.setItem(r, 0, QTableWidgetItem(e["topic"]))
            flist = json.loads(e.get("files") or "[]")
            self.table.setItem(r, 1, QTableWidgetItem(
                ", ".join(f["name"] for f in flist)
            ))

            # ── Last Review (plain text, read‐only) ────────────────────────────
            last = e.get("last_review", "")
            itm_last = QTableWidgetItem(last)
            itm_last.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(r, 2, itm_last)

            # ── Next Review / Expired ──────────────────────────────────────────
            nr_str = e.get("next_review", "")
            last_done = bool(e.get("last_review", "").strip())
            # If never reviewed, show blank
            if not last_done or not nr_str:
                self.table.setItem(r, 3, QTableWidgetItem(""))
            else:
                qnr = QDate.fromString(nr_str, "yyyy-MM-dd")
                # Expired?
                if qnr.isValid() and qnr <= QDate.currentDate():
                    itm = QTableWidgetItem("Expired")
                    itm.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                    self.table.setItem(r, 3, itm)
                else:
                    # Allow date pick for scheduling
                    ne = QDateEdit(qnr if qnr.isValid() else QDate.currentDate())
                    ne.setCalendarPopup(True)
                    ne.setDisplayFormat("yyyy-MM-dd")
                    ne.dateChanged.connect(partial(self.next_review_changed, r))
                    self.table.setCellWidget(r, 3, ne)

        self.table.resizeColumnsToContents()
        self.table.setUpdatesEnabled(True)
        
    def handle_header_clicked(self, col):
        if col not in (0, 2, 3):
            return
        prev = self.sort_states.get(col, Qt.SortOrder.AscendingOrder)
        order = (
            Qt.SortOrder.DescendingOrder
            if prev == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
        self.sort_states[col] = order
        if col == 0:
            key = lambda e: e["topic"].lower()
        elif col == 2:
            key = lambda e: (
                QDate.fromString(e.get("last_review", ""), "yyyy-MM-dd").toPyDate()
                if QDate.fromString(e.get("last_review", ""), "yyyy-MM-dd").isValid()
                else datetime(1970, 1, 1).date()
            )
        else:
            key = lambda e: (
                QDate.fromString(e.get("next_review", ""), "yyyy-MM-dd").toPyDate()
                if QDate.fromString(e.get("next_review", ""), "yyyy-MM-dd").isValid()
                else datetime(1970, 1, 1).date()
            )
        self.data.sort(key=key, reverse=(order == Qt.SortOrder.DescendingOrder))
        self.populate_table()

    def on_selection_changed(self, current, prev):
        r = current.row()
        if r < 0:
            self.log_view.clear()
            return

        # ── Display logs for the selected topic ───────────────────────────────
        topic = self.data[r]["topic"]
        entries = [L for L in self.logs if L["topic"] == topic]
        if not entries:
            self.log_view.clear()
        else:
            # Sort chronologically and show all entries
            entries.sort(key=lambda x: x["review_date"])
            lines = [
                f'{e["review_date"]} ({e["difficulty"]}): {e["comment"]}'
                for e in entries
            ]
            self.log_view.setPlainText("\n".join(lines))

        # ── Open first file & reset nav state ─────────────────────────────────
        flist = json.loads(self.data[r].get("files") or "[]")
        if flist:
            self.current_row = r
            self.current_file_index = 0
            self._open_file_by_index(r, 0)
        else:
            self.current_row = -1

    def _current(self):
        r = self.table.currentRow()
        if r < 0:
            QMessageBox.information(self, "No Selection", "Select a topic first.")
            return None, None
        return r, self.data[r]

    def open_selected(self):
        r, _ = self._current()
        if r is not None:
            self.open_file(r)

    def upload_selected(self):
        r, _ = self._current()
        if r is not None:
            self.start_upload(r)

    def settings_selected(self):
        r, _ = self._current()
        if r is not None:
            self.open_settings(r)

    def review_selected(self):
        r, _ = self._current()
        if r is not None:
            self.mark_reviewed(r)

    def last_review_changed(self, r, nd):
        ent = self.data[r]
        ent["last_review"] = nd.toString("yyyy-MM-dd")
        self._save_bg()

    def _reschedule(self, topic, ds):
        self.calendar.delete_future_events(topic)
        return self.calendar.create_event(topic, ds)

    def next_review_changed(self, r, nd):
        ent = self.data[r]
        ds = nd.toString("yyyy-MM-dd")
        ent["next_review"] = ds
        ent["calendar_event_id"] = ""

        # Persist the date change immediately
        self.uploader.write_csv(self.full_data)

        w = Worker(self._reschedule, ent["topic"], ds)
        w.signals.finished.connect(lambda eid, e=ent: self._on_new_event(e, eid))
        self.pool.start(w)

    def mark_reviewed(self, r):
        ent = self.data[r]
        opts = ["Difficult", "Medium", "Easy"]
        diff, ok = QInputDialog.getItem(self, "Reviewed", "How was this revision?", opts, editable=False)
        if not ok:
            return
        comment, ok2 = QInputDialog.getText(self, "Comment", "Add a note:")
        if not ok2:
            comment = ""
        today = QDate.currentDate().toString("yyyy-MM-dd")
        entry = {"topic": ent["topic"], "review_date": today, "difficulty": diff, "comment": comment}
        self.uploader.append_log(entry)
        self.logs.append(entry)

        last = ent.get("last_review", "")
        if not last:
            mapping = {"Difficult": 1, "Medium": 3, "Easy": 7}
            nxt_days = mapping[diff]
        else:
            d0 = QDate.fromString(last, "yyyy-MM-dd").toPyDate()
            d1 = datetime.strptime(today, "%Y-%m-%d").date()
            delta = max(1, (d1 - d0).days)
            factor = {"Difficult": 1.2, "Medium": 1.5, "Easy": 2.0}[diff]
            nxt_days = max(1, round(delta * factor))
        nxt_date = QDate.currentDate().addDays(nxt_days).toString("yyyy-MM-dd")

        ent["last_review"] = today
        ent["next_review"] = nxt_date
        ent["calendar_event_id"] = ""

        # Write updated last/next review straight back to Drive
        self.uploader.write_csv(self.full_data)
        self.populate_table()

        w = Worker(self._reschedule, ent["topic"], nxt_date)
        w.signals.finished.connect(lambda eid, e=ent: self._on_new_event(e, eid))
        self.pool.start(w)

    def _on_new_event(self, ent, eid):
        ent["calendar_event_id"] = eid or ""
        self._save_bg()

    def start_upload(self, r):
        path, _ = QFileDialog.getOpenFileName(self, "Select PDF", "", "PDF Files (*.pdf)")
        if not path:
            return

        folder_id = self.data[r]["drive_folder_id"]

        pd = QProgressDialog("Uploading…", None, 0, 0, self)
        pd.setWindowModality(Qt.WindowModality.WindowModal)
        pd.setCancelButton(None)
        pd.show()

        # Use the bot uploader for the upload
        w = Worker(self.bot_uploader.upload_file, path, folder_id)
        w.signals.finished.connect(lambda res, r=r, pd=pd: self._done_upload(res, r, pd))
        w.signals.error.connect(lambda m, pd=pd: (pd.close(), QMessageBox.critical(self, "Upload Error", m)))
        self.pool.start(w)

    def _done_upload(self, res, r, pd):
        pd.close()
        fid, name, link = res

        # Update the topic's file list
        ent = self.data[r]
        lst = json.loads(ent.get("files") or "[]")
        lst.append({"id": fid, "name": name, "link": link})
        ent["files"] = json.dumps(lst)
        for e in self.full_data:
            if e["topic"] == ent["topic"]:
                e.update(ent)
                break
        self._save_bg()
        self.populate_table()

    def open_settings(self, r):
        flist = json.loads(self.data[r].get("files") or "[]")
        if not flist:
            QMessageBox.information(self, "No Files", "No files to manage.")
            return

        dlg = SettingsDialog(flist, self)
        if dlg.exec() != QDialogButtonBox.StandardButton.Ok:
            return

        # Which file the user wants to delete:
        idx = dlg.selected_index()
        to_del = flist[idx]

        # Show a little progress dialog while we delete
        pd = QProgressDialog("Deleting file…", None, 0, 0, self)
        pd.setWindowModality(Qt.WindowModality.WindowModal)
        pd.setCancelButton(None)
        pd.show()

        def on_deleted(_):
            pd.close()
            # 1) Remove from our in-memory lists
            new_list = [f for f in flist if f["id"] != to_del["id"]]
            ent = self.data[r]
            ent["files"] = json.dumps(new_list)
            for e in self.full_data:
                if e["topic"] == ent["topic"]:
                    e.update(ent)
                    break

            # 2) Persist the cleaned-up CSV back to Drive
            try:
                self.uploader.write_csv(self.full_data)
            except Exception as e:
                QMessageBox.critical(self, "CSV Write Error", str(e))
                return

            # 3) Refresh the table & clear the PDF viewer
            self.populate_table()
            self.clear_pdf()

        def on_error(msg):
            pd.close()
            QMessageBox.critical(self, "Delete Error", msg)

        # Fire off the real delete, then hook in our callbacks
        w = Worker(self.bot_uploader.delete_file, to_del["id"])
        w.signals.finished.connect(on_deleted)
        w.signals.error.connect(on_error)
        self.pool.start(w)

    def compute_stats(self):
        total = len(self.full_data)
        today = datetime.utcnow().date()
        week = today + timedelta(days=7)
        upc = sum(
            1 for e in self.full_data
            if QDate.fromString(e.get("next_review", ""), "yyyy-MM-dd").toPyDate() <= week
        )
        ints = []
        for e in self.full_data:
            lr = QDate.fromString(e.get("last_review", ""), "yyyy-MM-dd")
            nr = QDate.fromString(e.get("next_review", ""), "yyyy-MM-dd")
            if lr.isValid() and nr.isValid() and nr > lr:
                ints.append((nr.toPyDate() - lr.toPyDate()).days)
        avg = round(sum(ints) / len(ints), 1) if ints else 0
        return {"Total Topics": total, "Upcoming ≤7d": upc, "Avg Interval(days)": avg}

    def open_dashboard(self):
        stats = self.compute_stats()
        dlg = DashboardDialog(stats, self)
        dlg.exec()

    def _save_bg(self):
        pass  # No longer needed since changes are written immediately

    def add_topic(self, _=None):
        txt, ok = QInputDialog.getText(self, "New Topic", "Enter topic name:")
        if not (ok and txt.strip()):
            return

        # Use the bot uploader to create the folder
        fid = self.bot_uploader.create_topic_folder(txt.strip())

        # Add the topic to the data
        ent = {
            "topic": txt.strip(),
            "files": "[]",
            "last_review": "",
            "next_review": QDate.currentDate().toString("yyyy-MM-dd"),
            "calendar_event_id": "",
            "drive_folder_id": fid
        }
        self.full_data.append(ent)
        self.data.append(ent)
        self._save_bg()
        self.populate_table()

    def remove_topic(self, _=None):
        r = self.table.currentRow()
        if r < 0:
            return
        ent = self.data[r]
        if QMessageBox.question(self, "Confirm Delete", f"Delete '{ent['topic']}'?") \
           == QMessageBox.StandardButton.Yes:
            self.calendar.delete_future_events(ent["topic"])
            if ent.get("drive_folder_id"):
                # Use the bot uploader to delete the folder
                self.pool.start(Worker(self.bot_uploader.delete_folder, ent["drive_folder_id"]))
            self.full_data.remove(ent)
            self.data.remove(ent)
            self._save_bg()
            self.populate_table()
            self.clear_pdf()

        self.current_row = r
        self.current_file_index = 0
        self._open_file_by_index(r, 0)

    def open_next_file(self):
        if self.current_row < 0:
            return
        self.current_file_index += 1
        self._open_file_by_index(self.current_row, self.current_file_index)

    def open_prev_file(self):
        if self.current_row < 0:
            return
        self.current_file_index -= 1
        self._open_file_by_index(self.current_row, self.current_file_index)

    def ensure_root_shared(self):
        drive = self.bot_uploader.drive
        # 1) Get existing permissions on the root folder
        try:
            resp = drive.permissions().list(
                fileId=SHARED_ROOT_FOLDER_ID,
                fields="permissions(id,emailAddress)"
            ).execute()
            perms = resp.get("permissions", [])
            if any(p.get("emailAddress") == USER_EMAIL for p in perms):
                return  # Already shared

            # 2) Otherwise, create the permission
            drive.permissions().create(
                fileId=SHARED_ROOT_FOLDER_ID,
                body={
                    "type": "user",
                    "role": "writer",
                    "emailAddress": USER_EMAIL
                }
            ).execute()
        except HttpError as e:
            # Log the error and move on
            print("Could not share root folder:", e)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ReviewApp()
    # → don’t call window.show() yet!
    sys.exit(app.exec())
