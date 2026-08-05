from django.contrib import admin
from megamind.models.connected_service import ConnectedService


@admin.register(ConnectedService)
class ConnectedServiceAdmin(admin.ModelAdmin):
    
    # Columns shown in list view
    list_display = (
        'id',
        'service_name',
        'user',
        'service_type',
        'status',
        'is_connected',
        'fetch_status',
        'last_fetch_time',
        'created_at',
    )
    
    # Filters on right sidebar
    list_filter = (
        'service_type',
        'status',
        'is_connected',
        'fetch_status',
        'created_at',
    )
    
    # Search functionality
    search_fields = (
        'service_name',
        'service_url',
        'user__username',
        'user__email',
    )
    
    # Readonly fields (prevent accidental edits)
    readonly_fields = (
        'last_fetched_data',
        'last_fetch_time',
        'fetch_status',
        'fetch_error',
        'created_at',
        'updated_at',
    )
    
    # Field grouping (better UI)
    fieldsets = (
        ("Basic Info", {
            'fields': (
                'user',
                'profile',
                'service_name',
                'service_url',
                'service_type',
                'status',
                'is_connected',
            )
        }),
        
        ("Authentication", {
            'classes': ('collapse',),
            'fields': (
                'api_key',
                'auth_token',
            )
        }),
        
        ("Fetch Data", {
            'classes': ('collapse',),
            'fields': (
                'last_fetched_data',
                'last_fetch_time',
                'fetch_status',
                'fetch_error',
            )
        }),
        
        ("Timestamps", {
            'fields': (
                'created_at',
                'updated_at',
            )
        }),
    )
    
    # Pagination
    list_per_page = 25
    
    # Default ordering
    ordering = ('-created_at',)