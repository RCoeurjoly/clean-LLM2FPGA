
#[cfg(test)]
mod tests {
    use super::LiveControlSet;
    use std::collections::HashSet;

    #[test]
    fn all_small_sets_and_intersections_match_hash_sets() {
        let sets: Vec<_> = (0..256u64).map(|mask| {
            let mut compact = LiveControlSet::default();
            let mut reference = HashSet::new();
            // Non-monotonic insertion exercises joining and extending ranges.
            for id in [7, 0, 4, 2, 6, 1, 5, 3] {
                if mask & (1 << id) != 0 {
                    assert_eq!(compact.insert(id), reference.insert(id));
                    assert_eq!(compact.insert(id), reference.insert(id));
                }
            }
            (compact, reference)
        }).collect();
        for (a, reference_a) in &sets {
            for (b, reference_b) in &sets {
                assert_eq!(a.is_disjoint(b), reference_a.is_disjoint(reference_b));
            }
        }
    }

    #[test]
    fn boundary_ids_do_not_wrap_or_create_false_intersections() {
        let mut a = LiveControlSet::default();
        for id in [u64::MAX, 0, u64::MAX - 2, u64::MAX - 1, 1] {
            assert!(a.insert(id));
        }
        let mut gap = LiveControlSet::default();
        gap.insert(2);
        gap.insert(u64::MAX - 3);
        assert!(a.is_disjoint(&gap));
        gap.insert(u64::MAX);
        assert!(!a.is_disjoint(&gap));
    }
}
