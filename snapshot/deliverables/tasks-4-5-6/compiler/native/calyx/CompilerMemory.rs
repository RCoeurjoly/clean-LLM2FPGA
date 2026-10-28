//! Allocator statistics and explicit reclamation of already freed pages.
//! GNU/Linux builds pin glibc with the size_t-based mallinfo2 ABI (>= 2.33).
//! Other platforms return None. Reclamation never changes live allocations or IR.

#[derive(Debug)]
pub struct HeapSnapshot {
    pub arena_bytes: usize,
    pub live_heap_bytes: usize,
    pub free_heap_bytes: usize,
    pub mapped_bytes: usize,
    pub releasable_top_bytes: usize,
}

#[cfg(all(target_os = "linux", target_env = "gnu"))]
mod gnu {
    use super::HeapSnapshot;

    // Field order and types match the pinned glibc malloc.h, struct mallinfo2.
    #[repr(C)]
    struct MallInfo2 {
        arena: usize,
        ordblks: usize,
        smblks: usize,
        hblks: usize,
        hblkhd: usize,
        usmblks: usize,
        fsmblks: usize,
        uordblks: usize,
        fordblks: usize,
        keepcost: usize,
    }

    unsafe extern "C" {
        fn mallinfo2() -> MallInfo2;
        fn malloc_trim(pad: usize) -> std::ffi::c_int;
    }

    pub fn reclaim_free_pages() -> bool {
        // SAFETY: pinned glibc accepts a size_t padding value. malloc_trim only
        // returns allocator-owned freed pages; it takes no Rust allocation pointer.
        unsafe { malloc_trim(0) != 0 }
    }

    pub fn snapshot() -> HeapSnapshot {
        // SAFETY: GNU/Linux's pinned glibc exports mallinfo2 with this C layout.
        // It returns an initialized value and does not retain any Rust pointer.
        let info = unsafe { mallinfo2() };
        HeapSnapshot {
            arena_bytes: info.arena,
            live_heap_bytes: info.uordblks,
            free_heap_bytes: info.fordblks,
            mapped_bytes: info.hblkhd,
            releasable_top_bytes: info.keepcost,
        }
    }
}

pub fn snapshot() -> Option<HeapSnapshot> {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    { Some(gnu::snapshot()) }
    #[cfg(not(all(target_os = "linux", target_env = "gnu")))]
    { None }
}

/// Best-effort resource cleanup, called explicitly after native component cleanup.
/// Some(false) means no pages were returned; None means this target lacks support.
pub fn reclaim_free_pages() -> Option<bool> {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    { Some(gnu::reclaim_free_pages()) }
    #[cfg(not(all(target_os = "linux", target_env = "gnu")))]
    { None }
}

#[cfg(all(test, target_os = "linux", target_env = "gnu"))]
mod tests {
    use super::*;
    fn rss() -> usize {
        let status = std::fs::read_to_string("/proc/self/status").unwrap();
        status.lines().find(|line| line.starts_with("VmRSS:")).unwrap()
            .split_whitespace().nth(1).unwrap().parse::<usize>().unwrap() * 1024
    }
    #[test]
    fn reclamation_releases_freed_pages_and_preserves_live_allocations() {
        let mut blocks: Vec<_> = (0..24000).map(|i| vec![(i % 251) as u8; 4096]).collect();
        for (i, block) in blocks.iter_mut().enumerate() {
            if i % 32 != 0 { *block = Vec::new(); }
        }
        let before = rss();
        assert!(reclaim_free_pages().expect("GNU/Linux allocator support"));
        let after = rss();
        for (i, block) in blocks.iter().enumerate() {
            if i % 32 == 0 {
                assert_eq!(block.len(), 4096);
                assert!(block.iter().all(|byte| *byte == (i % 251) as u8));
            }
        }
        assert!(after < before / 2, "freed pages not reclaimed: {before} -> {after}");
    }
}
