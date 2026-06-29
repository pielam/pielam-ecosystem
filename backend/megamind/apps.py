# megamind/apps.py

from django.apps import AppConfig

class MegamindConfig(AppConfig):
    name = 'megamind'

    def ready(self):
        import megamind.signals  # noqa