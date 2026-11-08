//! Strict navigation validation and secure URL dispatch for QueryPilot desktop.

use std::os::windows::ffi::OsStrExt;
use std::ptr;
use tauri::Url;
use windows_sys::Win32::UI::Shell::ShellExecuteW;
use windows_sys::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum NavigationDecision {
    /// Permitted navigation inside the local application WebView.
    AllowInternal,
    /// Valid external HTTP/HTTPS URL to open in system default browser.
    OpenExternal(String),
    /// Strictly blocked navigation (unauthorized origin, credentials, dangerous scheme).
    Block(String),
}

/// Strictly evaluate whether a navigation target URL is permitted inside the WebView,
/// should be delegated to the system browser, or must be blocked.
pub fn evaluate_navigation_target(url: &Url, expected_port: u16) -> NavigationDecision {
    let scheme = url.scheme().to_ascii_lowercase();

    match scheme.as_str() {
        "http" => {
            // Check credentials: must never contain embedded username or password
            if !url.username().is_empty() {
                return NavigationDecision::Block(
                    "Loopback URLs with embedded usernames are rejected".into(),
                );
            }
            if url.password().is_some() {
                return NavigationDecision::Block(
                    "Loopback URLs with embedded passwords are rejected".into(),
                );
            }

            // Check hostname: must be strictly 127.0.0.1
            match url.host_str() {
                Some("127.0.0.1") => {
                    // Check port: must match active sidecar port
                    if url.port() != Some(expected_port) {
                        return NavigationDecision::Block(format!(
                            "Port mismatch: expected {}, got {:?}",
                            expected_port,
                            url.port()
                        ));
                    }

                    // Check path: permit intended application endpoints
                    let path = url.path();
                    if path.starts_with("/app")
                        || path.starts_with("/agent/")
                        || path.starts_with("/api/")
                        || path == "/"
                        || path == "/favicon.ico"
                        || path == "/favicon.svg"
                    {
                        NavigationDecision::AllowInternal
                    } else {
                        NavigationDecision::Block(format!(
                            "Path '{}' is not an allowed local application route",
                            path
                        ))
                    }
                }
                Some(_) => {
                    // External HTTP URL: route to system browser
                    validate_external_url(url)
                }
                None => NavigationDecision::Block("Missing host in HTTP URL".into()),
            }
        }
        "https" => {
            // External HTTPS URL: route to system browser
            validate_external_url(url)
        }
        "about" => {
            if url.as_str() == "about:blank" {
                NavigationDecision::AllowInternal
            } else {
                NavigationDecision::Block(format!(
                    "Scheme '{}' is strictly prohibited in QueryPilot desktop",
                    scheme
                ))
            }
        }
        "javascript" | "data" | "file" | "blob" | "vbscript" | "ws" | "wss" => {
            NavigationDecision::Block(format!(
                "Scheme '{}' is strictly prohibited in QueryPilot desktop",
                scheme
            ))
        }
        _ => NavigationDecision::Block(format!(
            "Unsupported or unrecognized URL scheme '{}'",
            scheme
        )),
    }
}

/// Validate external HTTP/HTTPS URL for dispatch to system browser.
fn validate_external_url(url: &Url) -> NavigationDecision {
    let s = url.as_str();

    if url.host_str().is_none() {
        return NavigationDecision::Block("External URL missing hostname".into());
    }

    if s.chars()
        .any(|c| c.is_control() || c == '"' || c == '\'' || c.is_whitespace())
    {
        return NavigationDecision::Block(
            "External URL contains illegal control or whitespace characters".into(),
        );
    }

    NavigationDecision::OpenExternal(s.to_string())
}

/// Open an approved external URL in the system browser using Win32 ShellExecuteW.
/// Never invokes any command-line shell (cmd.exe / powershell.exe) or interpolated command.
pub fn open_in_system_browser_safe(url: &str) -> Result<(), String> {
    if !url.starts_with("https://") && !url.starts_with("http://") {
        return Err("Only http and https URLs may be opened in system browser".into());
    }

    if url
        .chars()
        .any(|c| c.is_control() || c == '"' || c == '\'' || c.is_whitespace())
    {
        return Err("URL contains illegal characters".into());
    }

    let wide_op: Vec<u16> = std::ffi::OsStr::new("open")
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let wide_url: Vec<u16> = std::ffi::OsStr::new(url)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();

    unsafe {
        let res = ShellExecuteW(
            ptr::null_mut(),
            wide_op.as_ptr(),
            wide_url.as_ptr(),
            ptr::null(),
            ptr::null(),
            SW_SHOWNORMAL as _,
        );
        if (res as usize) <= 32 {
            return Err(format!(
                "ShellExecuteW failed with error code: {}",
                res as usize
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_allowed_local_agent_routes() {
        let port = 54321;
        let allowed = [
            "http://127.0.0.1:54321/app/",
            "http://127.0.0.1:54321/app/index.html",
            "http://127.0.0.1:54321/app/assets/index.js",
            "http://127.0.0.1:54321/agent/v1/auth/bootstrap",
            "http://127.0.0.1:54321/agent/v1/profiles",
            "http://127.0.0.1:54321/api/v1/generate-sql",
            "http://127.0.0.1:54321/favicon.svg",
            "http://127.0.0.1:54321/",
        ];
        for u in allowed {
            let parsed = Url::parse(u).unwrap();
            assert_eq!(
                evaluate_navigation_target(&parsed, port),
                NavigationDecision::AllowInternal,
                "Expected AllowInternal for {}",
                u
            );
        }
    }

    #[test]
    fn test_rejected_loopback_routes() {
        let port = 54321;
        // Wrong port
        let wrong_port = Url::parse("http://127.0.0.1:8000/app/").unwrap();
        assert!(matches!(
            evaluate_navigation_target(&wrong_port, port),
            NavigationDecision::Block(_)
        ));

        // Embedded credentials
        let creds = Url::parse("http://admin:secret@127.0.0.1:54321/app/").unwrap();
        assert!(matches!(
            evaluate_navigation_target(&creds, port),
            NavigationDecision::Block(_)
        ));

        // Unapproved local route
        let bad_path = Url::parse("http://127.0.0.1:54321/admin/debug").unwrap();
        assert!(matches!(
            evaluate_navigation_target(&bad_path, port),
            NavigationDecision::Block(_)
        ));
    }

    #[test]
    fn test_external_http_https_routes() {
        let port = 54321;
        let gh = Url::parse("https://github.com/google/query-pilot").unwrap();
        assert_eq!(
            evaluate_navigation_target(&gh, port),
            NavigationDecision::OpenExternal("https://github.com/google/query-pilot".into())
        );

        let docs = Url::parse("https://learn.microsoft.com/en-us/sql/connect/odbc/").unwrap();
        assert_eq!(
            evaluate_navigation_target(&docs, port),
            NavigationDecision::OpenExternal(
                "https://learn.microsoft.com/en-us/sql/connect/odbc/".into()
            )
        );
    }

    #[test]
    fn test_blocked_dangerous_schemes() {
        let port = 54321;
        let dangerous = [
            "javascript:alert(1)",
            "data:text/html,<b>pwn</b>",
            "file:///C:/Windows/System32/cmd.exe",
            "blob:http://127.0.0.1:54321/1234",
            "about:flags",
            "custom-proto://execute",
            "powershell:calc",
        ];
        for d in dangerous {
            let parsed = Url::parse(d).unwrap();
            assert!(
                matches!(
                    evaluate_navigation_target(&parsed, port),
                    NavigationDecision::Block(_)
                ),
                "Expected Block for {}",
                d
            );
        }

        // about:blank is permitted as the initial unnavigated blank canvas for cookie initialization
        let blank = Url::parse("about:blank").unwrap();
        assert_eq!(
            evaluate_navigation_target(&blank, port),
            NavigationDecision::AllowInternal
        );
    }
}

