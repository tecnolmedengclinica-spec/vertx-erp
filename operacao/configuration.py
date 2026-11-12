import uuid
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods, require_POST
from django.utils import timezone
from platform_core.access import require_erp
from platform_core.forms import OrganizationForm
from platform_core.audit import previous_values, record_write
from ordens.storage import private_storage
from .forms import ConfiguracaoForm, ConfiguracaoEmailForm
from .models import Configuracao, Notificacao, ConfiguracaoNotificacaoEmail
from .email_service import obter_configuracao_email, testar_smtp

class CompanyForm(OrganizationForm):
    logo = forms.ImageField(required=False, help_text='PNG, JPEG ou WebP, até 2 MB.')
    class Meta(OrganizationForm.Meta):
        fields = ['razao_social', 'nome_fantasia', 'cnpj', 'email', 'telefone', 'document_details']
    def clean_logo(self):
        image = self.cleaned_data.get('logo')
        if image and (image.size > 2 * 1024 * 1024 or image.image.format not in ['PNG', 'JPEG', 'WEBP'] or image.image.width * image.image.height > 10000000):
            raise forms.ValidationError('Logomarca inválida; use PNG/JPEG/WebP até 2 MB.')
        return image

@login_required
@require_http_methods(['GET', 'POST'])
@transaction.atomic
def configuracao(request):
    require_erp(request.user, roles=['ADMIN'])
    company = CompanyForm(request.POST if request.method == 'POST' else None, request.FILES or None, instance=request.organization, prefix='empresa')
    obj = Configuracao.objects.select_for_update().first() or Configuracao()
    params = ConfiguracaoForm(request.POST if request.method == 'POST' else None, instance=obj, prefix='parametros')
    if request.method == 'POST' and company.is_valid() and params.is_valid():
        before_company, before_params = previous_values(company), previous_values(params)
        logo = company.cleaned_data.get('logo')
        if logo:
            from platform_core.saas_services import verificar_limite
            try:
                verificar_limite(request.organization, 'armazenamento_mb', incremento=logo.size, erp_code='assistencia')
            except ValidationError as exc:
                company.add_error('logo', '; '.join(exc.messages))
                return render(request, 'operacao/configuracao.html', {'company': company, 'params': params}, status=400)
            extension = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}[logo.image.format]
            logo.seek(0)
            try:
                company.instance.logo_key = private_storage.save(f'organizations/{request.organization.public_id}/branding/{uuid.uuid4().hex}.{extension}', logo)
            except OSError:
                company.add_error('logo', 'Storage indisponível; tente novamente.')
                return render(request, 'operacao/configuracao.html', {'company': company, 'params': params}, status=503)
        organization = company.save()
        params_obj = params.save()
        record_write(request, organization, before_company)
        record_write(request, params_obj, before_params)
        messages.success(request, 'Configurações salvas. Documentos já assinados mantêm a identidade original.')
        return redirect('operacao:configuracao')
    return render(request, 'operacao/configuracao.html', {'company': company, 'params': params}, status=400 if request.method == 'POST' else 200)

@login_required
@require_http_methods(['GET', 'POST'])
def notificacoes(request):
    require_erp(request.user, roles=['ADMIN','GESTOR','TECNICO','ESTOQUE','FINANCEIRO','CONTABILIDADE'])
    if request.method == 'POST':
        from .notifications import scan
        scan(request.user)
        return redirect('operacao:notificacoes')
    return render(request, 'operacao/notificacoes.html', {'notificacoes': Notificacao.objects.filter(usuario=request.user)[:100]})

@login_required
@require_POST
def notificacao_lida(request, pk):
    require_erp(request.user)
    from django.shortcuts import get_object_or_404
    row = get_object_or_404(Notificacao, pk=pk, usuario=request.user)
    row.lida_em = timezone.now()
    row.save()
    return redirect('operacao:notificacoes')


@login_required
@require_http_methods(['GET', 'POST'])
def configuracao_email(request):
    require_erp(request.user, roles=['ADMIN', 'GESTOR'])
    config = obter_configuracao_email(request.organization)
    form = ConfiguracaoEmailForm(request.POST if request.method == 'POST' else None, instance=config)

    if request.method == 'POST':
        action = request.POST.get('action', 'salvar')
        if action == 'testar':
            email_teste = request.POST.get('email_teste') or request.user.email
            if not email_teste:
                messages.warning(request, 'Informe um e-mail de destino no campo de teste.')
            else:
                # Atualiza os parâmetros em memória para o teste sem exigir salvar primeiro
                temp_config = form.save(commit=False) if form.is_valid() else config
                ok, msg = testar_smtp(temp_config, email_teste)
                if ok:
                    messages.success(request, f'✓ {msg}')
                else:
                    messages.error(request, f'✗ {msg}')
        elif action == 'salvar':
            if form.is_valid():
                form.save()
                messages.success(request, 'Configurações de e-mail salvas com sucesso!')
                return redirect('operacao:configuracao_email')
            else:
                messages.error(request, 'Por favor, corrija os campos obrigatórios destacados.')

    return render(request, 'operacao/configuracao_email.html', {
        'form': form,
        'config': config,
    })
