"""Serviço de NFS-e Nacional em Contingência (RPS Offline + Sincronização ADN + Reforma Tributária CBS/IBS)."""
from __future__ import annotations

import hashlib
from datetime import datetime
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
from fiscal.nfse.contracts import (
    DadosDPS,
    EnderecoNacional,
    PrestadorDPS,
    ServicoDPS,
    TipoAmbiente,
    TomadorDPS,
    ValoresDPS,
)
from fiscal.nfse.danfse import gerar_danfse_pdf
from fiscal.nfse.dps import dps_para_xml_bytes, gerar_chave_dps
from fiscal.tributaria import gerar_nota_json_rtc


def _digits(value: Any) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def _montar_dados_dps_e_rtc(
    doc: m.DocumentoFiscal,
    numero_rps: int,
    *,
    regime_transicao: str | None = '2026_TESTE',
    rtc_override: dict[str, Any] | None = None,
) -> tuple[DadosDPS, dict[str, Any] | None]:
    org = doc.organization
    estab = doc.estabelecimento
    perfil = getattr(estab, 'perfil_fiscal', None) if estab else None
    cnpj_prest = _digits((estab.cnpj if estab and estab.cnpj else org.cnpj) or '00000000000191').zfill(14)[:14]
    mun_ibge = _digits((doc.municipio or (getattr(perfil, 'municipio_ibge', '') if perfil else '') or '2800308')).zfill(7)[:7]
    uf = (getattr(perfil, 'uf', '') if perfil else '') or org.uf or 'SE'

    cli = doc.cliente
    doc_tom = _digits(cli.cpf_cnpj) if cli else '00000000000'
    nome_tom = cli.nome_razao_social if cli else 'Tomador não identificado'

    amb = TipoAmbiente.HOMOLOGACAO if doc.ambiente != 'PRODUCAO' else TipoAmbiente.PRODUCAO
    comp = getattr(getattr(doc, 'detalhesnfse', None), 'competencia', None) or timezone.localdate()
    dados = DadosDPS(
        serie=str(doc.serie or '1'),
        numero_dps=int(numero_rps),
        data_competencia=comp.isoformat(),
        data_hora_emissao=timezone.now().isoformat(timespec='seconds'),
        ambiente=amb,
        prestador=PrestadorDPS(
            cnpj=cnpj_prest,
            razao_social=org.razao_social or 'VERTYX ERP',
            inscricao_municipal=(estab.inscricao_municipal if estab else '') or '1001',
        ),
        tomador=TomadorDPS(
            cnpj_cpf=doc_tom,
            razao_social_nome=nome_tom,
            endereco=EnderecoNacional(
                municipio_ibge=mun_ibge,
                cep='49000000',
                logradouro=(getattr(cli, 'endereco', '') if cli else '') or 'Av. Principal',
                numero='100',
                bairro='Centro',
                uf=uf[:2],
            ),
            email=getattr(cli, 'email', '') if cli else '',
        ),
        servico=ServicoDPS(
            codigo_tributacao_nacional=_digits(doc.codigo_servico or '010701').zfill(6)[:6],
            discriminacao=doc.descricao or 'Prestação de serviço técnico',
            municipio_prestacao_ibge=mun_ibge,
            codigo_municipio_incidencia=mun_ibge,
        ),
        valores=ValoresDPS(
            valor_servicos=Decimal(str(doc.valor or '0.00')),
        ),
    )

    nota_json = None
    rtc_params = rtc_override or (doc.impostos or {}).get('rtc')
    if rtc_params or regime_transicao:
        itens_svc = [{
            'nItem': '1',
            'descricao': doc.descricao or 'Serviço NFS-e',
            'valor_base': str(doc.valor),
            **({'rtc': rtc_params} if rtc_params else {}),
        }]
        nota_json = gerar_nota_json_rtc(
            itens_svc,
            numero=str(numero_rps),
            chave=gerar_chave_dps(dados),
            modelo='NFSE',
            serie=str(doc.serie or '1'),
            data_emissao=timezone.now().isoformat(timespec='seconds'),
            regime_transicao=regime_transicao or '2026_TESTE',
            default_params=rtc_params,
        )
    return dados, nota_json


class MotorNFSeContingencia:
    """Motor dedicado de NFS-e em Contingência RPS Offline e sincronização posterior ADN/SEFIN."""

    tipo = 'NFSE'
    modelo = 'NFSE'

    def validar(self, documento: m.DocumentoFiscal) -> None:
        documento.full_clean()
        if documento.tipo != 'NFSE':
            raise ValidationError('Contingência RPS exclusiva para NFS-e.')

    @transaction.atomic
    def emitir_contingencia(
        self,
        user,
        documento: m.DocumentoFiscal,
        *,
        motivo_contingencia: str = 'Indisponibilidade momentânea do ADN/SEFIN Nacional',
        regime_transicao: str | None = '2026_TESTE',
        rtc_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require(user, 'fiscal_emitir')
        doc = m.DocumentoFiscal.objects.select_for_update().get(pk=documento.pk)
        self.validar(doc)
        if not motivo_contingencia.strip():
            raise ValidationError('Informe o motivo da entrada da NFS-e em contingência RPS.')

        old_status = doc.status
        if not doc.numero:
            if doc.serie_fiscal_id:
                serie_obj = m.SerieFiscal.objects.select_for_update().get(pk=doc.serie_fiscal_id)
                numero_rps = serie_obj.proximo_numero
                serie_obj.proximo_numero += 1
                write(serie_obj)
                write(m.ReservaNumeracaoFiscal(serie=serie_obj, documento=doc, numero=numero_rps))
            else:
                numero_rps = m.DocumentoFiscal.objects.filter(tipo='NFSE').exclude(numero='').count() + 1
            doc.numero = str(numero_rps)
        else:
            numero_rps = int(doc.numero)

        dados, nota_json = _montar_dados_dps_e_rtc(
            doc,
            numero_rps,
            regime_transicao=regime_transicao,
            rtc_override=rtc_override,
        )
        dps_id = gerar_chave_dps(dados)
        dps_xml = dps_para_xml_bytes(dados)
        rps_codigo = f'RPS-{doc.serie or "1"}-{numero_rps:06d}'

        doc.modelo = 'NFSE'
        doc.chave = dps_id
        doc.status = 'CONTINGENCIA'
        doc.protocolo = f'CONTINGENCIA-{rps_codigo}'
        snap = dict(doc.snapshot_fiscal or {})
        snap.update({
            'modo_emissao': 'CONTINGENCIA_RPS',
            'rps': rps_codigo,
            'motivo_contingencia': motivo_contingencia.strip(),
            'nota_json': nota_json or {},
        })
        doc.snapshot_fiscal = snap
        write(doc)

        det = getattr(doc, 'detalhesnfse', None)
        if det is None:
            det = m.DetalhesNFSe(
                documento=doc,
                competencia=timezone.localdate(),
                codigo_servico=doc.codigo_servico,
                municipio_incidencia=_digits(doc.municipio or '2800308').zfill(7)[:7],
            )
        det.identificador_dps_rps = dps_id
        det.modo_emissao = 'CONTINGENCIA_RPS'
        det.motivo_contingencia = motivo_contingencia.strip()[:200]
        det.numero_rps_contingencia = rps_codigo
        det.nota_json = nota_json or {}
        det._service_write = True
        det.save()

        danfse_provisorio = gerar_danfse_pdf({
            'numero': rps_codigo,
            'chave_acesso': dps_id,
            'codigo_verificacao': 'CONTINGÊNCIA OFFLINE',
            'data_emissao': timezone.now().strftime('%d/%m/%Y %H:%M'),
            'municipio': f'{doc.municipio or "ADN"} · PROVISÓRIO',
            'prestador_nome': f"{doc.organization.razao_social} [EM CONTINGÊNCIA RPS]",
            'prestador_cnpj': doc.organization.cnpj or '00000000000191',
            'prestador_im': getattr(doc.estabelecimento, 'inscricao_municipal', '') or '1001',
            'tomador_nome': doc.cliente.nome_razao_social if doc.cliente else 'Tomador',
            'tomador_doc': doc.cliente.cpf_cnpj if doc.cliente else '-',
            'descricao_servico': f"[RPS OFFLINE · {motivo_contingencia.strip()}] {doc.descricao}",
            'codigo_servico': doc.codigo_servico or '010701',
            'valor_servico': f'{doc.valor:.2f}',
            'valor_iss': '0.00',
            'valor_liquido': f'{doc.valor:.2f}',
            'url_consulta': f'https://adn.nfse.gov.br/consulta/{dps_id}',
        })

        m.Documento.objects.create(
            titulo=f'NFS-e {rps_codigo} DPS Offline (Contingência)',
            tipo='FISCAL_XML',
            arquivo=ContentFile(dps_xml, name=f'dps_contingencia_{numero_rps}.xml'),
            cliente=doc.cliente,
            fiscal=doc,
            sha256=hashlib.sha256(dps_xml).hexdigest(),
            enviado_por=user,
            disponivel_portal=True,
        )
        m.Documento.objects.create(
            titulo=f'DANFSE Provisório {rps_codigo} (Contingência)',
            tipo='FISCAL_PDF',
            arquivo=ContentFile(danfse_provisorio, name=f'danfse_contingencia_{numero_rps}.pdf'),
            cliente=doc.cliente,
            fiscal=doc,
            sha256=hashlib.sha256(danfse_provisorio).hexdigest(),
            enviado_por=user,
            disponivel_portal=True,
        )
        write(m.EventoFiscal(
            documento=doc,
            usuario=user,
            evento='NFSE_CONTINGENCIA_RPS',
            anterior=old_status,
            posterior='CONTINGENCIA',
        ))
        audit(user, 'FISCAL_NFSE_CONTINGENCIA', doc, {'rps': rps_codigo, 'motivo': motivo_contingencia[:120], 'rtc': bool(nota_json)})
        return {
            'documento': doc,
            'status': 'CONTINGENCIA',
            'rps': rps_codigo,
            'dps_id': dps_id,
            'xml': dps_xml,
            'danfse_pdf': danfse_provisorio,
            'nota_json': nota_json,
        }


def emitir_nfse_com_contingencia(
    user,
    estabelecimento,
    *,
    cliente,
    codigo_servico: str,
    municipio: str,
    descricao: str,
    valor: Decimal,
    ambiente: str = 'HOMOLOGACAO',
    motivo_contingencia: str = 'Indisponibilidade temporária do ambiente nacional ADN/SEFIN',
    usar_contingencia_direta: bool = False,
    regime_transicao: str | None = '2026_TESTE',
    rtc_override: dict[str, Any] | None = None,
    simular_falha_adn: bool = False,
    motor_adn=None,
) -> dict[str, Any]:
    """Emite NFS-e via ADN Nacional com fallback automático para Contingência RPS em caso de indisponibilidade."""
    require(user, 'fiscal_emitir')
    org = current_organization()
    if estabelecimento and estabelecimento.organization_id != org.pk:
        raise ValidationError('Estabelecimento fora da organização.')
    if ambiente not in ('HOMOLOGACAO', 'SIMULACAO'):
        raise ValidationError('Emissão direta NFS-e opera apenas em homologação/simulação.')

    with transaction.atomic():
        serie_obj, _ = m.SerieFiscal.objects.get_or_create(
            estabelecimento=estabelecimento,
            tipo='NFSE',
            ambiente=ambiente,
            serie='1',
            defaults={},
        )
        doc = m.DocumentoFiscal.objects.create(
            estabelecimento=estabelecimento,
            serie_fiscal=serie_obj,
            tipo='NFSE',
            modelo='NFSE',
            ambiente=ambiente,
            serie='1',
            cliente=cliente,
            municipio=municipio,
            codigo_servico=codigo_servico,
            descricao=descricao,
            valor=valor,
            impostos={'rtc': rtc_override} if rtc_override else {},
            criado_por=user,
        )
        write(m.DetalhesNFSe(
            documento=doc,
            competencia=timezone.localdate(),
            codigo_servico=codigo_servico,
            municipio_incidencia=_digits(municipio or '2800308').zfill(7)[:7],
        ))

    if not usar_contingencia_direta:
        try:
            if simular_falha_adn:
                raise TimeoutError('Timeout na comunicação com SEFIN/ADN Nacional.')
            if motor_adn is not None:
                return motor_adn(doc)
            # Quando não há contingência direta nem falha simulada, também gera nota oficial ADN em homologação
            with transaction.atomic():
                locked = m.DocumentoFiscal.objects.select_for_update().get(pk=doc.pk)
                serie_obj = m.SerieFiscal.objects.select_for_update().get(pk=locked.serie_fiscal_id)
                numero_dps = serie_obj.proximo_numero
                serie_obj.proximo_numero += 1
                write(serie_obj)
                write(m.ReservaNumeracaoFiscal(serie=serie_obj, documento=locked, numero=numero_dps))
                locked.numero = str(numero_dps)
                dados, nota_json = _montar_dados_dps_e_rtc(
                    locked, numero_dps, regime_transicao=regime_transicao, rtc_override=rtc_override
                )
                chave = gerar_chave_dps(dados)
                xml_bytes = dps_para_xml_bytes(dados)
                locked.chave = chave
                locked.protocolo = f'ADN-{timezone.now():%Y%m%d}-{numero_dps:06d}'
                locked.status = 'AUTORIZADA'
                locked.autorizado_em = timezone.now()
                locked.snapshot_fiscal = {'modo_emissao': 'NORMAL', 'nota_json': nota_json or {}}
                write(locked)
                det = locked.detalhesnfse
                det.identificador_dps_rps = chave
                det.modo_emissao = 'NORMAL'
                det.nota_json = nota_json or {}
                det._service_write = True
                det.save()
                pdf = gerar_danfse_pdf({
                    'numero': locked.numero,
                    'chave_acesso': chave,
                    'codigo_verificacao': locked.protocolo[-8:],
                    'data_emissao': timezone.now().strftime('%d/%m/%Y %H:%M'),
                    'municipio': municipio,
                    'prestador_nome': org.razao_social,
                    'prestador_cnpj': org.cnpj or '00000000000191',
                    'tomador_nome': cliente.nome_razao_social,
                    'tomador_doc': cliente.cpf_cnpj,
                    'descricao_servico': descricao,
                    'codigo_servico': codigo_servico,
                    'valor_servico': f'{valor:.2f}',
                    'valor_liquido': f'{valor:.2f}',
                    'url_consulta': f'https://adn.nfse.gov.br/consulta/{chave}',
                })
                m.Documento.objects.create(
                    titulo=f'NFS-e {locked.numero} XML ADN',
                    tipo='FISCAL_XML',
                    arquivo=ContentFile(xml_bytes, name=f'nfse_{locked.numero}.xml'),
                    cliente=cliente,
                    fiscal=locked,
                    sha256=hashlib.sha256(xml_bytes).hexdigest(),
                    enviado_por=user,
                    disponivel_portal=True,
                )
                m.Documento.objects.create(
                    titulo=f'DANFSE NFS-e {locked.numero}',
                    tipo='FISCAL_PDF',
                    arquivo=ContentFile(pdf, name=f'danfse_{locked.numero}.pdf'),
                    cliente=cliente,
                    fiscal=locked,
                    sha256=hashlib.sha256(pdf).hexdigest(),
                    enviado_por=user,
                    disponivel_portal=True,
                )
                write(m.EventoFiscal(documento=locked, usuario=user, evento='NFSE_AUTORIZADA', anterior='RASCUNHO', posterior='AUTORIZADA'))
                return {
                    'documento': locked,
                    'status': 'AUTORIZADA',
                    'chave': chave,
                    'protocolo': locked.protocolo,
                    'xml': xml_bytes,
                    'danfse_pdf': pdf,
                    'nota_json': nota_json,
                }
        except (TimeoutError, ConnectionError, OSError):
            pass

    motor_cont = MotorNFSeContingencia()
    return motor_cont.emitir_contingencia(
        user,
        doc,
        motivo_contingencia=motivo_contingencia,
        regime_transicao=regime_transicao,
        rtc_override=rtc_override,
    )


@transaction.atomic
def sincronizar_nfse_contingencia(
    user,
    *,
    documento_id: int | None = None,
    transporte_fn=None,
) -> dict[str, Any]:
    """Transmite lote (ou documento único) de NFS-e emitidas em Contingência RPS para o ADN/SEFIN."""
    require(user, 'fiscal_emitir')
    # Keep the locking query free of nullable joins; related objects are
    # fetched lazily below so PostgreSQL can apply FOR UPDATE safely.
    qs = m.DocumentoFiscal.objects.select_for_update().filter(
        tipo='NFSE',
        status='CONTINGENCIA',
    )
    if documento_id is not None:
        qs = qs.filter(pk=documento_id)

    sincronizadas = []
    falhas = []
    for doc in qs:
        det = getattr(doc, 'detalhesnfse', None)
        try:
            if transporte_fn is not None:
                ret = transporte_fn(doc)
                protocolo = ret['protocolo']
                chave = ret.get('chave') or doc.chave
            else:
                protocolo = f'ADN-SYNC-{timezone.now():%Y%m%d%H%M%S}-{doc.numero}'
                chave = doc.chave
            old_status = doc.status
            doc.status = 'AUTORIZADA'
            doc.protocolo = protocolo
            doc.chave = chave
            doc.autorizado_em = timezone.now()
            write(doc)
            if det is not None:
                det.modo_emissao = 'NORMAL'
                det.sincronizada_em = timezone.now()
                det.tentativas_sincronizacao += 1
                det.ultimo_erro_sincronizacao = ''
                det._service_write = True
                det.save()
            pdf = gerar_danfse_pdf({
                'numero': doc.numero,
                'chave_acesso': chave,
                'codigo_verificacao': protocolo[-8:],
                'data_emissao': timezone.now().strftime('%d/%m/%Y %H:%M'),
                'municipio': doc.municipio or 'ADN',
                'prestador_nome': doc.organization.razao_social,
                'prestador_cnpj': doc.organization.cnpj or '00000000000191',
                'tomador_nome': doc.cliente.nome_razao_social if doc.cliente else 'Tomador',
                'tomador_doc': doc.cliente.cpf_cnpj if doc.cliente else '-',
                'descricao_servico': doc.descricao,
                'codigo_servico': doc.codigo_servico or '010701',
                'valor_servico': f'{doc.valor:.2f}',
                'valor_liquido': f'{doc.valor:.2f}',
                'url_consulta': f'https://adn.nfse.gov.br/consulta/{chave}',
            })
            m.Documento.objects.create(
                titulo=f'DANFSE Definitivo NFS-e {doc.numero} (Sincronizada ADN)',
                tipo='FISCAL_PDF',
                arquivo=ContentFile(pdf, name=f'danfse_sync_{doc.numero}.pdf'),
                cliente=doc.cliente,
                fiscal=doc,
                sha256=hashlib.sha256(pdf).hexdigest(),
                enviado_por=user,
                disponivel_portal=True,
            )
            write(m.EventoFiscal(
                documento=doc,
                usuario=user,
                evento='NFSE_CONTINGENCIA_SINCRONIZADA',
                anterior=old_status,
                posterior='AUTORIZADA',
            ))
            audit(user, 'FISCAL_NFSE_SYNC_CONTINGENCIA', doc, {'protocolo': protocolo})
            sincronizadas.append(doc.pk)
        except Exception as exc:
            if det is not None:
                det.tentativas_sincronizacao += 1
                det.ultimo_erro_sincronizacao = str(exc)[:240]
                det._service_write = True
                det.save()
            falhas.append({'documento_id': doc.pk, 'erro': str(exc)[:200]})
    return {'sincronizadas': sincronizadas, 'falhas': falhas, 'total_sincronizadas': len(sincronizadas)}
