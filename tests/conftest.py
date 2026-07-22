"""Small dependency stubs keep pure unit tests independent of cloud services."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace


if "logfire" not in sys.modules:
    sys.modules["logfire"] = SimpleNamespace(
        span=lambda *args, **kwargs: nullcontext(),
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
