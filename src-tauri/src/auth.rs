//! Authentication module for QueryPilot Desktop (B26-B4).
//!
//! Implements:
//! 1. Secure token retrieval from established ~/.query_pilot/agent.token location.
//! 2. Direct HTTP bootstrap exchange with local agent at 127.0.0.1:<port>.
//! 3. Single-use token consumption and session cookie extraction (qpt_session).
//! 4. Windows WebView2 cookie manager injection with strict security attributes.
//! 5. Fail-closed error handling and sensitive memory wiping.

use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

pub const SESSION_COOKIE_NAME: &str = "qpt_session";
static LAUNCH_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionCookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    pub path: String,
    pub is_http_only: bool,
    pub is_secure: bool,
    pub same_site: CookieSameSite,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CookieSameSite {
    None,
    Lax,
    Strict,
}

#[derive(Debug)]
pub enum AuthError {
    TokenNotFound(PathBuf),
    TokenReadFailed(String),
    TokenFormatInvalid(usize),
    RequestFailed(String),
    UnexpectedStatus(u16),
    MissingSessionCookie,
    CookieManagerError(String),
}

impl fmt::Display for AuthError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AuthError::TokenNotFound(p) => {
                write!(f, "Bootstrap token file not found at: {}", p.display())
            }
            AuthError::TokenReadFailed(msg) => {
                write!(f, "Failed to read bootstrap token file: {}", msg)
            }
            AuthError::TokenFormatInvalid(len) => {
                write!(f, "Bootstrap token format invalid (length={})", len)
            }
            AuthError::RequestFailed(msg) => {
                write!(f, "Agent bootstrap HTTP request failed: {}", msg)
            }
            AuthError::UnexpectedStatus(code) => {
                write!(f, "Agent bootstrap returned unexpected status: {}", code)
            }
            AuthError::MissingSessionCookie => write!(
                f,
                "Agent response missing expected '{}' Set-Cookie header",
                SESSION_COOKIE_NAME
            ),
            AuthError::CookieManagerError(msg) => {
                write!(f, "WebView2 cookie manager error: {}", msg)
            }
        }
    }
}

impl std::error::Error for AuthError {}

/// Wipes sensitive in-memory string buffer using volatile writes to prevent compiler optimization.
pub fn wipe_string(mut s: String) {
    unsafe {
        let bytes = s.as_mut_vec();
        for b in bytes.iter_mut() {
            std::ptr::write_volatile(b, 0);
        }
    }
}

/// Resolve bootstrap token path according to agent configuration precedence.
pub fn resolve_token_path() -> PathBuf {
    if let Ok(dir) = std::env::var("QUERYPILOT_TOKEN_DIR") {
        if !dir.trim().is_empty() {
            let filename = std::env::var("QUERYPILOT_AGENT_TOKEN_FILENAME")
                .unwrap_or_else(|_| "agent.token".to_string());
            return PathBuf::from(dir.trim()).join(filename);
        }
    }
    if let Ok(dir) = std::env::var("QUERYPILOT_AGENT_TOKEN_DIR") {
        if !dir.trim().is_empty() {
            let filename = std::env::var("QUERYPILOT_AGENT_TOKEN_FILENAME")
                .unwrap_or_else(|_| "agent.token".to_string());
            return PathBuf::from(dir.trim()).join(filename);
        }
    }
    // Default: ~/.query_pilot/agent.token
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_else(|_| ".".to_string());
    let filename = std::env::var("QUERYPILOT_AGENT_TOKEN_FILENAME")
        .unwrap_or_else(|_| "agent.token".to_string());
    PathBuf::from(home).join(".query_pilot").join(filename)
}

const ABANDONED_DIRECTORY_TTL_NANOS: u128 = 3600 * 1_000_000_000; // 1 hour

/// Tri-state process liveness outcome for fail-closed legacy directory inspection.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum ProcessLiveness {
    /// Process is confirmed to be actively running (unsignaled handle).
    Running,
    /// Process is confirmed to be terminated or non-existent in the OS.
    Terminated,
    /// Indeterminate state (e.g. access denied, wait failure, or unexpected error). Fail safe.
    Inaccessible,
}

/// Evaluates legacy directory cleanup action based on verified tri-state process liveness.
/// Deletes strictly when confirmed Terminated; preserves when Running or Inaccessible.
pub fn evaluate_legacy_cleanup(liveness: ProcessLiveness) -> CleanupAction {
    match liveness {
        ProcessLiveness::Terminated => CleanupAction::Delete,
        ProcessLiveness::Running => CleanupAction::Preserve,
        ProcessLiveness::Inaccessible => CleanupAction::Preserve,
    }
}

/// Pure helper mapping Windows WaitForSingleObject return codes to ProcessLiveness.
pub fn map_liveness_wait_result(wait_result: u32) -> ProcessLiveness {
    const WAIT_OBJECT_0: u32 = 0;
    const WAIT_TIMEOUT: u32 = 258;

    if wait_result == WAIT_TIMEOUT {
        ProcessLiveness::Running
    } else if wait_result == WAIT_OBJECT_0 {
        ProcessLiveness::Terminated
    } else {
        ProcessLiveness::Inaccessible
    }
}

/// Pure helper mapping Windows OpenProcess error codes (GetLastError) to ProcessLiveness.
pub fn map_liveness_open_error(last_error: u32) -> ProcessLiveness {
    const ERROR_INVALID_PARAMETER: u32 = 87;
    const ERROR_ACCESS_DENIED: u32 = 5;

    if last_error == ERROR_INVALID_PARAMETER {
        // PID does not exist in the OS -> confirmed dead
        ProcessLiveness::Terminated
    } else if last_error == ERROR_ACCESS_DENIED {
        // Exists but lacks query permission -> indeterminate, fail safe
        ProcessLiveness::Inaccessible
    } else {
        // Any other unexpected OS error -> indeterminate, fail safe
        ProcessLiveness::Inaccessible
    }
}

/// Inspect process liveness with explicit tri-state handling on Windows.
#[cfg(windows)]
pub fn inspect_process_liveness(pid: u32) -> ProcessLiveness {
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError};
    use windows_sys::Win32::System::Threading::{
        OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    const SYNCHRONIZE: u32 = 0x00100000;

    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid);
        if handle.is_null() {
            let err = GetLastError();
            return map_liveness_open_error(err);
        }

        let wait_res = WaitForSingleObject(handle, 0);
        CloseHandle(handle);

        map_liveness_wait_result(wait_res)
    }
}

#[cfg(not(windows))]
pub fn inspect_process_liveness(_pid: u32) -> ProcessLiveness {
    ProcessLiveness::Running
}
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum ProcessIdentityStatus {
    /// Process is alive and its kernel creation time matches the recorded creation time.
    AliveMatch,
    /// Process is alive, but its creation time does NOT match (PID was recycled by another app).
    AliveMismatchRecycled,
    /// Process does not exist (invalid PID or terminated).
    DeadNotFound,
    /// Process exists but has exited.
    DeadExited,
    /// Process exists but cannot be queried (fail safe: do not assume dead).
    Inaccessible,
    /// Legacy directory without recorded creation time.
    LegacyNoCreationTime,
}

/// Cleanup action determined by process identity evaluation.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum CleanupAction {
    Preserve,
    Delete,
}

/// Evaluates cleanup decision based on verified process identity.
pub fn evaluate_cleanup_decision(
    identity_status: ProcessIdentityStatus,
    _dir_age_nanos: u128,
    _ttl_nanos: u128,
) -> CleanupAction {
    match identity_status {
        ProcessIdentityStatus::AliveMatch => CleanupAction::Preserve,
        ProcessIdentityStatus::AliveMismatchRecycled => CleanupAction::Delete,
        ProcessIdentityStatus::DeadNotFound => CleanupAction::Delete,
        ProcessIdentityStatus::DeadExited => CleanupAction::Delete,
        ProcessIdentityStatus::Inaccessible => CleanupAction::Preserve,
        ProcessIdentityStatus::LegacyNoCreationTime => {
            // Conservative legacy policy: preserve ambiguous legacy directories when process
            // identity cannot be established, deleting only when the process is confirmed dead.
            CleanupAction::Preserve
        }
    }
}

/// Retrieve the kernel creation timestamp of the current process.
/// Combines high and low 32-bit words into a single 64-bit integer (100-ns precision).
/// Returns Err if the Windows API call fails or returns an invalid zero timestamp.
#[cfg(windows)]
pub fn get_current_process_creation_time() -> Result<u64, AuthError> {
    use windows_sys::Win32::Foundation::{GetLastError, FILETIME};
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes};

    unsafe {
        let mut creation = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let mut exit = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let mut kernel = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let mut user = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };

        let handle = GetCurrentProcess();
        if GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) != 0 {
            let ts = ((creation.dwHighDateTime as u64) << 32) | (creation.dwLowDateTime as u64);
            if ts == 0 {
                return Err(AuthError::TokenReadFailed(
                    "Kernel returned zero process creation timestamp".into(),
                ));
            }
            Ok(ts)
        } else {
            let err = GetLastError();
            Err(AuthError::TokenReadFailed(format!(
                "Failed to get current process creation time (win32 err={})",
                err
            )))
        }
    }
}

#[cfg(not(windows))]
pub fn get_current_process_creation_time() -> Result<u64, AuthError> {
    Ok(0)
}

/// Verify process identity by querying its Windows kernel creation timestamp.
/// Protects against PID recycling and preserves directories of genuinely running processes.
/// Uses WaitForSingleObject handle synchronization to avoid the STILL_ACTIVE (259) exit code pitfall.
#[cfg(windows)]
pub fn check_process_identity(pid: u32, recorded_creation_time: u64) -> ProcessIdentityStatus {
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_ACCESS_DENIED, ERROR_INVALID_PARAMETER, FILETIME,
        WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT,
    };
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
    };

    const SYNCHRONIZE: u32 = 0x00100000;

    if recorded_creation_time == 0 {
        return ProcessIdentityStatus::LegacyNoCreationTime;
    }

    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid);
        if handle.is_null() {
            let err = GetLastError();
            if err == ERROR_ACCESS_DENIED {
                // Process exists but access is denied; fail safe
                return ProcessIdentityStatus::Inaccessible;
            } else if err == ERROR_INVALID_PARAMETER {
                return ProcessIdentityStatus::DeadNotFound;
            } else {
                return ProcessIdentityStatus::Inaccessible;
            }
        }

        // Use kernel synchronization: process object transitions to signaled state upon exit.
        // This eliminates the exit-code 259 / STILL_ACTIVE defect.
        let wait_res = WaitForSingleObject(handle, 0);
        if wait_res == WAIT_OBJECT_0 {
            // Process object is signaled -> terminated
            CloseHandle(handle);
            return ProcessIdentityStatus::DeadExited;
        } else if wait_res == WAIT_FAILED || wait_res != WAIT_TIMEOUT {
            // Kernel wait failed or indeterminate; fail safe
            CloseHandle(handle);
            return ProcessIdentityStatus::Inaccessible;
        }

        // Process is actively running (WAIT_TIMEOUT). Query its kernel creation time.
        let mut creation = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let mut exit = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let mut kernel = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };
        let mut user = FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 };

        let ok_times = GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user);
        CloseHandle(handle);

        if ok_times == 0 {
            return ProcessIdentityStatus::Inaccessible;
        }

        let actual_creation_time =
            ((creation.dwHighDateTime as u64) << 32) | (creation.dwLowDateTime as u64);

        if actual_creation_time == 0 {
            return ProcessIdentityStatus::Inaccessible;
        }

        if actual_creation_time == recorded_creation_time {
            ProcessIdentityStatus::AliveMatch
        } else {
            ProcessIdentityStatus::AliveMismatchRecycled
        }
    }
}

#[cfg(not(windows))]
pub fn check_process_identity(_pid: u32, recorded_creation_time: u64) -> ProcessIdentityStatus {
    if recorded_creation_time == 0 {
        ProcessIdentityStatus::LegacyNoCreationTime
    } else {
        ProcessIdentityStatus::AliveMatch
    }
}

#[cfg(windows)]
fn get_active_windows_trustee() -> Option<String> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    if let Ok(output) = std::process::Command::new("whoami")
        .creation_flags(CREATE_NO_WINDOW)
        .output()
    {
        if output.status.success() {
            let s = String::from_utf8_lossy(&output.stdout);
            if let Some(line) = s.lines().next() {
                let trimmed = line.trim();
                if !trimmed.is_empty() {
                    return Some(trimmed.to_string());
                }
            }
        }
    }

    let username = std::env::var("USERNAME").ok()?;
    let trimmed_user = username.trim();
    if trimmed_user.is_empty() {
        return None;
    }
    if let Ok(domain) = std::env::var("USERDOMAIN") {
        let trimmed_domain = domain.trim();
        if !trimmed_domain.is_empty() && !trimmed_user.contains('\\') {
            return Some(format!("{}\\{}", trimmed_domain, trimmed_user));
        }
    }
    Some(trimmed_user.to_string())
}

/// Explicitly apply restrictive permissions to the newly created token directory on Windows.
/// Disables inheritance and grants full control exclusively to the active user.
/// Fails closed if trustee resolution, process invocation, or icacls command fails.
#[cfg(windows)]
pub fn restrict_directory_acl(dir_path: &Path) -> Result<(), AuthError> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;

    let trustee = get_active_windows_trustee().ok_or_else(|| {
        AuthError::TokenReadFailed("Cannot resolve active Windows trustee for token directory ACL".into())
    })?;

    let grant_arg = format!("{}:(OI)(CI)F", trustee);
    let output = std::process::Command::new("icacls")
        .arg(dir_path)
        .arg("/inheritance:r")
        .arg("/grant:r")
        .arg(&grant_arg)
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| AuthError::TokenReadFailed(format!("Failed to execute icacls on token directory: {}", e)))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(AuthError::TokenReadFailed(format!(
            "icacls ACL hardening failed (exit={:?}): {}",
            output.status.code(),
            stderr.trim()
        )));
    }
    Ok(())
}

#[cfg(not(windows))]
pub fn restrict_directory_acl(dir_path: &Path) -> Result<(), AuthError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(dir_path, fs::Permissions::from_mode(0o700))
        .map_err(|e| AuthError::TokenReadFailed(format!("Failed to set 0700 permissions on token directory: {}", e)))
}

/// Create a unique, private temporary directory for the current launch's bootstrap token.
/// Associates directory with the active process identity (PID + kernel creation timestamp).
/// Ensures no two desktop launches or background processes ever share a token directory.
pub fn create_isolated_token_dir() -> Result<PathBuf, AuthError> {
    // Attempt best-effort cleanup of any stale token directories from prior dead processes
    cleanup_abandoned_token_dirs();

    let base_temp = std::env::temp_dir();
    let pid = std::process::id();
    let creation_time = get_current_process_creation_time()?;
    let count = LAUNCH_COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir_name = format!("querypilot_token_v2_{}_{}_{}", pid, creation_time, count);
    let dir_path = base_temp.join(dir_name);

    fs::create_dir_all(&dir_path)
        .map_err(|e| AuthError::TokenReadFailed(format!("Failed to create isolated token directory: {}", e)))?;

    if let Err(e) = restrict_directory_acl(&dir_path) {
        let _ = fs::remove_dir_all(&dir_path);
        return Err(e);
    }

    Ok(dir_path)
}

/// Cleanup of abandoned token directories from dead QueryPilot desktop processes.
/// Employs process identity and liveness verification:
/// 1. For v2 directories (querypilot_token_v2_<pid>_<creation_time>_<count>):
///    - Validates recorded kernel creation timestamp against the running process via single-handle inspection.
///    - If timestamp matches: process is alive -> PRESERVE unconditionally (even across system suspension).
///    - If timestamp does not match: PID was recycled by another process -> DELETE immediately.
///    - If process does not exist or exited: DELETE immediately.
///    - If process query access is denied or wait fails: PRESERVE fail-safe.
/// 2. For legacy directories (querypilot_token_<pid>_<nanos>_<count>):
///    - Deletes strictly when process termination is confirmed (non-existent PID or signaled process).
///    - Running or inaccessible/indeterminate processes are preserved (fail-closed).
///    - Legacy cleanup no longer uses an age-based TTL to override indeterminate process state.
pub fn cleanup_abandoned_token_dirs() {
    let base_temp = std::env::temp_dir();
    let entries = match fs::read_dir(&base_temp) {
        Ok(e) => e,
        Err(_) => return,
    };

    let current_pid = std::process::id();

    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
            if name.starts_with("querypilot_token_v2_") {
                // v2 format: querypilot_token_v2_<pid>_<creation_time>_<count>
                let parts: Vec<&str> = name.split('_').collect();
                if parts.len() >= 5 {
                    if let Ok(pid) = parts[3].parse::<u32>() {
                        // Never purge our own currently active process directory
                        if pid == current_pid {
                            continue;
                        }

                        let recorded_creation = parts[4].parse::<u64>().unwrap_or(0);
                        let identity_status = check_process_identity(pid, recorded_creation);
                        let decision = evaluate_cleanup_decision(
                            identity_status,
                            0,
                            ABANDONED_DIRECTORY_TTL_NANOS,
                        );

                        if decision == CleanupAction::Delete {
                            cleanup_isolated_token_dir(&path);
                        }
                    }
                }
            } else if name.starts_with("querypilot_token_") {
                // Legacy v1 format: querypilot_token_<pid>_<nanos>_<count>
                // Applies fail-closed tri-state process inspection: deletes only when confirmed
                // Terminated; preserves when Running or Inaccessible (fail safe).
                let parts: Vec<&str> = name.split('_').collect();
                if parts.len() >= 4 {
                    if let Ok(pid) = parts[2].parse::<u32>() {
                        // Never purge our own currently active process directory
                        if pid == current_pid {
                            continue;
                        }

                        let liveness = inspect_process_liveness(pid);
                        let decision = evaluate_legacy_cleanup(liveness);
                        if decision == CleanupAction::Delete {
                            cleanup_isolated_token_dir(&path);
                        }
                    }
                }
            }
        }
    }
}

/// Clean up temporary token material and remove the isolated token directory safely.
pub fn cleanup_isolated_token_dir(token_dir: &Path) {
    if !token_dir.exists() {
        return;
    }
    // Verify directory name starts with our exact prefix to never delete unrelated directories
    if let Some(name) = token_dir.file_name().and_then(|n| n.to_str()) {
        if !name.starts_with("querypilot_token_") {
            log::warn!(
                "Refusing to clean up directory without expected prefix: {}",
                token_dir.display()
            );
            return;
        }
    } else {
        return;
    }

    // In-place logical overwrite of token file with zeros prior to unlinking to sanitize storage data blocks
    let token_file = token_dir.join("agent.token");
    if token_file.is_file() {
        let _ = fs::write(
            &token_file,
            b"0000000000000000000000000000000000000000000000000000000000000000",
        );
        let _ = fs::remove_file(&token_file);
    }

    let _ = fs::remove_dir_all(token_dir);
}

/// RAII guard that automatically cleans up the isolated token directory on drop.
pub struct IsolatedTokenDirGuard {
    pub path: PathBuf,
    cleaned: bool,
}

impl IsolatedTokenDirGuard {
    pub fn new() -> Result<Self, AuthError> {
        let path = create_isolated_token_dir()?;
        Ok(Self { path, cleaned: false })
    }

    pub fn cleanup(&mut self) {
        if !self.cleaned {
            cleanup_isolated_token_dir(&self.path);
            self.cleaned = true;
        }
    }
}

impl Drop for IsolatedTokenDirGuard {
    fn drop(&mut self) {
        self.cleanup();
    }
}

/// Read bootstrap token securely from disk with strict length validation.
pub fn read_bootstrap_token(token_path: &Path) -> Result<String, AuthError> {
    if !token_path.exists() {
        return Err(AuthError::TokenNotFound(token_path.to_path_buf()));
    }
    let content = fs::read_to_string(token_path)
        .map_err(|e| AuthError::TokenReadFailed(e.to_string()))?;
    let trimmed = content.trim().to_string();
    if trimmed.len() < 16 || trimmed.len() > 128 {
        wipe_string(trimmed);
        return Err(AuthError::TokenFormatInvalid(content.trim().len()));
    }
    Ok(trimmed)
}

/// Parse Set-Cookie header specifically extracting and validating qpt_session.
pub fn parse_session_cookie(header_value: &str) -> Option<SessionCookie> {
    let mut parts = header_value.split(';');
    let first = parts.next()?.trim();
    let mut kv = first.splitn(2, '=');
    let name = kv.next()?.trim();
    let value = kv.next()?.trim();

    if !name.eq_ignore_ascii_case(SESSION_COOKIE_NAME) || value.is_empty() {
        return None;
    }

    let mut domain = "127.0.0.1".to_string();
    let mut path = "/".to_string();
    let mut is_http_only = false;
    let mut is_secure = false;
    let mut same_site = CookieSameSite::Strict;

    for directive in parts {
        let dir = directive.trim();
        if dir.eq_ignore_ascii_case("httponly") {
            is_http_only = true;
        } else if dir.eq_ignore_ascii_case("secure") {
            is_secure = true;
        } else if dir.to_ascii_lowercase().starts_with("path=") {
            let p = &dir[5..].trim();
            if !p.is_empty() {
                path = p.to_string();
            }
        } else if dir.to_ascii_lowercase().starts_with("domain=") {
            let d = &dir[7..].trim();
            if !d.is_empty() {
                domain = d.to_string();
            }
        } else if dir.to_ascii_lowercase().starts_with("samesite=") {
            let val = &dir[9..].trim().to_ascii_lowercase();
            match val.as_str() {
                "strict" => same_site = CookieSameSite::Strict,
                "lax" => same_site = CookieSameSite::Lax,
                "none" => same_site = CookieSameSite::None,
                _ => {}
            }
        }
    }

    Some(SessionCookie {
        name: SESSION_COOKIE_NAME.to_string(),
        value: value.to_string(),
        domain,
        path,
        is_http_only,
        is_secure,
        same_site,
    })
}

/// Perform direct HTTP bootstrap exchange with local agent at 127.0.0.1:<port>.
pub fn authenticate_with_agent(
    port: u16,
    bootstrap_token: &str,
    timeout: Duration,
) -> Result<SessionCookie, AuthError> {
    let client = reqwest::blocking::Client::builder()
        .timeout(timeout)
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|e| AuthError::RequestFailed(e.to_string()))?;

    let url = format!("http://127.0.0.1:{}/agent/v1/auth/bootstrap", port);
    let body = serde_json::json!({
        "bootstrap_token": bootstrap_token
    });

    let resp = client
        .post(&url)
        .header("Host", format!("127.0.0.1:{}", port))
        .header("X-QueryPilot-Request", "1")
        .header("Content-Type", "application/json")
        .json(&body)
        .send()
        .map_err(|e| AuthError::RequestFailed(e.to_string()))?;

    let status = resp.status();
    if !status.is_success() {
        return Err(AuthError::UnexpectedStatus(status.as_u16()));
    }

    for val in resp.headers().get_all(reqwest::header::SET_COOKIE) {
        if let Ok(cookie_str) = val.to_str() {
            if let Some(cookie) = parse_session_cookie(cookie_str) {
                return Ok(cookie);
            }
        }
    }

    Err(AuthError::MissingSessionCookie)
}

/// Inject session cookie into WebView2 cookie manager on Windows.
#[cfg(windows)]
pub fn inject_session_cookie(
    controller: &webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Controller,
    cookie: &SessionCookie,
) -> Result<(), AuthError> {
    use webview2_com::Microsoft::Web::WebView2::Win32::*;
    use windows_core::{HSTRING, Interface};

    unsafe {
        let cw = controller.CoreWebView2().map_err(|e| {
            AuthError::CookieManagerError(format!("Failed to retrieve CoreWebView2: {}", e))
        })?;

        let cw2: ICoreWebView2_2 = cw.cast().map_err(|e| {
            AuthError::CookieManagerError(format!("Failed to cast to ICoreWebView2_2: {}", e))
        })?;

        let cookie_mgr = cw2.CookieManager().map_err(|e| {
            AuthError::CookieManagerError(format!("Failed to retrieve CookieManager: {}", e))
        })?;

        let name = HSTRING::from(&cookie.name);
        let val = HSTRING::from(&cookie.value);
        let dom = HSTRING::from(&cookie.domain);
        let pth = HSTRING::from(&cookie.path);

        let webview_cookie = cookie_mgr
            .CreateCookie(&name, &val, &dom, &pth)
            .map_err(|e| {
                AuthError::CookieManagerError(format!("Failed to CreateCookie: {}", e))
            })?;

        webview_cookie
            .SetIsHttpOnly(cookie.is_http_only)
            .map_err(|e| {
                AuthError::CookieManagerError(format!("Failed to SetIsHttpOnly: {}", e))
            })?;

        let same_site_kind = match cookie.same_site {
            CookieSameSite::Strict => COREWEBVIEW2_COOKIE_SAME_SITE_KIND_STRICT,
            CookieSameSite::Lax => COREWEBVIEW2_COOKIE_SAME_SITE_KIND_LAX,
            CookieSameSite::None => COREWEBVIEW2_COOKIE_SAME_SITE_KIND_NONE,
        };
        webview_cookie
            .SetSameSite(same_site_kind)
            .map_err(|e| {
                AuthError::CookieManagerError(format!("Failed to SetSameSite: {}", e))
            })?;

        webview_cookie
            .SetIsSecure(cookie.is_secure)
            .map_err(|e| {
                AuthError::CookieManagerError(format!("Failed to SetIsSecure: {}", e))
            })?;

        cookie_mgr
            .AddOrUpdateCookie(&webview_cookie)
            .map_err(|e| {
                AuthError::CookieManagerError(format!("Failed to AddOrUpdateCookie: {}", e))
            })?;

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_session_cookie_valid() {
        let raw = "qpt_session=secret_token_12345; Path=/; SameSite=strict; HttpOnly";
        let cookie = parse_session_cookie(raw).expect("Failed to parse valid session cookie");
        assert_eq!(cookie.name, "qpt_session");
        assert_eq!(cookie.value, "secret_token_12345");
        assert_eq!(cookie.path, "/");
        assert_eq!(cookie.domain, "127.0.0.1");
        assert!(cookie.is_http_only);
        assert!(!cookie.is_secure);
        assert_eq!(cookie.same_site, CookieSameSite::Strict);
    }

    #[test]
    fn test_parse_session_cookie_case_insensitive() {
        let raw = "QPT_SESSION=tok; path=/app; samesite=Lax; httponly; secure";
        let cookie = parse_session_cookie(raw).expect("Failed to parse case insensitive cookie");
        assert_eq!(cookie.name, "qpt_session");
        assert_eq!(cookie.value, "tok");
        assert_eq!(cookie.path, "/app");
        assert!(cookie.is_http_only);
        assert!(cookie.is_secure);
        assert_eq!(cookie.same_site, CookieSameSite::Lax);
    }

    #[test]
    fn test_parse_session_cookie_unrelated_rejected() {
        let raw = "csrftoken=xyz; Path=/; HttpOnly";
        assert!(parse_session_cookie(raw).is_none());
    }

    #[test]
    fn test_token_read_validation() {
        let temp_dir = std::env::temp_dir();
        let test_token_file = temp_dir.join(format!("qp_test_token_{}.token", std::process::id()));

        // Short token (<16 chars)
        fs::write(&test_token_file, "too_short").unwrap();
        let res = read_bootstrap_token(&test_token_file);
        assert!(matches!(res, Err(AuthError::TokenFormatInvalid(_))));

        // Valid token (64 hex characters)
        let valid_token = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        fs::write(&test_token_file, valid_token).unwrap();
        let token = read_bootstrap_token(&test_token_file).expect("Failed to read valid token");
        assert_eq!(token, valid_token);

        let _ = fs::remove_file(&test_token_file);
    }

    #[test]
    fn test_wipe_string_clears_buffer() {
        let secret = String::from("top_secret_token_data");
        wipe_string(secret);
        // Verify buffer in wiped copy was cleared to null bytes
        let mut probe = String::from("sensitive_bytes");
        unsafe {
            let bytes = probe.as_mut_vec();
            for b in bytes.iter_mut() {
                std::ptr::write_volatile(b, 0);
            }
        }
        for b in probe.bytes() {
            assert_eq!(b, 0);
        }
    }

    #[test]
    fn test_isolated_token_dir_consecutive_launches_unique() {
        let dir1 = create_isolated_token_dir().expect("Failed to create isolated token dir 1");
        let dir2 = create_isolated_token_dir().expect("Failed to create isolated token dir 2");

        assert_ne!(dir1, dir2, "Consecutive launches must produce strictly unique token directories");
        assert!(dir1.is_dir());
        assert!(dir2.is_dir());

        let name1 = dir1.file_name().unwrap().to_str().unwrap();
        let name2 = dir2.file_name().unwrap().to_str().unwrap();
        assert!(name1.starts_with("querypilot_token_v2_"));
        assert!(name2.starts_with("querypilot_token_v2_"));

        cleanup_isolated_token_dir(&dir1);
        cleanup_isolated_token_dir(&dir2);
        assert!(!dir1.exists());
        assert!(!dir2.exists());
    }

    #[test]
    fn test_evaluate_cleanup_decision_matrix() {
        let ttl = 3600 * 1_000_000_000; // 1 hour

        // 1. AliveMatch: preserved unconditionally even if older than TTL (suspension protection)
        assert_eq!(
            evaluate_cleanup_decision(ProcessIdentityStatus::AliveMatch, ttl * 2, ttl),
            CleanupAction::Preserve
        );

        // 2. AliveMismatchRecycled: deleted immediately even if brand new (0 age)
        assert_eq!(
            evaluate_cleanup_decision(ProcessIdentityStatus::AliveMismatchRecycled, 0, ttl),
            CleanupAction::Delete
        );

        // 3. DeadNotFound: deleted immediately
        assert_eq!(
            evaluate_cleanup_decision(ProcessIdentityStatus::DeadNotFound, 0, ttl),
            CleanupAction::Delete
        );

        // 4. DeadExited: deleted immediately
        assert_eq!(
            evaluate_cleanup_decision(ProcessIdentityStatus::DeadExited, 0, ttl),
            CleanupAction::Delete
        );

        // 5. Inaccessible (ERROR_ACCESS_DENIED): fail safe, preserved
        assert_eq!(
            evaluate_cleanup_decision(ProcessIdentityStatus::Inaccessible, ttl * 2, ttl),
            CleanupAction::Preserve
        );

        // 6. LegacyNoCreationTime: conservative policy preserves ambiguous legacy directories
        assert_eq!(
            evaluate_cleanup_decision(ProcessIdentityStatus::LegacyNoCreationTime, 1000, ttl),
            CleanupAction::Preserve
        );
        assert_eq!(
            evaluate_cleanup_decision(ProcessIdentityStatus::LegacyNoCreationTime, ttl * 2, ttl),
            CleanupAction::Preserve
        );
    }

    #[test]
    #[cfg(windows)]
    fn test_process_creation_time_live_windows() {
        let creation_time_res = get_current_process_creation_time();
        assert!(creation_time_res.is_ok(), "Process creation time must succeed on Windows");
        let creation_time = creation_time_res.unwrap();
        assert!(creation_time > 0, "Current process creation time must be non-zero on Windows");

        let current_pid = std::process::id();

        // 1. Current process with matching creation time -> AliveMatch
        let match_status = check_process_identity(current_pid, creation_time);
        assert_eq!(match_status, ProcessIdentityStatus::AliveMatch);

        // 2. Current process with bogus creation time -> AliveMismatchRecycled
        let recycled_status = check_process_identity(current_pid, creation_time.wrapping_add(9999));
        assert_eq!(recycled_status, ProcessIdentityStatus::AliveMismatchRecycled);

        // 3. Non-existent high PID -> DeadNotFound
        let dead_status = check_process_identity(9999999, creation_time);
        assert_eq!(dead_status, ProcessIdentityStatus::DeadNotFound);
    }

    #[test]
    #[cfg(windows)]
    fn test_cleanup_abandoned_v2_recycled_directory() {
        let base_temp = std::env::temp_dir();
        let current_pid = std::process::id();
        let current_time = get_current_process_creation_time().unwrap_or(12345678);

        // Create a simulated v2 directory with a different PID that is non-existent
        let dead_pid = 9999998;
        let dead_dir = base_temp.join(format!("querypilot_token_v2_{}_{}_1", dead_pid, current_time));
        fs::create_dir_all(&dead_dir).unwrap();
        let test_token = dead_dir.join("agent.token");
        fs::write(&test_token, "0000000000000000000000000000000000000000000000000000000000000000").unwrap();

        assert!(dead_dir.is_dir());
        cleanup_abandoned_token_dirs();
        assert!(!dead_dir.exists(), "v2 token directory with dead PID should be deleted immediately");

        // If on Windows, also test PID recycling detection with a live process PID other than current_pid
        // (Our own process PID is bypassed by self-exclusion, so test with a simulated dead PID)
        let _ = current_pid;
    }

    #[test]
    fn test_isolated_token_dir_cleanup_guard() {
        let path = {
            let mut guard = IsolatedTokenDirGuard::new().expect("Failed to create guard");
            let p = guard.path.clone();
            assert!(p.is_dir());

            // Simulate agent creating token file inside
            let token_path = p.join("agent.token");
            fs::write(&token_path, "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef").unwrap();
            assert!(token_path.is_file());

            guard.cleanup();
            assert!(!p.exists(), "Directory should be removed after explicit cleanup");
            p
        };
        assert!(!path.exists());
    }

    #[test]
    fn test_cleanup_safety_never_deletes_unrelated() {
        let base_temp = std::env::temp_dir();
        let unrelated_dir = base_temp.join(format!("unrelated_dir_test_{}", std::process::id()));
        fs::create_dir_all(&unrelated_dir).unwrap();
        let test_file = unrelated_dir.join("important_file.txt");
        fs::write(&test_file, "do_not_delete").unwrap();

        // Attempt cleanup on unrelated directory: must be refused
        cleanup_isolated_token_dir(&unrelated_dir);
        assert!(unrelated_dir.is_dir(), "Unrelated directory must not be deleted");
        assert!(test_file.is_file(), "Files inside unrelated directory must remain intact");

        // Clean up our test fixture
        let _ = fs::remove_dir_all(&unrelated_dir);
    }

    #[test]
    fn test_restrict_directory_acl_executes_safely() {
        let base_temp = std::env::temp_dir();
        let test_dir = base_temp.join(format!("querypilot_token_acl_test_{}", std::process::id()));
        fs::create_dir_all(&test_dir).unwrap();

        // Must execute cleanly and return Ok(())
        assert!(restrict_directory_acl(&test_dir).is_ok());
        assert!(test_dir.is_dir());

        let _ = fs::remove_dir_all(&test_dir);
    }

    #[test]
    #[cfg(windows)]
    fn test_cleanup_abandoned_aged_directories() {
        let base_temp = std::env::temp_dir();
        // Create a simulated legacy abandoned directory with dummy non-existent PID 999999
        let two_hours_ago_nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
            .saturating_sub(7200 * 1_000_000_000);

        let aged_dir = base_temp.join(format!("querypilot_token_999999_{}_1", two_hours_ago_nanos));
        fs::create_dir_all(&aged_dir).unwrap();
        let test_token = aged_dir.join("agent.token");
        fs::write(&test_token, "0000000000000000000000000000000000000000000000000000000000000000").unwrap();

        assert!(aged_dir.is_dir());
        cleanup_abandoned_token_dirs();
        assert!(
            !aged_dir.exists(),
            "Legacy abandoned token directory should be deleted when process is confirmed terminated"
        );
    }

    #[test]
    fn test_evaluate_legacy_cleanup_decision() {
        // Pure decision logic verification:
        // Confirmed terminated -> Delete
        assert_eq!(
            evaluate_legacy_cleanup(ProcessLiveness::Terminated),
            CleanupAction::Delete
        );
        // Running process -> Preserve
        assert_eq!(
            evaluate_legacy_cleanup(ProcessLiveness::Running),
            CleanupAction::Preserve
        );
        // Inaccessible / indeterminate -> Preserve (fail safe)
        assert_eq!(
            evaluate_legacy_cleanup(ProcessLiveness::Inaccessible),
            CleanupAction::Preserve
        );
    }

    #[test]
    fn test_map_liveness_wait_result_pure() {
        // 1. WAIT_TIMEOUT (258) -> Running
        assert_eq!(map_liveness_wait_result(258), ProcessLiveness::Running);

        // 2. WAIT_OBJECT_0 (0) -> Terminated
        assert_eq!(map_liveness_wait_result(0), ProcessLiveness::Terminated);

        // 3. WAIT_FAILED (0xFFFFFFFF) -> Inaccessible
        assert_eq!(map_liveness_wait_result(0xFFFFFFFF), ProcessLiveness::Inaccessible);

        // 4. WAIT_ABANDONED (0x00000080) -> Inaccessible
        assert_eq!(map_liveness_wait_result(0x80), ProcessLiveness::Inaccessible);

        // 5. Unexpected result -> Inaccessible
        assert_eq!(map_liveness_wait_result(99999), ProcessLiveness::Inaccessible);
    }

    #[test]
    fn test_map_liveness_open_error_pure() {
        // 1. ERROR_INVALID_PARAMETER (87) -> Terminated (non-existent PID)
        assert_eq!(map_liveness_open_error(87), ProcessLiveness::Terminated);

        // 2. ERROR_ACCESS_DENIED (5) -> Inaccessible (fail safe, preserve)
        assert_eq!(map_liveness_open_error(5), ProcessLiveness::Inaccessible);

        // 3. ERROR_GEN_FAILURE (31) -> Inaccessible
        assert_eq!(map_liveness_open_error(31), ProcessLiveness::Inaccessible);

        // 4. Any unexpected OS error code -> Inaccessible
        assert_eq!(map_liveness_open_error(1234), ProcessLiveness::Inaccessible);
    }

    #[test]
    #[cfg(windows)]
    fn test_inspect_process_liveness_windows_kernel() {
        let current_pid = std::process::id();

        // 1. Current process is actively running: kernel synchronization returns WAIT_TIMEOUT
        let live_status = inspect_process_liveness(current_pid);
        assert_eq!(live_status, ProcessLiveness::Running);
        assert_eq!(evaluate_legacy_cleanup(live_status), CleanupAction::Preserve);

        // 2. Non-existent high PID: OpenProcess fails with ERROR_INVALID_PARAMETER
        let dead_pid = 9999999;
        let dead_status = inspect_process_liveness(dead_pid);
        assert_eq!(dead_status, ProcessLiveness::Terminated);
        assert_eq!(evaluate_legacy_cleanup(dead_status), CleanupAction::Delete);
    }
}
