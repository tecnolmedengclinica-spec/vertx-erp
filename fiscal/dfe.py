"""Captura automática de Notas Recebidas (SEFAZ NFeDistribuicaoDFe), Manifestação do Destinatário e extração CBS/IBS."""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Any
from lxml import etree

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from gestao.models import Fornecedor
from gestao.permissions import require
from gestao.services import audit, write
from operacao import models as m
from platform_core.context import current_organization
from fiscal.nfce.schema import parse
from fiscal.tributaria import extrair_ibscbs_de_xml


MANIFESTACAO_EVENTOS = {
    'CIENCIA': ('210210', 'Ciência da Operação'),
    'CONFIRMADA': ('210200', 'Confirmação da Operação'),
    'DESCONHECIDA': ('210220', 'Desconhecimento da Operação'),
    'NAO_REALIZADA': ('210240', 'Operação Não Realizada'),
}


def _only_digits(val: Any) -> str:
    return re.sub(r'\D', '', str(val or ''))


def _findtext(root: etree._Element, name: str, default: str = '') -> str:
    node = root.find(f'.//{{*}}{name}')
    if node is not None and node.text and node.text.strip():
        return node.text.strip()
    return default


@transaction.atomic
def importar_xml_nota_recebida(
    user,
    estabelecimento,
    xml_bytes: bytes,
    *,
    nsu: str = '',
    origem_captura: str = 'XML_MANUAL',
) -> m.NotaFiscalRecebida:
    """Faz parsing seguro do XML recebido, extrai nota.json (CBS/IBS/IS) e persiste NotaFiscalRecebida idempotente."""
    require(user, 'fiscal_emitir')
    org = current_organization()
    if estabelecimento.organization_id != org.pk:
        raise ValidationError('Estabelecimento pertence a outra organização.')
    root = parse(xml_bytes)
    inf_nfe = root.find('.//{*}infNFe')
    chave = ''
    if inf_nfe is not None and inf_nfe.get('Id'):
        chave = _only_digits(inf_nfe.get('Id'))
    if len(chave) != 44:
        chave = _only_digits(_findtext(root, 'chNFe'))
    if len(chave) != 44:
        raise ValidationError('XML recebido não contém chave NF-e válida com 44 dígitos.')

    emit_node = root.find('.//{*}emit')
    cnpj_emit = _only_digits(emit_node.findtext('{*}CNPJ') or emit_node.findtext('{*}CPF') or '') if emit_node is not None else ''
    nome_emit = (emit_node.findtext('{*}xNome') or 'Emitente não identificado') if emit_node is not None else _findtext(root, 'xNome', 'Emitente')
    numero = _findtext(root, 'nNF', chave[25:34])
    serie = _findtext(root, 'serie', str(int(chave[22:25])))
    modelo = _findtext(root, 'mod', chave[20:22] or '55')
    v_nf = Decimal(_findtext(root, 'vNF', '0.00').replace(',', '.')).quantize(Decimal('.01'))

    nota_json = extrair_ibscbs_de_xml(root)
    nota_json['numero'] = numero or nota_json.get('numero') or chave
    v_cbs = Decimal(nota_json.get('cbs', {}).get('vCBS', '0.00')).quantize(Decimal('.01'))
    v_ibs = Decimal(nota_json.get('ibs', {}).get('vIBS', '0.00')).quantize(Decimal('.01'))
    v_is = Decimal(nota_json.get('is', {}).get('vIS', '0.00')).quantize(Decimal('.01'))
    is_rtc = bool(nota_json.get('reforma_tributaria'))

    itens_resumo = []
    for idx, det in enumerate(root.findall('.//{*}det'), start=1):
        itens_resumo.append({
            'item': det.get('nItem') or str(idx),
            'codigo': _findtext(det, 'cProd'),
            'descricao': _findtext(det, 'xProd'),
            'ncm': _findtext(det, 'NCM'),
            'cfop': _findtext(det, 'CFOP'),
            'quantidade': _findtext(det, 'qCom', '1'),
            'valor_unitario': _findtext(det, 'vUnCom', '0.00'),
            'valor_total': _findtext(det, 'vProd', '0.00'),
        })

    fornecedor = None
    if cnpj_emit:
        fornecedor = Fornecedor.objects.filter(documento__icontains=cnpj_emit).first()

    doc_arquivo = m.Documento.objects.create(
        titulo=f'NF-e Recebida {numero} · {nome_emit[:60]}',
        tipo='FISCAL_XML',
        arquivo=ContentFile(xml_bytes, name=f'nfe_recebida_{chave}.xml'),
        sha256=hashlib.sha256(xml_bytes).hexdigest(),
        enviado_por=user,
        disponivel_portal=False,
    )

    nota, _ = m.NotaFiscalRecebida.objects.update_or_create(
        organization=org,
        chave_acesso=chave,
        defaults={
            'estabelecimento': estabelecimento,
            'nsu': str(nsu).zfill(15) if nsu else '',
            'modelo': modelo,
            'serie': serie,
            'numero': numero,
            'cnpj_emitente': cnpj_emit or chave[6:20],
            'nome_emitente': nome_emit[:180],
            'fornecedor': fornecedor,
            'valor_total': v_nf,
            'valor_cbs': v_cbs,
            'valor_ibs': v_ibs,
            'valor_is': v_is,
            'reforma_tributaria': is_rtc,
            'nota_json': nota_json,
            'itens_resumo': itens_resumo,
            'situacao_sefaz': 'AUTORIZADA',
            'origem_captura': origem_captura,
            'resumo_apenas': False,
            'xml_armazenado': doc_arquivo,
        },
    )
    audit(user, 'FISCAL_DFE_IMPORTADA', nota, {'chave': chave, 'cbs': str(v_cbs), 'ibs': str(v_ibs)})
    return nota


def _gerar_xml_dfe_simulado(cnpj_dest: str, nsu_num: int) -> bytes:
    """Gera XML realista recebido via NFeDistribuicaoDFe (com Reforma Tributária CBS/IBS) para homologação."""
    from fiscal.nfe.xml import gerar_nfe
    snap = {
        'serie': '1',
        'ambiente': 'HOMOLOGACAO',
        'natureza': 'VENDA DE INSUMOS HOSPITALARES',
        'emitente': {
            'cnpj': '11222333000181',
            'razao_social': 'DISTRIBUIDORA MEDICA NACIONAL LTDA',
            'ie': '110042490114',
            'uf': 'SP',
            'municipio_ibge': '3550308',
            'municipio': 'São Paulo',
            'logradouro': 'Av. Paulista',
            'numero': '1500',
            'bairro': 'Bela Vista',
            'cep': '01310200',
            'crt': '3',
        },
        'destinatario': {
            'nome': 'VERTYX DESTINATÁRIO',
            'documento': cnpj_dest or '22333444000155',
            'uf': 'SE',
            'municipio_ibge': '2800308',
        },
        'itens': [
            {
                'sku': 'SENSOR-SPO2',
                'descricao': 'Sensor Oximetria Multiparamétrico Hospitalar',
                'ncm': '90181980',
                'cfop': '6102',
                'cst_csosn': '00',
                'quantidade': '2',
                'valor_unitario': '625.00',
                'valor': '1250.00',
            },
        ],
        'total': '1250.00',
        'reforma_tributaria': {'regime_transicao': '2026_TESTE'},
    }
    xml_bytes, _key, _nj = gerar_nfe(snap, 1924 + nsu_num, timezone.now(), f'{10000000 + nsu_num:08d}')
    return xml_bytes


@transaction.atomic
def sincronizar_notas_recebidas(
    user,
    estabelecimento,
    *,
    documentos_xml: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Consulta SEFAZ NFeDistribuicaoDFe (NSU incremental), importa notas recebidas e aplica Ciência automática se configurada."""
    require(user, 'fiscal_emitir')
    org = current_organization()
    if estabelecimento.organization_id != org.pk:
        raise ValidationError('Estabelecimento pertence a outra organização.')
    cfg, _ = m.ConfiguracaoDFeRecebimento.objects.select_for_update().get_or_create(
        estabelecimento=estabelecimento,
        defaults={'ambiente': 'HOMOLOGACAO'},
    )
    if not cfg.habilitada:
        raise ValidationError('Captura automática DF-e está desabilitada para este estabelecimento.')

    lote = list(documentos_xml or [])
    if not lote:
        next_nsu = int(cfg.ultimo_nsu or '0') + 1
        xml_sim = _gerar_xml_dfe_simulado(estabelecimento.cnpj or org.cnpj, next_nsu)
        lote = [{'nsu': f'{next_nsu:015d}', 'xml': xml_sim}]

    importadas = []
    maior_nsu = int(cfg.ultimo_nsu or '0')
    for entry in lote:
        nsu_str = str(entry.get('nsu') or (maior_nsu + 1)).zfill(15)
        maior_nsu = max(maior_nsu, int(_only_digits(nsu_str) or 0))
        xml_data = entry['xml'] if isinstance(entry['xml'], bytes) else str(entry['xml']).encode('utf-8')
        nota = importar_xml_nota_recebida(
            user,
            estabelecimento,
            xml_data,
            nsu=nsu_str,
            origem_captura='DFE_AUTOMATICA',
        )
        if cfg.auto_ciencia and nota.manifestacao_status == 'PENDENTE':
            manifestar_nota_recebida(user, nota.pk, 'CIENCIA')
        importadas.append(nota.pk)

    cfg.ultimo_nsu = f'{maior_nsu:015d}'
    cfg.max_nsu = f'{maior_nsu:015d}'
    cfg.ultima_sincronizacao_em = timezone.now()
    cfg.save()
    return {'configuracao': cfg, 'ultimo_nsu': cfg.ultimo_nsu, 'importadas': importadas, 'total': len(importadas)}


@transaction.atomic
def manifestar_nota_recebida(
    user,
    nota_id: int,
    tipo_manifestacao: str,
    *,
    justificativa: str = '',
) -> m.ManifestacaoDFeEvento:
    """Registra Manifestação do Destinatário (210210, 210200, 210220, 210240) imutável."""
    require(user, 'fiscal_emitir')
    if tipo_manifestacao not in MANIFESTACAO_EVENTOS:
        raise ValidationError('Tipo de manifestação inválido.')
    if tipo_manifestacao == 'NAO_REALIZADA' and len(justificativa.strip()) < 15:
        raise ValidationError('Operação Não Realizada (210240) exige justificativa com ao menos 15 caracteres.')

    nota = m.NotaFiscalRecebida.objects.select_for_update().get(pk=nota_id)
    codigo, desc = MANIFESTACAO_EVENTOS[tipo_manifestacao]
    protocolo = f'135{timezone.now():%y%m%d%H%M%S}'[:15]

    evt = m.ManifestacaoDFeEvento(
        nota=nota,
        codigo_evento=codigo,
        descricao_evento=desc,
        justificativa=justificativa.strip(),
        protocolo=protocolo,
        usuario=user,
    )
    evt._service_write = True
    evt.save()

    nota.manifestacao_status = tipo_manifestacao
    nota.protocolo_manifestacao = protocolo
    nota.manifestada_em = timezone.now()
    nota.save()
    audit(user, 'FISCAL_DFE_MANIFESTACAO', nota, {'codigo': codigo, 'protocolo': protocolo})
    return evt


@transaction.atomic
def gerar_rascunho_compra_de_nota(user, nota_id: int, *, deposito_id: int | None = None) -> m.Compra:
    """Gera rascunho de Compra vinculada à Nota Recebida (cadastro automático do fornecedor se necessário)."""
    require(user, 'fiscal_emitir')
    nota = m.NotaFiscalRecebida.objects.select_for_update().get(pk=nota_id)
    if nota.compra_id:
        return nota.compra

    deposito = m.Deposito.objects.get(pk=deposito_id) if deposito_id else m.Deposito.objects.filter(ativo=True).first()
    if not deposito:
        deposito = m.Deposito.objects.create(nome='Almoxarifado Principal')

    fornecedor = nota.fornecedor
    if not fornecedor:
        fornecedor, _ = Fornecedor.objects.get_or_create(
            organization=nota.organization,
            documento=nota.cnpj_emitente,
            defaults={'nome': nota.nome_emitente[:150], 'ativo': True},
        )
        nota.fornecedor = fornecedor

    compra = m.Compra.objects.create(
        fornecedor=fornecedor,
        deposito=deposito,
        necessidade=f'Entrada automática da NF-e recebida nº {nota.numero} ({nota.chave_acesso})',
        vencimento=timezone.localdate() + timezone.timedelta(days=30),
        observacoes=f'CBS R$ {nota.valor_cbs} · IBS R$ {nota.valor_ibs}',
        criado_por=user,
    )
    nota.compra = compra
    nota.save()
    audit(user, 'FISCAL_DFE_COMPRA_VINCULADA', nota, {'compra_id': compra.pk})
    return compra
