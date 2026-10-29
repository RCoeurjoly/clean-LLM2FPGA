
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
