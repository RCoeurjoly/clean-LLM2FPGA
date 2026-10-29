//! Exact undirected conflict bits; traversal matches MatrixGraph, with usize node IDs.
pub type NodeIndex = petgraph::matrix_graph::NodeIndex<usize>;
use petgraph::visit::{GraphBase, IntoNeighbors, IntoNodeIdentifiers, NodeIndexable};

#[derive(Default)]
pub struct PackedConflictGraph {
    nodes: usize,
    words: Vec<u64>,
}

impl PackedConflictGraph {
    pub fn new_undirected() -> Self { Self::default() }
    pub fn add_node(&mut self, _: ()) -> NodeIndex {
        assert!(self.nodes < usize::MAX, "conflict graph node index overflow");
        let node = NodeIndex::new(self.nodes);
        self.nodes += 1;
        node
    }
    /// Reserve the known node domain without initializing absent edge words.
    /// WeightGraph constructs every node before inserting any conflict edge.
    pub fn reserve_node_domain(&mut self) {
        if self.nodes == 0 { return; }
        let bits = Self::position(self.nodes - 1, self.nodes - 1)
            .checked_add(1).expect("conflict matrix overflow");
        let words = bits.div_ceil(64);
        self.words.reserve_exact(words - self.words.len());
    }
    fn position(a: usize, b: usize) -> usize {
        let (row, column) = if a >= b { (a, b) } else { (b, a) };
        row.checked_mul(row + 1).expect("conflict matrix overflow") / 2 + column
    }
    pub fn update_edge(&mut self, a: NodeIndex, b: NodeIndex, value: bool) {
        assert!(a.index() < self.nodes && b.index() < self.nodes);
        let bit = Self::position(a.index(), b.index());
        if value {
            let word = bit / 64;
            if self.words.len() <= word { self.words.resize(word + 1, 0); }
            self.words[word] |= 1u64 << (bit % 64);
        } else if let Some(word) = self.words.get_mut(bit / 64) {
            *word &= !(1u64 << (bit % 64));
        }
    }
    pub fn has_edge(&self, a: NodeIndex, b: NodeIndex) -> bool {
        if a.index() >= self.nodes || b.index() >= self.nodes { return false; }
        let bit = Self::position(a.index(), b.index());
        self.words.get(bit / 64).is_some_and(|word| word & (1u64 << (bit % 64)) != 0)
    }
    pub fn node_count(&self) -> usize { self.nodes }
    pub fn neighbors(&self, node: NodeIndex) -> Neighbors<'_> {
        Neighbors { graph: self, node, next: 0 }
    }
    pub fn edge_references(&self) -> impl Iterator<Item=(NodeIndex, NodeIndex, &bool)> {
        (0..self.nodes).flat_map(move |row| (0..=row).filter_map(move |column| {
            let (a,b) = (NodeIndex::new(row), NodeIndex::new(column));
            self.has_edge(a,b).then_some((a,b,&true))
        }))
    }
}

pub struct Neighbors<'a> { graph: &'a PackedConflictGraph, node: NodeIndex, next: usize }
impl Iterator for Neighbors<'_> {
    type Item = NodeIndex;
    fn next(&mut self) -> Option<NodeIndex> {
        while self.next < self.graph.nodes {
            let node = NodeIndex::new(self.next);
            self.next += 1;
            if self.graph.has_edge(self.node, node) { return Some(node); }
        }
        None
    }
}
impl GraphBase for PackedConflictGraph { type NodeId = NodeIndex; type EdgeId = (NodeIndex,NodeIndex); }
impl NodeIndexable for PackedConflictGraph {
    fn node_bound(&self) -> usize { self.nodes }
    fn to_index(&self, node: NodeIndex) -> usize { node.index() }
    fn from_index(&self, index: usize) -> NodeIndex { NodeIndex::new(index) }
}
impl<'a> IntoNeighbors for &'a PackedConflictGraph {
    type Neighbors = Neighbors<'a>;
    fn neighbors(self, node: NodeIndex) -> Self::Neighbors { PackedConflictGraph::neighbors(self,node) }
}
impl IntoNodeIdentifiers for &PackedConflictGraph {
    type NodeIdentifiers = std::iter::Map<std::ops::Range<usize>,fn(usize)->NodeIndex>;
    fn node_identifiers(self) -> Self::NodeIdentifiers { (0..self.nodes).map(NodeIndex::new) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use petgraph::matrix_graph::{UnMatrix, NodeIndex as OriginalNodeIndex};
    use petgraph::visit::IntoEdgeReferences;
    use crate::BoolIdx;
    #[test]
    fn packed_conflict_matches_matrix_traversal_and_scc_exhaustively() {
        let pairs: Vec<_> = (0..4).flat_map(|row| (0..=row).map(move |col| (row,col))).collect();
        for mask in 0..(1 << pairs.len()) {
            let mut packed = PackedConflictGraph::default();
            let mut original: UnMatrix<(),BoolIdx> = UnMatrix::new_undirected();
            for _ in 0..4 { assert_eq!(packed.add_node(()).index(), original.add_node(()).index()); }
            for (bit,(a,b)) in pairs.iter().enumerate() {
                if mask & (1 << bit) != 0 {
                    let (a,b)=(NodeIndex::new(*a),NodeIndex::new(*b));
                    packed.update_edge(a,b,true); packed.update_edge(a,b,true);
                    original.update_edge(OriginalNodeIndex::new(a.index()),OriginalNodeIndex::new(b.index()),true.into());
                }
            }
            for a in 0..5 {
                let a=NodeIndex::new(a);
                assert_eq!(packed.neighbors(a).map(|n| n.index()).collect::<Vec<_>>(), original.neighbors(OriginalNodeIndex::new(a.index())).map(|n| n.index()).collect::<Vec<_>>());
            }
            assert_eq!(packed.edge_references().map(|(a,b,_)|(a.index(),b.index())).collect::<Vec<_>>(),
                original.edge_references().map(|(a,b,_)|(a.index(),b.index())).collect::<Vec<_>>());
            let ids = |groups: Vec<Vec<NodeIndex>>| groups.into_iter().map(|group| group.into_iter().map(|node| node.index()).collect::<Vec<_>>()).collect::<Vec<_>>();
            let original_ids = petgraph::algo::tarjan_scc(&original).into_iter().map(|group| group.into_iter().map(|node| node.index()).collect::<Vec<_>>()).collect::<Vec<_>>();
            assert_eq!(ids(petgraph::algo::tarjan_scc(&packed)), original_ids);
        }
    }
    #[test]
    fn packed_conflict_node_ids_cross_sixteen_bit_boundary() {
        let mut graph = PackedConflictGraph::default();
        for index in 0..70000 {
            assert_eq!(graph.add_node(()).index(), index);
        }
        let low = NodeIndex::new(65534);
        let high = NodeIndex::new(69999);
        assert_ne!(low, high);
        graph.update_edge(low, high, true);
        assert_eq!(graph.neighbors(high).collect::<Vec<_>>(), vec![low]);
        assert_eq!(graph.neighbors(low).collect::<Vec<_>>(), vec![high]);
        assert!(graph.has_edge(high, low));
        graph.update_edge(high, low, false);
        assert!(!graph.has_edge(low, high));
        assert_eq!(graph.node_count(), 70000);
    }
    #[test]
    fn packed_conflict_growth_keeps_edges_and_clears_exact_bit() {
        let mut graph=PackedConflictGraph::default();
        for _ in 0..130 { graph.add_node(()); }
        for (a,b) in [(0,0),(11,5),(129,128)] {
            graph.update_edge(NodeIndex::new(a),NodeIndex::new(b),true);
        }
        graph.update_edge(NodeIndex::new(11),NodeIndex::new(5),false);
        assert!(graph.has_edge(NodeIndex::new(0),NodeIndex::new(0)));
        assert!(!graph.has_edge(NodeIndex::new(5),NodeIndex::new(11)));
        assert!(graph.has_edge(NodeIndex::new(128),NodeIndex::new(129)));
    }
}
