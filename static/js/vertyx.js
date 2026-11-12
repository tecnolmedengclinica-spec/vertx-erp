if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register(document.body.classList.contains('vx-technician')?'/tecnico/sw.js':location.pathname.startsWith('/pdv/')?'/pdv/sw.js':location.pathname.startsWith('/vendedor/')?'/vendedor/sw.js':'/sw.js').catch(() => {
  const status=document.querySelector('[data-install-status]');
  if(status)status.textContent='Não foi possível ativar o service worker. Recarregue com conexão e confira o painel Application do navegador. As vendas continuam exigindo conexão.';
});
// Capture the install event before DOMContentLoaded; some browsers emit it early.
let vertyxInstallPrompt;
addEventListener('beforeinstallprompt',event=>{event.preventDefault();vertyxInstallPrompt=event;document.querySelectorAll('[data-install]').forEach(button=>button.hidden=false);});
/* Comportamentos compartilhados; nenhum dado empresarial é guardado em cache. */
document.addEventListener('DOMContentLoaded',()=>{
  const connection=document.querySelector('[data-connection-status]');
  if(connection){
    const report=()=>{connection.dataset.offline=String(!navigator.onLine);connection.textContent=navigator.onLine?'Conexão disponível. Alterações são confirmadas ao salvar.':'Sem conexão. Nenhuma alteração será enviada ou sincronizada automaticamente. Reconecte antes de salvar.';};
    report();addEventListener('online',report);addEventListener('offline',report);
    document.addEventListener('submit',event=>{if(!navigator.onLine){event.preventDefault();report();connection.scrollIntoView({block:'nearest'});}},true);
  }
  const sidebarToggles=document.querySelectorAll('[data-sidebar-toggle]');
  if(sidebarToggles.length){
    const apply=(collapsed)=>{
      document.body.classList.toggle('vx-sidebar-collapsed',collapsed);
      sidebarToggles.forEach(btn=>{
        btn.setAttribute('aria-expanded',String(!collapsed));
        btn.setAttribute('aria-label',collapsed?'Expandir menu lateral':'Recolher menu lateral');
        btn.setAttribute('title',collapsed?'Expandir menu lateral':'Recolher menu lateral');
      });
    };
    try{apply(localStorage.getItem('vertyx.sidebarCollapsed')==='true');}catch{}
    sidebarToggles.forEach(btn=>{
      btn.addEventListener('click',()=>{
        const collapsed=!document.body.classList.contains('vx-sidebar-collapsed');
        apply(collapsed);
        try{localStorage.setItem('vertyx.sidebarCollapsed',String(collapsed));}catch{}
      });
    });
  }
  document.querySelectorAll('.navbar-nav a').forEach(a=>{
    const curPath=location.pathname;
    if(a.pathname===curPath || (a.pathname!=='/' && a.pathname!=='/assistencia/' && a.pathname.length > 2 && curPath.startsWith(a.pathname))){
      a.classList.add('active');
      a.setAttribute('aria-current','page');
      const details=a.closest('details');
      if(details){details.open=true;}
    }
  });
  document.querySelectorAll('.sv-bottom a').forEach(a=>{if(a.pathname===location.pathname || (a.pathname==='/pdv/' && location.pathname.startsWith('/pdv/')))a.setAttribute('aria-current','page');});
  if(document.body.classList.contains('vx-seller')&&location.hash==='#abrir-meu-caixa'){const panel=document.getElementById('abrir-meu-caixa');if(panel){panel.open=true;panel.scrollIntoView({block:'center'});}}
  document.querySelectorAll('form').forEach(form=>form.addEventListener('submit',event=>{
    if(!form.checkValidity())return;
    const button=event.submitter;
    if(button&&!form.classList.contains('os-geo-form')){button.classList.add('vx-loading');button.setAttribute('aria-busy','true');}
  }));
  document.querySelectorAll('[data-confirm]').forEach(button=>button.addEventListener('click',event=>{if(!confirm(button.dataset.confirm))event.preventDefault();}));
  const install=document.querySelector('[data-install]');
  if(install){install.hidden=!vertyxInstallPrompt;install.addEventListener('click',async()=>{if(vertyxInstallPrompt){await vertyxInstallPrompt.prompt();vertyxInstallPrompt=null;install.hidden=true;}});}
  const installStatus=document.querySelector('[data-install-status]');
  if(installStatus&&!window.isSecureContext)installStatus.textContent='A instalação exige HTTPS ou localhost. Este endereço não é um contexto seguro.';
});
// A restored browser page must reauthorize against the current login/tenant.
addEventListener('pageshow',event=>{if(event.persisted)location.reload();});

// Logout/tenant switching lock the offline identity; pending intents are retained.
document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!globalThis.PDVOutbox||form.dataset.pdvIdentityLocked)return;
  // A control named "action" shadows HTMLFormElement.action in commerce forms.
  const action=form.getAttribute('action')||location.pathname;
  if(action.includes('/logout')||action.includes('/organizacao/')){
    event.preventDefault();
    try{await PDVOutbox.state.put('active',null);}catch(_){}
    form.dataset.pdvIdentityLocked='true';form.requestSubmit();
  }
});

// Global Vertyx Theme Management (Dark / Light Mode)
window.getVertyxTheme = function() {
  try {
    return localStorage.getItem('vertyx_theme') || 'dark';
  } catch (e) {
    return 'dark';
  }
};

window.applyVertyxTheme = function(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try {
    localStorage.setItem('vertyx_theme', theme);
  } catch (e) {}

  document.querySelectorAll('.vx-theme-toggle-btn').forEach(btn => {
    const label = btn.querySelector('.vx-theme-label');
    if (label) {
      label.textContent = theme === 'dark' ? 'Modo Claro' : 'Modo Escuro';
    }
  });

  window.dispatchEvent(new CustomEvent('vertyxThemeChanged', { detail: { theme: theme } }));
};

window.toggleVertyxTheme = function() {
  const current = document.documentElement.getAttribute('data-theme') || window.getVertyxTheme();
  const next = current === 'light' ? 'dark' : 'light';
  window.applyVertyxTheme(next);
};

(() => {
  const current = window.getVertyxTheme();
  document.documentElement.setAttribute('data-theme', current);
  document.addEventListener('DOMContentLoaded', () => {
    window.applyVertyxTheme(window.getVertyxTheme());
  });
})();
