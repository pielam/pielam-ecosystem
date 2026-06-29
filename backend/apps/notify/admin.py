from django.contrib import admin

# Register your models here.
from django.contrib import admin
from apps.notify.models.welcome_notify import WelcomeNotification

@admin.register(WelcomeNotification)
class WelcomeNotificationAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "title",
        "is_shown",
        "created_at",
    )
    list_filter = ("is_shown", "created_at")
    search_fields = (
        "user__email_or_phone",
        "title",
        "message",
    )
    readonly_fields = ("created_at",)

    # Optional: show user email/phone instead of ID inside admin object page
    def get_user_display(self, obj):
        return obj.user.email_or_phone
    get_user_display.short_description = "User"
