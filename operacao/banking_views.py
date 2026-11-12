"""Views da API de Integração Bancária (Configuração, Pix/Boleto, Extrato Automático e Webhook público)."""
from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from gestao.models import Lancamento
from gestao.permissions import require
from operacao import models as m
from operacao.banking import (
    configurar_integracao_bancaria,
    consultar_cobranca_bancaria,
    emitir_cobranca_bancaria,
    processar_webhook_bancario,
    simular_liquidacao_sandbox,
    sincronizar_extrato_api,
)


@login_required
def bancos_integracao(request):
    require(request.user, 'financeiro')
    contas = list(m.ContaBancaria.objects.filter(ativo=True, tipo='BANCO')) or list(m.ContaBancaria.objects.filter(ativo=True))
    integracoes = list(m.IntegracaoBancaria.objects.select_related('conta', 'organization').all())
    cobrancas = list(m.CobrancaBancaria.objects.select_related('conta', 'integracao', 'lancamento', 'pagamento').all()[:25])
    lancamentos_aberto = list(
        Lancamento.objects.filter(tipo='RECEBER', status__in=['PENDENTE', 'VENCIDO'])
        .select_related('cliente')
        .order_by('vencimento')[:50]
    )
    extratos = list(m.ExtratoBancario.objects.select_related('conta', 'movimento').order_by('-data', '-pk')[:15])
    webhooks = list(m.WebhookBancarioEvento.objects.select_related('integracao', 'cobranca').all()[:12])

    ultimo_webhook_secret = request.session.pop('vertyx_last_webhook_secret', None)

    if request.method == 'POST':
        require(request.user, 'financeiro_escrever')
        acao = request.POST.get('acao', '')
        try:
            if acao == 'configurar':
                integ, raw_secret = configurar_integracao_bancaria(
                    request.user,
                    int(request.POST['conta_id']),
                    provedor=request.POST.get('provedor', 'SIMULADO'),
                    ambiente=request.POST.get('ambiente', 'SANDBOX'),
                    client_id=request.POST.get('client_id', ''),
                    credencial_referencia=request.POST.get('credencial_referencia', 'simulacao://banco-local'),
                    chave_pix=request.POST.get('chave_pix', ''),
                    convenio_boleto=request.POST.get('convenio_boleto', ''),
                    conciliar_automaticamente=request.POST.get('conciliar_automaticamente') == '1',
                    webhook_secret=request.POST.get('webhook_secret', ''),
                )
                if request.POST.get('webhook_secret', '') or raw_secret:
                    request.session['vertyx_last_webhook_secret'] = raw_secret
                messages.success(request, f'Integração {integ.get_provedor_display()} salva com referência segura.')
            elif acao == 'emitir_cobranca':
                lanc_id = int(request.POST['lancamento_id']) if request.POST.get('lancamento_id') else None
                try:
                    valor = Decimal(str(request.POST.get('valor', '0')).replace(',', '.')).quantize(Decimal('.01'))
                except (InvalidOperation, ValueError):
                    raise ValidationError('Valor de cobrança inválido.')
                venc_raw = request.POST.get('vencimento') or timezone.localdate().isoformat()
                venc = date.fromisoformat(venc_raw)
                cob = emitir_cobranca_bancaria(
                    request.user,
                    int(request.POST['integracao_id']),
                    valor=valor,
                    vencimento=venc,
                    tipo=request.POST.get('tipo', 'HIBRIDO'),
                    lancamento_id=lanc_id,
                    pagador_nome=request.POST.get('pagador_nome', ''),
                    pagador_doc=request.POST.get('pagador_doc', ''),
                    descricao=request.POST.get('descricao', 'Cobrança API Bancária'),
                )
                messages.success(request, f'Cobrança {cob.tipo} gerada · TXID {cob.txid}.')
            elif acao == 'sincronizar_extrato':
                res = sincronizar_extrato_api(request.user, int(request.POST['integracao_id']))
                messages.success(request, f"Extrato sincronizado: {res['importados']} lançamento(s), {res['conciliados']} baixa(s) automática(s).")
            elif acao == 'consultar_cobranca':
                cob = consultar_cobranca_bancaria(request.user, int(request.POST['cobranca_id']))
                messages.success(request, f'Cobrança {cob.txid} consultada: {cob.get_status_display()}.')
            elif acao == 'simular_pagamento_sandbox':
                cob = get_object_or_404(m.CobrancaBancaria, pk=int(request.POST['cobranca_id']))
                simular_liquidacao_sandbox(request.user, cob.pk)
                messages.success(request, f'Liquidação Sandbox confirmada para {cob.txid} e baixa lançada no Financeiro.')
            return redirect('operacao:bancos_integracao')
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))

    return render(request, 'operacao/bancos_integracao.html', {
        'contas': contas,
        'integracoes': integracoes,
        'cobrancas': cobrancas,
        'lancamentos_aberto': lancamentos_aberto,
        'extratos': extratos,
        'webhooks': webhooks,
        'provedores': m.IntegracaoBancaria.PROVEDORES,
        'ultimo_webhook_secret': ultimo_webhook_secret,
        'hoje_iso': timezone.localdate().isoformat(),
    })


@csrf_exempt
@require_POST
def webhook_banco_publico(request, org_id, integracao_id):
    """Endpoint público multi-tenant para callbacks de Pix/Boleto autenticado por hash de token."""
    integracao_raw = m.IntegracaoBancaria._base_manager.select_related('organization').filter(pk=integracao_id).first()
    if integracao_raw is None or str(integracao_raw.organization.public_id) != str(org_id):
        return JsonResponse({'erro': 'Integração não encontrada.'}, status=404)

    token = request.headers.get('X-Webhook-Token', '')
    if not token and request.headers.get('Authorization', '').lower().startswith('bearer '):
        token = request.headers['Authorization'][7:].strip()
    if not token:
        token = request.GET.get('token', '')

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({'erro': 'Payload JSON inválido.'}, status=400)

    try:
        evt = processar_webhook_bancario(integracao_raw.pk, payload, token=token)
    except PermissionDenied:
        return JsonResponse({'erro': 'Token de webhook inválido.'}, status=403)
    except ValidationError as exc:
        return JsonResponse({'erro': '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc)}, status=400)

    return JsonResponse({
        'status': 'ok',
        'evento_id': evt.evento_id,
        'cobranca_id': evt.cobranca_id,
        'processado': evt.processado,
    })
