# apps/customer/services/contact_resolver.py

"""
Resolves a dealer's ContactInfo (+ optional BusinessInfo) into the
flat context shape product_detail.html expects:

    show_dealer_phone, dealer_phone, dealer_phone_verified
    show_dealer_email, dealer_email, dealer_email_verified
    dealer_whatsapp, dealer_whatsapp_verified
    dealer_messaging_channels   -> list[{key,label,icon,color,value,href,verified}]
    dealer_preferred_channel    -> one of the above dicts, or None

Rules:
- Phone/email prefer BusinessInfo (if public & not archived), falling
  back to ContactInfo.get_visible_contact_methods().
- WhatsApp and the other 14 messaging channels come only from
  ContactInfo.get_visible_contact_methods() (there's no BusinessInfo
  equivalent for these).
- 'whatsapp' is excluded from dealer_messaging_channels since it has
  its own dedicated button everywhere in the template.
- dealer_preferred_channel is only populated when the dealer's
  preferred_contact_method is one of the "other" messaging channels
  (never phone/email/whatsapp, which already have dedicated buttons).
"""

# Icon / color per messaging channel. Falls back to a generic bubble
# icon + neutral color for anything not listed here.
CHANNEL_META = {
    'telegram':            {'label': 'Telegram',   'icon': 'fa-brands fa-telegram',    'color': '#229ED9'},
    'signal':              {'label': 'Signal',      'icon': 'fa-solid fa-comment-dots', 'color': '#3A76F0'},
    'viber':               {'label': 'Viber',       'icon': 'fa-brands fa-viber',       'color': '#7360F2'},
    'wechat':              {'label': 'WeChat',      'icon': 'fa-brands fa-weixin',      'color': '#07C160'},
    'line':                {'label': 'LINE',        'icon': 'fa-brands fa-line',        'color': '#06C755'},
    'imo':                 {'label': 'imo',         'icon': 'fa-solid fa-comment-dots', 'color': '#0FA8E0'},
    'skype':               {'label': 'Skype',       'icon': 'fa-brands fa-skype',       'color': '#00AFF0'},
    'discord':             {'label': 'Discord',     'icon': 'fa-brands fa-discord',     'color': '#5865F2'},
    'facebook_messenger':  {'label': 'Messenger',   'icon': 'fa-brands fa-facebook-messenger', 'color': '#0084FF'},
    'instagram':           {'label': 'Instagram',   'icon': 'fa-brands fa-instagram',   'color': '#C13584'},
    'snapchat':            {'label': 'Snapchat',    'icon': 'fa-brands fa-snapchat',    'color': '#FFFC00'},
    'twitter':             {'label': 'X / Twitter', 'icon': 'fa-brands fa-x-twitter',   'color': '#000000'},
    'linkedin':            {'label': 'LinkedIn',    'icon': 'fa-brands fa-linkedin-in', 'color': '#0A66C2'},
    'slack':               {'label': 'Slack',       'icon': 'fa-brands fa-slack',       'color': '#4A154B'},
}

# Only channels with a well-known, reliable universal deep-link get an
# href builder. Everything else renders as a plain "label: value" pill
# (see ch.href check in the template) rather than a guessed-at link.
_HREF_BUILDERS = {
    'telegram':           lambda v: f"https://t.me/{v.lstrip('@')}",
    'facebook_messenger': lambda v: f"https://m.me/{v.lstrip('@')}",
    'instagram':          lambda v: f"https://instagram.com/{v.lstrip('@')}",
    'snapchat':           lambda v: f"https://snapchat.com/add/{v.lstrip('@')}",
    'twitter':            lambda v: f"https://twitter.com/{v.lstrip('@')}",
    'linkedin':           lambda v: v,  # already a full URL on the model
    'skype':              lambda v: f"skype:{v}?chat",
}


def _resolve_dealer_phone_email(dealer, contact, visible: dict):
    """BusinessInfo wins if public/active; ContactInfo is the fallback."""
    phone = phone_verified = email = email_verified = None

    business = getattr(dealer, 'business_info', None)
    if business and getattr(business, 'is_business_public', False) \
            and not getattr(business, 'is_business_archived', False):
        phone = getattr(business, 'business_phone', None) or None
        email = getattr(business, 'business_email', None) or None

    if contact is not None:
        if not phone and 'phone' in visible:
            phone, phone_verified = visible['phone']['value'], visible['phone']['verified']
        elif phone and contact.phone_number == phone:
            phone_verified = contact.is_phone_verified

        if not email and 'email' in visible:
            email, email_verified = visible['email']['value'], visible['email']['verified']
        elif email and contact.alt_email == email:
            email_verified = contact.is_alt_email_verified

    return phone, bool(phone_verified), email, bool(email_verified)


def _build_channel_dict(key, entry):
    meta = CHANNEL_META.get(key, {})
    value = entry['value']
    builder = _HREF_BUILDERS.get(key)
    return {
        'key': key,
        'label': meta.get('label', key.replace('_', ' ').title()),
        'icon': meta.get('icon', 'fa-solid fa-comment-dots'),
        'color': meta.get('color', '#6b7080'),
        'value': value,
        'verified': entry['verified'],
        'href': builder(value) if builder else None,
    }


def build_dealer_contact_context(dealer) -> dict:
    """
    Single entry point for the view: pass the dealer (User) instance,
    get back every dealer_* key product_detail.html reads.
    """
    if dealer is None:
        return {}

    contact = getattr(dealer, 'contactinfo', None)
    if contact is not None and (contact.is_contact_archived or not contact.is_contact_public):
        contact = None  # treat as if there's no usable contact info at all

    visible = contact.get_visible_contact_methods() if contact is not None else {}

    phone, phone_verified, email, email_verified = _resolve_dealer_phone_email(dealer, contact, visible)

    whatsapp = whatsapp_verified = None
    if 'whatsapp' in visible:
        whatsapp = visible['whatsapp']['value']
        whatsapp_verified = visible['whatsapp']['verified']

    channels = [
        _build_channel_dict(key, visible[key])
        for key, _field in (contact.MESSAGING_CHANNELS if contact is not None else ())
        if key in visible and key != 'whatsapp'
    ]

    preferred_channel = None
    if contact is not None:
        preferred_key = contact.preferred_contact_method
        preferred_channel = next((c for c in channels if c['key'] == preferred_key), None)

    return {
        'show_dealer_phone': bool(phone),
        'dealer_phone': phone,
        'dealer_phone_verified': phone_verified,

        'show_dealer_email': bool(email),
        'dealer_email': email,
        'dealer_email_verified': email_verified,

        'dealer_whatsapp': whatsapp,
        'dealer_whatsapp_verified': whatsapp_verified,

        'dealer_messaging_channels': channels,
        'dealer_preferred_channel': preferred_channel,
    }