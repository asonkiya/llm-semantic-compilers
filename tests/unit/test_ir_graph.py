from cgir.ir import Edge, EdgeKind, Node, NodeKind, RepoGraph


def test_add_and_query() -> None:
    g = RepoGraph()
    g.add_node(Node(id="a", kind=NodeKind.Function, name="a"))
    g.add_node(Node(id="b", kind=NodeKind.Function, name="b"))
    g.add_edge(Edge(src="a", dst="b", kind=EdgeKind.CALLS))

    funcs = list(g.nodes(NodeKind.Function))
    assert {n.id for n in funcs} == {"a", "b"}

    out = list(g.out_edges("a", EdgeKind.CALLS))
    assert len(out) == 1 and out[0].dst == "b"


def test_to_jsonable_round_trip_shape() -> None:
    g = RepoGraph()
    g.add_node(Node(id="m", kind=NodeKind.Module, name="m"))
    payload = g.to_jsonable()
    assert payload["nodes"][0]["kind"] == "Module"
    assert payload["edges"] == []


def test_from_jsonable_round_trip() -> None:
    """to_jsonable → from_jsonable reconstructs nodes, edges, kinds, attrs."""
    g = RepoGraph()
    g.add_node(Node(id="m", kind=NodeKind.Module, name="m", path="m.py"))
    g.add_node(
        Node(
            id="func:m.f",
            kind=NodeKind.Function,
            name="f",
            path="m.py",
            start_line=1,
            end_line=2,
            attrs={"qualname": "m.f", "writes": ["x"]},
        )
    )
    g.add_edge(Edge(src="m", dst="func:m.f", kind=EdgeKind.CONTAINS))

    restored = RepoGraph.from_jsonable(g.to_jsonable())

    assert len(restored) == 2
    func = restored.get_node("func:m.f")
    assert func.kind == NodeKind.Function
    assert func.start_line == 1
    assert func.attrs["writes"] == ["x"]
    [edge] = restored.out_edges("m", EdgeKind.CONTAINS)
    assert edge.dst == "func:m.f"
