// Bridge Desktop Shell — macOS (Step 8)
//
// Tauri v2 thin shell that:
// 1. Spawns Python backend with stderr capture + diagnostics
// 2. Manages process lifecycle with deterministic cleanup
// 3. Provides system tray (minimize-to-tray on close)
// 4. Shows actionable error page on startup failure

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{Manager, WindowEvent};

struct Backend {
    child: Mutex<Option<Child>>,
    port: u16,
    tracked_pgids: Mutex<Vec<i32>>,
    startup_log: Arc<Mutex<String>>,
    stderr_log_path: PathBuf,
}

// ── Port discovery ──

fn find_available_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

// ── Server readiness ──

fn wait_for_server(port: u16, timeout: Duration) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

// ── Environment replication ──

fn shell_escape(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn build_shell_command(bridge_py: &str, port: u16, log_dir: &str) -> String {
    let escaped_path = shell_escape(bridge_py);
    let escaped_log = shell_escape(log_dir);
    format!(
        r#"export PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.npm-global/bin:$PATH"
source "$HOME/.zshrc" || true
_missing=""
for cmd in python3 claude codex; do
    if ! command -v "$cmd" >/dev/null 2>&1; then _missing="$_missing $cmd"; fi
done
>&2 echo "BRIDGE_ENV: python3=$(which python3 2>/dev/null || echo MISSING) claude=$(which claude 2>/dev/null || echo MISSING) codex=$(which codex 2>/dev/null || echo MISSING)"
if [ -n "$_missing" ]; then >&2 echo "BRIDGE_MISSING:$_missing"; exit 127; fi
exec python3 {} --port {} --no-browser --log-dir {}"#,
        escaped_path, port, escaped_log
    )
}

// ── Path resolution ──

fn resolve_bridge_dir(app: &tauri::App) -> PathBuf {
    if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf()
    } else {
        app.path().resource_dir().unwrap()
    }
}

// ── CLI pgid discovery ──

fn find_cli_pids(python_pid: i32) -> Vec<i32> {
    std::process::Command::new("/usr/bin/pgrep")
        .args(["-P", &python_pid.to_string()])
        .output()
        .map(|out| {
            String::from_utf8_lossy(&out.stdout)
                .lines()
                .filter_map(|l| l.parse().ok())
                .collect()
        })
        .unwrap_or_default()
}

// ── Backend spawn ──

fn spawn_backend(bridge_py: &str, cwd: &str, port: u16, log_dir: &str) -> Child {
    let shell_cmd = build_shell_command(bridge_py, port, log_dir);
    unsafe {
        Command::new("/bin/zsh")
            .args(["-c", &shell_cmd])
            .current_dir(cwd)
            .stderr(Stdio::piped())
            .pre_exec(|| {
                libc::setsid();
                Ok(())
            })
            .spawn()
            .expect("Failed to start /bin/zsh")
    }
}

// ── Backend shutdown ──

fn shutdown_backend(backend: &Backend) {
    let mut guard = backend.child.lock().unwrap();
    let child = match guard.as_mut() {
        Some(c) => c,
        None => return,
    };
    if child.try_wait().ok().flatten().is_some() {
        return;
    }
    let pid = child.id() as i32;

    unsafe {
        libc::kill(pid, libc::SIGTERM);
    }

    for _ in 0..50 {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }

    let pgids = backend.tracked_pgids.lock().unwrap().clone();
    for pgid in &pgids {
        unsafe {
            libc::killpg(*pgid, libc::SIGKILL);
        }
    }
    unsafe {
        libc::kill(pid, libc::SIGKILL);
    }
    let _ = child.wait();
}

// ── System tray ──

fn setup_tray(app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::TrayIconBuilder;

    let show = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;

    TrayIconBuilder::new()
        .icon(app.default_window_icon().unwrap().clone())
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    w.show().ok();
                    w.set_focus().ok();
                }
            }
            "quit" => {
                let backend = app.state::<Backend>();
                shutdown_backend(backend.inner());
                app.exit(0);
            }
            _ => {}
        })
        .build(app)?;
    Ok(())
}

// ── JS escape for eval injection ──

fn js_escape(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('\'', "\\'")
        .replace('\n', "\\n")
}

// ── Main ──

fn main() {
    let port = find_available_port();

    tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .setup(move |app| {
            let bridge_dir = resolve_bridge_dir(app);
            let bridge_py = bridge_dir.join("bridge.py");
            let cwd = bridge_dir.to_string_lossy().to_string();

            let app_data = app.path().app_data_dir().unwrap();
            std::fs::create_dir_all(&app_data).ok();
            let stderr_log_path = app_data.join("startup.log");
            let log_dir = app_data.join("logs");

            let mut child = spawn_backend(
                &bridge_py.to_string_lossy(),
                &cwd,
                port,
                &log_dir.to_string_lossy(),
            );
            let python_pid = child.id() as i32;

            // Take stderr pipe BEFORE moving child into Backend
            let stderr_pipe = child.stderr.take().unwrap();

            let startup_log = Arc::new(Mutex::new(String::new()));

            app.manage(Backend {
                child: Mutex::new(Some(child)),
                port,
                tracked_pgids: Mutex::new(Vec::new()),
                startup_log: Arc::clone(&startup_log),
                stderr_log_path: stderr_log_path.clone(),
            });

            // stderr reader thread: tee to file + 64KB memory buffer
            let log_arc = Arc::clone(&startup_log);
            let log_file_path = stderr_log_path;
            std::thread::spawn(move || {
                use std::io::{BufRead, BufReader, Write};
                let mut file = std::fs::File::create(&log_file_path).ok();
                let reader = BufReader::new(stderr_pipe);
                for line in reader.lines().flatten() {
                    if let Some(ref mut f) = file {
                        writeln!(f, "{}", line).ok();
                    }
                    let mut buf = log_arc.lock().unwrap();
                    if buf.len() < 64 * 1024 {
                        buf.push_str(&line);
                        buf.push('\n');
                    }
                }
            });

            // Window must be obtained BEFORE health check
            let window = app.get_webview_window("main").unwrap();

            if !wait_for_server(port, Duration::from_secs(15)) {
                let backend = app.state::<Backend>();

                let exit_code = {
                    let mut guard = backend.child.lock().unwrap();
                    guard
                        .as_mut()
                        .and_then(|c| c.try_wait().ok().flatten())
                        .and_then(|s| s.code())
                };

                let error_type = match exit_code {
                    Some(127) => "cli_not_found",
                    Some(_) => "python_crashed",
                    None => "timeout",
                };
                let stderr_content = backend.startup_log.lock().unwrap().clone();
                let log_path = backend.stderr_log_path.to_string_lossy().to_string();

                window
                    .eval(&format!(
                        "showStartupError('{}', '{}', '{}')",
                        error_type,
                        js_escape(&stderr_content),
                        js_escape(&log_path),
                    ))
                    .ok();

                unsafe {
                    libc::killpg(python_pid, libc::SIGKILL);
                }
            } else {
                // Success: navigate webview to Python HTTP backend
                window.navigate(
                    format!("http://127.0.0.1:{}", port).parse().unwrap(),
                )?;

                // Health monitor (only after successful startup)
                let app_handle = app.handle().clone();
                std::thread::spawn(move || {
                    loop {
                        std::thread::sleep(Duration::from_secs(2));

                        let backend = app_handle.state::<Backend>();
                        let mut guard = backend.child.lock().unwrap();
                        if let Some(ref mut child) = *guard {
                            if let Ok(Some(_)) = child.try_wait() {
                                let pgids = backend.tracked_pgids.lock().unwrap().clone();
                                drop(guard);
                                for pgid in &pgids {
                                    unsafe {
                                        libc::killpg(*pgid, libc::SIGKILL);
                                    }
                                }
                                app_handle.exit(1);
                                return;
                            }
                        }
                        drop(guard);

                        let fresh = find_cli_pids(python_pid);
                        {
                            let mut tracked = backend.tracked_pgids.lock().unwrap();
                            for pid in &fresh {
                                if !tracked.contains(pid) {
                                    tracked.push(*pid);
                                }
                            }
                            tracked
                                .retain(|&pgid| unsafe { libc::killpg(pgid, 0) == 0 });
                        }
                    }
                });
            }

            setup_tray(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                window.hide().ok();
            }
        })
        .build(tauri::generate_context!())
        .expect("Failed to build Tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let backend = app.state::<Backend>();
                shutdown_backend(backend.inner());
            }
        });
}
