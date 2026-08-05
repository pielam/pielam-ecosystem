

import re
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.customer.validators import strong_password_validator

User = get_user_model()

class ResetPasswordSerializer(serializers.Serializer):
    new_password = serializers.CharField(
        write_only=True,
        validators=[strong_password_validator]
    )

    def save(self):
        user = self.context.get('user')
        new_password = self.validated_data['new_password']

        user.set_password(new_password)
        user.save()
        return user
