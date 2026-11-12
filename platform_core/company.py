"""Company metadata and explicit audited module/capability configuration."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from .forms import TenantModelForm
from .models import ConfiguracaoEmpresa, FilialEmpresa, Organization, ModuloOrganizacao, OrganizationMembership, PermissaoMembership
from .modules import CAPABILITIES, require_capability
from .context import current_organization

class EmpresaForm(TenantModelForm):
    class Meta:
        model = ConfiguracaoEmpresa
        # Commercial plan and quotas are platform-controlled SaaS entitlements.
        # Tenant administrators cannot raise them through the company settings API.
        fields = ['inscricao_estadual', 'inscricao_municipal', 'regime_tributario', 'crt', 'natureza_operacao', 'codigo_servico', 'iss_informativo', 'retencoes_informativas']

class FilialForm(TenantModelForm):
    class Meta:
        model = FilialEmpresa
        fields = ['codigo', 'nome', 'cnpj', 'endereco', 'inscricao_estadual', 'inscricao_municipal', 'ativo']

class PermissaoForm(TenantModelForm):
    capacidade = forms.ChoiceField(choices=[(key,key.replace('_',' ').capitalize()) for key in CAPABILITIES])
    class Meta:
        model = PermissaoMembership
        fields = ['membership', 'capacidade', 'permitido']
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['membership'].queryset = OrganizationMembership.objects.filter(organization=current_organization(), status='ATIVO').select_related('user', 'organization')
        self.fields['capacidade'].help_text = 'A permissão operar_pdv é individual e separada do perfil VENDEDOR; conceda-a somente a quem poderá operar o caixa.'

class ModulosForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        existing = dict(ModuloOrganizacao.objects.values_list('codigo','habilitado'))
        for code,label in ModuloOrganizacao._meta.get_field('codigo').choices:
            self.fields[code] = forms.BooleanField(label=label,required=False,initial=existing.get(code,True))

@login_required
def configuration(request, kind='empresa', public_id=None):
    require_capability(request.user, 'config_empresa')
    from gestao.services import audit
    classes = {'empresa':(EmpresaForm, ConfiguracaoEmpresa, 'Empresa — dados fiscais e plano informativo'), 'filial':(FilialForm, FilialEmpresa, 'Filial da empresa'), 'permissao':(PermissaoForm, PermissaoMembership, 'Permissão por vínculo'), 'modulos':(ModulosForm, ModuloOrganizacao, 'Módulos da organização')}
    cls, model, title = classes[kind]
    if kind == 'empresa':
        instance = model.objects.first() or model()
    elif public_id:
        instance = get_object_or_404(model,public_id=public_id)
    else:
        instance = model()
    if kind == 'permissao' and request.method == 'POST':
        member = request.POST.get('membership', '')
        if member.isdigit():
            instance = model.objects.filter(membership_id=int(member), capacidade=request.POST.get('capacidade', '')).first() or instance
    before_values = {name: getattr(instance, name) for name in cls._meta.fields if name != 'membership'} if kind != 'modulos' else {}
    form = cls(request.POST or None, **({'instance':instance} if kind!='modulos' else {}))
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=current_organization().pk)
                if kind=='modulos':
                    before = dict(model.objects.values_list('codigo','habilitado'))
                    for code,enabled in form.cleaned_data.items():
                        row = model.objects.filter(codigo=code).first() or model(codigo=code)
                        row.habilitado=enabled; row.save()
                    audit(request.user,'MODULOS_CONFIGURADOS',current_organization(),{'antes':before,'depois':form.cleaned_data})
                else:
                    if kind == 'filial' and not instance.pk:
                        from .saas_services import verificar_limite
                        verificar_limite(current_organization(), 'filiais', erp_code='assistencia')
                    if kind=='permissao':
                        prior=model.objects.filter(membership=form.cleaned_data['membership'],capacidade=form.cleaned_data['capacidade']).first()
                        if prior:
                            prior.permitido=form.cleaned_data['permitido']; prior.save(); instance=prior
                        else:
                            instance=form.save()
                    else:
                        instance=form.save()
                    audit(request.user,'CONFIGURACAO_EMPRESA',instance,{'tipo':kind,'antes':{key:str(value) for key,value in before_values.items()},'depois':{key:str(getattr(instance,key)) for key in before_values},'membership':getattr(instance,'membership_id',None)})
        except ValidationError as exc:
            form.add_error(None,'; '.join(exc.messages))
        else:
            return redirect('platform_core:empresa')
    return render(request,'platform_core/company.html',{'titulo':title,'form':form,'filiais':FilialEmpresa.objects.all(),'back_url':reverse('platform_core:home')},status=400 if request.method=='POST' else 200)


@login_required
def meu_plano(request):
    """Visualização estrita da assinatura, faturas e cotas exclusivas da organização ativa."""
    from .saas_models import AssinaturaSaaS, CobrancaSaaS, PlanoSaaS
    from .saas_services import metricas_uso, aplicar_modulos

    org = current_organization()
    subscription = AssinaturaSaaS.objects.filter(organization=org).select_related('plano', 'plano__erp').first()

    if request.method == 'POST' and request.POST.get('action') == 'trocar_plano':
        if getattr(request, 'membership', None) and request.membership.role == 'ADMIN':
            novo_slug = request.POST.get('plano_slug')
            novo_plano = PlanoSaaS.objects.filter(slug=novo_slug, publico_ativo=True, ativo=True).first()
            if novo_plano:
                if subscription:
                    subscription.plano = novo_plano
                    subscription.save()
                else:
                    subscription = AssinaturaSaaS.objects.create(organization=org, plano=novo_plano)
                aplicar_modulos(org, novo_plano)
                messages.success(request, f'Upgrade para o Plano {novo_plano.nome} realizado com sucesso! Seus novos módulos estão liberados.')
                return redirect('platform_core:meu_plano')
            else:
                messages.error(request, 'Plano selecionado inválido ou indisponível.')
        else:
            messages.error(request, 'Apenas o Administrador da organização pode alterar o plano contratado.')

    metrics = metricas_uso(org, erp_code='assistencia') if subscription else {}

    active_modules = []
    if subscription and subscription.plano:
        for pm in subscription.plano.modulos_produto.select_related('modulo').all():
            active_modules.append({
                'code': pm.modulo.code,
                'name': pm.modulo.name,
                'description': pm.modulo.description or 'Módulo operacional liberado no plano.',
            })

    cobrancas = CobrancaSaaS.objects.filter(organization=org).order_by('-criado_em')[:10]
    cobranca_pendente = CobrancaSaaS.objects.filter(organization=org, status=CobrancaSaaS.Status.PENDENTE).first()
    available_plans = list(PlanoSaaS.objects.filter(publico_ativo=True, ativo=True).order_by('preco'))
    upgrade_target = request.GET.get('upgrade', '')

    context = {
        'titulo': f'Minha Assinatura · {org.display_name}',
        'subscription': subscription,
        'metrics': metrics,
        'active_modules': active_modules,
        'cobrancas': cobrancas,
        'cobranca_pendente': cobranca_pendente,
        'organization': org,
        'available_plans': available_plans,
        'upgrade_target': upgrade_target,
        'back_url': reverse('platform_core:empresa'),
    }
    return render(request, 'platform_core/meu_plano.html', context)


@login_required
def gerar_cobranca_pix_view(request):
    """Gera cobrança Pix com Copia-e-Cola e QR Code para renovação da assinatura."""
    from .saas_billing import gerar_cobranca_pix
    org = current_organization()
    cobranca = gerar_cobranca_pix(org)
    messages.success(request, f'Cobrança Pix #{cobranca.pk} gerada! Utilize o QR Code ou Pix Copia e Cola para pagar.')
    return redirect('platform_core:meu_plano')


@login_required
def simular_pagamento_cobranca(request, cobranca_id):
    """Permite testar e validar o ciclo de liquidação e renovação da assinatura."""
    from .saas_billing import simular_confirmacao_pagamento
    org = current_organization()
    ok = simular_confirmacao_pagamento(cobranca_id, org, actor=request.user)
    if ok:
        messages.success(request, f'Pagamento da fatura #{cobranca_id} confirmado com sucesso! Assinatura estendida.')
    else:
        messages.error(request, 'Cobrança não encontrada ou já confirmada.')
    return redirect('platform_core:meu_plano')


@csrf_exempt
def webhook_asaas_saas(request):
    """Recepção de notificações de pagamento automáticas do Asaas."""
    if request.method != 'POST':
        return HttpResponse(status=405)
    import json
    from .saas_billing import processar_webhook_asaas
    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return HttpResponse(status=400)
    sucesso, msg = processar_webhook_asaas(data)
    return JsonResponse({'status': 'ok' if sucesso else 'ignored', 'message': msg})
