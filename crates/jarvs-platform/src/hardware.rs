use std::process::Command;

use serde::Serialize;
use sysinfo::{CpuRefreshKind, Disks, MemoryRefreshKind, RefreshKind, System};

#[derive(Debug, Clone, Serialize)]
pub struct HardwareReport {
    pub os: String,
    pub os_version: Option<String>,
    pub kernel_version: Option<String>,
    pub arch: String,
    pub cpu_brand: Option<String>,
    pub cpu_logical_cores: usize,
    pub cpu_physical_cores: Option<usize>,
    pub memory_total_bytes: u64,
    /// `None` when the OS does not report it (sysinfo returns 0 then).
    pub memory_available_bytes: Option<u64>,
    /// Apple Silicon shares one memory pool between CPU and GPU.
    pub unified_memory: bool,
    pub gpus: Vec<GpuInfo>,
    pub disk_total_bytes: Option<u64>,
    pub disk_available_bytes: Option<u64>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct GpuInfo {
    pub name: String,
    pub memory_bytes: Option<u64>,
    pub source: &'static str,
}

pub fn detect() -> HardwareReport {
    let system = System::new_with_specifics(
        RefreshKind::nothing()
            .with_memory(MemoryRefreshKind::nothing().with_ram())
            .with_cpu(CpuRefreshKind::nothing()),
    );
    let arch = std::env::consts::ARCH.to_string();
    let os = std::env::consts::OS.to_string();
    let unified_memory = os == "macos" && arch == "aarch64";
    let (disk_total_bytes, disk_available_bytes) = largest_disk();
    // Some macOS releases report 0 available; total - used is then a close
    // approximation (free + inactive + speculative pages).
    let available = match system.available_memory() {
        0 => system.total_memory().saturating_sub(system.used_memory()),
        n => n,
    };

    let mut gpus = nvidia_gpus();
    if unified_memory {
        gpus.push(GpuInfo {
            name: "Apple GPU (unified memory)".into(),
            memory_bytes: None,
            source: "platform",
        });
    }

    HardwareReport {
        os_version: System::long_os_version().or_else(System::os_version),
        kernel_version: System::kernel_version(),
        cpu_brand: system
            .cpus()
            .first()
            .map(|cpu| cpu.brand().trim().to_string())
            .filter(|brand| !brand.is_empty()),
        cpu_logical_cores: std::thread::available_parallelism().map_or(1, |n| n.get()),
        cpu_physical_cores: System::physical_core_count(),
        memory_total_bytes: system.total_memory(),
        memory_available_bytes: (available > 0).then_some(available),
        unified_memory,
        gpus,
        disk_total_bytes,
        disk_available_bytes,
        os,
        arch,
    }
}

fn largest_disk() -> (Option<u64>, Option<u64>) {
    Disks::new_with_refreshed_list()
        .list()
        .iter()
        .max_by_key(|disk| disk.total_space())
        .map_or((None, None), |disk| {
            (Some(disk.total_space()), Some(disk.available_space()))
        })
}

/// Queries `nvidia-smi` directly (no shell). Absent tool means no NVIDIA GPU
/// we can see; other vendors are reported as unknown rather than guessed.
fn nvidia_gpus() -> Vec<GpuInfo> {
    let output = Command::new("nvidia-smi")
        .args([
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .output();
    match output {
        Ok(out) if out.status.success() => parse_nvidia_smi(&String::from_utf8_lossy(&out.stdout)),
        _ => Vec::new(),
    }
}

fn parse_nvidia_smi(text: &str) -> Vec<GpuInfo> {
    text.lines()
        .filter_map(|line| {
            let (name, mib) = line.rsplit_once(',')?;
            Some(GpuInfo {
                name: name.trim().to_string(),
                memory_bytes: mib.trim().parse::<u64>().ok().map(|m| m * 1024 * 1024),
                source: "nvidia-smi",
            })
        })
        .collect()
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelSuggestion {
    pub min_billion_params: f32,
    pub max_billion_params: f32,
    pub basis: String,
    pub caveat: &'static str,
}

const CAVEAT: &str = "Approximate starting point for ~4-bit quantized models. Real memory use \
also depends on context length (KV cache), runtime overhead, and other running apps. \
Download size is not the full memory requirement. You can still choose any model.";

/// Suggested parameter range for a model that should run comfortably.
pub fn suggest_model_size(report: &HardwareReport) -> ModelSuggestion {
    let gib = |bytes: u64| bytes as f64 / (1u64 << 30) as f64;
    let ram = gib(report.memory_total_bytes);
    let vram = report
        .gpus
        .iter()
        .filter_map(|gpu| gpu.memory_bytes)
        .max()
        .map(gib);

    // The RAM table assumes ~40% of RAM is free for weights; almost all VRAM is,
    // so VRAM counts 2.5x when mapping onto the same table. A large discrete GPU
    // then decides the tier.
    let (memory, basis) = match vram {
        Some(v) if v * 2.5 > ram => (v * 2.5, format!("{v:.0} GiB GPU memory")),
        _ if report.unified_memory => (ram, format!("{ram:.0} GiB unified memory")),
        _ => (ram, format!("{ram:.0} GiB system RAM")),
    };
    // Machines report slightly under their nominal size (15.8 GiB for 16 GB).
    let tiers: [(f64, f32, f32); 6] = [
        (120.0, 32.0, 70.0),
        (60.0, 14.0, 32.0),
        (30.0, 7.0, 14.0),
        (15.0, 3.0, 7.0),
        (7.0, 1.0, 3.0),
        (0.0, 0.5, 1.5),
    ];
    let (_, min, max) = tiers
        .into_iter()
        .find(|(floor, _, _)| memory >= *floor)
        .unwrap_or((0.0, 0.5, 1.5));
    ModelSuggestion {
        min_billion_params: min,
        max_billion_params: max,
        basis,
        caveat: CAVEAT,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(ram_gib: u64, unified: bool, gpus: Vec<GpuInfo>) -> HardwareReport {
        HardwareReport {
            os: "test".into(),
            os_version: None,
            kernel_version: None,
            arch: "x86_64".into(),
            cpu_brand: None,
            cpu_logical_cores: 4,
            cpu_physical_cores: None,
            memory_total_bytes: ram_gib << 30,
            memory_available_bytes: None,
            unified_memory: unified,
            gpus,
            disk_total_bytes: None,
            disk_available_bytes: None,
        }
    }

    #[test]
    fn detect_reports_this_machine() {
        let hw = detect();
        assert!(hw.memory_total_bytes > 0);
        assert!(hw.cpu_logical_cores >= 1);
        assert_eq!(hw.os, std::env::consts::OS);
        if let Some(available) = hw.memory_available_bytes {
            assert!(available <= hw.memory_total_bytes);
        }
    }

    #[test]
    fn ram_tiers_match_published_table() {
        let range = |gib| {
            let s = suggest_model_size(&report(gib, false, vec![]));
            (s.min_billion_params, s.max_billion_params)
        };
        assert_eq!(range(4), (0.5, 1.5));
        assert_eq!(range(8), (1.0, 3.0));
        assert_eq!(range(16), (3.0, 7.0));
        assert_eq!(range(32), (7.0, 14.0));
        assert_eq!(range(64), (14.0, 32.0));
        assert_eq!(range(128), (32.0, 70.0));
    }

    #[test]
    fn large_gpu_raises_tier_and_unified_memory_is_named() {
        let gpu = GpuInfo {
            name: "x".into(),
            memory_bytes: Some(24 << 30),
            source: "test",
        };
        let s = suggest_model_size(&report(16, false, vec![gpu]));
        assert_eq!((s.min_billion_params, s.max_billion_params), (14.0, 32.0));
        assert!(s.basis.contains("GPU"));
        assert!(
            suggest_model_size(&report(32, true, vec![]))
                .basis
                .contains("unified")
        );
    }

    #[test]
    fn parses_nvidia_smi_csv() {
        let gpus = parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564\nbad line\n");
        assert_eq!(gpus.len(), 1);
        assert_eq!(gpus[0].name, "NVIDIA GeForce RTX 4090");
        assert_eq!(gpus[0].memory_bytes, Some(24564 * 1024 * 1024));
    }
}
