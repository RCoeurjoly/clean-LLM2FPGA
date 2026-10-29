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
