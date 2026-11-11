//! Operating-system facts and per-OS conventions. Every OS-specific detail of
//! jarvS lives here so the rest of the runtime stays platform-neutral.

pub mod hardware;
pub mod paths;

pub use hardware::{HardwareReport, ModelSuggestion, detect, suggest_model_size};
pub use paths::AppPaths;
