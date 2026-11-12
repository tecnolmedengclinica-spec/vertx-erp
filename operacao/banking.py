"""Serviços de API de Integração Bancária (Pix, Boleto, Extrato Automático e Webhook idempotente)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.request
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from gestao.models import Lancamento, ZERO
from gestao.permissions import require
from gestao.services import audit
from operacao import models as m
from platform_core.context import current_organization, tenant_context
from platform_core.models import OrganizationMembership


def crc16_ccitt(payload: str) -> str:
    """Calcula CRC-16/CCITT-FALSE (polinômio 0x1021) exigido no padrão EMV QRCPS / BR Code Pix."""
    crc = 0xFFFF
    for byte in payload.encode('utf-8'):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return f'{crc:04X}'


def _tlv(tag: str, value: str) -> str:
    return f'{tag}{len(value):02d}{value}'


def gerar_pix_brcode(
    *,
    chave_pix: str,
    nome_recebedor: str,
    cidade: str,
    valor: Decimal,
    txid: str,
) -> str:
    """Gera string Pix Copia e Cola (EMVCo BR Code) válida com CRC-16."""
    chave = (chave_pix or 'pix@vertyx.local').strip()[:77]
    nome = re.sub(r'[^A-Za-z0-9 ]', '', nome_recebedor or 'VERTYX ERP')[:25] or 'VERTYX ERP'
    cid = re.sub(r'[^A-Za-z0-9 ]', '', cidade or 'ARACAJU')[:15] or 'ARACAJU'
    tx_limpo = re.sub(r'[^A-Za-z0-9]', '', txid or 'VERTYX01')[:25] or 'VERTYX01'
    mai = _tlv('00', 'br.gov.bcb.pix') + _tlv('01', chave)
    add_data = _tlv('05', tx_limpo)
    parcial = (
        _tlv('00', '01')
        + _tlv('01', '12')
        + _tlv('26', mai)
        + _tlv('52', '0000')
        + _tlv('53', '986')
        + _tlv('54', f'{valor:.2f}')
        + _tlv('58', 'BR')
        + _tlv('59', nome)
        + _tlv('60', cid)
        + _tlv('62', add_data)
        + '6304'
    )
    return parcial + crc16_ccitt(parcial)


def gerar_boleto_dados(
    *,
    banco_codigo: str,
    nosso_numero: str,
    valor: Decimal,
    vencimento: date,
) -> tuple[str, str]:
    """Gera código de barras FEBRABAN (44 dígitos) e linha digitável (47 dígitos) determinísticos."""
    base_data = date(1997, 10, 7)
    fator_venc = max(1000, (vencimento - base_data).days % 10000)
    valor_cents = f'{int((valor * 100).quantize(Decimal("1"))):010d}'
    banco = ''.join(ch for ch in str(banco_codigo or '001') if ch.isdigit()).zfill(3)[:3]
    nosso = ''.join(ch for ch in str(nosso_numero or '1') if ch.isdigit()).zfill(17)[:17]
    livre = (nosso + '00000000')[:25]
    sem_dv = f'{banco}9{fator_venc:04d}{valor_cents}{livre}'
    # Módulo 11 FEBRABAN para DV geral na posição 5
    pesos = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = sum(int(d) * pesos[i % 8] for i, d in enumerate(reversed(sem_dv)))
    resto = soma % 11
    dv = 11 - resto
    dv = 1 if dv in (0, 10, 11) else dv
    codigo_barras = f'{sem_dv[:4]}{dv}{sem_dv[4:]}'

    def dv_mod10(campo: str) -> str:
        total = 0
        for idx, ch in enumerate(reversed(campo)):
            n = int(ch) * (2 if idx % 2 == 0 else 1)
            total += n if n < 10 else (n // 10 + n % 10)
        return str((10 - (total % 10)) % 10)

    c1_base = f'{banco}9{codigo_barras[19:24]}'
    c2_base = f'{codigo_barras[24:34]}'
    c3_base = f'{codigo_barras[34:44]}'
    c1 = c1_base + dv_mod10(c1_base)
    c2 = c2_base + dv_mod10(c2_base)
    c3 = c3_base + dv_mod10(c3_base)
    linha = f'{c1[:5]}.{c1[5:]} {c2[:5]}.{c2[5:]} {c3[:5]}.{c3[5:]} {dv} {fator_venc:04d}{valor_cents}'
    return linha, codigo_barras


def _resolver_credencial_bancaria(referencia: str) -> str:
    ref = str(referencia or '').strip()
    if ref.startswith('simulacao://'):
        return 'simulacao-local'
    if ref.startswith('env://'):
        var_name = ref.removeprefix('env://').strip()
        val = os.environ.get(var_name)
        if not val:
            raise ValidationError(f'Variável de ambiente {var_name} ausente para credencial bancária.')
        return val
    if ref.startswith('cofre://'):
        vault = getattr(settings, 'BANKING_VAULT_SECRETS', {}) or {}
        if ref in vault:
            return vault[ref]
        env_key = 'VERTYX_BANK_' + re.sub(r'[^A-Za-z0-9]', '_', ref.removeprefix('cofre://')).upper()
        val = os.environ.get(env_key)
        if val:
            return val
        raise ValidationError('Segredo bancário não provisionado no cofre/ambiente para esta referência.')
    raise ValidationError('Referência de credencial bancária inválida.')


class BaseBancoProvider:
    codigo_febraban = '001'
    base_url = ''

    def __init__(self, integracao: m.IntegracaoBancaria, *, transporte=None):
        self.integracao = integracao
        self.transporte = transporte

    def _credencial(self) -> str:
        return _resolver_credencial_bancaria(self.integracao.credencial_referencia)

    def _http_json(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from django.conf import settings
        if self.transporte is None and self.integracao.ambiente == 'PRODUCAO' and not getattr(settings, 'EXTERNAL_BANKING_ENABLED', True):
            raise ValidationError('Operações bancárias externas desabilitadas neste ambiente.')
        token = self._credencial()
        if self.transporte is not None:
            return self.transporte(method, url, payload, token=token, integracao=self.integracao)
        if self.integracao.ambiente == 'PRODUCAO' and not self.integracao.credencial_referencia.startswith('simulacao://'):
            body = json.dumps(payload).encode('utf-8') if payload is not None else None
            req = urllib.request.Request(
                url,
                data=body,
                method=method,
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                    'User-Agent': 'VERTYX-ERP-Banking/1.0',
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode('utf-8'))
        return {}

    def emitir_cobranca(self, *, tipo: str, valor: Decimal, vencimento: date, txid: str, pagador_nome: str, pagador_doc: str, descricao: str) -> dict[str, Any]:
        remote = self._http_json(
            'POST',
            f'{self.base_url}/cobrancas',
            {'tipo': tipo, 'valor': str(valor), 'vencimento': vencimento.isoformat(), 'txid': txid, 'pagador': {'nome': pagador_nome, 'documento': pagador_doc}, 'descricao': descricao},
        )
        nosso_num = str(remote.get('nosso_numero') or f'{abs(hash(txid)) % 10**12:012d}')
        pix = remote.get('pix_copia_cola') or (
            gerar_pix_brcode(
                chave_pix=self.integracao.chave_pix or f'{self.integracao.organization.cnpj or "00000000000191"}',
                nome_recebedor=self.integracao.organization.razao_social,
                cidade=self.integracao.organization.cidade or 'ARACAJU',
                valor=valor,
                txid=txid,
            )
            if tipo in ('PIX', 'HIBRIDO')
            else ''
        )
        linha, barras = gerar_boleto_dados(
            banco_codigo=self.codigo_febraban,
            nosso_numero=nosso_num,
            valor=valor,
            vencimento=vencimento,
        )
        return {
            'external_id': str(remote.get('id') or f'{self.integracao.provedor.lower()}_{txid.lower()}'),
            'txid': str(remote.get('txid') or txid),
            'nosso_numero': nosso_num,
            'pix_copia_cola': pix,
            'linha_digitavel': remote.get('linha_digitavel') or (linha if tipo in ('BOLETO', 'HIBRIDO') else ''),
            'codigo_barras': remote.get('codigo_barras') or (barras if tipo in ('BOLETO', 'HIBRIDO') else ''),
            'url_boleto': remote.get('url_boleto') or '',
            'status': str(remote.get('status') or 'ATIVA'),
        }

    def consultar_cobranca(self, cobranca: m.CobrancaBancaria) -> dict[str, Any]:
        if self.transporte is not None:
            return self.transporte('GET', f'{self.base_url}/cobrancas/{cobranca.external_id}', None, integracao=self.integracao)
        return {'status': cobranca.status, 'valor_pago': str(cobranca.valor_pago or cobranca.valor)}

    def obter_extrato(self, inicio: date, fim: date) -> list[dict[str, Any]]:
        if self.transporte is not None:
            resp = self.transporte('GET', f'{self.base_url}/extrato?inicio={inicio.isoformat()}&fim={fim.isoformat()}', None, integracao=self.integracao)
            return list(resp.get('lancamentos', []))
        # Em sandbox local sem transporte externo, retorna eventos pendentes prontos para conciliação
        pendentes = m.CobrancaBancaria.objects.filter(integracao=self.integracao, status='ATIVA')[:5]
        entries = []
        for cob in pendentes:
            entries.append({
                'identificador': f'API-{self.integracao.provedor}-{cob.txid}',
                'data': fim,
                'valor': cob.valor,
                'descricao': f'Crédito Pix/Boleto {cob.txid}',
                'txid': cob.txid,
            })
        return entries


class SimuladoBancoProvider(BaseBancoProvider):
    codigo_febraban = '999'
    base_url = 'https://sandbox.banco-simulado.local'


class AsaasBancoProvider(BaseBancoProvider):
    codigo_febraban = '461'
    base_url = 'https://sandbox.asaas.com/api/v3'


class BancoDoBrasilProvider(BaseBancoProvider):
    codigo_febraban = '001'
    base_url = 'https://api.hm.bb.com.br/pix/v2'


class InterBancoProvider(BaseBancoProvider):
    codigo_febraban = '077'
    base_url = 'https://cdpj-sandbox.partners.uatinter.co/cobranca/v3'


class SicoobBancoProvider(BaseBancoProvider):
    codigo_febraban = '756'
    base_url = 'https://sandbox.sicoob.com.br/sicoob/sandbox'


class SicrediBancoProvider(BaseBancoProvider):
    codigo_febraban = '748'
    base_url = 'https://api-parceiro.sicredi.com.br/sb'


PROVIDER_MAP = {
    'SIMULADO': SimuladoBancoProvider,
    'ASAAS': AsaasBancoProvider,
    'BANCO_DO_BRASIL': BancoDoBrasilProvider,
    'INTER': InterBancoProvider,
    'SICOOB': SicoobBancoProvider,
    'SICREDI': SicrediBancoProvider,
}


def obter_provedor_banco(integracao: m.IntegracaoBancaria, *, transporte=None) -> BaseBancoProvider:
    cls = PROVIDER_MAP.get(integracao.provedor)
    if cls is None:
        raise ValidationError(f'Provedor bancário não suportado: {integracao.provedor}.')
    return cls(integracao, transporte=transporte)


@transaction.atomic
def configurar_integracao_bancaria(
    user,
    conta_id: int,
    *,
    provedor: str = 'SIMULADO',
    ambiente: str = 'SANDBOX',
    client_id: str = '',
    credencial_referencia: str = 'simulacao://banco-local',
    chave_pix: str = '',
    convenio_boleto: str = '',
    conciliar_automaticamente: bool = True,
    webhook_secret: str = '',
) -> tuple[m.IntegracaoBancaria, str]:
    """Configura ou atualiza a integração API de uma ContaBancaria armazenando apenas referência opaca e hash do webhook."""
    require(user, 'financeiro_escrever')
    conta = m.ContaBancaria.objects.get(pk=conta_id, ativo=True)
    raw_secret = webhook_secret.strip()
    existing = m.IntegracaoBancaria.objects.filter(conta=conta).first()
    if not raw_secret and existing and existing.webhook_token_hash:
        token_hash = existing.webhook_token_hash
    else:
        raw_secret = raw_secret or uuid.uuid4().hex
        token_hash = hashlib.sha256(raw_secret.encode('utf-8')).hexdigest()

    integracao, _ = m.IntegracaoBancaria.objects.update_or_create(
        conta=conta,
        defaults={
            'provedor': provedor,
            'ambiente': ambiente,
            'client_id': client_id.strip(),
            'credencial_referencia': credencial_referencia.strip(),
            'chave_pix': chave_pix.strip(),
            'convenio_boleto': convenio_boleto.strip(),
            'conciliar_automaticamente': conciliar_automaticamente,
            'webhook_token_hash': token_hash,
            'ativo': True,
            'ultimo_status': f'Configurada ({provedor} / {ambiente})',
        },
    )
    audit(user, 'BANCARIO_INTEGRACAO_CONFIG', integracao, {'provedor': provedor, 'ambiente': ambiente})
    return integracao, raw_secret


@transaction.atomic
def emitir_cobranca_bancaria(
    user,
    integracao_id: int,
    *,
    valor: Decimal | str,
    vencimento: date,
    tipo: str = 'HIBRIDO',
    lancamento_id: int | None = None,
    pagador_nome: str = '',
    pagador_doc: str = '',
    descricao: str = 'Cobrança VERTYX ERP',
    request_id: uuid.UUID | str | None = None,
    transporte=None,
) -> m.CobrancaBancaria:
    """Emite cobrança Pix / Boleto via API bancária de forma idempotente e vinculada a Lancamento."""
    require(user, 'financeiro_escrever')
    rid = uuid.UUID(str(request_id)) if request_id else uuid.uuid4()
    existente = m.CobrancaBancaria.objects.filter(request_id=rid).first()
    if existente:
        return existente

    integracao = m.IntegracaoBancaria.objects.select_related('conta', 'organization').get(pk=integracao_id, ativo=True)
    lancamento = None
    if lancamento_id is not None:
        lancamento = Lancamento.objects.select_for_update().get(pk=lancamento_id)
        if lancamento.tipo != 'RECEBER' or lancamento.status not in ('PENDENTE', 'VENCIDO'):
            raise ValidationError('Cobrança bancária exige conta a receber em aberto.')
        if not pagador_nome and lancamento.cliente_id:
            pagador_nome = lancamento.cliente.nome_razao_social
            pagador_doc = lancamento.cliente.cpf_cnpj

    amount = Decimal(str(valor)).quantize(Decimal('.01'))
    if amount <= ZERO:
        raise ValidationError('Valor da cobrança deve ser positivo.')

    txid = f'VTX{uuid.uuid4().hex[:22].upper()}'
    provedor = obter_provedor_banco(integracao, transporte=transporte)
    retorno = provedor.emitir_cobranca(
        tipo=tipo,
        valor=amount,
        vencimento=vencimento,
        txid=txid,
        pagador_nome=pagador_nome or 'Cliente VERTYX',
        pagador_doc=pagador_doc or '',
        descricao=descricao,
    )

    cob = m.CobrancaBancaria.objects.create(
        request_id=rid,
        integracao=integracao,
        conta=integracao.conta,
        lancamento=lancamento,
        tipo=tipo,
        status=retorno.get('status', 'ATIVA'),
        external_id=retorno['external_id'],
        txid=retorno['txid'],
        nosso_numero=retorno['nosso_numero'],
        valor=amount,
        vencimento=vencimento,
        pix_copia_cola=retorno.get('pix_copia_cola', ''),
        linha_digitavel=retorno.get('linha_digitavel', ''),
        codigo_barras=retorno.get('codigo_barras', ''),
        url_boleto=retorno.get('url_boleto', ''),
        payload_sanitizado={'provedor': integracao.provedor, 'txid': retorno['txid'], 'external_id': retorno['external_id']},
    )
    audit(user, 'BANCARIO_COBRANCA_EMITIDA', cob, {'tipo': tipo, 'valor': str(amount), 'txid': cob.txid})
    return cob


def _liquidar_cobranca_locked(
    user,
    cob: m.CobrancaBancaria,
    *,
    valor_pago: Decimal,
    data_pagamento: date,
) -> m.CobrancaBancaria:
    if cob.status == 'PAGA':
        return cob
    cob.status = 'PAGA'
    cob.valor_pago = valor_pago
    cob.pago_em = timezone.now()
    if cob.lancamento_id:
        from operacao.services import pagar
        entry = Lancamento.objects.select_for_update().get(pk=cob.lancamento_id)
        if entry.status in ('PENDENTE', 'VENCIDO') and entry.saldo > ZERO:
            dt_valid = max(entry.data_emissao, min(data_pagamento, timezone.localdate()))
            val_baixar = min(valor_pago, entry.saldo)
            pagamento = pagar(
                user,
                entry.pk,
                val_baixar,
                dt_valid,
                observation=f'Liquidação automática API ({cob.integracao.provedor} · {cob.txid})',
                account_id=cob.conta_id,
                request_id=cob.request_id,
            )
            cob.pagamento = pagamento
    else:
        mov = m.MovimentoCaixa(
            conta=cob.conta,
            tipo='ENTRADA',
            valor=valor_pago,
            data=min(data_pagamento, timezone.localdate()),
            descricao=f'Recebimento API {cob.integracao.provedor} · {cob.txid}',
            referencia=cob.request_id,
            usuario=user,
            origem='CONCILIACAO',
            documento_ref=cob.txid or cob.external_id,
        )
        mov._service_write = True
        mov.save()
    cob.save()
    audit(user, 'BANCARIO_COBRANCA_PAGA', cob, {'valor_pago': str(valor_pago), 'lancamento_id': cob.lancamento_id})
    return cob


@transaction.atomic
def consultar_cobranca_bancaria(user, cobranca_id: int, *, transporte=None) -> m.CobrancaBancaria:
    require(user, 'financeiro')
    cob = m.CobrancaBancaria.objects.select_for_update().select_related('integracao', 'conta').get(pk=cobranca_id)
    provedor = obter_provedor_banco(cob.integracao, transporte=transporte)
    ret = provedor.consultar_cobranca(cob)
    status = str(ret.get('status') or cob.status).upper()
    if status in ('PAGA', 'RECEIVED', 'CONFIRMED', 'LIQUIDADO', 'CONCLUIDA'):
        valor_pago = Decimal(str(ret.get('valor_pago') or cob.valor)).quantize(Decimal('.01'))
        data_pg = ret.get('data_pagamento') or timezone.localdate()
        if isinstance(data_pg, str):
            data_pg = date.fromisoformat(data_pg[:10])
        return _liquidar_cobranca_locked(user, cob, valor_pago=valor_pago, data_pagamento=data_pg)
    if status in ('CANCELADA', 'VENCIDA'):
        cob.status = status
        cob.save()
    return cob


@transaction.atomic
def sincronizar_extrato_api(
    user,
    integracao_id: int,
    *,
    inicio: date | None = None,
    fim: date | None = None,
    transporte=None,
) -> dict[str, Any]:
    """Importa lançamentos de extrato bancário via API e concilia cobranças automaticamente."""
    require(user, 'financeiro_escrever')
    integracao = m.IntegracaoBancaria.objects.select_for_update().select_related('conta').get(pk=integracao_id, ativo=True)
    dt_fim = fim or timezone.localdate()
    dt_inicio = inicio or (dt_fim - timezone.timedelta(days=7))
    provedor = obter_provedor_banco(integracao, transporte=transporte)
    linhas = provedor.obter_extrato(dt_inicio, dt_fim)

    importados = 0
    conciliados = 0
    for item in linhas:
        ident = str(item['identificador'])[:150]
        dt_item = item['data']
        if isinstance(dt_item, str):
            dt_item = date.fromisoformat(dt_item[:10])
        val = Decimal(str(item['valor'])).quantize(Decimal('.01'))
        ext, created = m.ExtratoBancario.objects.get_or_create(
            conta=integracao.conta,
            identificador=ident,
            defaults={
                'data': dt_item,
                'valor': val,
                'descricao': str(item.get('descricao') or 'Lançamento API')[:255],
            },
        )
        if created:
            importados += 1
        if integracao.conciliar_automaticamente and not ext.movimento_id and val > ZERO:
            txid = str(item.get('txid') or '')
            cob = (
                m.CobrancaBancaria.objects.select_for_update()
                .filter(integracao=integracao, status='ATIVA')
                .filter(txid=txid if txid else '__none__')
                .first()
            )
            if cob is not None:
                _liquidar_cobranca_locked(user, cob, valor_pago=val, data_pagamento=dt_item)
                if cob.lancamento_id:
                    mov = cob.lancamento.movimentos_caixa.order_by('-pk').first()
                else:
                    mov = integracao.conta.movimentos.filter(referencia=cob.request_id).first()
                if mov is not None:
                    ext.movimento = mov
                    ext.save()
                conciliados += 1

    integracao.ultima_sincronizacao_em = timezone.now()
    integracao.ultimo_status = f'Sincronizado: {importados} lançamentos, {conciliados} baixas'
    integracao.save()
    return {'importados': importados, 'conciliados': conciliados}


def _resolver_operador_webhook(org) -> Any:
    mem = (
        OrganizationMembership.objects.filter(
            organization=org,
            status=OrganizationMembership.Status.ACTIVE,
            role__in=['ADMIN', 'FINANCEIRO', 'GESTOR'],
        )
        .select_related('user')
        .first()
    )
    if mem is None:
        raise PermissionDenied('Nenhum operador financeiro ativo encontrado na organização para conciliar webhook.')
    return mem.user


def processar_webhook_bancario(
    integracao_id: int,
    payload: dict[str, Any],
    *,
    token: str = '',
    user=None,
) -> m.WebhookBancarioEvento:
    """Processa webhook bancário de forma autenticada, isolada por tenant e estritamente idempotente."""
    # Busca organização da integração antes de entrar no tenant_context (rota pública de webhook)
    integracao_raw = m.IntegracaoBancaria._base_manager.select_related('organization', 'conta').get(pk=integracao_id)
    if not integracao_raw.ativo:
        raise ValidationError('Integração bancária desativada.')
    if integracao_raw.webhook_token_hash:
        provided_hash = hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()
        if not hmac.compare_digest(provided_hash, integracao_raw.webhook_token_hash):
            raise PermissionDenied('Token do webhook bancário inválido.')

    with tenant_context(integracao_raw.organization), transaction.atomic():
        integracao = m.IntegracaoBancaria.objects.select_for_update().get(pk=integracao_raw.pk)
        evento_id = str(payload.get('evento_id') or payload.get('id') or '')[:120]
        if not evento_id:
            raise ValidationError('Webhook sem identificador de evento (evento_id).')
        ja_processado = m.WebhookBancarioEvento.objects.filter(integracao=integracao, evento_id=evento_id).first()
        if ja_processado:
            return ja_processado

        tipo_evento = str(payload.get('tipo_evento') or payload.get('event') or 'PAYMENT_RECEIVED')[:60]
        external_id = str(payload.get('external_id') or payload.get('payment_id') or '')
        txid = str(payload.get('txid') or '')
        cob = None
        if external_id:
            cob = m.CobrancaBancaria.objects.select_for_update().filter(integracao=integracao, external_id=external_id).first()
        if cob is None and txid:
            cob = m.CobrancaBancaria.objects.select_for_update().filter(integracao=integracao, txid=txid).first()

        operator = user or _resolver_operador_webhook(integracao.organization)
        if cob is not None and tipo_evento.upper() in ('PAGA', 'PAYMENT_RECEIVED', 'PAYMENT_CONFIRMED', 'LIQUIDADO', 'PIX_RECEIVED'):
            valor_pg = Decimal(str(payload.get('valor_pago') or cob.valor)).quantize(Decimal('.01'))
            dt_pg = payload.get('data_pagamento') or timezone.localdate()
            if isinstance(dt_pg, str):
                dt_pg = date.fromisoformat(dt_pg[:10])
            cob = _liquidar_cobranca_locked(operator, cob, valor_pago=valor_pg, data_pagamento=dt_pg)

        sanitized = {k: v for k, v in payload.items() if 'secret' not in k.lower() and 'token' not in k.lower()}
        evt = m.WebhookBancarioEvento(
            integracao=integracao,
            evento_id=evento_id,
            tipo_evento=tipo_evento,
            cobranca=cob,
            payload=sanitized,
            processado=True,
            processado_em=timezone.now(),
        )
        evt._service_write = True
        evt.save()
        return evt


@transaction.atomic
def simular_liquidacao_sandbox(user, cobranca_id: int) -> m.WebhookBancarioEvento:
    """Simula confirmação bancária em ambiente SANDBOX/SIMULADO sem sobrescrever token de webhook."""
    require(user, 'financeiro_escrever')
    cob = m.CobrancaBancaria.objects.select_for_update().select_related('integracao').get(pk=cobranca_id)
    if cob.integracao.ambiente != 'SANDBOX' and cob.integracao.provedor != 'SIMULADO':
        raise ValidationError('Simulação de liquidação permitida apenas em conta Sandbox ou Simulada.')
    cob = _liquidar_cobranca_locked(user, cob, valor_pago=cob.valor, data_pagamento=timezone.localdate())
    evt = m.WebhookBancarioEvento(
        integracao=cob.integracao,
        evento_id=f'SANDBOX-{uuid.uuid4().hex[:14]}',
        tipo_evento='SANDBOX_PAYMENT_RECEIVED',
        cobranca=cob,
        payload={'txid': cob.txid, 'valor_pago': str(cob.valor), 'sandbox': True},
        processado=True,
        processado_em=timezone.now(),
    )
    evt._service_write = True
    evt.save()
    return evt
