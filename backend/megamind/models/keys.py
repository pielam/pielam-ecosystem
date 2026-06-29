# apps/your_app/models/keys.py

from django.db import models
from apps.customer.models.account import User  # Adjust import path as needed


class UserKey(models.Model):
    """
    Secure storage for user-specific cryptographic keys.
    All sensitive keys are stored encrypted at rest.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='keys',
        help_text="Associated user for these keys"
    )

    # ========== USER-SPECIFIC SYMMETRIC ENCRYPTION KEY ==========
    encryption_key = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        help_text="User-specific Fernet encryption key (auto-generated, hex-encoded)"
    )

    # ========== END-TO-END (E2E) ASYMMETRIC KEYS ==========
    public_key = models.TextField(
        null=True,
        blank=True,
        help_text="User's public key for E2E encryption (PEM or OpenSSH format)"
    )
    private_key_encrypted = models.TextField(
        null=True,
        blank=True,
        help_text="User's private key encrypted with user's password-derived key"
    )

    # ========== JWT SIGNING KEYS (RSA) ==========
    jwt_private_key_encrypted = models.TextField(
        null=True,
        blank=True,
        help_text="Encrypted RSA private key for JWT signing (PEM format)"
    )
    jwt_public_key = models.TextField(
        null=True,
        blank=True,
        help_text="RSA public key for JWT verification (PEM format)"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'user_keys'
        verbose_name = 'User Key'
        verbose_name_plural = 'User Keys'

    def __str__(self):
        return f"Keys for {self.user}"

    @property
    def has_e2e_keys(self):
        return bool(self.public_key and self.private_key_encrypted)

    @property
    def has_jwt_keys(self):
        return bool(self.jwt_public_key and self.jwt_private_key_encrypted)

    @property
    def has_symmetric_key(self):
        return bool(self.encryption_key)