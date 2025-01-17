from torch import nn
from typing import Optional, Union
import logging
import numpy as np
import torch
import torch.nn.functional as F

from .configuration_performer_attention import PerformerAttentionConfig
from .modeling_utils import (
    find_pruneable_heads_and_indices,
    prune_linear_layer
)

KERNEL_CALLABLES = {
    'cosh': lambda x, h: torch.cat((torch.exp(h + x), torch.exp(h - x)), dim=-1),
    'exp': lambda x, h: torch.exp(h + x), # Default
    'elu': lambda x: F.elu(x) + 1,
    'relu': F.relu
}

SHORT_SEQUENCE_BEHAVIOR_CALLABLES = {
    'use_softmax_eval_only': lambda L, M, training: False if training else L < 2.0 * M,
    'use_softmax_eval_and_train': lambda L, M, training: L < 2.0 * M, 
    'never_use_softmax': lambda L, M, training: False
}


class CausalNumerator(torch.autograd.Function):
    """
    Computes not-normalized FAVOR causal attention A_{masked}V
    Args:
        L: seq_length, B: batch, H: head, M: num_feature, D: dim_per_head
        q_prime: query_prime tensor of the shape [L,B,H,M].
        k_prime: key_prime tensor of the shape [L,B,H,M].
        v: value tensor of the shape [L,B,H,D].
    Returns:
        Not-normalized FAVOR causal attention A_{masked}V.
        shape: [L,B,H,D].
    """
    @staticmethod
    def forward(ctx, q_prime, k_prime, v):
        result = []
        sums = torch.zeros_like(torch.einsum("ijk,ijl->ijkl", k_prime[0], v[0]))
        for index in range(q_prime.shape[0]):
            sums = sums + torch.einsum("ijk,ijl->ijkl", k_prime[index], v[index])
            result.append(torch.einsum("ijkl,ijk->ijl", sums, q_prime[index])[None, Ellipsis])

        ctx.save_for_backward(q_prime, k_prime, v, sums)
        result = torch.cat(result, dim=0)
        return result

    @staticmethod
    def backward(ctx, grad_outputs):
        q_prime, k_prime, v, sums = ctx.saved_tensors

        grads = torch.zeros_like(torch.einsum("ijk,ijl->ijkl", k_prime[0], v[0]))

        gr_sums = sums

        q_grads = []
        k_grads = []
        v_grads = []

        for index in range(q_prime.shape[0] - 1, -1, -1):
            q_grads.append(
                torch.einsum("ijkl,ijl->ijk", gr_sums, grad_outputs[index])[None, Ellipsis])
            grads = grads + torch.einsum("ijk,ijl->ijkl", q_prime[index], grad_outputs[index])
            k_grads.append(torch.einsum("ijkl,ijl->ijk", grads, k_prime[index])[None, Ellipsis])
            v_grads.append(torch.einsum("ijkl,ijk->ijl", grads, k_prime[index])[None, Ellipsis])
            gr_sums = gr_sums - torch.einsum("ijk,ijl->ijkl", k_prime[index], v[index])

        q_grads = torch.cat(q_grads[::-1], dim=0)
        k_grads = torch.cat(k_grads[::-1], dim=0)
        v_grads = torch.cat(v_grads[::-1], dim=0)
        return q_grads, k_grads, v_grads


class CausalDenominator(torch.autograd.Function):
    """Computes FAVOR normalizer in causal attention.
      Args:
        q_prime: query_prime tensor of the shape [L,B,H,M].
        k_prime: key_prime tensor of the shape [L,B,H,M].
      Returns:
        FAVOR normalizer in causal attention. [L,B,H]
      """
    @staticmethod
    def forward(ctx, q_prime, k_prime):
        result = []
        # shape: [M, ]
        sums = torch.zeros_like(k_prime[0])

        for index in range(q_prime.shape[0]):
            sums = sums + k_prime[index]
            result.append(torch.sum(q_prime[index] * sums, dim=2)[None, Ellipsis])
        ctx.save_for_backward(q_prime, k_prime, sums)
        result = torch.cat(result, dim=0)
        return result

    @staticmethod
    def backward(ctx, grad_outputs):
        q_prime, k_prime, sums = ctx.saved_tensors
        k_grad = torch.zeros_like(k_prime[0])

        gr_sums = sums

        q_grads = []
        k_grads = []

        for index in range(q_prime.shape[0] - 1, -1, -1):
            q_grads.append(
                torch.einsum("ijk,ij->ijk", gr_sums, grad_outputs[index])[None, Ellipsis])
            k_grad = k_grad + torch.einsum("ijk,ij->ijk", q_prime[index], grad_outputs[index])
            k_grads.append(k_grad[None, Ellipsis][None, Ellipsis])
            gr_sums = gr_sums - k_prime[index]

        q_grads = torch.cat(q_grads[::-1], dim=0)
        k_grads = torch.cat(k_grads[::-1], dim=0)
        return q_grads, k_grads

causal_numerator = CausalNumerator.apply
causal_denominator = CausalDenominator.apply


class PerformerAttention(nn.Module):
    def __init__(self, config: Optional[Union[dict, PerformerAttentionConfig]] = None, **kwargs):
        super().__init__()
        
        if config is not None:
            # config can either be a dictionary or a PerformerAttentionConfig object
            if not isinstance(config, dict):
                config = config.__dict__
            
            # Just copy over all the parameters
            self.__dict__.update(config)
        else:
            # Make sure we have all the default values filled in
            config = PerformerAttentionConfig(**kwargs)
            kwargs = config.__dict__
        
        # kwargs take precedence over the default values that might be stored in the config object
        self.__dict__.update(kwargs)
        
        if self.num_heads is None or self.d_model is None:
            raise ValueError("PerformerAttention: num_heads and d_model must be non-None")
        
        self.dropout = nn.Dropout(p=self.attention_dropout)
        self.calls_since_last_redraw = 0
        self.random_features = None
        
        behavior = self.short_sequence_behavior
        if not behavior:
            behavior = 'never_use_softmax' if self.kernel_type == 'relu' else 'use_softmax_eval_only'
            self.should_fallback_to_softmax = SHORT_SEQUENCE_BEHAVIOR_CALLABLES[behavior]
        
        elif self.kernel_type == 'relu' and behavior != 'never_use_softmax':
            raise ValueError(f"PerformerAttention: short_sequence_behavior = {behavior} cannot be combined with the relu "
                             "kernel type")
        
        elif isinstance(behavior, str):
            self.should_fallback_to_softmax = SHORT_SEQUENCE_BEHAVIOR_CALLABLES[behavior]
        elif callable(behavior):
            self.should_fallback_to_softmax = behavior
        else:
            raise ValueError("PerformerAttention: short_sequence_behavior must be either str or Callable")
        
        self.kernel_fn = KERNEL_CALLABLES[self.kernel_type]

        assert self.d_model % self.num_heads == 0
        
        if self.use_qkv_linear_layers:
            self.q_lin = nn.Linear(in_features=self.d_model, out_features=self.d_model)
            self.k_lin = nn.Linear(in_features=self.d_model, out_features=self.d_model)
            self.v_lin = nn.Linear(in_features=self.d_model, out_features=self.d_model)
        
        self.out_lin = nn.Linear(in_features=self.d_model, out_features=self.d_model)

        self.pruned_heads = set()

    def prune_heads(self, heads):
        attention_head_size = self.d_model // self.num_heads
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(heads, self.num_heads, attention_head_size, self.pruned_heads)
        # Prune linear layers
        if self.use_qkv_linear_layers:
            self.q_lin = prune_linear_layer(self.q_lin, index)
            self.k_lin = prune_linear_layer(self.k_lin, index)
            self.v_lin = prune_linear_layer(self.v_lin, index)
        
        self.out_lin = prune_linear_layer(self.out_lin, index, dim=1)
        # Update hyper params
        self.num_heads = self.num_heads - len(heads)
        self.d_model = attention_head_size * self.num_heads
        self.pruned_heads = self.pruned_heads.union(heads)
    
    def redraw_features_now(self):
        device = self.random_features.device
        self._generate_feature_matrix(device)
        
        if self.training and self.redraw_verbose:
            logging.info("PerformerAttention: Just redrew random features.")
        
        self.calls_since_last_redraw = 0

    def forward(self, query, key, value, mask=None, head_mask=None, output_attentions=False):
        """
        Parameters:
            query: torch.tensor(bs, seq_length, dim)
            key: torch.tensor(bs, seq_length, dim)
            value: torch.tensor(bs, seq_length, dim)
            mask: torch.tensor(bs, seq_length)

        Returns:
            weights: torch.tensor(bs, num_heads, seq_length, seq_length) Attention weights context: torch.tensor(bs,
            seq_length, dim) Contextualized layer. Optional: only if `output_attentions=True`
        """
        bs, q_length, dim = query.size()
        k_length = key.size(1)
        # assert dim == self.d_model, 'Dimensions do not match: %s input vs %s configured' % (dim, self.d_model)
        # assert key.size() == value.size()

        dim_per_head = self.d_model // self.num_heads
        mask_reshp = (bs, 1, 1, k_length)

        def shape(x):
            """ separate heads """
            return x.view(bs, -1, self.num_heads, dim_per_head).transpose(1, 2)
        
        if self.use_qkv_linear_layers:
            q = self.q_lin(query)
            k = self.k_lin(key)
            v = self.v_lin(value)
        else:
            q, k, v = query, key, value
        
        # (bs, num_heads, q_length, dim_per_head)
        q, k, v = (shape(x) for x in (q, k, v))
        
        # If the sequence length is short enough that FAVOR+ would use considerably more time and/or memory than just
        # using softmax attention, use softmax. This works because FAVOR+ is an unbiased estimator of softmax attention.
        m = round(dim_per_head * np.log(dim_per_head)) # m is the number of random features
        if self.should_fallback_to_softmax(q_length, m, self.training):
            scores = q @ k.transpose(-2, -1) / (dim ** 0.5)
            
            if mask is not None:
                mask = (mask == 0).view(mask_reshp).expand_as(scores)  # (bs, num_heads, q_length, k_length)
                scores.masked_fill_(mask, -float("inf"))  # (bs, num_heads, q_length, k_length)

            if self.causal:
                attention_mask = torch.tril(torch.ones(q_length, q_length)).view(1, 1, q_length, q_length)
                # (bs, num_heads, q_length, k_length)
                attention_mask = (attention_mask == 0).expand_as(scores)
                scores.masked_fill_(attention_mask, -float("inf"))  # (bs, num_heads, q_length, k_length)

            attn_map = nn.Softmax(dim=-1)(scores)
            attn_map = self.dropout(attn_map)  # (bs, num_heads, q_length, k_length)
            return self._finalize_attention_output(attn_map @ v, head_mask, attn_map)
        
        # When we're using FAVOR+ we can't output the attention matrix
        if output_attentions:
            raise ValueError("PerformerAttention: Can't output attention maps when using FAVOR+ linear attention.")
        
        self._redraw_features_if_needed(q.device)
        
        # Get the transformed values of Q and K
        q_prime, k_prime = self.get_projected_queries_and_keys(q, k)
        return self.compute_attention_with_projected_queries_and_keys(q_prime, k_prime, v, mask, head_mask)
    
    # Turns Q into Q', K into K'
    def get_projected_queries_and_keys(self, q, k):
        # Broadcast the feature matrix across the batch dimension
        new_shape = list(q.shape)
        new_shape[-2] = self.random_features.shape[-2]
        W_t = self.random_features.expand(new_shape).transpose(-2, -1)
        
        # Instead of dividing the product QK^T by sqrt(d), we divide Q and K by the 4th root of d.
        q = q / (self.d_model ** 0.25)
        k = k / (self.d_model ** 0.25)
        
        projected_q = q @ W_t
        projected_k = k @ W_t
        
        # Special logic for kernels that attempt to approximate softmax
        if self.kernel_type in ('cosh', 'exp'):
            # The h(x) function is defined in Lemma 1 in Choromanski et al. pg. 4 as exp(-||x||**2 / 2). For numerical
            # stability we leverage the fact that exp(x)*exp(y) = exp(x + y) here and delay computing the exp().
            h_of_q = -torch.sum(q ** 2, dim=-1, keepdim=True) / 2
            h_of_k = -torch.sum(k ** 2, dim=-1, keepdim=True) / 2
            
            # Compute the numerical stabilizer that we subtract from the input to exp(). For some reason the original
            # Jax implementation uses different types of stabilizers for queries vs. keys, and we follow that here.
            # This is a workaround for very slow performance of torch.max(dim=N) on PyTorch 1.4 and earlier;
            # see this GitHub discussion: https://github.com/pytorch/pytorch/issues/36900
            q_indices = h_of_q.argmax(-1).unsqueeze(-1)
            q_stabilizer = h_of_q.gather(-1, q_indices)  # Note this is a (d_model, 1) matrix that gets broadcasted
            
            # This is just a scalar
            k_stabilizer = torch.max(h_of_k)
            
            q_kernel_output = self.kernel_fn(projected_q - q_stabilizer, h_of_q)
            k_kernel_output = self.kernel_fn(projected_k - k_stabilizer, h_of_k)
            
            # By multiplying by 1/sqrt(m), we ensure the final matrix product will contain a factor of 1/m. This means
            # each row of Q'K'^T can be interpreted as an average over the exp(omega^T * q) * exp(omega^T * k) terms.
            normalizing_constant = (q_kernel_output.shape[-1] ** -0.5)
            
            q_prime = normalizing_constant * (q_kernel_output + self.kernel_epsilon)
            k_prime = normalizing_constant * (k_kernel_output + self.kernel_epsilon)
            return q_prime, k_prime
        
        # Generalized attention (ReLU, ELU...)
        else:
            return (self.kernel_fn(x) + self.kernel_epsilon for x in (projected_q, projected_k))
    
    def compute_attention_with_projected_queries_and_keys(self, q_prime, k_prime, v, mask = None, head_mask = None):
        # q_prime, k_prime (bs, num_heads, q_length, num_features)
        # v (bs, num_heads, q_length, dim_per_head)
        # mask (bs, seq_length)

        # Apply the padding mask to K'. Also applying it to Q' would be redundant.
        if mask is not None:
            # mask: (bs, 1, q_length, 1) -> (bs, num_heads, q_length, num_features)
            k_prime *= mask.unsqueeze(1).unsqueeze(-1).expand_as(k_prime)

        # (bs, num_heads, num_features, q_length)
        k_prime_t = k_prime.transpose(-2, -1)

        # q_prime, k_prime shape: (bs, num_heads, q_length, num_features)
        # v shape: (bs, num_heads, q_length, dim_per_head)
        # output: (bs, num_heads, q_length, dim_per_head)

        def _reshape(x):
            # (bs, num_heads, q_length, x) - >(q_length, bs, num_head, x)
            return torch.einsum('bhlx->lbhx', x)

        def _dereshape(x):
            return torch.einsum('lbhx->bhlx', x)

        if self.casual:
            # q_prime: query_prime tensor of the shape [L,B,H,M].
            # k_prime: key_prime tensor of the shape [L,B,H,M].
            # v: value tensor of the shape [L,B,H,D].
            q_prime, k_prime, v = (_reshape(x) for x in (q_prime, k_prime, v))
            output = causal_numerator(q_prime, k_prime, v)  # [L,B,H,D].
            output = torch.einsum('lbhd->bhld', output)  # (bs, num_heads, q_length, dim_per_head)
            q_prime, k_prime, v = (_dereshape(x) for x in (q_prime, k_prime, v))

        else:
            output = q_prime @ (k_prime_t @ v)  # (bs, num_heads, q_length, dim_per_head)

        # Ensure that the output vectors are convex combinations of input vectors; that is,
        # the implied attention scores sum to 1
        if self.normalize_output:
            # Equivalent to multiplying K'^T by a ones vector
            if self.casual:
                # q_prime: query_prime tensor of the shape [L,B,H,M].
                # k_prime: key_prime tensor of the shape [L,B,H,M].
                q_prime, k_prime = (_reshape(x) for x in (q_prime, k_prime))
                d = causal_denominator(q_prime, k_prime)  # [L,B,H]
                d = torch.einsum('lbh->bhl', d).unsqueeze(-1)  # (bs, num_head, q_length, 1)
                # q_prime, k_prime shape: (bs, num_heads, q_length, num_features)
                q_prime, k_prime = (_dereshape(x) for x in (q_prime, k_prime))

            else:
                # d shape: (bs, num_head, q_length, 1)
                d = q_prime @ k_prime.sum(dim=-2).unsqueeze(-1)

            # Avoid dividing by very small numbers
            d += 2 * self.normalization_stabilizer * (torch.abs(d) <= self.normalization_stabilizer)
            output /= d

        # output: (bs, num_heads, q_length, dim_per_head)
        return self._finalize_attention_output(output, head_mask)
    
    def _finalize_attention_output(self, context, head_mask=None, att_map_to_output=None):
        def unshape(x):
            """ group heads """
            return x.transpose(1, 2).contiguous().view(x.shape[0], -1, x.shape[1] * x.shape[-1])
        
        # Mask heads if we want to
        if head_mask is not None:
            context = context * head_mask
            
        context = unshape(context)  # (bs, q_length, dim)
        context = self.out_lin(context)  # (bs, q_length, dim)

        if att_map_to_output:
            return context, att_map_to_output
        else:
            return context,

    def _generate_feature_matrix(self, device):
        dim_per_head = self.d_model // self.num_heads
        num_rows = round(dim_per_head * np.log(dim_per_head))
        
        if not self.use_orthogonal_features:
            return torch.randn(num_rows, dim_per_head, device=device)
        
        def get_square_block(size):
            unstructured_block = torch.randn(size, size, device='cpu')
            q, r = torch.qr(unstructured_block, some = True)
            return q.t()
        
        num_full_blocks = num_rows // dim_per_head
        block_list = [get_square_block(dim_per_head) for _ in range(num_full_blocks)]
        
        remaining_rows = num_rows - num_full_blocks * dim_per_head
        if remaining_rows > 0:
            q = get_square_block(dim_per_head)
            block_list.append(q[:remaining_rows])
        
        final_matrix = torch.cat(block_list)
        
        # This option yields SMREG
        if self.regularize_feature_norms:
            final_matrix *= dim_per_head ** 0.5
        else:
            # Hack to make the matrix columns have the norm we would expect them to have if they were sampled straight
            # from a Gaussian, instead of being all norm 1 since they went through QR decomposition
            multiplier = torch.randn(num_rows, dim_per_head, device='cpu').norm(dim = 1)
            final_matrix = torch.diag(multiplier) @ final_matrix
        
        random_features = final_matrix.to(device)
        self.random_features = random_features
    
    def _redraw_features_if_needed(self, device):
        # We haven't created the projection matrix yet, let's create it
        if self.random_features is None:
            self._generate_feature_matrix(device)
        
        elif self.feature_redraw_interval is not None:
            if self.redraw_stochastically:
                # Flip a (very biased) coin
                if np.random.default_rng().binomial(1, 1. / self.feature_redraw_interval):
                    self.redraw_features_now()
            
            # It's time to redraw the projection matrix
            elif self.calls_since_last_redraw >= self.feature_redraw_interval:
                self.redraw_features_now()
        
            # Keep track of how many forward passes we do before we redraw again
            else:
                self.calls_since_last_redraw += 1
