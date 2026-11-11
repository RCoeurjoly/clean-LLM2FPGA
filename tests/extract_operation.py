import torch


def extract_operation(program, name):
    class Found(Exception):
        pass

    class Capture(torch.fx.Interpreter):
        def run_node(self, node):
            if node.name != name:
                return super().run_node(node)
            graph = torch.fx.Graph()
            inputs = node.all_input_nodes
            mapping = {value: graph.placeholder(value.name) for value in inputs}
            graph.output(graph.node_copy(node, mapping.__getitem__))
            self.result = (torch.fx.GraphModule({}, graph),
                           tuple(self.env[value] for value in inputs),
                           super().run_node(node))
            raise Found

    capture = Capture(program.module())
    try:
        capture.run(*program.example_inputs[0], **program.example_inputs[1])
    except Found:
        return capture.result
    raise ValueError(f'operation not found: {name}')
