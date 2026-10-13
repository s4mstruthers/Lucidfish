// Copyright (C) 2026 Sam Struthers
// SPDX-License-Identifier: AGPL-3.0-or-later
// This file is part of Lucidfish <https://github.com/s4mstruthers/Lucidfish>; see LICENSE for the terms.

//! The Lucidfish desktop shell.
//!
//! A thin native window around the Lucidfish backend. On start it launches the
//! packaged backend (`lucidfish-server`, a sidecar built with PyInstaller) as a
//! child process, waits for the line `LUCIDFISH_URL http://127.0.0.1:<port>` on
//! its standard output, and points the window at that address: from then on the
//! page served by the backend *is* the app. The shell also owns what a web page
//! cannot do by itself: checking for new releases, installing them and
//! restarting, and stopping the backend when the window closes.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// What the shell knows about the backend process.
#[derive(Default)]
pub struct Backend {
    child: Mutex<Option<CommandChild>>,
    url: Mutex<Option<String>>,
    log: Mutex<Option<fs::File>>,
}

impl Backend {
    fn log(&self, line: &str) {
        log::info!("{line}");
        if let Ok(mut guard) = self.log.lock() {
            if let Some(file) = guard.as_mut() {
                let _ = writeln!(file, "{line}");
            }
        }
    }

    fn kill(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.take() {
                let _ = child.kill();
            }
        }
    }
}

/// Lucidfish's data folder, the same one the backend uses (see `lucidfish/config.py`):
/// `~/Library/Application Support/Lucidfish`, `%APPDATA%\Lucidfish` or `~/.local/share/lucidfish`.
fn data_dir(app: &AppHandle) -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("LUCIDFISH_DATA_DIR") {
        if !dir.is_empty() {
            return Some(PathBuf::from(dir));
        }
    }
    let base = app.path().data_dir().ok()?;
    Some(base.join(if cfg!(target_os = "linux") { "lucidfish" } else { "Lucidfish" }))
}

/// The Stockfish shipped inside the app bundle, copied into the data folder so it is on a
/// writable disk with its executable bit set (an AppImage, for one, is mounted read-only).
fn bundled_engine(app: &AppHandle, data: &Path) -> Option<PathBuf> {
    let source_dir = app.path().resource_dir().ok()?.join("engine");
    let source = fs::read_dir(&source_dir).ok()?.flatten().map(|e| e.path()).find(|p| {
        p.is_file()
            && p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("stockfish"))
                .unwrap_or(false)
    })?;
    let target_dir = data.join("engine-bundled");
    fs::create_dir_all(&target_dir).ok()?;
    let target = target_dir.join(source.file_name()?);
    let same_size = fs::metadata(&target)
        .and_then(|t| fs::metadata(&source).map(|s| s.len() == t.len()))
        .unwrap_or(false);
    if !same_size {
        fs::copy(&source, &target).ok()?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&target, fs::Permissions::from_mode(0o755));
    }
    Some(target)
}

fn spawn_backend(app: &AppHandle) -> Result<(), String> {
    let backend = app.state::<Backend>();
    backend.kill();
    let data = data_dir(app);
    if let Some(dir) = &data {
        let _ = fs::create_dir_all(dir);
        if let Ok(file) = fs::File::create(dir.join("desktop.log")) {
            *backend.log.lock().map_err(|e| e.to_string())? = Some(file);
        }
    }
    let mut command = app
        .shell()
        .sidecar("lucidfish-server")
        .map_err(|e| format!("The backend is missing from this installation: {e}"))?
        .args(["web", "--desktop"])
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONIOENCODING", "utf-8");
    if let Some(dir) = &data {
        command = command.env("LUCIDFISH_DATA_DIR", dir.to_string_lossy().to_string());
        if let Some(engine) = bundled_engine(app, dir) {
            backend.log(&format!("bundled engine: {}", engine.display()));
            command = command.env("LUCIDFISH_BUNDLED_STOCKFISH", engine.to_string_lossy().to_string());
        }
    }
    let (mut rx, child) = command.spawn().map_err(|e| format!("Could not start the backend: {e}"))?;
    *backend.child.lock().map_err(|e| e.to_string())? = Some(child);
    *backend.url.lock().map_err(|e| e.to_string())? = None;
    backend.log("backend starting");

    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        let backend = handle.state::<Backend>();
        let mut tail: Vec<String> = Vec::new();
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(bytes) | CommandEvent::Stderr(bytes) => {
                    let line = String::from_utf8_lossy(&bytes).trim_end().to_string();
                    if line.is_empty() {
                        continue;
                    }
                    backend.log(&line);
                    tail.push(line.clone());
                    if tail.len() > 40 {
                        tail.remove(0);
                    }
                    if let Some(url) = line.strip_prefix("LUCIDFISH_URL ") {
                        let url = url.trim().to_string();
                        if let Ok(mut guard) = backend.url.lock() {
                            *guard = Some(url.clone());
                        }
                        let _ = handle.emit("backend-ready", url.clone());
                        if let Some(window) = handle.get_webview_window("main") {
                            if let Ok(parsed) = url.parse() {
                                let _ = window.navigate(parsed);
                            }
                        }
                    }
                }
                CommandEvent::Terminated(status) => {
                    let code = status.code.unwrap_or(-1);
                    backend.log(&format!("backend exited with code {code}"));
                    let started = backend.url.lock().map(|u| u.is_some()).unwrap_or(false);
                    if !started {
                        let _ = handle.emit("backend-failed", tail.join("\n"));
                    }
                    break;
                }
                CommandEvent::Error(err) => backend.log(&format!("backend error: {err}")),
                _ => {}
            }
        }
    });
    Ok(())
}

// ---------------------------------------------------------------- commands the page can call

#[tauri::command]
fn backend_url(backend: State<Backend>) -> Option<String> {
    backend.url.lock().ok().and_then(|u| u.clone())
}

#[tauri::command]
fn restart_backend(app: AppHandle) -> Result<(), String> {
    spawn_backend(&app)
}

#[derive(Serialize)]
pub struct UpdateInfo {
    version: String,
    current: String,
    notes: Option<String>,
    date: Option<String>,
}

#[cfg(not(any(target_os = "android", target_os = "ios")))]
#[tauri::command]
async fn check_update(app: AppHandle) -> Result<Option<UpdateInfo>, String> {
    use tauri_plugin_updater::UpdaterExt;
    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await.map_err(|e| e.to_string())? {
        Some(update) => Ok(Some(UpdateInfo {
            version: update.version.clone(),
            current: update.current_version.clone(),
            notes: update.body.clone(),
            date: update.date.map(|d| d.to_string()),
        })),
        None => Ok(None),
    }
}

/// Download and install the newest release, then restart the app. Progress goes to the page as
/// `update-progress` events; the backend is stopped just before the restart.
#[cfg(not(any(target_os = "android", target_os = "ios")))]
#[tauri::command]
async fn install_update(app: AppHandle) -> Result<(), String> {
    use tauri_plugin_updater::UpdaterExt;
    let updater = app.updater().map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "You already have the latest version.".to_string())?;
    let progress_app = app.clone();
    let mut downloaded: u64 = 0;
    update
        .download_and_install(
            move |chunk, total| {
                downloaded += chunk as u64;
                let _ = progress_app.emit(
                    "update-progress",
                    serde_json::json!({ "downloaded": downloaded, "total": total }),
                );
            },
            || {},
        )
        .await
        .map_err(|e| e.to_string())?;
    app.state::<Backend>().kill();
    app.restart();
}

#[tauri::command]
fn app_version(app: AppHandle) -> String {
    app.package_info().version.to_string()
}

pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Backend::default())
        .setup(|app| {
            if let Err(err) = spawn_backend(app.handle()) {
                log::error!("{err}");
                let _ = app.handle().emit("backend-failed", err);
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                window.app_handle().state::<Backend>().kill();
            }
        });

    #[cfg(not(any(target_os = "android", target_os = "ios")))]
    let builder = builder
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            backend_url,
            restart_backend,
            app_version,
            check_update,
            install_update
        ]);
    #[cfg(any(target_os = "android", target_os = "ios"))]
    let builder = builder.invoke_handler(tauri::generate_handler![backend_url, restart_backend, app_version]);

    let app = builder
        .build(tauri::generate_context!())
        .expect("error while building the Lucidfish desktop app");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            handle.state::<Backend>().kill();
        }
    });
}
