# # megamind/models/profile_info.py

# from django.db import models
# from django.core.validators import MinLengthValidator
# from apps.customer.models.account import User  # Adjust import path as needed


# class ProfileInfo(models.Model):
#     # ========== PROFILE TYPE CHOICES ==========
#     PROFILE_TYPE_CHOICES = [
#         ('Person', 'Person'),
#         ('Book', 'Book'),
#         ('Academy', 'Academy'),
#         ('Business', 'Business'),
#     ]

#     GENDER_CHOICES = [
#         ('Male', 'Male'),
#         ('Female', 'Female'),
#         ('Other', 'Other'),
#     ]

#     user = models.OneToOneField(
#         User,
#         on_delete=models.CASCADE,
#         primary_key=True,
#         help_text="Associated user account"
#     )

#     # Basic profile fields
#     profile_name = models.CharField(
#         max_length=70,
#         blank=True,
#         default="Anonymous",
#         help_text="Display name (not unique, can be changed)"
#     )

#     profile_type = models.CharField(
#         max_length=70,
#         choices=PROFILE_TYPE_CHOICES,
#         blank=True,
#         null=True
#     )

#     profile_bio = models.TextField(
#         max_length=101,
#         blank=True,
#         null=True,
#         help_text="Short public description"
#     )

#     # Media
#     profile_photo = models.ImageField(
#         upload_to='profile-pics/',
#         blank=True,
#         null=True
#     )
#     profile_cover_photo = models.ImageField(
#         upload_to='cover-pics/',
#         blank=True,
#         null=True
#     )

#     # Personal info
#     profile_gender = models.CharField(
#         max_length=10,
#         choices=GENDER_CHOICES,
#         blank=True,
#         null=True
#     )
#     profile_dob = models.DateField(
#         blank=True,
#         null=True,
#         help_text="Date of birth"
#     )
#     profile_language = models.CharField(
#         max_length=50,
#         blank=True,
#         null=True,
#         help_text="Preferred language"
#     )

#     # Timestamps
#     profile_creation_time = models.DateTimeField(auto_now_add=True)
#     profile_updated_time = models.DateTimeField(auto_now=True)

#     # Visibility & status flags
#     is_profile_public = models.BooleanField(default=True)
#     is_profile_verified = models.BooleanField(default=False)
#     is_profile_featured = models.BooleanField(default=False)
#     is_profile_archived = models.BooleanField(default=False)
#     is_profile_suspended = models.BooleanField(default=False)

#     # Relationships
#     followers = models.ManyToManyField(
#         User,
#         related_name='following_profiles',  # User.following_profiles.all()
#         blank=True,
#         help_text="Users who follow this profile"
#     )
#     subscribers = models.ManyToManyField(
#         User,
#         related_name='subscribed_profiles',  # User.subscribed_profiles.all()
#         blank=True,
#         help_text="Users subscribed to this profile"
#     )

#     class Meta:
#         db_table = 'user_profiles'
#         verbose_name = 'Profile Info'
#         verbose_name_plural = 'Profile Infos'
#         ordering = ['-profile_creation_time']
#         indexes = [
#             models.Index(fields=['profile_type']),
#             models.Index(fields=['is_profile_public']),
#             models.Index(fields=['is_profile_verified']),
#             models.Index(fields=['profile_language']),
#         ]

#     def __str__(self):
#         return f"{self.profile_name} ({self.user})"

#     @property
#     def follower_count(self):
#         return self.followers.count()

#     @property
#     def subscriber_count(self):
#         return self.subscribers.count()

#     def archive(self):
#         """Archive the profile (soft delete)"""
#         self.is_profile_archived = True
#         self.save(update_fields=['is_profile_archived'])

#     def suspend(self):
#         """Suspend the profile"""
#         self.is_profile_suspended = True
#         self.save(update_fields=['is_profile_suspended'])