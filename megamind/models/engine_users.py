# """
# Production-ready User model with:
# - Email or Phone authentication
# - E2E encryption support
# - JWT key generation
# - Soft delete capability
# - Security features
# """
# import uuid
# from datetime import timedelta
# import secrets
# from datetime import timedelta

# from django.db import models
# from django.utils import timezone
# from django.conf import settings
# from django.core.exceptions import ValidationError
# from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin

# from cryptography.fernet import Fernet
# from cryptography.hazmat.primitives.asymmetric import rsa
# from cryptography.hazmat.primitives import serialization
# from cryptography.hazmat.backends import default_backend

# from megamind.validators.email_or_phone_validators import email_or_phone_validator, parse_identity
# from megamind.managers.users_manager import UserManager



# class User(AbstractBaseUser, PermissionsMixin):
#     """
#     Custom User model supporting email OR phone as primary identifier.
#     Includes E2E encryption and JWT key management.
#     """

#     # ========== PRIMARY KEY ==========
#     user_uuid = models.UUIDField(
#         primary_key=True,
#         default=uuid.uuid4,
#         editable=False,
#         db_index=True,
#         help_text="Immutable unique identifier"
#     )

#     # ========== IDENTITY FIELDS ==========
#     email_or_phone = models.CharField(
#         max_length=100,
#         unique=True,
#         validators=[email_or_phone_validator],
#         db_index=True,
#         help_text="Primary login: email or E.164 phone (e.g., +1234567890)"
#     )

#     identity_type = models.CharField(
#         max_length=10,
#         choices=[("email", "Email"), ("phone", "Phone")],
#         editable=False,
#         db_index=True,
#         help_text="Automatically inferred from email_or_phone"
#     )

    
#     # ========== ACCESS CONTROL ==========
#     ROLE_CHOICES = [
#         ("admin", "Administrator"),
#         ("user", " User"),
#     ]

#     role = models.CharField(
#         max_length=20,
#         choices=ROLE_CHOICES,
#         default="user",
#         db_index=True
#     )

#     is_active = models.BooleanField(default=True, db_index=True)
#     is_staff = models.BooleanField(default=False)

#     # Add these fields to your User model (after is_staff field)

#     # ========== VERIFICATION ==========
#     is_verified = models.BooleanField(default=False, db_index=True)  # Fixed typo from is_verfied
#     verification_token = models.CharField(max_length=255, blank=True, null=True)
#     verification_token_expires = models.DateTimeField(null=True, blank=True)

#     # ========== PASSWORD SECURITY ==========
#     password_reset_token = models.CharField(max_length=255, blank=True, null=True)
#     password_reset_expires = models.DateTimeField(null=True, blank=True)
#     password_expires_at = models.DateTimeField(
#         null=True,
#         blank=True,
#         help_text="Force password change after this date"
#     )

#     # ========== SOFT DELETE ==========
#     is_deleted = models.BooleanField(default=False, db_index=True)
#     deleted_at = models.DateTimeField(null=True, blank=True)
#     deletion_reason = models.CharField(max_length=255, blank=True, null=True)

#     # ========== SECURITY & TRACKING ==========
#     failed_login_attempts = models.PositiveSmallIntegerField(default=0)
#     locked_until = models.DateTimeField(null=True, blank=True)
#     lock_reason = models.CharField(max_length=255, blank=True, null=True)

#     last_login_at = models.DateTimeField(null=True, blank=True)
#     last_activity_at = models.DateTimeField(null=True, blank=True)
#     last_password_change = models.DateTimeField(null=True, blank=True)

#     # ========== TIMESTAMPS ==========
#     created_at = models.DateTimeField(default=timezone.now, db_index=True)
#     updated_at = models.DateTimeField(auto_now=True)

#     # ========== PREFERENCES ==========
#     user_timezone = models.CharField(max_length=50, default="UTC")

#     # ========== DJANGO AUTH RELATIONS ==========
#     groups = models.ManyToManyField(
#         "auth.Group",
#         related_name="custom_user_set",
#         blank=True,
#     )
#     user_permissions = models.ManyToManyField(
#         "auth.Permission",
#         related_name="custom_user_set",
#         blank=True,
#     )

#     objects = UserManager()


#     # ========== AUTH CONFIG ==========
#     USERNAME_FIELD = "email_or_phone"
#     REQUIRED_FIELDS = []

#     """
# Add these methods inside your User model class
# """


#     def save(self, *args, **kwargs):
#         """Override save to auto-detect identity_type"""
#         if not self.identity_type:
#             self.identity_type = parse_identity(self.email_or_phone)
        
#         # Set password change timestamp on first save or password change
#         if self.pk is None or self._password_has_changed():
#             self.last_password_change = timezone.now()
        
#         super().save(*args, **kwargs)

#     def _password_has_changed(self):
#         """Check if password was changed"""
#         if not self.pk:
#             return False
        
#         try:
#             old = User.objects.get(pk=self.pk)
#             return old.password != self.password
#         except User.DoesNotExist:
#             return False

#     # ========== ACCOUNT LOCKING ==========

#     def lock_account(self, duration_minutes=30, reason="Too many failed login attempts"):
#         """Lock user account temporarily"""
#         self.locked_until = timezone.now() + timedelta(minutes=duration_minutes)
#         self.lock_reason = reason
#         self.save(update_fields=["locked_until", "lock_reason", "updated_at"])

#     def unlock_account(self):
#         """Unlock user account"""
#         self.locked_until = None
#         self.lock_reason = None
#         self.failed_login_attempts = 0
#         self.save(update_fields=["locked_until", "lock_reason", "failed_login_attempts", "updated_at"])

#     def is_locked(self):
#         """Check if account is currently locked"""
#         if self.locked_until and self.locked_until > timezone.now():
#             return True
#         elif self.locked_until and self.locked_until <= timezone.now():
#             # Auto-unlock if lock period expired
#             self.unlock_account()
#         return False

#     def increment_failed_login(self, lock_threshold=5):
#         """Increment failed login attempts and lock if threshold reached"""
#         self.failed_login_attempts += 1
#         self.save(update_fields=["failed_login_attempts", "updated_at"])
        
#         if self.failed_login_attempts >= lock_threshold:
#             self.lock_account()

#     def reset_failed_login(self):
#         """Reset failed login attempts on successful login"""
#         self.failed_login_attempts = 0
#         self.last_login_at = timezone.now()
#         self.save(update_fields=["failed_login_attempts", "last_login_at", "updated_at"])

#     # ========== SOFT DELETE ==========

#     def soft_delete(self, reason="User requested deletion"):
#         """Soft delete user account"""
#         self.is_deleted = True
#         self.deleted_at = timezone.now()
#         self.deletion_reason = reason
#         self.is_active = False
#         self.save(update_fields=["is_deleted", "deleted_at", "deletion_reason", "is_active", "updated_at"])

#     def restore(self):
#         """Restore soft-deleted account"""
#         self.is_deleted = False
#         self.deleted_at = None
#         self.deletion_reason = None
#         self.is_active = True
#         self.save(update_fields=["is_deleted", "deleted_at", "deletion_reason", "is_active", "updated_at"])

#         """
#     Add these verification and password reset methods to your User model
#     """

#     # ========== VERIFICATION ==========

#     def generate_verification_token(self, expiry_hours=24):
#         """Generate email/phone verification token"""
#         self.verification_token = secrets.token_urlsafe(32)
#         self.verification_token_expires = timezone.now() + timedelta(hours=expiry_hours)
#         self.save(update_fields=["verification_token", "verification_token_expires", "updated_at"])
#         return self.verification_token

#     def verify_account(self, token):
#         """Verify user account with token"""
#         if not self.verification_token:
#             raise ValidationError("No verification token found")
        
#         if self.verification_token_expires < timezone.now():
#             raise ValidationError("Verification token expired")
        
#         if self.verification_token != token:
#             raise ValidationError("Invalid verification token")
        
#         self.is_verified = True
#         self.verification_token = None
#         self.verification_token_expires = None
#         self.save(update_fields=["is_verified", "verification_token", "verification_token_expires", "updated_at"])
#         return True

#     def is_verification_expired(self):
#         """Check if verification token is expired"""
#         if not self.verification_token_expires:
#             return True
#         return self.verification_token_expires < timezone.now()

#     # ========== PASSWORD RESET ==========

#     def generate_password_reset_token(self, expiry_hours=1):
#         """Generate password reset token"""
#         self.password_reset_token = secrets.token_urlsafe(32)
#         self.password_reset_expires = timezone.now() + timedelta(hours=expiry_hours)
#         self.save(update_fields=["password_reset_token", "password_reset_expires", "updated_at"])
#         return self.password_reset_token

#     def verify_password_reset_token(self, token):
#         """Verify password reset token is valid"""
#         if not self.password_reset_token:
#             return False
        
#         if self.password_reset_expires < timezone.now():
#             return False
        
#         return self.password_reset_token == token

#     def reset_password(self, token, new_password):
#         """Reset password with valid token"""
#         if not self.verify_password_reset_token(token):
#             raise ValidationError("Invalid or expired reset token")
        
#         self.set_password(new_password)
#         self.password_reset_token = None
#         self.password_reset_expires = None
#         self.last_password_change = timezone.now()
#         self.save(update_fields=[
#             "password", 
#             "password_reset_token", 
#             "password_reset_expires", 
#             "last_password_change",
#             "updated_at"
#         ])
#         return True

#     def is_password_expired(self, max_age_days=90):
#         """Check if password needs to be changed"""
#         if not self.last_password_change:
#             return False
        
#         if self.password_expires_at:
#             return timezone.now() > self.password_expires_at
        
#         # Default: expire after max_age_days
#         expiry = self.last_password_change + timedelta(days=max_age_days)
#         return timezone.now() > expiry

#     """
#     Add these utility methods and properties to your User model
#     """

#     # ========== PROPERTIES ==========

#     @property
#     def is_email_user(self):
#         """Check if user uses email for authentication"""
#         return self.identity_type == "email"

#     @property
#     def is_phone_user(self):
#         """Check if user uses phone for authentication"""
#         return self.identity_type == "phone"

#     @property
#     def display_identity(self):
#         """Get masked identity for display (e.g., a***@email.com or +1***567890)"""
#         if self.is_email_user:
#             parts = self.email_or_phone.split('@')
#             if len(parts) == 2:
#                 username = parts[0]
#                 domain = parts[1]
#                 masked = username[0] + '*' * (len(username) - 1) + '@' + domain
#                 return masked
#         elif self.is_phone_user:
#             phone = self.email_or_phone
#             if len(phone) > 6:
#                 return phone[:3] + '*' * (len(phone) - 6) + phone[-3:]
        
#         return self.email_or_phone

#     @property
#     def is_admin(self):
#         """Check if user has admin role"""
#         return self.role == "admin"

#     # ========== ACTIVITY TRACKING ==========

#     def update_last_activity(self):
#         """Update last activity timestamp"""
#         self.last_activity_at = timezone.now()
#         self.save(update_fields=["last_activity_at", "updated_at"])

#     def is_inactive(self, days=30):
#         """Check if user has been inactive for specified days"""
#         if not self.last_activity_at:
#             return False
        
#         inactive_threshold = timezone.now() - timedelta(days=days)
#         return self.last_activity_at < inactive_threshold

#     # ========== ROLE MANAGEMENT ==========

#     def make_admin(self):
#         """Promote user to admin"""
#         self.role = "admin"
#         self.is_staff = True
#         self.save(update_fields=["role", "is_staff", "updated_at"])

#     def remove_admin(self):
#         """Demote admin to regular user"""
#         self.role = "user"
#         self.is_staff = False
#         self.save(update_fields=["role", "is_staff", "updated_at"])

#     # ========== VALIDATION ==========

#     def can_login(self):
#         """Check if user can login (comprehensive check)"""
#         if not self.is_active:
#             return False, "Account is inactive"
        
#         if self.is_deleted:
#             return False, "Account has been deleted"
        
#         if self.is_locked():
#             return False, f"Account is locked until {self.locked_until}"
        
#         if not self.is_verified:
#             return False, "Account is not verified"
        
#         return True, "OK"

#     # ========== SECURITY INFO ==========

#     def get_security_summary(self):
#         """Get security status summary for user"""
#         return {
#             "is_verified": self.is_verified,
#             "is_locked": self.is_locked(),
#             "failed_attempts": self.failed_login_attempts,
#             "last_login": self.last_login_at,
#             "last_activity": self.last_activity_at,
#             "password_age_days": (timezone.now() - self.last_password_change).days if self.last_password_change else None,
#             "account_age_days": (timezone.now() - self.created_at).days,
#         }

#     """
#     Replace your existing Meta class with this updated version
#     """

#     class Meta:
#         db_table = "users"
#         verbose_name = "User"
#         verbose_name_plural = "Users"

#         indexes = [
#             models.Index(fields=["email_or_phone"]),
#             models.Index(fields=["identity_type"]),
#             models.Index(fields=["role"]),
#             models.Index(fields=["is_active", "is_deleted"]),
#             models.Index(fields=["is_verified"]),  # Added for verification queries
#             models.Index(fields=["created_at"]),
#             models.Index(fields=["last_activity_at"]),  # For inactive user queries
#             models.Index(fields=["locked_until"]),  # For account lock checks
#             models.Index(fields=["verification_token"]),  # For verification lookups
#             models.Index(fields=["password_reset_token"]),  # For reset lookups
#         ]

#         constraints = [
#             models.CheckConstraint(
#                 condition=~models.Q(email_or_phone=""),
#                 name="email_or_phone_not_empty"
#             ),
#             # Ensure verification token has expiry if set
#             models.CheckConstraint(
#                 condition=(
#                     models.Q(verification_token__isnull=True) |
#                     models.Q(verification_token_expires__isnull=False)
#                 ),
#                 name="verification_token_has_expiry"
#             ),
#             # Ensure password reset token has expiry if set
#             models.CheckConstraint(
#                 condition=(
#                     models.Q(password_reset_token__isnull=True) |
#                     models.Q(password_reset_expires__isnull=False)
#                 ),
#                 name="password_reset_token_has_expiry"
#             ),
#         ]