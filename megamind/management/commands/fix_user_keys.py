"""
Django Management Command: Fix Missing User Encryption Keys

File: megamind/management/commands/fix_user_keys.py

Usage:
    python manage.py fix_user_keys
    python manage.py fix_user_keys --dry-run
    python manage.py fix_user_keys --user user@example.com
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from megamind.models.engine_users import User, generate_user_encryption_key


class Command(BaseCommand):
    help = 'Fix users with missing encryption keys or JWT keys'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be fixed without making changes',
        )
        parser.add_argument(
            '--user',
            type=str,
            help='Fix a specific user by email_or_phone',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        specific_user = options.get('user')

        self.stdout.write(self.style.WARNING('=' * 70))
        self.stdout.write(self.style.WARNING('USER KEY REPAIR UTILITY'))
        self.stdout.write(self.style.WARNING('=' * 70))
        self.stdout.write('')

        if dry_run:
            self.stdout.write(self.style.NOTICE('🔍 DRY RUN MODE - No changes will be made'))
            self.stdout.write('')

        # Get users to fix
        if specific_user:
            try:
                users = [User.objects.get(email_or_phone=specific_user)]
                self.stdout.write(f"Checking user: {specific_user}")
            except User.DoesNotExist:
                raise CommandError(f"User '{specific_user}' not found")
        else:
            users = User.objects.all()
            self.stdout.write(f"Checking all {users.count()} users...\n")

        # Track statistics
        stats = {
            'total': 0,
            'missing_encryption': 0,
            'missing_jwt': 0,
            'fixed': 0,
            'errors': 0,
        }

        # Check and fix each user
        for user in users:
            stats['total'] += 1
            needs_fix = False
            issues = []

            # Check encryption key
            if not user.encryption_key:
                stats['missing_encryption'] += 1
                needs_fix = True
                issues.append('❌ Missing encryption key')

            # Check JWT keys
            if not user.jwt_public_key or not user.jwt_private_key_encrypted:
                stats['missing_jwt'] += 1
                needs_fix = True
                issues.append('❌ Missing JWT keys')

            # Display and fix if needed
            if needs_fix:
                self.stdout.write(
                    self.style.ERROR(f"\n🔴 User: {user.email_or_phone} ({user.user_uuid})")
                )
                for issue in issues:
                    self.stdout.write(f"   {issue}")

                if not dry_run:
                    try:
                        with transaction.atomic():
                            # Generate encryption key if missing
                            if not user.encryption_key:
                                user.encryption_key = generate_user_encryption_key()
                                self.stdout.write(
                                    self.style.SUCCESS('   ✅ Generated encryption key')
                                )

                            # Generate JWT keys if missing
                            if not user.jwt_public_key or not user.jwt_private_key_encrypted:
                                user._generate_jwt_keys()
                                self.stdout.write(
                                    self.style.SUCCESS('   ✅ Generated JWT keys')
                                )

                            # Save changes
                            user.save()
                            stats['fixed'] += 1
                            self.stdout.write(
                                self.style.SUCCESS(f'   ✅ User fixed successfully\n')
                            )

                    except Exception as e:
                        stats['errors'] += 1
                        self.stdout.write(
                            self.style.ERROR(f'   ❌ Error: {str(e)}\n')
                        )
                else:
                    self.stdout.write(
                        self.style.NOTICE('   ℹ️  Would fix in normal mode\n')
                    )

        # Display summary
        self.stdout.write('\n')
        self.stdout.write(self.style.WARNING('=' * 70))
        self.stdout.write(self.style.WARNING('SUMMARY'))
        self.stdout.write(self.style.WARNING('=' * 70))
        self.stdout.write(f"Total users checked: {stats['total']}")
        self.stdout.write(f"Users missing encryption keys: {stats['missing_encryption']}")
        self.stdout.write(f"Users missing JWT keys: {stats['missing_jwt']}")

        if dry_run:
            self.stdout.write(
                self.style.NOTICE(f"\n🔍 DRY RUN: {stats['missing_encryption'] + stats['missing_jwt']} users would be fixed")
            )
            self.stdout.write(
                self.style.NOTICE("Run without --dry-run to apply changes")
            )
        else:
            if stats['fixed'] > 0:
                self.stdout.write(
                    self.style.SUCCESS(f"\n✅ Successfully fixed {stats['fixed']} users")
                )
            if stats['errors'] > 0:
                self.stdout.write(
                    self.style.ERROR(f"❌ {stats['errors']} errors occurred")
                )
            if stats['fixed'] == 0 and stats['errors'] == 0:
                self.stdout.write(
                    self.style.SUCCESS("\n✅ All users have valid keys!")
                )

        self.stdout.write('')