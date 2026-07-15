from django.db import models
from apps.customer.models.account import User
from apps.customer.models.profile_info import ProfileInfo


class ConnectedService(models.Model):
    SERVICE_TYPES = [
        ('education', 'Education'),
        ('product', 'Product'),
        ('brand', 'Brand'),
        ('category', 'Category'),
        ('subcategory', 'SubCategory'),  # ← new
        ('news', 'News'),
        ('location', 'Location'),
        ('api', 'API Endpoint'),
        ('person', 'Person'),
        ('business', 'Business'),
        ('entertainment', 'Entertainment'),
    ]

    STATUS_CHOICES = [
        ('private', 'Private'),
        ('public', 'Public'),
    ]

    FETCH_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('error', 'Error'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='connected_services_user')
    profile = models.ForeignKey(ProfileInfo, blank=True, null=True, on_delete=models.CASCADE, related_name='connected_services_profile')
    service_name = models.CharField(max_length=200)
    service_url = models.URLField(max_length=500)
    service_type = models.CharField(max_length=50, choices=SERVICE_TYPES, default='product')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='private')
    is_connected = models.BooleanField(default=False)

    # API authentication fields (optional)
    api_key = models.CharField(max_length=500, blank=True, null=True)
    auth_token = models.TextField(blank=True, null=True)

    # --- OG / Meta fields ---
    og_title = models.CharField(max_length=500, blank=True, null=True)
    og_description = models.TextField(blank=True, null=True)
    og_thumbnail = models.URLField(max_length=500, blank=True, null=True)  # og:image URL
    og_site_name = models.CharField(max_length=200, blank=True, null=True)
    og_type = models.CharField(max_length=100, blank=True, null=True)      # website, article, video, etc.

    # --- Extracted media & content ---
    # Lists stored as JSON arrays of URLs / strings
    extracted_images = models.JSONField(default=list, blank=True)   # [{"url": ..., "alt": ...}, ...]
    extracted_videos = models.JSONField(default=list, blank=True)   # [{"url": ..., "type": ...}, ...]
    extracted_links = models.JSONField(default=list, blank=True)    # [{"href": ..., "text": ...}, ...]
    extracted_text = models.TextField(blank=True, null=True)        # main visible text content

    # --- Raw cache (full scrape dump) ---
    last_fetched_data = models.JSONField(null=True, blank=True)
    last_fetch_time = models.DateTimeField(null=True, blank=True)
    fetch_status = models.CharField(max_length=50, choices=FETCH_STATUS_CHOICES, default='pending')
    fetch_error = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    cached_feed_payload = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.service_name} - {self.user.email_or_phone}"