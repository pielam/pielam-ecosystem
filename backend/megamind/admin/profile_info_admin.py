# # apps/your_app/admin/profile_info_admin.py

# from django.contrib import admin
# from django.utils.html import format_html
# from megamind.models.profile_info import ProfileInfo


# @admin.register(ProfileInfo)
# class ProfileInfoAdmin(admin.ModelAdmin):
#     list_display = (
#         'profile_name',
#         'user',
#         'profile_type',
#         'visibility_badge',
#         'verification_badge',
#         'profile_creation_time',
#     )

#     list_filter = (
#         'profile_type',
#         'is_profile_public',
#         'is_profile_verified',
#         'is_profile_suspended',
#         'profile_language',
#     )

#     search_fields = (
#         'profile_name',
#         'user__email',
#         'user__phone',
#     )

#     readonly_fields = (
#         'profile_creation_time',
#         'profile_updated_time',
#         'follower_count',
#         'subscriber_count',
#     )

#     fieldsets = (
#         ('Basic Info', {
#             'fields': (
#                 'user',
#                 'profile_name',
#                 'profile_type',
#                 'profile_bio',
#             )
#         }),
#         ('Media', {
#             'fields': (
#                 'profile_photo',
#                 'profile_cover_photo',
#             )
#         }),
#         ('Personal Info', {
#             'fields': (
#                 'profile_gender',
#                 'profile_dob',
#                 'profile_language',
#             )
#         }),
#         ('Status & Visibility', {
#             'fields': (
#                 'is_profile_public',
#                 'is_profile_verified',
#                 'is_profile_featured',
#                 'is_profile_archived',
#                 'is_profile_suspended',
#             )
#         }),
#         ('Statistics', {
#             'fields': (
#                 'follower_count',
#                 'subscriber_count',
#             )
#         }),
#         ('Timestamps', {
#             'fields': (
#                 'profile_creation_time',
#                 'profile_updated_time',
#             )
#         }),
#     )

#     # ===============================
#     # Custom Admin Display Methods
#     # ===============================

#     @admin.display(description='Visibility')
#     def visibility_badge(self, obj):
#         if obj.is_profile_public:
#             return format_html(
#                 "<span style='color: green; font-weight: bold;'>{}</span>",
#                 "Public"
#             )
#         return format_html(
#             "<span style='color: red; font-weight: bold;'>{}</span>",
#             "Private"
#         )

#     @admin.display(description='Verified')
#     def verification_badge(self, obj):
#         if obj.is_profile_verified:
#             return format_html(
#                 "<span style='color: #0d6efd; font-weight: bold;'>{}</span>",
#                 "✔ Verified"
#             )
#         return format_html(
#             "<span style='color: gray;'>{}</span>",
#             "✖ Unverified"
#         )
