"""
Enterprise-level Django Admin for User model
- Advanced filtering and search
- Bulk actions
- Security monitoring
- Custom actions and views
- Export capabilities
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth import get_user_model
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.urls import path, reverse
from django.shortcuts import render
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q
from django.http import HttpResponse
import csv
import json
from datetime import timedelta
from django.db.models import Count, Q



User = get_user_model()


# ============================================================================
# CUSTOM FILTERS
# ============================================================================

class VerificationStatusFilter(admin.SimpleListFilter):
    title = 'Verification Status'
    parameter_name = 'verification'

    def lookups(self, request, model_admin):
        return [
            ('verified', 'Verified'),
            ('unverified', 'Unverified'),
            ('email_verified', 'Email Verified'),
            ('phone_verified', 'Phone Verified'),
        ]

    def queryset(self, request, queryset):
        if self.value() == 'verified':
            return queryset.filter(
                Q(identity_type='email', is_email_verified=True) |
                Q(identity_type='phone', is_phone_verified=True)
            )
        elif self.value() == 'unverified':
            return queryset.filter(
                Q(identity_type='email', is_email_verified=False) |
                Q(identity_type='phone', is_phone_verified=False)
            )
        elif self.value() == 'email_verified':
            return queryset.filter(is_email_verified=True)
        elif self.value() == 'phone_verified':
            return queryset.filter(is_phone_verified=True)
        return queryset


class AccountStatusFilter(admin.SimpleListFilter):
    title = 'Account Status'
    parameter_name = 'status'

    def lookups(self, request, model_admin):
        return [
            ('active', 'Active'),
            ('inactive', 'Inactive'),
            ('locked', 'Locked'),
            ('deleted', 'Deleted'),
        ]

    def queryset(self, request, queryset):
        if self.value() == 'active':
            return queryset.filter(is_active=True, is_deleted=False)
        elif self.value() == 'inactive':
            return queryset.filter(is_active=False, is_deleted=False)
        elif self.value() == 'locked':
            return queryset.filter(locked_until__gt=timezone.now())
        elif self.value() == 'deleted':
            return queryset.filter(is_deleted=True)
        return queryset


class RegistrationDateFilter(admin.SimpleListFilter):
    title = 'Registration Date'
    parameter_name = 'reg_date'

    def lookups(self, request, model_admin):
        return [
            ('today', 'Today'),
            ('week', 'This Week'),
            ('month', 'This Month'),
            ('quarter', 'This Quarter'),
            ('year', 'This Year'),
        ]

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'today':
            return queryset.filter(created_at__date=now.date())
        elif self.value() == 'week':
            return queryset.filter(created_at__gte=now - timedelta(days=7))
        elif self.value() == 'month':
            return queryset.filter(created_at__gte=now - timedelta(days=30))
        elif self.value() == 'quarter':
            return queryset.filter(created_at__gte=now - timedelta(days=90))
        elif self.value() == 'year':
            return queryset.filter(created_at__gte=now - timedelta(days=365))
        return queryset


class LastActivityFilter(admin.SimpleListFilter):
    title = 'Last Activity'
    parameter_name = 'activity'

    def lookups(self, request, model_admin):
        return [
            ('24h', 'Last 24 Hours'),
            ('7d', 'Last 7 Days'),
            ('30d', 'Last 30 Days'),
            ('90d', 'Last 90 Days'),
            ('inactive', 'Inactive 90+ Days'),
        ]

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == '24h':
            return queryset.filter(last_activity_at__gte=now - timedelta(hours=24))
        elif self.value() == '7d':
            return queryset.filter(last_activity_at__gte=now - timedelta(days=7))
        elif self.value() == '30d':
            return queryset.filter(last_activity_at__gte=now - timedelta(days=30))
        elif self.value() == '90d':
            return queryset.filter(last_activity_at__gte=now - timedelta(days=90))
        elif self.value() == 'inactive':
            return queryset.filter(
                Q(last_activity_at__lt=now - timedelta(days=90)) |
                Q(last_activity_at__isnull=True)
            )
        return queryset


# ============================================================================
# MAIN USER ADMIN
# ============================================================================

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = [
        'email_or_phone_display',
        'identity_badge',
        'verification_badge',
        'role_badge',
        'status_badge',
        'failed_attempts_display',
        'created_display',
        'last_login_display',
        # 'action_buttons',  # ❌ Removed: non-functional
    ]

    list_display_links = ['email_or_phone_display']
    list_filter = [
        AccountStatusFilter,
        VerificationStatusFilter,
        'identity_type',
        'role',
        RegistrationDateFilter,
        LastActivityFilter,
        'is_staff',
        'is_superuser',
    ]
    search_fields = ['email_or_phone', 'email', 'phone', 'user_uuid']
    ordering = ['-created_at']
    list_per_page = 50

    fieldsets = (
        ('🔑 Identity', {
            'fields': ('user_uuid', 'email_or_phone', 'identity_type'),
            'classes': ('wide',),
        }),
        ('📧 Contact Information', {
            'fields': (
                ('email', 'is_email_verified', 'email_verified_at'),
                ('phone', 'is_phone_verified', 'phone_verified_at'),
            ),
            'classes': ('wide',),
        }),
        ('🔐 Security', {
            'fields': (
                'last_password_change',
                ('failed_login_attempts', 'locked_until', 'lock_reason'),
            ),
            'classes': ('wide',),
        }),
        ('👤 Permissions', {
            'fields': ('role', 'is_active', 'is_staff', 'is_superuser'),
            'classes': ('wide',),
        }),
        ('🔒 Encryption Keys', {
            'fields': ('encryption_key', 'jwt_public_key'),
            'classes': ('collapse',),
            'description': 'User-specific encryption and JWT signing keys'
        }),
        ('🗑️ Soft Delete', {
            'fields': ('is_deleted', 'deleted_at', 'deletion_reason'),
            'classes': ('collapse',),
        }),
        ('📊 Activity Tracking', {
            'fields': (
                'last_login_at',
                'last_activity_at',
                'created_at',
                'updated_at',
            ),
            'classes': ('collapse',),
        }),
        ('⚙️ Preferences', {
            'fields': ('user_timezone',),
            'classes': ('collapse',),
        }),
    )

    add_fieldsets = (
        ('Create New User', {
            'fields': (
                'email_or_phone',
                'password1',
                'password2',
                'role',
                'is_active',
                'is_staff',
            ),
            'classes': ('wide',),
        }),
    )

    readonly_fields = [
        'user_uuid',
        'identity_type',
        'encryption_key',
        'jwt_public_key',
        'created_at',
        'updated_at',
        'last_login_at',
        'last_activity_at',
        'last_password_change',
        'email_verified_at',
        'phone_verified_at',
        'deleted_at',
    ]

    actions = [
        'activate_users',
        'deactivate_users',
        'verify_users',
        'lock_users',
        'unlock_users',
        'soft_delete_users',
        'restore_users',
        'reset_failed_attempts',
        'export_csv',
        'export_json',
    ]

    # ========== DISPLAY METHODS ==========

    @admin.display(description='User', ordering='email_or_phone')
    def email_or_phone_display(self, obj):
        return format_html(
            '<div style="display: flex; align-items: center;">'
            '<strong>{}</strong>'
            '<button onclick="navigator.clipboard.writeText(\'{}\'); alert(\'Copied!\')" '
            'style="margin-left: 8px; cursor: pointer; padding: 2px 6px; font-size: 10px;">📋</button>'
            '</div>',
            obj.email_or_phone,
            obj.email_or_phone
        )

    @admin.display(description='Type')
    def identity_badge(self, obj):
        colors = {'email': '#3498db', 'phone': '#2ecc71'}
        icons = {'email': '📧', 'phone': '📱'}
        return format_html(
            '<span style="background: {}; color: white; padding: 3px 8px; '
            'border-radius: 3px; font-size: 11px;">{} {}</span>',
            colors.get(obj.identity_type, '#95a5a6'),
            icons.get(obj.identity_type, ''),
            obj.identity_type.upper()
        )

    @admin.display(description='Verification')
    def verification_badge(self, obj):
        if obj.is_email_verified and obj.is_phone_verified:
            color, text = '#28a745', '✔ EMAIL & PHONE VERIFIED'
        elif obj.is_email_verified:
            color, text = '#17a2b8', '✔ EMAIL VERIFIED'
        elif obj.is_phone_verified:
            color, text = '#20c997', '✔ PHONE VERIFIED'
        else:
            color, text = '#dc3545', '✘ UNVERIFIED'
        return format_html(
            '<span style="background-color: {}; color: white; padding: 4px 8px; '
            'border-radius: 4px; font-size: 0.8em; font-weight: 600;">{}</span>',
            color, text
        )

    @admin.display(description='Role')
    def role_badge(self, obj):
        colors = {'admin': '#e74c3c', 'user': '#3498db'}
        return format_html(
            '<span style="background: {}; color: white; padding: 3px 8px; '
            'border-radius: 3px; font-size: 11px; font-weight: bold;">{}</span>',
            colors.get(obj.role, '#95a5a6'),
            obj.role.upper()
        )

    @admin.display(description='Status')
    def status_badge(self, obj):
        if obj.is_deleted:
            color, text = '#dc3545', '🗑 DELETED'
        elif obj.locked_until and obj.locked_until > timezone.now():
            color, text = '#fd7e14', '🔒 LOCKED'
        elif not obj.is_active:
            color, text = '#ffc107', '⏸ INACTIVE'
        elif obj.is_superuser:
            color, text = '#6f42c1', '👑 SUPERUSER'
        elif obj.is_staff:
            color, text = '#20c997', '⚙ STAFF'
        else:
            color, text = '#28a745', '✓ ACTIVE'
        return format_html(
            '<span style="background-color: {}; color: white; padding: 4px 8px; '
            'border-radius: 4px; font-size: 0.8em; font-weight: 600;">{}</span>',
            color, text
        )

    @admin.display(description='Failed Logins')
    def failed_attempts_display(self, obj):
        color = '#e74c3c' if obj.failed_login_attempts >= 5 else \
                '#f39c12' if obj.failed_login_attempts >= 3 else '#95a5a6'
        return format_html('<span style="color: {}; font-weight: bold;">{}</span>', color, obj.failed_login_attempts)

    @admin.display(description='Created', ordering='created_at')
    def created_display(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.days == 0:
            return mark_safe('<span style="color: #27ae60;">Today</span>')
        elif delta.days == 1:
            return mark_safe('<span style="color: #f39c12;">Yesterday</span>')
        elif delta.days < 7:
            return format_html('<span>{} days ago</span>', delta.days)
        elif delta.days < 30:
            return format_html('<span>{} weeks ago</span>', delta.days // 7)
        else:
            return obj.created_at.strftime('%Y-%m-%d')

    @admin.display(description='Last Login')
    def last_login_display(self, obj):
        if not obj.last_login_at:
            return mark_safe('<span style="color: #95a5a6;">Never</span>')
        delta = timezone.now() - obj.last_login_at
        if delta.days == 0:
            return mark_safe('<span style="color: #27ae60;">Today</span>')
        elif delta.days < 7:
            return format_html('<span>{} days ago</span>', delta.days)
        else:
            return obj.last_login_at.strftime('%Y-%m-%d')

    # ========== BULK ACTIONS ==========

    @admin.action(description='✓ Activate selected users')
    def activate_users(self, request, queryset):
        count = queryset.update(is_active=True, updated_at=timezone.now())
        self.message_user(request, f'{count} user(s) activated.', messages.SUCCESS)

    @admin.action(description='✗ Deactivate selected users')
    def deactivate_users(self, request, queryset):
        count = queryset.update(is_active=False, updated_at=timezone.now())
        self.message_user(request, f'{count} user(s) deactivated.', messages.WARNING)

    @admin.action(description='✓ Mark as verified')
    def verify_users(self, request, queryset):
        for user in queryset:
            user.mark_verified()
        self.message_user(request, f'{queryset.count()} user(s) verified.', messages.SUCCESS)

    @admin.action(description='🔒 Lock selected users')
    def lock_users(self, request, queryset):
        for user in queryset:
            user.lock_account(duration_minutes=60, reason='Admin action')
        self.message_user(request, f'{queryset.count()} user(s) locked.', messages.WARNING)

    @admin.action(description='🔓 Unlock selected users')
    def unlock_users(self, request, queryset):
        for user in queryset:
            user.unlock_account()
        self.message_user(request, f'{queryset.count()} user(s) unlocked.', messages.SUCCESS)

    @admin.action(description='🗑️ Soft delete selected users')
    def soft_delete_users(self, request, queryset):
        for user in queryset:
            user.soft_delete(reason='Admin action')
        self.message_user(request, f'{queryset.count()} user(s) soft deleted.', messages.WARNING)

    @admin.action(description='♻️ Restore deleted users')
    def restore_users(self, request, queryset):
        restored = queryset.filter(is_deleted=True).update(
            is_deleted=False, deleted_at=None, is_active=True, deletion_reason=None
        )
        self.message_user(request, f'{restored} user(s) restored.', messages.SUCCESS)

    @admin.action(description='🔄 Reset failed login attempts')
    def reset_failed_attempts(self, request, queryset):
        count = queryset.update(
            failed_login_attempts=0,
            locked_until=None,
            lock_reason=None,
            updated_at=timezone.now()
        )
        self.message_user(request, f'Reset for {count} user(s).', messages.SUCCESS)

    @admin.action(description='📥 Export to CSV')
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="users_export.csv"'
        writer = csv.writer(response)
        writer.writerow(['UUID', 'Email/Phone', 'Type', 'Role', 'Active', 'Verified', 'Created', 'Last Login'])
        for user in queryset:
            verified = user.is_email_verified if user.identity_type == 'email' else user.is_phone_verified
            writer.writerow([
                str(user.user_uuid),
                user.email_or_phone,
                user.identity_type,
                user.role,
                user.is_active,
                verified,
                user.created_at.strftime('%Y-%m-%d %H:%M'),
                user.last_login_at.strftime('%Y-%m-%d %H:%M') if user.last_login_at else 'Never',
            ])
        return response

    @admin.action(description='📥 Export to JSON')
    def export_json(self, request, queryset):
        users_data = [{
            'uuid': str(user.user_uuid),
            'email_or_phone': user.email_or_phone,
            'identity_type': user.identity_type,
            'role': user.role,
            'is_active': user.is_active,
            'is_verified': user.is_email_verified if user.identity_type == 'email' else user.is_phone_verified,
            'created_at': user.created_at.isoformat(),
            'last_login_at': user.last_login_at.isoformat() if user.last_login_at else None,
        } for user in queryset]
        response = HttpResponse(json.dumps(users_data, indent=2), content_type='application/json')
        response['Content-Disposition'] = 'attachment; filename="users_export.json"'
        return response

    # ========== QUERYSET ==========

    def get_queryset(self, request):
        return super().get_queryset(request)  # Shows all, including deleted
    
    # Add these imports at the top if not already present

    # Inside UserAdmin class:

    def get_urls(self):
        """Add custom admin URLs"""
        urls = super().get_urls()
        custom_urls = [
            path('statistics/', self.admin_site.admin_view(self.statistics_view), name='user_statistics'),
        ]
        return custom_urls + urls

    def statistics_view(self, request):
        """Display user statistics dashboard"""
        total = User.objects.count()
        active = User.objects.filter(is_active=True, is_deleted=False).count()
        inactive = User.objects.filter(is_active=False, is_deleted=False).count()
        deleted = User.objects.filter(is_deleted=True).count()
        
        verified = User.objects.filter(
            Q(identity_type='email', is_email_verified=True) |
            Q(identity_type='phone', is_phone_verified=True)
        ).count()
        
        locked = User.objects.filter(locked_until__gt=timezone.now()).count()
        
        recent_30d = User.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=30)
        ).count()
        
        by_role = list(User.objects.values('role').annotate(count=Count('role')))
        by_identity = list(User.objects.values('identity_type').annotate(count=Count('identity_type')))

        stats = {
            'total': total,
            'active': active,
            'inactive': inactive,
            'deleted': deleted,
            'verified': verified,
            'locked': locked,
            'recent_30d': recent_30d,
            'by_role': by_role,
            'by_identity': by_identity,
        }
        
        context = {
            **self.admin_site.each_context(request),
            'title': 'User Statistics Dashboard',
            'stats': stats,
            'opts': self.model._meta,
        }
        
        return render(request, 'admin/user_statistics.html', context)

    def changelist_view(self, request, extra_context=None):
        """Add statistics button to changelist"""
        extra_context = extra_context or {}
        extra_context['show_statistics'] = True
        return super().changelist_view(request, extra_context=extra_context)