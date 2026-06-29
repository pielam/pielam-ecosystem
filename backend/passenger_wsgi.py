#import os
#import sys


#sys.path.insert(0, os.path.dirname(__file__))


#def application(environ, start_response):
#    start_response('200 OK', [('Content-Type', 'text/plain')])
#    message = 'It works fine!\n'
#    version = 'Python %s\n' % sys.version.split()[0]
#    response = '\n'.join([message, version])
#    return [response.encode()]

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, APP_DIR)
sys.path.insert(0, os.path.join(APP_DIR, "pielam"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pielam.settings.prod")

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
