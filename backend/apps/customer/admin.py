from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django import forms

from apps.customer.models.account import User


class CustomUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        # Include email_or_phone and email since you have both
        fields = ('email_or_phone', 'email', 'role')


class CustomUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = ('email_or_phone', 'email', 'role', 'is_active', 'is_staff')


class UserAdmin(BaseUserAdmin):
    form = CustomUserChangeForm
    add_form = CustomUserCreationForm

    list_display = ('email_or_phone', 'email', 'role', 'is_staff', 'is_active')
    list_filter = ('role', 'is_staff', 'is_active')
    ordering = ('email_or_phone',)
    search_fields = ('email_or_phone', 'email')

    fieldsets = (
        (None, {'fields': ('email_or_phone', 'email', 'password')}),
        (_('Personal info'), {'fields': ('role',)}),
        (_('Permissions'), {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        (_('Important dates'), {'fields': ('last_login', 'date_joined')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email_or_phone', 'email', 'role', 'password1', 'password2', 'is_staff', 'is_active'),
        }),
    )


admin.site.register(User, UserAdmin)


from django.contrib import admin
from apps.customer.models.profile_info import ProfileInfo


@admin.register(ProfileInfo)
class ProfileInfoAdmin(admin.ModelAdmin):

    # Columns shown in admin list view
    list_display = (
        "user",
        "profile_name",
        "is_profile_public",
        "is_profile_verified",
        "is_profile_featured",
        "is_profile_suspended",
        "is_profile_archived",
        "profile_creation_time",
    )

    # Filters on right sidebar
    list_filter = (
        "is_profile_public",
        "is_profile_verified",
        "is_profile_featured",
        "is_profile_suspended",
        "is_profile_archived",
        "profile_gender",
        "profile_language",
        "profile_creation_time",
    )

    # Search bar functionality
    search_fields = (
        "user__email_or_phone",
        "profile_name",
        "profile_gender",
        "profile_language",
        "profile_address",
    )

    # Field grouping inside edit page
    fieldsets = (
        ("User", {
            "fields": ("user",)
        }),

        ("Profile Identity", {
            "fields": ("profile_name",)
        }),

        ("Profile Details", {
            "fields": (
                "profile_address",
                "profile_gender",
                "profile_language",
                "profile_dob",
            )
        }),

        ("Photos", {
            "fields": (
                "profile_photo",
                "profile_cover_photo",
            )
        }),

        ("Status", {
            "fields": (
                "is_profile_public",
                "is_profile_verified",
                "is_profile_featured",
                "is_profile_suspended",
                "is_profile_archived",
            )
        }),

        ("Followers & Following", {
            "fields": ("followers", "following"),
        }),

        ("Timestamps", {
            "fields": (
                "profile_creation_time",
                "profile_updated_time",
            ),
        }),
    )

    # Make timestamps read-only
    readonly_fields = (
        "profile_creation_time",
        "profile_updated_time",
    )

    # Allow selection of followers/following using admin multiselect UI
    filter_horizontal = ("followers", "following")
