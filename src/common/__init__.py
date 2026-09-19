"""Provider-agnostic core: the shared CSV schema and dialogue segmentation logic.

Any provider (Telegram, Discord, Slack, ...) that exports messages into the
CSV format described in README.md can reuse `ExportedMessageRecord` and the
segmentation/dossier tooling in this package without any changes.
"""

from .models import ExportedMessageRecord
from .segmenter import DialogueClusterer, ParsedMessage, load_exported_csv

__all__ = [
    "ExportedMessageRecord",
    "DialogueClusterer",
    "ParsedMessage",
    "load_exported_csv",
]
