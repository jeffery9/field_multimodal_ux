# -*- coding: utf-8 -*-
from . import mmux_service
# The seven files below all `_inherit = "mmux.service"`, so they merge into a
# single model at runtime.  Listed in dependency order (bottom first).
from . import mmux_service_infra
from . import mmux_service_skill
from . import mmux_service_capability
from . import mmux_service_scenario
from . import mmux_service_loop
from . import mmux_service_flow
from . import mmux_service_plan
from . import mmux_interaction_log
from . import mmux_scenario_config
from . import mmux_settings
from . import mmux_capability_view
from . import mmux_capability_toggle
from . import mmux_flow_override
from . import res_users
