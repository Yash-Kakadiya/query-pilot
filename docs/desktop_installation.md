# QueryPilot Desktop — Installation & User Guide

QueryPilot Desktop is a standalone Windows x64 desktop application combining a native Tauri 2 desktop shell, an embedded FastAPI local agent, and a React frontend.

---

## 1. System Requirements & Runtime Dependencies

| Component | Status | Description |
| :--- | :--- | :--- |
| **Operating System** | Required | Windows 10 (version 1809+) or Windows 11 (64-bit x64) |
| **Microsoft Edge WebView2** | Required Runtime | Microsoft Edge WebView2 Evergreen Runtime. Pre-installed on modern Windows 10 and 11; if missing, the installer bootstrapper will download and install it. |
| **Python 3.12 Runtime** | **Bundled** | **Pre-packaged**. Users do NOT need Python installed on their system. The standalone Python runtime and all internal application dependencies are self-contained. |
| **SQLite Engine** | **Built-in** | **Pre-packaged**. Native SQLite introspection, read-only safety validation, and local execution require no external drivers. |
| **SQL Server (MSSQL)** | External Prerequisite | Microsoft ODBC Driver 17 or 18 for SQL Server is required **only** if configuring and connecting to Microsoft SQL Server databases. |
| **Administrator Rights** | Not Required | Standard per-user installation into `%LOCALAPPDATA%\QueryPilot`. |

---

## 2. Installation Walkthrough

### Step 1: Download
Download the latest `QueryPilot_0.2.0_x64-setup.exe` installer from the release distribution.

### Step 2: Run Installer
Double-click `QueryPilot_0.2.0_x64-setup.exe` to start the setup wizard.
- The installer installs QueryPilot into your personal local applications folder:
  `%LOCALAPPDATA%\QueryPilot`
- It does **not** require administrative elevation (UAC prompt).
- It creates shortcuts on your **Desktop** and in your **Start Menu**.

> [!NOTE]
> **Silent / Automated Installation**:
> For automated lab deployments, run the installer with the `/S` flag:
> ```powershell
> .\QueryPilot_0.2.0_x64-setup.exe /S
> ```

---

## 3. Windows SmartScreen & Binary Integrity

QueryPilot Desktop pre-release staging builds are distributed without a commercial Authenticode code-signing certificate. Consequently, Windows Defender SmartScreen may display a warning dialog upon launching the installer or application for the first time:

```
Windows protected your PC
Microsoft Defender SmartScreen prevented an unrecognized app from starting.
```

### Archive Integrity vs. Publisher Identity:
- **Archive Integrity (SHA-256):** Users can independently verify binary integrity against the official checksum manifest (`SHA256SUMS.txt`). Verifying the SHA-256 hash guarantees that the binary has not been modified or corrupted in transit.
- **Publisher Identity:** Unsigned staging builds do not embed a commercial Authenticode certificate, so Windows SmartScreen flags them with "Unknown Publisher" regardless of checksum validity.

### How to Proceed:
1. (Optional but recommended) Verify the installer hash in PowerShell:
   ```powershell
   Get-FileHash QueryPilot_0.2.0_x64-setup.exe -Algorithm SHA256
   ```
2. Click the **"More info"** link in the SmartScreen dialog.
3. Click the **"Run anyway"** button that appears.

---

## 4. First Launch & Zero-Touch Authentication

1. Launch QueryPilot from your **Desktop** or **Start Menu**.
2. **Automated Lifecycle**:
   - The native Tauri host initializes a dedicated Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
   - The bundled Python agent sidecar is launched in desktop mode, binding to an OS-assigned ephemeral loopback port (`127.0.0.1:<port>`).
   - The sidecar transmits a readiness handshake (`QUERYPILOT_READY:{"status":"ready", ...}`).
   - The native shell performs automatic token authentication, retrieves an ambient `HttpOnly; SameSite=Strict` loopback session cookie, and injects it into WebView2.
   - The window navigates directly to `/app/`.
3. **User Experience**:
   - The user is authenticated automatically. You will never need to copy, view, or enter a bootstrap token manually.

---

## 5. Working with Databases

### SQLite (Built-in, Zero-Configuration)
QueryPilot includes full native support for SQLite databases:
1. In the QueryPilot workspace, navigate to **Profiles** > **New Profile**.
2. Select **SQLite** as the database engine.
3. Browse or enter the path to your `.db` or `.sqlite` file (e.g., `C:\Data\chinook.db`).
4. QueryPilot connects immediately in read-only mode and performs schema introspection.

### Microsoft SQL Server (Optional)
To connect to Microsoft SQL Server:
1. Ensure the [Microsoft ODBC Driver for SQL Server (Driver 17 or 18)](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) is installed on your Windows system.
2. Create a new profile selecting **MSSQL**.
3. Specify your server hostname, port, database name, and authentication credentials.

---

## 6. User Data and Settings

All user configuration, connection profiles, and cached schemas are saved in your user home directory:

```
%USERPROFILE%\.query_pilot\
└── profiles.json       # Connection profile configurations
```

Bootstrap tokens are generated in a dedicated, per-launch temporary directory (`%LOCALAPPDATA%\Temp\querypilot_token_<pid>_<nanos>_<count>\agent.token`) hardened with current-user Win32 ACLs. The token is read by the desktop host, consumed via the loopback bootstrap endpoint, logically overwritten in-place, and deleted from disk upon session initialization.

- **Separation of Concerns**: User data is stored strictly outside the program installation directory (`%LOCALAPPDATA%\QueryPilot`).
- **Upgrade Safety**: Updating, reinstalling, or repairing QueryPilot never alters or deletes your configured connection profiles.

---

## 7. Clean Uninstallation

To uninstall QueryPilot:
1. Open **Windows Settings** > **Apps** > **Installed apps** (or **Add or Remove Programs**).
2. Locate **QueryPilot** in the list and select **Uninstall**.
   - Alternatively, launch **Uninstall QueryPilot** directly from the Start Menu, or run:
     ```powershell
     & "$env:LOCALAPPDATA\QueryPilot\uninstall.exe" /S
     ```
3. The uninstaller removes all program files, bundled libraries, and shortcuts.
4. Your connection profiles in `%USERPROFILE%\.query_pilot\` are preserved by default to protect your data. If you wish to completely remove all profiles, simply delete the `%USERPROFILE%\.query_pilot` directory.

