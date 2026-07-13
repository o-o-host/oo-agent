"""Public collector API.

Built-in collectors and custom drop-in plugins share the same contract:
subclass :class:`Collector` and implement :meth:`Collector.collect`.

Custom plugins are plain ``.py`` files placed into the plugins directory
(``plugins_dir`` in ``agent.ini``). Metric keys of custom plugins are
automatically prefixed with ``custom.<plugin name>.``.
"""

from __future__ import annotations

from typing import Any


class Collector:
    """Base class for all collectors.

    Class attributes:
        name: unique collector name; also the metric prefix for plugins.
        interval: seconds between runs; ``None`` means the agent default.
        inventory: True for collectors that produce inventory snapshots
            and therefore run at ``inventory_interval`` cadence.
    """

    name: str = "base"
    interval: int | None = None
    inventory: bool = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        #: Options from the ``[collector:<name>]`` config section.
        self.config: dict[str, Any] = config or {}

    def available(self) -> bool:
        """Whether this collector can run on this host.

        Return ``False`` when the underlying hardware, kernel interface
        or optional library is missing. Called once at startup; a
        ``False`` result disables the collector without an error.
        """
        return True

    def collect(self) -> dict[str, Any]:
        """Run one collection pass and return a payload dict.

        Supported keys (all optional):
            metrics:   flat mapping of metric key -> int/float.
            sensors:   list of normalized sensor readings, each a dict:
                       ``{"id", "name", "kind", "value", "unit",
                          "max", "crit", "threshold_source"}``
                       (``max``/``crit``/``threshold_source`` only when
                       the hardware provides passport thresholds).
            inventory: structured snapshot data (dict), stored server-side
                       as-is; sent at inventory cadence, not every push.
        """
        raise NotImplementedError
