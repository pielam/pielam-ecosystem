"""
Custom User Manager for User model
- Email or Phone based user creation
- Active/Deleted user filtering
- Secure user management
"""
from django.contrib.auth.models import BaseUserManager
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class UserQuerySet(models.QuerySet):
    """Custom QuerySet for User model with filtering methods"""

    def active(self):
        """Return only active, non-deleted users"""
        return self.filter(is_active=True, is_deleted=False)

    def inactive(self):
        """Return only inactive users"""
        return self.filter(is_active=False)

    def deleted(self):
        """Return only soft-deleted users"""
        return self.filter(is_deleted=True)

    def not_deleted(self):
        """Return only non-deleted users (regardless of active status)"""
        return self.filter(is_deleted=False)

    def verified(self):
        """Return users with verified accounts"""
        return self.filter(is_verified=True)

    def unverified(self):
        """Return users with unverified accounts"""
        return self.filter(is_verified=False)

    def by_role(self, role):
        """Filter users by role"""
        return self.filter(role=role)

    def admins(self):
        """Return only admin users"""
        return self.filter(role="admin")

    def regular_users(self):
        """Return only regular users"""
        return self.filter(role="user")

    def locked(self):
        """Return currently locked accounts"""
        now = timezone.now()
        return self.filter(locked_until__gt=now)

    def unlocked(self):
        """Return unlocked accounts"""
        now = timezone.now()
        return self.filter(
            models.Q(locked_until__isnull=True) |
            models.Q(locked_until__lte=now)
        )

    def email_users(self):
        """Return users who registered with email"""
        return self.filter(identity_type="email")

    def phone_users(self):
        """Return users who registered with phone"""
        return self.filter(identity_type="phone")

    def created_after(self, date):
        """Return users created after a specific date"""
        return self.filter(created_at__gte=date)

    def created_before(self, date):
        """Return users created before a specific date"""
        return self.filter(created_at__lte=date)

    def last_login_after(self, date):
        """Return users who logged in after a specific date"""
        return self.filter(last_login_at__gte=date)

    def last_login_before(self, date):
        """Return users who logged in before a specific date"""
        return self.filter(last_login_at__lte=date)

    def inactive_for_days(self, days):
        """Return users inactive for more than specified days"""
        cutoff_date = timezone.now() - timezone.timedelta(days=days)
        return self.filter(
            models.Q(last_activity_at__lt=cutoff_date) |
            models.Q(last_activity_at__isnull=True)
        )


class UserManager(BaseUserManager):
    """Custom manager for User model"""

    def get_queryset(self):
        """Return custom QuerySet"""
        return UserQuerySet(self.model, using=self._db)

    # ========== REQUIRED METHODS FOR DJANGO AUTH ==========

    def get_by_natural_key(self, email_or_phone):
        """
        Required by Django for authentication.
        Retrieves user by their natural key (email_or_phone).
        """
        return self.get(**{self.model.USERNAME_FIELD: email_or_phone})

    def normalize_email(self, email):
        """
        Normalize email address by lowercasing the domain part.
        """
        email = email or ''
        try:
            email_name, domain_part = email.strip().rsplit('@', 1)
        except ValueError:
            pass
        else:
            email = email_name.lower() + '@' + domain_part.lower()
        return email

    # ========== QuerySet method proxies ==========

    def active(self):
        """Return only active, non-deleted users"""
        return self.get_queryset().active()

    def inactive(self):
        """Return only inactive users"""
        return self.get_queryset().inactive()

    def deleted(self):
        """Return only soft-deleted users"""
        return self.get_queryset().deleted()

    def not_deleted(self):
        """Return only non-deleted users"""
        return self.get_queryset().not_deleted()

    def verified(self):
        """Return users with verified primary identity"""
        return self.get_queryset().verified()

    def unverified(self):
        """Return users with unverified primary identity"""
        return self.get_queryset().unverified()

    def by_role(self, role):
        """Filter users by role"""
        return self.get_queryset().by_role(role)

    def admins(self):
        """Return only admin users"""
        return self.get_queryset().admins()

    def regular_users(self):
        """Return only regular users"""
        return self.get_queryset().regular_users()

    def locked(self):
        """Return currently locked accounts"""
        return self.get_queryset().locked()

    def unlocked(self):
        """Return unlocked accounts"""
        return self.get_queryset().unlocked()

    def email_users(self):
        """Return users who registered with email"""
        return self.get_queryset().email_users()

    def phone_users(self):
        """Return users who registered with phone"""
        return self.get_queryset().phone_users()

    # ========== User creation methods ==========

    def _create_user(self, email_or_phone, password, **extra_fields):
        """
        Internal method to create and save a user.
        
        Args:
            email_or_phone: Email address or phone number
            password: User password
            **extra_fields: Additional user fields
            
        Returns:
            User: Created user instance
            
        Raises:
            ValueError: If email_or_phone is not provided
        """
        if not email_or_phone:
            raise ValueError("The email_or_phone field must be set")

        # Normalize email if it's an email
        if '@' in email_or_phone:
            email_or_phone = self.normalize_email(email_or_phone)

        # Create user instance (validation happens in model.save())
        user = self.model(email_or_phone=email_or_phone, **extra_fields)
        
        # Set password
        user.set_password(password)
        
        # Save user (this will trigger validation, normalization, and key generation)
        user.save(using=self._db)
        
        return user

    def create_user(self, email_or_phone, password=None, **extra_fields):
        """
        Create and save a regular user.
        
        Args:
            email_or_phone: Email address or phone number
            password: User password (optional)
            **extra_fields: Additional user fields
            
        Returns:
            User: Created user instance
            
        Examples:
            # Create user with email
            user = User.objects.create_user(
                email_or_phone='user@example.com',
                password='securepass123'
            )
            
            # Create user with phone
            user = User.objects.create_user(
                email_or_phone='+12025551234',
                password='securepass123'
            )
        """
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        extra_fields.setdefault('role', 'user')
        extra_fields.setdefault('is_verified', False)
        
        return self._create_user(email_or_phone, password, **extra_fields)

    def create_superuser(self, email_or_phone, password=None, **extra_fields):
        """
        Create and save a superuser.
        
        Args:
            email_or_phone: Email address or phone number
            password: User password
            **extra_fields: Additional user fields
            
        Returns:
            User: Created superuser instance
            
        Raises:
            ValueError: If is_staff or is_superuser is not True
            
        Examples:
            # Create superuser with email
            admin = User.objects.create_superuser(
                email_or_phone='admin@example.com',
                password='adminpass123'
            )
        """
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'admin')
        extra_fields.setdefault('is_verified', True)  # Superusers are auto-verified

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self._create_user(email_or_phone, password, **extra_fields)

    # ========== User lookup methods ==========
    

    def get_by_email_or_phone(self, identifier):
        """
        Get user by email or phone (primary identifier).
        
        Args:
            identifier: Email or phone number
            
        Returns:
            User: User instance or None
        """
        identifier = identifier.strip()
        try:
            return self.get(email_or_phone=identifier)
        except self.model.DoesNotExist:
            return None

    def exists_by_email_or_phone(self, identifier):
        """
        Check if user exists with given email or phone.
        
        Args:
            identifier: Email or phone number
            
        Returns:
            bool: True if user exists
        """
        return self.filter(email_or_phone=identifier).exists()

    # ========== Bulk operations ==========

    def bulk_activate(self, user_ids):
        """
        Activate multiple users by their UUIDs.
        
        Args:
            user_ids: List of user UUIDs
            
        Returns:
            int: Number of users activated
        """
        return self.filter(user_uuid__in=user_ids).update(
            is_active=True,
            updated_at=timezone.now()
        )

    def bulk_deactivate(self, user_ids):
        """
        Deactivate multiple users by their UUIDs.
        
        Args:
            user_ids: List of user UUIDs
            
        Returns:
            int: Number of users deactivated
        """
        return self.filter(user_uuid__in=user_ids).update(
            is_active=False,
            updated_at=timezone.now()
        )

    def bulk_soft_delete(self, user_ids, reason=None):
        """
        Soft delete multiple users by their UUIDs.
        
        Args:
            user_ids: List of user UUIDs
            reason: Deletion reason (optional)
            
        Returns:
            int: Number of users deleted
        """
        return self.filter(user_uuid__in=user_ids).update(
            is_deleted=True,
            deleted_at=timezone.now(),
            deletion_reason=reason,
            is_active=False,
            updated_at=timezone.now()
        )

    def bulk_restore(self, user_ids):
        """
        Restore multiple soft-deleted users by their UUIDs.
        
        Args:
            user_ids: List of user UUIDs
            
        Returns:
            int: Number of users restored
        """
        return self.filter(user_uuid__in=user_ids).update(
            is_deleted=False,
            deleted_at=None,
            deletion_reason=None,
            is_active=True,
            updated_at=timezone.now()
        )

    # ========== Statistics methods ==========

    def count_by_role(self):
        """
        Get count of users by role.
        
        Returns:
            dict: Role counts {'admin': 5, 'user': 100}
        """
        from django.db.models import Count
        
        result = self.values('role').annotate(count=Count('role'))
        return {item['role']: item['count'] for item in result}

    def count_by_identity_type(self):
        """
        Get count of users by identity type.
        
        Returns:
            dict: Identity type counts {'email': 80, 'phone': 20}
        """
        from django.db.models import Count
        
        result = self.values('identity_type').annotate(count=Count('identity_type'))
        return {item['identity_type']: item['count'] for item in result}

    def registration_stats(self, days=30):
        """
        Get registration statistics for the last N days.
        
        Args:
            days: Number of days to look back
            
        Returns:
            dict: Registration stats
        """
        from django.db.models import Count
        from django.db.models.functions import TruncDate
        
        cutoff_date = timezone.now() - timezone.timedelta(days=days)
        
        registrations = self.filter(
            created_at__gte=cutoff_date
        ).annotate(
            date=TruncDate('created_at')
        ).values('date').annotate(
            count=Count('user_uuid')
        ).order_by('date')
        
        return {
            'total': self.filter(created_at__gte=cutoff_date).count(),
            'by_date': list(registrations)
        }

    def verification_rate(self):
        """
        Get verification rate statistics.
        
        Returns:
            dict: Verification statistics
        """
        total = self.count()
        verified = self.verified().count()
        
        return {
            'total': total,
            'verified': verified,
            'unverified': total - verified,
            'rate': (verified / total * 100) if total > 0 else 0
        }

    # ========== Cleanup methods ==========

    def cleanup_unverified(self, days=30):
        """
        Soft delete unverified users older than N days.
        
        Args:
            days: Age threshold in days
            
        Returns:
            int: Number of users deleted
        """
        cutoff_date = timezone.now() - timezone.timedelta(days=days)
        
        users_to_delete = self.unverified().filter(
            created_at__lt=cutoff_date,
            is_deleted=False
        )
        
        count = users_to_delete.count()
        
        users_to_delete.update(
            is_deleted=True,
            deleted_at=timezone.now(),
            deletion_reason=f"Unverified for {days} days",
            is_active=False,
            updated_at=timezone.now()
        )
        
        return count

    def cleanup_inactive(self, days=365):
        """
        Soft delete inactive users older than N days.
        
        Args:
            days: Inactivity threshold in days
            
        Returns:
            int: Number of users deleted
        """
        users_to_delete = self.get_queryset().inactive_for_days(days).filter(is_deleted=False)
        
        count = users_to_delete.count()
        
        users_to_delete.update(
            is_deleted=True,
            deleted_at=timezone.now(),
            deletion_reason=f"Inactive for {days} days",
            is_active=False,
            updated_at=timezone.now()
        )
        
        return count

    def permanently_delete_soft_deleted(self, days=90):
        """
        Permanently delete users that have been soft-deleted for N days.
        WARNING: This is irreversible!
        
        Args:
            days: Age threshold in days since soft deletion
            
        Returns:
            int: Number of users permanently deleted
        """
        cutoff_date = timezone.now() - timezone.timedelta(days=days)
        
        users_to_delete = self.filter(
            is_deleted=True,
            deleted_at__lt=cutoff_date
        )
        
        count = users_to_delete.count()
        users_to_delete.delete()  # Permanent deletion
        
        return count