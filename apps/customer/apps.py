from django.apps import AppConfig


class CustomerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.customer'
    label = 'customer'  # Add this line explicitly

    def ready(self):
        import apps.customer.signals
        import apps.customer.models.account_log  # wires up AccountLog's pre/post_save signals