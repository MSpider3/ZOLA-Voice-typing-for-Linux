use std::fs::{self, OpenOptions};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

/// True if a `zola-daemon` process exists, even one still loading its models
/// before it opens port 5001 (the systemd service from install.sh at login).
fn daemon_process_running() -> bool {
    let Ok(entries) = fs::read_dir("/proc") else { return false };
    entries.flatten().any(|entry| {
        fs::read_to_string(entry.path().join("comm"))
            .map(|comm| comm.trim() == "zola-daemon")
            .unwrap_or(false)
    })
}

/// `$XDG_<kind>_HOME/zola`, falling back to `~/<fallback>/zola`.
fn zola_dir(kind: &str, fallback: &str) -> Option<PathBuf> {
    std::env::var_os(format!("XDG_{kind}_HOME"))
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(fallback)))
        .map(|dir| dir.join("zola"))
}

/// Daemon output goes to $XDG_STATE_HOME/zola/daemon.log (~/.local/state/zola),
/// so startup errors are visible when the app was launched from the desktop.
/// The log starts afresh once it passes 5 MB.
fn daemon_log() -> Option<fs::File> {
    let dir = zola_dir("STATE", ".local/state")?;
    fs::create_dir_all(&dir).ok()?;
    let path = dir.join("daemon.log");
    let too_big = fs::metadata(&path).map(|m| m.len() > 5 * 1024 * 1024).unwrap_or(false);
    OpenOptions::new()
        .create(true)
        .write(true)
        .append(!too_big)
        .truncate(too_big)
        .open(path)
        .ok()
}

/// The daemon's local API token (~/.config/zola/token), for the UI's requests.
#[tauri::command]
fn api_token() -> Result<String, String> {
    let path = zola_dir("CONFIG", ".config").ok_or("no home directory")?.join("token");
    fs::read_to_string(&path)
        .map(|token| token.trim().to_string())
        .map_err(|err| format!("{}: {err}", path.display()))
}

/// Start the backend daemon bundled next to this executable (Tauri sidecar,
/// installed as `zola-daemon` by the rpm/deb/AppImage), unless one is already
/// running — e.g. the systemd service set up by install.sh.
fn start_bundled_daemon() -> Option<Child> {
    let addr: SocketAddr = "127.0.0.1:5001".parse().unwrap();
    if TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok() || daemon_process_running() {
        return None;
    }
    let daemon = std::env::current_exe()
        .ok()?
        .parent()?
        .join("zola-daemon");
    if !daemon.is_file() {
        eprintln!("zola: no bundled zola-daemon found and none running on 127.0.0.1:5001");
        return None;
    }
    let mut command = Command::new(&daemon);
    if let Some(log) = daemon_log() {
        if let Ok(log_err) = log.try_clone() {
            command.stdout(Stdio::from(log)).stderr(Stdio::from(log_err));
        }
    }
    command
        .spawn()
        .map_err(|err| eprintln!("zola: could not start {}: {err}", daemon.display()))
        .ok()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    if let Some(mut daemon) = start_bundled_daemon() {
        // The daemon keeps running after the window closes, so the dictation
        // hotkey still works; this thread reaps it if it exits first.
        std::thread::spawn(move || {
            let _ = daemon.wait();
        });
    }

    #[cfg(target_os = "linux")]
    {
        // Disable DMA-BUF renderer to fix black/grey screen issues on Linux (especially NVIDIA/Wayland)
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }

    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![api_token])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
