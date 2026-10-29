//! Lossless live control IDs represented by sorted, disjoint inclusive ranges.
//! Only membership insertion and intersection are needed by cell sharing.
#[derive(Default)]
pub(crate) struct LiveControlSet {
    ranges: Vec<(u64, u64)>,
}

impl LiveControlSet {
    pub(crate) fn insert(&mut self, value: u64) -> bool {
        let index = self.ranges.partition_point(|(_, end)| *end < value);
        if index < self.ranges.len() && self.ranges[index].0 <= value {
            return false;
        }
        let left = index > 0
            && self.ranges[index - 1].1.checked_add(1) == Some(value);
        let right = index < self.ranges.len()
            && value.checked_add(1) == Some(self.ranges[index].0);
        match (left, right) {
            (true, true) => {
                self.ranges[index - 1].1 = self.ranges[index].1;
                self.ranges.remove(index);
            }
            (true, false) => self.ranges[index - 1].1 = value,
            (false, true) => self.ranges[index].0 = value,
            (false, false) => self.ranges.insert(index, (value, value)),
        }
        true
    }

    pub(crate) fn is_disjoint(&self, other: &Self) -> bool {
        let (mut left, mut right) = (0, 0);
        while left < self.ranges.len() && right < other.ranges.len() {
            let a = self.ranges[left];
            let b = other.ranges[right];
            if a.1 < b.0 {
                left += 1;
            } else if b.1 < a.0 {
                right += 1;
            } else {
                return false;
            }
        }
        true
    }
}

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
