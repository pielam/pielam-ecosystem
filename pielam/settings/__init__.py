import os

env = os.getenv("DJANGO_ENV", "local")

if env == "prod":
    from .prod import *
elif env == "dev":
    from .dev import *
else:
    from .local import *
