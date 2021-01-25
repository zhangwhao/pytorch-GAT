import time


import torch
import torch.nn as nn


from utils.constants import LayerType


# todo: experiment with both transductive AND inductive settings
# todo: be explicit about shapes throughout the code
class GAT(torch.nn.Module):
    def __init__(self, num_of_layers, num_heads_per_layer, num_features_per_layer, dropout=0.6, layer_type=LayerType.IMP3):
        super().__init__()

        # Short names for readability (much shorter lines)
        nfpl = num_features_per_layer
        nhpl = num_heads_per_layer

        GATLayer = get_layer_type(layer_type)

        self.gat_net = nn.Sequential(
            *[GATLayer(nfpl[i - 1] * nhpl[i-2], nfpl[i], nhpl[i-1], dropout_prob=dropout) for i in range(1, num_of_layers)] if num_of_layers >= 2 else nn.Identity(),
            GATLayer(nfpl[-2] * nhpl[-2], nfpl[-1], nhpl[-1], dropout_prob=dropout, concat=False, activation=None)
        )

    # data is just a (in_nodes_features, edge_index) tuple, I had to do it like this because of the nn.Sequential:
    # https://discuss.pytorch.org/t/forward-takes-2-positional-arguments-but-3-were-given-for-nn-sqeuential-with-linear-layers/65698
    def forward(self, data):
        return self.gat_net(data)


# todo: nobody should be able to instantiate this one
class GATLayer(torch.nn.Module):

    def __init__(self, num_in_features, num_out_features, num_of_heads, layer_type, concat=True, activation=nn.ELU(),
                 dropout_prob=0.6, add_skip_connection=True, bias=True, log_attention_weights=False):

        super().__init__()

        self.num_of_heads = num_of_heads
        self.num_out_features = num_out_features
        self.concat = concat  # whether we should concatenate or average the attention heads
        self.add_skip_connection = add_skip_connection

        #
        # Trainable weights: linear projection matrix (denoted as "W" in the paper), attention target/source
        # (denoted as "a" in the paper) and bias (not mentioned in the paper but present in the official GAT repo)
        #

        if layer_type == LayerType.IMP4:
            self.linear_proj = nn.Parameter(torch.Tensor(num_in_features, num_of_heads * num_out_features))
        elif layer_type == LayerType.IMP1:
            self.linear_proj = nn.Parameter(torch.Tensor(num_of_heads, num_in_features, num_out_features))
        else:
            # You can treat this one matrix as num_of_heads independent W matrices
            self.linear_proj = nn.Linear(num_in_features, num_of_heads * num_out_features, bias=False)

        # After we concatenate target node (node i) and source node (node j) we apply the additive scoring function
        # which gives us un-normalized score "e". Here we split the "a" vector - but the semantics remain the same.

        if layer_type == LayerType.IMP1:
            self.scoring_fn_target = nn.Parameter(torch.Tensor(num_of_heads, num_out_features, 1))
            self.scoring_fn_source = nn.Parameter(torch.Tensor(num_of_heads, num_out_features, 1))
        else:
            # Basically instead of doing [x, y] (concatenation, x/y are node feature vectors) and dot product with "a"
            # we instead do a dot product between x and "a_left" and y and "a_right" and we sum them up
            self.scoring_fn_target = nn.Parameter(torch.Tensor(1, num_of_heads, num_out_features))
            self.scoring_fn_source = nn.Parameter(torch.Tensor(1, num_of_heads, num_out_features))

        # Bias is not crucial to GAT method (I pinged the main author, Petar, on this one)
        if bias and concat:
            self.bias = nn.Parameter(torch.Tensor(num_of_heads * num_out_features))
        elif bias and not concat:
            self.bias = nn.Parameter(torch.Tensor(num_out_features))
        else:
            self.register_parameter('bias', None)

        #
        # End of trainable weights
        #

        self.leakyReLU = nn.LeakyReLU(0.2)  # using 0.2 as in the paper, no need to expose every setting
        self.softmax = nn.Softmax(dim=-1)  # -1 stands for apply the log-softmax along the last dimension
        self.activation = activation
        # Probably not the nicest design but I use the same module in 3 locations, before/after features projection
        # and for attention coefficients. Functionality-wise it's the same as using independent modules.
        self.dropout = nn.Dropout(p=dropout_prob)

        self.log_attention_weights = log_attention_weights  # whether we should log the attention weights
        self.attention_weights = None  # for later visualization purposes, I cache the weights here

        self.init_params(layer_type)

    def init_params(self, layer_type):
        """
        The reason we're using Glorot (aka Xavier uniform) initialization is because it's a default TF initialization:
            https://stackoverflow.com/questions/37350131/what-is-the-default-variable-initializer-in-tensorflow

        The original repo was developed in TensorFlow (TF) and they used the default initialization.
        Feel free to experiment - there may be better initializations depending on your problem.

        """

        if layer_type == LayerType.IMP1 or layer_type == LayerType.IMP4:
            nn.init.xavier_uniform_(self.linear_proj)
        else:
            nn.init.xavier_uniform_(self.linear_proj.weight)
        nn.init.xavier_uniform_(self.scoring_fn_target)
        nn.init.xavier_uniform_(self.scoring_fn_source)
        if self.bias is not None:
            torch.nn.init.zeros_(self.bias)


# todo: implementation with torch.sparse.mm!!!
class GATLayerImp4(GATLayer):
    """
    Implementation #4 was inspired by PyGAT and official GAT's sparse implementation

    """

    # todo: think this through for inductive setup
    src_nodes_dim = 0  # position of source nodes in edge index
    trg_nodes_dim = 1  # position of target nodes in edge index
    scatter_dim = 0
    nodes_dim = 0

    def __init__(self, num_in_features, num_out_features, num_of_heads, concat=True, activation=nn.ELU(),
                 dropout_prob=0.6, add_skip_connection=True, bias=True, log_attention_weights=False):

        super().__init__(num_in_features, num_out_features, num_of_heads, LayerType.IMP4, concat, activation, dropout_prob,
                      add_skip_connection, bias, log_attention_weights)

    def forward(self, data):
        in_nodes_features, edge_index = data  # unpack data
        num_of_nodes = in_nodes_features.shape[0]

        # shape = (N, FIN) where N - number of nodes in the graph, FIN number of input features per node
        # We apply the dropout to all of the input node features (as mentioned in the paper)
        in_nodes_features = self.dropout(in_nodes_features)

        # shape = (N, NH, FOUT) where NH - number of heads, FOUT number of output features per head
        # We project the input node features into NH independent output features (one for each attention head)
        nodes_features_proj = torch.mm(in_nodes_features, self.linear_proj).view(-1, self.num_of_heads, self.num_out_features)

        nodes_features_proj = self.dropout(nodes_features_proj)  # in the official GAT imp they did dropout here as well

        # todo: like pyGAT using torch.mm (my way takes: 0.000205 per iteration)
        ts = time.time()
        num_of_loops = 100000
        for i in range(num_of_loops):
            # Apply the scoring function (* represents element-wise (a.k.a. Hadamard) product)
            # shape = (N, NH), dim=-1 squeezes the last dimension
            scores_source = (nodes_features_proj * self.scoring_fn_source).sum(dim=-1)
            scores_target = (nodes_features_proj * self.scoring_fn_target).sum(dim=-1)

            # We simply repeat the scores for source/target nodes based on the edge index
            # scores shape = (E, NH, 1), where E is the number of edges in the graph
            # nodes_features_proj_lifted shape = (E, NH, FOUT)
            scores_source_lifted, scores_target_lifted, nodes_features_proj_lifted = self.lift(scores_source, scores_target,
                                                                                               nodes_features_proj,
                                                                                               edge_index)
            scores_per_edge = self.leakyReLU(scores_source_lifted + scores_target_lifted)
        print(f'time elapsed = {(time.time()-ts)/num_of_loops}')

        attentions_per_edge = self.neighborhood_aware_softmax(scores_per_edge, edge_index[self.trg_nodes_dim],
                                                              num_of_nodes)
        # Add stochasticity to neighborhood aggregation
        attentions_per_edge = self.dropout(attentions_per_edge)

    def lift(self, scores_source, scores_target, nodes_features_matrix_proj, edge_index):
        src_nodes_index = edge_index[self.src_nodes_dim]
        trg_nodes_index = edge_index[self.trg_nodes_dim]
        # Using index_select is faster than "normal" indexing (scores_source[src_nodes_index]) in PyTorch!
        scores_source = scores_source.index_select(self.nodes_dim, src_nodes_index)
        scores_target = scores_target.index_select(self.nodes_dim, trg_nodes_index)
        nodes_features_matrix_proj_lifted = nodes_features_matrix_proj.index_select(self.nodes_dim, src_nodes_index)

        return scores_source, scores_target, nodes_features_matrix_proj_lifted


class GATLayerImp3(GATLayer):
    """
    Implementation #3 was inspired by PyTorch Geometric: https://github.com/rusty1s/pytorch_geometric

    But, it's faster (since I don't have the message passing framework overhead) and hopefully more readable!

    """

    # todo: think this through for inductive setup
    src_nodes_dim = 0  # position of source nodes in edge index
    trg_nodes_dim = 1  # position of target nodes in edge index
    scatter_dim = 0
    nodes_dim = 0

    def __init__(self, num_in_features, num_out_features, num_of_heads, concat=True, activation=nn.ELU(),
                 dropout_prob=0.6, add_skip_connection=True, bias=True, log_attention_weights=False):

        super().__init__(num_in_features, num_out_features, num_of_heads, LayerType.IMP3, concat, activation, dropout_prob,
                      add_skip_connection, bias, log_attention_weights)

    def forward(self, data):
        in_nodes_features, edge_index = data  # unpack data
        num_of_nodes = in_nodes_features.shape[0]

        # shape = (N, FIN) where N - number of nodes in the graph, FIN number of input features per node
        # We apply the dropout to all of the input node features (as mentioned in the paper)
        in_nodes_features = self.dropout(in_nodes_features)

        # shape = (N, NH, FOUT) where NH - number of heads, FOUT number of output features per head
        # We project the input node features into NH independent output features (one for each attention head)
        # todo: torch.mm (it would be faster since lin proj doesn't have bias...)?
        nodes_features_proj = self.linear_proj(in_nodes_features).view(-1, self.num_of_heads, self.num_out_features)

        nodes_features_proj = self.dropout(nodes_features_proj)  # in the official GAT imp they did dropout here as well

        # Apply the scoring function (* represents element-wise (a.k.a. Hadamard) product)
        # shape = (N, NH), dim=-1 squeezes the last dimension
        # todo: torch.sum instead of sum
        scores_source = (nodes_features_proj * self.scoring_fn_source).sum(dim=-1)
        scores_target = (nodes_features_proj * self.scoring_fn_target).sum(dim=-1)

        # We simply repeat the scores for source/target nodes based on the edge index
        # scores shape = (E, NH, 1), where E is the number of edges in the graph
        # nodes_features_proj_lifted shape = (E, NH, FOUT)
        scores_source_lifted, scores_target_lifted, nodes_features_proj_lifted = self.lift(scores_source, scores_target, nodes_features_proj, edge_index)
        scores_per_edge = self.leakyReLU(scores_source_lifted + scores_target_lifted)

        attentions_per_edge = self.neighborhood_aware_softmax(scores_per_edge, edge_index[self.trg_nodes_dim], num_of_nodes)
        # Add stochasticity to neighborhood aggregation
        attentions_per_edge = self.dropout(attentions_per_edge)

        # Element-wise (aka Hadamard) product. Operator * does the same thing as torch.mul
        nodes_features_proj_lifted_weighted = nodes_features_proj_lifted * attentions_per_edge

        # This part adds up weighted, projected neighborhoods for every target node
        size = list(nodes_features_proj_lifted_weighted.shape)  # convert to list otherwise assignment is not possible
        size[self.scatter_dim] = num_of_nodes  # shape = (N, NH, FOUT)
        out_nodes_features = torch.zeros(size, dtype=in_nodes_features.dtype, device=in_nodes_features.device)
        trg_index_broadcasted = self.broadcast(edge_index[self.trg_nodes_dim], nodes_features_proj_lifted_weighted)
        out_nodes_features.scatter_add_(self.scatter_dim, trg_index_broadcasted, nodes_features_proj_lifted_weighted)

        # todo: consider moving beginning and end of the forward function to the parent class
        # todo: consider adding skip/residual connection

        if self.log_attention_weights:
            self.attention_weights = attentions_per_edge

        if self.concat:
            out_nodes_features = out_nodes_features.view(-1, self.num_of_heads * self.num_out_features)
        else:
            out_nodes_features = out_nodes_features.mean(dim=1)

        if self.bias is not None:
            out_nodes_features += self.bias

        out_nodes_features = out_nodes_features if self.activation is None else self.activation(out_nodes_features)
        return (out_nodes_features, edge_index)

    #
    # Helper functions
    #
    def lift(self, scores_source, scores_target, nodes_features_matrix_proj, edge_index):
        src_nodes_index = edge_index[self.src_nodes_dim]
        trg_nodes_index = edge_index[self.trg_nodes_dim]
        # Using index_select is faster than "normal" indexing (scores_source[src_nodes_index]) in PyTorch!
        scores_source = scores_source.index_select(self.nodes_dim, src_nodes_index)
        scores_target = scores_target.index_select(self.nodes_dim, trg_nodes_index)
        nodes_features_matrix_proj_lifted = nodes_features_matrix_proj.index_select(self.nodes_dim, src_nodes_index)

        return scores_source, scores_target, nodes_features_matrix_proj_lifted

    def broadcast(self, this, other):
        # Append singleton dimensions until this.dim() == other.dim()
        for _ in range(this.dim(), other.dim()):
            this = this.unsqueeze(-1)

        # Explicitly expand so that shapes are the same
        return this.expand_as(other)

    def neighborhood_aware_softmax(self, scores_per_edge, trg_index, num_of_nodes):
        """
        As the fn name suggest it does softmax over the neighborhoods. Example: say we have 5 nodes in a graph.
        Two of them 1, 2 are connected to node 3. If we want to calculate the representation for node 3 we should take
        into account feature vectors of 1, 2 and 3 itself. Since we have scores for edges 1-3, 2-3 and 3-3
        in scores_per_edge variable, this function will calculate attention scores like this: 1-3/(1-3+2-3+3-3)
        (where 1-3 is overloaded notation it represents the edge 1-3 and it's (exp) score) and similarly for 2-3 and 3-3
         i.e. we don't care about other edge scores that include nodes 4 and 5.

        Note:
        Subtracting the max value from logits doesn't change the end result but it improves the numerical stability
        and it's a fairly common "trick" used in pretty much every deep learning framework.
        Check out this link for more details:

        https://stats.stackexchange.com/questions/338285/how-does-the-subtraction-of-the-logit-maximum-improve-learning

        """
        # Make logits <= 0 so that e^logit <= 1 (this will improve the numerical stability)
        scores_per_edge = scores_per_edge - scores_per_edge.max()
        exp_scores_per_edge = scores_per_edge.exp()  # softmax
        # shape = (E, NH)
        neigborhood_aware_denominator = self.sum_edge_scores_neighborhood_aware(exp_scores_per_edge, trg_index, num_of_nodes)

        # The only case where the value could be 0 (and thus we'd need 1e-16) is if some target node had no edge
        # pointing to it.
        attentions_per_edge = exp_scores_per_edge / (neigborhood_aware_denominator + 1e-16)
        return attentions_per_edge.unsqueeze(-1)  # so that we can do element-wise multiplication with proj features

    def sum_edge_scores_neighborhood_aware(self, exp_scores_per_edge, trg_index, num_of_nodes):
        # The shape must be the same as in exp_scores_per_edge (required by scatter_add_) i.e. from N -> (N, NH)
        trg_index_broadcasted = self.broadcast(trg_index, exp_scores_per_edge)

        # shape = (N, NH), where N is the number of nodes and NH the number of attention heads
        size = list(exp_scores_per_edge.shape)  # convert to list otherwise assignment is not possible
        size[self.scatter_dim] = num_of_nodes
        neighborhood_sums = torch.zeros(size, dtype=exp_scores_per_edge.dtype, device=exp_scores_per_edge.device)

        # position i will contain a sum of exp scores of all the nodes that point to the node i (as dictated by the
        # target index)
        neighborhood_sums.scatter_add_(self.scatter_dim, trg_index_broadcasted, exp_scores_per_edge)

        # Expand again so that we can use it as a softmax denominator. e.g. node i's sum will be copied to
        # all the locations where the source nodes pointed to i (as dictated by the target index)
        return neighborhood_sums.index_select(self.nodes_dim, trg_index)


# todo: the idea for the imp 2 or 1 was to use torch sparse (maybe add 4th imp lol)
class GATLayerImp2(GATLayer):
    """
        Implementation #2 was inspired by the official GAT implementation: https://github.com/PetarV-/GAT

        It's conceptually simpler but computationally much less efficient.

        Note: this is the naive implementation not the sparse one.

    """

    def __init__(self, num_in_features, num_out_features, num_of_heads, concat=True, activation=nn.ELU(),
                 dropout_prob=0.6, add_skip_connection=True, bias=True, log_attention_weights=False):

        super().__init__(num_in_features, num_out_features, num_of_heads, LayerType.IMP2, concat, activation, dropout_prob,
                         add_skip_connection, bias, log_attention_weights)

    def forward(self, data):
        in_nodes_features, connectivity_mask = data  # unpack data
        num_of_nodes = in_nodes_features.shape[0]
        assert connectivity_mask.shape == (num_of_nodes, num_of_nodes), \
            f'Expected connectivity matrix with shape=({num_of_nodes},{num_of_nodes}), got shape={connectivity_mask.shape}.'

        # shape = (N, FIN) where N - number of nodes in the graph, FIN number of input features per node
        # We apply the dropout to all of the input node features (as mentioned in the paper)
        in_nodes_features = self.dropout(in_nodes_features)

        # shape = (N, NH, FOUT) where NH - number of heads, FOUT number of output features per head
        # We project the input node features into NH independent output features (one for each attention head)
        nodes_features_proj = self.linear_proj(in_nodes_features).view(-1, self.num_of_heads, self.num_out_features)

        nodes_features_proj = self.dropout(nodes_features_proj)  # in the official GAT imp they did dropout here as well

        # Apply the scoring function (* represents element-wise (a.k.a. Hadamard) product)
        # shape = (N, NH, 1)
        scores_source = torch.sum((nodes_features_proj * self.scoring_fn_source), dim=-1, keepdim=True)
        scores_target = torch.sum((nodes_features_proj * self.scoring_fn_target), dim=-1, keepdim=True)

        # src shape = (NH, N, 1) and trg shape = (NH, 1, N)
        scores_source = scores_source.transpose(0, 1)
        scores_target = scores_target.reshape(self.num_of_heads, 1, num_of_nodes)  # todo: profile?

        # shape = (NH, N, N) = (NH, N, 1) + (NH, 1, N) + the magic of automatic broadcast <3
        # all because in Imp3 we are much smarter and don't have to calculate all i.e. NxN scores! (only E!)
        all_scores = self.leakyReLU(scores_source + scores_target)
        all_attention_coefficients = self.softmax(all_scores + connectivity_mask)

        # shape = (NH, N, N) * (NH, N, FOUT) = (NH, N, FOUT)  # todo: profile
        out_nodes_features = torch.bmm(all_attention_coefficients, nodes_features_proj.transpose(0, 1))

        # shape = (N, NH, FOUT) # todo: profile?
        out_nodes_features = out_nodes_features.reshape(num_of_nodes, self.num_of_heads, self.num_out_features)

        if self.log_attention_weights:
            self.attention_weights = all_attention_coefficients

        if self.concat:
            out_nodes_features = out_nodes_features.view(-1, self.num_of_heads * self.num_out_features)
        else:
            out_nodes_features = out_nodes_features.mean(dim=1)

        if self.bias is not None:
            out_nodes_features += self.bias

        out_nodes_features = out_nodes_features if self.activation is None else self.activation(out_nodes_features)
        return (out_nodes_features, connectivity_mask)


# Other
class GATLayerImp1(GATLayer):
    def __init__(self, num_in_features, num_out_features, num_of_heads, concat=True, activation=nn.ELU(),
                 dropout_prob=0.6, add_skip_connection=True, bias=True, log_attention_weights=False):

        super().__init__(num_in_features, num_out_features, num_of_heads, LayerType.IMP1, concat, activation, dropout_prob,
                         add_skip_connection, bias, log_attention_weights)

    def forward(self, data):
        in_nodes_features, connectivity_mask = data  # unpack data
        num_of_nodes = in_nodes_features.shape[0]
        assert connectivity_mask.shape == (num_of_nodes, num_of_nodes), \
            f'Expected connectivity matrix with shape=({num_of_nodes},{num_of_nodes}), got shape={connectivity_mask.shape}.'

        # shape = (N, FIN) where N - number of nodes in the graph, FIN number of input features per node
        # We apply the dropout to all of the input node features (as mentioned in the paper)
        in_nodes_features = self.dropout(in_nodes_features)

        # shape = (NH, N, FOUT) where NH - number of heads, FOUT number of output features per head
        # We project the input node features into NH independent output features (one for each attention head)
        nodes_features_proj = torch.matmul(in_nodes_features.unsqueeze(0), self.linear_proj)

        nodes_features_proj = self.dropout(nodes_features_proj)  # in the official GAT imp they did dropout here as well

        # Apply the scoring function (* represents element-wise (a.k.a. Hadamard) product)
        # shape = (NH, N, 1)
        scores_source = torch.bmm(nodes_features_proj, self.scoring_fn_source)
        scores_target = torch.bmm(nodes_features_proj, self.scoring_fn_target)

        # shape = (NH, N, N) = (NH, N, 1) + (NH, 1, N) + the magic of automatic broadcast <3
        # all because in Imp3 we are much smarter and don't have to calculate all i.e. NxN scores! (only E!)
        all_scores = self.leakyReLU(scores_source + scores_target.transpose(1, 2))
        all_attention_coefficients = self.softmax(all_scores + connectivity_mask)

        # shape = (NH, N, N) * (NH, N, FOUT) = (NH, N, FOUT)
        out_nodes_features = torch.bmm(all_attention_coefficients, nodes_features_proj)

        # shape = (N, NH, FOUT) # todo: profile?
        out_nodes_features = out_nodes_features.transpose(0, 1)  # reshape(num_of_nodes, self.num_of_heads, self.num_out_features)

        if self.log_attention_weights:
            self.attention_weights = all_attention_coefficients

        if self.concat:
            out_nodes_features = out_nodes_features.reshape(-1, self.num_of_heads * self.num_out_features)
        else:
            out_nodes_features = out_nodes_features.mean(dim=1)

        if self.bias is not None:
            out_nodes_features += self.bias

        out_nodes_features = out_nodes_features if self.activation is None else self.activation(out_nodes_features)
        return (out_nodes_features, connectivity_mask)


#
# Helper functions
#
def get_layer_type(layer_type):
    assert isinstance(layer_type, LayerType), f'Expected {LayerType} got {type(layer_type)}.'

    if layer_type == LayerType.IMP1:
        return GATLayerImp1
    elif layer_type == LayerType.IMP2:
        return GATLayerImp2
    elif layer_type == LayerType.IMP3:
        return GATLayerImp3
    elif layer_type == LayerType.IMP4:
        return GATLayerImp4
    else:
        raise Exception(f'Layer type {layer_type} not yet supported.')


