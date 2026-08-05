# apps/customer/admin.py

"""
Django Admin Configuration for User Model
------------------------------------------
Features:
- Custom list display with role badges
- Advanced filtering and search
- Security action buttons
- Verification management
- Audit trail display
- GDPR compliance actions
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.db.models import Q
from django.contrib import messages
from django import forms

from apps.customer.models.account import User


# ====================================================================
# CUSTOM FORMS
# ====================================================================

class UserCreationForm(forms.ModelForm):
    """Form for creating new users in admin"""
    password1 = forms.CharField(
        label='Password',
        widget=forms.PasswordInput,
        required=False,
        help_text='Leave blank for OAuth users or to set password later'
    )
    password2 = forms.CharField(
        label='Password confirmation',
        widget=forms.PasswordInput,
        required=False,
        help_text='Enter the same password as before, for verification.'
    )

    class Meta:
        model = User
        fields = ('email_or_phone', 'email', 'phone', 'role')

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("Passwords don't match")
        return password2

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password1")
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        if commit:
            user.save()
        return user


class UserChangeForm(forms.ModelForm):
    """Form for updating users in admin"""
    password = ReadOnlyPasswordHashField(
        label=_("Password"),
        help_text=_(
            "Raw passwords are not stored, so there is no way to see this "
            "user's password, but you can change the password using "
            "<a href=\"../password/\">this form</a>."
        ),
    )

    class Meta:
        model = User
        fields = '__all__'


# ====================================================================
# CUSTOM FILTERS
# ====================================================================

class VerificationStatusFilter(admin.SimpleListFilter):
    """Filter users by verification status"""
    title = _('Verification Status')
    parameter_name = 'verification'
    
    def lookups(self, request, model_admin):
        return (
            ('verified', _('Verified (Email or Phone)')),
            ('fully_verified', _('Fully Verified (Both)')),
            ('unverified', _('Unverified')),
            ('email_verified', _('Email Verified')),
            ('phone_verified', _('Phone Verified')),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'verified':
            return queryset.filter(Q(email_verified=True) | Q(phone_verified=True))
        elif self.value() == 'fully_verified':
            return queryset.filter(email_verified=True, phone_verified=True)
        elif self.value() == 'unverified':
            return queryset.filter(email_verified=False, phone_verified=False)
        elif self.value() == 'email_verified':
            return queryset.filter(email_verified=True)
        elif self.value() == 'phone_verified':
            return queryset.filter(phone_verified=True)
        return queryset


class AccountSecurityFilter(admin.SimpleListFilter):
    """Filter users by security status"""
    title = _('Security Status')
    parameter_name = 'security'
    
    def lookups(self, request, model_admin):
        return (
            ('locked', _('Locked')),
            ('mfa_enabled', _('MFA Enabled')),
            ('failed_attempts', _('Failed Login Attempts')),
            ('password_rotation_needed', _('Password Rotation Needed')),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'locked':
            return queryset.filter(
                locked_until__gt=timezone.now()
            )
        elif self.value() == 'mfa_enabled':
            return queryset.filter(mfa_enabled=True)
        elif self.value() == 'failed_attempts':
            return queryset.filter(failed_login_attempts__gt=0)
        elif self.value() == 'password_rotation_needed':
            return queryset.filter(require_password_change=True)
        return queryset


class SoftDeletedFilter(admin.SimpleListFilter):
    """Filter to show/hide soft-deleted users"""
    title = _('Deletion Status')
    parameter_name = 'deleted'
    
    def lookups(self, request, model_admin):
        return (
            ('active', _('Active (Not Deleted)')),
            ('deleted', _('Soft Deleted')),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'active':
            return queryset.filter(deleted_at__isnull=True)
        elif self.value() == 'deleted':
            return queryset.filter(deleted_at__isnull=False)
        return queryset


# ====================================================================
# USER ADMIN
# ====================================================================

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Enhanced Admin interface for User model
    """
    
    # Use custom forms
    form = UserChangeForm
    add_form = UserCreationForm
    
    # ================================================================
    # LIST DISPLAY
    # ================================================================
    
    list_display = (
        'email_or_phone',
        'display_name_colored',
        'role_badge',
        'account_status_badge',
        'verification_badges',
        'mfa_badge',
        'is_staff',
        'is_active',
        'date_joined',
        'last_login',
    )
    
    list_display_links = ('email_or_phone', 'display_name_colored')
    
    list_filter = (
        'role',
        'account_status',
        VerificationStatusFilter,
        AccountSecurityFilter,
        SoftDeletedFilter,
        'is_active',
        'is_staff',
        'is_superuser',
        'mfa_enabled',
        'marketing_consent',
        'language',
        'country',
        'date_joined',
    )
    
    search_fields = (
        'email_or_phone',
        'email',
        'phone',
        'uuid',
        'last_login_ip',
    )
    
    ordering = ('-date_joined',)
    
    # ================================================================
    # CUSTOM DISPLAY METHODS
    # ================================================================
    
    @admin.display(description='Name', ordering='email_or_phone')
    def display_name_colored(self, obj):
        """Display name with color coding based on verification"""
        try:
            if obj.is_fully_verified:
                color = '#28a745'  # Green
                icon = '✓'
            elif obj.is_verified:
                color = '#ffc107'  # Yellow
                icon = '⚠'
            else:
                color = '#dc3545'  # Red
                icon = '✗'
            
            return format_html(
                '<span style="color: {};">{} {}</span>',
                color,
                icon,
                obj.display_name
            )
        except Exception:
            return obj.email_or_phone
    
    @admin.display(description='Role')
    def role_badge(self, obj):
        """Display role with colored badge"""
        colors = {
            'admin': '#dc3545',      # Red
            'dealer': '#17a2b8',     # Cyan
            'customer': '#6c757d',   # Gray
            'staff': '#007bff',      # Blue
            'moderator': '#ffc107',  # Yellow
        }
        
        color = colors.get(obj.role, '#6c757d')
        
        return format_html(
            '<span style="background-color: {}; color: white; '
            'padding: 3px 10px; border-radius: 3px; font-size: 11px; '
            'font-weight: bold;">{}</span>',
            color,
            obj.get_role_display().upper()
        )
    
    @admin.display(description='Status')
    def account_status_badge(self, obj):
        """Display account status with colored badge"""
        colors = {
            'active': '#28a745',      # Green
            'inactive': '#6c757d',    # Gray
            'suspended': '#dc3545',   # Red
            'pending': '#ffc107',     # Yellow
            'locked': '#fd7e14',      # Orange
        }
        
        color = colors.get(obj.account_status, '#6c757d')
        
        return format_html(
            '<span style="background-color: {}; color: white; '
            'padding: 3px 10px; border-radius: 3px; font-size: 11px;">{}</span>',
            color,
            obj.get_account_status_display().upper()
        )
    
    @admin.display(description='Verification')
    def verification_badges(self, obj):
        """Display verification status badges"""
        badges = []
        
        # Email verification
        if obj.email:
            if obj.email_verified:
                badges.append(
                    '<span style="background-color: #28a745; color: white; '
                    'padding: 2px 6px; border-radius: 3px; font-size: 10px; '
                    'margin-right: 3px;">📧 ✓</span>'
                )
            else:
                badges.append(
                    '<span style="background-color: #dc3545; color: white; '
                    'padding: 2px 6px; border-radius: 3px; font-size: 10px; '
                    'margin-right: 3px;">📧 ✗</span>'
                )
        
        # Phone verification
        if obj.phone:
            if obj.phone_verified:
                badges.append(
                    '<span style="background-color: #28a745; color: white; '
                    'padding: 2px 6px; border-radius: 3px; font-size: 10px;">📱 ✓</span>'
                )
            else:
                badges.append(
                    '<span style="background-color: #dc3545; color: white; '
                    'padding: 2px 6px; border-radius: 3px; font-size: 10px;">📱 ✗</span>'
                )
        
        return format_html(''.join(badges) if badges else '-')
    
    @admin.display(description='MFA')
    def mfa_badge(self, obj):
        """Display MFA status badge"""
        if obj.mfa_enabled:
            return format_html(
                '<span style="background-color: #28a745; color: white; '
                'padding: 2px 6px; border-radius: 3px; font-size: 10px;">🔐 ON</span>'
            )
        return format_html(
            '<span style="background-color: #6c757d; color: white; '
            'padding: 2px 6px; border-radius: 3px; font-size: 10px;">🔐 OFF</span>'
        )
    
    # ================================================================
    # FIELDSETS (for detail view)
    # ================================================================
    
    def get_fieldsets(self, request, obj=None):
        """
        Dynamic fieldsets that only include fields that exist in the model
        """
        # Get all field names from the model
        model_fields = [f.name for f in self.model._meta.get_fields()]
        
        # Helper function to filter fields
        def filter_fields(fields):
            """Filter out fields that don't exist in model"""
            filtered = []
            for field in fields:
                if isinstance(field, tuple):
                    # Handle tuple of fields (for inline display)
                    tuple_fields = tuple(f for f in field if f in model_fields)
                    if tuple_fields:
                        filtered.append(tuple_fields)
                elif field in model_fields:
                    filtered.append(field)
            return tuple(filtered) if filtered else None
        
        fieldsets = []
        
        # Basic Information
        basic_fields = filter_fields((
            'uuid',
            'email_or_phone',
            'email',
            'phone',
            'password',
        ))
        if basic_fields:
            fieldsets.append((_('Basic Information'), {
                'fields': basic_fields,
                'description': 'Core user identification and authentication fields'
            }))
        
        # Role & Permissions
        role_fields = filter_fields((
            'role',
            'account_status',
            'is_active',
            'is_staff',
            'is_superuser',
            'groups',
            'user_permissions',
        ))
        if role_fields:
            fieldsets.append((_('Role & Permissions'), {
                'fields': role_fields,
                'classes': ('collapse',),
            }))
        
        # Verification Status
        verification_fields = filter_fields((
            ('email_verified', 'email_verified_at'),
            ('phone_verified', 'phone_verified_at'),
        ))
        if verification_fields:
            fieldsets.append((_('Verification Status'), {
                'fields': verification_fields,
                'classes': ('collapse',),
            }))
        
        # Multi-Factor Authentication
        mfa_fields = filter_fields((
            'mfa_enabled',
            'mfa_method',
            'mfa_secret',
        ))
        if mfa_fields:
            fieldsets.append((_('Multi-Factor Authentication'), {
                'fields': mfa_fields,
                'classes': ('collapse',),
                'description': 'Two-factor authentication settings'
            }))
        
        # Security
        security_fields = filter_fields((
            'failed_login_attempts',
            'locked_until',
            'last_login',
            'last_login_ip',
            'last_login_user_agent',
            'password_changed_at',
            'require_password_change',
        ))
        if security_fields:
            fieldsets.append((_('Security & Login'), {
                'fields': security_fields,
                'classes': ('collapse',),
            }))
        
        # Internationalization (only if fields exist)
        i18n_fields = filter_fields((
            'timezone',
            'language',
            'country',
            'currency',
        ))
        if i18n_fields:
            fieldsets.append((_('Localization'), {
                'fields': i18n_fields,
                'classes': ('collapse',),
            }))
        
        # GDPR & Privacy
        gdpr_fields = filter_fields((
            ('marketing_consent', 'marketing_consent_date'),
            ('terms_accepted', 'terms_accepted_date'),
            ('privacy_accepted', 'privacy_accepted_date'),
            'data_processing_consent',
        ))
        if gdpr_fields:
            fieldsets.append((_('GDPR & Privacy'), {
                'fields': gdpr_fields,
                'classes': ('collapse',),
            }))
        
        # Timestamps
        timestamp_fields = filter_fields((
            'date_joined',
            'updated_at',
            ('deleted_at', 'deleted_by'),
        ))
        if timestamp_fields:
            fieldsets.append((_('Timestamps'), {
                'fields': timestamp_fields,
                'classes': ('collapse',),
            }))
        
        # Metadata
        metadata_fields = filter_fields(('metadata',))
        if metadata_fields:
            fieldsets.append((_('Additional Data'), {
                'fields': metadata_fields,
                'classes': ('collapse',),
            }))
        
        return fieldsets
    
    # ================================================================
    # FIELDSETS FOR ADD USER
    # ================================================================
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'email_or_phone',
                'email',
                'phone',
                'password1',
                'password2',
                'role',
                'is_staff',
                'is_active',
            ),
        }),
    )
    
    # ================================================================
    # READONLY FIELDS
    # ================================================================
    
    def get_readonly_fields(self, request, obj=None):
        """
        Make certain fields readonly based on conditions
        Dynamic to handle fields that may not exist in DB yet
        """
        # Get all field names from the model
        model_fields = [f.name for f in self.model._meta.get_fields()]
        
        # Base readonly fields (only if they exist)
        readonly_candidates = [
            'uuid',
            'date_joined',
            'updated_at',
            'last_login',
            'email_verified_at',
            'phone_verified_at',
            'marketing_consent_date',
            'terms_accepted_date',
            'privacy_accepted_date',
            'password_changed_at',
            'deleted_at',
            'deleted_by',
            'last_login_ip',
            'last_login_user_agent',
        ]
        
        readonly = [field for field in readonly_candidates if field in model_fields]
        
        # Make email_or_phone readonly after creation
        if obj and 'email_or_phone' in model_fields:
            if 'email_or_phone' not in readonly:
                readonly.append('email_or_phone')
        
        return readonly
    
    # ================================================================
    # FILTERS & ACTIONS
    # ================================================================
    
    filter_horizontal = ('groups', 'user_permissions')
    
    actions = [
        'verify_email_action',
        'verify_phone_action',
        'activate_accounts',
        'deactivate_accounts',
        'suspend_accounts',
        'unlock_accounts',
        'enable_mfa_action',
        'disable_mfa_action',
        'force_password_change_action',
        'export_user_data_action',
        'soft_delete_action',
    ]
    
    # ================================================================
    # ADMIN ACTIONS
    # ================================================================
    
    @admin.action(description='✓ Verify Email for selected users')
    def verify_email_action(self, request, queryset):
        """Bulk verify email addresses"""
        count = 0
        for user in queryset:
            if user.email and not user.email_verified:
                user.verify_email()
                count += 1
        
        self.message_user(
            request,
            f'Successfully verified email for {count} user(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='✓ Verify Phone for selected users')
    def verify_phone_action(self, request, queryset):
        """Bulk verify phone numbers"""
        count = 0
        for user in queryset:
            if user.phone and not user.phone_verified:
                user.verify_phone()
                count += 1
        
        self.message_user(
            request,
            f'Successfully verified phone for {count} user(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='✓ Activate selected accounts')
    def activate_accounts(self, request, queryset):
        """Bulk activate user accounts"""
        count = 0
        for user in queryset:
            if not user.is_active:
                user.activate_account()
                count += 1
        
        self.message_user(
            request,
            f'Successfully activated {count} account(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='✗ Deactivate selected accounts')
    def deactivate_accounts(self, request, queryset):
        """Bulk deactivate user accounts"""
        count = 0
        for user in queryset:
            if user.is_active:
                user.deactivate_account()
                count += 1
        
        self.message_user(
            request,
            f'Successfully deactivated {count} account(s).',
            messages.WARNING
        )
    
    @admin.action(description='🚫 Suspend selected accounts')
    def suspend_accounts(self, request, queryset):
        """Bulk suspend user accounts"""
        count = 0
        for user in queryset:
            if user.account_status != User.AccountStatus.SUSPENDED:
                user.suspend_account()
                count += 1
        
        self.message_user(
            request,
            f'Successfully suspended {count} account(s).',
            messages.WARNING
        )
    
    @admin.action(description='🔓 Unlock selected accounts')
    def unlock_accounts(self, request, queryset):
        """Bulk unlock user accounts"""
        count = 0
        for user in queryset:
            if user.is_locked:
                user.unlock_account()
                count += 1
        
        self.message_user(
            request,
            f'Successfully unlocked {count} account(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='🔐 Enable MFA for selected users')
    def enable_mfa_action(self, request, queryset):
        """Bulk enable MFA"""
        count = 0
        for user in queryset:
            if not user.mfa_enabled:
                user.enable_mfa(method='email')
                count += 1
        
        self.message_user(
            request,
            f'Successfully enabled MFA for {count} user(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='🔓 Disable MFA for selected users')
    def disable_mfa_action(self, request, queryset):
        """Bulk disable MFA"""
        count = 0
        for user in queryset:
            if user.mfa_enabled:
                user.disable_mfa()
                count += 1
        
        self.message_user(
            request,
            f'Successfully disabled MFA for {count} user(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='🔑 Force password change on next login')
    def force_password_change_action(self, request, queryset):
        """Force users to change password on next login"""
        count = queryset.update(require_password_change=True)
        
        self.message_user(
            request,
            f'Password change required for {count} user(s) on next login.',
            messages.WARNING
        )
    
    @admin.action(description='📥 Export user data (GDPR)')
    def export_user_data_action(self, request, queryset):
        """Export user data for GDPR compliance"""
        data = []
        for user in queryset:
            data.append(user.export_data())
        
        self.message_user(
            request,
            f'Data exported for {len(data)} user(s). '
            f'In production, this would trigger a download or email.',
            messages.INFO
        )
    
    @admin.action(description='🗑️ Soft delete selected accounts (GDPR)')
    def soft_delete_action(self, request, queryset):
        """Soft delete user accounts (GDPR compliant)"""
        count = 0
        for user in queryset:
            if not user.deleted_at:
                user.soft_delete(deleted_by_user=request.user)
                count += 1
        
        self.message_user(
            request,
            f'Successfully soft-deleted {count} account(s). '
            f'Data anonymized per GDPR requirements.',
            messages.WARNING
        )
    
    # ================================================================
    # ADDITIONAL CONFIGURATIONS
    # ================================================================
    
    def get_queryset(self, request):
        """
        Customize queryset to optimize database queries
        """
        qs = super().get_queryset(request)
        # Optimize queries with select_related
        qs = qs.select_related('deleted_by')
        return qs
    
    def save_model(self, request, obj, form, change):
        """
        Custom save logic
        """
        super().save_model(request, obj, form, change)
    
    def has_delete_permission(self, request, obj=None):
        """
        Control delete permission - only superusers can hard delete
        """
        if request.user.is_superuser:
            return True
        return False