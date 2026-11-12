import uuid
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from operacao import models as m
from . import foundation as s, forms

def check_fields(request, fields):
    if request.method == 'POST' and set(request.POST)-set(fields)-{'csrfmiddlewaretoken'}:
        raise ValidationError('Campos não permitidos. Organização e credenciais são determinadas no backend.')

@login_required
@never_cache
def home(request):
    s.permissao(request.user, 'fiscal_consultar')
    return render(request, 'fiscal/home.html', {'docs': m.DocumentoFiscal.objects.filter(configuracao_fiscal__isnull=False).order_by('-pk'),
        'configuracoes': m.ConfiguracaoFiscal.objects.all(), 'series': m.SerieFiscal.objects.all(),
        'cadastros': forms.CADASTROS, 'enabled': getattr(settings, 'FISCAL_SIMULATION_ENABLED', False)})

@login_required
@never_cache
def cadastrar(request, kind):
    if kind not in forms.CADASTROS:
        raise Http404()
    model, formclass = forms.CADASTROS[kind]
    s.permissao(request.user, 'fiscal_gerenciar_credenciais' if kind in ('referencia', 'vinculo') else 'fiscal_configurar')
    form = formclass(request.POST if request.method == 'POST' else None)
    if request.method == 'POST':
        try:
            check_fields(request, form.fields)
            if form.is_valid():
                values = dict(form.cleaned_data)
                if kind == 'referencia':
                    values.update(referencia='simulacao://'+str(uuid.uuid4()), mecanismo='SIMULACAO')
                s.cadastrar(request.user, model, **values)
                messages.success(request, 'Cadastro de simulação registrado e auditado.')
                return redirect('operacao:fiscal_simulacao')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'fiscal/form.html', {'form': form, 'titulo': 'Cadastro fiscal de simulação: '+kind})

@login_required
@never_cache
def novo(request):
    s.permissao(request.user, 'fiscal_emitir')
    form = forms.DocumentoSimuladoForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST':
        try:
            check_fields(request, form.fields)
            if form.is_valid():
                d = form.cleaned_data
                doc = s.preparar_documento(request.user, d['configuracao'].pk, d['serie'].pk,
                    cliente_id=d['cliente'].pk if d['cliente'] else None, venda_id=d['venda'].pk if d['venda'] else None,
                    descricao=d['descricao'], valor=d['valor'], credencial_id=d['credencial'].pk if d['credencial'] else None,
                    regra_id=d['regra'].pk if d['regra'] else None)
                return redirect('operacao:fiscal_simulacao_detalhe', pk=doc.pk)
        except (ValidationError, ValueError, ArithmeticError, ObjectDoesNotExist) as exc:
            form.add_error(None, exc if isinstance(exc, ValidationError) else 'Dados fiscais de simulação inválidos.')
    return render(request, 'fiscal/form.html', {'form': form, 'titulo': 'Preparar documento sem valor fiscal'})

@login_required
@never_cache
def detalhe(request, pk):
    s.permissao(request.user, 'fiscal_consultar')
    doc = get_object_or_404(m.DocumentoFiscal, pk=pk, configuracao_fiscal__isnull=False)
    return render(request, 'fiscal/detail.html', {'doc': doc, 'solicitacao_form': forms.SolicitacaoForm(initial={'request_id': uuid.uuid4()}),
        'cancelamento_form': forms.CancelamentoForm(initial={'request_id': uuid.uuid4()}), 'requests': doc.solicitacoes_fiscais.order_by('pk'),
        'eventos': doc.eventos_fiscais.all(), 'arquivos': m.Documento.objects.filter(fiscal=doc)})

@login_required
@never_cache
@require_POST
def solicitar(request, pk, operation):
    if operation not in ('EMITIR', 'CANCELAR'):
        raise Http404()
    s.permissao(request.user, 'fiscal_cancelar' if operation == 'CANCELAR' else 'fiscal_emitir')
    doc = get_object_or_404(m.DocumentoFiscal, pk=pk, configuracao_fiscal__isnull=False)
    form = (forms.CancelamentoForm if operation == 'CANCELAR' else forms.SolicitacaoForm)(request.POST)
    try:
        check_fields(request, form.fields)
        if not form.is_valid():
            return HttpResponseBadRequest('Dados de solicitação inválidos.')
        d = form.cleaned_data
        req = s.solicitar(request.user, doc.pk, d['request_id'], operacao=operation, motivo=d.get('motivo', ''), cenario=d.get('cenario', 'AUTORIZAR'))
        s.processar(request.user, req.pk)
    except ValidationError as exc:
        return HttpResponseBadRequest('; '.join(exc.messages))
    return redirect('operacao:fiscal_simulacao_detalhe', pk=doc.pk)

@login_required
@never_cache
@require_POST
def reconciliar(request, pk):
    req = get_object_or_404(m.SolicitacaoFiscal, pk=pk)
    s.permissao(request.user, 'fiscal_cancelar' if req.operacao == 'CANCELAR' else 'fiscal_emitir')
    try:
        check_fields(request, [])
        s.processar(request.user, req.pk)
    except ValidationError as exc:
        return HttpResponseBadRequest('; '.join(exc.messages))
    return redirect('operacao:fiscal_simulacao_detalhe', pk=req.documento_id)

@login_required
@never_cache
def danfse(request, pk):
    s.permissao(request.user, 'fiscal_consultar')
    doc = get_object_or_404(m.DocumentoFiscal, pk=pk, tipo='NFSE')
    from django.http import HttpResponse
    from fiscal.motors.nfse_adn import MotorNFSeADN
    from fiscal.nfse.danfse import gerar_danfse_pdf

    motor = MotorNFSeADN()
    dados_dps = motor.converter_para_dados_dps(doc)
    danfse_dados = {
        'chave_acesso': doc.chave or f"352611{dados_dps.prestador.cnpj[:14]}55001{doc.numero or 1:0>10}",
        'numero_nfse': str(doc.numero or 1),
        'data_emissao': doc.criado_em.strftime('%d/%m/%Y %H:%M') if doc.criado_em else '',
        'competencia': dados_dps.data_competencia,
        'prestador': {
            'razao_social': dados_dps.prestador.razao_social,
            'cnpj': dados_dps.prestador.cnpj,
            'municipio': dados_dps.servico.municipio_prestacao_ibge,
            'im': dados_dps.prestador.inscricao_municipal or '-'
        },
        'tomador': {
            'razao_social_nome': dados_dps.tomador.razao_social_nome,
            'cnpj_cpf': dados_dps.tomador.cnpj_cpf,
            'endereco': f"{dados_dps.tomador.endereco.logradouro}, {dados_dps.tomador.endereco.numero}" if dados_dps.tomador.endereco else '-',
            'email': dados_dps.tomador.email or '-'
        },
        'servico': {
            'discriminacao': dados_dps.servico.discriminacao,
            'codigo_tributacao': dados_dps.servico.codigo_tributacao_nacional
        },
        'valores': {
            'valor_servicos': f"{dados_dps.valores.valor_servicos:.2f}",
            'valor_deducoes': "0,00",
            'base_calculo': f"{dados_dps.valores.base_calculo_iss:.2f}",
            'aliquota': f"{dados_dps.servico.aliquota_iss:.2f}",
            'valor_iss': f"{dados_dps.valores.calcular_iss(dados_dps.servico.aliquota_iss):.2f}",
            'valor_liquido': f"{dados_dps.valores.valor_servicos:.2f}"
        }
    }
    pdf_bytes = gerar_danfse_pdf(danfse_dados)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="DANFSE_{doc.numero or doc.pk}.pdf"'
    return response
