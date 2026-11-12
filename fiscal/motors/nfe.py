"""Motores de NF-e (Modelo 55 · Produtos): Simulado preservado e Homologação SEFAZ v4.00 + Reforma Tributária."""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from gestao.permissions import require
from gestao.services import audit, write
from operacao import models as m
from platform_core.context import current_organization
from fiscal.nfe.danfe import gerar_danfe_nfe_pdf
from fiscal.nfe.sefaz import ClienteSEFAZNFe
from fiscal.nfe.xml import MODOS_EMISSAO_TPEMIS, construir_evento_nfe, gerar_nfe
from .base import MotorSimulado


class MotorNFeSimulado(MotorSimulado):
    tipo = 'NFE'
    modelo = '55'

    def validar(self, documento):
        super().validar(documento)
        if not hasattr(documento, 'detalhesnfe') or not documento.venda_id or not documento.detalhesnfe.itens_snapshot:
            raise ValidationError('NF-e simulada exige venda com itens.')


def _resolver_emitente_snapshot(doc: m.DocumentoFiscal) -> dict[str, Any]:
    org = doc.organization
    estab = doc.estabelecimento
    perfil = getattr(estab, 'perfil_fiscal', None) if estab else None
    end = dict(getattr(perfil, 'endereco_estruturado', {}) or {})
    uf = (getattr(perfil, 'uf', '') or org.uf or 'SE').upper()[:2]
    mun_ibge = getattr(perfil, 'municipio_ibge', '') or '2800308'
    cnpj = (estab.cnpj if estab and estab.cnpj else org.cnpj) or '00000000000191'
    razao = org.razao_social or (estab.nome if estab else 'VERTYX ERP')
    ie = (estab.inscricao_estadual if estab else '') or 'ISENTO'
    end_txt = (estab.endereco if estab else org.endereco) or 'Endereço fiscal do emitente'
    return {
        'cnpj': ''.join(ch for ch in cnpj if ch.isdigit()).zfill(14)[:14],
        'razao_social': razao,
        'ie': ie,
        'uf': uf,
        'municipio_ibge': mun_ibge,
        'municipio': end.get('municipio') or org.cidade or 'Aracaju',
        'municipio_nome': end.get('municipio') or org.cidade or 'Aracaju',
        'logradouro': end.get('logradouro') or end_txt,
        'numero': end.get('numero') or 'S/N',
        'bairro': end.get('bairro') or 'Centro',
        'cep': end.get('cep') or '49000000',
        'crt': (doc.regra_fiscal.parametros.get('crt') if doc.regra_fiscal_id and isinstance(doc.regra_fiscal.parametros, dict) else None) or '1',
        'endereco': {
            'logradouro': end.get('logradouro') or end_txt,
            'numero': end.get('numero') or 'S/N',
            'bairro': end.get('bairro') or 'Centro',
            'cep': end.get('cep') or '49000000',
        },
    }


def _resolver_destinatario_snapshot(doc: m.DocumentoFiscal) -> dict[str, Any]:
    cli = doc.cliente
    if not cli:
        return {'nome': 'Consumidor não identificado', 'documento': '', 'uf': 'SE', 'municipio_ibge': '2800308'}
    return {
        'nome': cli.nome_razao_social,
        'documento': cli.cpf_cnpj,
        'uf': (getattr(cli, 'uf', '') or doc.organization.uf or 'SE')[:2],
        'municipio_ibge': getattr(cli, 'codigo_municipio_ibge', '') or '2800308',
        'municipio': getattr(cli, 'cidade', '') or doc.organization.cidade or 'Aracaju',
        'logradouro': getattr(cli, 'endereco', '') or 'Endereço destinatário',
        'numero': 'S/N',
        'bairro': 'Centro',
        'cep': '49000000',
        'ie': getattr(cli, 'inscricao_estadual', '') or '',
    }


def _resolver_itens_snapshot(doc: m.DocumentoFiscal) -> list[dict[str, Any]]:
    snap = dict(doc.snapshot_fiscal or {})
    det = getattr(doc, 'detalhesnfe', None)
    raw_items = list(snap.get('itens') or (det.itens_snapshot if det else []) or [])
    if not raw_items and doc.venda_id:
        raw_items = [
            {
                'sku': item.peca.sku,
                'descricao': item.peca.descricao,
                'ncm': item.peca.ncm or '90189099',
                'cfop': item.peca.cfop or '5102',
                'cst_csosn': item.peca.csosn or item.peca.cst_csosn or '102',
                'quantidade': str(item.quantidade),
                'valor_unitario': str(item.valor_unitario),
                'desconto': str(item.desconto),
                'valor': str(Decimal(item.quantidade) * Decimal(item.valor_unitario) - Decimal(item.desconto)),
            }
            for item in doc.venda.itens.select_related('peca')
        ]
    if not raw_items:
        raw_items = [{
            'sku': 'SERV-PROD-01',
            'descricao': doc.descricao or 'Mercadoria / Produto NF-e',
            'ncm': '90189099',
            'cfop': '5102',
            'cst_csosn': '102',
            'quantidade': '1',
            'valor_unitario': str(doc.valor),
            'desconto': '0.00',
            'valor': str(doc.valor),
        }]
    normalized = []
    for item in raw_items:
        qtd = Decimal(str(item.get('quantidade') or '1'))
        vunit = Decimal(str(item.get('valor_unitario') or item.get('valor') or doc.valor))
        desc = Decimal(str(item.get('desconto') or '0'))
        total_item = item.get('valor') or item.get('liquido') or str((qtd * vunit - desc).quantize(Decimal('.01')))
        normalized.append({
            **item,
            'ncm': ''.join(ch for ch in str(item.get('ncm') or '90189099') if ch.isdigit()).zfill(8)[:8],
            'cfop': ''.join(ch for ch in str(item.get('cfop') or '5102') if ch.isdigit()).zfill(4)[:4],
            'quantidade': str(qtd),
            'valor_unitario': str(vunit),
            'desconto': str(desc),
            'valor': str(total_item),
            'valor_base': str(total_item),
        })
    return normalized


class MotorNFeHomologacao:
    """Motor oficial NF-e Modelo 55 (SEFAZ v4.00 · Homologação, Contingência SVC/FS-DA/EPEC e CBS/IBS)."""

    tipo = 'NFE'
    modelo = '55'
    ambiente = 'HOMOLOGACAO'

    def __init__(self, *, transporte=None):
        self.transporte = transporte

    def validar(self, documento: m.DocumentoFiscal) -> None:
        documento.full_clean()
        if documento.tipo != 'NFE':
            raise ValidationError('Motor NF-e exclusivo para documentos modelo 55 (NFE).')
        if documento.ambiente == 'PRODUCAO' and self.transporte is None:
            raise ValidationError('Produção NF-e exige transporte mTLS ativo.')
        if not documento.estabelecimento_id:
            raise ValidationError('NF-e exige estabelecimento emitente.')

    @transaction.atomic
    def emitir_documento(
        self,
        user,
        documento_id: int,
        *,
        modo_emissao: str = 'NORMAL',
        motivo_contingencia: str = '',
        regime_transicao: str | None = '2026_TESTE',
        rtc_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require(user, 'fiscal_emitir')
        if modo_emissao not in MODOS_EMISSAO_TPEMIS:
            raise ValidationError(f'Modo de emissão NF-e inválido: {modo_emissao}.')

        doc = m.DocumentoFiscal.objects.select_for_update().get(pk=documento_id)
        self.validar(doc)
        if doc.status not in ('RASCUNHO', 'CONTINGENCIA', 'REJEITADA'):
            raise ValidationError(f'Documento no estado {doc.status} não pode ser reemitido.')

        old_status = doc.status
        if not doc.numero:
            if doc.serie_fiscal_id:
                serie_obj = m.SerieFiscal.objects.select_for_update().get(pk=doc.serie_fiscal_id)
                numero_int = serie_obj.proximo_numero
                serie_obj.proximo_numero += 1
                write(serie_obj)
                write(m.ReservaNumeracaoFiscal(serie=serie_obj, documento=doc, numero=numero_int))
            else:
                ultimo = (
                    m.DocumentoFiscal.objects.filter(
                        estabelecimento=doc.estabelecimento,
                        tipo='NFE',
                        serie=doc.serie or '1',
                    )
                    .exclude(numero='')
                    .count()
                )
                numero_int = ultimo + 1
            doc.numero = str(numero_int)
        else:
            numero_int = int(doc.numero)

        emit = _resolver_emitente_snapshot(doc)
        dest = _resolver_destinatario_snapshot(doc)
        itens = _resolver_itens_snapshot(doc)
        rtc_cfg = dict(rtc_override or (doc.impostos or {}).get('rtc') or {})
        if not rtc_cfg and regime_transicao:
            rtc_cfg = {'regime_transicao': regime_transicao}

        snapshot_nfe = {
            'serie': doc.serie or '1',
            'ambiente': doc.ambiente if doc.ambiente in ('HOMOLOGACAO', 'PRODUCAO') else 'HOMOLOGACAO',
            'natureza': (doc.snapshot_fiscal or {}).get('natureza') or 'VENDA DE MERCADORIA',
            'emitente': emit,
            'destinatario': dest,
            'itens': itens,
            'total': str(doc.valor),
            'observacoes': doc.descricao,
        }
        if rtc_cfg:
            snapshot_nfe['reforma_tributaria'] = rtc_cfg

        emissao_dt = timezone.now()
        cnf = f'{(doc.pk * 7919 + numero_int) % 100000000:08d}'
        xml_bytes, chave, nota_json = gerar_nfe(
            snapshot_nfe,
            numero_int,
            emissao_dt,
            cnf,
            modo_emissao=modo_emissao,
            motivo_contingencia=motivo_contingencia,
            contingencia_em=emissao_dt if modo_emissao != 'NORMAL' else None,
        )

        cliente = ClienteSEFAZNFe(transporte=self.transporte, simulacao=(self.transporte is None))
        cliente.validar_estrutura(xml_bytes, sem_assinatura=True)
        retorno = cliente.autorizar(
            xml_bytes,
            chave=chave,
            uf=emit['uf'],
            ambiente=snapshot_nfe['ambiente'],
            modo_emissao=modo_emissao,
        )

        doc.modelo = '55'
        doc.chave = chave
        doc.protocolo = retorno.protocolo
        if retorno.status == 'AUTORIZADA':
            if modo_emissao in ('FS_DA', 'EPEC') and self.transporte is None:
                doc.status = 'CONTINGENCIA'
            else:
                doc.status = 'AUTORIZADA'
                doc.autorizado_em = emissao_dt
        else:
            doc.status = retorno.status
        merged_snapshot = dict(doc.snapshot_fiscal or {})
        merged_snapshot.update({
            'emitente': emit,
            'destinatario': dest,
            'itens': itens,
            'modo_emissao': modo_emissao,
            'motivo_contingencia': motivo_contingencia,
            'nota_json': nota_json or {},
        })
        doc.snapshot_fiscal = merged_snapshot
        write(doc)

        det = getattr(doc, 'detalhesnfe', None)
        if det is None:
            det = m.DetalhesNFe(documento=doc, itens_snapshot=itens)
        det.modo_emissao = modo_emissao if modo_emissao in ('NORMAL', 'SVC_AN', 'SVC_RS', 'FS_DA', 'EPEC') else 'NORMAL'
        det.motivo_contingencia = motivo_contingencia[:200]
        det.nota_json = nota_json or {}
        det._service_write = True
        det.save()

        danfe_pdf = gerar_danfe_nfe_pdf({
            'emitente': emit,
            'destinatario': dest,
            'itens': itens,
            'totais': {'vProd': str(doc.valor), 'vNF': str(doc.valor)},
            'nota_json': nota_json or {},
            'chave': chave,
            'numero': doc.numero,
            'serie': doc.serie or '1',
            'protocolo': doc.protocolo,
            'data_emissao': emissao_dt.strftime('%d/%m/%Y %H:%M'),
            'ambiente_label': f"{snapshot_nfe['ambiente']} · {modo_emissao}",
        })

        m.Documento.objects.create(
            titulo=f'NF-e {doc.numero} XML ({modo_emissao})',
            tipo='FISCAL_XML',
            arquivo=ContentFile(xml_bytes, name=f'nfe_{doc.numero}_{chave}.xml'),
            cliente=doc.cliente,
            fiscal=doc,
            sha256=hashlib.sha256(xml_bytes).hexdigest(),
            enviado_por=user,
            disponivel_portal=True,
        )
        m.Documento.objects.create(
            titulo=f'DANFE NF-e {doc.numero} (Mod. 55)',
            tipo='FISCAL_PDF',
            arquivo=ContentFile(danfe_pdf, name=f'danfe_nfe_{doc.numero}.pdf'),
            cliente=doc.cliente,
            fiscal=doc,
            sha256=hashlib.sha256(danfe_pdf).hexdigest(),
            enviado_por=user,
            disponivel_portal=True,
        )
        write(m.EventoFiscal(documento=doc, usuario=user, evento=f'NFE_{doc.status}', anterior=old_status, posterior=doc.status))
        audit(user, 'FISCAL_NFE_EMITIDA', doc, {'chave': chave, 'modo_emissao': modo_emissao, 'status': doc.status, 'rtc': bool(nota_json)})
        return {
            'documento': doc,
            'chave': chave,
            'protocolo': doc.protocolo,
            'status': doc.status,
            'xml': xml_bytes,
            'danfe_pdf': danfe_pdf,
            'nota_json': nota_json,
        }

    @transaction.atomic
    def cancelar_documento(self, user, documento_id: int, justificativa: str) -> dict[str, Any]:
        require(user, 'fiscal_cancelar')
        doc = m.DocumentoFiscal.objects.select_for_update().get(pk=documento_id)
        if doc.tipo != 'NFE' or doc.status != 'AUTORIZADA' or len(doc.chave or '') != 44:
            raise ValidationError('Apenas NF-e autorizada com chave de 44 dígitos pode ser cancelada.')
        emit = _resolver_emitente_snapshot(doc)
        evento_xml = construir_evento_nfe(
            chave=doc.chave,
            cnpj=emit['cnpj'],
            tp_evento='110111',
            n_seq=1,
            ambiente='HOMOLOGACAO' if doc.ambiente != 'PRODUCAO' else 'PRODUCAO',
            protocolo=doc.protocolo or '126000000000001',
            justificativa=justificativa,
        )
        ret = ClienteSEFAZNFe(transporte=self.transporte, simulacao=(self.transporte is None)).enviar_evento(
            evento_xml, chave=doc.chave, tp_evento='110111', ambiente=doc.ambiente
        )
        old_status = doc.status
        doc.status = 'CANCELADA'
        write(doc)
        m.Documento.objects.create(
            titulo=f'NF-e {doc.numero} Evento Cancelamento 110111',
            tipo='FISCAL_XML',
            arquivo=ContentFile(evento_xml, name=f'canc_nfe_{doc.numero}.xml'),
            cliente=doc.cliente,
            fiscal=doc,
            sha256=hashlib.sha256(evento_xml).hexdigest(),
            enviado_por=user,
            disponivel_portal=True,
        )
        write(m.EventoFiscal(documento=doc, usuario=user, evento='NFE_CANCELADA', anterior=old_status, posterior='CANCELADA'))
        audit(user, 'FISCAL_NFE_CANCELADA', doc, {'protocolo_evento': ret.protocolo, 'justificativa': justificativa[:120]})
        return {'status': 'CANCELADA', 'protocolo': ret.protocolo, 'xml': evento_xml}

    @transaction.atomic
    def carta_correcao_documento(self, user, documento_id: int, correcao: str) -> dict[str, Any]:
        require(user, 'fiscal_emitir')
        doc = m.DocumentoFiscal.objects.select_for_update().get(pk=documento_id)
        if doc.tipo != 'NFE' or doc.status != 'AUTORIZADA' or len(doc.chave or '') != 44:
            raise ValidationError('Carta de Correção (CC-e) exige NF-e autorizada com chave válida.')
        n_seq = m.EventoFiscal.objects.filter(documento=doc, evento='NFE_CCE').count() + 1
        emit = _resolver_emitente_snapshot(doc)
        evento_xml = construir_evento_nfe(
            chave=doc.chave,
            cnpj=emit['cnpj'],
            tp_evento='110110',
            n_seq=n_seq,
            ambiente='HOMOLOGACAO' if doc.ambiente != 'PRODUCAO' else 'PRODUCAO',
            correcao=correcao,
        )
        ret = ClienteSEFAZNFe(transporte=self.transporte, simulacao=(self.transporte is None)).enviar_evento(
            evento_xml, chave=doc.chave, tp_evento='110110', ambiente=doc.ambiente
        )
        m.Documento.objects.create(
            titulo=f'NF-e {doc.numero} CC-e seq {n_seq}',
            tipo='FISCAL_XML',
            arquivo=ContentFile(evento_xml, name=f'cce_nfe_{doc.numero}_{n_seq}.xml'),
            cliente=doc.cliente,
            fiscal=doc,
            sha256=hashlib.sha256(evento_xml).hexdigest(),
            enviado_por=user,
            disponivel_portal=True,
        )
        write(m.EventoFiscal(documento=doc, usuario=user, evento='NFE_CCE', anterior=doc.status, posterior=doc.status))
        audit(user, 'FISCAL_NFE_CCE', doc, {'seq': n_seq, 'protocolo': ret.protocolo})
        return {'status': 'REGISTRADO', 'n_seq': n_seq, 'protocolo': ret.protocolo, 'xml': evento_xml}


@transaction.atomic
def emitir_nfe_hub(
    user,
    estabelecimento,
    *,
    cliente=None,
    descricao: str = 'Venda de produtos / equipamentos',
    valor: Decimal = Decimal('100.00'),
    itens: list[dict[str, Any]] | None = None,
    serie: str = '1',
    ambiente: str = 'HOMOLOGACAO',
    modo_emissao: str = 'NORMAL',
    motivo_contingencia: str = '',
    regime_transicao: str | None = '2026_TESTE',
    rtc_override: dict[str, Any] | None = None,
    natureza: str = 'VENDA DE MERCADORIA',
) -> dict[str, Any]:
    """Cria e emite NF-e Modelo 55 diretamente pelo Hub Fiscal com suporte a CBS/IBS e contingência."""
    require(user, 'fiscal_emitir')
    org = current_organization()
    if estabelecimento.organization_id != org.pk:
        raise ValidationError('Estabelecimento fora da organização.')
    if ambiente not in ('HOMOLOGACAO', 'SIMULACAO'):
        raise ValidationError('Emissão direta pelo Hub opera apenas em homologação/simulação.')

    serie_obj, _ = m.SerieFiscal.objects.get_or_create(
        estabelecimento=estabelecimento,
        tipo='NFE',
        ambiente=ambiente,
        serie=serie,
        defaults={},
    )
    itens_final = itens or [{
        'sku': 'PROD-55',
        'descricao': descricao,
        'ncm': '90189099',
        'cfop': '5102',
        'cst_csosn': '102',
        'quantidade': '1',
        'valor_unitario': str(valor),
        'desconto': '0.00',
        'valor': str(valor),
    }]
    doc = m.DocumentoFiscal.objects.create(
        estabelecimento=estabelecimento,
        serie_fiscal=serie_obj,
        tipo='NFE',
        modelo='55',
        ambiente=ambiente,
        serie=serie,
        cliente=cliente,
        descricao=descricao,
        valor=valor,
        snapshot_fiscal={'natureza': natureza, 'itens': itens_final},
        impostos={'rtc': rtc_override} if rtc_override else {},
        criado_por=user,
    )
    write(m.DetalhesNFe(documento=doc, itens_snapshot=itens_final))
    motor = MotorNFeHomologacao()
    return motor.emitir_documento(
        user,
        doc.pk,
        modo_emissao=modo_emissao,
        motivo_contingencia=motivo_contingencia,
        regime_transicao=regime_transicao,
        rtc_override=rtc_override,
    )
