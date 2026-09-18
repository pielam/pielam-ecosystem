from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

User = get_user_model()


from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

User = get_user_model()

# apps/customer/serializers/account_serializers.py
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.customer.validators import email_or_phone_validator

User = get_user_model()




class UserLoginSerializer(serializers.Serializer):
    """
    Basic login serializer using email/phone + password.
    Useful if you build a custom token view.
    """
    email_or_phone = serializers.CharField(required=True)
    password = serializers.CharField(write_only=True)

    # Actual authentication logic will be in the view.
