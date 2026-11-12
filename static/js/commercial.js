window.toggleCommercialTheme = function() {
  const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try {
    localStorage.setItem('vertyx_theme', next);
  } catch(e) {}
  window.dispatchEvent(new CustomEvent('vertyxThemeChanged', { detail: { theme: next } }));
};

document.addEventListener('DOMContentLoaded',()=>{
  const toggle=document.querySelector('[data-menu-toggle]');
  const menu=document.querySelector('#public-menu');
  if(toggle&&menu){
    const setMenu=open=>{toggle.setAttribute('aria-expanded',String(open));toggle.setAttribute('aria-label',open?'Fechar menu':'Abrir menu');menu.classList.toggle('is-open',open);};
    toggle.addEventListener('click',()=>setMenu(toggle.getAttribute('aria-expanded')!=='true'));
    menu.querySelectorAll('a').forEach(link=>link.addEventListener('click',()=>setMenu(false)));
    document.addEventListener('keydown',event=>{if(event.key==='Escape'){const mobileWasOpen=toggle.getAttribute('aria-expanded')==='true';setMenu(false);if(mobileWasOpen)toggle.focus();}});
    matchMedia('(min-width:1101px)').addEventListener('change',event=>{if(event.matches)setMenu(false)});
  }
  const form=document.querySelector('.demo-form');
  if(form)form.addEventListener('submit',event=>{
    if(!form.checkValidity())return;
    const button=event.submitter;if(button){button.setAttribute('aria-busy','true');button.textContent='Enviando solicitação…';}
  });

  // Cards de planos interativos / tocáveis
  document.querySelectorAll('.plan-card[data-cta]').forEach(card => {
    // Efeito tátil imediato ao tocar ou pressionar
    card.addEventListener('pointerdown', (event) => {
      if (!event.target.closest('a') && !event.target.closest('button')) {
        card.classList.add('is-pressed');
      }
    });

    const removePressed = () => card.classList.remove('is-pressed');
    card.addEventListener('pointerup', removePressed);
    card.addEventListener('pointercancel', removePressed);
    card.addEventListener('pointerleave', removePressed);

    card.addEventListener('click', (event) => {
      // Se clicou diretamente em um link ou botão interno (ex: "Solicitar demonstração"), segue o link clicado
      if (event.target.closest('a') || event.target.closest('button')) {
        return;
      }
      const cta = card.getAttribute('data-cta');
      if (cta) {
        card.classList.add('is-pressed');
        window.location.href = cta;
      }
    });

    card.addEventListener('keydown', (event) => {
      if ((event.key === 'Enter' || event.key === ' ') && !event.target.closest('a') && !event.target.closest('button')) {
        event.preventDefault();
        const cta = card.getAttribute('data-cta');
        if (cta) {
          card.classList.add('is-pressed');
          window.location.href = cta;
        }
      }
    });
  });
});
