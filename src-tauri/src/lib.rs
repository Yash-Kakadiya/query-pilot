pub mod auth;
pub mod navigation;
pub mod process_manager;

use std::sync::{Arc, Mutex};

use navigation::{
    evaluate_navigation_target, open_in_system_browser_safe, NavigationDecision,
};
use process_manager::{
    enforce_single_instance, find_sidecar_executable, focus_existing_window,
    launch_supervised_sidecar, show_native_error, AutoHandle, SupervisedSidecar,
};

/// App state storing the active sidecar and single-instance mutex.
pub struct AppState {
    pub sidecar: Arc<Mutex<Option<SupervisedSidecar>>>,
    pub _mutex: AutoHandle,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let show_popups = std::env::var("QUERYPILOT_NO_POPUP").is_err();

    // 1. Single-Instance Enforcement
    let instance_mutex = match enforce_single_instance("QueryPilot_SingleInstance_Mutex") {
        Ok(m) => m,
        Err(err) => {
            eprintln!("[QueryPilot] {}", err);
            focus_existing_window("QueryPilot");
            std::process::exit(0);
        }
    };

    // 2. Locate packaged Python sidecar executable
    let sidecar_path = match find_sidecar_executable() {
        Ok(p) => p,
        Err(err) => {
            eprintln!("[QueryPilot] {}", err);
            if show_popups {
                show_native_error("QueryPilot Startup Error", &err);
            }
            std::process::exit(1);
        }
    };

    log::info!("Found QueryPilot sidecar executable at: {}", sidecar_path.display());

    // 3. Resolve canonical token directory (~/.query_pilot) so both desktop and web frontend
    // share the same consistent token location without temp dir mismatch.
    let token_path = auth::resolve_token_path();
    let token_dir = token_path.parent().map(|p| p.to_path_buf());

    // Launch supervised sidecar under Win32 Job Object with canonical token directory
    let sidecar = match launch_supervised_sidecar(&sidecar_path, token_dir.as_deref(), None) {
        Ok(s) => s,
        Err(err) => {
            eprintln!("[QueryPilot] {}", err);
            if show_popups {
                show_native_error("QueryPilot Sidecar Failure", &err);
            }
            std::process::exit(1);
        }
    };

    let bound_port = sidecar.port;
    let bound_pid = sidecar.pid;
    println!(
        "[QueryPilot] Sidecar running on port {} (PID={})",
        bound_port, bound_pid
    );

    let sidecar_state = Arc::new(Mutex::new(Some(sidecar)));
    let sidecar_state_for_monitor = Arc::clone(&sidecar_state);
    let sidecar_state_for_cleanup = Arc::clone(&sidecar_state);

    // Spawn monitor for unexpected termination
    if let Some(ref s) = *sidecar_state_for_monitor.lock().unwrap() {
        s.monitor_termination(move |exit_code| {
            log::warn!("QueryPilot sidecar exited unexpectedly with code {}", exit_code);
        });
    }

    // 4. Authenticate directly with local agent using bootstrap token (B26-B4)
    let mut bootstrap_token = None;
    for _ in 0..40 {
        if let Ok(t) = auth::read_bootstrap_token(&token_path) {
            bootstrap_token = Some(t);
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }

    let token = match bootstrap_token {
        Some(t) => t,
        None => {
            let err_msg = format!(
                "Could not read bootstrap token from '{}'",
                token_path.display()
            );
            eprintln!("[QueryPilot] {}", err_msg);
            if show_popups {
                show_native_error("QueryPilot Authentication Failure", &err_msg);
            }
            if let Ok(mut lock) = sidecar_state_for_cleanup.lock() {
                if let Some(s) = lock.take() {
                    s.terminate();
                }
            }
            std::process::exit(1);
        }
    };

    let mut session_cookie = match auth::authenticate_with_agent(
        bound_port,
        &token,
        std::time::Duration::from_secs(5),
    ) {
        Ok(c) => {
            auth::wipe_string(token);
            c
        }
        Err(err) => {
            auth::wipe_string(token);
            let err_msg = format!("Agent bootstrap authentication failed: {}", err);
            eprintln!("[QueryPilot] {}", err_msg);
            if show_popups {
                show_native_error("QueryPilot Authentication Failure", &err_msg);
            }
            if let Ok(mut lock) = sidecar_state_for_cleanup.lock() {
                if let Some(s) = lock.take() {
                    s.terminate();
                }
            }
            std::process::exit(1);
        }
    };

    log::info!("Bootstrap authentication succeeded; ambient session cookie acquired");

    let sidecar_state_for_setup = Arc::clone(&sidecar_state);

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            sidecar: sidecar_state,
            _mutex: instance_mutex,
        })
        .setup(move |app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // Step 1: Create main desktop window initialized to blank without navigating to application
            let initial_blank_url: tauri::Url =
                "about:blank".parse().expect("Failed to parse about:blank");
            let webview_url = tauri::WebviewUrl::External(initial_blank_url);

            let window_builder = tauri::WebviewWindowBuilder::new(app, "main", webview_url)
                .title("QueryPilot")
                .inner_size(1280.0, 800.0)
                .min_inner_size(960.0, 640.0)
                .center()
                .resizable(true)
                .fullscreen(false);

            let window_builder = if let Some(icon) = app.default_window_icon() {
                window_builder.icon(icon.clone()).expect("Failed to set window icon")
            } else {
                window_builder
            };

            let window_builder = window_builder
                .on_navigation(move |url| {
                    match evaluate_navigation_target(url, bound_port) {
                        NavigationDecision::AllowInternal => true,
                        NavigationDecision::OpenExternal(target_url) => {
                            log::info!("Opening external URL in system browser: {}", target_url);
                            let _ = open_in_system_browser_safe(&target_url);
                            false
                        }
                        NavigationDecision::Block(reason) => {
                            log::warn!("Blocked navigation to '{}': {}", url.as_str(), reason);
                            false
                        }
                    }
                });

            let window = window_builder.build()?;

            // Steps 2-5: Wait for WebView2 controller, create and inject session cookie
            #[cfg(windows)]
            {
                let (tx, rx) = std::sync::mpsc::channel();
                let mut cookie_to_inject = session_cookie.clone();
                let webview_res = window.with_webview(move |webview| {
                    let controller = webview.controller();
                    let res = auth::inject_session_cookie(&controller, &cookie_to_inject);
                    auth::wipe_string(std::mem::take(&mut cookie_to_inject.value));
                    let _ = tx.send(res);
                });

                if let Err(err) = webview_res {
                    let err_msg = format!("WebView2 controller error: {}", err);
                    eprintln!("[QueryPilot] {}", err_msg);
                    if show_popups {
                        show_native_error("QueryPilot Startup Error", &err_msg);
                    }
                    if let Ok(mut lock) = sidecar_state_for_setup.lock() {
                        if let Some(s) = lock.take() {
                            s.terminate();
                        }
                    }
                    std::process::exit(1);
                }

                match rx.recv() {
                    Ok(Ok(())) => {
                        log::info!("Session cookie successfully installed into WebView2");
                    }
                    Ok(Err(err)) => {
                        let err_msg = format!("WebView2 session cookie injection failed: {}", err);
                        eprintln!("[QueryPilot] {}", err_msg);
                        if show_popups {
                            show_native_error("QueryPilot Authentication Failure", &err_msg);
                        }
                        if let Ok(mut lock) = sidecar_state_for_setup.lock() {
                            if let Some(s) = lock.take() {
                                s.terminate();
                            }
                        }
                        std::process::exit(1);
                    }
                    Err(recv_err) => {
                        let err_msg = format!("Failed to receive cookie injection status: {}", recv_err);
                        eprintln!("[QueryPilot] {}", err_msg);
                        if let Ok(mut lock) = sidecar_state_for_setup.lock() {
                            if let Some(s) = lock.take() {
                                s.terminate();
                            }
                        }
                        std::process::exit(1);
                    }
                }
            }

            // Step 6: Navigate to active sidecar's /app/ URL
            let app_url: tauri::Url = format!("http://127.0.0.1:{}/app/", bound_port)
                .parse()
                .expect("Failed to parse local start URL");
            window
                .navigate(app_url)
                .expect("Failed to navigate to local agent application");

            // Clear sensitive session cookie value from memory
            auth::wipe_string(std::mem::take(&mut session_cookie.value));

            Ok(())
        })
        .on_window_event(move |_window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // Terminate sidecar cleanly on window destroy
                if let Ok(mut lock) = sidecar_state_for_cleanup.lock() {
                    if let Some(s) = lock.take() {
                        log::info!("Window closed. Terminating sidecar...");
                        s.terminate();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while building tauri application");
}
