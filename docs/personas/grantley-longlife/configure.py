"""Scoped, repeatable config transformation for the authorized deployment."""
from copy import deepcopy


def desired_config(current):
    config = deepcopy(current)
    assert config['model']['default'] == 'gemini-3.8-flash-high'
    provider = config['providers']['cornna']
    models = provider['models']
    if isinstance(models, list):
        assert all(isinstance(model, str) for model in models)
        models = {model: {} for model in models}
    else:
        models = deepcopy(models)
    models.setdefault('gemini-3.8-flash-high', {})['supports_vision'] = True
    provider['models'] = models
    config.setdefault('agent', {})['image_input_mode'] = 'auto'
    config['group_sessions_per_user'] = False
    config.setdefault('session_reset', {})['mode'] = 'none'
    # DMs (including contacts without a character binding) use the same
    # existing compressor. Keep separate gateway DM keys for each contact.
    config.setdefault('compression', {}).update(
        enabled=True, threshold=0.75, threshold_tokens=256000, tail_tokens=32000,
        in_place=True, abort_on_summary_failure=True, micro_compact=False,
        min_tail_user_messages=2,
    )
    auto = config['plugins']['entries']['grantley']['settings'].setdefault('autocompact', {})
    auto.update(enabled=True, threshold=0.75, threshold_tokens=256000, tail_tokens=32000)
    return config
