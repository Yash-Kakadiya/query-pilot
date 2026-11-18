fn main() {
  let agent_dir = std::path::Path::new("../dist/querypilot-agent");
  if !agent_dir.exists() {
    let _ = std::fs::create_dir_all(agent_dir);
  }
  if std::fs::read_dir(agent_dir).map(|mut it| it.next().is_none()).unwrap_or(true) {
    let _ = std::fs::write(agent_dir.join(".placeholder"), "placeholder for compile check");
  }
  tauri_build::build()
}
