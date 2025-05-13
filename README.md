# Spaced Repetition Review App

A PyQt6-based GUI application to manage spaced repetition reviews with PDF notes, backed by Google Drive for file storage and Google Calendar for scheduled reminders.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Setup Instructions](#setup-instructions)
- [Usage](#usage)
- [Folder Structure](#folder-structure)
- [Notes](#notes)
- [License](#license)

---

## Features

- Review scheduling using spaced repetition algorithms
- PDF preview for attached notes using embedded PDF.js
- File uploads and downloads managed via Google Drive
- Review session logging with comments and difficulty ratings
- Automatic calendar reminders via Google Calendar
- Local caching of PDFs for offline use

---

## Requirements

- Python 3.10+
- A Google Cloud Project with:
  - A **Service Account** with Drive access
  - **OAuth 2.0 Client ID** for user consent
  - Drive API and Calendar API enabled
- QtWebEngine and PyQt6
- Access to a shared Google Drive folder

Install dependencies:

```bash
pip install -r requirements.txt
```

You may need to install `PyQt6` and `PyQt6-WebEngine` manually if not in the requirements.

---

## Installation

1. **Clone the repo:**

```bash
git clone https://github.com/yourname/review-app.git
cd review-app
```

2. **Set up Google Cloud credentials. See below.**

---

## Setup Instructions

### 1. **Google Cloud Setup**

You must create the following files via the [Google Cloud Console](https://console.cloud.google.com):

#### a. `service_account.json`

- Go to IAM & Admin → Service Accounts.
- Create a new Service Account.
- Grant it "Editor" or "Drive Admin" roles.
- Enable **Drive API**.
- Create a key and download the JSON file.
- Share your **Drive root folder** with this service account email.

#### b. `oauth_credentials.json`

- Go to "APIs & Services" → "Credentials".
- Create an OAuth 2.0 Client ID (Desktop App).
- Download the JSON file.

### 2. **Application Configuration**

Update the following constants in the code (at the top of the file):

```python
SERVICE_ACCOUNT_FILE = Path("path/to/your-service-account.json")
OAUTH_CREDENTIALS_FILE = Path("path/to/your-oauth-credentials.json")
SHARED_ROOT_FOLDER_ID = "<YOUR_SHARED_ROOT_FOLDER_ID>"
USER_EMAIL = "<YOUR_EMAIL_ADDRESS>"
```

You **must replace all four values**:
- `SERVICE_ACCOUNT_FILE`: path to your downloaded service account key
- `OAUTH_CREDENTIALS_FILE`: path to your OAuth credentials
- `SHARED_ROOT_FOLDER_ID`: folder ID from Google Drive where topics will live
- `USER_EMAIL`: your primary Google account (used as default Calendar ID)

### 3. **PDF Viewer**

This app uses [PDF.js](https://mozilla.github.io/pdf.js/). Download the viewer:

- Go to [PDF.js GitHub](https://github.com/mozilla/pdf.js/)
- Clone/download the repo and copy the `web/viewer.html` directory
- Place it inside your project: `pdfjs/web/viewer.html`

Then update:

```python
PDFJS_VIEWER = Path(__file__).parent / "pdfjs" / "web" / "viewer.html"
```

---

## Usage

Run the app:

```bash
python app.py
```

On first launch, you’ll be prompted to authenticate with Google via your browser (OAuth). A token will be saved as `token.pickle`.

You can:

- **Add Topics**: Creates a new Drive folder under the root
- **Upload Files**: PDFs are uploaded and linked
- **Review**: Opens a calendar event, logs difficulty
- **Sync**: Refreshes your local file cache
- **Dashboard**: Shows stats like upcoming reviews

---

## Folder Structure

```
review-app/
│
├── app.py
├── token.pickle                  # Created after login
├── service_account.json          # You provide
├── oauth_credentials.json        # You provide
├── pdfjs/
│   └── web/
│       └── viewer.html           # From PDF.js
├── local_records/                # Auto-created cache
```

---

## Notes

- Only **PDF files** are currently supported.
- Data (CSV logs) are stored on Google Drive under a special `records/` folder.
- Any file deleted from the app is removed from Drive but not locally.

---

## License

This project is licensed under the MIT License.
