import jax
from jax import numpy as jnp


# CONVERTER Falcon-7B
def convert_pt_to_flax_7b(state_dict_pt, n_layers: int, device=jax.devices('cpu')[0]):
    with jax.default_device(device):
        state_dict_flax = {}
        state_dict_flax[('transformer'), ('wte'), ('embedding')] = state_dict_pt[
            'transformer.word_embeddings.weight'].cpu().detach().numpy()
        for i in range(n_layers):
            state_dict_flax[('transformer'), ('h'), (f'{i}'), ('input_layernorm'), ('scale')] = state_dict_pt[
                f'transformer.h.{i}.input_layernorm.weight'].cpu().detach().numpy()
            state_dict_flax[('transformer'), ('h'), (f'{i}'), ('input_layernorm'), ('bias')] = state_dict_pt[
                f'transformer.h.{i}.input_layernorm.bias'].cpu().detach().numpy()
            state_dict_flax[('transformer'), ('h'), (f'{i}'), ('mlp'), ('down'), ('kernel')] = jnp.transpose(
                state_dict_pt[f'transformer.h.{i}.mlp.dense_4h_to_h.weight'].cpu().detach().numpy(), (1, 0))
            state_dict_flax[('transformer'), ('h'), (f'{i}'), ('mlp'), ('up'), ('kernel')] = jnp.transpose(
                state_dict_pt[f'transformer.h.{i}.mlp.dense_h_to_4h.weight'].cpu().detach().numpy(), (1, 0))
            state_dict_flax[
                ('transformer'), ('h'), (f'{i}'), ('self_attention'), ('w_qkv'), ('kernel')] = jnp.transpose(
                state_dict_pt[f'transformer.h.{i}.self_attention.query_key_value.weight'].cpu().detach().numpy(),
                (1, 0))
            state_dict_flax[('transformer'), ('h'), (f'{i}'), ('self_attention'), ('wo'), ('kernel')] = jnp.transpose(
                state_dict_pt[f'transformer.h.{i}.self_attention.dense.weight'].cpu().detach().numpy(), (1, 0))
        state_dict_flax[('transformer'), ('ln_f'), ('scale')] = state_dict_pt[
            f'transformer.ln_f.weight'].cpu().detach().numpy()
        state_dict_flax[('transformer'), ('ln_f'), ('bias')] = state_dict_pt[
            f'transformer.ln_f.bias'].cpu().detach().numpy()
        state_dict_flax[('lm_head'), ('kernel')] = jnp.transpose(
            state_dict_pt[f'lm_head.weight'].cpu().detach().numpy(), (1, 0))
    return state_dict_flax