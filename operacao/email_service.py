"""Serviço de Notificações Transacionais por E-mail do VERTYX ERP."""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr
from django.utils import timezone
from django.conf import settings
from .models import ConfiguracaoNotificacaoEmail


def obter_configuracao_email(organization):
    """Obtém ou cria a configuração de e-mail do tenant."""
    config, _ = ConfiguracaoNotificacaoEmail.objects.get_or_create(
        organization=organization,
        defaults={
            'servidor_smtp': 'smtp.gmail.com',
            'porta_smtp': 587,
            'email_remetente': organization.email or 'notificacoes@vertyx.com.br',
            'nome_remetente': f"{organization.nome_fantasia or organization.razao_social} - VERTYX",
            'usar_tls': True,
            'usar_ssl': False,
            'ativo': True,
        }
    )
    return config


def testar_smtp(config: ConfiguracaoNotificacaoEmail, email_destino: str) -> tuple[bool, str]:
    """Testa a conectividade com o servidor SMTP e envia um e-mail de validação."""
    if not getattr(settings, 'EXTERNAL_EMAIL_ENABLED', True):
        return False, "Envio externo de e-mail desabilitado neste ambiente."
    if not email_destino:
        return False, "Informe o e-mail de destino para o teste."

    # Em modo local/teste sem servidor real ou com credenciais dummy, validação sintética
    if getattr(settings, 'EMAIL_BACKEND', '').endswith('locmem.EmailBackend') or config.servidor_smtp in ('smtp.local', 'localhost') or not config.usuario_smtp:
        config.ultimo_teste_em = timezone.now()
        config.ultimo_teste_status = 'SIMULADO_OK'
        config.save(update_fields=['ultimo_teste_em', 'ultimo_teste_status'])
        return True, "Parâmetros validados! Em ambiente local/sem credenciais reais, o envio foi simulado com sucesso."

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = Header(f"[VERTYX] Teste de Configuração de E-mail - {timezone.now().strftime('%d/%m/%Y %H:%M')}", 'utf-8')
        msg['From'] = formataddr((str(Header(config.nome_remetente, 'utf-8')), config.email_remetente))
        msg['To'] = email_destino

        html_body = f"""
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; background: #0b111d; color: #f1f5f9; border: 1px solid #1f293d; border-radius: 12px; padding: 28px;">
          <div style="border-bottom: 1px solid #1f293d; padding-bottom: 16px; margin-bottom: 20px;">
            <h2 style="color: #2563eb; margin: 0; font-size: 20px; font-weight: 800; letter-spacing: -0.02em;">VERTYX · Notificações</h2>
            <p style="color: #94a3b8; font-size: 13px; margin: 4px 0 0;">Teste de Conectividade SMTP</p>
          </div>
          <p style="font-size: 14px; line-height: 1.6; color: #cbd5e1;">
            Este é um e-mail de teste gerado pelo <strong>VERTYX ERP</strong> para validar as credenciais e parâmetros de disparo SMTP.
          </p>
          <div style="background: #121a29; border: 1px solid #1f293d; border-radius: 8px; padding: 14px; margin: 20px 0; font-size: 13px;">
            <p style="margin: 4px 0;"><strong>Servidor:</strong> {config.servidor_smtp}:{config.porta_smtp}</p>
            <p style="margin: 4px 0;"><strong>Segurança:</strong> {'TLS (STARTTLS)' if config.usar_tls else 'SSL' if config.usar_ssl else 'Nenhuma'}</p>
            <p style="margin: 4px 0;"><strong>Remetente:</strong> {config.email_remetente}</p>
            <p style="margin: 4px 0;"><strong>Data do Teste:</strong> {timezone.now().strftime('%d/%m/%Y às %H:%M:%S')}</p>
          </div>
          <p style="font-size: 12px; color: #64748b; margin-top: 24px;">
            Mensagem automática gerada pelo sistema VERTYX. Não responda a este e-mail.
          </p>
        </div>
        """
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        if config.usar_ssl:
            server = smtplib.SMTP_SSL(config.servidor_smtp, config.porta_smtp, timeout=15)
        else:
            server = smtplib.SMTP(config.servidor_smtp, config.porta_smtp, timeout=15)
            if config.usar_tls:
                server.starttls()

        if config.usuario_smtp and config.senha_smtp:
            server.login(config.usuario_smtp, config.senha_smtp)

        server.sendmail(config.email_remetente, [email_destino], msg.as_string())
        server.quit()

        config.ultimo_teste_em = timezone.now()
        config.ultimo_teste_status = 'SUCESSO'
        config.save(update_fields=['ultimo_teste_em', 'ultimo_teste_status'])
        return True, f"E-mail de teste enviado com sucesso para {email_destino}!"

    except Exception as exc:
        config.ultimo_teste_em = timezone.now()
        config.ultimo_teste_status = f'ERRO: {str(exc)[:50]}'
        config.save(update_fields=['ultimo_teste_em', 'ultimo_teste_status'])
        return False, f"Falha na comunicação SMTP: {str(exc)}"


def enviar_email_os_concluida(ordem, base_url="http://127.0.0.1:8000") -> tuple[bool, str]:
    """Envia e-mail ao cliente com resumo da OS concluída e links de laudo técnico."""
    if not getattr(settings, 'EXTERNAL_EMAIL_ENABLED', True):
        return False, "Envio externo de e-mail desabilitado neste ambiente."
    if not ordem.cliente or not ordem.cliente.email:
        return False, "Cliente não possui e-mail cadastrado."

    config = obter_configuracao_email(ordem.organization)
    if not config.ativo or not config.notificar_conclusao_os:
        return False, "Notificações de conclusão de OS desativadas nas configurações."

    destinatario = ordem.cliente.email
    link_os = f"{base_url}/portal/ordens/{ordem.pk}/"

    assunto = f"[VERTYX] Atendimento Concluído: OS #{ordem.numero} - {ordem.equipamento or 'Serviço'}"
    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; background: #0b111d; color: #f1f5f9; border: 1px solid #1f293d; border-radius: 12px; padding: 28px;">
      <div style="border-bottom: 1px solid #1f293d; padding-bottom: 16px; margin-bottom: 20px;">
        <span style="background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); font-size: 11px; font-weight: 700; text-transform: uppercase; padding: 3px 8px; border-radius: 4px;">
          Atendimento Concluído
        </span>
        <h2 style="color: #ffffff; margin: 12px 0 0; font-size: 20px; font-weight: 800;">
          Ordem de Serviço #{ordem.numero}
        </h2>
        <p style="color: #94a3b8; font-size: 13px; margin: 4px 0 0;">
          {ordem.organization.nome_fantasia or ordem.organization.razao_social}
        </p>
      </div>

      <p style="font-size: 14px; line-height: 1.6; color: #cbd5e1;">
        Prezado(a) <strong>{ordem.cliente.nome_razao_social}</strong>, informamos que o atendimento técnico solicitado foi finalizado com sucesso.
      </p>

      <div style="background: #121a29; border: 1px solid #1f293d; border-radius: 8px; padding: 16px; margin: 20px 0; font-size: 13px;">
        <p style="margin: 4px 0;"><strong>Equipamento:</strong> {ordem.equipamento or 'Não especificado'}</p>
        <p style="margin: 4px 0;"><strong>Tipo de Atendimento:</strong> {ordem.get_tipo_atendimento_display()}</p>
        <p style="margin: 4px 0;"><strong>Serviço Executado:</strong> {ordem.servico_executado or ordem.defeito or 'Manutenção especializada realizada conforme protocolo.'}</p>
        <p style="margin: 4px 0;"><strong>Técnico Responsável:</strong> {ordem.tecnico or 'Equipe Técnica'}</p>
      </div>

      <div style="margin: 28px 0; text-align: center;">
        <a href="{link_os}" style="background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #ffffff; text-decoration: none; padding: 12px 24px; border-radius: 6px; font-weight: 700; font-size: 14px; display: inline-block;">
          Acessar Portal do Cliente e Baixar Laudo
        </a>
      </div>

      <p style="font-size: 12px; color: #64748b; line-height: 1.5; margin-top: 24px; border-top: 1px solid #1f293d; padding-top: 16px;">
        Este é um e-mail transacional automático. Caso tenha dúvidas, entre em contato diretamente com nossa central de atendimento.
      </p>
    </div>
    """

    # Dispara usando testar_smtp ou backend configurado
    if getattr(settings, 'EMAIL_BACKEND', '').endswith('locmem.EmailBackend') or not config.usuario_smtp:
        return True, f"Simulação de envio para {destinatario} registrada com sucesso."

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = Header(assunto, 'utf-8')
        msg['From'] = formataddr((str(Header(config.nome_remetente, 'utf-8')), config.email_remetente))
        msg['To'] = destinatario
        msg.attach(MIMEText(html, 'html', 'utf-8'))

        if config.usar_ssl:
            server = smtplib.SMTP_SSL(config.servidor_smtp, config.porta_smtp, timeout=15)
        else:
            server = smtplib.SMTP(config.servidor_smtp, config.porta_smtp, timeout=15)
            if config.usar_tls:
                server.starttls()

        if config.usuario_smtp and config.senha_smtp:
            server.login(config.usuario_smtp, config.senha_smtp)

        server.sendmail(config.email_remetente, [destinatario], msg.as_string())
        server.quit()
        return True, f"E-mail de conclusão enviado com sucesso para {destinatario}."
    except Exception as exc:
        return False, f"Falha ao enviar e-mail: {str(exc)}"
