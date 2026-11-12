"""Homologação comercial com R2 opcional e sem envios reais.

Selecionar explicitamente via DJANGO_SETTINGS_MODULE no serviço de testes.
Não cria banco, usuários, credenciais ou chave clínica.
"""
from .settings import *  # noqa: F403,F401
from copy import deepcopy

# Evaluate here too: importing this module directly must remain safe even when
# base settings were imported by a different entry point.
PRIVATE_STORAGE = homologation_storage(config)
FILE_UPLOADS_ENABLED = PRIVATE_STORAGE['BACKEND'] != 'platform_core.uploads.DisabledStorage'
STORAGES = deepcopy(STORAGES)
STORAGES['default'] = deepcopy(PRIVATE_STORAGE)
STORAGES['default']['OPTIONS']['location'] = 'media'
PRIVATE_STORAGE['OPTIONS']['location'] = 'private'
if not FILE_UPLOADS_ENABLED:
    FILE_UPLOAD_HANDLERS = ['platform_core.uploads.DisabledUploadHandler']
MIDDLEWARE = [*MIDDLEWARE, 'platform_core.uploads.DisabledUploadsMiddleware']
TEMPLATES = deepcopy(TEMPLATES)
TEMPLATES[0]['OPTIONS']['context_processors'].append('platform_core.uploads.upload_context')

EMAIL_BACKEND = 'django.core.mail.backends.dummy.EmailBackend'
EXTERNAL_EMAIL_ENABLED = False
EXTERNAL_BANKING_ENABLED = False
EXTERNAL_FISCAL_ENABLED = False
FISCAL_NFCE_SEFAZ_APPROVED = False
FISCAL_SIMULATION_ENABLED = False
FISCAL_NFCE_LOCAL_ENABLED = False
SESSION_COOKIE_NAME = 'vertyx_homologacao_sessionid'
CSRF_COOKIE_NAME = 'vertyx_homologacao_csrftoken'
COMMERCIAL_DEMO_ENABLED = True
