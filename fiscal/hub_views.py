"""Views da Central Fiscal Unificada (NF-e 55, NFS-e Contingência, NFC-e, CBS/IBS nota.json e Notas Recebidas DF-e)."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from clientes.models import Cliente
from gestao.permissions import allowed, require
from operacao import models as m
from platform_core.context import current_organization
from platform_core.models import FilialEmpresa
from fiscal.dfe import (
    gerar_rascunho_compra_de_nota,
    importar_xml_nota_recebida,
    manifestar_nota_recebida,
    sincronizar_notas_recebidas,
)
from fiscal.motors.nfe import MotorNFeHomologacao, emitir_nfe_hub
from fiscal.nfce.contingencia import emitir_nfce_pdv_com_rtc, sincronizar_nfce_contingencia
from fiscal.nfe.danfe import gerar_danfe_nfe_pdf
from fiscal.nfse.contingencia import emitir_nfse_com_contingencia, sincronizar_nfse_contingencia
from fiscal.tributaria import REGIMES_TRANSICAO, gerar_nota_json_rtc


def _default_estabelecimento():
    estab = FilialEmpresa.objects.filter(ativo=True).first()
    if estab is None:
        org = current_organization()
        estab = FilialEmpresa.objects.create(
            organization=org,
            codigo='MATRIZ',
            nome=org.nome_fantasia or org.razao_social or 'Matriz',
            cnpj=org.cnpj or '00000000000191',
            endereco=org.endereco or 'Endereço da Matriz',
            ativo=True,
        )
    return estab


def _nota_json_de_documento(doc: m.DocumentoFiscal) -> dict:
    for rel in ('detalhesnfe', 'detalhesnfse', 'detalhesnfce'):
        det = getattr(doc, rel, None)
        if det and getattr(det, 'nota_json', None):
            return det.nota_json
    snap_nj = (doc.snapshot_fiscal or {}).get('nota_json')
    if snap_nj:
        return snap_nj
    return gerar_nota_json_rtc(
        [{'nItem': '1', 'descricao': doc.descricao or doc.get_tipo_display(), 'valor_base': str(doc.valor)}],
        numero=doc.numero or str(doc.pk),
        chave=doc.chave or '',
        modelo=doc.modelo or doc.tipo,
        serie=doc.serie or '1',
        data_emissao=(doc.autorizado_em or timezone.now()).isoformat(timespec='seconds'),
        regime_transicao='2026_TESTE',
    )


def _parse_decimal(raw: str, default: str = '100.00') -> Decimal:
    try:
        val = Decimal(str(raw or default).replace(',', '.')).quantize(Decimal('.01'))
    except (InvalidOperation, ValueError):
        raise ValidationError('Valor monetário inválido.')
    if val <= Decimal('0.00'):
        raise ValidationError('Informe um valor positivo.')
    return val


@login_required
def fiscal_hub(request):
    require(request.user, 'fiscal_emitir')
    estabs = list(FilialEmpresa.objects.filter(ativo=True))
    clientes = list(Cliente.objects.order_by('nome_razao_social')[:100])
    regime_sim = request.GET.get('regime', '2026_TESTE')
    valor_sim = request.GET.get('valor_sim', '1250.00')

    if request.method == 'POST':
        acao = request.POST.get('acao', '')
        estab_id = request.POST.get('estabelecimento_id')
        estab = FilialEmpresa.objects.filter(pk=estab_id).first() if estab_id else _default_estabelecimento()
        cliente = Cliente.objects.filter(pk=request.POST.get('cliente_id')).first() if request.POST.get('cliente_id') else None
        regime = request.POST.get('regime_transicao') or '2026_TESTE'
        try:
            if acao == 'emitir_nfe':
                val = _parse_decimal(request.POST.get('valor', '250.00'))
                desc = (request.POST.get('descricao') or 'Equipamento / Produto Hospitalar').strip()
                res = emitir_nfe_hub(
                    request.user,
                    estab,
                    cliente=cliente,
                    descricao=desc,
                    valor=val,
                    modo_emissao=request.POST.get('modo_emissao', 'NORMAL'),
                    motivo_contingencia=request.POST.get('motivo_contingencia', ''),
                    regime_transicao=None if regime == 'SEM_RTC' else regime,
                )
                messages.success(request, f"NF-e nº {res['documento'].numero} processada ({res['status']}) · Chave {res['chave'][:12]}...")
            elif acao == 'emitir_nfse':
                if not cliente:
                    cliente = Cliente.objects.first()
                if not cliente:
                    raise ValidationError('Cadastre ao menos um cliente/tomador antes de emitir NFS-e.')
                val = _parse_decimal(request.POST.get('valor', '380.00'))
                desc = (request.POST.get('descricao') or 'Serviço de Engenharia / Manutenção Técnica').strip()
                contingencia_direta = request.POST.get('modo_nfse') == 'CONTINGENCIA_RPS'
                simular_falha = request.POST.get('simular_falha_adn') == '1'
                res = emitir_nfse_com_contingencia(
                    request.user,
                    estab,
                    cliente=cliente,
                    codigo_servico=request.POST.get('codigo_servico', '010701'),
                    municipio=request.POST.get('municipio', '2800308'),
                    descricao=desc,
                    valor=val,
                    usar_contingencia_direta=contingencia_direta,
                    simular_falha_adn=simular_falha,
                    motivo_contingencia=request.POST.get('motivo_contingencia') or 'Indisponibilidade temporária ADN/SEFIN',
                    regime_transicao=None if regime == 'SEM_RTC' else regime,
                )
                messages.success(request, f"NFS-e nº {res['documento'].numero} emitida em estado {res['status']}.")
            elif acao == 'emitir_nfce':
                val = _parse_decimal(request.POST.get('valor', '89.90'))
                desc = (request.POST.get('descricao') or 'Venda Varejo / PDV').strip()
                off = request.POST.get('contingencia_offline') == '1'
                res = emitir_nfce_pdv_com_rtc(
                    request.user,
                    estab,
                    itens=[{'sku': 'PDV-01', 'descricao': desc, 'valor': str(val)}],
                    cliente=cliente,
                    contingencia_offline=off,
                    motivo_contingencia=request.POST.get('motivo_contingencia') or 'PDV operando em Contingência Offline (tpEmis=9)',
                    regime_transicao=None if regime == 'SEM_RTC' else regime,
                )
                messages.success(request, f"NFC-e nº {res['documento'].numero} emitida ({res['status']}).")
            elif acao == 'sincronizar_nfse':
                res = sincronizar_nfse_contingencia(
                    request.user,
                    documento_id=int(request.POST['documento_id']) if request.POST.get('documento_id') else None,
                )
                messages.success(request, f"{res['total_sincronizadas']} NFS-e(s) em contingência sincronizadas com ADN/SEFIN.")
            elif acao == 'sincronizar_nfce':
                res = sincronizar_nfce_contingencia(
                    request.user,
                    documento_id=int(request.POST['documento_id']) if request.POST.get('documento_id') else None,
                )
                messages.success(request, f"{res['total_sincronizadas']} NFC-e(s) offline sincronizadas com a SEFAZ.")
            elif acao == 'cce_nfe':
                motor = MotorNFeHomologacao()
                ret = motor.carta_correcao_documento(request.user, int(request.POST['documento_id']), request.POST.get('correcao', ''))
                messages.success(request, f"Carta de Correção seq {ret['n_seq']} registrada · Protocolo {ret['protocolo']}.")
            elif acao == 'cancelar_nfe':
                motor = MotorNFeHomologacao()
                ret = motor.cancelar_documento(request.user, int(request.POST['documento_id']), request.POST.get('justificativa', ''))
                messages.success(request, f"NF-e cancelada na SEFAZ · Protocolo {ret['protocolo']}.")
            return redirect('operacao:fiscal_hub')
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))

    try:
        sim_val = _parse_decimal(valor_sim, '1250.00')
    except ValidationError:
        sim_val = Decimal('1250.00')
    preview_nota_json = gerar_nota_json_rtc(
        [{'nItem': '1', 'descricao': 'Equipamento Médico / Serviço', 'valor_base': str(sim_val)}],
        numero='135240001924',
        modelo='55',
        serie='1',
        data_emissao=timezone.now().isoformat(timespec='seconds'),
        regime_transicao=regime_sim if regime_sim in REGIMES_TRANSICAO else '2026_TESTE',
    )

    docs = list(
        m.DocumentoFiscal.objects.select_related('cliente', 'estabelecimento', 'detalhesnfe', 'detalhesnfse', 'detalhesnfce')
        .order_by('-pk')[:25]
    )
    nfse_contingencia = [d for d in docs if d.tipo == 'NFSE' and d.status == 'CONTINGENCIA']
    nfce_contingencia = [d for d in docs if d.tipo == 'NFCE' and d.status == 'CONTINGENCIA']
    recebidas_count = m.NotaFiscalRecebida.objects.count()

    return render(request, 'fiscal/hub.html', {
        'estabelecimentos': estabs or [_default_estabelecimento()],
        'clientes': clientes,
        'documentos': docs,
        'nfse_contingencia': nfse_contingencia,
        'nfce_contingencia': nfce_contingencia,
        'recebidas_count': recebidas_count,
        'regimes': REGIMES_TRANSICAO,
        'regime_sim': regime_sim,
        'valor_sim': str(sim_val),
        'preview_nota_json_pretty': json.dumps(preview_nota_json, indent=2, ensure_ascii=False),
        'can_cancel': allowed(request.user, 'fiscal_cancelar'),
    })


@login_required
def fiscal_nfe_danfe(request, pk):
    require(request.user, 'fiscal_emitir')
    doc = get_object_or_404(m.DocumentoFiscal.objects.select_related('cliente', 'estabelecimento', 'detalhesnfe'), pk=pk, tipo='NFE')
    pdf_doc = m.Documento.objects.filter(fiscal=doc, tipo='FISCAL_PDF').order_by('-pk').first()
    if pdf_doc and pdf_doc.arquivo:
        with pdf_doc.arquivo.open('rb') as fh:
            data = fh.read()
        resp = HttpResponse(data, content_type='application/pdf')
        resp['Content-Disposition'] = f'inline; filename="danfe_nfe_{doc.numero or doc.pk}.pdf"'
        return resp
    nota_json = _nota_json_de_documento(doc)
    pdf_bytes = gerar_danfe_nfe_pdf({
        'emitente': (doc.snapshot_fiscal or {}).get('emitente') or {'razao_social': doc.organization.razao_social, 'cnpj': doc.organization.cnpj},
        'destinatario': {'nome': doc.cliente.nome_razao_social, 'documento': doc.cliente.cpf_cnpj} if doc.cliente else {},
        'itens': (doc.snapshot_fiscal or {}).get('itens') or [{'descricao': doc.descricao, 'ncm': '90189099', 'cfop': '5102', 'quantidade': '1', 'valor_unitario': str(doc.valor), 'valor': str(doc.valor)}],
        'totais': {'vProd': str(doc.valor), 'vNF': str(doc.valor)},
        'nota_json': nota_json,
        'chave': doc.chave or ('0' * 44),
        'numero': doc.numero or str(doc.pk),
        'serie': doc.serie or '1',
        'protocolo': doc.protocolo or 'PRÉVIA',
        'data_emissao': timezone.now().strftime('%d/%m/%Y %H:%M'),
    })
    return HttpResponse(pdf_bytes, content_type='application/pdf')


@login_required
def fiscal_nota_json(request, pk):
    require(request.user, 'fiscal_emitir')
    doc = get_object_or_404(m.DocumentoFiscal.objects.select_related('detalhesnfe', 'detalhesnfse', 'detalhesnfce'), pk=pk)
    return JsonResponse(_nota_json_de_documento(doc), json_dumps_params={'indent': 2, 'ensure_ascii': False})


@login_required
def fiscal_notas_recebidas(request):
    require(request.user, 'fiscal_emitir')
    estab = _default_estabelecimento()
    if request.method == 'POST':
        acao = request.POST.get('acao', '')
        try:
            if acao == 'sincronizar_dfe':
                res = sincronizar_notas_recebidas(request.user, estab)
                messages.success(request, f"DF-e sincronizado até NSU {res['ultimo_nsu']} · {res['total']} nota(s) recebida(s).")
            elif acao == 'importar_xml':
                uploaded = request.FILES.get('arquivo_xml')
                raw_xml = request.POST.get('xml_conteudo', '').strip()
                xml_bytes = uploaded.read() if uploaded else raw_xml.encode('utf-8')
                if not xml_bytes:
                    raise ValidationError('Envie um arquivo XML NF-e válido.')
                nota = importar_xml_nota_recebida(request.user, estab, xml_bytes)
                messages.success(request, f"NF-e nº {nota.numero} importada (CBS R$ {nota.valor_cbs} · IBS R$ {nota.valor_ibs}).")
            elif acao == 'manifestar':
                evt = manifestar_nota_recebida(
                    request.user,
                    int(request.POST['nota_id']),
                    request.POST['tipo_manifestacao'],
                    justificativa=request.POST.get('justificativa', ''),
                )
                messages.success(request, f"Manifestação {evt.codigo_evento} ({evt.descricao_evento}) protocolada: {evt.protocolo}.")
            elif acao == 'gerar_compra':
                compra = gerar_rascunho_compra_de_nota(request.user, int(request.POST['nota_id']))
                messages.success(request, f"Rascunho de compra COMP-{compra.pk:06d} vinculado à nota recebida.")
            return redirect('operacao:fiscal_notas_recebidas')
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))

    cfg = m.ConfiguracaoDFeRecebimento.objects.filter(estabelecimento=estab).first()
    notas = list(m.NotaFiscalRecebida.objects.select_related('fornecedor', 'compra', 'estabelecimento').all()[:50])
    return render(request, 'fiscal/notas_recebidas.html', {
        'estabelecimento': estab,
        'configuracao_dfe': cfg,
        'notas': notas,
        'ultima_nota_json': json.dumps(notas[0].nota_json, indent=2, ensure_ascii=False) if notas else '',
    })


@login_required
def fiscal_nota_recebida_detalhe(request, pk):
    require(request.user, 'fiscal_emitir')
    nota = get_object_or_404(m.NotaFiscalRecebida.objects.select_related('fornecedor', 'compra', 'estabelecimento'), pk=pk)
    if request.method == 'POST':
        acao = request.POST.get('acao', '')
        try:
            if acao == 'manifestar':
                evt = manifestar_nota_recebida(
                    request.user,
                    nota.pk,
                    request.POST['tipo_manifestacao'],
                    justificativa=request.POST.get('justificativa', ''),
                )
                messages.success(request, f"Manifestação {evt.codigo_evento} registrada: {evt.protocolo}.")
            elif acao == 'gerar_compra':
                compra = gerar_rascunho_compra_de_nota(request.user, nota.pk)
                messages.success(request, f"Compra COMP-{compra.pk:06d} gerada a partir do XML.")
            return redirect('operacao:fiscal_nota_recebida_detalhe', pk=nota.pk)
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))

    eventos = list(nota.manifestacoes.select_related('usuario').all())
    return render(request, 'fiscal/nota_recebida_detalhe.html', {
        'nota': nota,
        'eventos': eventos,
        'nota_json_pretty': json.dumps(nota.nota_json, indent=2, ensure_ascii=False),
    })
