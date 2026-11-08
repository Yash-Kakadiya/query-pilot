//! Process management module for QueryPilot desktop sidecar supervision.
//!
//! Enforces:
//! 1. Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
//! 2. Suspended sidecar process creation (`CreateProcessW` with `CREATE_SUSPENDED`).
//! 3. Explicit handle inheritance whitelisting (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`) ensuring
//!    only child stdout/stderr write handles are inherited and no parent handles leak.
//! 4. Non-inheritable parent read handles (`HANDLE_FLAG_INHERIT = 0`).
//! 5. Immediate assignment to the Job Object before resuming the primary thread.
//! 6. Inheritable stdout/stderr pipes with background draining to prevent buffer deadlocks.
//! 7. Parsing and validation of `QUERYPILOT_READY:{"status":"ready","port":<port>,"pid":<pid>}`.
//! 8. Deterministic termination and prevention of orphaned processes on all code paths.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::FromRawHandle;
use std::path::{Path, PathBuf};
use std::ptr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Deserialize;
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, SetHandleInformation, ERROR_ALREADY_EXISTS, HANDLE,
};
use windows_sys::Win32::Security::SECURITY_ATTRIBUTES;
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
    JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Pipes::CreatePipe;
use windows_sys::Win32::System::Threading::{
    CreateMutexW, CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess,
    InitializeProcThreadAttributeList, ResumeThread, TerminateProcess,
    UpdateProcThreadAttribute, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED,
    EXTENDED_STARTUPINFO_PRESENT, INFINITE, PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    STARTF_USESTDHANDLES, STARTUPINFOEXW,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    FindWindowW, MessageBoxW, SetForegroundWindow, MB_ICONERROR, MB_OK,
};

const HANDLE_FLAG_INHERIT: u32 = 0x00000001;

/// Payload parsed from the sidecar's machine-readable handshake line.
#[derive(Debug, Deserialize, Clone)]
pub struct HandshakeData {
    pub status: String,
    pub port: u16,
    pub pid: u32,
}

/// Owned handle wrapper ensuring Win32 `CloseHandle` is invoked on drop.
pub struct AutoHandle(pub HANDLE);

impl Drop for AutoHandle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != -1 as _ {
            unsafe {
                CloseHandle(self.0);
            }
            self.0 = ptr::null_mut();
        }
    }
}

unsafe impl Send for AutoHandle {}
unsafe impl Sync for AutoHandle {}

/// Supervised sidecar instance holding Job and Process handles.
pub struct SupervisedSidecar {
    pub port: u16,
    pub pid: u32,
    _job_handle: Arc<AutoHandle>,
    process_handle: Arc<AutoHandle>,
    pub stderr_buffer: Arc<Mutex<VecDeque<String>>>,
}

impl SupervisedSidecar {
    /// Explicitly terminate the sidecar process.
    pub fn terminate(&self) {
        unsafe {
            if !self.process_handle.0.is_null() {
                TerminateProcess(self.process_handle.0, 0);
            }
        }
    }

    /// Spawn a background thread that monitors unexpected termination of the sidecar process.
    pub fn monitor_termination<F>(&self, on_unexpected_exit: F)
    where
        F: FnOnce(u32) + Send + 'static,
    {
        let handle_wrapper = Arc::clone(&self.process_handle);
        std::thread::Builder::new()
            .name("querypilot-sidecar-monitor".into())
            .spawn(move || unsafe {
                let handle = handle_wrapper.0;
                if handle.is_null() {
                    return;
                }
                WaitForSingleObject(handle, INFINITE);
                let mut exit_code: u32 = 0;
                GetExitCodeProcess(handle, &mut exit_code);
                log::warn!(
                    "Supervised sidecar process exited with code: {}",
                    exit_code
                );
                on_unexpected_exit(exit_code);
            })
            .ok();
    }
}

/// Display a native Windows error dialog with title and message.
pub fn show_native_error(title: &str, message: &str) {
    let wide_title: Vec<u16> = std::ffi::OsStr::new(title)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let wide_message: Vec<u16> = std::ffi::OsStr::new(message)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        MessageBoxW(
            ptr::null_mut(),
            wide_message.as_ptr(),
            wide_title.as_ptr(),
            MB_ICONERROR | MB_OK,
        );
    }
}

/// Focus an existing window by title and bring it to foreground.
pub fn focus_existing_window(window_title: &str) {
    let wide_title: Vec<u16> = std::ffi::OsStr::new(window_title)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        let hwnd = FindWindowW(ptr::null(), wide_title.as_ptr());
        if !hwnd.is_null() {
            SetForegroundWindow(hwnd);
        }
    }
}

/// Parse and validate the machine-readable sidecar readiness handshake line.
/// Returns Ok(Some(HandshakeData)) on valid handshake, Ok(None) on non-handshake lines,
/// or Err(reason) on malformed JSON or invalid status/port/pid.
pub fn parse_handshake_line(line: &str) -> Result<Option<HandshakeData>, String> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }
    if let Some(json_payload) = trimmed.strip_prefix("QUERYPILOT_READY:") {
        match serde_json::from_str::<HandshakeData>(json_payload.trim()) {
            Ok(data) => {
                if data.status != "ready" {
                    return Err(format!(
                        "Handshake rejected: expected status 'ready', got '{}'",
                        data.status
                    ));
                }
                if data.port <= 1024 {
                    return Err(format!(
                        "Handshake rejected: port must be a non-privileged ephemeral port (>1024), got {}",
                        data.port
                    ));
                }
                if data.pid == 0 {
                    return Err("Handshake rejected: process ID must be positive".into());
                }
                Ok(Some(data))
            }
            Err(e) => Err(format!(
                "Invalid handshake JSON payload: {}: {}",
                e, trimmed
            )),
        }
    } else {
        Ok(None)
    }
}

/// Enforce single-instance application execution via a named Win32 Mutex.
/// Returns Ok(AutoHandle) for the primary instance, or Err if another instance exists.
pub fn enforce_single_instance(mutex_name: &str) -> Result<AutoHandle, String> {
    let wide_name: Vec<u16> = std::ffi::OsStr::new(mutex_name)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    let mutex = unsafe { CreateMutexW(ptr::null_mut(), 1, wide_name.as_ptr()) };
    if mutex.is_null() {
        return Err("Failed to create single-instance mutex.".into());
    }

    let last_err = unsafe { GetLastError() };
    if last_err == ERROR_ALREADY_EXISTS {
        unsafe { CloseHandle(mutex) };
        return Err("Another instance of QueryPilot Desktop is already running.".into());
    }

    Ok(AutoHandle(mutex))
}

/// Locate the packaged QueryPilot agent executable across standard directories.
pub fn find_sidecar_executable() -> Result<PathBuf, String> {
    // 0. Environment variable override (supports custom installs and testing)
    if let Ok(env_path) = std::env::var("QUERYPILOT_SIDECAR_PATH") {
        let p = PathBuf::from(env_path);
        if p.is_file() {
            return Ok(p);
        } else {
            return Err(format!(
                "Configured QUERYPILOT_SIDECAR_PATH '{}' does not exist or is not a file.",
                p.display()
            ));
        }
    }

    let mut candidates = Vec::new();

    // 1. Current executable directory (production/installed layout)
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(exe_dir) = current_exe.parent() {
            candidates.push(exe_dir.join("querypilot-agent").join("querypilot-agent.exe"));
            candidates.push(exe_dir.join("resources").join("querypilot-agent").join("querypilot-agent.exe"));
            candidates.push(exe_dir.join("resources").join("dist").join("querypilot-agent").join("querypilot-agent.exe"));
            candidates.push(exe_dir.join("bin").join("querypilot-agent.exe"));
            candidates.push(exe_dir.join("querypilot-agent.exe"));
        }
    }

    // 2. Current working directory / repository root (development layout)
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("dist").join("querypilot-agent").join("querypilot-agent.exe"));
        candidates.push(cwd.join("..").join("dist").join("querypilot-agent").join("querypilot-agent.exe"));
    }

    // 3. Relative to Tauri manifest directory if known at compile time
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    candidates.push(manifest_dir.join("..").join("dist").join("querypilot-agent").join("querypilot-agent.exe"));

    for candidate in candidates {
        if candidate.is_file() {
            if let Ok(canonical) = candidate.canonicalize() {
                return Ok(canonical);
            }
            return Ok(candidate);
        }
    }

    Err(
        "Could not locate 'querypilot-agent.exe'. Please ensure the sidecar has been packaged into 'dist/querypilot-agent/'."
            .into(),
    )
}

/// Launch the packaged sidecar supervised under a Win32 Job Object.
pub fn launch_supervised_sidecar(
    sidecar_path: &Path,
    custom_token_dir: Option<&Path>,
    custom_profiles_file: Option<&Path>,
) -> Result<SupervisedSidecar, String> {
    unsafe {
        // 1. Create and configure Win32 Job Object
        let job = CreateJobObjectW(ptr::null_mut(), ptr::null());
        if job.is_null() {
            return Err("Failed to create Win32 Job Object for sidecar supervision.".into());
        }
        let job_handle = Arc::new(AutoHandle(job));

        let mut job_info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        job_info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        let set_info_ok = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &job_info as *const _ as *const _,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if set_info_ok == 0 {
            return Err(format!(
                "Failed to set JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE on Job Object (err={}).",
                GetLastError()
            ));
        }

        // 2. Create pipes for stdout and stderr with inheritable write handles
        let mut sa: SECURITY_ATTRIBUTES = std::mem::zeroed();
        sa.nLength = std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32;
        sa.bInheritHandle = 1;

        let mut stdout_read: HANDLE = ptr::null_mut();
        let mut stdout_write: HANDLE = ptr::null_mut();
        if CreatePipe(&mut stdout_read, &mut stdout_write, &sa, 0) == 0 {
            return Err(format!("Failed to create stdout pipe (err={}).", GetLastError()));
        }
        // Read handle must strictly NOT be inherited by child
        SetHandleInformation(stdout_read, HANDLE_FLAG_INHERIT, 0);

        let mut stderr_read: HANDLE = ptr::null_mut();
        let mut stderr_write: HANDLE = ptr::null_mut();
        if CreatePipe(&mut stderr_read, &mut stderr_write, &sa, 0) == 0 {
            CloseHandle(stdout_read);
            CloseHandle(stdout_write);
            return Err(format!("Failed to create stderr pipe (err={}).", GetLastError()));
        }
        // Read handle must strictly NOT be inherited by child
        SetHandleInformation(stderr_read, HANDLE_FLAG_INHERIT, 0);

        // 3. Assemble command line
        let mut cmd_str = format!("\"{}\" --desktop", sidecar_path.display());
        if let Some(td) = custom_token_dir {
            cmd_str.push_str(&format!(" --token-dir \"{}\"", td.display()));
        }
        if let Some(pf) = custom_profiles_file {
            cmd_str.push_str(&format!(" --profiles-file \"{}\"", pf.display()));
        }

        let mut cmd_wide: Vec<u16> = std::ffi::OsStr::new(&cmd_str)
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();

        // 4. Configure STARTUPINFOEXW with explicit handle inheritance whitelist
        // This guarantees ONLY stdout_write and stderr_write are inherited by the child.
        // No other parent handles (job, mutex, sockets, files) can leak!
        let mut handles_to_inherit = [stdout_write, stderr_write];
        let mut attr_size: usize = 0;
        let _ = InitializeProcThreadAttributeList(ptr::null_mut(), 1, 0, &mut attr_size);

        let mut attr_buffer = vec![0u8; attr_size];
        let attr_list = attr_buffer.as_mut_ptr() as *mut _;
        let init_ok = InitializeProcThreadAttributeList(attr_list, 1, 0, &mut attr_size);
        if init_ok == 0 {
            CloseHandle(stdout_read);
            CloseHandle(stdout_write);
            CloseHandle(stderr_read);
            CloseHandle(stderr_write);
            return Err(format!(
                "Failed to initialize proc thread attribute list (err={}).",
                GetLastError()
            ));
        }

        let update_ok = UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            handles_to_inherit.as_mut_ptr() as *mut _,
            handles_to_inherit.len() * std::mem::size_of::<HANDLE>(),
            ptr::null_mut(),
            ptr::null_mut(),
        );
        if update_ok == 0 {
            DeleteProcThreadAttributeList(attr_list);
            CloseHandle(stdout_read);
            CloseHandle(stdout_write);
            CloseHandle(stderr_read);
            CloseHandle(stderr_write);
            return Err(format!(
                "Failed to configure handle inheritance whitelist (err={}).",
                GetLastError()
            ));
        }

        let mut siex: STARTUPINFOEXW = std::mem::zeroed();
        siex.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        siex.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        siex.StartupInfo.hStdOutput = stdout_write;
        siex.StartupInfo.hStdError = stderr_write;
        siex.StartupInfo.hStdInput = ptr::null_mut();
        siex.lpAttributeList = attr_list;

        let mut pi: PROCESS_INFORMATION = std::mem::zeroed();

        // 5. Create sidecar suspended with explicit attribute list and no console window
        let create_ok = CreateProcessW(
            ptr::null(),
            cmd_wide.as_mut_ptr(),
            ptr::null_mut(),
            ptr::null_mut(),
            1, // bInheritHandles = TRUE (whitelisted exclusively to handles_to_inherit)
            CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
            ptr::null_mut(),
            ptr::null(),
            &siex.StartupInfo,
            &mut pi,
        );

        DeleteProcThreadAttributeList(attr_list);

        // Immediately close parent's write handles so child holds the only writers
        CloseHandle(stdout_write);
        CloseHandle(stderr_write);

        if create_ok == 0 {
            CloseHandle(stdout_read);
            CloseHandle(stderr_read);
            return Err(format!(
                "Failed to spawn sidecar executable (err={}). Path: {}",
                GetLastError(),
                sidecar_path.display()
            ));
        }

        // Wrap process handle in AutoHandle immediately so drops close it safely
        let process_handle = Arc::new(AutoHandle(pi.hProcess));

        // 6. Assign process to Job Object strictly BEFORE resuming
        let assign_ok = AssignProcessToJobObject(job, pi.hProcess);
        if assign_ok == 0 {
            let err = GetLastError();
            TerminateProcess(pi.hProcess, 1);
            CloseHandle(pi.hThread);
            CloseHandle(stdout_read);
            CloseHandle(stderr_read);
            return Err(format!(
                "Failed to assign sidecar to Windows Job Object (err={}).",
                err
            ));
        }

        // 7. Resume primary thread
        let resume_res = ResumeThread(pi.hThread);
        CloseHandle(pi.hThread);
        if resume_res == u32::MAX {
            let err = GetLastError();
            TerminateProcess(pi.hProcess, 1);
            CloseHandle(stdout_read);
            CloseHandle(stderr_read);
            return Err(format!("Failed to resume sidecar primary thread (err={}).", err));
        }

        // 8. Background thread for stderr drain with length ceiling (prevents deadlock)
        let stderr_file = File::from_raw_handle(stderr_read as _);
        let stderr_buffer: Arc<Mutex<VecDeque<String>>> =
            Arc::new(Mutex::new(VecDeque::with_capacity(100)));
        let stderr_buf_clone = Arc::clone(&stderr_buffer);

        std::thread::Builder::new()
            .name("querypilot-sidecar-stderr".into())
            .spawn(move || {
                let reader = BufReader::new(stderr_file);
                for line in reader.lines() {
                    match line {
                        Ok(l) => {
                            let mut truncated = l;
                            if truncated.len() > 2048 {
                                truncated.truncate(2048);
                                truncated.push_str("... [truncated]");
                            }
                            eprintln!("[sidecar:stderr] {}", truncated);
                            let mut buf = stderr_buf_clone.lock().unwrap();
                            if buf.len() >= 100 {
                                buf.pop_front();
                            }
                            buf.push_back(truncated);
                        }
                        Err(_) => break,
                    }
                }
            })
            .map_err(|e| format!("Failed to spawn stderr reader thread: {}", e))?;

        // 9. Read stdout line-by-line looking for QUERYPILOT_READY handshake
        let stdout_file = File::from_raw_handle(stdout_read as _);
        let mut stdout_reader = BufReader::new(stdout_file);

        let deadline = Instant::now() + Duration::from_secs(15);
        let mut handshake_data: Option<HandshakeData> = None;
        let mut initial_line = String::new();

        while Instant::now() < deadline {
            initial_line.clear();
            match stdout_reader.read_line(&mut initial_line) {
                Ok(0) => {
                    // EOF reached before handshake
                    break;
                }
                Ok(_) => {
                    match parse_handshake_line(&initial_line) {
                        Ok(Some(data)) => {
                            handshake_data = Some(data);
                            break;
                        }
                        Ok(None) => continue,
                        Err(err) => {
                            TerminateProcess(pi.hProcess, 1);
                            return Err(err);
                        }
                    }
                }
                Err(e) => {
                    TerminateProcess(pi.hProcess, 1);
                    return Err(format!("Error reading sidecar handshake: {}", e));
                }
            }
        }

        if let Some(handshake) = handshake_data {
            // Spawn background thread to continue consuming stdout
            std::thread::Builder::new()
                .name("querypilot-sidecar-stdout".into())
                .spawn(move || {
                    let mut line = String::new();
                    while let Ok(n) = stdout_reader.read_line(&mut line) {
                        if n == 0 {
                            break;
                        }
                        line.clear();
                    }
                })
                .ok();

            log::info!(
                "Supervised sidecar initialized successfully on port {} (pid={})",
                handshake.port,
                handshake.pid
            );
            Ok(SupervisedSidecar {
                port: handshake.port,
                pid: handshake.pid,
                _job_handle: job_handle,
                process_handle,
                stderr_buffer,
            })
        } else {
            // Sidecar failed or timed out. Terminate process and capture exit code.
            let mut exit_code: u32 = 0;
            GetExitCodeProcess(pi.hProcess, &mut exit_code);
            TerminateProcess(pi.hProcess, 1);

            let logs: Vec<String> = stderr_buffer.lock().unwrap().iter().cloned().collect();
            let logs_str = logs.join("\n");
            let err_msg = format!(
                "QueryPilot Python sidecar failed to complete startup handshake within 15s (exit_code={}).\n\nRecent sidecar logs:\n{}",
                exit_code,
                if logs_str.is_empty() { "(no stderr output recorded)" } else { &logs_str }
            );
            Err(err_msg)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_handshake_line_valid() {
        let line = "QUERYPILOT_READY:{\"status\":\"ready\",\"port\":54321,\"pid\":12345}";
        let res = parse_handshake_line(line).expect("Valid handshake failed to parse");
        let data = res.expect("Expected handshake data");
        assert_eq!(data.status, "ready");
        assert_eq!(data.port, 54321);
        assert_eq!(data.pid, 12345);
    }

    #[test]
    fn test_parse_handshake_line_non_handshake_ignored() {
        let line = "INFO: Uvicorn running on http://127.0.0.1:8000";
        let res = parse_handshake_line(line).expect("Non-handshake line should return Ok(None)");
        assert!(res.is_none());
    }

    #[test]
    fn test_parse_handshake_line_invalid_status() {
        let line = "QUERYPILOT_READY:{\"status\":\"failed\",\"port\":54321,\"pid\":12345}";
        let err = parse_handshake_line(line).expect_err("Expected error for non-ready status");
        assert!(err.contains("expected status 'ready'"));
    }

    #[test]
    fn test_parse_handshake_line_privileged_or_zero_port() {
        let line = "QUERYPILOT_READY:{\"status\":\"ready\",\"port\":80,\"pid\":12345}";
        let err = parse_handshake_line(line).expect_err("Expected error for privileged port");
        assert!(err.contains("must be a non-privileged ephemeral port"));
    }

    #[test]
    fn test_parse_handshake_line_zero_pid() {
        let line = "QUERYPILOT_READY:{\"status\":\"ready\",\"port\":54321,\"pid\":0}";
        let err = parse_handshake_line(line).expect_err("Expected error for pid 0");
        assert!(err.contains("process ID must be positive"));
    }

    #[test]
    fn test_parse_handshake_line_malformed_json() {
        let line = "QUERYPILOT_READY:{not valid json}";
        let err = parse_handshake_line(line).expect_err("Expected error for malformed JSON");
        assert!(err.contains("Invalid handshake JSON payload"));
    }
}
