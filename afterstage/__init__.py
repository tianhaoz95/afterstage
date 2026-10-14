"""afterstage: extend a movie clip with on-device generative AI."""

import os

# On-device means no phone-home either: disable library telemetry before anything imports them.
for _key, _value in {
    "GRADIO_ANALYTICS_ENABLED": "False",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    "TOKENIZERS_PARALLELISM": "false",
}.items():
    os.environ.setdefault(_key, _value)

__version__ = "0.1.0"
