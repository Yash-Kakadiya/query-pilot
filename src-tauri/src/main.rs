// Prevents console window on Windows across both dev and release builds.
#![windows_subsystem = "windows"]

fn main() {
    querypilot_lib::run();
}
