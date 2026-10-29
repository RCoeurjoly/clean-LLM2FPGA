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
