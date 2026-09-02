"""``LightspeedAgent``: Harbor's external ``BaseAgent`` for the Lightspeed agent system.

The agent loop runs in hosted Lightspeed, not in the task container. This
class runs in Harbor's orchestrator process and uses the Harbor environment
only to upload and start ``lightspeed-envd`` before registration. Everything
after that goes through the Lightspeed API and the registered environment.

Job configuration:

.. code-block:: yaml

    agents:
      - import_path: lightspeed_harbor.agent:LightspeedAgent
        model_name: openai/<immutable-model-id>
        kwargs:
          lightspeed_provider_id: <provider-id>
          profile_id: harbor-terminal
          reasoning_effort: <effort>
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from lightspeed_harbor import __version__
from lightspeed_harbor.config import AgentSettings, HostSettings

AGENT_NAME = "lightspeed"


class LightspeedAgent(BaseAgent):
    # Raw Lightspeed events are exported until a faithful ATIF mapping exists.
    SUPPORTS_ATIF = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        lightspeed_provider_id: str | None = None,
        profile_id: str | None = None,
        reasoning_effort: str | None = None,
        host_settings: HostSettings | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        # Validate at construction so a bad configuration stops the job before
        # Harbor builds any sandbox. Harbor's ``agents[].env`` (``extra_env``)
        # takes precedence over the process environment, matching BaseAgent.
        self.settings = AgentSettings.resolve(
            model_name=model_name,
            lightspeed_provider_id=lightspeed_provider_id,
            profile_id=profile_id,
            reasoning_effort=reasoning_effort,
        )
        self.host = host_settings or HostSettings.from_env({**os.environ, **self._extra_env})

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def version(self) -> str | None:
        return __version__

    async def setup(self, environment: BaseEnvironment) -> None:
        """Upload and verify ``envd`` in the sandbox. Does not start it.

        Planned steps: select the pinned artifact for the sandbox platform,
        verify its checksum on the host, upload it, mark it executable, check
        ``envd --version`` as ``environment.default_user``, and create the
        agent log directory under ``/logs``.
        """
        raise NotImplementedError(
            "LightspeedAgent.setup is not implemented yet; see docs/next-steps.md, slice 2"
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Perform exactly one Harbor trial through hosted Lightspeed.

        Planned steps: write the registration key file, start ``envd``, wait for
        the receipt and delete the key file, start a session, activate the
        receipt's environment, start one run with ``instruction`` unchanged,
        wait for a terminal status, project usage into ``context``, export
        artifacts, and clean up in ``finally`` without touching the sandbox.
        """
        raise NotImplementedError(
            "LightspeedAgent.run is not implemented yet; see docs/next-steps.md, slice 2"
        )
