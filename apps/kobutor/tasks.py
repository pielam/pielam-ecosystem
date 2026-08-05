from django.conf import settings

if getattr(settings, "CELERY_ENABLED", False):
    from celery import shared_task
else:
    shared_task = None


def _log_user_visit(*args, **kwargs):
    # your actual logic here
    pass


if shared_task:
    @shared_task
    def log_user_visit(*args, **kwargs):
        _log_user_visit(*args, **kwargs)
else:
    def log_user_visit(*args, **kwargs):
        _log_user_visit(*args, **kwargs)
