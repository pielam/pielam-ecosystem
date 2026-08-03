"""
Hand-written to match the hardening of ``ForgotPasswordOTP``.

Widens ``reset_otp`` (it now holds a hex digest, not the code), adds the
attempt counter and the consumed marker, and drops any plaintext codes left
over from the old scheme -- they can never match a hash, and leaving live
codes sitting in the table is the thing this change exists to stop.
"""

from django.conf import settings
from django.db import migrations, models


def clear_legacy_plaintext_codes(apps, schema_editor):
    ForgotPasswordOTP = apps.get_model('kobutor', 'ForgotPasswordOTP')
    ForgotPasswordOTP.objects.exclude(reset_otp__isnull=True).update(
        reset_otp=None,
        reset_otp_created_at=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('kobutor', '0001_initial'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='forgotpasswordotp',
            options={
                'verbose_name': 'password reset code',
                'verbose_name_plural': 'password reset codes',
            },
        ),
        migrations.AlterField(
            model_name='forgotpasswordotp',
            name='reset_otp',
            field=models.CharField(
                blank=True, max_length=128, null=True,
                verbose_name='hashed reset code',
            ),
        ),
        migrations.AlterField(
            model_name='forgotpasswordotp',
            name='reset_otp_created_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='issued at'),
        ),
        migrations.AlterField(
            model_name='forgotpasswordotp',
            name='user',
            field=models.OneToOneField(
                on_delete=models.CASCADE,
                related_name='otp_info',
                to=settings.AUTH_USER_MODEL,
                verbose_name='user',
            ),
        ),
        migrations.AddField(
            model_name='forgotpasswordotp',
            name='attempts',
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text='Wrong guesses against the current code.',
                verbose_name='failed attempts',
            ),
        ),
        migrations.AddField(
            model_name='forgotpasswordotp',
            name='verified_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='Set once the code has been accepted; blocks reuse.',
                verbose_name='verified at',
            ),
        ),
        migrations.RunPython(
            clear_legacy_plaintext_codes,
            migrations.RunPython.noop,
        ),
    ]
