# apps/customer/models/account.py

"""
Enhanced User Model for International Business Standards
---------------------------------------------------------
Features:
- Multi-factor authentication support
- Email verification tracking
- Phone verification tracking
- Account lockout/security
- Timezone and locale support
- GDPR compliance fields
- Audit trail
- Soft delete
- Enhanced security
"""

import uuid
from datetime import timedelta
from typing import Optional

from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.db.models import Q

from apps.customer.validators import email_or_phone_validator


# ====================================================================
# USER MANAGER
# ====================================================================

class UserManager(BaseUserManager):
    """
    Custom user manager for handling email/phone authentication
    """

    def create_user(
        self, 
        email_or_phone: str, 
        password: Optional[str] = None, 
        email: Optional[str] = None, 
        **extra_fields
    ):
        """
        Create and save a regular user with email or phone
        """
        if not email_or_phone:
            raise ValueError(_("Email or phone number is required"))
        
        # Normalize email if provided
        if email:
            email = self.normalize_email(email)
        
        # Set default values for required fields
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        
        user = self.model(
            email_or_phone=email_or_phone,
            email=email,
            **extra_fields
        )
        
        if password:
            user.set_password(password)
        else:
            # For OAuth users without password
            user.set_unusable_password()
        
        user.save(using=self._db)
        return user

    def create_superuser(
        self, 
        email_or_phone: str, 
        password: Optional[str] = None, 
        email: Optional[str] = None, 
        **extra_fields
    ):
        """
        Create and save a superuser with admin privileges
        """
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("email_verified", True)  # Auto-verify superusers
        extra_fields.setdefault("role", "admin")
        
        if extra_fields.get("is_staff") is not True:
            raise ValueError(_("Superuser must have is_staff=True"))
        if extra_fields.get("is_superuser") is not True:
            raise ValueError(_("Superuser must have is_superuser=True"))
        
        return self.create_user(
            email_or_phone=email_or_phone,
            email=email,
            password=password,
            **extra_fields
        )
    
    def get_by_natural_key(self, username):
        """
        Support for authentication with email OR phone
        """
        return self.get(
            Q(email_or_phone=username) | 
            Q(email=username)
        )
    
    def active_users(self):
        """Get all active, non-deleted users"""
        return self.filter(is_active=True, deleted_at__isnull=True)
    
    def verified_users(self):
        """Get users with verified email or phone"""
        return self.filter(
            Q(email_verified=True) | Q(phone_verified=True),
            is_active=True,
            deleted_at__isnull=True
        )


# ====================================================================
# USER MODEL
# ====================================================================

class User(AbstractBaseUser, PermissionsMixin):
    """
    Enhanced Custom User Model for International Business
    
    Features:
    - Email or Phone login
    - Multi-factor authentication support
    - Email/Phone verification tracking
    - Account security (lockout, failed attempts)
    - GDPR compliance (soft delete, data export)
    - Internationalization (timezone, language, country)
    - Audit trail (login tracking, IP addresses)
    - Role-based access control
    """
    
    # ================================================================
    # ROLE CHOICES
    # ================================================================
    class Role(models.TextChoices):
        ADMIN = 'admin', _('Admin')
        DEALER = 'dealer', _('Dealer')
        CUSTOMER = 'customer', _('Customer')
        STAFF = 'staff', _('Staff')
        MODERATOR = 'moderator', _('Moderator')
    
    class AccountStatus(models.TextChoices):
        ACTIVE = 'active', _('Active')
        INACTIVE = 'inactive', _('Inactive')
        SUSPENDED = 'suspended', _('Suspended')
        PENDING_VERIFICATION = 'pending', _('Pending Verification')
        LOCKED = 'locked', _('Locked')
    
    # ================================================================
    # PRIMARY FIELDS
    # ================================================================
    
    # UUID for external references (more secure than sequential IDs)
    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text=_("Unique identifier for external API references")
    )
    
    # Email OR Phone authentication
    email_or_phone = models.CharField(
        _("Email or Phone"),
        max_length=100,
        unique=True,
        validators=[email_or_phone_validator],
        help_text=_("Primary login identifier - email or phone number")
    )
    
    # Separate email field for OAuth and notifications
    email = models.EmailField(
        _("Email Address"),
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Email address for OAuth and notifications")
    )
    
    # Separate phone field for SMS notifications
    phone = models.CharField(
        _("Phone Number"),
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("International phone number with country code")
    )
    
    # ================================================================
    # ROLE & PERMISSIONS
    # ================================================================
    
    role = models.CharField(
        _("User Role"),
        max_length=20,
        choices=Role.choices,
        default=Role.CUSTOMER,
        db_index=True,
        help_text=_("User role for permission management")
    )
    
    # Account status
    account_status = models.CharField(
        _("Account Status"),
        max_length=20,
        choices=AccountStatus.choices,
        default=AccountStatus.PENDING_VERIFICATION,
        db_index=True,
        help_text=_("Current account status")
    )
    
    # Django auth fields
    is_active = models.BooleanField(
        _("Active"),
        default=True,
        help_text=_("Designates whether this user should be treated as active")
    )
    
    is_staff = models.BooleanField(
        _("Staff Status"),
        default=False,
        help_text=_("Designates whether the user can log into admin site")
    )
    
    # ================================================================
    # VERIFICATION FIELDS
    # ================================================================
    
    email_verified = models.BooleanField(
        _("Email Verified"),
        default=False,
        help_text=_("Whether the email address has been verified")
    )
    
    email_verified_at = models.DateTimeField(
        _("Email Verified At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when email was verified")
    )
    
    phone_verified = models.BooleanField(
        _("Phone Verified"),
        default=False,
        help_text=_("Whether the phone number has been verified")
    )
    
    phone_verified_at = models.DateTimeField(
        _("Phone Verified At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when phone was verified")
    )
    
    # ================================================================
    # MULTI-FACTOR AUTHENTICATION
    # ================================================================
    
    mfa_enabled = models.BooleanField(
        _("MFA Enabled"),
        default=False,
        help_text=_("Whether multi-factor authentication is enabled")
    )
    
    mfa_method = models.CharField(
        _("MFA Method"),
        max_length=20,
        choices=[
            ('sms', _('SMS')),
            ('email', _('Email')),
            ('totp', _('TOTP App')),
            ('backup_codes', _('Backup Codes')),
        ],
        null=True,
        blank=True,
        help_text=_("Preferred multi-factor authentication method")
    )
    
    mfa_secret = models.CharField(
        _("MFA Secret"),
        max_length=255,
        null=True,
        blank=True,
        help_text=_("Encrypted MFA secret key")
    )
    
    # ================================================================
    # SECURITY FIELDS
    # ================================================================
    
    failed_login_attempts = models.PositiveIntegerField(
        _("Failed Login Attempts"),
        default=0,
        help_text=_("Number of consecutive failed login attempts")
    )
    
    locked_until = models.DateTimeField(
        _("Locked Until"),
        null=True,
        blank=True,
        help_text=_("Account locked until this timestamp")
    )
    
    last_login_ip = models.GenericIPAddressField(
        _("Last Login IP"),
        null=True,
        blank=True,
        help_text=_("IP address of last login")
    )
    
    last_login_user_agent = models.TextField(
        _("Last Login User Agent"),
        null=True,
        blank=True,
        help_text=_("User agent string of last login")
    )
    
    password_changed_at = models.DateTimeField(
        _("Password Changed At"),
        null=True,
        blank=True,
        help_text=_("Timestamp of last password change")
    )
    
    require_password_change = models.BooleanField(
        _("Require Password Change"),
        default=False,
        help_text=_("Force password change on next login")
    )
    
    # ================================================================
    # INTERNATIONALIZATION
    # ================================================================
    
    
    language = models.CharField(
        _("Language"),
        max_length=10,
        default='en',
        choices=[
            ('en', _('English')),
            ('bn', _('Bengali')),
            ('es', _('Spanish')),
            ('fr', _('French')),
            ('de', _('German')),
            ('zh', _('Chinese')),
            ('ar', _('Arabic')),
            ('hi', _('Hindi')),
        ],
        help_text=_("User's preferred language")
    )
    
    country = models.CharField(
        _("Country"),
        max_length=2,
        null=True,
        blank=True,
        help_text=_("ISO 3166-1 alpha-2 country code")
    )
    
    currency = models.CharField(
        _("Currency"),
        max_length=3,
        default='USD',
        help_text=_("ISO 4217 currency code")
    )
    
    # ================================================================
    # GDPR & PRIVACY
    # ================================================================
    
    marketing_consent = models.BooleanField(
        _("Marketing Consent"),
        default=False,
        help_text=_("User consent for marketing communications")
    )
    
    marketing_consent_date = models.DateTimeField(
        _("Marketing Consent Date"),
        null=True,
        blank=True,
        help_text=_("When marketing consent was given")
    )
    
    terms_accepted = models.BooleanField(
        _("Terms Accepted"),
        default=False,
        help_text=_("User accepted terms and conditions")
    )
    
    terms_accepted_date = models.DateTimeField(
        _("Terms Accepted Date"),
        null=True,
        blank=True,
        help_text=_("When terms were accepted")
    )
    
    privacy_accepted = models.BooleanField(
        _("Privacy Policy Accepted"),
        default=False,
        help_text=_("User accepted privacy policy")
    )
    
    privacy_accepted_date = models.DateTimeField(
        _("Privacy Accepted Date"),
        null=True,
        blank=True,
        help_text=_("When privacy policy was accepted")
    )
    
    data_processing_consent = models.BooleanField(
        _("Data Processing Consent"),
        default=True,
        help_text=_("Consent for data processing (GDPR)")
    )
    
    # ================================================================
    # TIMESTAMPS
    # ================================================================
    date_joined = models.DateTimeField(
        _("Date Joined"),
        default=timezone.now,
        help_text=_("When the user account was created")
    )
    
    updated_at = models.DateTimeField(
        _("Updated At"),
        auto_now=True,
        help_text=_("Last time the user record was updated")
    )
    
    # Soft delete
    deleted_at = models.DateTimeField(
        _("Deleted At"),
        null=True,
        blank=True,
        help_text=_("Soft delete timestamp for GDPR compliance")
    )
    
    deleted_by = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_users',
        help_text=_("Admin who deleted this account")
    )
    
    # ================================================================
    # METADATA
    # ================================================================
    
    metadata = models.JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional user metadata in JSON format")
    )
    
    # ================================================================
    # MANAGER
    # ================================================================
    
    objects = UserManager()
    
    USERNAME_FIELD = "email_or_phone"
    REQUIRED_FIELDS = []  # Email is optional for phone-only users
    
    # ================================================================
    # META
    # ================================================================
    
    class Meta:
        verbose_name = _("User")
        verbose_name_plural = _("Users")
        
        ordering = ['-date_joined']
        indexes = [
            models.Index(fields=['email_or_phone']),
            models.Index(fields=['email']),
            models.Index(fields=['phone']),
            models.Index(fields=['uuid']),
            models.Index(fields=['role']),
            models.Index(fields=['account_status']),
            models.Index(fields=['is_active', 'deleted_at']),
            models.Index(fields=['date_joined']),
        ]
    
    # ================================================================
    # STRING REPRESENTATION
    # ================================================================
    
    def __str__(self) -> str:
        """Return the best available identifier"""
        if self.email:
            return self.email
        return self.email_or_phone
    
    def __repr__(self) -> str:
        return f"<User: {self.email_or_phone} ({self.role})>"
    
    # ================================================================
    # PROPERTY METHODS
    # ================================================================
    
    @property
    def is_email(self) -> bool:
        """Check if email_or_phone is an email address"""
        return "@" in self.email_or_phone
    
    @property
    def is_phone(self) -> bool:
        """Check if email_or_phone is a phone number"""
        return self.email_or_phone.replace("+", "").replace(" ", "").isdigit()
    
    @property
    def is_verified(self) -> bool:
        """Check if user has verified email OR phone"""
        return self.email_verified or self.phone_verified
    
    @property
    def is_fully_verified(self) -> bool:
        """Check if user has verified BOTH email AND phone (if both exist)"""
        verified = True
        if self.email:
            verified = verified and self.email_verified
        if self.phone:
            verified = verified and self.phone_verified
        return verified
    
    @property
    def is_locked(self) -> bool:
        """Check if account is currently locked"""
        if self.locked_until is None:
            return False
        return timezone.now() < self.locked_until
    
    @property
    def display_name(self) -> str:
        """Return the best display name for the user"""
        # Try to get from related profile if exists
        if hasattr(self, 'profileinfo') and self.profileinfo.profile_name:
            return self.profileinfo.profile_name
        
        # Fallback to email or phone
        if self.email:
            return self.email.split('@')[0]
        return self.email_or_phone
    
    @property
    def full_name(self) -> str:
        """Get full name from profile or fallback"""
        if hasattr(self, 'profileinfo') and self.profileinfo.profile_name:
            return self.profileinfo.profile_name
        return self.display_name
    
    @property
    def is_dealer(self) -> bool:
        """Check if user is a dealer"""
        return self.role == self.Role.DEALER
    
    @property
    def is_customer(self) -> bool:
        """Check if user is a customer"""
        return self.role == self.Role.CUSTOMER
    
    @property
    def is_admin(self) -> bool:
        """Check if user is an admin"""
        return self.role == self.Role.ADMIN or self.is_superuser
    
    @property
    def days_since_joined(self) -> int:
        """Calculate days since account creation"""
        return (timezone.now() - self.date_joined).days
    
    @property
    def password_age_days(self) -> Optional[int]:
        """Calculate days since last password change"""
        if self.password_changed_at:
            return (timezone.now() - self.password_changed_at).days
        return None
    
    @property
    def needs_password_rotation(self) -> bool:
        """Check if password needs rotation (90 days)"""
        if self.password_age_days:
            return self.password_age_days > 90
        return False
    
    # ================================================================
    # VERIFICATION METHODS
    # ================================================================
    
    def verify_email(self, save: bool = True) -> None:
        """Mark email as verified"""
        self.email_verified = True
        self.email_verified_at = timezone.now()
        if self.account_status == self.AccountStatus.PENDING_VERIFICATION:
            self.account_status = self.AccountStatus.ACTIVE
        if save:
            self.save(update_fields=['email_verified', 'email_verified_at', 'account_status'])
    
    def verify_phone(self, save: bool = True) -> None:
        """Mark phone as verified"""
        self.phone_verified = True
        self.phone_verified_at = timezone.now()
        if self.account_status == self.AccountStatus.PENDING_VERIFICATION:
            self.account_status = self.AccountStatus.ACTIVE
        if save:
            self.save(update_fields=['phone_verified', 'phone_verified_at', 'account_status'])
    
    def unverify_email(self, save: bool = True) -> None:
        """Mark email as unverified"""
        self.email_verified = False
        self.email_verified_at = None
        if save:
            self.save(update_fields=['email_verified', 'email_verified_at'])
    
    def unverify_phone(self, save: bool = True) -> None:
        """Mark phone as unverified"""
        self.phone_verified = False
        self.phone_verified_at = None
        if save:
            self.save(update_fields=['phone_verified', 'phone_verified_at'])
    
    # ================================================================
    # SECURITY METHODS
    # ================================================================
    
    def record_failed_login(self, save: bool = True) -> None:
        """Record a failed login attempt"""
        self.failed_login_attempts += 1
        
        # Lock account after 5 failed attempts for 30 minutes
        if self.failed_login_attempts >= 5:
            self.locked_until = timezone.now() + timedelta(minutes=30)
            self.account_status = self.AccountStatus.LOCKED
        
        if save:
            self.save(update_fields=['failed_login_attempts', 'locked_until', 'account_status'])
    
    def record_successful_login(
        self, 
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        save: bool = True
    ) -> None:
        """Record a successful login"""
        self.failed_login_attempts = 0
        self.locked_until = None
        self.last_login_ip = ip_address
        self.last_login_user_agent = user_agent
        
        # Unlock account if it was locked
        if self.account_status == self.AccountStatus.LOCKED:
            self.account_status = self.AccountStatus.ACTIVE
        
        if save:
            self.save(update_fields=[
                'failed_login_attempts', 
                'locked_until',
                'last_login_ip',
                'last_login_user_agent',
                'account_status'
            ])
    
    def unlock_account(self, save: bool = True) -> None:
        """Manually unlock a locked account"""
        self.locked_until = None
        self.failed_login_attempts = 0
        if self.account_status == self.AccountStatus.LOCKED:
            self.account_status = self.AccountStatus.ACTIVE
        if save:
            self.save(update_fields=['locked_until', 'failed_login_attempts', 'account_status'])
    
    def lock_account(self, duration_minutes: int = 30, save: bool = True) -> None:
        """Manually lock account for specified duration"""
        self.locked_until = timezone.now() + timedelta(minutes=duration_minutes)
        self.account_status = self.AccountStatus.LOCKED
        if save:
            self.save(update_fields=['locked_until', 'account_status'])
    
    def suspend_account(self, save: bool = True) -> None:
        """Suspend the account"""
        self.account_status = self.AccountStatus.SUSPENDED
        self.is_active = False
        if save:
            self.save(update_fields=['account_status', 'is_active'])
    
    def activate_account(self, save: bool = True) -> None:
        """Activate the account"""
        self.account_status = self.AccountStatus.ACTIVE
        self.is_active = True
        self.locked_until = None
        if save:
            self.save(update_fields=['account_status', 'is_active', 'locked_until'])
    
    def deactivate_account(self, save: bool = True) -> None:
        """Deactivate the account"""
        self.account_status = self.AccountStatus.INACTIVE
        self.is_active = False
        if save:
            self.save(update_fields=['account_status', 'is_active'])
    
    # ================================================================
    # MFA METHODS
    # ================================================================
    
    def enable_mfa(self, method: str = 'totp', secret: Optional[str] = None, save: bool = True) -> None:
        """Enable multi-factor authentication"""
        self.mfa_enabled = True
        self.mfa_method = method
        if secret:
            self.mfa_secret = secret
        if save:
            self.save(update_fields=['mfa_enabled', 'mfa_method', 'mfa_secret'])
    
    def disable_mfa(self, save: bool = True) -> None:
        """Disable multi-factor authentication"""
        self.mfa_enabled = False
        self.mfa_method = None
        self.mfa_secret = None
        if save:
            self.save(update_fields=['mfa_enabled', 'mfa_method', 'mfa_secret'])
    
    # ================================================================
    # GDPR METHODS
    # ================================================================
    
    def soft_delete(self, deleted_by_user=None, save: bool = True) -> None:
        """
        Soft delete the user account (GDPR compliant)
        Keeps the record but marks as deleted
        """
        self.deleted_at = timezone.now()
        self.deleted_by = deleted_by_user
        self.is_active = False
        self.account_status = self.AccountStatus.INACTIVE
        
        # Anonymize sensitive data
        self.email = None
        self.phone = None
        # Keep email_or_phone for referential integrity but could hash it
        
        if save:
            self.save()
    
    def restore(self, save: bool = True) -> None:
        """Restore a soft-deleted account"""
        self.deleted_at = None
        self.deleted_by = None
        self.is_active = True
        self.account_status = self.AccountStatus.ACTIVE
        if save:
            self.save()
    
    def accept_terms(self, save: bool = True) -> None:
        """Record terms and conditions acceptance"""
        self.terms_accepted = True
        self.terms_accepted_date = timezone.now()
        if save:
            self.save(update_fields=['terms_accepted', 'terms_accepted_date'])
    
    def accept_privacy(self, save: bool = True) -> None:
        """Record privacy policy acceptance"""
        self.privacy_accepted = True
        self.privacy_accepted_date = timezone.now()
        if save:
            self.save(update_fields=['privacy_accepted', 'privacy_accepted_date'])
    
    def give_marketing_consent(self, save: bool = True) -> None:
        """Give consent for marketing communications"""
        self.marketing_consent = True
        self.marketing_consent_date = timezone.now()
        if save:
            self.save(update_fields=['marketing_consent', 'marketing_consent_date'])
    
    def revoke_marketing_consent(self, save: bool = True) -> None:
        """Revoke marketing consent"""
        self.marketing_consent = False
        if save:
            self.save(update_fields=['marketing_consent'])
    
    def export_data(self) -> dict:
        """
        Export user data for GDPR compliance
        Returns a dictionary of all user data
        """
        data = {
            'uuid': str(self.uuid),
            'email': self.email,
            'phone': self.phone,
            'email_or_phone': self.email_or_phone,
            'role': self.role,
            'account_status': self.account_status,
            'email_verified': self.email_verified,
            'phone_verified': self.phone_verified,
            'timezone': self.timezone,
            'language': self.language,
            'country': self.country,
            'currency': self.currency,
            'marketing_consent': self.marketing_consent,
            'terms_accepted': self.terms_accepted,
            'privacy_accepted': self.privacy_accepted,
            'date_joined': self.date_joined.isoformat(),
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'metadata': self.metadata,
        }
        
        # Include profile data if exists
        if hasattr(self, 'profileinfo'):
            data['profile'] = {
                'name': self.profileinfo.profile_name,
                'gender': self.profileinfo.profile_gender,
                'dob': str(self.profileinfo.profile_dob) if self.profileinfo.profile_dob else None,
                'language': self.profileinfo.profile_language,
            }
        
        return data
    
    # ================================================================
    # PASSWORD METHODS
    # ================================================================
    
    def set_password(self, raw_password: str) -> None:
        """Override to track password change date"""
        super().set_password(raw_password)
        self.password_changed_at = timezone.now()
        self.require_password_change = False
    
    def force_password_change(self, save: bool = True) -> None:
        """Force user to change password on next login"""
        self.require_password_change = True
        if save:
            self.save(update_fields=['require_password_change'])
    
    # ================================================================
    # ROLE MANAGEMENT
    # ================================================================
    
    def change_role(self, new_role: str, save: bool = True) -> None:
        """
        Change user role with validation
        """
        if new_role not in dict(self.Role.choices):
            raise ValidationError(f"Invalid role: {new_role}")
        
        old_role = self.role
        self.role = new_role
        
        # Update permissions based on role
        if new_role == self.Role.ADMIN:
            self.is_staff = True
            self.is_superuser = True
        elif new_role == self.Role.STAFF:
            self.is_staff = True
            self.is_superuser = False
        else:
            # Regular users
            if old_role in [self.Role.ADMIN, self.Role.STAFF]:
                self.is_staff = False
                self.is_superuser = False
        
        if save:
            self.save()
    
    # ================================================================
    # HELPER METHODS
    # ================================================================
    
    def get_short_name(self) -> str:
        """Return short name for the user"""
        return self.display_name
    
    def get_full_name(self) -> str:
        """Return full name for the user"""
        return self.full_name
    
    def has_perm(self, perm, obj=None) -> bool:
        """
        Check if user has a specific permission
        Admins have all permissions
        """
        if self.is_active and self.is_superuser:
            return True
        return super().has_perm(perm, obj)
    
    def has_module_perms(self, app_label) -> bool:
        """
        Check if user has permissions to view the app
        Admins have all permissions
        """
        if self.is_active and self.is_superuser:
            return True
        return super().has_module_perms(app_label)
    
    def clean(self) -> None:
        """
        Validate model fields before saving
        """
        super().clean()
        
        # Ensure email_or_phone is either email or phone
        if '@' in self.email_or_phone:
            # If email_or_phone is email, copy to email field if empty
            if not self.email:
                self.email = self.email_or_phone
        else:
            # If email_or_phone is phone, copy to phone field if empty
            if not self.phone:
                self.phone = self.email_or_phone
        
        # Validate that superusers must have email
        if self.is_superuser and not self.email:
            raise ValidationError(_("Superusers must have an email address"))
    
    def save(self, *args, **kwargs):
        """
        Override save to run clean validation
        """
        self.full_clean()
        super().save(*args, **kwargs)