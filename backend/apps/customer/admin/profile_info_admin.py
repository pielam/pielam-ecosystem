# apps/customer/admin/profile_info_admin.py
# (ProfileInfo Admin)

"""
Django Admin Configuration for ProfileInfo Model
------------------------------------------------
Features:
- Profile management with badges
- Verification controls
- Social feature management
- Business profile handling
- Privacy settings
- Analytics display
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.db.models import Count, Q
from django.contrib import messages
from django.urls import reverse

from apps.customer.models.profile_info import ProfileInfo


# ====================================================================
# CUSTOM FILTERS
# ====================================================================

class VerificationStatusFilter(admin.SimpleListFilter):
    """Filter profiles by verification status"""
    title = _('Verification Status')
    parameter_name = 'verification'
    
    def lookups(self, request, model_admin):
        return (
            ('verified', _('Verified')),
            ('unverified', _('Not Verified')),
            ('email', _('Email Verified')),
            ('phone', _('Phone Verified')),
            ('id', _('ID Verified')),
            ('business', _('Business Verified')),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'verified':
            return queryset.filter(is_profile_verified=True)
        elif self.value() == 'unverified':
            return queryset.filter(is_profile_verified=False)
        elif self.value() in ['email', 'phone', 'id', 'business']:
            return queryset.filter(verification_level=self.value())
        return queryset


class ProfileTypeFilter(admin.SimpleListFilter):
    """Filter profiles by type"""
    title = _('Profile Type')
    parameter_name = 'profile_type'
    
    def lookups(self, request, model_admin):
        return ProfileInfo.ProfileType.choices
    
    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(profile_type=self.value())
        return queryset


class ProfileStatusFilter(admin.SimpleListFilter):
    """Filter profiles by various status flags"""
    title = _('Profile Status')
    parameter_name = 'status'
    
    def lookups(self, request, model_admin):
        return (
            ('public', _('Public')),
            ('private', _('Private')),
            ('featured', _('Featured')),
            ('suspended', _('Suspended')),
            ('archived', _('Archived')),
            ('complete', _('Complete')),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'public':
            return queryset.filter(is_profile_public=True, is_profile_archived=False)
        elif self.value() == 'private':
            return queryset.filter(is_profile_public=False)
        elif self.value() == 'featured':
            return queryset.filter(is_profile_featured=True)
        elif self.value() == 'suspended':
            return queryset.filter(is_profile_suspended=True)
        elif self.value() == 'archived':
            return queryset.filter(is_profile_archived=True)
        return queryset


class FollowerCountFilter(admin.SimpleListFilter):
    """Filter profiles by follower count ranges"""
    title = _('Follower Count')
    parameter_name = 'followers'
    
    def lookups(self, request, model_admin):
        return (
            ('0', _('No followers')),
            ('1-10', _('1-10 followers')),
            ('11-50', _('11-50 followers')),
            ('51-100', _('51-100 followers')),
            ('100+', _('100+ followers')),
        )
    
    def queryset(self, request, queryset):
        # Use a different annotation name to avoid conflict with property
        queryset = queryset.annotate(followers_count_filter=Count('followers'))
        
        if self.value() == '0':
            return queryset.filter(followers_count_filter=0)
        elif self.value() == '1-10':
            return queryset.filter(followers_count_filter__gte=1, followers_count_filter__lte=10)
        elif self.value() == '11-50':
            return queryset.filter(followers_count_filter__gte=11, followers_count_filter__lte=50)
        elif self.value() == '51-100':
            return queryset.filter(followers_count_filter__gte=51, followers_count_filter__lte=100)
        elif self.value() == '100+':
            return queryset.filter(followers_count_filter__gte=100)
        return queryset


class BusinessProfileFilter(admin.SimpleListFilter):
    """Filter business profiles"""
    title = _('Business Profile')
    parameter_name = 'is_business'
    
    def lookups(self, request, model_admin):
        return (
            ('yes', _('Yes')),
            ('no', _('No')),
        )
    
    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(
                profile_type__in=['business', 'professional']
            )
        elif self.value() == 'no':
            return queryset.exclude(
                profile_type__in=['business', 'professional']
            )
        return queryset


# ====================================================================
# PROFILE INFO ADMIN
# ====================================================================

@admin.register(ProfileInfo)
class ProfileInfoAdmin(admin.ModelAdmin):
    """
    Enhanced Admin interface for ProfileInfo model
    """
    
    # ================================================================
    # LIST DISPLAY
    # ================================================================
    
    list_display = (
        'profile_photo_thumbnail',
        'profile_name_display',
        'user_link',
        'profile_type_badge',
        'verification_badge',
        'status_badges',
        'follower_count_display',
        'profile_views',
        'completion_badge',
        'profile_creation_time',
    )
    
    list_display_links = ('profile_name_display', 'user_link')
    
    list_filter = (
        VerificationStatusFilter,
        ProfileTypeFilter,
        ProfileStatusFilter,
        BusinessProfileFilter,
        FollowerCountFilter,
        'is_profile_verified',
        'is_profile_public',
        'is_profile_featured',
        'is_profile_suspended',
        'is_profile_archived',
        'profile_gender',
        'profile_country',
        'profile_creation_time',
    )
    
    search_fields = (
        'profile_name',
        'profile_name_slug',
        'profile_bio',
        'user__email',
        'user__email_or_phone',
        'profile_phone',
        'business_name',
        'uuid',
    )
    
    ordering = ('-profile_creation_time',)
    
    date_hierarchy = 'profile_creation_time'
    
    # ================================================================
    # CUSTOM DISPLAY METHODS
    # ================================================================
    
    @admin.display(description='Photo')
    def profile_photo_thumbnail(self, obj):
        """Display profile photo thumbnail"""
        if obj.profile_photo:
            return format_html(
                '<img src="{}" style="width: 50px; height: 50px; '
                'border-radius: 50%; object-fit: cover;" />',
                obj.profile_photo.url
            )
        return format_html(
            '<div style="width: 50px; height: 50px; border-radius: 50%; '
            'background-color: #e0e0e0; display: flex; align-items: center; '
            'justify-content: center; font-size: 20px;">👤</div>'
        )
    
    @admin.display(description='Profile Name', ordering='profile_name')
    def profile_name_display(self, obj):
        """Display profile name with link"""
        name = obj.profile_name or obj.user.email_or_phone
        
        if obj.is_profile_verified:
            icon = '✓'
            color = '#28a745'
        else:
            icon = ''
            color = '#000'
        
        return format_html(
            '<strong style="color: {};">{} {}</strong><br>'
            '<small style="color: #666;">@{}</small>',
            color,
            name,
            icon,
            obj.profile_name_slug or 'no-slug'
        )
    
    @admin.display(description='User', ordering='user__email_or_phone')
    def user_link(self, obj):
        """Display link to user admin"""
        url = reverse('admin:customer_user_change', args=[obj.user.pk])
        return format_html(
            '<a href="{}">{}</a>',
            url,
            obj.user.email_or_phone
        )
    
    @admin.display(description='Type')
    def profile_type_badge(self, obj):
        """Display profile type badge"""
        colors = {
            'personal': '#6c757d',
            'business': '#17a2b8',
            'professional': '#007bff',
            'public_figure': '#ffc107',
        }
        
        color = colors.get(obj.profile_type, '#6c757d')
        
        return format_html(
            '<span style="background-color: {}; color: white; '
            'padding: 3px 8px; border-radius: 3px; font-size: 11px; '
            'font-weight: bold;">{}</span>',
            color,
            obj.get_profile_type_display().upper()
        )
    
    @admin.display(description='Verification')
    def verification_badge(self, obj):
        """Display verification badge"""
        if obj.is_profile_verified:
            colors = {
                'email': '#17a2b8',
                'phone': '#28a745',
                'id': '#ffc107',
                'business': '#007bff',
                'none': '#6c757d',
            }
            
            color = colors.get(obj.verification_level, '#28a745')
            
            return format_html(
                '<span style="background-color: {}; color: white; '
                'padding: 3px 8px; border-radius: 3px; font-size: 10px;">'
                '✓ {}</span>',
                color,
                obj.get_verification_level_display().upper()
            )
        
        return format_html(
            '<span style="background-color: #6c757d; color: white; '
            'padding: 3px 8px; border-radius: 3px; font-size: 10px;">NOT VERIFIED</span>'
        )
    
    @admin.display(description='Status')
    def status_badges(self, obj):
        """Display status badges"""
        badges = []
        
        # Public/Private
        if obj.is_profile_public:
            badges.append(
                '<span style="background-color: #28a745; color: white; '
                'padding: 2px 5px; border-radius: 3px; font-size: 9px; '
                'margin-right: 2px;">PUBLIC</span>'
            )
        else:
            badges.append(
                '<span style="background-color: #6c757d; color: white; '
                'padding: 2px 5px; border-radius: 3px; font-size: 9px; '
                'margin-right: 2px;">PRIVATE</span>'
            )
        
        # Featured
        if obj.is_profile_featured:
            badges.append(
                '<span style="background-color: #ffc107; color: #000; '
                'padding: 2px 5px; border-radius: 3px; font-size: 9px; '
                'margin-right: 2px;">⭐ FEATURED</span>'
            )
        
        # Suspended
        if obj.is_profile_suspended:
            badges.append(
                '<span style="background-color: #dc3545; color: white; '
                'padding: 2px 5px; border-radius: 3px; font-size: 9px; '
                'margin-right: 2px;">🚫 SUSPENDED</span>'
            )
        
        # Archived
        if obj.is_profile_archived:
            badges.append(
                '<span style="background-color: #6c757d; color: white; '
                'padding: 2px 5px; border-radius: 3px; font-size: 9px;">📁 ARCHIVED</span>'
            )
        
        return format_html(''.join(badges))
    
    @admin.display(description='Followers', ordering='followers_count_annotated')
    def follower_count_display(self, obj):
        """Display follower count with icon"""
        # Use annotated value if available, otherwise use property
        count = getattr(obj, 'followers_count_annotated', obj.follower_count)
        
        if count >= 1000:
            display = f"{count/1000:.1f}K"
        else:
            display = str(count)
        
        color = '#28a745' if count > 100 else '#6c757d'
        
        return format_html(
            '<span style="color: {}; font-weight: bold;">👥 {}</span>',
            color,
            display
        )
    
    @admin.display(description='Completion')
    def completion_badge(self, obj):
        """Display profile completion percentage"""
        percentage = obj.completion_percentage
        
        if percentage >= 80:
            color = '#28a745'
        elif percentage >= 50:
            color = '#ffc107'
        else:
            color = '#dc3545'
        
        return format_html(
            '<div style="width: 60px; background-color: #e0e0e0; '
            'border-radius: 10px; overflow: hidden; height: 18px;">'
            '<div style="width: {}%; background-color: {}; height: 100%; '
            'display: flex; align-items: center; justify-content: center; '
            'color: white; font-size: 10px; font-weight: bold;">{}</div>'
            '</div>',
            percentage,
            color,
            f'{percentage}%'
        )
    
    # ================================================================
    # FIELDSETS
    # ================================================================
    
    def get_fieldsets(self, request, obj=None):
        """
        Dynamic fieldsets based on profile type and existing fields
        """
        # Get all field names from the model
        model_fields = [f.name for f in self.model._meta.get_fields()]
        
        # Helper function to filter fields
        def filter_fields(fields):
            """Filter out fields that don't exist in model"""
            filtered = []
            for field in fields:
                if isinstance(field, tuple):
                    tuple_fields = tuple(f for f in field if f in model_fields)
                    if tuple_fields:
                        filtered.append(tuple_fields)
                elif field in model_fields:
                    filtered.append(field)
            return tuple(filtered) if filtered else None
        
        fieldsets = []
        
        # Basic Information
        basic_fields = filter_fields((
            'user',
            'uuid',
            'profile_name',
            'profile_name_slug',
            'profile_type',
            'profile_tagline',
            'profile_bio',
        ))
        if basic_fields:
            fieldsets.append((_('Basic Information'), {
                'fields': basic_fields,
                'description': 'Core profile information and identity'
            }))
        
        # Profile Images
        image_fields = filter_fields((
            'profile_photo',
            'profile_cover_photo',
        ))
        if image_fields:
            fieldsets.append((_('Profile Images'), {
                'fields': image_fields,
                'classes': ('collapse',),
            }))
        
        # Personal Information
        personal_fields = filter_fields((
            'profile_gender',
            'profile_dob',
            'profile_phone',
            'profile_language',
        ))
        if personal_fields:
            fieldsets.append((_('Personal Information'), {
                'fields': personal_fields,
                'classes': ('collapse',),
            }))
        
        # Location
        location_fields = filter_fields((
            'profile_country',
            'profile_state',
            'profile_city',
            'profile_address',
            'profile_postal_code',
            ('profile_latitude', 'profile_longitude'),
        ))
        if location_fields:
            fieldsets.append((_('Location & Address'), {
                'fields': location_fields,
                'classes': ('collapse',),
            }))
        
        # Business Information (only show for business profiles)
        if obj and obj.is_business:
            business_fields = filter_fields((
                'business_name',
                'business_type',
                'business_registration',
                'business_tax_id',
                'business_website',
                ('business_email', 'business_phone'),
                'business_description',
            ))
            if business_fields:
                fieldsets.append((_('Business Information'), {
                    'fields': business_fields,
                    'classes': ('wide',),
                    'description': 'Business and professional details'
                }))
        
        # Social Media Links
        social_fields = filter_fields((
            'social_facebook',
            'social_twitter',
            'social_instagram',
            'social_linkedin',
            'social_youtube',
            'social_tiktok',
            'social_whatsapp',
        ))
        if social_fields:
            fieldsets.append((_('Social Media'), {
                'fields': social_fields,
                'classes': ('collapse',),
            }))
        
        # Verification & Status
        verification_fields = filter_fields((
            'verification_level',
            ('is_profile_verified', 'verified_at'),
            'verified_by',
            'is_profile_public',
            'is_profile_featured',
            ('is_profile_suspended', 'suspended_at'),
            'suspension_reason',
        ))
        if verification_fields:
            fieldsets.append((_('Verification & Status'), {
                'fields': verification_fields,
                'classes': ('collapse',),
            }))
        
        # Privacy Settings
        privacy_fields = filter_fields((
            'show_email',
            'show_phone',
            'show_dob',
            'show_age',
            'show_location',
            ('show_followers', 'show_following'),
            ('allow_messages', 'allow_follow'),
        ))
        if privacy_fields:
            fieldsets.append((_('Privacy Settings'), {
                'fields': privacy_fields,
                'classes': ('collapse',),
            }))
        
        # Notification Preferences
        notification_fields = filter_fields((
            'notify_on_follow',
            'notify_on_message',
            'notify_on_comment',
            'notify_on_mention',
            ('email_notifications', 'sms_notifications'),
        ))
        if notification_fields:
            fieldsets.append((_('Notification Preferences'), {
                'fields': notification_fields,
                'classes': ('collapse',),
            }))
        
        # Social Features
        social_feature_fields = filter_fields((
            'followers',
            'following',
            'blocked_users',
        ))
        if social_feature_fields:
            fieldsets.append((_('Social Features'), {
                'fields': social_feature_fields,
                'classes': ('collapse',),
                'description': 'Follower/following relationships and blocked users'
            }))
        
        # Analytics
        analytics_fields = filter_fields((
            'profile_views',
            'last_viewed_at',
        ))
        if analytics_fields:
            fieldsets.append((_('Analytics'), {
                'fields': analytics_fields,
                'classes': ('collapse',),
            }))
        
        # Timestamps
        timestamp_fields = filter_fields((
            'profile_creation_time',
            'profile_updated_time',
            ('is_profile_archived', 'archived_at'),
            'archived_by',
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
    # READONLY FIELDS
    # ================================================================
    
    def get_readonly_fields(self, request, obj=None):
        """
        Dynamic readonly fields
        """
        model_fields = [f.name for f in self.model._meta.get_fields()]
        
        readonly_candidates = [
            'user',
            'uuid',
            'profile_name_slug',
            'verified_at',
            'verified_by',
            'suspended_at',
            'archived_at',
            'archived_by',
            'profile_creation_time',
            'profile_updated_time',
            'last_viewed_at',
            'profile_views',
        ]
        
        readonly = [field for field in readonly_candidates if field in model_fields]
        
        # Make user readonly after creation
        if obj and 'user' not in readonly:
            readonly.append('user')
        
        return readonly
    
    # ================================================================
    # FILTERS
    # ================================================================
    
    filter_horizontal = ('followers', 'following', 'blocked_users')
    
    # ================================================================
    # ACTIONS
    # ================================================================
    
    actions = [
        'verify_profiles',
        'unverify_profiles',
        'make_public',
        'make_private',
        'feature_profiles',
        'unfeature_profiles',
        'suspend_profiles',
        'unsuspend_profiles',
        'archive_profiles',
        'restore_profiles',
        'generate_slugs',
    ]
    
    # ================================================================
    # ADMIN ACTIONS
    # ================================================================
    
    @admin.action(description='✓ Verify selected profiles')
    def verify_profiles(self, request, queryset):
        """Bulk verify profiles"""
        count = 0
        for profile in queryset:
            if not profile.is_profile_verified:
                profile.verify(verified_by_user=request.user)
                count += 1
        
        self.message_user(
            request,
            f'Successfully verified {count} profile(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='✗ Unverify selected profiles')
    def unverify_profiles(self, request, queryset):
        """Bulk unverify profiles"""
        count = 0
        for profile in queryset:
            if profile.is_profile_verified:
                profile.unverify()
                count += 1
        
        self.message_user(
            request,
            f'Successfully unverified {count} profile(s).',
            messages.WARNING
        )
    
    @admin.action(description='🌐 Make profiles public')
    def make_public(self, request, queryset):
        """Make profiles public"""
        count = 0
        for profile in queryset:
            if not profile.is_profile_public:
                profile.make_public()
                count += 1
        
        self.message_user(
            request,
            f'Made {count} profile(s) public.',
            messages.SUCCESS
        )
    
    @admin.action(description='🔒 Make profiles private')
    def make_private(self, request, queryset):
        """Make profiles private"""
        count = 0
        for profile in queryset:
            if profile.is_profile_public:
                profile.make_private()
                count += 1
        
        self.message_user(
            request,
            f'Made {count} profile(s) private.',
            messages.WARNING
        )
    
    @admin.action(description='⭐ Feature selected profiles')
    def feature_profiles(self, request, queryset):
        """Feature profiles"""
        count = 0
        for profile in queryset:
            if not profile.is_profile_featured:
                profile.feature()
                count += 1
        
        self.message_user(
            request,
            f'Featured {count} profile(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='Remove featured status')
    def unfeature_profiles(self, request, queryset):
        """Remove featured status"""
        count = 0
        for profile in queryset:
            if profile.is_profile_featured:
                profile.unfeature()
                count += 1
        
        self.message_user(
            request,
            f'Unfeatured {count} profile(s).',
            messages.INFO
        )
    
    @admin.action(description='🚫 Suspend selected profiles')
    def suspend_profiles(self, request, queryset):
        """Suspend profiles"""
        count = 0
        for profile in queryset:
            if not profile.is_profile_suspended:
                profile.suspend(reason='Suspended by admin')
                count += 1
        
        self.message_user(
            request,
            f'Suspended {count} profile(s).',
            messages.WARNING
        )
    
    @admin.action(description='✓ Unsuspend selected profiles')
    def unsuspend_profiles(self, request, queryset):
        """Remove suspension"""
        count = 0
        for profile in queryset:
            if profile.is_profile_suspended:
                profile.unsuspend()
                count += 1
        
        self.message_user(
            request,
            f'Unsuspended {count} profile(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='📁 Archive selected profiles')
    def archive_profiles(self, request, queryset):
        """Archive profiles"""
        count = 0
        for profile in queryset:
            if not profile.is_profile_archived:
                profile.soft_delete(archived_by_user=request.user)
                count += 1
        
        self.message_user(
            request,
            f'Archived {count} profile(s).',
            messages.WARNING
        )
    
    @admin.action(description='♻️ Restore archived profiles')
    def restore_profiles(self, request, queryset):
        """Restore archived profiles"""
        count = 0
        for profile in queryset:
            if profile.is_profile_archived:
                profile.restore()
                count += 1
        
        self.message_user(
            request,
            f'Restored {count} profile(s).',
            messages.SUCCESS
        )
    
    @admin.action(description='🔗 Generate slugs for selected profiles')
    def generate_slugs(self, request, queryset):
        """Generate slugs for profiles without them"""
        count = 0
        for profile in queryset:
            if profile.profile_name and not profile.profile_name_slug:
                profile.generate_slug(save=True)
                count += 1
        
        self.message_user(
            request,
            f'Generated slugs for {count} profile(s).',
            messages.SUCCESS
        )
    
    # ================================================================
    # ADDITIONAL CONFIGURATIONS
    # ================================================================
    
    def get_queryset(self, request):
        """
        Optimize queryset with select_related and prefetch_related
        """
        qs = super().get_queryset(request)
        qs = qs.select_related(
            'user',
            'verified_by',
            'archived_by'
        )
        qs = qs.prefetch_related(
            'followers',
            'following',
            'blocked_users'
        )
        # Annotate with follower count for sorting (use different name to avoid conflict with property)
        qs = qs.annotate(followers_count_annotated=Count('followers'))
        return qs
    
    def save_model(self, request, obj, form, change):
        """
        Custom save logic
        """
        # Generate slug if name exists but slug doesn't
        if obj.profile_name and not obj.profile_name_slug:
            obj.generate_slug()
        
        super().save_model(request, obj, form, change)
    
    def has_delete_permission(self, request, obj=None):
        """
        Control delete permission - use soft delete instead
        """
        # Only superusers can hard delete
        if request.user.is_superuser:
            return True
        return False