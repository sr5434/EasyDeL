import dataclasses
import typing

from EasyDel.trainer.config import TrainArguments

import jax
import flax
import optax
from transformers import FlaxAutoModelForCausalLM, AutoConfig
from IPython.display import clear_output
from tqdm import tqdm
from EasyDel.utils.utils import Timers
from torch.utils.tensorboard import SummaryWriter
from jax.experimental.pjit import pjit, with_sharding_constraint, PartitionSpec
from flax.training import train_state
from jax import numpy as jnp
from torch.utils.data import DataLoader
from fjutils import match_partition_rules, make_shard_and_gather_fns, StreamingCheckpointer, count_params


def fsdp_train_step(state, batch):
    batch = with_sharding_constraint(batch, PartitionSpec(('dp', 'fsdp')))

    def calculate_loss(params):
        logits = state.apply_fn(params=params, **batch,
                                return_dict=True).logits
        loss = optax.softmax_cross_entropy_with_integer_labels(logits=logits[..., :-1, :],
                                                               labels=batch['input_ids'][..., 1:])
        loss = jnp.mean(loss)
        return loss

    grad_fn = jax.value_and_grad(calculate_loss, has_aux=False)
    loss__, grad = grad_fn(state.params)
    state = state.apply_gradients(grads=grad)
    return state, loss__


def predict(state, input_ids):
    input_ids = with_sharding_constraint(input_ids, PartitionSpec(('dp', 'fsdp')))
    pred = state.apply_fn(params=state.params, input_ids=input_ids, return_dict=True)
    token = jnp.argmax(jax.nn.softmax(pred.logits)[:, -1, :])
    input_ids = jnp.concatenate([input_ids, token.reshape(1, -1)], axis=-1)
    return input_ids


@dataclasses.dataclass
class OutputFineTuner:
    train_state: typing.Any
    predict_fun: typing.Any
    mesh: typing.Any
    ckpt_stream: typing.Any
    gather_fns: typing.Any
    shard_fns: typing.Any
    last_save_file_name: str


def finetuner(
        dataset,
        ckpt_path,
        training_arguments: TrainArguments,
        use_pjit_attention_force: bool = True,
        dtype=jnp.bfloat16,
        param_dtype=jnp.bfloat16,
        fully_fsdp=True,
        use_wandb: bool = True,
        custom_rule=None
) -> OutputFineTuner:
    wandb_runtime = training_arguments.get_wandb_init() if use_wandb else None
    max_length = training_arguments.max_length

    def collate_fn(batch):
        rs = {}
        for key in batch[0].keys():
            ssp = [jnp.array(f[key])[..., -max_length:] for f in batch]
            rs[key] = jnp.stack(ssp).reshape(-1, ssp[0].shape[-1])
        return rs

    dataloader = DataLoader(dataset, collate_fn=collate_fn,
                            batch_size=training_arguments.total_batch_size, drop_last=True)
    max_steps = training_arguments.num_train_epochs * len(
        dataloader) if training_arguments.max_steps is None else training_arguments.max_steps
    config = AutoConfig.from_pretrained(training_arguments.model_id, trust_remote_code=True
                                        , gradient_checkpointing=training_arguments.gradient_checkpointing,
                                        use_pjit_attention_force=use_pjit_attention_force
                                        )

    assert hasattr(config, 'get_partition_rules')
    model = FlaxAutoModelForCausalLM.from_config(config, trust_remote_code=True, dtype=dtype,
                                                 param_dtype=param_dtype,
                                                 _do_init=False)
    tx, scheduler = training_arguments.get_optimizer_and_scheduler(max_steps)

    def init_fn():
        # from flax.training import train_state
        params__ = model.init_weights(jax.random.PRNGKey(0), (1, max_length))
        if dtype == jnp.bfloat16:
            params__ = model.to_bf16(params__)
        elif dtype == jnp.float16:
            params__ = model.to_fp16(params__)
        return train_state.TrainState.create(
            tx=tx,
            params=flax.core.freeze({'params': params__}),
            apply_fn=model.__call__
        )

    def create_train_state_from_params(params_):
        # from flax.training import train_state
        return train_state.TrainState.create(
            tx=tx,
            apply_fn=model.__call__,
            params=params_
        )

    train_state_shape = jax.eval_shape(init_fn)
    train_state_partition_spec = match_partition_rules(
        config.get_partition_rules(fully_fsdp=fully_fsdp) if custom_rule is None else custom_rule,
        train_state_shape)
    sharded_create_from_params_fn = pjit(
        create_train_state_from_params,
        in_shardings=(train_state_partition_spec.params,),
        out_shardings=train_state_partition_spec,
        donate_argnums=(0,)
    )
    sharded_train_step_fn = pjit(
        fsdp_train_step,
        in_shardings=(train_state_partition_spec, PartitionSpec()),
        out_shardings=(train_state_partition_spec, PartitionSpec()),
        donate_argnums=(0, 0, 0),
    )
    sharded_predict = pjit(predict, out_shardings=PartitionSpec(),
                           in_shardings=(train_state_partition_spec, PartitionSpec()))
    mesh = training_arguments.get_mesh()
    training_arguments.ckpt_path_exists()
    ckpt_streamer = training_arguments.get_streaming_checkpointer()
    with mesh:
        shard_fns, gather_fns = make_shard_and_gather_fns(train_state_partition_spec, dtype_specs=dtype)
        _, params = StreamingCheckpointer.load_trainstate_checkpoint(
            f'params::{ckpt_path}', train_state_shape, shard_fns
        )
        sharded_train_state_ = sharded_create_from_params_fn(params)
        count_params(sharded_train_state_.params)

        pbar = tqdm(total=max_steps)
        i = sharded_train_state_.step.tolist()
        losses = []
        pbar.update(sharded_train_state_.step.tolist())
        learning_rates = []
        for ep in range(training_arguments.num_train_epochs):
            for batch in dataloader:
                i += 1
                if i < max_steps:

                    _ = batch.pop('token_type_ids', None)
                    sharded_train_state_, loss = sharded_train_step_fn(sharded_train_state_, batch)
                    losses.append(loss)
                    learning_rates.append(scheduler(i).tolist())
                    pbar.update(1)
                    clear_output(True)
                    if use_wandb:
                        wandb_runtime.log(
                            {'loss': loss, 'learning_rate': scheduler(sharded_train_state_.step.tolist()).tolist(),
                             'step': sharded_train_state_.step.tolist()}
                        )
                    pbar.set_postfix(loss=loss, learning_rate=scheduler(sharded_train_state_.step.tolist()).tolist(),
                                     step=sharded_train_state_.step.tolist())
                else:
                    break
                if training_arguments.save_steps is not None and i % training_arguments.save_steps == 0:
                    filename = f'{training_arguments.model_name}-{sum(losses) / len(losses)}-{i}'
                    print(f'Saving Model to \033[1;30m{filename}\033[1;0m')
                    ckpt_streamer.save_checkpoint(sharded_train_state_.params['params'],
                                                  filename,
                                                  gather_fns=gather_fns.params['params'])
        if training_arguments.save_steps is None:
            filename = f'{training_arguments.model_name}-{sum(losses) / len(losses)}-{i}'
            print(f'Saving Model to \033[1;30m{filename}\033[1;0m')
            ckpt_streamer.save_checkpoint(sharded_train_state_.params['params'],
                                          filename,
                                          gather_fns=gather_fns.params['params'])

    output = OutputFineTuner(
        last_save_file_name=filename,
        predict_fun=sharded_predict,
        train_state=sharded_train_state_,
        mesh=mesh,
        shard_fns=shard_fns,
        gather_fns=gather_fns,
        ckpt_stream=ckpt_streamer
    )
    return output