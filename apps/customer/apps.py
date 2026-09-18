from django.apps import AppConfig


class CustomerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.customer"

    def ready(self):
        import apps.customer.signals
        import apps.customer.models.account_log
        import apps.customer.models.contact_info_log
        import apps.customer.models.location_info_log
        import apps.customer.models.profile_info_log
        import apps.customer.models.social_info_log
