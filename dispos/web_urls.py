"""
dispos/web_urls.py
Complete URL configuration for PIELAM identity and profile management
"""

from django.urls import path

# Profile Views
from engine.business_engine.create_business_profile import CreateBusinessProfileView
from engine.business_engine.business_profile_edit import EditBusinessProfileView
from engine.business_engine.business_profile import BusinessProfileView

from engine.business_engine.personal_engine import (
    EngineView,
    PersonalEngineView,
    engine_feed_api,
)
from engine.profile_views.personal_profile import ProfileView

from engine.profile_views.engine_profile import (
    CrawlEngineView,
    add_service,
    toggle_service_connection,
    delete_service,
)

# Security Views
from engine.profile_views.security_views import (
    ChangePasswordView,
    Setup2FAView,
    Verify2FAView,
    Disable2FAView,
    SessionManagementView,
    TerminateSessionView,
    RegenerateJWTKeysView,
)

# Verification Views
from engine.profile_views.verification_views import (
    SendEmailVerificationView,
    VerifyEmailView,
    SendPhoneVerificationView,
    VerifyPhoneView,
)

# Data Export Views
from engine.profile_views.data_views import (
    ExportUserDataView,
    DeleteAccountView,
    DeactivateAccountView,
)

from engine.security_views.identity_settings import IdentitySettingsView

from engine.profile_views.engine_profile import (
    batch_refresh_services,
    refresh_service_data,
    get_service_media,
)



urlpatterns = [

    # ==================== MAIN PAGES ====================
    path('',                          EngineView,               name='engine'),
    path('api/feed/',                 engine_feed_api,          name='engine_feed_api'),
    path('personal/engine/',          PersonalEngineView,       name='personal_engine'),
    path('business/profile/',         BusinessProfileView,      name='business_profile'),
    path('edit_business_profile/',    EditBusinessProfileView,  name='edit_business_profile'),
    path('create_business_profile/',  CreateBusinessProfileView,name='create_business_profile'),

    # ==================== PROFILE PAGES ====================
    path('personal/profile/',   ProfileView,   name='profile'),
    path('service/profile/',  CrawlEngineView,  name='service_profile'),

    # ==================== SECURITY MANAGEMENT ====================
    path('security/password/change/',     ChangePasswordView,     name='change_password'),
    path('security/2fa/setup/',           Setup2FAView,           name='setup_2fa'),
    path('security/2fa/verify/',          Verify2FAView,          name='verify_2fa'),
    path('security/2fa/disable/',         Disable2FAView,         name='disable_2fa'),
    path('security/sessions/',            SessionManagementView,  name='session_management'),
    path('security/sessions/terminate/',  TerminateSessionView,   name='terminate_session'),
    path('security/jwt/regenerate/',      RegenerateJWTKeysView,  name='regenerate_jwt'),
    path('identity/settings/',            IdentitySettingsView,   name='identity_settings'),

    # ==================== VERIFICATION ====================
    path('verify/email/send/',    SendEmailVerificationView,  name='send_email_verification'),
    path('verify/email/confirm/', VerifyEmailView,            name='verify_email'),
    path('verify/phone/send/',    SendPhoneVerificationView,  name='send_phone_verification'),
    path('verify/phone/confirm/', VerifyPhoneView,            name='verify_phone'),

    # ==================== DATA MANAGEMENT ====================
    path('data/export/',       ExportUserDataView,   name='export_data'),
    path('account/deactivate/', DeactivateAccountView, name='deactivate_account'),
    path('account/delete/',    DeleteAccountView,    name='delete_account'),

    # ==================== SERVICE API ====================
    path('api/services/add/',                          add_service,               name='add_service'),
    path('api/services/batch-refresh/',                batch_refresh_services,    name='batch_refresh_services'),
    path('api/services/<int:service_id>/toggle/',      toggle_service_connection, name='toggle_service'),
    path('api/services/<int:service_id>/delete/',      delete_service,            name='delete_service'),
    path('api/services/<int:service_id>/refresh/',     refresh_service_data,      name='refresh_service'),
    path('api/services/<int:service_id>/media/',       get_service_media,         name='get_service_media'),

 
]