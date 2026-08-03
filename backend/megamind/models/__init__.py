# megamind/models/__init__.py

"""
Re-exports every model in this package so callers can do
`from megamind.models import DiscoveryVisitLog` instead of reaching
into the submodule directly.

Each submodule is imported twice on purpose:
  1. `from . import <module> as _<module>` — gives us a handle on the
     module object so we can read its `__all__` (if it defines one)
     without guessing class names here.
  2. `from .<module> import *` — actually binds the public names into
     this package's namespace.

If a submodule doesn't define `__all__`, `import *` still pulls in
its public (non-underscore) names, it's just not tracked in this
package's own `__all__` list below — add an `__all__` to that
submodule to fix that.
"""

from __future__ import annotations


from . import recovery as _recovery
from . import keys as _keys
from . import visit_log as _visit_log
from . import connected_service as _connected_service


from .recovery import *  # noqa: F401,F403
from .keys import *  # noqa: F401,F403
from .visit_log import *  # noqa: F401,F403
from .connected_service import ConnectedService  # noqa: F401

# ``profile_info`` and ``engine_users`` are fully commented out in this
# package (superseded by ``apps.customer``); importing them would bind
# nothing, so they are intentionally not re-exported here.

__all__ = [
    *getattr(_recovery, '__all__', []),
    *getattr(_keys, '__all__', []),
    *getattr(_visit_log, '__all__', []),
    *getattr(_connected_service, '__all__', ['ConnectedService']),
]