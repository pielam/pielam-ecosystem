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

class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["email_or_phone", "password", "role"]

    def validate_email_or_phone(self, value):
        # Use our centralized validator which raises Django ValidationError
        try:
            email_or_phone_validator(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages)
        # Uniqueness check
        if User.objects.filter(email_or_phone=value).exists():
            raise serializers.ValidationError("A user with this email or phone already exists.")
        return value

    # Optionally add password validation (if you have a custom strong_password_validator,
    # call it here similar to above).
    def validate_password(self, value):
        # keep your strong password rules here or call your existing validator:
        from apps.customer.validators import strong_password_validator
        try:
            strong_password_validator(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.messages)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, **validated_data)
        return user
