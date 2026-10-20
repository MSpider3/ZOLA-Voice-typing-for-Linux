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

/// Daemon output goes to $XDG_STATE_HOME/zola/daemon.log (~/.local/state/zola),
/// so startup errors are visible when the app was launched from the desktop.
fn daemon_log() -> Option<fs::File> {
    let dir = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/state")))?
        .join("zola");
    fs::create_dir_all(&dir).ok()?;
    OpenOptions::new().create(true).append(true).open(dir.join("daemon.log")).ok()
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
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
