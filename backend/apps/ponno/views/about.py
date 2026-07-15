# apps/ponno/views/about.py

from django.shortcuts import render


def AboutView(request):
    """
    Static "About PIELAM" page.
    Simple informational view — no DB queries required.
    """

    context = {
        'meta_description': 'Learn more about PIELAM — who we are and what we do.',
        'meta_keywords':    'PIELAM, about, company, marketplace',
        'og_title':         'About PIELAM',
        'og_description':   'Learn more about PIELAM — who we are and what we do.',
        'og_url':           request.build_absolute_uri(),
    }

    return render(request, 'ponno/about.html', context)