# apps/customer/validators_enhanced.py

"""
Enhanced Validators for International Business Standards
--------------------------------------------------------
Includes validators for:
- International phone numbers
- Strong passwords with complexity rules
- Email validation
- Name validation
- Country codes
- Currency codes
- Timezone validation
"""

import re
from typing import Optional

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.utils.translation import gettext_lazy as _
import phonenumbers
from phonenumbers import NumberParseException


# ========================================================================
# PHONE NUMBER VALIDATORS (INTERNATIONAL)
# ========================================================================

def validate_international_phone(value: str) -> str:
    """
    Validate international phone number using phonenumbers library
    
    Accepts:
    - E.164 format: +8801712345678
    - National format: 01712345678 (requires country code in context)
    - International format with spaces/dashes
    
    Returns normalized phone number
    """
    if not value:
        raise ValidationError(_("Phone number is required"))
    
    # Clean the input
    cleaned = value.strip()
    
    try:
        # Parse the phone number
        # If no country code, assume Bangladesh (BD)
        if not cleaned.startswith('+'):
            # Try parsing with Bangladesh country code
            parsed = phonenumbers.parse(cleaned, 'BD')
        else:
            parsed = phonenumbers.parse(cleaned, None)
        
        # Validate the parsed number
        if not phonenumbers.is_valid_number(parsed):
            raise ValidationError(
                _("Invalid phone number. Please provide a valid phone number.")
            )
        
        # Return in E.164 format
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    
    except NumberParseException as e:
        raise ValidationError(
            _("Invalid phone number format. Use international format like +8801712345678")
        )


def validate_bangladeshi_phone(value: str) -> str:
    """
    Specifically validate Bangladeshi phone numbers
    
    Accepts:
    - 01XXXXXXXXX (11 digits)
    - 8801XXXXXXXXX (13 digits)
    - +8801XXXXXXXXX (14 characters)
    """
    if not value:
        raise ValidationError(_("Phone number is required"))
    
    # Remove spaces and dashes
    cleaned = re.sub(r'[\s\-]', '', value)
    
    # Pattern for Bangladeshi mobile numbers
    # Operators: GP (017), Robi (018), Banglalink (019, 014), Teletalk (015), Airtel (016)
    bd_pattern = re.compile(r'^(\+?880|0)1[3-9]\d{8}$')
    
    if not bd_pattern.match(cleaned):
        raise ValidationError(
            _("Invalid Bangladeshi phone number. Use format: 01XXXXXXXXX or +8801XXXXXXXXX")
        )
    
    # Normalize to E.164 format
    if cleaned.startswith('0'):
        return f'+880{cleaned[1:]}'
    elif cleaned.startswith('880'):
        return f'+{cleaned}'
    elif cleaned.startswith('+880'):
        return cleaned
    
    raise ValidationError(_("Invalid phone number format"))


# ========================================================================
# EMAIL VALIDATORS
# ========================================================================

# Use Django's built-in email validator
validate_email = EmailValidator(message=_("Enter a valid email address"))


def validate_business_email(value: str) -> str:
    """
    Validate business email (no free email providers)
    """
    validate_email(value)
    
    # List of free email providers to reject for business accounts
    free_providers = [
        'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
        'live.com', 'aol.com', 'icloud.com', 'mail.com',
        'protonmail.com', 'zoho.com', 'yandex.com'
    ]
    
    domain = value.split('@')[1].lower()
    
    if domain in free_providers:
        raise ValidationError(
            _("Business email required. Free email providers are not allowed.")
        )
    
    return value


# ========================================================================
# EMAIL OR PHONE VALIDATOR
# ========================================================================

def email_or_phone_validator(value: str) -> str:
    """
    Enhanced validator that accepts either email or phone number
    Supports international phone numbers
    """
    if not value or not isinstance(value, str):
        raise ValidationError(
            _("Please provide a valid email address or phone number")
        )
    
    value = value.strip()
    
    # Try email first
    if '@' in value:
        try:
            validate_email(value)
            return value
        except ValidationError:
            raise ValidationError(_("Invalid email address"))
    
    # Try phone number
    try:
        return validate_international_phone(value)
    except ValidationError as e:
        raise ValidationError(
            _("Invalid email or phone number. Please provide a valid email address "
              "or international phone number (e.g., +8801712345678)")
        )


# ========================================================================
# PASSWORD VALIDATORS
# ========================================================================

def strong_password_validator(value: str) -> str:
    """
    Enforce strong password policy
    
    Requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    - No common passwords
    - No sequential characters
    """
    if len(value) < 8:
        raise ValidationError(_("Password must be at least 8 characters long"))
    
    if len(value) > 128:
        raise ValidationError(_("Password must be at most 128 characters long"))
    
    # Check for uppercase
    if not re.search(r'[A-Z]', value):
        raise ValidationError(_("Password must contain at least one uppercase letter"))
    
    # Check for lowercase
    if not re.search(r'[a-z]', value):
        raise ValidationError(_("Password must contain at least one lowercase letter"))
    
    # Check for digit
    if not re.search(r'\d', value):
        raise ValidationError(_("Password must contain at least one digit"))
    
    # Check for special character
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', value):
        raise ValidationError(
            _("Password must contain at least one special character (!@#$%^&*(),.?\":{}|<>)")
        )
    
    # Check for common passwords
    common_passwords = [
        'password', '12345678', 'qwerty', 'abc123', 
        'password123', 'admin', 'letmein', 'welcome'
    ]
    if value.lower() in common_passwords:
        raise ValidationError(_("This password is too common"))
    
    # Check for sequential characters
    if re.search(r'(abc|bcd|cde|def|123|234|345|456|567|678|789)', value.lower()):
        raise ValidationError(_("Password contains sequential characters"))
    
    return value


def enterprise_password_validator(value: str) -> str:
    """
    Enterprise-level password requirements (even stricter)
    
    Requirements:
    - Minimum 12 characters
    - At least 2 uppercase letters
    - At least 2 lowercase letters
    - At least 2 digits
    - At least 2 special characters
    - No repeating characters (more than 2)
    """
    # Run basic validation first
    strong_password_validator(value)
    
    if len(value) < 12:
        raise ValidationError(_("Password must be at least 12 characters long for enterprise accounts"))
    
    # Count character types
    uppercase_count = len(re.findall(r'[A-Z]', value))
    lowercase_count = len(re.findall(r'[a-z]', value))
    digit_count = len(re.findall(r'\d', value))
    special_count = len(re.findall(r'[!@#$%^&*(),.?":{}|<>]', value))
    
    if uppercase_count < 2:
        raise ValidationError(_("Password must contain at least 2 uppercase letters"))
    
    if lowercase_count < 2:
        raise ValidationError(_("Password must contain at least 2 lowercase letters"))
    
    if digit_count < 2:
        raise ValidationError(_("Password must contain at least 2 digits"))
    
    if special_count < 2:
        raise ValidationError(_("Password must contain at least 2 special characters"))
    
    # Check for repeating characters
    if re.search(r'(.)\1{2,}', value):
        raise ValidationError(_("Password contains repeating characters"))
    
    return value


# ========================================================================
# NAME VALIDATORS
# ========================================================================

def name_validator(value: str) -> str:
    """
    Validate person name (international support)
    
    Allows:
    - Letters (any language)
    - Spaces
    - Hyphens
    - Apostrophes
    - Dots
    """
    if not value:
        raise ValidationError(_("Name is required"))
    
    value = value.strip()
    
    if len(value) < 2:
        raise ValidationError(_("Name must be at least 2 characters long"))
    
    if len(value) > 100:
        raise ValidationError(_("Name must be at most 100 characters long"))
    
    # Allow unicode letters, spaces, hyphens, apostrophes, and dots
    # This supports international names
    if not re.match(r"^[\p{L}\s.'\-]+$", value, re.UNICODE):
        raise ValidationError(
            _("Name can only contain letters, spaces, dots, apostrophes, and hyphens")
        )
    
    return value


def username_validator(value: str) -> str:
    """
    Validate username (alphanumeric and underscores only)
    """
    if not value:
        raise ValidationError(_("Username is required"))
    
    value = value.strip().lower()
    
    if len(value) < 3:
        raise ValidationError(_("Username must be at least 3 characters long"))
    
    if len(value) > 30:
        raise ValidationError(_("Username must be at most 30 characters long"))
    
    # Alphanumeric and underscores only
    if not re.match(r'^[a-z0-9_]+$', value):
        raise ValidationError(
            _("Username can only contain lowercase letters, numbers, and underscores")
        )
    
    # Must start with letter
    if not value[0].isalpha():
        raise ValidationError(_("Username must start with a letter"))
    
    # Reserved usernames
    reserved = ['admin', 'root', 'system', 'user', 'test', 'api', 'www']
    if value in reserved:
        raise ValidationError(_("This username is reserved"))
    
    return value


# ========================================================================
# COUNTRY CODE VALIDATOR
# ========================================================================

def validate_country_code(value: str) -> str:
    """
    Validate ISO 3166-1 alpha-2 country code
    """
    if not value:
        return value
    
    value = value.strip().upper()
    
    if len(value) != 2:
        raise ValidationError(_("Country code must be 2 characters (ISO 3166-1 alpha-2)"))
    
    # You can add a list of valid country codes here
    # For now, just check format
    if not value.isalpha():
        raise ValidationError(_("Country code must contain only letters"))
    
    return value


# ========================================================================
# CURRENCY CODE VALIDATOR
# ========================================================================

def validate_currency_code(value: str) -> str:
    """
    Validate ISO 4217 currency code
    """
    if not value:
        return value
    
    value = value.strip().upper()
    
    if len(value) != 3:
        raise ValidationError(_("Currency code must be 3 characters (ISO 4217)"))
    
    # Common currency codes
    valid_currencies = [
        'USD', 'EUR', 'GBP', 'JPY', 'CNY', 'INR', 'BDT',
        'AUD', 'CAD', 'CHF', 'HKD', 'SGD', 'SEK', 'KRW',
        'NOK', 'NZD', 'MXN', 'ZAR', 'BRL', 'RUB', 'TRY',
        'AED', 'SAR', 'THB', 'MYR', 'IDR', 'PHP', 'VND'
    ]
    
    if value not in valid_currencies:
        raise ValidationError(
            _(f"Invalid currency code. Must be one of: {', '.join(valid_currencies)}")
        )
    
    return value


# ========================================================================
# TIMEZONE VALIDATOR
# ========================================================================

def validate_timezone(value: str) -> str:
    """
    Validate timezone string
    """
    if not value:
        return 'UTC'
    
    import pytz
    
    try:
        pytz.timezone(value)
        return value
    except pytz.exceptions.UnknownTimeZoneError:
        raise ValidationError(
            _("Invalid timezone. Use format like 'Asia/Dhaka' or 'UTC'")
        )


# ========================================================================
# LANGUAGE CODE VALIDATOR
# ========================================================================

def validate_language_code(value: str) -> str:
    """
    Validate ISO 639-1 language code
    """
    if not value:
        return 'en'
    
    value = value.strip().lower()
    
    if len(value) != 2:
        raise ValidationError(_("Language code must be 2 characters (ISO 639-1)"))
    
    # Common language codes
    valid_languages = [
        'en', 'bn', 'es', 'fr', 'de', 'zh', 'ar', 'hi',
        'ja', 'ko', 'pt', 'ru', 'it', 'nl', 'sv', 'pl',
        'tr', 'vi', 'th', 'id', 'ms', 'tl', 'ur', 'fa'
    ]
    
    if value not in valid_languages:
        raise ValidationError(
            _(f"Invalid language code. Must be one of: {', '.join(valid_languages)}")
        )
    
    return value


# ========================================================================
# URL VALIDATORS
# ========================================================================

def validate_website_url(value: str) -> str:
    """
    Validate website URL
    """
    if not value:
        return value
    
    from django.core.validators import URLValidator
    
    url_validator = URLValidator(
        schemes=['http', 'https'],
        message=_("Enter a valid URL (http:// or https://)")
    )
    
    try:
        url_validator(value)
        return value
    except ValidationError:
        raise ValidationError(_("Invalid website URL"))


# ========================================================================
# METADATA VALIDATORS
# ========================================================================

def validate_json_metadata(value: dict) -> dict:
    """
    Validate JSON metadata field
    """
    if not isinstance(value, dict):
        raise ValidationError(_("Metadata must be a valid JSON object"))
    
    # Check size (max 64KB)
    import json
    json_str = json.dumps(value)
    if len(json_str) > 65536:
        raise ValidationError(_("Metadata is too large (max 64KB)"))
    
    return value


# ========================================================================
# UTILITY FUNCTIONS
# ========================================================================

def normalize_phone_number(phone: str, country: str = 'BD') -> str:
    """
    Normalize phone number to E.164 format
    """
    try:
        if not phone.startswith('+'):
            parsed = phonenumbers.parse(phone, country)
        else:
            parsed = phonenumbers.parse(phone, None)
        
        return phonenumbers.format_number(
            parsed, 
            phonenumbers.PhoneNumberFormat.E164
        )
    except NumberParseException:
        return phone


def get_phone_country_code(phone: str) -> Optional[str]:
    """
    Extract country code from phone number
    """
    try:
        if not phone.startswith('+'):
            return None
        
        parsed = phonenumbers.parse(phone, None)
        country_code = phonenumbers.region_code_for_number(parsed)
        return country_code
    except NumberParseException:
        return None


def format_phone_display(phone: str) -> str:
    """
    Format phone number for display (international format)
    """
    try:
        if not phone.startswith('+'):
            parsed = phonenumbers.parse(phone, 'BD')
        else:
            parsed = phonenumbers.parse(phone, None)
        
        return phonenumbers.format_number(
            parsed,
            phonenumbers.PhoneNumberFormat.INTERNATIONAL
        )
    except NumberParseException:
        return phone


# ========================================================================
# PASSWORD STRENGTH CHECKER
# ========================================================================

def check_password_strength(password: str) -> dict:
    """
    Check password strength and return a score
    
    Returns:
    {
        'score': 0-100,
        'strength': 'weak' | 'fair' | 'good' | 'strong' | 'excellent',
        'suggestions': [list of suggestions]
    }
    """
    score = 0
    suggestions = []
    
    # Length check
    if len(password) >= 8:
        score += 10
    else:
        suggestions.append("Use at least 8 characters")
    
    if len(password) >= 12:
        score += 10
    
    if len(password) >= 16:
        score += 10
    
    # Character variety
    if re.search(r'[a-z]', password):
        score += 15
    else:
        suggestions.append("Add lowercase letters")
    
    if re.search(r'[A-Z]', password):
        score += 15
    else:
        suggestions.append("Add uppercase letters")
    
    if re.search(r'\d', password):
        score += 15
    else:
        suggestions.append("Add numbers")
    
    if re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        score += 15
    else:
        suggestions.append("Add special characters")
    
    # Complexity bonuses
    unique_chars = len(set(password))
    if unique_chars >= len(password) * 0.7:
        score += 10
    
    # Penalties
    if re.search(r'(.)\1{2,}', password):
        score -= 10
        suggestions.append("Avoid repeating characters")
    
    if re.search(r'(abc|bcd|cde|123|234|345)', password.lower()):
        score -= 10
        suggestions.append("Avoid sequential characters")
    
    # Determine strength
    if score < 40:
        strength = 'weak'
    elif score < 60:
        strength = 'fair'
    elif score < 80:
        strength = 'good'
    elif score < 95:
        strength = 'strong'
    else:
        strength = 'excellent'
    
    return {
        'score': max(0, min(100, score)),
        'strength': strength,
        'suggestions': suggestions
    }