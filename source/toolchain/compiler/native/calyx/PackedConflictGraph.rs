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
impl GraphBase for PackedConflictGraph {
    type NodeId = NodeIndex;
    type EdgeId = (NodeIndex,NodeIndex);
}
impl NodeIndexable for PackedConflictGraph {
    fn node_bound(&self) -> usize { self.nodes }
    fn to_index(&self, node: NodeIndex) -> usize { node.index() }
    fn from_index(&self, index: usize) -> NodeIndex { NodeIndex::new(index) }
}
impl<'a> IntoNeighbors for &'a PackedConflictGraph {
    type Neighbors = Neighbors<'a>;
    fn neighbors(self, node: NodeIndex) -> Self::Neighbors {
        PackedConflictGraph::neighbors(self,node)
    }
}
impl IntoNodeIdentifiers for &PackedConflictGraph {
    type NodeIdentifiers = std::iter::Map<std::ops::Range<usize>,fn(usize)->NodeIndex>;
    fn node_identifiers(self) -> Self::NodeIdentifiers { (0..self.nodes).map(NodeIndex::new) }
}
