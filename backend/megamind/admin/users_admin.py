"""
Django Admin configuration for User model
File: admin/users_admin.py
"""
from datetime import timedelta

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from django.utils import timezone
from django.contrib import messages

from django.utils.safestring import mark_safe
from apps.customer.models import User


# ========== CUSTOM FILTERS ==========

class VerificationStatusFilter(admin.SimpleListFilter):
    """Custom filter for verification status"""
    title = 'verification status'
    parameter_name = 'verification'
    
    def lookups(self, request, model_admin):
        return (
            ('verified', 'Verified'),
            ('unverified', 'Unverified'),
            ('pending', 'Pending (has token)'),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'verified':
            return queryset.filter(is_verified=True)
        if self.value() == 'unverified':
            return queryset.filter(is_verified=False)
        if self.value() == 'pending':
            return queryset.filter(
                is_verified=False,
                verification_token__isnull=False
            )


class AccountStatusFilter(admin.SimpleListFilter):
    """Custom filter for account status"""
    title = 'account status'
    parameter_name = 'status'
    
    def lookups(self, request, model_admin):
        return (
            ('active', 'Active'),
            ('inactive', 'Inactive'),
            ('locked', 'Locked'),
            ('deleted', 'Soft Deleted'),
        )
    
    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'active':
            return queryset.filter(is_active=True, is_deleted=False)
        if self.value() == 'inactive':
            return queryset.filter(is_active=False, is_deleted=False)
        if self.value() == 'locked':
            return queryset.filter(locked_until__gt=now)
        if self.value() == 'deleted':
            return queryset.filter(is_deleted=True)


class InactivityFilter(admin.SimpleListFilter):
    """Custom filter for user inactivity"""
    title = 'inactivity period'
    parameter_name = 'inactivity'
    
    def lookups(self, request, model_admin):
        return (
            ('7days', 'Inactive 7+ days'),
            ('30days', 'Inactive 30+ days'),
            ('90days', 'Inactive 90+ days'),
        )
    
    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == '7days':
            return queryset.filter(last_activity_at__lt=now - timedelta(days=7))
        if self.value() == '30days':
            return queryset.filter(last_activity_at__lt=now - timedelta(days=30))
        if self.value() == '90days':
            return queryset.filter(last_activity_at__lt=now - timedelta(days=90))


# ========== MAIN ADMIN CLASS ==========

# @admin.register(User)
# class UserAdmin(BaseUserAdmin):
#     """
#     Custom admin interface for User model with enhanced functionality
#     """
    
#     # List display configuration
#     list_display = [
#         'email_or_phone',
#         'identity_type_badge',
#         'role_badge',
#         'status_badges',
#         'verification_status',
#         'last_login_display',
#         'created_at_display',
#         'actions_column',
#     ]
    
#     list_filter = [
#         'identity_type',
#         'role',
#         VerificationStatusFilter,
#         AccountStatusFilter,
#         InactivityFilter,
#         'is_staff',
#         'created_at',
#     ]
    
#     search_fields = [
#         'email_or_phone',
#         'user_uuid',
#     ]
    
#     ordering = ['-created_at']
#     list_per_page = 50
    
#     readonly_fields = [
#         'user_uuid',
#         'identity_type',
#         'created_at',
#         'updated_at',
#         'last_login_at',
#         'last_activity_at',
#         'last_password_change',
#         'deleted_at',
#         'security_info_display',
#     ]
    
#     # Fieldsets for change form
#     fieldsets = (
#         ('🔑 Authentication', {
#             'fields': (
#                 'user_uuid',
#                 'email_or_phone',
#                 'identity_type',
#                 'password',
#             )
#         }),
        
#         ('👤 Personal Information', {
#             'fields': (
#                 'user_timezone',
#             ),
#             'classes': ('collapse',),
#         }),
        
#         ('✓ Verification & Security', {
#             'fields': (
#                 'is_verified',
#                 'verification_token',
#                 'verification_token_expires',
#                 'security_info_display',
#             ),
#             'classes': ('collapse',),
#         }),
        
#         ('🔒 Password Security', {
#             'fields': (
#                 'last_password_change',
#                 'password_expires_at',
#                 'password_reset_token',
#                 'password_reset_expires',
#             ),
#             'classes': ('collapse',),
#         }),
        
#         ('🚫 Account Locking', {
#             'fields': (
#                 'failed_login_attempts',
#                 'locked_until',
#                 'lock_reason',
#             ),
#             'classes': ('collapse',),
#         }),
        
#         ('🗑️ Soft Delete', {
#             'fields': (
#                 'is_deleted',
#                 'deleted_at',
#                 'deletion_reason',
#             ),
#             'classes': ('collapse',),
#         }),
        
#         ('⚙️ Permissions & Access', {
#             'fields': (
#                 'role',
#                 'is_active',
#                 'is_staff',
#                 'is_superuser',
#             )
#         }),
        
#         ('👥 Groups & Permissions', {
#             'fields': (
#                 'groups',
#                 'user_permissions',
#             ),
#             'classes': ('collapse',),
#         }),
        
#         ('📊 Activity Tracking', {
#             'fields': (
#                 'last_login_at',
#                 'last_activity_at',
#                 'created_at',
#                 'updated_at',
#             ),
#             'classes': ('collapse',),
#         }),
#     )
    
#     # Fieldsets for add form
#     add_fieldsets = (
#         ('🔑 Authentication', {
#             'fields': (
#                 'email_or_phone',
#                 'password1',
#                 'password2',
#             )
#         }),
        
#         ('⚙️ Permissions', {
#             'fields': (
#                 'role',
#                 'is_active',
#                 'is_staff',
#                 'is_verified',
#             )
#         }),
#     )
    
#     filter_horizontal = ('groups', 'user_permissions')
#     date_hierarchy = 'created_at'
    
#     actions = [
#         'verify_users',
#         'unverify_users',
#         'activate_users',
#         'deactivate_users',
#         'unlock_users',
#         'lock_users',
#         'soft_delete_users',
#         'restore_users',
#         'make_admin',
#         'remove_admin',
#         'reset_failed_attempts',
#     ]
    
#     # ========== DISPLAY METHODS ==========
    
#     @admin.display(description='Identity Type')
#     def identity_type_badge(self, obj):
#         colors = {'email': '#4CAF50', 'phone': '#2196F3'}
#         color = colors.get(obj.identity_type, '#757575')
#         return format_html(
#             '<span style="background-color: {}; color: white; padding: 3px 10px; '
#             'border-radius: 3px; font-size: 11px; font-weight: bold;">{}</span>',
#             color, obj.identity_type.upper()
#         )
    
#     @admin.display(description='Role')
#     def role_badge(self, obj):
#         colors = {'admin': '#f44336', 'user': '#9E9E9E'}
#         color = colors.get(obj.role, '#757575')
#         icon = '👑' if obj.role == 'admin' else '👤'
#         return format_html(
#             '{} <span style="background-color: {}; color: white; padding: 3px 10px; '
#             'border-radius: 3px; font-size: 11px; font-weight: bold;">{}</span>',
#             icon, color, obj.role.upper()
#         )
    

#     @admin.display(description='Status')
#     def status_badges(self, obj):
#         badges = []

#         if obj.is_active:
#             badges.append(
#                 '<span style="background:#4CAF50;color:white;padding:2px 8px;border-radius:3px;font-size:10px;">ACTIVE</span>'
#             )
#         else:
#             badges.append(
#                 '<span style="background:#f44336;color:white;padding:2px 8px;border-radius:3px;font-size:10px;">INACTIVE</span>'
#             )

#         if obj.is_locked():
#             badges.append(
#                 '<span style="background:#FF9800;color:white;padding:2px 8px;border-radius:3px;font-size:10px;">🔒 LOCKED</span>'
#             )

#         if obj.is_deleted:
#             badges.append(
#                 '<span style="background:#9E9E9E;color:white;padding:2px 8px;border-radius:3px;font-size:10px;">🗑️ DELETED</span>'
#             )

#         if obj.is_staff:
#             badges.append(
#                 '<span style="background:#673AB7;color:white;padding:2px 8px;border-radius:3px;font-size:10px;">STAFF</span>'
#             )

#         return mark_safe(' '.join(badges))

    
#     @admin.display(description='Verified', boolean=True)
#     def verification_status(self, obj):
#         return obj.is_verified
    
#     from django.utils.safestring import mark_safe

#     @admin.display(description='Last Login')
#     def last_login_display(self, obj):
#         if not obj.last_login_at:
#             return mark_safe('<span style="color:#999;">Never</span>')

#         delta = timezone.now() - obj.last_login_at
#         color = '#f44336' if delta.days > 30 else '#FF9800' if delta.days > 7 else '#4CAF50'
#         return format_html(
#             '<span style="color:{};">{}</span>',
#             color,
#             obj.last_login_at.strftime('%Y-%m-%d %H:%M')
#         )

    
#     @admin.display(description='Created')
#     def created_at_display(self, obj):
#         return obj.created_at.strftime('%Y-%m-%d')
    
#     @admin.display(description='Actions')
#     def actions_column(self, obj):
#         actions = []

#         if obj.is_locked():
#             actions.append('<a href="#" style="color:#4CAF50;">🔓 Unlock</a>')

#         if not obj.is_verified:
#             actions.append('<a href="#" style="color:#2196F3;">✓ Verify</a>')

#         if obj.is_deleted:
#             actions.append('<a href="#" style="color:#FF9800;">♻️ Restore</a>')

#         return mark_safe(' | '.join(actions)) if actions else '-'

    
#     @admin.display(description='Security Information')
#     def security_info_display(self, obj):
#         if not obj.pk:
#             return '-'

#         summary = obj.get_security_summary()

#         html = f"""
#         <div style="background:#f5f5f5;padding:15px;border-radius:5px;">
#             <h3>Security Summary</h3>
#             <table style="width:100%;">
#                 <tr><td><b>Verified</b></td><td>{'Yes' if summary['is_verified'] else 'No'}</td></tr>
#                 <tr><td><b>Locked</b></td><td>{'Yes' if summary['is_locked'] else 'No'}</td></tr>
#                 <tr><td><b>Failed Attempts</b></td><td>{summary['failed_attempts']}</td></tr>
#             </table>
#         </div>
#         """

#         return mark_safe(html)

    
#     # ========== ADMIN ACTIONS ==========
    
#     @admin.action(description='✓ Verify selected users')
#     def verify_users(self, request, queryset):
#         count = 0
#         for user in queryset:
#             if not user.is_verified:
#                 user.is_verified = True
#                 user.verification_token = None
#                 user.verification_token_expires = None
#                 user.save(update_fields=['is_verified', 'verification_token', 'verification_token_expires'])
#                 count += 1
#         self.message_user(request, f'Successfully verified {count} user(s).', messages.SUCCESS)
    
#     @admin.action(description='✗ Unverify selected users')
#     def unverify_users(self, request, queryset):
#         count = queryset.update(is_verified=False)
#         self.message_user(request, f'Successfully unverified {count} user(s).', messages.WARNING)
    
#     @admin.action(description='✓ Activate selected users')
#     def activate_users(self, request, queryset):
#         count = queryset.update(is_active=True)
#         self.message_user(request, f'Successfully activated {count} user(s).', messages.SUCCESS)
    
#     @admin.action(description='✗ Deactivate selected users')
#     def deactivate_users(self, request, queryset):
#         count = queryset.update(is_active=False)
#         self.message_user(request, f'Successfully deactivated {count} user(s).', messages.WARNING)
    
#     @admin.action(description='🔓 Unlock selected users')
#     def unlock_users(self, request, queryset):
#         count = 0
#         for user in queryset:
#             if user.is_locked():
#                 user.unlock_account()
#                 count += 1
#         self.message_user(request, f'Successfully unlocked {count} user(s).', messages.SUCCESS)
    
#     @admin.action(description='🔒 Lock selected users (30 min)')
#     def lock_users(self, request, queryset):
#         count = 0
#         for user in queryset:
#             if not user.is_locked():
#                 user.lock_account(duration_minutes=30, reason="Locked by admin")
#                 count += 1
#         self.message_user(request, f'Successfully locked {count} user(s).', messages.WARNING)
    
#     @admin.action(description='🗑️ Soft delete selected users')
#     def soft_delete_users(self, request, queryset):
#         count = 0
#         for user in queryset:
#             if not user.is_deleted:
#                 user.soft_delete(reason="Deleted by admin")
#                 count += 1
#         self.message_user(request, f'Successfully soft deleted {count} user(s).', messages.WARNING)
    
#     @admin.action(description='♻️ Restore deleted users')
#     def restore_users(self, request, queryset):
#         count = 0
#         for user in queryset:
#             if user.is_deleted:
#                 user.restore()
#                 count += 1
#         self.message_user(request, f'Successfully restored {count} user(s).', messages.SUCCESS)
    
#     @admin.action(description='👑 Make admin')
#     def make_admin(self, request, queryset):
#         count = 0
#         for user in queryset:
#             if not user.is_admin:
#                 user.make_admin()
#                 count += 1
#         self.message_user(request, f'Successfully promoted {count} user(s) to admin.', messages.SUCCESS)
    
#     @admin.action(description='👤 Remove admin')
#     def remove_admin(self, request, queryset):
#         count = 0
#         for user in queryset:
#             if user.is_admin:
#                 user.remove_admin()
#                 count += 1
#         self.message_user(request, f'Successfully demoted {count} admin(s) to regular user.', messages.SUCCESS)
    
#     @admin.action(description='🔄 Reset failed login attempts')
#     def reset_failed_attempts(self, request, queryset):
#         count = queryset.update(failed_login_attempts=0)
#         self.message_user(request, f'Successfully reset failed login attempts for {count} user(s).', messages.SUCCESS)
    
#     # ========== CUSTOM METHODS ==========
    
#     def get_queryset(self, request):
#         return super().get_queryset(request).select_related()
    
#     def save_model(self, request, obj, form, change):
#         if not change and 'password1' in form.cleaned_data:
#             obj.set_password(form.cleaned_data['password1'])
#             obj.generate_verification_token()
#         super().save_model(request, obj, form, change)
#         action = 'updated' if change else 'created'
#         self.message_user(request, f'User {obj.email_or_phone} was successfully {action}.', messages.SUCCESS)
    
#     def has_delete_permission(self, request, obj=None):
#         return False
    
#     def get_readonly_fields(self, request, obj=None):
#         readonly = list(self.readonly_fields)
#         if obj:
#             readonly.extend(['email_or_phone'])
#         return readonly
    
#     def get_fieldsets(self, request, obj=None):
#         if not obj:
#             return self.add_fieldsets
#         return super().get_fieldsets(request, obj)